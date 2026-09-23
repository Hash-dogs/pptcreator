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


class TestVisionModelGuard(unittest.TestCase):
    """「已知看不见图」名单**只提醒，不否决配置**。

    这条曾经是一道硬拦截：`PPTGEN_VISION_MODEL` 命中名单就 `vision_config()`
    返回 None。后果实测过 —— `.env` 里配得好好的视觉模型被判成「未配置」，
    `run.py config` 报「看图输出人工复核包」，用户看到的是「我明明配了」。
    名单里那个名字（`deepseek-flash`）在他的部署里就是能看图的。

    要保住的是**防静默**那一半：命中名单时要有一句可听见的提醒。
    """

    ENV = {
        'PPTGEN_VISION_BASE_URL': 'https://example.invalid/v1',
        'PPTGEN_VISION_API_KEY': 'k',
        'PPTGEN_VISION_MODEL': 'deepseek-flash',
    }

    def test_explicit_model_wins_over_the_blacklist(self):
        with mock.patch.dict(os.environ, self.ENV):
            cfg = config.vision_config()
            self.assertIsNotNone(cfg, '显式配了视觉模型名就不该被判成未配置')
            self.assertEqual(cfg.model, 'deepseek-flash')

    def test_blacklisted_model_gets_a_visible_warning(self):
        with mock.patch.dict(os.environ, self.ENV):
            self.assertTrue(config.vision_warning())
            self.assertIn('deepseek-flash', config.vision_warning())

    def test_capable_model_has_no_warning(self):
        with mock.patch.dict(os.environ, dict(self.ENV,
                                              PPTGEN_VISION_MODEL='qwen-vl-max')):
            self.assertTrue(config.vision_capable('qwen-vl-max'))
            self.assertEqual(config.vision_warning(), '')

    def test_no_model_name_still_means_no_vision(self):
        """名单最初要拦的是**这个**：没给视觉模型名时别拿文本模型顶上去。

        早先回退分支会直接返回文本模型配置，于是看不见图的模型被当视觉模型调用，
        返回一段像样的文字、看起来像「看图通过」。
        """
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('PPTGEN_VISION_')}
        env['PPTGEN_LLM_BASE_URL'] = 'https://example.invalid/v1'
        env['PPTGEN_LLM_API_KEY'] = 'k'
        env['PPTGEN_LLM_MODEL'] = 'deepseek-flash'
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(config.vision_config())


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


class RepairRounds(unittest.TestCase):
    """修复轮数（`PPTGEN_REPAIR_ROUNDS`）的取值口径。

    这一条从「网页上的输入框」搬进 `.env` 之后，**唯一的出口就是这个函数** ——
    界面不再传、`server` 也不再读请求体，所以它一旦悄悄变了个值，没有任何地方
    会报错：给大了只是多烧几次模型调用，给成 0 则是整个修复回环一页都不修，
    产物看上去「生成成功」，只是几何告警还在。两种都要钉住。
    """

    def setUp(self):
        pat = mock.patch.dict(os.environ, {'PPTGEN_REPAIR_ROUNDS': '3'})
        pat.start()
        self.addCleanup(pat.stop)

    def test_missing_env_falls_back_to_three(self):
        env = {k: v for k, v in os.environ.items() if k != 'PPTGEN_REPAIR_ROUNDS'}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.repair_rounds(), 3)

    def test_explicit_value_wins(self):
        os.environ['PPTGEN_REPAIR_ROUNDS'] = '5'
        self.assertEqual(config.repair_rounds(), 5)

    def test_zero_means_no_repair_and_is_not_treated_as_unset(self):
        """0 是「不修」的正当取值 —— 别被 `or` 顶回默认值。"""
        os.environ['PPTGEN_REPAIR_ROUNDS'] = '0'
        self.assertEqual(config.repair_rounds(), 0)

    def test_negative_is_clamped_to_zero(self):
        os.environ['PPTGEN_REPAIR_ROUNDS'] = '-2'
        self.assertEqual(config.repair_rounds(), 0)

    def test_garbage_falls_back_to_three(self):
        for bad in ('', '  ', 'three', '3轮', '2.5'):
            os.environ['PPTGEN_REPAIR_ROUNDS'] = bad
            self.assertEqual(config.repair_rounds(), 3, bad)


if __name__ == '__main__':
    unittest.main()
