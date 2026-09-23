# -*- coding: utf-8 -*-
"""按页修订的纯逻辑：页码映射与 deck 指纹。

页码映射是这个功能里**最容易出错、错了又最贵**的一处：用户对着预览图说「第 5 页」，
而 `slides[]` 的下标是 `5 - 3`，中间隔着封面、目录、封底，还混着章节分隔页。
映射只实现一次（`revise.build_page_index`），这里把它钉死。
"""
import copy
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))

from pptgen import build, revise                                 # noqa: E402


def _deck(n_slides, *, dividers_at=(), title='一份 deck', toc=None, **extra):
    """造一份 deck：第 i 页（0 基）在 `dividers_at` 里就是章节分隔页。"""
    slides = []
    for i in range(n_slides):
        if i in dividers_at:
            slides.append(dict(layout='section_divider', num='%02d' % (i + 1),
                               title='第 %d 章' % (i + 1), lead='导语'))
        else:
            slides.append(dict(layout='numbered_columns', title='页标题 %d' % (i + 1),
                               kicker='01 初识 Dify', columns=3,
                               items=[dict(name='名', desc='说明')],
                               source='Source: 《x》§1'))
    d = dict(slides=slides, toc=toc if toc is not None else ['01 初识 Dify'], title=title)
    d.update(extra)
    return d


class TestPageIndex(unittest.TestCase):
    def test_预览序号与slides下标差三(self):
        idx = revise.build_page_index(_deck(15))
        self.assertEqual(len(idx), 18)          # 15 页正文 + 封面 + 目录 + 封底
        by_preview = {e['preview']: e for e in idx}

        self.assertEqual(by_preview[1]['kind'], 'cover')
        self.assertEqual(by_preview[2]['kind'], 'toc')
        self.assertEqual(by_preview[18]['kind'], 'back')
        # 第 3 张 = slides[0]，最后一张正文 = slides[14]（预览第 17 张）
        self.assertEqual(by_preview[3]['slide_index'], 0)
        self.assertEqual(by_preview[17]['slide_index'], 14)
        # 模板页没有 slide_index —— 它们不在 slides[] 里，改的是 deck 顶层字段
        self.assertIsNone(by_preview[1]['slide_index'])
        self.assertIsNone(by_preview[2]['slide_index'])
        self.assertIsNone(by_preview[18]['slide_index'])

    def test_正文序号跳过章节分隔页(self):
        # slides[0] 与 slides[3] 是分隔页
        idx = revise.build_page_index(_deck(6, dividers_at=(0, 3)))
        got = [(e['preview'], e['kind'], e['label']) for e in idx]
        self.assertEqual(got[:7], [
            (1, 'cover', '预览 01 · 封面'),
            (2, 'toc', '预览 02 · 目录'),
            (3, 'divider', '预览 03 · 章节分隔'),
            (4, 'body', '预览 04 · 正文 01'),
            (5, 'body', '预览 05 · 正文 02'),
            (6, 'divider', '预览 06 · 章节分隔'),
            (7, 'body', '预览 07 · 正文 03'),
        ])
        # 正文序号只数正文页：6 页 slide、2 页分隔 → 4 页正文，最后一张是封底
        self.assertEqual(idx[-1]['kind'], 'back')
        self.assertEqual(idx[-1]['preview'], 9)

    def test_模板页带上它们真正可改的东西(self):
        idx = revise.build_page_index(_deck(2, title='我的标题', toc=['01 A', '02 B']))
        cover, toc_page = idx[0], idx[1]
        # 封面的 title / 目录的 toc 都不在 slides[] 里 —— 不显式给出来，
        # 模型就只能猜，而这两个字段在 deck 顶层。
        self.assertEqual(cover['fields'], ['title', 'subtitle'])
        self.assertEqual(cover['title'], '我的标题')
        self.assertEqual(toc_page['fields'], ['toc'])
        self.assertEqual(toc_page['toc'], ['01 A', '02 B'])

    def test_字段清单剔掉layout与build写入的page(self):
        d = _deck(1)
        d['slides'][0]['page'] = 3          # build.build 原地写进去的
        e = revise.build_page_index(d)[2]
        self.assertIn('items', e['fields'])
        self.assertNotIn('layout', e['fields'])     # 版式不走字段补丁
        self.assertNotIn('page', e['fields'])       # 那是渲染时写的页码，不是内容

    def test_statement没有标题位_字段清单不撒谎(self):
        d = _deck(1)
        d['slides'][0] = dict(layout='statement', kicker='01 初识 Dify',
                              lines=[[['把复杂流程留给平台。', {}]]],
                              body=['支撑段一', '支撑段二'])
        e = revise.build_page_index(d)[2]
        self.assertNotIn('title', e['fields'])      # statement 会被 pop('title')
        self.assertIn('lines', e['fields'])
        # 那一页在界面上是靠 lines 里的大字立住的，headline 必须能取到它，
        # 否则面板上是一片空白、用户不知道该怎么说
        self.assertEqual(e['headline'], '把复杂流程留给平台。')

    def test_headline_优先标题(self):
        d = _deck(1)
        e = revise.build_page_index(d)[2]
        self.assertEqual(e['headline'], '页标题 1')

    def test_空deck只剩三张模板页(self):
        idx = revise.build_page_index(dict(slides=[], toc=[], title='空'))
        self.assertEqual([e['preview'] for e in idx], [1, 2, 3])
        self.assertEqual([e['kind'] for e in idx], ['cover', 'toc', 'back'])


class TestDeckFingerprint(unittest.TestCase):
    def test_忽略build写入的page键(self):
        a = _deck(3)
        b = _deck(3)
        for i, sl in enumerate(b['slides']):
            sl['page'] = i + 3              # 只有这一处不同
        self.assertEqual(revise.deck_fingerprint(a), revise.deck_fingerprint(b))

    def test_忽略下划线开头的顶层键(self):
        a = _deck(3)
        b = _deck(3, _generated_by='llm', _warnings=['x'])
        self.assertEqual(revise.deck_fingerprint(a), revise.deck_fingerprint(b))

    def test_内容变了指纹就变(self):
        a = _deck(3)
        b = _deck(3)
        b['slides'][1]['title'] = '改过的标题'
        self.assertNotEqual(revise.deck_fingerprint(a), revise.deck_fingerprint(b))

    def test_顶层字段也算进去(self):
        self.assertNotEqual(revise.deck_fingerprint(_deck(2, title='甲')),
                            revise.deck_fingerprint(_deck(2, title='乙')))
        self.assertNotEqual(revise.deck_fingerprint(_deck(2, toc=['a'])),
                            revise.deck_fingerprint(_deck(2, toc=['b'])))

    def test_键序不影响指纹(self):
        a = _deck(2)
        b = dict(a)
        b['slides'] = [dict(reversed(list(s.items()))) for s in a['slides']]
        self.assertEqual(revise.deck_fingerprint(a), revise.deck_fingerprint(b))


class TestTexts(unittest.TestCase):
    def test_三种富文本写法都认得(self):
        # layouts._runs 容错的三种写法，JSON 里都可能是 list
        self.assertEqual(revise.texts([['甲', {}], ['乙', {'hl': True}]]), ['甲', '乙'])
        self.assertEqual(revise.texts([[['甲', {}], ['乙', {}]]]), ['甲', '乙'])
        self.assertEqual(revise.texts([{'text': '甲', 'hl': True}]), ['甲'])

    def test_样式键不算内容(self):
        self.assertEqual(revise.texts({'hl': True, 'size': 13, 'bold': False}), [])

    def test_空与空白被丢掉(self):
        self.assertEqual(revise.texts(['', '  ', None, 3]), [])


class TestPerPageRenderFingerprint(unittest.TestCase):
    """按页渲染指纹 —— 增量渲染的**正确性**所在。

    渲染一页要 8–10 秒（`config.py:142`），所以「谁该重渲」的判断被做成了
    纯函数 `_render_plan`：只有它是纯的，才能在这里全覆盖，而不是靠肉眼验收。
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, ROOT)
        import server                                        # noqa: PLC0415
        cls.s = server

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = self.tmp.name

    def _touch(self, pages):
        for pg in pages:
            with open(os.path.join(self.work, 'page-%02d.png' % pg), 'wb') as f:
                f.write(b'png')

    # ── _page_payload / _page_digest ─────────────────────────
    def test_每一页的像素由什么决定(self):
        d = _deck(2, title='甲', toc=['01 A'])
        n = 5                       # 2 页正文 + 封面 + 目录 + 封底
        self.assertEqual(self.s._page_payload(d, 1, n)['cover'], '甲')
        self.assertEqual(self.s._page_payload(d, 2, n), {'toc': ['01 A']})
        self.assertEqual(self.s._page_payload(d, 3, n)['title'], '页标题 1')
        self.assertEqual(self.s._page_payload(d, 4, n)['title'], '页标题 2')
        self.assertEqual(self.s._page_payload(d, 5, n), {'back': True})

    def test_封面副标题用生效后的兜底值(self):
        # deck 里通常没有 subtitle，build.fill_cover 会兜底成 today_cn()。
        # 指纹必须跟着这个**生效值**走，否则兜底值一变，封面内容变了而指纹没变。
        d = _deck(1)
        self.assertEqual(self.s._page_payload(d, 1, 4)['subtitle'], build.today_cn())
        self.assertEqual(
            self.s._page_digest(d, 1, 4, 's'),
            self.s._page_digest(dict(d, subtitle=build.today_cn()), 1, 4, 's'))

    def test_build写入的page不进指纹(self):
        # build.build 会原地写 sl['page']（build.py:194）：跑过一次 build 的内存
        # deck 有它、刚 load 的盘上那份可能没有。它是派生值，进指纹就会让同内容的
        # 两份 spec 算出不同指纹、白渲一整遍。
        a, b = _deck(2), _deck(2)
        for i, sl in enumerate(b['slides']):
            sl['page'] = i + 3
        self.assertEqual(self.s._page_digest(a, 3, 5, 's'),
                         self.s._page_digest(b, 3, 5, 's'))

    def test_改了内容指纹就变_没改的不变(self):
        a, b = _deck(2), _deck(2)
        b['slides'][0]['title'] = '改过的标题'
        self.assertNotEqual(self.s._page_digest(a, 3, 5, 's'),
                            self.s._page_digest(b, 3, 5, 's'))
        # 没动的那一页指纹必须不变 —— 这是「只重渲一页」成立的前提
        self.assertEqual(self.s._page_digest(a, 4, 5, 's'),
                         self.s._page_digest(b, 4, 5, 's'))

    def test_分辨率进指纹(self):
        d = _deck(1)
        self.assertNotEqual(self.s._page_digest(d, 3, 4, '1920x1080'),
                            self.s._page_digest(d, 3, 4, '1280x720'))

    # ── _render_plan ─────────────────────────────────────────
    def test_没有清单就全部渲(self):
        self._touch([1, 2, 3])
        plan = self.s._render_plan(self.work, {1: 'a', 2: 'b', 3: 'c'}, {},
                                   sig='s', template_sha='t')
        self.assertEqual(plan['render'], [1, 2, 3])
        self.assertEqual(plan['keep'], [])
        self.assertTrue(plan['stale_contact_sheet'])

    def test_只重渲变了的页(self):
        self._touch([1, 2, 3, 4, 5])
        man = dict(sig='s', template_sha='t',
                   pages={'1': 'a', '2': 'b', '3': 'c', '4': 'd', '5': 'e'})
        plan = self.s._render_plan(
            self.work, {1: 'a', 2: 'b', 3: '变了', 4: 'd', 5: 'e'}, man,
            sig='s', template_sha='t')
        self.assertEqual(plan['render'], [3])
        self.assertEqual(sorted(plan['keep']), ['page-01.png', 'page-02.png',
                                                'page-04.png', 'page-05.png'])
        self.assertEqual(plan['drop'], [])

    def test_什么都没变就一张都不渲(self):
        self._touch([1, 2, 3])
        man = dict(sig='s', template_sha='t', pages={'1': 'a', '2': 'b', '3': 'c'})
        plan = self.s._render_plan(self.work, {1: 'a', 2: 'b', 3: 'c'}, man,
                                   sig='s', template_sha='t')
        self.assertEqual(plan['render'], [])
        # 一张都没变 → 联系表仍然可信，别白删（重出它要 30 多秒）
        self.assertFalse(plan['stale_contact_sheet'])

    def test_图丢了就要重渲(self):
        self._touch([1, 2])                     # 第 3 页的图被删了
        man = dict(sig='s', template_sha='t', pages={'1': 'a', '2': 'b', '3': 'c'})
        plan = self.s._render_plan(self.work, {1: 'a', 2: 'b', 3: 'c'}, man,
                                   sig='s', template_sha='t')
        self.assertEqual(plan['render'], [3])

    def test_页数变少就删掉多出来的旧图(self):
        # 15 页 → 3 页：page-13..15 会留在页面上，用户按预览页码反馈时又指错页
        # —— 这正是 .pptx-stamp 时代那次事故的形态。
        self._touch([1, 2, 13, 14, 15])
        man = dict(sig='s', template_sha='t',
                   pages={str(p): 'x' for p in (1, 2, 13, 14, 15)})
        plan = self.s._render_plan(self.work, {1: 'x', 2: 'x'}, man,
                                   sig='s', template_sha='t')
        self.assertEqual(plan['drop'], ['page-13.png', 'page-14.png', 'page-15.png'])
        self.assertTrue(plan['stale_contact_sheet'])

    def test_换了分辨率或模板就全部作废(self):
        self._touch([1, 2, 3])
        man = dict(sig='1920x1080', template_sha='t',
                   pages={'1': 'a', '2': 'b', '3': 'c'})
        for kw in (dict(sig='1280x720', template_sha='t'),        # 改了分辨率
                   dict(sig='1920x1080', template_sha='换过')):    # 换了模板
            plan = self.s._render_plan(self.work, {1: 'a', 2: 'b', 3: 'c'}, man, **kw)
            self.assertEqual(plan['render'], [1, 2, 3], kw)

    def test_目录不存在也算全部过期(self):
        plan = self.s._render_plan(os.path.join(self.work, '还没建'), {1: 'a'}, {},
                                   sig='s', template_sha='t')
        self.assertEqual(plan['render'], [1])

    def test_对比快照不会被当成页码图(self):
        # page-05-rv1234.png 是「改前」快照：既不能算进 have（否则页码对不上），
        # 更不能被 drop 删掉 —— 删了就再也拿不回改前那一帧。
        # 老代码那句 `f.startswith('page-')` 正好会把它删掉。
        self._touch([1, 2])
        with open(os.path.join(self.work, 'page-01-rv9.png'), 'wb') as f:
            f.write(b'png')
        man = dict(sig='s', template_sha='t', pages={'1': 'a', '2': 'b'})
        plan = self.s._render_plan(self.work, {1: 'a', 2: 'b'}, man,
                                   sig='s', template_sha='t')
        self.assertEqual(plan['render'], [])
        self.assertEqual(plan['drop'], [])

    def test_清单能存能读(self):
        self.s._save_manifest(self.work, dict(sig='s', pages={'1': 'a'}, deck='x'))
        self.assertEqual(self.s._load_manifest(self.work)['pages'], {'1': 'a'})
        # 坏了 / 不是对象 → 当「全部过期」，自愈
        with open(os.path.join(self.work, self.s.MANIFEST), 'w', encoding='utf-8') as f:
            f.write('[1, 2]')
        self.assertEqual(self.s._load_manifest(self.work), {})
        with open(os.path.join(self.work, self.s.MANIFEST), 'w', encoding='utf-8') as f:
            f.write('{ 坏 json')
        self.assertEqual(self.s._load_manifest(self.work), {})


class TestPathsOf(unittest.TestCase):
    """模式 A 的路径白名单 —— 「版式不变」靠它成为结构上不可能。"""

    def setUp(self):
        self.cols = dict(layout='numbered_columns', title='页标题', kicker='01 甲章',
                         columns=3, source='Source: 《x》§1',
                         items=[dict(name='名一', desc='说明一'),
                                dict(name='名二', desc='说明二')])
        self.stmt = dict(layout='statement', kicker='01 甲章',
                         lines=[[['大字一', {}]], [['大字二', {'hl': True}]]],
                         body=['段落一', '段落二'])

    def test_对象列表逐条逐字段可寻址(self):
        t = revise.paths_of(self.cols)
        self.assertEqual(t['title'], 'str')
        self.assertEqual(t['columns'], 'num')
        self.assertEqual(t['items'], 'objlist')
        self.assertEqual(t['items[1]'], 'obj')
        self.assertEqual(t['items[1].desc'], 'str')
        self.assertEqual(t['items[1].name'], 'str')

    def test_layout与page不在表里(self):
        # 「版式不变」不是靠事后比对，而是这两个路径根本不存在（见 check_ops）
        t = revise.paths_of(dict(self.cols, page=3))
        self.assertNotIn('layout', t)
        self.assertNotIn('page', t)

    def test_statement没有title位_且不下钻到run(self):
        t = revise.paths_of(self.stmt)
        self.assertNotIn('title', t)
        self.assertEqual(t['lines'], 'rows')
        self.assertEqual(t['lines[0]'], 'row')
        self.assertEqual(t['body'], 'strlist')
        self.assertEqual(t['body[0]'], 'str')
        # 半个 run（lines[0][0]）会破坏富文本 —— 只开放到「整行」这一层
        self.assertNotIn('lines[0][0]', t)

    def test_标量子字段可寻址_嵌套列表不可(self):
        t = revise.paths_of(dict(layout='stat_hero', title='t',
                                 hero=dict(num='87', unit=' 亿美元'),
                                 stats=[dict(num='3', label='档方案')]))
        self.assertEqual(t['hero.num'], 'str')
        self.assertEqual(t['stats[0].label'], 'str')
        # 表格行是 list[list]，同样只到整行
        t2 = revise.paths_of(dict(layout='data_table', header=['a', 'b'],
                                  rows=[['x', 'y'], ['p', 'q']]))
        self.assertEqual(t2['rows'], 'rows')
        self.assertEqual(t2['rows[1]'], 'row')
        self.assertNotIn('rows[1][0]', t2)


class TestSplitPath(unittest.TestCase):
    def test_往返(self):
        for path, parts in (('title', ['title']),
                            ('items[2].desc', ['items', 2, 'desc']),
                            ('hero.num', ['hero', 'num']),
                            ('lines[0]', ['lines', 0])):
            self.assertEqual(revise.split_path(path), parts)
            self.assertEqual(revise.join_path(parts), path)

    def test_不合法写法抛错(self):
        # 字段名不限 ASCII（中文子字段要落到 _missing_hint 去解释），
        # 但结构上残缺的写法必须当场抛错
        for bad in ('', 'items[0', 'a..b', '[0]', 'items[x]', '.'):
            with self.assertRaises(ValueError, msg=bad):
                revise.split_path(bad)

    def test_取值与写值(self):
        spec = dict(items=[dict(name='甲', desc='乙')])
        parts = revise.split_path('items[0].desc')
        self.assertEqual(revise.get_path(spec, parts), '乙')
        revise.set_path(spec, parts, '丙')
        self.assertEqual(spec['items'][0]['desc'], '丙')
        with self.assertRaises(KeyError):
            revise.get_path(spec, revise.split_path('items[9].desc'))


class TestCheckOps(unittest.TestCase):
    """三档护栏里**便宜的那两档**：不调模型、不构建，先拒掉明显错误。"""

    def setUp(self):
        self.cols = dict(layout='numbered_columns', title='页标题', kicker='01 甲章',
                         columns=3, items=[dict(name='名一', desc='说明一'),
                                           dict(name='名二', desc='说明二')])
        self.stmt = dict(layout='statement', kicker='01 甲章',
                         lines=[[['把复杂留给平台。', {}]]], body=['段落一'])

    def _op(self, path, value, expect=None):
        op = dict(path=path, value=value)
        if expect is not None:
            op['expect'] = expect
        return op

    def test_合法补丁通过(self):
        level, why = revise.check_ops(self.cols, [
            self._op('title', '新标题', '页标题')])
        self.assertEqual((level, why), ('ok', ''))

    def test_没回带现值只是提醒(self):
        level, why = revise.check_ops(self.cols, [self._op('title', '新标题')])
        self.assertEqual(level, 'warn')
        self.assertIn('现值', why)

    def test_现值为空时缺expect不算问题(self):
        # deck 里从来没有副标题（`_normalise_plan` 早先不回吐它）→ 没有可核对的
        # 东西，不该报一句用户看不懂的「没回带现值」。封面重做天天走这条路。
        level, why = revise.check_ops(
            {'title': '', 'subtitle': ''},
            [dict(path='subtitle', value='2026 年 9 月')])
        self.assertEqual((level, why), ('ok', ''))

    def test_现值对不上就拒(self):
        # 这是模式 A 最危险的失败：模型把 items[2] 按 1 基理解、指着第 2 条，
        # 补丁本身完全合法、会静默落盘、改错条目。只有比对现值能拦住。
        level, why = revise.check_ops(self.cols, [
            self._op('items[1].desc', '新说明', '说明一')])
        self.assertEqual(level, 'reject')
        self.assertIn('现值', why)
        self.assertIn('说明二', why)

    def test_现值模糊比对_忽略空白与标点(self):
        level, _ = revise.check_ops(self.cols, [
            self._op('items[0].desc', '新说明', '  说明一。 ')])
        self.assertEqual(level, 'ok')

    def test_改layout被拒并指向整页重做(self):
        level, why = revise.check_ops(self.cols, [self._op('layout', 'statement')])
        self.assertEqual(level, 'reject')
        self.assertIn('整页重做', why)

    def test_statement上改标题_说清该怎么提(self):
        level, why = revise.check_ops(self.stmt, [
            self._op('title', '新标题', '把复杂留给平台。')])
        self.assertEqual(level, 'reject')
        self.assertIn('没有标题位', why)
        self.assertIn('大字', why)

    def test_拆太深被拒并指出只能改到哪一层(self):
        level, why = revise.check_ops(self.stmt, [
            self._op('lines[0][0]', '新大字', '把复杂留给平台。')])
        self.assertEqual(level, 'reject')
        self.assertIn('lines[0]', why)      # 告诉它只能改到整行
        self.assertIn('只能改到', why)

    def test_序号越界被拒并给出条数(self):
        level, why = revise.check_ops(self.cols, [
            self._op('items[9].desc', '新说明', '说明一')])
        self.assertEqual(level, 'reject')
        self.assertIn('只有 2 条', why)

    def test_子字段写错被拒并列出现有子字段(self):
        level, why = revise.check_ops(self.cols, [
            self._op('items[0].标题', 'x', '名一')])
        self.assertEqual(level, 'reject')
        self.assertIn('name', why)
        self.assertIn('desc', why)

    def test_类型不符被拒(self):
        level, why = revise.check_ops(self.cols, [
            self._op('items', '一整段文字', None)])
        self.assertEqual(level, 'reject')
        self.assertIn('一组条目', why)
        # columns 是数字，不能换成文字
        level, why = revise.check_ops(self.cols, [self._op('columns', '三', 3)])
        self.assertEqual(level, 'reject')
        self.assertIn('一个数字', why)

    def test_不允许改成空串(self):
        # 那一格空着会渲染成一片空白，而 large_empty_area 只是 warn 级
        level, why = revise.check_ops(self.cols, [self._op('title', '  ', '页标题')])
        self.assertEqual(level, 'reject')
        self.assertIn('空', why)

    def test_缺value被拒(self):
        level, why = revise.check_ops(self.cols, [dict(path='title', expect='页标题')])
        self.assertEqual(level, 'reject')
        self.assertIn('没有给出新值', why)

    def test_路径写法不合法被拒(self):
        level, why = revise.check_ops(self.cols, [self._op('items[0', 'x', None)])
        self.assertEqual(level, 'reject')
        self.assertIn('路径写法', why)

    def test_富文本整行替换以文本比对现值(self):
        # lines[0] 是 [[片段,样式]]，flat_text 要能把它压成一句可比的文本
        level, why = revise.check_ops(self.stmt, [
            dict(path='lines[0]', value=[[['新的大字。', {'hl': True}]]],
                 expect='把复杂留给平台。')])
        self.assertEqual((level, why), ('ok', ''))


class TestApplyOps(unittest.TestCase):
    def test_不动原对象(self):
        spec = dict(layout='numbered_columns', title='旧', items=[dict(name='甲')])
        out = revise.apply_ops(spec, [dict(path='title', value='新'),
                                      dict(path='items[0].name', value='乙')])
        self.assertEqual(out['title'], '新')
        self.assertEqual(out['items'][0]['name'], '乙')
        self.assertEqual(spec['title'], '旧')              # 原件没被改
        self.assertEqual(spec['items'][0]['name'], '甲')

    def test_diff是逐路径的(self):
        spec = dict(layout='statement', lines=[[['旧大字', {}]]])
        ops = [dict(path='lines[0]', value=[[['新大字', {}]]], why='压短')]
        rows = revise.diff_ops(spec, ops)
        self.assertEqual(rows[0]['before'], '旧大字')
        self.assertEqual(rows[0]['after'], '新大字')
        self.assertEqual(rows[0]['why'], '压短')
        self.assertEqual(rows[0]['path'], 'lines[0]')

    def test_多条依次叠加(self):
        # 同一页两条意见按顺序叠加：第二条对着第一条的结果走
        spec = dict(layout='numbered_columns', title='甲',
                    items=[dict(name='名一', desc='说明一')])
        ops = [dict(path='title', value='乙'), dict(path='items[0].desc', value='说明二')]
        out = revise.apply_ops(spec, ops)
        self.assertEqual((out['title'], out['items'][0]['desc']), ('乙', '说明二'))


class TestCatalogFor(unittest.TestCase):
    def test_只给一个版式的契约(self):
        from pptgen import layout_spec
        txt = layout_spec.catalog_for('numbered_columns')
        self.assertIn('numbered_columns', txt)
        self.assertIn('items', txt)
        self.assertIn('必填', txt)
        # 只发这一套 —— 不能把整个版式库的目录（5000 多字符）全带上
        self.assertLess(len(txt), 600)
        self.assertNotIn('metric_trend', txt)

    def test_未知版式返回空串(self):
        from pptgen import layout_spec
        self.assertEqual(layout_spec.catalog_for('node_flow'), '')


class TestPageSpec(unittest.TestCase):
    def setUp(self):
        self.deck = _deck(2, title='原标题', toc=['01 甲章'], subtitle='2026 年 9 月')

    def test_封面与目录的可改字段在deck顶层(self):
        self.assertEqual(revise.page_spec(self.deck, 1),
                         {'title': '原标题', 'subtitle': '2026 年 9 月'})
        self.assertEqual(revise.page_spec(self.deck, 2), {'toc': ['01 甲章']})

    def test_正文页拿到那一页自己(self):
        got = revise.page_spec(self.deck, 3)
        self.assertEqual(got['title'], '页标题 1')
        self.assertIs(got, self.deck['slides'][0])      # 是它本身，不是副本

    def test_封底没有可改内容(self):
        self.assertEqual(revise.page_spec(self.deck, 5), {})

    def test_越界返回空(self):
        self.assertEqual(revise.page_spec(self.deck, 99), {})


class TestCommitPage(unittest.TestCase):
    def setUp(self):
        self.deck = _deck(2, title='原标题', toc=['01 甲章'])

    def test_封面标题要同步回大纲(self):
        # 封面 title 派生自 outline['title']（pipeline.py:1535）——
        # 只改 deck 的话，下次重新规划就没了
        got = revise.commit_page(self.deck, 1, {'title': '新标题', 'subtitle': 'x'})
        self.assertEqual(self.deck['title'], '新标题')
        fields = {p['field'] for p in got}
        self.assertIn('cover.title', fields)
        self.assertIn('cover.subtitle', fields)

    def test_目录条目过宽度夹取并同步回大纲(self):
        long = '零' * 80
        got = revise.commit_page(self.deck, 2, {'toc': [long]})
        from pptgen import pipeline
        self.assertEqual(self.deck['toc'], [pipeline._toc_line(long)])
        self.assertLess(len(self.deck['toc'][0]), len(long))
        self.assertEqual([p['field'] for p in got], ['toc'])

    def test_正文页整页替换不做键合并(self):
        # 换版式后旧版式的键必须一起丢：留着会让 repair._text_len 把它们算进
        # 长度、触发莫名其妙的压文案
        old = dict(self.deck['slides'][0])
        self.assertIn('items', old)
        revise.commit_page(self.deck, 3, dict(layout='statement', lines=[[['甲', {}]]]))
        self.assertEqual(self.deck['slides'][0],
                         dict(layout='statement', lines=[[['甲', {}]]]))
        self.assertNotIn('items', self.deck['slides'][0])

    def test_正文页不需要同步大纲(self):
        self.assertEqual(revise.commit_page(self.deck, 3, dict(self.deck['slides'][0])), [])

    def test_封底写不动(self):
        before = repr(self.deck)
        revise.commit_page(self.deck, 5, {'x': 1})
        self.assertEqual(repr(self.deck), before)


class TestApplyRevision(unittest.TestCase):
    def setUp(self):
        self.deck = _deck(2, title='原标题', toc=['01 甲章'])

    def test_微调一条正文页(self):
        out, rep = revise.apply_revision(self.deck, [dict(
            preview=3, request='标题压短',
            ops=[dict(path='title', value='新标题', expect='页标题 1')])])
        self.assertEqual(out['slides'][0]['title'], '新标题')
        self.assertEqual(rep['items'][0]['status'], 'ok')
        self.assertEqual(rep['items'][0]['changes'][0]['before'], '页标题 1')
        # 原 deck 不动
        self.assertEqual(self.deck['slides'][0]['title'], '页标题 1')

    def test_一条被拒不拖累其余(self):
        out, rep = revise.apply_revision(self.deck, [
            dict(preview=3, ops=[dict(path='items[9].desc', value='x', expect='y')]),
            dict(preview=4, ops=[dict(path='title', value='改好了', expect='页标题 2')]),
        ])
        self.assertEqual(rep['items'][0]['status'], 'reject')
        self.assertIn('只有 1 条', rep['items'][0]['reason'])
        self.assertEqual(rep['items'][1]['status'], 'ok')
        self.assertEqual(out['slides'][1]['title'], '改好了')
        # 被拒的那页原样保留
        self.assertEqual(out['slides'][0], self.deck['slides'][0])

    def test_封面微调带上大纲补丁(self):
        out, rep = revise.apply_revision(self.deck, [dict(
            preview=1, ops=[dict(path='title', value='新封面标题', expect='原标题')])])
        self.assertEqual(rep['items'][0]['status'], 'ok')
        self.assertEqual(out['title'], '新封面标题')
        self.assertEqual(rep['outline'][0]['field'], 'cover.title')

    def test_封底被拒(self):
        out, rep = revise.apply_revision(self.deck, [dict(
            preview=5, ops=[dict(path='title', value='x', expect=None)])])
        self.assertEqual(rep['items'][0]['status'], 'reject')
        self.assertIn('封底', rep['items'][0]['reason'])

    def test_页码越界被拒并给出总数(self):
        out, rep = revise.apply_revision(self.deck, [dict(
            preview=42, ops=[dict(path='title', value='x', expect=None)])])
        self.assertEqual(rep['items'][0]['status'], 'reject')
        self.assertIn('只有 5 张预览图', rep['items'][0]['reason'])

    def test_没说第几页被拒(self):
        out, rep = revise.apply_revision(self.deck, [dict(ops=[])])
        self.assertEqual(rep['items'][0]['status'], 'reject')
        self.assertIn('第几页', rep['items'][0]['reason'])

    def test_整页重做的条目不能按微调应用(self):
        # 用户在方案出来之后拨了模式开关：这一条只有整页内容、没有逐字段补丁，
        # 照旧按微调应用会「什么都没改，却报成已改动」，还顺手写进台账
        out, rep = revise.apply_revision(self.deck, [dict(
            preview=3, request='重排',
            new=dict(layout='statement', lines=[[['甲', {}]]]))])
        self.assertEqual(rep['items'][0]['status'], 'reject')
        self.assertIn('整页重做', rep['items'][0]['reason'])
        self.assertEqual(out['slides'][0], self.deck['slides'][0])

    def test_模板页重做的条目按微调也能应用(self):
        # 与上一条相反：封面/目录的重做本身就带 ops（逐条可手改），拨错开关也
        # 不会丢 —— 它走的本来就是字段补丁那条路
        out, rep = revise.apply_revision(self.deck, [dict(
            preview=1, new={'title': '新标题'},
            ops=[dict(path='title', expect='原标题', value='新标题')])])
        self.assertEqual(rep['items'][0]['status'], 'ok')
        self.assertEqual(out['title'], '新标题')


class TestTemplateRewrite(unittest.TestCase):
    """封面/目录的「整页重做」= 重出文案（模板页没有版式可换）。

    这两页不走 `redo_slide`（那条路按 `slides[]` 与 `outline.pages[]` 一一对应的
    下标取内容，而封面/目录不在 `slides[]` 里），所以校验与落盘另有一套 ——
    这一组把「模型给什么算能用」钉死。
    """

    def setUp(self):
        self.deck = _deck(2, title='原标题', toc=['01 甲章 —— 甲', '02 乙章 —— 乙'],
                          subtitle='2026 年 9 月')

    def test_页面清单说明模板页可以重做(self):
        # 这段 note 会进喂给模型的页码表（`page_table`），写着「不支持换版式」
        # 时模型会把封面/目录的要求放进 unclear —— 文案一改，这条就得跟着改
        idx = {e['preview']: e for e in revise.build_page_index(self.deck)}
        self.assertIn('重做', idx[1]['note'])
        self.assertIn('重做', idx[2]['note'])
        self.assertIn('没有可改的内容', idx[5]['note'])       # 封底不变

    def test_封面只取这一页有的键(self):
        # 多写一个 layout 会顺着 commit_page 漏进 deck 顶层，而指纹、大纲、
        # `_normalise_plan` 都不认它
        self.assertEqual(
            revise.template_new(self.deck, 1, {'title': '新标题', 'subtitle': '新副标题',
                                               'layout': 'statement'}),
            {'title': '新标题', 'subtitle': '新副标题'})

    def test_封面只给标题就不动副标题(self):
        self.assertEqual(revise.template_new(self.deck, 1, {'title': '新'}),
                         {'title': '新'})

    def test_空值当作没给(self):
        self.assertEqual(revise.template_new(self.deck, 1, {'title': '  '}), {})
        self.assertEqual(revise.template_new(self.deck, 2, {'toc': ['', ' 甲 ']}),
                         {'toc': ['甲']})

    def test_目录条数不能变(self):
        self.assertEqual(revise.check_template_new(self.deck, 2, {'toc': ['a', 'b']}),
                         ('ok', ''))
        level, why = revise.check_template_new(self.deck, 2, {'toc': ['只剩一条']})
        self.assertEqual(level, 'reject')
        self.assertIn('一条对应一个章节', why)

    def test_目录超长条目给提醒(self):
        level, why = revise.check_template_new(self.deck, 2,
                                               {'toc': ['零' * 40, '乙']})
        self.assertEqual(level, 'warn')
        self.assertIn('裁成', why)

    def test_封面标题超长给提醒不拒(self):
        level, why = revise.check_template_new(self.deck, 1, {'title': '长' * 25})
        self.assertEqual(level, 'warn')
        self.assertIn('折行', why)

    def test_没有文案就拒(self):
        self.assertEqual(revise.check_template_new(self.deck, 1, {})[0], 'reject')

    def test_ops逐条带现值_目录按条拆(self):
        # 面板只让 ≤80 字的标量手改，整列 toc 一条会渲染成一个装不下的长串
        ops = revise.template_ops(revise.page_spec(self.deck, 2),
                                  {'toc': ['新甲', '新乙']})
        self.assertEqual([o['path'] for o in ops], ['toc[0]', 'toc[1]'])
        self.assertEqual([o['expect'] for o in ops],
                         ['01 甲章 —— 甲', '02 乙章 —— 乙'])

    def test_提示词带上大纲与上限(self):
        outline = dict(title='整份标题', sections=[
            dict(name='01 甲章', summary='甲的一句话', pages=[dict(title='页一')]),
            dict(name='02 乙章', summary='乙的一句话', pages=[])])

        cover = revise.rewrite_template_prompt(self.deck, 1, outline, '压短一点')
        self.assertIn('整份标题', cover)
        self.assertIn('01 甲章', cover)
        self.assertIn('压短一点', cover)
        self.assertIn('%d 字' % revise.COVER_TITLE_MAX, cover)

        toc = revise.rewrite_template_prompt(self.deck, 2, outline, '重排')
        self.assertIn('正好 2 条', toc)          # 条数写进提示词，回来还要校验


class TestCommitRewrite(unittest.TestCase):
    """重做的落盘分派。这里有一条必须钉死的回归网：**负下标**。

    早先两个入口各自写 `slides[preview - PAGE_OFFSET]`，`preview=1` 算出的是
    `slides[-2]` —— 负下标在 Python 里合法，于是封面重做会**静默改掉倒数第二张
    正文页**：不抛异常、`changed` 还报着「第 1 页变了」，而 deck 顶层的 title
    一个字没动、成品封面根本没变。
    """

    def setUp(self):
        self.deck = _deck(4, title='原标题', toc=['01 甲章 —— 甲'],
                          subtitle='2026 年 9 月')

    def test_封面重做写顶层_一页正文都不动(self):
        slides = copy.deepcopy(self.deck['slides'])
        got = revise.commit_rewrite(self.deck, 1, {'title': '新标题'})
        self.assertEqual(got['reason'], '')
        self.assertTrue(got['changed'])
        self.assertEqual(self.deck['title'], '新标题')
        self.assertEqual(self.deck['slides'], slides)           # slides[-2] 没被碰
        self.assertEqual([p['field'] for p in got['outline']], ['cover.title'])

    def test_目录重做写顶层_一页正文都不动(self):
        slides = copy.deepcopy(self.deck['slides'])
        got = revise.commit_rewrite(self.deck, 2, {'toc': ['01 甲章 —— 改了']})
        self.assertEqual(got['reason'], '')
        self.assertEqual(self.deck['toc'], ['01 甲章 —— 改了'])
        self.assertEqual(self.deck['slides'], slides)
        self.assertEqual([p['field'] for p in got['outline']], ['toc'])

    def test_目录条目仍然过宽度夹取(self):
        from pptgen import pipeline
        long = '零' * 60
        got = revise.commit_rewrite(self.deck, 2, {'toc': [long]})
        self.assertEqual(self.deck['toc'], [pipeline._toc_line(long)])
        self.assertLess(len(self.deck['toc'][0]), len(long))
        self.assertTrue(got['changed'])

    def test_与原来一样就不算改动(self):
        # 没变就不该重渲这一页、也不该记进台账
        got = revise.commit_rewrite(self.deck, 1, {'title': '原标题'})
        self.assertEqual(got['reason'], '')
        self.assertFalse(got['changed'])
        self.assertEqual(got['outline'], [])

    def test_面板上改过的ops以用户为准(self):
        got = revise.commit_rewrite(self.deck, 1, {'title': '模型的'},
                                    [dict(path='title', expect='原标题',
                                          value='用户改的')])
        self.assertEqual(got['reason'], '')
        self.assertEqual(self.deck['title'], '用户改的')

    def test_ops现值对不上就拒(self):
        got = revise.commit_rewrite(self.deck, 1, None,
                                    [dict(path='title', expect='别的标题',
                                          value='x')])
        self.assertIn('现值', got['reason'])
        self.assertFalse(got['changed'])
        self.assertEqual(self.deck['title'], '原标题')

    def test_没有文案就拒(self):
        got = revise.commit_rewrite(self.deck, 1, None)
        self.assertIn('没有给出新文案', got['reason'])

    def test_没有页码不动deck(self):
        before = repr(self.deck)
        got = revise.commit_rewrite(self.deck, None, dict(layout='statement'))
        self.assertIn('没有说明是第几页', got['reason'])
        self.assertEqual(repr(self.deck), before)

    def test_越界页码不动deck(self):
        before = repr(self.deck)
        got = revise.commit_rewrite(self.deck, 99, dict(layout='statement'))
        self.assertIn('不对应任何正文页', got['reason'])
        self.assertEqual(repr(self.deck), before)

    def test_正文重做仍然整页替换(self):
        got = revise.commit_rewrite(self.deck, 3,
                                    dict(layout='statement', lines=[[['甲', {}]]]))
        self.assertTrue(got['changed'])
        self.assertEqual(self.deck['slides'][0]['layout'], 'statement')
        self.assertNotIn('items', self.deck['slides'][0])       # 不合并旧键
        self.assertEqual(got['outline'], [])                    # 正文页不碰大纲

    def test_不认识的版式被拒(self):
        got = revise.commit_rewrite(self.deck, 3, dict(layout='node_flow'))
        self.assertIn('不认识', got['reason'])
        self.assertFalse(got['changed'])

    def test_正文页越界被拒(self):
        got = revise.commit_rewrite(self.deck, 42, dict(layout='statement'))
        self.assertIn('不对应任何正文页', got['reason'])


class TestCheckByBuild(unittest.TestCase):
    """真正的容量门：几乎全部版式的溢出只有构建+几何检查才看得见。"""

    def _qa(self, issues):
        return lambda spec: dict(issues=issues, summary=dict(error=len(issues)))

    def test_只报关心的那几页(self):
        qa = self._qa([
            dict(kind='text_overflow', slide=5, detail='超 12 字'),
            dict(kind='text_overflow', slide=9, detail='超 3 字'),
        ])
        got = revise.check_by_build({}, qa, {5})
        self.assertEqual(list(got), [5])
        self.assertIn('超 12 字', got[5][0])

    def test_忽略非硬问题(self):
        # font_below_floor / large_empty_area 只是 warn 级，不该拦住补丁
        qa = self._qa([dict(kind='font_below_floor', slide=5, detail='11pt'),
                       dict(kind='large_empty_area', slide=5, detail='空')])
        self.assertEqual(revise.check_by_build({}, qa, {5}), {})

    def test_同一页多条问题都收(self):
        qa = self._qa([dict(kind='text_overflow', slide=5, detail='a'),
                       dict(kind='text_overlap', slide=5, detail='b')])
        self.assertEqual(len(revise.check_by_build({}, qa, {5})[5]), 2)

    def test_坏数据不炸(self):
        qa = self._qa([dict(kind='text_overflow'), dict(kind='text_overflow', slide='x')])
        self.assertEqual(revise.check_by_build({}, qa, {5}), {})


class TestCandidatesAnyIntent(unittest.TestCase):
    """修订单页时不锁意图 —— 否则用户说「换成能对比的形式」会做不到。"""

    def test_比锁意图的候选宽(self):
        from pptgen import layout_spec
        shape = layout_spec.shape_of([dict(type='para', text='甲。'),
                                      dict(type='para', text='乙。'),
                                      dict(type='para', text='丙。')])
        tight = layout_spec.candidates('content', 'definition', shape)
        wide = layout_spec.candidates_any_intent('content', shape,
                                                 first_intent='definition')
        self.assertTrue(set(s.name for s in tight) <= set(wide))
        self.assertLess(len(tight), len(wide))
        # 「换成能对比的形式」要求的那套必须够得着
        self.assertIn('comparison_rows', wide)

    def test_原意图的版式排在最前(self):
        from pptgen import layout_spec
        # 3 条素材：`process_chain` 的容量是 3–5 条，1 条时会被 `_capacity_ok`
        # 正确地挡掉（那是容量约束，不是意图约束）
        shape = layout_spec.shape_of([dict(type='para', text='甲。'),
                                      dict(type='para', text='乙。'),
                                      dict(type='para', text='丙。')])
        wide = layout_spec.candidates_any_intent('content', shape,
                                                 first_intent='process')
        self.assertIn('process_chain', wide[:3])

    def test_仍然受容量形态约束(self):
        # 放开的只是「意图」，不是容量：点名要表格的版式在没表格时依然出不来
        from pptgen import layout_spec
        shape = layout_spec.shape_of([dict(type='para', text='甲。')])
        wide = layout_spec.candidates_any_intent('content', shape)
        self.assertNotIn('data_table', wide)

    def test_结构页不掺进来(self):
        from pptgen import layout_spec
        wide = layout_spec.candidates_any_intent('content', None)
        self.assertNotIn('section_divider', wide)

    def test_有上限(self):
        from pptgen import layout_spec
        self.assertLessEqual(len(layout_spec.candidates_any_intent('content', None, limit=5)), 5)


if __name__ == '__main__':
    unittest.main()
