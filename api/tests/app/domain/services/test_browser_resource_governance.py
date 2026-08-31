#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_browser_resource_governance.py
02-浏览器工具资源治理单元测试
- 截图节流策略: 必截图/不截图/节流操作
- 混合方案VNC降级: VNC连接时降低截图频率
- 浏览器操作超时包装: 超时返回友好错误
- ReActAgent工具重试: 浏览器工具降级重试1次
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.app_config import AgentConfig
from app.domain.models.tool_result import ToolResult
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.agents.base import BaseAgent
from app.domain.services.tools.browser import BrowserTool
from app.domain.services.tools.budget_tracker import ToolBudgetTracker
from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser
from app.infrastructure.storage.vnc_status_tracker import VNCStatusTracker


class TestScreenshotStrategy:
    """截图策略测试 — 视觉变化优先策略

    策略分四类:
    1. 关键视觉操作(CRITICAL_OPS): VNC连接时也必截图(不受节流控制)
    2. 视觉变化操作(REQUIRED_OPS): 必截图,VNC连接时降级为3秒节流
    3. 非视觉操作(SKIP_OPS): 不截图,无条件返回False
    4. 未知操作: 兜底节流(1秒间隔),平衡截图覆盖与上传开销
    """

    def _create_runner(self) -> AgentTaskRunner:
        """创建mock AgentTaskRunner实例(仅含截图策略相关字段)"""
        with patch.object(AgentTaskRunner, '__init__', lambda self: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._last_browser_screenshot_time = 0.0
            runner._session_id = "test-screenshot-session"
            return runner

    # ===== 视觉变化操作: 必截图 =====

    def test_navigate_always_screenshots(self):
        """导航操作必截图(页面发生重大变化)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_navigate") is True

    def test_restart_always_screenshots(self):
        """重启操作必截图"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_restart") is True

    def test_view_always_screenshots(self):
        """查看页面操作必截图"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_view") is True

    def test_click_always_screenshots(self):
        """点击操作必截图(交互后页面可能变化,用户需确认结果)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_click") is True

    def test_click_always_screenshots_even_within_throttle(self):
        """点击操作在节流间隔内也必截图(视觉变化操作不受节流限制)"""
        runner = self._create_runner()
        # 先执行一次必截图操作设置时间戳
        runner._should_take_screenshot("browser_navigate")
        # 立即执行click仍应截图(视觉变化操作无条件截图)
        assert runner._should_take_screenshot("browser_click") is True

    def test_console_exec_always_screenshots(self):
        """JS执行必截图(JS可能修改DOM,用户需确认执行结果)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_console_exec") is True

    def test_scroll_down_always_screenshots(self):
        """向下滚动必截图(改变视口,用户需确认滚动位置)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_scroll_down") is True

    def test_scroll_up_always_screenshots(self):
        """向上滚动必截图(改变视口)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_scroll_up") is True

    def test_scroll_to_top_always_screenshots(self):
        """滚动到顶部必截图(改变视口)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_scroll_to_top") is True

    def test_scroll_to_text_always_screenshots(self):
        """滚动定位必截图(改变视口,用户需确认定位结果)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_scroll_to_text") is True

    def test_input_always_screenshots(self):
        """输入操作必截图(页面内容变化,用户需确认输入结果)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_input") is True

    def test_press_key_always_screenshots(self):
        """按键操作必截图(可能触发表单提交/导航等视觉变化)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_press_key") is True

    def test_select_option_always_screenshots(self):
        """下拉选择必截图(选择项变化,用户需确认)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_select_option") is True

    def test_move_mouse_always_screenshots(self):
        """鼠标移动必截图(可能触发hover效果,用户需确认)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_move_mouse") is True

    def test_wait_for_always_screenshots(self):
        """增量等待必截图(等待元素出现后页面已变化,用户需确认)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_wait_for") is True

    # ===== 非视觉操作: 不截图 =====

    def test_console_view_never_screenshots(self):
        """查看控制台不截图(纯文本数据,无页面可见内容变化)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_console_view") is False

    def test_wait_never_screenshots(self):
        """定时等待不截图(无视觉变化)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_wait") is False

    def test_network_requests_never_screenshots(self):
        """网络请求查询不截图(纯文本数据)"""
        runner = self._create_runner()
        assert runner._should_take_screenshot("browser_network_requests") is False

    # ===== 未知操作: 兜底节流 =====

    def test_unknown_op_screenshots_after_throttle(self):
        """未知操作距上次截图超过阈值时截图(兜底节流)"""
        runner = self._create_runner()
        runner._last_browser_screenshot_time = 0.0  # 初始为0,确保超过阈值
        assert runner._should_take_screenshot("browser_unknown_op") is True

    def test_unknown_op_skipped_within_throttle(self):
        """未知操作在节流间隔内不截图(兜底节流)"""
        runner = self._create_runner()
        # 先执行一次必截图操作设置时间戳
        runner._should_take_screenshot("browser_navigate")
        # 立即执行未知操作应被节流
        assert runner._should_take_screenshot("browser_unknown_op") is False


class TestVNCModeScreenshotStrategy:
    """混合方案: VNC连接时截图降级策略测试

    VNC连接时:
    1. 关键视觉操作(CRITICAL_OPS): 仍必截图(navigate/click/input/select不受节流)
    2. 视觉变化操作(REQUIRED_OPS): 降级为3秒节流(截图仅用于历史回放)
    3. 非视觉操作(SKIP_OPS): 仍不截图(无变化)
    4. 未知操作: 跳过截图(VNC实时画面已覆盖)
    """

    def _create_runner(self, session_id: str = "test-vnc-session") -> AgentTaskRunner:
        """创建mock AgentTaskRunner实例(含VNC状态跟踪)"""
        with patch.object(AgentTaskRunner, '__init__', lambda self: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._last_browser_screenshot_time = 0.0
            runner._session_id = session_id
            return runner

    @pytest.fixture(autouse=True)
    def _cleanup_vnc_status(self):
        """每个测试后清理VNC状态(防止测试间状态泄漏)"""
        yield
        # 同步清理: 测试中使用patch,无需异步清理
        VNCStatusTracker._sessions.clear()

    @pytest.mark.asyncio
    async def test_vnc_connected_required_op_throttled(self):
        """VNC连接时,非关键视觉变化操作降级为3秒节流"""
        runner = self._create_runner()
        await VNCStatusTracker.set_connected("test-vnc-session", True)

        # 首次截图: 距上次超过3秒,允许截图(使用browser_view,非关键操作)
        runner._last_browser_screenshot_time = 0.0
        assert runner._should_take_screenshot("browser_view") is True

        # 立即再截图: 被节流(3秒内,browser_scroll_down非关键操作)
        assert runner._should_take_screenshot("browser_scroll_down") is False

    @pytest.mark.asyncio
    async def test_vnc_connected_critical_op_not_throttled(self):
        """VNC连接时,关键视觉操作不受节流控制(会话34af4e8d: click 50%截图缺失修复)"""
        runner = self._create_runner()
        await VNCStatusTracker.set_connected("test-vnc-session", True)

        # 关键操作首次截图
        runner._last_browser_screenshot_time = 0.0
        assert runner._should_take_screenshot("browser_navigate") is True

        # 立即再执行关键操作: 不受节流,仍截图
        assert runner._should_take_screenshot("browser_click") is True
        assert runner._should_take_screenshot("browser_input") is True
        assert runner._should_take_screenshot("browser_select_option") is True

    @pytest.mark.asyncio
    async def test_vnc_connected_critical_op_always_screenshot(self):
        """VNC连接时,关键操作连续调用都截图(确保100%覆盖)"""
        runner = self._create_runner()
        await VNCStatusTracker.set_connected("test-vnc-session", True)

        # 连续3次click操作: 每次都应截图
        assert runner._should_take_screenshot("browser_click") is True
        assert runner._should_take_screenshot("browser_click") is True
        assert runner._should_take_screenshot("browser_click") is True

    @pytest.mark.asyncio
    async def test_vnc_connected_skip_op_still_skipped(self):
        """VNC连接时,非视觉操作仍不截图"""
        runner = self._create_runner()
        await VNCStatusTracker.set_connected("test-vnc-session", True)

        assert runner._should_take_screenshot("browser_console_view") is False
        assert runner._should_take_screenshot("browser_wait") is False
        assert runner._should_take_screenshot("browser_network_requests") is False

    @pytest.mark.asyncio
    async def test_vnc_connected_unknown_op_skipped(self):
        """VNC连接时,未知操作跳过截图(实时画面已覆盖)"""
        runner = self._create_runner()
        await VNCStatusTracker.set_connected("test-vnc-session", True)

        # 即使距上次截图很久,未知操作也跳过
        runner._last_browser_screenshot_time = 0.0
        assert runner._should_take_screenshot("browser_unknown_op") is False

    @pytest.mark.asyncio
    async def test_vnc_disconnected_required_op_always_screenshot(self):
        """VNC断开后,视觉变化操作恢复无条件截图"""
        runner = self._create_runner()
        # VNC未连接(默认状态)

        runner._last_browser_screenshot_time = 0.0
        assert runner._should_take_screenshot("browser_navigate") is True
        # 立即再截图: 仍允许(VNC未连接时REQUIRED_OPS无条件截图)
        assert runner._should_take_screenshot("browser_click") is True

    @pytest.mark.asyncio
    async def test_vnc_disconnect_restores_full_screenshot(self):
        """VNC从连接到断开,截图模式恢复完整"""
        runner = self._create_runner()
        session_id = "test-vnc-session"

        # VNC连接时: 未知操作跳过
        await VNCStatusTracker.set_connected(session_id, True)
        assert runner._should_take_screenshot("browser_unknown_op") is False

        # VNC断开后: 未知操作恢复节流
        await VNCStatusTracker.set_connected(session_id, False)
        runner._last_browser_screenshot_time = 0.0
        assert runner._should_take_screenshot("browser_unknown_op") is True


class TestVNCStatusTracker:
    """VNC状态跟踪器单元测试"""

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        yield
        VNCStatusTracker._sessions.clear()

    @pytest.mark.asyncio
    async def test_set_and_check_connected(self):
        """设置VNC连接状态后可查询到"""
        await VNCStatusTracker.set_connected("session-1", True)
        assert VNCStatusTracker.is_connected("session-1") is True

    @pytest.mark.asyncio
    async def test_set_disconnected_clears_status(self):
        """设置VNC断开后状态被清除"""
        await VNCStatusTracker.set_connected("session-1", True)
        await VNCStatusTracker.set_connected("session-1", False)
        assert VNCStatusTracker.is_connected("session-1") is False

    def test_unconnected_session_returns_false(self):
        """未连接的会话返回False"""
        assert VNCStatusTracker.is_connected("nonexistent-session") is False

    @pytest.mark.asyncio
    async def test_multiple_sessions_independent(self):
        """多会话VNC状态相互独立"""
        await VNCStatusTracker.set_connected("session-1", True)
        await VNCStatusTracker.set_connected("session-2", True)

        assert VNCStatusTracker.is_connected("session-1") is True
        assert VNCStatusTracker.is_connected("session-2") is True

        await VNCStatusTracker.set_connected("session-1", False)
        assert VNCStatusTracker.is_connected("session-1") is False
        assert VNCStatusTracker.is_connected("session-2") is True


class TestBrowserTimeout:
    """浏览器操作超时包装测试"""

    def _create_browser_tool(self) -> BrowserTool:
        """创建带mock browser的BrowserTool"""
        mock_browser = MagicMock()
        return BrowserTool(browser=mock_browser), mock_browser

    @pytest.mark.asyncio
    async def test_navigate_timeout_returns_error(self):
        """导航超时返回友好错误而非抛出异常"""
        tool, mock_browser = self._create_browser_tool()
        mock_browser.navigate = AsyncMock(side_effect=asyncio.TimeoutError())

        result = await tool.browser_navigate("https://slow-site.com")

        assert result.success is False
        assert "超时" in result.message

    @pytest.mark.asyncio
    async def test_navigate_exception_returns_error(self):
        """导航异常返回错误结果而非抛出"""
        tool, mock_browser = self._create_browser_tool()
        mock_browser.navigate = AsyncMock(side_effect=RuntimeError("connection refused"))

        result = await tool.browser_navigate("https://unreachable.com")

        assert result.success is False
        assert "导航失败" in result.message

    @pytest.mark.asyncio
    async def test_click_normal_returns_success(self):
        """正常点击返回成功结果"""
        tool, mock_browser = self._create_browser_tool()
        expected = ToolResult(success=True, message="OK")
        mock_browser.click = AsyncMock(return_value=expected)

        result = await tool.browser_click(text="提交")

        assert result.success is True

    @pytest.mark.asyncio
    async def test_wait_uses_user_timeout_when_longer(self):
        """wait操作超时取用户指定值与上限的较大值"""
        tool, mock_browser = self._create_browser_tool()
        mock_browser.wait = AsyncMock(return_value=ToolResult(success=True))

        await tool.browser_wait(seconds=60)

        mock_browser.wait.assert_called_once_with(60)


class TestBrowserToolRetry:
    """浏览器工具重试降级测试"""

    def _create_agent(self, max_retries: int = 3) -> BaseAgent:
        """创建mock BaseAgent实例

        设置_memory/_token_counter为None,避免_invoke_tool访问这些属性时
        抛AttributeError(高token压力预截断分支因self._memory为None自动跳过)。
        补齐工具并行/缓存字段,默认关闭保持原串行+无缓存语义。
        """
        with patch.object(BaseAgent, '__init__', lambda self: None):
            agent = BaseAgent.__new__(BaseAgent)
            agent._agent_config = MagicMock(max_retries=max_retries)
            agent._retry_interval = 0.01
            agent._memory = None
            agent._token_counter = None
            # 工具并行执行/工具结果缓存默认关闭,保持原串行+无缓存语义
            agent._parallel_enabled = False
            agent._concurrency_classifier = None
            agent._max_concurrency = 1
            agent._tool_cache = None
            agent._idempotent_registry = None  # P10-1幂等去重关闭
            agent._session_start_ts = 0.0  # P10-3会话级超时熔断: 0表示不启用
            return agent

    @pytest.mark.asyncio
    async def test_browser_tool_retries_once(self):
        """浏览器工具仅重试1次(而非max_retries次)"""
        agent = self._create_agent(max_retries=3)
        mock_tool = MagicMock()
        mock_tool.invoke = AsyncMock(side_effect=RuntimeError("browser error"))

        result = await agent._invoke_tool(mock_tool, "browser_navigate", {"url": "https://test.com"})

        assert result.success is False
        # 浏览器工具应只调用1次(max_retries=1)
        assert mock_tool.invoke.call_count == 1

    @pytest.mark.asyncio
    async def test_non_browser_tool_retries_max(self):
        """非浏览器工具重试max_retries次"""
        agent = self._create_agent(max_retries=3)
        mock_tool = MagicMock()
        mock_tool.invoke = AsyncMock(side_effect=RuntimeError("search error"))

        result = await agent._invoke_tool(mock_tool, "search", {"query": "test"})

        assert result.success is False
        # 非浏览器工具应重试3次
        assert mock_tool.invoke.call_count == 3

    @pytest.mark.asyncio
    async def test_browser_tool_success_on_first_try(self):
        """浏览器工具首次成功不重试"""
        agent = self._create_agent(max_retries=3)
        mock_tool = MagicMock()
        mock_tool.invoke = AsyncMock(return_value=ToolResult(success=True, message="OK"))

        result = await agent._invoke_tool(mock_tool, "browser_click", {"text": "OK"})

        assert result.success is True
        assert mock_tool.invoke.call_count == 1


class TestBrowserConsoleExecBudget:
    """browser_console_exec 预算限制集成测试

    验证 project_memory 硬约束: browser_console_exec=10 会话级上限
    会话34af4e8d暴露: 无预算限制导致LLM调用27次console_exec提取页面内容
    """

    def _create_tool(self, tracker=None):
        """创建带 budget_tracker 的 BrowserTool"""
        mock_browser = MagicMock()
        mock_browser.console_exec = AsyncMock(return_value=ToolResult(success=True, message="OK"))
        return BrowserTool(browser=mock_browser, budget_tracker=tracker), mock_browser

    @pytest.mark.asyncio
    async def test_budget_exceeded_returns_error(self):
        """console_exec 达10次后,第11次返回错误 ToolResult"""
        tracker = ToolBudgetTracker()
        for _ in range(10):
            tracker.increment("browser_console_exec")

        tool, mock_browser = self._create_tool(tracker)
        result = await tool.browser_console_exec("return document.title")

        assert result.success is False
        assert "上限" in result.message
        assert "browser_view" in result.message  # 引导切换到 browser_view
        # 浏览器不应被调用
        mock_browser.console_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_budget_counted_on_execution(self):
        """console_exec 执行后计数+1"""
        tracker = ToolBudgetTracker()
        tool, _ = self._create_tool(tracker)

        await tool.browser_console_exec("return document.title")

        assert tracker.get_count("browser_console_exec") == 1

    @pytest.mark.asyncio
    async def test_budget_not_exceeded_allows_execution(self):
        """console_exec 未达上限时正常执行"""
        tracker = ToolBudgetTracker()
        for _ in range(9):
            tracker.increment("browser_console_exec")

        tool, mock_browser = self._create_tool(tracker)
        result = await tool.browser_console_exec("return document.title")

        assert result.success is True
        mock_browser.console_exec.assert_called_once()
        assert tracker.get_count("browser_console_exec") == 10

    @pytest.mark.asyncio
    async def test_no_budget_tracker_allows_unlimited(self):
        """无 budget_tracker 时不受限制(向后兼容)"""
        tool, mock_browser = self._create_tool(tracker=None)

        result = await tool.browser_console_exec("return document.title")

        assert result.success is True
        mock_browser.console_exec.assert_called_once()


class TestWrapJsIfNeeded:
    """console_exec JavaScript代码智能包装测试

    验证 _wrap_js_if_needed 方法正确处理顶层return语句,
    避免Playwright page.evaluate()的"Illegal return statement"错误。
    """

    def test_top_level_return_wrapped(self):
        """顶层return语句被包装为箭头函数"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        result = PlaywrightBrowser._wrap_js_if_needed("return document.title")
        assert result == "() => { return document.title }"

    def test_multiline_with_return_wrapped(self):
        """多行代码含return被包装"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "const links = document.querySelectorAll('a');\nreturn links.length"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result.startswith("() => {")
        assert "return links.length" in result

    def test_arrow_function_not_wrapped(self):
        """已是箭头函数形式不包装"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "() => document.title"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == code

    def test_function_declaration_not_wrapped(self):
        """已是function声明不包装"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "function foo() { return 1; }"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == code

    def test_expression_not_wrapped(self):
        """纯表达式(无return)不包装"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "document.title"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == code

    def test_empty_string_returned_as_is(self):
        """空字符串原样返回"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        result = PlaywrightBrowser._wrap_js_if_needed("")
        assert result == ""

    def test_return_alone_wrapped(self):
        """单独return语句被包装"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        result = PlaywrightBrowser._wrap_js_if_needed("return")
        assert result == "() => { return }"

    def test_compact_arrow_function_not_wrapped(self):
        """紧凑箭头函数(()=>无空格)不包装

        回归测试: 旧代码typo "() =>>" 导致紧凑写法未被识别为函数形式,
        被错误地再次包装为 () => { ()=>{...} } 嵌套结构。
        """
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "()=>document.title"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == code

    def test_compact_arrow_function_with_return_not_wrapped(self):
        """紧凑箭头函数带return不包装"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "()=>{ return document.title }"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == code

    def test_arrow_function_with_space_not_wrapped(self):
        """带空格箭头函数() =>不包装"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "() => { return document.title }"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == code


class TestTruncateBrowserViewEmptyFields:
    """browser_view结果截断空字段保留测试

    会话3c4debd1暴露: _truncate_browser_view_result跳过空字段(空列表[]/空字符串""),
    导致LLM看不到interactive_elements/content字段存在,误判"字段缺失"而滥用
    browser_console_exec查找元素(27次调用)。

    修复后: interactive_elements/ref_map/content即使为空也必须包含在结果中,
    仅accessibility_tree(辅助通道)为空时跳过。
    """

    def test_empty_interactive_elements_preserved(self):
        """interactive_elements为空列表时仍包含在结果中"""
        from app.domain.models.memory import Memory

        data = {
            "screenshot": "base64data",
            "page_state": {"url": "https://example.com"},
            "interactive_elements": [],
            "ref_map": [],
            "content": "",
            "snapshot_version": 1,
        }
        result = Memory._truncate_browser_view_result(data, 12000)
        import json
        parsed = json.loads(result)
        # 空列表也必须存在,让LLM知道字段存在但当前为空
        assert "interactive_elements" in parsed
        assert parsed["interactive_elements"] == []

    def test_empty_content_preserved(self):
        """content为空字符串时仍包含在结果中"""
        from app.domain.models.memory import Memory

        data = {
            "screenshot": "base64data",
            "page_state": {"url": "https://example.com"},
            "interactive_elements": ["0: <button>OK</button>"],
            "ref_map": [],
            "content": "",
            "snapshot_version": 1,
        }
        result = Memory._truncate_browser_view_result(data, 12000)
        import json
        parsed = json.loads(result)
        # 空字符串也必须存在,让LLM知道字段存在但当前为空
        assert "content" in parsed
        assert parsed["content"] == ""

    def test_empty_ref_map_preserved(self):
        """ref_map为空列表时仍包含在结果中"""
        from app.domain.models.memory import Memory

        data = {
            "screenshot": "base64data",
            "page_state": {"url": "https://example.com"},
            "interactive_elements": [],
            "ref_map": [],
            "content": "",
            "snapshot_version": 1,
        }
        result = Memory._truncate_browser_view_result(data, 12000)
        import json
        parsed = json.loads(result)
        assert "ref_map" in parsed
        assert parsed["ref_map"] == []

    def test_empty_accessibility_tree_omitted(self):
        """accessibility_tree为空时跳过(辅助通道,空值无意义)"""
        from app.domain.models.memory import Memory

        data = {
            "screenshot": "base64data",
            "page_state": {"url": "https://example.com"},
            "interactive_elements": ["0: <button>OK</button>"],
            "ref_map": ["@e1: button[OK]"],
            "content": "页面文本",
            "accessibility_tree": "",
            "snapshot_version": 1,
        }
        result = Memory._truncate_browser_view_result(data, 12000)
        import json
        parsed = json.loads(result)
        # accessibility_tree为空时不包含
        assert "accessibility_tree" not in parsed

    def test_non_empty_fields_all_preserved(self):
        """非空字段全部包含且截断正常工作"""
        from app.domain.models.memory import Memory

        data = {
            "screenshot": "base64data",
            "page_state": {"url": "https://example.com", "title": "测试"},
            "interactive_elements": ["0: <button>提交</button>", "1: <a>链接</a>"],
            "ref_map": ["@e1: button[提交]", "@e2: a[链接]"],
            "content": "这是页面文本内容",
            "accessibility_tree": "button 提交\na 链接",
            "snapshot_version": 5,
            "pending_dialogs": [],
            "dialog_history": [],
        }
        result = Memory._truncate_browser_view_result(data, 12000)
        import json
        parsed = json.loads(result)
        assert parsed["interactive_elements"] == ["0: <button>提交</button>", "1: <a>链接</a>"]
        assert parsed["ref_map"] == ["@e1: button[提交]", "@e2: a[链接]"]
        assert parsed["content"] == "这是页面文本内容"
        assert parsed["accessibility_tree"] == "button 提交\na 链接"
        assert parsed["snapshot_version"] == 5


class TestWrapJsIfNeededExtended:
    """_wrap_js_if_needed 增强测试 — 覆盖return变体与多语句场景

    会话4b2ad987暴露: 旧版仅检测"return "(带空格),漏掉return{/return(/return[等
    无空格变体,以及const/let/var声明(非表达式)直接执行导致SyntaxError。
    """

    def test_return_with_brace_no_space_wrapped(self):
        """return{...} (无空格)被正确包装"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "return{title: document.title}"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == "() => { return{title: document.title} }"

    def test_return_with_paren_no_space_wrapped(self):
        """return(...) (无空格)被正确包装"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "return(document.title)"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == "() => { return(document.title) }"

    def test_return_with_bracket_no_space_wrapped(self):
        """return[...] (无空格)被正确包装"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "return[1, 2, 3]"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == "() => { return[1, 2, 3] }"

    def test_return_value_identifier_not_wrapped(self):
        """returnValue(标识符)不被误包装为return语句"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "returnValue"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        # returnValue是普通标识符(变量引用),不是return语句,应原样返回作为表达式
        assert result == "returnValue"

    def test_const_declaration_wrapped(self):
        """const声明(语句而非表达式)被包装为箭头函数"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "const x = document.title"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == "() => { const x = document.title }"

    def test_let_declaration_wrapped(self):
        """let声明被包装为箭头函数"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "let x = 1"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == "() => { let x = 1 }"

    def test_var_declaration_wrapped(self):
        """var声明被包装为箭头函数"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "var x = 42"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == "() => { var x = 42 }"

    def test_multi_statement_without_return_wrapped(self):
        """多语句代码(无return)被包装(避免SyntaxError)"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "const x = 1; x + 1"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result.startswith("() => {")
        assert "const x = 1" in result

    def test_single_expression_not_wrapped(self):
        """单条表达式(无声明关键字)不包装"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "document.querySelector('a').href"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == code

    def test_semicolon_terminated_single_statement_not_wrapped(self):
        """单条表达式带分号结尾不包装(分号后无内容)"""
        from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser

        code = "document.title;"
        result = PlaywrightBrowser._wrap_js_if_needed(code)
        assert result == code


class TestExtractContentFallback:
    """_extract_content 空内容回退测试

    会话4b2ad987根因: GET_VISIBLE_CONTENT_FUNC的walk()返回null时返回'{}',
    _dom_tree_to_text返回空字符串但不抛异常,导致不触发markdownify回退。
    LLM连续11次view_page获取空content,被迫滥用console_exec(27次)。
    """

    def _create_browser(self) -> PlaywrightBrowser:
        """创建mock PlaywrightBrowser实例(仅含_extract_content所需字段)"""
        with patch.object(PlaywrightBrowser, '__init__', lambda self, *a, **kw: None):
            browser = PlaywrightBrowser.__new__(PlaywrightBrowser)
            browser.browser = MagicMock()  # _ensure_browser检查此属性
            browser.page = MagicMock()
            return browser

    @pytest.mark.asyncio
    async def test_empty_dom_falls_back_to_inner_text(self):
        """非文档容器页面: DOM树遍历返回空时回退到body.innerText"""
        browser = self._create_browser()
        # DETECT_DOC_CONTAINER返回None(非文档容器),GET_VISIBLE返回'{}'(walk返回null),
        # EXTRACT_SPA返回空,最终body.innerText回退成功
        browser.page.evaluate = AsyncMock(side_effect=[
            None,              # DETECT_DOC_CONTAINER_FUNC → 非文档容器
            '{}',              # GET_VISIBLE_CONTENT_FUNC → 空
            '',                # EXTRACT_SPA_CONTENT_FUNC → 空
            '页面文本内容',     # body.innerText回退
        ])

        with patch.object(browser, '_ensure_page', new_callable=AsyncMock):
            content = await browser._extract_content()

        assert content == '页面文本内容'
        assert browser.page.evaluate.call_count == 4

    @pytest.mark.asyncio
    async def test_dom_exception_falls_back_to_markdownify(self):
        """非文档容器页面: DOM提取异常时回退到markdownify"""
        browser = self._create_browser()
        # DETECT返回None,GET_VISIBLE_CONTENT_FUNC抛异常
        browser.page.evaluate = AsyncMock(side_effect=[
            None,                              # DETECT_DOC_CONTAINER_FUNC → 非文档容器
            RuntimeError("evaluation failed"),  # GET_VISIBLE_CONTENT_FUNC → 异常
        ])
        browser.page.content = AsyncMock(return_value="<html><body>HTML内容</body></html>")

        with patch.object(browser, '_ensure_page', new_callable=AsyncMock):
            content = await browser._extract_content()

        # markdownify应该提取出"HTML内容"
        assert 'HTML内容' in content

    @pytest.mark.asyncio
    async def test_both_dom_and_inner_text_empty_returns_empty(self):
        """非文档容器页面: DOM和body.innerText都为空时返回空字符串"""
        browser = self._create_browser()
        browser.page.evaluate = AsyncMock(side_effect=[
            None,   # DETECT_DOC_CONTAINER_FUNC → 非文档容器
            '{}',   # GET_VISIBLE_CONTENT_FUNC → 空
            '',     # EXTRACT_SPA_CONTENT_FUNC → 空
            '',     # body.innerText也为空
        ])

        with patch.object(browser, '_ensure_page', new_callable=AsyncMock):
            content = await browser._extract_content()

        assert content == ''

    @pytest.mark.asyncio
    async def test_non_empty_dom_skips_fallback(self):
        """非文档容器页面: DOM树有内容时不触发回退"""
        browser = self._create_browser()
        # 模拟DOM树返回有内容的JSON
        dom_json = '{"tag":"body","children":[{"tag":"h1","text":"标题"}]}'
        browser.page.evaluate = AsyncMock(side_effect=[
            None,       # DETECT_DOC_CONTAINER_FUNC → 非文档容器
            dom_json,   # GET_VISIBLE_CONTENT_FUNC → 有内容
        ])

        with patch.object(browser, '_ensure_page', new_callable=AsyncMock):
            content = await browser._extract_content()

        assert '标题' in content
        # 只调用2次(DETECT + GET_VISIBLE),未触发SPA/body回退
        assert browser.page.evaluate.call_count == 2


class TestExtractInteractiveElementsFallback:
    """_extract_interactive_elements 空结果回退测试

    主选择器(GET_INTERACTIVE_ELEMENTS_FUNC)可能因Shadow DOM/CSP/复杂SPA返回空,
    回退到基础选择器确保至少获取原生交互元素(a/button/input)。
    """

    def _create_browser(self) -> PlaywrightBrowser:
        """创建mock PlaywrightBrowser实例"""
        with patch.object(PlaywrightBrowser, '__init__', lambda self, *a, **kw: None):
            browser = PlaywrightBrowser.__new__(PlaywrightBrowser)
            browser.browser = MagicMock()  # _ensure_browser检查此属性
            browser.page = MagicMock()
            browser.page.interactive_elements_cache = []
            browser._snapshot_version = 0
            browser._ref_map = {}
            browser._prev_ref_map = {}
            return browser

    @pytest.mark.asyncio
    async def test_empty_main_result_triggers_fallback(self):
        """主选择器返回空时触发基础回退"""
        browser = self._create_browser()
        fallback_elements = [
            {"tag": "a", "text": "链接", "index": 0, "inViewport": True, "inShadowDOM": False, "inDialog": False}
        ]
        browser.page.evaluate = AsyncMock(side_effect=[[], fallback_elements])

        with patch.object(browser, '_ensure_page', new_callable=AsyncMock), \
             patch.object(browser, '_build_ref_map', new_callable=AsyncMock):
            elements = await browser._extract_interactive_elements()

        assert len(elements) == 1
        assert elements[0]["tag"] == "a"
        assert browser.page.evaluate.call_count == 2

    @pytest.mark.asyncio
    async def test_main_exception_triggers_fallback(self):
        """主选择器异常时触发基础回退"""
        browser = self._create_browser()
        fallback_elements = [
            {"tag": "button", "text": "按钮", "index": 0, "inViewport": True, "inShadowDOM": False, "inDialog": False}
        ]
        browser.page.evaluate = AsyncMock(side_effect=[
            RuntimeError("query failed"),
            fallback_elements,
        ])

        with patch.object(browser, '_ensure_page', new_callable=AsyncMock), \
             patch.object(browser, '_build_ref_map', new_callable=AsyncMock):
            elements = await browser._extract_interactive_elements()

        assert len(elements) == 1
        assert elements[0]["tag"] == "button"

    @pytest.mark.asyncio
    async def test_both_fail_returns_empty(self):
        """主选择器和回退都失败时返回空列表"""
        browser = self._create_browser()
        browser.page.evaluate = AsyncMock(side_effect=[
            RuntimeError("main failed"),
            RuntimeError("fallback failed"),
        ])

        with patch.object(browser, '_ensure_page', new_callable=AsyncMock), \
             patch.object(browser, '_build_ref_map', new_callable=AsyncMock):
            elements = await browser._extract_interactive_elements()

        assert elements == []

    @pytest.mark.asyncio
    async def test_non_empty_main_skips_fallback(self):
        """主选择器有结果时不触发回退"""
        browser = self._create_browser()
        main_elements = [
            {"tag": "a", "text": "链接1", "index": 0, "inViewport": True, "inShadowDOM": False, "inDialog": False},
            {"tag": "button", "text": "按钮", "index": 1, "inViewport": True, "inShadowDOM": False, "inDialog": False},
        ]
        browser.page.evaluate = AsyncMock(return_value=main_elements)

        with patch.object(browser, '_ensure_page', new_callable=AsyncMock), \
             patch.object(browser, '_build_ref_map', new_callable=AsyncMock):
            elements = await browser._extract_interactive_elements()

        assert len(elements) == 2
        # 只调用1次(主选择器),未触发回退
        assert browser.page.evaluate.call_count == 1


class TestAdaptiveConsoleExecBudget:
    """复杂页面自适应console_exec预算测试(方案B/会话437cbc75根因修复)

    browser_view检测到交互元素>200的复杂页面(企业App)后,
    通过budget_tracker.raise_budget动态上调console_exec预算(10→20)。
    """

    def _create_tool_with_tracker(self) -> tuple:
        """创建带真实budget_tracker的BrowserTool"""
        mock_browser = MagicMock()
        tracker = ToolBudgetTracker()
        tool = BrowserTool(browser=mock_browser, budget_tracker=tracker)
        return tool, mock_browser, tracker

    def _make_view_result(self, total_elements: int) -> ToolResult:
        """构造browser_view成功结果(含element_summary)"""
        return ToolResult(success=True, data={
            "content": "page text",
            "interactive_elements": [f"{i}: <a>link{i}</a>" for i in range(total_elements)],
            "element_summary": {
                "visible": total_elements,
                "offscreen": 0,
                "total": total_elements,
            },
            "page_state": {"url": "https://test.com", "title": "Test"},
        })

    def test_complex_page_raises_budget(self):
        """交互元素>200的复杂页面触发console_exec预算上调(10→20)"""
        tool, _, tracker = self._create_tool_with_tracker()
        result = self._make_view_result(total_elements=250)

        tool._maybe_raise_console_exec_budget(result)

        assert tracker.get_budget("browser_console_exec") == 20, \
            "复杂页面(250元素)应将console_exec预算从10上调到20"

    def test_simple_page_does_not_raise_budget(self):
        """交互元素≤200的简单页面不触发预算上调"""
        tool, _, tracker = self._create_tool_with_tracker()
        result = self._make_view_result(total_elements=150)

        tool._maybe_raise_console_exec_budget(result)

        assert tracker.get_budget("browser_console_exec") == 10, \
            "简单页面(150元素)不应上调console_exec预算"

    def test_threshold_boundary_200_raises(self):
        """恰好200元素触发预算上调(>=阈值)"""
        tool, _, tracker = self._create_tool_with_tracker()
        result = self._make_view_result(total_elements=200)

        tool._maybe_raise_console_exec_budget(result)

        assert tracker.get_budget("browser_console_exec") == 20

    def test_failed_result_does_not_raise(self):
        """失败的browser_view结果不触发预算上调"""
        tool, _, tracker = self._create_tool_with_tracker()
        result = ToolResult(success=False, message="查看页面超时")

        tool._maybe_raise_console_exec_budget(result)

        assert tracker.get_budget("browser_console_exec") == 10

    def test_no_budget_tracker_does_not_raise(self):
        """无budget_tracker时不报错(no-op)"""
        mock_browser = MagicMock()
        tool = BrowserTool(browser=mock_browser, budget_tracker=None)
        result = self._make_view_result(total_elements=300)

        # 不应抛出异常
        tool._maybe_raise_console_exec_budget(result)

    def test_missing_element_summary_does_not_raise(self):
        """结果缺少element_summary字段时不触发预算上调"""
        tool, _, tracker = self._create_tool_with_tracker()
        result = ToolResult(success=True, data={
            "content": "page text",
            "interactive_elements": ["0: <a>link</a>"],
            # 缺少 element_summary
        })

        tool._maybe_raise_console_exec_budget(result)

        assert tracker.get_budget("browser_console_exec") == 10

    def test_raise_is_idempotent_across_multiple_views(self):
        """多次browser_view复杂页面,预算只上调一次(幂等)"""
        tool, _, tracker = self._create_tool_with_tracker()
        result = self._make_view_result(total_elements=300)

        tool._maybe_raise_console_exec_budget(result)
        tool._maybe_raise_console_exec_budget(result)
        tool._maybe_raise_console_exec_budget(result)

        assert tracker.get_budget("browser_console_exec") == 20, \
            "多次调用不应叠加(幂等,上限20)"

    @pytest.mark.asyncio
    async def test_browser_view_triggers_budget_raise_for_complex_page(self):
        """browser_view端到端: 复杂页面结果触发console_exec预算上调"""
        tool, mock_browser, tracker = self._create_tool_with_tracker()
        mock_browser.view_page = AsyncMock(return_value=self._make_view_result(total_elements=280))

        await tool.browser_view()

        assert tracker.get_budget("browser_console_exec") == 20, \
            "browser_view应触发复杂页面(280元素)的console_exec预算上调"
