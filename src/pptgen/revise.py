# -*- coding: utf-8 -*-
"""按页修订：页码映射、补丁寻址与校验、修订落盘。

这个模块是「统一窗口」（用户在一个框里用自然语言说多个页面要改什么）的纯逻辑层，
不碰 HTTP、不碰渲染 —— 于是「哪一页」「这个补丁合法吗」都能在单测里钉死。

## 页码：一个数，四处用

    预览图第 N 张  =  pptx 第 N 页  =  slides[N-3]（0 基）  =  geometry issue['slide']

三页模板页不占 `slides[]` 的额度，但它们在各处都算页码：

    1     = 封面（模板页）        `spec['title']` / `spec['subtitle']`
    2     = 目录（模板页）        `spec['toc']`
    3..n-1 = `slides[0..]`       正文页或章节分隔页
    n     = 封底（模板页）        **没有可改内容**

封面与目录两种模式都支持：微调是用户指名路径（`title` / `toc[2]`），
**整页重做退化成「重出文案」** —— 它们没有版式可换（就是模板的第 1、2 页，
`build.fill_cover` / `fill_agenda` 往占位符里写字），所以重做只换文字、
不换那一页的身份。见 `propose_template_rewrite`。

`build.build()` 里 `sl['page'] = i + 3`（build.py:193-195）与 `repair.PAGE_OFFSET`
（repair.py:30 的 `idx = slide - 1 - 2`）是同一个约定的两处表述。

**用户可见页码 = 预览图序号**，因为界面上每张卡片就是这么标的（web/app.js:560）。
不要把「正文第几页」当成用户说的页码 —— deck 里混着章节分隔页时，两种理解能差 1~5 页。
`build_page_index()` 是这份映射的**唯一出口**，前端只显示不算。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

# 封面 + 目录 之后才是 slides[0]；封底在最后。别再各写一份。
PAGE_OFFSET = 3

# 只写文字、不动格式的模板页（见 build.fill_cover / build.fill_agenda）
KIND_COVER = 'cover'
KIND_TOC = 'toc'
KIND_DIVIDER = 'divider'
KIND_BODY = 'body'
KIND_BACK = 'back'

# 章节分隔页的**版式名**。别与上面的 `KIND_DIVIDER`（那是页面种类的标记）混用 ——
# 两者字面不同（'section_divider' vs 'divider'），混了不会报错，只会静默失配。
DIVIDER_LAYOUT = 'section_divider'

_KIND_LABEL = {
    KIND_COVER: '封面',
    KIND_TOC: '目录',
    KIND_BACK: '封底',
    KIND_DIVIDER: '章节分隔',
}


def texts(o) -> list[str]:
    """递归收集一段 spec 里的所有字符串，保持出现顺序。

    富文本形态有三种写法（见 `layouts._runs` 的容错）：`[("t",{})]` / `[["t",{}]]` /
    `[{"text":"t","hl":true}]`。JSON 里没有 tuple，模型返回的一定是 list。
    这里不关心样式，只按顺序把可读文本捞出来 —— 用于给用户看的 headline、
    以及生成给模型看的字段清单。
    """
    out: list[str] = []
    if isinstance(o, str):
        if o.strip():
            out.append(o)
    elif isinstance(o, dict):
        for k, v in o.items():
            if k in ('hl', 'size', 'bold', 'color'):
                continue          # 样式键不是内容
            out.extend(texts(v))
    elif isinstance(o, (list, tuple)):
        for x in o:
            out.extend(texts(x))
    return out


def headline(sl: dict) -> str:
    """这一页「用户眼里的大字」是什么。

    `statement` 没有标题位（`_normalise_plan:1512-1515` 会 `pop('title')`），
    它的大字就在 `lines` 里。面板与给模型的卡片清单都要显示它，
    否则那一页在界面上是一片空白，用户根本不知道该怎么说。
    """
    title = (sl.get('title') or '').strip()
    if title:
        return title
    for k in ('lines', 'quote', 'body', 'claim', 'lead', 'takeaways'):
        got = texts(sl.get(k))
        if got:
            return got[0]
    return ''


def editable_fields(sl: dict) -> list[str]:
    """这一页顶层可寻址的内容字段（保持 spec 里的书写顺序）。

    剔掉 `layout`（版式由模式决定能不能换，不走字段补丁）与 `page`
    （`build.build` 原地写进去的页码，不是内容）。
    这张清单直接进给模型的卡片说明 —— 「这一页有哪些字段可用」，
    它就不会去改一个不存在的 `title`（statement 页的静默失败就是这么来的）。
    """
    return [k for k in sl
            if k not in ('layout', 'page') and not k.startswith('_')]


def build_page_index(deck: dict) -> list[dict]:
    """deck → 每张预览图的清单（页码映射的唯一出口）。

    返回的每一项（`preview` 是 1 基的预览图序号）：

        {"preview": 3, "kind": "body", "label": "预览 03 · 正文 01",
         "slide_index": 0, "layout": "statement",
         "headline": "…", "fields": ["lines", "body"], "kicker": "01 初识 Dify"}

    模板页（封面/目录/封底）不带 `slide_index`；封面与目录额外给出它们**真正**
    可改的东西（`title`/`subtitle` 与 `toc`），因为那两页的可改字段不在 `slides[]` 里。
    """
    slides = deck.get('slides') or []
    n = len(slides) + PAGE_OFFSET
    out: list[dict] = []

    out.append(dict(
        preview=1, kind=KIND_COVER, label='预览 01 · 封面', slide_index=None,
        fields=['title', 'subtitle'],
        title=deck.get('title') or '',
        subtitle=deck.get('subtitle') or '',
        note='模板页：微调改标题/副标题，重做则重出这两句（版式不变）'))

    out.append(dict(
        preview=2, kind=KIND_TOC, label='预览 02 · 目录', slide_index=None,
        fields=['toc'], toc=list(deck.get('toc') or []),
        note='模板页：微调改目录条目，重做则重排这些条目（版式不变）'))

    body_no = 0
    for i, sl in enumerate(slides):
        preview = i + PAGE_OFFSET
        layout = sl.get('layout') or ''
        is_div = layout == 'section_divider'
        if not is_div:
            body_no += 1
        suffix = _KIND_LABEL[KIND_DIVIDER] if is_div else '正文 %02d' % body_no
        out.append(dict(
            preview=preview,
            kind=KIND_DIVIDER if is_div else KIND_BODY,
            label='预览 %02d · %s' % (preview, suffix),
            slide_index=i,
            layout=layout,
            headline=headline(sl),
            fields=editable_fields(sl),
            kicker=(sl.get('kicker') or '').strip(),
            note=('结构页，只能改章节号/章节名/导语' if is_div else '')))

    out.append(dict(
        preview=n, kind=KIND_BACK, label='预览 %02d · 封底' % n, slide_index=None,
        fields=[], note='品牌收尾页，没有可改的内容'))

    return out


def deck_fingerprint(deck: dict) -> str:
    """deck 的内容指纹 —— 用于「提案之后 deck 有没有被改过」的乐观并发校验。

    不能用文件 mtime/size：`build.build()` 会**原地**往每个 slide 写 `page`
    （build.py:194），每次重建 pptx 都会重存 deck.json、mtime 必变。
    所以做语义指纹：**剔除 `page` 与 `_` 开头的顶层键**，其余按键排序后哈希。
    """
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in sorted(o.items())
                    if k != 'page' and not k.startswith('_')}
        if isinstance(o, list):
            return [clean(x) for x in o]
        return o

    body = clean({'slides': deck.get('slides') or [],
                  'toc': deck.get('toc') or [],
                  'title': deck.get('title') or '',
                  'subtitle': deck.get('subtitle') or ''})
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


# ══════════════════════════════════════════════════════════════
# 模式 A：字段级补丁
#
# 模型返回的是**逐路径的操作**（`{path, expect, value, why}`），不是整页 spec：
#   · 「版式不变」成为**结构上不可能** —— 路径表里根本没有 `layout`
#     （对比 `repair.repair_slide` 是靠事后比对来拒，代价是整页作废）
#   · `expect`（现值回带）能抓住模式 A 最危险的失败：模型把 `items[2]` 按 1 基
#     理解、指着第 2 条 —— 补丁**合法、静默落盘、改错了条目**
#   · diff 天然是逐路径的
# ══════════════════════════════════════════════════════════════

# 只到「元素」这一层，不下钻嵌套列表。半个 run 会破坏富文本；而嵌套列表的
# 条目数在 `pipeline._items` 里是**展开计数**的（层内的模块名各算一条），
# 改动后必须重跑容量校验才发现超了 —— 与其留这个坑，不如直接拒绝。
_SCALAR = (str, int, float, bool)
# 忽略的字段：layout 由模式决定能不能换，page 是 build 原地写的派生值
_SKIP_FIELDS = ('layout', 'page')


def _kind_of(v) -> str:
    if isinstance(v, bool):
        return 'bool'
    if isinstance(v, (int, float)):
        return 'num'
    if isinstance(v, str):
        return 'str'
    return ''


def paths_of(spec: dict) -> dict[str, str]:
    """这一页**可寻址**的路径 → 形状标记。

    形状 ∈ `str` / `num` / `bool` / `obj` / `strlist` / `objlist` / `rows` / `list`。
    判据是**实际数据**，不是手写的字段清单 —— 于是 `_ITEM_FIELDS` 没覆盖的版式
    （`metric_trend`、`definition`、`quote`）自动得到「只有标量可寻址」的结果，
    正是我们想要的保守行为。
    """
    out: dict[str, str] = {}
    for k, v in spec.items():
        if k in _SKIP_FIELDS or k.startswith('_'):
            continue
        kd = _kind_of(v)
        if kd:
            out[k] = kd
            continue
        if isinstance(v, dict):
            out[k] = 'obj'
            for k2, v2 in v.items():
                if _kind_of(v2):
                    out['%s.%s' % (k, k2)] = _kind_of(v2)
            continue
        if not isinstance(v, list):
            continue
        if not v:
            out[k] = 'list'
        elif all(isinstance(x, str) for x in v):
            out[k] = 'strlist'
            for i in range(len(v)):
                out['%s[%d]' % (k, i)] = 'str'
        elif all(isinstance(x, dict) for x in v):
            out[k] = 'objlist'
            for i, x in enumerate(v):
                out['%s[%d]' % (k, i)] = 'obj'
                for k2, v2 in x.items():
                    if _kind_of(v2):
                        out['%s[%d].%s' % (k, i, k2)] = _kind_of(v2)
        else:
            # 富文本行 `[[(片段,{样式}),…],…]` 与表格行 `[[单元格,…],…]` 都是
            # list[list]：只开放整行替换，不开放 `[i][j]`。
            out[k] = 'rows'
            for i in range(len(v)):
                out['%s[%d]' % (k, i)] = 'row'
    return out


def split_path(path: str) -> list:
    """`'items[2].desc'` → `['items', 2, 'desc']`；不合法就抛 ValueError。

    字段名不限定 ASCII：模型有时会写 `items[0].标题` 这种不存在的子字段。
    这里让它**解析得过去**，好让 `_missing_hint` 能回一句「`items[0]` 里没有
    `标题`，它有 name、desc」—— 比笼统的「路径写法不对」有用得多。
    真正拦住它的是路径表查找，不是语法。
    """
    parts: list = []
    for seg in (path or '').split('.'):
        m = re.match(r'^([^\[\].]+)((?:\[\d+\])*)$', seg)
        if not m:
            raise ValueError('路径写法不对：%r' % path)
        parts.append(m.group(1))
        parts.extend(int(i) for i in re.findall(r'\[(\d+)\]', m.group(2)))
    if not parts:
        raise ValueError('空路径')
    return parts


def get_path(spec: dict, parts: list):
    cur = spec
    for p in parts:
        try:
            cur = cur[p]
        except (KeyError, IndexError, TypeError):
            raise KeyError('.'.join(str(x) for x in parts))
    return cur


def set_path(spec: dict, parts: list, value) -> None:
    """就地写。调用方负责先 copy —— 见 `apply_ops`。"""
    cur = spec
    for p in parts[:-1]:
        cur = cur[p]
    cur[parts[-1]] = value


def flat_text(v) -> str:
    """把一个值压成一句可比较的文本（用于 `expect` 的模糊比对）。"""
    if isinstance(v, (list, tuple, dict)):
        return ''.join(texts(v))
    return '' if v is None else str(v)


_PUNCT = re.compile(r'[\s，。、：；！？,.;:!?「」『』（）()\[\]“”"\'\-—…·]+')


def loose(s) -> str:
    """归一化：去空白与标点。模型回带的「现值」很少与原文逐字相同。"""
    return _PUNCT.sub('', flat_text(s))


def _type_ok(kind: str, v) -> bool:
    if kind == 'str':
        return isinstance(v, str)
    if kind == 'num':
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if kind == 'bool':
        return isinstance(v, bool)
    if kind == 'obj':
        return isinstance(v, dict)
    if kind == 'strlist':
        return isinstance(v, list) and all(isinstance(x, str) for x in v)
    if kind == 'objlist':
        return isinstance(v, list) and all(isinstance(x, dict) for x in v)
    if kind == 'rows':
        return isinstance(v, list) and all(isinstance(x, (list, tuple)) for x in v)
    if kind == 'row':
        return isinstance(v, (list, tuple))
    if kind == 'list':
        return isinstance(v, list)
    return False


def check_ops(spec: dict, ops: list[dict], *, path_of=None) -> tuple[str, str]:
    """逐条校验补丁。→ ('ok' | 'reject' | 'warn', 人话原因)。

    `path_of` 可注入（模式 B 换了版式后，旧路径表就不适用了）。
    这一层是**便宜的那一档**：目的不是替代容量校验，而是在花掉模型调用与
    整份构建之前，就把「改一个不存在的字段」「条目序号对不上」这类错误拒掉，
    并且给出一句人能看懂的原因。
    """
    table = path_of if path_of is not None else paths_of(spec)
    msgs: list[str] = []
    level = 'ok'
    for i, op in enumerate(ops):
        path = (op or {}).get('path') or ''
        try:
            parts = split_path(path)
        except ValueError as e:
            return 'reject', str(e)
        if parts[0] in _SKIP_FIELDS:
            return 'reject', ('`%s` 不能这样改：%s'
                              % (parts[0], _FIELD_HINT.get(parts[0], '')))
        if path not in table:
            return 'reject', _missing_hint(spec, parts, table)
        kind = table[path]
        if 'value' not in op:
            return 'reject', '这一条没有给出新值（path=%s）' % path
        val = op['value']
        if not _type_ok(kind, val):
            return 'reject', ('`%s` 原本是%s，不能换成%s'
                              % (path, _KIND_CN.get(kind, kind),
                                 type(val).__name__))
        if kind == 'str' and not str(val).strip():
            return 'reject', ('`%s` 不能改成空 —— 那一格空着会渲染成一片空白'
                              % path)
        # 现值回带：这是整个校验器里性价比最高的一条。模型把 `items[2]` 按 1 基
        # 理解时，补丁本身完全合法、会静默落盘、改错条目 —— 只有比对现值能拦住。
        expect = op.get('expect')
        if expect is None or expect == '':
            # 现值本来就是空的 → 没什么可核对的，别报一句用户看不懂的
            # 「没回带现值」。这是封面/目录重做最常走的一条路：`page_spec` 对
            # 封面总是给出 title 与 subtitle 两个键，而 deck 里通常只有 title
            # （`_normalise_plan` 早先不回吐 subtitle）。
            if not flat_text(get_path(spec, parts)).strip():
                continue
            level = 'warn'
            msgs.append('`%s` 没有回带现值，无法确认改的是不是你说的那一处' % path)
            continue
        cur, want = loose(get_path(spec, parts)), loose(expect)
        if cur != want:
            return 'reject', ('`%s` 的现值是「%s」，不是你说的「%s」—— '
                              '条目序号或页码可能对不上'
                              % (path, flat_text(get_path(spec, parts))[:24],
                                 flat_text(expect)[:24]))
    return level, '；'.join(msgs)


_KIND_CN = {'str': '一段文字', 'num': '一个数字', 'bool': '一个开关',
            'obj': '一组字段', 'strlist': '一列文字', 'objlist': '一组条目',
            'rows': '多行内容', 'row': '一行内容', 'list': '一个列表'}

# 带下标寻址的宿主形状（用来区分「序号越界」与「拆太深」）
_LIST_KINDS = ('strlist', 'objlist', 'rows', 'list')

_FIELD_HINT = {
    'layout': '换版式要用「整页重做」模式',
    'page': '页码是渲染时算出来的，改它没用',
}


def join_path(parts: list) -> str:
    """`['items', 2, 'desc']` → `'items[2].desc'`（`split_path` 的逆）。"""
    s = ''
    for p in parts:
        s += ('[%d]' % p) if isinstance(p, int) else (('.' if s else '') + p)
    return s


def _field_list(table: dict) -> str:
    return '、'.join(k for k in table if '[' not in k and '.' not in k)


def _missing_hint(spec: dict, parts: list, table: dict) -> str:
    """路径不存在时，尽量说清「这一页到底有什么、该怎么提」。

    这是整套设计里**唯一直接面向用户**的文案（校验器写的那些话会原样显示在
    面板上），所以按「序号越界 / 拆太深 / 子字段写错 / 没有这个字段」分开说 ——
    一句笼统的「路径不合法」对用户毫无帮助。
    """
    path = join_path(parts)
    # ① 序号越界：下标的宿主是个已知列表字段，但带上这个下标后不在表里。
    #    必须同时判「不在表里」—— 否则 `lines[0][0]` 会被误报成「lines 只有 1 条」，
    #    而它其实是「拆太深」。
    for i, p in enumerate(parts):
        if not isinstance(p, int):
            continue
        host_parts = parts[:i]
        host = join_path(host_parts)
        if (table.get(host) in _LIST_KINDS
                and join_path(parts[:i + 1]) not in table):
            try:
                n = len(get_path(spec, host_parts))
            except (KeyError, IndexError, TypeError):
                break
            return ('`%s` 只有 %d 条，你说的第 %d 条不存在' % (host, n, p + 1))
    parent = join_path(parts[:-1]) if len(parts) > 1 else ''
    kind = table.get(parent)
    if kind:
        if isinstance(parts[-1], int):
            return ('只能改到 `%s` 这一层 —— 再往下拆（`%s`）会破坏富文本'
                    '或表格的结构' % (parent, path))
        subs = sorted(k.rsplit('.', 1)[-1] for k in table
                      if k.startswith(parent + '.')
                      and k.count('.') == parent.count('.') + 1)
        return ('`%s` 里没有 `%s` 这个子字段。它有：%s'
                % (parent, parts[-1],
                   '、'.join(subs) if subs else '（没有可单独改的子字段）'))
    if parts[0] == 'title' and 'title' not in table:
        return ('这一页（%s）没有标题位 —— 它的大字就是正文本身。'
                '改那句话请说「把这一行大字改成……」。这一页可改的字段：%s'
                % (spec.get('layout'), _field_list(table)))
    return ('这一页没有 `%s` 这个字段。可改的是：%s' % (path, _field_list(table)))


def apply_ops(spec: dict, ops: list[dict]) -> dict:
    """应用补丁，返回**新的** spec（原对象不动）。"""
    out = copy.deepcopy(spec)
    for op in ops:
        set_path(out, split_path(op['path']), op['value'])
    return out


def diff_ops(before: dict, ops: list[dict]) -> list[dict]:
    """给用户看的逐路径 diff（`before` → `after`）。"""
    rows = []
    for op in ops:
        parts = split_path(op['path'])
        try:
            was = flat_text(get_path(before, parts))
        except KeyError:
            was = ''
        rows.append(dict(path=op['path'], before=was,
                         after=flat_text(op['value']), why=op.get('why') or ''))
    return rows


# ══════════════════════════════════════════════════════════════
# 编排：一页的 spec 片段、落盘、真正的容量门
# ══════════════════════════════════════════════════════════════

def page_spec(deck: dict, preview: int) -> dict:
    """这一「预览页」可改字段所在的 spec 片段。

    补丁引擎只认 dict，而三张模板页的可改字段**不在 `slides[]` 里**：
    封面的 `title`/`subtitle` 与目录的 `toc` 都是 deck 顶层的键。
    这里把它们统一成一个 dict 交给同一套引擎 —— 于是「统一窗口」对封面、
    目录、正文用的是同一条代码路径，不需要第二套表单/校验。

    ⚠️ 正文页返回的是 `slides[i]` **本身**（不是副本）：`commit_page` 用整个
    片段**替换**它，不做逐键合并 —— 换版式后旧版式的键必须一起丢掉，否则
    `repair._text_len`（只跳过 `layout`）会把这些残键算进长度、触发莫名其妙的
    压文案。调用方要改就先 `apply_ops`（它会 deepcopy）。
    """
    n = len(deck.get('slides') or []) + PAGE_OFFSET
    if preview == 1:
        return {'title': deck.get('title') or '',
                'subtitle': deck.get('subtitle') or ''}
    if preview == 2:
        return {'toc': list(deck.get('toc') or [])}
    if preview == n:
        return {}                       # 封底：品牌收尾页，没有可改内容
    slides = deck.get('slides') or []
    i = preview - PAGE_OFFSET
    return slides[i] if 0 <= i < len(slides) else {}


def commit_page(deck: dict, preview: int, patch: dict, *, sync_outline: bool = True
                ) -> list[dict]:
    """把改好的 spec 片段写回 deck（**就地**）。→ outline 需要同步的补丁列表。

    为什么目录条目必须同步回大纲：`_normalise_plan` 里
    `toc = [_toc_line(t) for t in outline['toc']]`（pipeline.py:1529）会**无条件**
    重算 toc —— 只改 deck 的话，下次任何一次重新规划都会把它冲掉。封面的
    `title` 同理（它派生自 `outline['title']`，pipeline.py:1535）。
    正文页的字段不需要：收尾只在字段**为空时**回填（pipeline.py:1510-1517）。
    """
    n = len(deck.get('slides') or []) + PAGE_OFFSET
    if preview == 1:
        out = []
        for k in ('title', 'subtitle'):
            if k in patch and patch[k] != (deck.get(k) or ''):
                out.append(dict(field='cover.' + k, before=deck.get(k) or '',
                                after=patch[k]))
                deck[k] = patch[k]
        return out
    if preview == 2:
        toc = patch.get('toc')
        if toc is None:
            return []
        # 必须过 `_toc_line`：目录页**不在几何检查范围**里（geometry 默认
        # skip={1,2,n}），宽度超了没有任何东西会报 —— `build.fill_agenda`
        # 里那段高度估算与告警是唯一防线（pipeline.py:1525-1528 记着同一条）。
        from . import pipeline
        toc = [pipeline._toc_line(t) for t in toc]
        out = []
        if toc != list(deck.get('toc') or []):
            out.append(dict(field='toc', before=list(deck.get('toc') or []), after=toc))
            deck['toc'] = toc
        return out
    i = preview - PAGE_OFFSET
    slides = deck.get('slides') or []
    if 0 <= i < len(slides):
        slides[i] = patch
    return []


# ══════════════════════════════════════════════════════════════
# 模板页的「整页重做」：重出文案，版式仍是模板自己那一页
#
# 封面与目录没有版式可换 —— 它们就是模板的第 1、2 页，`build.fill_cover` /
# `fill_agenda` 往占位符里写字。于是「整页重做」在这两页上退化成**重出文案**：
# 封面给新的 title/subtitle，目录给重排过的 toc 条目。它与微调共用同一条落盘
# 路径（`check_ops` → `apply_ops` → `commit_page`），区别只在**谁出这份补丁** ——
# 微调是用户指名路径，重做是模型看着整份大纲重拟。
#
# 这一段是纯逻辑：`commit_rewrite` 是唯一往 deck 上写的口子（两个入口都走它），
# `template_new` / `check_template_new` 不碰模型，「重做出来的封面能不能用」
# 于是能在单测里钉死。
# ══════════════════════════════════════════════════════════════

# 封面标题的字数上限。**版面事实**，不是随手定的：封面占位符 6.18×2.16 英寸
# （docs/程序运行逻辑.md 的提示词硬约束表）。超了 `build.fill_cover` 会先折行、
# 再往下降字号，54→20 磅全试完还装不下就**静默截断**（只留一行
# `[build] N 处文案超长被截断` 的日志），而封面又在几何检查的 skip 名单里 ——
# 没有任何东西会替它报警。所以提前给模型划一条线。
COVER_TITLE_MAX = 20
# 副标题同理，但模板那个框比标题小得多，且 python-pptx 读不到继承字号，
# 算不出宽度 —— 只能按字数提醒，不做精确计算。
COVER_SUBTITLE_MAX = 20


def template_new(deck: dict, preview: int, raw) -> dict:
    """模型给的重做结果 → 这一页的补丁 dict（只留这一页真有的键）。

    封面只有 `title`/`subtitle`、目录只有 `toc`；模型多写一个 `layout` 就会顺着
    `commit_page` 漏进 deck 顶层，而 `deck_fingerprint`、`_normalise_plan` 与大纲
    都不认它，只会让「到底改了什么」说不清。空字符串当作「这条没给」——
    不留空标题。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k in page_spec(deck, preview):
        if k not in raw:
            continue
        v = raw[k]
        if k == 'toc':
            got = [str(t).strip() for t in v] if isinstance(v, list) else []
            got = [t for t in got if t]
            if got:
                out[k] = got
            continue
        v = v.strip() if isinstance(v, str) else v
        if v:
            out[k] = v
    return out


def check_template_new(deck: dict, preview: int, patch: dict) -> tuple[str, str]:
    """校验模板页重做出来的新文案。→ ('ok' | 'warn' | 'reject', 人话原因)。

    这一页**没有几何检查兜底**：`qa/geometry.py` 默认跳过第 1/2/n 页，而
    `repair.repair_slide` 也救不了它（它要求版式不变**且**文案变短，
    repair.py:83-90 —— 重做出来的文案完全可能更长）。长度与条数这两件事
    只能在这里守，外加 `commit_page` 里那一道 `_toc_line` 宽度夹取。

    拒的都是会让成品说不清的：空补丁、目录条数与原来对不上（一条对应一个章节，
    对不上就会出现对不上的分隔页）。超字数只是提醒 —— 它会折行或缩字号。
    """
    if not patch:
        return 'reject', '这一页没有给出任何新文案'
    msgs: list[str] = []
    if preview == 1:
        if not (patch.get('title') or ''):
            return 'reject', '没有给出新的封面标题'
        if len(patch['title']) > COVER_TITLE_MAX:
            msgs.append('标题 %d 字，超过 %d 字会被折行或截断'
                        % (len(patch['title']), COVER_TITLE_MAX))
        sub = patch.get('subtitle') or ''
        if len(sub) > COVER_SUBTITLE_MAX:
            msgs.append('副标题 %d 字偏长，模板那个框很小，可能压到边'
                        % len(sub))
        return ('warn' if msgs else 'ok'), '；'.join(msgs)

    toc = patch.get('toc') or []
    if not toc:
        return 'reject', '没有给出新的目录条目'
    cur = len(page_spec(deck, 2).get('toc') or [])
    if cur and len(toc) != cur:
        return 'reject', ('目录 %d 条，原来是 %d 条 —— 一条对应一个章节，'
                          '重做只能重写每条的措辞，不能增删条目'
                          % (len(toc), cur))
    # 超宽的条目会被 `_toc_line` 夹掉尾巴，而目录页不做几何检查 —— 夹了就要
    # 说出来：静默变短正是这一整块最不想要的那种失败。
    from . import pipeline
    for i, t in enumerate(toc):
        if pipeline._toc_line(t) != t:
            msgs.append('第 %d 条太长，会被裁成「%s」'
                        % (i + 1, pipeline._toc_line(t)))
    return ('warn' if msgs else 'ok'), '；'.join(msgs)


def template_ops(spec: dict, patch: dict) -> list[dict]:
    """重做出来的文案 → 逐路径 ops（带 `expect` 现值回带）。

    转这一道是为了**面板上能逐条手改**，也是为了让重做与微调在落盘时走同一条
    校验：`diff_ops` 的每一行都要有 path，而整列 `toc` 一条会渲染成一个装不下
    的长串（面板只让 ≤80 字的标量可编辑）。目录因此按 `toc[i]` 逐条出。
    """
    ops: list[dict] = []
    for k, v in patch.items():
        if k == 'toc':
            cur = list(spec.get('toc') or [])
            for i, t in enumerate(v):
                ops.append(dict(path='toc[%d]' % i,
                                expect=cur[i] if i < len(cur) else '',
                                value=t, why=''))
            continue
        ops.append(dict(path=k, expect=spec.get(k) or '', value=v, why=''))
    return ops


def commit_rewrite(deck: dict, preview: int, new, ops: list | None = None) -> dict:
    """把一条「整页重做」的结果写回 deck（**就地**）。

    → `{'outline': [大纲补丁], 'changed': bool, 'reason': '' 或人话}`

    两种页面走两条路：

        预览 1/2   模板页 → 重出文案：补丁写进 deck 顶层，并带回
                   `cover.title` / `cover.subtitle` / `toc` 三条大纲补丁
        正文页     整页替换 `slides[preview - 3]`（换版式后旧版式的键一起丢）

    **这个分派必须在这里收口。** 早先 `server.run_apply` 与 `run.py` 各自写
    `out_deck['slides'][preview - PAGE_OFFSET] = new`，而 `preview=1` 算出的是
    `slides[-2]` —— 负下标在 Python 里合法，于是封面重做会**静默改掉倒数第二张
    正文页**：不抛异常、`changed` 还报着「第 1 页变了」，而 deck 顶层的 `title`
    一个字没动、成品封面根本没变。两份实现迟早会分叉，所以收成一个函数。

    `changed` 为假 = 写了但内容与原来一样（`commit_page` 只在真的变了才回补丁），
    调用方据此决定要不要重渲这一页、算不算进台账。
    """
    if not isinstance(preview, int):
        return dict(outline=[], changed=False, reason='没有说明是第几页')
    if preview in (1, 2):
        spec = page_spec(deck, preview)
        if ops:
            # 面板上可能被手改过新值 —— 和微调一样过一遍校验（`expect` 回带能
            # 抓住「改的是另一条」）。`ops` 来自浏览器，不是可信输入。
            level, why = check_ops(spec, ops)
            if level == 'reject':
                return dict(outline=[], changed=False, reason=why)
            patch = apply_ops(spec, ops)
        else:
            patch = template_new(deck, preview, new)
        if not patch:
            return dict(outline=[], changed=False,
                        reason='这一页没有给出新文案')
        patches = commit_page(deck, preview, patch)
        return dict(outline=patches, changed=bool(patches), reason='')

    slides = deck.get('slides') or []
    i = preview - PAGE_OFFSET
    if not (0 <= i < len(slides)):
        return dict(outline=[], changed=False,
                    reason='第 %d 页不对应任何正文页' % preview)
    if not isinstance(new, dict) or not new.get('layout'):
        return dict(outline=[], changed=False,
                    reason='这一页重做没有给出可用的内容')
    if new['layout'] not in known_layouts():
        return dict(outline=[], changed=False,
                    reason='版式 %r 本版不认识' % new['layout'])
    # 整页替换、不做键合并 —— 换版式后旧版式的键必须一起丢，否则
    # `repair._text_len` 会把它们算进长度、触发莫名其妙的压文案（见 commit_page）。
    slides[i] = new
    return dict(outline=[], changed=True, reason='')


# 几何检查里「这条会让页面难看/装不下」的三类
HARD_ISSUES = ('text_overflow', 'text_overlap', 'past_safe_area')


def check_by_build(deck: dict, build_qa, pages: set[int]) -> dict[int, list[str]]:
    """**真正的容量门**：构建一份 + 几何检查，只看关心的那几页。

    为什么不靠 `pipeline.overflow_reason`：实测内置 19 套里只有 3 套声明了
    `max_item_chars`（单条字数的硬上限），而 `section_divider` / `statement` /
    `definition` / `metric_trend` / `quote` 在 `_ITEM_FIELDS` 里**根本没有规则**
    —— 它挡不住大多数溢出。

    而 build（python-pptx）与几何检查合计约 1–2 秒，贵的是渲染（8–10 秒/页），
    所以这一档完全可以每次都跑。`pages` 用的是**预览页码**：预览页 = pptx 页 =
    几何报告里的 `issue['slide']`，三者同一个数（见模块 docstring）。

    `build_qa(spec)` 由调用方注入 —— 它要用 build 与 geometry 两个模块，
    放进来会形成循环依赖。与 `repair.repair_deck` 同一个约定。
    """
    report = build_qa(deck)
    out: dict[int, list[str]] = {}
    for it in report.get('issues') or []:
        if it.get('kind') not in HARD_ISSUES:
            continue
        try:
            slide = int(it.get('slide') or 0)
        except (TypeError, ValueError):
            continue
        if slide in pages:
            out.setdefault(slide, []).append(
                '%s（%s）' % (it['kind'], it.get('detail') or ''))
    return out


def apply_outline_patch(outline: dict, patches: list[dict]) -> dict:
    """把 `commit_page` 报出来的补丁写回大纲（返回**新的** dict）。

    为什么非做不可：`_normalise_plan` 里
    `toc = [_toc_line(t) for t in outline['toc']]`（pipeline.py:1529）会**无条件**
    重算目录，封面 `title` 也是 `outline.get('title','')` 派生的（pipeline.py:1535）。
    只改 deck 的话，用户下一次点「生成」就会把这些改动**静默冲掉**。
    正文页的字段不用管：收尾只在字段为空时回填（pipeline.py:1510-1517）。
    """
    out = copy.deepcopy(outline)
    for p in patches or []:
        field, after = p.get('field') or '', p.get('after')
        if field == 'cover.title':
            out['title'] = after
        elif field == 'cover.subtitle':
            out['subtitle'] = after
        elif field == 'toc':
            out['toc'] = after
    return out


def apply_revision(deck: dict, items: list[dict], *, log=print) -> tuple[dict, dict]:
    """把确认过的条目应用到 deck 上。→ (新 deck, 报告)。

    每条自带 `ops`（面板上用户可能手改过新值）。一条被拒**不影响其余** ——
    用户写了 5 条，不该因为第 3 条模型看错条目序号就全丢。

    报告：`{'items': [{preview, mode, status, reason, changes}], 'outline': [...]}`
    """
    out = copy.deepcopy(deck)
    report: dict = dict(items=[], outline=[])
    for it in items:
        preview = it.get('preview')
        rec = dict(preview=preview, mode=it.get('mode') or 'patch',
                   request=it.get('request') or '', status='ok',
                   reason='', changes=[])
        if not isinstance(preview, int):
            rec.update(status='reject', reason='没有说明是第几页')
            report['items'].append(rec)
            continue
        n = len(out.get('slides') or []) + PAGE_OFFSET
        if not (1 <= preview <= n):
            rec.update(status='reject',
                       reason='这份 deck 只有 %d 张预览图，没有第 %d 页' % (n, preview))
            report['items'].append(rec)
            continue
        patch = page_spec(out, preview)
        if not patch:
            rec.update(status='reject', reason='封底是品牌收尾页，没有可改的内容')
            report['items'].append(rec)
            continue
        if it.get('new') and not it.get('ops'):
            # 这一条其实是「整页重做」的方案，却被当成微调提交了（用户在方案出来
            # 之后拨了模式开关）。它没有逐字段补丁 → 什么都不会改，而 `changed`
            # 照样把这一页报成已改动：**静默的假成功**，还顺手写进台账。
            rec.update(status='reject',
                       reason='这一条是「整页重做」的方案（只有整页内容、没有逐字段'
                              '补丁）—— 当前是「小范围修改」模式，请重新生成方案')
            report['items'].append(rec)
            continue
        level, why = check_ops(patch, it.get('ops') or [])
        if level == 'reject':
            rec.update(status='reject', reason=why)
            report['items'].append(rec)
            continue
        new_patch = apply_ops(patch, it['ops'])
        rec['changes'] = diff_ops(patch, it['ops'])
        if level == 'warn':
            rec['reason'] = why
        report['outline'].extend(
            commit_page(out, preview, new_patch, sync_outline=True))
        report['items'].append(rec)
    return out, report


# ══════════════════════════════════════════════════════════════
# 模型层：分诊（一段话 → 多条）与出补丁
#
# 分两次调用而不是一次：一次只做「拆条 + 归属页码」，输出极小、失败了也只丢归属；
# 再一次出补丁。合起来做的话，一次输出里既有拆分又有补丁，任何一处 JSON 坏掉
# 就整份重来（`llm.ask_json` 没有流式、也没有取消）。
# ══════════════════════════════════════════════════════════════

SPLIT_SYSTEM = (
    '你在把用户对一份演示文稿的一段修改意见拆成逐条可执行的条目。'
    '只做拆分与归属，不要评价、不要替用户改写意图。'
)

PATCH_SYSTEM = (
    '你是演示文稿的文字编辑。**只改用户点名的那几处**，'
    '其余字段一个字符都不要动，也不要新增字段。'
)


def known_layouts() -> set:
    """本版认得的版式名集合（`check_deck_layouts` 与「用户确认过的整页」共用）。"""
    from . import layout_spec
    return set(layout_spec.names())


def max_rewrite_pages() -> int:
    """一次提交里最多重做几页（`PPTGEN_REVISE_MAX_PAGES`，默认 3）。

    存在的理由很实际：`llm.ask_json` **没有取消接口**，单次调用最坏
    `3×180s + 退避` ≈ 9 分钟（`server.py` 里那条注释记着这个数）。
    一页一次调用，用户写「第 3、5、7、9、11 页都重做」就是 5 次 ——
    最坏 45 分钟，而且中途停不下来。所以给个上限，**并明确告诉用户剩下的没做**
    （截断不吭声比慢更糟）。
    """
    from . import config
    return max(1, config.get_int('PPTGEN_REVISE_MAX_PAGES', 3))


def check_deck_layouts(deck: dict) -> str:
    """这份 deck 里有没有**本版已经不认**的版式。→ 空串 = 没问题。

    实测 4 份历史产物里躺着已删除的 `node_flow`（第 16 页）。`render_slide` 按
    `spec['layout']` 查表分派（layouts.py:1254-1257），未知版式直接 `KeyError`。
    而 `_normalise_plan` 会把它**悄悄换成 statement**（pipeline.py:1502-1507）——
    那会静默改掉用户正在看的那一页，比拒绝更糟。

    所以修订入口先做这道校验，不通过就整份拒绝：让用户重新生成，
    而不是让他在一张「看起来还是原来那样、其实已经换了版式」的页上提意见。
    """
    from . import layout_spec
    known = set(layout_spec.names())
    bad = [(i + PAGE_OFFSET, sl.get('layout'))
           for i, sl in enumerate(deck.get('slides') or [])
           if sl.get('layout') not in known]
    if not bad:
        return ''
    return ('这份 deck 用了本版已不支持的版式（%s）—— 它是旧版式集产出的，'
            '请重新生成一份再改。' % '、'.join(
                '第 %d 页 %s' % (p, n) for p, n in bad[:4]))


def page_table(index: list[dict]) -> str:
    """给模型看的页码表 —— 不让它自己数页码。

    用户说的「第 5 页」是**预览图序号**，而 `slides[]` 的下标差 3，中间还混着
    章节分隔页。这张表把「预览号 / 是什么 / 版式 / 大字 / 可改字段」摊开，
    并要求它**只回 `preview`**，换算交给服务端。
    """
    L = ['预览页清单（`preview` 就是用户在界面上看到的页码，**只能回这个数**）：']
    for e in index:
        bits = ['%d' % e['preview'], e['label'].split('·')[-1].strip()]
        if e.get('layout'):
            bits.append(e['layout'])
        if e.get('headline'):
            bits.append('大字「%s」' % e['headline'][:26])
        if e.get('fields'):
            bits.append('可改字段：%s' % '、'.join(e['fields']))
        if e.get('note'):
            bits.append(e['note'])
        L.append('  ' + ' ｜ '.join(bits))
    return '\n'.join(L)


def split_requests(text: str, index: list[dict], cfg, log=print) -> dict:
    """一段自由文本 → 逐条 {preview, quote, request}。

    → `{'items': [...], 'unclear': [原因...]}`。判断不出页码、或页码不在表里的
    条目进 `unclear` 并附原因 —— **不猜、不夹取**：改错页比不改更糟。
    """
    from . import llm
    prompt = f"""用户对一份已生成的演示文稿提了下面这段修改意见，请拆成逐条。

{page_table(index)}

用户原话：
<意见>
{text}
</意见>

要求：
- 一条意见一个条目。`preview` **只能是上表第一列里出现过的数字**。
- `quote` 摘录用户原话里对应的那一小段（原样抄，便于用户核对归属）。
- `request` 写成一句可执行的要求；可以补全指代，但不要改变意图、不要自行加要求。
- 判断不出属于哪一页、或那一页根本不支持修改（比如封底）时，放进 `unclear` 并写原因。

只输出 JSON：
{{"items": [{{"preview": 5, "quote": "…", "request": "…"}}], "unclear": ["…"]}}
"""
    data = llm.ask_json(prompt, cfg, system=SPLIT_SYSTEM)
    known = {e['preview']: e for e in index}
    items, unclear = [], list(data.get('unclear') or [])
    for it in (data.get('items') or []):
        if not isinstance(it, dict):
            continue
        try:
            preview = int(it.get('preview'))
        except (TypeError, ValueError):
            unclear.append('有一条看不清是第几页：%s' % (it.get('quote') or '')[:40])
            continue
        if preview not in known:
            unclear.append('第 %d 页不在预览清单里（一共 %d 页）：%s'
                           % (preview, len(index), (it.get('quote') or '')[:40]))
            continue
        items.append(dict(preview=preview, quote=(it.get('quote') or '').strip(),
                          request=(it.get('request') or '').strip(),
                          kind=known[preview]['kind'],
                          layout=known[preview].get('layout') or ''))
    log('[revise] 分诊：%d 条可用，%d 条待澄清' % (len(items), len(unclear)))
    return dict(items=items, unclear=unclear)


def propose_patches(deck: dict, items: list[dict], cfg, log=print) -> list[dict]:
    """模式 A：给每条意见出一组 ops（一次调用，各页互不相干、输出小）。"""
    from . import layout_spec, llm
    if not items:
        return []
    blocks = []
    for it in items:
        spec = page_spec(deck, it['preview'])
        name = spec.get('layout') or ('cover' if it['preview'] == 1 else 'toc')
        catalog = layout_spec.catalog_for(name) or '（模板页：只有 title / subtitle / toc 这类顶层字段）'
        blocks.append(f"""### 预览第 {it['preview']} 页（{it.get('kind') or ''}）
用户要求：{it['request']}
版式契约：
{catalog}
该页当前的完整内容：
{json.dumps(spec, ensure_ascii=False, indent=1)}
**这一页可以改的路径**（只能用这些，不要自创）：
{'、'.join(sorted(paths_of(spec))) or '（没有可改字段）'}""")
    prompt = f"""你要对一份已生成的演示文稿做**小范围文字修改**（版式保持不变）。

{chr(10).join(blocks)}

要求：
- 每条意见给一组 `ops`。每个 op 三个字段：
  · `path`   —— 只能取上面「可以改的路径」里列出的；
  · `expect` —— **该路径当前的值**，原样照抄（用来确认你改的正是用户说的那一处。条目序号写错时它会拦住你）；
  · `value`  —— 新值。
- 富文本行（`lines[0]` 这类）的 value 必须是 `[[["文字", {{}}]]]` 形状；要强调就用 `{{"hl": true}}`，**不要**手写颜色。
- 只改用户点名的地方。别的字段一个字都不要动。
- 严格遵守版式契约里的字数与条数上限。
- 做不到的（比如用户要换版式、要改图表数据）不要硬凑，在该条的 `why` 里写明「做不到：…」。

只输出 JSON：
{{"patches": [{{"preview": 5, "ops": [{{"path": "title", "expect": "…", "value": "…", "why": "…"}}]}}]}}
"""
    data = llm.ask_json(prompt, cfg, system=PATCH_SYSTEM)
    by_preview: dict[int, list] = {}
    for p in (data.get('patches') or []):
        if isinstance(p, dict):
            try:
                by_preview[int(p.get('preview'))] = p.get('ops') or []
            except (TypeError, ValueError):
                continue
    out = []
    for it in items:
        spec = page_spec(deck, it['preview'])
        ops = by_preview.get(it['preview']) or []
        if not ops:
            out.append(dict(it, ops=[], status='reject',
                            reason='模型没能给出可执行的修改（可能这条要求'
                                   '用字段改不了，试试「整页重做」）'))
            continue
        level, why = check_ops(spec, ops)
        out.append(dict(it, ops=ops, status=level, reason=why,
                        changes=diff_ops(spec, ops) if level != 'reject' else []))
    return out


# ── 模板页的整页重做（模型层，见上面那段纯逻辑的说明）────────────

TEMPLATE_REWRITE_SYSTEM = (
    '你在重写一份演示文稿的封面或目录页上的文字。只重写这一页，'
    '不新增章节、不改动正文、不做解释。'
)


def rewrite_template_prompt(deck: dict, preview: int, outline: dict,
                           request: str) -> str:
    """封面/目录重做的提示词。

    封面的标题与目录的条目都是**整份 deck 的概括**：只给这一页的现状，模型
    无从知道该概括什么 —— 所以把章节名、各章 summary 与页标题都摊开，
    并把条数与字数上限写死（这两条回来会被 `check_template_new` 校验）。
    """
    from . import pipeline
    outline = outline or {}
    secs = outline.get('sections') or []
    lines = ['这一页现在的内容：',
             json.dumps(page_spec(deck, preview), ensure_ascii=False, indent=1),
             '',
             '整份演示文稿（重写的依据）：',
             '  标题：%s' % (outline.get('title') or '（没有）'),
             '  共 %d 个章节：' % len(secs)]
    for i, s in enumerate(secs, 1):
        lines.append('    %d. %s —— %s'
                     % (i, s.get('name') or '', (s.get('summary') or '')[:40]))
        titles = [t for t in (p.get('title') or ''
                              for p in (s.get('pages') or [])) if t]
        if titles:
            lines.append('       页：%s' % '、'.join(titles)[:120])
    if not secs:
        lines.append('    （拿不到大纲 —— 只能依据这一页现有的文字重写）')

    if preview == 1:
        what = '封面（第 1 页）'
        want = ('只输出两句话，JSON：{"title": "…", "subtitle": "…"}\n'
                '- `title` 是整份文稿的标题，不超过 %d 字'
                '（版面事实：超了会被折行、再超就截断）；\n'
                '- `subtitle` 一句补充（范围、对象、时间都行），不超过 %d 字；\n'
                '- 两句都不要写「封面」「标题」这类标签，也不要换行符。'
                % (COVER_TITLE_MAX, COVER_SUBTITLE_MAX))
    else:
        what = '目录（第 2 页）'
        want = ('只输出目录条目，JSON：'
                '{"toc": ["01  章节名 —— 一句话概括", "…"]}\n'
                '- **正好 %d 条**，一条对应一个章节，顺序照上面的章节顺序；\n'
                '- 章节名照抄（含编号），概括压到 15 字以内，整条不超过 %d 字；\n'
                '- 分隔符用「 —— 」，不要项目符号、不要多余编号。'
                % (len(page_spec(deck, 2).get('toc') or []), pipeline._TOC_MAX))

    return f"""重写这份演示文稿的{what}上的文字。

{chr(10).join(lines)}

用户的要求：
{request}

要求：
- 重写后的文字要自足：只看这一页就知道整份文稿讲什么。
- 只重写这一页，不要提「第几页」，不要解释改动，不要动别的页。
- 用户只点名改其中一部分时，**没点名的原样照抄** —— 不要顺手润色、不要换措辞
  （改一句就动整页，用户在面板上要逐条核对，多出来的改动全是噪声）。

{want}
"""


def propose_template_rewrite(deck: dict, preview: int, outline: dict, request: str,
                             cfg, log=print) -> dict:
    """模板页的整页重做：重出这一页的文案。→ 条目字段（`new`/`ops`/`changes`/…）。

    为什么不复用 `redo_slide`：那条路是**版式驱动**的 —— `pipeline._plan_batch`
    按 `slides[]` 与 `outline.pages[]` 一一对应的下标取内容，而封面/目录根本不在
    `slides[]` 里，`redo_slide` 第一句 `i = preview - PAGE_OFFSET` 就是负数、
    被它自己的越界检查挡回来。所以换一个产出：模型只回这一页的顶层字段，
    落盘仍走 `commit_page` —— 与微调同一条路，于是校验、diff、面板渲染都不用
    另写一套。

    返回的正是**条目字段**，调用方 `dict(it, **got)` 拼进方案。面板上模板页的
    重做因此与微调长得一样：逐条可勾选、新值可手改（`template_ops` 已经把
    目录拆成 `toc[i]` 逐条）。
    """
    from . import llm
    log('[revise] 第 %d 页整页重做（%s）…'
        % (preview, '封面' if preview == 1 else '目录'))
    try:
        raw = llm.ask_json(rewrite_template_prompt(deck, preview, outline, request),
                           cfg, system=TEMPLATE_REWRITE_SYSTEM)
    except llm.LLMError as e:
        return dict(status='reject', new=None, ops=[], changes=[],
                    reason='模型调用失败：%s' % str(e)[:120])
    patch = template_new(deck, preview, raw)
    level, why = check_template_new(deck, preview, patch)
    if level == 'reject':
        return dict(status='reject', new=None, ops=[], changes=[],
                    reason='重做没能给出可用的文案：%s' % why)
    spec = page_spec(deck, preview)
    ops = template_ops(spec, patch)
    # `tpl_kind` 是给面板看的标记：模板页重做的 `new` 是个**没有 `layout` 的 dict**，
    # 面板光看 `new` 分不清它是「封面文案」还是「正文页但版式丢了」，会渲染成
    # 「版式 ? → ?」。显式标出来，界面就能说人话。
    return dict(status=level, new=patch, ops=ops, tpl_kind=KIND_COVER if preview == 1
                else KIND_TOC, changes=diff_ops(spec, ops), reason=why)


def _layout_ok(sl: dict, cands: list[str], banned: set[str]) -> str:
    """模式 B 的版式护栏。→ 空串 = 合格。"""
    from . import layout_spec
    name = sl.get('layout')
    if name not in set(layout_spec.names()):
        return '版式 %r 不存在' % name
    if name not in cands:
        return '版式 %r 不在这一页的候选里（候选：%s）' % (name, '、'.join(cands))
    if name in banned:
        return '版式 %r 与相邻页重复' % name
    return ''


def redo_slide(deck: dict, preview: int, outline: dict, doc: dict, request: str,
               cfg, log=print) -> tuple[dict | None, str]:
    """模式 B：整页重做（允许换版式）。→ (新 spec 或 None, 原因/说明)。

    走 `pipeline._plan_batch`，**不复用 `repair.repair_slide`** —— 它那三道护栏
    全部指向「压缩」：`layout` 不许改（repair.py:83-86，而换版式正是 B 的核心）、
    `_text_len` 必须变短（repair.py:87-90，而 B 可能从 statement 换成 data_table
    变长）。从 `repair_slide` 借的是**风格约定**：失败原样返回、把「跳过原因」
    写进日志（那种可听见的失败）。

    相邻页不得同版式的护栏在 `_plan_by_llm` 里跑在**全部批次回来之后**
    （pipeline.py:1268-1282），直接调 `_plan_batch` 会绕过它 —— 所以这里自己补。
    """
    from . import layout_spec, llm, pipeline
    slides = deck.get('slides') or []
    i = preview - PAGE_OFFSET
    if not (0 <= i < len(slides)):
        return None, '第 %d 页不对应任何正文页' % preview
    page = _outline_page(outline, i)
    section = _outline_section(outline, i)
    content_of = pipeline._content_index(doc)
    blocks = content_of(page)
    role = pipeline._page_roles([page])[0]
    intent = page.get('intent') or pipeline.guess_intent(page, blocks)
    # **不锁意图**：用户的重做请求可能改变这一页的性质（「换成能对比的形式」），
    # 而锁在原文意图的候选里时模型根本选不到 comparison_rows。见
    # layout_spec.candidates_any_intent 的注释。
    cands = [c for c in layout_spec.candidates_any_intent(
        role, layout_spec.shape_of(blocks), first_intent=intent)
        if c != DIVIDER_LAYOUT]
    # 相邻页的版式必须避开：这一步**在发出去之前**做，比回来再改省一次调用
    banned = set()
    for j in (i - 1, i + 1):
        if 0 <= j < len(slides):
            banned.add(slides[j].get('layout'))
    cands = [c for c in cands if c not in banned] or cands
    if not cands:
        return None, '这一页按内容形态没有可用的版式'

    src = '\n'.join((b.get('text') or '') for b in blocks).strip() \
        or pipeline._source_text(doc)
    used: dict[str, int] = {}
    for sl in slides:
        used[sl.get('layout')] = used.get(sl.get('layout'), 0) + 1
    payload = dict(section=section, title=page.get('title') or '',
                   hint=page.get('hint') or '', intent=intent,
                   source=page.get('source') or '', anchor=page.get('anchor') or '',
                   candidates=cands)
    notes = (
        '这一页要**整页重做**，按用户的要求重新组织内容与版式。\n'
        '用户的要求：%s\n'
        '这一页现在长这样（供参考，不必保留）：%s\n'
        '约束：只能产出一页，不要拆成两页，也不要新增页面；'
        '版式必须从 candidates 里选，且不要用 %s（相邻页已在用）。'
        % (request, json.dumps(slides[i], ensure_ascii=False)[:900],
           '、'.join(sorted(banned)) or '（无）'))

    last_reason = ''
    for attempt in (1, 2):
        try:
            got = pipeline._plan_batch(
                [payload], src, cfg, used, 1,
                notes=notes if attempt == 1 else
                notes + '\n**上一次不合规，原因**：' + last_reason)
        except llm.LLMError as e:
            return None, '模型调用失败：%s' % str(e)[:120]
        sl = got[0] if got and isinstance(got[0], dict) else None
        if sl is None:
            last_reason = '没有返回对象'
            continue
        last_reason = _layout_ok(sl, cands, banned)
        if not last_reason:
            last_reason = pipeline.overflow_reason(sl) or ''
        if not last_reason:
            # 页眉三件套与规划路径共用同一份规则（`pipeline._fill_header`），
            # 免得「statement 要弹掉 title」这类规则在这里被抄漏
            return pipeline._fill_header(sl, page, section), ''
        log('[revise]   第 %d 页第 %d 次不合规：%s' % (preview, attempt, last_reason))

    # 两次都不合规 → 确定性生成。字段必然对得上，但内容只能来自原文条目 ——
    # 这是**可听见的降级**，不静默：原因原样带回给用户。
    log('[revise]   第 %d 页退回确定性生成' % preview)
    sl = pipeline._heuristic_slide(page, section, blocks, candidates=cands,
                                   taken=used,
                                   summary=_outline_summary(outline, i))
    return pipeline._fill_header(sl, page, section), \
        '模型两次都没给出合规的重做，已按原文条目重新排版（内容可能不如预期）'


def _outline_page(outline: dict, i: int) -> dict:
    """`slides[i]` 对应大纲里的哪一页。

    两者是**一一对应**的：大纲的 `pages[]` 里本身就含 `divider: true` 的章节
    分隔页，`_plan_by_llm` 对每个下标都产出一页（分隔页走 `_divider_slide`），
    所以下标直接对齐，不需要跳过什么。
    """
    flat = [p for s in (outline.get('sections') or []) for p in (s.get('pages') or [])]
    return flat[i] if i < len(flat) else {}


def _outline_section(outline: dict, i: int) -> str:
    names = [s.get('name') or '' for s in (outline.get('sections') or [])
             for _ in (s.get('pages') or [])]
    return names[i] if i < len(names) else ''


def _outline_summary(outline: dict, i: int) -> str:
    """`slides[i]` 所属章节的 summary —— 与 `_outline_section` 同一套下标对齐。

    兜底页（`_heuristic_slide` 取不到素材时）只有这句能当正文，所以这条
    降级路也要带上它，不能只有规划路带（`pipeline._plan_by_llm`）。
    """
    got = [s.get('summary') or '' for s in (outline.get('sections') or [])
           for _ in (s.get('pages') or [])]
    return got[i] if i < len(got) else ''

