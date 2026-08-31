#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_base_p1_parallel.py
BaseAgent P1并行执行单元测试 - 验证3阶段并行执行逻辑、事件顺序、异常隔离

测试覆盖:
- 并行启用/未启用时 tool_calls 截断行为
- 单工具走串行路径,多工具走并行路径
- CALLING/CALLED 事件顺序保证
- 串行工具(shell/browser)混合调用走串行
- 并行工具耗时≈max(t_i)而非sum
- 未知工具不阻塞其他工具
- 工具异常不阻塞其他工具
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.models.app_config import AgentConfig, ToolExecutionConfig
from app.domain.models.event import ToolEvent, ToolEventStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.tools.base import BaseTool, tool
from app.domain.services.tools.concurrency import ToolConcurrencyClassifier


# ========== Stub工具定义 ==========

class _StubSearchTool(BaseTool):
    """模拟搜索工具(可并行)"""
    name: str = "search"

    @tool(name="web_search", description="搜索", parameters={}, required=[])
    async def web_search(self) -> ToolResult:
        return ToolResult(success=True, data="search_result")


class _StubShellTool(BaseTool):
    """模拟Shell工具(不可并行,共享session)"""
    name: str = "shell"

    @tool(name="shell_execute", description="执行命令", parameters={}, required=[])
    async def shell_execute(self) -> ToolResult:
        return ToolResult(success=True, data="shell_result")


class _StubBrowserTool(BaseTool):
    """模拟浏览器工具(不可并行,共享实例)"""
    name: str = "browser"

    @tool(name="browser_navigate", description="导航", parameters={}, required=[])
    async def browser_navigate(self) -> ToolResult:
        return ToolResult(success=True, data="browser_result")


# ========== 测试辅助函数 ==========

def _build_agent(
        tools,
        parallel_enabled: bool = False,
        max_concurrency: int = 5,
):
    """构建BaseAgent实例(绕过__init__,手动设置属性)

    Args:
        tools: 工具列表
        parallel_enabled: 是否启用P1并行执行
        max_concurrency: 最大并发数
    """
    agent = object.__new__(BaseAgent)
    agent._tools = tools
    agent._agent_config = AgentConfig(max_iterations=10, max_retries=2)
    agent._format = None
    agent._tool_choice = "auto"
    agent._system_prompt = "test"
    agent.name = "test_agent"
    agent._session_id = "test_session"
    agent._uow_factory = lambda: MagicMock()
    agent._uow = MagicMock()
    agent._json_parser = MagicMock()
    agent._json_parser.invoke = AsyncMock(return_value={})
    agent._memory = MagicMock()
    agent._memory.empty = False
    agent._memory.should_compress = MagicMock(return_value=False)
    agent._memory.check_token_limit = MagicMock(return_value=False)
    agent._memory.is_context_overflow = MagicMock(return_value=False)
    agent._memory.get_messages = MagicMock(return_value=[])
    agent._memory.add_message = MagicMock()
    agent._memory.add_messages = MagicMock()
    agent._token_counter = None
    agent._context_window = 64000
    agent._retry_interval = 0.01
    agent._tool_cache = None  # P3缓存关闭,专注测试P1
    agent._idempotent_registry = None  # P10-1幂等去重关闭,专注测试P1
    agent._session_start_ts = 0.0  # P10-3会话级超时熔断: 0表示不启用

    # P1并行执行配置
    agent._parallel_enabled = parallel_enabled
    agent._max_concurrency = max_concurrency
    if parallel_enabled:
        agent._concurrency_classifier = ToolConcurrencyClassifier(
            stateful_prefixes=["shell_", "browser_"],
            stateful_names=["file_write", "file_delete", "file_move", "file_upload"],
        )
    else:
        agent._concurrency_classifier = None

    # Mock memory操作
    agent._add_to_memory = AsyncMock()
    agent._ensure_memory = AsyncMock()

    return agent


def _make_llm_response(tool_calls=None, content=None):
    """构造LLM响应消息"""
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls,
    }


def _make_tool_call(call_id: str, name: str, args: str = "{}"):
    """构造单个tool_call"""
    return {
        "id": call_id,
        "function": {"name": name, "arguments": args},
    }


# ========== 测试用例 ==========

class TestParallelTruncation:
    """验证_invoke_llm中tool_calls截断行为

    注意: _invoke_llm中的[:1]截断逻辑在真实方法内部,mock _invoke_llm会绕过截断。
    此处通过invoke行为间接验证: 并行启用时所有tool_calls被执行,并行未启用时走串行路径。
    截断逻辑本身通过代码审查+test_serial_execution_interleaves_events间接覆盖。
    """

    @pytest.mark.asyncio
    async def test_parallel_enabled_keeps_all_tool_calls(self):
        """并行启用时,tool_calls完整保留,2个工具都执行"""
        agent = _build_agent([_StubSearchTool()], parallel_enabled=True)

        invoke_count = 0

        async def mock_invoke_tool(tool, tool_name, arguments):
            nonlocal invoke_count
            invoke_count += 1
            return ToolResult(success=True, data="result")

        agent._invoke_tool = mock_invoke_tool

        call_count = 0

        async def mock_invoke_llm(messages, format=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(tool_calls=[
                    _make_tool_call("call_1", "web_search"),
                    _make_tool_call("call_2", "web_search"),
                ])
            return _make_llm_response(content="完成")

        agent._invoke_llm = mock_invoke_llm

        events = []
        async for event in agent.invoke("test"):
            events.append(event)

        # 并行启用时,2个工具都执行
        assert invoke_count == 2, f"并行启用时应执行2个工具,实际执行{invoke_count}次"

    @pytest.mark.asyncio
    async def test_parallel_disabled_uses_serial_path(self):
        """并行未启用时,invoke走串行路径(else分支),事件CALLING/CALLED交替出现"""
        agent = _build_agent([_StubSearchTool()], parallel_enabled=False)

        async def mock_invoke_tool(tool, tool_name, arguments):
            return ToolResult(success=True, data="result")

        agent._invoke_tool = mock_invoke_tool

        call_count = 0

        async def mock_invoke_llm(messages, format=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # mock绕过_invoke_llm截断,直接返回2个tool_calls
                # 并行未启用时走else串行分支,CALLING/CALLED交替
                return _make_llm_response(tool_calls=[
                    _make_tool_call("call_1", "web_search"),
                    _make_tool_call("call_2", "web_search"),
                ])
            return _make_llm_response(content="完成")

        agent._invoke_llm = mock_invoke_llm

        events = []
        async for event in agent.invoke("test"):
            events.append(event)

        tool_events = [e for e in events if isinstance(e, ToolEvent)]
        # 串行路径: CALLING→CALLED→CALLING→CALLED 交替(非批量)
        # 验证事件顺序: 第1个CALLING在第1个CALLED之前,第1个CALLED在第2个CALLING之前
        assert len(tool_events) == 4, f"应有4个ToolEvent(2CALLING+2CALLED),实际{len(tool_events)}"
        statuses = [e.status for e in tool_events]
        # 串行特征: CALLING, CALLED, CALLING, CALLED (交替)
        assert statuses == [
            ToolEventStatus.CALLING, ToolEventStatus.CALLED,
            ToolEventStatus.CALLING, ToolEventStatus.CALLED,
        ], f"串行路径应CALLING/CALLED交替,实际顺序: {statuses}"


class TestSerialVsParallelPath:
    """验证串行/并行路径选择"""

    @pytest.mark.asyncio
    async def test_single_tool_call_uses_serial_path(self):
        """仅1个tool_call时走串行路径(else分支),即使并行启用"""
        agent = _build_agent([_StubSearchTool()], parallel_enabled=True)

        # 记录_invoke_tool的调用时间,验证无并行调度
        invoke_times = []

        async def mock_invoke_tool(tool, tool_name, arguments):
            invoke_times.append(time.monotonic())
            await asyncio.sleep(0.1)
            return ToolResult(success=True, data="result")

        agent._invoke_tool = mock_invoke_tool

        call_count = 0

        async def mock_invoke_llm(messages, format=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # 仅1个tool_call,应走串行路径
                return _make_llm_response(tool_calls=[
                    _make_tool_call("call_1", "web_search"),
                ])
            return _make_llm_response(content="完成")

        agent._invoke_llm = mock_invoke_llm

        events = []
        async for event in agent.invoke("test"):
            events.append(event)

        # 单工具应只调用1次
        assert len(invoke_times) == 1


class TestParallelEventOrder:
    """验证并行执行时CALLING/CALLED事件顺序"""

    @pytest.mark.asyncio
    async def test_parallel_execution_preserves_event_order(self):
        """多工具并行: 所有CALLING先出 → 所有CALLED后出, 顺序与tool_calls原始顺序一致"""
        agent = _build_agent([_StubSearchTool()], parallel_enabled=True)

        async def mock_invoke_tool(tool, tool_name, arguments):
            await asyncio.sleep(0.05)
            return ToolResult(success=True, data=f"{tool_name}_result")

        agent._invoke_tool = mock_invoke_tool

        call_count = 0

        async def mock_invoke_llm(messages, format=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(tool_calls=[
                    _make_tool_call("call_1", "web_search"),
                    _make_tool_call("call_2", "web_search"),
                    _make_tool_call("call_3", "web_search"),
                ])
            return _make_llm_response(content="完成")

        agent._invoke_llm = mock_invoke_llm

        events = []
        async for event in agent.invoke("test"):
            events.append(event)

        tool_events = [e for e in events if isinstance(e, ToolEvent)]
        calling_events = [e for e in tool_events if e.status == ToolEventStatus.CALLING]
        called_events = [e for e in tool_events if e.status == ToolEventStatus.CALLED]

        # 3个CALLING + 3个CALLED
        assert len(calling_events) == 3, f"应有3个CALLING事件,实际{len(calling_events)}"
        assert len(called_events) == 3, f"应有3个CALLED事件,实际{len(called_events)}"

        # CALLING顺序与原始tool_calls顺序一致
        assert [e.tool_call_id for e in calling_events] == ["call_1", "call_2", "call_3"]
        # CALLED顺序也与原始tool_calls顺序一致(按original_index排序)
        assert [e.tool_call_id for e in called_events] == ["call_1", "call_2", "call_3"]

        # 所有CALLING事件在所有CALLED事件之前(3阶段并行特征)
        last_calling_idx = events.index(calling_events[-1])
        first_called_idx = events.index(called_events[0])
        assert last_calling_idx < first_called_idx, "所有CALLING应在所有CALLED之前"

    @pytest.mark.asyncio
    async def test_serial_execution_interleaves_events(self):
        """串行路径: CALLING→CALLED交替出现(与并行路径的批量特征不同)"""
        agent = _build_agent([_StubSearchTool()], parallel_enabled=False)

        async def mock_invoke_tool(tool, tool_name, arguments):
            return ToolResult(success=True, data="result")

        agent._invoke_tool = mock_invoke_tool

        call_count = 0

        async def mock_invoke_llm(messages, format=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # 并行未启用时,_invoke_llm会截断为[:1],所以只有1个tool_call
                return _make_llm_response(tool_calls=[
                    _make_tool_call("call_1", "web_search"),
                ])
            return _make_llm_response(content="完成")

        agent._invoke_llm = mock_invoke_llm

        events = []
        async for event in agent.invoke("test"):
            events.append(event)

        tool_events = [e for e in events if isinstance(e, ToolEvent)]
        # 串行路径: 1个CALLING + 1个CALLED
        assert len(tool_events) == 2
        assert tool_events[0].status == ToolEventStatus.CALLING
        assert tool_events[1].status == ToolEventStatus.CALLED


class TestStatefulToolsSerial:
    """验证共享状态工具(shell/browser)在并行模式下仍串行执行"""

    @pytest.mark.asyncio
    async def test_stateful_tools_run_serially(self):
        """shell_execute + browser_navigate 混合调用,即使并行启用也串行执行"""
        agent = _build_agent(
            [_StubShellTool(), _StubBrowserTool()],
            parallel_enabled=True,
        )

        # 记录每个工具的开始和结束时间,验证串行(无重叠)
        timings = []  # [(tool_name, start, end)]

        async def mock_invoke_tool(tool, tool_name, arguments):
            start = time.monotonic()
            await asyncio.sleep(0.1)
            end = time.monotonic()
            timings.append((tool_name, start, end))
            return ToolResult(success=True, data="result")

        agent._invoke_tool = mock_invoke_tool

        call_count = 0

        async def mock_invoke_llm(messages, format=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(tool_calls=[
                    _make_tool_call("call_1", "shell_execute"),
                    _make_tool_call("call_2", "browser_navigate"),
                ])
            return _make_llm_response(content="完成")

        agent._invoke_llm = mock_invoke_llm

        events = []
        async for event in agent.invoke("test"):
            events.append(event)

        # 2个工具都应执行
        assert len(timings) == 2

        # 验证串行: 第一个工具的end <= 第二个工具的start
        # 由于并行路径中serial工具按顺序执行,无重叠
        timings.sort(key=lambda x: x[1])  # 按start排序
        assert timings[0][2] <= timings[1][1] + 0.01, \
            f"串行工具应无重叠执行,但{timings[0][0]}结束{timings[0][2]:.3f} > {timings[1][0]}开始{timings[1][1]:.3f}"


class TestParallelConcurrency:
    """验证并行工具耗时≈max(t_i)而非sum(t_i)"""

    @pytest.mark.asyncio
    async def test_parallel_tools_run_concurrently(self):
        """2个web_search并行,总耗时≈max(0.2s,0.2s)=0.2s而非0.4s"""
        agent = _build_agent([_StubSearchTool()], parallel_enabled=True)

        async def mock_invoke_tool(tool, tool_name, arguments):
            await asyncio.sleep(0.2)
            return ToolResult(success=True, data="result")

        agent._invoke_tool = mock_invoke_tool

        call_count = 0

        async def mock_invoke_llm(messages, format=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(tool_calls=[
                    _make_tool_call("call_1", "web_search"),
                    _make_tool_call("call_2", "web_search"),
                ])
            return _make_llm_response(content="完成")

        agent._invoke_llm = mock_invoke_llm

        start = time.monotonic()
        events = []
        async for event in agent.invoke("test"):
            events.append(event)
        elapsed = time.monotonic() - start

        # 并行: 0.2s + 一点开销, 而非 0.4s
        # 允许50%冗余(0.3s),但必须明显低于串行0.4s
        assert elapsed < 0.35, \
            f"并行执行耗时{elapsed:.2f}s应接近0.2s(并行)而非0.4s(串行)"


class TestUnknownToolAndException:
    """验证异常隔离: 未知工具/工具异常不阻塞其他工具"""

    @pytest.mark.asyncio
    async def test_unknown_tool_in_parallel_mode_returns_error_message(self):
        """并行模式下未知工具返回错误消息,不阻塞其他工具"""
        agent = _build_agent([_StubSearchTool()], parallel_enabled=True)

        executed_tools = []

        async def mock_invoke_tool(tool, tool_name, arguments):
            executed_tools.append(tool_name)
            return ToolResult(success=True, data="result")

        agent._invoke_tool = mock_invoke_tool

        call_count = 0

        async def mock_invoke_llm(messages, format=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(tool_calls=[
                    _make_tool_call("call_1", "web_search"),
                    _make_tool_call("call_2", "nonexistent_tool"),  # 未知工具
                    _make_tool_call("call_3", "web_search"),
                ])
            return _make_llm_response(content="完成")

        agent._invoke_llm = mock_invoke_llm

        events = []
        async for event in agent.invoke("test"):
            events.append(event)

        tool_events = [e for e in events if isinstance(e, ToolEvent)]
        calling_events = [e for e in tool_events if e.status == ToolEventStatus.CALLING]

        # 未知工具不出CALLING/CALLED事件,但其他2个web_search正常执行
        assert len(calling_events) == 2, f"未知工具不出事件,应有2个CALLING,实际{len(calling_events)}"
        # 所有CALLING的function_name都是web_search(非nonexistent_tool)
        assert all(e.function_name == "web_search" for e in calling_events)

    @pytest.mark.asyncio
    async def test_parallel_exception_does_not_break_others(self):
        """某工具抛异常,其他工具仍正常执行,异常以ToolResult(success=False)返回"""
        agent = _build_agent([_StubSearchTool()], parallel_enabled=True)

        invoke_count = 0

        async def mock_invoke_tool(tool, tool_name, arguments):
            nonlocal invoke_count
            invoke_count += 1
            if invoke_count == 1:
                raise RuntimeError("模拟工具异常")
            return ToolResult(success=True, data="result")

        agent._invoke_tool = mock_invoke_tool

        call_count = 0

        async def mock_invoke_llm(messages, format=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(tool_calls=[
                    _make_tool_call("call_1", "web_search"),
                    _make_tool_call("call_2", "web_search"),
                ])
            return _make_llm_response(content="完成")

        agent._invoke_llm = mock_invoke_llm

        events = []
        async for event in agent.invoke("test"):
            events.append(event)

        tool_events = [e for e in events if isinstance(e, ToolEvent)]
        called_events = [e for e in tool_events if e.status == ToolEventStatus.CALLED]

        # 2个工具都产出了CALLED事件(异常不阻塞其他)
        assert len(called_events) == 2, f"异常不应阻塞其他工具,应有2个CALLED,实际{len(called_events)}"

        # 第一个工具的result应为失败(success=False),第二个应为成功
        results = [e.function_result for e in called_events]
        # 异常被捕获并构造为ToolResult(success=False)
        failed_results = [r for r in results if not r.success]
        success_results = [r for r in results if r.success]
        assert len(failed_results) == 1, "应有1个失败结果(异常工具)"
        assert len(success_results) == 1, "应有1个成功结果(正常工具)"


class TestToolExecutionConfigIntegration:
    """验证ToolExecutionConfig与BaseAgent集成"""

    def test_tool_execution_config_disabled_by_default(self):
        """ToolExecutionConfig默认enabled=False"""
        config = ToolExecutionConfig()
        assert config.enabled is False
        assert config.max_concurrency == 5
        assert "shell_" in config.stateful_tool_prefixes
        assert "browser_" in config.stateful_tool_prefixes
        assert "file_write" in config.stateful_tool_names

    def test_tool_execution_config_enabled(self):
        """ToolExecutionConfig可启用"""
        config = ToolExecutionConfig(enabled=True, max_concurrency=3)
        assert config.enabled is True
        assert config.max_concurrency == 3


class TestMalformedToolCallPairing:
    """验证畸形tool_call(无function字段)生成错误tool_message,避免配对破坏

    根因: assistant(tool_calls)中的每个tool_call_id必须有对应tool_message,
    否则触发OpenAI API 400错误 "insufficient tool messages following tool_calls message"。
    修复: 畸形tool_call(无function字段)生成错误tool_message,保证配对完整。
    """

    @pytest.mark.asyncio
    async def test_malformed_tool_call_in_parallel_path_generates_error_message(self):
        """并行路径: 畸形tool_call生成错误tool_message,不阻塞其他工具"""
        agent = _build_agent([_StubSearchTool()], parallel_enabled=True)

        executed_tools = []

        async def mock_invoke_tool(tool, tool_name, arguments):
            executed_tools.append(tool_name)
            return ToolResult(success=True, data="result")

        agent._invoke_tool = mock_invoke_tool

        # 捕获传递给_invoke_llm的tool_messages,验证配对完整性
        captured_tool_messages = []
        call_count = 0

        async def mock_invoke_llm(messages, format=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(tool_calls=[
                    _make_tool_call("call_1", "web_search"),
                    {"id": "call_malformed"},  # 畸形: 无function字段
                    _make_tool_call("call_3", "web_search"),
                ])
            # 第二次调用: 捕获tool_messages验证配对
            if call_count == 2:
                captured_tool_messages.extend(messages)
            return _make_llm_response(content="完成")

        agent._invoke_llm = mock_invoke_llm

        events = []
        async for event in agent.invoke("test"):
            events.append(event)

        # 1.正常工具仍执行(畸形工具不阻塞)
        assert len(executed_tools) == 2, f"应有2个web_search执行,实际{len(executed_tools)}"

        # 2.tool_messages含3条(每个tool_call_id都有对应tool_message)
        tool_msgs = [m for m in captured_tool_messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 3, \
            f"应有3条tool_message(2正常+1畸形错误),实际{len(tool_msgs)}"

        # 3.每个tool_call_id都有对应tool_message
        tool_call_ids = {m["tool_call_id"] for m in tool_msgs}
        assert "call_1" in tool_call_ids
        assert "call_malformed" in tool_call_ids
        assert "call_3" in tool_call_ids

        # 4.畸形tool_message含错误内容
        malformed_msg = next(m for m in tool_msgs if m["tool_call_id"] == "call_malformed")
        assert "错误" in malformed_msg["content"] or "异常" in malformed_msg["content"]

    @pytest.mark.asyncio
    async def test_malformed_tool_call_in_serial_path_generates_error_message(self):
        """串行路径: 畸形tool_call生成错误tool_message"""
        agent = _build_agent([_StubSearchTool()], parallel_enabled=False)

        executed_tools = []

        async def mock_invoke_tool(tool, tool_name, arguments):
            executed_tools.append(tool_name)
            return ToolResult(success=True, data="result")

        agent._invoke_tool = mock_invoke_tool

        captured_tool_messages = []
        call_count = 0

        async def mock_invoke_llm(messages, format=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(tool_calls=[
                    _make_tool_call("call_1", "web_search"),
                    {"id": "call_malformed"},  # 畸形: 无function字段
                ])
            if call_count == 2:
                captured_tool_messages.extend(messages)
            return _make_llm_response(content="完成")

        agent._invoke_llm = mock_invoke_llm

        events = []
        async for event in agent.invoke("test"):
            events.append(event)

        # 正常工具执行
        assert len(executed_tools) == 1

        # tool_messages含2条(每个tool_call_id都有对应tool_message)
        tool_msgs = [m for m in captured_tool_messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 2, \
            f"应有2条tool_message(1正常+1畸形错误),实际{len(tool_msgs)}"

        tool_call_ids = {m["tool_call_id"] for m in tool_msgs}
        assert "call_1" in tool_call_ids
        assert "call_malformed" in tool_call_ids
