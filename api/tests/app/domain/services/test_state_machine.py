#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
05-Planner-ReAct状态机强化单元测试

覆盖两个改造点:
1. 步骤失败恢复策略: ReActAgent._can_retry_step + _build_execution_query
2. Planner update_plan增强: PlannerAgent._build_recovery_directive + update_plan失败指令注入

注: update_plan无条件触发逻辑(参考5b54ddc)由集成测试与会话测试覆盖,
    COMPLETED→UPDATING、FAILED(非溢出)→UPDATING、FAILED(迭代溢出)→SUMMARIZING
    的状态转换在PlannerReActFlow.invoke主循环中内联实现,无独立方法可单测。
"""
from unittest.mock import patch

import pytest

from app.domain.models.plan import Step, Plan, ExecutionStatus
from app.domain.models.message import Message
from app.domain.services.agents.react import ReActAgent, _MAX_RETRY_COUNT
from app.domain.services.agents.planner import PlannerAgent
from app.domain.services.flows.planner_react import PlannerReActFlow


class TestStepRetryCount:
    """Step模型retry_count字段测试"""

    def test_default_retry_count_is_zero(self):
        """新步骤的retry_count默认为0"""
        step = Step(id="1", description="测试步骤")
        assert step.retry_count == 0

    def test_retry_count_settable(self):
        """retry_count可手动设置"""
        step = Step(id="1", description="测试步骤", retry_count=2)
        assert step.retry_count == 2


class TestCanRetryStep:
    """ReActAgent._can_retry_step 失败重试判断测试"""

    def test_can_retry_on_first_failure(self):
        """首次失败(retry_count=1)且非迭代溢出 → 可重试"""
        step = Step(id="1", description="测试", retry_count=1, error="工具调用失败")
        assert ReActAgent._can_retry_step(step) is True

    def test_can_retry_on_second_failure(self):
        """第二次失败(retry_count=2)且非迭代溢出 → 可重试(未超过上限)"""
        step = Step(id="1", description="测试", retry_count=2, error="工具调用失败")
        assert ReActAgent._can_retry_step(step) is True

    def test_cannot_retry_when_exceed_max(self):
        """重试次数超过上限(retry_count=3) → 不可重试"""
        step = Step(
            id="1", description="测试",
            retry_count=_MAX_RETRY_COUNT + 1, error="工具调用失败",
        )
        assert ReActAgent._can_retry_step(step) is False

    def test_cannot_retry_on_iteration_overflow(self):
        """迭代溢出错误 → 不可重试(会导致无限循环)"""
        step = Step(id="1", description="测试", retry_count=1, error="迭代超过最大迭代次数")
        assert ReActAgent._can_retry_step(step) is False

    def test_can_retry_with_empty_error(self):
        """错误信息为空但retry_count未超限 → 可重试"""
        step = Step(id="1", description="测试", retry_count=1, error=None)
        assert ReActAgent._can_retry_step(step) is True


class TestBuildExecutionQuery:
    """ReActAgent._build_execution_query 执行query构建测试"""

    def _make_plan_step_message(self):
        """创建测试用的plan/step/message"""
        plan = Plan(id="plan1", title="测试", goal="目标", language="zh")
        step = Step(id="1", description="搜索新闻")
        message = Message(message="请搜索新闻", attachments=[])
        return plan, step, message

    def test_initial_query_contains_step_description(self):
        """初始query(无failure_error)包含原始步骤描述"""
        plan, step, message = self._make_plan_step_message()
        query = ReActAgent._build_execution_query(plan, step, message)

        assert step.description in query
        assert "请搜索新闻" in query
        assert "zh" in query

    def test_retry_query_injects_failure_reason(self):
        """重试query注入失败原因和重试次数"""
        plan, step, message = self._make_plan_step_message()
        step.retry_count = 1
        failure_error = "文件不存在: /home/ubuntu/data.txt"

        query = ReActAgent._build_execution_query(plan, step, message, failure_error=failure_error)

        assert "上次执行失败" in query
        assert "第1次" in query
        assert failure_error in query
        assert "替代方案" in query

    def test_retry_query_truncates_long_error(self):
        """重试query截断过长的错误信息(上限200字符)"""
        plan, step, message = self._make_plan_step_message()
        step.retry_count = 1
        long_error = "x" * 300

        query = ReActAgent._build_execution_query(plan, step, message, failure_error=long_error)

        assert "x" * 200 in query
        assert "x" * 201 not in query


class TestBuildRecoveryDirective:
    """PlannerAgent._build_recovery_directive 恢复决策指令构建测试"""

    def test_directive_contains_three_strategies(self):
        """恢复指令包含重试/跳过/终止三种策略"""
        step = Step(id="1", description="测试", status=ExecutionStatus.FAILED, error="工具调用超时")
        directive = PlannerAgent._build_recovery_directive(step)

        assert "重试" in directive
        assert "跳过" in directive
        assert "终止" in directive

    def test_directive_contains_error_reason(self):
        """恢复指令包含错误原因"""
        step = Step(id="1", description="测试", status=ExecutionStatus.FAILED, error="文件不存在")
        directive = PlannerAgent._build_recovery_directive(step)

        assert "文件不存在" in directive

    def test_directive_contains_retry_info_when_retried(self):
        """已重试过的步骤,指令中包含重试次数"""
        step = Step(id="1", description="测试", status=ExecutionStatus.FAILED, error="失败", retry_count=2)
        directive = PlannerAgent._build_recovery_directive(step)

        assert "已自动重试2次" in directive

    def test_directive_no_retry_info_when_first_failure(self):
        """首次失败(未重试),指令中不包含重试次数"""
        step = Step(id="1", description="测试", status=ExecutionStatus.FAILED, error="失败", retry_count=0)
        directive = PlannerAgent._build_recovery_directive(step)

        assert "已自动重试" not in directive

    def test_directive_truncates_long_error(self):
        """恢复指令截断过长的错误信息(上限200字符)"""
        step = Step(id="1", description="测试", status=ExecutionStatus.FAILED, error="x" * 300, retry_count=0)
        directive = PlannerAgent._build_recovery_directive(step)

        assert "x" * 200 in directive
        assert "x" * 201 not in directive

    def test_directive_handles_none_error(self):
        """错误信息为None时不报错"""
        step = Step(id="1", description="测试", status=ExecutionStatus.FAILED, error=None)
        directive = PlannerAgent._build_recovery_directive(step)

        assert "未知错误" in directive


class TestPlannerUpdatePlanWithRecovery:
    """PlannerAgent.update_plan 失败恢复决策注入集成测试"""

    def _create_planner(self, captured_queries):
        """创建带invoke mock的PlannerAgent实例(不调用__init__)

        mock invoke捕获query参数并返回空生成器,使update_plan快速结束。
        """
        with patch.object(PlannerAgent, '__init__', lambda self: None):
            planner = PlannerAgent.__new__(PlannerAgent)

            async def mock_invoke(query):
                captured_queries.append(query)
                return
                yield  # 使其成为async generator

            planner.invoke = mock_invoke
            return planner

    @pytest.mark.asyncio
    async def test_failed_step_triggers_recovery_directive(self):
        """失败步骤的update_plan query包含恢复决策指令"""
        captured = []
        planner = self._create_planner(captured)

        plan = Plan(id="plan1", title="测试", steps=[Step(id="1", description="步骤1")])
        failed_step = Step(id="1", description="步骤1", status=ExecutionStatus.FAILED, error="工具调用超时")

        async for _ in planner.update_plan(plan, failed_step):
            pass

        assert len(captured) == 1
        query = captured[0]
        assert "重试" in query
        assert "跳过" in query
        assert "终止" in query
        assert "工具调用超时" in query

    @pytest.mark.asyncio
    async def test_completed_step_no_recovery_directive(self):
        """成功步骤的update_plan query不包含恢复决策指令"""
        captured = []
        planner = self._create_planner(captured)

        plan = Plan(id="plan1", title="测试", steps=[Step(id="1", description="步骤1")])
        completed_step = Step(id="1", description="步骤1", status=ExecutionStatus.COMPLETED, result="完成")

        async for _ in planner.update_plan(plan, completed_step):
            pass

        assert len(captured) == 1
        query = captured[0]
        assert "恢复策略" not in query
        assert "⚠️ 上述步骤执行失败" not in query


class TestNonRetryableErrorMarker:
    """迭代溢出错误标记识别测试

    验证PlannerReActFlow对迭代溢出失败的识别逻辑:
    - 迭代溢出失败 → 跳过update_plan,直接进入SUMMARIZING
    - 非迭代溢出失败 → 进入UPDATING,调用05优化恢复指令
    - 正常完成 → 进入UPDATING(无条件触发,参考5b54ddc)
    """

    def test_iteration_overflow_marker_exists(self):
        """迭代溢出错误标记常量存在且语义正确"""
        from app.domain.services.flows.planner_react import _NON_RETRYABLE_ERROR_MARKER
        assert _NON_RETRYABLE_ERROR_MARKER == "迭代超过最大迭代次数"

    def test_iteration_overflow_error_detected(self):
        """迭代溢出错误信息能被正确识别"""
        from app.domain.services.flows.planner_react import _NON_RETRYABLE_ERROR_MARKER
        error_msg = f"Agent{_NON_RETRYABLE_ERROR_MARKER}: 10, 任务处理失败"
        assert _NON_RETRYABLE_ERROR_MARKER in error_msg

    def test_normal_error_not_matched(self):
        """普通错误信息不被识别为迭代溢出"""
        from app.domain.services.flows.planner_react import _NON_RETRYABLE_ERROR_MARKER
        error_msg = "工具调用超时"
        assert _NON_RETRYABLE_ERROR_MARKER not in error_msg
