# -*- coding: utf-8 -*-
"""版式回归网：每套版式各渲染一页，逐页过 build 自检与几何检查。

为什么需要它：在这之前，全部渲染函数里只有 `statement` 被间接触达，而且那条
断言检查的是 spec 字段、**不是真实渲染**（`test_structure.py`）。新增或修改版式
时，唯一的结构性防护是 `build.selfcheck()`（zip/rId 层面）与 `qa/geometry.py`
（包围盒层面），二者都不遍历 `LAYOUT_NAMES` —— 写错一个坐标不会有任何东西报警。

测三件事：
  1. 注册表与渲染器一一对应（`@layout` 元数据没漏、没有孤儿渲染函数）
  2. 每套版式的样例 spec 都**装得进**自己的容量声明（几何检查干净）
  3. 依赖模板的版式（`data_table` / 图表）在模板存在时才跑
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))

from unittest import mock                                # noqa: E402

import pptx                                              # noqa: E402

from pptgen import build, layouts, layout_spec, pipeline, tokens  # noqa: E402
from pptgen import config                                # noqa: E402
from pptgen import layout_store, recognize               # noqa: E402
from pptgen.qa import geometry                           # noqa: E402

# 版式样例 spec 上提到 `src/pptgen/samples.py`：回归网、图鉴、页面预览共用一批，
# 不再由这个测试文件独占（gallery.py 曾经靠往 sys.path 插 tests/ 才 import 得到它）。
from pptgen.samples import FIXTURES                      # noqa: E402


def _template():
    p = config.template_path()
    return p if os.path.isfile(p) else None


class _LibraryIntact(unittest.TestCase):
    """假设「版式库是全的」的测试类继承它。

    `_state.json` 里的 `disabled` 是**跑测试这台机器上的用户状态**，而
    `layout_store.load_all()` 一个进程只加载一次 —— 于是同一个文件，单跑一个
    模块与跑整个 discover 会拿到不同的启用状态（实测：本机禁用了
    statement / quote 之后，本文件的 `test_intent_maps_to_matching_layouts`
    与 `test_every_layout_is_reachable_by_intent` 单跑必挂）。这些断言讲的是
    「版式系统的设计」，不该随用户禁用了哪几套而变，所以在这里显式声明
    「一套都没禁用」；禁用相关的断言各自 `set_disabled` / 打补丁。
    """

    @classmethod
    def setUpClass(cls):
        cls._disabled_before = layout_spec.disabled_names()
        layout_spec.set_disabled([])
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        layout_spec.set_disabled(cls._disabled_before)
        super().tearDownClass()


class TestRegistry(_LibraryIntact):
    def test_renderers_and_specs_match(self):
        """每个渲染函数都有元数据，每条元数据都有渲染函数。"""
        self.assertEqual(sorted(layouts.LAYOUTS), sorted(layout_spec.REGISTRY))

    def test_builtin_layout_count(self):
        """**内置**版式是 19 套（原 20 套减去 node_flow，它被 phase_grouped_flow 取代）。

        只数 `source == 'builtin'`：版式库里还会有用户加的自定义版式
        （`layouts_custom/*.json`），把它们算进来这条断言就随环境变了。
        """
        builtins = [n for n, sp in layout_spec.REGISTRY.items()
                    if sp.source == 'builtin']
        self.assertEqual(len(builtins), 19, '内置版式应为 19 套')

    def test_layout_spec_alone_is_not_empty(self):
        """只 import layout_spec 也该看到内置版式（外加目录里的自定义版式）。

        注册分两步：`layouts.py` 的 `@layout` 装饰器注册内置的，
        `layout_store.load_all()` 加载 `layouts_custom/` 下的自定义版式。
        两步都由 `_ensure_loaded()` 触发 —— 否则「必须先 import layouts」会变成
        一条隐形约定，调用方拿到空目录还找不到原因。
        """
        import subprocess
        code = ('import sys; sys.path.insert(0, %r);'
                'from pptgen import layout_spec;'
                'print(len(layout_spec.names()), len(layout_spec.candidates("content","status")))'
                % os.path.join(ROOT, 'src'))
        out = subprocess.run([sys.executable, '-c', code],
                             capture_output=True, encoding='utf-8')
        parts = (out.stdout or '').split()
        self.assertEqual(len(parts), 2, out.stderr or out.stdout)
        # 内置 19 套，外加用户可能加进来的自定义版式（所以是 >= 而不是 ==）
        self.assertGreaterEqual(int(parts[0]), 19, out.stdout)
        self.assertGreaterEqual(int(parts[1]), 1, out.stdout)

    def test_every_layout_is_reachable_by_intent(self):
        """每套版式都要能被某个 (role, intent) 组合选到，否则它永远不会被使用。"""
        for name, sp in layout_spec.REGISTRY.items():
            hit = False
            for role in sp.roles:
                for intent in (sp.intents or ('',)):
                    got = layout_spec.candidates(role, intent)
                    if any(x.name == name for x in got):
                        hit = True
                        break
                if hit:
                    break
            self.assertTrue(hit, '%s 没有任何 (role, intent) 能选到它' % name)

    def test_intents_are_known(self):
        for sp in layout_spec.REGISTRY.values():
            for it in sp.intents:
                self.assertIn(it, layout_spec.INTENTS, sp.name)
            for r in sp.roles:
                self.assertIn(r, layout_spec.ROLES, sp.name)

    def test_fallbacks_exist(self):
        for sp in layout_spec.REGISTRY.values():
            for fb in sp.fallback:
                self.assertIn(fb, layout_spec.REGISTRY,
                              '%s 的降级目标 %s 不存在' % (sp.name, fb))

    def test_catalog_and_capacity_nonempty(self):
        for sp in layout_spec.REGISTRY.values():
            self.assertTrue(sp.catalog.strip(), '%s 缺 catalog' % sp.name)
            self.assertTrue(sp.capacity.strip(), '%s 缺 capacity' % sp.name)

    def test_catalog_text_lists_every_layout(self):
        txt = layout_spec.catalog_text()
        for name in layouts.LAYOUT_NAMES:
            self.assertIn(name, txt)


class TestCapacityFilter(unittest.TestCase):
    """容量前置校验：这是「标签被静默截断」的事前防线。"""

    @staticmethod
    def _shape(n=None, table=False, numbers=False, item=0, total=0):
        return dict(known=True, n_items=n or 0, has_table=table,
                    has_numbers=numbers, max_item_chars=item, total_chars=total)

    def test_source_item_length_does_not_filter_candidates(self):
        """**源文档条目的长度不能用来筛候选。**

        源条目天然是一整句（实测 40–120 字），而版式的单条预算是针对**成品**的
        （22 字）—— 模型的工作正是把长句压短。早先拿源条目长度去卡候选，版式被
        筛得只剩 statement，13 页内容全塌成一种版式。单条字数改为事后校验成品：
        见 `pipeline.overflow_reason`。
        """
        for item in (25, 80, 200):
            got = [c.name for c in layout_spec.candidates(
                'content', 'process', self._shape(n=8, item=item, total=900))]
            self.assertIn('phase_grouped_flow', got,
                          '源条目 %d 字不该把候选筛光' % item)

    def test_overflow_reason_flags_long_module_names(self):
        """成品超容量要被拦下来 —— 这正是 `_fit()` 静默截断的事前防线。

        历史案例（该版式已删除）：`node_flow` 那 8 个节点里 6 个被截成
        `小红书正文 · 爆款写作…`，而几何报告是干净的（截断消除了溢出）。

        删除 node_flow 之后，声明了 `max_item_chars` 的版式只剩
        `phase_grouped_flow`(34) 与 `layered_stack`(16)，所以这条挂在后者的
        模块名上（单行定高字段，写长了必被 `_fit()` 截断）。**别把这条删了** ——
        它是「超限被拦下」这一侧唯一的覆盖（`test_named_layouts_actually_render`
        只覆盖「不超」那侧），删了 34/16 两个声明就再没人验。
        """
        long_modules = dict(layout='layered_stack', layers=[
            dict(name='接入层', modules=['这是一个明显超过十六个字符的模块名'])])
        self.assertIn('单条', pipeline.overflow_reason(long_modules))

    def test_overflow_reason_passes_short_labels(self):
        ok = dict(layout='layered_stack', layers=[
            dict(name='接入层', modules=['WebApp', 'API', '嵌入网站']),
            dict(name='编排层', modules=['Prompt 编排', '工作流', 'Agent 框架']),
            dict(name='能力层', modules=['RAG 引擎', '模型管理', '插件系统'])])
        self.assertEqual('', pipeline.overflow_reason(ok))

    def test_overflow_reason_flags_too_many_items(self):
        many = dict(layout='quadrant',
                    items=[dict(name='维度%d' % i, desc='说明') for i in range(5)])
        self.assertIn('超过上限', pipeline.overflow_reason(many))

    def test_five_metrics_rule_out_stat_hero(self):
        got = [c.name for c in layout_spec.candidates(
            'content', 'quantitative', self._shape(n=5, numbers=True, item=8))]
        self.assertNotIn('stat_hero', got)
        self.assertIn('kpi_grid', got)

    def test_quadrant_needs_exactly_four(self):
        for n in (3, 5):
            got = [c.name for c in layout_spec.candidates(
                'content', 'enumeration', self._shape(n=n, item=20))]
            self.assertNotIn('quadrant', got, 'n_items=%d 不该选象限' % n)

    def test_chart_needs_numbers(self):
        got = [c.name for c in layout_spec.candidates(
            'content', 'quantitative', self._shape(n=3, numbers=False, item=20))]
        self.assertNotIn('metric_trend', got)

    def test_unknown_shape_does_not_kill_everything(self):
        """anchor 取不到素材时 shape 全 0 —— 那是「没取到」，不是「内容为空」。"""
        got = layout_spec.candidates('content', 'enumeration', layout_spec.EMPTY_SHAPE)
        self.assertIn('numbered_columns', [c.name for c in got])


def _doc():
    """一份最小可用的 doc：两章、每章两页、页内各 4 条要点。"""
    blocks, chapters = [], []
    for ci, cname in enumerate(['01 第一章', '02 第二章']):
        blocks.append(dict(type='heading', level=1, text=cname,
                           role='divider', slide=ci * 3 + 1))
        pages = []
        for pi in (1, 2):
            pname = '页面 %d-%d' % (ci + 1, pi)
            start = len(blocks)
            blocks.append(dict(type='heading', level=2, text=pname, slide=ci * 3 + pi))
            for k in range(4):
                blocks.append(dict(type='bullets', slide=ci * 3 + pi,
                                   items=['要点 %d：一句说明文字' % k]))
            pages.append(dict(name=pname, start=start, end=len(blocks),
                              lead='', chars=40))
        chapters.append(dict(key='%02d' % (ci + 1), name=cname, subtitle='',
                             start=0, end=len(blocks), chars=80, pages=pages))
    return dict(source='t.pptx', kind='pptx', title='测试文档', blocks=blocks,
                structure=dict(chapters=chapters, method='divider', front_matter=[]))


class TestRichTextTolerance(unittest.TestCase):
    """富文本的写法得容错 —— 渲染层不该崩在模型输出上。

    实测模型对同一个字段给出过三种形态，第三种和第二种都会让渲染直接抛异常：
        [("文本", {"hl": true})]、[["文本", {"hl": true}]]、
        [{"text": "文本", "hl": true}]

    被测的 `tokens.paras` 原先叫 `layouts._paras`，2026-09-23 上提到 `tokens`
    —— 内置版式与声明式版式（自定义版式）共用同一份容错，不该各有一份。
    """

    def _text(self, paras):
        return ['|'.join(str(t) for t, _ in p) for p in paras]

    def test_standard_tuple_runs(self):
        got = tokens.paras([[('把', {}), ('创新', {'hl': True})]])
        self.assertEqual(['把|创新'], self._text(got))
        self.assertEqual(layouts.RED, got[0][1][1]['color'])
        self.assertTrue(got[0][1][1]['bold'])

    def test_single_paragraph_written_with_lists(self):
        """`[["文本", {"hl": true}]]` —— 用 list 而非 tuple。"""
        got = tokens.paras([['GitHub 60,000+ Star', {'hl': True}]])
        self.assertEqual(['GitHub 60,000+ Star'], self._text(got))
        self.assertEqual(layouts.RED, got[0][0][1]['color'])

    def test_runs_written_as_dicts(self):
        """`[{"text": …, "hl": …}]` —— 用 dict 描述 run。"""
        got = tokens.paras([[{'text': '定位：', 'hl': False},
                            {'text': '开源平台', 'hl': True}]])
        self.assertEqual(['定位：|开源平台'], self._text(got))
        self.assertEqual(layouts.RED, got[0][1][1]['color'])

    def test_bare_strings_are_separate_paragraphs(self):
        got = tokens.paras(['第一段', '第二段'])
        self.assertEqual(['第一段', '第二段'], self._text(got))

    def test_nested_paragraphs(self):
        got = tokens.paras([[{'text': 'a', 'hl': False}], [{'text': 'b', 'hl': False}]])
        self.assertEqual(['a', 'b'], self._text(got))

    def test_bare_string_and_dict(self):
        self.assertEqual(['一段'], self._text(tokens.paras('一段')))
        self.assertEqual(['一段'], self._text(tokens.paras({'text': '一段'})))

    def test_empty_is_empty(self):
        self.assertEqual([], tokens.paras([]))


class TestPlanStage(_LibraryIntact):
    """意图 → 候选 → 护栏。这一层决定「版式选得贴不贴内容」。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.doc = _doc()

    def _outline(self):
        # 强制走确定性路径：不配置文本模型
        with mock.patch.object(config, 'llm_config', lambda: None):
            return pipeline.make_outline(self.doc)

    def _plan(self):
        with mock.patch.object(config, 'llm_config', lambda: None):
            return pipeline.make_plan(self._outline(), self.doc)

    def test_dividers_only_when_budget_allows(self):
        """分隔页要占页数预算，装不下就完全不插（而不是砍正文）。"""
        # 2 章 / 10 页上限：插得下分隔页，正文区间同步下移（宽度不变）
        n, lo, hi = pipeline._divider_budget(self.doc, 6, 10)
        self.assertEqual((2, 4, 8), (n, lo, hi))
        # 预算太紧（每章摊不到「一页分隔 + 两页正文」）→ 一页都不插
        self.assertEqual(0, pipeline._divider_budget(self.doc, 2, 3)[0])
        # 开关关掉 → 一页都不插
        with mock.patch.object(config, 'section_dividers', lambda: False):
            self.assertEqual(0, pipeline._divider_budget(self.doc, 6, 12)[0])

    def test_dividers_yield_when_they_would_eat_the_content(self):
        """分隔页只在正文撑得住时插。

        6 章 / 12 页是那条老判据（`hi >= 章数 × 2`）刚好踩线通过的样子：
        6 页分隔 + 6 页正文 —— 每章恰好一页，无论上传什么文档都是同一副骨架。
        """
        six = dict(structure={'chapters': [{}] * 6})
        self.assertEqual(0, pipeline._divider_budget(six, 9, 12)[0])
        # 4 章 / 12 页就还留得下：4 页分隔 + 8 页正文
        four = dict(structure={'chapters': [{}] * 4})
        self.assertEqual((4, 5, 8), pipeline._divider_budget(four, 9, 12))

    def test_dividers_inserted_at_each_chapter_head(self):
        out = self._outline()
        self.assertEqual(2, out['divider_count'])
        for s in out['sections']:
            self.assertTrue(s['pages'][0].get('divider'), s['name'])
            self.assertEqual('section', s['pages'][0]['intent'])
        # 分隔页不计入正文页数
        self.assertEqual(4, out['page_count'])
        self.assertEqual(6, out['total_pages'])

    def test_every_page_gets_an_intent(self):
        out = self._outline()
        for s in out['sections']:
            for p in s['pages']:
                if p.get('divider'):
                    self.assertEqual('section', p['intent'])   # 结构页不走内容意图
                    continue
                self.assertIn(p.get('intent'), layout_spec.INTENTS, p['title'])

    def test_plan_has_no_adjacent_duplicate(self):
        """相邻页不得同版式 —— 实测旧产出里第 12、13 页是相邻的两个 `node_flow`
        （该版式已删除，但相邻重复仍是硬约束）。"""
        plan = self._plan()
        names = [s['layout'] for s in plan['slides']]
        dup = [i for i in range(1, len(names)) if names[i] == names[i - 1]]
        self.assertEqual([], dup, names)

    def test_dividers_survive_planning(self):
        plan = self._plan()
        divs = [s for s in plan['slides'] if s['layout'] == 'section_divider']
        self.assertEqual(2, len(divs))
        # 隔断页不给 kicker：它自己就是章节名，补一行小字等于写两遍
        for d in divs:
            self.assertFalse(d.get('kicker'))
            self.assertTrue(d.get('title'))

    def test_intent_maps_to_matching_layouts(self):
        """意图 → 候选：这是「选得贴内容」的执行点。"""
        n = dict(known=True, n_items=4, has_table=False, has_numbers=True,
                 max_item_chars=20, total_chars=120)
        want = {
            'status': 'progress_checklist',
            'hierarchy': 'layered_stack',
            'summary': 'executive_summary',
            'quote': 'quote',
            'comparison': 'comparison_rows',
            'quantitative': 'kpi_grid',
            'definition': 'definition',
        }
        for intent, layout in want.items():
            got = [c.name for c in layout_spec.candidates('content', intent, n)]
            self.assertIn(layout, got, 'intent=%s 的候选里没有 %s' % (intent, layout))

    def test_intent_filter_actually_filters(self):
        """意图不同的两页，候选集不该一样 —— 否则等于没筛。"""
        n = dict(known=True, n_items=4, has_table=False, has_numbers=True,
                 max_item_chars=20, total_chars=120)
        a = {c.name for c in layout_spec.candidates('content', 'status', n)}
        b = {c.name for c in layout_spec.candidates('content', 'process', n)}
        self.assertNotEqual(a, b)

    def test_section_role_only_gets_structure_layouts(self):
        got = [c.name for c in layout_spec.candidates('section', 'section')]
        self.assertEqual(['section_divider'], got)

    def test_named_layouts_actually_render(self):
        """每套版式的成品定义都能过容量自检（拿目录里的样例反查）。"""
        for name, spec in FIXTURES:
            self.assertEqual('', pipeline.overflow_reason(spec),
                             '%s 的样例自己就超容量' % name)


class TestDisabledLayoutsStayOut(unittest.TestCase):
    """禁用清单是用户的显式选择，**代码兜底没有资格绕过它**。

    现场：在版式管理里禁用 `statement` / `quote` 之后，一份 21 页的 deck 里
    照样有两页是「一行字」的 statement。它们不是模型选的 —— 是确定性兜底
    硬写的。硬写 statement 的地方有四处：`_heuristic_slide` 的「本页没有素材」
    分支与收尾、`allowed` 为空时的 `or ['statement']`、`_normalise_plan` 的
    「版式名未知」替换、`layout_spec.resolve` 的全局兜底。

    这里不碰磁盘上的 `_state.json`（那会让测试随环境变），直接改
    `layout_spec._DISABLED` —— 它是 `is_enabled()` 唯一读的东西。
    """

    @classmethod
    def setUpClass(cls):
        cls.doc = _doc()

    def _off(self, *names):
        return mock.patch.object(layout_spec, '_DISABLED', set(names))

    def test_title_only_picks_an_enabled_layout(self):
        with self._off('statement', 'quote'):
            sl = pipeline._title_only('选择性同步的两套规则', '本章讲这两套规则')
            self.assertTrue(layout_spec.is_enabled(sl['layout']), sl['layout'])
        # 一套都没禁用时保持原样：一行大字本来就是 statement 的本职
        with self._off():
            self.assertEqual('statement', pipeline._title_only('标题')['layout'])

    def test_page_without_material_does_not_fall_back_to_a_disabled_layout(self):
        """**本页没有素材**正是这次的现场：没有内容可排时 `_heuristic_slide`
        直接返回 statement —— 候选集与启用清单都不看。"""
        page = dict(title='选择性同步：客户端与服务端双重规则')
        with self._off('statement', 'quote'):
            sl = pipeline._heuristic_slide(page, '04 同步共享安全', [],
                                          candidates=['comparison_rows'],
                                          summary='本章讲选择性同步的两套规则')
        self.assertNotIn(sl['layout'], ('statement', 'quote'))
        # 素材是真的没有，所以也不能编：能放上去的只有标题与本章 summary
        self.assertEqual('本章讲选择性同步的两套规则', sl.get('thesis'))
        self.assertEqual([], sl.get('points'))

    def test_normalise_plan_unknown_layout_uses_an_enabled_one(self):
        outline = dict(title='T', toc=[], sections=[dict(name='01 甲', summary='', pages=[
            dict(title='页一', hint='', source='S1')])])
        with self._off('statement', 'quote'):
            plan = pipeline._normalise_plan(
                [dict(layout='并不存在的版式', title='页一')], outline)
        sl = plan['slides'][0]
        self.assertNotIn(sl['layout'], ('statement', 'quote'))
        self.assertEqual('页一', sl['title'])        # 页眉照旧回填

    def test_fallback_plan_never_uses_a_disabled_layout(self):
        with mock.patch.object(config, 'llm_config', lambda: None):
            out = pipeline.make_outline(self.doc)
            with self._off('statement', 'quote'):
                plan = pipeline.make_plan(out, self.doc)
        used = {s['layout'] for s in plan['slides']}
        self.assertEqual(set(), used & {'statement', 'quote'}, sorted(used))

    def test_title_only_pages_render(self):
        """三档兜底页都要真的渲得出来（`_TITLE_ONLY_ORDER` 逐级禁用各渲一张）。

        几何回归覆盖不到它们：`FIXTURES` 里每套版式都按**满容量**写样例，
        而兜底页恰恰是空的（`executive_summary` 的 points 为空、`tinted_bands`
        只有一条带）—— 而它又是坏掉时最难看的那种页。
        """
        tpl = _template()
        if tpl is None:
            self.skipTest('模板文件不存在，跳过渲染回归')
        page = dict(title='选择性同步：客户端与服务端双重规则',
                    hint='', source='Source: 《白皮书》· 12')
        slides, banned = [], {'statement', 'quote'}
        for _ in range(len(pipeline._TITLE_ONLY_ORDER)):
            with mock.patch.object(layout_spec, '_DISABLED', set(banned)):
                sl = pipeline._title_only(page['title'], '本章讲选择性同步的两套规则')
            slides.append(pipeline._fill_header(
                sl, page, '04 同步共享安全'))
            banned.add(sl['layout'])
        tmp = tempfile.mkdtemp(prefix='pptgen-titleonly-')
        path = os.path.join(tmp, 'title-only.pptx')
        build.build(dict(slides=slides, toc=['04 同步共享安全']), tpl, path)
        rep = geometry.analyse(path)
        errs = [i for i in rep['issues'] if i['severity'] == 'error']
        self.assertEqual([], errs, geometry.format_report(rep))

    def test_resolve_never_returns_a_disabled_layout(self):
        """`resolve()` 是候选塌空时的最后一道 —— 它不能把禁用版式放回来。"""
        for role, intent in (('content', 'quote'), ('section', 'section')):
            with self._off('statement', 'quote', 'section_divider'):
                sp = layout_spec.resolve(None, role, intent)
                self.assertTrue(layout_spec.is_enabled(sp.name),
                                '%s/%s → %s' % (role, intent, sp.name))


class TestRenderAll(unittest.TestCase):
    """每套版式渲染一页 → build 自检 + 几何检查。"""

    @classmethod
    def setUpClass(cls):
        tpl = _template()
        if tpl is None:
            raise unittest.SkipTest('模板文件不存在，跳过渲染回归')
        cls.tpl = tpl
        cls.tmp = tempfile.mkdtemp(prefix='pptgen-layouts-')
        slides = [dict(spec) for _, spec in FIXTURES]
        cls.path = os.path.join(cls.tmp, 'layouts.pptx')
        build.build(dict(slides=slides, toc=['01 初识 Dify']), tpl, cls.path)
        cls.rep = geometry.analyse(cls.path)

    def test_fixtures_cover_every_layout(self):
        covered = {s['layout'] for _, s in FIXTURES}
        self.assertEqual(covered, set(layouts.LAYOUT_NAMES),
                         '有版式没有样例：%s' % (set(layouts.LAYOUT_NAMES) - covered))

    def test_no_geometry_errors(self):
        errs = [i for i in self.rep['issues'] if i['severity'] == 'error']
        self.assertEqual([], errs, geometry.format_report(self.rep))

    def test_no_text_overflow(self):
        """样例 spec 是贴着容量声明写的，溢出说明版式自身的框给小了。"""
        bad = [i for i in self.rep['issues']
               if i['kind'] in ('text_overflow', 'text_overlap')]
        self.assertEqual([], bad, geometry.format_report(self.rep))

    def test_no_silent_truncation(self):
        """样例的内容都该放得下 —— 出现截断说明框宽/框高估错了。

        `_fit()` 的截断会消除溢出，所以几何检查看不见它（历史案例：已删除的
        `node_flow` 8 个节点里 6 个被截成残句而报告全绿）。这条断言把静默截断
        变成可见的失败。
        """
        from pptx import Presentation
        prs = Presentation(self.path)
        for slide in list(prs.slides)[2:-1]:      # 去掉公司封面与封底
            for sh in slide.shapes:
                if sh.has_text_frame:
                    for p in sh.text_frame.paragraphs:
                        for r in p.runs:
                            self.assertNotIn('…', r.text,
                                             '出现截断：%r' % r.text)


class TestWrapAndFit(unittest.TestCase):
    """标题的折行原语：**该折行就折行，别截断**。

    背景：模型把源文档的「章名　—　副题」整串当章节名（30+ 字），分隔页 40pt
    的大字框放不下，早先被 `_fit()` 截成 `01 初识 Dify　—　什么是 Di…`。
    而大纲是**可以被用户改长的**，所以渲染层必须自己会折行。
    """

    def test_long_divider_title_wraps_instead_of_truncating(self):
        title = '01 初识 Dify　—　什么是 Dify · 设计初衷 · 九大核心理念'
        tokens.take_truncations()
        lines, size = tokens.fit_block(title, 7.73, 1.60, sizes=(40, 36, 32, 28, 24))
        self.assertEqual([], tokens.take_truncations(), '折行不该产生截断')
        self.assertLessEqual(len(lines), 2)
        # 折行只在断点处吃掉空格，文字本身一个都不能少
        self.assertEqual(title.replace(' ', ''), ''.join(lines).replace(' ', ''))
        for ln in lines:
            self.assertNotIn('…', ln)
            self.assertLessEqual(tokens.text_w_in(ln, size), 7.73)

    def test_short_title_stays_one_line(self):
        _, size = tokens.fit_block('01 初识 Dify', 7.73, 1.60,
                                   sizes=(40, 36, 32, 28, 24))
        self.assertEqual(40, size)

    def test_bigger_type_beats_fewer_lines(self):
        """**字号优先**：能在两行内放下就用最大的字号，不为了一行把字压小。

        14 个汉字在 40pt 下差 0.44" 放不进一行 —— 这时取「40pt 折两行」，
        而不是「26pt 挤一行」（分隔页要靠大字号压住版面）。
        """
        lines, size = tokens.fit_block('一二三四五六七八九十十一十二十三十四',
                                       7.73, 1.60, sizes=(40, 36, 32))
        self.assertEqual(40, size)
        self.assertEqual(2, len(lines))

    def test_line_never_starts_with_punctuation(self):
        """行首不挂避头标点（`、` `，` `·`）。标点宁可吊在上一行末尾。"""
        text = '从三类落地场景到四类应用形态、五项辅助能力与三档版本定价'
        for ln in tokens.wrap_lines(text, 7.73, 36):
            self.assertNotIn(ln[0], tokens._NO_LINE_START, ln)

    def test_latin_word_is_not_split(self):
        for ln in tokens.wrap_lines('平台对比 GPT-4o 与 langgenius/dify 的能力', 4.2, 20):
            self.assertNotIn('Dif\n', ln + '\n')
            self.assertFalse(ln.endswith('langge'), ln)

    def test_absurd_title_truncates_and_records(self):
        """阶梯全试完（用户把标题改到极端长度）才截断，而且要**记下来**。"""
        tokens.take_truncations()
        lines, _ = tokens.fit_block('标' * 200, 7.73, 1.60,
                                    sizes=(40, 36, 32, 28, 24))
        self.assertEqual(2, len(lines))
        self.assertTrue(lines[-1].endswith('…'))
        cuts = tokens.take_truncations()
        self.assertEqual(1, len(cuts))
        self.assertEqual('标' * 200, cuts[0][0], '记录里要有完整原文')


class TestCoverAndAgenda(unittest.TestCase):
    """封面填标题 + 日期，目录页填真正的目录 —— 都**不覆盖模板自己的版式**。

    `spec['title']` 从 pipeline 一路传到 build 却从来没人消费，成品第一页
    只有一个 MEVION 机器图；`fill_agenda` 则把标题写成 28pt `DARK`，
    压在同色的深灰通栏上几乎看不见。
    """

    @classmethod
    def setUpClass(cls):
        tpl = _template()
        if tpl is None:
            raise unittest.SkipTest('模板文件不存在，跳过渲染回归')
        cls.tpl = tpl
        cls.tmp = tempfile.mkdtemp(prefix='pptgen-cover-')
        slides = [dict(FIXTURES[0][1]), dict(FIXTURES[1][1])]

        def make(name, title, toc):
            path = os.path.join(cls.tmp, name + '.pptx')
            build.build(dict(slides=[dict(s) for s in slides], toc=toc,
                             title=title), tpl, path)
            return pptx.Presentation(path)

        cls.prs = make('normal', 'Dify 介绍与实战：从初识到落地',
                       ['01 初识 Dify —— 什么是 Dify、设计初衷与九大核心理念',
                        '02 为什么选 Dify —— 挑战与价值'])
        cls.long = make('long', '一个非常长的标题' * 3, ['01 很长的章节名' * 4])
        cls.absurd = make('absurd', '一个非常长的标题' * 6, [])
        cls.empty = make('empty', '', [])

    def _ph(self, prs, page, idx):
        for sh in prs.slides[page].shapes:
            if sh.is_placeholder and sh.placeholder_format.idx == idx:
                return sh
        return None

    def test_cover_gets_the_deck_title(self):
        tf = self._ph(self.prs, 0, 0).text_frame
        self.assertEqual('Dify 介绍与实战：从初识到落地',
                         tf.text.replace('\n', ''))

    def test_cover_subtitle_is_the_date(self):
        got = self._ph(self.prs, 0, 1).text_frame.text.strip()
        self.assertRegex(got, r'^\d{4} 年 \d{1,2} 月$')

    def test_long_cover_title_wraps_without_ellipsis(self):
        """24 字的标题在封面上折两行放得下 —— 完全不该出现省略号。"""
        tf = self._ph(self.long, 0, 0).text_frame
        self.assertEqual(2, len(tf.paragraphs))
        self.assertNotIn('…', tf.text)
        self.assertEqual('一个非常长的标题' * 3, tf.text.replace('\n', ''))
        self.assertEqual(32, tf.paragraphs[0].runs[0].font.size.pt)

    def test_absurd_cover_title_truncates_but_says_so(self):
        """48 字（远超封面能放下的量）才截断，而且**记录在案**，不是静默的。"""
        tf = self._ph(self.absurd, 0, 0).text_frame
        self.assertEqual(2, len(tf.paragraphs))
        self.assertTrue(tf.text.endswith('…'))

    def test_cover_keeps_the_template_typography(self):
        """只设字号，颜色/对齐/字体全部继承 layout（模板封面是深色居中大字）。"""
        for p in self._ph(self.prs, 0, 0).text_frame.paragraphs:
            for r in p.runs:
                self.assertIsNotNone(r.font.size)
                self.assertIsNone(r.font.color.rgb if r.font.color
                                  and r.font.color.type is not None else None)
        # 副标题一个字号都不该设：24pt 加粗浅灰是 layout 给的
        for p in self._ph(self.prs, 0, 1).text_frame.paragraphs:
            for r in p.runs:
                self.assertIsNone(r.font.size)

    def test_agenda_gets_the_toc(self):
        tf = self._ph(self.prs, 1, 0).text_frame
        self.assertEqual('目录', tf.text)
        body = self._ph(self.prs, 1, 12).text_frame
        self.assertEqual(['01 初识 Dify —— 什么是 Dify、设计初衷与九大核心理念',
                          '02 为什么选 Dify —— 挑战与价值'],
                         [p.text for p in body.paragraphs])

    def test_agenda_keeps_the_template_typography(self):
        """目录标题（深灰通栏上的 40pt 白字）与条目（24pt + 品牌红圆点）
        都来自 layout —— 一个显式字号都不该有，否则就会重演「深色压深色」。"""
        for idx in (0, 12):
            for p in self._ph(self.prs, 1, idx).text_frame.paragraphs:
                for r in p.runs:
                    self.assertIsNone(r.font.size, '不应覆盖模板字号')
                    self.assertIsNone(r.font.color.rgb if r.font.color
                                      and r.font.color.type is not None else None)

    def test_template_sample_text_never_survives(self):
        """目录为空也要清空占位符 —— 否则成品上留着「议题一/议题二/议题三」。"""
        for idx in (0, 12):
            got = self._ph(self.empty, 1, idx).text_frame.text
            for junk in ('议题', '会议议程', 'Agenda Items'):
                self.assertNotIn(junk, got)

    def test_no_title_leaves_the_cover_alone(self):
        """spec 里没有 title（`--content <老模块>` 那条路径）就别动封面。"""
        self.assertEqual('', self._ph(self.empty, 0, 0).text_frame.text)


class TestTruncationIsReported(unittest.TestCase):
    """截断记录要真的被收走 —— 早先 `take_truncations()` 全仓库无人调用，
    「记下来写进日志」是一句假注释，长驻进程里还会跨 job 累积。"""

    def test_build_reports_truncations_through_on_log(self):
        tpl = _template()
        if tpl is None:
            raise unittest.SkipTest('模板文件不存在')
        tmp = tempfile.mkdtemp(prefix='pptgen-trunc-')
        # 来源行走 `fit_one_line`（单行定高 0.30"），长到一定程度必然被截断。
        spec = dict(FIXTURES[1][1])
        spec['source'] = 'Source: 《' + '很长的来源说明' * 8 + '》'
        seen = []
        build.build(dict(slides=[spec], toc=[]), tpl,
                    os.path.join(tmp, 'x.pptx'), on_log=seen.append)
        self.assertTrue(any('截断' in m for m in seen), seen)
        self.assertTrue(any('很长的来源说明' in m for m in seen), seen)


class TestCustomLayouts(unittest.TestCase):
    """**自定义版式的回归网**。

    内置 19 套有 `TestRenderAll` 那份 FIXTURES 兜着，自定义版式没有 —— 它们的
    渲染质量只由「入库那一刻的试片闸门」保证过一次。手工改过 JSON、或者从别处
    拷来一份版式之后，就没有任何东西再检查它们了。

    所以这里把同一道闸门搬进回归网：**每套启用的自定义版式，拿它自己的样例
    渲一页，过几何 + 截断检查**。它红了说明版式库里有坏东西 —— 与 `run.py
    layouts --check` 是同一套判据（那边给人在命令行上用）。
    """

    @classmethod
    def setUpClass(cls):
        cls.metas = [m for m in layout_store.list_metas()
                     if not m.get('_problems')
                     and isinstance(m.get('sample'), dict)
                     and m.get('blocks')]

    def test_no_broken_meta_files(self):
        """坏掉的声明文件要在这里现形，而不是等到生成时才炸。"""
        broken = [m.get('name') for m in layout_store.list_metas()
                  if m.get('_problems')]
        self.assertEqual(broken, [], '这些自定义版式有问题：%s' % broken)

    def test_custom_layouts_are_registered(self):
        for m in self.metas:
            self.assertIn(m['name'], layout_spec.names(),
                          '%s 在目录里但没注册进版式库' % m['name'])

    def test_every_custom_sample_passes_the_gate(self):
        tpl = _template()
        if tpl is None:
            raise unittest.SkipTest('模板文件不存在，跳过渲染回归')
        if not self.metas:
            self.skipTest('版式库里还没有自定义版式')
        tmp = tempfile.mkdtemp(prefix='pptgen-custom-')
        bad = []
        for m in self.metas:
            if not layout_spec.is_enabled(m['name']):
                continue                       # 禁用的不查：它不进候选，也不该拦
            trial = recognize.render_trial(m, os.path.join(tmp, m['name']),
                                           template=tpl)
            problems = recognize.gate(trial)
            if problems:
                bad.append('%s：%s' % (m['name'], '；'.join(problems[:2])))
        self.assertEqual(bad, [], '自定义版式的样例没通过试片闸门：\n' + '\n'.join(bad))


if __name__ == '__main__':
    unittest.main()
