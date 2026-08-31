#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_browser.py
工业级记忆系统单元测试 - 四层渐进式压缩、关键事实提取、容量管控
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.models.memory import (
    Memory, KeyFact, MemoryMetrics, CompressionLevel,
    _BROWSER_VIEW_TOOLS, _BROWSER_ACTION_TOOLS, _FILE_TOOLS,
    _SHELL_TOOLS, _SEARCH_TOOLS,
    _MAX_MESSAGES_SOFT, _MAX_MESSAGES_HARD,
    _PROTECT_HEAD_COUNT, _PROTECT_TAIL_COUNT,
)
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.browser import BrowserTool


class TestMemoryCompact:
    """Memory Layer1常规压缩测试"""

    def test_compress_browser_view_with_long_content(self):
        memory = Memory()
        long_content = json.dumps({
            "content": "x" * 10000,
            "interactive_elements": ["el1", "el2", "el3"],
            "page_state": {"url": "https://example.com", "title": "Test", "hasBlockingElement": False},
        })
        memory.add_message({"role": "tool", "function_name": "browser_view", "content": long_content})
        memory.compact()
        compressed = memory.messages[0]["content"]
        assert "compressed" in compressed
        assert "3 interactive elements" in compressed
        assert "example.com" in compressed

    def test_compress_browser_view_with_short_content(self):
        memory = Memory()
        memory.add_message({"role": "tool", "function_name": "browser_view", "content": "short"})
        memory.compact()
        assert memory.messages[0]["content"] == "short"

    def test_compress_browser_view_with_non_string_content(self):
        """Phase E: 非字符串内容返回工具名+removed标记"""
        memory = Memory()
        memory.add_message({"role": "tool", "function_name": "browser_view", "content": {"key": "value"}})
        memory.compact()
        assert "browser_view" in memory.messages[0]["content"]
        assert "removed" in memory.messages[0]["content"]

    def test_compress_browser_action_tools(self):
        memory = Memory()
        for fn in ["browser_click", "browser_input", "browser_scroll_up",
                    "browser_scroll_down", "browser_scroll_to_text",
                    "browser_press_key", "browser_wait"]:
            memory.add_message({"role": "tool", "function_name": fn, "content": "some result"})
        memory.compact()
        for msg in memory.messages:
            assert "executed" in msg["content"]

    def test_compress_search_results(self):
        memory = Memory()
        memory.add_message({"role": "tool", "function_name": "search_web", "content": "x" * 2000})
        memory.compact()
        content = memory.messages[0]["content"]
        assert "truncated" in content
        assert len(content) < 2000

    def test_compress_search_results_short(self):
        memory = Memory()
        memory.add_message({"role": "tool", "function_name": "search_web", "content": "short result"})
        memory.compact()
        assert memory.messages[0]["content"] == "short result"

    def test_compress_shell_output(self):
        memory = Memory()
        memory.add_message({"role": "tool", "function_name": "shell_exec", "content": "x" * 1000})
        memory.compact()
        content = memory.messages[0]["content"]
        assert "truncated" in content

    def test_compress_file_content(self):
        memory = Memory()
        long_file = json.dumps({"filepath": "/tmp/test.txt", "content": "x" * 2000})
        memory.add_message({"role": "tool", "function_name": "file_read", "content": long_file})
        memory.compact()
        content = memory.messages[0]["content"]
        assert "test.txt" in content

    def test_remove_reasoning_content(self):
        memory = Memory()
        memory.add_message({"role": "assistant", "content": "hello", "reasoning_content": "thinking"})
        memory.compact()
        assert "reasoning_content" not in memory.messages[0]
        assert memory.messages[0]["content"] == "hello"

    def test_non_tool_messages_unchanged(self):
        memory = Memory()
        memory.add_message({"role": "user", "content": "hello"})
        memory.add_message({"role": "assistant", "content": "world"})
        memory.compact()
        assert memory.messages[0]["content"] == "hello"
        assert memory.messages[1]["content"] == "world"

    def test_compact_updates_metrics(self):
        memory = Memory()
        memory.add_message({"role": "tool", "function_name": "browser_click", "content": "ok"})
        memory.compact()
        assert memory.metrics.compact_count == 1


class TestCompactReasoningAndMetrics:
    """常规压缩 — reasoning清理与metrics更新(Phase E: 合并原aggressive测试)"""

    def test_compact_removes_reasoning(self):
        """compact()应移除非tool_calls消息的reasoning_content"""
        memory = Memory()
        memory.add_message({"role": "assistant", "content": "answer", "reasoning_content": "long reasoning"})
        memory.compact()
        assert "reasoning_content" not in memory.messages[0]

    def test_compact_preserves_user_messages(self):
        """compact()不应修改user消息内容"""
        memory = Memory()
        memory.add_message({"role": "user", "content": "important question"})
        memory.compact()
        assert memory.messages[0]["content"] == "important question"

    def test_compact_updates_metrics(self):
        """compact()应更新compact_count"""
        memory = Memory()
        memory.add_message({"role": "tool", "function_name": "browser_click", "content": "ok"})
        memory.compact()
        assert memory.metrics.compact_count == 1


class TestEmergencyCompact:
    """紧急压缩测试(Phase E: 合并原Layer3/Layer4为单一紧急层)"""

    def test_emergency_reduces_message_count(self):
        memory = Memory()
        memory.add_message({"role": "system", "content": "system"})
        memory.add_message({"role": "user", "content": "task"})
        for i in range(20):
            memory.add_message({"role": "tool", "function_name": "browser_click", "content": f"result {i}"})
        memory.add_message({"role": "assistant", "content": "final"})
        memory.add_message({"role": "user", "content": "next"})
        memory.add_message({"role": "assistant", "content": "done"})
        memory.add_message({"role": "user", "content": "end"})
        original_count = len(memory.messages)
        memory.emergency_compact()
        assert len(memory.messages) < original_count
        assert len(memory.messages) == _PROTECT_HEAD_COUNT + 1 + _PROTECT_TAIL_COUNT

    def test_emergency_preserves_head_and_tail(self):
        memory = Memory()
        memory.add_message({"role": "system", "content": "system"})
        memory.add_message({"role": "user", "content": "first user"})
        for i in range(10):
            memory.add_message({"role": "tool", "function_name": "browser_click", "content": f"r{i}"})
        memory.add_message({"role": "assistant", "content": "final answer"})
        memory.emergency_compact()
        assert memory.messages[0]["content"] == "system"
        assert memory.messages[1]["content"] == "first user"
        assert memory.messages[-1]["content"] == "final answer"

    def test_emergency_includes_key_facts(self):
        memory = Memory()
        memory.add_message({"role": "system", "content": "system"})
        memory.add_message({"role": "user", "content": "task"})
        memory.add_message({
            "role": "tool", "function_name": "browser_view",
            "content": json.dumps({"page_state": {"url": "https://test.com"}, "success": True}),
        })
        for i in range(10):
            memory.add_message({"role": "tool", "function_name": "browser_click", "content": f"r{i}"})
        memory.add_message({"role": "assistant", "content": "done"})
        memory.emergency_compact()
        summary_msg = memory.messages[_PROTECT_HEAD_COUNT]
        assert "紧急压缩" in summary_msg["content"]

    def test_emergency_skips_if_too_few_messages(self):
        memory = Memory()
        memory.add_message({"role": "system", "content": "system"})
        memory.add_message({"role": "user", "content": "hi"})
        memory.emergency_compact()
        assert len(memory.messages) == 2


class TestEmergencyCompactUserRequirement:
    """紧急压缩 — 用户需求保留测试(Phase E: 合并原minimal_compact能力)"""

    def test_emergency_preserves_user_requirement_in_summary(self):
        """紧急压缩应在summary_msg中保留用户原始需求"""
        memory = Memory()
        memory.add_message({"role": "system", "content": "system prompt"})
        memory.add_message({"role": "user", "content": "do something important"})
        for i in range(30):
            memory.add_message({"role": "tool", "function_name": "browser_click", "content": f"r{i}"})
        memory.add_message({"role": "assistant", "content": "done"})
        memory.emergency_compact()
        assert memory.messages[0]["content"] == "system prompt"
        # summary_msg位于head之后
        summary_msg = memory.messages[_PROTECT_HEAD_COUNT]
        assert "紧急压缩" in summary_msg["content"]
        assert "do something important" in summary_msg["content"]

    def test_emergency_preserves_user_requirement_text(self):
        """紧急压缩应保留用户需求关键词"""
        memory = Memory()
        memory.add_message({"role": "system", "content": "system"})
        memory.add_message({"role": "user", "content": "search for weather data and create a report"})
        for i in range(20):
            memory.add_message({"role": "tool", "function_name": "browser_click", "content": f"r{i}"})
        memory.emergency_compact()
        summary_msg = memory.messages[_PROTECT_HEAD_COUNT]
        assert "weather" in summary_msg["content"]


class TestKeyFactsExtraction:
    """关键事实提取测试"""

    def test_extract_url_from_browser_view(self):
        memory = Memory()
        memory.add_message({
            "role": "tool", "function_name": "browser_view",
            "content": json.dumps({"page_state": {"url": "https://example.com"}, "success": True}),
        })
        facts = memory.extract_key_facts()
        assert any(f.category == "url" and "example.com" in f.content for f in facts)

    def test_extract_filepath_from_file_tool(self):
        memory = Memory()
        memory.add_message({
            "role": "tool", "function_name": "file_read",
            "content": json.dumps({"filepath": "/tmp/data.csv", "success": True}),
        })
        facts = memory.extract_key_facts()
        assert any(f.category == "file" and "data.csv" in f.content for f in facts)

    def test_extract_command_from_shell(self):
        memory = Memory()
        memory.add_message({
            "role": "tool", "function_name": "shell_exec",
            "content": json.dumps({"command": "pip install pandas", "success": True}),
        })
        facts = memory.extract_key_facts()
        assert any(f.category == "cmd" and "pip install pandas" in f.content for f in facts)

    def test_key_facts_deduplication(self):
        memory = Memory()
        for _ in range(3):
            memory.add_message({
                "role": "tool", "function_name": "browser_view",
                "content": json.dumps({"page_state": {"url": "https://same.com"}, "success": True}),
            })
        facts = memory.extract_key_facts()
        url_facts = [f for f in facts if f.category == "url"]
        assert len(url_facts) == 1

    def test_key_facts_max_limit(self):
        memory = Memory()
        for i in range(20):
            memory.add_message({
                "role": "tool", "function_name": "browser_view",
                "content": json.dumps({"page_state": {"url": f"https://page{i}.com"}, "success": True}),
            })
        facts = memory.extract_key_facts()
        assert len(facts) <= 10

    def test_get_key_facts_text(self):
        memory = Memory()
        memory.key_facts = [
            KeyFact(category="url", content="https://test.com"),
            KeyFact(category="file", content="/tmp/data.csv"),
        ]
        text = memory.get_key_facts_text()
        assert "test.com" in text
        assert "data.csv" in text


class TestCapacityManagement:
    """容量管控测试"""

    def test_should_compress_below_threshold(self):
        memory = Memory()
        for i in range(10):
            memory.add_message({"role": "user", "content": f"msg {i}"})
        assert not memory.should_compress(threshold=0.5)

    def test_should_compress_above_threshold(self):
        memory = Memory()
        for i in range(_MAX_MESSAGES_SOFT):
            memory.add_message({"role": "user", "content": f"msg {i}"})
        assert memory.should_compress(threshold=0.5)

    def test_is_context_overflow(self):
        memory = Memory()
        for i in range(_MAX_MESSAGES_HARD):
            memory.add_message({"role": "user", "content": f"msg {i}"})
        assert memory.is_context_overflow()

    def test_not_overflow_below_hard_limit(self):
        memory = Memory()
        for i in range(_MAX_MESSAGES_HARD - 1):
            memory.add_message({"role": "user", "content": f"msg {i}"})
        assert not memory.is_context_overflow()

    def test_get_compression_level_none(self):
        memory = Memory()
        for i in range(10):
            memory.add_message({"role": "user", "content": f"msg {i}"})
        assert memory.get_compression_level() == CompressionLevel.NONE

    def test_get_compression_level_normal(self):
        memory = Memory()
        for i in range(_MAX_MESSAGES_SOFT):
            memory.add_message({"role": "user", "content": f"msg {i}"})
        assert memory.get_compression_level() == CompressionLevel.NORMAL

    def test_get_compression_level_emergency(self):
        """Phase E: ≥60条消息触发紧急压缩级别"""
        memory = Memory()
        for i in range(_MAX_MESSAGES_HARD):
            memory.add_message({"role": "user", "content": f"msg {i}"})
        assert memory.get_compression_level() == CompressionLevel.EMERGENCY

    def test_auto_compact_returns_level(self):
        """Phase E: 40条消息触发常规压缩"""
        memory = Memory()
        for i in range(_MAX_MESSAGES_SOFT):
            memory.add_message({"role": "user", "content": f"msg {i}"})
        level = memory.auto_compact()
        assert level == CompressionLevel.NORMAL


class TestMemoryBasicOperations:
    """Memory基础操作测试"""

    def test_add_message_updates_metrics(self):
        memory = Memory()
        memory.add_message({"role": "user", "content": "hello"})
        assert memory.metrics.message_count == 1

    def test_add_messages_updates_metrics(self):
        memory = Memory()
        memory.add_messages([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
        assert memory.metrics.message_count == 2

    def test_roll_back_updates_metrics(self):
        memory = Memory()
        memory.add_message({"role": "user", "content": "hello"})
        memory.roll_back()
        assert memory.metrics.message_count == 0

    def test_empty_property(self):
        memory = Memory()
        assert memory.empty
        memory.add_message({"role": "user", "content": "hello"})
        assert not memory.empty

    def test_get_last_message(self):
        memory = Memory()
        assert memory.get_last_message() is None
        memory.add_message({"role": "user", "content": "hello"})
        assert memory.get_last_message()["content"] == "hello"

    def test_serialization_roundtrip(self):
        memory = Memory()
        memory.add_message({"role": "system", "content": "prompt"})
        memory.add_message({"role": "tool", "function_name": "browser_view", "content": "data"})
        memory.key_facts = [KeyFact(category="url", content="https://test.com")]

        data = memory.model_dump(mode="json")
        restored = Memory(**data)
        assert len(restored.messages) == 2
        assert len(restored.key_facts) == 1
        assert restored.key_facts[0].content == "https://test.com"


class TestBrowserTool:
    """BrowserTool工具注册测试"""

    def test_browser_tool_has_all_tools(self):
        browser_mock = AsyncMock()
        browser_mock.view_page = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.navigate = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.restart = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.click = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.input = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.move_mouse = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.press_key = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.select_option = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.scroll_up = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.scroll_down = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.scroll_to_text = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.console_exec = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.console_view = AsyncMock(return_value=ToolResult(success=True))
        browser_mock.wait = AsyncMock(return_value=ToolResult(success=True))

        tool = BrowserTool(browser_mock)
        tools = tool.get_tools()
        tool_names = [t["function"]["name"] for t in tools]

        expected_tools = [
            "browser_view", "browser_navigate", "browser_restart",
            "browser_click", "browser_input", "browser_move_mouse",
            "browser_press_key", "browser_select_option",
            "browser_scroll_up", "browser_scroll_down", "browser_scroll_to_text",
            "browser_console_exec", "browser_console_view",
            "browser_wait",
        ]
        for expected in expected_tools:
            assert expected in tool_names, f"缺少工具: {expected}"


class TestPlaywrightBrowserDomTree:
    """DOM树文本转换测试"""

    def test_dom_tree_to_text_basic(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        node = {
            "tag": "div", "text": "Hello", "attrs": {"class": "container"},
            "interactive": True, "children": [{"tag": "span", "text": "World", "attrs": {}}],
        }
        result = PlaywrightBrowser._dom_tree_to_text(node)
        assert "div" in result
        assert "Hello" in result
        assert "[interactive]" in result

    def test_dom_tree_to_text_empty(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        assert PlaywrightBrowser._dom_tree_to_text(None) == ""
        assert PlaywrightBrowser._dom_tree_to_text({}) == ""

    def test_dom_tree_to_text_offscreen(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        node = {"tag": "button", "text": "Click", "offscreen": True}
        result = PlaywrightBrowser._dom_tree_to_text(node)
        assert "[offscreen]" in result

    def test_dom_tree_to_text_semantic_attrs(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        node = {"tag": "input", "attrs": {"aria-label": "Search", "type": "text"}, "interactive": True}
        result = PlaywrightBrowser._dom_tree_to_text(node)
        assert 'aria-label="Search"' in result


class TestPlaywrightBrowserUrlValidation:
    """URL合法性校验测试"""

    def test_validate_url_valid_http(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        assert PlaywrightBrowser._validate_url("http://example.com") is True

    def test_validate_url_valid_https(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        assert PlaywrightBrowser._validate_url("https://example.com/path?q=1") is True

    def test_validate_url_invalid_no_scheme(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        assert PlaywrightBrowser._validate_url("example.com") is False

    def test_validate_url_invalid_ftp(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        assert PlaywrightBrowser._validate_url("ftp://example.com") is False

    def test_validate_url_invalid_empty(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        assert PlaywrightBrowser._validate_url("") is False

    def test_validate_url_invalid_none(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        assert PlaywrightBrowser._validate_url(None) is False


class TestPlaywrightBrowserConcurrencyLock:
    """并发安全锁测试 - 验证同一浏览器实例上的操作被串行化"""

    @pytest.mark.asyncio
    async def test_concurrent_operations_are_serialized(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        # 用mock替换内部方法，记录调用顺序
        execution_order = []

        async def mock_navigate_impl(url):
            execution_order.append(f"start:{url}")
            await asyncio.sleep(0.05)
            execution_order.append(f"end:{url}")
            return ToolResult(success=True)

        browser._navigate_impl = mock_navigate_impl

        # 并发发起两个导航操作
        await asyncio.gather(
            browser.navigate("https://a.com"),
            browser.navigate("https://b.com"),
        )
        # 验证操作被串行化: 第一个操作结束后第二个才开始
        assert execution_order[0].startswith("start:")
        assert execution_order[1].startswith("end:")
        assert execution_order[2].startswith("start:")
        assert execution_order[3].startswith("end:")


class TestPlaywrightBrowserNavigateValidation:
    """导航URL校验测试"""

    @pytest.mark.asyncio
    async def test_navigate_rejects_invalid_url(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        result = await browser.navigate("not-a-url")
        assert result.success is False
        assert "不合法" in result.message

    @pytest.mark.asyncio
    async def test_navigate_rejects_empty_url(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        result = await browser.navigate("")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_restart_rejects_invalid_url(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        result = await browser.restart("ftp://bad.com")
        assert result.success is False
        assert "不合法" in result.message


class TestPlaywrightBrowserSelectOptionFallback:
    """下拉选项多级容错策略测试 - 4级策略：text(label) → index → value → JS派发"""

    @pytest.mark.asyncio
    async def test_select_option_text_strategy_priority(self):
        """text参数优先：直接调用select_option(label=text)，不触发index/value策略"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        mock_element.select_option = AsyncMock(return_value=None)
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        result = await browser._select_option_impl(0, option=2, text="北京")
        assert result.success is True
        assert result.data["method"] == "text"
        mock_element.select_option.assert_called_once_with(label="北京")

    @pytest.mark.asyncio
    async def test_select_option_index_strategy_when_no_text(self):
        """无text时走策略2：按序号select_option(index=option)"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        mock_element.select_option = AsyncMock(side_effect=[None])
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        result = await browser._select_option_impl(0, option=2)
        assert result.success is True
        assert result.data["method"] == "index"
        mock_element.select_option.assert_called_once_with(index=2)

    @pytest.mark.asyncio
    async def test_select_option_fallback_to_value(self):
        """index失败后回退策略3：按值select_option(value=str(option))"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        mock_element.select_option = AsyncMock(side_effect=[
            Exception("index not found"),
            None,
        ])
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        result = await browser._select_option_impl(0, option=1)
        assert result.success is True
        assert result.data["method"] == "value"
        assert mock_element.select_option.call_count == 2

    @pytest.mark.asyncio
    async def test_select_option_fallback_to_js(self):
        """index/value均失败后回退策略4：JS赋值+事件派发"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        mock_element.select_option = AsyncMock(side_effect=[
            Exception("index not found"),
            Exception("value not found"),
        ])
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=None)
        browser.page = mock_page
        result = await browser._select_option_impl(0, option=1)
        assert result.success is True
        assert result.data["method"] == "js"
        assert mock_page.evaluate.call_count >= 1

    @pytest.mark.asyncio
    async def test_select_option_all_strategies_fail(self):
        """4级策略全部失败时返回失败并提示已尝试4种策略"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        mock_element.select_option = AsyncMock(side_effect=Exception("fail"))
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=Exception("js fail"))
        browser.page = mock_page
        result = await browser._select_option_impl(0, option=1)
        assert result.success is False
        assert "4种策略" in result.message

    @pytest.mark.asyncio
    async def test_select_option_no_option_and_no_text(self):
        """既无option也无text时返回失败提示"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        result = await browser._select_option_impl(0)
        assert result.success is False
        assert "option序号或text" in result.message

    @pytest.mark.asyncio
    async def test_select_option_element_not_found(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        browser._get_element_by_id = AsyncMock(return_value=None)
        browser._ensure_page = AsyncMock()
        result = await browser._select_option_impl(99, 0)
        assert result.success is False
        assert "不存在" in result.message


class TestPlaywrightBrowserScreenshotRetry:
    """截图重试机制测试"""

    @pytest.mark.asyncio
    async def test_screenshot_success_on_first_try(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.screenshot = AsyncMock(return_value=b"png_data")
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        result = await browser.screenshot()
        assert result == b"png_data"
        assert mock_page.screenshot.call_count == 1

    @pytest.mark.asyncio
    async def test_screenshot_retries_on_failure(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.screenshot = AsyncMock(side_effect=[
            Exception("timeout"),  # 第一次失败
            b"png_data",  # 第二次成功
        ])
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        result = await browser.screenshot()
        assert result == b"png_data"
        assert mock_page.screenshot.call_count == 2

    @pytest.mark.asyncio
    async def test_screenshot_raises_after_all_retries(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.screenshot = AsyncMock(side_effect=Exception("permanent failure"))
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        with pytest.raises(Exception, match="permanent failure"):
            await browser.screenshot()
        # _SCREENSHOT_RETRIES=2, 所以总共尝试3次
        assert mock_page.screenshot.call_count == 3


class TestPlaywrightBrowserOperationMetrics:
    """操作指标日志测试"""

    @pytest.mark.asyncio
    async def test_log_action_records_success(self, caplog):
        import logging
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        with caplog.at_level(logging.INFO):
            browser._log_action("click", True, 0.123, index=5)
        assert any("browser_action|click|success" in r.message for r in caplog.records)
        assert any("0.123" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_log_action_records_failure(self, caplog):
        import logging
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        with caplog.at_level(logging.INFO):
            browser._log_action("navigate", False, 1.5, url="https://test.com")
        assert any("browser_action|navigate|failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_navigate_logs_metrics(self, caplog):
        import logging
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        browser._navigate_impl = AsyncMock(return_value=ToolResult(success=True))
        with caplog.at_level(logging.INFO):
            await browser.navigate("https://example.com")
        assert any("browser_action|navigate|success" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_click_logs_metrics(self, caplog):
        import logging
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        browser._click_impl = AsyncMock(return_value=ToolResult(success=True))
        with caplog.at_level(logging.INFO):
            await browser.click(index=3)
        assert any("browser_action|click|success" in r.message for r in caplog.records)
        assert any("index=3" in r.message for r in caplog.records)


class TestPlaywrightBrowserScrollCacheRefresh:
    """滚动/等待后缓存刷新测试"""

    @pytest.mark.asyncio
    async def test_scroll_down_refreshes_cache(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={"url": "https://test.com"})
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._scroll_dialog = AsyncMock(return_value=False)
        refresh_called = []

        async def mock_refresh():
            refresh_called.append(True)

        browser._refresh_interactive_cache = mock_refresh
        result = await browser.scroll_down()
        assert result.success is True
        assert len(refresh_called) == 1

    @pytest.mark.asyncio
    async def test_scroll_up_refreshes_cache(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={"url": "https://test.com"})
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._scroll_dialog = AsyncMock(return_value=False)
        refresh_called = []

        async def mock_refresh():
            refresh_called.append(True)

        browser._refresh_interactive_cache = mock_refresh
        result = await browser.scroll_up(to_top=True)
        assert result.success is True
        assert len(refresh_called) == 1

    @pytest.mark.asyncio
    async def test_wait_refreshes_cache(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={"url": "https://test.com"})
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._wait_dom_stable = AsyncMock()
        browser._wait_for_loading_disappear = AsyncMock()
        refresh_called = []

        async def mock_refresh():
            refresh_called.append(True)

        browser._refresh_interactive_cache = mock_refresh
        result = await browser.wait(seconds=0.01)
        assert result.success is True
        assert len(refresh_called) == 1


class TestPlaywrightBrowserInputEventDispatch:
    """输入操作事件派发测试"""

    @pytest.mark.asyncio
    async def test_input_js_fallback_dispatches_change_event(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        # fill和type都失败，触发JS兜底
        mock_element.fill = AsyncMock(side_effect=Exception("fill fail"))
        mock_element.type = AsyncMock(side_effect=Exception("type fail"))
        mock_element.click = AsyncMock(side_effect=Exception("click fail"))
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        mock_page = AsyncMock()
        mock_page.keyboard = AsyncMock()
        evaluate_calls = []
        original_evaluate = mock_page.evaluate

        async def capture_evaluate(script, *args):
            evaluate_calls.append(script)
            return None

        mock_page.evaluate = capture_evaluate
        browser.page = mock_page
        result = await browser._input_impl("test text", False, index=0)
        assert result.success is True
        # 验证JS兜底脚本中包含change事件派发
        js_script = evaluate_calls[0] if evaluate_calls else ""
        assert "change" in js_script
        assert "input" in js_script


class TestPlaywrightBrowserErrorHandling:
    """操作异常处理测试"""

    @pytest.mark.asyncio
    async def test_move_mouse_handles_error(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.mouse = AsyncMock()
        mock_page.mouse.move = AsyncMock(side_effect=Exception("mouse error"))
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        result = await browser.move_mouse(100, 200)
        assert result.success is False
        assert "移动鼠标失败" in result.message

    @pytest.mark.asyncio
    async def test_press_key_handles_error(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.keyboard = AsyncMock()
        mock_page.keyboard.press = AsyncMock(side_effect=Exception("key error"))
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        result = await browser.press_key("Enter")
        assert result.success is False
        assert "按键操作失败" in result.message

    @pytest.mark.asyncio
    async def test_console_exec_handles_error(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=Exception("js error"))
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        result = await browser.console_exec("invalid js")
        assert result.success is False
        assert "JS执行失败" in result.message

    @pytest.mark.asyncio
    async def test_view_page_handles_error(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        browser._ensure_page = AsyncMock(side_effect=Exception("page error"))
        result = await browser.view_page()
        assert result.success is False
        assert "查看页面失败" in result.message


class TestPlaywrightBrowserScrollToText:
    """scroll_to_text 动作测试 - 文本匹配滚动定位"""

    @pytest.mark.asyncio
    async def test_scroll_to_text_success(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        # SCROLL_TO_TEXT_FUNC返回{found,text,inViewport}, GET_PAGE_STATE_FUNC返回page_state
        mock_page.evaluate = AsyncMock(side_effect=[
            {"found": True, "text": "提交按钮", "inViewport": True},
            {"url": "https://example.com", "title": "Test", "scrollY": 0},
        ])
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._refresh_interactive_cache = AsyncMock()
        result = await browser._scroll_to_text_impl("提交")
        assert result.success is True
        assert result.data["matched_text"] == "提交"
        assert result.data["target_visible"] is True
        assert result.data["target_text"] == "提交按钮"
        assert mock_page.evaluate.call_count == 2  # SCROLL_TO_TEXT_FUNC + GET_PAGE_STATE_FUNC

    @pytest.mark.asyncio
    async def test_scroll_to_text_target_not_in_viewport(self):
        """目标元素匹配但不在视口内时,target_visible应为False"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=[
            {"found": True, "text": "底部元素", "inViewport": False},
            {"url": "https://example.com", "title": "Test", "scrollY": 500},
        ])
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._refresh_interactive_cache = AsyncMock()
        result = await browser._scroll_to_text_impl("底部")
        assert result.success is True
        assert result.data["target_visible"] is False
        assert result.data["target_text"] == "底部元素"

    @pytest.mark.asyncio
    async def test_scroll_to_text_not_found(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        # SCROLL_TO_TEXT_FUNC返回null表示未找到目标文本
        mock_page.evaluate = AsyncMock(return_value=None)
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        result = await browser._scroll_to_text_impl("不存在的文本")
        assert result.success is False
        assert "未找到" in result.message

    @pytest.mark.asyncio
    async def test_scroll_to_text_empty_text_rejected(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        result = await browser._scroll_to_text_impl("   ")
        assert result.success is False
        assert "不能为空" in result.message

    @pytest.mark.asyncio
    async def test_scroll_to_text_handles_evaluate_error(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=Exception("eval error"))
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        result = await browser._scroll_to_text_impl("目标")
        assert result.success is False
        assert "滚动至文本失败" in result.message

    @pytest.mark.asyncio
    async def test_scroll_to_text_public_method_acquires_lock(self, caplog):
        """公开方法持有操作锁并记录指标"""
        import logging
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        browser._scroll_to_text_impl = AsyncMock(
            return_value=ToolResult(success=True, data={"matched_text": "目标"})
        )
        with caplog.at_level(logging.INFO):
            result = await browser.scroll_to_text("目标")
        assert result.success is True
        assert any("browser_action|scroll_to_text|success" in r.message for r in caplog.records)


class TestPlaywrightBrowserSnapshotVersion:
    """快照版本号机制测试 - 检测元素索引过期"""

    def test_initial_snapshot_version_is_zero(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        assert browser._snapshot_version == 0
        assert browser._current_snapshot_version() == 0

    @pytest.mark.asyncio
    async def test_extract_interactive_elements_increments_version(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=[{"tag": "button", "index": 0}])
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        await browser._extract_interactive_elements()
        assert browser._snapshot_version == 1
        await browser._extract_interactive_elements()
        assert browser._snapshot_version == 2

    @pytest.mark.asyncio
    async def test_click_returns_snapshot_version(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=True)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        browser.page = AsyncMock()
        browser.page.evaluate = AsyncMock(return_value={})
        browser._snapshot_version = 5
        result = await browser._click_impl(index=0)
        assert result.success is True
        assert result.data["snapshot_version"] == 5


class TestPlaywrightBrowserClickNavigationDetection:
    """点击导航检测测试 - 点击后URL变化时提供导航通知+页面状态

    会话0a288ffe根因: 旧版navigation_warning消息"可能误触了导航链接而非弹窗按钮。
    如需返回原页面请使用browser_navigate"导致LLM点击SPA侧边栏菜单后误以为出错,
    反复navigate返回原页面再重试(6次navigate)。改为navigation_info+page_state,
    让LLM直接感知新页面位置,无需额外browser_view确认。
    """

    @pytest.mark.asyncio
    async def test_click_provides_navigation_info_when_navigation_occurs(self):
        """点击导致URL变化时，结果应包含navigation_info和page_state"""
        from unittest.mock import PropertyMock, MagicMock
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=True)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        mock_page = MagicMock()
        # 模拟点击前后URL变化（列表页 -> 首页）
        type(mock_page).url = PropertyMock(side_effect=[
            "https://example.com/list",
            "https://example.com/home",
        ])
        mock_page.evaluate = AsyncMock(return_value={
            "url": "https://example.com/home",
            "title": "首页",
            "scrollHeight": 2000,
            "readyState": "complete",
        })
        browser.page = mock_page
        browser._snapshot_version = 3
        result = await browser._click_impl(index=0)
        assert result.success is True
        # navigation_info应为结构化dict,包含from_url/to_url/title
        assert "navigation_info" in result.data
        nav_info = result.data["navigation_info"]
        assert nav_info["from_url"] == "https://example.com/list"
        assert nav_info["to_url"] == "https://example.com/home"
        assert nav_info["title"] == "首页"
        # page_state应包含完整页面状态(供LLM判断滚动/加载状态)
        assert "page_state" in result.data
        assert result.data["page_state"]["url"] == "https://example.com/home"
        # 不应包含旧的navigation_warning字段
        assert "navigation_warning" not in result.data

    @pytest.mark.asyncio
    async def test_click_no_navigation_info_when_url_unchanged(self):
        """点击后URL未变化（弹窗场景）时，结果不应包含navigation_info"""
        from unittest.mock import PropertyMock, MagicMock
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=True)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        mock_page = MagicMock()
        type(mock_page).url = PropertyMock(return_value="https://example.com/list")
        browser.page = mock_page
        browser._snapshot_version = 3
        result = await browser._click_impl(index=0)
        assert result.success is True
        assert "navigation_info" not in result.data
        assert "navigation_warning" not in result.data

    @pytest.mark.asyncio
    async def test_click_text_branch_provides_navigation_info(self):
        """text分支点击导致导航时也应包含navigation_info"""
        from unittest.mock import PropertyMock, MagicMock
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        browser._locate_element_by_text = AsyncMock(return_value=mock_element)
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=True)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        mock_page = MagicMock()
        type(mock_page).url = PropertyMock(side_effect=[
            "https://shop.com/products",
            "https://shop.com/dashboard",
        ])
        mock_page.evaluate = AsyncMock(return_value={
            "url": "https://shop.com/dashboard",
            "title": "Dashboard",
        })
        browser.page = mock_page
        browser._snapshot_version = 7
        result = await browser._click_impl(text="查看详情")
        assert result.success is True
        assert "navigation_info" in result.data
        assert result.data["navigation_info"]["from_url"] == "https://shop.com/products"
        assert result.data["navigation_info"]["to_url"] == "https://shop.com/dashboard"

    @pytest.mark.asyncio
    async def test_navigation_info_no_misleading_language(self):
        """navigation_info不应包含"误触"或"返回原页面"等误导性措辞

        会话0a288ffe根因: 旧消息"可能误触了导航链接而非弹窗按钮。如需返回原页面
        请使用browser_navigate"导致LLM误以为点击出错而反复navigate返回。
        """
        from unittest.mock import PropertyMock, MagicMock
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=True)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        mock_page = MagicMock()
        type(mock_page).url = PropertyMock(side_effect=[
            "https://example.com/overview",
            "https://example.com/form",
        ])
        mock_page.evaluate = AsyncMock(return_value={
            "url": "https://example.com/form",
            "title": "Form 表单",
        })
        browser.page = mock_page
        browser._snapshot_version = 1
        result = await browser._click_impl(index=0)
        assert result.success is True
        assert "navigation_info" in result.data
        # navigation_info是dict,不含误导性文字
        nav_info = result.data["navigation_info"]
        assert isinstance(nav_info, dict)
        # 确保结果中不包含"误触"、"返回原页面"等误导性措辞
        import json
        result_str = json.dumps(result.data, ensure_ascii=False)
        assert "误触" not in result_str
        assert "返回原页面" not in result_str


class TestPlaywrightBrowserViewPageExtras:
    """view_page 截图与accessibility树辅助通道测试"""

    @pytest.mark.asyncio
    async def test_view_page_includes_screenshot_and_accessibility(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        browser._ensure_page = AsyncMock()
        browser.wait_for_page_load = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        browser._auto_dismiss_blocking_elements = AsyncMock(return_value=[])
        browser._extract_interactive_elements = AsyncMock(return_value=[])
        browser._extract_content = AsyncMock(return_value="content")
        browser._format_elements = AsyncMock(return_value=[])
        browser._take_view_screenshot = AsyncMock(return_value="base64data")
        browser._extract_accessibility_tree = AsyncMock(return_value="- button \"Submit\"")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={"url": "https://test.com"})
        browser.page = mock_page
        result = await browser.view_page()
        assert result.success is True
        assert result.data["screenshot"] == "base64data"
        assert result.data["accessibility_tree"] == "- button \"Submit\""
        assert "snapshot_version" in result.data

    @pytest.mark.asyncio
    async def test_flatten_accessibility_captures_semantic_fields(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        snapshot = {
            "role": "button", "name": "Submit", "value": "x",
            "checked": True, "children": [{"role": "textbox", "name": "Search"}],
        }
        lines = []
        browser._flatten_accessibility(snapshot, 0, lines)
        text = "\n".join(lines)
        assert "button" in text
        assert "Submit" in text
        assert "checked" in text
        assert "textbox" in text
        assert "Search" in text

    def test_flatten_accessibility_caps_node_count(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        from app.infrastructure.external.browser.playwright_browser import _ACCESSIBILITY_MAX_NODES
        browser = PlaywrightBrowser("ws://fake")
        node = {"role": "div", "name": "x", "children": []}
        # 构造超过上限的节点数
        root = {"role": "root", "name": "", "children": [dict(node) for _ in range(_ACCESSIBILITY_MAX_NODES + 50)]}
        lines = []
        browser._flatten_accessibility(root, 0, lines)
        assert len(lines) <= _ACCESSIBILITY_MAX_NODES


class TestPlaywrightBrowserXpathFallback:
    """元素定位三级回退测试 - data-manus-id → 语义属性 → XPath文本"""

    @pytest.mark.asyncio
    async def test_get_element_by_id_primary_selector_hit(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_el = AsyncMock()
        mock_page.query_selector = AsyncMock(return_value=mock_el)
        browser.page = mock_page
        browser.page.interactive_elements_cache = [{"tag": "button", "text": "OK"}]
        el = await browser._get_element_by_id(0)
        assert el is mock_el

    @pytest.mark.asyncio
    async def test_get_element_by_id_fallback_to_semantic_attrs(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        semantic_el = AsyncMock()
        # tag校验需返回与缓存meta一致的tag(小写)
        semantic_el.evaluate = AsyncMock(return_value="input")
        # 主选择器返回None，语义属性选择器命中
        mock_page.query_selector = AsyncMock(side_effect=[None, semantic_el])
        browser.page = mock_page
        browser.page.interactive_elements_cache = [{
            "tag": "input", "text": "",
            "semanticAttrs": {"aria-label": "Search"},
        }]
        el = await browser._get_element_by_id(0)
        assert el is semantic_el

    @pytest.mark.asyncio
    async def test_get_element_by_id_rejects_stale_tag_via_fallback(self):
        """SPA重渲染后同索引元素tag变化，回退定位应拒绝避免误操作"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        stale_el = AsyncMock()
        # 缓存meta记录tag=button，但实际元素已变成a标签
        stale_el.evaluate = AsyncMock(return_value="a")
        mock_page.query_selector = AsyncMock(side_effect=[None, stale_el, None])
        browser.page = mock_page
        browser.page.interactive_elements_cache = [{
            "tag": "button", "text": "提交",
            "semanticAttrs": {"aria-label": "Submit"},
        }]
        el = await browser._get_element_by_id(0)
        assert el is None  # tag不匹配，拒绝返回

    @pytest.mark.asyncio
    async def test_post_action_sync_refreshes_cache(self):
        """操作后刷新缓存而非清空，减少下次操作往返延迟"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        browser._sync_new_tab = AsyncMock()
        browser._wait_dom_stable = AsyncMock()
        browser._wait_for_loading_disappear = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        refresh_called = []
        browser._refresh_interactive_cache = AsyncMock(
            side_effect=lambda: refresh_called.append(True)
        )
        browser.page = AsyncMock()
        await browser._post_action_sync()
        assert len(refresh_called) == 1
        # 验证SPA内容就绪等待被调用(会话e5cce96a: click导航后内容为空)
        browser._wait_for_content_ready.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_locate_by_xpath_text_escapes_double_quotes(self):
        """含双引号文本用concat()构造XPath，避免注入"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_el = AsyncMock()
        captured = {}

        async def fake_query(selector):
            captured["selector"] = selector
            return mock_el

        mock_page.query_selector = fake_query
        browser.page = mock_page
        el = await browser._locate_by_xpath_text({"tag": "button", "text": '点击"我'})
        assert el is mock_el
        assert "concat" in captured["selector"]

    @pytest.mark.asyncio
    async def test_locate_by_xpath_text_returns_none_for_empty(self):
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        browser.page = AsyncMock()
        assert await browser._locate_by_xpath_text({"tag": "div", "text": ""}) is None
        assert await browser._locate_by_xpath_text({"tag": "div", "text": "[No text]"}) is None


class TestMultimodalToolMessage:
    """_build_tool_message_content 多模态工具消息组装测试"""

    def test_non_browser_view_returns_plain_text(self):
        from app.domain.services.agents.base import BaseAgent
        result = ToolResult(success=True, data={"result": "ok"})
        content = BaseAgent._build_tool_message_content(result, "shell_exec")
        assert isinstance(content, str)

    def test_browser_view_without_screenshot_returns_text(self):
        from app.domain.services.agents.base import BaseAgent
        result = ToolResult(success=True, data={"page_state": {"url": "https://x.com"}, "screenshot": None})
        content = BaseAgent._build_tool_message_content(result, "browser_view")
        assert isinstance(content, str)

    def test_browser_view_with_screenshot_returns_multimodal_array(self):
        from app.domain.services.agents.base import BaseAgent
        result = ToolResult(
            success=True,
            data={"page_state": {"url": "https://x.com"}, "screenshot": "AAAA"},
        )
        content = BaseAgent._build_tool_message_content(result, "browser_view")
        assert isinstance(content, list)
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_browser_view_screenshot_not_in_text(self):
        """截图base64不写入文本部分，避免上下文膨胀"""
        from app.domain.services.agents.base import BaseAgent
        result = ToolResult(
            success=True,
            data={"page_state": {"url": "https://x.com"}, "screenshot": "AAAA"},
        )
        content = BaseAgent._build_tool_message_content(result, "browser_view")
        text_part = content[0]["text"]
        assert "AAAA" not in text_part

    def test_mcp_images_returns_multimodal_array(self):
        """MCP工具返回images字段时构建多模态数组，每张图片一个image_url块"""
        from app.domain.services.agents.base import BaseAgent
        result = ToolResult(success=True, data={
            "text": "image generated",
            "images": [
                {"data": "IMG1", "mime_type": "image/png"},
                {"data": "IMG2", "mime_type": "image/webp"},
            ],
        })
        content = BaseAgent._build_tool_message_content(result, "mcp_test_gen_image")
        assert isinstance(content, list)
        # 1个文本 + 2个图片
        assert len(content) == 3
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"] == "data:image/png;base64,IMG1"
        assert content[2]["image_url"]["url"] == "data:image/webp;base64,IMG2"

    def test_mcp_images_text_excludes_base64(self):
        """MCP图片base64不写入文本部分，避免上下文膨胀"""
        from app.domain.services.agents.base import BaseAgent
        result = ToolResult(success=True, data={
            "text": "ok",
            "images": [{"data": "BBBB", "mime_type": "image/png"}],
        })
        content = BaseAgent._build_tool_message_content(result, "mcp_test_gen_image")
        text_part = content[0]["text"]
        assert "BBBB" not in text_part
        # images字段在文本中仅保留数量标记
        assert "[1 attached]" in text_part

    def test_non_multimodal_llm_skips_image_url(self):
        """supports_images=False时(非多模态LLM如DeepSeek),截图不构建image_url块,
        仅返回含[attached]标记的文本,避免API返回400错误(会话a34fcdc1根因)"""
        from app.domain.services.agents.base import BaseAgent
        result = ToolResult(
            success=True,
            data={"page_state": {"url": "https://x.com"}, "screenshot": "AAAA"},
        )
        content = BaseAgent._build_tool_message_content(
            result, "browser_view", supports_images=False
        )
        # 应返回纯文本字符串,而非多模态list
        assert isinstance(content, str)
        assert "AAAA" not in content  # base64不写入文本
        assert "[attached]" in content  # 截图存在标记保留

    def test_non_multimodal_llm_skips_mcp_images(self):
        """supports_images=False时,MCP工具images字段也不构建image_url块"""
        from app.domain.services.agents.base import BaseAgent
        result = ToolResult(success=True, data={
            "text": "ok",
            "images": [{"data": "CCCC", "mime_type": "image/png"}],
        })
        content = BaseAgent._build_tool_message_content(
            result, "mcp_test_gen_image", supports_images=False
        )
        assert isinstance(content, str)
        assert "CCCC" not in content
        assert "[1 attached]" in content

    def test_mcp_and_browser_screenshot_combined(self):
        """同时存在浏览器截图与MCP图片时，全部进入多模态数组"""
        from app.domain.services.agents.base import BaseAgent
        result = ToolResult(success=True, data={
            "screenshot": "SHOT",
            "images": [{"data": "EXTRA", "mime_type": "image/jpeg"}],
        })
        content = BaseAgent._build_tool_message_content(result, "browser_view")
        assert isinstance(content, list)
        # 1个文本 + 1个浏览器截图 + 1个MCP图片
        assert len(content) == 3
        assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,SHOT"
        assert content[2]["image_url"]["url"] == "data:image/jpeg;base64,EXTRA"

    def test_invalid_image_entries_are_filtered(self):
        """非字典或data非字符串的图片条目被过滤掉"""
        from app.domain.services.agents.base import BaseAgent
        result = ToolResult(success=True, data={
            "images": [
                "not_a_dict",
                {"data": "", "mime_type": "image/png"},  # 空data
                {"mime_type": "image/png"},  # 缺data字段
                {"data": 123, "mime_type": "image/png"},  # data非字符串
                {"data": "VALID", "mime_type": "image/png"},  # 合法
            ],
        })
        content = BaseAgent._build_tool_message_content(result, "mcp_test_gen_image")
        # 仅1张合法图片 + 1个文本
        assert isinstance(content, list)
        assert len(content) == 2
        assert content[1]["image_url"]["url"] == "data:image/png;base64,VALID"

    def test_mcp_image_default_mime_when_missing(self):
        """MCP图片缺少mime_type时回退为image/png"""
        from app.domain.services.agents.base import BaseAgent
        result = ToolResult(success=True, data={
            "images": [{"data": "IMG"}],  # 无 mime_type
        })
        content = BaseAgent._build_tool_message_content(result, "mcp_test_gen_image")
        assert content[1]["image_url"]["url"] == "data:image/png;base64,IMG"


class TestScreenshotCompression:
    """截图base64不污染压缩文本测试"""

    def test_compress_browser_view_uses_screenshot_marker(self):
        """Layer1压缩时截图字段仅留[attached]标记，不写入base64"""
        memory = Memory()
        content = json.dumps({
            "page_state": {"url": "https://example.com", "title": "T"},
            "interactive_elements": ["a", "b"],
            "screenshot": "BASE64DATA" * 100,
        })
        memory.add_message({"role": "tool", "function_name": "browser_view", "content": content})
        memory.compact()
        compressed = memory.messages[0]["content"]
        assert "[screenshot: attached]" in compressed
        assert "BASE64DATA" not in compressed

    def test_truncate_tool_result_drops_screenshot_base64(self):
        """截断时screenshot字段替换为[attached]，不保留base64"""
        long_content = json.dumps({
            "page_state": {"url": "https://example.com", "title": "T"},
            "screenshot": "B" * 10000,
        })
        truncated = Memory.truncate_tool_result(long_content, "browser_view")
        assert "BBBB" not in truncated
        assert "[attached]" in truncated

    def test_strip_image_data_replaces_image_url(self):
        """strip_image_data将image_url替换为文本标记"""
        messages = [{
            "role": "tool", "function_name": "browser_view",
            "content": [
                {"type": "text", "text": "page info"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
            ],
        }]
        cleaned = Memory.strip_image_data(messages)
        content = cleaned[0]["content"]
        assert isinstance(content, list)
        assert all(item.get("type") != "image_url" for item in content)
        assert any("removed" in item.get("text", "") for item in content)


class TestPlaywrightBrowserNavigateRetry:
    """导航重试机制测试 - 应对瞬时网络抖动,与initialize重试策略对称"""

    @pytest.mark.asyncio
    async def test_navigate_succeeds_on_first_try_without_retry(self):
        """首次导航成功时不触发重试,page.goto仅调用一次"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(return_value=None)
        mock_page.evaluate = AsyncMock(return_value={"url": "https://x.com", "title": "T"})
        mock_page.interactive_elements_cache = []
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._wait_dom_stable = AsyncMock()
        browser._wait_for_loading_disappear = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        browser._auto_dismiss_blocking_elements = AsyncMock(return_value=[])
        browser._extract_interactive_elements = AsyncMock(return_value=[])
        browser._format_elements = AsyncMock(return_value=[])
        result = await browser._navigate_impl("https://x.com")
        assert result.success is True
        assert mock_page.goto.call_count == 1

    @pytest.mark.asyncio
    async def test_navigate_retries_on_failure(self):
        """首次失败后重试成功,总尝试次数=2"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(side_effect=[
            Exception("network timeout"),
            None,  # 重试成功
        ])
        mock_page.evaluate = AsyncMock(return_value={"url": "https://x.com", "title": "T"})
        mock_page.interactive_elements_cache = []
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._wait_dom_stable = AsyncMock()
        browser._wait_for_loading_disappear = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        browser._auto_dismiss_blocking_elements = AsyncMock(return_value=[])
        browser._extract_interactive_elements = AsyncMock(return_value=[])
        browser._format_elements = AsyncMock(return_value=[])
        result = await browser._navigate_impl("https://x.com")
        assert result.success is True
        assert mock_page.goto.call_count == 2

    @pytest.mark.asyncio
    async def test_navigate_fails_after_all_retries(self):
        """所有重试均失败后返回失败,消息含尝试次数"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(side_effect=Exception("permanent failure"))
        mock_page.interactive_elements_cache = []
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        result = await browser._navigate_impl("https://x.com")
        assert result.success is False
        # _NAVIGATE_RETRIES=2, 总尝试3次
        assert mock_page.goto.call_count == 3
        assert "已重试3次" in result.message


class TestPlaywrightBrowserExtractContentLLMFallback:
    """_extract_content LLM摘要降级测试 - 异常不静默,记录日志与指标"""

    @pytest.mark.asyncio
    async def test_llm_summary_success(self):
        """LLM摘要成功时返回摘要内容"""
        from app.infrastructure.external.browser import playwright_browser as pb_module
        # _CONTENT_SUMMARY默认关闭,测试需临时开启
        original = pb_module._CONTENT_SUMMARY
        pb_module._CONTENT_SUMMARY = True
        try:
            from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
            browser = PlaywrightBrowser("ws://fake")
            mock_page = AsyncMock()
            # 返回足够长的内容触发LLM摘要(>5000字符)
            long_dom = json.dumps({"tag": "div", "children": [{"tag": "p", "text": "x" * 6000}]})
            mock_page.evaluate = AsyncMock(return_value=long_dom)
            browser.page = mock_page
            browser._ensure_page = AsyncMock()
            mock_llm = AsyncMock()
            mock_llm.invoke = AsyncMock(return_value={"content": "摘要内容"})
            browser.llm = mock_llm
            content = await browser._extract_content()
            assert content == "摘要内容"
            mock_llm.invoke.assert_called_once()
        finally:
            pb_module._CONTENT_SUMMARY = original

    @pytest.mark.asyncio
    async def test_llm_summary_failure_falls_back_to_original(self, caplog):
        """LLM调用失败时降级返回原始内容截断,记录warning日志"""
        import logging
        from app.infrastructure.external.browser import playwright_browser as pb_module
        original = pb_module._CONTENT_SUMMARY
        pb_module._CONTENT_SUMMARY = True
        try:
            from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
            browser = PlaywrightBrowser("ws://fake")
            mock_page = AsyncMock()
            long_dom = json.dumps({"tag": "div", "children": [{"tag": "p", "text": "x" * 6000}]})
            mock_page.evaluate = AsyncMock(return_value=long_dom)
            browser.page = mock_page
            browser._ensure_page = AsyncMock()
            mock_llm = AsyncMock()
            mock_llm.invoke = AsyncMock(side_effect=Exception("LLM unavailable"))
            browser.llm = mock_llm
            with caplog.at_level(logging.WARNING):
                content = await browser._extract_content()
            # 降级返回原始内容
            assert "x" * 6000 in content
            assert any("LLM内容摘要失败" in r.message for r in caplog.records)
        finally:
            pb_module._CONTENT_SUMMARY = original

    @pytest.mark.asyncio
    async def test_short_content_skips_llm(self):
        """短内容(<5000字符)不触发LLM摘要"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        short_dom = json.dumps({"tag": "div", "text": "short"})
        mock_page.evaluate = AsyncMock(return_value=short_dom)
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        mock_llm = AsyncMock()
        browser.llm = mock_llm
        content = await browser._extract_content()
        assert "short" in content
        mock_llm.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_dom_extraction_failure_falls_back_to_html(self, caplog):
        """DOM结构提取失败时回退到markdownify(HTML模式)"""
        import logging
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        # GET_VISIBLE_CONTENT_FUNC失败,content()返回HTML
        mock_page.evaluate = AsyncMock(side_effect=Exception("eval fail"))
        mock_page.content = AsyncMock(return_value="<html><body><p>HTML fallback</p></body></html>")
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        with caplog.at_level(logging.WARNING):
            content = await browser._extract_content()
        assert "HTML fallback" in content
        assert any("DOM结构提取失败" in r.message for r in caplog.records)


class TestPlaywrightBrowserClickStrategies:
    """五级点击容错策略测试 - normal→scroll→force→coordinate→JS dispatch"""

    @pytest.mark.asyncio
    async def test_normal_click_succeeds_first_strategy(self):
        """第一级normal click成功,不触发后续策略"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        # normal click直接成功
        mock_element.click = AsyncMock(return_value=None)
        success = await browser._click_with_retry(mock_element)
        assert success is True
        assert mock_element.click.call_count == 1

    @pytest.mark.asyncio
    async def test_click_falls_back_through_all_strategies(self):
        """前4级全失败,第5级JS dispatch成功"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        # normal/scroll_then/force都抛异常,coordinate bounding_box返回None,JS dispatch成功
        mock_element.click = AsyncMock(side_effect=Exception("click fail"))
        mock_element.bounding_box = AsyncMock(return_value=None)
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=True)
        mock_page.mouse = AsyncMock()
        browser.page = mock_page
        success = await browser._click_with_retry(mock_element)
        assert success is True
        # 验证JS dispatch策略被触发(page.evaluate调用DISPATCH_CLICK_FUNC)
        assert mock_page.evaluate.call_count >= 1

    @pytest.mark.asyncio
    async def test_click_all_strategies_fail_returns_false(self):
        """五级策略全部失败时返回False"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        mock_element.click = AsyncMock(side_effect=Exception("fail"))
        mock_element.bounding_box = AsyncMock(return_value=None)
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=Exception("js fail"))
        browser.page = mock_page
        success = await browser._click_with_retry(mock_element)
        assert success is False

    @pytest.mark.asyncio
    async def test_click_impl_returns_failure_message_when_all_strategies_fail(self):
        """_click_impl在五级策略全失败时返回失败消息"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=False)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        result = await browser._click_impl(index=0)
        assert result.success is False
        assert "5种策略" in result.message

    @pytest.mark.asyncio
    async def test_click_impl_refreshes_cache_when_index_stale(self):
        """索引过期时刷新缓存重试一次"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        # 第一次_get_element_by_id返回None,第二次返回元素
        mock_element = AsyncMock()
        browser._get_element_by_id = AsyncMock(side_effect=[None, mock_element])
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=True)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        browser._extract_interactive_elements = AsyncMock()
        browser.page = AsyncMock()
        result = await browser._click_impl(index=0)
        assert result.success is True


class TestPlaywrightBrowserClickByText:
    """文本语义定位元素测试 - get_by_role→get_by_text(exact)→get_by_text(contains)→JS回退"""

    @staticmethod
    def _make_mock_locator(count_val=0, element=None):
        """创建模拟Playwright Locator对象(count+first.element_handle)"""
        locator = MagicMock()
        locator.count = AsyncMock(return_value=count_val)
        first = MagicMock()
        first.element_handle = AsyncMock(return_value=element)
        locator.first = first
        return locator

    @pytest.mark.asyncio
    async def test_locate_by_text_via_role(self):
        """策略1: get_by_role(name=exact)命中,不触发后续策略"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = MagicMock()
        hit_locator = self._make_mock_locator(count_val=1, element=mock_element)
        miss_locator = self._make_mock_locator(count_val=0)
        mock_page = AsyncMock()
        # button角色命中,其他角色未命中(role为位置参数)
        mock_page.get_by_role = MagicMock(side_effect=lambda role, **kwargs: hit_locator if role == "button" else miss_locator)
        browser.page = mock_page
        result = await browser._locate_element_by_text("提交")
        assert result is mock_element

    @pytest.mark.asyncio
    async def test_locate_by_text_via_get_by_text_exact(self):
        """策略2: 所有role未命中,get_by_text(exact)命中"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = MagicMock()
        miss_locator = self._make_mock_locator(count_val=0)
        hit_locator = self._make_mock_locator(count_val=1, element=mock_element)
        mock_page = AsyncMock()
        mock_page.get_by_role = MagicMock(return_value=miss_locator)
        # get_by_text(exact=True)命中, get_by_text(contains)不调用
        mock_page.get_by_text = MagicMock(side_effect=lambda text, exact=None: hit_locator if exact else self._make_mock_locator(count_val=0))
        browser.page = mock_page
        result = await browser._locate_element_by_text("提交")
        assert result is mock_element

    @pytest.mark.asyncio
    async def test_locate_by_text_via_get_by_text_contains(self):
        """策略3: exact未命中,contains唯一匹配(count==1)命中"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = MagicMock()
        miss_locator = self._make_mock_locator(count_val=0)
        hit_locator = self._make_mock_locator(count_val=1, element=mock_element)
        mock_page = AsyncMock()
        mock_page.get_by_role = MagicMock(return_value=miss_locator)
        # exact命中0个,contains命中1个(唯一匹配)
        mock_page.get_by_text = MagicMock(side_effect=lambda text, exact=None: miss_locator if exact else hit_locator)
        browser.page = mock_page
        result = await browser._locate_element_by_text("提交")
        assert result is mock_element

    @pytest.mark.asyncio
    async def test_locate_by_text_via_js_fallback(self):
        """策略4: locator API全未命中,JS evaluate_handle回退命中"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = MagicMock()
        miss_locator = self._make_mock_locator(count_val=0)
        mock_page = AsyncMock()
        mock_page.get_by_role = MagicMock(return_value=miss_locator)
        mock_page.get_by_text = MagicMock(return_value=miss_locator)
        mock_handle = MagicMock()
        mock_handle.as_element = MagicMock(return_value=mock_element)
        mock_page.evaluate_handle = AsyncMock(return_value=mock_handle)
        browser.page = mock_page
        result = await browser._locate_element_by_text("搜索")
        assert result is mock_element

    @pytest.mark.asyncio
    async def test_locate_by_text_not_found(self):
        """全部策略未命中返回None"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        miss_locator = self._make_mock_locator(count_val=0)
        mock_page = AsyncMock()
        mock_page.get_by_role = MagicMock(return_value=miss_locator)
        mock_page.get_by_text = MagicMock(return_value=miss_locator)
        mock_handle = MagicMock()
        mock_handle.as_element = MagicMock(return_value=None)
        mock_page.evaluate_handle = AsyncMock(return_value=mock_handle)
        browser.page = mock_page
        result = await browser._locate_element_by_text("不存在")
        assert result is None

    @pytest.mark.asyncio
    async def test_locate_by_text_empty_text(self):
        """空文本直接返回None,不调用任何locator API"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        browser.page = mock_page
        result = await browser._locate_element_by_text("")
        assert result is None
        mock_page.get_by_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_click_impl_text_branch_success(self):
        """text分支完整流程成功: 定位→interactable检查→五级容错点击"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = MagicMock()
        browser._locate_element_by_text = AsyncMock(return_value=mock_element)
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=True)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        result = await browser._click_impl(text="商品审核")
        assert result.success is True
        browser._locate_element_by_text.assert_called_with("商品审核")

    @pytest.mark.asyncio
    async def test_click_impl_text_branch_not_found(self):
        """text定位失败(刷新重试后仍找不到)返回错误消息"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        browser._locate_element_by_text = AsyncMock(return_value=None)
        browser._ensure_page = AsyncMock()
        browser._extract_interactive_elements = AsyncMock()
        result = await browser._click_impl(text="不存在的按钮")
        assert result.success is False
        assert "未找到" in result.message

    @pytest.mark.asyncio
    async def test_click_impl_text_priority_over_index(self):
        """同时传text+index时text优先,_get_element_by_id不应被调用"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = MagicMock()
        browser._locate_element_by_text = AsyncMock(return_value=mock_element)
        browser._get_element_by_id = AsyncMock()
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=True)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        result = await browser._click_impl(text="提交", index=0)
        assert result.success is True
        browser._locate_element_by_text.assert_called_once_with("提交")
        browser._get_element_by_id.assert_not_called()


class TestPlaywrightBrowserInputStrategies:
    """三级输入容错策略测试 - keyboard→fill→JS(框架兼容优先)"""

    @pytest.mark.asyncio
    async def test_keyboard_succeeds_first_strategy(self):
        """第一级keyboard.type成功(click→Control+a→Backspace→type),不触发后续策略"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        mock_element.click = AsyncMock(return_value=None)
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        mock_page = AsyncMock()
        mock_page.keyboard = AsyncMock()
        browser.page = mock_page
        result = await browser._input_impl("hello", False, index=0)
        assert result.success is True
        mock_element.click.assert_called_once()
        mock_page.keyboard.press.assert_any_call("Control+a")
        mock_page.keyboard.press.assert_any_call("Backspace")
        mock_page.keyboard.type.assert_called_once_with("hello")

    @pytest.mark.asyncio
    async def test_input_falls_back_to_fill(self):
        """策略1(click)失败后回退到策略2(fill)"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        mock_element.click = AsyncMock(side_effect=Exception("click fail"))
        mock_element.fill = AsyncMock(return_value=None)
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        mock_page = AsyncMock()
        mock_page.keyboard = AsyncMock()
        browser.page = mock_page
        result = await browser._input_impl("hello", False, index=0)
        assert result.success is True
        mock_element.fill.assert_called_once_with("hello")

    @pytest.mark.asyncio
    async def test_input_falls_back_to_js(self):
        """策略1(click)+策略2(fill)都失败后回退到JS赋值+完整事件序列(input+change+keyup+blur)"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        mock_element.click = AsyncMock(side_effect=Exception("click fail"))
        mock_element.fill = AsyncMock(side_effect=Exception("fill fail"))
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=None)
        browser.page = mock_page
        result = await browser._input_impl("hello", False, index=0)
        assert result.success is True
        # 验证JS赋值脚本被调用且包含keyup+blur事件(Angular/Element NgZone变更检测需要)
        assert mock_page.evaluate.call_count >= 1
        js_script = str(mock_page.evaluate.call_args)
        assert "keyup" in js_script
        assert "blur" in js_script

    @pytest.mark.asyncio
    async def test_input_with_press_enter(self):
        """press_enter=True时策略1成功后按回车"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_element = AsyncMock()
        mock_element.click = AsyncMock(return_value=None)
        browser._get_element_by_id = AsyncMock(return_value=mock_element)
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        mock_page = AsyncMock()
        mock_page.keyboard = AsyncMock()
        browser.page = mock_page
        result = await browser._input_impl("hello", True, index=0)
        assert result.success is True
        mock_page.keyboard.press.assert_any_call("Enter")

    @pytest.mark.asyncio
    async def test_input_no_index_no_coordinate_returns_failure(self):
        """既无index也无坐标时返回失败"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        browser._ensure_page = AsyncMock()
        result = await browser._input_impl("hello", False)
        assert result.success is False
        assert "index或coordinate" in result.message


class TestPlaywrightBrowserConsoleExecTimeout:
    """console_exec超时保护测试 - 防止死循环JS阻塞浏览器实例"""

    @pytest.mark.asyncio
    async def test_console_exec_normal_returns_result(self):
        """正常JS执行返回结果"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={"computed": 42})
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        result = await browser.console_exec("() => 1+1")
        assert result.success is True
        assert result.data["result"]["computed"] == 42

    @pytest.mark.asyncio
    async def test_console_exec_waits_for_content_ready(self):
        """console_exec执行前等待内容就绪(会话e5cce96a根因: SPA路由切换中evaluate返回空)"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value="Element Plus")
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        result = await browser.console_exec("return document.title")
        # 验证_wait_for_content_ready被调用(确保JS在内容就绪后执行)
        browser._wait_for_content_ready.assert_awaited_once_with(timeout=5)
        assert result.success is True
        assert result.data["result"] == "Element Plus"

    @pytest.mark.asyncio
    async def test_console_exec_timeout_returns_failure(self):
        """JS执行超时时返回失败,消息含超时秒数"""
        import asyncio
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser, _CONSOLE_EXEC_TIMEOUT
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()

        # 模拟超时: INJECT_CONSOLE_LOGS_FUNC立即返回, 用户JS永不返回
        call_count = [0]
        async def slow_evaluate(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # INJECT_CONSOLE_LOGS_FUNC立即返回
            await asyncio.sleep(100)  # 用户JS挂起
            return None

        mock_page.evaluate = slow_evaluate
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        # 用 monkey patch 缩短超时以加速测试
        import app.infrastructure.external.browser.playwright_browser as mod
        original = mod._CONSOLE_EXEC_TIMEOUT
        mod._CONSOLE_EXEC_TIMEOUT = 0.1
        try:
            result = await browser.console_exec("while(true){}")
        finally:
            mod._CONSOLE_EXEC_TIMEOUT = original
        assert result.success is False
        assert "超时" in result.message

    @pytest.mark.asyncio
    async def test_console_exec_handles_evaluate_error(self):
        """JS执行抛异常时返回失败"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=Exception("syntax error"))
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        result = await browser.console_exec("invalid js")
        assert result.success is False
        assert "JS执行失败" in result.message


class TestPlaywrightBrowserDismissBlocking:
    """阻塞元素自动消除测试 - cookie banner/loading overlay(不含应用弹窗el-dialog)"""

    @pytest.mark.asyncio
    async def test_dismiss_blocking_elements_closes_cookie_banner(self):
        """检测到cookie_banner后调用close选择器消除"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=[
            [{"category": "cookie_banner", "closeSelectors": [".accept"]}],
            True,
        ])
        browser.page = mock_page
        dismissed = await browser._auto_dismiss_blocking_elements()
        assert "cookie_banner" in dismissed

    @pytest.mark.asyncio
    async def test_dismiss_blocking_loading_overlay_waits(self):
        """loading_overlay类别触发等待而非close"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=[
            {"category": "loading_overlay", "closeSelectors": []},
        ])
        browser.page = mock_page
        browser._wait_for_loading_disappear = AsyncMock()
        dismissed = await browser._auto_dismiss_blocking_elements()
        assert "loading_overlay" in dismissed
        browser._wait_for_loading_disappear.assert_called_once()

    @pytest.mark.asyncio
    async def test_dismiss_blocking_fallback_escape_key(self):
        """检测到阻塞但未消除时按Escape兜底"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=[
            [{"category": "cookie_banner", "closeSelectors": [".nonexistent"]}],
            False,
        ])
        mock_page.keyboard = AsyncMock()
        browser.page = mock_page
        dismissed = await browser._auto_dismiss_blocking_elements()
        assert "escape_key" in dismissed
        mock_page.keyboard.press.assert_called_with("Escape")

    @pytest.mark.asyncio
    async def test_dismiss_blocking_no_blocking_returns_empty(self):
        """无阻塞元素时返回空列表"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=[])
        browser.page = mock_page
        dismissed = await browser._auto_dismiss_blocking_elements()
        assert dismissed == []

    @pytest.mark.asyncio
    async def test_dismiss_blocking_handles_detection_error(self):
        """检测阻塞元素异常时不中断主流程"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=Exception("detect fail"))
        browser.page = mock_page
        dismissed = await browser._auto_dismiss_blocking_elements()
        assert dismissed == []


class TestPlaywrightBrowserDomStableFallback:
    """_wait_dom_stable异常降级测试"""

    @pytest.mark.asyncio
    async def test_dom_stable_succeeds(self):
        """正常执行JS等待DOM稳定"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=True)
        browser.page = mock_page
        await browser._wait_dom_stable()
        mock_page.evaluate.assert_called_once()

    @pytest.mark.asyncio
    async def test_dom_stable_falls_back_to_sleep(self):
        """JS执行异常时降级到asyncio.sleep(1)"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=Exception("eval fail"))
        browser.page = mock_page
        # 不应抛异常
        await browser._wait_dom_stable()
        mock_page.evaluate.assert_called_once()

    @pytest.mark.asyncio
    async def test_dom_stable_passes_timeout_arg(self):
        """timeout参数正确传递给JS(毫秒)"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=True)
        browser.page = mock_page
        await browser._wait_dom_stable(timeout=15)
        # 验证timeout以毫秒传递(位置参数)
        call_args = mock_page.evaluate.call_args
        # 第二个位置参数应为15000
        assert call_args.args[1] == 15000


class TestPlaywrightBrowserSyncNewTab:
    """SPA+新标签页加固测试 - 智能体打开新页面场景"""

    def test_is_blank_page_url_recognizes_about_blank(self):
        """about:blank被识别为空白页"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        assert PlaywrightBrowser._is_blank_page_url("about:blank") is True

    def test_is_blank_page_url_recognizes_chrome_urls(self):
        """chrome://系列URL被识别为空白页"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        assert PlaywrightBrowser._is_blank_page_url("chrome://newtab/") is True
        assert PlaywrightBrowser._is_blank_page_url("chrome://new-tab-page/") is True

    def test_is_blank_page_url_recognizes_extended_urls(self):
        """扩展的空白URL前缀(chrome-search/javascript/edge/view-source)被识别"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        assert PlaywrightBrowser._is_blank_page_url("chrome-search://local-ntp/") is True
        assert PlaywrightBrowser._is_blank_page_url("javascript:void(0)") is True
        assert PlaywrightBrowser._is_blank_page_url("edge://newtab/") is True
        assert PlaywrightBrowser._is_blank_page_url("view-source:https://x.com") is True

    def test_is_blank_page_url_rejects_normal_urls(self):
        """正常URL不被识别为空白页"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        assert PlaywrightBrowser._is_blank_page_url("https://example.com") is False
        assert PlaywrightBrowser._is_blank_page_url("http://localhost:8000") is False

    def test_is_blank_page_url_treats_empty_as_blank(self):
        """空URL视为空白页"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        assert PlaywrightBrowser._is_blank_page_url("") is True

    @pytest.mark.asyncio
    async def test_sync_new_tab_closes_blank_old_tabs(self):
        """多个标签页时关闭空白旧标签"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        old_page = AsyncMock()
        old_page.url = "about:blank"
        new_page = AsyncMock()
        new_page.url = "https://example.com"
        mock_context = AsyncMock()
        mock_context.pages = [old_page, new_page]
        mock_browser = AsyncMock()
        mock_browser.contexts = [mock_context]
        browser.browser = mock_browser
        browser._wait_page_interactive = AsyncMock(return_value=True)
        await browser._sync_new_tab()
        old_page.close.assert_called_once()
        assert browser.page is new_page

    @pytest.mark.asyncio
    async def test_sync_new_tab_keeps_non_blank_old_tabs(self):
        """非空白旧标签不被关闭"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        old_page = AsyncMock()
        old_page.url = "https://old-site.com"  # 非空白
        new_page = AsyncMock()
        new_page.url = "https://new-site.com"
        mock_context = AsyncMock()
        mock_context.pages = [old_page, new_page]
        mock_browser = AsyncMock()
        mock_browser.contexts = [mock_context]
        browser.browser = mock_browser
        browser._wait_page_interactive = AsyncMock(return_value=True)
        await browser._sync_new_tab()
        old_page.close.assert_not_called()
        assert browser.page is new_page

    @pytest.mark.asyncio
    async def test_sync_new_tab_waits_for_ready_state(self):
        """切换前等待新page readyState就绪"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        # 真实场景:智能体点击target=_blank后存在旧空白标签+新标签两个page
        old_page = AsyncMock()
        old_page.url = "about:blank"
        new_page = AsyncMock()
        new_page.url = "https://example.com"
        mock_context = AsyncMock()
        mock_context.pages = [old_page, new_page]
        mock_browser = AsyncMock()
        mock_browser.contexts = [mock_context]
        browser.browser = mock_browser
        wait_called = []
        browser._wait_page_interactive = AsyncMock(side_effect=lambda p, t: wait_called.append(True))
        await browser._sync_new_tab()
        assert len(wait_called) == 1

    @pytest.mark.asyncio
    async def test_sync_new_tab_single_page_no_op(self):
        """仅一个标签页时不切换"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        only_page = AsyncMock()
        mock_context = AsyncMock()
        mock_context.pages = [only_page]
        mock_browser = AsyncMock()
        mock_browser.contexts = [mock_context]
        browser.browser = mock_browser
        original_page = browser.page
        await browser._sync_new_tab()
        # 单页面不进入切换逻辑
        assert browser.page is original_page

    @pytest.mark.asyncio
    async def test_wait_page_interactive_returns_true_when_ready(self):
        """page readyState=complete时立即返回True"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=True)
        result = await browser._wait_page_interactive(mock_page, timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_page_interactive_returns_false_on_timeout(self):
        """page readyState长时间未就绪时超时返回False"""
        import asyncio
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=False)  # 永不就绪
        result = await browser._wait_page_interactive(mock_page, timeout=0.3)
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_page_interactive_returns_false_on_error(self):
        """evaluate异常时返回False"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=Exception("eval fail"))
        result = await browser._wait_page_interactive(mock_page, timeout=0.5)
        assert result is False


class TestPlaywrightBrowserLoadingSelectors:
    """_wait_for_loading_disappear选择器扩展测试"""

    @pytest.mark.asyncio
    async def test_loading_disappear_returns_when_no_loading(self):
        """无loading元素时立即返回"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        # query_selector返回None
        mock_page.query_selector = AsyncMock(return_value=None)
        browser.page = mock_page
        await browser._wait_for_loading_disappear(timeout=1)
        # 至少调用一次query_selector
        assert mock_page.query_selector.call_count >= 1

    @pytest.mark.asyncio
    async def test_loading_discover_includes_generic_selectors(self):
        """验证扩展的通用选择器被检查(loading-container/page-loading)"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        captured_selectors = []

        async def fake_query(sel):
            captured_selectors.append(sel)
            return None

        mock_page.query_selector = fake_query
        browser.page = mock_page
        await browser._wait_for_loading_disappear(timeout=1)
        # 验证通用选择器被检查
        assert any("loading-container" in s for s in captured_selectors)
        assert any("page-loading" in s for s in captured_selectors)


class TestPlaywrightBrowserDialogScroll:
    """弹窗内容滚动测试 - 弹窗打开时scroll_down/up滚动弹窗体而非主窗口"""

    @pytest.mark.asyncio
    async def test_scroll_dialog_returns_true_when_dialog_exists(self):
        """弹窗存在时_scroll_dialog返回True"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=True)
        browser.page = mock_page
        result = await browser._scroll_dialog("down", False)
        assert result is True

    @pytest.mark.asyncio
    async def test_scroll_dialog_returns_false_when_no_dialog(self):
        """无弹窗时_scroll_dialog返回False"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=False)
        browser.page = mock_page
        result = await browser._scroll_dialog("down", False)
        assert result is False

    @pytest.mark.asyncio
    async def test_scroll_dialog_returns_false_on_exception(self):
        """_scroll_dialog异常时返回False不中断"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=Exception("eval fail"))
        browser.page = mock_page
        result = await browser._scroll_dialog("up", True)
        assert result is False

    @pytest.mark.asyncio
    async def test_scroll_down_with_dialog_scrolls_dialog_not_page(self):
        """弹窗打开时scroll_down滚动弹窗,不调用主窗口scrollBy"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        # _scroll_dialog返回True → 跳过主窗口scrollBy → 只调用GET_PAGE_STATE_FUNC
        state = {"url": "https://test.com", "hasDialog": True, "dialogInfo": {"canScroll": True}}
        mock_page.evaluate = AsyncMock(return_value=state)
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._refresh_interactive_cache = AsyncMock()
        result = await browser.scroll_down()
        assert result.success is True
        assert result.data["page_state"]["hasDialog"] is True

    @pytest.mark.asyncio
    async def test_scroll_up_with_dialog_scrolls_dialog_not_page(self):
        """弹窗打开时scroll_up滚动弹窗,不调用主窗口scrollTo"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        state = {"url": "https://test.com", "hasDialog": True}
        mock_page.evaluate = AsyncMock(return_value=state)
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._refresh_interactive_cache = AsyncMock()
        result = await browser.scroll_up(to_top=True)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_scroll_down_without_dialog_scrolls_page(self):
        """无弹窗时scroll_down滚动主窗口"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        mock_page = AsyncMock()
        state = {"url": "https://test.com", "hasDialog": False}
        mock_page.evaluate = AsyncMock(return_value=state)
        browser.page = mock_page
        browser._ensure_page = AsyncMock()
        browser._refresh_interactive_cache = AsyncMock()
        result = await browser.scroll_down()
        assert result.success is True


class TestPlaywrightBrowserDialogElementMarking:
    """弹窗元素标记测试 - _format_elements显示[dialog]标记"""

    @pytest.mark.asyncio
    async def test_format_elements_includes_dialog_mark(self):
        """弹窗内元素显示[dialog]标记"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        elements = [
            {"index": 0, "tag": "button", "text": "提交", "inViewport": True, "inShadowDOM": False, "inDialog": True},
            {"index": 1, "tag": "a", "text": "返回", "inViewport": True, "inShadowDOM": False, "inDialog": False},
        ]
        formatted = await browser._format_elements(elements)
        assert "[dialog]" in formatted[0]
        assert "[dialog]" not in formatted[1]

    @pytest.mark.asyncio
    async def test_format_elements_dialog_mark_with_offscreen(self):
        """弹窗内+视窗外元素同时显示[offscreen][dialog]标记"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        elements = [
            {"index": 0, "tag": "input", "text": "[搜索]", "inViewport": False, "inShadowDOM": False, "inDialog": True},
        ]
        formatted = await browser._format_elements(elements)
        # offscreen元素位于分隔符之后的元素行(可见性优先排序:offscreen分区展示)
        element_lines = [l for l in formatted if "[offscreen]" in l and "---" not in l]
        assert len(element_lines) == 1, "应有一个offscreen元素行"
        assert "[dialog]" in element_lines[0], "offscreen元素行应同时含[dialog]标记"

    @pytest.mark.asyncio
    async def test_format_elements_no_dialog_mark_by_default(self):
        """无inDialog字段时不显示[dialog]标记(向后兼容)"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
        browser = PlaywrightBrowser("ws://fake")
        elements = [
            {"index": 0, "tag": "button", "text": "点击", "inViewport": True, "inShadowDOM": False},
        ]
        formatted = await browser._format_elements(elements)
        assert "[dialog]" not in formatted[0]


class TestPlaywrightBrowserDialogNotBlocking:
    """应用弹窗(el-dialog)不被误判为阻塞元素测试"""

    @pytest.mark.asyncio
    async def test_el_dialog_not_in_blocking_categories(self):
        """DETECT_BLOCKING_ELEMENTS_FUNC不含modal_dialog类别,el-dialog不会被检测"""
        from app.infrastructure.external.browser.playwright_browser_fun import DETECT_BLOCKING_ELEMENTS_FUNC
        # 验证JS函数中不包含el-dialog__wrapper和modal_dialog
        assert "el-dialog__wrapper" not in DETECT_BLOCKING_ELEMENTS_FUNC
        assert "ant-modal-wrap" not in DETECT_BLOCKING_ELEMENTS_FUNC
        assert "modal_dialog" not in DETECT_BLOCKING_ELEMENTS_FUNC

    @pytest.mark.asyncio
    async def test_page_state_detects_el_dialog(self):
        """GET_PAGE_STATE_FUNC包含el-dialog弹窗检测"""
        from app.infrastructure.external.browser.playwright_browser_fun import GET_PAGE_STATE_FUNC
        assert "el-dialog" in GET_PAGE_STATE_FUNC
        assert "el-drawer" in GET_PAGE_STATE_FUNC
        assert "dialogInfo" in GET_PAGE_STATE_FUNC
        assert "canScroll" in GET_PAGE_STATE_FUNC

    @pytest.mark.asyncio
    async def test_interactive_elements_marks_in_dialog(self):
        """GET_INTERACTIVE_ELEMENTS_FUNC包含inDialog标记"""
        from app.infrastructure.external.browser.playwright_browser_fun import GET_INTERACTIVE_ELEMENTS_FUNC
        assert "inDialog" in GET_INTERACTIVE_ELEMENTS_FUNC
        assert "el-dialog" in GET_INTERACTIVE_ELEMENTS_FUNC
