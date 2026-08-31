#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_db_sanitize.py
sanitize_for_postgres 单元测试 - 验证 \u0000 字符清洗逻辑

测试重点:
- 字符串中的 \u0000 被移除
- 嵌套 dict / list 递归清洗
- 非 string 类型（int/float/bool/None）原样返回
- 原始对象不被修改（不可变语义）
- 空字符串/无 \u0000 字符串保持不变
"""
import pytest

from app.infrastructure.db_sanitize import sanitize_for_postgres


class TestSanitizeString:
    """字符串清洗测试"""

    def test_removes_null_char_from_string(self):
        assert sanitize_for_postgres("hello\u0000world") == "helloworld"

    def test_removes_multiple_null_chars(self):
        assert sanitize_for_postgres("\u0000a\u0000b\u0000") == "ab"

    def test_string_without_null_char_unchanged(self):
        assert sanitize_for_postgres("hello world") == "hello world"

    def test_empty_string_unchanged(self):
        assert sanitize_for_postgres("") == ""

    def test_only_null_char_becomes_empty(self):
        assert sanitize_for_postgres("\u0000") == ""

    def test_preserves_other_control_chars(self):
        assert sanitize_for_postgres("a\nb\tc") == "a\nb\tc"

    def test_preserves_unicode(self):
        assert sanitize_for_postgres("你好\u0000世界") == "你好世界"


class TestSanitizeDict:
    """字典清洗测试"""

    def test_cleans_string_values_in_dict(self):
        data = {"key": "value\u0000"}
        assert sanitize_for_postgres(data) == {"key": "value"}

    def test_cleans_nested_dict(self):
        data = {"outer": {"inner": "a\u0000b"}}
        assert sanitize_for_postgres(data) == {"outer": {"inner": "ab"}}

    def test_cleans_dict_key_is_preserved(self):
        data = {"ke\u0000y": "value"}
        result = sanitize_for_postgres(data)
        assert "ke\u0000y" in result
        assert result["ke\u0000y"] == "value"

    def test_preserves_non_string_values_in_dict(self):
        data = {"int": 1, "float": 2.5, "bool": True, "none": None}
        assert sanitize_for_postgres(data) == data

    def test_original_dict_not_modified(self):
        data = {"key": "value\u0000"}
        sanitize_for_postgres(data)
        assert data == {"key": "value\u0000"}

    def test_empty_dict_unchanged(self):
        assert sanitize_for_postgres({}) == {}


class TestSanitizeList:
    """列表清洗测试"""

    def test_cleans_string_items_in_list(self):
        assert sanitize_for_postgres(["a\u0000", "b"]) == ["a", "b"]

    def test_cleans_nested_list(self):
        assert sanitize_for_postgres([["x\u0000"], ["y"]]) == [["x"], ["y"]]

    def test_preserves_non_string_items_in_list(self):
        data = [1, 2.5, True, None]
        assert sanitize_for_postgres(data) == data

    def test_original_list_not_modified(self):
        data = ["a\u0000"]
        sanitize_for_postgres(data)
        assert data == ["a\u0000"]

    def test_empty_list_unchanged(self):
        assert sanitize_for_postgres([]) == []


class TestSanizeComplexStructure:
    """复杂嵌套结构清洗测试"""

    def test_cleans_deeply_nested_structure(self):
        data = {
            "level1": {
                "level2": [
                    {"level3": "data\u0000"},
                    "clean",
                ],
                "value": "a\u0000b",
            },
            "top": "\u0000",
        }
        expected = {
            "level1": {
                "level2": [
                    {"level3": "data"},
                    "clean",
                ],
                "value": "ab",
            },
            "top": "",
        }
        assert sanitize_for_postgres(data) == expected

    def test_mixed_types_in_list(self):
        data = [1, "a\u0000", {"k": "v\u0000"}, [True, "x\u0000"]]
        expected = [1, "a", {"k": "v"}, [True, "x"]]
        assert sanitize_for_postgres(data) == expected

    def test_realistic_event_data(self):
        """模拟真实事件数据结构（含搜索结果）"""
        data = {
            "id": "event-1",
            "type": "tool",
            "tool_call_id": "call_abc",
            "tool_name": "search",
            "function_args": {"query": "AI news\u0000"},
            "function_result": {
                "success": True,
                "data": {
                    "results": [
                        {"title": "AI Breakthrough\u0000", "url": "https://example.com"},
                        {"title": "Clean", "url": "https://clean.com"},
                    ],
                },
            },
        }
        result = sanitize_for_postgres(data)
        assert result["function_args"]["query"] == "AI news"
        assert result["function_result"]["data"]["results"][0]["title"] == "AI Breakthrough"
        assert result["function_result"]["data"]["results"][1]["title"] == "Clean"


class TestSanitizeNonStringTypes:
    """非字符串类型测试"""

    def test_int_unchanged(self):
        assert sanitize_for_postgres(42) == 42

    def test_float_unchanged(self):
        assert sanitize_for_postgres(3.14) == 3.14

    def test_bool_unchanged(self):
        assert sanitize_for_postgres(True) is True
        assert sanitize_for_postgres(False) is False

    def test_none_unchanged(self):
        assert sanitize_for_postgres(None) is None
