# -*- coding: utf-8 -*-
"""文字适配修复回环：build → qa → 让模型压文案 → 再 build，直到几何检查干净。

为什么需要这一层：
  模型第一次规划时不知道每个版式的**容量**，写出来的文案常比框长。
  纯几何检查只能告诉你「哪里装不下」，不会替你改短。
  这个回环把「装不下」翻译成模型能执行的要求（「压到 N 字以内，保留数字」）。

设计取舍：
  - **整页重写，不做字段级映射。** 几何报告里只有形状名（如 "TextBox 17"），
    回溯到 spec 的哪个字段既脆弱又易错。整页重发更稳，且模型能顺带调整措辞。
  - **每轮只修溢出的页**，不动的页保持原样，避免无谓的改写与 token 消耗。
  - **数字、专有名词、结论句必须保留**（与 PPTGEN_CONTENT_MODE 的口径一致）。
"""
from __future__ import annotations

from . import config, llm
from .layouts import LAYOUT_NAMES

# 每个版式大致能装多少字。给模型一个明确的预算，比让它猜有效得多。
LAYOUT_CAPACITY = {
    'statement':      'lines 每行 ≤ 22 字，最多 2 行；body 最多 3 段、每段 ≤ 60 字。',
    'stat_hero':      'hero.num ≤ 8 字符；claim 最多 3 段、每段 ≤ 40 字；'
                      'stats 每条 label ≤ 22 字。',
    'definition':     'term ≤ 10 字；formula ≤ 26 字符；lead ≤ 24 字；'
                      'body ≤ 110 字；aside ≤ 30 字。',
    'numbered_columns': '每条 name ≤ 8 字、desc ≤ 22 字；最多 9 条。',
    'quadrant':       '每条 name ≤ 10 字、desc ≤ 46 字；正好 4 条。',
    'comparison_rows': '每行 dim ≤ 6 字、a 与 b 各 ≤ 34 字；最多 4 行。',
    'process_chain':  '每步 name ≤ 7 字、desc 两行、每行 ≤ 7 字；最多 5 步；'
                      'note ≤ 40 字。',
    'timeline_vertical': '每条 name ≤ 8 字、desc ≤ 22 字；最多 8 条。',
    'node_flow':      '每个节点 ≤ 10 字；最多 8 个；note ≤ 45 字。',
    'data_table':     '单元格 ≤ 22 字；最多 7 行 × 4 列。',
    'tinted_bands':   '每条 name ≤ 10 字、desc ≤ 52 字；最多 4 条。',
    'quote':          'quote 每行 ≤ 20 字，最多 2 行；attribution ≤ 30 字；'
                      'body 最多 3 段、每段 ≤ 40 字。',
}

# 报告里的页码 → spec 下标：封面(1) + 目录(2) 之后是正文
PAGE_OFFSET = 2

REPAIR_SYSTEM = (
    '你是演示文稿的文案编辑。你的唯一任务是把过长文案压到版式容量以内，'
    '同时**完整保留所有数字、百分比、专有名词、产品版本号与结论句**。'
    '不要改变事实，不要新增信息，只做删减与合并。'
)


def overflowing_slides(report: dict) -> dict[int, list[str]]:
    """从几何报告里挑出需要修的页。

    返回 {spec 下标: [问题描述, ...]}。同时收 text_overflow 与 text_overlap
    —— 重叠往往也是文字太长导致的。
    """
    out: dict[int, list[str]] = {}
    for it in report.get('issues', []):
        if it['kind'] not in ('text_overflow', 'text_overlap', 'past_safe_area'):
            continue
        idx = int(it['slide']) - 1 - PAGE_OFFSET
        if idx < 0:
            continue
        out.setdefault(idx, []).append('%s（%s）' % (it['kind'], it['detail']))
    return out


def repair_slide(spec: dict, problems: list[str], cfg, log=print) -> dict:
    """让模型重写单页 spec，压到容量以内。失败则原样返回。"""
    name = spec.get('layout')
    cap = LAYOUT_CAPACITY.get(name, '尽量精简，控制在原文的 60% 以内。')
    prompt = f"""下面是一页幻灯片的定义 JSON，它的文字超出了版式能容纳的范围。

版式：{name}
该版式的容量上限：{cap}

几何检查报出的问题：
{chr(10).join('- ' + p for p in problems)}

当前定义：
{_dump(spec)}

请返回**修正后的同一页定义 JSON**，要求：
1. 只删减与合并，**不得改动数字、百分比、专有名词、版本号**，不得新增信息。
2. 严格遵守上面的容量上限。
3. 保持 layout 字段不变，字段名与结构完全不变。
4. 只输出 JSON 对象本身，不要包 ``` 围栏，不要任何解释。
"""
    data = llm.ask_json(prompt, cfg, system=REPAIR_SYSTEM, max_tokens=None)
    if isinstance(data, dict) and isinstance(data.get('slides'), list) and data['slides']:
        data = data['slides'][0]
    if not isinstance(data, dict):
        log('      [跳过] 模型未返回对象（%s）' % type(data).__name__)
        return spec
    if data.get('layout') != name:
        # 版式被改掉会让下游渲染走错分支，宁可原样保留
        log('      [跳过] 模型改了 layout：%r → %r' % (name, data.get('layout')))
        return spec
    if _text_len(data) >= _text_len(spec):
        log('      [跳过] 压缩后并未变短（%d → %d 字）'
            % (_text_len(spec), _text_len(data)))
        return spec
    return data


def _text_len(o) -> int:
    """递归统计一个 spec 里所有字符串的总长度，用来判断是否真的压短了。"""
    if isinstance(o, str):
        return len(o)
    if isinstance(o, dict):
        return sum(_text_len(v) for k, v in o.items() if k != 'layout')
    if isinstance(o, (list, tuple)):
        return sum(_text_len(x) for x in o)
    return 0


def _dump(o) -> str:
    import json
    return json.dumps(o, ensure_ascii=False, indent=1)[:3000]


def repair_deck(deck: dict, build_qa, *, rounds: int = 3, verbose: bool = True,
                on_log=None) -> tuple[dict, dict]:
    """build → qa → 修 的循环。

    build_qa(spec) 由调用方注入，返回几何报告（因为它要用到 build 与 qa 两个模块，
    放在这里会形成循环依赖）。

    `on_log` 是日志出口（Web 端必须给，见下面 verbose 的说明）。
    """
    # `verbose` 只管控制台，`on_log` 管日志 —— 两者是正交的。服务器传
    # verbose=False 是为了别把控制台刷满，但那些行（尤其「跳过 / 压缩后并未变短」）
    # 恰恰是判断修复到底有没有生效的依据，必须进日志。
    log = on_log or (print if verbose else (lambda m: None))
    cfg = config.llm_config()
    if cfg is None:
        log('[repair] 未配置文本模型，跳过修复回环')
        return deck, {}

    report = {}
    for r in range(1, rounds + 1):
        report = build_qa(deck)
        todo = overflowing_slides(report)
        if not todo:
            log('[repair] 第 %d 轮：几何检查已干净，停止' % r)
            break
        log('[repair] 第 %d 轮：%d 页需要压缩' % (r, len(todo)))
        for idx, problems in sorted(todo.items()):
            if idx >= len(deck['slides']):
                continue
            old = deck['slides'][idx]
            try:
                new = repair_slide(old, problems, cfg, log)
            except llm.LLMError as e:
                log('[repair]   第 %d 页修复失败：%s' % (idx + 1, str(e)[:120]))
                continue
            deck['slides'][idx] = new
            log('[repair]   第 %d 页已压缩（%s）' % (idx + 1, old.get('layout')))
    else:
        log('[repair] 达到最大轮数 %d，仍有未解决问题' % rounds)
    return deck, report


def repair_summary(before: dict, after: dict) -> str:
    b = before.get('summary', {})
    a = after.get('summary', {})
    return ('修复前 %d error / %d warn  →  修复后 %d error / %d warn'
            % (b.get('error', 0), b.get('warn', 0),
               a.get('error', 0), a.get('warn', 0)))
