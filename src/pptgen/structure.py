# -*- coding: utf-8 -*-
"""文档骨架：从解析出来的 blocks 里**确定性地**抽出章节结构。

这一层不调用模型，是整个大纲生成的地基。它存在的理由：

- 模型看到的应该是**结构**，而不是一坨被截断的正文。早先喂给大纲模型的是
  「头 60% + 尾 35%」的粗暴截断，长文档中段整段消失，章节结构更是无从谈起。
- 兜底路径也需要结构。没有模型时 `_outline_fallback` 只能靠标题的数字前缀猜
  章节，实测把一份 5 章的文档猜成了 1 章。

做法借鉴了 book-to-skill 的思路：**先确定性抽结构，再让模型在结构上生成**
（"Structure, not a summary"）。章节信号按可靠性从高到低尝试：

    divider  分隔页（pptx 里那张「巨号序号 + 章节名」的页）
    kicker   每页左上角的章节标签（`01 初识 DIFY`）
    numeric  显式数字标题（`第3章` / `Chapter 5` / `01 产品介绍`）
    depth    标题层级（docx 的 Heading 1、md 的 `##`）
    flat     都没有 → 单章

方法认不出来就往下退化，最差退化到 `flat`（等同改进前的行为），不会更糟。
"""
from __future__ import annotations
import re

# ── 章节标记的模式 ────────────────────────────────────────────
# kicker / 数字标题的公共前缀：`01 初识 DIFY`、`03 DIFY 能做什么`、`1. 概述`
_NUM_PREFIX_RE = re.compile(r'^(\d{1,2})\s*[、.·]?\s+\S')

# 显式章节标题。中英文常见写法都认。
_CHAPTER_RE = re.compile(
    r'^(?:第\s*([\d一二三四五六七八九十百]+)\s*[章部节篇]'
    r'|(?:Chapter|Part|Section|CHAPTER|PART|SECTION)\s*(\d{1,3})'
    r'|(\d{1,2})\s*[、.·]?\s+\S)', re.I)

# 标题长度上限（book-to-skill 的护栏：超长的多半是段落不是标题）。
_MAX_TITLE_CHARS = 80

# 判定「是章节还是列表项」的护栏：numeric 方法下，章均正文太少的
# 多半是把教程步骤/列表项当成了章节。
_MIN_CHAPTER_CHARS = 120

_FRONT_ROLES = ('cover', 'toc', 'back')


def block_text(b: dict) -> str:
    """一个块的纯文本（三个解析器共用）。"""
    if b['type'] == 'bullets':
        return ''.join(b.get('items') or [])
    if b['type'] == 'table':
        return ''.join(b.get('header') or []) + ''.join(
            ''.join(r) for r in (b.get('rows') or []))
    return b.get('text', '')


def _title_ok(t: str) -> bool:
    return 3 <= len(t) <= _MAX_TITLE_CHARS


# 标题里至少要有 4 个「实义字符」（汉字/字母）才算标题。用来挡掉
# `60,000+`、`3 个`、`01` 这类**大字号但不是标题**的数字噪声 ——
# 它们是数据展示，却常常比真标题字号还大（实测 S4 的 30pt 数字压过了 29pt 的真标题）。
_WORDCHAR_RE = re.compile(r'[一-鿿A-Za-z]')


def is_title_like(t: str) -> bool:
    """这段文字像不像一个标题（而不是数字 / 装饰 / 序号）。

    解析层用它挑页标题，大纲层用它拦退化标题 —— 同一把尺子，避免
    「解析层认为是标题、大纲层认为是噪声」这种自相矛盾。
    """
    return len(_WORDCHAR_RE.findall(t or '')) >= 4


def _num_key(text: str):
    """从 `01 初识 DIFY` / `1. 概述` 这类文本里取章节号。取不到返回 None。"""
    m = _NUM_PREFIX_RE.match(text or '')
    return m.group(1) if m else None


def _chapter_key(text: str, level: int, method: str):
    """按指定方法，从标题文本里取章节号；取不到返回 None。"""
    t = (text or '').strip()
    if method == 'numeric':
        m = _CHAPTER_RE.match(t)
        if not m:
            return None
        return next((g for g in m.groups() if g), None)
    return None


# ══════════════════════════════════════════════════════════════
# ① 找章节标记
# ══════════════════════════════════════════════════════════════
def _marks_from_roles(blocks: list[dict]) -> list[dict]:
    """走 pptx 的 `role` / `kicker`，返回章节标记。

    分隔页与 kicker **取并集**，不是谁优先 —— 实测这份 deck 的第 5 章
    只有 kicker（`05 总结`）没有分隔页，只认分隔页会少一章。
    章节名优先用分隔页给的（更完整：`04 基于 Dify 的开发实战`），
    没有分隔页才用 kicker 文本（`05 总结`）。
    """
    marks: list[dict] = []
    for i, b in enumerate(blocks):
        if b['type'] != 'heading':
            continue
        role = b.get('role')
        if role in _FRONT_ROLES:
            continue
        if role == 'divider':
            key = b.get('ordinal') or _num_key(b['text'])
            if key:
                marks.append(dict(idx=i, key=str(key), name=b['text'],
                                  divider=True, blk=b))
        else:
            kick = b.get('kicker') or ''
            key = _num_key(kick)
            if key:
                marks.append(dict(idx=i, key=key, name=kick,
                                  divider=False, blk=b))
    return marks


def _marks_from_text(blocks: list[dict], method: str) -> list[dict]:
    """走纯文本：numeric（数字标题）或 depth（层级）。"""
    marks: list[dict] = []
    if method == 'numeric':
        for i, b in enumerate(blocks):
            if b['type'] != 'heading':
                continue
            t = b['text'].strip()
            key = _chapter_key(t, b.get('level', 1), 'numeric')
            if key and _title_ok(t):
                marks.append(dict(idx=i, key=str(key), name=t,
                                  divider=False, blk=b))
        return marks

    # depth：取「最浅且标题数 ≥2」的那一层；都不满足就取最浅的一层。
    heads = [(i, b) for i, b in enumerate(blocks) if b['type'] == 'heading']
    if not heads:
        return []
    by_level: dict[int, list[tuple[int, dict]]] = {}
    for i, b in heads:
        by_level.setdefault(b.get('level', 1), []).append((i, b))
    levels = sorted(by_level)
    picked = levels[0]
    for lv in levels:
        if len(by_level[lv]) >= 2:
            picked = lv
            break
    for i, b in by_level[picked]:
        t = b['text'].strip()
        if _title_ok(t):
            marks.append(dict(idx=i, key=t, name=t, divider=False, blk=b))
    return marks


def _collapse(marks: list[dict]) -> list[dict]:
    """同一章节号只留一条：位置取最先出现的，章节名由**分隔页优先**供给。

    第 4 章的分隔页叫「04 基于 Dify 的开发实战」，而它内部还有
    「04 实战一 · 聊天助手」这类子标签 —— 章节名要的是前者。
    """
    order, by_key = [], {}
    for m in marks:
        k = m['key']
        if k not in by_key:
            by_key[k] = dict(m)
            order.append(k)
            continue
        cur = by_key[k]
        if m['divider'] and not cur['divider']:
            # 分隔页更权威：名字用它的，位置也往前提（它本来就在该章最前）
            cur['name'] = m['name']
            cur['divider'] = True
            if m['idx'] < cur['idx']:
                cur['idx'] = m['idx']
    return [by_key[k] for k in order]


# ══════════════════════════════════════════════════════════════
# ② 骨架
# ══════════════════════════════════════════════════════════════
def _page_lead(blocks: list[dict], start: int, end: int, limit: int = 60) -> str:
    """一页的「首句」——取标题之后的头一段正文，压到 limit 字。"""
    for x in range(start + 1, end):
        b = blocks[x]
        if b['type'] in ('para', 'bullets'):
            t = block_text(b).strip()
            if t:
                return t[:limit] + ('…' if len(t) > limit else '')
    return ''


def _assemble(blocks: list[dict], marks: list[dict], method: str) -> dict:
    """把章节标记展开成「章节 → 页」的两层结构，并算出块区间。"""
    n = len(blocks)
    # front matter 要标**整页**，不能只标那个标题块 —— 封面的装饰文字
    # （大数字、星标数）如果留在正文里，会被当成文档内容喂给模型。
    front_slides = {b.get('slide') for b in blocks
                    if b['type'] == 'heading' and b.get('role') in _FRONT_ROLES}
    front_slides.discard(None)
    front = [i for i, b in enumerate(blocks) if b.get('slide') in front_slides]
    chapters = []
    for k, m in enumerate(marks):
        start = m['idx']
        end = marks[k + 1]['idx'] if k + 1 < len(marks) else n
        head_blk = blocks[start]
        pages = []
        for j in range(start, end):
            b = blocks[j]
            if b['type'] != 'heading':
                continue
            if b.get('role') in _FRONT_ROLES:
                continue
            # 分隔页自身是**章名**，不是这一章的内容页，别把它算成第一页
            if b.get('role') == 'divider':
                continue
            p_end = next((x for x in range(j + 1, end)
                          if blocks[x]['type'] == 'heading'), end)
            pages.append(dict(name=b['text'], start=j, end=p_end,
                              lead=_page_lead(blocks, j, p_end),
                              chars=sum(len(block_text(blocks[x]))
                                        for x in range(j, p_end))))
        chapters.append(dict(
            key=m['key'], name=m['name'] or m['key'],
            subtitle=head_blk.get('subtitle', ''),
            start=start, end=end,
            chars=sum(len(block_text(blocks[x])) for x in range(start, end)),
            pages=pages))
    return dict(chapters=chapters, method=method, front_matter=sorted(front))


def _flat(blocks: list[dict]) -> dict:
    """兜底：整份文档当一章，每个标题当一页。"""
    heads = [i for i, b in enumerate(blocks) if b['type'] == 'heading']
    if not heads:
        return dict(chapters=[], method='flat', front_matter=[])
    pages = []
    for k, j in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(blocks)
        pages.append(dict(name=blocks[j]['text'], start=j, end=end,
                          lead=_page_lead(blocks, j, end),
                          chars=sum(len(block_text(blocks[x]))
                                    for x in range(j, end))))
    doc_title = blocks[0]['text']
    return dict(chapters=[dict(key='0', name=doc_title, subtitle='',
                               start=heads[0], end=len(blocks),
                               chars=sum(len(block_text(b)) for b in blocks),
                               pages=pages)],
                method='flat', front_matter=[])


def _plausible(sk: dict) -> bool:
    """numeric/depth 这两种纯文本方法的护栏。

    pptx 的 divider/kicker 是版面事实，不需要护栏；纯文本推断才需要 ——
    把教程步骤、列表项误当章节是这类方法的典型误报。
    """
    chs = sk['chapters']
    if len(chs) < 2:
        return False
    body = sorted(c['chars'] for c in chs)
    median = body[len(body) // 2]
    return median >= _MIN_CHAPTER_CHARS


def build_skeleton(blocks: list[dict], kind: str = '') -> dict:
    """blocks → 文档骨架。纯确定性，不调用模型。"""
    if not blocks:
        return dict(chapters=[], method='flat', front_matter=[])

    # ① pptx 专用：分隔页 / kicker（最可靠，是版面事实）
    marks = _collapse(_marks_from_roles(blocks))
    if len(marks) >= 2:
        return _assemble(blocks, marks, 'divider')

    # ② 纯文本：显式数字标题
    marks = _collapse(_marks_from_text(blocks, 'numeric'))
    if len(marks) >= 2:
        sk = _assemble(blocks, marks, 'numeric')
        if _plausible(sk):
            return sk

    # ③ 纯文本：标题层级
    marks = _collapse(_marks_from_text(blocks, 'depth'))
    if len(marks) >= 2:
        return _assemble(blocks, marks, 'depth')

    # ④ 都不成立
    return _flat(blocks)


# ══════════════════════════════════════════════════════════════
# ③ 结构感知的压缩
# ══════════════════════════════════════════════════════════════
def skeleton_digest(sk: dict, page_chars: int = 60) -> str:
    """骨架压成喂给模型的紧凑文本：章节树 + 每页首句 + 字数。

    这是「1000 token 的摘要胜过 10000 token 的摘录」—— 结构永远完整，
    被截掉的只是每页的正文细节。

    ⚠️ 章名与副题**分两行**写。早先拼成 `## 01 初识 Dify　—　什么是 Dify · 设计初衷
    · 九大核心理念` 一行，模型就照抄整行当章节名 —— 而那个名字要印在分隔页的
    40pt 大字上（一行只放得下约 14 字），必然被截成 `01 初识 Dify　—　什么是 Di…`。
    分开写是釜底抽薪：**没有整行可以抄**。
    """
    L = []
    for c in sk.get('chapters', []):
        L.append('## %s' % c['name'])
        if c.get('subtitle'):
            L.append('   副标题（不是章节名的一部分）：%s' % c['subtitle'])
        for p in c['pages']:
            first = p.get('lead') or ''
            L.append('   - %s%s（%d 字）'
                     % (p['name'], ('　' + first) if first else '', p['chars']))
        L.append('')
    return '\n'.join(L) or '（未能从文档中识别出章节结构）'


def page_index(sk: dict) -> dict:
    """源页标题 → `(start, end)` 块区间。

    大纲页上的 `anchor` 存的是**源页标题**而不是块下标：块下标会被解析器
    改动（重跑一次 parse 就全错位），而标题是人能看懂、也能手动改的。
    映射每次用的时候从骨架现算，所以永远不会「过期」。
    """
    out = {}
    for c in sk.get('chapters') or []:
        for p in c.get('pages') or []:
            out.setdefault(p['name'], (p['start'], p['end']))
    return out


def section_text(blocks: list[dict], chapter: dict, max_chars: int = 6000,
                 front_matter: list[int] | None = None) -> str:
    """取某一章的原文（按块区间切），并压到 `max_chars` 以内。

    超长时**按页均匀截断**而不是砍头去尾：每页都留开头，章节内部的
    结构不会因为超长而整段消失。
    """
    skip = set(front_matter or [])
    chunks = []
    for p in chapter.get('pages') or []:
        body = [block_text(blocks[x]) for x in range(p['start'], p['end'])
                if x not in skip and blocks[x]['type'] != 'heading']
        chunks.append((p['name'], ' '.join(t for t in body if t)))
    # 均分预算；每页至少留 200 字
    per = max(200, max_chars // max(1, len(chunks)))
    out = []
    for name, body in chunks:
        if len(body) > per:
            body = body[:per] + '…'
        out.append('### %s\n%s' % (name, body))
    txt = '\n'.join(out)
    return txt[:max_chars]
