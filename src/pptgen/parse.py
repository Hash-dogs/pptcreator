# -*- coding: utf-8 -*-
"""源文档解析：pptx / docx / pdf / txt / md → 统一的结构化 blocks。

输出结构::

    {
      "source": "Dify 介绍与实战.pptx",
      "kind": "pptx",
      "title": "…",                     # 猜出来的文档标题
      "blocks": [
        {"type": "heading", "level": 1, "text": "01 初识 Dify"},
        {"type": "para",    "text": "…"},
        {"type": "bullets", "items": ["…", "…"]},
        {"type": "table",   "header": [...], "rows": [[...], ...]},
      ],
      "stats": {"blocks": 42, "chars": 12345},
    }
"""
from __future__ import annotations
import collections
import io
import os
import re

from . import structure

SUPPORTED = ('.pptx', '.docx', '.pdf', '.txt', '.md', '.markdown')

# ── DLP 哨兵 ──────────────────────────────────────────────────
# 本机有些文件带透明加密头（%TSD-Header-###%），非信任进程打开拿到的是密文。
# 上传入口尤其容易撞上：读文件的那一方若不是受信任进程，递上来的就是密文。
# 不拦住的话，python-docx / pypdfium2 会抛 "not a zip file"/"corrupted data"
# 这类误导性报错，看起来像文件损坏。
DLP_MAGIC = b'%TSD-Header-###%'

DLP_HINT = ('文件带本机 DLP 的加密头，拿到的是密文而不是原文 —— '
            '换一份未加密的副本，或先用 cmd.exe 把明文导出来再上传。')


def is_dlp(data: bytes) -> bool:
    """字节流是不是 DLP 密文。

    上传入口也用它：客户端（浏览器 / curl）若不是加密客户端信任的进程，
    读出来的就是密文 —— 与其等到解析时才炸，不如上传时就说清楚。
    """
    return data[:len(DLP_MAGIC)] == DLP_MAGIC


def _guard_dlp(path: str):
    with open(path, 'rb') as f:
        if f.read(len(DLP_MAGIC)) == DLP_MAGIC:
            raise ValueError('%s：%s' % (os.path.basename(path), DLP_HINT))


def parse(path: str) -> dict:
    """从磁盘解析源文档。"""
    _guard_dlp(path)
    ext = os.path.splitext(path)[1].lower()
    return _dispatch(path, path, ext)


def parse_bytes(data: bytes, name: str) -> dict:
    """从内存解析，绕开文件系统。

    Web 上传优先走这条：字节已经在服务端手里，没必要再经一次磁盘。
    落盘那份仍在（负责持久化），两边互为兜底。
    """
    if is_dlp(data):
        raise ValueError('%s：%s' % (name, DLP_HINT))
    ext = os.path.splitext(name)[1].lower()
    return _dispatch(io.BytesIO(data), name, ext)


def _dispatch(src, name: str, ext: str) -> dict:
    """`src` 可以是路径，也可以是 file-like —— 四个解析器都接受两种。"""
    if ext == '.pptx':
        doc = _parse_pptx(src, name)
    elif ext == '.docx':
        doc = _parse_docx(src, name)
    elif ext == '.pdf':
        doc = _parse_pdf(src, name)
    elif ext in ('.txt', '.md', '.markdown'):
        doc = _parse_text(src, name)
    else:
        raise ValueError('不支持的格式 %s；支持：%s' % (ext, ' / '.join(SUPPORTED)))
    blocks = _clean(doc['blocks'])
    doc['blocks'] = blocks
    # 骨架在 `_clean` **之后**算：块的下标会被 `_clean` 改动（合并短段、
    # 丢页码），在下标定下来之前算出来的区间是错的。
    # `outline` 只有 PDF 有（`/Outlines` 书签树），是比版面推断更硬的章节信号，
    # 所以单独传给骨架层，而不是塞进块流里。
    doc['structure'] = structure.build_skeleton(blocks, doc.get('kind', ''),
                                                doc.get('outline'))
    doc['stats'] = dict(blocks=len(blocks),
                        chars=sum(len(_block_text(b)) for b in blocks))
    return doc


def _block_text(b: dict) -> str:
    if b['type'] == 'bullets':
        return ''.join(b['items'])
    if b['type'] == 'table':
        return ''.join(b['header']) + ''.join(''.join(r) for r in b['rows'])
    return b.get('text', '')


# ── 各格式 ────────────────────────────────────────────────────
def _para_font_pt(p, default: float = 0.0) -> float:
    """段落内最大字号；未显式设字号时返回 default。"""
    best = default
    for r in p.runs:
        if r.font.size is not None:
            best = max(best, r.font.size.pt)
    return best


# ── pptx 的版面判据 ───────────────────────────────────────────
# 「像不像标题」的判据（挡掉 `60,000+` / `01` 这类数字噪声）与骨架层共用，
# 见 `structure.is_title_like` —— 两处用同一把尺子，免得解析层认成标题、
# 大纲层又当成噪声。
_is_title_like = structure.is_title_like

# 纯序号：分隔页上的巨号 `01`，或 `IV`。
_ORDINAL_RE = re.compile(r'^(?:0?\d{1,2}|[IVX]{1,4})$')

# 目录页标志。
_TOC_RE = re.compile(r'^(?:目\s*录|目\s*錄|CONTENTS?|Table of Contents)$', re.I)

# 卡片左上角的章节标签：`01 初识 DIFY`、`04 实战一 · 聊天助手`。
_KICKER_RE = re.compile(r'^(\d{1,2})\s*[、.·]?\s+(\S.*)$')

# 分隔页判据用**相对**字号：≥ 全 deck 标题字号中位数 × 2.2。
# 不写字面 pt 值 —— 换个模板（分隔数字 60pt）或换份源就失效了。
# 实测本 deck：标题中位 29pt → 阈值 64pt；分隔页 120pt 通过，
# 封面 52pt / 封底 40pt / 正文 29–36pt 全部不通过，余量很大。
_DIVIDER_RATIO = 2.2


def _slide_items(slide):
    """把一个 slide 拆成 (非列表段, 列表项, 表格)。

    非列表段收集成 `(文本, 字号, top)`；列表项与表格分开返回。
    """
    items, bullets, tables = [], [], []
    for sh in slide.shapes:
        if getattr(sh, 'has_table', False):
            rows = [[c.text.strip() for c in r.cells] for r in sh.table.rows]
            if rows:
                tables.append(dict(type='table', header=rows[0], rows=rows[1:]))
            continue
        if not sh.has_text_frame:
            continue
        for p in sh.text_frame.paragraphs:
            t = p.text.strip()
            if not t:
                continue
            is_bullet = (p.level or 0) > 0 or t.startswith(('•', '-', '·', '▪'))
            body = t.lstrip('•-·▪ ').strip()
            if is_bullet:
                bullets.append(body)
            else:
                items.append((body, _para_font_pt(p, 18.0),
                              sh.top if sh.top is not None else 0))
    return items, bullets, tables


def _pick_title(items):
    """挑本页标题，返回 (标题, 余下的段)。

    「字号最大」单条判据会选错：实测 S4 的 `60,000+`（30pt，其实是数据展示）
    压过了真标题 `什么是 Dify`（29pt），于是真标题被当成正文。
    所以先在候选里挑「像标题的」，都不像才退回字号最大者（宁可有个标题）。
    """
    if not items:
        return None, []
    ordered = sorted(items, key=lambda x: (-x[1], x[2]))
    for i, (t, _, _) in enumerate(ordered):
        if _is_title_like(t):
            return t, ordered[:i] + ordered[i + 1:]
    return ordered[0][0], ordered[1:]


# 章节标签所在的顶部带（占页高比例）。它是**位置**判据，不是字号判据 ——
# 早先用「字号 ≤ 标题 ×0.6」过滤，实测这份 deck 的标签是 18pt、标题 29pt，
# 比值 0.62 刚好被挡掉，整份只剩两页认出来。
_KICKER_BAND = 0.12


def _pick_kicker(rest, title_font: float, slide_h: float):
    """卡片左上角的章节标签（`01 初识 DIFY`），即这页的章节归属。

    判据 = 在顶部带内 + 字号比标题小 + 形如「序号 + 文字」+ 文字不太长。
    位置是主判据：标签永远在最上面一条，而正文里的编号段落不会跑到标题上方。
    """
    for t, font, top in sorted(rest, key=lambda x: x[2]):
        if top > slide_h * _KICKER_BAND:
            break                       # 已越过顶部带，后面只会更低
        if font > title_font:
            continue
        m = _KICKER_RE.match(t)
        if m and len(m.group(2)) <= 24:
            return t
    return None


def _divider_parts(items):
    """分隔页 → (序号, 章节名, 副标题)。

    版面固定是「巨号序号 + SECTION NN + 章节名 + 一句话说明」四层，
    所以**按字号排就行**：最大的那个是序号，次大的就是章节名。
    别用「像不像标题」去挑章名 —— 那个判据要求 ≥4 个实义字符，而
    「第一章」这种只有 3 个字的章名会被判成不是标题，章名于是错取成
    下面那行说明文字（实测过）。
    """
    ordinal = None
    rest = []
    for t, font, top in sorted(items, key=lambda x: (-x[1], x[2])):
        if ordinal is None and _ORDINAL_RE.match(t) and font >= 40:
            ordinal = t
            continue
        if re.match(r'^SECTION\b', t, re.I) or _ORDINAL_RE.match(t) or not t.strip():
            continue
        rest.append(t)
    name = rest[0] if rest else None           # 字号次大的就是章节名
    subtitle = max(rest[1:], key=len) if len(rest) > 1 else None
    return ordinal, name, subtitle


def _parse_pptx(src, name: str) -> dict:
    """把每页拆成块，并**保留页面的角色与层级**。

    早先这里把所有标题都拍成 `level=1`，于是分隔页 / 目录页 / 正文页在块流里
    完全一样 —— 章节结构在下游无从恢复（`_outline_fallback` 只能靠数字前缀
    猜，实测整份文档猜成了 1 个章节）。

    现在两遍扫描：先拆页并算出全 deck 的标题字号中位数，再据此分类
    （分隔页 / 目录页 / 封面 / 封底 / 正文），给每页的 heading 块打上
    `role`、`level`（正文页 2、分隔页 1）和 `kicker`。
    """
    from pptx import Presentation
    prs = Presentation(src)
    slides = list(prs.slides)
    slide_h = float(prs.slide_height or 0) or 6858000.0        # EMU，兜底 7.5"

    # 第一遍：拆页 + 定标题
    pages = []
    for i, slide in enumerate(slides, 1):
        items, bullets, tables = _slide_items(slide)
        head, rest = _pick_title(items)
        pages.append(dict(i=i, items=items, head=head, rest=rest,
                          bullets=bullets, tables=tables))

    # 全 deck 的**每页最大字号**的中位数 → 分隔页阈值（相对判据）。
    # 取「每页最大」而不是 `items[0]`：items 是按形状出现顺序排的，
    # 第一段是哪一段纯属偶然，拿它当代表值阈值会飘。
    fonts = sorted(max(f for _, f, _ in p['items']) for p in pages if p['items'])
    median = fonts[len(fonts) // 2] if fonts else 24.0
    divider_min = median * _DIVIDER_RATIO

    blocks, title = [], None
    for p in pages:
        i, head = p['i'], p['head']
        big = max([f for _, f, _ in p['items']] or [0])
        has_ordinal = any(_ORDINAL_RE.match(t) and f >= 40 for t, f, _ in p['items'])
        # 目录页要在**挑标题之前**认出来：目录页最大字号的那段是「目录」两个字，
        # 只有 2 个实义字符、过不了 `_is_title_like`，于是 `_pick_title` 会退到
        # 字号次大的**目录条目**（如「初识 Dify」）上，把条目当成了页标题。
        # 所以这里扫**全部**段落文本，而不是只看选中的标题。
        is_toc = any(_TOC_RE.match(t) and f >= 18 for t, f, _ in p['items'])
        kicker = _pick_kicker(p['rest'], big, slide_h) if head else None

        # 角色判定顺序有讲究：目录 → 分隔页 → 封面 → 封底 → 正文。
        if is_toc:
            role = 'toc'
        elif has_ordinal and big >= divider_min:
            role = 'divider'
        elif i == 1:
            role = 'cover'
        elif i == len(pages) and not kicker:
            role = 'back'
        else:
            role = 'body'

        if role == 'divider':
            ordinal, sec_name, subtitle = _divider_parts(p['items'])
            label = ' '.join(x for x in (ordinal, sec_name) if x) or head
            blk = dict(type='heading', level=1, text=label, slide=i,
                       role='divider', ordinal=ordinal)
            if subtitle:
                blk['subtitle'] = subtitle
            head = label
        else:
            blk = dict(type='heading', level=2 if role == 'body' else 1,
                       text=head or ('第 %d 页' % i), slide=i, role=role)
            if kicker:
                blk['kicker'] = kicker
        if title is None:
            title = blk['text']
        blocks.append(blk)

        # 分隔页与目录页的其余文字不再进正文：分隔页的已经被折进 heading 的
        # subtitle；目录页的那些段是**各章标题拼在一起的产物**（解析器不保留
        # 文本框边界，会粘成「初识 Dify为什么选 DifyDify 能做什么…」），
        # 留着只会污染章节检测和喂给模型的内容。
        if role in ('divider', 'toc'):
            continue
        for t, _, _ in p['rest']:
            if t == kicker:
                continue
            blocks.append(dict(type='para', text=t, slide=i))
        if p['bullets']:
            blocks.append(dict(type='bullets', items=p['bullets'], slide=i))
        for tb in p['tables']:
            blocks.append(dict(tb, slide=i))
    return dict(source=name, kind='pptx', title=title, blocks=blocks)


def _parse_docx(src, name: str) -> dict:
    import docx                                     # python-docx
    d = docx.Document(src)
    blocks, title = [], None
    pending = []

    def flush():
        nonlocal pending
        if pending:
            blocks.append(dict(type='bullets', items=list(pending)))
            pending = []

    for p in d.paragraphs:
        t = (p.text or '').strip()
        if not t:
            continue
        style = (p.style.name or '').lower()
        m = re.search(r'heading\s*(\d)', style)
        if m:
            flush()
            lvl = int(m.group(1))
            if title is None and lvl <= 2:
                title = t
            blocks.append(dict(type='heading', level=lvl, text=t))
        elif 'list' in style or t.startswith(('•', '-', '·')):
            pending.append(t.lstrip('•-· ').strip())
        else:
            flush()
            blocks.append(dict(type='para', text=t))
    flush()
    for tb in d.tables:
        rows = [[c.text.strip() for c in r.cells] for r in tb.rows]
        if rows:
            blocks.append(dict(type='table', header=rows[0], rows=rows[1:]))
    return dict(source=name, kind='docx', title=title, blocks=blocks)


def _parse_pdf(src, name: str) -> dict:
    """PDF → blocks。**先认标题，再分页。**

    早先这里把每页第一行直接当标题。实测上传的《Dify 介绍与实战》每页第一行是
    **页码**（`1`/`2`/…/`39`），于是 40 页抽出 41 个 `1`、`2` 这样的「标题」，
    连 `doc['title']` 都成了 `"1"`；真正的章标题（`1 初识 Dify`、`第二章 …`）躺在
    正文里当段落，骨架只看到一页一个纯数字，最后整份文档压成 **1 个章节**。

    现在按开源实现收敛出来的信号优先级抽标题：

        书签(/Outlines)  →  编号模式  →  字号

    书签是文档自带的目录，比任何版面推断都硬（实测这份 PDF 带完整三级书签），
    所以有书签时不再按版面猜 —— 两种信号混在一起只会互相打架。都没有时退到
    「每页一个页单元」，也就是改进前的粒度，不会更糟。
    """
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(src)
    pages = [_pdf_lines(pdf[i]) for i in range(len(pdf))]

    # 页码与页眉页脚要在**算正文高度之前**去掉：它们是噪声，会带动基线。
    running = _pdf_running_lines(pages)
    for ls in pages:
        keep = []
        for k, x in enumerate(ls):
            hit = x['text'] in running
            # 页码只认页首/页尾那个位置：正文里的 `1`、`2` 可能是列表项编号。
            if not hit and k in (0, len(ls) - 1) and _PAGE_NUM_RE.match(x['text']):
                hit = True
            if not hit:
                keep.append(x)
        ls[:] = keep

    body = _pdf_body_height([x for ls in pages for x in ls])
    bookmarks = _pdf_bookmarks(pdf)

    blocks, title = [], None
    for i, ls in enumerate(pages, 1):
        if not ls:
            continue
        here = [b for b in bookmarks if b['page'] == i]
        levels: dict[int, int] = {}
        cover = set()
        for k, x in enumerate(ls):
            hit = _pdf_bookmark_level(x['text'], here)
            if hit is not None:
                levels[k] = hit
            elif not here:                  # 有书签就以书签为准，不按版面猜
                lv = _pdf_line_level(x, body)
                if lv is not None:
                    levels[k] = lv
                    # 第 1 页上**只靠字号**升出来的行 = 封面标题 / 装饰，不是章。
                    # 不挡的话封面那两行大字会各成一个「章」（实测：22pt 的标题行
                    # 16pt 的字盒，是正文 8pt 的两倍，稳定过 1.60 那道线）。
                    # 带编号的（`第一章 …`、`1 绪论`）不算 —— 那页就是正文第一页，
                    # 把整页当前置信息会让它的正文也一起消失。
                    if i == 1 and _pdf_pattern_level(x['text']) is None:
                        cover.add(k)
        toc = _pdf_is_toc_page(ls)
        if toc and not levels:
            # 目录页整页都是点线引导，一行标题都升不出来 —— 但它必须是个页单元，
            # 否则会被当成「没有前置页」而混进正文。
            levels[0] = _FALLBACK_LEVEL
        if not levels:
            # **兜底页单元**：这一页没有标题，用第一行当页单元名。
            # 每页都得有页单元，否则「大纲标题对了、正文却取不到」——
            # `_content_index` 是靠页名 → 块区间取内容的。
            levels[0] = _FALLBACK_LEVEL
        for k, x in enumerate(ls):
            if k in levels:
                blk = dict(type='heading', level=levels[k], text=x['text'], slide=i)
                if toc:
                    blk['role'] = 'toc'
                elif k in cover:
                    blk['role'] = 'cover'
                blocks.append(blk)
            else:
                blocks.append(dict(type='para', text=x['text'], slide=i))
        if i == 1:
            title = _pdf_cover_title(ls)
    return dict(source=name, kind='pdf', title=title, blocks=blocks,
                outline=bookmarks)


# ── pdf 的版面判据 ────────────────────────────────────────────
# 页码：单独占一行、只有数字或罗马数字。挡掉实测的 `1`…`39`。
_PAGE_NUM_RE = re.compile(r'^(?:\d{1,4}|[ivxlcdmIVXLCDM]{1,5})$')

# 显式章节编号。中英文 PDF 里最硬的层级信号 —— 实测制度文件里正文 13.2pt、
# `第一章 总则` 13.1pt，**字号完全分不开**，只有编号能认。
# 单位表取自 RAGFlow `proj_match()` 与 LightRAG 的 `STYLE_KEY_PRIORITY`：
# `部分` 要排在 `部` 前面，否则 `第二部分` 只会吃掉一个「部」字。
_PDF_CHAP_RE = re.compile(r'^第\s*[\d一二三四五六七八九十百千]+\s*(?:章|篇|部分|部|编|卷)')
_PDF_SECT_RE = re.compile(r'^第\s*[\d一二三四五六七八九十百千]+\s*[条节]')
# 中文数字枚举：`一、概述`。RAGFlow 把它排在 `第X章`/`第X条` 之后。
_PDF_CN_NUM_RE = re.compile(r'^[一二三四五六七八九十]{1,3}[、.．]\s*\S')
# 括号枚举：`（一）概述` / `(1) 概述`
_PDF_CN_PAREN_RE = re.compile(r'^[（(](?:[一二三四五六七八九十]{1,3}|\d{1,2})[）)]\s*\S')
# 多级编号 `1.1` / `1.2.3`（`1. 定义` 不在此列 —— 点号后必须跟数字）。
_PDF_MULTI_RE = re.compile(r'^(\d{1,2}(?:\.\d{1,2}){1,3})\s*[、.．]?\s+\S')
# 单级编号 `1. 定义：` / `3、小结`
_PDF_ONE_RE = re.compile(r'^(\d{1,2})\s*[、.．]\s*\S')
# 光秃秃的序号：`1 初识 Dify`（中文书里最常见的一种章标题写法）
_PDF_BARE_NUM_RE = re.compile(r'^(\d{1,2})\s+\S')

# 编号层级的**相对阶梯**，抄 RAGFlow `proj_match()` / LightRAG `STYLE_KEY_PRIORITY`
# 的顺序：章 → 条/节 → `一、` → `（一）` → 单级阿拉伯数字 → 多级数字（越深越细）。
# 数值本身没有意义，`depth` 取的是「最浅且出现 ≥2 次的那一层」，
# 所以一份只有 `一、` 枚举的文档照样能分成几章。
_LV_CHAP, _LV_SECT = 1, 2
_LV_CN_NUM, _LV_CN_PAREN, _LV_ENUM = 3, 4, 5

# 点线引导的目录行：`前言........................ 7`
_DOTLEAD_RE = re.compile(r'[.．·]{5,}\s*\d{0,3}\s*$')

# 标题长度上限。**单级编号判得最保守**：实测制度文件里
# `1. 负责基础 IT 架构运维，为 AI 应用提供计算资源、网络支撑。`（33 字）这类
# 列表项会被当成小节标题，凭空多出两章。所以单级编号要短、且不能以句末标点收尾。
_PDF_HEAD_MAX = 30
_PDF_ONE_MAX = 20

# 字号判据用**相对**比例：行高 ≥ 正文行高 × 1.30 才算大字号标题，
# ≥ 1.60 才够第 1 层。实测《Dify》正文 10.2 / 二级 14.1（1.38×）/ 一级 20.0（1.96×）；
# 制度文件标题 15.1 / 正文 13.2 = 1.14×，正好该被挡在门外（它靠编号分章）。
_BODY_RATIO = 1.30
_CHAP_RATIO = 1.60

# 兜底页单元的层级。**不声称层级**，所以沉到第 2 层：
# 它只是个内容单元，不该去争「章节」那一层 —— 否则末页那种没有 `第X章` 的页面
# 会凭空多出一个 level 1 标题，被 `depth` 方法当成一章（实测制度文件会多出第 5 章）。
_FALLBACK_LEVEL = 2


def _pdf_lines(page) -> list[dict]:
    """一页 → 逐行 `{text, h}`，h 是该行字符盒高度的最大值（≈ 该行字号）。

    走 `get_charbox()` 而不是 `FPDFText_GetFontSize()` —— 后者在实测的两份
    PDF 上**一律返回 1.0**（文本层被矩阵缩放过），标题和正文完全分不开。
    charbox 给的是渲染后的盒子，高度直接可比。

    字符下标要在**原始文本**上累加：`\r` 也是字符，先 `replace` 掉再数下标，
    整页的偏移都会错位（行高于是张冠李戴）。
    """
    tp = page.get_textpage()
    raw = tp.get_text_range()
    out, off = [], 0
    for seg in raw.split('\n'):
        text = seg.rstrip('\r').strip()
        if text:
            hs = []
            for i in range(off, off + len(seg)):
                try:
                    _, bottom, _, top = tp.get_charbox(i)
                except Exception:           # 越界 / 无法成盒的字符
                    continue
                hs.append(top - bottom)
            out.append(dict(text=text, h=max(hs) if hs else 0.0))
        off += len(seg) + 1                 # +1 是 '\n' 本身
    return out


def _pdf_running_lines(pages: list[list[dict]]) -> set[str]:
    """跨页重复出现的行 = 页眉 / 页脚 / 水印，整份文档都丢掉。

    两个条件取向不同，都要：

    - `≥50% 的页`：每页都盖一遍的水印。实测制度文件的
      `迈胜医疗设备有限公司制度（信息安全）`是 7/7 页。
    - `≥3 页 且落在页首前两行 且 ≤40 字`：只在开头几页出现的页眉。实测
      `Dify: Product Introduction` 只在前 3 页，但它同时是第 2、3 页的**首行**，
      留着会把这两页的页单元名字污染成同一串。
    """
    if len([ls for ls in pages if ls]) < 3:
        return set()
    seen: collections.Counter = collections.Counter()
    for ls in pages:
        for t in {x['text'] for x in ls}:
            seen[t] += 1
    half = max(3, int(len([ls for ls in pages if ls]) * 0.5))
    heads = [tuple(x['text'] for x in ls[:2]) for ls in pages if ls]
    return {t for t, c in seen.items()
            if c >= 3 and (c >= half or
                           (len(t) <= 40 and any(t in h for h in heads)))}


def _pdf_body_height(lines: list[dict]) -> float:
    """正文行高：**按字符数加权**的行高众数。

    加权而不是取平均：几个大标题动不了它，短标题也不会因为条数多而压过正文。
    行高按 0.5pt 归并 —— 同一行里的字符本身有 0.1 上下的抖动。
    """
    weight: collections.Counter = collections.Counter()
    for x in lines:
        if x['h'] > 0:
            weight[round(x['h'] * 2) / 2.0] += len(x['text'])
    return weight.most_common(1)[0][0] if weight else 0.0


# 行 / 书签标题的归一：PDF 抽出来的文本里空白很不规律（连着几个空格、
# 全角空格、行尾空格），比对前统一成单空格。
_SPACES_RE = re.compile(r'\s+')

# 标题开头的编号标记。比对书签与正文时要**带不带编号两种写法都比一遍** ——
# 书签里写 `1.2 什么是 Dify`、正文那行写 `什么是 Dify`（或反过来）都常见。
_MARKER_RE = re.compile(
    r'^(?:第\s*[\d一二三四五六七八九十百千]+\s*(?:部分|部|章|篇|编|卷|[条节])\s*'
    r'|[\d一二三四五六七八九十]{1,3}\s*[、.．]\s*'
    r'|\d{1,3}(?:\.\d{1,3}){1,3}\s*[、.．]?\s+'
    r'|\d{1,3}\s+)')


def _norm_line(s: str) -> str:
    return _SPACES_RE.sub(' ', s or '').strip()


def _pdf_bookmarks(pdf) -> list[dict]:
    """`/Outlines` → `[{level, title, page}]`（页码 1 基）。

    这份书签树就是文档自己的目录，骨架层直接拿它当章节划分（`method='outline'`）。
    """
    out = []
    for e in pdf.get_toc():
        t = _norm_line(e.get_title())
        # 书签里偶有整段重复（实测 `4.2 x 4.2 x `）—— 对齐后去重，否则页单元名很长。
        half = len(t) // 2
        if half > 3 and t[:half].strip() == t[half:].strip():
            t = t[:half].strip()
        try:
            page = e.get_dest().get_index() + 1
        except Exception:                   # 书签指向了不存在的页
            continue
        if t:
            out.append(dict(level=int(e.level or 0), title=t, page=page))
    return out


def _pdf_bookmark_level(text: str, here: list[dict]):
    """这一行是不是本页某条书签的标题？是的话返回它的层级（level + 1）。

    匹配要宽容一点：书签里常带编号而正文那行不带（或反过来），所以**带不带编号
    两种写法都比一遍**（docling 的 `HeadingHierarchyModel` 就是这么做的）。
    反方向（正文是书签的前缀）要求 ≥4 个字 —— 否则页码那种残行会匹配上书签。
    """
    t = _norm_line(text)
    for b in here:
        for form in (b['title'], _MARKER_RE.sub('', b['title']).strip()):
            if not form:
                continue
            if t == form or t.startswith(form + ' '):
                return b['level'] + 1
            if len(t) >= 4 and form.startswith(t + ' '):
                return b['level'] + 1
    return None


def _pdf_pattern_level(text: str):
    """编号模式给出的层级；不是编号标题返回 None。

    层级见 `_LV_*` 那张相对阶梯。中英文写法都认 —— 中文文档的
    `第X章`/`第X条`/`一、`/`（一）` 是两条纯文本路都指望不上的东西
    （`structure._CHAPTER_RE` 只认前两种）。
    """
    t = text.strip()
    if _PDF_CHAP_RE.match(t):
        return _LV_CHAP
    if _PDF_SECT_RE.match(t):
        return _LV_SECT
    if _PDF_MULTI_RE.match(t) and len(t) <= _PDF_HEAD_MAX:
        return _LV_ENUM + _PDF_MULTI_RE.match(t).group(1).count('.')
    if _PDF_CN_PAREN_RE.match(t):
        return _LV_CN_PAREN if _enum_ok(t) else None
    if _PDF_CN_NUM_RE.match(t):
        return _LV_CN_NUM if _enum_ok(t) else None
    if _PDF_ONE_RE.match(t) or _PDF_BARE_NUM_RE.match(t):
        return _LV_ENUM if _enum_ok(t) else None
    return None


def _enum_ok(t: str) -> bool:
    """枚举类编号（`一、`、`1.`、`1 `）的护栏：**要短、且不能以句末标点收尾**。

    否则正文里的列表项会被当成小节标题 —— 实测
    `1. 负责基础 IT 架构运维，为 AI 应用提供计算资源、网络支撑。` 让制度文件
    凭空多出两章。结尾的 `：` 不算句子结束：`1. 定义：` 正是「标签 + 内容在后」
    的小节标题写法。
    """
    return len(t) <= _PDF_ONE_MAX and not t.endswith(('。', '！', '？', '.', '!', '?'))


def _pdf_size_level(h: float, body: float):
    """字号给出的层级；不够大返回 None。"""
    if body <= 0 or h < body * _BODY_RATIO:
        return None
    return 1 if h >= body * _CHAP_RATIO else 2


def _pdf_line_level(x: dict, body: float):
    """无书签时的标题判据：编号与字号**取更浅的那个**。

    取更浅（而不是更可信的）是因为两者是「至少这么浅」的证据：编号说它是
    `第X章`（第 1 层）、字号说它只是 1.4 倍（第 2 层），那它是章的**可能性**不
    因为字号小而消失；反过来字号 2 倍但编号是 `1.1`，它至少是个小节。
    """
    pat = _pdf_pattern_level(x['text'])
    size = _pdf_size_level(x['h'], body)
    if pat is None:
        return size
    if size is None:
        return pat
    return min(pat, size)


def _pdf_is_toc_page(ls: list[dict]) -> bool:
    """目录页：有 `目录` / `CONTENTS` 行，或有 ≥3 行点线引导（`前言......7`）。"""
    if any(_TOC_RE.match(x['text']) for x in ls):
        return True
    return sum(1 for x in ls if _DOTLEAD_RE.search(x['text'])) >= 3


def _pdf_cover_title(ls: list[dict]):
    """封面标题：首页**最大字号**那几行拼起来。

    早先取 `lines[0]`，实测拿到的是页码 `"1"`。封面的大字标题常常被排版成两行
    （实测 `Dify: Product` + `Introduction`），所以同字号的连续行要拼回去。
    """
    hs = [x['h'] for x in ls if x['h'] > 0]
    if not hs:
        return None
    big = max(hs)
    picked, n = [], 0
    for x in ls:
        if x['h'] < big * 0.95:
            continue
        if n and n + len(x['text']) > 60:
            break
        picked.append(x['text'])
        n += len(x['text'])
        if len(picked) >= 3:
            break
    if not picked:
        return None
    t = ' '.join(picked)
    # 中英混排的排版习惯：汉字之间不留空格，汉字与拉丁字母之间留。
    t = re.sub(r'([一-鿿，。：；、（）「」]) +(?=[一-鿿，。：；、（）「」])', r'\1', t)
    return t.strip() or None


def _decode_text(raw: bytes) -> str:
    """中文 Windows 上的 .txt 多半是 GBK；先按 UTF-8（含 BOM）试，失败退 GBK。

    上传入口会收到任意的用户文本文件，只认 UTF-8 会让一半中文 txt 直接报错。
    """
    for enc in ('utf-8-sig', 'gbk'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def _parse_text(src, name: str) -> dict:
    if hasattr(src, 'read'):
        raw = _decode_text(src.read())
    else:
        with open(src, encoding='utf-8-sig') as f:
            raw = f.read()
    blocks, title, pending = [], None, []

    def flush():
        nonlocal pending
        if pending:
            blocks.append(dict(type='bullets', items=list(pending)))
            pending = []

    for line in raw.split('\n'):
        t = line.strip()
        if not t:
            flush()                     # 空行结束一个列表
            continue
        m = re.match(r'^(#{1,6})\s+(.*)$', t)
        if m:
            flush()
            lvl = len(m.group(1))
            if title is None:
                title = m.group(2)
            blocks.append(dict(type='heading', level=lvl, text=m.group(2)))
        elif re.match(r'^[-*+•·]\s+', t):
            # 与 _parse_docx 对齐：`- 要点` 是列表项，不是段落。
            # 不认的话会被 _clean 当短段落合并成一坨，上传 txt 时尤其明显。
            pending.append(re.sub(r'^[-*+•·]\s+', '', t).strip())
        elif re.match(r'^\d+(\.\d+)*[\s、.]', t) and len(t) < 60:
            flush()
            blocks.append(dict(type='heading', level=2, text=t))
        else:
            flush()
            blocks.append(dict(type='para', text=t))
    flush()
    return dict(source=name, kind='text', title=title, blocks=blocks)


# ── 清理 ──────────────────────────────────────────────────────
def _clean(blocks: list[dict]) -> list[dict]:
    """去页眉页脚重复、去纯页码、合并相邻同类块。"""
    out = []
    seen_head = {}
    for b in blocks:
        if b['type'] == 'heading':
            key = b['text']
            seen_head[key] = seen_head.get(key, 0) + 1
            # 同一标题重复出现 3 次以上 → 判定为页眉，只保留第一次。
            # pptx 的块带 `slide`：页面是按 slide 拆的，重复标题（章节标签之类）
            # 已经被收进 heading 的 `kicker` 字段、不再重复出现，所以这条规则
            # 对它没有意义，反而可能误删合法的同名标题。
            if 'slide' not in b and seen_head[key] > 3:
                continue
        if b['type'] == 'para':
            t = b['text'].strip()
            if re.fullmatch(r'[\d\s/·—\-]{1,8}', t):     # 纯页码
                continue
            if len(t) < 2:
                continue
            if out and out[-1]['type'] == 'para' and len(out[-1]['text']) < 40:
                # 合并被切断的句子。**要带上原块的其他键**（slide 等）——
                # 早先这里重建 dict 只留 type/text，把页码归属悄悄丢了，
                # 下游按区间取内容就会错位。
                merged = dict(out[-1])
                merged['text'] = merged['text'] + t
                out[-1] = merged
                continue
        if b['type'] == 'bullets' and not b['items']:
            continue
        out.append(b)
    return out


def _blocks_to_text(blocks: list[dict], lo: int = 0, hi: int | None = None,
                    skip: set[int] | None = None) -> str:
    lines = []
    for i in range(lo, len(blocks) if hi is None else hi):
        if skip and i in skip:
            continue
        b = blocks[i]
        if b['type'] == 'heading':
            lines.append('%s%s' % ('#' * min(b.get('level', 1), 3), b['text']))
        elif b['type'] == 'para':
            lines.append(b['text'])
        elif b['type'] == 'bullets':
            lines.extend('- ' + i for i in b['items'])
        elif b['type'] == 'table':
            lines.append(' | '.join(b['header']))
            lines.extend(' | '.join(r) for r in b['rows'])
    return '\n'.join(lines)


def outline_source(doc: dict, max_chars: int = 12000) -> str:
    """把解析结果压成喂给模型的紧凑文本。

    短文档直接全文给出（信息最全）。**超长时不再砍头去尾** —— 早先保留头 60%
    + 尾 35%，中段整段消失，而中段往往正是章节主体，模型于是只能靠运气猜。

    改成分两段：先给**骨架**（章节树 + 每页首句，结构永远完整），再按章均分
    剩余预算给正文。任何一章都不会因为「排在中间」而整体消失。
    """
    sk = doc.get('structure') or {}
    blocks = doc['blocks']
    # 封面 / 目录 / 封底不进正文：目录页的内容是各章标题粘在一起的产物，
    # 留在里面只会让模型把目录条目当成章节。
    front = set(sk.get('front_matter') or [])

    txt = _blocks_to_text(blocks, skip=front)
    if len(txt) <= max_chars or not sk.get('chapters'):
        return txt

    # ── 超长：骨架 + 按章配额的正文 ──────────────────────────────
    head = ('【文档结构】\n%s\n\n【正文（长文档，按章摘录）】\n'
            % structure.skeleton_digest(sk))
    budget = max(1000, max_chars - len(head))
    chapters = sk['chapters']
    per = max(600, budget // max(1, len(chapters)))
    out = [head]
    for c in chapters:
        seg = _blocks_to_text(blocks, c['start'], c['end'], skip=front)
        if len(seg) > per:
            seg = seg[:per] + '\n…（本章其余内容省略）…'
        out.append('## %s\n%s' % (c['name'], seg))
    return '\n\n'.join(out)
