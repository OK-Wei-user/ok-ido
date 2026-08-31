#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Batch 34: 前序步骤上下文构建器 DRY 重构单元测试

验证 _step_context_builder 共享模块的正确性,确保:
- execution/planning 两种上下文类型输出正确的警告文本
- 空计划/无已完成步骤返回空串
- Step 对象与 step_id 字符串等价(同一 plan 产出相同结果)
"""
import pytest

from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.services.agents._step_context_builder import build_prior_steps_context


def _make_plan_with_completed(current_step_id: str = "step-current") -> Plan:
    """构造含2个已完成步骤+1个当前步骤的测试计划"""
    steps = [
        Step(id="step-1", description="导出数据", status=ExecutionStatus.COMPLETED, result="已导出data.xlsx"),
        Step(id="step-2", description="分析数据", status=ExecutionStatus.COMPLETED, result="分析完成,发现趋势上升"),
        Step(id=current_step_id, description="生成报告", status=ExecutionStatus.PENDING, result=None),
    ]
    return Plan(steps=steps)


class TestBuildContextType:
    """上下文类型区分测试"""

    def test_execution_type_contains_execution_warning(self):
        """execution 类型输出含'严禁重复执行已完成操作'"""
        plan = _make_plan_with_completed()
        result = build_prior_steps_context(plan, "step-current", context_type="execution")
        assert "严禁重复执行已完成操作" in result
        assert "可直接复用,不得重复执行" in result

    def test_planning_type_contains_planning_warning(self):
        """planning 类型输出含'严禁重建或重复规划已完成步骤'"""
        plan = _make_plan_with_completed()
        result = build_prior_steps_context(plan, "step-current", context_type="planning")
        assert "严禁重建或重复规划已完成步骤" in result
        assert "不得在更新后的计划中重建或重复" in result

    def test_both_types_include_completed_steps(self):
        """两种类型都包含已完成步骤的摘要"""
        plan = _make_plan_with_completed()
        for ctx_type in ("execution", "planning"):
            result = build_prior_steps_context(plan, "step-current", context_type=ctx_type)
            assert "step-1" in result
            assert "step-2" in result
            assert "已导出data.xlsx" in result


class TestEmptyCases:
    """空值/边界情况测试"""

    def test_empty_plan_returns_empty_string(self):
        """空计划返回空串"""
        result = build_prior_steps_context(None, "step-1")
        assert result == ""

    def test_plan_with_no_steps_returns_empty(self):
        """无步骤的计划返回空串"""
        plan = Plan(steps=[])
        result = build_prior_steps_context(plan, "step-1")
        assert result == ""

    def test_no_completed_steps_returns_empty(self):
        """无已完成步骤返回空串"""
        steps = [
            Step(id="step-1", description="任务A", status=ExecutionStatus.PENDING, result=None),
            Step(id="step-2", description="任务B", status=ExecutionStatus.RUNNING, result=None),
        ]
        plan = Plan(steps=steps)
        result = build_prior_steps_context(plan, "step-1")
        assert result == ""


class TestStepObjectVsStringEquivalence:
    """Step 对象与 step_id 字符串等价性测试"""

    def test_step_object_and_string_produce_same_result(self):
        """同一 plan 下,Step 对象与 step_id 字符串产出相同结果"""
        plan = _make_plan_with_completed(current_step_id="step-current")
        current_step = next(s for s in plan.steps if s.id == "step-current")

        result_from_step = build_prior_steps_context(plan, current_step, context_type="execution")
        result_from_str = build_prior_steps_context(plan, "step-current", context_type="execution")

        assert result_from_step == result_from_str
        # 当前步骤自身不应出现在摘要中
        assert "step-current" not in result_from_step

    def test_default_context_type_is_execution(self):
        """默认 context_type 为 execution"""
        plan = _make_plan_with_completed()
        result_default = build_prior_steps_context(plan, "step-current")
        result_explicit = build_prior_steps_context(plan, "step-current", context_type="execution")
        assert result_default == result_explicit
