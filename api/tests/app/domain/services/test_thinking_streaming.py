#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_thinking_streaming.py
改进A 思考过程流式单元测试 - 验证思考切片推送契约、降级策略、is_thinking 守卫

测试覆盖:
- _stream_thinking: 配置开关/空内容/切片推送顺序与标记/异常降级/短内容单片
- PlannerAgent.create_plan is_thinking 守卫: 思考事件透传,JSON 事件被解析为 PlanEvent
- ReActAgent._execute_step_impl is_thinking 守卫: 思考事件透传,Step JSON 正常解析
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.app_config import AgentConfig
from app.domain.models.event import MessageEvent, PlanEvent, StepEvent, ToolEvent, ToolEventStatus
from app.domain.models.plan import Plan, Step
from app.domain.services.agents.base import BaseAgent
from app.domain.services.agents.planner import PlannerAgent
from app.domain.services.agents.react import ReActAgent


# ========== 测试辅助函数 ==========

def _build_agent(stream_thinking: bool = True,
                 min_chars: int = 10,
                 max_chars: int = 50,
                 delay_ms: int = 0) -> BaseAgent:
    """构建 BaseAgent 实例(绕过 __init__,仅设置思考切片所需属性)"""
    agent = object.__new__(BaseAgent)
    agent.name = "test_agent"
    agent._agent_config = AgentConfig(
        max_iterations=10,
        stream_thinking=stream_thinking,
        stream_chunk_min_chars=min_chars,
        stream_chunk_max_chars=max_chars,
        stream_chunk_delay_ms=delay_ms,
    )
    return agent


async def _collect_events(gen) -> list:
    """收集异步生成器的所有事件"""
    events = []
    async for evt in gen:
        events.append(evt)
    return events


# ========== _stream_thinking 单元测试 ==========

class TestStreamThinking:
    """测试思考切片推送契约与降级策略"""

    @pytest.mark.asyncio
    async def test_disabled_returns_no_events(self):
        """配置关闭(stream_thinking=False)时不产出任何事件,降级为现状不推送"""
        agent = _build_agent(stream_thinking=False)
        reasoning = "这是一段足够长的思考内容用于测试切片逻辑。"
        events = await _collect_events(agent._stream_thinking(reasoning))
        assert events == []

    @pytest.mark.asyncio
    async def test_empty_reasoning_returns_no_events(self):
        """reasoning 为空字符串时不产出事件"""
        agent = _build_agent(stream_thinking=True)
        events = await _collect_events(agent._stream_thinking(""))
        assert events == []

    @pytest.mark.asyncio
    async def test_chunks_carry_is_thinking_flag(self):
        """切片增量 is_thinking=True && is_streaming=True,末尾聚合 is_final=True 写DB

        前后端契约(仿 _stream_final_answer 的 chunk+final 模式):
        - 切片增量(is_streaming=True): 多片,仅推SSE不写DB,前端按 is_thinking 累积
        - 最终聚合(is_final=True): 单片,写DB实现历史回放,前端据此替换累积
        """
        agent = _build_agent(stream_thinking=True, min_chars=10, max_chars=50, delay_ms=0)
        # 构造足够长的思考内容触发切片(两句,每句超 min_chars)
        reasoning = "这是第一句足够长的思考内容用于测试切片。这是第二句足够长的思考内容用于测试切片。"
        events = await _collect_events(agent._stream_thinking(reasoning))

        assert len(events) >= 2, "应至少产出1个增量 + 1个聚合事件"

        # 切片增量: 除最后一个聚合事件外的所有事件
        streaming_events = events[:-1]
        final_event = events[-1]
        for evt in streaming_events:
            assert evt.is_thinking is True, "思考增量必须 is_thinking=True"
            assert evt.is_streaming is True, "思考增量必须 is_streaming=True(仅推SSE不写DB)"
            assert evt.is_final is False, "思考增量不应携带 is_final"
        # 增量拼接还原原文(切片无损)
        assert "".join(evt.message for evt in streaming_events) == reasoning

        # 最终聚合事件: 写DB(is_streaming=False) + 前端替换(is_final=True),message 为完整原文
        assert final_event.is_thinking is True
        assert final_event.is_streaming is False, "聚合事件应写DB(is_streaming=False)"
        assert final_event.is_final is True, "聚合事件应标记 is_final=True 触发前端替换"
        assert final_event.message == reasoning, "聚合事件 message 应为完整思考原文"

    @pytest.mark.asyncio
    async def test_exception_degrades_silently(self):
        """切片异常时静默降级,不抛出,产出0事件(不影响主流程)"""
        agent = _build_agent(stream_thinking=True)
        reasoning = "足够长的思考内容用于触发切片异常测试。"

        with patch.object(
            BaseAgent, "_split_content_into_chunks",
            side_effect=RuntimeError("mock切片异常"),
        ):
            events = await _collect_events(agent._stream_thinking(reasoning))

        assert events == [], "异常时应静默降级,产出0事件"

    @pytest.mark.asyncio
    async def test_short_content_single_chunk(self):
        """短内容(不足 min_chars)作为单片增量 + 末尾聚合事件返回

        短内容不触发切片,产出 1 个增量(is_streaming=True) + 1 个聚合(is_final=True)。
        """
        agent = _build_agent(stream_thinking=True, min_chars=50, max_chars=300, delay_ms=0)
        reasoning = "简短思考。"
        events = await _collect_events(agent._stream_thinking(reasoning))

        assert len(events) == 2, "短内容应产出 1 个增量 + 1 个聚合"
        # 增量事件
        assert events[0].is_thinking is True
        assert events[0].is_streaming is True
        assert events[0].is_final is False
        assert events[0].message == reasoning
        # 聚合事件(写DB + 前端替换)
        assert events[1].is_thinking is True
        assert events[1].is_streaming is False
        assert events[1].is_final is True
        assert events[1].message == reasoning


# ========== PlannerAgent is_thinking 守卫测试 ==========

class TestPlannerThinkingGuard:
    """测试 PlannerAgent.create_plan 的 is_thinking 守卫

    场景: invoke 产出思考 MessageEvent(is_thinking=True) + Plan JSON MessageEvent,
    守卫应确保思考事件透传不被解析为 Plan JSON,JSON 事件正常解析为 PlanEvent。
    """

    @staticmethod
    def _build_planner() -> PlannerAgent:
        """构建 PlannerAgent 实例(绕过 __init__,mock 必要依赖)"""
        planner = object.__new__(PlannerAgent)
        planner.name = "planner"
        planner._build_tools_summary = MagicMock(return_value="")
        return planner

    @pytest.mark.asyncio
    async def test_thinking_event_passthrough_and_json_parsed(self):
        """思考事件透传,JSON 事件被解析为 PlanEvent,二者不混淆"""
        planner = self._build_planner()

        thinking_event = MessageEvent(
            message="这是思考过程的增量内容。",
            is_thinking=True,
            is_streaming=True,
        )
        plan_json = '{"title":"测试任务","goal":"目标","steps":[{"id":"1","description":"步骤一"}]}'
        json_event = MessageEvent(message=plan_json)

        async def _mock_invoke(query):
            yield thinking_event
            yield json_event

        planner.invoke = _mock_invoke
        mock_parser = MagicMock()
        mock_parser.invoke = AsyncMock(return_value={
            "title": "测试任务",
            "goal": "目标",
            "steps": [{"id": "1", "description": "步骤一"}],
        })
        planner._json_parser = mock_parser

        message = MagicMock()
        message.message = "用户任务"
        message.attachments = []

        events = await _collect_events(planner.create_plan(message))

        # 断言: 思考事件透传
        thinking_events = [
            e for e in events
            if isinstance(e, MessageEvent) and getattr(e, "is_thinking", False)
        ]
        assert len(thinking_events) == 1, "思考事件应透传不被解析"
        assert thinking_events[0].message == "这是思考过程的增量内容。"
        # 断言: JSON 事件被解析为 PlanEvent
        plan_events = [e for e in events if isinstance(e, PlanEvent)]
        assert len(plan_events) == 1, "JSON 事件应被解析为 PlanEvent"
        assert plan_events[0].plan.title == "测试任务"

    @pytest.mark.asyncio
    async def test_thinking_event_not_parsed_as_json(self):
        """思考事件不被误解析为 Plan JSON(守卫生效)

        验证: _json_parser.invoke 仅对非思考 MessageEvent 调用一次,
        思考事件不触发 JSON 解析(否则解析失败会创建降级 Plan,污染事件流)。
        """
        planner = self._build_planner()

        thinking_event = MessageEvent(
            message="思考内容不应被JSON解析",
            is_thinking=True,
            is_streaming=True,
        )
        plan_json = '{"title":"任务","goal":"g","steps":[]}'
        json_event = MessageEvent(message=plan_json)

        async def _mock_invoke(query):
            yield thinking_event
            yield json_event

        planner.invoke = _mock_invoke
        mock_parser = MagicMock()
        mock_parser.invoke = AsyncMock(return_value={
            "title": "任务", "goal": "g", "steps": [],
        })
        planner._json_parser = mock_parser

        message = MagicMock()
        message.message = "任务"
        message.attachments = []

        await _collect_events(planner.create_plan(message))

        # _json_parser.invoke 仅被调用一次(仅对 JSON 事件,不对思考事件)
        assert mock_parser.invoke.call_count == 1
        called_arg = mock_parser.invoke.call_args.args[0]
        assert called_arg == plan_json, "解析器应仅被传入 Plan JSON,而非思考内容"


# ========== ReActAgent is_thinking 守卫测试 ==========

class TestReactThinkingGuard:
    """测试 ReActAgent._execute_step_impl 的 is_thinking 守卫

    场景: invoke 产出思考 MessageEvent + ToolEvent(CALLED) + Step JSON MessageEvent,
    守卫应确保思考事件透传,Step JSON 正常解析为 StepEvent(COMPLETED)。
    ToolEvent 用于使 tool_called=True,避免触发「未调用工具」重试逻辑。
    """

    @staticmethod
    def _build_react() -> ReActAgent:
        """构建 ReActAgent 实例(绕过 __init__,mock 必要依赖)"""
        react = object.__new__(ReActAgent)
        react.name = "react"
        react._agent_config = AgentConfig(max_iterations=10)
        react._shell_profiler = None  # 跳过 shell_profiler 交互
        react.set_step_context = MagicMock()
        react.reset_step_context = MagicMock()
        react.force_include_tool = MagicMock()
        react._ensure_research_tool_assembled = MagicMock()
        return react

    @pytest.mark.asyncio
    async def test_thinking_passthrough_and_step_parsed(self):
        """思考事件透传,Step JSON 被解析为 StepEvent(COMPLETED)"""
        react = self._build_react()

        thinking_event = MessageEvent(
            message="执行步骤前的思考过程。",
            is_thinking=True,
            is_streaming=True,
        )
        tool_event = ToolEvent(
            tool_call_id="c1",
            tool_name="shell",
            function_name="shell_execute",
            function_args={"session_id": "s1"},
            status=ToolEventStatus.CALLED,
        )
        step_json = '{"success":true,"result":"步骤完成","attachments":[]}'
        json_event = MessageEvent(message=step_json)

        async def _mock_invoke(query, tool_choice=None, format=None):
            yield thinking_event
            yield tool_event
            yield json_event

        react.invoke = _mock_invoke
        mock_parser = MagicMock()
        mock_parser.invoke = AsyncMock(return_value={
            "success": True,
            "result": "步骤完成",
            "attachments": [],
        })
        react._json_parser = mock_parser

        plan = Plan(
            title="任务", goal="g",
            steps=[Step(id="1", description="步骤一")],
        )
        step = Step(id="1", description="步骤一")
        message = MagicMock()
        message.message = "用户任务"
        message.attachments = []

        # patch 静态方法避免依赖 plan/step 复杂上下文构建
        with patch.object(ReActAgent, "_build_execution_query", return_value=""), \
             patch("app.domain.services.agents.react.verify_batch_completeness",
                   return_value=(True, None)):
            events = await _collect_events(react._execute_step_impl(plan, step, message))

        # 断言: 思考事件透传
        thinking_events = [
            e for e in events
            if isinstance(e, MessageEvent) and getattr(e, "is_thinking", False)
        ]
        assert len(thinking_events) == 1, "思考事件应透传"
        assert thinking_events[0].message == "执行步骤前的思考过程。"
        # 断言: StepEvent(COMPLETED) 产出,step 结果被正确解析
        step_events = [e for e in events if isinstance(e, StepEvent)]
        completed = [e for e in step_events if e.status.value == "completed"]
        assert len(completed) == 1, "应产出 COMPLETED StepEvent"
        assert step.success is True
        assert step.result == "步骤完成"

    @pytest.mark.asyncio
    async def test_thinking_not_parsed_as_step_json(self):
        """思考事件不被误解析为 Step JSON(守卫生效)

        验证: _json_parser.invoke 仅对 Step JSON 调用一次,不对思考事件调用。
        """
        react = self._build_react()

        thinking_event = MessageEvent(
            message="思考内容不应被JSON解析",
            is_thinking=True,
            is_streaming=True,
        )
        tool_event = ToolEvent(
            tool_call_id="c1",
            tool_name="shell",
            function_name="shell_execute",
            function_args={"session_id": "s1"},
            status=ToolEventStatus.CALLED,
        )
        step_json = '{"success":true,"result":"完成","attachments":[]}'
        json_event = MessageEvent(message=step_json)

        async def _mock_invoke(query, tool_choice=None, format=None):
            yield thinking_event
            yield tool_event
            yield json_event

        react.invoke = _mock_invoke
        mock_parser = MagicMock()
        mock_parser.invoke = AsyncMock(return_value={
            "success": True, "result": "完成", "attachments": [],
        })
        react._json_parser = mock_parser

        plan = Plan(
            title="任务", goal="g",
            steps=[Step(id="1", description="步骤一")],
        )
        step = Step(id="1", description="步骤一")
        message = MagicMock()
        message.message = "任务"
        message.attachments = []

        with patch.object(ReActAgent, "_build_execution_query", return_value=""), \
             patch("app.domain.services.agents.react.verify_batch_completeness",
                   return_value=(True, None)):
            await _collect_events(react._execute_step_impl(plan, step, message))

        # _json_parser.invoke 仅被调用一次(仅对 Step JSON,不对思考事件)
        assert mock_parser.invoke.call_count == 1
        called_arg = mock_parser.invoke.call_args.args[0]
        assert called_arg == step_json, "解析器应仅被传入 Step JSON,而非思考内容"
