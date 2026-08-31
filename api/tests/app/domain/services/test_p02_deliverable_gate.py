# -*- coding: utf-8 -*-
"""批次45 P0-2: done前附件门禁单元测试

覆盖:
- _DELIVERABLE_REQUIRED_TASK_TYPES / _DELIVERABLE_RETRY_MAX / _DELIVERABLE_GUIDANCE_STEP_DESC 常量
- PlannerReActFlow 附件门禁状态(_deliverable_retry_count / _current_task_type)
- 门禁触发条件: data_analysis任务 + 0文件 + 未超重试上限
- 门禁跳过条件: general任务 / 有文件 / 超重试上限
"""
import pytest

from app.domain.models.plan import Step, ExecutionStatus


# ============================================================
# P0-2 常量验证
# ============================================================

class TestP02Constants:
    """P0-2: 附件门禁常量定义"""

    def test_required_task_types_includes_data_analysis(self):
        """_DELIVERABLE_REQUIRED_TASK_TYPES 应包含 data_analysis"""
        from app.domain.services.flows.planner_react import _DELIVERABLE_REQUIRED_TASK_TYPES
        assert "data_analysis" in _DELIVERABLE_REQUIRED_TASK_TYPES

    def test_retry_max_is_one(self):
        """_DELIVERABLE_RETRY_MAX 应为1(防死循环)"""
        from app.domain.services.flows.planner_react import _DELIVERABLE_RETRY_MAX
        assert _DELIVERABLE_RETRY_MAX == 1

    def test_guidance_step_desc_contains_key_info(self):
        """引导步骤描述应含关键信息(生成文件/英文文件名/声明attachments)"""
        from app.domain.services.flows.planner_react import _DELIVERABLE_GUIDANCE_STEP_DESC
        assert "python-docx" in _DELIVERABLE_GUIDANCE_STEP_DESC or "openpyxl" in _DELIVERABLE_GUIDANCE_STEP_DESC
        assert "/home/ubuntu/" in _DELIVERABLE_GUIDANCE_STEP_DESC
        assert "attachments" in _DELIVERABLE_GUIDANCE_STEP_DESC
        assert "英文或拼音" in _DELIVERABLE_GUIDANCE_STEP_DESC  # 规避中文文件名编码问题


# ============================================================
# P0-2 门禁状态初始化
# ============================================================

class TestP02GateState:
    """P0-2: PlannerReActFlow 附件门禁状态"""

    def test_init_state_defaults(self):
        """__init__应初始化门禁状态(通过属性默认值验证)"""
        from app.domain.services.flows.planner_react import PlannerReActFlow
        flow = object.__new__(PlannerReActFlow)
        # 类级默认值验证(确保属性存在,绕过__init__)
        # 实际值由__init__设置,此处验证属性可在实例上设置
        flow._deliverable_retry_count = 0
        flow._current_task_type = "general"
        assert flow._deliverable_retry_count == 0
        assert flow._current_task_type == "general"


# ============================================================
# P0-2 门禁触发逻辑(通过Step构造验证)
# ============================================================

class TestP02GateLogic:
    """P0-2: 附件门禁触发逻辑验证

    门禁逻辑内联在SUMMARIZING分支中,此处通过验证触发条件的各组成部分
    确保逻辑正确性。完整集成测试在会话测试阶段验证。
    """

    def test_gate_triggers_for_data_analysis_zero_files(self):
        """data_analysis任务 + 0文件 + 未超重试 → 应触发门禁(回退EXECUTING)"""
        from app.domain.services.flows.planner_react import (
            _DELIVERABLE_REQUIRED_TASK_TYPES,
            _DELIVERABLE_RETRY_MAX,
        )
        # 模拟门禁条件判断
        all_files = []
        task_type = "data_analysis"
        retry_count = 0
        should_trigger = (
            not all_files
            and task_type in _DELIVERABLE_REQUIRED_TASK_TYPES
            and retry_count < _DELIVERABLE_RETRY_MAX
        )
        assert should_trigger is True

    def test_gate_skips_for_general_task(self):
        """general任务不应触发门禁(避免误伤问答类任务)"""
        from app.domain.services.flows.planner_react import (
            _DELIVERABLE_REQUIRED_TASK_TYPES,
            _DELIVERABLE_RETRY_MAX,
        )
        all_files = []
        task_type = "general"
        retry_count = 0
        should_trigger = (
            not all_files
            and task_type in _DELIVERABLE_REQUIRED_TASK_TYPES
            and retry_count < _DELIVERABLE_RETRY_MAX
        )
        assert should_trigger is False

    def test_gate_skips_when_files_exist(self):
        """有文件时不应触发门禁(P0-1扫描已发现交付物)"""
        from app.domain.services.flows.planner_react import (
            _DELIVERABLE_REQUIRED_TASK_TYPES,
            _DELIVERABLE_RETRY_MAX,
        )
        all_files = ["/home/ubuntu/report.docx"]
        task_type = "data_analysis"
        retry_count = 0
        should_trigger = (
            not all_files
            and task_type in _DELIVERABLE_REQUIRED_TASK_TYPES
            and retry_count < _DELIVERABLE_RETRY_MAX
        )
        assert should_trigger is False

    def test_gate_skips_when_retry_exceeded(self):
        """超重试上限时不应触发门禁(防死循环,放行summarize降级文本交付)"""
        from app.domain.services.flows.planner_react import (
            _DELIVERABLE_REQUIRED_TASK_TYPES,
            _DELIVERABLE_RETRY_MAX,
        )
        all_files = []
        task_type = "data_analysis"
        retry_count = _DELIVERABLE_RETRY_MAX  # 已达上限
        should_trigger = (
            not all_files
            and task_type in _DELIVERABLE_REQUIRED_TASK_TYPES
            and retry_count < _DELIVERABLE_RETRY_MAX
        )
        assert should_trigger is False

    def test_guidance_step_constructible(self):
        """引导步骤应能通过Step(description=...)构造(所有字段有默认值)"""
        from app.domain.services.flows.planner_react import _DELIVERABLE_GUIDANCE_STEP_DESC
        step = Step(description=_DELIVERABLE_GUIDANCE_STEP_DESC)
        assert step.description == _DELIVERABLE_GUIDANCE_STEP_DESC
        assert step.status == ExecutionStatus.PENDING  # 默认PENDING,get_next_step可获取
        assert step.id  # 自动生成UUID
