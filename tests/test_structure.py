# -*- coding: utf-8 -*-
"""结构抽取 / 大纲组装的回归测试。

这些是**纯确定性**逻辑（不联网、不调用模型），也正是整条链路里最容易
悄悄坏掉的一层：结构抽错了不会有任何报错，只会让大纲变差、让生成的 PPT
每页变成空壳。所以用内存里合成的夹具把它们钉住。

跑法::

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from pptgen import parse, pipeline, structure          # noqa: E402


# ══════════════════════════════════════════════════════════════
# 夹具：在内存里合成，不提交二进制文件
# ══════════════════════════════════════════════════════════════
def _tb(slide, text, size_pt, top_in, left_in=0.5, width_in=11.0):
    from pptx.util import Inches, Pt
    box = slide.shapes.add_textbox(Inches(left_in), Inches(top_in),
                                   Inches(width_in), Inches(0.9))
    run = box.text_frame.paragraphs[0].add_run()
    run.text = text
    run.font.size = Pt(size_pt)
    return box


def make_pptx_bytes() -> bytes:
    """仿「Dify 介绍与实战」的版面：封面 / 目录 / 分隔页 / 正文页 / 封底。

    关键特征一个不少：分隔页有 120pt 巨号序号，正文页左上角有章节 kicker，
    正文页里有一处**比真标题字号还大**的数字噪声（30pt 的 `60,000+`）。
    """
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]

    # 封面
    s = prs.slides.add_slide(blank)
    _tb(s, 'DIFY · 产品介绍与实战', 13, 1.4)
    _tb(s, '演示夹具标题', 52, 1.8)
    _tb(s, '50,186', 28, 4.8)

    # 目录页
    s = prs.slides.add_slide(blank)
    _tb(s, 'CONTENTS', 18, 0.4)
    _tb(s, '目录', 29, 0.7)
    _tb(s, '第一章内容与第二章内容', 18, 2.2)

    # 两章：分隔页 + 正文页
    for num, name in (('01', '第一章'), ('02', '第二章')):
        s = prs.slides.add_slide(blank)
        _tb(s, num, 120, 1.9)
        _tb(s, 'SECTION %s' % num, 13, 2.5)
        _tb(s, name, 40, 2.9)
        _tb(s, '%s 的要点说明' % name, 15, 3.9)
        for j in range(2):
            s = prs.slides.add_slide(blank)
            _tb(s, '%s %s' % (num, name), 12, 0.4)          # kicker
            _tb(s, '%s 第 %d 节的内容主张' % (name, j + 1), 29, 0.7)
            if j == 0:
                # 比标题字号更大的数字噪声：标题启发式必须不选它
                _tb(s, '60,000+', 30, 3.0)
            _tb(s, '这一节的正文说明文字，用来撑出一点内容长度。' * 3, 15, 3.6)

    # 封底
    s = prs.slides.add_slide(blank)
    _tb(s, '让我们一起探索', 40, 2.9)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def make_docx_bytes() -> bytes:
    import docx
    d = docx.Document()
    d.add_heading('第一章 产品概述', level=1)
    d.add_paragraph('这是一段正文，用来说明产品的定位与目标用户。' * 3)
    d.add_heading('核心能力', level=2)
    d.add_paragraph('支持多种文档格式导入。')
    d.add_heading('第二章 技术架构', level=1)
    d.add_paragraph('解析层把文档拆成统一的内容块。' * 3)
    d.add_heading('模块划分', level=2)
    d.add_paragraph('解析、大纲、规划、渲染四段。')
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def pptx_doc() -> dict:
    return parse.parse_bytes(make_pptx_bytes(), '夹具.pptx')


def _slide_text(spec: dict, skip=('layout', 'title', 'kicker', 'source', 'page')) -> str:
    """把一个 slide spec 里所有渲染出来的文字拼起来（用于断言「取到内容了」）。"""
    out = []
    for k, v in spec.items():
        if k in skip:
            continue
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            out.extend(_slide_text({'x': x}, skip) if isinstance(x, dict) else str(x)
                       for x in v)
    return ' '.join(out)


# ══════════════════════════════════════════════════════════════
class TestTitleHeuristic(unittest.TestCase):
    """「什么算标题」的判据。大字号 ≠ 标题。"""

    def test_rejects_number_noise(self):
        for t in ('60,000+', '01', 'IV', '3 个', '1', '50,186', '2024-10-31'):
            self.assertFalse(structure.is_title_like(t), '%r 不该算标题' % t)

    def test_accepts_real_titles(self):
        for t in ('什么是 Dify', '九大核心理念', '工作流总览：8 个节点跑通内容生产线'):
            self.assertTrue(structure.is_title_like(t), '%r 应该算标题' % t)

    def test_pptx_does_not_pick_bigger_number_as_title(self):
        """30pt 的 `60,000+` 不能压过 29pt 的真标题（实测踩过这个坑）。"""
        doc = pptx_doc()
        heads = [b['text'] for b in doc['blocks'] if b['type'] == 'heading']
        self.assertIn('第一章 第 1 节的内容主张', heads)
        self.assertNotIn('60,000+', heads)


class TestPptxStructure(unittest.TestCase):

    def setUp(self):
        self.doc = pptx_doc()
        self.sk = self.doc['structure']

    def test_chapters_from_dividers(self):
        self.assertEqual(self.sk['method'], 'divider')
        self.assertEqual([c['name'] for c in self.sk['chapters']],
                         ['01 第一章', '02 第二章'])
        self.assertEqual([len(c['pages']) for c in self.sk['chapters']], [2, 2])

    def test_front_matter_covers_whole_slides(self):
        """front matter 要标整页：封面的装饰文字不能漏进正文。"""
        blocks = self.doc['blocks']
        front_text = ' '.join(structure.block_text(blocks[i])
                              for i in self.sk['front_matter'])
        self.assertIn('50,186', front_text)          # 封面的大数字被排除了
        self.assertNotIn('50,186', parse.outline_source(self.doc))

    def test_divider_is_not_a_content_page(self):
        """分隔页是章名，不该被算成那一章的第一页。"""
        names = [p['name'] for c in self.sk['chapters'] for p in c['pages']]
        self.assertNotIn('01 第一章', names)
        self.assertNotIn('02 第二章', names)

    def test_chapter_without_divider_still_detected(self):
        """章节号集合 = 分隔页 ∪ kicker。

        实测源 deck 的第 5 章没有分隔页、只有 kicker —— 只认分隔页会少一章。
        """
        doc = self.doc
        # 造一个只靠 kicker 出现的第 3 章
        import copy
        blocks = copy.deepcopy(doc['blocks'])
        blocks.append(dict(type='heading', level=2, text='收尾', slide=99,
                           role='body', kicker='03 总结'))
        blocks.append(dict(type='para', text='落地要点与下一步。', slide=99))
        sk = structure.build_skeleton(blocks)
        self.assertEqual(len(sk['chapters']), 3)
        self.assertEqual(sk['chapters'][2]['name'], '03 总结')


class TestDocxStructure(unittest.TestCase):

    def test_depth_fallback(self):
        doc = parse.parse_bytes(make_docx_bytes(), '夹具.docx')
        sk = doc['structure']
        self.assertEqual(sk['method'], 'depth')
        self.assertEqual([c['name'] for c in sk['chapters']],
                         ['第一章 产品概述', '第二章 技术架构'])


class TestCompression(unittest.TestCase):
    """超页数时**保章压页**：章节一个不少。"""

    def test_allocate_guarantees_one_page_per_chapter(self):
        # 5 章 18 页压到 15：每章至少 1 页
        self.assertEqual(sum(pipeline._allocate([3, 3, 4, 7, 1], 15)), 15)
        self.assertTrue(all(q >= 1 for q in pipeline._allocate([3, 3, 4, 7, 1], 15)))

    def test_allocate_never_drops_chapters(self):
        for budget in range(1, 12):
            quota = pipeline._allocate([3, 3, 4, 7, 1], budget)
            self.assertTrue(all(q >= 1 for q in quota[:min(budget, 5)]))

    def test_compress_sections_keeps_every_chapter(self):
        sections = [dict(name='第%d章' % i, pages=list(range(n)))
                    for i, n in enumerate([3, 3, 4, 7, 1])]
        out = pipeline._compress_sections(sections, 15)
        self.assertEqual(len(out), 5)                       # 一章都没少
        self.assertEqual(sum(len(s['pages']) for s in out), 15)

    def test_pick_evenly_keeps_first_and_last(self):
        pages = list(range(10))
        picked = pipeline._pick_evenly(pages, 3)
        self.assertEqual(picked[0], 0)
        self.assertEqual(picked[-1], 9)

    def test_long_doc_keeps_structure(self):
        """长文档走压缩路径时，每一章都要还在。"""
        doc = pptx_doc()
        txt = parse.outline_source(doc, max_chars=800)
        for c in doc['structure']['chapters']:
            self.assertIn(c['name'], txt)


class TestOutlineAssembly(unittest.TestCase):

    def test_toc_line_is_capped(self):
        long_name = '04 基于 Dify 的开发实战'
        line = pipeline._toc_line(long_name, '一段很长很长很长很长的章节概括说明文字')
        self.assertLessEqual(len(line), pipeline._TOC_MAX)

    def test_normalise_plan_backfills_header(self):
        """规划结果缺 title/kicker 时要从大纲补齐。

        实测所有 LLM 生成的 deck 都是 has_title=0/N —— 渲染出来正文页顶部
        一片空白（`tokens.header()` 在两者都空时什么都不画）。
        """
        outline = dict(title='T', toc=[], sections=[dict(name='01 甲', summary='', pages=[
            dict(title='页一', hint='', source='S1'),
            dict(title='页二', hint='', source='S2'),
        ])])
        plan = pipeline._normalise_plan(
            [dict(layout='numbered_columns', items=[dict(name='a', desc='b')]),
             dict(layout='data_table', header=['h'], rows=[['r']])],
            outline)
        self.assertEqual(plan['slides'][0]['title'], '页一')
        self.assertEqual(plan['slides'][0]['kicker'], '01 甲')
        self.assertEqual(plan['slides'][1]['title'], '页二')
        self.assertEqual(plan['slides'][1]['source'], 'S2')

    def _plan_one(self, anchor: str, title: str) -> dict:
        """用「模型自创的标题」（对不上任何 heading）规划一页。

        标题对不上时唯一能救的就是锚点 —— 这正是实测的失配场景：
        LLM 大纲给的是主张式标题，`by_title` 永远查不到。
        """
        doc = pptx_doc()
        sk = doc['structure']
        outline = dict(title='T', toc=[], sections=[dict(
            name=sk['chapters'][0]['name'], summary='', pages=[
                dict(title=title, hint='', source='', anchor=anchor)])])
        return pipeline._plan_fallback(outline, doc)['slides'][0]

    def test_plan_fallback_uses_anchor(self):
        src_page = pptx_doc()['structure']['chapters'][0]['pages'][0]['name']
        with_anchor = self._plan_one(src_page, '一个自创的主张式标题')
        # 锚点生效 → 取到了该源页的正文，而不是只有标题
        self.assertIn('这一节的正文说明文字', _slide_text(with_anchor))

    def test_plan_fallback_without_anchor_degrades(self):
        """没有锚点、标题又对不上 → 退化成只有标题的 statement 页。

        这条是「锚点为什么必要」的对照：以前正是这样静默降级的。
        """
        without = self._plan_one('', '一个自创的主张式标题')
        self.assertEqual(without['layout'], 'statement')
        self.assertNotIn('这一节的正文说明文字', _slide_text(without))


if __name__ == '__main__':
    unittest.main()
