#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_f10_5_progress.py
F10-5 步骤进度状态扩展单元测试 - 验证 StepEvent.progress 字段与进度估算算法

测试覆盖:
- StepEvent.progress 字段默认值与显式赋值
- StepEventData.progress 序列化透传
- ReActAgent._estimate_step_progress 渐进表与上限收敛
- 进度不返回 100(由 COMPLETED 事件显式设置)
"""
from unittest.mock import MagicMock

import pytest

from app.domain.models.event import StepEvent, StepEventStatus
from app.domain.models.plan import Step, ExecutionStatus
from app.domain.services.agents.react import ReActAgent


# ========== StepEvent.progress 字段测试 ==========

class TestStepEventProgress:
    """StepEvent.progress 字段行为测试"""

    def _build_step(self) -> Step:
        """构造测试用 Step"""
        return Step(
            id="step_test",
            description="测试步骤",
            status=ExecutionStatus.PENDING,
        )

    def test_default_progress_is_zero(self):
        """未显式指定 progress 时默认为 0(前端契约:0 表示未上报)"""
        step = self._build_step()
        event = StepEvent(step=step, status=StepEventStatus.STARTED)
        assert event.progress == 0

    def test_explicit_progress_started(self):
        """STARTED 事件可显式设置 progress"""
        step = self._build_step()
        event = StepEvent(
            step=step,
            status=StepEventStatus.STARTED,
            progress=45,
            message="执行中",
        )
        assert event.progress == 45

    def test_completed_progress_is_100(self):
        """COMPLETED 事件 progress 显式设置 100(前端据此完成进度条)"""
        step = self._build_step()
        step.status = ExecutionStatus.COMPLETED
        event = StepEvent(
            step=step,
            status=StepEventStatus.COMPLETED,
            progress=100,
        )
        assert event.progress == 100
        assert event.step.status == ExecutionStatus.COMPLETED

    def test_failed_progress_keeps_last_value(self):
        """FAILED 事件保持上次进度值(前端可看到中断位置)"""
        step = self._build_step()
        # 模拟上次进度为 60,失败时不重置
        event = StepEvent(
            step=step,
            status=StepEventStatus.FAILED,
            progress=60,
            message="步骤失败",
        )
        assert event.progress == 60
        assert event.status == StepEventStatus.FAILED


# ========== StepEventData.progress 序列化测试 ==========

class TestStepEventDataProgress:
    """StepEventData.progress 序列化测试"""

    def test_from_event_progress_passthrough(self):
        """StepSSEEvent.from_event 透传 progress 字段"""
        from app.interfaces.schemas.event import StepSSEEvent

        step = Step(id="s1", description="步骤1", status=ExecutionStatus.RUNNING)
        event = StepEvent(
            step=step,
            status=StepEventStatus.STARTED,
            progress=70,
            message="执行中",
        )
        sse = StepSSEEvent.from_event(event)
        assert sse.data.progress == 70
        # 修复: status 来源于 event.status(StepEventStatus.STARTED→RUNNING),非 event.step.status
        assert sse.data.status == ExecutionStatus.RUNNING
        assert sse.data.description == "步骤1"

    def test_from_event_default_progress_zero(self):
        """未指定 progress 时 SSE 事件 progress=0(向后兼容)"""
        from app.interfaces.schemas.event import StepSSEEvent

        step = Step(id="s2", description="步骤2", status=ExecutionStatus.RUNNING)
        event = StepEvent(step=step, status=StepEventStatus.STARTED)
        sse = StepSSEEvent.from_event(event)
        assert sse.data.progress == 0


# ========== ReActAgent._estimate_step_progress 算法测试 ==========

class TestEstimateStepProgress:
    """ReActAgent._estimate_step_progress 静态方法测试"""

    def test_zero_or_negative_returns_zero(self):
        """0 或负数工具调用次数返回 0(防御性)"""
        assert ReActAgent._estimate_step_progress(0) == 0
        assert ReActAgent._estimate_step_progress(-1) == 0

    def test_progress_table_progression(self):
        """渐进表 [25, 45, 60, 70, 78] 依次推进"""
        assert ReActAgent._estimate_step_progress(1) == 25
        assert ReActAgent._estimate_step_progress(2) == 45
        assert ReActAgent._estimate_step_progress(3) == 60
        assert ReActAgent._estimate_step_progress(4) == 70
        assert ReActAgent._estimate_step_progress(5) == 78

    def test_sixth_call_adds_two_percent(self):
        """第 6 次工具调用 +2%(78+2=80)"""
        assert ReActAgent._estimate_step_progress(6) == 80

    def test_high_call_count_caps_at_90(self):
        """高频次工具调用收敛到 90%(保留 10% 余量给最终总结)"""
        assert ReActAgent._estimate_step_progress(50) == 90
        assert ReActAgent._estimate_step_progress(100) == 90
        assert ReActAgent._estimate_step_progress(1000) == 90

    def test_progress_never_reaches_100(self):
        """算法永远不会返回 100(100 由 COMPLETED 事件显式设置)"""
        # 覆盖渐进表、过渡区、收敛上限三个区间
        for n in [1, 5, 6, 10, 20, 50, 100, 500]:
            p = ReActAgent._estimate_step_progress(n)
            assert 0 <= p < 100, f"n={n} 时 progress={p} 不应在 [0,100) 区间外"

    def test_progress_monotonically_non_decreasing(self):
        """进度单调非递减(避免前端进度条倒退)"""
        prev = 0
        for n in range(1, 50):
            curr = ReActAgent._estimate_step_progress(n)
            assert curr >= prev, f"n={n} 时 progress={curr} 小于上次 {prev}"
            prev = curr


# ========== 集成测试: 进度上报到事件流 ==========

class TestProgressReportingIntegration:
    """ReActAgent 进度上报到事件流的集成测试"""

    def test_react_agent_has_estimate_method(self):
        """ReActAgent 类暴露 _estimate_step_progress 静态方法"""
        assert hasattr(ReActAgent, "_estimate_step_progress")
        assert callable(ReActAgent._estimate_step_progress)

    def test_progress_algorithm_is_pure_function(self):
        """算法是纯函数(相同输入产生相同输出,无副作用)"""
        results_1 = [ReActAgent._estimate_step_progress(n) for n in range(1, 20)]
        results_2 = [ReActAgent._estimate_step_progress(n) for n in range(1, 20)]
        assert results_1 == results_2


# ========== StepSSEEvent 状态映射测试(会话bffcb4ae根因修复) ==========

class TestStepSSEEventStatusMapping:
    """StepSSEEvent.from_event 状态映射测试

    根因: 原 from_event 使用 event.step.status(共享可变引用,可被污染为 completed)
    修复: 改用 event.status(StepEventStatus,创建时设定且不可变)映射为 ExecutionStatus
    映射表: STARTED→RUNNING, COMPLETED→COMPLETED, FAILED→FAILED
    """

    def test_started_event_maps_to_running(self):
        """STARTED 事件 → SSE status=running(前端保持 lastStepId,不误判为新步骤)"""
        from app.interfaces.schemas.event import StepSSEEvent

        step = Step(id="s1", description="步骤1", status=ExecutionStatus.RUNNING)
        event = StepEvent(step=step, status=StepEventStatus.STARTED, progress=45)
        sse = StepSSEEvent.from_event(event)
        assert sse.data.status == ExecutionStatus.RUNNING

    def test_completed_event_maps_to_completed(self):
        """COMPLETED 事件 → SSE status=completed(前端重置 lastStepId)"""
        from app.interfaces.schemas.event import StepSSEEvent

        step = Step(id="s1", description="步骤1", status=ExecutionStatus.COMPLETED)
        event = StepEvent(step=step, status=StepEventStatus.COMPLETED, progress=100)
        sse = StepSSEEvent.from_event(event)
        assert sse.data.status == ExecutionStatus.COMPLETED

    def test_failed_event_maps_to_failed(self):
        """FAILED 事件 → SSE status=failed(前端重置 lastStepId)"""
        from app.interfaces.schemas.event import StepSSEEvent

        step = Step(id="s1", description="步骤1", status=ExecutionStatus.FAILED)
        event = StepEvent(step=step, status=StepEventStatus.FAILED, progress=60)
        sse = StepSSEEvent.from_event(event)
        assert sse.data.status == ExecutionStatus.FAILED

    def test_step_status_pollution_does_not_affect_sse(self):
        """step.status 被污染为 completed 时,SSE status 仍反映 event.status(核心修复验证)

        模拟会话bffcb4ae场景:
        - 事件创建时 step.status=RUNNING, event.status=STARTED
        - 事件持久化前 step.status 被污染为 COMPLETED
        - SSE 序列化应使用 event.status(STARTED→RUNNING),而非被污染的 step.status(COMPLETED)
        """
        from app.interfaces.schemas.event import StepSSEEvent

        step = Step(id="s2", description="下载文件", status=ExecutionStatus.RUNNING)
        event = StepEvent(step=step, status=StepEventStatus.STARTED, progress=50)
        # 模拟共享引用污染: step.status 被后续代码改为 COMPLETED
        step.status = ExecutionStatus.COMPLETED
        # SSE 序列化应反映 event.status(STARTED→RUNNING),而非 step.status(COMPLETED)
        sse = StepSSEEvent.from_event(event)
        assert sse.data.status == ExecutionStatus.RUNNING, (
            "step.status 污染不应影响 SSE status,应使用 event.status 映射"
        )


# ========== Step 快照隔离测试(根因修复:_make_step_event 深拷贝) ==========

class TestMakeStepEventSnapshotIsolation:
    """_make_step_event 深拷贝隔离测试

    根因: StepEvent(step=step) 持有共享引用,step.status 后续变更会污染已产出事件
    修复: _make_step_event 使用 model_copy(deep=True) 创建 step 快照
    """

    def test_make_step_event_creates_independent_snapshot(self):
        """_make_step_event 创建的事件中 step 是独立副本,不受原 step 变更影响"""
        step = Step(id="s1", description="测试步骤", status=ExecutionStatus.RUNNING)
        event = ReActAgent._make_step_event(step, StepEventStatus.STARTED, progress=30)
        # 修改原 step 的状态
        step.status = ExecutionStatus.COMPLETED
        step.description = "被修改的描述"
        # 事件中的 step 应不受影响(深拷贝隔离)
        assert event.step.status == ExecutionStatus.RUNNING
        assert event.step.description == "测试步骤"

    def test_make_step_event_preserves_all_fields(self):
        """_make_step_event 正确传递所有字段(status/progress/message)"""
        step = Step(id="s1", description="步骤", status=ExecutionStatus.RUNNING)
        event = ReActAgent._make_step_event(
            step, StepEventStatus.STARTED, progress=70, message="执行中"
        )
        assert event.status == StepEventStatus.STARTED
        assert event.progress == 70
        assert event.message == "执行中"
        assert event.step.id == "s1"

    def test_make_step_event_attachments_isolated(self):
        """_make_step_event 深拷贝 attachments 列表,原 step 修改不影响事件"""
        step = Step(
            id="s1", description="步骤",
            status=ExecutionStatus.RUNNING,
            attachments=["/tmp/file1.csv"],
        )
        event = ReActAgent._make_step_event(step, StepEventStatus.STARTED, progress=50)
        # 修改原 step 的 attachments
        step.attachments.append("/tmp/file2.csv")
        # 事件中的 attachments 应不受影响
        assert event.step.attachments == ["/tmp/file1.csv"]
