#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_react_agent.py
ReActAgent单元测试 — 13优化: 前序步骤完成情况注入
"""
import pytest

from app.domain.models.event import (
    StepEvent, StepEventStatus, MessageEvent, WaitEvent,
)
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.services.agents.react import ReActAgent, _is_likely_question


def _make_step(
    step_id: str,
    description: str = "",
    result: str = "",
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
) -> Step:
    """构造测试用Step对象"""
    step = Step(id=step_id, description=description)
    step.result = result
    step.status = status
    step.success = (status == ExecutionStatus.COMPLETED)
    return step


def _make_plan(steps: list[Step]) -> Plan:
    """构造测试用Plan对象"""
    return Plan(title="test", steps=steps, language="zh")


class TestBuildPriorStepsContext:
    """_build_prior_steps_context 前序步骤完成情况注入测试 (13优化)"""

    def test_no_completed_steps_returns_empty(self):
        """无已完成步骤时返回空字符串"""
        steps = [
            _make_step("1", description="步骤1", status=ExecutionStatus.PENDING),
            _make_step("2", description="步骤2", status=ExecutionStatus.PENDING),
        ]
        plan = _make_plan(steps)
        current = steps[1]
        result = ReActAgent._build_prior_steps_context(plan, current)
        assert result == ""

    def test_one_completed_step_includes_result(self):
        """单个已完成步骤的结果应包含在摘要中"""
        steps = [
            _make_step("1", description="下载数据", result="数据已下载至/home/ubuntu/data.xlsx"),
            _make_step("2", description="分析数据", status=ExecutionStatus.PENDING),
        ]
        plan = _make_plan(steps)
        current = steps[1]
        result = ReActAgent._build_prior_steps_context(plan, current)
        assert "前序步骤完成情况" in result
        assert "步骤1" in result
        assert "数据已下载至/home/ubuntu/data.xlsx" in result
        assert "严禁重复执行" in result

    def test_multiple_completed_steps_all_included(self):
        """多个已完成步骤的结果都应包含在摘要中"""
        steps = [
            _make_step("1", description="导出数据", result="数据导出完成"),
            _make_step("2", description="下载数据", result="文件已下载"),
            _make_step("3", description="分析数据", status=ExecutionStatus.PENDING),
        ]
        plan = _make_plan(steps)
        current = steps[2]
        result = ReActAgent._build_prior_steps_context(plan, current)
        assert "步骤1" in result
        assert "数据导出完成" in result
        assert "步骤2" in result
        assert "文件已下载" in result

    def test_completed_step_without_result_excluded(self):
        """已完成但无result的步骤不包含在摘要中"""
        steps = [
            _make_step("1", description="导出数据", result=""),
            _make_step("2", description="分析数据", status=ExecutionStatus.PENDING),
        ]
        plan = _make_plan(steps)
        current = steps[1]
        result = ReActAgent._build_prior_steps_context(plan, current)
        assert result == ""

    def test_current_step_excluded_even_if_completed(self):
        """当前步骤即使已完成也不包含在摘要中"""
        steps = [
            _make_step("1", description="步骤1", result="结果1"),
            _make_step("2", description="步骤2", result="结果2"),
        ]
        plan = _make_plan(steps)
        current = steps[1]
        result = ReActAgent._build_prior_steps_context(plan, current)
        assert "步骤1" in result
        assert "结果1" in result
        # 当前步骤(步骤2)不应出现在前序摘要中
        assert "结果2" not in result

    def test_no_plan_returns_empty(self):
        """plan为None时返回空字符串"""
        result = ReActAgent._build_prior_steps_context(None, None)
        assert result == ""

    def test_empty_steps_returns_empty(self):
        """空步骤列表返回空字符串"""
        plan = _make_plan([])
        step = _make_step("1")
        result = ReActAgent._build_prior_steps_context(plan, step)
        assert result == ""

    def test_long_result_truncated(self):
        """过长的步骤结果应被截断到300字符"""
        long_result = "A" * 500
        steps = [
            _make_step("1", description="步骤1", result=long_result),
            _make_step("2", description="步骤2", status=ExecutionStatus.PENDING),
        ]
        plan = _make_plan(steps)
        current = steps[1]
        result = ReActAgent._build_prior_steps_context(plan, current)
        # 截取后应不超过300字符(加上前缀文本)
        assert "A" * 300 in result
        assert "A" * 301 not in result

    def test_failed_step_excluded(self):
        """FAILED状态的步骤不包含在摘要中"""
        steps = [
            _make_step("1", description="步骤1", result="失败结果", status=ExecutionStatus.FAILED),
            _make_step("2", description="步骤2", status=ExecutionStatus.PENDING),
        ]
        plan = _make_plan(steps)
        current = steps[1]
        result = ReActAgent._build_prior_steps_context(plan, current)
        assert result == ""

    def test_running_step_excluded(self):
        """RUNNING状态的步骤不包含在摘要中"""
        steps = [
            _make_step("1", description="步骤1", result="运行中", status=ExecutionStatus.RUNNING),
            _make_step("2", description="步骤2", status=ExecutionStatus.PENDING),
        ]
        plan = _make_plan(steps)
        current = steps[1]
        result = ReActAgent._build_prior_steps_context(plan, current)
        assert result == ""

    def test_format_contains_reuse_directive(self):
        """摘要应包含"可直接复用,不得重复执行"指令"""
        steps = [
            _make_step("1", description="下载数据", result="已下载"),
            _make_step("2", description="分析", status=ExecutionStatus.PENDING),
        ]
        plan = _make_plan(steps)
        current = steps[1]
        result = ReActAgent._build_prior_steps_context(plan, current)
        assert "可直接复用" in result
        assert "不得重复执行" in result


class TestBuildExecutionQueryWithPriorContext:
    """_build_execution_query 前序步骤上下文注入 + MCP能力引导注入集成测试"""

    def test_execution_query_includes_prior_steps(self):
        """执行query应包含前序步骤完成情况"""
        from app.domain.models.message import Message

        steps = [
            _make_step("1", description="导出数据", result="数据已导出至data.xlsx"),
            _make_step("2", description="分析数据", status=ExecutionStatus.PENDING),
        ]
        plan = _make_plan(steps)
        message = Message(message="分析数据", attachments=[])
        query = ReActAgent._build_execution_query(plan, steps[1], message)

        assert "【前序步骤完成情况" in query
        assert "数据已导出至data.xlsx" in query
        assert "当前步骤：分析数据" in query

    def test_execution_query_no_prior_steps_when_first_step(self):
        """第一步执行时query不应包含前序步骤完成情况块"""
        from app.domain.models.message import Message

        steps = [
            _make_step("1", description="第一步", status=ExecutionStatus.PENDING),
            _make_step("2", description="第二步", status=ExecutionStatus.PENDING),
        ]
        plan = _make_plan(steps)
        message = Message(message="执行任务", attachments=[])
        query = ReActAgent._build_execution_query(plan, steps[0], message)

        # EXECUTION_PROMPT含"前序步骤复用"约束文本,但不应含实际的前序步骤结果块
        assert "【前序步骤完成情况" not in query
        assert "当前步骤：第一步" not in query

    def test_execution_query_injects_mcp_hint_for_weather_step(self):
        """天气步骤的执行query应主动注入MCP工具直接调用引导(根因会话9c803902核心验证)

        验证F2+F3修复:步骤开始时主动注入MCP引导,而非等no_tool_retry才注入。
        MCP直接加载:引导LLM在工具列表中查找mcp_前缀工具直接调用。
        """
        from app.domain.models.message import Message

        step = _make_step("1", description="搜索 广州 今天 天气", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="查天气", attachments=[])
        query = ReActAgent._build_execution_query(plan, step, message)

        # 应包含MCP工具直接调用引导(主动注入,非retry场景)
        assert "mcp_" in query
        assert "专业领域能力" in query
        assert "直接调用" in query

    def test_execution_query_no_mcp_hint_for_plain_step(self):
        """普通步骤的执行query不应注入MCP工具直接调用引导"""
        from app.domain.models.message import Message

        step = _make_step("1", description="导出数据到Excel文件", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="导出数据", attachments=[])
        query = ReActAgent._build_execution_query(plan, step, message)

        # 不应含主动注入的【系统提示】专业能力引导块
        assert "【系统提示】本步骤涉及专业领域能力" not in query

    def test_execution_query_injects_mcp_hint_for_image_step(self):
        """图片步骤的执行query应主动注入MCP工具直接调用引导(多模态回归)"""
        from app.domain.models.message import Message

        step = _make_step(
            "1",
            description="使用多模态图片理解工具分析图片/home/ubuntu/upload/test.jpg",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="分析图片", attachments=[])
        query = ReActAgent._build_execution_query(plan, step, message)

        assert "mcp_" in query
        assert "专业领域能力" in query
        assert "直接调用" in query


class _FakeJsonParser:
    """伪JSON解析器,解析文本为字典供 Step.model_validate 使用;解析失败时返回默认结果"""

    def __init__(self, fallback_dict: dict):
        self._fallback_dict = fallback_dict

    async def invoke(self, text: str):
        import json as _json
        try:
            return _json.loads(text)
        except Exception:
            return self._fallback_dict


class TestExecuteStepToolCallGuard:
    """execute_step 工具调用保障单元测试 (首次无工具调用注入引导重试)"""

    @staticmethod
    def _make_agent(event_sequences: list[list]) -> ReActAgent:
        """构造测试用 ReActAgent,mock invoke 按序列返回事件

        Args:
            event_sequences: 每次调用 invoke 返回的事件列表序列

        Note:
            mock_invoke 接受 tool_choice 参数(与 BaseAgent.invoke 签名一致),
            并记录每次调用的 tool_choice 到 agent._invoke_tool_choices 供断言。
            通过 object.__new__ 绕过 __init__,需手动补全 _json_parser 与 _agent_config
            (后者用于F2-4外置的special_capability_keywords注入)。
        """
        from app.domain.models.app_config import AgentConfig

        agent = object.__new__(ReActAgent)
        agent._json_parser = _FakeJsonParser(
            fallback_dict={"success": True, "result": "任务完成", "attachments": []}
        )
        # 补全_agent_config(F2-4外置后,execute_step需读取special_capability_keywords)
        agent._agent_config = AgentConfig()
        call_idx = [0]
        agent._invoke_tool_choices = []  # 记录每次invoke调用的tool_choice参数

        async def mock_invoke(query: str, format=None, tool_choice=None):
            agent._invoke_tool_choices.append(tool_choice)
            idx = call_idx[0]
            call_idx[0] += 1
            if idx < len(event_sequences):
                for ev in event_sequences[idx]:
                    yield ev

        agent.invoke = mock_invoke
        return agent

    @pytest.mark.asyncio
    async def test_with_tool_call_completes_normally(self):
        """有工具调用时正常完成步骤,不触发重试"""
        from app.domain.models.event import (
            ToolEvent, ToolEventStatus, MessageEvent,
        )
        step = _make_step("1", description="执行任务", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="测试任务", attachments=[])

        agent = self._make_agent([
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="shell_execute",
                    function_args={"command": "echo hi"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="shell_execute",
                    function_args={"command": "echo hi"},
                    status=ToolEventStatus.CALLED,
                ),
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "完成", "attachments": []}',
                ),
            ]
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        assert step.status == ExecutionStatus.COMPLETED
        assert any(
            isinstance(e, StepEvent) and e.status == StepEventStatus.COMPLETED
            for e in events
        )

    @pytest.mark.asyncio
    async def test_first_no_tool_triggers_retry_then_completes(self):
        """首次无工具调用时注入引导重试,重试后有工具调用则正常完成"""
        from app.domain.models.event import ToolEvent, ToolEventStatus, MessageEvent
        step = _make_step("1", description="分析数据", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="分析数据", attachments=[])

        agent = self._make_agent([
            # 第一次invoke: 无工具调用,直接返回MessageEvent
            [MessageEvent(role="assistant", message='{"success": true, "result": "完成", "attachments": []}')],
            # 第二次invoke: 有工具调用后返回MessageEvent
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="read_file",
                    function_args={"filepath": "/tmp/data.txt"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="read_file",
                    function_args={"filepath": "/tmp/data.txt"},
                    status=ToolEventStatus.CALLED,
                ),
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "分析完成", "attachments": []}',
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 步骤最终完成
        assert step.status == ExecutionStatus.COMPLETED
        # 应有STARTED和COMPLETED的StepEvent
        started = [e for e in events if isinstance(e, StepEvent) and e.status == StepEventStatus.STARTED]
        completed = [e for e in events if isinstance(e, StepEvent) and e.status == StepEventStatus.COMPLETED]
        assert len(started) >= 1
        assert len(completed) >= 1
        # 验证重试后调用了read_file工具
        tool_events = [e for e in events if isinstance(e, ToolEvent)]
        assert any(e.function_name == "read_file" for e in tool_events)

    @pytest.mark.asyncio
    async def test_second_no_tool_accepts_result_no_infinite_loop(self):
        """二次仍无工具调用时接受结果完成步骤,不陷入无限重试"""
        from app.domain.models.event import MessageEvent
        step = _make_step("1", description="纯文本回复", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="自我介绍", attachments=[])

        agent = self._make_agent([
            # 第一次: 无工具调用
            [MessageEvent(role="assistant", message='{"success": true, "result": "我是AI", "attachments": []}')],
            # 第二次: 仍无工具调用
            [MessageEvent(role="assistant", message='{"success": true, "result": "我是AI助手", "attachments": []}')],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 二次无工具调用后应接受结果,步骤完成
        assert step.status == ExecutionStatus.COMPLETED
        assert step.result == "我是AI助手"
        # 仅一次COMPLETED事件(无第三次重试)
        completed = [e for e in events if isinstance(e, StepEvent) and e.status == StepEventStatus.COMPLETED]
        assert len(completed) == 1

    @pytest.mark.asyncio
    async def test_message_ask_user_yields_wait_event(self):
        """message_ask_user 工具调用应触发 WaitEvent 并提前返回"""
        from app.domain.models.event import ToolEvent, ToolEventStatus
        step = _make_step("1", description="询问用户", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="需要输入", attachments=[])

        agent = self._make_agent([
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="message",
                    function_name="message_ask_user",
                    function_args={"text": "请提供账号"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="message",
                    function_name="message_ask_user",
                    function_args={"text": "请提供账号"},
                    status=ToolEventStatus.CALLED,
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 应产生 WaitEvent
        assert any(isinstance(e, WaitEvent) for e in events)
        # 应产生 CALLING 阶段转发的 MessageEvent(ask_user 文本)
        msg_events = [e for e in events if isinstance(e, MessageEvent)]
        assert any(e.message == "请提供账号" for e in msg_events)
        # 步骤不应被标记为 COMPLETED(ask_user 提前 return)
        assert step.status == ExecutionStatus.RUNNING

    @pytest.mark.asyncio
    async def test_message_ask_user_empty_text_skips_wait_event(self):
        """message_ask_user 空文本调用应跳过 WaitEvent,避免会话永久卡死

        根因: P4-6流式改造后,LLM偶发产生空参数的message_ask_user tool_call,
        参数JSON解析失败降级为空字典{},导致text缺失。
        若yield WaitEvent,会话将因无提问内容而永久卡死(用户无内容可回复)。
        修复: 空文本时不yield WaitEvent,让invoke循环继续,LLM重新生成响应。
        """
        from app.domain.models.event import ToolEvent, ToolEventStatus, MessageEvent
        step = _make_step("1", description="查询天气", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="今天天气怎么样", attachments=[])

        agent = self._make_agent([
            # 第一次invoke: message_ask_user空文本调用 + 后续正常完成
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="message",
                    function_name="message_ask_user",
                    function_args={},  # 空参数(text缺失)
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="message",
                    function_name="message_ask_user",
                    function_args={},  # 空参数(text缺失)
                    status=ToolEventStatus.CALLED,
                ),
                # LLM收到工具结果后重新生成,返回正常步骤结果
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "天气查询完成", "attachments": []}',
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 核心断言: 不应产生 WaitEvent(空文本时跳过)
        assert not any(isinstance(e, WaitEvent) for e in events), \
            "空文本message_ask_user不应产生WaitEvent,否则会话会永久卡死"
        # 不应产生空消息的MessageEvent
        msg_events = [e for e in events if isinstance(e, MessageEvent) and not getattr(e, "is_thinking", False)]
        assert all(e.message for e in msg_events), \
            "不应产生空消息的MessageEvent"
        # 步骤应正常完成(不是RUNNING/WAITING)
        assert step.status == ExecutionStatus.COMPLETED
        assert step.success is True

    @pytest.mark.asyncio
    async def test_message_ask_user_whitespace_text_skips_wait_event(self):
        """message_ask_user 纯空白文本调用应跳过 WaitEvent(防御性测试)

        验证strip()处理: 仅含空格/换行的text也视为空文本。
        """
        from app.domain.models.event import ToolEvent, ToolEventStatus, MessageEvent
        step = _make_step("1", description="查询天气", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="今天天气怎么样", attachments=[])

        agent = self._make_agent([
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="message",
                    function_name="message_ask_user",
                    function_args={"text": "   \n  "},  # 纯空白
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="message",
                    function_name="message_ask_user",
                    function_args={"text": "   \n  "},  # 纯空白
                    status=ToolEventStatus.CALLED,
                ),
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "完成", "attachments": []}',
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 纯空白文本也应跳过 WaitEvent
        assert not any(isinstance(e, WaitEvent) for e in events)
        assert step.status == ExecutionStatus.COMPLETED


class TestBuildNoToolRetryGuidance:
    """_build_no_tool_retry_guidance 无工具调用重试引导测试"""

    def test_base_guidance_for_plain_task(self):
        """普通步骤(非专业能力)应返回基础引导,不含MCP直接调用提示"""
        guidance = ReActAgent._build_no_tool_retry_guidance("导出数据到Excel文件")
        assert "未调用任何工具" in guidance
        assert "mcp_前缀MCP工具" in guidance  # 基础引导也提及mcp_前缀MCP工具
        assert "特别注意" not in guidance

    def test_mcp_hint_for_image_keyword(self):
        """步骤描述含'图片'时应追加MCP工具直接调用提示"""
        guidance = ReActAgent._build_no_tool_retry_guidance(
            "使用多模态图片理解工具分析附件图片"
        )
        assert "未调用任何工具" in guidance
        assert "mcp_" in guidance
        assert "特别注意" in guidance

    def test_mcp_hint_for_ocr_keyword(self):
        """步骤描述含'OCR'时应追加MCP工具直接调用提示"""
        guidance = ReActAgent._build_no_tool_retry_guidance(
            "对文档执行OCR文字识别"
        )
        assert "特别注意" in guidance
        assert "mcp_" in guidance

    def test_mcp_hint_for_video_keyword(self):
        """步骤描述含'视频'时应追加MCP工具直接调用提示"""
        guidance = ReActAgent._build_no_tool_retry_guidance(
            "分析视频内容并提取关键帧"
        )
        assert "特别注意" in guidance

    def test_mcp_hint_for_english_image_keyword(self):
        """步骤描述含英文'image'时应追加MCP工具直接调用提示"""
        guidance = ReActAgent._build_no_tool_retry_guidance(
            "Analyze the image to extract location"
        )
        assert "特别注意" in guidance

    def test_mcp_hint_for_weather_keyword(self):
        """步骤描述含'天气'时应追加MCP工具直接调用提示(新增,根因会话9c803902)"""
        guidance = ReActAgent._build_no_tool_retry_guidance("搜索 广州 今天 天气")
        assert "特别注意" in guidance
        assert "mcp_" in guidance
        assert "专业领域能力" in guidance

    def test_mcp_hint_for_map_keyword(self):
        """步骤描述含'地图'时应追加MCP工具直接调用提示(新增)"""
        guidance = ReActAgent._build_no_tool_retry_guidance("使用地图导航规划路线")
        assert "特别注意" in guidance
        assert "mcp_" in guidance

    def test_no_mcp_hint_for_shell_task(self):
        """步骤描述为shell操作时不应追加MCP提示"""
        guidance = ReActAgent._build_no_tool_retry_guidance(
            "执行shell命令查看目录文件"
        )
        assert "特别注意" not in guidance

    def test_guidance_starts_with_newlines(self):
        """引导文本应以两个换行开头(用于追加到query末尾)"""
        guidance = ReActAgent._build_no_tool_retry_guidance("测试步骤")
        assert guidance.startswith("\n\n")

    def test_mcp_hint_for_vision_keyword(self):
        """步骤描述含'视觉'时应追加MCP工具直接调用提示"""
        guidance = ReActAgent._build_no_tool_retry_guidance(
            "使用视觉模型识别图片中的文字"
        )
        assert "特别注意" in guidance
        assert "mcp_" in guidance


class TestBuildMcpCapabilityHint:
    """_build_mcp_capability_hint 主动MCP工具直接调用引导测试(新增,F2+F3核心)

    验证步骤开始时主动注入MCP工具直接调用引导,防止LLM首调即退化到search_web。
    MCP直接加载: 工具已全量加载到工具列表,引导LLM查找mcp_前缀工具直接调用。
    根因会话9c803902: "搜索广州今天天气"首调即用search_web,因search_web也是工具,
    未触发no_tool_retry,导致MCP天气工具从未被调用。
    """

    def test_returns_none_for_plain_task(self):
        """普通步骤(非专业能力)应返回None,不注入MCP引导"""
        result = ReActAgent._build_mcp_capability_hint("导出数据到Excel文件")
        assert result is None

    def test_returns_none_for_shell_task(self):
        """shell操作应返回None"""
        result = ReActAgent._build_mcp_capability_hint("执行shell命令查看目录")
        assert result is None

    def test_returns_none_for_empty_description(self):
        """空描述应返回None"""
        result = ReActAgent._build_mcp_capability_hint("")
        assert result is None

    def test_returns_hint_for_weather_keyword(self):
        """天气步骤应返回MCP直接调用引导(根因会话9c803902核心场景)"""
        result = ReActAgent._build_mcp_capability_hint("搜索 广州 今天 天气")
        assert result is not None
        assert "mcp_" in result
        assert "专业领域能力" in result
        assert "直接调用" in result
        assert "search_web" in result  # 兜底说明仍提及search_web

    def test_returns_hint_for_image_keyword(self):
        """图片步骤应返回MCP直接调用引导(多模态回归)"""
        result = ReActAgent._build_mcp_capability_hint("使用多模态图片理解工具分析图片")
        assert result is not None
        assert "mcp_" in result
        assert "直接调用" in result

    def test_returns_hint_for_map_keyword(self):
        """地图步骤应返回MCP直接调用引导"""
        result = ReActAgent._build_mcp_capability_hint("使用地图导航规划路线")
        assert result is not None
        assert "mcp_" in result
        assert "直接调用" in result

    def test_returns_hint_for_location_keyword(self):
        """位置步骤应返回MCP直接调用引导"""
        result = ReActAgent._build_mcp_capability_hint("识别图片中的位置信息")
        assert result is not None
        assert "mcp_" in result
        assert "直接调用" in result

    def test_hint_does_not_start_with_newlines(self):
        """主动注入的引导不应以换行开头(与retry guidance不同,由_build_execution_query负责拼接)"""
        result = ReActAgent._build_mcp_capability_hint("查询广州天气")
        assert result is not None
        assert not result.startswith("\n")

    def test_hint_includes_direct_call_guidance(self):
        """引导应包含直接调用MCP工具的指引(MCP直接加载模式)"""
        result = ReActAgent._build_mcp_capability_hint("查询天气")
        assert result is not None
        # MCP直接加载: 引导在工具列表中查找mcp_前缀工具直接调用
        assert "mcp_" in result
        assert "直接调用" in result
        assert "工具列表" in result

    def test_hint_includes_fallback_to_search_web(self):
        """引导应说明无匹配MCP工具后才可改用search_web兜底"""
        result = ReActAgent._build_mcp_capability_hint("查询天气")
        assert result is not None
        assert "search_web" in result
        assert "兜底" in result


class TestIsSpecialCapabilityStep:
    """_is_special_capability_step 专业领域能力步骤识别测试

    覆盖两类能力:
    1. 多模态能力: 图片/OCR/语音/视频/视觉等(原_is_multimodal_step能力)
    2. 专业领域服务: 天气/地图/位置/翻译/汇率等(新增,根因会话9c803902)
    """

    # === 多模态能力关键词(回归测试,原_is_multimodal_step覆盖范围) ===

    def test_image_chinese_keyword(self):
        """中文'图片'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("使用多模态图片理解工具分析图片") is True

    def test_image_chinese_keyword_2(self):
        """中文'图像'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("对图像进行分析") is True

    def test_ocr_keyword(self):
        """'OCR'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("执行OCR文字识别") is True

    def test_multimodal_keyword(self):
        """'多模态'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("使用多模态能力分析") is True

    def test_speech_keyword(self):
        """'语音'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("语音转文字") is True

    def test_video_keyword(self):
        """'视频'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("分析视频内容") is True

    def test_vision_keyword(self):
        """'视觉'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("使用视觉模型") is True

    def test_english_image_keyword(self):
        """英文'image'关键词应识别为专业能力步骤(大小写不敏感)"""
        assert ReActAgent._is_special_capability_step("Analyze the image") is True

    def test_english_vision_keyword(self):
        """英文'vision'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("Use vision model") is True

    def test_english_ocr_keyword(self):
        """英文'ocr'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("Run OCR on document") is True

    def test_case_insensitive_english(self):
        """英文关键词应大小写不敏感"""
        assert ReActAgent._is_special_capability_step("IMAGE ANALYSIS") is True
        assert ReActAgent._is_special_capability_step("Video Processing") is True

    # === 专业领域服务关键词(新增,根因会话9c803902: 天气查询未走MCP) ===

    def test_weather_chinese_keyword(self):
        """中文'天气'关键词应识别为专业能力步骤(根因会话9c803902核心场景)"""
        assert ReActAgent._is_special_capability_step("搜索 广州 今天 天气") is True

    def test_weather_english_keyword(self):
        """英文'weather'关键词应识别为专业能力步骤(大小写不敏感)"""
        assert ReActAgent._is_special_capability_step("Check the weather in Beijing") is True

    def test_map_chinese_keyword(self):
        """中文'地图'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("使用地图导航规划路线") is True

    def test_map_english_keyword(self):
        """英文'map'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("Show me the map of Shanghai") is True

    def test_location_chinese_keyword(self):
        """中文'位置'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("识别图片中的位置信息") is True

    def test_location_english_keyword(self):
        """英文'location'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("Extract location from photo") is True

    def test_supplier_name_not_treated_as_special(self):
        """供应商名称(高德/amap)不作为独立识别关键词(通用型智能体原则)

        供应商名称是特定品牌,不应作为独立识别关键词。
        但"amap"包含通用能力词"map"的子串,仍会被识别(合理:amap确实是地图服务)。
        纯供应商名"高德"已从关键词列表移除,不含通用能力词时不触发识别。
        """
        # "调用高德地图API查询"被识别:因包含通用能力词"地图"(非因"高德")
        assert ReActAgent._is_special_capability_step("调用高德地图API查询") is True
        # "Use amap service"被识别:因"amap"包含"map"子串(amap确实是地图服务,合理)
        assert ReActAgent._is_special_capability_step("Use amap service") is True
        # 纯供应商名"高德"不含任何通用能力词,不触发识别
        assert ReActAgent._is_special_capability_step("调用高德API查询服务") is False

    def test_translate_keyword(self):
        """'翻译'/'translate'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("将文档翻译为英文") is True
        assert ReActAgent._is_special_capability_step("translate this text") is True

    def test_exchange_rate_keyword(self):
        """'汇率'/'exchange'关键词应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("查询今日美元汇率") is True
        assert ReActAgent._is_special_capability_step("currency exchange rate") is True

    def test_session_9c803902_scenario(self):
        """复现会话9c803902场景: '搜索 广州 今天 天气'应被识别为专业能力步骤"""
        # 这是本次优化的核心验证: 该步骤描述必须触发MCP工具搜索引导
        assert ReActAgent._is_special_capability_step("搜索 广州 今天 天气") is True

    # === 非专业能力步骤(不应触发MCP引导) ===

    def test_non_special_shell_task(self):
        """shell操作不应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("执行shell命令查看目录") is False

    def test_non_special_data_task(self):
        """数据处理不应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("导出数据到Excel文件") is False

    def test_non_special_file_task(self):
        """文件操作不应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("读取文件内容并分析") is False

    def test_non_special_search_task(self):
        """通用搜索(不含专业能力关键词)不应识别为专业能力步骤"""
        # "搜索"本身不是专业能力关键词,避免通用搜索任务误触发MCP引导
        assert ReActAgent._is_special_capability_step("搜索最新的行业报告") is False

    def test_empty_description(self):
        """空描述不应识别为专业能力步骤"""
        assert ReActAgent._is_special_capability_step("") is False


class TestMultimodalStepForceToolChoice:
    """步骤强制工具调用测试 (execute_step重试时tool_choice="required")

    优化说明:原仅多模态步骤强制tool_choice="required",现扩展到所有步骤,
    防止LLM在json_object模式下跳过工具调用直接返回JSON结果。
    """

    @pytest.mark.asyncio
    async def test_multimodal_step_retry_forces_tool_choice_required(self):
        """多模态步骤首次无工具调用时,重试应强制tool_choice='required'"""
        from app.domain.models.event import MessageEvent, ToolEvent, ToolEventStatus
        step = _make_step(
            "1",
            description="使用多模态图片理解工具分析图片/home/ubuntu/upload/test.jpg",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="分析图片", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            # 第一次invoke: 无工具调用,直接返回MessageEvent
            [MessageEvent(role="assistant", message='{"success": true, "result": "未找到工具", "attachments": []}')],
            # 第二次invoke(强制tool_choice="required"): 有工具调用(MCP直接调用模式)
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="mcp",
                    function_name="mcp_mcp-multimodal_vl_image_understand",
                    function_args={"image_source": "upload://test.jpg"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="mcp",
                    function_name="mcp_mcp-multimodal_vl_image_understand",
                    function_args={"image_source": "upload://test.jpg"},
                    status=ToolEventStatus.CALLED,
                ),
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "已识别图片", "attachments": []}',
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 步骤最终完成
        assert step.status == ExecutionStatus.COMPLETED
        # 第一次调用tool_choice应为None(默认),第二次应为"required"(强制工具调用)
        assert len(agent._invoke_tool_choices) == 2
        assert agent._invoke_tool_choices[0] is None
        assert agent._invoke_tool_choices[1] == "required"
        # 验证重试后调用了MCP工具(直接调用模式)
        tool_events = [e for e in events if isinstance(e, ToolEvent)]
        assert any(e.function_name.startswith("mcp_") for e in tool_events)

    @pytest.mark.asyncio
    async def test_non_multimodal_step_retry_also_forces_tool_choice(self):
        """非多模态步骤首次无工具调用时,重试也应强制tool_choice='required'

        优化点:原仅多模态步骤强制,现所有步骤统一强制,防止LLM跳过工具调用。
        """
        from app.domain.models.event import MessageEvent, ToolEvent, ToolEventStatus
        step = _make_step(
            "1",
            description="导出数据到Excel文件",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="导出数据", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            # 第一次invoke: 无工具调用
            [MessageEvent(role="assistant", message='{"success": true, "result": "完成", "attachments": []}')],
            # 第二次invoke(强制tool_choice="required"): 有工具调用
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="shell_execute",
                    function_args={"command": "echo hi"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="shell_execute",
                    function_args={"command": "echo hi"},
                    status=ToolEventStatus.CALLED,
                ),
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "导出完成", "attachments": []}',
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 步骤最终完成
        assert step.status == ExecutionStatus.COMPLETED
        # 第一次tool_choice=None,第二次tool_choice="required"(所有步骤统一强制)
        assert len(agent._invoke_tool_choices) == 2
        assert agent._invoke_tool_choices[0] is None
        assert agent._invoke_tool_choices[1] == "required"

    @pytest.mark.asyncio
    async def test_file_operation_step_retry_forces_tool_choice(self):
        """文件操作步骤(如write_file)首次无工具调用时,重试应强制tool_choice='required'

        验证场景:用户反馈'使用write_file创建test_hello.txt'步骤未进行工具调用。
        修复后:所有步骤(含文件操作)重试时统一强制tool_choice="required"。
        """
        from app.domain.models.event import MessageEvent, ToolEvent, ToolEventStatus
        step = _make_step(
            "1",
            description="使用write_file工具在工作目录创建test_hello.txt文件",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="创建测试文件", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            # 第一次: 无工具调用,直接返回JSON结果
            [MessageEvent(role="assistant", message='{"success": true, "result": "文件已创建", "attachments": []}')],
            # 第二次(强制tool_choice="required"): 调用write_file
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="file",
                    function_name="write_file",
                    function_args={"filepath": "/home/ubuntu/test_hello.txt", "content": "Hello World"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="file",
                    function_name="write_file",
                    function_args={"filepath": "/home/ubuntu/test_hello.txt", "content": "Hello World"},
                    status=ToolEventStatus.CALLED,
                ),
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "文件已创建", "attachments": ["/home/ubuntu/test_hello.txt"]}',
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 步骤最终完成
        assert step.status == ExecutionStatus.COMPLETED
        # 第一次tool_choice=None,第二次tool_choice="required"
        assert len(agent._invoke_tool_choices) == 2
        assert agent._invoke_tool_choices[0] is None
        assert agent._invoke_tool_choices[1] == "required"
        # 验证重试后调用了write_file工具
        tool_events = [e for e in events if isinstance(e, ToolEvent)]
        assert any(e.function_name == "write_file" for e in tool_events)

    @pytest.mark.asyncio
    async def test_data_check_step_retry_forces_tool_choice(self):
        """数据检查步骤(如'检查工作区现有数据文件')首次无工具调用时,重试应强制tool_choice='required'

        验证场景:用户反馈'检查工作区现有数据文件'步骤未进行工具调用。
        修复后:所有步骤(含数据检查)重试时统一强制tool_choice="required"。
        """
        from app.domain.models.event import MessageEvent, ToolEvent, ToolEventStatus
        step = _make_step(
            "1",
            description="检查工作区现有数据文件,确认是否已有相关出入库和库存数据文件",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="检查数据文件", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            # 第一次: 无工具调用,直接返回JSON结果
            [MessageEvent(role="assistant", message='{"success": true, "result": "无数据文件", "attachments": []}')],
            # 第二次(强制tool_choice="required"): 调用shell_execute检查文件
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="shell_execute",
                    function_args={"command": "ls -la /home/ubuntu/data/"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="shell_execute",
                    function_args={"command": "ls -la /home/ubuntu/data/"},
                    status=ToolEventStatus.CALLED,
                ),
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "已检查工作区", "attachments": []}',
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 步骤最终完成
        assert step.status == ExecutionStatus.COMPLETED
        # 第一次tool_choice=None,第二次tool_choice="required"
        assert len(agent._invoke_tool_choices) == 2
        assert agent._invoke_tool_choices[0] is None
        assert agent._invoke_tool_choices[1] == "required"
        # 验证重试后调用了shell_execute工具
        tool_events = [e for e in events if isinstance(e, ToolEvent)]
        assert any(e.function_name == "shell_execute" for e in tool_events)

    @pytest.mark.asyncio
    async def test_multimodal_step_normal_execution_no_force(self):
        """多模态步骤首次即调用工具时,不应强制tool_choice"""
        from app.domain.models.event import MessageEvent, ToolEvent, ToolEventStatus
        step = _make_step(
            "1",
            description="使用图片理解工具分析图片",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="分析图片", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            # 第一次invoke即有工具调用(MCP直接调用模式),无需重试
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="mcp",
                    function_name="mcp_mcp-multimodal_vl_image_understand",
                    function_args={"image_source": "upload://test.jpg"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="mcp",
                    function_name="mcp_mcp-multimodal_vl_image_understand",
                    function_args={"image_source": "upload://test.jpg"},
                    status=ToolEventStatus.CALLED,
                ),
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "完成", "attachments": []}',
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 步骤完成
        assert step.status == ExecutionStatus.COMPLETED
        # 只调用了一次invoke,tool_choice应为None(首次调用不强制)
        assert len(agent._invoke_tool_choices) == 1
        assert agent._invoke_tool_choices[0] is None

    @pytest.mark.asyncio
    async def test_ocr_step_retry_forces_tool_choice_required(self):
        """OCR步骤首次无工具调用时,重试应强制tool_choice='required'

        工具调用幻觉防护增强: OCR为动作类步骤(含'识别'关键词),
        重试后仍无工具调用时标记 FAILED(拒绝 LLM 幻觉结果),
        不再接受无工具调用的 COMPLETED。
        """
        from app.domain.models.event import MessageEvent, ErrorEvent
        step = _make_step(
            "1",
            description="对文档执行OCR文字识别",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="OCR识别", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            # 第一次: 无工具调用
            [MessageEvent(role="assistant", message='{"success": false, "result": "", "attachments": []}')],
            # 第二次: 仍无工具调用(触发幻觉防护门禁 → FAILED)
            [MessageEvent(role="assistant", message='{"success": false, "result": "无法完成", "attachments": []}')],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 动作类步骤重试后仍无工具调用 → 标记 FAILED(幻觉防护)
        assert step.status == ExecutionStatus.FAILED
        assert step.success is False
        # 第一次tool_choice=None,第二次tool_choice="required"(所有步骤统一强制)
        assert len(agent._invoke_tool_choices) == 2
        assert agent._invoke_tool_choices[0] is None
        assert agent._invoke_tool_choices[1] == "required"
        # 应产出 FAILED 的 StepEvent 和 ErrorEvent
        assert any(
            isinstance(e, StepEvent) and e.status == StepEventStatus.FAILED
            for e in events
        )
        assert any(isinstance(e, ErrorEvent) for e in events)


class TestToolCallHallucinationGuard:
    """工具调用幻觉防护门禁测试

    验证动作类步骤重试后仍无工具调用时,拒绝 LLM 幻觉结果并标记 FAILED。
    根因: 会话 f7ef16db 中,LLM 在 json_object 模式下未产出 tool_calls,
    直接在 content 返回 {"success": true, "attachments": [...]} 幻觉 JSON,
    导致文件未实际创建却标记完成。
    """

    @pytest.mark.asyncio
    async def test_action_step_no_tool_rejected_as_failed(self):
        """动作类步骤(生成文档)重试后仍无工具调用 → 标记 FAILED"""
        from app.domain.models.event import MessageEvent, ErrorEvent
        step = _make_step(
            "1",
            description="生成Word文档介绍自己",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="生成自我介绍文档", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            # 第一次: 无工具调用,返回幻觉JSON(声明成功+附件)
            [MessageEvent(
                role="assistant",
                message='{"success": true, "result": "文档已生成", "attachments": ["/home/ubuntu/intro.docx"]}',
            )],
            # 第二次: 仍无工具调用(触发幻觉防护门禁)
            [MessageEvent(
                role="assistant",
                message='{"success": true, "result": "文档已生成", "attachments": ["/home/ubuntu/intro.docx"]}',
            )],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 动作类步骤无工具调用 → FAILED(拒绝幻觉结果)
        assert step.status == ExecutionStatus.FAILED
        assert step.success is False
        assert step.error is not None
        assert "幻觉" in step.error
        # 不应接受 LLM 声明的 attachments
        assert step.attachments == []
        # 应产出 FAILED 的 StepEvent
        assert any(
            isinstance(e, StepEvent) and e.status == StepEventStatus.FAILED
            for e in events
        )
        # 应产出 ErrorEvent
        assert any(isinstance(e, ErrorEvent) for e in events)

    @pytest.mark.asyncio
    async def test_cognitive_step_no_tool_allowed_completed(self):
        """认知类步骤(纯文本回复)无工具调用 → 允许 COMPLETED(不破坏现有能力)"""
        from app.domain.models.event import MessageEvent
        step = _make_step(
            "1",
            description="纯文本回复",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="自我介绍", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            # 第一次: 无工具调用
            [MessageEvent(role="assistant", message='{"success": true, "result": "我是AI", "attachments": []}')],
            # 第二次: 仍无工具调用(认知类步骤允许)
            [MessageEvent(role="assistant", message='{"success": true, "result": "我是AI助手", "attachments": []}')],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 认知类步骤无工具调用 → 允许 COMPLETED
        assert step.status == ExecutionStatus.COMPLETED
        assert step.result == "我是AI助手"

    @pytest.mark.asyncio
    async def test_action_step_with_tool_call_completes_normally(self):
        """动作类步骤有工具调用 → 正常 COMPLETED(门禁不影响正常流程)"""
        from app.domain.models.event import MessageEvent, ToolEvent, ToolEventStatus
        step = _make_step(
            "1",
            description="生成Word文档介绍自己",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="生成自我介绍文档", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="shell_execute",
                    function_args={"command": "python generate_doc.py"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="shell_execute",
                    function_args={"command": "python generate_doc.py"},
                    status=ToolEventStatus.CALLED,
                ),
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "文档已生成", "attachments": ["/home/ubuntu/intro.docx"]}',
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 有工具调用 → 正常 COMPLETED
        assert step.status == ExecutionStatus.COMPLETED
        assert step.success is True
        assert "/home/ubuntu/intro.docx" in step.attachments

    @pytest.mark.asyncio
    async def test_action_step_retry_then_tool_call_completes(self):
        """动作类步骤首次无工具→重试→有工具调用 → 正常 COMPLETED"""
        from app.domain.models.event import MessageEvent, ToolEvent, ToolEventStatus
        step = _make_step(
            "1",
            description="导出数据到Excel文件",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="导出数据", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            # 第一次: 无工具调用
            [MessageEvent(role="assistant", message='{"success": true, "result": "完成", "attachments": []}')],
            # 第二次: 有工具调用(重试成功)
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="shell_execute",
                    function_args={"command": "python export.py"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="shell_execute",
                    function_args={"command": "python export.py"},
                    status=ToolEventStatus.CALLED,
                ),
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "导出完成", "attachments": ["/home/ubuntu/data.xlsx"]}',
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 重试后有工具调用 → 正常 COMPLETED
        assert step.status == ExecutionStatus.COMPLETED
        assert step.success is True

    @pytest.mark.asyncio
    async def test_json_parse_degradation_with_tool_call(self):
        """有工具调用 + JSON解析降级 → success=True(降级不误伤已执行步骤)"""
        from app.domain.models.event import MessageEvent, ToolEvent, ToolEventStatus
        step = _make_step(
            "1",
            description="生成数据分析报告",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="生成报告", attachments=[])

        # 使用抛异常的 parser,触发 Step.model_validate 降级路径
        class _ThrowingJsonParser:
            async def invoke(self, text: str):
                raise ValueError("模拟JSON解析失败")

        from app.domain.models.app_config import AgentConfig
        agent = object.__new__(ReActAgent)
        agent._json_parser = _ThrowingJsonParser()
        agent._agent_config = AgentConfig()
        call_idx = [0]
        agent._invoke_tool_choices = []

        async def mock_invoke(query: str, format=None, tool_choice=None):
            agent._invoke_tool_choices.append(tool_choice)
            idx = call_idx[0]
            call_idx[0] += 1
            if idx == 0:
                yield ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="shell_execute",
                    function_args={"command": "echo hi"},
                    status=ToolEventStatus.CALLING,
                )
                yield ToolEvent(
                    tool_call_id="t1", tool_name="shell",
                    function_name="shell_execute",
                    function_args={"command": "echo hi"},
                    status=ToolEventStatus.CALLED,
                )
                # 非JSON内容,触发降级(parser抛异常)
                yield MessageEvent(role="assistant", message="这不是JSON格式")

        agent.invoke = mock_invoke

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 有工具调用 + JSON解析降级 → 应完成(success由降级逻辑判断)
        assert step.status == ExecutionStatus.COMPLETED
        # 有工具调用 → success=True(降级不误伤已执行步骤)
        assert step.success is True

    @pytest.mark.asyncio
    async def test_hallucination_guard_with_english_action_keyword(self):
        """英文动作类步骤(generate)重试后仍无工具调用 → 标记 FAILED"""
        from app.domain.models.event import MessageEvent
        step = _make_step(
            "1",
            description="generate a report about AI trends",
            status=ExecutionStatus.PENDING,
        )
        plan = _make_plan([step])
        message = Message(message="generate report", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            [MessageEvent(role="assistant", message='{"success": true, "result": "done", "attachments": []}')],
            [MessageEvent(role="assistant", message='{"success": true, "result": "done", "attachments": []}')],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 英文动作类步骤无工具调用 → FAILED
        assert step.status == ExecutionStatus.FAILED
        assert step.success is False


class TestProgressDeduplication:
    """进度事件去重测试

    验证 progress 触顶后不再产出冗余 StepEvent,减少 SSE 推送和 DB 持久化负担。
    根因: 会话 eef7dbcd 中,步骤1有130次工具调用,progress 在第11次达到90%后,
    后续119次工具调用都产出 progress=90 的冗余事件(共约100个),污染 SSE 流。
    去重后仅产出进度变化事件,大幅减少冗余。
    """

    @staticmethod
    def _build_tool_call_events(count: int) -> list:
        """构造指定数量的工具调用事件对(CALLING + CALLED)"""
        from app.domain.models.event import ToolEvent, ToolEventStatus
        events = []
        for i in range(count):
            call_id = f"t{i}"
            events.append(ToolEvent(
                tool_call_id=call_id, tool_name="shell",
                function_name="shell_execute",
                function_args={"command": f"echo {i}"},
                status=ToolEventStatus.CALLING,
            ))
            events.append(ToolEvent(
                tool_call_id=call_id, tool_name="shell",
                function_name="shell_execute",
                function_args={"command": f"echo {i}"},
                status=ToolEventStatus.CALLED,
            ))
        return events

    @pytest.mark.asyncio
    async def test_progress_dedup_when_capped_at_90(self):
        """进度触顶90%后,后续工具调用不再产出冗余 StepEvent"""
        from app.domain.models.event import MessageEvent
        # 15次工具调用: 第11次progress=90%, 第12-15次也=90%(去重后不产出)
        tool_events = self._build_tool_call_events(15)
        tool_events.append(MessageEvent(
            role="assistant",
            message='{"success": true, "result": "完成", "attachments": []}',
        ))

        step = _make_step("1", description="执行任务", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="测试", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([tool_events])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 统计进度推进 StepEvent(STARTED 状态且 progress>0,排除初始 progress=0)
        progress_events = [
            e for e in events
            if isinstance(e, StepEvent)
            and e.status == StepEventStatus.STARTED
            and e.progress > 0
        ]

        # 15次工具调用的 progress 序列(无去重): 25,45,60,70,78,80,82,84,86,88,90,90,90,90,90
        # 去重后: 25,45,60,70,78,80,82,84,86,88,90 (11个)
        assert len(progress_events) == 11
        progress_values = [e.progress for e in progress_events]
        assert progress_values == [25, 45, 60, 70, 78, 80, 82, 84, 86, 88, 90]
        # 无重复 progress 值
        assert len(progress_values) == len(set(progress_values))

    @pytest.mark.asyncio
    async def test_long_step_progress_events_far_less_than_tool_calls(self):
        """长步骤(30次工具调用)的进度 StepEvent 数应远少于工具调用数"""
        from app.domain.models.event import MessageEvent
        tool_events = self._build_tool_call_events(30)
        tool_events.append(MessageEvent(
            role="assistant",
            message='{"success": true, "result": "完成", "attachments": []}',
        ))

        step = _make_step("1", description="执行任务", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="测试", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([tool_events])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        progress_events = [
            e for e in events
            if isinstance(e, StepEvent)
            and e.status == StepEventStatus.STARTED
            and e.progress > 0
        ]
        # 30次工具调用,去重后只有11个不同 progress 值(25~90)
        assert len(progress_events) == 11
        # 远少于工具调用数(30)
        assert len(progress_events) < 30

    @pytest.mark.asyncio
    async def test_short_step_progress_not_affected(self):
        """短步骤(3次工具调用)的进度事件不受去重影响(每次 progress 都不同)"""
        from app.domain.models.event import MessageEvent
        tool_events = self._build_tool_call_events(3)
        tool_events.append(MessageEvent(
            role="assistant",
            message='{"success": true, "result": "完成", "attachments": []}',
        ))

        step = _make_step("1", description="执行任务", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="测试", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([tool_events])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        progress_events = [
            e for e in events
            if isinstance(e, StepEvent)
            and e.status == StepEventStatus.STARTED
            and e.progress > 0
        ]
        # 3次工具调用,progress 分别为 25,45,60(都不同,去重不影响)
        assert len(progress_events) == 3
        assert [e.progress for e in progress_events] == [25, 45, 60]


class TestIsLikelyQuestion:
    """_is_likely_question 提问语义检测测试 (P0-8: 防御LLM误用message_ask_user)

    根因会话93b442bd: LLM完成天气查询后,误用message_ask_user发送声明性文本
    "已完成图片识别与天气查询",触发WaitEvent导致会话永久卡在WAITING状态。
    _is_likely_question通过提问标志词和问号检测,区分提问与声明性文本。
    """

    # === 提问文本(应返回True) ===

    def test_cn_question_mark(self):
        """中文问号判定"""
        assert _is_likely_question("您希望使用哪种图表类型？") is True

    def test_en_question_mark(self):
        """英文问号判定"""
        assert _is_likely_question("Which format do you prefer?") is True

    def test_cn_question_particle_ma(self):
        """中文疑问语气词[吗]"""
        assert _is_likely_question("您需要生成报告吗") is True

    def test_cn_question_particle_ne(self):
        """中文疑问语气词[呢]"""
        assert _is_likely_question("这个数据怎么处理呢") is True

    def test_cn_question_word_what(self):
        """中文疑问代词[什么]"""
        assert _is_likely_question("您需要什么格式的文件") is True

    def test_cn_question_word_how(self):
        """中文疑问代词[怎么]"""
        assert _is_likely_question("请问怎么导出数据") is True

    def test_cn_request_input(self):
        """中文请求输入指令[请提供]"""
        assert _is_likely_question("请提供您的账号信息") is True

    def test_cn_request_confirm(self):
        """中文请求确认[请确认]"""
        assert _is_likely_question("请确认以下信息是否正确") is True

    def test_cn_yes_no_question(self):
        """中文正反问句[是不是]"""
        assert _is_likely_question("这个数据是不是需要导出") is True

    def test_en_question_word(self):
        """英文疑问词what"""
        assert _is_likely_question("What is the target format") is True

    def test_en_could_you(self):
        """英文礼貌提问could you"""
        assert _is_likely_question("Could you provide the date range") is True

    def test_en_please_enter(self):
        """英文请求输入please enter"""
        assert _is_likely_question("Please enter your credentials") is True

    # === 声明性文本(应返回False) ===

    def test_declarative_completion(self):
        """声明性完成通知(会话93b442bd根因文本)"""
        assert _is_likely_question("已完成图片识别与天气查询") is False

    def test_declarative_progress(self):
        """声明性进度通知"""
        assert _is_likely_question("正在生成分析报告") is False

    def test_declarative_simple_statement(self):
        """简单声明句"""
        assert _is_likely_question("数据清洗已完成") is False

    def test_declarative_task_summary(self):
        """任务摘要声明"""
        assert _is_likely_question("共处理100条记录,生成2个文件") is False

    def test_empty_string(self):
        """空字符串"""
        assert _is_likely_question("") is False

    def test_none_input(self):
        """None输入"""
        assert _is_likely_question(None) is False

    def test_whitespace_only(self):
        """纯空白文本"""
        assert _is_likely_question("   \n  ") is False

    def test_en_declarative(self):
        """英文声明句"""
        assert _is_likely_question("Task completed successfully") is False

    def test_en_whatever_not_matched(self):
        """英文"whatever"不应被"what"误匹配(词边界检查)"""
        assert _is_likely_question("Whatever you decide is fine") is False

    def test_en_thank_you_not_matched(self):
        """英文"thank you"不是提问"""
        assert _is_likely_question("Thank you for your patience") is False


class TestMessageAskUserDeclarativeTextGuard:
    """message_ask_user 声明性文本防御测试 (P0-8: 会话93b442bd根因)

    验证LLM误用message_ask_user发送声明性通知时,不触发WaitEvent导致会话卡死。
    根因: LLM完成天气查询后,调用message_ask_user发送"已完成图片识别与天气查询",
    该文本非空(绕过P0-7空文本检查)但非提问,触发WaitEvent导致会话永久卡在WAITING。
    修复: _is_likely_question语义检测,非提问文本跳过WaitEvent。
    """

    @pytest.mark.asyncio
    async def test_declarative_text_skips_wait_event(self):
        """声明性文本"已完成图片识别与天气查询"不应触发WaitEvent(会话93b442bd根因)"""
        from app.domain.models.event import ToolEvent, ToolEventStatus, MessageEvent
        step = _make_step("1", description="识别图片并查询天气", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="这今天天气怎么样", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            [
                # LLM误用message_ask_user发送声明性完成通知
                ToolEvent(
                    tool_call_id="t1", tool_name="message",
                    function_name="message_ask_user",
                    function_args={"text": "已完成图片识别与天气查询"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="message",
                    function_name="message_ask_user",
                    function_args={"text": "已完成图片识别与天气查询"},
                    status=ToolEventStatus.CALLED,
                ),
                # LLM收到工具结果后重新生成,返回正常步骤结果
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "广州今日中雨,25-31度", "attachments": []}',
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 核心断言1: 不应产生WaitEvent(声明性文本不触发等待)
        assert not any(isinstance(e, WaitEvent) for e in events), \
            "声明性文本'已完成图片识别与天气查询'不应触发WaitEvent,否则会话会永久卡死"
        # 核心断言2: 步骤应正常完成(非RUNNING/WAITING)
        assert step.status == ExecutionStatus.COMPLETED
        assert step.success is True
        # 核心断言3: CALLING阶段的MessageEvent仍然产出(声明性文本展示给用户无害)
        msg_events = [
            e for e in events
            if isinstance(e, MessageEvent)
            and not getattr(e, "is_thinking", False)
        ]
        assert any("已完成图片识别与天气查询" in e.message for e in msg_events), \
            "CALLING阶段的声明性文本应正常展示给用户"

    @pytest.mark.asyncio
    async def test_question_text_still_triggers_wait_event(self):
        """真正的提问文本仍应正常触发WaitEvent(回归测试)"""
        from app.domain.models.event import ToolEvent, ToolEventStatus
        step = _make_step("1", description="询问用户偏好", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="生成报告", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="message",
                    function_name="message_ask_user",
                    function_args={"text": "您希望生成哪种格式的报告？"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="message",
                    function_name="message_ask_user",
                    function_args={"text": "您希望生成哪种格式的报告？"},
                    status=ToolEventStatus.CALLED,
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        # 提问文本应正常触发WaitEvent
        assert any(isinstance(e, WaitEvent) for e in events)
        # 步骤不应完成(ask_user提前return)
        assert step.status == ExecutionStatus.RUNNING

    @pytest.mark.asyncio
    async def test_declarative_progress_text_skips_wait_event(self):
        """进度通知"正在生成报告"不应触发WaitEvent"""
        from app.domain.models.event import ToolEvent, ToolEventStatus, MessageEvent
        step = _make_step("1", description="生成报告", status=ExecutionStatus.PENDING)
        plan = _make_plan([step])
        message = Message(message="生成报告", attachments=[])

        agent = TestExecuteStepToolCallGuard._make_agent([
            [
                ToolEvent(
                    tool_call_id="t1", tool_name="message",
                    function_name="message_ask_user",
                    function_args={"text": "正在生成报告"},
                    status=ToolEventStatus.CALLING,
                ),
                ToolEvent(
                    tool_call_id="t1", tool_name="message",
                    function_name="message_ask_user",
                    function_args={"text": "正在生成报告"},
                    status=ToolEventStatus.CALLED,
                ),
                MessageEvent(
                    role="assistant",
                    message='{"success": true, "result": "报告已生成", "attachments": []}',
                ),
            ],
        ])

        events = []
        async for ev in agent.execute_step(plan, step, message):
            events.append(ev)

        assert not any(isinstance(e, WaitEvent) for e in events)
        assert step.status == ExecutionStatus.COMPLETED
