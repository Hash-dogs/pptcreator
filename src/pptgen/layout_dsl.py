# -*- coding: utf-8 -*-
"""声明式版式 —— 自定义版式的渲染路径。

19 套内置版式是**命令式**的：每套一个 Python 渲染函数，坐标写死在函数体里。
自定义版式不能走那条路（Web 上传一张截图，没人为它写函数），所以这里定一层
**数据形态**：一份 JSON 描述「版面上有几个区块、每个区块放什么内容」，程序负责
把内容塞进区块 —— 折行、降字号、行高自适应都在这一层做。

    {"kind": "text",    "field": "lead",  "x": 0.67, "y": 2.0, "w": 4.2, "h": 2.2}
    {"kind": "bullets", "field": "items", "x": 5.4,  "y": 2.0, "w": 6.6, "h": 4.6}
    {"kind": "rule",    "y": 6.5}

**坐标是英寸，且已经归一到版心**（`normalize_blocks` 负责把识别结果从
「图片里的大致比例」换算成这个坐标系）。区块不允许越出：
左 `LEFT`、右 `RIGHT`（12.00"）、上 `Y_CONTENT`（2.00"）、下 `Y_BOTTOM`（6.95"）。

## 三条设计决定，都是有理由的

**① 只渲染纯文本，不支持段内混排。**
内置版式里的 `hl: true` 强调色在这里被**拍平**（文字保留、颜色丢弃）。
理由：段内混排要求逐 run 折行，`fit_block` 那一套是按纯文本量宽的，混排会让
「算出来的行数」和 PowerPoint 实际折的行数对不上 —— 而自定义版式没有人工复核，
对不上就是溢出。强调靠**区块自身的 color** 表达（`{"kind":"text","color":"RED"}`），
不靠段内着色。

**② 一切字号都由 `fit` 阶梯决定，不由模型决定。**
模型只给区块的框，不给字号。写多了就降字号，再不行才截断（记进 `TRUNCATIONS`，
和内置版式同一套可听见的机制）。

**③ 高度一律自适应**：`rh = min(上限, 可用高度 / 条数)`。
这是从内置版式抄来的教训（`comparison_rows` 早先用固定行高，4 行时排到 y=7.78"，
越出 7.50" 画布，officecli 与几何检查同时报错）。
"""
from __future__ import annotations

import re

from pptx.enum.text import PP_ALIGN

from . import tokens
from .tokens import (
    LEFT, RIGHT, W, FS, RED, DARK, MUTED, GREY,
    Y_CONTENT, Y_BOTTOM, put, hrule, dot, tint_band, table,
    header, footer, wrap_lines, text_w_in,
)

__all__ = ['BLOCK_KINDS', 'render_blocks', 'normalize_blocks', 'problems',
           'catalog_of', 'capacity_of', 'field_names']

# 区块类型。识别模型只能从这里挑 —— 挑不出来就是「这张截图的结构本版做不了」，
# 而不是让它自由发挥画一版画不出来的东西。
BLOCK_KINDS = ('text', 'bullets', 'columns', 'kpi', 'rule', 'band', 'table',
               'steps', 'rows')

# 定高字段（单行放不下就截断，而不是溢出）与多行字段的分界：按最小字号算，
# 一个框最多能放几行。用于给 `fit` 阶梯一个上限，免得算出「放 30 行 12pt」。
MIN_SIZE = FS['min']        # 12pt —— 几何检查的字号下限，别再往下走
_TEXT_COLOR = 'DARK'


# ══════════════════════════════════════════════════════════════
# 文字：折行 + 降字号的阶梯
# ══════════════════════════════════════════════════════════════

def _ladder(max_size, min_size=MIN_SIZE):
    """字号阶梯：从大到小、每档降 2pt，下限 `min_size`。"""
    out, s = [], float(max_size)
    while s > min_size:
        out.append(int(s))
        s -= 2
    out.append(int(min_size))
    return tuple(dict.fromkeys(out))       # 去重且保序


def _max_lines(h, ls, min_size=MIN_SIZE):
    """这个框按最小字号最多放得下几行。"""
    return max(1, int(h * 72.0 / (min_size * ls)))


def _fit_lines(text, w, h, *, sizes, ls=1.3, max_lines=None):
    """把一段（可含 `\\n` 分段的）文字放进 `w × h`：折行 + 降字号 + 末路截断。

    返回 `(行列表, 字号)`。`fit_block` 只吃单段，这里按 `\\n` 分段后再统一量高 ——
    自定义版式的字段常是「一句主张 + 一行说明」，得按一个整体选字号，
    否则两段会各挑各的字号，看起来像两篇文章拼在一起。

    ⚠️ 截断会记进 `tokens.TRUNCATIONS`：它让文字不再溢出，于是几何检查全绿，
    问题只在肉眼看渲染图时才暴露 —— 这是本仓库反复踩过的坑（见 tokens 里的注释）。
    自定义版式的识别闸门正是靠这个记录判定「这版不合格」。
    """
    text = '' if text is None else str(text)
    parts = text.split('\n')
    if max_lines is None:
        max_lines = _max_lines(h, ls)
    for size in sizes:
        lines = []
        for p in parts:
            lines += wrap_lines(p, w, size, max_lines=max_lines)
            if len(lines) > max_lines:
                break
        if len(lines) <= max_lines and len(lines) * size * ls / 72.0 <= h:
            return lines, size
    # 阶梯全试完仍装不下 —— 按最小字号折，末行截断
    size = sizes[-1]
    lines = []
    for p in parts:
        lines += wrap_lines(p, w, size, max_lines=max_lines)
    if len(lines) <= max_lines and len(lines) * size * ls / 72.0 <= h:
        return lines, size
    keep = lines[:max_lines]
    # 用**不记账**的裁剪：这一条由下面自己记（记的是整段原文，比只剩一行的末行
    # 有用得多）。`fit_block` 里同样的处理。
    tail = tokens.cut_silent(keep[-1] + '…', w, size)
    tokens.TRUNCATIONS.append((text, ''.join(keep[:-1]) + tail))
    return keep[:-1] + [tail], size


def _flat_lines(value):
    """把任意写法的字段拍平成 `[段落文本, ...]`。

    富文本的 run 结构在这里被**丢掉**（见模块注释 ①）—— 走 `tokens.paras`
    把模型那三种写法（tuple / list / dict）都吃下来，再拼回纯文本。
    """
    out = []
    for para in tokens.paras(value):
        t = ''.join(str(x) for x, _ in para).strip()
        if t:
            out.append(t)
    return out or ['']


def _one_line(value, w, size=FS['small'], color=MUTED):
    """单行字段：放不下就截断（记 TRUNCATIONS）。"""
    txt = ' '.join(_flat_lines(value))
    return tokens.fit_one_line(txt, w, size)


def _str(v):
    return '' if v is None else str(v)


def _items(value):
    """条目的归一：`"文本"` / `{"name":…}` / `{"text":…}` 都收成一个 dict。"""
    out = []
    for it in (value or []):
        if isinstance(it, dict):
            out.append(dict(it))
        elif isinstance(it, (list, tuple)):
            out.append({'name': ' '.join(_flat_lines(it))})
        else:
            out.append({'name': _str(it)})
    return out


def _rows(value):
    """表格字段的归一：`{"header": [...], "rows": [[...]]}`，也接受裸的行列表。"""
    if isinstance(value, dict):
        head = value.get('header') or []
        body = value.get('rows') or []
    else:
        body = value or []
        head = []
    data = []
    if head:
        data.append([_cell(c) for c in head])
    for r in body:
        data.append([_cell(c) for c in (r if isinstance(r, (list, tuple)) else [r])])
    return data


def _cell(c):
    return ' '.join(_flat_lines(c)) if not isinstance(c, (int, float)) else _str(c)


# ══════════════════════════════════════════════════════════════
# 各区块的渲染
# ══════════════════════════════════════════════════════════════

def _box(b):
    """区块的框，带上限保护（越界的框在渲染前就该被 normalize 收好）。"""
    x = float(b.get('x', LEFT))
    y = float(b.get('y', Y_CONTENT))
    w = max(float(b.get('w', W)), 0.60)
    h = max(float(b.get('h', 0.60)), 0.28)
    return x, y, w, h


def _render_text(s, b, spec):
    x, y, w, h = _box(b)
    txt = '\n'.join(_flat_lines(spec.get(b['field'])))
    if not txt.strip():
        return
    ls = float(b.get('ls', 1.30))
    lines, size = _fit_lines(
        txt, w, h, ls=ls, max_lines=b.get('max_lines'),
        sizes=_ladder(b.get('size', 28), b.get('min_size', MIN_SIZE)))
    align = dict(center=PP_ALIGN.CENTER, right=PP_ALIGN.RIGHT,
                 left=PP_ALIGN.LEFT)[str(b.get('align', 'left')).lower()]
    put(s, x, y, w, h,
        [[(ln, dict(size=size, color=b.get('color', _TEXT_COLOR),
                    bold=bool(b.get('bold', False))))] for ln in lines],
        align=align, ls=ls)


def _render_items_grid(s, b, spec, *, default_cols, numbered):
    """`bullets` / `columns` 共用的网格：条目 = 名称（+ 说明）逐格排。"""
    x, y, w, h = _box(b)
    items = _items(spec.get(b['field']))
    if not items:
        return
    ncol = max(1, int(b.get('ncol', default_cols)))
    gap = float(b.get('gap', 0.42 if ncol > 1 else 0.0))
    cw = (w - (ncol - 1) * gap) / ncol
    nrow = (len(items) + ncol - 1) // ncol
    rh = min(float(b.get('row_h', 1.62)), h / max(nrow, 1))
    name_size = int(b.get('name_size', FS['h3'] if numbered else 16))
    desc_size = int(b.get('desc_size', FS['small']))
    for i, it in enumerate(items):
        col, row = i % ncol, i // ncol
        cx = x + col * (cw + gap)
        cy = y + row * rh
        mark = ('%02d' % (i + 1) + '   ') if numbered else ''
        # 名称与序号拼进**同一个** `put`：分成两个文本框会有 text_overlap 风险
        # （内置版式 numbered_columns 用的就是这一招）。
        name = _one_line(it.get('name', ''), max(cw - 0.55, 0.5), name_size, DARK)
        put(s, cx, cy, cw, 0.40,
            [[(mark, dict(size=14, color=RED, bold=True)),
              (name, dict(size=name_size, color=DARK, bold=True))]])
        desc = _str(it.get('desc') or it.get('text') or '')
        if desc:
            dls, dsize = _fit_lines(desc, cw, max(rh - 0.46, 0.30),
                                    ls=1.34, sizes=_ladder(desc_size, FS['small']))
            put(s, cx, cy + 0.44, cw, max(rh - 0.46, 0.30),
                [[(ln, dict(size=dsize, color=MUTED))] for ln in dls], ls=1.34)
        if b.get('row_rule') and row < nrow - 1:
            hrule(s, x, cy + rh - 0.14, w)


def _render_kpi(s, b, spec):
    x, y, w, h = _box(b)
    items = _items(spec.get(b['field']))
    if not items:
        return
    ncol = max(1, int(b.get('ncol', 3)))
    gap = float(b.get('gap', 0.42))
    cw = (w - (ncol - 1) * gap) / ncol
    nrow = (len(items) + ncol - 1) // ncol
    rh = min(float(b.get('row_h', 1.70)), h / max(nrow, 1))
    num_size = int(b.get('num_size', 40))
    for i, it in enumerate(items):
        col, row = i % ncol, i // ncol
        cx = x + col * (cw + gap)
        cy = y + row * rh
        num = _str(it.get('num'))
        unit = _str(it.get('unit'))
        # 数字框定高一行：先把单位压到剩余宽度里，别让 72pt 的数字被挤到第二行
        put(s, cx, cy, cw, 0.80,
            [[(num, dict(size=num_size, color=DARK, bold=True)),
              (_one_line(unit, max(cw - text_w_in(num, num_size) - 0.10, 0.25), 16),
               dict(size=16, color=MUTED))]])
        put(s, cx, cy + 0.84, cw, 0.38,
            [[(_one_line(it.get('label', ''), cw, 14), dict(size=14, color=DARK, bold=True))]])
        note = _str(it.get('note'))
        if note:
            put(s, cx, cy + 1.22, cw, 0.34,
                [[(_one_line(note, cw, FS['foot']),
                   dict(size=FS['foot'], color=b.get('note_color', 'RED')))]])
        if row < nrow - 1 and b.get('row_rule', True):
            hrule(s, x, cy + rh - 0.18, w)


def _render_table(s, b, spec):
    x, y, w, h = _box(b)
    data = _rows(spec.get(b['field']))
    if len(data) < 2:
        return
    table(s, x, y, w, h, data,
          col_widths=b.get('col_widths'),
          font=float(b.get('font', 13.5)),
          header_h=float(b.get('header_h', 0.52)),
          row_h=b.get('row_h'))


def _render_steps(s, b, spec):
    """横向流程链：序号 + 名称 + 说明，标记点落在一条公共基线发丝线上。

    ⚠️ 三步的纵向位置由 `baseline`（区块高度的比例）推出来，所以**区块太矮时
    说明会画到框外面**（实测：h=0.16 的区块，说明落在框底之下 0.37"）。
    这里按「放得下才画」收口 —— 少画一行说明是可见的缺失（构图比对会看出来），
    画到框外面则是不可见的越界（几何检查只会把它算成另一处重叠）。
    """
    x, y, w, h = _box(b)
    steps = _items(spec.get(b['field']))
    if not steps:
        return
    n = len(steps)
    gap = float(b.get('gap', 0.34))
    cw = (w - (n - 1) * gap) / n
    base = y + h * float(b.get('baseline', 0.45))       # 基线相对区块高度的位置
    hrule(s, x, base, w)
    num_size = int(b.get('num_size', 26))
    name_size = int(b.get('name_size', FS['h3']))
    for i, st in enumerate(steps):
        cx = x + i * (cw + gap)
        num = _one_line(st.get('num') or ('%02d' % (i + 1)), cw, num_size, RED)
        put(s, cx, max(base - 0.90, y), cw, 0.55,
            [[(num, dict(size=num_size, color=RED, bold=True))]])
        if b.get('marker', True):
            dot(s, cx, base - 0.055, 0.11, GREY)        # 点在线上：+0.055 = 线宽一半
        put(s, cx, base + 0.30, cw, 0.45,
            [[(_one_line(st.get('name', ''), cw, name_size),
               dict(size=name_size, color=DARK, bold=True))]])
        desc = _str(st.get('desc'))
        desc_top = base + 0.78
        avail = y + h - desc_top
        if desc and avail >= 0.26:                      # 放不下一行就不画，别越界
            dls, dsize = _fit_lines(desc, cw, avail, ls=1.38,
                                    sizes=_ladder(int(b.get('desc_size', FS['small'])),
                                                  FS['small']),
                                    max_lines=b.get('desc_lines', 2))
            put(s, cx, desc_top, cw, avail,
                [[(ln, dict(size=dsize, color=MUTED))] for ln in dls], ls=1.38)


def _render_rows(s, b, spec):
    """维度对照行：`维度 | A 列 | B 列`，逐行一条发丝线。"""
    x, y, w, h = _box(b)
    rows = _items(spec.get(b['field']))
    if not rows:
        return
    dim_w = float(b.get('dim_w', 2.30))
    gap = 0.20
    cw = (w - dim_w - 2 * gap) / 2.0
    x2 = x + dim_w + gap
    x3 = x2 + cw + gap
    col_a = _str(spec.get('col_a'))
    col_b = _str(spec.get('col_b'))
    head_h = 0.36 if (col_a or col_b) else 0.0
    if head_h:
        put(s, x, y, dim_w, 0.32,
            [[(_one_line(b.get('dim_head', '维度'), dim_w, FS['small']),
               dict(size=FS['small'], color=MUTED, bold=True))]])
        put(s, x2, y, cw, 0.32,
            [[(_one_line(col_a, cw, FS['small']),
               dict(size=FS['small'], color=GREY, bold=True))]])
        put(s, x3, y, cw, 0.32,
            [[(_one_line(col_b, cw, FS['small']),
               dict(size=FS['small'], color=DARK, bold=True))]])
        hrule(s, x, y + head_h - 0.04, w)
    y0 = y + head_h + 0.06
    n = max(len(rows), 1)
    rh = min(float(b.get('row_h', 1.32)), (y + h - y0) / n)
    size = int(b.get('cell_size', 14))
    yy = y0
    for i, r in enumerate(rows):
        put(s, x, yy + 0.02, dim_w, 0.40,
            [[(_one_line(r.get('dim') or r.get('name', ''), dim_w, 15),
               dict(size=15, color=DARK, bold=True))]])
        for xx, key, color in ((x2, 'a', GREY), (x3, 'b', DARK)):
            txt = _str(r.get(key))
            if not txt:
                continue
            ls_, sz = _fit_lines(txt, cw, max(rh - 0.10, 0.24), ls=1.38,
                                 sizes=_ladder(size, FS['small']),
                                 max_lines=b.get('cell_lines'))
            put(s, xx, yy, cw, max(rh - 0.10, 0.24),
                [[(ln, dict(size=sz, color=color))] for ln in ls_], ls=1.38)
        yy += rh
        if i < n - 1:
            hrule(s, x, yy - 0.16, w)


_RENDERERS = {
    'text': _render_text,
    'bullets': lambda s, b, spec: _render_items_grid(s, b, spec, default_cols=1, numbered=True),
    'columns': lambda s, b, spec: _render_items_grid(s, b, spec, default_cols=3, numbered=False),
    'kpi': _render_kpi,
    'table': _render_table,
    'steps': _render_steps,
    'rows': _render_rows,
}


def _render_rule(s, b, spec):
    """一条水平发丝线。宽度默认取区块自己的宽度（识别出来的那条线的长度）。"""
    x, y, w, _h = _box(b)
    hrule(s, x, y, w, b.get('color', 'RULE'))


def _render_band(s, b, spec):
    """通栏浅色带。左右出血到画布边缘（`tint_band` 的行为），只认 y 与高度。"""
    _x, y, _w, h = _box(b)
    tint_band(s, y, h, b.get('color', 'TINT'))


_RENDERERS['rule'] = _render_rule
_RENDERERS['band'] = _render_band


def render_blocks(slide, spec):
    """声明式渲染器的入口：与 19 套内置版式的渲染函数**同签名**。

    页眉页脚由这里统一画（自定义版式不再各自操心 kicker/title/页码），
    区块只负责正文带。

    **区块从哪儿来**：优先用 `spec['blocks']`（临时的试片渲染走这条路 —— 那时版式
    还没注册、也不该注册，注册了就等于把半成品放进了候选池），否则按
    `spec['layout']` 从声明注册表查回来。正式渲染走后者：区块属于**版式**，
    不属于这一页的内容，所以 `pipeline` 不必把区块抄进每一页的 spec。
    """
    header(slide, spec.get('kicker'), spec.get('title'))
    blocks = spec.get('blocks') or _DECLS.get(spec.get('layout')) or []
    for b in blocks:
        kind = b.get('kind')
        fn = _RENDERERS.get(kind)
        if fn is None:                      # 认不出的区块跳过，而不是崩掉整页
            continue
        if kind in ('rule', 'band') or b.get('field'):
            fn(slide, b, spec)
    footer(slide, spec['page'])


# 已注册的声明：版式名 → 区块列表。加载自定义版式时填（见 layout_store）。
_DECLS: dict[str, list[dict]] = {}


def register_decl(name: str, blocks: list[dict]) -> None:
    _DECLS[name] = list(blocks or [])


def decl_of(name: str) -> list[dict]:
    return _DECLS.get(name) or []


# ══════════════════════════════════════════════════════════════
# 归一化：识别结果的「图片比例」→「英寸」
# ══════════════════════════════════════════════════════════════

# 区块的框小于这个就丢掉：窄到 0.8" 以下放不下任何中文，留着只会变成一坨溢出。
# 高度下限取 0.18" 而不是 0.30"：**一行文字本来就矮** —— 版式里的 kicker 框是
# 0.28"、页码行是 0.28"（见 tokens.header/footer）。早先按 0.30" 卡，把识别出来的
# 0.28" 说明行当噪声丢了，那一轮因此不合格（实测）。0.18" 只杀真正的碎片。
MIN_BOX_W, MIN_BOX_H = 0.80, 0.18
# 贴边吸附的容差：识别出来的左边界差 0.2" 以内就算「贴着版心左边」
SNAP = 0.20
# 区块整体的纵向占比低于这个值时，不做「撑满内容带」的重映射 ——
# 那种情况通常是识别本身就不确定，硬撑开会把窄内容拉成两倍行距。
FILL_MIN = 0.40


def normalize_blocks(raw, dropped=None):
    """识别结果 → 可渲染的区块。坐标从「内容带内的 0–1 比例」换算成英寸。

    **刻意不接受「整页比例」**：那样得先判断截图里哪一块是页面、页边距多宽，
    而这一步没有可靠依据（截图往往带背景、阴影、浏览器边框）。改成让模型
    只描述**内容带内部**的相对布局 —— 页眉页脚由程序统一画，本来就不该识别。

    除换算外还做三件事，全部是确定性的，不依赖模型：
      ① 吸附到版心左右边（差 0.2" 以内算贴边）；
      ② 裁掉越界的部分（右不越 12.00"，下不越 Y_BOTTOM）；
      ③ 丢太小、认不出种类的区块。

    ③ 是**有损**的，所以 `dropped` 传一个 list 进来就能收回被丢掉的原因 ——
    识别链路要把它写进「带违例清单重试」的提示词里。静默丢弃正是这个仓库
    反复在修的那类问题（`_fit()` 的静默截断就是前车之鉴）。
    """
    out = []
    for b in (raw or []):
        if not isinstance(b, dict):
            _drop(dropped, b, '不是对象')
            continue
        kind = b.get('kind')
        if kind not in BLOCK_KINDS:
            _drop(dropped, b, '区块种类 %r 不认识' % (kind,))
            continue
        try:
            x = LEFT + float(b.get('x', 0.0)) * W
            y = Y_CONTENT + float(b.get('y', 0.0)) * (Y_BOTTOM - Y_CONTENT)
            w = float(b.get('w', 1.0)) * W
            h = float(b.get('h', 0.2)) * (Y_BOTTOM - Y_CONTENT)
        except (TypeError, ValueError):
            _drop(dropped, b, '坐标不是数字')
            continue
        w = min(w, RIGHT - LEFT)
        h = min(h, Y_BOTTOM - Y_CONTENT)
        # ① 吸附
        if abs(x - LEFT) <= SNAP:
            x = LEFT
        if abs((x + w) - RIGHT) <= SNAP:
            w = RIGHT - x
        # ② 裁剪
        x = min(max(x, LEFT), RIGHT - 0.2)
        w = min(w, RIGHT - x)
        y = min(max(y, Y_CONTENT), Y_BOTTOM - 0.2)
        h = min(h, Y_BOTTOM - y)
        # ③ 丢弃：装饰性区块不适用「框太小」这条 —— 发丝线本来就是 0.01" 高，
        # 按内容区块的下限去卡会把每一条分隔线都丢掉（实测踩过）。
        min_h = {'rule': 0.0, 'band': 0.06}.get(kind, MIN_BOX_H)
        if w < MIN_BOX_W or h < min_h:
            _drop(dropped, b, '太窄或太矮（%.2f" × %.2f"）' % (w, h))
            continue
        # 取整要**先取整再夹一次边界**：`x` 与 `w` 各自四舍五入之后，两者之和会
        # 比裁剪时算的越过 0.01"（实测 `Y_BOTTOM` 从 6.60 改到 6.95 之后，
        # 6.46 + 0.50 = 6.96 > 6.95 —— 裁剪那一刻是刚好贴边的）。
        # 渲染层画的是取整后的值，所以越过的那一分是真越界。
        x2, y2 = round(x, 2), round(y, 2)
        nb = dict(b)
        nb.update(x=x2, y=y2, w=round(min(round(w, 2), RIGHT - x2), 2),
                  h=round(min(round(h, 2), Y_BOTTOM - y2), 2))
        out.append(nb)
    return _fill_band(out)


def _drop(dropped, block, why):
    if dropped is not None:
        dropped.append('%s 区块被丢掉：%s' % ((block or {}).get('kind', '?'), why))


def _fill_band(blocks):
    """把区块整体在内容带里**撑开**：让最高到最低的那一段占满 2.00–6.60。

    截图里的版式上下留白多少是随机的（截图可能被裁过），直接照搬比例会得到
    「内容挤在上三分之一」的版式 —— 那类页面每页都会被几何检查报
    `large_empty_area`。整段重映射能保证每个版式都是**撑满内容带**的构图。

    纵向占比小于 `FILL_MIN` 时不动：那更可能是识别不确定，硬撑开只会把
    正常的一栏拉成夸张的行距。横向**不重映射** —— 居中的窄栏（如大字陈述）
    是合法的构图，拉满反而失真。
    """
    boxes = [b for b in blocks if b.get('kind') not in ('rule', 'band')]
    if len(boxes) < 2:
        return blocks
    top = min(b['y'] for b in boxes)
    bot = max(b['y'] + b['h'] for b in boxes)
    span = bot - top
    band = Y_BOTTOM - Y_CONTENT
    if span < band * FILL_MIN or span <= 0:
        return blocks
    k = band / span
    for b in blocks:
        b['y'] = round(Y_CONTENT + (b['y'] - top) * k, 2)
        b['h'] = round(b['h'] * k, 2)
        # 重映射是按**内容区块**的并集锚定的，装饰区块（色带/分隔线）可能因此被
        # 推到内容带之外 —— 实测一条通栏色带的顶边会被抬到 1.91"（内容带之上）。
        # 版式整体的装饰不该越出自己的内容带，所以这里统一收回来。
        b['y'] = max(b['y'], Y_CONTENT)
        b['h'] = min(b['h'], Y_BOTTOM - b['y'])
    return blocks


# ══════════════════════════════════════════════════════════════
# 字段契约：给模型的目录与容量文本，**从区块生成**
# ══════════════════════════════════════════════════════════════

# 每种区块 → (字段描述模板, 容量描述, 一句话说明)。
# 前两项给「选到这套版式之后」的规划模型看，第三项给**识别模型**看
# （它要看着截图挑区块种类）—— 三个用途共用一张表，不另写清单。
_FIELD_DOC = {
    'text':    ('%(field)s：一段文字（%(cap)s），渲染为大字陈述/标题',
                '%(field)s %(cap)s',
                '一段文字，字号自动放大/缩小以填满框（主张、结论句、大字标题）'),
    'bullets': ('%(field)s：[{"name":"要点（%(name)s）","desc":"说明（%(desc)s）"}]，'
                '%(n)s',
                '%(field)s %(n)s，name %(name)s、desc %(desc)s',
                '竖排编号要点列表，每条 {"name":…,"desc":…}（desc 可省）'),
    'columns': ('%(field)s：[{"name":"小标题（%(name)s）","desc":"说明（%(desc)s）"}]，'
                '%(n)s',
                '%(field)s %(n)s，name %(name)s、desc %(desc)s',
                '分栏卡片，每条 {"name":…,"desc":…}，ncol 决定分几栏'),
    'kpi':     ('%(field)s：[{"num":"数字（%(num)s）","unit":"单位",'
                '"label":"标签（%(label)s）","note":"备注（可选）"}]，%(n)s',
                '%(field)s %(n)s，num %(num)s、label %(label)s',
                '指标卡网格，每张 {"num":…,"unit":…,"label":…}，ncol 决定几列'),
    'table':   ('%(field)s：{"header":["列1","列2"],"rows":[["…","…"]]}，%(n)s、'
                '%(nc)s 列',
                '%(field)s 单元格 ≤22 字，%(n)s、%(nc)s 列',
                '原生表格 {"header":[…],"rows":[[…]]}'),
    'steps':   ('%(field)s：[{"num":"01","name":"步骤名（%(name)s）",'
                '"desc":"说明（%(desc)s）"}]，%(n)s',
                '%(field)s %(n)s，name %(name)s、desc %(desc)s',
                '横向流程链，每步 {"num":…,"name":…,"desc":…}，标记点落在一条基线上'),
    'rows':    ('%(field)s：[{"dim":"维度（%(dim)s）","a":"A 列内容（%(cell)s）",'
                '"b":"B 列内容（%(cell)s）"}]，%(n)s',
                '%(field)s %(n)s，dim %(dim)s、单元格 %(cell)s',
                '维度对照行，每行 {"dim":…,"a":…,"b":…}（配字段 col_a / col_b 作表头）'),
    'rule':    ('（一条发丝线，无字段）', '',
                '一条水平发丝线（无字段），靠 y 定位，用来分隔上下两区'),
    'band':    ('（一条通栏浅色带，无字段）', '',
                '一条通栏浅色带（无字段），靠 y/h 定位，左右出血到页面边缘'),
}


def kind_catalog_text() -> str:
    """区块种类清单 —— 给**识别模型**的提示词用。

    从 `_FIELD_DOC` 生成，不另写一份：识别模型挑出来的种类必须与渲染器认识的
    完全一致，两处手写清单迟早对不上（这个仓库最早的坑就是「同一份版式清单
    手写在三处」，见 layout_spec 的模块注释）。
    """
    return '\n'.join('  %-8s %s' % (k, _FIELD_DOC[k][2]) for k in BLOCK_KINDS)

# 条目数参与「主要列表」判定的区块种类：一份版式里通常只有一个这样的块，
# 它的条目数就是版式的 min/max_items。
_ITEM_KINDS = ('bullets', 'columns', 'kpi', 'steps', 'rows', 'table')


def _n_text(cap, meta=None, *, primary=False):
    """条目数怎么写进目录文本。

    **版式自己声明的 min/max_items 优先**（那是作者或识别给出的意图），框容量只做
    上限 —— 行高是自适应的（`rh = min(上限, 可用高度 / 条数)`），所以框能塞下的条数
    往往比声明的多，只报框容量会让模型写出「比版式设计意图更多」的条目。
    """
    if primary and meta:
        lo, hi = meta.get('min_items'), meta.get('max_items')
        if isinstance(hi, int):
            hi = min(hi, cap)
            if isinstance(lo, int) and 0 < lo < hi:
                return '%d–%d 条' % (lo, hi)
            if isinstance(lo, int) and lo == hi:
                return '正好 %d 条' % hi
            return '≤%d 条' % hi
    return '≤%d 条' % cap


def _block_cap(b, meta=None, *, primary=False):
    """区块的容量数字。**由程序定，不由识别模型定** —— 它是「这版式装得下多少」，
    只有渲染器知道：框多宽、字号阶梯降到哪、行高怎么分配。"""
    _x, _y, w, h = _box(b)
    kind = b['kind']
    if kind == 'text':
        big = float(b.get('size', 28))
        # 一行放得下几个中文字（按该区块的最大字号估）+ 最多几行
        per_line = max(int(w * 72.0 / big), 2)
        lines = max(int(h * 72.0 / (big * 1.3)), 1)
        return dict(cap='≤%d 字' % (per_line * lines))
    if kind in ('bullets', 'columns'):
        # 一行按 0.85" 算：低于这个高度，名称与说明就挤在一起了
        nrow = max(int(h / 0.85), 1)
        ncol = max(int(b.get('ncol', 1 if kind == 'bullets' else 3)), 1)
        cap = nrow * ncol
        return dict(n_cap=cap, n=_n_text(cap, meta, primary=primary),
                    name='≤%d 字' % max(int(w / ncol * 72.0 / 16) - 1, 2),
                    desc='≤%d 字' % max(int(w / ncol * 72.0 / 13) * 2, 6))
    if kind == 'kpi':
        ncol = max(int(b.get('ncol', 3)), 1)
        cap = max(int(h / 1.70), 1) * ncol
        return dict(n_cap=cap, n=_n_text(cap, meta, primary=primary),
                    num='≤8 字符', label='≤%d 字' % max(int(w / ncol * 72.0 / 14) - 4, 3))
    if kind == 'steps':
        cap = max(int(w / 1.40), 1)
        return dict(n_cap=cap, n=_n_text(cap, meta, primary=primary),
                    name='≤%d 字' % max(int(w / cap * 72.0 / 17) - 2, 2),
                    desc='≤%d 字' % max(int(w / cap * 72.0 / 13) - 2, 4))
    if kind == 'rows':
        cap = max(int(h / 1.10), 1)
        return dict(n_cap=cap, n=_n_text(cap, meta, primary=primary),
                    dim='≤%d 字' % max(int(2.30 * 72 / 15) - 2, 2),
                    cell='≤%d 字' % max(int((W - 2.5) / 2 * 72.0 / 14) * 2, 6))
    if kind == 'table':
        return dict(n_cap=8, n=_n_text(8, meta, primary=primary), nc=5)
    return {}


def item_capacity(meta) -> int:
    """版式的主要条目列表最多放几条。

    给识别结果兜底用：模型没声明 `min/max_items` 时，用渲染器的框容量顶上 ——
    这个数只有渲染器知道，让模型自己报「我这版能放 5 条」等于让它自己出题。
    """
    for _b, _doc, vals in _doc_pairs(meta):
        if vals.get('n_cap'):
            return int(vals['n_cap'])
    return 0


def field_names(blocks):
    """这份声明用到哪些 spec 字段（`title` / `kicker` / `page` 不算 —— 页眉页脚）。"""
    out = []
    for b in blocks or []:
        f = b.get('field')
        if f and f not in out:
            out.append(f)
    return out


def _doc_pairs(meta):
    """逐区块产出 `(模板, 值字典)`，把「第一个条目型区块」标成 primary。

    为什么要标：一份版式里通常只有一个条目列表，它的条目数就是版式的
    `min/max_items`；其余区块（比如并列的一个小表）只能用框容量。
    """
    primary_used = False
    for b in meta.get('blocks') or []:
        doc = _FIELD_DOC.get(b.get('kind'))
        if not doc or not b.get('field'):
            continue
        primary = (b.get('kind') in _ITEM_KINDS and not primary_used)
        if primary:
            primary_used = True
        vals = dict(_block_cap(b, meta, primary=primary))
        vals['field'] = b['field']
        yield b, doc, vals


def catalog_of(meta) -> str:
    """给模型的字段契约（进 `layout_spec.catalog_for`）。

    从区块生成，**不是识别时手写的一段话** —— 手写的那份会与渲染器漂移，
    而这个仓库已经因为「同一件事写三处」吃过亏（见 layout_spec 的模块注释）。

    ⚠️ `meta['blocks']` 必须是 `normalize_blocks` **之后**的（英寸）：容量是按
    区块的实际框尺寸算的，喂原始比例值会算出「一条都放不下」（实测踩过）。
    落到磁盘上的声明本来就是归一化后的，所以正常路径不受影响。
    """
    lens = []
    for b, doc, vals in _doc_pairs(meta):
        try:
            lens.append('  - ' + doc[0] % vals)
        except KeyError:
            lens.append('  - %s：%s' % (b['field'], b.get('kind')))
    if not lens:
        return '必填 title（一句话主张，≤24 字）、kicker（章节标签）。'
    return ('必填（按区块给出，缺字段的那一块留空）：\n' + '\n'.join(lens)
            + '\n选填 kicker（章节标签）。')


def capacity_of(meta) -> str:
    """压文案时的容量说明（进 `layout_spec.capacity_text`），同样从区块生成。"""
    bits = []
    for b, doc, vals in _doc_pairs(meta):
        if not doc[1]:
            continue
        try:
            bits.append(doc[1] % vals)
        except KeyError:
            continue
    return '；'.join(bits) + '。' if bits else '尽量精简，控制在原文的 60% 以内。'


# ══════════════════════════════════════════════════════════════
# 校验：识别/新建的版式能不能入库
# ══════════════════════════════════════════════════════════════

import re                                                   # noqa: E402

_NAME_RE = re.compile(r'^[a-z][a-z0-9_]{2,31}$')


def problems(meta: dict, *, existing: set[str] | None = None) -> list[str]:
    """一份版式声明有什么问题 → 中文原因列表（空 = 可以入库）。

    返回人话而不是抛异常：这些原因要原样贴进「带违例清单重试」的提示词里
    （与 `pipeline._outline_problems` 同一套路数），也要显示给用户。
    """
    from . import layout_spec

    out = []
    name = _str(meta.get('name'))
    if not _NAME_RE.match(name):
        out.append('版式名 %r 不合规：只能用小写字母、数字、下划线，3–32 字符，'
                   '且以字母开头' % name)
    if existing and name in existing:
        out.append('版式名 %s 已被占用' % name)

    roles = tuple(meta.get('roles') or ())
    if not roles:
        out.append('缺少 roles（应为 %s 中的一个）' % '、'.join(layout_spec.ROLES))
    for r in roles:
        if r not in layout_spec.ROLES:
            out.append('roles 里的 %r 不是合法角色（可选 %s）'
                       % (r, '、'.join(layout_spec.ROLES)))

    intents = tuple(meta.get('intents') or ())
    for it in intents:
        if it not in layout_spec.INTENTS:
            out.append('intents 里的 %r 不是合法意图（可选 %s）'
                       % (it, '、'.join(layout_spec.INTENTS)))
    if not intents and roles != ('section',):
        out.append('缺少 intents —— 内容版式必须声明它承担哪种表达意图，'
                   '否则永远不会被选中')

    lo, hi = meta.get('min_items'), meta.get('max_items')
    if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
        out.append('min_items(%d) 大于 max_items(%d)' % (lo, hi))

    blocks = meta.get('blocks') or []
    if not blocks:
        out.append('没有任何区块（blocks 为空）—— 这个版式画不出东西')
    for b in blocks:
        if b.get('kind') not in BLOCK_KINDS:
            out.append('区块种类 %r 不认识（可选 %s）'
                       % (b.get('kind'), '、'.join(BLOCK_KINDS)))
    fields = set(field_names(blocks))
    sample = meta.get('sample') or {}
    for f in sorted(fields):
        if f not in sample:
            out.append('区块用到字段 %s，但样例（sample）里没有它 —— '
                       '没法渲出试片，也就没法自检' % f)

    if not _str(meta.get('signature')).strip():
        out.append('缺少 signature（一句话说明这版式的视觉特征）')
    return out
