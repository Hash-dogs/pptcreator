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

from . import config, llm, structure
from .layouts import LAYOUT_NAMES


# ══════════════════════════════════════════════════════════════
# 版式目录 —— 喂给模型，让它知道每页能挑哪些版式、各自要填什么
# ══════════════════════════════════════════════════════════════
LAYOUT_CATALOG = """
**每个版式都有两个公共字段**（别漏，页眉靠它们）：
  title  —— 本页标题，一句话主张，≤24 字。statement / quote 没有标题位，不用给。
  kicker —— 左上角小字章节标签，填所属章节名（如「03 Dify 能做什么」）。
  source —— 页脚来源标注。

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
def make_outline(doc: dict, on_log=None) -> dict:
    """大纲的两个出口**都必须**带上 `_meta`。

    回退本身是设计（没配模型也能跑完整条链路），但回退产物曾经冒充模型产出一路走下去：
    失败只 print 一行 stdout，Web 界面上完全看不出来。`_meta` 就是让这件事可见的抓手 ——
    预览、前端、日志都读它。

    `on_log` 是日志出口。**Web 端必须给** —— 下面这行「模型调用失败，退回确定性大纲」
    是整个流程里最该被记下来的一行，而服务器上 `print` 只会进控制台，
    任务日志和文件日志都拿不到。
    """
    log = on_log or print
    lo, hi = config.page_range()
    src = _source_text(doc)
    cfg = config.llm_config()
    if cfg is not None:
        try:
            out = _outline_by_llm(doc, src, lo, hi, cfg, log)
            out['_meta'] = dict(generated_by='llm', model=cfg.model,
                                warnings=out.pop('_warnings', []))
            return out
        except (llm.LLMError, ValueError) as e:
            # ValueError 也要接住：模型返回了合法 JSON 但 sections 为空时
            # `_normalise_outline` 会抛「大纲为空」，早先这里只捕 LLMError，
            # 于是直接炸穿到 job 层，而不是退回兜底。
            warn = '%s: %s' % (type(e).__name__, e)
            log('[outline] 模型调用失败，退回确定性大纲：%s' % e)
            out = _outline_fallback(doc, lo, hi)
            out['_meta'] = dict(generated_by='fallback', model=cfg.model,
                                warnings=[warn])
            return out
    out = _outline_fallback(doc, lo, hi)
    out['_meta'] = dict(generated_by='fallback', model=None,
                        warnings=['未配置文本模型（PPTGEN_LLM_*），'
                                  '大纲走确定性兜底，未经过模型提炼'])
    return out


# 单次调用能塞下的正文上限。超过就按章分片 —— 分片是**为长文档准备的**，
# 短文档仍走一次调用（信息最全、也最省钱）。
OUTLINE_SINGLE_CHARS = 24000

_SYSTEM = '你是资深的中文商业演示顾问，擅长把长文档压缩成有主张的幻灯片大纲。'

_HEAD_TAIL = """
只输出 JSON，结构如下：
{
  "title": "整份 PPT 的标题",
  "sections": [
    {"name": "01 章节名", "summary": "一句话概括",
      "pages": [{"title": "页面标题", "hint": "展示形态",
                 "source": "来源标注", "anchor": "该页内容主要来自的源页标题"}]}
  ]
}
"""


def _mode_hint() -> str:
    return {
        'strict': '只做结构整理，不要增删原文中的事实。',
        'balance': '允许合并与提炼，但数字、专有名词、结论句必须原样保留。',
        'enrich': '可以在不违背原文的前提下补写过渡与解释。',
    }[config.content_mode()]


def _structure_block(sk: dict) -> str:
    """章节结构提示。有骨架时**章节划分以它为准**。"""
    chapters = (sk or {}).get('chapters') or []
    if not chapters:
        return ('源文档没有可识别的章节结构，请你按内容逻辑自行分成 3–5 章。\n')
    return ('源文档的章节结构如下（**章节划分以它为准**，一章不少、不改名、不合并）：\n\n'
            '<structure>\n%s\n</structure>\n\n共 %d 章。\n'
            % (structure.skeleton_digest(sk), len(chapters)))


def _outline_by_llm(doc, src, lo, hi, cfg, log=print) -> dict:
    """结构先行的大纲生成，超长时按章分片，最后过一遍校验。

    早先是「把整份文档截断后一次性丢给模型」—— 结构要靠模型从正文里自己猜，
    长文档的中间章节还会因为截断整段消失。现在骨架先给出来（章节沿用原文），
    模型只负责在既定结构上做内容取舍与标题主张。
    """
    sk = doc.get('structure') or {}
    chapters = sk.get('chapters') or []
    chunked = bool(chapters) and len(src) > OUTLINE_SINGLE_CHARS

    def gen(feedback: list[str] | None = None):
        if chunked:
            return _outline_chunked(doc, sk, lo, hi, cfg)
        return _outline_oneshot(doc, src, lo, hi, cfg, sk, feedback)

    data = gen()
    problems = _outline_problems(data, sk, lo, hi)
    if problems:
        # 带着违规清单重试一次。模型对「页数区间」「每页要 hint/source」这类
        # 硬约束经常第一轮不当回事 —— 实测省掉页数约束时它产出 18–19 页。
        log('[outline] 校验发现问题，带清单重试一次：%s' % '；'.join(problems))
        try:
            retry = gen(problems)
            if not _outline_problems(retry, sk, lo, hi):
                return retry
            data = retry
            problems = _outline_problems(retry, sk, lo, hi)
        except llm.LLMError as e:
            log('[outline] 重试失败，保留首轮结果：%s' % e)
    if problems:
        # 重试仍不合格也不退回兜底（兜底更差）—— 确定性修能修的部分，其余记 warning。
        data['_warnings'] = _repair_outline(data, sk, lo, hi, problems)
        log('[outline] 已确定性修正：%s' % '；'.join(data['_warnings']))
    return _normalise_outline(data, doc, lo, hi)


# ══════════════════════════════════════════════════════════════
# 大纲校验：模型给的约束是**建议**，这里才是执行
# ══════════════════════════════════════════════════════════════
def _outline_problems(data: dict, sk: dict, lo: int, hi: int) -> list[str]:
    """返回违规清单（空 = 通过）。"""
    sections = data.get('sections') or []
    problems = []
    if not sections:
        return ['没有任何章节']

    want = len(sk.get('chapters') or [])
    if want >= 2 and len(sections) != want:
        problems.append('章节数应为 %d，实际 %d（章节必须沿用原文结构）'
                        % (want, len(sections)))

    pages = [p for s in sections for p in (s.get('pages') or [])]
    n = len(pages)
    if n < lo or n > hi:
        problems.append('正文页数 %d 不在 %d–%d 区间内' % (n, lo, hi))
    if not pages:
        return problems

    missing = [p.get('title') for p in pages
               if not (p.get('hint') or '').strip() or not (p.get('source') or '').strip()]
    if missing:
        problems.append('%d 页缺 hint 或 source（例：%s）'
                        % (len(missing), missing[0]))

    bad = []
    seen = set()
    for p in pages:
        t = (p.get('title') or '').strip()
        if (not structure.is_title_like(t) or _NOT_A_TITLE.match(t)
                or len(t) > 40 or t in seen):
            bad.append(t)
        seen.add(t)
    if bad:
        problems.append('%d 个退化或重复的标题（例：%s）' % (len(bad), bad[0]))
    return problems


def _repair_outline(data: dict, sk: dict, lo: int, hi: int,
                    problems: list[str]) -> list[str]:
    """确定性地修能修的部分，剩下的记成 warning。

    模型不一定听话，但**不该因为不听话就整份退回兜底** —— 兜底产出更差。
    所以先尽力修：补章节、补 hint/source、超页数就章内压页。
    """
    warnings = list(problems)
    chapters = sk.get('chapters') or []
    sections = data.get('sections') or []

    # ① 章节对不上：以骨架为准补齐（骨架是原文结构这个事实）
    if chapters and len(sections) != len(chapters):
        by_name = {s.get('name', ''): s for s in sections}
        fixed = []
        for c in chapters:
            hit = by_name.get(c['name'])
            if hit is None:
                # 按名字对不上就按顺序取，取不到就用骨架的源页标题兜底
                hit = sections[len(fixed)] if len(fixed) < len(sections) else None
            if hit is None:
                hit = dict(name=c['name'], summary='',
                           pages=[dict(title=p['name'], hint='', source='') for p in c['pages']])
            hit = dict(hit)
            hit['name'] = c['name']
            fixed.append(hit)
        sections = fixed
        data['sections'] = sections

    # ② 补 hint / source：按 anchor 找源页，找不到就按顺序取
    for s, c in zip(sections, chapters or [None] * len(sections)):
        src_pages = {p['name']: p for p in (c or {}).get('pages', [])}
        spans = list(src_pages)
        for j, p in enumerate(s.get('pages') or []):
            if not (p.get('hint') or '').strip():
                p['hint'] = ''
            if not (p.get('source') or '').strip():
                p['source'] = 'Source: 《%s》%s' % (data.get('title') or '', s.get('name', ''))
            if not (p.get('anchor') or '').strip() and spans:
                p['anchor'] = spans[min(j, len(spans) - 1)]

    # ③ 页数超限：保章压页
    sections = _compress_sections(sections, hi)
    data['sections'] = sections
    return warnings




def _outline_oneshot(doc, src, lo, hi, cfg, sk, feedback=None) -> dict:
    fix = ''
    if feedback:
        fix = ('\n⚠️ 上一轮的结果有以下问题，这次**必须**修正：\n'
               + '\n'.join('- %s' % p for p in feedback) + '\n')
    prompt = f"""你在为一份中文汇报 PPT 设计大纲。

{_structure_block(sk)}

源文档《{doc.get('title') or doc['source']}》的正文：

<document>
{src}
</document>

要求：
- **章节沿用上面的结构**：一章不少，章名用给定的。
- 正文总页数控制在 {lo}–{hi} 页之间（不含封面/目录/封底）。
- 每章至少 1 页；**页可以在章内合并，章节不可合并、不可丢弃**。
- 每页给一个**具体的、有信息量的标题**（≤24 字），不要「概述」「简介」这类空标题。
- 每页标注最适合的展示形态 hint（如「对比表」「流程图」「三个并列要点」「一个核心数字」）。
- 每页给 source（来源标注），并给 anchor：填它主要取材的那条源页标题（照抄上面的）。
- {_mode_hint()}
{fix}{_HEAD_TAIL}"""
    return llm.ask_json(prompt, cfg, system=_SYSTEM,
                        max_tokens=config.outline_max_tokens())


def _outline_chunked(doc, sk, lo, hi, cfg) -> dict:
    """长文档：**每章一次调用**，各自只带该章的正文。

    分片而不是「先粗后细两遍」：一遍把结构定下来，另一遍再改标题，等于用
    更少的信息重做一遍同样的活，还多一份失败面。分片则每章都拿到自己那段的
    全文，prompt 有界，长文档也不会因为截断丢章。
    """
    chapters = sk['chapters']
    blocks = doc['blocks']
    front = set(sk.get('front_matter') or [])
    # 页数预算按各章源页数比例分配（每章至少 1 页）
    quota = _allocate([len(c['pages']) for c in chapters], hi)
    sections = []
    for c, k in zip(chapters, quota):
        if k <= 0:
            continue
        seg = structure.section_text(blocks, c, max_chars=OUTLINE_SINGLE_CHARS,
                                     front_matter=front)
        prompt = f"""你在为一份中文汇报 PPT 写其中**一章**的大纲。

整份 deck 的章节结构（供你理解上下文，**不要输出其他章**）：
<structure>
{structure.skeleton_digest(sk)}
</structure>

本章是「{c['name']}」，源文档里这一章的正文：

<document>
{seg}
</document>

要求：
- 为本章设计 **{k} 页**，顺序与原文一致。
- 页可以在章内合并，但不要跨章取材。
- 每页给一个具体的、有信息量的标题（≤24 字），不要「概述」「简介」这类空标题。
- 每页标注展示形态 hint、来源 source，以及 anchor（照抄本章源页标题里最相关的一条）。
- 数字、专有名词、结论句必须原样保留。

只输出 JSON：
{{"name": "{c['name']}", "summary": "本章一句话概括",
  "pages": [{{"title": "…", "hint": "…", "source": "…", "anchor": "…"}}]}}
"""
        data = llm.ask_json(prompt, cfg, system=_SYSTEM,
                            max_tokens=config.outline_max_tokens())
        pages = [p for p in (data.get('pages') or []) if (p.get('title') or '').strip()]
        if pages:
            sections.append(dict(name=c['name'], summary=data.get('summary') or '',
                                 pages=pages))
    if not sections:
        raise ValueError('分章生成结果为空')
    return dict(title=doc.get('title') or doc['source'], sections=sections)


# 明显不是页面标题的块（页眉 / 章节序号 / 目录标记）
_NOT_A_TITLE = re.compile(
    r'^(SECTION\s*\d*|CONTENTS?|目\s*录|目录|\d{1,2}|第\s*\d+\s*[章部节])$', re.I)

# 目录页一行的长度上限。模板的目录占位符是 10.12"×4.45"（16pt 正文），
# 一行大约放得下这么多字 —— 章节从 1 个变成 5 个之后，再让每行拖一句长
# summary 就会溢出（几何检查会报 text_overflow）。
# summary 的完整版仍留在大纲 JSON 里给人看，目录页上只放放得下的部分。
_TOC_MAX = 30


_TOC_SEP = ' —— '


def _toc_line(name: str, summary: str = '') -> str:
    name = (name or '').strip()
    summary = (summary or '').strip()
    room = _TOC_MAX - len(name) - len(_TOC_SEP)
    if not summary or room < 4:
        return name[:_TOC_MAX]              # 名字本身就快占满了，别带 summary
    if len(summary) <= room:
        return name + _TOC_SEP + summary
    return name + _TOC_SEP + summary[:room].rstrip()


def _outline_fallback(doc: dict, lo: int, hi: int) -> dict:
    """无模型时的确定性大纲：**章节直接沿用文档骨架**。

    早先这里按标题的数字前缀分组 —— 而这份 deck 的标题没有数字前缀
    （章节号在分隔页上、是独立的块），于是全部落进同一个分组，整份文档被压成了
    **1 个章节**；而 `_NOT_A_TITLE` 又把分隔页的 `01/02/03/04` 当噪声删掉，
    连猜的依据都没了。骨架是从版面事实上抽出来的，不用猜。
    """
    sk = doc.get('structure') or {}
    chapters = [c for c in (sk.get('chapters') or []) if c.get('pages')]

    sections = []
    for c in chapters:
        pages = [dict(title=p['name'], hint='', source='')
                 for p in c['pages'] if not _NOT_A_TITLE.match(p['name'].strip())]
        if pages:
            sections.append(dict(name=c['name'], summary='', pages=pages))

    if not sections:
        # 没有骨架（极简文档 / 空文档）：退回「每个标题一页」的单章
        titles, seen = [], set()
        for b in doc['blocks']:
            if b['type'] != 'heading':
                continue
            t = b['text'].strip()
            if len(t) < 3 or _NOT_A_TITLE.match(t) or t in seen:
                continue
            seen.add(t)
            titles.append(t)
        titles = titles or [doc.get('title') or doc['source']]
        sections = [dict(name=doc.get('title') or doc['source'], summary='',
                         pages=[dict(title=t, hint='', source='') for t in titles])]

    sections = _compress_sections(sections, hi)
    return _normalise_outline(
        dict(title=doc.get('title') or doc['source'], sections=sections,
             toc=[_toc_line(s['name'], s['summary']) for s in sections]), doc, lo, hi)


def _allocate(counts: list[int], budget: int) -> list[int]:
    """把 `budget` 个页位分给各章：**每章先保底 1 页**，余下按原页数比例分配。

    保底是硬要求：章节来自原文结构（用户明确要求沿用），压页只能压章内的页。
    早先按全局均匀抽样，实测把只有 1 页的第 5 章整章抽没了。
    """
    n = len(counts)
    if n == 0:
        return []
    if budget <= n:
        return [1] * budget + [0] * (n - budget)
    quota = [1] * n
    left = budget - n
    weights = [max(0, c - 1) for c in counts]
    wsum = sum(weights) or n
    frac = []
    for i, w in enumerate(weights):
        raw = w * left / float(wsum)
        add = int(raw)
        quota[i] += add
        frac.append((raw - add, i))
    for _, i in sorted(frac, reverse=True)[:max(0, budget - sum(quota))]:
        quota[i] += 1
    return quota


def _pick_evenly(pages: list, k: int) -> list:
    """从 `pages` 里均匀挑 k 个，**首尾一定保留**。

    均匀而不是取前 k 个：章节的开头和结尾往往是最重要的两页
    （引入与结论），取前 k 会系统性地丢掉章的收尾。
    """
    n = len(pages)
    if k >= n:
        return list(pages)
    if k <= 1:
        return [pages[0]]
    idx = sorted({int(round(j * (n - 1) / float(k - 1))) for j in range(k)})
    return [pages[i] for i in idx]


def _compress_sections(sections: list[dict], hi: int) -> list[dict]:
    """超页数上限时**保章、压页**：章节一个不少，只在章内合并页。"""
    total = sum(len(s['pages']) for s in sections)
    if total <= hi:
        return sections
    quota = _allocate([len(s['pages']) for s in sections], hi)
    out = []
    for s, k in zip(sections, quota):
        if k <= 0:
            continue
        kept = dict(s)
        kept['pages'] = _pick_evenly(s['pages'], k)
        out.append(kept)
    return out


def _normalise_outline(data: dict, doc: dict, lo: int, hi: int) -> dict:
    sections = data.get('sections') or []
    clean = []
    for s in sections:
        pages = [p for p in (s.get('pages') or []) if (p.get('title') or '').strip()]
        if pages:
            # **保留 section 上的其他键** —— 早先这里重建 dict 只留
            # name/summary/pages，把挂在章节上的元信息（如骨架的章节号）
            # 悄悄丢了，下游再想用就没有了。
            item = dict(s)
            item.update(name=s.get('name') or '未命名章节',
                        summary=s.get('summary') or '', pages=pages)
            clean.append(item)
    if not clean:
        raise ValueError('大纲为空')
    if not data.get('toc'):
        data['toc'] = [_toc_line(s['name'], s['summary']) for s in clean]
    data['toc'] = [_toc_line(t) for t in data['toc']]
    data['sections'] = clean
    data['title'] = data.get('title') or doc.get('title') or doc['source']
    data['page_count'] = sum(len(s['pages']) for s in clean)
    data['_page_range'] = [lo, hi]
    return data


def outline_preview(outline: dict) -> str:
    """给人看/改的大纲文本。"""
    L = ['# %s' % outline['title'], '']
    meta = outline.get('_meta') or {}
    if meta.get('generated_by') != 'llm':
        # 兜底产物「看起来」和模型产出一样，这是它最危险的地方 —— 顶到最显眼的位置。
        L.append('> ⚠️ **本大纲是确定性兜底产物，不是模型产出。**')
        for w in meta.get('warnings') or []:
            L.append('> - %s' % w)
        L.append('> 结构能跑完，但内容取舍、标题主张、版式与语义的匹配都未生效，'
                 '产出会明显单调。')
        L.append('')
    L += ['正文页数：%d（目标 %d–%d）'
          % (outline.get('page_count', 0), *(outline.get('_page_range') or [0, 0])),
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
            plan = _plan_by_llm(outline, src, cfg, log)
            # plan 以前**没有任何「走没走模型」的标记**（outline 有 `_meta`），
            # 所以事后无法回答「这次规划是不是退回兜底生成的」。挂在 **plan 字典**
            # 上而不是单个 slide 上 —— 逐 slide 的字段会被 repair 的 `_text_len`
            # 计入长度（repair.py:110 只跳过 'layout'），也会被 build 忽略不掉。
            plan['_generated_by'] = 'llm'
            return plan
        except (llm.LLMError, ValueError) as e:
            log('[plan] 模型规划失败，退回确定性规划：%s' % e)
    plan = _plan_fallback(outline, doc, log)
    plan['_generated_by'] = 'fallback'
    return plan


# 单批规划几页。一次让模型输出十几页的完整 spec 会超出输出 token 上限，
# JSON 被截断后解析失败 —— 实测 19 页一次性规划就退回了确定性兜底。
PLAN_BATCH = 6


def _plan_by_llm(outline: dict, src: str, cfg, log) -> dict:
    pages = []
    for s in outline['sections']:
        for p in s['pages']:
            # `anchor` 一定要带上：它是「这页的内容在源文档的哪一段」，
            # 白名单少一个字段，规划模型就只能瞎猜取材范围。
            pages.append({'section': s['name'], 'title': p['title'],
                          'hint': p.get('hint', ''), 'source': p.get('source', ''),
                          'anchor': p.get('anchor', '')})
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
    return _normalise_plan(slides, outline, log)


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
                        max_tokens=config.plan_max_tokens())
    return data.get('slides') or []


def _plan_fallback(outline: dict, doc: dict, log=None) -> dict:
    """无模型时的确定性规划：按每页的提示与内容形状选版式。

    取内容**优先走锚点**。锚点是源页标题 → 骨架里的块区间，比「标题精确
    字符串匹配」稳得多：LLM 大纲给的是自创的主张式标题（如「七大价值：…」），
    永远匹配不上任何 heading，于是每一页都退化成只有标题的 statement 页、
    且不报错。锚点允许缺失或过期（人工编辑过大纲），取不到再退回标题匹配。
    """
    blocks_all = doc['blocks']
    # 骨架缺失就现场重算 —— parsed.json 可能是加骨架之前写的旧文件，
    # 也可能被人手改过。没有骨架时锚点查不到，整份 deck 会退化成
    # 只有标题的空白页，而且**不报错**，所以这里不省这一步。
    sk = doc.get('structure') or structure.build_skeleton(blocks_all)
    spans = structure.page_index(sk)
    by_title = {}
    for b in blocks_all:
        if b['type'] == 'heading':
            by_title[b['text']] = []
            cur = b['text']
        elif by_title:
            by_title[cur].append(b)

    def content_of(page: dict) -> list[dict]:
        span = spans.get((page.get('anchor') or '').strip())
        if span:
            return [blocks_all[i] for i in range(span[0], span[1])
                    if blocks_all[i]['type'] != 'heading']
        return by_title.get(page['title'], [])

    slides, i = [], 0
    for s in outline['sections']:
        for p in s['pages']:
            slides.append(_heuristic_slide(p, s['name'], content_of(p), rotate=i))
            i += 1
    return _normalise_plan(slides, outline, log)


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


def _normalise_plan(slides: list[dict], outline: dict, log=None) -> dict:
    """校验版式名、补齐页眉字段，保证 build 一定能渲染。

    `title` / `kicker` 是版式级的公共字段：10 个版式都会调 `layouts.header()` 画页眉，
    而 `tokens.header()` 在两者都为空时**什么都不画**。这两个字段一直靠模型自觉产出，
    于是实测所有 LLM 生成的 deck 都是 `has_title=0/N` —— 渲染出来正文页顶部一片空白。
    所以这里**无条件从大纲回填**：模型给了就用模型的，没给就用大纲的。
    """
    sections, pages = [], []
    for s in outline.get('sections') or []:
        for p in s.get('pages') or []:
            sections.append(s.get('name') or '')
            pages.append(p)
    clean = []
    for i, sl in enumerate(slides, 1):
        name = sl.get('layout')
        if name not in LAYOUT_NAMES:
            # 这行以前只进 stdout —— 服务器上看不到、日志里也没有，
            # 而「版式名不被识别」正是内容会突然变得单调的典型原因。
            (log or print)('[plan] 第 %d 页版式 %r 未知，改用 statement' % (i, name))
            sl = dict(layout='statement',
                      lines=[[(sl.get('title') or '未命名', {})]])
        sl = dict(sl)
        page = pages[i - 1] if i - 1 < len(pages) else {}
        if not (sl.get('source') or '').strip():
            sl['source'] = page.get('source') or ''
        if sl['layout'] == 'statement':
            # statement 没有标题位（见 layouts.render_statement），留个 title 反而
            # 会被 repair 的 _text_len 算进去。这是原有行为，别改。
            sl.pop('title', None)
        elif not (sl.get('title') or '').strip():
            sl['title'] = (page.get('title') or '').strip()
        if not (sl.get('kicker') or '').strip() and i - 1 < len(sections):
            sl['kicker'] = sections[i - 1]
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
