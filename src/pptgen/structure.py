# -*- coding: utf-8 -*-
"""文档骨架：从解析出来的 blocks 里**确定性地**抽出章节结构。

这一层不调用模型，是整个大纲生成的地基。它存在的理由：

- 模型看到的应该是**结构**，而不是一坨被截断的正文。早先喂给大纲模型的是
  「头 60% + 尾 35%」的粗暴截断，长文档中段整段消失，章节结构更是无从谈起。
- 兜底路径也需要结构。没有模型时 `_outline_fallback` 只能靠标题的数字前缀猜
  章节，实测把一份 5 章的文档猜成了 1 章。

做法借鉴了 book-to-skill 的思路：**先确定性抽结构，再让模型在结构上生成**
（"Structure, not a summary"）。章节信号按可靠性从高到低尝试：

    outline  文档自带的目录（PDF 的 `/Outlines` 书签树）
    divider  分隔页（pptx 里那张「巨号序号 + 章节名」的页）
    kicker   每页左上角的章节标签（`01 初识 DIFY`）
    depth    标题层级（docx 的 Heading 1、md 的 `##`、pdf 升出来的标题）
    numeric  显式数字标题（`第3章` / `Chapter 5` / `01 产品介绍`）
    flat     都没有 → 单章

方法认不出来就往下退化，最差退化到 `flat`（等同改进前的行为），不会更糟。

`depth` 排在 `numeric` **之前**是有代价也有理由的：层级是解析层给的**事实**，
而数字前缀是猜的 —— 实测制度文件里的 `1. 定义：`、`2. 公司自主 AI 平台：` 这类
列表项会被数字前缀当成章标题，凭空多出 2 章（而且章均字数还过得去，
`_plausible` 的护栏拦不住）。
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

# 前置信息（不是章节）：目录 / 封面 / 扉页 / 版权页。文档自带的目录里常有它们，
# 拿它们当章会让大纲第一章变成「目录」。
_FRONT_TITLE_RE = re.compile(
    r'^(?:目\s*录|目\s*錄|CONTENTS?|Table of Contents'
    r'|封\s*面|扉\s*页|封\s*底|版\s*权|Cover|Title Page)$', re.I)

# 章名前缀的剥除（`1 初识 Dify` / `第一章 总则` / `Chapter 2 Method`）——
# 书签章名要重编成 `01 …`，不剥掉源编号就成了 `01 第一章 总则`。
_SRC_NUM_RE = re.compile(
    r'^(?:第\s*[\d一二三四五六七八九十百]+\s*[章篇节部]\s*'
    r'|(?:Chapter|Part|Section)\s*\d{1,3}\s*[:.、]?\s*'
    r'|\d{1,3}\s*[、.．]?\s+)', re.I)

# 书签里的**占位名**：Acrobat 会给没命名的书签自动起名（`书签 1` / `書籤 1` /
# `Bookmark 1`）。实测那份 Synology 白皮书的第一条书签就是它 —— 而且它落在的
# 页面上没有任何标题，拿它当第 1 章只会白占一页分隔页加一页模型凭空编的正文。
_PLACEHOLDER_TITLE_RE = re.compile(
    r'^(?:書籤|书签|標籤|标签|Bookmark|Untitled|未命名|无标题)\s*\d*$', re.I)

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


def placeholder_title(t: str) -> bool:
    """这条标题是不是**工具自动起的占位名**（`书签 1` / `Bookmark 3` / `未命名`）。

    只有 PDF 书签树会这么写（人写的标题不会叫「书签 1」），所以这道尺子只用在
    书签那条路上：书签不当章（`_marks_from_outline`），它的标题块也不当页单元
    （`_assemble`，否则被下一章的区间前扩吸收，变成一张标题叫「书签 1」的页）。
    """
    return bool(_PLACEHOLDER_TITLE_RE.match((t or '').strip()))


def front_title(t: str) -> bool:
    """这段文字是不是前置信息（目录 / 封面 / 扉页）的标题。

    文档自带的目录里常有这些条目，它们不是章节 —— 拿它们当章，大纲的第一章
    会变成「目录」，而目录页的内容是各章标题粘在一起的产物，正好是这里最不想要的东西。
    """
    return bool(_FRONT_TITLE_RE.match((t or '').strip()))


def has_word_char(t: str) -> bool:
    """这段文字里有没有实义字符（汉字 / 字母）。

    比 `is_title_like` 松一档，专给**兜底路径**用：`_flat` 是最后一道网，
    把 `前言`、`总则` 这种两字标题滤掉是真丢内容，而把 `1`、`60,000+` 这种
    纯数字留着只是难看 —— 两者取轻，所以这里只挡「一个实义字符都没有的」。
    """
    return bool(_WORDCHAR_RE.search(t or ''))


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
def _norm(s: str) -> str:
    return re.sub(r'\s+', ' ', s or '').strip()


def _marks_from_outline(blocks: list[dict], outline: list[dict]) -> list[dict]:
    """文档自带的目录（PDF 的 `/Outlines` 书签树）→ 章节标记。

    它比任何版面推断都硬：这是文档作者自己划的章节。实测上传的《Dify 介绍与实战》
    带完整三级书签（章 / 节 / 小节），**只取最浅一层**当章节，节与小节留给块流
    里的标题去当页单元（书签标题已在解析层升成 heading 了）。

    章名重编成 `01 …`：源编号与 deck 编号是两回事（源里「前言」不占号、
    正章从 1 起，直接沿用会撞号 —— 分隔页的大号编号靠 `_num_prefix` 取，
    `01` 和 `1` 会同时出现在同一份 deck 上）。
    """
    entries = [e for e in (outline or []) if _norm(e.get('title'))]
    if len(entries) < 2:
        return []
    lvl = min(e.get('level', 0) for e in entries)
    entries = [e for e in entries
               if e.get('level', 0) == lvl
               and not front_title(e['title'])
               and not placeholder_title(e['title'])]
    if len(entries) < 2:
        return []

    # 页号 → 该页第一个块。书签的 dest 偶尔会落到页首之前，那时靠它兜底。
    by_page: dict = {}
    for i, b in enumerate(blocks):
        by_page.setdefault(b.get('slide'), i)

    marks = []
    for k, e in enumerate(entries):
        title = _norm(e['title'])
        idx = None
        for i, b in enumerate(blocks):
            if (b['type'] == 'heading' and b.get('slide') == e.get('page')
                    and _norm(b['text']) == title):
                idx = i
                break
        if idx is None:
            idx = by_page.get(e.get('page'))
        if idx is None:
            continue
        base = _SRC_NUM_RE.sub('', title).strip() or title
        marks.append(dict(idx=idx, key=title, name='%02d %s' % (k + 1, base),
                          divider=False, blk=blocks[idx]))
    return marks


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
    """走纯文本：numeric（数字标题）或 depth（层级）。

    两条路都跳过前置页的标题（封面 / 目录 / 封底）—— `_marks_from_roles` 一直在
    跳，纯文本这两条路原先没跳，于是走 docx / pdf 时封面标题会被当成第 1 章。
    """
    marks: list[dict] = []
    if method == 'numeric':
        for i, b in enumerate(blocks):
            if b['type'] != 'heading' or b.get('role') in _FRONT_ROLES:
                continue
            t = b['text'].strip()
            key = _chapter_key(t, b.get('level', 1), 'numeric')
            if key and _title_ok(t):
                marks.append(dict(idx=i, key=str(key), name=t,
                                  divider=False, blk=b))
        return marks

    # depth：取「最浅且标题数 ≥2」的那一层；都不满足就取最浅的一层。
    heads = [(i, b) for i, b in enumerate(blocks)
             if b['type'] == 'heading' and b.get('role') not in _FRONT_ROLES]
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
def _span_chars(blocks: list[dict], start: int, end: int) -> int:
    """一个块区间里有几个字（**不算占位标题**）。

    占位标题（`书签 1`）不是内容：把它算进字数，一个只剩它的章就「有正文」了，
    于是躲过空章过滤，最后变成一页模型凭空编出来的正文。
    """
    return sum(len(block_text(blocks[x])) for x in range(start, end)
               if not (blocks[x]['type'] == 'heading'
                       and placeholder_title(blocks[x]['text'])))


def _page_lead(blocks: list[dict], start: int, end: int, limit: int = 60) -> str:
    """一页的「首句」——取标题之后的头一段正文，压到 limit 字。"""
    for x in range(start + 1, end):
        b = blocks[x]
        if b['type'] in ('para', 'bullets'):
            t = block_text(b).strip()
            if t:
                return t[:limit] + ('…' if len(t) > limit else '')
    return ''


def _renumber(chapters: list[dict]) -> list[dict]:
    """把 `01 章名` 的序号重排一遍。

    只在书签路径上用：那里的 `01` 是**代码给的 deck 编号**（源编号已被剥掉），
    丢掉一个空章就会留出「`02 Introduction` 打头、却找不到 01」的窟窿。别的路径
    的章名是原文标题（`第 3 章 总则`），那属于源文档的说法，不能动。
    """
    out = []
    for i, c in enumerate(chapters):
        # 只剥「两位以内的数字 + 空格」这种代码自己加的编号，源编号在
        # `_marks_from_outline` 里已经剥过一次了。
        base = re.sub(r'^\d{1,2}\s+', '', c['name']).strip() or c['name']
        c = dict(c)
        c['name'] = '%02d %s' % (i + 1, base)
        out.append(c)
    return out


def _assemble(blocks: list[dict], marks: list[dict], method: str,
              renumber: bool = False) -> dict:
    """把章节标记展开成「章节 → 页」的两层结构，并算出块区间。"""
    n = len(blocks)
    # front matter 要标**整页**，不能只标那个标题块 —— 封面的装饰文字
    # （大数字、星标数）如果留在正文里，会被当成文档内容喂给模型。
    front_slides = {b.get('slide') for b in blocks
                    if b['type'] == 'heading' and b.get('role') in _FRONT_ROLES}
    front_slides.discard(None)
    front = [i for i, b in enumerate(blocks) if b.get('slide') in front_slides]
    # 第 1 章的区间向前扩到**第一个非前置块**：封面之后、第一个章节标记之前
    # 往往就是真内容（实测《Dify》书签的第一个正章是「前言」，而它前面还有
    # 三页产品介绍）。不吸进来的话这段内容掉在所有章节之外 —— 而
    # `_content_index` 是按页名取内容的，章节之外的块谁也拿不到，会静默变成
    # 「标题对、正文空」的页面。
    first = 0
    front_set = set(front)
    for i in range(n):
        if i not in front_set:
            first = i
            break
    chapters = []
    for k, m in enumerate(marks):
        start = first if k == 0 else m['idx']
        end = marks[k + 1]['idx'] if k + 1 < len(marks) else n
        head_blk = blocks[m['idx']]
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
            # 占位标题（`书签 1`）也不当页单元：书签被滤掉之后，它的标题块会
            # 被下一章的区间前扩吸收进来，然后变成一张标题叫「書籤 1」的页。
            if placeholder_title(b['text']):
                continue
            p_end = next((x for x in range(j + 1, end)
                          if blocks[x]['type'] == 'heading'), end)
            pages.append(dict(name=b['text'], start=j, end=p_end,
                              lead=_page_lead(blocks, j, p_end),
                              chars=_span_chars(blocks, j, p_end)))
        chapters.append(dict(
            key=m['key'], name=m['name'] or m['key'],
            subtitle=head_blk.get('subtitle', ''),
            start=start, end=end,
            chars=_span_chars(blocks, start, end),
            pages=pages))
    # **空章要丢掉**：既没有页单元、又没有正文的章，是书签落在了一个没有标题的
    # 页上（实测那份白皮书的第一条书签就是），区间宽度甚至是 0。留着它，大纲会
    # 给它编一页正文、分隔页也照插一页 —— 两页预算换一页模型瞎编的内容。
    chapters = [c for c in chapters if c['pages'] or c['chars']]
    if renumber:
        chapters = _renumber(chapters)
    return dict(chapters=chapters, method=method, front_matter=sorted(front))


def _flat(blocks: list[dict]) -> dict:
    """兜底：整份文档当一章，每个标题当一页。

    标题仍要过一道 `has_word_char`：实测 PDF 里每页的「标题」可能只是页码
    （`1`、`2`），让它们当页名，大纲会拿一串数字去锚内容。但只挡纯数字 ——
    `前言` 这种两字标题在这条路上被滤掉就是真丢内容。
    """
    heads = [i for i, b in enumerate(blocks)
             if b['type'] == 'heading' and has_word_char(b['text'])]
    if not heads:
        return dict(chapters=[], method='flat', front_matter=[])
    pages = []
    for k, j in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(blocks)
        pages.append(dict(name=blocks[j]['text'], start=j, end=end,
                          lead=_page_lead(blocks, j, end),
                          chars=sum(len(block_text(blocks[x]))
                                    for x in range(j, end))))
    doc_title = blocks[heads[0]]['text']
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


def build_skeleton(blocks: list[dict], kind: str = '',
                   outline: list[dict] | None = None) -> dict:
    """blocks → 文档骨架。纯确定性，不调用模型。

    `outline` 是可选的文档自带目录（PDF 的 `/Outlines`），有它就以它为准。
    """
    if not blocks:
        return dict(chapters=[], method='flat', front_matter=[])

    def assembled(marks: list[dict], method: str, renumber: bool = False):
        """标记 → 骨架。空章被丢掉之后不足 2 章的，视同这条路没抽出来。

        判据得看**丢完之后**的章数：书签树可能整份都是占位名加空章，
        标记数够而真章数不够，那就该往下一个方法退化。
        """
        if len(marks) < 2:
            return None
        sk = _assemble(blocks, marks, method, renumber=renumber)
        return sk if len(sk['chapters']) >= 2 else None

    # ① 文档自带的目录（PDF 书签树）—— 最硬：这是文档作者自己划的章节
    sk = assembled(_collapse(_marks_from_outline(blocks, outline or [])),
                   'outline', renumber=True)
    if sk:
        return sk

    # ② pptx 专用：分隔页 / kicker（是版面事实）
    sk = assembled(_collapse(_marks_from_roles(blocks)), 'divider')
    if sk:
        return sk

    # ③ 标题层级（docx 的 Heading、md 的 `#`、pdf 升出来的标题）
    sk = assembled(_collapse(_marks_from_text(blocks, 'depth')), 'depth')
    if sk:
        return sk

    # ④ 纯文本：显式数字标题
    sk = assembled(_collapse(_marks_from_text(blocks, 'numeric')), 'numeric')
    if sk and _plausible(sk):
        return sk

    # ⑤ 都不成立
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


# 骨架摘要里「页名」与「首句」之间用全角空格分隔，行尾挂`（N 字）`
# （见 `skeleton_digest`）。大纲模型被要求「照抄源页标题」，实际抄回来的
# 常常是**整行摘要** —— 这两个正则把那层包装剥掉。
_DIGEST_CHARS_RE = re.compile(r'（\s*\d+\s*字\s*）\s*$')
_DIGEST_SEP = '　'


def _anchor_variants(anchor: str) -> list[str]:
    """一条 anchor 的几种读法，从最严（原样）到最松（只留页名）。"""
    out = [anchor]
    body = _DIGEST_CHARS_RE.sub('', anchor).strip()
    if body and body != anchor:
        out.append(body)
    head = body.split(_DIGEST_SEP)[0].strip()
    if head and head != body:
        out.append(head)
    return out


def _anchor_key(text: str) -> str:
    """词序与空白都无关的比较键：`群晖白皮书 06` 与 `06 群晖白皮书` 同键。

    PDF 的页眉里「序号 + 文档名」的顺序并不稳定，模型抄回来的顺序也就跟着飘。
    """
    return ''.join(sorted((text or '').split()))


def match_page_name(sk: dict, anchor: str) -> str:
    """模型写的 `anchor` → 骨架里**真实存在**的源页名；对不上返回 `''`。

    这道尺子存在的理由：`anchor` 是「这页的内容在源文档的哪一段」的唯一索引，
    对不上就取不到任何源块（`pipeline._content_index`），而**取不到素材是静默的** ——
    实测一份白皮书 13 页的 anchor 全部是整行摘要（`12 群晖白皮书　选择性同步…
    （1380 字）`），于是每一页的素材都是空的：模型那条路拿全文写内容看不出来，
    一旦掉进确定性兜底，产出的就是「一页只有一行标题」的空白页。

    匹配从严到松：原样 → 剥掉`（N 字）` → 再剥掉`　首句` → 词序无关的键。
    """
    names = [p['name'] for c in sk.get('chapters') or []
             for p in c.get('pages') or []]
    a = (anchor or '').strip()
    if not a or not names:
        return ''
    if a in names:
        return a
    variants = _anchor_variants(a)
    stripped = {n.strip(): n for n in names}
    for cand in variants:
        if cand in stripped:
            return stripped[cand]
    keys = {_anchor_key(n): n for n in names}
    for cand in variants:
        hit = keys.get(_anchor_key(cand))
        if hit:
            return hit
    return ''


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
