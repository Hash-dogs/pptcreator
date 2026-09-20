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
import os
import re

SUPPORTED = ('.pptx', '.docx', '.pdf', '.txt', '.md', '.markdown')


def parse(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    if ext == '.pptx':
        doc = _parse_pptx(path)
    elif ext == '.docx':
        doc = _parse_docx(path)
    elif ext == '.pdf':
        doc = _parse_pdf(path)
    elif ext in ('.txt', '.md', '.markdown'):
        doc = _parse_text(path)
    else:
        raise ValueError('不支持的格式 %s；支持：%s' % (ext, ' / '.join(SUPPORTED)))
    blocks = _clean(doc['blocks'])
    doc['blocks'] = blocks
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


def _parse_pptx(path: str) -> dict:
    """按**字号**判断每页的标题，而不是取第一个文本框。

    生成的 deck 里每页第一个文本框通常是 kicker（如「01 初识 DIFY」），
    真正的标题字号更大（如「什么是 Dify」）。取第一个会把标题认错。
    """
    from pptx import Presentation
    prs = Presentation(path)
    blocks, title = [], None
    for i, slide in enumerate(prs.slides, 1):
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
        # 标题 = 字号最大的那段；同字号取位置最靠上的
        head = None
        if items:
            items.sort(key=lambda x: (-x[1], x[2]))
            head = items[0][0]
            items = items[1:]
        head = head or ('第 %d 页' % i)
        if title is None:
            title = head
        blocks.append(dict(type='heading', level=1, text=head))
        for t, _, _ in items:
            blocks.append(dict(type='para', text=t))
        if bullets:
            blocks.append(dict(type='bullets', items=bullets))
        blocks.extend(tables)
    return dict(source=path, kind='pptx', title=title, blocks=blocks)


def _parse_docx(path: str) -> dict:
    import docx                                     # python-docx
    d = docx.Document(path)
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
    return dict(source=path, kind='docx', title=title, blocks=blocks)


def _parse_pdf(path: str) -> dict:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(path)
    blocks, title = [], None
    for i in range(len(pdf)):
        t = pdf[i].get_textpage().get_text_range().replace('\r', '')
        lines = [l.strip() for l in t.split('\n') if l.strip()]
        if not lines:
            continue
        head = lines[0]
        if title is None:
            title = head
        blocks.append(dict(type='heading', level=1, text=head))
        for l in lines[1:]:
            blocks.append(dict(type='para', text=l))
    return dict(source=path, kind='pdf', title=title, blocks=blocks)


def _parse_text(path: str) -> dict:
    with open(path, encoding='utf-8-sig') as f:
        raw = f.read()
    blocks, title = [], None
    for line in raw.split('\n'):
        t = line.strip()
        if not t:
            continue
        m = re.match(r'^(#{1,6})\s+(.*)$', t)
        if m:
            lvl = len(m.group(1))
            if title is None:
                title = m.group(2)
            blocks.append(dict(type='heading', level=lvl, text=m.group(2)))
        elif re.match(r'^\d+(\.\d+)*[\s、.]', t) and len(t) < 60:
            blocks.append(dict(type='heading', level=2, text=t))
        else:
            blocks.append(dict(type='para', text=t))
    return dict(source=path, kind='text', title=title, blocks=blocks)


# ── 清理 ──────────────────────────────────────────────────────
def _clean(blocks: list[dict]) -> list[dict]:
    """去页眉页脚重复、去纯页码、合并相邻同类块。"""
    out = []
    seen_head = {}
    for b in blocks:
        if b['type'] == 'heading':
            key = b['text']
            seen_head[key] = seen_head.get(key, 0) + 1
            # 同一标题重复出现 3 次以上 → 判定为页眉，只保留第一次
            if seen_head[key] > 3:
                continue
        if b['type'] == 'para':
            t = b['text'].strip()
            if re.fullmatch(r'[\d\s/·—\-]{1,8}', t):     # 纯页码
                continue
            if len(t) < 2:
                continue
            if out and out[-1]['type'] == 'para' and len(out[-1]['text']) < 40:
                out[-1] = dict(type='para', text=out[-1]['text'] + t)   # 合并被切断的句子
                continue
        if b['type'] == 'bullets' and not b['items']:
            continue
        out.append(b)
    return out


def outline_source(doc: dict, max_chars: int = 12000) -> str:
    """把解析结果压成喂给模型的紧凑文本（超长时按比例截断）。"""
    lines = []
    for b in doc['blocks']:
        if b['type'] == 'heading':
            lines.append('%s%s' % ('#' * min(b.get('level', 1), 3), b['text']))
        elif b['type'] == 'para':
            lines.append(b['text'])
        elif b['type'] == 'bullets':
            lines.extend('- ' + i for i in b['items'])
        elif b['type'] == 'table':
            lines.append(' | '.join(b['header']))
            lines.extend(' | '.join(r) for r in b['rows'])
    txt = '\n'.join(lines)
    if len(txt) > max_chars:
        head = txt[:int(max_chars * 0.6)]
        tail = txt[-int(max_chars * 0.35):]
        txt = head + '\n\n…（中间省略）…\n\n' + tail
    return txt
