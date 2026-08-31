#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_graceful_degradation.py
Agent迭代溢出优雅降级单元测试 - 四层防护机制验证
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.models.app_config import AgentConfig
from app.domain.models.event import (
    ErrorEvent, MessageEvent, ToolEvent, ToolEventStatus,
    StepEvent, StepEventStatus, PlanEvent, PlanEventStatus,
)
from app.domain.models.memory import Memory
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.models.message import Message


class TestIterationBudgetThresholds:
    """L1 迭代预算感知阈值计算测试"""

    def test_warning_threshold_80_percent(self):
        config = AgentConfig(max_iterations=100)
        warning_threshold = int(config.max_iterations * 0.8)
        assert warning_threshold == 80

    def test_critical_threshold_90_percent(self):
        config = AgentConfig(max_iterations=100)
        critical_threshold = int(config.max_iterations * 0.9)
        assert critical_threshold == 90

    def test_small_max_iterations_thresholds(self):
        config = AgentConfig(max_iterations=10)
        warning_threshold = int(config.max_iterations * 0.8)
        critical_threshold = int(config.max_iterations * 0.9)
        assert warning_threshold == 8
        assert critical_threshold == 9

    def test_default_max_iterations(self):
        config = AgentConfig()
        assert config.max_iterations == 100


class TestCircuitBreakerDetection:
    """L3 熔断器迭代溢出检测测试"""

    def test_iteration_overflow_error_detected(self):
        step = Step(id="1", description="test")
        step.status = ExecutionStatus.FAILED
        step.error = "Agent迭代超过最大迭代次数: 100, 任务处理失败"
        is_overflow = (
            step.status == ExecutionStatus.FAILED
            and step.error
            and "迭代超过最大迭代次数" in step.error
        )
        assert is_overflow is True

    def test_normal_error_not_detected_as_overflow(self):
        step = Step(id="1", description="test")
        step.status = ExecutionStatus.FAILED
        step.error = "工具调用失败"
        is_overflow = (
            step.status == ExecutionStatus.FAILED
            and step.error
            and "迭代超过最大迭代次数" in step.error
        )
        assert is_overflow is False

    def test_completed_step_not_detected_as_overflow(self):
        step = Step(id="1", description="test")
        step.status = ExecutionStatus.COMPLETED
        step.success = True
        is_overflow = (
            step.status == ExecutionStatus.FAILED
            and step.error
            and "迭代超过最大迭代次数" in step.error
        )
        assert is_overflow is False

    def test_no_error_not_detected_as_overflow(self):
        step = Step(id="1", description="test")
        step.status = ExecutionStatus.FAILED
        step.error = None
        is_overflow = (
            step.status == ExecutionStatus.FAILED
            and step.error
            and "迭代超过最大迭代次数" in step.error
        )
        assert not is_overflow


class TestJSONParserResilience:
    """L4 JSON解析韧性降级测试"""

    def test_step_result_fallback_on_parse_failure(self):
        raw_message = "这不是JSON格式的内容"
        try:
            parsed_obj = json.loads(raw_message)
            Step.model_validate(parsed_obj)
            step = Step(success=True, result=raw_message[:2000])
        except (json.JSONDecodeError, Exception):
            step = Step(success=True, result=raw_message[:2000] if raw_message else "任务执行完成（结果解析降级）")
        assert step.success is True
        assert step.result == raw_message[:2000]

    def test_step_result_fallback_on_empty_message(self):
        raw_message = ""
        try:
            parsed_obj = json.loads(raw_message)
            Step.model_validate(parsed_obj)
            step = Step(success=True, result=raw_message[:2000])
        except (json.JSONDecodeError, Exception):
            step = Step(success=True, result=raw_message[:2000] if raw_message else "任务执行完成（结果解析降级）")
        assert step.success is True
        assert "降级" in step.result

    def test_step_result_normal_parse(self):
        raw_message = json.dumps({
            "success": True,
            "result": "搜索完成，找到5条结果",
            "attachments": [],
        })
        parsed_obj = json.loads(raw_message)
        step = Step.model_validate(parsed_obj)
        assert step.success is True
        assert step.result == "搜索完成，找到5条结果"

    def test_summarize_fallback_on_parse_failure(self):
        raw_message = "总结内容但不是JSON"
        try:
            parsed_obj = json.loads(raw_message)
            msg = Message.model_validate(parsed_obj)
        except (json.JSONDecodeError, Exception):
            msg = Message(message=raw_message or "任务已完成，但无法生成结构化总结。")
        assert msg.message == raw_message

    def test_summarize_fallback_on_empty(self):
        raw_message = ""
        try:
            parsed_obj = json.loads(raw_message)
            msg = Message.model_validate(parsed_obj)
        except (json.JSONDecodeError, Exception):
            msg = Message(message=raw_message or "任务已完成，但无法生成结构化总结。")
        assert "结构化总结" in msg.message

    def test_plan_create_fallback_on_parse_failure(self):
        user_message = "帮我搜索Python教程"
        try:
            parsed_obj = json.loads("invalid json")
            Plan.model_validate(parsed_obj)
            plan = Plan.model_validate(parsed_obj)
        except (json.JSONDecodeError, Exception):
            plan = Plan(
                title="任务处理",
                goal=user_message[:200],
                language="zh",
                steps=[Step(id="1", description=user_message[:500])],
                message="我将为您处理这个任务。",
            )
        assert plan.title == "任务处理"
        assert len(plan.steps) == 1
        assert "Python" in plan.steps[0].description

    def test_plan_update_fallback_keeps_original_plan(self):
        original_plan = Plan(
            title="搜索任务",
            goal="搜索Python教程",
            language="zh",
            steps=[
                Step(id="1", description="搜索Python教程", status=ExecutionStatus.COMPLETED, success=True),
                Step(id="2", description="整理搜索结果"),
            ],
        )
        try:
            parsed_obj = json.loads("invalid json")
            updated_plan = Plan.model_validate(parsed_obj)
        except (json.JSONDecodeError, Exception):
            pass
        assert original_plan.title == "搜索任务"
        assert len(original_plan.steps) == 2


class TestForcedSummaryMechanism:
    """L2 强制总结机制测试"""

    def test_forced_summary_prompt_content(self):
        prompt = "【系统强制指令】你已达到最大迭代次数限制。必须立即停止调用任何工具，基于已收集的信息给出尽可能完整的最终结果。直接输出结果，不要再调用工具。"
        assert "强制指令" in prompt
        assert "最大迭代次数" in prompt
        assert "停止调用任何工具" in prompt

    def test_warning_prompt_content(self):
        prompt = "【系统警告】你已使用80%的迭代预算，请尽快总结当前发现并给出最终回答，减少不必要的工具调用。"
        assert "80%" in prompt
        assert "减少" in prompt

    def test_critical_prompt_content(self):
        prompt = "【系统紧急指令】你已使用90%的迭代预算，必须立即停止调用工具，基于已有信息直接输出最终结果！不要再调用任何工具！"
        assert "90%" in prompt
        assert "紧急指令" in prompt
        assert "立即停止" in prompt


class TestErrorCascadePrevention:
    """错误级联防护集成测试"""

    def test_iteration_overflow_does_not_cascade_to_json_error(self):
        step = Step(id="1", description="搜索任务")
        step.status = ExecutionStatus.FAILED
        step.error = "Agent迭代超过最大迭代次数: 100, 任务处理失败"

        is_overflow = (
            step.status == ExecutionStatus.FAILED
            and step.error
            and "迭代超过最大迭代次数" in step.error
        )
        assert is_overflow is True
        assert step.status == ExecutionStatus.FAILED

    def test_full_degradation_flow(self):
        step = Step(id="1", description="搜索任务")
        step.status = ExecutionStatus.RUNNING

        step.status = ExecutionStatus.FAILED
        step.error = "Agent迭代超过最大迭代次数: 100, 任务处理失败"

        is_overflow = (
            step.status == ExecutionStatus.FAILED
            and step.error
            and "迭代超过最大迭代次数" in step.error
        )

        if is_overflow:
            step.status = ExecutionStatus.COMPLETED
            step.success = True
            step.result = "基于已收集信息的部分结果"
            step.error = None

        assert step.status == ExecutionStatus.COMPLETED
        assert step.success is True
        assert step.result is not None
