#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""文档转 PPT 生成器 —— 统一入口。

完整链路（人工确认卡在大纲那一步）::

    python run.py config                        # 看配置状态
    python run.py parse  --src 文档.pptx         # ① 解析源文档
    python run.py outline --src 文档.pptx        # ② 生成大纲 → out/plans/*.outline.json + .md
    #   ← 打开 .outline.json 改，改完继续
    python run.py plan   --outline out/plans/xxx.outline.json   # ③ 规划每页版式与内容
    python run.py build  --spec    out/plans/xxx.deck.json      # ④ 渲染 pptx
    python run.py qa                            # ⑤ 几何 + 结构检查
    python run.py render                        # ⑥ 渲染 + AI 看图

    也可以一步走：python run.py auto --src 文档.pptx     # 做到大纲就停，等你确认

其他::

    python run.py build --content dify           # 用内置示例内容
    python run.py all                            # build → qa → render
"""
from __future__ import annotations
import argparse
import importlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'src'))

# Windows 控制台默认 GBK，中文输出会抛 UnicodeEncodeError。这里强制 UTF-8，
# 免得每次都要用户手动设 PYTHONIOENCODING。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

from pptgen import build as build_mod                      # noqa: E402
from pptgen import config as cfg_mod                       # noqa: E402
from pptgen import parse as parse_mod                      # noqa: E402
from pptgen import pipeline                                # noqa: E402
from pptgen import repair as repair_mod                    # noqa: E402
from pptgen import runlog                                  # noqa: E402
from pptgen.qa import geometry, visual                     # noqa: E402

# 目录常量统一走 config.out_sub()（server.py 用的是同一份）——
# 以前这两处各自硬编码一份 `out/<sub>`，`PPTGEN_OUT` 因此成了死配置。
#
# 但 `out_dir()` 是**调用时读 env** 的，而这些常量在 import 期就求值了 ——
# 不先把 .env 读进来，`PPTGEN_OUT` 改了也不会生效（会静默退回默认的 'out'）。
# `load_env` 幂等且 override=False，main() 里再调一次无害。
cfg_mod.load_env()
SAMPLES = cfg_mod.out_sub('samples')
REPORTS = cfg_mod.out_sub('reports')
VISUAL = cfg_mod.out_sub('visual')
PLANS = cfg_mod.out_sub('plans')


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0][:40]


def _content(name: str):
    mod = importlib.import_module('pptgen.content_%s' % name)
    return dict(slides=[dict(s) for s in mod.SLIDES], toc=list(mod.TOC))


def _abs(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


# ══════════════════════════════════════════════════════════════
def cmd_config(args):
    cfg_mod.load_env()
    print(cfg_mod.summary())


def cmd_parse(args):
    cfg_mod.load_env()
    with runlog.stage('parse') as st:
        doc = parse_mod.parse(_abs(args.src))
        out = _abs(args.out or os.path.join(PLANS, _stem(args.src) + '.parsed.json'))
        pipeline.save_json(doc, out)
        print('[parse] %s（%s）→ %s' % (doc.get('title'), doc['kind'], out))
        print('[parse] %d 个内容块 / %d 字' % (doc['stats']['blocks'], doc['stats']['chars']))
        kinds = {}
        for b in doc['blocks']:
            kinds[b['type']] = kinds.get(b['type'], 0) + 1
        print('[parse] 块类型分布: %s' % kinds)
        sk = doc.get('structure') or {}
        st.update(blocks=doc['stats']['blocks'], chars=doc['stats']['chars'])
        runlog.note('document', title=doc.get('title'), kind=doc.get('kind'),
                    blocks=doc['stats']['blocks'], chars=doc['stats']['chars'],
                    blocks_by_type=kinds, parsed_json=out,
                    structure={'method': sk.get('method'),
                               'chapters': len(sk.get('chapters') or []),
                               'chapter_names': [c['name'] for c in sk.get('chapters') or []],
                               'pages': sum(len(c['pages']) for c in sk.get('chapters') or [])})
        runlog.attach_source(_abs(args.src))
    return out


def cmd_outline(args):
    cfg_mod.load_env()
    if args.parsed:
        doc = pipeline.load_json(_abs(args.parsed))
    else:
        doc = parse_mod.parse(_abs(args.src))
    with runlog.stage('outline') as st:
        outline = pipeline.make_outline(doc)
        meta = outline.get('_meta') or {}
        st.update(pages=outline.get('page_count'), sections=len(outline.get('sections') or []))
        runlog.note('outline', generated_by=meta.get('generated_by'),
                    model=meta.get('model'), sections=len(outline.get('sections') or []),
                    pages=outline.get('page_count'),
                    page_range=outline.get('_page_range'),
                    chapter_names=[s['name'] for s in outline.get('sections') or []],
                    warnings=meta.get('warnings') or [])
    stem = _stem(args.src or doc['source'])
    # 把刚解析出来的 doc **一并落盘**：下一步 `plan` 从 parsed.json 读锚点，
    # 而解析结果里带着结构骨架。不写的话 `plan` 会读到上一次的旧文件
    # （甚至是没有 structure 的老格式），锚点全部落空且不报错。
    pipeline.save_json(doc, _abs(os.path.join(PLANS, stem + '.parsed.json')))
    jpath = _abs(args.out or os.path.join(PLANS, stem + '.outline.json'))
    pipeline.save_json(outline, jpath)
    runlog.attach_file('outline_json', jpath)
    mpath = os.path.splitext(jpath)[0] + '.md'
    with open(mpath, 'w', encoding='utf-8') as f:
        f.write(pipeline.outline_preview(outline) + '\n')
    print('[outline] %d 页正文（目标 %s）' % (outline['page_count'], outline['_page_range']))
    print('[outline] %s' % jpath)
    print('[outline] %s  ← 打开它审阅/修改' % mpath)
    print()
    print(pipeline.outline_preview(outline))
    return jpath


def cmd_plan(args):
    cfg_mod.load_env()
    outline = pipeline.load_json(_abs(args.outline))
    base = os.path.splitext(_abs(args.outline))[0]
    doc_path = args.parsed or outline.get('_doc')
    if doc_path and os.path.isfile(_abs(doc_path)):
        doc = pipeline.load_json(_abs(doc_path))
    else:
        # 从大纲文件名反推 parsed.json（<stem>.outline.json → <stem>.parsed.json）
        cand = base.replace('.outline', '.parsed') + '.json'
        if os.path.isfile(cand):
            doc = pipeline.load_json(cand)
        else:
            raise SystemExit('找不到解析结果 %s，请加 --parsed 指定，或先跑 parse' % cand)
    with runlog.stage('plan') as st:
        plan = pipeline.make_plan(outline, doc)
        # <stem>.outline.json → <stem>.deck.json（去掉 .outline 再拼）
        out = _abs(args.out or (base[:-len('.outline')] if base.endswith('.outline') else base)
                   + '.deck.json')
        pipeline.save_json(plan, out)
        counts = {}
        for s in plan['slides']:
            counts[s['layout']] = counts.get(s['layout'], 0) + 1
        print('[plan] %d 页 → %s' % (len(plan['slides']), out))
        print('[plan] 版式分布: %s' % counts)
        st.update(slides=len(plan['slides']))
        runlog.note('plan', slides=len(plan['slides']), layouts=counts,
                    generated_by=plan.get('_generated_by'), deck_json=out)
    return out


def cmd_build(args):
    cfg_mod.load_env()
    if args.spec:
        spec = pipeline.load_json(_abs(args.spec))
    else:
        spec = _content(args.content)
    stem = args.name or (_stem(args.spec) if args.spec else args.content)
    out = _abs(args.out or os.path.join(SAMPLES, '%s.pptx' % stem))
    with runlog.stage('build') as st:
        build_mod.build(spec, args.template or cfg_mod.template_path(), out, fill_toc=True)
        print('[build] %s（%d 页正文 + 公司封面/目录/封底）' % (out, len(spec['slides'])))
        print('[build] 结构自检通过')
        st.update(slides=len(spec['slides']))
        # 只在 build **成功**之后归档：build() 是先 save 再 selfcheck，
        # 自检不过时磁盘上已经有一个不合格的 pptx，别把它当成品记下来。
        runlog.attach_pptx(out)
    return out


def cmd_qa(args):
    cfg_mod.load_env()
    with runlog.stage('qa') as st:
        return _qa(args, st)


def _qa(args, st):
    path = _abs(args.file) if args.file else _latest_pptx()
    os.makedirs(REPORTS, exist_ok=True)
    rep = geometry.analyse(path)
    struct = build_mod.selfcheck(path)
    if struct:
        rep['issues'] = [dict(severity='error', slide=0, shape='-',
                              kind='structure', detail=m) for m in struct] + rep['issues']
        rep['summary']['error'] += len(struct)
        rep['summary']['total'] += len(struct)
    print(geometry.format_report(rep))
    stem = _stem(path)
    pipeline.save_json(rep, os.path.join(REPORTS, stem + '.geometry.json'))
    with open(os.path.join(REPORTS, stem + '.geometry.md'), 'w', encoding='utf-8') as f:
        f.write('# 几何 QA 报告\n\n```\n' + geometry.format_report(rep) + '\n```\n')
    s = rep['summary']
    st.update(error=s['error'], warn=s['warn'])
    runlog.note('qa', error=s['error'], warn=s['warn'], issues=rep['issues'][:20],
                # 无参数跑 qa 时是按 mtime 挑的产物，记下来才知道「检查的到底是哪一份」
                checked=path)
    return rep


def cmd_render(args):
    cfg_mod.load_env()
    with runlog.stage('render') as st:
        return _render(args, st)


def _render(args, st):
    path = _abs(args.file) if args.file else _latest_pptx()
    stem = _stem(path)
    rp = os.path.join(REPORTS, stem + '.geometry.json')
    rep = pipeline.load_json(rp) if os.path.isfile(rp) else None
    res = visual.run(path, os.path.join(VISUAL, stem), geometry_report=rep)
    print(visual.format_report(res))
    st.update(mode=res.get('mode'), pages=len(res.get('verdicts') or []))
    runlog.note('render', mode=res.get('mode'), rendered=path,
                contact_sheet=res.get('sheet'),
                visual_dir=os.path.join(VISUAL, stem),
                # 有没有读到几何报告会影响行为（geometry_report=None 时不做细看）
                geometry_report_found=rep is not None)
    return res


def cmd_repair(args):
    """build → qa → 让模型压文案 → 再 build，直到几何检查干净。"""
    cfg_mod.load_env()
    deck = pipeline.load_json(_abs(args.spec))
    stem = args.name or _stem(args.spec).replace('.deck', '')
    out_pptx = _abs(args.out or os.path.join(SAMPLES, stem + '.pptx'))
    rounds = args.rounds or 3

    state = {'before': None}

    def build_qa(spec):
        build_mod.build(spec, args.template or cfg_mod.template_path(),
                        out_pptx, fill_toc=True)
        rep = geometry.analyse(out_pptx)
        if state['before'] is None:
            state['before'] = rep
        s = rep['summary']
        print('  → %d error / %d warn' % (s['error'], s['warn']))
        return rep

    with runlog.stage('repair') as st:
        print('[repair] 开始（最多 %d 轮）' % rounds)
        deck, final = repair_mod.repair_deck(deck, build_qa, rounds=rounds)
        print('[repair] %s' % repair_mod.repair_summary(state['before'] or {}, final))

        spec_out = os.path.splitext(_abs(args.spec))[0] + '.repaired.json'
        pipeline.save_json(deck, spec_out)
        print('[repair] 修正后的 spec → %s' % spec_out)
        print('[repair] 成品 → %s' % out_pptx)
        before, after = (state['before'] or {}).get('summary', {}), final.get('summary', {})
        st.update(rounds=rounds)
        runlog.note('repair', rounds=rounds,
                    before={'error': before.get('error'), 'warn': before.get('warn')},
                    after={'error': after.get('error'), 'warn': after.get('warn')},
                    repaired_json=spec_out)
        runlog.attach_pptx(out_pptx)
    return out_pptx


def cmd_full(args):
    """一条命令走完：大纲 → 规划 → 修复回环 → 渲染。"""
    cfg_mod.load_env()
    print(cfg_mod.summary())
    print()
    parsed = cmd_parse(argparse.Namespace(src=args.src, out=None))
    print()
    outline_path = cmd_outline(argparse.Namespace(src=args.src, parsed=parsed, out=None))
    if not args.yes:
        print()
        print('─' * 60)
        print('大纲已生成，请先审阅：%s' % outline_path)
        print('确认无误后重跑并加 --yes 继续，或手动执行：')
        print('  python run.py plan --outline %s' % outline_path)
        return
    print()
    deck_path = cmd_plan(argparse.Namespace(outline=outline_path, parsed=parsed, out=None))
    print()
    name = args.name or _stem(args.src)
    out_pptx = cmd_repair(argparse.Namespace(spec=deck_path, name=name,
                                             out=None, rounds=args.rounds,
                                             template=None))
    print()
    cmd_qa(argparse.Namespace(file=out_pptx))
    cmd_render(argparse.Namespace(file=out_pptx))


def cmd_all(args):
    args.file = cmd_build(args)
    cmd_qa(args)
    cmd_render(args)


def cmd_auto(args):
    """parse → outline，停在大纲等人工确认。"""
    cfg_mod.load_env()
    print(cfg_mod.summary())
    print()
    parsed = cmd_parse(argparse.Namespace(src=args.src, out=None))
    print()
    outline = cmd_outline(argparse.Namespace(src=args.src, parsed=parsed, out=None))
    print()
    print('─' * 60)
    print('下一步：审阅并修改 %s' % outline)
    print('确认后执行：python run.py plan --outline %s' % outline)


def _latest_pptx() -> str:
    if os.path.isdir(SAMPLES):
        cands = [os.path.join(SAMPLES, f) for f in os.listdir(SAMPLES)
                 if f.endswith('.pptx')]
        if cands:
            return max(cands, key=os.path.getmtime)
    raise SystemExit('out/samples 下没有产物，先跑 python run.py build')


def main():
    ap = argparse.ArgumentParser(description='文档转 PPT 生成器')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('config', help='查看配置状态')

    p = sub.add_parser('parse', help='解析源文档')
    p.add_argument('--src', required=True)
    p.add_argument('--out')

    p = sub.add_parser('outline', help='生成大纲（人工确认的落点）')
    p.add_argument('--src')
    p.add_argument('--parsed')
    p.add_argument('--out')

    p = sub.add_parser('plan', help='大纲 → 每页版式与内容')
    p.add_argument('--outline', required=True)
    p.add_argument('--parsed')
    p.add_argument('--out')

    p = sub.add_parser('build', help='渲染 pptx')
    p.add_argument('--spec')
    p.add_argument('--content', default='dify')
    p.add_argument('--out')
    p.add_argument('--name')
    p.add_argument('--template')

    p = sub.add_parser('qa', help='几何 + 结构检查')
    p.add_argument('--file')

    p = sub.add_parser('render', help='渲染 + AI 看图')
    p.add_argument('--file')

    p = sub.add_parser('all', help='build → qa → render')
    p.add_argument('--spec')
    p.add_argument('--content', default='dify')
    p.add_argument('--out')
    p.add_argument('--name')
    p.add_argument('--template')
    p.add_argument('--file')

    p = sub.add_parser('auto', help='parse → outline，停在大纲等确认')
    p.add_argument('--src', required=True)

    p = sub.add_parser('repair', help='build → qa → 压文案的修复回环')
    p.add_argument('--spec', required=True)
    p.add_argument('--name')
    p.add_argument('--out')
    p.add_argument('--rounds', type=int, default=3)
    p.add_argument('--template')

    p = sub.add_parser('full', help='大纲 → 规划 → 修复 → 渲染 一条命令走完')
    p.add_argument('--src', required=True)
    p.add_argument('--name')
    p.add_argument('--rounds', type=int, default=3)
    p.add_argument('--yes', action='store_true',
                   help='跳过人工确认直接往下走（默认会在大纲处停下）')

    args = ap.parse_args()
    # 顺序有讲究：`load_env` 必须在 scope 之前 —— 目录常量是调用时读 env 的
    # （见文件顶部 out_sub 那段注释）。`config` 是唯一既不消费也不产出任何东西的
    # 命令，给它建文件夹纯属噪音，所以不进 scope。
    cfg_mod.load_env()
    if args.cmd == 'config':
        return cmd_config(args)
    with runlog.scope(_task_name(args), origin='cli', command=args.cmd) as rl:
        rl.put('config', runlog.config_snapshot())
        with runlog.tee(rl):
            {'config': cmd_config, 'parse': cmd_parse, 'outline': cmd_outline,
             'plan': cmd_plan, 'build': cmd_build, 'repair': cmd_repair,
             'full': cmd_full, 'qa': cmd_qa, 'render': cmd_render,
             'all': cmd_all, 'auto': cmd_auto}[args.cmd](args)
        if rl.dir:
            print()
            print('[log] %s' % rl.dir)


# 中间产物的后缀，别让它混进任务名（`x.outline.json` → `x`）
_STAGE_SUFFIX = re.compile(r'\.(parsed|outline|deck|repaired)$')


def _task_name(args) -> str:
    """日志文件夹里那个「任务名」。

    优先用用户显式给的名字（`--name`），否则用源文件 / 输入产物的 stem ——
    文档的身份比 pptx 的标签更贴近「这次流程在做什么」。
    """
    if getattr(args, 'name', None):
        return str(args.name)
    for attr in ('src', 'spec', 'file', 'outline', 'parsed'):
        v = getattr(args, attr, None)
        if v:
            # `out/plans/x.outline.json` → `x`，而不是 `x.outline`
            return _STAGE_SUFFIX.sub('', _stem(v))
    # `qa` / `render` 不带 --file 时按 mtime 挑产物，名字要跟着那份产物走 ——
    # 否则一摞文件夹全叫 `run`，事后根本认不出哪个是查哪一份的。
    if getattr(args, 'cmd', '') in ('qa', 'render'):
        try:
            return _stem(_latest_pptx())
        except SystemExit:
            pass
    return 'run'


if __name__ == '__main__':
    main()
