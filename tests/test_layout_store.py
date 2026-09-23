# -*- coding: utf-8 -*-
"""自定义版式的存储与注册：加载 / 注册 / 启停 / 删除。

**隔离到临时目录**（`PPTGEN_LAYOUTS_DIR`）—— 跑测试绝不能把版式写进仓库里的
`layouts_custom/`，否则测试会污染真实版式库（那是一条会真的改到用户资产的路径）。
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))

from pptgen import layout_dsl, layout_spec, layout_store, layouts  # noqa: E402

TMP = tempfile.mkdtemp(prefix='pptgen-store-test-')

META = dict(
    name='left_hero_stack', roles=['content'], intents=['enumeration'],
    min_items=2, max_items=4, item_chars=22, total_chars=220,
    signature='左侧大字主张 + 右侧要点列表', best_for='一页讲两件事',
    avoid_for='条目超过 4 条', fallback=['numbered_columns'],
    blocks=[
        dict(kind='text', field='lead', x=0.0, y=0.0, w=0.38, h=0.55, size=30),
        dict(kind='bullets', field='items', x=0.46, y=0.0, w=0.54, h=1.0, ncol=1),
    ],
    sample=dict(lead='一句话主张。',
                items=[dict(name='要点一', desc='说明'),
                       dict(name='要点二', desc='说明')]),
)


def setUpModule():
    os.environ['PPTGEN_LAYOUTS_DIR'] = TMP
    layout_store.load_all(force=True)


def tearDownModule():
    # 还原：别把禁用状态与自定义版式泄漏给别的测试文件
    layout_store.save_state({'disabled': []})
    for name in ['left_hero_stack']:
        try:
            layout_store.unregister(name)
        except ValueError:
            pass
    layout_spec.set_disabled(set())
    os.environ.pop('PPTGEN_LAYOUTS_DIR', None)


class TestRegistry(unittest.TestCase):

    def test_empty_dir_keeps_builtins(self):
        names = layout_spec.names()
        self.assertIn('statement', names)
        self.assertEqual(
            len([n for n in names if layout_spec.REGISTRY[n].source == 'builtin']),
            19)

    def test_save_registers_everywhere(self):
        """注册要同时写四处，缺一处就是静默失效 —— 这条断言把四处都点一遍。"""
        path = layout_store.save_meta(dict(META))
        self.assertTrue(os.path.isfile(path))
        self.assertIn('left_hero_stack', layout_spec.names())          # REGISTRY
        self.assertIn('left_hero_stack', layouts.LAYOUTS)              # 渲染函数
        self.assertIn('left_hero_stack', layouts.LAYOUT_NAMES)         # 合法性判据
        self.assertTrue(layout_dsl.decl_of('left_hero_stack'))         # 区块声明
        self.assertEqual(layout_spec.get('left_hero_stack').source, 'custom')

    def test_new_layout_is_selectable(self):
        got = [c.name for c in layout_spec.candidates('content', 'enumeration')]
        self.assertIn('left_hero_stack', got)
        self.assertIn('left_hero_stack', layout_spec.catalog_text())

    def test_catalog_and_capacity_are_generated(self):
        sp = layout_spec.get('left_hero_stack')
        self.assertIn('lead', sp.catalog)
        self.assertTrue(sp.capacity.strip())

    def test_remove_takes_it_out_of_the_pipeline_whitelist(self):
        layout_store.remove_meta('left_hero_stack')
        self.assertNotIn('left_hero_stack', layout_spec.names())
        self.assertNotIn('left_hero_stack', layouts.LAYOUT_NAMES)
        self.assertNotIn('left_hero_stack', layouts.LAYOUTS)
        self.assertEqual(layout_dsl.decl_of('left_hero_stack'), [])

    def test_builtin_cannot_be_removed(self):
        with self.assertRaises(ValueError):
            layout_store.remove_meta('statement')


class TestEnableDisable(unittest.TestCase):

    def setUp(self):
        layout_store.save_meta(dict(META))
        layout_store.save_state({'disabled': []})
        layout_spec.set_disabled(set())

    def test_disable_keeps_renderer(self):
        """禁用 ≠ 删除：旧 deck 里用到它的页面照样要能渲染、能按页改。"""
        layout_store.set_enabled('left_hero_stack', False)
        self.assertFalse(layout_spec.is_enabled('left_hero_stack'))
        self.assertNotIn('left_hero_stack',
                         [c.name for c in layout_spec.candidates('content', 'enumeration')])
        self.assertNotIn('left_hero_stack', layout_spec.catalog_text())
        self.assertNotIn('left_hero_stack', layout_spec.enabled_names())
        # 但注册表与渲染函数都还在
        self.assertIn('left_hero_stack', layout_spec.names())
        self.assertIn('left_hero_stack', layouts.LAYOUTS)

    def test_is_enabled_matches_candidates_after_cold_load(self):
        """冷启动一致性：`is_enabled` 不能与 `candidates` 给出不同答案。

        实测过的坑：`is_enabled` 不触发注册表加载时，会对一个其实被禁用的版式
        返回 True，而同一进程里 `candidates()` 里又没有它。
        """
        layout_store.set_enabled('quadrant', False)
        try:
            self.assertEqual(layout_spec.is_enabled('quadrant'),
                             'quadrant' in [c.name for c in
                                            layout_spec.candidates('content', 'enumeration')])
        finally:
            layout_store.set_enabled('quadrant', True)

    def test_disable_builtin_and_resolve_avoids_it(self):
        layout_store.set_enabled('quadrant', False)
        try:
            got = layout_spec.resolve(layout_spec.get('quadrant'), 'content', 'enumeration')
            self.assertNotEqual(got.name, 'quadrant')
            self.assertTrue(layout_spec.is_enabled(got.name))
        finally:
            layout_store.set_enabled('quadrant', True)

    def test_state_survives_reload(self):
        layout_store.set_enabled('left_hero_stack', False)
        layout_store.load_all(force=True)
        self.assertFalse(layout_spec.is_enabled('left_hero_stack'))

    def test_unknown_name_raises(self):
        with self.assertRaises(ValueError):
            layout_store.set_enabled('no_such_layout', False)


class TestBrokenFiles(unittest.TestCase):
    """一个坏文件不该拖垮整个版式库，但也不能静默消失。"""

    def test_broken_json_is_skipped_but_visible(self):
        p = os.path.join(TMP, 'broken.json')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('{ this is not json')
        try:
            n = layout_store.load_all(force=True)
            self.assertEqual(n, 0)
            metas = {m.get('name'): m for m in layout_store.list_metas()}
            self.assertIn('broken', metas)
            self.assertTrue(metas['broken'].get('_problems'))
            self.assertNotIn('broken', layout_spec.names())
        finally:
            os.remove(p)
            layout_store.load_all(force=True)

    def test_meta_missing_fields_is_reported_not_registered(self):
        bad = dict(META, name='no_intent_layout', intents=[])
        p = os.path.join(TMP, 'no_intent_layout.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(bad, f, ensure_ascii=False)
        try:
            layout_store.load_all(force=True)
            self.assertNotIn('no_intent_layout', layout_spec.names())
            metas = {m.get('name'): m for m in layout_store.list_metas()}
            self.assertTrue(metas['no_intent_layout'].get('_problems'))
        finally:
            os.remove(p)
            layout_store.load_all(force=True)

    def test_save_rejects_invalid_meta(self):
        with self.assertRaises(ValueError):
            layout_store.save_meta(dict(META, name='Bad Name!'))


if __name__ == '__main__':
    unittest.main()
