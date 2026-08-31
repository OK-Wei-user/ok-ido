#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_json_parser_empty_text.py
RepairJSONParser 空文本防御性处理的单元测试

背景:会话 378d345c 因 LLM 输出空文本导致 _json_parser.invoke("") 抛出
ValueError("json文本为空，且无默认值")，异常在 base.py:510 无 try/except 捕获，
传播至 AgentTaskRunner 导致会话中断。

修复策略:repair_json_parser.py 在空文本时返回 default_value(若提供)或空 dict {},
避免抛异常。本测试覆盖空文本、None、纯空白、正常 JSON、JSON 数组、需修复 JSON 等场景。
"""
import pytest

from app.infrastructure.external.json_parser.repair_json_parser import RepairJSONParser


@pytest.fixture
def parser() -> RepairJSONParser:
    """构造 RepairJSONParser 实例"""
    return RepairJSONParser()


class TestEmptyTextDefensiveHandling:
    """空文本防御性处理:不抛异常,返回安全默认值"""

    @pytest.mark.asyncio
    async def test_empty_string_returns_empty_dict(self, parser: RepairJSONParser):
        """空字符串应返回空 dict 而非抛异常(修复会话中断的关键行为)"""
        result = await parser.invoke("")
        assert result == {}

    @pytest.mark.asyncio
    async def test_none_text_returns_empty_dict(self, parser: RepairJSONParser):
        """None 文本应返回空 dict(防御性处理)"""
        result = await parser.invoke(None)  # type: ignore[arg-type]
        assert result == {}

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_empty_dict(self, parser: RepairJSONParser):
        """纯空白文本(空格/制表符/换行)应返回空 dict"""
        result = await parser.invoke("   \t\n  ")
        assert result == {}

    @pytest.mark.asyncio
    async def test_empty_text_with_default_value_returns_default(self, parser: RepairJSONParser):
        """空文本时 default_value 优先于空 dict 默认值"""
        sentinel = {"fallback": True}
        result = await parser.invoke("", default_value=sentinel)
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_whitespace_with_default_value_returns_default(self, parser: RepairJSONParser):
        """纯空白文本时 default_value 优先于空 dict 默认值"""
        sentinel = []
        result = await parser.invoke("   ", default_value=sentinel)
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_none_with_default_value_returns_default(self, parser: RepairJSONParser):
        """None 文本时 default_value 优先于空 dict 默认值"""
        sentinel = {"recovered": True}
        result = await parser.invoke(None, default_value=sentinel)  # type: ignore[arg-type]
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_empty_text_does_not_raise(self, parser: RepairJSONParser):
        """空文本不应抛出任何异常(核心修复目标)"""
        try:
            await parser.invoke("")
        except Exception as exc:  # noqa: BLE001 - 明确断言不抛异常
            pytest.fail(f"空文本不应抛异常,但抛出: {type(exc).__name__}: {exc}")


class TestNormalJsonParsing:
    """正常 JSON 解析行为(回归保护)"""

    @pytest.mark.asyncio
    async def test_valid_json_object_parses_correctly(self, parser: RepairJSONParser):
        """合法 JSON 对象应正常解析"""
        result = await parser.invoke('{"name": "test", "value": 42}')
        assert result == {"name": "test", "value": 42}

    @pytest.mark.asyncio
    async def test_valid_json_array_parses_correctly(self, parser: RepairJSONParser):
        """合法 JSON 数组应正常解析"""
        result = await parser.invoke('[1, 2, 3, "four"]')
        assert result == [1, 2, 3, "four"]

    @pytest.mark.asyncio
    async def test_nested_json_parses_correctly(self, parser: RepairJSONParser):
        """嵌套 JSON 结构应正常解析"""
        text = '{"outer": {"inner": [1, 2], "flag": true}, "empty": null}'
        result = await parser.invoke(text)
        assert result == {"outer": {"inner": [1, 2], "flag": True}, "empty": None}

    @pytest.mark.asyncio
    async def test_chinese_content_parses_correctly(self, parser: RepairJSONParser):
        """含中文内容的 JSON 应正常解析(ensure_ascii=False)"""
        result = await parser.invoke('{"标题": "出入库分析", "数量": 100}')
        assert result == {"标题": "出入库分析", "数量": 100}

    @pytest.mark.asyncio
    async def test_normal_text_ignores_default_value(self, parser: RepairJSONParser):
        """非空文本时 default_value 不应被使用"""
        sentinel = {"fallback": True}
        result = await parser.invoke('{"ok": true}', default_value=sentinel)
        assert result == {"ok": True}
        assert result is not sentinel


class TestMalformedJsonRepair:
    """JSON 修复能力(回归保护)"""

    @pytest.mark.asyncio
    async def test_trailing_comma_repaired(self, parser: RepairJSONParser):
        """尾随逗号应被修复"""
        result = await parser.invoke('{"a": 1, "b": 2,}')
        assert result == {"a": 1, "b": 2}

    @pytest.mark.asyncio
    async def test_missing_closing_brace_repaired(self, parser: RepairJSONParser):
        """缺失闭合括号应被修复"""
        result = await parser.invoke('{"key": "value"')
        # json_repair 会补全缺失的闭合括号
        assert result.get("key") == "value"

    @pytest.mark.asyncio
    async def test_single_quotes_repaired(self, parser: RepairJSONParser):
        """单引号字符串应被修复为双引号"""
        result = await parser.invoke("{'name': 'test'}")
        assert result == {"name": "test"}

    @pytest.mark.asyncio
    async def test_json_with_code_fence_repaired(self, parser: RepairJSONParser):
        """带 ```json 代码围栏的文本应被修复"""
        text = '```json\n{"plan": "step1"}\n```'
        result = await parser.invoke(text)
        assert result == {"plan": "step1"}
