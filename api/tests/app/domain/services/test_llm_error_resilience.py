#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_llm_error_resilience.py
LLM调用错误韧性+降级交付机制单元测试
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.models.app_config import AgentConfig
from app.domain.models.event import (
    ErrorEvent, MessageEvent, ToolEvent, ToolEventStatus,
)
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.tools.base import BaseTool, tool


class _StubTool(BaseTool):
    name: str = "stub"

    @tool(name="stub_action", description="测试工具", parameters={}, required=[])
    async def stub_action(self) -> ToolResult:
        return ToolResult(success=True, data="ok")


def _build_base_agent() -> BaseAgent:
    agent = object.__new__(BaseAgent)
    agent._tools = [_StubTool()]
    agent._agent_config = AgentConfig(max_iterations=5, max_retries=3)
    agent._format = None
    agent._tool_choice = "auto"
    agent._system_prompt = "test"
    agent.name = "test_agent"
    agent._session_id = "test_session"
    agent._uow_factory = lambda: MagicMock()
    agent._uow = MagicMock()
    agent._retry_interval = 0.01
    agent._json_parser = MagicMock()
    agent._json_parser.invoke = AsyncMock(return_value={})
    agent._memory = MagicMock()
    agent._memory.empty = False
    agent._memory.should_compress = MagicMock(return_value=False)
    agent._memory.is_context_overflow = MagicMock(return_value=False)
    agent._memory.check_token_limit = MagicMock(return_value=False)
    agent._memory.predict_token_pressure = MagicMock(return_value={
        "current_ratio": 0.0, "projected_ratio": 0.0, "pressure_level": "safe",
        "should_proactive_compress": False, "should_emergency_compress": False,
    })
    agent._memory.get_messages = MagicMock(return_value=[])
    agent._add_to_memory = AsyncMock()
    agent._ensure_memory = AsyncMock()
    agent._token_counter = None
    agent._context_window = 64000
    return agent


class TestInvokeLLMErrorMessageRetention:
    """验证_invoke_llm在LLM调用失败时正确保留错误消息"""

    @pytest.mark.asyncio
    async def test_error_detail_from_exception_message(self):
        """异常str(e)非空时，错误消息应被完整保留"""
        agent = _build_base_agent()
        agent._llm = MagicMock()
        agent._llm.invoke = AsyncMock(side_effect=RuntimeError("API rate limit exceeded"))

        with pytest.raises(RuntimeError, match="API rate limit exceeded"):
            await agent._invoke_llm([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_error_detail_from_exception_type_when_str_empty(self):
        """异常str(e)为空时，应使用异常类型名作为错误详情"""
        agent = _build_base_agent()
        agent._llm = MagicMock()

        class SilentError(Exception):
            def __str__(self):
                return ""

        agent._llm.invoke = AsyncMock(side_effect=SilentError())

        with pytest.raises(RuntimeError, match="SilentError"):
            await agent._invoke_llm([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_fallback_to_unknown_error(self):
        """所有重试均返回空内容时，应使用'未知错误'兜底"""
        agent = _build_base_agent()
        agent._llm = MagicMock()
        agent._llm.invoke = AsyncMock(return_value={
            "role": "assistant", "content": "", "tool_calls": None,
        })

        with pytest.raises(RuntimeError, match="未知错误"):
            await agent._invoke_llm([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_context_overflow_triggers_recovery(self):
        """上下文溢出异常应触发溢出恢复并重试"""
        agent = _build_base_agent()
        agent._llm = MagicMock()
        agent._overflow_recovery = AsyncMock()

        call_count = 0

        async def mock_invoke(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("context_length_exceeded")
            return {"role": "assistant", "content": "recovered", "tool_calls": None}

        agent._llm.invoke = mock_invoke

        result = await agent._invoke_llm([{"role": "user", "content": "test"}])
        assert result["content"] == "recovered"
        assert agent._overflow_recovery.call_count == 2

    @pytest.mark.asyncio
    async def test_max_retries_exhausted_with_context_overflow(self):
        """上下文溢出重试耗尽后应抛出含错误详情的RuntimeError"""
        agent = _build_base_agent()
        agent._llm = MagicMock()
        agent._overflow_recovery = AsyncMock()
        agent._llm.invoke = AsyncMock(
            side_effect=RuntimeError("maximum context length exceeded")
        )

        with pytest.raises(RuntimeError, match="maximum context length exceeded"):
            await agent._invoke_llm([{"role": "user", "content": "test"}])


class TestEmitDegradedSummary:
    """验证_emit_degraded_summary降级交付机制"""

    def _build_runner(self, session_events=None):
        from app.domain.services.agent_task_runner import AgentTaskRunner

        runner = object.__new__(AgentTaskRunner)
        runner._session_id = "test_session"

        _uow = MagicMock()
        runner._uow = _uow
        runner._uow_factory = lambda: _uow

        mock_session = MagicMock()
        mock_session.events = session_events or []

        async def mock_get_by_id(session_id):
            return mock_session

        _uow.session = MagicMock()
        _uow.session.get_by_id = mock_get_by_id
        _uow.__aenter__ = AsyncMock(return_value=_uow)
        _uow.__aexit__ = AsyncMock(return_value=False)
        _uow.session.add_event = AsyncMock()

        return runner

    def _build_task(self):
        task = MagicMock()
        task.output_stream = MagicMock()
        task.output_stream.put = AsyncMock(return_value="evt_001")
        return task

    @pytest.mark.asyncio
    async def test_summary_with_tool_events(self):
        """存在工具调用记录时，降级总结应包含工具结果"""
        session_events = [
            ToolEvent(
                tool_call_id="call_001",
                tool_name="search",
                function_name="search",
                function_args={"query": "Python教程"},
                function_result=ToolResult(success=True, data="找到5条搜索结果"),
                status=ToolEventStatus.CALLED,
            ),
            ToolEvent(
                tool_call_id="call_002",
                tool_name="shell",
                function_name="shell",
                function_args={"command": "ls"},
                function_result=ToolResult(success=True, data="file1.txt file2.txt"),
                status=ToolEventStatus.CALLED,
            ),
        ]
        runner = self._build_runner(session_events)
        task = self._build_task()

        await runner._emit_degraded_summary(task, "LLM调用超时")

        task.output_stream.put.assert_called_once()
        add_event_call = runner._uow.session.add_event.call_args
        event_obj = add_event_call[0][1]
        assert isinstance(event_obj, MessageEvent)
        assert "LLM调用超时" in event_obj.message
        assert "search" in event_obj.message
        assert "shell" in event_obj.message

    @pytest.mark.asyncio
    async def test_summary_without_tool_events(self):
        """无工具调用记录时，降级总结应提示未获取到有效结果"""
        runner = self._build_runner([])
        task = self._build_task()

        await runner._emit_degraded_summary(task, "连接超时")

        add_event_call = runner._uow.session.add_event.call_args
        event_obj = add_event_call[0][1]
        assert isinstance(event_obj, MessageEvent)
        assert "连接超时" in event_obj.message
        assert "未获取到有效工具结果" in event_obj.message

    @pytest.mark.asyncio
    async def test_summary_with_none_session(self):
        """会话不存在时，降级总结应提示未获取到有效结果"""
        runner = self._build_runner()
        runner._uow.session.get_by_id = AsyncMock(return_value=None)
        task = self._build_task()

        await runner._emit_degraded_summary(task, "模型不可用")

        add_event_call = runner._uow.session.add_event.call_args
        event_obj = add_event_call[0][1]
        assert isinstance(event_obj, MessageEvent)
        assert "模型不可用" in event_obj.message
        assert "未获取到有效工具结果" in event_obj.message

    @pytest.mark.asyncio
    async def test_summary_filters_only_called_tool_events(self):
        """降级总结应只提取status=called的工具事件"""
        session_events = [
            ToolEvent(
                tool_call_id="call_001",
                tool_name="search",
                function_name="search",
                function_args={},
                status=ToolEventStatus.CALLING,
            ),
            ToolEvent(
                tool_call_id="call_002",
                tool_name="shell",
                function_name="shell",
                function_args={"command": "pwd"},
                function_result=ToolResult(success=True, data="/home/ubuntu"),
                status=ToolEventStatus.CALLED,
            ),
            MessageEvent(role="assistant", message="hello"),
        ]
        runner = self._build_runner(session_events)
        task = self._build_task()

        await runner._emit_degraded_summary(task, "重试耗尽")

        add_event_call = runner._uow.session.add_event.call_args
        event_obj = add_event_call[0][1]
        assert "shell" in event_obj.message
        assert "search" not in event_obj.message

    @pytest.mark.asyncio
    async def test_summary_db_failure_does_not_crash(self):
        """数据库访问失败时，降级总结不应导致二次异常"""
        runner = self._build_runner()
        runner._uow.session.get_by_id = AsyncMock(side_effect=Exception("DB连接断开"))
        runner._uow.__aenter__ = AsyncMock(side_effect=Exception("DB连接断开"))
        task = self._build_task()

        await runner._emit_degraded_summary(task, "LLM失败")

        task.output_stream.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_summary_truncates_long_content(self):
        """工具结果过长时应截断显示"""
        long_data = "x" * 500
        session_events = [
            ToolEvent(
                tool_call_id="call_001",
                tool_name="search",
                function_name="search",
                function_args={"query": "test"},
                function_result=ToolResult(success=True, data=long_data),
                status=ToolEventStatus.CALLED,
            ),
        ]
        runner = self._build_runner(session_events)
        task = self._build_task()

        await runner._emit_degraded_summary(task, "超时")

        add_event_call = runner._uow.session.add_event.call_args
        event_obj = add_event_call[0][1]
        assert len(event_obj.message) < len(long_data)
