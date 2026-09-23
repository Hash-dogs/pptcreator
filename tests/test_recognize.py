# -*- coding: utf-8 -*-
"""截图 → 版式识别：名字收敛、声明校验、试片闸门、重试循环。

**不联网**：模型调用一律用桩替换 `llm.ask_vision_json`。桩回答得好/不好两种情况
各跑一遍 —— 重点是「不合格就不入库」这条，因为用户不能对话修正识别结果，
闸门是唯一的防线。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))

from pptgen import config, layout_spec, layout_store, llm, recognize  # noqa: E402

TMP = tempfile.mkdtemp(prefix='pptgen-recognize-test-')
# 桩配置：`recognize` 会先查视觉模型是否配置，缺了就直接返回「做不了」。
# 桩把网络调用整个换掉了，这里的 base/key 不会被用到。
CFG = config.LLMConfig(base='http://stub', key='k', model='stub-vision')

GOOD = dict(
    name='Left Hero Stack', roles=['content'], intents=['enumeration'],
    signature='左侧大字主张 + 右侧三条要点', best_for='一页讲两件事',
    avoid_for='条目超过 4 条',
    min_items=2, max_items=4, item_chars=22, total_chars=220,
    blocks=[
        dict(kind='text', field='lead', x=0.0, y=0.0, w=0.38, h=0.55, size=30),
        dict(kind='bullets', field='items', x=0.46, y=0.0, w=0.54, h=1.0, ncol=1),
        dict(kind='rule', x=0.0, y=0.98, w=1.0, h=0.01),
    ],
    sample=dict(lead='把复杂流程留给平台。',
                items=[dict(name='可视化编排', desc='拖拽式工作流'),
                       dict(name='RAG 引擎', desc='文档变答案'),
                       dict(name='可观测性', desc='每步都有日志')]),
)

VERIFY_KEYS = ('两张图', 'consistent')


def setUpModule():
    os.environ['PPTGEN_LAYOUTS_DIR'] = TMP
    layout_store.load_all(force=True)


def tearDownModule():
    layout_store.save_state({'disabled': []})
    for m in layout_store.list_metas():
        try:
            layout_store.unregister(m.get('name'))
        except ValueError:
            pass
    os.environ.pop('PPTGEN_LAYOUTS_DIR', None)


def _template_ok():
    return os.path.isfile(config.template_path())


class TestNaming(unittest.TestCase):

    def test_slugify(self):
        self.assertEqual(recognize.slugify('Left Hero Stack'), 'left_hero_stack')
        self.assertEqual(recognize.slugify('左 右 对 照!!'), 'layout_new')
        self.assertTrue(len(recognize.slugify('x')) >= 3)
        self.assertLessEqual(len(recognize.slugify('a' * 80)), 32)

    def test_unique_name(self):
        self.assertEqual(recognize.unique_name('abc', set()), 'abc')
        self.assertEqual(recognize.unique_name('abc', {'abc'}), 'abc_2')
        self.assertEqual(recognize.unique_name('abc', {'abc', 'abc_2'}), 'abc_3')


class TestCoerce(unittest.TestCase):

    def test_name_and_enum_filtering(self):
        got = recognize.coerce_meta(dict(GOOD, name='Bad Name!',
                                         intents=['design', 'enumeration'],
                                         roles=['slide', 'content']))
        self.assertEqual(got['name'], 'bad_name')
        self.assertEqual(got['intents'], ['enumeration'])
        self.assertEqual(got['roles'], ['content'])

    def test_blocks_are_normalized_once(self):
        """归一只做一次 —— 拿英寸再喂一遍会得到完全错位的版式。

        这条断言就是那个陷阱的**证据**：`coerce_meta` 的输出必须等于「原始比例
        归一化一次」的结果；如果哪天有人在里面又归一化了一遍，这里会红。
        """
        from pptgen import layout_dsl
        got = recognize.coerce_meta(dict(GOOD))
        once = layout_dsl.normalize_blocks(GOOD['blocks'])
        self.assertEqual([b['x'] for b in got['blocks']],
                         [b['x'] for b in once])
        self.assertGreater(got['blocks'][0]['w'], 1.0)          # 已经是英寸

    def test_fallback_points_to_unknown_names_are_dropped(self):
        got = recognize.coerce_meta(dict(GOOD, fallback=['no_such', 'statement']))
        self.assertEqual(got['fallback'], ['statement'])
        self.assertTrue(any('降级链' in n for n in got['_notes']))

    def test_item_defaults_come_from_box_capacity(self):
        got = recognize.coerce_meta(dict(GOOD, min_items=None, max_items=None))
        self.assertGreaterEqual(got['max_items'], got['min_items'])
        self.assertGreater(got['max_items'], 0)

    def test_problems_report_dropped_blocks(self):
        dropped = ['text 区块被丢掉：太窄或太矮']
        got = recognize.coerce_meta(dict(GOOD), dropped=dropped)
        self.assertIn(dropped[0], recognize.problems_of(got, dropped=dropped))


class TestGate(unittest.TestCase):

    def _trial(self, issues, truncations=()):
        return dict(geometry=dict(issues=list(issues), summary=dict(error=0, warn=0)),
                    truncations=list(truncations))

    def test_clean_trial_passes(self):
        self.assertEqual(recognize.gate(self._trial([])), [])

    def test_overflow_and_truncation_block(self):
        bad = self._trial([dict(severity='warn', slide=3, shape='x',
                                kind='text_overflow', detail='装不下')],
                          [('很长的原文', '很长的原…')])
        got = recognize.gate(bad)
        self.assertTrue(any('装不下' in p for p in got), got)
        self.assertTrue(any('截断' in p for p in got), got)

    def test_geometry_error_blocks(self):
        bad = self._trial([dict(severity='error', slide=3, shape='x',
                                kind='out_of_canvas', detail='越界')])
        self.assertTrue(any('几何错误' in p for p in recognize.gate(bad)))


class TestNoVisionModel(unittest.TestCase):

    def test_missing_vision_is_reported_not_raised(self):
        work = tempfile.mkdtemp(prefix='pptgen-novision-')
        img = os.path.join(work, 'shot.png')
        with open(img, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n')
        got = recognize.recognize(img, work, cfg=None, on_log=lambda m: None)
        self.assertFalse(got['ok'])
        self.assertIn('视觉模型', got['reason'])


@unittest.skipUnless(_template_ok(), '模板文件不存在，跳过试片渲染')
class TestRecognizeLoop(unittest.TestCase):
    """整条链路：桩模型 → 归一化 → 校验 → 渲试片 → 构图比对 → 草稿。"""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix='pptgen-recognize-loop-')
        cls.img = os.path.join(cls.work, 'shot.png')
        with open(cls.img, 'wb') as f:                 # 桩不看内容，但文件要存在
            f.write(b'\x89PNG\r\n\x1a\n')

    def setUp(self):
        self._orig = llm.ask_vision_json
        self.addCleanup(lambda: setattr(llm, 'ask_vision_json', self._orig))
        for name in [m.get('name') for m in layout_store.list_metas()]:
            try:
                layout_store.unregister(name)
            except ValueError:
                pass

    def _stub(self, meta, verify=None):
        verify = verify or dict(consistent=True, reason='构图一致')

        def fn(prompt, image_paths, cfg=None, **kw):
            if any(k in prompt for k in VERIFY_KEYS):
                return verify
            return dict(meta)
        llm.ask_vision_json = fn

    def test_happy_path(self):
        self._stub(GOOD)
        got = recognize.recognize(self.img, self.work, cfg=CFG, on_log=lambda m: None)
        self.assertTrue(got['ok'], got.get('reason'))
        self.assertEqual(got['meta']['name'], 'left_hero_stack')
        self.assertEqual(got['attempts'], [])
        self.assertTrue(got['trial']['png'])
        self.assertEqual(got['trial']['geometry']['summary']['error'], 0)
        self.assertEqual(got['trial']['truncations'], [])
        self.assertTrue(got['verify']['consistent'])

    def test_recognition_does_not_touch_the_registry(self):
        """识别不该把半成品放进版式库 —— 否则并发的生成可能正好选中它。"""
        self._stub(GOOD)
        recognize.recognize(self.img, self.work, cfg=CFG, on_log=lambda m: None)
        self.assertNotIn('left_hero_stack', layout_spec.names())

    def test_invalid_declaration_is_not_adopted(self):
        """意图非法 + 样例缺字段：每轮都不合格，最终不入库且原因可读。"""
        self._stub(dict(GOOD, intents=['design'], sample={'lead': '只有 lead'}))
        got = recognize.recognize(self.img, self.work, cfg=CFG, rounds=2,
                                  on_log=lambda m: None)
        self.assertFalse(got['ok'])
        self.assertEqual(len(got['attempts']), 2)
        self.assertTrue(got['reason'])

    def test_visual_mismatch_blocks_adoption(self):
        self._stub(GOOD, verify=dict(consistent=False, reason='分栏数不对'))
        got = recognize.recognize(self.img, self.work, cfg=CFG, rounds=1,
                                  on_log=lambda m: None)
        self.assertFalse(got['ok'])
        self.assertTrue(any('构图不一致' in p
                            for a in got['attempts'] for p in a['problems']))

    def test_prompt_lists_every_block_kind(self):
        """提示词里的区块清单从代码生成 —— 它必须覆盖渲染器认识的全部种类。"""
        from pptgen import layout_dsl
        txt = recognize.recognize_prompt()
        for k in layout_dsl.BLOCK_KINDS:
            self.assertIn(k, txt)
        for it in layout_spec.INTENTS:
            self.assertIn(it, txt)

    def test_prompt_carries_feedback(self):
        txt = recognize.recognize_prompt('第一轮的问题')
        self.assertIn('必须', txt)
        self.assertIn('第一轮的问题', txt)


if __name__ == '__main__':
    unittest.main()
