# -*- coding: utf-8 -*-
"""设计令牌与绘图原语。

本文件是全部版式的单一真相来源。所有尺寸单位为**英寸**。

三条硬约束（来自最小验证的实测结论，不要违反）：

1. **只用 `Blank` / `Title Only` layout。**
   `Full Blank` 上有个 `Rectangle 3`（x=9.19, y=0, 4.15x1.24，纯色填充）会盖住母版的
   MEVION 徽标，实测渲染出来是纯白页 —— 没有任何公司品牌元素。

2. **内容右边界 12.00"。**
   装饰弧线 `Picture` 位于 x=10.19~15.13（图片框伸出画布外），实测墨迹自 x≈12.00 起。

3. **每个 run 必须显式设置字体**（`font.name` + `<a:ea>`）。
   公司模板的主题 `fontScheme` 里 `ea` 为空，不显式设置中文会走系统默认。
"""
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn

# ── 画布与版心 ────────────────────────────────────────────────
CANVAS_W, CANVAS_H = 13.33, 7.50   # 公司模板实际为 13.333 x 7.5
LEFT, RIGHT = 0.67, 12.00          # 版心左右边界
W = RIGHT - LEFT                   # 11.33

# 纵向分区
Y_KICKER = 0.38
Y_TITLE = 0.68
Y_CONTENT = 2.00
Y_CONTENT_BOTTOM = 6.95
Y_SOURCE = 6.78
Y_PAGENUM = 7.18

# ── 品牌色（取自模板 theme1.xml 的 clrScheme）─────────────────
RED   = RGBColor(0xD3, 0x12, 0x45)   # accent1 —— 强调，只打在单一焦点上
BLUE  = RGBColor(0x00, 0x46, 0x7F)   # accent4 —— 结构色
DARK  = RGBColor(0x23, 0x1F, 0x20)   # accent3 —— 正文
MUTED = RGBColor(0x64, 0x64, 0x63)   # accent5 —— 次要
GREY  = RGBColor(0x80, 0x7F, 0x83)   # accent2
RULE  = RGBColor(0xC9, 0xC9, 0xC9)   # 发丝线
TINT  = RGBColor(0xEE, 0xF3, 0xF8)   # 浅色带
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

# ── 字体与字号 ────────────────────────────────────────────────
EA, LAT = '微软雅黑', 'Segoe UI'
FS = dict(kicker=12, title=26, h2=18, h3=17, body=16, small=13, source=12, min=12)

# 公司骨架页在模板里的 layout 名
LAYOUT_BLANK = 'Blank'
LAYOUT_TITLE_ONLY = 'Title Only'
LAYOUT_AGENDA = 'Agenda'


# ══════════════════════════════════════════════════════════════
# 绘图原语
# ══════════════════════════════════════════════════════════════

def _rect(slide, x, y, w, h, color, *, outline=False, radius=None):
    """基础矩形。x/y/w/h 全部是**英寸**。

    ⚠️ 曾踩过的坑：早期版本对四个尺寸统一套 `Inches()`，而调用方传的是 `Pt(0.75)`
       （已是 EMU 值），于是 `Inches(9525)` 得到 9525 英寸高的巨型矩形 —— 所有发丝线
       都变成覆盖整页的色块。**officecli 的 view issues 查不出这个**（它只检查是否
       越过右边界），只有自写的边界扫描能抓到。所以：这里只接受英寸，转换在调用方做。
    """
    kind = MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE
    sh = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    if outline:
        sh.fill.background()
        sh.line.color.rgb = color
        sh.line.width = Pt(0.9)
    else:
        sh.fill.solid()
        sh.fill.fore_color.rgb = color
        sh.line.fill.background()
    sh.shadow.inherit = False
    if radius:
        sh.adjustments[0] = radius
    return sh


def hrule(slide, x, y, w, color=RULE, pt=0.75):
    """水平发丝线。pt → 英寸在这里转换，不把 Pt() 往外传。"""
    return _rect(slide, x, y, w, pt / 72.0, color)


def vrule(slide, x, y, h, color=RULE, pt=0.75):
    return _rect(slide, x, y, pt / 72.0, h, color)


def dot(slide, x, y, size=0.10, color=RED):
    return _rect(slide, x, y, size, size, color)


def outline_box(slide, x, y, w, h, color=BLUE, radius=0.10):
    """1px 描边、无填充的容器（zcode 明确许可的形态）。"""
    return _rect(slide, x, y, w, h, color, outline=True, radius=radius)


def tint_band(slide, y, h, color=TINT):
    """通栏浅色带：左右出血到画布边缘，无圆角、无边框、无阴影。"""
    return _rect(slide, 0, y, CANVAS_W, h, color)


PALETTE = None  # 延迟填充，避免与上面的常量定义顺序耦合


def resolve_color(c):
    """颜色可以是 RGBColor，也可以是令牌名（'RED' / 'BLUE' / ...）。

    内容 spec 里写 `{'color': 'MUTED'}` 比写 RGBColor(0x64,0x64,0x63) 可读得多。
    """
    global PALETTE
    if PALETTE is None:
        PALETTE = dict(RED=RED, BLUE=BLUE, DARK=DARK, MUTED=MUTED,
                       GREY=GREY, RULE=RULE, TINT=TINT, WHITE=WHITE)
    if isinstance(c, str):
        try:
            return PALETTE[c.upper()]
        except KeyError:
            raise KeyError('unknown color name %r; known: %s'
                           % (c, sorted(PALETTE)))
    return c


def _as_text(v) -> str:
    """把「文本」字段统一成字符串。

    内容来自模型，字段类型不能假设：同一个字段可能是 `"文本"`、`["文本", {}]`、
    或 `[["文本", {}], ["续", {}]]`。递归拼平，避免 `r.text = list` 那种崩法。
    """
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return ''.join(_as_text(x) for x in v)
    return '' if v is None else str(v)


def put(slide, x, y, w, h, paras, *, align=PP_ALIGN.LEFT,
        anchor=MSO_ANCHOR.TOP, ls=None, sa=None):
    """写文本。

    paras 结构：`[[(text, opts), ...], ...]`
      外层 = 段落，内层 = 段内的 run。
      opts 支持 size / bold / color（颜色对象或令牌名）/ ea / lat。
    """
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    for m in ('margin_left', 'margin_right', 'margin_top', 'margin_bottom'):
        setattr(tf, m, 0)
    first = True
    for para in paras:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.alignment = align
        if ls is not None:
            p.line_spacing = ls
        if sa is not None:
            p.space_after = Pt(sa)
        for txt, o in para:
            r = p.add_run()
            r.text = _as_text(txt)
            r.font.size = Pt(o.get('size', FS['body']))
            r.font.bold = o.get('bold', False)
            r.font.color.rgb = resolve_color(o.get('color', DARK))
            r.font.name = o.get('lat', LAT)
            rPr = r._r.get_or_add_rPr()
            el = rPr.find(qn('a:ea'))
            if el is None:
                el = rPr.makeelement(qn('a:ea'), {})
                rPr.append(el)
            el.set('typeface', o.get('ea', EA))
    return tf


# ── 页面级组件 ────────────────────────────────────────────────

def header(slide, kicker=None, title=None):
    if kicker:
        put(slide, LEFT, Y_KICKER, W, 0.28,
            [[(kicker, dict(size=FS['kicker'], color=RED, bold=True))]])
    if title:
        put(slide, LEFT, Y_TITLE, W, 0.62,
            [[(title, dict(size=FS['title'], color=DARK, bold=True))]])


def fit_one_line(text: str, max_in: float, size_pt: float, lines: int = 1) -> str:
    """把文本裁到 `lines` 行内放得下（超出加省略号）。

    有些框的高度只够固定行数（来源行 0.30" 只够 1 行；脚注 0.60" 够 2 行），
    模型一旦写长就会折行溢出。几何检查只能报「装不下」，修复回环又可能因为
    「不得改动数字」而改不动 —— 这类**确定性**超标由程序截断最可靠。
    """
    limit = max_in * 72.0 * max(lines, 1)
    w = 0.0
    for i, ch in enumerate(text):
        w += size_pt * (1.0 if ord(ch) > 0x2E80 else 0.52)
        if w > limit:
            return text[:max(i - 1, 1)].rstrip() + '…'
    return text


def footer(slide, page_no, source=None):
    if source:
        # 来源行框高仅 0.30"，必须单行放下 —— 超长直接截断
        src = fit_one_line(str(source), W - 1.4, FS['source'])
        put(slide, LEFT, Y_SOURCE, W - 1.4, 0.30,
            [[(src, dict(size=FS['source'], color=MUTED))]])
    # 页码框宽度取 1.10 而不是照抄模板占位符的 2.51 —— 模板那个右边界是 13.34"，
    # 已经越出 13.33" 画布，且右对齐后数字会落进装饰弧线区。
    put(slide, 10.83, Y_PAGENUM, 1.10, 0.28,
        [[(str(page_no), dict(size=FS['source'], color=MUTED))]],
        align=PP_ALIGN.RIGHT)
