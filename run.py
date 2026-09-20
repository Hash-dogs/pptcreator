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
from pptgen.qa import geometry, visual                     # noqa: E402

SAMPLES = os.path.join(ROOT, 'out', 'samples')
REPORTS = os.path.join(ROOT, 'out', 'reports')
VISUAL = os.path.join(ROOT, 'out', 'visual')
PLANS = os.path.join(ROOT, 'out', 'plans')


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
    doc = parse_mod.parse(_abs(args.src))
    out = _abs(args.out or os.path.join(PLANS, _stem(args.src) + '.parsed.json'))
    pipeline.save_json(doc, out)
    print('[parse] %s（%s）→ %s' % (doc.get('title'), doc['kind'], out))
    print('[parse] %d 个内容块 / %d 字' % (doc['stats']['blocks'], doc['stats']['chars']))
    kinds = {}
    for b in doc['blocks']:
        kinds[b['type']] = kinds.get(b['type'], 0) + 1
    print('[parse] 块类型分布: %s' % kinds)
    return out


def cmd_outline(args):
    cfg_mod.load_env()
    if args.parsed:
        doc = pipeline.load_json(_abs(args.parsed))
    else:
        doc = parse_mod.parse(_abs(args.src))
    outline = pipeline.make_outline(doc)
    stem = _stem(args.src or doc['source'])
    jpath = _abs(args.out or os.path.join(PLANS, stem + '.outline.json'))
    pipeline.save_json(outline, jpath)
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
    return out


def cmd_build(args):
    cfg_mod.load_env()
    if args.spec:
        spec = pipeline.load_json(_abs(args.spec))
    else:
        spec = _content(args.content)
    stem = args.name or (_stem(args.spec) if args.spec else args.content)
    out = _abs(args.out or os.path.join(SAMPLES, '%s.pptx' % stem))
    build_mod.build(spec, args.template or cfg_mod.template_path(), out, fill_toc=True)
    print('[build] %s（%d 页正文 + 公司封面/目录/封底）' % (out, len(spec['slides'])))
    print('[build] 结构自检通过')
    return out


def cmd_qa(args):
    cfg_mod.load_env()
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
    # .md 而不是 .txt：本机 DLP 会按扩展名加密 .txt
    with open(os.path.join(REPORTS, stem + '.geometry.md'), 'w', encoding='utf-8') as f:
        f.write('# 几何 QA 报告\n\n```\n' + geometry.format_report(rep) + '\n```\n')
    return rep


def cmd_render(args):
    cfg_mod.load_env()
    path = _abs(args.file) if args.file else _latest_pptx()
    stem = _stem(path)
    rp = os.path.join(REPORTS, stem + '.geometry.json')
    rep = pipeline.load_json(rp) if os.path.isfile(rp) else None
    res = visual.run(path, os.path.join(VISUAL, stem), geometry_report=rep)
    print(visual.format_report(res))
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

    print('[repair] 开始（最多 %d 轮）' % rounds)
    deck, final = repair_mod.repair_deck(deck, build_qa, rounds=rounds)
    print('[repair] %s' % repair_mod.repair_summary(state['before'] or {}, final))

    spec_out = os.path.splitext(_abs(args.spec))[0] + '.repaired.json'
    pipeline.save_json(deck, spec_out)
    print('[repair] 修正后的 spec → %s' % spec_out)
    print('[repair] 成品 → %s' % out_pptx)
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
    {'config': cmd_config, 'parse': cmd_parse, 'outline': cmd_outline,
     'plan': cmd_plan, 'build': cmd_build, 'repair': cmd_repair,
     'full': cmd_full, 'qa': cmd_qa, 'render': cmd_render,
     'all': cmd_all, 'auto': cmd_auto}[args.cmd](args)


if __name__ == '__main__':
    main()
