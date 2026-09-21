# -*- coding: utf-8 -*-
"""12 套内容版式。每套是一个 `render(slide, spec)` 函数，spec 为 dict。

版式清单（每套构图不同，来自 zcode 规范推导 + 实测筛选）：
  statement          大字陈述 + 支撑段
  stat_hero          大数字焦点 + 说明 + 一排支撑数据
  definition         术语定义
  numbered_columns   分栏编号列表
  quadrant           四象限
  comparison_rows    维度对照（A 列 / B 列）
  process_chain      横向流程链
  timeline_vertical  纵向时间线
  node_flow          节点链（描边框 + 自动折返连接）
  data_table         原生表格
  tinted_bands       通栏浅色带
  quote              引语

约定：spec 里的富文本用 `[(text, opts), ...]`；opts 里 `hl=True` 表示着强调色（RED）。
"""
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn

from .tokens import (
    LEFT, RIGHT, W, FS, RED, DARK, MUTED, GREY, RULE, TINT, WHITE,
    EA, LAT, Y_CONTENT, Y_CONTENT_BOTTOM, Y_SOURCE,
    put, hrule, vrule, dot, outline_box, tint_band,
    header, footer, fit_one_line,
)


def _w_in(text, pt):
    """估算一段文字占用的宽度（英寸）。"""
    return sum(pt * (1.0 if ord(c) > 0x2E80 else 0.52) for c in str(text)) / 72.0


def _fit(text, avail_in, pt, lines=1):
    """把文字裁到 `lines` 行内放得下。

    有些框（大数字、步骤名、节点标签、脚注）高度只够固定行数，模型一旦写长了
    就会折行溢出。几何检查只能报「装不下」，修复回环又可能因为「不得改动数字」
    而改不动 —— 这类确定性超标由程序截断最可靠。
    """
    return fit_one_line(str(text), avail_in, pt, lines=lines)

__all__ = ['LAYOUTS', 'render_slide', 'LAYOUT_NAMES']


def _runs(runs):
    """把 spec 的 run 列表转成 put() 需要的格式，处理 hl 强调色。"""
    out = []
    for txt, o in runs:
        o = dict(o)
        if o.pop('hl', False):
            o['color'] = RED
            o.setdefault('bold', True)
        out.append((txt, o))
    return out


def _paras(lines):
    """lines: 段落列表，每段是 run 列表或裸字符串。

    容错：如果传进来的是**单个段落的 run 列表**（`[(text, opts), ...]`），
    自动包成一段，避免内容作者在「一段」和「多段」之间写错层级。
    """
    if lines and isinstance(lines[0], tuple):
        lines = [lines]
    out = []
    for ln in lines:
        if isinstance(ln, str):
            out.append([(ln, {})])
        else:
            out.append(_runs(ln))
    return out


# ══════════════════════════════════════════════════════════════
def render_statement(s, spec):
    """大字陈述。没有标题——陈述本身就是标题。"""
    header(s, spec.get('kicker'))
    lines = _paras(spec['lines'])
    for p in lines:
        for t, o in p:
            o['size'] = spec.get('size', 36)
            o.setdefault('color', DARK)
            o.setdefault('bold', True)
    put(s, LEFT, spec.get('y', 2.40), 11.0, 2.4, lines, ls=1.34)
    y = spec.get('body_y', 5.05)
    if spec.get('body'):
        put(s, LEFT, y, 10.6, 1.7, _paras(spec['body']), ls=1.45)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
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
        put(s, 4.85, 2.42, W - 4.18, 1.5, _paras(spec['claim']), ls=1.42)
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
        put(s, LEFT, 6.28, 11.0, 0.35, _paras(spec['aside']))
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
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
        flat = ''.join(t for p in _paras([spec['note']]) for t, _ in p)
        put(s, LEFT, 5.95, W, 0.78,
            [[(_fit(flat, W, 15, lines=2), dict(size=15, color=MUTED))]], ls=1.42)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
def render_timeline_vertical(s, spec):
    """纵向时间线：左侧竖线 + 标记点 + 单行「名称 + 说明」。"""
    header(s, spec.get('kicker'), spec.get('title'))
    steps = spec['steps']
    y0 = 2.10
    ystep = min(0.665, (Y_CONTENT_BOTTOM - y0 - 0.20) / max(len(steps) - 1, 1))
    vrule(s, 1.02, y0 + 0.06, ystep * (len(steps) - 1) + 0.10)
    for i, st in enumerate(steps):
        y = y0 + i * ystep
        put(s, 0.30, y, 0.58, 0.4,
            [[('%02d' % (i + 1), dict(size=14, color=RED, bold=True))]],
            align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
        dot(s, 0.965, y + 0.13, 0.11, GREY)
        put(s, 1.30, y, 10.7, 0.4,
            [[(st['name'], dict(size=15, color=DARK, bold=True)),
              ('   ' + st['desc'], dict(size=FS['small'], color=MUTED))]],
            anchor=MSO_ANCHOR.MIDDLE)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
def render_node_flow(s, spec):
    """节点链：描边圆角框 + 连接线，超过每行上限时自动折返。"""
    header(s, spec.get('kicker'), spec.get('title'))
    nodes = spec['nodes']
    accent = spec.get('accent', [0, len(nodes) - 1])
    bw = spec.get('box_w', 2.30)
    bh = spec.get('box_h', 0.62)
    per_row = spec.get('per_row', 4)
    rows = (len(nodes) + per_row - 1) // per_row
    rowgap = spec.get('row_gap', 1.75)
    y0 = spec.get('y', 2.35)
    for i, nm in enumerate(nodes):
        col, row = i % per_row, i // per_row
        in_row = min(per_row, len(nodes) - row * per_row)
        gapx = (W - in_row * bw) / max(in_row - 1, 1) if in_row > 1 else 0
        x = LEFT + col * (bw + gapx)
        y = y0 + row * rowgap
        is_accent = i in accent
        b = outline_box(s, x, y, bw, bh, RED if is_accent else DARK)
        tf = b.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        # 节点框高 0.62"，13pt 只够一行
        r.text = _fit(nm, bw - 0.20, FS['small'])
        r.font.size = Pt(FS['small'])
        r.font.bold = True
        r.font.color.rgb = RED if is_accent else DARK
        r.font.name = LAT
        rPr = r._r.get_or_add_rPr()
        el = rPr.makeelement(qn('a:ea'), {})
        el.set('typeface', EA)
        rPr.append(el)
        if col < in_row - 1:
            hrule(s, x + bw + 0.09, y + bh / 2, gapx - 0.18, GREY, 1.0)
    # 折返连接：上一行末尾 → 下一行开头
    if rows > 1:
        yret = y0 + bh + 0.53
        vrule(s, RIGHT - 0.01, y0 + bh, yret - (y0 + bh), GREY, 1.0)
        hrule(s, LEFT, yret, W, GREY, 1.0)
        vrule(s, LEFT, yret, (y0 + rowgap) - yret, GREY, 1.0)
    if spec.get('note'):
        # note 可能是纯字符串、run 列表、或多段 —— 统一拼平后截成单行。
        # 框高 0.6" 只够一行（15pt 两行正好顶到边界，实测会报溢出）。
        note = _paras([spec['note']])
        flat = ''.join(t for p in note for t, _ in p)
        put(s, LEFT, 6.05, W, 0.6,
            [[(_fit(flat, W, 15), dict(size=15, color=MUTED))]])
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
def render_data_table(s, spec):
    """原生 PowerPoint 表格。"""
    header(s, spec.get('kicker'), spec.get('title'))
    header_row = spec['header']
    data = [header_row] + spec['rows']
    nr, nc = len(data), len(header_row)
    y = spec.get('y', 2.20)
    hgt = spec.get('h', 3.9)
    gf = s.shapes.add_table(nr, nc, Inches(LEFT), Inches(y), Inches(W), Inches(hgt))
    tb = gf.table
    tb.first_row = False
    tb.horz_banding = False
    widths = spec.get('col_widths') or [W / nc] * nc
    for i, wd in enumerate(widths):
        tb.columns[i].width = Inches(wd)
    tb.rows[0].height = Inches(spec.get('header_h', 0.52))
    body_h = (hgt - spec.get('header_h', 0.52)) / (nr - 1)
    for r in range(1, nr):
        tb.rows[r].height = Inches(spec.get('row_h', body_h))
    for r in range(nr):
        for c in range(nc):
            cell = tb.cell(r, c)
            cell.margin_left = cell.margin_right = Inches(0.12)
            cell.margin_top = cell.margin_bottom = Inches(0.06)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.fill.solid()
            # 表头用近黑 DARK 而不是 accent4 蓝：模板自身的页面从不用蓝（theme1 里
            # accent4 只出现在一个空段落的 endParaRPr 上，是残留不是设计），
            # 整条深色带比深藏青更贴「红顶栏 + 深色正文」这套语言，也不跟红顶栏抢。
            cell.fill.fore_color.rgb = DARK if r == 0 else WHITE
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.LEFT
            run = p.add_run()
            run.text = data[r][c]
            run.font.size = Pt(spec.get('font', 13.5) if r else spec.get('font', 13.5) - 0.5)
            # 首列与正文同为 DARK —— 层级只靠加粗区分。原来首列是蓝色，
            # 等于在正文区又开了一个色相；现在整页只有表头那一条深色带和
            # 页眉那一抹红，色相收敛到两个。
            run.font.bold = (r == 0) or (c == 0)
            run.font.color.rgb = WHITE if r == 0 else DARK
            run.font.name = LAT
            rPr = run._r.get_or_add_rPr()
            el = rPr.makeelement(qn('a:ea'), {})
            el.set('typeface', EA)
            rPr.append(el)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
def render_tinted_bands(s, spec):
    """通栏浅色带：每条带 = 序号 + 名称 + 说明，左右出血。"""
    header(s, spec.get('kicker'), spec.get('title'))
    bands = spec['bands']
    bh = spec.get('band_h', 1.42)
    gap = spec.get('band_gap', 0.20)
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
def render_quote(s, spec):
    """引语页：一页只讲一句话。"""
    header(s, spec.get('kicker'))
    put(s, LEFT, spec.get('y', 2.55), 11.0, 1.6,
        _paras(spec['quote']), ls=1.32)
    if spec.get('attribution'):
        put(s, LEFT, 4.20, 11.0, 0.4,
            [[(spec['attribution'], dict(size=14, color=MUTED))]])
    hrule(s, LEFT, 4.92, W)
    if spec.get('body'):
        put(s, LEFT, 5.20, 11.0, 1.5, _paras(spec['body']), ls=1.45)
    footer(s, spec['page'], spec.get('source'))


# ══════════════════════════════════════════════════════════════
LAYOUTS = {
    'statement': render_statement,
    'stat_hero': render_stat_hero,
    'definition': render_definition,
    'numbered_columns': render_numbered_columns,
    'quadrant': render_quadrant,
    'comparison_rows': render_comparison_rows,
    'process_chain': render_process_chain,
    'timeline_vertical': render_timeline_vertical,
    'node_flow': render_node_flow,
    'data_table': render_data_table,
    'tinted_bands': render_tinted_bands,
    'quote': render_quote,
}
LAYOUT_NAMES = list(LAYOUTS)


def render_slide(slide, spec):
    name = spec['layout']
    if name not in LAYOUTS:
        raise KeyError('unknown layout %r; known: %s' % (name, LAYOUT_NAMES))
    LAYOUTS[name](slide, spec)
