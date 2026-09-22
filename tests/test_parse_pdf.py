# -*- coding: utf-8 -*-
"""PDF 解析层的回归测试 —— 「上传的 PDF 只剩一个章节」的那条根因链。

实测的坑（`out/uploads/Dify_介绍与实战.pdf`）：每页第一行是**页码**，
早先的 `_parse_pdf` 把它当标题，40 页抽出 41 个 `1`/`2` 这样的标题，
真正的章标题全躺在正文里当段落 —— 骨架于是只剩 1 章，大纲全挂在它下面。

这些用例不联网、不读磁盘：**PDF 在内存里手写**。环境里没有 reportlab / fpdf，
但 pdfium 只要求一份格式合法的 PDF，自己拼对象表最短也最可控（要什么版面
就写什么）。中文要靠 Identity-H + ToUnicode CMap —— 不嵌字体文件也能抽到
正确的 Unicode，只是拿不到字形尺寸（恰好也验证了「量不出字号就别猜」）。

跑法::

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from pptgen import parse, structure     # noqa: E402


# ══════════════════════════════════════════════════════════════
# 夹具：手写 PDF
# ══════════════════════════════════════════════════════════════
def _pdfstr(s: str) -> bytes:
    """PDF 字符串。非 latin-1 的走 UTF-16BE 十六进制串（PDF 规范允许）。"""
    if all(ord(c) < 256 for c in s):
        return b'(' + s.encode('latin-1').replace(b'(', b'\\(').replace(b')', b'\\)') + b')'
    return b'<' + (b'\xfe\xff' + s.encode('utf-16-be')).hex().upper().encode() + b'>'


def _tounicode(chars: list[str]) -> bytes:
    """Identity-H 的 ToUnicode CMap：码位 i+1 → 第 i 个字符。

    pdfium 抽文本走的就是它 —— 所以不嵌字体文件也能拿到正确的中文。
    """
    body = ['/CIDInit /ProcSet findresource begin', '12 dict begin', 'begincmap',
            '/CMapName /UCS def', '/CMapType 2 def',
            '1 begincodespacerange', '<0000> <FFFF>', 'endcodespacerange']
    for i in range(0, len(chars), 100):
        chunk = list(enumerate(chars[i:i + 100], i + 1))
        body.append('%d beginbfchar' % len(chunk))
        for code, ch in chunk:
            body.append('<%04X> <%s>' % (code, ch.encode('utf-16-be').hex().upper()))
        body.append('endbfchar')
    body += ['endcmap', 'CMapName currentdict /CMap defineresource pop', 'end', 'end']
    return '\n'.join(body).encode('latin-1')


def build_pdf(pages: list[list[tuple[str, float]]],
              bookmarks: list[tuple[int, str, int]] = ()) -> bytes:
    """手写一份最小 PDF。

    pages     每一页的 `[(文本, 字号pt), …]`，顺序即从上到下。
    bookmarks `[(层级, 标题, 页码1基), …]` —— 生成一棵真的 `/Outlines` 书签树。

    含中文时自动切到 Type0/Identity-H（字符盒量不出高度），纯 ASCII 用
    Helvetica（量得出，所以字号那条判据要用 ASCII 夹具测）。
    """
    cjk = any(ord(c) > 255 for p in pages for t, _ in p for c in t)
    p_count = len(pages)

    # 对象号先排好：目录/页 → 内容流 → 字体 → 书签
    cat_no, pages_no = 1, 2
    page_no = lambda i: 3 + i                      # noqa: E731
    cont_no = lambda i: 3 + p_count + i            # noqa: E731
    font_no = 3 + 2 * p_count
    nxt = font_no + 1
    cid_no = tou_no = desc_no = None
    if cjk:
        cid_no, tou_no, desc_no = nxt, nxt + 1, nxt + 2
        nxt += 3
    bm_root = nxt if bookmarks else None
    bm_first = (nxt + 1) if bookmarks else None

    kids = ' '.join('%d 0 R' % page_no(i) for i in range(p_count))
    objs = {pages_no: b'<< /Type /Pages /Kids [%s] /Count %d >>'
                      % (kids.encode(), p_count)}
    objs[cat_no] = (b'<< /Type /Catalog /Pages %d 0 R' % pages_no
                    + (b' /Outlines %d 0 R' % bm_root if bookmarks else b'')
                    + b' >>')

    chars = sorted({c for p in pages for t, _ in p for c in t})
    code_of = {c: i + 1 for i, c in enumerate(chars)}
    for i, page in enumerate(pages):
        parts, y = [], 700
        for text, size in page:
            if cjk:
                show = '<%s>' % ''.join('%04X' % code_of[c] for c in text)
            else:
                show = _pdfstr(text).decode('latin-1')
            parts.append('BT /F1 %g Tf 72 %d Td %s Tj ET' % (size, y, show))
            y -= size + 8
        content = '\n'.join(parts).encode('latin-1')
        objs[cont_no(i)] = (b'<< /Length %d >>\nstream\n' % len(content)
                            + content + b'\nendstream')
        objs[page_no(i)] = (
            b'<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R '
            b'/Resources << /Font << /F1 %d 0 R >> >> >>'
            % (pages_no, cont_no(i), font_no))
    if cjk:
        objs[font_no] = (b'<< /Type /Font /Subtype /Type0 /BaseFont /Fixture '
                         b'/Encoding /Identity-H /DescendantFonts [%d 0 R] '
                         b'/ToUnicode %d 0 R >>' % (cid_no, tou_no))
        objs[cid_no] = (b'<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Fixture '
                        b'/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) '
                        b'/Supplement 0 >> /FontDescriptor %d 0 R /DW 1000 >>' % desc_no)
        cm = _tounicode(chars)
        objs[tou_no] = (b'<< /Length %d >>\nstream\n' % len(cm) + cm + b'\nendstream')
        objs[desc_no] = (b'<< /Type /FontDescriptor /FontName /Fixture /Flags 4 '
                         b'/FontBBox [0 0 1000 1000] /ItalicAngle 0 /Ascent 900 '
                         b'/Descent -200 /CapHeight 700 /StemV 80 >>')
    else:
        objs[font_no] = b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'

    if bookmarks:
        # 层级用父子关系表达（pdfium 就是按树读的）：level 0 挂根，更深的挂到
        # 前面最近的那一级上。
        entries, parents = [], {-1: bm_root}
        for i, (lvl, title, page) in enumerate(bookmarks):
            no = bm_first + i
            parent = parents.get(lvl - 1, bm_root)
            parents[lvl] = no
            entries.append((no, parent, title, page))
        kids_of: dict = {}
        for no, parent, _, _ in entries:
            kids_of.setdefault(parent, []).append(no)
        for no, parent, title, page in entries:
            sibs = kids_of[parent]
            at = sibs.index(no)
            kids_here = kids_of.get(no, [])
            body = (b'<< /Title ' + _pdfstr(title) + b' /Parent %d 0 R' % parent
                    + (b' /Prev %d 0 R' % sibs[at - 1] if at else b'')
                    + (b' /Next %d 0 R' % sibs[at + 1] if at + 1 < len(sibs) else b'')
                    + (b' /First %d 0 R /Last %d 0 R /Count %d'
                       % (kids_here[0], kids_here[-1], len(kids_here)) if kids_here else b'')
                    + b' /Dest [%d 0 R /Fit] >>' % page_no(page - 1))
            objs[no] = body
        objs[bm_root] = (b'<< /Type /Outlines /First %d 0 R /Last %d 0 R /Count %d >>'
                         % (kids_of[bm_root][0], kids_of[bm_root][-1], len(entries)))

    total = max(objs) + 1
    out = bytearray(b'%PDF-1.4\n')
    offsets = {}
    for no in range(1, total):
        offsets[no] = len(out)
        out += str(no).encode() + b' 0 obj\n' + objs[no] + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % total
    for no in range(1, total):
        out += ('%010d 00000 n \n' % offsets[no]).encode()
    out += (b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n'
            % (total, xref))
    return bytes(out)


# 仿实测的那份 PDF：**每页第一行是页码**，每页顶部还有一行页眉，
# 真正的章标题埋在正文里。这一页一句是刻意排的：
#   p1 封面（大字号两行标题）  p2 正文页  p3 目录（点线引导）
#   p4 前言                    p5 第一章  p6 第二章
HEADER = 'MEVION 制度文件 IS-0001'
PAGES = [
    [(HEADER, 9), ('1', 9), ('Dify: Product', 22), ('Introduction', 22),
     ('Dify is an open-source platform.', 11)],
    [(HEADER, 9), ('2', 9), ('What can Dify do?', 15), ('It speeds up delivery.', 11)],
    [(HEADER, 9), ('3', 9), ('目录', 20), ('前言 ............. 4', 11),
     ('第一章 总则 ....... 5', 11)],
    [(HEADER, 9), ('4', 9), ('前言', 20), ('还记得 2013 年，我们创立了乐豆信息。', 11)],
    [(HEADER, 9), ('5', 9), ('第一章 总则', 20), ('第一条 目的：', 15),
     ('为规范公司 AI 应用管理。', 11)],
    [(HEADER, 9), ('6', 9), ('第二章 范围', 20), ('第二条 适用范围：', 15),
     ('适用于全体部门。', 11)],
]
BOOKMARKS = [(0, '前言', 4), (0, '第一章 总则', 5), (0, '第二章 范围', 6)]


def parse_pdf(pages=None, bookmarks=BOOKMARKS) -> dict:
    return parse.parse_bytes(build_pdf(pages or PAGES, bookmarks), '夹具.pdf')


def page_units(doc: dict) -> list[str]:
    return [p['name'] for c in doc['structure']['chapters'] for p in c['pages']]


def headings(doc: dict) -> dict:
    return {b['text']: b for b in doc['blocks'] if b['type'] == 'heading'}


# ══════════════════════════════════════════════════════════════
class TestPageNoise(unittest.TestCase):
    """页码与页眉页脚：它们既是噪声，也是早先「章名 = 1」的元凶。"""

    def setUp(self):
        self.doc = parse_pdf()

    def test_title_is_not_a_page_number(self):
        """`doc['title']` 取封面上最大字号的那两行，而不是第一行的页码 `1`。"""
        self.assertEqual(self.doc['title'], 'Dify: Product Introduction')

    def test_page_numbers_become_no_block_at_all(self):
        for b in self.doc['blocks']:
            self.assertNotEqual(structure.block_text(b).strip(), '1')
            self.assertNotIn('1', headings(self.doc))

    def test_running_header_dropped_everywhere(self):
        self.assertNotIn(HEADER, parse.outline_source(self.doc))
        self.assertNotIn(HEADER, headings(self.doc))

    def test_no_page_unit_is_a_bare_number(self):
        """页单元是锚点体系的键：叫 `1`、`2` 的话，大纲会拿页码去锚正文。"""
        for name in page_units(self.doc):
            self.assertTrue(structure.has_word_char(name), '%r 不该当页单元' % name)


class TestBookmarks(unittest.TestCase):
    """文档自带书签时，章节**完全**按书签走。"""

    def setUp(self):
        self.doc = parse_pdf()

    def test_chapters_come_from_bookmarks(self):
        sk = self.doc['structure']
        self.assertEqual(sk['method'], 'outline')
        self.assertEqual([c['name'] for c in sk['chapters']],
                         ['01 前言', '02 总则', '03 范围'])

    def test_chapter_number_is_rewritten(self):
        """源编号与 deck 编号解耦：`第一章 总则` → `02 总则`，不会出现 `02 第一章 总则`。"""
        names = [c['name'] for c in self.doc['structure']['chapters']]
        self.assertNotIn('第一章', ' '.join(names))

    def test_first_chapter_absorbs_leading_content(self):
        """封面之后、第一个书签之前的内容（产品介绍）要归到第 1 章，不能掉在章节外。"""
        first = self.doc['structure']['chapters'][0]
        self.assertIn('What can Dify do?', [p['name'] for p in first['pages']])

    def test_no_content_falls_outside_every_chapter(self):
        """章节区间是连续的，所以「没有内容掉在章节之外」等价于首尾都对上。

        掉出去的内容是真丢：`_content_index` 按页名 → 块区间取内容，
        章节之外的块谁也拿不到，那一页会静默变成「标题对、正文空」。
        """
        sk = self.doc['structure']
        covered = set()
        for c in sk['chapters']:
            covered |= set(range(c['start'], c['end']))
        outside = set(range(len(self.doc['blocks']))) - covered - set(sk['front_matter'])
        self.assertFalse(outside, '这些块掉在所有章节之外：%s' % sorted(outside))
        self.assertEqual(sk['chapters'][-1]['end'], len(self.doc['blocks']))

    def test_bookmarks_kept_on_the_doc(self):
        self.assertEqual([b['title'] for b in self.doc['outline']],
                         ['前言', '第一章 总则', '第二章 范围'])

    def test_toc_page_is_front_matter(self):
        """目录页要整体排除：它的内容是各章标题粘在一起的产物。"""
        self.assertIn('toc', {b.get('role') for b in self.doc['blocks']})
        self.assertTrue(self.doc['structure']['front_matter'])


class TestNoBookmarks(unittest.TestCase):
    """没有书签就退回版面判据：编号（`第一章`/`第X条`）+ 字号。

    ⚠️ 这份夹具是中文的，走 Identity-H + ToUnicode，**量不出字形盒高度**
    （没有字体文件就没有字形度量）—— 字号那条判据在这里天然不生效，只有编号
    能分章。这是设计内的降级：量不出字号就不猜。字号判据由
    `TestFontSizeHeadings`（纯 ASCII、Helvetica，量得出）覆盖。
    """

    def setUp(self):
        self.doc = parse_pdf(bookmarks=())

    def test_numbered_headings_become_chapters(self):
        sk = self.doc['structure']
        self.assertEqual(sk['method'], 'depth')
        self.assertEqual([c['name'] for c in sk['chapters']],
                         ['第一章 总则', '第二章 范围'])

    def test_cover_lines_are_not_chapters(self):
        """封面那两行大字只是字号大，不是章 —— 否则一份 PDF 会多出两个假章。"""
        self.assertEqual(headings(self.doc)['Dify: Product']['role'], 'cover')
        self.assertNotIn('Dify: Product',
                         [c['name'] for c in self.doc['structure']['chapters']])

    def test_numbered_subheadings_are_page_units(self):
        self.assertIn('第一条 目的：', page_units(self.doc))

    def test_without_any_signal_it_degrades_to_one_page_per_pdf_page(self):
        """既没书签也没编号没字号差 → 每页一个页单元（等同改进前的粒度）。"""
        plain = [[('Page %d body text goes here.' % i, 11)] for i in range(1, 5)]
        doc = parse_pdf(pages=plain, bookmarks=())
        self.assertEqual(len(page_units(doc)), 4)


class TestFontSizeHeadings(unittest.TestCase):
    """字号判据要拿量得出字形盒的字体测（中文夹具量不出高度，见上）。"""

    def setUp(self):
        self.doc = parse_pdf(pages=[
            [('Overview of the Platform', 22), ('Body text line one.', 11)],
            [('2 Method Overview', 22), ('Body line.', 11)],
            [('3 Results', 22), ('Body line again.', 11)],
            [('3.1 Background', 15), ('Body line twice.', 11)],
        ], bookmarks=())

    def test_big_line_is_level_one(self):
        self.assertEqual(headings(self.doc)['2 Method Overview']['level'], 1)

    def test_slightly_bigger_line_is_level_two(self):
        """15pt 对 11pt ≈ 1.39×：过大字号那道线（1.30）但不到章节那道线（1.60）。

        同时它带着 `3.1` 的编号 —— 两者取更浅的那个，仍落在第 2 层。
        """
        self.assertEqual(headings(self.doc)['3.1 Background']['level'], 2)

    def test_body_text_is_not_a_heading(self):
        self.assertNotIn('Body text line one.', headings(self.doc))

    def test_first_page_big_line_is_cover(self):
        """封面的大字标题不是章 —— 判断依据是「只靠字号升出来的」。"""
        self.assertEqual(headings(self.doc)['Overview of the Platform']['role'], 'cover')

    def test_numbered_line_on_page_one_is_still_content(self):
        """第 1 页上带编号的行是正文第一页的章标题，不能连正文一起当前置信息排掉。"""
        doc = parse_pdf(pages=[
            [('1 Introduction', 22), ('This article studies X.', 11)],
            [('2 Method', 22), ('We do Y.', 11)],
        ], bookmarks=())
        sk = doc['structure']
        self.assertEqual([c['name'] for c in sk['chapters']], ['1 Introduction', '2 Method'])
        self.assertFalse([b for b in doc['blocks']
                          if b.get('role') in ('cover', 'toc', 'back')])

    def test_size_alone_can_carry_the_chapters(self):
        """整个文档只有字号层级、没有任何编号时，也要能分章。"""
        sk = self.doc['structure']
        self.assertEqual(sk['method'], 'depth')
        self.assertEqual([c['name'] for c in sk['chapters']],
                         ['2 Method Overview', '3 Results'])


class TestPatternLevel(unittest.TestCase):
    """编号模式的判据（纯函数，直接测中文写法）。

    层级数值本身没有意义，有意义的是**相对顺序**：章 < 条/节 < `一、` < `（一）`
    < 单级数字 < 多级数字。
    """

    def test_explicit_chapter_and_section(self):
        for t in ('第一章 总则', '第 2 章 范围', '第三篇 实战',
                  '第二部分 技术架构', '第一卷 起', '第二编 分则'):
            self.assertEqual(parse._pdf_pattern_level(t), parse._LV_CHAP, t)
        for t in ('第一条 目的：', '第 2 节 概述', '第二节 范围'):
            self.assertEqual(parse._pdf_pattern_level(t), parse._LV_SECT, t)

    def test_chinese_enumerations(self):
        self.assertEqual(parse._pdf_pattern_level('一、概述'), parse._LV_CN_NUM)
        self.assertEqual(parse._pdf_pattern_level('十、其他事项'), parse._LV_CN_NUM)
        self.assertEqual(parse._pdf_pattern_level('（一）适用范围'), parse._LV_CN_PAREN)
        self.assertEqual(parse._pdf_pattern_level('(2) 术语'), parse._LV_CN_PAREN)

    def test_multi_level_numbering(self):
        self.assertEqual(parse._pdf_pattern_level('1.1 什么是 Dify'), parse._LV_ENUM + 1)
        self.assertEqual(parse._pdf_pattern_level('1.1.1 概述'), parse._LV_ENUM + 2)
        self.assertEqual(parse._pdf_pattern_level('2.2.4 私有化部署'), parse._LV_ENUM + 2)

    def test_single_level_written_three_ways(self):
        for t in ('1. 定义：', '3、小结', '1 初识 Dify'):
            self.assertEqual(parse._pdf_pattern_level(t), parse._LV_ENUM, t)

    def test_numbering_is_ordered_shallowest_first(self):
        ladder = ['第一章 总则', '第一条 目的：', '一、概述', '（一）范围',
                  '1 初识 Dify', '1.1 什么是 Dify', '1.1.1 概述']
        levels = [parse._pdf_pattern_level(t) for t in ladder]
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(len(set(levels)), len(levels), '每一档都该是独立一层')

    def test_enumeration_needs_to_be_short_and_not_a_sentence(self):
        # 实测的误报：列表项被当成小节标题，制度文件里凭空多出两章
        for t in ('1. 负责基础 IT 架构运维，为 AI 应用提供计算资源、网络支撑。',
                  '4. 严禁转借 AI 应用的账号与权限。',
                  '一、负责基础 IT 架构运维，为 AI 应用提供计算资源、网络支撑。'):
            self.assertIsNone(parse._pdf_pattern_level(t), t)

    def test_homophone_entities_are_not_numbering(self):
        """`2026年度工作报告`、`2026.12` 不是编号 —— 数字后面必须跟分隔符/空白。"""
        for t in ('2026 年 04 月 17 日', '2026年度工作总结', '2025.12 结账'):
            self.assertIsNone(parse._pdf_pattern_level(t), t)

    def test_known_limit_decimal_quantity_looks_like_a_subsection(self):
        """**已知边界**：`3.5 亿元`、`11.11 大促` 与 `1.1 什么是 Dify` 文本上没法区分。

        它们只会落在很深的一层，因此只有在「全文档再没有别的标题」时才可能被当成章 ——
        那时由 `pipeline` 的章节数护栏 + 模型分章兜底收场（见
        `test_structure.TestChapterSegmentation`）。这里把边界写死，免得以后有人
        以为它是被覆盖了的。
        """
        for t in ('3.5 亿元', '11.11 大促'):
            self.assertEqual(parse._pdf_pattern_level(t), parse._LV_ENUM + 1, t)

    def test_plain_text_is_not_a_heading(self):
        for t in ('Dify 是一个多合一的数据处理与分析平台。', '前言', '目录'):
            self.assertIsNone(parse._pdf_pattern_level(t), t)

    def test_size_level_is_relative_to_body(self):
        self.assertEqual(parse._pdf_size_level(20.0, 10.0), 1)
        self.assertEqual(parse._pdf_size_level(14.1, 10.2), 2)
        self.assertIsNone(parse._pdf_size_level(13.1, 13.2))   # 制度文件那种：分不开
        self.assertIsNone(parse._pdf_size_level(15.1, 13.2))
        # 量不出字号（伪造字体 / 扫描件）时不能瞎猜
        self.assertIsNone(parse._pdf_size_level(0.02, 0.0))


class TestDlpGuard(unittest.TestCase):
    """加密头仍然要被拦住（和 pdf 解析同一条入口）。"""

    def test_dlp_bytes_rejected(self):
        with self.assertRaises(ValueError):
            parse.parse_bytes(parse.DLP_MAGIC + b'...', 'x.pdf')


if __name__ == '__main__':
    unittest.main()
