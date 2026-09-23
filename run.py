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
import copy
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
from pptgen import layout_spec                             # noqa: E402
from pptgen import layout_store                            # noqa: E402
from pptgen import parse as parse_mod                      # noqa: E402
from pptgen import pipeline                                # noqa: E402
from pptgen import recognize as recognize_mod              # noqa: E402
from pptgen import repair as repair_mod                    # noqa: E402
from pptgen import revise as revise_mod                    # noqa: E402
from pptgen import runlog                                  # noqa: E402
from pptgen import samples                                 # noqa: E402
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
    # `TITLE` 是选填的：带上它封面才有标题（`build.fill_cover` 靠 spec['title']）。
    return dict(slides=[dict(s) for s in mod.SLIDES], toc=list(mod.TOC),
                title=getattr(mod, 'TITLE', ''))


def _abs(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


# ══════════════════════════════════════════════════════════════
def cmd_config(args):
    cfg_mod.load_env()
    print(cfg_mod.summary())


def _layouts_table() -> str:
    L = ['版式库（%d 套，其中自定义 %d 套）' % (len(layout_spec.names()),
                                        len([n for n in layout_spec.names()
                                             if layout_spec.REGISTRY[n].source == 'custom'])),
         '  %-24s %-6s %-6s %-10s %s' % ('名字', '来源', '状态', '分类', '容量 / 特征')]
    for name in layout_spec.names():
        i = layout_store.info(name)
        L.append('  %-24s %-6s %-6s %-10s %s'
                 % (name, '自定义' if i['source'] == 'custom' else '内置',
                    '启用' if i['enabled'] else '禁用',
                    i['intent_label'], i['items'] or (i['signature'] or '')[:24]))
    return '\n'.join(L)


def _layouts_check() -> int:
    """自定义版式的样例过一遍「试片闸门」（几何 + 截断）。

    内置 19 套的回归在 `tests/test_layouts.py` 里；**自定义版式没有那份测试**，
    所以给它们一条能随时跑的自检命令 —— 手工改过 JSON、或者从别处拷来一份
    版式之后，用它确认还没坏。
    """
    names = [n for n in layout_spec.names()
             if layout_spec.REGISTRY[n].source == 'custom']
    if not names:
        print('版式库里还没有自定义版式（内置 19 套的回归在 tests/ 里）。')
        return 0
    bad = 0
    work = os.path.join(VISUAL, 'layouts_check')
    for name in names:
        if not layout_spec.is_enabled(name):
            print('— %s（已禁用，跳过）' % name)
            continue
        meta = None
        for m in layout_store.list_metas():
            if m.get('name') == name:
                meta = m
                break
        if not meta or not isinstance(meta.get('sample'), dict):
            print('✗ %s：没有样例（sample），无法自检' % name)
            bad += 1
            continue
        try:
            trial = recognize_mod.render_trial(meta, os.path.join(work, name))
            problems = recognize_mod.gate(trial)
        except Exception as e:                       # noqa: BLE001
            print('✗ %s：渲染失败 %s' % (name, e))
            bad += 1
            continue
        rep = trial['geometry']['summary']
        if problems:
            print('✗ %s：%s' % (name, '；'.join(problems[:3])))
            bad += 1
        else:
            print('✓ %s（几何 %d error / %d warn，无截断）'
                  % (name, rep['error'], rep['warn']))
    print()
    print('自检结果：%d 套有问题' % bad if bad else '自检结果：全部通过')
    if bad:
        # 退出码要能传出去（CI / 脚本里判断用）—— main() 不接返回值，只能 SystemExit
        raise SystemExit(1)


def _layouts_add(args) -> int:
    if cfg_mod.vision_config() is None:
        raise SystemExit('未配置视觉模型（PPTGEN_VISION_*），无法识别截图。\n'
                         '识别要用**看得见图**的模型，见 .env.example 的 2.4 节。')
    img = _abs(args.add_image)
    if not os.path.isfile(img):
        raise SystemExit('图片不存在：%s' % img)
    print('识别中…（调用视觉模型，可能要十几秒）')
    draft = recognize_mod.recognize(img, os.path.join(VISUAL, 'layouts_cli'),
                                    on_log=lambda m: print('  ' + m))
    if not draft['ok']:
        print()
        print('不合格，未入库：%s' % draft['reason'])
        for a in draft['attempts']:
            print('  第 %d 轮：%s' % (a['round'], '；'.join(a['problems'])))
        raise SystemExit(1)
    meta = draft['meta']
    print()
    print('识别通过：%s' % meta['name'])
    if draft['attempts']:
        # 重试过就要说出来 —— 静默重试与静默截断是同一类问题（本仓库反复在修）。
        # 它也是**调提示词的线索**：如果每一张图的第一轮都栽在同一件事上，
        # 该改的是提示词而不是让模型多试几次。
        for a in draft['attempts']:
            print('  ⚠ 第 %d 轮不合格（已自动修正）：%s'
                  % (a['round'], '；'.join(a['problems'][:2])))
    print('  视觉特征：%s' % meta['signature'])
    print('  分类    ：%s' % '、'.join(meta['intents']) or '—')
    print('  容量    ：%s–%s 条，单条 ≤%s 字'
          % (meta['min_items'], meta['max_items'], meta['item_chars']))
    print('  试片    ：%s' % ((draft['trial'] or {}).get('png') or '（未渲染）'))
    if not args.adopt:
        print()
        print('加 `--adopt` 就把它写进版式库；不加则只是看一眼。')
        return 0
    path = layout_store.save_meta(meta)
    print()
    print('已入库：%s' % path)
    return 0


def cmd_layouts(args) -> int:
    """版式库管理：查看 / 启停 / 删除 / 自检 / 从截图添加。"""
    cfg_mod.load_env()
    if args.enable or args.disable:
        name = args.enable or args.disable
        try:
            layout_store.set_enabled(name, bool(args.enable))
        except ValueError as e:
            raise SystemExit(str(e))
        print('%s %s（下次生成生效；旧 PPT 不受影响）'
              % ('已启用' if args.enable else '已禁用', name))
        return 0
    if args.rm:
        try:
            removed = layout_store.remove_meta(args.rm)
        except ValueError as e:
            raise SystemExit(str(e))
        if not removed:
            # 注册表里有、文件却不在：这种情况说明目录被手工动过
            raise SystemExit('%s 的声明文件不在版式目录里，没能删掉' % args.rm)
        print('已删除 %s' % args.rm)
        return 0
    if args.check:
        return _layouts_check()
    if args.add_image:
        return _layouts_add(args)
    print(_layouts_table())
    print()
    print('版式目录：%s' % layout_store.directory())
    return 0


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
        build_mod.build(spec, args.template or cfg_mod.template_path(), out,
                        fill_toc=True, on_log=print)
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
    # 没给 `--rounds` 就跟 Web 端同源读 `.env`。不能用 `args.rounds or ...`：
    # `--rounds 0`（明确要求不修）会被 `or` 当成没给而顶回默认值。
    rounds = (cfg_mod.repair_rounds() if args.rounds is None
              else max(0, args.rounds))

    state = {'before': None}

    def build_qa(spec):
        build_mod.build(spec, args.template or cfg_mod.template_path(),
                        out_pptx, fill_toc=True, on_log=print)
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


def _deck_stem(path: str) -> str:
    """`x.deck.json` / `x.deck.repaired.json` → `x`。

    不能只 `splitext`：后缀是**叠加**的（`x.deck.repaired.json`），
    只剥一层会得到 `x.deck`。服务器那边有同一件事的 `_stem_of`（带正则），
    这里判据简单些但也必须叠加着剥。
    """
    base = os.path.basename(path)
    i = base.find('.deck')
    return base[:i] if i > 0 else os.path.splitext(base)[0]


def _sibling(deck_path: str, suffix: str) -> str:
    """deck 旁边的同名中间产物（`<stem>.outline.json` / `<stem>.parsed.json`）。"""
    return _abs(os.path.join(PLANS, _deck_stem(deck_path) + suffix))


def cmd_revise(args):
    """按页修订：一段自然语言 → 逐页的修改方案（`--apply` 才落盘）。

    这是**不启前端就能验**的通道：prompt 质量的问题不该穿过整个 Web 层才被发现，
    而 `--apply` 之前只打印方案、一个字节都不写。
    """
    cfg_mod.load_env()
    cfg = cfg_mod.llm_config()
    if cfg is None:
        # 这里刻意**不学** outline/plan 的「没模型也能跑完」兜底：修订的全部价值
        # 就在于理解用户那句话，出不了补丁的「成功」是最坏的静默失败。
        raise SystemExit('修订需要文本模型（配 PPTGEN_LLM_*）。'
                         '没有模型时可以直接改 out/plans/*.deck.repaired.json。')
    deck_path = _abs(args.deck)
    deck = pipeline.load_json(deck_path)

    bad = revise_mod.check_deck_layouts(deck)
    if bad:
        raise SystemExit(bad)

    index = revise_mod.build_page_index(deck)
    mode = args.mode or 'patch'
    print('[revise] %s ｜ 模式：%s ｜ 预览图 %d 张'
          % (deck_path, '微调' if mode == 'patch' else '整页重做', len(index)))
    print()

    with runlog.stage('revise') as st:
        triage = revise_mod.split_requests(args.text, index, cfg)
        for u in triage['unclear']:
            print('[revise] ? %s' % u)
        if not triage['items']:
            raise SystemExit('没有解析出任何可执行的修改要求。')

        outline, doc = None, None
        if mode == 'rewrite':
            outline = pipeline.load_json(args.outline or _sibling(deck_path, '.outline.json'))
            doc = pipeline.load_json(args.parsed or _sibling(deck_path, '.parsed.json'))

        by_preview = {e['preview']: e for e in index}
        proposals = []
        if mode == 'patch':
            proposals = revise_mod.propose_patches(deck, triage['items'], cfg)
        else:
            cap = revise_mod.max_rewrite_pages()
            rewrote = 0
            for it in triage['items']:
                page = by_preview.get(it['preview']) or {}
                if page.get('kind') == 'back':
                    proposals.append(dict(it, status='reject', ops=[], new=None,
                                          reason='%s —— 封底是品牌收尾页，'
                                                 '没有可改的内容' % page.get('label')))
                    continue
                if page.get('layout') == revise_mod.DIVIDER_LAYOUT:
                    proposals.append(dict(it, status='reject', ops=[], new=None,
                                          reason='章节分隔页是结构页，没有版式可换 —— '
                                                 '只能微调它的章节名/导语'))
                    continue
                if rewrote >= cap:
                    proposals.append(dict(it, status='reject', ops=[], new=None,
                                          reason='一次最多重做 %d 页'
                                                 '（PPTGEN_REVISE_MAX_PAGES）'
                                                 '—— 这一页本次没做' % cap))
                    continue
                rewrote += 1
                print('[revise] 正在重做第 %d 页…' % it['preview'])
                if page.get('kind') in ('cover', 'toc'):
                    # 模板页没有版式可换：重做 = 重出这一页的文案
                    proposals.append(dict(it, **revise_mod.propose_template_rewrite(
                        deck, it['preview'], outline, it['request'], cfg)))
                    continue
                new, why = revise_mod.redo_slide(deck, it['preview'], outline, doc,
                                                it['request'], cfg)
                if new is None:
                    proposals.append(dict(it, status='reject', ops=[], new=None,
                                          reason=why))
                else:
                    proposals.append(dict(it, status='warn' if why else 'ok', ops=[],
                                          new=new, reason=why))

        for p in proposals:
            page = by_preview.get(p['preview']) or {}
            print()
            print('  第 %d 页（%s）· %s'
                  % (p['preview'], (page.get('label') or '').split('·')[-1].strip(),
                     '整页重做' if mode == 'rewrite' else '微调'))
            print('    你说：%s' % (p.get('quote') or p.get('request')))
            if p['status'] == 'reject':
                print('    ✗ 不能应用：%s' % p['reason'])
                continue
            if mode == 'rewrite' and (p.get('new') or {}).get('layout'):
                print('    版式：%s → %s' % (page.get('layout'),
                                            (p['new'] or {}).get('layout')))
                print('    新大字：%s' % revise_mod.headline(p['new'] or {}))
                if p['reason']:
                    print('    ! %s' % p['reason'])
                continue
            # 微调，以及封面/目录的重做（重出文案）—— 都是逐条的文字改动
            for c in p.get('changes') or []:
                print('    改 %s：「%s」→「%s」' % (c['path'], c['before'][:34],
                                                  c['after'][:34]))
            if p['reason']:
                print('    ! %s' % p['reason'])

        ok = [p for p in proposals if p['status'] != 'reject']
        st.update(items=len(proposals), applicable=len(ok), mode=mode)
        if not args.apply:
            print()
            print('[revise] 以上是方案，**没有落盘**。确认无误后加 --apply 应用。')
            return ''

        # ── 应用：先全量应用 + 预演构建，干净了才写盘 ──────────────
        rep: dict = {}
        outline_patch: list = []
        if mode == 'rewrite':
            out_deck = copy.deepcopy(deck)
            for it, p in zip(triage['items'], proposals):
                if p['status'] == 'reject':
                    continue
                # 分派收在 `commit_rewrite` 里：封面/目录写 deck 顶层并带回大纲补丁，
                # 正文页整页替换 —— 别自己写 `slides[preview-3]`，preview=1 是负下标。
                got = revise_mod.commit_rewrite(out_deck, it['preview'],
                                                p.get('new'), p.get('ops'))
                if got['reason']:
                    print('[revise] 第 %s 页未应用：%s'
                          % (it['preview'], got['reason']))
                    continue
                outline_patch.extend(got['outline'])
        else:
            out_deck, rep = revise_mod.apply_revision(
                deck, [dict(it, ops=p['ops']) for it, p in
                       zip(triage['items'], proposals) if p['status'] != 'reject'])

        out_pptx = _abs(args.out or os.path.join(SAMPLES, _deck_stem(deck_path) + '.pptx'))
        template = args.template or cfg_mod.template_path()

        def build_qa(spec):
            build_mod.build(spec, template, out_pptx, fill_toc=True, on_log=print)
            return geometry.analyse(out_pptx)

        changed = {p['preview'] for p in proposals if p['status'] != 'reject'}
        print()
        print('[revise] 预演构建（只为看改动的页有没有撑破版面）…')
        issues = revise_mod.check_by_build(out_deck, build_qa,
                                           {pg for pg in changed
                                            if pg >= revise_mod.PAGE_OFFSET})
        if issues:
            for pg, why in sorted(issues.items()):
                print('[revise] ✗ 第 %d 页：%s' % (pg, '；'.join(why)))
            raise SystemExit('改动的页有版面问题，**没有落盘**。'
                             '把要求写得更短一些，或改用 --mode rewrite 重排这一页。')

        spec_out = os.path.splitext(deck_path)[0] + '.revised.json'
        pipeline.save_json(out_deck, spec_out)
        print('[revise] 已应用 %d 条 → %s' % (len(ok), spec_out))
        print('[revise] 成品 → %s' % out_pptx)
        if outline_patch:
            print('[revise] 需要同步回大纲的字段：%s'
                  % '；'.join('%s → %s' % (p['field'], str(p['after'])[:30])
                             for p in outline_patch))
            # 真的写回去 —— 目录条目与封面标题都是**从大纲派生的**，只改 deck 的话
            # 下一次 `plan` 会把它们 silently 冲掉。**先备份**：这一步改的是
            # 用户手头那份大纲，写坏了就没有回头路。
            opath = args.outline or _sibling(deck_path, '.outline.json')
            if os.path.isfile(opath):
                cur = pipeline.load_json(opath)
                bak = os.path.splitext(opath)[0] + '.before-revise.json'
                if not os.path.isfile(bak):
                    pipeline.save_json(cur, bak)
                    print('[revise] 原大纲已备份 → %s' % bak)
                pipeline.save_json(revise_mod.apply_outline_patch(cur, outline_patch),
                                   opath)
                print('[revise] 已同步回大纲 → %s' % opath)
        runlog.note('revise', mode=mode, items=len(proposals), applied=len(ok),
                    spec=spec_out, outline_patch=outline_patch)
        runlog.attach_pptx(out_pptx)
        st.update(applied=len(ok))
    return spec_out


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

    p = sub.add_parser('layouts', help='版式库：查看 / 启停 / 删除 / 自检 / 从截图添加')
    p.add_argument('--list', action='store_true', help='列出版式（不带参数时的默认行为）')
    p.add_argument('--enable', metavar='名字', help='启用一套版式（内置也可以）')
    p.add_argument('--disable', metavar='名字', help='禁用一套版式：不再被选中，旧 PPT 仍能渲染')
    p.add_argument('--rm', metavar='名字', help='删除一套**自定义**版式（内置的只能禁用）')
    p.add_argument('--check', action='store_true',
                   help='自定义版式的样例过一遍几何 + 截断自检')
    p.add_argument('--add-image', metavar='图片', help='从一张版式截图识别新版式')
    p.add_argument('--adopt', action='store_true',
                   help='配合 --add-image：识别通过就直接入库（不加则只看一眼）')

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

    p = sub.add_parser('revise', help='按页修订：一段话 → 逐页方案（--apply 才落盘）')
    p.add_argument('--deck', required=True,
                   help='out/plans/<name>.deck.repaired.json —— 要改哪一份')
    p.add_argument('--text', required=True, help='用自然语言说哪几页要怎么改')
    p.add_argument('--mode', choices=('patch', 'rewrite'), default='patch',
                   help='patch=只改文字（版式不变）；rewrite=整页重做（可换版式）')
    p.add_argument('--outline', help='rewrite 模式要读大纲取锚点/意图')
    p.add_argument('--parsed')
    p.add_argument('--apply', action='store_true', help='真的落盘（默认只打印方案）')
    p.add_argument('--out')
    p.add_argument('--template')

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
    p.add_argument('--rounds', type=int, default=None,
                   help='最大修复轮数，不给则取 .env 的 PPTGEN_REPAIR_ROUNDS')
    p.add_argument('--template')

    p = sub.add_parser('full', help='大纲 → 规划 → 修复 → 渲染 一条命令走完')
    p.add_argument('--src', required=True)
    p.add_argument('--name')
    p.add_argument('--rounds', type=int, default=None,
                   help='最大修复轮数，不给则取 .env 的 PPTGEN_REPAIR_ROUNDS')
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
             'all': cmd_all, 'auto': cmd_auto, 'revise': cmd_revise,
             'layouts': cmd_layouts}[args.cmd](args)
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
    # `revise` 的输入是 deck —— 用 `_deck_stem` 剥（后缀是叠加的，
    # `_STAGE_SUFFIX.sub` 只剥一层，会把 `x.deck.repaired` 留成 `x.deck`）
    if getattr(args, 'deck', None):
        return _deck_stem(args.deck)
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
