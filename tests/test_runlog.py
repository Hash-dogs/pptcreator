# -*- coding: utf-8 -*-
"""文件日志内核的回归测试。

全部用 `tempfile` 注入 root —— **绝不能写进仓库真实的 `out/logs/`**。

跑法::

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from pptgen import runlog                                # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, 'logs')
        self.addCleanup(self.tmp.cleanup)

    def run_log(self, task='演示文稿', **kw):
        kw.setdefault('origin', 'cli')
        kw.setdefault('command', 'outline')
        rl = runlog.RunLog(task, root=self.root, **kw)
        self.addCleanup(lambda: rl.finish(ok=True))
        return rl

    def read_json(self, rl):
        with open(os.path.join(rl.dir, 'run.json'), encoding='utf-8') as f:
            return json.load(f)


class TestNaming(_Base):

    def test_folder_name_is_ymd_hm_plus_task(self):
        rl = self.run_log('Dify 介绍与实战')
        name = os.path.basename(rl.dir)
        stamp = rl.started.strftime('%Y-%m-%d-%H-%M')
        self.assertEqual(name, '%s-Dify 介绍与实战' % stamp)
        self.assertTrue(name.startswith(stamp))

    def test_same_minute_same_task_gets_suffix(self):
        a, b = self.run_log('报告'), self.run_log('报告')
        self.assertNotEqual(a.dir, b.dir)
        self.assertTrue(os.path.basename(b.dir).endswith('-2'))

    def test_concurrent_claim_never_shares_a_folder(self):
        """两个线程同分钟同名必须拿到**不同**目录。

        `ThreadingHTTPServer` 是多线程的，用 `makedirs(exist_ok=True)` 的话
        它们会同时看到「不存在」然后写进同一个文件夹、互相覆盖对方记录。
        """
        got, errs = [], []
        barrier = threading.Barrier(4)

        def worker():
            try:
                barrier.wait(timeout=5)
                rl = runlog.RunLog('并发', root=self.root)
                got.append(rl.dir)
            except Exception as e:                       # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=worker) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=10)
        self.assertEqual(errs, [])
        self.assertEqual(len(got), 4)
        self.assertEqual(len(set(got)), 4, '并发下出现了重复目录：%s' % got)


class TestTaskNameSanitize(unittest.TestCase):

    def test_strips_path_separators(self):
        self.assertNotIn('/', runlog.safe_task_name('a/b\\c'))
        self.assertNotIn('\\', runlog.safe_task_name('a/b\\c'))

    def test_rejects_dot_and_dotdot_and_reserved(self):
        for bad in ('.', '..', '', '   ', 'CON', 'con', 'NUL', 'LPT1', 'PRN'):
            self.assertEqual(runlog.safe_task_name(bad), 'run', '%r 应被拒' % bad)

    def test_strips_trailing_dot_and_space(self):
        # Windows 会静默吃掉尾部的点和空格，不如自己先剥掉，免得名字对不上
        self.assertEqual(runlog.safe_task_name('报告. '), '报告')

    def test_caps_length(self):
        self.assertLessEqual(len(runlog.safe_task_name('长' * 200)), runlog._MAX_NAME)

    def test_keeps_chinese_and_dashes(self):
        self.assertEqual(runlog.safe_task_name('Dify 介绍与实战-2'), 'Dify 介绍与实战-2')


class TestJson(_Base):

    def test_note_lands_and_is_readable_immediately(self):
        rl = self.run_log()
        rl.note('outline', generated_by='fallback', pages=15)
        self.assertEqual(self.read_json(rl)['outline']['generated_by'], 'fallback')

    def test_note_merges_rather_than_replaces(self):
        rl = self.run_log()
        rl.note('outline', generated_by='llm')
        rl.note('outline', pages=14)
        d = self.read_json(rl)['outline']
        self.assertEqual(d, {'generated_by': 'llm', 'pages': 14})

    def test_write_is_atomic_on_failure(self):
        """写坏了也不能留下截断的 JSON —— 那正是最需要它可读的时刻。"""
        rl = self.run_log()
        rl.note('outline', pages=14)
        good = self.read_json(rl)
        rl.note('bad', obj=object())          # 不可序列化 → dump 中途抛
        self.assertEqual(self.read_json(rl), good, '失败后应保留上一版合法 JSON')
        self.assertFalse(os.path.exists(os.path.join(rl.dir, 'run.json.tmp')))
        # 坏值要回滚，否则之后每一次写都会失败、记录永久停在旧版本
        rl.note('plan', slides=14)
        self.assertEqual(self.read_json(rl)['plan']['slides'], 14)

    def test_stage_records_timing_and_error(self):
        rl = self.run_log()
        with self.assertRaises(ValueError):
            with rl.stage('plan'):
                raise ValueError('boom')
        stages = self.read_json(rl)['stages']
        self.assertEqual(stages[0]['name'], 'plan')
        self.assertFalse(stages[0]['ok'])
        self.assertIn('boom', stages[0]['error'])
        self.assertIsNotNone(stages[0]['duration_sec'])


class TestLogFile(_Base):

    def test_log_lines_have_timestamps(self):
        rl = self.run_log()
        rl.log('解析完成')
        with open(os.path.join(rl.dir, 'run.log'), encoding='utf-8') as f:
            line = f.read().strip()
        self.assertRegex(line, r'^\[\d{2}:\d{2}:\d{2}\] 解析完成$')

    def test_log_survives_disabled(self):
        rl = self.run_log(enabled=False)
        rl.log('x')
        rl.note('a', b=1)
        self.assertIsNone(rl.dir)


class TestAttach(_Base):

    def _src(self, name='源.docx', size=32):
        p = os.path.join(self.tmp.name, name)
        with open(p, 'wb') as f:
            f.write(b'x' * size)
        return p

    def test_upload_is_copied(self):
        rl = self.run_log()
        rl.attach_source(self._src(), origin='upload')
        self.assertTrue(self.read_json(rl)['source']['copied'])
        self.assertTrue(os.path.isfile(os.path.join(rl.dir, '源.docx')))

    def test_root_source_is_not_copied(self):
        """用本地根目录文件时不放上传副本 —— 用户明确要求的规则。"""
        rl = self.run_log()
        rl.attach_source(self._src(), origin='root')
        self.assertFalse(self.read_json(rl)['source']['copied'])
        self.assertFalse(os.path.exists(os.path.join(rl.dir, '源.docx')))
        self.assertIsNotNone(self.read_json(rl)['source']['sha256'])

    def test_pptx_copy_does_not_overwrite_existing(self):
        rl = self.run_log()
        a = self._src('deck.pptx')
        rl.attach_pptx(a)
        rl.attach_pptx(a)
        names = sorted(os.listdir(rl.dir))
        self.assertIn('deck.pptx', names)
        self.assertIn('deck-2.pptx', names)

    def test_original_name_only_in_json(self):
        rl = self.run_log()
        rl.attach_source(self._src(), origin='upload',
                         original_name='../../evil.docx')
        info = self.read_json(rl)['source']
        self.assertEqual(info['original_name'], '../../evil.docx')
        # 原始名绝不能出现在磁盘上的路径里
        self.assertFalse(any('evil' in n for n in os.listdir(rl.dir)))


class TestOpenAt(_Base):

    def test_rejects_path_outside_root(self):
        rl = self.run_log()
        out = os.path.join(self.tmp.name, '别处')
        os.makedirs(out)
        self.assertFalse(rl.open_at(out))

    def test_rejects_traversal(self):
        rl = self.run_log()
        os.makedirs(self.root, exist_ok=True)
        self.assertFalse(rl.open_at(os.path.join(self.root, '..', '..', 'Windows')))

    def test_rejects_root_itself(self):
        rl = self.run_log()
        os.makedirs(self.root, exist_ok=True)
        self.assertFalse(rl.open_at(self.root))

    def test_accepts_own_dir_and_keeps_prior_data(self):
        first = self.run_log(command='outline')
        first.note('outline', generated_by='llm')
        first.finish(ok=True)

        second = runlog.RunLog('演示文稿', root=self.root, command='generate')
        self.assertTrue(second.open_at(first.dir))
        second.note('plan', slides=14)
        second.finish(ok=True)

        d = self.read_json(second)
        self.assertEqual(d['outline']['generated_by'], 'llm')   # 上一段的记录还在
        self.assertEqual(d['plan']['slides'], 14)
        self.assertEqual(len(d['attempts']), 1)


class TestScope(_Base):

    def test_nested_scope_reuses_one_folder(self):
        with runlog.scope('嵌套', root=self.root) as outer:
            with runlog.scope('嵌套', root=self.root) as inner:
                self.assertIs(inner, outer)
            d1 = outer.dir
        self.assertEqual(len(os.listdir(self.root)), 1)
        self.assertEqual(os.listdir(self.root)[0], os.path.basename(d1))

    def test_scope_does_not_leak_between_runs(self):
        with runlog.scope('A', root=self.root):
            first = runlog.active().dir
        self.assertIsNone(runlog.active())
        with runlog.scope('A', root=self.root):
            second = runlog.active().dir
        self.assertNotEqual(first, second)
        self.assertEqual(len(os.listdir(self.root)), 2)

    def test_scope_records_exception_and_reraises(self):
        with self.assertRaises(RuntimeError):
            with runlog.scope('失败', root=self.root):
                raise RuntimeError('炸了')
        d = os.path.join(self.root, os.listdir(self.root)[0])
        with open(os.path.join(d, 'run.json'), encoding='utf-8') as f:
            data = json.load(f)
        self.assertFalse(data['ok'])
        self.assertIn('炸了', data['error'])

    def test_systemexit_is_recorded_too(self):
        """run.py 里有两处真的 raise SystemExit，它绕过 except Exception。"""
        with self.assertRaises(SystemExit):
            with runlog.scope('退出', root=self.root):
                raise SystemExit('找不到解析结果')
        d = os.path.join(self.root, os.listdir(self.root)[0])
        with open(os.path.join(d, 'run.json'), encoding='utf-8') as f:
            data = json.load(f)
        self.assertFalse(data['ok'])
        self.assertIn('SystemExit', data['error'])


class TestServerPathHelpers(_Base):
    """server.py 里两处踩过的路径坑。"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, ROOT)
        import server                                   # noqa: PLC0415
        cls.server = server

    def test_stem_of_strips_stacked_suffixes(self):
        """`splitext` 只剥一层 —— `x.deck.repaired.json` 曾是 `x.deck`。"""
        s = self.server
        for path in ('x.parsed.json', 'x.outline.json', 'x.deck.json',
                     'x.deck.repaired.json'):
            self.assertEqual(s._stem_of('out/plans/' + path), 'x', path)
        self.assertEqual(s._stem_of('out/plans/plain.json'), 'plain')

    def test_preview_stamp_tracks_render_recipe(self):
        """指纹里必须带渲染配方，否则改了分辨率旧图不会重渲。

        `_drop_stale_renders` 原来只看 pptx 的 mtime+size —— pptx 没动、
        我们改了 `PPTGEN_PREVIEW_WIDTH` 时，它会认为「没变化」，把旧的
        1280 图留在原地。页面上看不出区别（只是糊一点），于是
        「提高了清晰度」这件事悄悄没生效。
        """
        s = self.server
        import pptgen.config as config                   # noqa: PLC0415
        old = (os.environ.get('PPTGEN_PREVIEW_WIDTH'),
               os.environ.get('PPTGEN_PREVIEW_HEIGHT'))
        self.addCleanup(self._restore_preview_env, old)
        os.environ['PPTGEN_PREVIEW_WIDTH'] = '1920'
        os.environ['PPTGEN_PREVIEW_HEIGHT'] = '1080'
        wide = s._preview_sig()
        self.assertEqual(wide, '1920x1080')
        os.environ['PPTGEN_PREVIEW_WIDTH'] = '1280'
        os.environ['PPTGEN_PREVIEW_HEIGHT'] = '720'
        self.assertNotEqual(s._preview_sig(), wide)
        # 默认值：不配也该是 1920x1080（native 渲染的上限）
        os.environ.pop('PPTGEN_PREVIEW_WIDTH', None)
        os.environ.pop('PPTGEN_PREVIEW_HEIGHT', None)
        self.assertEqual(config.preview_size(), (1920, 1080))

    @staticmethod
    def _restore_preview_env(old):
        for k, v in zip(('PPTGEN_PREVIEW_WIDTH', 'PPTGEN_PREVIEW_HEIGHT'), old):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_find_source_reports_where_it_found_it(self):
        s = self.server
        name = 'probe.txt'
        up = os.path.join(s.UPLOADS, name)
        os.makedirs(s.UPLOADS, exist_ok=True)
        with open(up, 'w', encoding='utf-8') as f:
            f.write('上传的')
        try:
            got = s._find_source(name)
            self.assertIsNotNone(got)
            self.assertEqual(got[1], 'upload')
        finally:
            os.remove(up)

    def test_find_source_prefers_root_and_says_so(self):
        """根目录同名文件会**遮蔽**上传的那份 —— 这是既有行为，不是 bug。

        所以来源必须由查找那一刻决定并记进日志，不能按用户意图猜。
        """
        s = self.server
        name = 'probe_shadow.txt'
        up = os.path.join(s.UPLOADS, name)
        root = os.path.join(s.ROOT, name)
        os.makedirs(s.UPLOADS, exist_ok=True)
        for p, text in ((up, '上传的'), (root, '根目录的')):
            with open(p, 'w', encoding='utf-8') as f:
                f.write(text)
        try:
            got = s._find_source(name)
            self.assertEqual(got[0], root)
            self.assertEqual(got[1], 'root')
        finally:
            for p in (up, root):
                if os.path.exists(p):
                    os.remove(p)

    def test_find_source_rejects_non_document_extensions(self):
        s = self.server
        for bad in ('../../.env', 'notes.exe', '', 'noext'):
            self.assertIsNone(s._find_source(bad))


class TestConfigSnapshot(unittest.TestCase):

    def test_never_leaks_secrets(self):
        """快照是白名单 —— .env 就在仓库根，里面有 API key。"""
        snap = runlog.config_snapshot()
        blob = json.dumps(snap, ensure_ascii=False).lower()
        for bad in ('key', 'token_secret', 'api_key', 'sk-'):
            self.assertNotIn(bad, blob)
        self.assertIn('page_range', snap)
        self.assertIn('template_sha256', snap)


if __name__ == '__main__':
    unittest.main()
