# -*- coding: utf-8 -*-
"""内容策略（`PPTGEN_CONTENT_MODE`）与其逐次覆盖的回归测试。

这条配置有两处**没有报错、只有产物变差**的失败方式，所以值得钉住：

  - 非法值 / 空值悄悄把口径换掉：`.env` 里写错一个字，本该退回 `balance`，
    实际却可能在提示词里注入一句空话（或者干脆崩在 `KeyError` 上）；
  - 覆盖**漏出任务线程**：Web 端两个任务并发跑，一个选了 `strict`、另一个选了
    `enrich`，若覆盖写的是进程级全局（`os.environ`），两个任务会互相改口径，
    而这种污染在日志里看不出来 —— 它只让某一页的内容莫名多了或少了几段。

跑法::

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations
import os
import sys
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from pptgen import config                                 # noqa: E402


class ContentMode(unittest.TestCase):
    def setUp(self):
        # 真实 `.env` 已经由别的模块 `load_env()` 进 `os.environ` 了，
        # 用例自己钉一份，跑的顺序就不再影响结果。
        pat = mock.patch.dict(os.environ, {'PPTGEN_CONTENT_MODE': 'balance'})
        pat.start()
        self.addCleanup(pat.stop)

    # ── 取值与归一化 ────────────────────────────────────────
    def test_missing_env_falls_back_to_balance(self):
        env = {k: v for k, v in os.environ.items() if k != 'PPTGEN_CONTENT_MODE'}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.content_mode(), 'balance')

    def test_env_value_is_lowercased(self):
        os.environ['PPTGEN_CONTENT_MODE'] = 'ENRICH'
        self.assertEqual(config.content_mode(), 'enrich')

    def test_illegal_env_value_falls_back_to_balance(self):
        for bad in ('nope', '', '  ', 'balanced', '1'):
            os.environ['PPTGEN_CONTENT_MODE'] = bad
            self.assertEqual(config.content_mode(), 'balance', bad)

    def test_pick_normalises_legal_values(self):
        self.assertEqual(config.pick_content_mode(' Strict '), 'strict')
        self.assertEqual(config.pick_content_mode('enrich'), 'enrich')

    def test_pick_rejects_everything_unknown(self):
        for bad in ('nope', '', '   ', None, 'balanced', 'strict,balance'):
            self.assertIsNone(config.pick_content_mode(bad), bad)

    # ── 覆盖的作用域 ────────────────────────────────────────
    def test_override_wins_inside_the_block_only(self):
        os.environ['PPTGEN_CONTENT_MODE'] = 'balance'
        with config.content_mode_override('strict') as m:
            self.assertEqual(m, 'strict')
            self.assertEqual(config.content_mode(), 'strict')
        self.assertEqual(config.content_mode(), 'balance')

    def test_override_restores_on_exception(self):
        with self.assertRaises(ValueError):
            with config.content_mode_override('enrich'):
                raise ValueError('boom')
        self.assertEqual(config.content_mode(), 'balance')

    def test_illegal_override_falls_back_to_env_not_to_previous(self):
        os.environ['PPTGEN_CONTENT_MODE'] = 'enrich'
        with config.content_mode_override('whatever'):
            self.assertEqual(config.content_mode(), 'enrich')

    def test_none_override_means_no_override(self):
        os.environ['PPTGEN_CONTENT_MODE'] = 'strict'
        with config.content_mode_override(None):
            self.assertEqual(config.content_mode(), 'strict')

    def test_override_never_touches_os_environ(self):
        """覆盖必须**只是**线程局部 —— 写 `os.environ` 会污染 CLI 与后续任务。"""
        with config.content_mode_override('strict'):
            self.assertEqual(os.environ['PPTGEN_CONTENT_MODE'], 'balance')

    # ── 并发：这正是覆盖要挂在线程局部上的理由 ──────────────
    def test_override_does_not_leak_into_other_threads(self):
        seen = {}
        started = threading.Barrier(2, timeout=10)

        def other():
            started.wait()                    # 保证覆盖已经生效
            seen['mode'] = config.content_mode()

        with config.content_mode_override('strict'):
            t = threading.Thread(target=other)
            t.start()
            started.wait()
            t.join()
            self.assertEqual(config.content_mode(), 'strict')
        self.assertEqual(seen['mode'], 'balance')


if __name__ == '__main__':
    unittest.main()
