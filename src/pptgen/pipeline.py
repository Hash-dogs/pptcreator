# -*- coding: utf-8 -*-
"""链路：解析 → 大纲 → 〔人工确认〕 → 规划 → 渲染。

两段生成都**优先用模型，未配置时走确定性回退**，所以没有 API key 也能跑完整条链路，
只是内容质量会退到「按标题切页」的朴素版本。

大纲是人工确认的落点：`outline.json` 产出后由人改，改完再进规划。
"""
from __future__ import annotations
import json
import os
import re

from . import config, llm
from .layouts import LAYOUT_NAMES


# ══════════════════════════════════════════════════════════════
# 版式目录 —— 喂给模型，让它知道每页能挑哪些版式、各自要填什么
# ══════════════════════════════════════════════════════════════
LAYOUT_CATALOG = """
可用版式（layout 字段填左边的名字）：

1. statement —— 大字陈述。适合开篇、章节引言、一句话主张。
   必填 lines: [[(文本,{})], ...] 每行一段；关键词用 {"hl":true} 着强调色。
   选填 kicker, body: [段落...], source

2. stat_hero —— 一个大数字 + 右侧说明 + 一排支撑数据。适合核心指标页。
   必填 hero: {"num":"87","unit":" 亿美元"}
     ⚠️ num 必须是**数字**（≤8 字符），unit 是单位（≤10 字符）。
        不要把文字标题塞进 num —— 那个位置只放得下一行 72pt 的数字。
   选填 claim: [段落...]（≤3 段、每段 ≤40 字）
        stats: [{"num":"38 家","label":"说明"}]，**2–3 个**，
               num 同为短数字（≤8 字符），label ≤20 字
        source

3. definition —— 术语定义。适合解释一个概念/名词。
   必填 term, formula（如 "= Define + Modify"）
   选填 lead（一句加粗断言）, body, aside: [(文本,{})], source

4. numbered_columns —— 分栏编号列表。适合并列的若干要点（4–9 条）。
   必填 items: [{"name":"关键词","desc":"一句说明"}]
   选填 columns（默认 3）, source

5. quadrant —— 四象限，正好 4 条。适合四个并列维度/挑战。
   必填 items: [{"name","desc"}] × 4
   选填 source

6. comparison_rows —— 维度对照（A 列 / B 列），2–5 行。适合前后对比、优劣对比。
   必填 col_a, col_b, rows: [{"dim":"维度","a":"...","b":"..."}]
   选填 source

7. process_chain —— 横向流程链，3–5 步。适合操作步骤、实施路径。
   必填 steps: [{"num":"01","name":"步骤名","desc":"两行短句，用 \\n 分隔"}]
   选填 note: [(文本,{})], source

8. timeline_vertical —— 纵向时间线，5–8 步。适合步骤较多、每条一句话的场景。
   必填 steps: [{"name":"步骤名","desc":"一句说明"}]
   选填 source

9. node_flow —— 节点链，6–8 个节点，自动折返。适合工作流、系统流程。
   必填 nodes: ["Start 收集信息", "生成标题", ...]
   选填 note, source

10. data_table —— 原生表格，2–8 行 × 2–5 列。适合参数对比、版本对比、类型矩阵。
    必填 header: ["列1","列2"], rows: [[...], ...]
    选填 col_widths（英寸，需合计 11.33）, source

11. tinted_bands —— 通栏浅色带，2–4 条。适合场景分类、并列陈述。
    必填 bands: [{"name":"名称","desc":"一到两句说明"}]
    选填 source

12. quote —— 引语页。适合结语、核心观点。一页只讲一件事。
    必填 quote: [[(文本,{})], ...]
    选填 kicker, attribution, body: [段落...], source
"""

DESIGN_RULES = """
设计要求（必须遵守）：
- 每页只讲**一个**核心信息，标题要具体、有主张，不要写「概述」「介绍」这类空标题。
- 页面上的文字要**精炼**：正文段落每条不超过 60 字，列表项不超过 30 字。
  这是幻灯片，不是文档——把细节砍掉，留下最锋利的事实。
- 数字、专有名词、结论句必须保留原文，不得改写或编造。
- 同一版式不要连续出现在相邻两页；整份 deck 里每种版式最多用 3 次。
- 第一页用 statement 或 stat_hero 开场；最后一页用 quote 收尾。
- 每页都要给 source 字段（写来源标注，如 "Source: 《Dify 介绍与实战》§1.1"）。
"""


# ══════════════════════════════════════════════════════════════
# ① 大纲
# ══════════════════════════════════════════════════════════════
def make_outline(doc: dict) -> dict:
    lo, hi = config.page_range()
    src = _source_text(doc)
    cfg = config.llm_config()
    if cfg is not None:
        try:
            return _outline_by_llm(doc, src, lo, hi, cfg)
        except llm.LLMError as e:
            print('[outline] 模型调用失败，退回确定性大纲：%s' % e)
    return _outline_fallback(doc, lo, hi)


def _outline_by_llm(doc, src, lo, hi, cfg) -> dict:
    mode = config.content_mode()
    mode_hint = {
        'strict': '只做结构整理，不要增删原文中的事实。',
        'balance': '允许合并与提炼，但数字、专有名词、结论句必须原样保留。',
        'enrich': '可以在不违背原文的前提下补写过渡与解释。',
    }[mode]
    prompt = f"""你在为一份中文汇报 PPT 设计大纲。

源文档《{doc.get('title') or doc['source']}》的内容如下：

<document>
{src}
</document>

要求：
- 正文页数控制在 {lo}–{hi} 页之间（不含封面/目录/封底）。
- 按源文档的逻辑分成 3–5 个章节，每章 2–5 页。
- 每页给一个**具体的、有信息量的标题**，不要「概述」「简介」这类空标题。
- 每页标注这页最适合的展示形态 hint（用中文描述，如「对比表」「流程图」「三个并列要点」「一个核心数字」）。
- {mode_hint}

只输出 JSON，结构如下：
{{
  "title": "整份 PPT 的标题",
  "sections": [
    {{"name": "01 章节名", "summary": "一句话概括",
      "pages": [{{"title": "页面标题", "hint": "展示形态", "source": "来源标注"}}]}}
  ],
  "toc": ["01 章节名 —— 一句话概括", "..."]
}}
"""
    data = llm.ask_json(prompt, cfg,
                        system='你是资深的中文商业演示顾问，擅长把长文档压缩成有主张的幻灯片大纲。',
                        max_tokens=4000)
    return _normalise_outline(data, doc, lo, hi)


# 明显不是页面标题的块（页眉 / 章节序号 / 目录标记）
_NOT_A_TITLE = re.compile(
    r'^(SECTION\s*\d*|CONTENTS?|目\s*录|目录|\d{1,2}|第\s*\d+\s*[章部节])$', re.I)


def _outline_fallback(doc: dict, lo: int, hi: int) -> dict:
    """无模型时的确定性大纲。

    pptx 解析器会把每个 slide 的首行都标成 level=1（它无从判断层级），
    所以这里**不能依赖 level 区分章节与页**，改用标题里的数字前缀分组：
    形如「01 初识 Dify」「02 为什么选 Dify」共享前缀 1/2，自然归到同一章。
    """
    heads = [b['text'].strip() for b in doc['blocks'] if b['type'] == 'heading']
    titles, seen = [], set()
    for t in heads:
        if len(t) < 3 or _NOT_A_TITLE.match(t) or t in seen:
            continue
        seen.add(t)
        titles.append(t)
    if not titles:
        titles = [doc.get('title') or doc['source']]

    groups, order = {}, []
    for t in titles:
        m = re.match(r'^(\d{1,2})[\s、.·]+', t)
        key = m.group(1) if m else '0'
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(t)

    sections = []
    for k in order:
        raw = groups[k]
        name = raw[0]
        pages = [dict(title=re.sub(r'^\d{1,2}[\s、.·]+', '', t) or t,
                      hint='', source='') for t in raw]
        # 章节首页与章节同名时，它本身就是章节封面，不重复作为页
        if len(pages) > 1 and pages[0]['title'] == name:
            pages = pages[1:]
        sections.append(dict(name=name, summary='', pages=pages))
    sections = [s for s in sections if s['pages']]
    if not sections:
        sections = [dict(name='正文', summary='',
                         pages=[dict(title=t, hint='', source='') for t in titles])]

    total = sum(len(s['pages']) for s in sections)
    if total > hi:                            # 超上限：按比例均匀抽样到 hi 页
        keep = {int(i * total / float(hi)) for i in range(hi)}
        n = 0
        for s in sections:
            kept = []
            for p in s['pages']:
                if n in keep:
                    kept.append(p)
                n += 1
            s['pages'] = kept
        sections = [s for s in sections if s['pages']]
    return _normalise_outline(
        dict(title=doc.get('title') or doc['source'], sections=sections,
             toc=['%s —— %s' % (s['name'], s['summary']) if s['summary'] else s['name']
                  for s in sections]), doc, lo, hi)


def _normalise_outline(data: dict, doc: dict, lo: int, hi: int) -> dict:
    sections = data.get('sections') or []
    clean = []
    for s in sections:
        pages = [p for p in (s.get('pages') or []) if (p.get('title') or '').strip()]
        if pages:
            clean.append(dict(name=s.get('name') or '未命名章节',
                              summary=s.get('summary') or '', pages=pages))
    if not clean:
        raise ValueError('大纲为空')
    if not data.get('toc'):
        data['toc'] = ['%s —— %s' % (s['name'], s['summary']) if s['summary'] else s['name']
                       for s in clean]
    data['sections'] = clean
    data['title'] = data.get('title') or doc.get('title') or doc['source']
    data['page_count'] = sum(len(s['pages']) for s in clean)
    data['_page_range'] = [lo, hi]
    return data


def outline_preview(outline: dict) -> str:
    """给人看/改的大纲文本。"""
    L = ['# %s' % outline['title'], '',
         '正文页数：%d（目标 %d–%d）' % (outline['page_count'], *outline['_page_range']),
         '']
    n = 0
    for s in outline['sections']:
        L.append('## %s' % s['name'])
        if s.get('summary'):
            L.append('> %s' % s['summary'])
        for p in s['pages']:
            n += 1
            hint = ('  〔%s〕' % p['hint']) if p.get('hint') else ''
            L.append('%2d. %s%s' % (n, p['title'], hint))
        L.append('')
    return '\n'.join(L)


# ══════════════════════════════════════════════════════════════
# ② 规划：大纲 → 每页版式与内容
# ══════════════════════════════════════════════════════════════
def make_plan(outline: dict, doc: dict, on_log=None) -> dict:
    log = on_log or (lambda m: print(m))
    cfg = config.llm_config()
    src = _source_text(doc)
    if cfg is not None:
        try:
            return _plan_by_llm(outline, src, cfg, log)
        except (llm.LLMError, ValueError) as e:
            log('[plan] 模型规划失败，退回确定性规划：%s' % e)
    return _plan_fallback(outline, doc)


# 单批规划几页。一次让模型输出十几页的完整 spec 会超出输出 token 上限，
# JSON 被截断后解析失败 —— 实测 19 页一次性规划就退回了确定性兜底。
PLAN_BATCH = 6


def _plan_by_llm(outline: dict, src: str, cfg, log) -> dict:
    pages = []
    for s in outline['sections']:
        for p in s['pages']:
            pages.append({'section': s['name'], 'title': p['title'],
                          'hint': p.get('hint', ''), 'source': p.get('source', '')})
    slides, used = [], {}
    for i in range(0, len(pages), PLAN_BATCH):
        chunk = pages[i:i + PLAN_BATCH]
        log('[plan] 规划第 %d–%d 页（共 %d）…'
            % (i + 1, i + len(chunk), len(pages)))
        got = _plan_batch(chunk, src, cfg, used, len(pages))
        for sl in got:
            used[sl.get('layout')] = used.get(sl.get('layout'), 0) + 1
        slides.extend(got)
    if not slides:
        raise ValueError('规划结果为空')
    return _normalise_plan(slides, outline)


def _plan_batch(chunk: list[dict], src: str, cfg, used: dict, total: int) -> list[dict]:
    used_txt = ('已用过的版式及次数：%s（尽量不要再堆同一种）'
                % (used or '无')) if used else '这是第一批。'
    prompt = f"""你在把一份大纲落成具体的幻灯片。整份共 {total} 页，本批需要处理 {len(chunk)} 页。

{LAYOUT_CATALOG}

{DESIGN_RULES}

{used_txt}

本批要处理的大纲页：
{json.dumps(chunk, ensure_ascii=False, indent=1)}

源文档内容（供你取用具体事实、数字与原文表述）：
<document>
{src}
</document>

请为上面**每一页**各产出一个幻灯片定义，**顺序与给定顺序一致**。要求：
- 严格按上面 12 种版式的字段填写，不要自创字段。
- 每页的 layout 必须来自那 12 个名字。
- **严格遵守每个版式的容量上限**（如 stat_hero 的 stats 只放 2–3 条、
  num 必须是短数字；numbered_columns 每条 desc ≤22 字）。
- 内容必须来自源文档，不得编造数字或事实。
- 每页都要有 source 字段。

只输出 JSON：
{{"slides": [ {{"layout": "…", ...该版式的字段…}}, ... ]}}
"""
    data = llm.ask_json(prompt, cfg,
                        system='你是资深的演示文稿设计师，负责把内容分配到最合适的版式并精炼文案。',
                        max_tokens=8000)
    return data.get('slides') or []


def _plan_fallback(outline: dict, doc: dict) -> dict:
    """无模型时的确定性规划：按每页的提示与内容形状选版式。"""
    by_title = {}
    for b in doc['blocks']:
        if b['type'] == 'heading':
            by_title[b['text']] = []
            cur = b['text']
        elif by_title:
            by_title[cur].append(b)
    slides, i = [], 0
    for s in outline['sections']:
        for p in s['pages']:
            blocks = by_title.get(p['title'], [])
            slides.append(_heuristic_slide(p, s['name'], blocks, rotate=i))
            i += 1
    return _normalise_plan(slides, outline)


_SPLIT_RE = re.compile(r'^(.{2,14}?)\s*[：:，,。；;]\s*(.+)$')


def _split_item(t: str) -> tuple[str, str]:
    """把一句正文切成「小标题 + 说明」。切不出来时退化为前 12 字 + 余下。"""
    t = re.sub(r'^[\d①-⑩]{1,2}[\s、.·]+', '', t.strip())
    m = _SPLIT_RE.match(t)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return (t[:12], t[12:].strip()) if len(t) > 14 else (t, '')


def _heuristic_slide(page: dict, section: str, blocks: list[dict],
                     rotate: int = 0) -> dict:
    """无模型时的版式选择。

    这一层**注定只是兜底**：内容的取舍、标题的主张、版式与语义的匹配
    都依赖模型。这里只保证「有内容、版式不单调、一定能渲染」。
    """
    paras = [b['text'] for b in blocks if b['type'] == 'para']
    bullets = [i for b in blocks if b['type'] == 'bullets' for i in b['items']]
    tables = [b for b in blocks if b['type'] == 'table']
    src = page.get('source') or ('Source: %s' % section)
    base = dict(kicker=section, source=src)

    if tables:
        t = tables[0]
        return dict(layout='data_table', title=page['title'],
                    header=t['header'], rows=t['rows'], **base)

    items = bullets or paras
    n = len(items)
    if n == 0:
        return dict(layout='statement', lines=[[(page['title'], {'hl': True})]], **base)
    if n <= 2:
        return dict(layout='statement', lines=[[(page['title'], {})]],
                    body=[[(p, {'size': 16, 'color': 'MUTED'})] for p in items], **base)
    if n == 4 and all(len(i) <= 26 for i in items):
        return dict(layout='quadrant',
                    items=[dict(name=a, desc=b) for a, b in map(_split_item, items)],
                    **base)
    if 3 <= n <= 5 and rotate % 3 == 2:
        return dict(layout='tinted_bands',
                    bands=[dict(name=a, desc=b) for a, b in map(_split_item, items)],
                    **base)
    if 3 <= n <= 9:
        return dict(layout='numbered_columns',
                    items=[dict(name=a, desc=b) for a, b in map(_split_item, items)],
                    **base)
    # 超过 9 条：轮换用色带 / 大数字，避免整份都是同一种版式
    if rotate % 2 == 0:
        return dict(layout='tinted_bands',
                    bands=[dict(name=a, desc=b) for a, b in
                           map(_split_item, items[:4])], **base)
    return dict(layout='statement', lines=[[(page['title'], {})]],
                body=[[(p, {'size': 16, 'color': 'MUTED'})] for p in items[:3]], **base)


def _normalise_plan(slides: list[dict], outline: dict) -> dict:
    """校验版式名、剔除未知字段、补齐 source，保证 build 一定能渲染。"""
    clean = []
    for i, sl in enumerate(slides, 1):
        name = sl.get('layout')
        if name not in LAYOUT_NAMES:
            print('[plan] 第 %d 页版式 %r 未知，改用 statement' % (i, name))
            sl = dict(layout='statement',
                      lines=[[(sl.get('title') or '未命名', {})]])
        sl = dict(sl)
        sl.pop('title', None) if sl['layout'] == 'statement' else None
        sl.setdefault('source', '')
        clean.append(sl)
    return dict(slides=clean, toc=list(outline.get('toc') or []),
                title=outline.get('title', ''))


def _source_text(doc: dict) -> str:
    from .parse import outline_source
    return outline_source(doc)


# ══════════════════════════════════════════════════════════════
def save_json(obj, path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_json(path: str):
    with open(path, encoding='utf-8-sig') as f:
        return json.load(f)
