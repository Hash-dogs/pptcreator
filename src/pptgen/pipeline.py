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

from . import config, layout_spec, llm, structure
from .layouts import LAYOUT_NAMES


# ══════════════════════════════════════════════════════════════
# 版式目录 —— 从 layout_spec 的注册表生成，不在这里手写
#
# 早先这份目录、`layouts.LAYOUTS`、`repair.LAYOUT_CAPACITY` 是三份彼此独立
# 手写的清单，已经漂移（`stats.label` 一处 ≤20 字、一处 ≤22 字 —— 模型先看到
# 20，被压时被告知 22）。现在只有注册表一处。
# ══════════════════════════════════════════════════════════════
def layout_catalog() -> str:
    return layout_spec.catalog_text()


DESIGN_RULES = """
设计要求（必须遵守）：
- 每页只讲**一个**核心信息，标题要具体、有主张，不要写「概述」「介绍」这类空标题。
- 页面上的文字要**精炼**：正文段落每条不超过 60 字，列表项不超过 30 字。
  这是幻灯片，不是文档——把细节砍掉，留下最锋利的事实。
- 数字、专有名词、结论句必须保留原文，不得改写或编造。
- 每页都要给 source 字段（写来源标注，如 "Source: 《Dify 介绍与实战》§1.1"）。

**版式只能从该页给出的候选里挑**（候选已经按这一页的表达意图与内容容量筛过）。
候选里排在前面的更贴题。不要在候选之外自创版式名。
"""


# ══════════════════════════════════════════════════════════════
# 意图 → 候选版式
#
# 让模型从 20 个版式里盲选，是「大纲写『六参数对比表 + 三类调优技巧』、
# 规划却选了 timeline_vertical」这类错配的温床。改成两步：先用**表达意图**与
# **内容形态**把候选收窄到 2–4 个，再让模型在候选内选 —— 借鉴自 PPTAgent 的
# layout_selector（它把版式按纯文本/多模态先二分，再在集合内让模型选）。
# ══════════════════════════════════════════════════════════════

# 无模型时按标题关键词猜意图。有模型时用大纲给的 intent。
_INTENT_HINTS = (
    ('status',       ('进展', '进度', '状态', '风险', '落实', '完成情况', '跟踪')),
    ('summary',      ('总结', '结论', '要点回顾', '下一步', '结语', '落地要点', '小结')),
    ('comparison',   ('对比', '对照', '优劣', '前后', '区别', '差异', '传统', 'vs')),
    ('hierarchy',    ('分层', '架构', '技术栈', '层级', '体系', '中台', '金字塔')),
    ('timeline',     ('阶段', '时间线', '演进', '里程碑', '历程', '迭代', '路线')),
    ('process',      ('步骤', '流程', '如何', '怎么', '搭建', '做法', '操作')),
    ('definition',   ('是什么', '什么是', '定义', '含义', '概念')),
    ('quantitative', ('数据', '指标', '参数', '定价', '价格', '版本', '数字', '统计')),
    ('enumeration',  ('要点', '并列', '清单', '列表', '亮点', '能力', '功能',
                      '关键词', '措施', '理念', '要素')),
)


def guess_intent(page: dict, blocks: list[dict] | None = None) -> str:
    """猜一页的表达意图。纯确定性 —— 兜底路径与 intent 缺失时都用它。"""
    text = '%s %s' % (page.get('title') or '', page.get('hint') or '')
    for intent, keys in _INTENT_HINTS:
        if any(k in text for k in keys):
            return intent
    shape = layout_spec.shape_of(blocks or [])
    if shape['has_table']:
        return 'quantitative'
    n = shape['n_items']
    if n <= 2:
        return 'statement'
    return 'enumeration'


def _content_index(doc: dict):
    """返回 `content_of(page) -> [源块]`。锚点优先，标题精确匹配兜底。

    取内容**优先走锚点**。锚点是源页标题 → 骨架里的块区间，比「标题精确字符串
    匹配」稳得多：LLM 大纲给的是自创的主张式标题（如「七大价值：…」），永远
    匹配不上任何 heading，于是每一页都退化成只有标题的 statement 页、且不报错。
    锚点允许缺失或过期（人工编辑过大纲），取不到再退回标题匹配。
    """
    blocks_all = doc['blocks']
    # 骨架缺失就现场重算 —— parsed.json 可能是加骨架之前写的旧文件，
    # 也可能被人手改过。没有骨架时锚点查不到，整份 deck 会退化成
    # 只有标题的空白页，而且**不报错**，所以这里不省这一步。
    sk = doc.get('structure') or structure.build_skeleton(blocks_all)
    spans = structure.page_index(sk)
    by_title: dict[str, list] = {}
    cur = None
    for b in blocks_all:
        if b['type'] == 'heading':
            by_title[b['text']] = []
            cur = b['text']
        elif cur is not None:
            by_title[cur].append(b)

    def content_of(page: dict) -> list[dict]:
        span = spans.get((page.get('anchor') or '').strip())
        if span:
            return [blocks_all[i] for i in range(span[0], span[1])
                    if blocks_all[i]['type'] != 'heading']
        return by_title.get(page.get('title'), [])

    return content_of


def _page_roles(pages: list[dict]) -> list[str]:
    """整份 deck 的 page_role。结构性判断，不需要模型。

    第一页是开场、最后一页是收尾，其余都是内容页 —— 章节隔断页由
    `_add_dividers` 规则插入（结构页不该交给模型挑，它没有内容可依据）。
    """
    # 只有两档：结构页（隔断）与内容页。位置语义（开场/收尾）交给 intent，
    # 不再单开一档 —— 那会让首尾页绕过意图过滤，候选塌成一个版式。
    return ['section' if p.get('divider') else 'content' for p in pages]


def page_candidates(page: dict, role: str, blocks: list[dict] | None) -> list[str]:
    """一页的候选版式名（已按意图与容量筛过），最多 4 个。"""
    intent = page.get('intent') or guess_intent(page, blocks)
    shape = layout_spec.shape_of(blocks or [])
    got = layout_spec.candidates(role, intent, shape)
    if not got:
        sp = layout_spec.resolve(None, role, intent, shape)
        return [sp.name]
    return [sp.name for sp in got[:4]]


# 每个版式的「条目列表」在 spec 的哪个字段、条目文本要从哪些子字段拼。
# 用来校验**模型写出来的成品**是否超容量 —— 这是 `_fit()` 静默截断的事前防线：
# 实测 node_flow 8 个节点里 6 个被截成「小红书正文 · 爆款写作…」，而几何报告全绿。
_ITEM_FIELDS = {
    'numbered_columns':   ('items', ('name', 'desc')),
    'tinted_bands':       ('bands', ('name', 'desc')),
    'quadrant':           ('items', ('name', 'desc')),
    'split_main_aside':   ('items', ('name', 'desc')),
    'comparison_rows':    ('rows', ('dim', 'a', 'b')),
    'process_chain':      ('steps', ('name', 'desc')),
    'timeline_vertical':  ('steps', ('name', 'desc')),
    'node_flow':          ('nodes', ()),
    # 阶段名与**节点标签**都要量：真正会被 `_fit()` 截断的是节点标签，不是阶段名
    'phase_grouped_flow': ('phases', ('name', 'nodes')),
    'layered_stack':      ('layers', ('name', 'modules')),
    'kpi_grid':           ('items', ('label', 'note')),
    'progress_checklist': ('items', ('name', 'desc')),
    'executive_summary':  ('points', ('text',)),
    'data_table':         ('rows', ()),
    'stat_hero':          ('stats', ('label',)),
}


def _items(sl: dict) -> tuple[int, list[str]]:
    """一页 spec 的 (条目数, 各条目的文本)。

    **条目数** = 列表里有几项（kpi_grid 的 6 个指标是 6 条，不是 12 条）；
    **条目文本** = 该项各标量子字段拼起来算一条，嵌套列表（层内的模块名、
    组内的节点标签）则每个元素各算一条 —— 它们各自是独立的单行文本，
    也各自会被 `_fit()` 截断。
    """
    rule = _ITEM_FIELDS.get(sl.get('layout') or '')
    if not rule:
        return 0, []
    key, subs = rule
    got = sl.get(key)
    if not isinstance(got, list):
        return 0, []
    texts: list[str] = []
    for it in got:
        if isinstance(it, str):
            texts.append(it)
        elif isinstance(it, dict):
            joined = ''
            for s in subs:
                v = it.get(s)
                if isinstance(v, (list, tuple)):
                    texts.extend(str(x) for x in v)
                else:
                    joined += str(v or '')
            if joined:
                texts.append(joined)
        elif isinstance(it, (list, tuple)):
            texts.extend(str(c) for c in it)
    return len(got), [t for t in texts if t]


def overflow_reason(sl: dict) -> str:
    """模型写出来的这一页有没有超容量。返回原因（空串 = 没超）。"""
    sp = layout_spec.get(sl.get('layout') or '')
    if sp is None:
        return ''
    count, texts = _items(sl)
    if not texts:
        return ''
    if sp.max_items is not None and count > sp.max_items:
        return '条目 %d 条，超过上限 %d 条' % (count, sp.max_items)
    hard = sp.hard_item_chars
    if hard:
        over = [t for t in texts if len(t) > hard]
        if over:
            return '单条 %d 字，超过上限 %d 字（%s…）' % (len(over[0]), hard, over[0][:16])
    return ''


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
    # 先决定要不要留出章节分隔页 —— 它是结构页，要占页数预算，所以必须**先**扣掉，
    # 而不是等正文按 hi 生成完了再硬塞进去（那样总页数会超标）。
    n_div, lo, hi = _divider_budget(doc, lo, hi)
    if n_div:
        log('[outline] 预留 %d 页给章节分隔页，正文按 %d–%d 页控制' % (n_div, lo, hi))
    src = _source_text(doc)
    cfg = config.llm_config()
    if cfg is not None:
        try:
            out = _outline_by_llm(doc, src, lo, hi, cfg, log)
            out = _add_dividers(out, doc, log)
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
            out = _add_dividers(out, doc, log)
            out['_meta'] = dict(generated_by='fallback', model=cfg.model,
                                warnings=[warn])
            return out
    out = _outline_fallback(doc, lo, hi)
    out = _add_dividers(out, doc, log)
    out['_meta'] = dict(generated_by='fallback', model=None,
                        warnings=['未配置文本模型（PPTGEN_LLM_*），'
                                  '大纲走确定性兜底，未经过模型提炼'])
    return out


# ══════════════════════════════════════════════════════════════
# 章节分隔页
# ══════════════════════════════════════════════════════════════
def _divider_budget(doc: dict, lo: int, hi: int) -> tuple[int, int, int]:
    """决定插不插章节分隔页，返回 (分隔页数, 正文页下限, 正文页上限)。

    判据是「扣掉分隔页后，每章至少还留得下一页正文」。装不下就**完全不插**，
    而不是硬塞或砍正文 —— 正文是内容，结构是锦上添花。

    关掉：`.env` 里设 `PPTGEN_SECTION_DIVIDERS=0`。
    """
    if not config.section_dividers():
        return 0, lo, hi
    chapters = (doc.get('structure') or {}).get('chapters') or []
    n = len(chapters)
    if n < 2 or hi < n * 2:
        return 0, lo, hi
    return n, min(lo, hi - n), hi - n


def _add_dividers(outline: dict, doc: dict, log=print) -> dict:
    """给每章开头插一页章节隔断。

    **规则插入而不是让模型挑**：隔断页是结构页，它没有内容可依据 ——
    让模型在 20 个版式里「选」一个结构页，只会选错。借鉴 PPTAgent 的
    `_add_functional_layouts()`：功能性版式按位置规则插入，不参与内容驱动的选择。
    """
    if not config.section_dividers():
        return outline
    chapters = (doc.get('structure') or {}).get('chapters') or []
    sections = outline.get('sections') or []
    if len(chapters) < 2 or len(chapters) != len(sections):
        return outline
    n = 0
    for i, (s, c) in enumerate(zip(sections, chapters)):
        name = (s.get('name') or c.get('name') or '').strip()
        m = re.match(r'^\s*(\d{1,2})', name)
        pages = s.setdefault('pages', [])
        pages.insert(0, dict(
            title=name,
            hint='章节隔断页',
            intent='section',
            source='',
            anchor='',
            divider=True,
            num=(m.group(1) if m else '%02d' % (i + 1)),
            # 章节的 summary 正好当隔断页那句导语
            lead=(s.get('summary') or '')[:40],
        ))
        n += 1
    outline['divider_count'] = n
    outline['page_count'] = sum(1 for s in sections for p in s['pages']
                                if not p.get('divider'))
    outline['total_pages'] = outline['page_count'] + n
    if log:
        log('[outline] 已插入 %d 页章节分隔页（总页数 %d）' % (n, outline['total_pages']))
    return outline


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
      "pages": [{"title": "页面标题", "hint": "展示形态", "intent": "表达意图",
                 "source": "来源标注", "anchor": "该页内容主要来自的源页标题"}]}
  ]
}
"""

# 每页的 `intent` 决定后面能挑哪些版式 —— 它是「这页在表达什么」，
# 与 `hint`（自由描述的展示形态）不同，必须是**固定枚举里的一个**，
# 否则下游无法把它映射到候选版式集。
_INTENT_BLOCK = """
每页的 `intent` 必须从下面这些值里**原样选一个**（不要自创、不要写中文）：

  statement     单点主张 —— 一句话结论或判断
  definition    概念解释 —— 术语 + 释义
  enumeration   并列列举 —— 若干同级要点
  comparison    对照 —— A/B 两方、优劣、前后
  process       步骤流程 —— 有先后的动作、操作路径
  timeline      时间推进 —— 阶段、里程碑、演进
  hierarchy     层级包含 —— 分层、支撑、嵌套（技术栈、架构、体系）
  quantitative  数据指标 —— 数字、指标、参数、表格
  status        状态进度 —— 已完成 / 进行中 / 有风险 / 待启动
  summary       结论回收 —— 把要点收成几条可念的结论
  quote         引语 —— 一句被引用的话
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

    # intent 是版式选择的输入：缺了就退化成按标题猜，选出来的版式自然不贴内容。
    bad_intent = [p.get('title') for p in pages
                  if (p.get('intent') or '') not in layout_spec.INTENTS]
    if bad_intent:
        problems.append('%d 页缺 intent 或不在枚举内（例：%s）'
                        % (len(bad_intent), bad_intent[0]))

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
            # intent 缺失会让版式选择退化成「按标题猜」，这里先按标题/形态补一个，
            # 规划阶段还会用真实源块重算（见 page_candidates）。
            if (p.get('intent') or '') not in layout_spec.INTENTS:
                p['intent'] = guess_intent(p)

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
{_INTENT_BLOCK}{fix}{_HEAD_TAIL}"""
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
{_INTENT_BLOCK}
只输出 JSON：
{{"name": "{c['name']}", "summary": "本章一句话概括",
  "pages": [{{"title": "…", "hint": "…", "intent": "…", "source": "…", "anchor": "…"}}]}}
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
    content_of = _content_index(doc)

    sections = []
    for c in chapters:
        pages = []
        for p in c['pages']:
            if _NOT_A_TITLE.match(p['name'].strip()):
                continue
            page = dict(title=p['name'], hint='', source='', anchor=p['name'])
            # 兜底路径没有模型给的 intent，就按**真实源块**的形态猜一个 ——
            # 有块可比对，比只看标题准得多（9 条要点的页不该猜成「单点主张」）。
            page['intent'] = guess_intent(page, content_of(page))
            pages.append(page)
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
                         pages=[dict(title=t, hint='', source='',
                                     intent=guess_intent(dict(title=t)))
                                for t in titles])]

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
    # intent 是版式选择的输入，必须每页都有。模型漏了或写了枚举外的值，
    # 就在这里按标题/形态补一个，而不是让它留空、导致下游退化成盲选。
    if clean:
        content_of = _content_index(doc)
        for s in clean:
            for p in s['pages']:
                if (p.get('intent') or '') not in layout_spec.INTENTS:
                    p['intent'] = guess_intent(p, content_of(p))
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
    n_div = outline.get('divider_count', 0)
    L += ['正文页数：%d（目标 %d–%d）%s'
          % (outline.get('page_count', 0), *(outline.get('_page_range') or [0, 0]),
             '　+ %d 页章节分隔页，合计 %d 页'
             % (n_div, outline.get('total_pages', 0)) if n_div else ''),
          '']
    n = 0
    for s in outline['sections']:
        L.append('## %s' % s['name'])
        if s.get('summary'):
            L.append('> %s' % s['summary'])
        for p in s['pages']:
            n += 1
            if p.get('divider'):
                L.append('%2d. 〔章节分隔页〕%s' % (n, p['title']))
                continue
            bits = []
            if p.get('intent'):
                bits.append(p['intent'])
            if p.get('hint'):
                bits.append(p['hint'])
            tag = ('  〔%s〕' % ' · '.join(bits)) if bits else ''
            L.append('%2d. %s%s' % (n, p['title'], tag))
        L.append('')
    L += ['', '> `intent` 决定这一页能挑哪些版式（见 layout_spec.INTENTS）；'
              '手动改它就能改版式走向。',
          '> 章节分隔页由 `PPTGEN_SECTION_DIVIDERS=0` 关闭。']
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
            plan = _plan_by_llm(outline, doc, src, cfg, log)
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


def _divider_slide(page: dict, section: str) -> dict:
    """章节隔断页的 spec —— 结构固定，不经过模型。"""
    return dict(layout='section_divider',
                num=str(page.get('num') or '01'),
                title=page.get('title') or section,
                lead=page.get('lead') or '',
                source='')


def _plan_by_llm(outline: dict, doc: dict, src: str, cfg, log) -> dict:
    """逐页：算意图与内容形态 → 收窄候选 → 模型在候选内选 → 过一致性护栏。

    与早先「把版式目录整个丢给模型盲选」的区别在于，模型看到的每一页都带着
    **这一页的候选集**（按表达意图与容量筛过），于是「大纲写『六参数对比表』、
    规划却选了 timeline_vertical」这类错配从源头被掐掉。
    """
    flat = [(s['name'], p) for s in outline['sections'] for p in s['pages']]
    roles = _page_roles([p for _, p in flat])
    content_of = _content_index(doc)

    by_index: dict[int, dict] = {}
    todo: list[tuple[int, dict, dict]] = []       # (下标, 送模型的页, 护栏元信息)
    for i, ((sec, p), role) in enumerate(zip(flat, roles)):
        if p.get('divider'):
            by_index[i] = _divider_slide(p, sec)
            continue
        blocks = content_of(p)
        cands = page_candidates(p, role, blocks)
        intent = p.get('intent') or guess_intent(p, blocks)
        # `anchor` 一定要带上：它是「这页的内容在源文档的哪一段」，
        # 白名单少一个字段，规划模型就只能瞎猜取材范围。
        todo.append((i, {'section': sec, 'title': p['title'],
                         'hint': p.get('hint', ''), 'intent': intent,
                         'source': p.get('source', ''), 'anchor': p.get('anchor', ''),
                         'candidates': cands},
                     dict(role=role, intent=intent, candidates=cands,
                          page=p, section=sec, blocks=blocks)))

    used: dict[str, int] = {}
    off_candidate = 0
    for k in range(0, len(todo), PLAN_BATCH):
        chunk = todo[k:k + PLAN_BATCH]
        log('[plan] 规划第 %d–%d 页（共 %d）…' % (k + 1, k + len(chunk), len(todo)))
        got = _plan_batch([c[1] for c in chunk], src, cfg, used, len(todo))
        for n, (i, _payload, meta) in enumerate(chunk):
            sl = got[n] if n < len(got) else None
            if not isinstance(sl, dict):
                sl = None
            elif sl.get('layout') not in meta['candidates']:
                # 模型挑了候选外的版式。**不能只把 layout 名改掉** ——
                # 字段是照着另一个版式填的，改名会缺键、渲染时才炸。
                # 这一页改用确定性生成（字段必然对得上）。
                log('[plan] 第 %d 页选了候选外的版式 %r，本页改用确定性生成'
                    % (i + 1, sl.get('layout')))
                off_candidate += 1
                sl = None
            else:
                why = overflow_reason(sl)
                if why:
                    # 条目超容量 → 再渲染就会被 `_fit()` 静默截成残句。
                    # 这一页改用确定性生成，让内容按版式真正的容量重排。
                    log('[plan] 第 %d 页（%s）%s，本页改用确定性生成'
                        % (i + 1, sl.get('layout'), why))
                    off_candidate += 1
                    sl = None
            if sl is None:
                sl = _heuristic_slide(meta['page'], meta['section'], meta['blocks'],
                                      candidates=meta['candidates'], taken=used)
            used[sl.get('layout')] = used.get(sl.get('layout'), 0) + 1
            by_index[i] = sl

    if not by_index:
        raise ValueError('规划结果为空')

    # 护栏要在**全部批次都回来之后**跑：相邻判断跨批，批内看不出跨批的重复。
    order = sorted(by_index)
    metas = {i: m for i, _p, m in todo}
    prev = None
    for i in order:
        sl = by_index[i]
        if sl.get('layout') == prev and sl.get('layout') != 'section_divider':
            meta = metas.get(i)
            rest = _diversity_alternatives((meta or {}).get('candidates'), prev)
            if meta is not None and rest:
                log('[plan] 第 %d 页与上一页同为 %s，改用 %s' % (i + 1, prev, rest[0]))
                by_index[i] = _heuristic_slide(
                    meta['page'], meta['section'], meta['blocks'],
                    candidates=rest, taken=used)
        prev = by_index[i].get('layout')

    slides = [by_index[i] for i in order]
    plan = _normalise_plan(slides, outline, log)
    if off_candidate:
        plan['_warnings'] = ['%d 页的版式不在候选集内，已改用确定性生成'
                             % off_candidate]
    return plan


def _plan_batch(chunk: list[dict], src: str, cfg, used: dict, total: int) -> list[dict]:
    used_txt = ('已用过的版式及次数：%s（不要再堆同一种）'
                % (used or '无')) if used else '这是第一批。'
    prompt = f"""你在把一份大纲落成具体的幻灯片。整份共 {total} 页，本批需要处理 {len(chunk)} 页。

{layout_catalog()}

{DESIGN_RULES}

{used_txt}

本批要处理的大纲页（每页的 `candidates` 是**这一页可以用的版式**）：
{json.dumps(chunk, ensure_ascii=False, indent=1)}

源文档内容（供你取用具体事实、数字与原文表述）：
<document>
{src}
</document>

请为上面**每一页**各产出一个幻灯片定义，**顺序与给定顺序一致**。要求：
- 每页的 `layout` 必须从**该页 candidates 里的名字**中选，不许用别的版式。
  候选按贴合度排列，排在前面的更贴题。
- 严格按该版式的字段填写，不要自创字段。
- **严格遵守该版式的容量上限**（目录里逐条写明了条数与字数）。
- 内容必须来自源文档，不得编造数字或事实；图表的数据点必须是原文里有的。
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
    flat = [(s['name'], p) for s in outline['sections'] for p in s['pages']]
    roles = _page_roles([p for _, p in flat])
    content_of = _content_index(doc)

    slides, taken, prev = [], {}, None
    for (sec, p), role in zip(flat, roles):
        if p.get('divider'):
            slides.append(_divider_slide(p, sec))
            prev = 'section_divider'
            continue
        blocks = content_of(p)
        cands = page_candidates(p, role, blocks)
        # 相邻页不得同版式 —— 这条规则与 LLM 路径共用，兜底路径同样受约束
        if prev in cands:
            cands = _diversity_alternatives(cands, prev)
        sl = _heuristic_slide(p, sec, blocks, candidates=cands, taken=taken)
        taken[sl['layout']] = taken.get(sl['layout'], 0) + 1
        prev = sl['layout']
        slides.append(sl)
    return _normalise_plan(slides, outline, log)


_SPLIT_RE = re.compile(r'^(.{2,14}?)\s*[：:，,。；;]\s*(.+)$')


def _split_item(t: str) -> tuple[str, str]:
    """把一句正文切成「小标题 + 说明」。切不出来时退化为前 12 字 + 余下。"""
    t = re.sub(r'^[\d①-⑩]{1,2}[\s、.·]+', '', t.strip())
    m = _SPLIT_RE.match(t)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return (t[:12], t[12:].strip()) if len(t) > 14 else (t, '')


# 无模型时**能确定性构造**的版式。别的版式需要模型才有的信息（状态、层级名、
# 图表的序列值、总纲句……），硬凑出来就是编造 —— 宁可不用，也不能编数字。
_BUILDABLE = ('data_table', 'quadrant', 'numbered_columns', 'tinted_bands',
              'split_main_aside', 'process_chain', 'timeline_vertical',
              'node_flow', 'statement')

# 条目可以**裁剪**的版式（多出来的条目去掉、排版仍然成立）。候选集用光时
# 从这几个里借一个来避开相邻重复 —— 裁掉两条，也好过连着三页同一种构图。
_TRIM_FRIENDLY = ('tinted_bands', 'split_main_aside', 'numbered_columns',
                  'timeline_vertical')


def _diversity_alternatives(cands: list[str], prev: str) -> list[str]:
    """相邻去重时能换成什么。候选里还有别的就用别的；用光了才借可裁剪的版式。"""
    rest = [c for c in (cands or []) if c != prev]
    if rest:
        return rest
    return [c for c in _TRIM_FRIENDLY if c != prev]

_STEP_RE = re.compile(
    r'^\s*(?:\d{1,2}\s*[、.．)）]|第[一二三四五六七八九十]+步|首先|然后|接着|最后'
    r'|step\s*\d)', re.I)


def _looks_like_steps(items: list[str]) -> bool:
    """这些条目看起来是**有序步骤**吗（而不是并列要点）。"""
    hits = sum(1 for i in items if _STEP_RE.match(i))
    return hits >= max(2, len(items) // 2)


def _heuristic_slide(page: dict, section: str, blocks: list[dict], *,
                     candidates: list[str] | None = None,
                     taken: dict | None = None) -> dict:
    """确定性版式选择：无模型时用它，模型给了候选外版式的那一页也用它。

    这一层**注定只是兜底** —— 内容的取舍与标题的主张都依赖模型。它只保证四件事：
    有内容、版式不与相邻页重复、**字段与版式一定对得上**、一定能渲染。

    `candidates` 是已按意图与容量筛过的候选集（见 `page_candidates`），
    `taken` 是用过的版式计数 —— 在候选里优先挑没用过的，避免整份 deck
    退化成同一个版式（这正是「18 页里 12 页同一种版式」的成因）。
    """
    paras = [b['text'] for b in blocks if b['type'] == 'para']
    bullets = [i for b in blocks if b['type'] == 'bullets' for i in b['items']]
    tables = [b for b in blocks if b['type'] == 'table']
    src = page.get('source') or ('Source: %s' % section)
    base = dict(kicker=section, source=src)
    title = page.get('title') or section
    tbl = tables[0] if tables else None

    allowed = [c for c in (candidates or _BUILDABLE) if c in _BUILDABLE] or ['statement']
    used = taken or {}

    def pick(*order) -> str:
        pool = [c for c in order if c in allowed] or allowed
        fresh = [c for c in pool if not used.get(c)]
        return (fresh or pool)[0]

    items = [i.strip() for i in (bullets or paras) if (i or '').strip()]
    n = len(items)
    # node_flow 的节点框只有 2.30" 宽，13pt 单行约 12 字。源条目更长时把它排除 ——
    # 兜底路径没有模型来压文案，硬用只会被 `_fit()` 截成残句。
    if items and max(len(i) for i in items) > 13:
        allowed = [c for c in allowed if c != 'node_flow'] or ['statement']

    if n == 0:
        return dict(layout='statement',
                    lines=[[(title, {'hl': True})]], **base)

    if tbl and n <= 5 and 'split_main_aside' in allowed:
        want = 'split_main_aside'          # 有表又有要点 —— 一页讲两件事
    elif tbl and 'data_table' in allowed:
        want = 'data_table'
    elif n <= 2:
        want = pick('statement')
    elif n == 4 and 'quadrant' in allowed:
        want = pick('quadrant', 'numbered_columns', 'tinted_bands')
    elif 3 <= n <= 5 and 'process_chain' in allowed and _looks_like_steps(items):
        want = pick('process_chain', 'numbered_columns')
    elif n <= 9:
        want = pick('numbered_columns', 'tinted_bands', 'split_main_aside')
    else:
        want = pick('timeline_vertical', 'numbered_columns', 'tinted_bands')

    if want == 'data_table':
        return dict(layout='data_table', title=title, header=tbl['header'],
                    rows=tbl['rows'][:8], **base)
    if want == 'split_main_aside':
        spec = dict(layout='split_main_aside', title=title,
                    items=[dict(name=a, desc=b)
                           for a, b in map(_split_item, items[:5])], **base)
        if tbl:
            spec['aside_table'] = dict(header=tbl['header'], rows=tbl['rows'][:4])
        return spec
    if want == 'quadrant':
        return dict(layout='quadrant', title=title,
                    items=[dict(name=a, desc=b)
                           for a, b in map(_split_item, items[:4])], **base)
    if want == 'process_chain':
        steps = []
        for k, it in enumerate(items[:5], 1):
            nm, ds = _split_item(it)
            steps.append(dict(num='%02d' % k, name=nm, desc=ds))
        return dict(layout='process_chain', title=title, steps=steps, **base)
    if want == 'timeline_vertical':
        return dict(layout='timeline_vertical', title=title,
                    steps=[dict(name=a, desc=b)
                           for a, b in map(_split_item, items[:8])], **base)
    if want == 'node_flow':
        return dict(layout='node_flow', title=title,
                    nodes=[i[:13] for i in items[:8]], **base)
    if want == 'tinted_bands':
        return dict(layout='tinted_bands', title=title,
                    bands=[dict(name=a, desc=b)
                           for a, b in map(_split_item, items[:4])], **base)
    if want == 'numbered_columns':
        return dict(layout='numbered_columns', title=title, columns=3,
                    items=[dict(name=a, desc=b)
                           for a, b in map(_split_item, items[:9])], **base)
    return dict(layout='statement', lines=[[(title, {})]],
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
        # 章节隔断页不给 kicker：它自己就是章节名，再补一行小字到左上角
        # 等于把同一个名字写两遍。
        if (sl['layout'] != 'section_divider'
                and not (sl.get('kicker') or '').strip()
                and i - 1 < len(sections)):
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
