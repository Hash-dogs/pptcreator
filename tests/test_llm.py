# -*- coding: utf-8 -*-
"""模型返回值的宽容解析（`llm.parse_json`）。

这条链路的失败方式很隐蔽：解析失败 → 那一轮识别作废 → 重试 → 看起来像「模型不行」，
而其实是解析器不够宽容。实测（2026-09-23）撞到的是**服务端把 `response_format`
回显进正文**：`{"type": "json_object"}` 单独一行，后面才是真正的 JSON。老实现取
「最外层大括号」，正好把这个回显和真正的对象跨成一个非法 JSON，四个输入里挂了两个。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))

from pptgen import llm                                       # noqa: E402


class TestParseJson(unittest.TestCase):

    def test_plain_object(self):
        self.assertEqual(llm.parse_json('{"a": 1}'), {'a': 1})

    def test_response_format_echo_is_skipped(self):
        """**实测踩到的那个**：回显行 + 真正的对象。"""
        raw = '{"type": "json_object"}\n{"name": "x", "blocks": []}'
        self.assertEqual(llm.parse_json(raw), {'name': 'x', 'blocks': []})

    def test_two_objects_keeps_the_substantive_one(self):
        raw = '前言\n{"name": "x", "signature": "s"}\n{"name": "y"}'
        self.assertEqual(llm.parse_json(raw)['name'], 'x')

    def test_code_fence(self):
        raw = '```json\n{"name": "x"}\n```'
        self.assertEqual(llm.parse_json(raw), {'name': 'x'})

    def test_bare_array_is_wrapped(self):
        self.assertEqual(llm.parse_json('[{"a": 1}]'), {'items': [{'a': 1}]})

    def test_truncated_json_still_raises(self):
        """真的坏了要报错（调用方据此重试），不能被「宽容」掩盖掉。"""
        with self.assertRaises(llm.LLMError):
            llm.parse_json('{"name": "x", "blocks": [{"kind": "text"')

    def test_only_type_object_alone_is_not_returned(self):
        """只有 `type` 的对象是回显形状 —— 单独出现时不能当成结果。"""
        with self.assertRaises(llm.LLMError):
            llm.parse_json('{"type": "json_object"}')


if __name__ == '__main__':
    unittest.main()
