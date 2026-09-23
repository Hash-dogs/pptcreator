# -*- coding: utf-8 -*-
"""声明式版式（自定义版式的渲染路径）：归一化、校验、字段契约、渲染。

为什么这些要单独测：19 套内置版式有 `tests/test_layouts.py` 那份回归网兜着，
**自定义版式没有** —— 它是运行时由识别结果生成的，写错坐标不会有任何东西报警。
所以这一层把「归一化是确定性的」「区块装不下会被拦下来」这两件事钉死。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))

from pptgen import build, config, layout_dsl, tokens           # noqa: E402
from pptgen.qa import geometry                                 # noqa: E402

LEFT, RIGHT = tokens.LEFT, tokens.RIGHT
Y0, Y1 = tokens.Y_CONTENT, tokens.Y_BOTTOM


class TestNormalize(unittest.TestCase):
    """比例 → 英寸，以及四件确定性的事：吸附、裁剪、丢弃、撑满。"""

    def test_fractions_become_inches(self):
        got = layout_dsl.normalize_blocks([
            dict(kind='bullets', field='items', x=0.5, y=0.25, w=0.5, h=0.5)])
        b = got[0]
        self.assertAlmostEqual(b['x'], LEFT + 0.5 * (RIGHT - LEFT), places=2)
        self.assertAlmostEqual(b['y'], Y0 + 0.25 * (Y1 - Y0), places=2)
        self.assertLessEqual(b['x'] + b['w'], RIGHT + 1e-6)

    def test_snaps_to_content_edges(self):
        """贴着版心左右边（差 0.2" 以内）要吸附，不然渲染出来右边参差。"""
        got = layout_dsl.normalize_blocks([
            dict(kind='text', field='a', x=0.01, y=0, w=1.0, h=0.3)])
        self.assertEqual(got[0]['x'], LEFT)
        self.assertAlmostEqual(got[0]['x'] + got[0]['w'], RIGHT, places=2)

    def test_clips_out_of_band(self):
        got = layout_dsl.normalize_blocks([
            dict(kind='text', field='a', x=0.9, y=0.9, w=0.6, h=0.6)])
        b = got[0]
        self.assertLessEqual(b['x'] + b['w'], RIGHT + 1e-6)
        self.assertLessEqual(b['y'] + b['h'], Y1 + 1e-6)

    def test_drops_tiny_blocks_with_reason(self):
        """丢弃是**有损**的，所以必须报出原因（识别链路要拿它去重试）。"""
        dropped = []
        got = layout_dsl.normalize_blocks([
            dict(kind='text', field='a', x=0.0, y=0.0, w=0.03, h=0.5),
            dict(kind='bullets', field='items', x=0.4, y=0.0, w=0.6, h=0.9,
                 ncol=1),
        ], dropped)
        self.assertEqual([b['kind'] for b in got], ['bullets'])
        self.assertEqual(len(dropped), 1)
        self.assertIn('text', dropped[0])
        self.assertIn('丢掉', dropped[0])

    def test_unknown_kind_is_dropped_with_reason(self):
        dropped = []
        got = layout_dsl.normalize_blocks([dict(kind='carousel', x=0, y=0, w=1, h=1)],
                                          dropped)
        self.assertEqual(got, [])
        self.assertIn('不认识', dropped[0])

    def test_rule_survives_thin_height(self):
        """发丝线本来就只有 0.01" 高 —— 按内容区块的下限去卡会把每条都丢掉。"""
        got = layout_dsl.normalize_blocks([
            dict(kind='text', field='a', x=0, y=0, w=0.5, h=0.5),
            dict(kind='rule', x=0, y=0.9, w=1.0, h=0.01),
        ])
        self.assertIn('rule', [b['kind'] for b in got])

    def test_fill_band_stretches_content(self):
        """内容只占内容带上半截时，整段撑满 —— 否则每页都会报「留白过多」。"""
        got = layout_dsl.normalize_blocks([
            dict(kind='text', field='a', x=0, y=0.0, w=0.5, h=0.2),
            dict(kind='text', field='b', x=0.5, y=0.4, w=0.5, h=0.2),
        ])
        top = min(b['y'] for b in got)
        bot = max(b['y'] + b['h'] for b in got)
        self.assertAlmostEqual(top, Y0, places=1)
        self.assertAlmostEqual(bot, Y1, places=1)

    def test_fill_band_keeps_decorations_inside(self):
        """色带会被重映射推出内容带（实测顶边被抬到 1.91"），必须收回来。"""
        got = layout_dsl.normalize_blocks([
            dict(kind='band', x=0, y=0, w=1, h=0.4),
            dict(kind='kpi', field='items', x=0, y=0.05, w=1, h=0.35, ncol=3),
            dict(kind='table', field='table', x=0, y=0.55, w=1, h=0.4),
        ])
        band = [b for b in got if b['kind'] == 'band'][0]
        self.assertGreaterEqual(band['y'], Y0 - 1e-6)


GOOD = dict(
    name='left_hero_stack', roles=('content',), intents=('enumeration',),
    min_items=2, max_items=4, item_chars=22, total_chars=220,
    signature='左侧大字主张 + 右侧要点列表',
    blocks=[
        dict(kind='text', field='lead', x=0, y=0, w=0.38, h=0.55, size=30),
        dict(kind='bullets', field='items', x=0.46, y=0, w=0.54, h=1.0, ncol=1),
    ],
    sample=dict(lead='一句话主张。',
                items=[dict(name='要点一', desc='说明'), dict(name='要点二', desc='说明')]),
)


class TestProblems(unittest.TestCase):
    """校验给的是**人话原因**：它要原样进「带违例清单重试」的提示词。"""

    def test_good_meta_has_no_problem(self):
        self.assertEqual(layout_dsl.problems(GOOD), [])

    def test_bad_name(self):
        got = layout_dsl.problems(dict(GOOD, name='Left Hero!'))
        self.assertTrue(any('版式名' in p for p in got), got)

    def test_name_collision(self):
        got = layout_dsl.problems(GOOD, existing={'left_hero_stack'})
        self.assertTrue(any('已被占用' in p for p in got), got)

    def test_unknown_intent_and_role(self):
        got = layout_dsl.problems(dict(GOOD, intents=('design',), roles=('slide',)))
        self.assertTrue(any('intents' in p for p in got), got)
        self.assertTrue(any('roles' in p for p in got), got)

    def test_content_layout_needs_an_intent(self):
        """没有意图的内容版式永远不会被选中 —— 这是静默失效，必须拦。"""
        got = layout_dsl.problems(dict(GOOD, intents=()))
        self.assertTrue(any('永远不会被选中' in p for p in got), got)

    def test_item_range_inverted(self):
        got = layout_dsl.problems(dict(GOOD, min_items=5, max_items=2))
        self.assertTrue(any('大于' in p for p in got), got)

    def test_block_kind_and_missing_sample_field(self):
        got = layout_dsl.problems(dict(GOOD, blocks=[
            dict(kind='carousel', field='x', x=0, y=0, w=1, h=1)]))
        self.assertTrue(any('不认识' in p for p in got), got)
        got2 = layout_dsl.problems(dict(GOOD, sample=dict(lead='只有 lead')))
        self.assertTrue(any('items' in p for p in got2), got2)

    def test_empty_blocks(self):
        got = layout_dsl.problems(dict(GOOD, blocks=[]))
        self.assertTrue(any('画不出东西' in p for p in got), got)


class TestContract(unittest.TestCase):
    """字段契约与容量文本从区块生成 —— 手写一份就会与渲染器漂移。

    ⚠️ 容量按框的**实际英寸尺寸**算，所以这里先归一化（`catalog_of` 的
    docstring 里写了这条要求）。
    """

    @classmethod
    def setUpClass(cls):
        cls.meta = dict(GOOD, blocks=layout_dsl.normalize_blocks(GOOD['blocks']))

    def test_catalog_from_blocks(self):
        txt = layout_dsl.catalog_of(self.meta)
        self.assertIn('lead', txt)
        self.assertIn('items', txt)
        self.assertIn('2–4 条', txt)          # 用**声明的**范围，不是框容量

    def test_capacity_not_empty(self):
        cap = layout_dsl.capacity_of(self.meta)
        self.assertTrue(cap.strip())
        self.assertIn('lead', cap)

    def test_kind_catalog_lists_every_kind(self):
        txt = layout_dsl.kind_catalog_text()
        for k in layout_dsl.BLOCK_KINDS:
            self.assertIn(k, txt)

    def test_item_capacity_positive(self):
        self.assertGreater(layout_dsl.item_capacity(self.meta), 0)


class TestFitLines(unittest.TestCase):
    """折行 + 降字号 + 末路截断，且截断要**记账**。"""

    def setUp(self):
        tokens.take_truncations()

    def test_short_text_keeps_size(self):
        lines, size = layout_dsl._fit_lines('短句', 4.0, 1.0, sizes=(30, 24, 18))
        self.assertEqual(lines, ['短句'])
        self.assertEqual(size, 30)
        self.assertEqual(tokens.take_truncations(), [])

    def test_long_text_shrinks_then_truncates(self):
        long = '这是一句很长的说明' * 30
        lines, size = layout_dsl._fit_lines(long, 2.0, 0.4, sizes=(30, 24, 18))
        self.assertLessEqual(len(lines), layout_dsl._max_lines(0.4, 1.3))
        self.assertTrue(tokens.take_truncations(), '截断必须记进 TRUNCATIONS')

    def test_newlines_are_paragraphs(self):
        lines, _ = layout_dsl._fit_lines('第一段\n第二段', 6.0, 2.0, sizes=(16,))
        self.assertEqual(lines, ['第一段', '第二段'])


# 九种区块分四套版式（一页一类），每套的框都不重叠。
# **刻意不是「九种挤一页」**：那种自检样例自己就会重叠，报出来的告警分不清是
# 渲染器的毛病还是样例的毛病（第一版就是这么写的，红了两条测试）。
PROBES = [
    dict(name='probe_band_kpi', roles=('content',), intents=('quantitative',),
         min_items=2, max_items=3, item_chars=16, total_chars=200,
         signature='通栏色带上的大字与指标', best_for='自检',
         fallback=('kpi_grid',),
         blocks=[
             dict(kind='band', x=0, y=0, w=1, h=0.42),
             dict(kind='text', field='lead', x=0, y=0.03, w=0.36, h=0.36, size=24),
             dict(kind='kpi', field='stats', x=0.40, y=0.03, w=0.60, h=0.36, ncol=3),
             dict(kind='rule', x=0, y=0.50, w=1.0, h=0.01),
         ],
         sample=dict(lead='通栏色带 + 三个指标。',
                     stats=[dict(num='1', unit='个', label='指标'),
                            dict(num='2', unit='个', label='指标'),
                            dict(num='3', unit='个', label='指标')])),
    dict(name='probe_lists', roles=('content',), intents=('enumeration',),
         min_items=2, max_items=4, item_chars=22, total_chars=220,
         signature='左要点列表 + 右分栏卡片', best_for='自检',
         fallback=('numbered_columns',),
         blocks=[
             dict(kind='bullets', field='items', x=0, y=0, w=0.46, h=1.0, ncol=1),
             dict(kind='columns', field='cols', x=0.52, y=0, w=0.48, h=1.0, ncol=2),
             dict(kind='rule', x=0, y=0.995, w=1.0, h=0.01),
         ],
         sample=dict(items=[dict(name='要点一', desc='说明'),
                            dict(name='要点二', desc='说明'),
                            dict(name='要点三', desc='说明')],
                     cols=[dict(name='左栏', desc='说明'),
                           dict(name='中栏', desc='说明'),
                           dict(name='右栏', desc='说明'),
                           dict(name='第四栏', desc='说明')])),
    dict(name='probe_flow_table', roles=('content',), intents=('process',),
         min_items=2, max_items=4, item_chars=20, total_chars=240,
         signature='上方流程链 + 下方参数表', best_for='自检',
         fallback=('process_chain',),
         blocks=[
             dict(kind='steps', field='steps', x=0, y=0, w=1.0, h=0.42),
             dict(kind='table', field='table', x=0, y=0.52, w=1.0, h=0.46),
         ],
         sample=dict(steps=[dict(num='01', name='第一步', desc='说明'),
                            dict(num='02', name='第二步', desc='说明'),
                            dict(num='03', name='第三步', desc='说明')],
                     table=dict(header=['列一', '列二'],
                                rows=[['a', 'b'], ['c', 'd']]))),
    dict(name='probe_rows', roles=('content',), intents=('comparison',),
         min_items=2, max_items=4, item_chars=20, total_chars=200,
         signature='维度对照行', best_for='自检', fallback=('comparison_rows',),
         blocks=[
             dict(kind='rows', field='rows', x=0, y=0, w=1.0, h=1.0),
         ],
         sample=dict(col_a='改造前', col_b='改造后',
                     rows=[dict(dim='维度一', a='A 列内容', b='B 列内容'),
                           dict(dim='维度二', a='A 列内容', b='B 列内容'),
                           dict(dim='维度三', a='A 列内容', b='B 列内容')])),
]


def _template():
    p = config.template_path()
    return p if os.path.isfile(p) else None


class TestRenderAllKinds(unittest.TestCase):
    """九种区块各渲一页，过几何检查 —— 相当于自定义版式的回归网。

    区块的框是按「比例」写的，渲染器把它们换算成英寸并自适应字号；只要有一处
    越界/重叠/溢出，几何检查就会红。缺模板就整类跳过（与 `tests/test_layouts.py`
    对模板的处理一致）。
    """

    @classmethod
    def setUpClass(cls):
        tpl = _template()
        if tpl is None:
            raise unittest.SkipTest('模板文件不存在，跳过渲染回归')
        cls.tmp = tempfile.mkdtemp(prefix='pptgen-dsl-')
        cls.metas = []
        slides = []
        for raw in PROBES:
            meta = dict(raw)
            meta['blocks'] = layout_dsl.normalize_blocks(raw['blocks'])
            cls.metas.append(meta)
            sl = dict(meta['sample'])
            sl['layout'] = meta['name']
            sl['blocks'] = meta['blocks']      # 试片走 spec，不注册
            sl['kicker'] = '自检'
            sl['title'] = meta['signature']
            slides.append(sl)
        cls.path = os.path.join(cls.tmp, 'dsl.pptx')
        tokens.take_truncations()
        build.build(dict(slides=slides, toc=[]), tpl, cls.path)
        cls.truncations = tokens.take_truncations()
        cls.rep = geometry.analyse(cls.path)

    def test_every_probe_is_valid(self):
        for meta in self.metas:
            self.assertEqual(layout_dsl.problems(meta), [], meta['name'])

    def test_no_geometry_errors(self):
        bad = [i for i in self.rep['issues'] if i['severity'] == 'error']
        self.assertEqual(bad, [], geometry.format_report(self.rep))

    def test_no_text_overflow_or_overlap(self):
        bad = [i for i in self.rep['issues']
               if i['kind'] in ('text_overflow', 'text_overlap')]
        self.assertEqual(bad, [], geometry.format_report(self.rep))

    def test_no_silent_truncation(self):
        """截断是静默的（几何检查看不见），只能靠这条断言把它变成可见的失败。"""
        self.assertEqual(self.truncations, [],
                         '有文案被截断：%s' % self.truncations[:2])


if __name__ == '__main__':
    unittest.main()
