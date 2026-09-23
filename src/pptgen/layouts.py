# -*- coding: utf-8 -*-
"""内置的 19 套内容版式。每套是一个 `render(slide, spec)` 函数，spec 为 dict。

**这里只有内置版式。** 用户从截图识别出来的自定义版式不走这条路 —— 它们是
声明式的（`layouts_custom/*.json` + `layout_dsl.render_blocks`），在
`layout_store.load_all()` 里注册进同一份 `LAYOUTS` / `REGISTRY`。

版式**元数据与渲染函数写在一起**（`@layout(...)` 装饰器），登记进
`layout_spec.REGISTRY`。喂给模型的版式目录与压文案的容量预算都由那份注册表
生成 —— 在这之前它们是三份手写清单，已经漂移（`stats.label` 一处 ≤20 字、
一处 ≤22 字）。

版式清单（每套构图不同）：

  结构骨架  section_divider     章节隔断（大号章节号 + 章节名）
  陈述      statement           大字陈述 + 支撑段
  指标      stat_hero           单焦点大数字 + 一排支撑数据
            kpi_grid            3–6 个等权指标卡（多焦点）
  概念      definition          术语定义
  并列      numbered_columns    分栏编号列表
            tinted_bands        通栏浅色带
            quadrant            四象限
  对照      comparison_rows     维度对照（A 列 / B 列）
  复合      split_main_aside    主区（编号要点）+ 辅区（小表/数字/要点）
  流程      process_chain       横向流程链
            phase_grouped_flow  阶段分组流程（左侧阶段栏 + 组内并列）
            timeline_vertical   纵向时间线
  层级      layered_stack       分层架构（层 × 模块）
  数据      data_table          原生表格
            metric_trend        原生图表（趋势 / 占比 / 排行）
  状态      progress_checklist  状态进度清单
  结论      executive_summary   结论先行（总纲 + 若干条完整结论）
  引语      quote               引语

约定：spec 里的富文本用 `[(text, opts), ...]`；opts 里 `hl=True` 表示着强调色（RED）。
"""
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn

from . import layout_spec
from .layout_spec import LayoutSpec
from .tokens import (
    LEFT, RIGHT, W, FS, RED, DARK, MUTED, GREY, RULE, TINT, Y_BOTTOM,
    EA, LAT, Y_CONTENT, Y_SOURCE,
    put, hrule, vrule, dot, outline_box, tint_band, table, paras, runs,
    header, footer, fit_one_line, fit_block, text_w_in,
)

# 正文下界 `Y_BOTTOM` 从 tokens 导入 —— 声明式版式（自定义版式）用同一个，
# 常量留在两个文件里迟早会分叉。

__all__ = ['LAYOUTS', 'render_slide', 'LAYOUT_NAMES']


# ══════════════════════════════════════════════════════════════
# 注册 —— 渲染函数与元数据写在一起
# ══════════════════════════════════════════════════════════════
LAYOUTS: dict = {}


def layout(name: str, **meta):
    """登记一套版式：渲染函数 + 元数据。两者写在同一个地方，不可能再漂移。"""
    def deco(fn):
        LAYOUTS[name] = fn
        layout_spec.register(LayoutSpec(name=name, **meta))
        return fn
    return deco


def _w_in(text, pt):
    """估算一段文字占用的宽度（英寸）。量宽模型在 tokens（与折行/截断同一套）。"""
    return text_w_in(text, pt)


def _fit(text, avail_in, pt, lines=1):
    """把文字裁到 `lines` 行内放得下。

    有些框（大数字、步骤名、节点标签、脚注）高度只够固定行数，模型一旦写长了
    就会折行溢出。几何检查只能报「装不下」，修复回环又可能因为「不得改动数字」
    而改不动 —— 这类确定性超标由程序截断最可靠。

    ⚠️ 截断是**静默**的：它让文字不再溢出，于是几何检查全绿，问题只在肉眼看渲染图
    时才暴露（历史案例：已删除的 `node_flow` 8 个节点里 6 个被截成残句
    `小红书正文 · 爆款写作…`，而几何报告是干净的 —— 这条实测就是 `TRUNCATIONS`
    与 `max_item_chars` 两个机制的由来，别因为那个版式没了就把它们当冗余删掉）。
    所以 `fit_one_line` 会把每次截断记进 `tokens.TRUNCATIONS`，
    由 `build()` 收走并写进日志。
    """
    return fit_one_line(str(text), avail_in, pt, lines=lines)


def _flat(lines) -> str:
    """段落列表压成纯文本（用于按宽度截断的场合）。

    形参不叫 `paras` —— 那个名字是同名归一函数的，遮住它是个陷阱。
    """
    return ''.join(t for p in paras(lines) for t, _ in p)


# ══════════════════════════════════════════════════════════════
# 结构骨架
# ══════════════════════════════════════════════════════════════
@layout('section_divider',
        roles=('section',),
        intents=(),
        min_items=None, max_items=None,
        signature='章节隔断：超大章节号 + 章节名 + 一句导语，几乎全留白',
        best_for='每章开头，宣告进入新章节',
        avoid_for='内容页（它只有 2 个元素，放不下正文）',
        fallback=('statement',),
        reuse_friendly=True,
        catalog='''适用于**章节隔断页**（每章第一页）。
   必填 num（章节号，≤2 字符，如 "03"）、title（章节名，≤14 字）
   选填 lead（一句导语，≤40 字）、source
   ⚠️ 这是结构页：**不要放正文、不要放列表**。它的作用是让读者看见「换章了」。''',
        capacity='num ≤2 字符；title ≤14 字（40pt 一行放得下约 14 字，'
                 '源章名带着副题时压成短语，完整说法交给本页的 lead）；'
                 'lead ≤40 字，超过就删。')
def render_section_divider(s, spec):
    """章节隔断：大号章节号在左，章节名与导语在右，中间一条竖发丝线。

    构图刻意撑满上半页：早先把元素全挤在左上角，几何检查的
    `large_empty_area` 每次都报「最大连续空白块约占 50%」—— 安静是隔断页的
    设计意图，但**空**不是：大号数字 + 竖线 + 章节名这一组要把版面立住。

    标题**折行而不是截断**：源文档的章名常常是「章名　—　副题」一整串
    （`structure.skeleton_digest` 就是这么拼的），40pt 一行只放得下约 14 字，
    早先 30 字的章名被 `_fit()` 截成「01 初识 Dify　—　什么是 Di…」。
    现在先降到能一行放下的字号，实在降不下来就折两行，都不行才截断。
    """
    header(s, spec.get('kicker'))
    num = _fit(spec.get('num', ''), 3.0, 120)
    put(s, LEFT, 2.30, 3.20, 2.30,
        [[(num, dict(size=120, color=RED, bold=True))]])
    vrule(s, LEFT + 3.30, 2.40, 2.10, RULE)
    if spec.get('title'):
        # 盒子不动（一行时位置与早先完全一致）；两行时向下长到 4.08"，
        # 仍在发丝线（4.85"）之上。
        lines, size = fit_block(spec['title'], W - 3.60, 1.60,
                                sizes=(40, 36, 32, 28, 24), max_lines=2)
        put(s, LEFT + 3.60, 2.75, W - 3.60, 1.60,
            [[(ln, dict(size=size, color=DARK, bold=True))] for ln in lines],
            ls=1.20)
    hrule(s, LEFT, 4.85, W)
    if spec.get('lead'):
        put(s, LEFT, 5.05, 9.60, 0.80,
            [[(spec['lead'], dict(size=15, color=MUTED))]], ls=1.40)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
@layout('statement',
        roles=('content',),
        intents=('statement',),
        signature='大字陈述 —— 陈述本身就是标题，没有标题位',
        best_for='开篇、章节引言、一句话主张',
        avoid_for='需要罗列多条并列信息的页',
        fallback=('quote',),
        reuse_friendly=True,
        catalog='''大字陈述。适合开篇、章节引言、一句话主张。
   必填 lines: [[(文本,{})], ...] 每行一段；关键词用 {"hl":true} 着强调色。
   选填 body: [段落...]（最多 3 段、每段 ≤60 字）、source
   ⚠️ 没有标题位 —— 陈述本身就是标题，别给 title。''',
        capacity='lines 每行 ≤22 字，最多 2 行；body 最多 3 段、每段 ≤60 字。')
def render_statement(s, spec):
    """大字陈述。没有标题——陈述本身就是标题。"""
    header(s, spec.get('kicker'))
    lines = paras(spec['lines'])
    for p in lines:
        for t, o in p:
            o['size'] = spec.get('size', 36)
            o.setdefault('color', DARK)
            o.setdefault('bold', True)
    put(s, LEFT, spec.get('y', 2.40), 11.0, 2.4, lines, ls=1.34)
    y = spec.get('body_y', 5.05)
    if spec.get('body'):
        put(s, LEFT, y, 10.6, 1.7, paras(spec['body']), ls=1.45)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
# 指标
# ══════════════════════════════════════════════════════════════
@layout('stat_hero',
        roles=('content',),
        intents=('quantitative',),
        min_items=0, max_items=3,
        item_chars=22, total_chars=260,
        requires=('numbers',),
        signature='单焦点：一个 72pt 大数字 + 右侧说明 + 一排支撑数据',
        best_for='一个数字最重要、需要压倒性呈现的核心指标页',
        avoid_for='4 个以上等权指标（那些用 kpi_grid）',
        fallback=('kpi_grid', 'data_table'),
        reuse_friendly=True,
        catalog='''一个大数字 + 右侧说明 + 一排支撑数据。适合**单一**核心指标页。
   必填 hero: {"num":"87","unit":" 亿美元"}
     ⚠️ num 必须是**数字**（≤8 字符），unit 是单位（≤10 字符）。
        不要把文字标题塞进 num —— 那个位置只放得下一行 72pt 的数字。
   选填 claim: [段落...]（≤3 段、每段 ≤40 字）
        stats: [{"num":"38 家","label":"说明"}]，**2–3 个**，
               num 同为短数字（≤8 字符），label ≤20 字
        source
   ⚠️ 有 4 个以上指标就别用这套（stats 只装得下 3 个），改用 kpi_grid。''',
        capacity='hero.num ≤8 字符；claim 最多 3 段、每段 ≤40 字；'
                 'stats 最多 3 条，每条 label ≤20 字、num ≤8 字符。')
def render_stat_hero(s, spec):
    """一个 72pt 焦点数字 + 右侧说明 + 发丝线 + 一排支撑数据。"""
    header(s, spec.get('kicker'), spec.get('title'))
    h = spec['hero']
    # hero 框高 1.5"，72pt 只够一行（两行是 204pt）。数字不能动，压单位。
    num = str(h['num'])
    unit = str(h.get('unit', ''))
    unit = _fit(unit, max(4.1 - _w_in(num, 72), 0.4), 22)
    put(s, LEFT, 2.05, 4.1, 1.5,
        [[(num, dict(size=72, color=RED, bold=True)),
          (unit, dict(size=22, color=MUTED))]])
    if spec.get('claim'):
        put(s, 4.85, 2.42, W - 4.18, 1.5, paras(spec['claim']), ls=1.42)
    hrule(s, LEFT, 4.62, W)
    stats = spec.get('stats') or []
    if stats:
        stats = stats[:3]                       # 容量上限 3 条，多出来的直接丢
        cw = W / len(stats)
        for i, st in enumerate(stats):
            x = LEFT + i * cw
            # 数字框高 0.6"，30pt 只够一行 —— 必须截断，否则折行溢出
            # 支撑数字用 DARK 而不是 BLUE：一页里只允许 hero 那一个红焦点，
            # 支撑数据是次级信息，走正文色。
            put(s, x, 4.90, cw - 0.45, 0.6,
                [[(_fit(st['num'], cw - 0.45, 30),
                   dict(size=30, color=DARK, bold=True))]])
            put(s, x, 5.56, cw - 0.45, 0.9,
                [[(st['label'], dict(size=FS['small'], color=MUTED))]], ls=1.3)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
@layout('kpi_grid',
        roles=('content',),
        intents=('quantitative',),
        min_items=3, max_items=6,
        item_chars=14, total_chars=220,
        requires=('numbers',),
        signature='多焦点等权指标卡网格（3–6 个同级数字，一张卡一个指标）',
        best_for='一组指标要同时看：仪表盘、季度数据、对比口径',
        avoid_for='只有一个数字最重要的页（那个用 stat_hero）',
        fallback=('stat_hero', 'data_table'),
        reuse_friendly=True,
        catalog='''指标卡网格：**3–6 个等权指标**，一个指标一张卡。
   必填 items: [{"num":"38","unit":"%","label":"渗透率","note":"同比 +6pt"}]
     · num 是**短数字**（≤6 字符），unit 是单位（≤4 字符，可省）
     · label 是指标名（≤10 字），note 是补充口径（≤16 字，可省）
   选填 columns（默认 3，可选 2 或 3）、source
   ⚠️ 想要「一个数字压倒一切」就用 stat_hero；这套是**多个指标同等重要**。''',
        capacity='items 3–6 条；每条 num ≤6 字符、unit ≤4 字符、'
                 'label ≤10 字、note ≤16 字。超出就删掉 note。')
def render_kpi_grid(s, spec):
    """指标卡网格：数字 + 标签 + 口径说明，发丝线分格。

    数字走 DARK 而不是 RED：一页里有 3–6 个数字，全打红会让强调色失效
    （tokens.py 写明 RED 只打在**单一**焦点上）。红色只留给 note 那一行小字。
    """
    header(s, spec.get('kicker'), spec.get('title'))
    items = spec['items'][:6]
    ncol = spec.get('columns', 3)
    ncol = 3 if ncol not in (2, 3) else ncol
    nrow = (len(items) + ncol - 1) // ncol
    gap = 0.40
    cw = (W - (ncol - 1) * gap) / ncol
    rh = min(1.70, (Y_BOTTOM - 2.15) / max(nrow, 1))
    for i, it in enumerate(items):
        col, row = i % ncol, i // ncol
        x = LEFT + col * (cw + gap)
        y = 2.15 + row * rh
        num = _fit(it['num'], cw - 0.20, 40)
        unit = str(it.get('unit', ''))
        unit = _fit(unit, max(cw - 0.30 - _w_in(num, 40), 0.30), 16)
        put(s, x, y, cw, 0.78,
            [[(num, dict(size=40, color=DARK, bold=True)),
              (unit, dict(size=16, color=MUTED))]])
        put(s, x, y + 0.82, cw, 0.36,
            [[(_fit(it['label'], cw, 14),
               dict(size=14, color=DARK, bold=True))]])
        if it.get('note'):
            put(s, x, y + 1.20, cw, 0.34,
                [[(_fit(it['note'], cw, FS['source']),
                   dict(size=FS['source'], color=RED))]])
        if row < nrow - 1:
            hrule(s, LEFT, y + rh - 0.18, W)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
# 概念
# ══════════════════════════════════════════════════════════════
@layout('definition',
        roles=('content',),
        intents=('definition',),
        signature='大词条 + 公式 + 释义（一个概念的专场）',
        best_for='解释一个概念/名词/缩写',
        avoid_for='多个概念并列（那些用 numbered_columns 或 split_main_aside）',
        fallback=('statement', 'split_main_aside'),
        reuse_friendly=True,
        catalog='''术语定义。适合解释一个概念/名词。
   必填 term（词条，≤10 字）、formula（如 "= Define + Modify"，≤26 字符）
   选填 lead（一句加粗断言，≤24 字）、body（≤110 字）、
        aside: [(文本,{})]（≤30 字）、source''',
        capacity='term ≤10 字；formula ≤26 字符；lead ≤24 字；'
                 'body ≤110 字；aside ≤30 字。')
def render_definition(s, spec):
    """术语定义：大词条 + 公式 + 释义。"""
    header(s, spec.get('kicker'), spec.get('title'))
    put(s, LEFT, 2.30, 6.0, 1.2,
        [[(spec['term'], dict(size=56, color=DARK, bold=True))]])
    put(s, LEFT, 3.45, 7.0, 0.6,
        [[(spec['formula'], dict(size=24, color=RED, bold=True))]])
    hrule(s, LEFT, 4.32, W)
    body = []
    if spec.get('lead'):
        body.append([(spec['lead'], dict(size=FS['body'], color=DARK, bold=True))])
    if spec.get('body'):
        body.append([(spec['body'], dict(size=FS['body'], color=MUTED))])
    # 正文框与 aside 框不能重叠：1.9" 高 + y=6.35 时两者相交 0.17"，
    # 被几何检查的 text_overlap 抓到。收紧为 1.55" 高、aside 下移到 6.28。
    if body:
        put(s, LEFT, 4.62, 11.0, 1.55, body, ls=1.45)
    if spec.get('aside'):
        put(s, LEFT, 6.28, 11.0, 0.35, paras(spec['aside']))
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
# 并列
# ══════════════════════════════════════════════════════════════
@layout('numbered_columns',
        roles=('content',),
        intents=('enumeration',),
        min_items=4, max_items=9,
        item_chars=22, total_chars=240,
        signature='分栏编号列表（3 栏网格 + 序号）',
        best_for='若干并列要点、功能清单、理念条目',
        avoid_for='有先后顺序的步骤（用 process_chain）、带状态的事项',
        fallback=('tinted_bands', 'split_main_aside'),
        reuse_friendly=True,
        catalog='''分栏编号列表。适合并列的若干要点。
   必填 items: [{"name":"关键词（≤8 字）","desc":"一句说明（≤22 字）"}]
   选填 columns（默认 3）、source''',
        capacity='items 4–9 条；每条 name ≤8 字、desc ≤22 字。')
def render_numbered_columns(s, spec):
    """分栏编号列表。可按 group 插入分组小标题。"""
    header(s, spec.get('kicker'), spec.get('title'))
    items = spec['items']
    ncol = spec.get('columns', 3)
    gap = spec.get('gap', 0.55)
    cw = (W - (ncol - 1) * gap) / ncol
    rowh = spec.get('row_h', 1.62)
    for i, it in enumerate(items):
        col, row = i % ncol, i // ncol
        x = LEFT + col * (cw + gap)
        y = 2.05 + row * rowh
        put(s, x, y, cw, 0.35,
            [[('%02d' % (i + 1), dict(size=15, color=RED, bold=True)),
              ('   ' + it['name'], dict(size=16, color=DARK, bold=True))]])
        put(s, x, y + 0.42, cw, 1.0,
            [[(it['desc'], dict(size=FS['small'], color=MUTED))]], ls=1.35)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
@layout('tinted_bands',
        roles=('content',),
        intents=('enumeration',),
        min_items=2, max_items=4,
        item_chars=52, total_chars=300,
        signature='通栏浅色带（左右出血的横带，每条独立、彼此无关系）',
        best_for='场景分类、并列陈述 —— 每条都要一整句说明',
        avoid_for='层内有并列模块的分层结构（用 layered_stack）',
        fallback=('numbered_columns', 'split_main_aside'),
        reuse_friendly=True,
        catalog='''通栏浅色带，2–4 条。适合场景分类、并列陈述。
   必填 bands: [{"name":"名称（≤10 字）","desc":"一到两句说明（≤52 字）"}]
   选填 source''',
        capacity='bands 2–4 条；每条 name ≤10 字、desc ≤52 字。')
def render_tinted_bands(s, spec):
    """通栏浅色带：每条带 = 序号 + 名称 + 说明，左右出血。"""
    header(s, spec.get('kicker'), spec.get('title'))
    bands = spec['bands'][:4]
    gap = spec.get('band_gap', 0.20)
    # 带高必须按条数自适应：4 条 × 默认 1.42" + 3 × 0.20" 间距 = 从 2.15 一路
    # 铺到 7.01"，最后一条整个冲出画布（几何检查报 out_of_canvas）。
    # 原文写死 1.42"，是按「最多 3 条」估的。
    avail = Y_BOTTOM - 2.15
    bh = min(spec.get('band_h', 1.42),
             (avail - (len(bands) - 1) * gap) / max(len(bands), 1))
    for i, bd in enumerate(bands):
        y = 2.15 + i * (bh + gap)
        tint_band(s, y, bh)
        # 序号框收到 0.68"，避免与名称框包围盒重叠；说明框高度绑定带高，
        # 否则最后一条会压到来源行（实测重叠 0.21"）。
        put(s, LEFT, y + 0.22, 0.68, 0.5,
            [[('%02d' % (i + 1), dict(size=24, color=RED, bold=True))]])
        put(s, LEFT + 0.80, y + 0.30, 2.6, 0.45,
            [[(_fit(bd['name'], 2.6, FS['h3']),
               dict(size=FS['h3'], color=DARK, bold=True))]])
        put(s, LEFT + 3.55, y + 0.24, W - 3.85, bh - 0.30,
            [[(bd['desc'], dict(size=14, color=MUTED))]], ls=1.40)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
@layout('quadrant',
        roles=('content',),
        intents=('enumeration', 'comparison'),
        min_items=4, max_items=4,
        item_chars=46, total_chars=240,
        signature='四象限（十字发丝线分成 4 格，格子等大、无先后）',
        best_for='四个并列维度/挑战，彼此正交',
        avoid_for='有层级关系的层（用 layered_stack）、正好 3 或 5 条',
        fallback=('numbered_columns', 'tinted_bands'),
        reuse_friendly=True,
        catalog='''四象限，**正好 4 条**。适合四个并列维度/挑战。
   必填 items: [{"name":"≤10 字","desc":"≤46 字"}] × 4
   选填 source
   ⚠️ 条数不是 4 就别用这套。''',
        capacity='items 正好 4 条；每条 name ≤10 字、desc ≤46 字。')
def render_quadrant(s, spec):
    """四象限：十字发丝线分隔，每格一个大号序号 + 标题 + 说明。"""
    header(s, spec.get('kicker'), spec.get('title'))
    vrule(s, LEFT + W / 2, 2.15, 4.45)
    hrule(s, LEFT, 4.42, W)
    for i, it in enumerate(spec['items'][:4]):
        col, row = i % 2, i // 2
        x = LEFT + col * (W / 2 + 0.35)
        y = 2.15 + row * 2.42
        # 序号框宽度收到 0.62"：原先 3.4" 会与右侧名称框的包围盒重叠 2.68"（几何检查可查）
        put(s, x, y, 0.62, 0.5,
            [[('%02d' % (i + 1), dict(size=22, color=RED, bold=True))]])
        put(s, x + 0.72, y + 0.10, 4.6, 0.4,
            [[(_fit(it['name'], 4.6, FS['h2']),
               dict(size=FS['h2'], color=DARK, bold=True))]])
        put(s, x, y + 0.72, W / 2 - 0.55, 1.5,
            [[(it['desc'], dict(size=14, color=MUTED))]], ls=1.4)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
# 对照
# ══════════════════════════════════════════════════════════════
@layout('comparison_rows',
        roles=('content',),
        intents=('comparison',),
        min_items=2, max_items=5,
        item_chars=34, total_chars=300,
        signature='维度对照表（每行一个维度，A 列 / B 列并排）',
        best_for='前后对比、方案 A/B、优劣对照',
        avoid_for='不是对照关系的内容（别为了凑两列把并列要点硬拆成 A/B）',
        fallback=('data_table', 'split_main_aside'),
        reuse_friendly=True,
        catalog='''维度对照（A 列 / B 列），2–5 行。适合前后对比、优劣对比。
   必填 col_a、col_b（两个对比方的名字，各 ≤10 字）
        rows: [{"dim":"维度（≤6 字）","a":"…（≤34 字）","b":"…（≤34 字）"}]
   选填 source
   ⚠️ 只有内容**确实是 A 与 B 的对照**时才用；三条并列要点不是对照。''',
        capacity='rows 2–5 行；每行 dim ≤6 字、a 与 b 各 ≤34 字。')
def render_comparison_rows(s, spec):
    """维度对照：维度 | A 列 | B 列，逐行发丝线分隔。"""
    header(s, spec.get('kicker'), spec.get('title'))
    x1, x2 = LEFT, LEFT + 2.5
    cw = 4.35
    x3 = LEFT + 6.9
    put(s, x1, 2.10, 2.3, 0.4, [[('维度', dict(size=FS['small'], color=MUTED, bold=True))]])
    put(s, x2, 2.10, cw, 0.4, [[(spec['col_a'], dict(size=FS['small'], color=GREY, bold=True))]])
    put(s, x3, 2.10, cw, 0.4, [[(spec['col_b'], dict(size=FS['small'], color=DARK, bold=True))]])
    hrule(s, LEFT, 2.46, W)
    # 行高自适应：固定 1.32" 时 4 行会溢出到 y=7.78（超出 7.50 画布），
    # 实测被 officecli 与本项目几何检查同时抓到。按可用高度均分即可。
    y0 = 2.62
    n = max(len(spec['rows']), 1)
    # 上界取到来源行之前，否则最后一行的框会压住来源行（实测重叠 0.09"）
    rh = min(spec.get('row_h', 1.32), (Y_SOURCE - 0.10 - y0) / n)
    y = y0
    for row in spec['rows']:
        put(s, x1, y + 0.04, 2.3, 0.4,
            [[(row['dim'], dict(size=15, color=DARK, bold=True))]])
        put(s, x2, y, cw, rh - 0.08, [[(row['a'], dict(size=14, color=GREY))]], ls=1.38)
        put(s, x3, y, cw, rh - 0.08, [[(row['b'], dict(size=14, color=DARK))]], ls=1.38)
        y += rh
        hrule(s, LEFT, y - 0.20, W)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
# 复合：主区 + 辅区
# ══════════════════════════════════════════════════════════════
@layout('split_main_aside',
        roles=('content',),
        # 复合版式对**多数意图**开放：实测约三成的大纲 hint 是「主 + 辅」两件事
        # （「五步流程图 + 六参数对照表」「三栏卡片 + 能力标签带」），单一构图的
        # 版式装不下它们，只能用这套。它是兜底而非首选 —— 候选排序会把它排在
        # 意图更专一的版式之后（见 layout_spec.candidates 的排序）。
        intents=('enumeration', 'comparison', 'definition', 'process',
                 'quantitative', 'timeline'),
        min_items=3, max_items=5,
        item_chars=40, total_chars=380,
        signature='主区（左侧编号要点）+ 辅区（右侧小表/数字/要点），中间一条竖发丝线',
        best_for='一页讲两件事：「五条要点 + 一张版本表」「三个能力 + 一组数字」',
        avoid_for='单一构图的内容（用更专用的版式，别用这套凑）',
        fallback=('numbered_columns', 'data_table'),
        reuse_friendly=True,
        catalog='''**主区 + 辅区**复合版式：左侧是编号要点（主），右侧是佐证材料（辅）。
   适合大纲 hint 写成「左侧 N 条要点 + 右侧一张表 / 一组数字」这类**复合**内容 ——
   以前的 12 套版式全是单一构图，这类内容只能丢掉一半。
   必填 items: [{"name":"要点名（≤10 字）","desc":"一句说明（≤40 字）"}]，3–5 条
   辅区**三选一**（不给就不画辅区）：
     aside_table: {"header":["列1","列2"], "rows":[["…","…"], ...]}  2–4 行
     aside_stats: [{"num":"50,186","label":"≤14 字"}]  2–4 条
     aside_points: ["一句要点", ...]  2–4 条
   选填 aside_title（辅区小标题，≤10 字）、source''',
        capacity='主区 items 3–5 条，每条 name ≤10 字、desc ≤40 字；'
                 'aside_table 最多 4 行、单元格 ≤18 字；'
                 'aside_stats 最多 4 条、label ≤14 字；aside_points 最多 4 条、每条 ≤26 字。')
def render_split_main_aside(s, spec):
    """主区 + 辅区。辅区按 aside_table / aside_stats / aside_points 依次降级。"""
    header(s, spec.get('kicker'), spec.get('title'))
    main_w = 6.95
    ax = LEFT + main_w + 0.42                 # 辅区左边界
    aw = RIGHT - ax                           # 辅区宽度
    vrule(s, LEFT + main_w + 0.16, 2.15, Y_BOTTOM - 2.15, RULE)

    items = spec['items'][:5]
    n = max(len(items), 1)
    rh = min(1.05, (Y_BOTTOM - 2.15) / n)
    y = 2.15
    for i, it in enumerate(items):
        put(s, LEFT, y, main_w, 0.34,
            [[('%02d' % (i + 1), dict(size=14, color=RED, bold=True)),
              ('   ' + _fit(it['name'], main_w - 0.55, 16),
               dict(size=16, color=DARK, bold=True))]])
        put(s, LEFT, y + 0.38, main_w, rh - 0.42,
            [[(it['desc'], dict(size=FS['small'], color=MUTED))]], ls=1.34)
        y += rh

    if spec.get('aside_title'):
        put(s, ax, 2.15, aw, 0.32,
            [[(_fit(spec['aside_title'], aw, 14),
               dict(size=14, color=DARK, bold=True))]])
    top = 2.58 if spec.get('aside_title') else 2.20
    # 局部变量名不能叫 `table` —— 那会遮住 tokens 里同名的表格原语，
    # 本函数后半段正是要调它。
    aside_tbl = spec.get('aside_table')
    stats = (spec.get('aside_stats') or [])[:4]
    points = (spec.get('aside_points') or [])[:4]
    if aside_tbl and aside_tbl.get('header') and aside_tbl.get('rows'):
        data = [aside_tbl['header']] + aside_tbl['rows'][:4]
        # 辅区表紧凑一档：字小一号、边距收一半、表头行更矮，行高下限 0.30"
        # 撑住最小值（辅区宽度只有 4" 上下，行再矮就压字了）。
        table(s, ax, top, aw, min(Y_BOTTOM - top, 0.46 * len(data)), data,
              font=12, header_font=12, header_h=0.42, min_row_h=0.30,
              margin=(0.08, 0.03))
    elif stats:
        rh2 = min(1.00, (Y_BOTTOM - top) / len(stats))
        for i, st in enumerate(stats):
            yy = top + i * rh2
            put(s, ax, yy, aw, 0.50,
                [[(_fit(st['num'], aw, 26),
                   dict(size=26, color=DARK, bold=True))]])
            put(s, ax, yy + 0.52, aw, 0.40,
                [[(_fit(st['label'], aw, FS['small']),
                   dict(size=FS['small'], color=MUTED))]], ls=1.30)
    elif points:
        for i, tx in enumerate(points):
            yy = top + i * 0.62
            put(s, ax, yy, 0.22, 0.30,
                [[('·', dict(size=15, color=RED, bold=True))]])
            put(s, ax + 0.24, yy, aw - 0.24, 0.56,
                [[(tx, dict(size=FS['small'], color=MUTED))]], ls=1.32)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
# 流程
# ══════════════════════════════════════════════════════════════
@layout('process_chain',
        roles=('content',),
        intents=('process',),
        min_items=3, max_items=5,
        item_chars=24, total_chars=200,
        signature='横向流程链（序号 + 发丝线上的标记点 + 名称 + 两行说明）',
        best_for='3–5 步操作步骤、实施路径',
        avoid_for='超过 5 步（用 phase_grouped_flow 或 timeline_vertical）',
        fallback=('timeline_vertical', 'phase_grouped_flow'),
        reuse_friendly=True,
        catalog='''横向流程链，3–5 步。适合操作步骤、实施路径。
   必填 steps: [{"num":"01","name":"步骤名（≤7 字）",
                "desc":"两行短句，用 \\n 分隔，每行 ≤7 字"}]
   选填 note: [(文本,{})]（≤40 字）、source''',
        capacity='steps 3–5 步；每步 name ≤7 字、desc 两行每行 ≤7 字；note ≤40 字。')
def render_process_chain(s, spec):
    """横向流程链：序号 + 标记点落在发丝线上 + 名称 + 说明。"""
    header(s, spec.get('kicker'), spec.get('title'))
    steps = spec['steps']
    n = len(steps)
    gap = 0.34
    cw = (W - (n - 1) * gap) / n
    hrule(s, LEFT, 3.22, W)
    for i, st in enumerate(steps):
        x = LEFT + i * (cw + gap)
        put(s, x, 2.35, cw, 0.55,
            [[(st['num'], dict(size=26, color=RED, bold=True))]])
        # 标记点走次要灰（accent2）：它是结构分隔，不是焦点，红色留给步骤序号
        dot(s, x, 3.165, 0.11, GREY)
        # 步骤名框高 0.45"，17pt 只够一行
        put(s, x, 3.52, cw, 0.45,
            [[(_fit(st['name'], cw, FS['h3']),
               dict(size=FS['h3'], color=DARK, bold=True))]])
        desc = st['desc'].split('\n') if isinstance(st['desc'], str) else st['desc']
        put(s, x, 4.02, cw, 1.7,
            [[(ln, dict(size=FS['small'], color=MUTED))] for ln in desc], ls=1.38)
    if spec.get('note'):
        # note 常被写成多段。0.45" 的框只够一行，所以把规则线与 note 上移、
        # 给出 0.78"（≈56pt）的高度容纳两行，再按两行宽度截断。
        hrule(s, LEFT, 5.80, W)
        flat = ''.join(t for p in paras([spec['note']]) for t, _ in p)
        put(s, LEFT, 5.95, W, 0.78,
            [[(_fit(flat, W, 15, lines=2), dict(size=15, color=MUTED))]], ls=1.42)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
@layout('phase_grouped_flow',
        roles=('content',),
        intents=('process', 'timeline'),
        # 条数按**阶段数**算（2–4），且不参与预筛：源文的条目是**节点**，
        # 阶段是它们的再分组，拿节点数去卡阶段数会误杀（见 LayoutSpec.filter_items）。
        min_items=2, max_items=4, filter_items=False,
        item_chars=20, total_chars=320,
        # 节点标签是**单行定高**字段，写长了会被 `_fit()` 静默截成残句。
        # 框宽 3.09"（3 节点时）折两行约 34 字 —— 这是硬边界。
        max_item_chars=34,
        signature='阶段分组流程：左侧阶段名竖栏 + 组内节点横向并列，纵向堆叠',
        best_for='节点多（6–12 个）且标签较长的流程；内容能按阶段分组',
        avoid_for='3–5 步的简单流程（用 process_chain）',
        fallback=('timeline_vertical', 'numbered_columns'),
        reuse_friendly=True,
        catalog='''阶段分组流程：把节点**按阶段分组**，每组一行，组内节点横向并列。
   必填 phases: [{"name":"阶段名（≤6 字）","nodes":["节点标签", ...]}]
     · 2–4 个阶段，每组 1–4 个节点，总节点 4–12 个
     · 节点标签 ≤20 字（框宽，可折两行；超过会被截断）
   选填 source
   ⚠️ 节点多、标签长时**优先用这套**：它是本套版式里唯一能让节点框折两行、
      容下长标签的构图（框宽 3.09"、折两行约 34 字）。''',
        capacity='2–4 个阶段、每组 1–4 个节点、总节点 ≤12；'
                 '阶段名 ≤6 字；节点标签 ≤20 字。')
def render_phase_grouped_flow(s, spec):
    """阶段分组流程：左侧阶段名 + 组内并列节点框。"""
    header(s, spec.get('kicker'), spec.get('title'))
    phases = spec['phases'][:4]
    m = max(len(phases), 1)
    y0 = 2.15
    rh = min(1.45, (Y_BOTTOM - y0) / m)
    lab_w = 1.75
    vrule(s, LEFT + lab_w + 0.16, y0, rh * m - 0.20, RULE)
    x0 = LEFT + lab_w + 0.42
    avail = RIGHT - x0
    gap = 0.26
    for r, ph in enumerate(phases):
        y = y0 + r * rh
        nodes = (ph.get('nodes') or [])[:4]
        put(s, LEFT, y + 0.10, lab_w, 0.36,
            [[(_fit(ph['name'], lab_w, FS['h3']),
               dict(size=FS['h3'], color=DARK, bold=True))]])
        put(s, LEFT, y + 0.50, lab_w, 0.30,
            [[('%02d' % (r + 1), dict(size=FS['source'], color=RED, bold=True))]])
        k = max(len(nodes), 1)
        bw = (avail - (k - 1) * gap) / k
        bh = min(0.86, rh - 0.34)
        for c, nm in enumerate(nodes):
            x = x0 + c * (bw + gap)
            b = outline_box(s, x, y + 0.12, bw, bh)
            tf = b.text_frame
            tf.word_wrap = True
            tf.vertical_anchor = MSO_ANCHOR.MIDDLE
            # 内边距必须为 0：几何检查会报 textbox_margin（非 0 会与描边框对不齐）
            tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.CENTER
            run = p.add_run()
            # 框宽 3.09"（3 节点）时 13pt 单行约 17 字，折两行约 34 字
            run.text = _fit(nm, bw - 0.14, FS['small'], lines=2)
            run.font.size = Pt(FS['small'])
            run.font.bold = True
            run.font.color.rgb = DARK
            run.font.name = LAT
            rPr = run._r.get_or_add_rPr()
            el = rPr.makeelement(qn('a:ea'), {})
            el.set('typeface', EA)
            rPr.append(el)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
@layout('timeline_vertical',
        roles=('content',),
        intents=('timeline', 'process'),
        min_items=5, max_items=8,
        item_chars=22, total_chars=250,
        # 左右交替后每条只占半幅（5.245"），name 与 desc 各自单行。超了就折行顶破
        # 0.56" 的框高 —— 硬上限 = name 8 字 + desc 单行 28 字。`overflow_reason`
        # 量的是 `pipeline._items` 把 name+desc **拼起来**的那条，所以写 36。
        max_item_chars=36,
        signature='纵向时间线（轴线居中、左右交替，每步「序号 + 名称」+ 一句说明）',
        best_for='步骤较多（5–8 条）、每条一句话、有时间推进感',
        avoid_for='说明需要三行以上（左右交替后每条只占半幅，说明只有一行）',
        fallback=('phase_grouped_flow', 'numbered_columns'),
        reuse_friendly=True,
        catalog='''纵向时间线，5–8 步。轴线居中，奇数步靠左、偶数步靠右。
   适合步骤较多、每条一句话、有时间推进感的场景。
   必填 steps: [{"name":"步骤名（≤8 字）","desc":"一句说明（≤22 字）"}]
   选填 source''',
        capacity='steps 5–8 条；每条 name ≤8 字、desc ≤22 字（单行硬上限 28 字，'
                 'name+desc 合计 ≤36 字）。')
def render_timeline_vertical(s, spec):
    """纵向时间线：轴线居中，左右交替，每步两行「序号 + 名称 / 说明」。

    轴线落在**版心中心**而不是幻灯片中心 —— 公司模板的版心左右边距本就不对称
    （左 0.67 / 右 1.33，右侧留给装饰弧线），用版心中心两半才等宽。
    """
    header(s, spec.get('kicker'), spec.get('title'))
    steps = spec['steps']
    n = len(steps)

    cx = LEFT + W / 2.0             # 6.335
    axis_w = 0.75 / 72.0            # vrule 的线宽
    gap = 0.42                      # 文本框 ↔ 轴线
    half = cx - gap - LEFT          # 5.245 —— 左框 x=LEFT、右框 x=cx+gap，右边界正好 12.00
    h_box = 0.56                    # 两行的框高。**下限 0.522**（几何⑤ need 39.76pt
                                    #   ÷ (72×1.06)）—— 调小必报 text_overflow。
    y_top, y_bot = 2.10, 6.64       # 末项底 6.64，距来源行 6.78 留 0.14"
    span = y_bot - y_top - h_box    # 3.98
    ystep = min(0.95, span / max(n - 1, 1))
    y0 = y_top + (span - ystep * (n - 1)) / 2.0

    # 轴线：`vrule` 的 x 是**左边缘**，`dot` 的中心是 x+size/2 —— 让两者都落在 cx。
    # 纵向从首个点的上沿到末个点的下沿（点中心在 y+0.280，直径 0.11）。
    vrule(s, cx - axis_w / 2, y0 + 0.225, ystep * (n - 1) + 0.11)

    # name 的可用宽度必须**扣掉序号前缀**：几何⑤ 把段内所有 run 的宽度相加，
    # 按整幅 half 去 fit 再拼上前缀会被估成两行（need 61.1pt > 42.7pt）→ text_overflow。
    # 前缀 '01' + 两个空格 = 0.4044"，取 0.42。
    name_w = half - 0.42
    desc_w = half - 0.10

    for i, st in enumerate(steps):
        y = y0 + i * ystep
        left_side = (i % 2 == 0)
        x = LEFT if left_side else cx + gap
        align = PP_ALIGN.RIGHT if left_side else PP_ALIGN.LEFT
        # 分隔空格写进 14pt 那个 run —— 放进 name 的 run 会被按 15pt 计价
        paras = [
            [('%02d' % (i + 1), dict(size=14, color=RED, bold=True)),
             ('  ', dict(size=14, color=RED)),
             (_fit(st['name'], name_w, 15), dict(size=15, color=DARK, bold=True))],
            [(_fit(st.get('desc') or '', desc_w, FS['small']),
              dict(size=FS['small'], color=MUTED))],
        ]
        # ls=1.32 显式给定：几何按写死的 1.42 估高，ls 调大真实行高会顶破 0.56"
        # 而 QA 不报。不设 sa —— need_pt 里不含 space_after。
        put(s, x, y, half, h_box, paras, align=align,
            anchor=MSO_ANCHOR.MIDDLE, ls=1.32)

        # 标记点落在轴线上（中心 = cx），连接线从文本框连到点的边缘。
        # 点的 y 取两行文本块的**竖向中心**（= h_box/2），与 MIDDLE 锚定一致，
        # 使连接线正对两行之间的留白，不与任一行相撞。
        dot(s, cx - 0.055, y + 0.225, 0.11, GREY)
        conn_y = y + 0.2748         # 点中心 0.280 − 发丝线半宽 0.0052
        if left_side:
            hrule(s, cx - gap + 0.02, conn_y, 0.345)
        else:
            hrule(s, cx + 0.055, conn_y, 0.345)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
# 层级
# ══════════════════════════════════════════════════════════════
@layout('layered_stack',
        roles=('content',),
        intents=('hierarchy',),
        min_items=3, max_items=5, filter_items=False,   # 层是源条目的再分组
        item_chars=18, total_chars=300,
        max_item_chars=16,          # 模块框单行定高，写长了会被截断
        signature='分层架构（左侧层名 + 右侧该层内的并列模块方框，层间等宽堆叠）',
        best_for='技术栈分层、系统架构、组织中台 —— 每层内部还有若干模块',
        avoid_for='每层只有一个实体的层级（那是金字塔，不是分层架构）',
        fallback=('tinted_bands', 'numbered_columns'),
        reuse_friendly=True,
        catalog='''分层架构：**3–5 层**，每层内部有 2–4 个并列模块。
   必填 layers: [{"name":"层名（≤8 字）","modules":["模块1","模块2","模块3"]}]
     · 数组顺序 = **自上而下**；每层 2–4 个模块，模块名 ≤16 字
   选填 note（≤45 字）、source
   ⚠️ 与 tinted_bands 的区别：那是几条彼此独立的横带，**这套每层内部有并列模块**
      （二维结构）。技术栈、数据仓库分层、中台架构用这套。''',
        capacity='layers 3–5 层；每层 2–4 个模块；层名 ≤8 字；模块名 ≤16 字；note ≤45 字。')
def render_layered_stack(s, spec):
    """分层架构：左侧层名，右侧层内并列模块框，层间一条发丝线。"""
    header(s, spec.get('kicker'), spec.get('title'))
    layers = spec['layers'][:5]
    m = max(len(layers), 1)
    y0 = 2.15
    rh = min(1.10, (Y_BOTTOM - y0) / m)
    lab_w = 2.05
    x0 = LEFT + lab_w + 0.30
    avail = RIGHT - x0
    gap = 0.24
    for r, ly in enumerate(layers):
        y = y0 + r * rh
        put(s, LEFT, y + 0.06, lab_w, 0.40,
            [[(_fit(ly['name'], lab_w, 15),
               dict(size=15, color=DARK, bold=True))]],
            anchor=MSO_ANCHOR.MIDDLE)
        mods = (ly.get('modules') or [])[:4]
        k = max(len(mods), 1)
        bw = (avail - (k - 1) * gap) / k
        bh = min(0.56, rh - 0.28)
        for c, nm in enumerate(mods):
            x = x0 + c * (bw + gap)
            b = outline_box(s, x, y + 0.04, bw, bh, GREY)
            tf = b.text_frame
            tf.word_wrap = True
            tf.vertical_anchor = MSO_ANCHOR.MIDDLE
            # 内边距必须为 0（同 phase_grouped_flow）
            tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.CENTER
            run = p.add_run()
            run.text = _fit(nm, bw - 0.12, FS['small'])
            run.font.size = Pt(FS['small'])
            run.font.bold = False
            run.font.color.rgb = DARK
            run.font.name = LAT
            rPr = run._r.get_or_add_rPr()
            el = rPr.makeelement(qn('a:ea'), {})
            el.set('typeface', EA)
            rPr.append(el)
        if r < m - 1:
            hrule(s, LEFT, y + rh - 0.10, W)
    if spec.get('note'):
        flat = _flat([spec['note']])
        put(s, LEFT, 6.05, W, 0.50,
            [[(_fit(flat, W, 14), dict(size=14, color=MUTED))]])
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
# 数据
# ══════════════════════════════════════════════════════════════
@layout('data_table',
        roles=('content',),
        intents=('quantitative', 'comparison'),
        min_items=2, max_items=8,
        item_chars=22, total_chars=600,
        requires=('table',),
        signature='原生 PowerPoint 表格（要查阅精确值时用它）',
        best_for='参数对比、版本对比、类型矩阵 —— 读者要看到确切数值',
        avoid_for='看趋势/占比（那是形状阅读，用 metric_trend）',
        fallback=('comparison_rows', 'split_main_aside'),
        reuse_friendly=True,
        catalog='''原生表格。适合参数对比、版本对比、类型矩阵。
   必填 header: ["列1","列2"]（2–5 列）、rows: [[...], ...]（2–8 行）
   选填 col_widths（英寸，需合计 11.33）、source
   ⚠️ 单元格 ≤22 字。要看**趋势/占比**就用 metric_trend，别用表格。''',
        capacity='rows 2–8 行 × 2–5 列；单元格 ≤22 字。')
def render_data_table(s, spec):
    """原生 PowerPoint 表格。"""
    header(s, spec.get('kicker'), spec.get('title'))
    table(s, LEFT, spec.get('y', 2.20), W, spec.get('h', 3.9),
          [spec['header']] + spec['rows'],
          col_widths=spec.get('col_widths'),
          font=spec.get('font', 13.5),
          header_h=spec.get('header_h', 0.52),
          row_h=spec.get('row_h'))
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
@layout('metric_trend',
        roles=('content',),
        intents=('quantitative',),
        item_chars=40, total_chars=300,
        requires=('numbers',),
        signature='原生图表（折线/柱状/条形/饼图）+ 右侧结论，看的是形状不是数值',
        best_for='趋势、构成占比、类目排行 —— 读者要看拐点和比例',
        avoid_for='要查阅精确数值（那是 data_table）；源文没有成序列的数字',
        fallback=('kpi_grid', 'data_table'),
        reuse_friendly=False,
        catalog='''原生图表页。适合趋势（line）、对比（column/bar）、占比（pie）。
   必填 chart: {"type":"line|column|bar|pie",
               "labels":["2021","2022",...],
               "series":[{"name":"系列名","values":[1,2,3]}]}
     · **values 的个数必须与 labels 相同**；pie 只用第一个 series
     · ⚠️ 数值必须**原样来自源文档**，不得推算、不得编造。
       源文没有成序列的数字时，改用 data_table 或 kpi_grid。
   选填 takeaways: [(文本,{})]（≤3 条，每条 ≤34 字）、source''',
        capacity='chart.labels 3–8 个；series 最多 2 组且长度与 labels 一致；'
                 'takeaways 最多 3 条、每条 ≤34 字。')
def render_metric_trend(s, spec):
    """原生图表 + 右侧结论。数据非法时降级为把 labels 排成要点，宁可难看也不崩。"""
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION

    header(s, spec.get('kicker'), spec.get('title'))
    ch = spec.get('chart') or {}
    labels = [str(x) for x in (ch.get('labels') or [])][:8]
    series = []
    for se in (ch.get('series') or [])[:2]:
        vals = list(se.get('values') or [])
        # 长度对不上就丢这一组 —— 画出来是错的数据比不画更糟
        if labels and len(vals) == len(labels):
            series.append((str(se.get('name') or ''), vals))
    kinds = {'line': XL_CHART_TYPE.LINE_MARKERS, 'column': XL_CHART_TYPE.COLUMN_CLUSTERED,
             'bar': XL_CHART_TYPE.BAR_CLUSTERED, 'pie': XL_CHART_TYPE.PIE}
    ctype = kinds.get(str(ch.get('type', 'column')).lower(), XL_CHART_TYPE.COLUMN_CLUSTERED)

    cx, cy, cw, chh = LEFT, 2.25, 7.85, 3.95
    if not labels or not series:
        put(s, cx, cy, cw, 1.0,
            [[('（图表数据不足，未能绘制）', dict(size=14, color=MUTED))]])
    else:
        cd = CategoryChartData()
        cd.categories = labels
        for nm, vals in series:
            cd.add_series(nm or '数值', vals)
        gf = s.shapes.add_chart(ctype, Inches(cx), Inches(cy),
                                Inches(cw), Inches(chh), cd)
        chart = gf.chart
        chart.has_title = False
        chart.font.size = Pt(FS['source'])        # 图表文字同样受 12pt 下限约束
        chart.font.name = LAT
        chart.font.color.rgb = MUTED
        chart.has_legend = len(series) > 1 and ctype != XL_CHART_TYPE.PIE
        if chart.has_legend:
            chart.legend.position = XL_LEGEND_POSITION.BOTTOM
            chart.legend.include_in_layout = False
        # 色相收敛：只有品牌红与深灰两个色相，不用默认蓝。
        palette = [RED, DARK, GREY, MUTED]
        for i, se in enumerate(chart.plots[0].series):
            col = palette[i % len(palette)]
            if ctype == XL_CHART_TYPE.PIE:
                for j, pt in enumerate(se.points):
                    pt.format.fill.solid()
                    pt.format.fill.fore_color.rgb = palette[j % len(palette)]
            elif ctype in (XL_CHART_TYPE.LINE_MARKERS,):
                se.format.line.color.rgb = col
                se.smooth = False
            else:
                se.format.fill.solid()
                se.format.fill.fore_color.rgb = col
    if spec.get('takeaways'):
        tx = cx + cw + 0.35
        tw = RIGHT - tx
        for i, para in enumerate(spec['takeaways'][:3]):
            yy = cy + i * 1.35
            put(s, tx, yy, 0.26, 0.30,
                [[('—', dict(size=14, color=RED, bold=True))]])
            put(s, tx + 0.28, yy, tw - 0.28, 1.25,
                paras([para]), ls=1.38)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
# 状态
# ══════════════════════════════════════════════════════════════
# 状态 → (色, 显示名)。调色板里只有品牌红与几档灰，所以「风险」不能再开一个色相，
# 靠**加粗 + 深色**与「进行中」的红区分：红=当前焦点，深色块=需要盯的事。
_STATUS = {
    'done':  (GREY,  '已完成'),
    'doing': (RED,   '进行中'),
    'risk':  (DARK,  '有风险'),
    'todo':  (MUTED, '待启动'),
}


@layout('progress_checklist',
        roles=('content',),
        intents=('status',),
        min_items=4, max_items=8,
        item_chars=34, total_chars=320,
        signature='状态进度清单（每条带状态色标：已完成/进行中/有风险/待启动）',
        best_for='项目进展、整改落实、风险跟踪 —— 事项要带状态',
        avoid_for='没有状态维度的事项罗列（用 numbered_columns）',
        fallback=('numbered_columns', 'timeline_vertical'),
        reuse_friendly=True,
        catalog='''状态进度清单：每条事项带一个**状态标签**。
   必填 items: [{"name":"事项名（≤12 字）","desc":"一句说明（≤34 字）",
                "status":"done|doing|risk|todo"}]
     · status 四选一：done=已完成、doing=进行中、risk=有风险、todo=待启动
     · 4–8 条
   选填 progress（进度列标题，如 "进度"）、source
   ⚠️ 这套版式专治「已完成/进行中/风险」这类状态语义 —— 没有状态就别用。''',
        capacity='items 4–8 条；每条 name ≤12 字、desc ≤34 字；progress ≤6 字。')
def render_progress_checklist(s, spec):
    """状态进度清单：左侧状态标 + 事项名 + 说明，逐行发丝线。"""
    header(s, spec.get('kicker'), spec.get('title'))
    items = spec['items'][:8]
    n = max(len(items), 1)
    hdr_y = 2.10
    if spec.get('progress'):
        put(s, 10.55, hdr_y, 1.45, 0.30,
            [[(spec['progress'], dict(size=FS['source'], color=MUTED, bold=True))]],
            align=PP_ALIGN.RIGHT)
        hrule(s, LEFT, hdr_y + 0.32, W)
        hdr_y += 0.02
    y0 = 2.52
    rh = min(0.86, (Y_BOTTOM - y0) / n)
    for i, it in enumerate(items):
        y = y0 + i * rh
        col, label = _STATUS.get(str(it.get('status', 'todo')).lower(),
                                 (MUTED, '待启动'))
        # 状态标是一个小方块 + 状态名。色标与状态名放**同一个**文本框：
        # 拆成两个框时，色标框（0.20"=14pt）装不下 12pt 的一行（需 17pt），
        # 且两框包围盒重叠 0.57×0.29" —— 两条都会被几何检查抓到。
        put(s, LEFT, y, 1.42, 0.30,
            [[('■ ', dict(size=12, color=col, bold=True)),
              (label, dict(size=FS['source'], color=col, bold=True))]])
        put(s, LEFT + 1.50, y - 0.02, 3.60, 0.34,
            [[(_fit(it['name'], 3.60, 15),
               dict(size=15, color=DARK, bold=True))]])
        # 说明框右界必须停在进度列（10.55"）之前：宽 5.20" 时伸到 11.12"，
        # 与进度框重叠 0.57 × 0.29"（几何检查抓到）。
        put(s, LEFT + 5.25, y + 0.01, 4.45, 0.34,
            [[(it.get('desc') or '', dict(size=FS['small'], color=MUTED))]])
        if spec.get('progress') and it.get('progress'):
            put(s, 10.55, y, 1.45, 0.30,
                [[(str(it['progress']), dict(size=FS['small'], color=DARK))]],
                align=PP_ALIGN.RIGHT)
        y += rh
        hrule(s, LEFT, y - 0.16, W)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
# 结论
# ══════════════════════════════════════════════════════════════
@layout('executive_summary',
        roles=('content',),
        intents=('summary',),
        min_items=3, max_items=5,
        item_chars=60, total_chars=380,
        signature='结论先行：一句总纲 + 3–5 条**完整句**结论（不是关键词）',
        best_for='章节小结、汇报收尾 —— 每条要能直接念出来',
        avoid_for='关键词式条目（那是 numbered_columns）',
        fallback=('numbered_columns', 'statement'),
        reuse_friendly=False,
        catalog='''结论先行：上方一句总纲，下面 3–5 条**完整句子**的结论。
   必填 thesis（总纲，≤30 字）、points: [{"num":"01","text":"一条能直接念出来的结论（≤60 字）"}]
   选填 source
   ⚠️ points 里是**句子**不是关键词 —— 与 numbered_columns 的区别就在这里。
      适合章节小结、汇报收尾。''',
        capacity='thesis ≤30 字；points 3–5 条、每条 ≤60 字。')
def render_executive_summary(s, spec):
    """结论先行：总纲 + 编号完整句结论。"""
    header(s, spec.get('kicker'), spec.get('title'))
    if spec.get('thesis'):
        put(s, LEFT, 2.10, W, 0.85,
            [[(_fit(spec['thesis'], W, 20, lines=2),
               dict(size=20, color=DARK, bold=True))]], ls=1.32)
    hrule(s, LEFT, 3.05, W)
    points = spec['points'][:5]
    n = max(len(points), 1)
    y0 = 3.25
    rh = min(0.80, (Y_BOTTOM - y0) / n)
    for i, pt in enumerate(points):
        y = y0 + i * rh
        # 框高 0.44"（≈32pt）：20pt 一行需 28.4pt，给 0.36"（26pt）会报溢出
        put(s, LEFT, y + 0.02, 0.62, 0.44,
            [[(str(pt.get('num') or '%02d' % (i + 1)),
               dict(size=20, color=RED, bold=True))]])
        put(s, LEFT + 0.78, y, W - 0.78, rh - 0.10,
            [[(pt['text'], dict(size=16, color=DARK))]], ls=1.38)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
@layout('quote',
        roles=('content',),
        intents=('quote',),
        signature='引语页 —— 一页只讲一句话',
        best_for='结语、核心观点',
        avoid_for='需要铺陈多条信息的页',
        fallback=('statement',),
        reuse_friendly=False,
        catalog='''引语页。适合结语、核心观点。一页只讲一件事。
   必填 quote: [[(文本,{})], ...]（每行 ≤20 字，最多 2 行）
   选填 attribution（≤30 字）、body: [段落...]（≤3 段、每段 ≤40 字）、source''',
        capacity='quote 每行 ≤20 字，最多 2 行；attribution ≤30 字；'
                 'body 最多 3 段、每段 ≤40 字。')
def render_quote(s, spec):
    """引语页：一页只讲一句话。"""
    header(s, spec.get('kicker'))
    put(s, LEFT, spec.get('y', 2.55), 11.0, 1.6,
        paras(spec['quote']), ls=1.32)
    if spec.get('attribution'):
        put(s, LEFT, 4.20, 11.0, 0.4,
            [[(spec['attribution'], dict(size=14, color=MUTED))]])
    hrule(s, LEFT, 4.92, W)
    if spec.get('body'):
        put(s, LEFT, 5.20, 11.0, 1.5, paras(spec['body']), ls=1.45)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
LAYOUT_NAMES = list(LAYOUTS)


def render_slide(slide, spec):
    name = spec['layout']
    fn = LAYOUTS.get(name)
    if fn is None:
        # 声明式版式的**试片**：识别出来的版式还没入库（也不该入库 —— 半成品进了
        # 注册表就可能被并发的生成选中），所以它的区块随 spec 一起来。
        # 这类 spec 自带 `blocks`，直接交给声明式渲染器。
        if spec.get('blocks'):
            from . import layout_dsl          # 局部 import：内置 19 套不该依赖声明式那条路
            return layout_dsl.render_blocks(slide, spec)
        raise KeyError('unknown layout %r; known: %s' % (name, LAYOUT_NAMES))
    fn(slide, spec)
