#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_unknown_tool.py
BaseAgent未知工具调用优雅降级的单元测试
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.models.app_config import AgentConfig
from app.domain.models.event import ToolEvent, ToolEventStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.tools.base import BaseTool, tool


class _StubShellTool(BaseTool):
    name: str = "shell"

    @tool(name="shell_execute", description="执行Shell命令", parameters={}, required=[])
    async def shell_execute(self) -> ToolResult:
        return ToolResult(success=True, data="ok")


class _StubFileTool(BaseTool):
    name: str = "file"

    @tool(name="write_file", description="写入文件", parameters={}, required=[])
    async def write_file(self) -> ToolResult:
        return ToolResult(success=True, data="ok")


def _build_agent_with_tools(tools):
    agent = object.__new__(BaseAgent)
    agent._tools = tools
    return agent


def _apply_agent_attrs(agent):
    """补齐 BaseAgent 实例属性（token_counter/context_window/retry_interval + 工具并行/缓存字段）"""
    agent._token_counter = None
    agent._context_window = 64000
    agent._retry_interval = 0.01
    # 工具并行执行/工具结果缓存默认关闭,保持原串行+无缓存语义
    agent._parallel_enabled = False
    agent._concurrency_classifier = None
    agent._max_concurrency = 1
    agent._tool_cache = None
    agent._idempotent_registry = None  # P10-1幂等去重关闭
    agent._session_start_ts = 0.0  # P10-3会话级超时熔断: 0表示不启用
    # 补全 memory 的 predict_token_pressure mock(避免 MagicMock 被 :.1% 格式化报错)
    if hasattr(agent, "_memory") and isinstance(agent._memory, MagicMock):
        agent._memory.predict_token_pressure = MagicMock(return_value={
            "should_emergency_compress": False,
            "should_proactive_compress": False,
            "projected_ratio": 0.0,
        })


class TestGetTool:
    def test_known_tool(self):
        agent = _build_agent_with_tools([_StubShellTool()])
        result = agent._get_tool("shell_execute")
        assert result.name == "shell"

    def test_unknown_tool_raises(self):
        agent = _build_agent_with_tools([_StubShellTool()])
        with pytest.raises(ValueError, match="未知工具: grep"):
            agent._get_tool("grep")

    def test_empty_tools_raises(self):
        agent = _build_agent_with_tools([])
        with pytest.raises(ValueError, match="未知工具: anything"):
            agent._get_tool("anything")


class TestUnknownToolGracefulDegradation:
    """验证BaseAgent.invoke中未知工具调用不会崩溃，而是优雅降级"""

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_to_llm(self):
        """LLM调用不存在的工具时，应返回错误消息给LLM让其自行纠正"""
        agent = _build_agent_with_tools([_StubShellTool()])
        agent._agent_config = AgentConfig(max_iterations=5, max_retries=2)
        agent._format = None
        agent._tool_choice = "auto"
        agent._system_prompt = "test"
        agent.name = "test_agent"
        agent._session_id = "test"
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

        llm_call_count = 0
        llm_responses = []

        first_response = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_001",
                "function": {"name": "grep", "arguments": '{"pattern": "test"}'},
            }],
        }

        second_response = {
            "role": "assistant",
            "content": "已通过shell_execute完成",
            "tool_calls": None,
        }

        llm_responses.append(first_response)
        llm_responses.append(second_response)

        async def mock_invoke_llm(messages, format=None, tools_enabled=True, tool_choice=None, **kwargs):
            nonlocal llm_call_count
            if llm_call_count < len(llm_responses):
                resp = llm_responses[llm_call_count]
                llm_call_count += 1
                return resp
            return {"role": "assistant", "content": "完成", "tool_calls": None}

        agent._invoke_llm = mock_invoke_llm
        _apply_agent_attrs(agent)
        agent._add_to_memory = AsyncMock()
        agent._ensure_memory = AsyncMock()
        agent._memory.add_message = MagicMock()
        agent._memory.add_messages = MagicMock()

        events = []
        async for event in agent.invoke("test query"):
            events.append(event)

        tool_events = [e for e in events if isinstance(e, ToolEvent)]
        assert len(tool_events) == 0, "未知工具不应产生ToolEvent"

        message_events = [e for e in events if hasattr(e, "message") and hasattr(e, "role")]
        assert len(message_events) > 0, "应有最终消息事件"

    @pytest.mark.asyncio
    async def test_unknown_tool_then_correct_tool(self):
        """LLM先调用未知工具失败后，能正确调用已知工具"""
        agent = _build_agent_with_tools([_StubShellTool(), _StubFileTool()])
        agent._agent_config = AgentConfig(max_iterations=10, max_retries=2)
        agent._format = None
        agent._tool_choice = "auto"
        agent._system_prompt = "test"
        agent.name = "test_agent"
        agent._session_id = "test"
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

        call_sequence = []

        first_response = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_001",
                "function": {"name": "grep", "arguments": "{}"},
            }],
        }

        second_response = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_002",
                "function": {"name": "shell_execute", "arguments": "{}"},
            }],
        }

        third_response = {
            "role": "assistant",
            "content": "任务完成",
            "tool_calls": None,
        }

        llm_responses = [first_response, second_response, third_response]
        call_idx = 0

        async def mock_invoke_llm(messages, format=None, tools_enabled=True, tool_choice=None, **kwargs):
            nonlocal call_idx
            if call_idx < len(llm_responses):
                resp = llm_responses[call_idx]
                call_idx += 1
                return resp
            return {"role": "assistant", "content": "完成", "tool_calls": None}

        agent._invoke_llm = mock_invoke_llm
        _apply_agent_attrs(agent)
        agent._add_to_memory = AsyncMock()
        agent._ensure_memory = AsyncMock()
        agent._memory.add_message = MagicMock()
        agent._memory.add_messages = MagicMock()

        events = []
        async for event in agent.invoke("test query"):
            events.append(event)

        tool_events = [e for e in events if isinstance(e, ToolEvent)]
        assert len(tool_events) == 2, "应有2个ToolEvent（calling+called各一个）"
        assert tool_events[0].function_name == "shell_execute"
        assert tool_events[0].status == ToolEventStatus.CALLING
        assert tool_events[1].function_name == "shell_execute"
        assert tool_events[1].status == ToolEventStatus.CALLED

    @pytest.mark.asyncio
    async def test_all_unknown_tools_no_crash(self):
        """所有工具调用都是未知工具时，不应崩溃"""
        agent = _build_agent_with_tools([_StubShellTool()])
        agent._agent_config = AgentConfig(max_iterations=5, max_retries=2)
        agent._format = None
        agent._tool_choice = "auto"
        agent._system_prompt = "test"
        agent.name = "test_agent"
        agent._session_id = "test"
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

        responses = []
        for i in range(6):
            responses.append({
                "role": "assistant",
                "content": None if i < 5 else "放弃调用",
                "tool_calls": [{
                    "id": f"call_{i:03d}",
                    "function": {"name": f"nonexistent_tool_{i}", "arguments": "{}"},
                }] if i < 5 else None,
            })

        call_idx = 0

        async def mock_invoke_llm(messages, format=None, tools_enabled=True, tool_choice=None, **kwargs):
            nonlocal call_idx
            if call_idx < len(responses):
                resp = responses[call_idx]
                call_idx += 1
                return resp
            return {"role": "assistant", "content": "完成", "tool_calls": None}

        agent._invoke_llm = mock_invoke_llm
        _apply_agent_attrs(agent)
        agent._add_to_memory = AsyncMock()
        agent._ensure_memory = AsyncMock()
        agent._memory.add_message = MagicMock()
        agent._memory.add_messages = MagicMock()

        events = []
        async for event in agent.invoke("test query"):
            events.append(event)

        error_events = [e for e in events if isinstance(e, type(events[0])) and hasattr(e, 'error')]
        assert len(error_events) == 0, "不应产生ErrorEvent崩溃"
