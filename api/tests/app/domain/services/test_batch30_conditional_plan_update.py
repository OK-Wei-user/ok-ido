#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Batch 30 (F11-1) 条件化计划更新单元测试

覆盖 _plan_update_policy 纯函数模块:
1. should_skip_update_plan: 判断是否可跳过 update_plan
2. step_output_referenced_later: 检测后续步骤是否引用当前步骤产出
3. _get_subsequent_pending_descriptions: 获取后续 PENDING 步骤描述
4. _extract_output_paths: 从步骤产出提取文件路径

测试用例:
- 顺序独立步骤成功 → 跳过 update_plan
- 步骤产出文件被后续引用 → 触发 update_plan
- 失败步骤 → 触发 update_plan
- 连续跳过2次后第3步 → 强制触发
- 迭代溢出失败 → 不跳过(由Flow层直接进总结,policy层也返回False)
"""
import pytest

from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.services.flows._plan_update_policy import (
    should_skip_update_plan,
    step_output_referenced_later,
    _get_subsequent_pending_descriptions,
    _extract_output_paths,
    MAX_CONSECUTIVE_SKIPS,
)


def _make_step(
    step_id: str = "s1",
    description: str = "执行步骤",
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    success: bool = True,
    result: str = "步骤完成",
    error: str = None,
    attachments: list = None,
) -> Step:
    """构造测试用 Step"""
    return Step(
        id=step_id,
        description=description,
        status=status,
        success=success,
        result=result,
        error=error,
        attachments=attachments or [],
    )


def _make_plan(steps: list) -> Plan:
    """构造测试用 Plan"""
    return Plan(id="plan1", title="测试", steps=steps)


class TestShouldSkipUpdatePlan:
    """should_skip_update_plan 核心判断逻辑测试"""

    def test_skip_for_independent_completed_step(self):
        """顺序独立步骤成功且无引用 → 跳过 update_plan"""
        step = _make_step("s1", description="导出数据", result="导出完成")
        plan = _make_plan([
            step,
            _make_step("s2", description="生成报告", status=ExecutionStatus.PENDING, success=False, result=None),
        ])
        assert should_skip_update_plan(step, plan, consecutive_skipped=0) is True

    def test_no_skip_when_output_referenced(self):
        """步骤产出文件被后续步骤引用 → 触发 update_plan"""
        step = _make_step(
            "s1", description="导出数据",
            result="已导出到 /home/ubuntu/data.csv",
            attachments=["/home/ubuntu/data.csv"],
        )
        plan = _make_plan([
            step,
            _make_step(
                "s2", description="分析上述文件 /home/ubuntu/data.csv",
                status=ExecutionStatus.PENDING, success=False, result=None,
            ),
        ])
        assert should_skip_update_plan(step, plan, consecutive_skipped=0) is False

    def test_no_skip_for_failed_step(self):
        """失败步骤 → 触发 update_plan(需恢复决策)"""
        step = _make_step(
            "s1", description="导出数据",
            status=ExecutionStatus.FAILED, success=False, error="工具调用超时",
        )
        plan = _make_plan([step, _make_step("s2", status=ExecutionStatus.PENDING)])
        assert should_skip_update_plan(step, plan, consecutive_skipped=0) is False

    def test_force_update_after_max_skips(self):
        """连续跳过达上限(MAX_CONSECUTIVE_SKIPS)后 → 强制触发更新"""
        step = _make_step("s1", description="导出数据", result="完成")
        plan = _make_plan([
            step,
            _make_step("s2", status=ExecutionStatus.PENDING),
        ])
        # consecutive_skipped 达上限 → 安全网触发
        assert should_skip_update_plan(
            step, plan, consecutive_skipped=MAX_CONSECUTIVE_SKIPS
        ) is False

    def test_skip_within_limit_before_max(self):
        """连续跳过未达上限(MAX-1) → 仍可跳过"""
        step = _make_step("s1", description="独立步骤", result="完成")
        plan = _make_plan([
            step,
            _make_step("s2", status=ExecutionStatus.PENDING),
        ])
        assert should_skip_update_plan(
            step, plan, consecutive_skipped=MAX_CONSECUTIVE_SKIPS - 1
        ) is True

    def test_no_skip_for_browser_navigate_step(self):
        """浏览器导航步骤 → 强制更新(状态依赖,跳过会导致重复操作)"""
        step = _make_step("s1", description="使用browser打开页面", result="已打开页面")
        plan = _make_plan([
            step,
            _make_step("s2", description="点击按钮", status=ExecutionStatus.PENDING),
        ])
        assert should_skip_update_plan(step, plan, consecutive_skipped=0) is False

    def test_no_skip_for_browser_click_step(self):
        """浏览器点击步骤 → 强制更新(单步可能完成多个操作,需同步计划)"""
        step = _make_step("s1", description="点击Form表单菜单", result="已进入表单页面")
        plan = _make_plan([
            step,
            _make_step("s2", description="滚动到对齐方式", status=ExecutionStatus.PENDING),
        ])
        assert should_skip_update_plan(step, plan, consecutive_skipped=0) is False

    def test_no_skip_for_browser_scroll_step(self):
        """浏览器滚动步骤 → 强制更新(页面视口变化,后续步骤需感知当前位置)"""
        step = _make_step("s1", description="滚动到对齐方式区域", result="已滚动到位")
        plan = _make_plan([
            step,
            _make_step("s2", description="输入name为杰瑞", status=ExecutionStatus.PENDING),
        ])
        assert should_skip_update_plan(step, plan, consecutive_skipped=0) is False

    def test_no_skip_for_browser_input_step(self):
        """浏览器输入步骤 → 强制更新(表单状态变化)"""
        step = _make_step("s1", description="在输入框输入杰瑞", result="已输入")
        plan = _make_plan([
            step,
            _make_step("s2", description="提交表单", status=ExecutionStatus.PENDING),
        ])
        assert should_skip_update_plan(step, plan, consecutive_skipped=0) is False

    def test_skip_for_non_browser_step_with_same_keywords(self):
        """非浏览器步骤(无浏览器关键词) → 正常跳过"""
        step = _make_step("s1", description="导出数据到CSV", result="导出完成")
        plan = _make_plan([
            step,
            _make_step("s2", description="生成报告", status=ExecutionStatus.PENDING),
        ])
        assert should_skip_update_plan(step, plan, consecutive_skipped=0) is True

    def test_no_skip_for_unsuccessful_step(self):
        """步骤完成但 success=False → 触发更新"""
        step = _make_step(
            "s1", status=ExecutionStatus.COMPLETED, success=False, result="异常完成",
        )
        plan = _make_plan([step, _make_step("s2", status=ExecutionStatus.PENDING)])
        assert should_skip_update_plan(step, plan, consecutive_skipped=0) is False

    def test_iteration_overflow_failed_returns_false(self):
        """迭代溢出失败 → policy层返回False(Flow层会直接进SUMMARIZING)"""
        step = _make_step(
            "s1", description="执行任务",
            status=ExecutionStatus.FAILED, success=False,
            error="Agent迭代超过最大迭代次数: 10, 任务处理失败",
        )
        plan = _make_plan([step, _make_step("s2", status=ExecutionStatus.PENDING)])
        # policy层对任何FAILED都返回False,Flow层在此之前已拦截迭代溢出
        assert should_skip_update_plan(step, plan, consecutive_skipped=0) is False


class TestStepOutputReferencedLater:
    """step_output_referenced_later 引用检测测试"""

    def test_no_subsequent_steps(self):
        """无后续步骤 → 未引用(最后一个步骤)"""
        step = _make_step("s1", attachments=["/home/ubuntu/data.csv"])
        plan = _make_plan([step])
        assert step_output_referenced_later(step, plan) is False

    def test_subsequent_step_references_file_path(self):
        """后续步骤描述包含产出文件路径 → 引用"""
        step = _make_step(
            "s1", result="导出到 /home/ubuntu/report.xlsx",
            attachments=["/home/ubuntu/report.xlsx"],
        )
        plan = _make_plan([
            step,
            _make_step("s2", description="读取 /home/ubuntu/report.xlsx 进行分析",
                       status=ExecutionStatus.PENDING),
        ])
        assert step_output_referenced_later(step, plan) is True

    def test_subsequent_step_references_basename(self):
        """后续步骤描述包含产出文件名(非完整路径) → 引用"""
        step = _make_step(
            "s1", attachments=["/home/ubuntu/output/data.csv"],
        )
        plan = _make_plan([
            step,
            _make_step("s2", description="处理 data.csv 文件",
                       status=ExecutionStatus.PENDING),
        ])
        assert step_output_referenced_later(step, plan) is True

    def test_subsequent_step_has_referential_keyword(self):
        """后续步骤含指代词且当前步骤有文件产出 → 保守判断为引用"""
        step = _make_step(
            "s1", attachments=["/home/ubuntu/result.json"],
        )
        plan = _make_plan([
            step,
            _make_step("s2", description="分析上述文件并生成摘要",
                       status=ExecutionStatus.PENDING),
        ])
        assert step_output_referenced_later(step, plan) is True

    def test_no_reference_when_no_file_output(self):
        """当前步骤无文件产出 → 即使后续有指代词也不判断为引用"""
        step = _make_step("s1", result="任务完成,无文件产出")
        plan = _make_plan([
            step,
            _make_step("s2", description="基于上述结果进行分析",
                       status=ExecutionStatus.PENDING),
        ])
        assert step_output_referenced_later(step, plan) is False

    def test_completed_subsequent_step_not_checked(self):
        """已完成的后续步骤不参与引用检测(只检查PENDING)"""
        step = _make_step("s1", attachments=["/home/ubuntu/data.csv"])
        plan = _make_plan([
            step,
            _make_step("s2", description="读取 /home/ubuntu/data.csv",
                       status=ExecutionStatus.COMPLETED, result="完成"),
        ])
        assert step_output_referenced_later(step, plan) is False


class TestExtractOutputPaths:
    """_extract_output_paths 文件路径提取测试"""

    def test_extract_from_attachments(self):
        """从 attachments 提取文件路径"""
        step = _make_step(attachments=["/home/ubuntu/a.csv", "/home/ubuntu/b.txt"])
        paths = _extract_output_paths(step)
        assert "/home/ubuntu/a.csv" in paths
        assert "/home/ubuntu/b.txt" in paths

    def test_extract_from_result(self):
        """从 result 文本提取文件路径"""
        step = _make_step(result="已保存到 /home/ubuntu/output/report.xlsx")
        paths = _extract_output_paths(step)
        assert "/home/ubuntu/output/report.xlsx" in paths

    def test_deduplicate_paths(self):
        """重复路径去重"""
        step = _make_step(
            attachments=["/home/ubuntu/data.csv"],
            result="保存到 /home/ubuntu/data.csv",
        )
        paths = _extract_output_paths(step)
        assert paths.count("/home/ubuntu/data.csv") == 1

    def test_no_paths_when_empty(self):
        """无产出时返回空列表"""
        step = _make_step(result="任务完成")
        assert _extract_output_paths(step) == []


class TestGetSubsequentPendingDescriptions:
    """_get_subsequent_pending_descriptions 后续步骤描述获取测试"""

    def test_returns_pending_after_current(self):
        """返回当前步骤之后的所有 PENDING 步骤描述"""
        step = _make_step("s2", status=ExecutionStatus.COMPLETED)
        plan = _make_plan([
            _make_step("s1", status=ExecutionStatus.COMPLETED, result="完成"),
            step,
            _make_step("s3", description="步骤三", status=ExecutionStatus.PENDING),
            _make_step("s4", description="步骤四", status=ExecutionStatus.PENDING),
        ])
        descs = _get_subsequent_pending_descriptions(step, plan)
        assert "步骤三" in descs
        assert "步骤四" in descs
        assert len(descs) == 2

    def test_excludes_completed_subsequent(self):
        """排除已完成的后续步骤"""
        step = _make_step("s1")
        plan = _make_plan([
            step,
            _make_step("s2", description="已完成步骤", status=ExecutionStatus.COMPLETED, result="done"),
            _make_step("s3", description="待执行步骤", status=ExecutionStatus.PENDING),
        ])
        descs = _get_subsequent_pending_descriptions(step, plan)
        assert descs == ["待执行步骤"]

    def test_empty_when_last_step(self):
        """当前步骤是最后一步 → 返回空列表"""
        step = _make_step("s1")
        plan = _make_plan([step])
        assert _get_subsequent_pending_descriptions(step, plan) == []

    def test_empty_when_step_not_in_plan(self):
        """当前步骤不在计划中 → 返回空列表(异常安全)"""
        step = _make_step("unknown")
        plan = _make_plan([_make_step("s1")])
        assert _get_subsequent_pending_descriptions(step, plan) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
