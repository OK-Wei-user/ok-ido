#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_deliverable_selection.py
交付物选择单元测试

设计理念(参考5b54ddc): 信任LLM,代码层仅做类型过滤+截断,
交付质量由提示词优化驱动。本测试覆盖:
- _get_relevant_files: 类型过滤与截断逻辑
- _is_temp_file: 临时文件扩展名判断
- _deduplicate_files: 文件列表去重(防御性去重)
- _build_fallback_summary: summarize失败兜底交付构造
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.plan import Plan, Step, ExecutionStatus


def _make_step(step_id: str, description: str = "", result: str = "") -> Step:
    """构造测试用Step对象"""
    step = Step(id=step_id, description=description)
    step.result = result
    return step


def _make_completed_step(step_id: str, description: str = "", result: str = "") -> Step:
    """构造已完成的Step对象"""
    step = _make_step(step_id, description, result)
    step.status = ExecutionStatus.COMPLETED
    step.success = True
    return step


def _make_plan(steps: list[Step]) -> Plan:
    """构造测试用Plan对象"""
    return Plan(title="test", steps=steps, language="zh")


def _create_flow(plan: Plan = None, memory_messages: list = None):
    """创建mock的PlannerReActFlow实例,预设plan和react memory"""
    from app.domain.services.flows.planner_react import PlannerReActFlow

    with patch.object(PlannerReActFlow, '__init__', lambda self: None):
        flow = PlannerReActFlow.__new__(PlannerReActFlow)
        flow.plan = plan

        # mock react agent
        flow.react = MagicMock()
        flow.react._memory = MagicMock()
        flow.react._memory.messages = memory_messages or []

        # _ensure_memory 是 async 方法
        flow.react._ensure_memory = AsyncMock()

        return flow


# ---------------------------------------------------------------------------
# _get_relevant_files: 类型过滤+截断(参考5b54ddc简洁形式)
# ---------------------------------------------------------------------------
class TestGetRelevantFiles:
    """_get_relevant_files 交付物筛选测试"""

    @pytest.mark.asyncio
    async def test_empty_files_returns_empty(self):
        """空文件列表: 返回空"""
        flow = _create_flow()
        result = await flow._get_relevant_files([])
        assert result == []

    @pytest.mark.asyncio
    async def test_all_files_returned_when_below_limit(self):
        """文件数≤_MAX_DELIVERABLE_FILES: 全部返回(不截断)"""
        flow = _create_flow()
        files = [f"/home/ubuntu/file{i}.md" for i in range(10)]
        result = await flow._get_relevant_files(files)
        assert len(result) == 10
        assert set(result) == set(files)

    @pytest.mark.asyncio
    async def test_temp_files_filtered(self):
        """临时文件(.tmp/.log/.pyc等)被过滤"""
        flow = _create_flow()
        files = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/cache.tmp",      # 临时
            "/home/ubuntu/debug.log",      # 日志
            "/home/ubuntu/bytecode.pyc",   # 编译产物
            "/home/ubuntu/data.xlsx",
        ]
        result = await flow._get_relevant_files(files)
        assert "/home/ubuntu/report.docx" in result
        assert "/home/ubuntu/data.xlsx" in result
        assert "/home/ubuntu/cache.tmp" not in result
        assert "/home/ubuntu/debug.log" not in result
        assert "/home/ubuntu/bytecode.pyc" not in result

    @pytest.mark.asyncio
    async def test_truncation_takes_recent_files(self):
        """超过_MAX_DELIVERABLE_FILES: 取最近N个(按列表末尾)"""
        flow = _create_flow()
        from app.domain.services.flows.planner_react import _MAX_DELIVERABLE_FILES
        total = _MAX_DELIVERABLE_FILES + 5
        files = [f"/home/ubuntu/file{i}.md" for i in range(total)]
        result = await flow._get_relevant_files(files)
        assert len(result) == _MAX_DELIVERABLE_FILES
        # 取最后_MAX_DELIVERABLE_FILES个
        expected = files[-_MAX_DELIVERABLE_FILES:]
        assert result == expected

    @pytest.mark.asyncio
    async def test_all_temp_files_fallback_to_original(self):
        """全部为临时文件: 返回原始列表(兜底,避免交付物为空)"""
        flow = _create_flow()
        files = [
            "/home/ubuntu/cache.tmp",
            "/home/ubuntu/debug.log",
            "/home/ubuntu/bytecode.pyc",
        ]
        result = await flow._get_relevant_files(files)
        # 全部被过滤后返回原始列表
        assert result == files

    @pytest.mark.asyncio
    async def test_preserves_file_order(self):
        """文件顺序保持不变(类型过滤后)"""
        flow = _create_flow()
        files = [
            "/home/ubuntu/a.docx",
            "/home/ubuntu/b.tmp",   # 被过滤
            "/home/ubuntu/c.xlsx",
            "/home/ubuntu/d.log",   # 被过滤
            "/home/ubuntu/e.md",
        ]
        result = await flow._get_relevant_files(files)
        assert result == ["/home/ubuntu/a.docx", "/home/ubuntu/c.xlsx", "/home/ubuntu/e.md"]


# ---------------------------------------------------------------------------
# _is_temp_file: 临时文件扩展名判断
# ---------------------------------------------------------------------------
class TestIsTempFile:
    """_is_temp_file 临时文件判断测试"""

    def test_temp_extension_is_temp(self):
        """ .tmp扩展名为临时文件"""
        from app.domain.services.flows.planner_react import PlannerReActFlow
        assert PlannerReActFlow._is_temp_file("/home/ubuntu/cache.tmp") is True

    def test_log_extension_is_temp(self):
        """ .log扩展名为临时文件"""
        from app.domain.services.flows.planner_react import PlannerReActFlow
        assert PlannerReActFlow._is_temp_file("/home/ubuntu/debug.log") is True

    def test_pyc_extension_is_temp(self):
        """ .pyc扩展名为临时文件"""
        from app.domain.services.flows.planner_react import PlannerReActFlow
        assert PlannerReActFlow._is_temp_file("/home/ubuntu/bytecode.pyc") is True

    def test_docx_not_temp(self):
        """ .docx不是临时文件"""
        from app.domain.services.flows.planner_react import PlannerReActFlow
        assert PlannerReActFlow._is_temp_file("/home/ubuntu/report.docx") is False

    def test_py_not_temp(self):
        """ .py不是临时文件(可能是用户交付的分析脚本)"""
        from app.domain.services.flows.planner_react import PlannerReActFlow
        assert PlannerReActFlow._is_temp_file("/home/ubuntu/analysis.py") is False

    def test_no_extension_not_temp(self):
        """无扩展名不是临时文件"""
        from app.domain.services.flows.planner_react import PlannerReActFlow
        assert PlannerReActFlow._is_temp_file("/home/ubuntu/Makefile") is False

    def test_case_insensitive(self):
        """扩展名判断大小写不敏感"""
        from app.domain.services.flows.planner_react import PlannerReActFlow
        assert PlannerReActFlow._is_temp_file("/home/ubuntu/FILE.TMP") is True
        assert PlannerReActFlow._is_temp_file("/home/ubuntu/FILE.Log") is True


# ---------------------------------------------------------------------------
# deduplicate_files: 文件去重(防御性去重)
# F2-2抽离: 方法已从SessionService._deduplicate_files迁移至FilePresentationService.deduplicate_files
# ---------------------------------------------------------------------------
class TestDeduplicateFiles:
    """deduplicate_files 文件去重测试"""

    def _make_file(self, filepath: str, file_id: str = None):
        """构造测试用File对象"""
        from app.domain.models.file import File
        return File(
            id=file_id or f"id-{filepath}",
            filename=filepath.split("/")[-1],
            filepath=filepath,
        )

    def test_no_duplicates(self):
        """无重复: 返回全部文件"""
        from app.application.services.file_presentation_service import FilePresentationService

        files = [
            self._make_file("/home/ubuntu/a.md"),
            self._make_file("/home/ubuntu/b.py"),
            self._make_file("/home/ubuntu/c.txt"),
        ]
        result = FilePresentationService.deduplicate_files(files)
        assert len(result) == 3

    def test_with_duplicates(self):
        """有重复: 按filepath去重,保留首次出现的记录"""
        from app.application.services.file_presentation_service import FilePresentationService

        files = [
            self._make_file("/home/ubuntu/report.md", "id-1"),
            self._make_file("/home/ubuntu/script.py", "id-2"),
            self._make_file("/home/ubuntu/report.md", "id-3"),  # 重复filepath
            self._make_file("/tmp/output.txt", "id-4"),
            self._make_file("/home/ubuntu/script.py", "id-5"),  # 重复filepath
        ]
        result = FilePresentationService.deduplicate_files(files)
        assert len(result) == 3
        # 保留首次出现的id
        result_ids = [f.id for f in result]
        assert "id-1" in result_ids
        assert "id-2" in result_ids
        assert "id-4" in result_ids
        assert "id-3" not in result_ids
        assert "id-5" not in result_ids

    def test_empty_list(self):
        """空列表: 返回空"""
        from app.application.services.file_presentation_service import FilePresentationService

        result = FilePresentationService.deduplicate_files([])
        assert result == []

    def test_all_same_filepath(self):
        """全部相同filepath: 只保留第一个"""
        from app.application.services.file_presentation_service import FilePresentationService

        files = [
            self._make_file("/home/ubuntu/dup.md", "id-1"),
            self._make_file("/home/ubuntu/dup.md", "id-2"),
            self._make_file("/home/ubuntu/dup.md", "id-3"),
        ]
        result = FilePresentationService.deduplicate_files(files)
        assert len(result) == 1
        assert result[0].id == "id-1"

    def test_preserves_order(self):
        """去重后保持首次出现的顺序"""
        from app.application.services.file_presentation_service import FilePresentationService

        files = [
            self._make_file("/home/ubuntu/c.md"),
            self._make_file("/home/ubuntu/a.md"),
            self._make_file("/home/ubuntu/b.md"),
            self._make_file("/home/ubuntu/a.md"),  # 重复
        ]
        result = FilePresentationService.deduplicate_files(files)
        assert len(result) == 3
        assert result[0].filepath == "/home/ubuntu/c.md"
        assert result[1].filepath == "/home/ubuntu/a.md"
        assert result[2].filepath == "/home/ubuntu/b.md"


# ---------------------------------------------------------------------------
# _build_fallback_summary: summarize失败兜底交付构造
# ---------------------------------------------------------------------------
class TestBuildFallbackSummary:
    """_build_fallback_summary 兜底交付构造测试

    Phase D简化: 直接使用最后完成步骤的result，不做Markdown拼接。
    测试summarize失败时,PlannerReActFlow基于已完成步骤结果构造兜底最终回复的逻辑。
    """

    def test_no_plan_returns_generic_fallback(self):
        """无计划(plan=None): 返回通用兜底文案"""
        flow = _create_flow(plan=None)
        result = flow._build_fallback_summary()
        assert "任务已执行完成" in result
        assert "请查看执行步骤" in result

    def test_empty_steps_returns_generic_fallback(self):
        """空步骤列表: 返回通用兜底文案"""
        plan = _make_plan([])
        flow = _create_flow(plan=plan)
        result = flow._build_fallback_summary()
        assert "任务已执行完成" in result
        assert "请查看执行步骤" in result

    def test_no_completed_steps_returns_generic_fallback(self):
        """无已完成步骤(PENDING状态): 返回通用兜底文案"""
        steps = [_make_step("1", description="待执行步骤")]
        plan = _make_plan(steps)
        flow = _create_flow(plan=plan)
        result = flow._build_fallback_summary()
        assert "任务已执行完成" in result
        assert "请查看执行步骤" in result

    def test_single_completed_step_result_as_summary(self):
        """单步完成: 以该步骤result作为兜底核心内容"""
        steps = [
            _make_completed_step("1", description="数据分析", result="共分析100条记录"),
        ]
        plan = _make_plan(steps)
        flow = _create_flow(plan=plan)
        result = flow._build_fallback_summary()
        assert "共分析100条记录" in result
        assert "请查看本次会话生成的文件附件" in result

    def test_last_completed_step_used_as_core_summary(self):
        """多步完成: 以最后完成步骤的result作为核心摘要"""
        steps = [
            _make_completed_step("1", description="数据清洗", result="清洗完成"),
            _make_completed_step("2", description="数据分析", result="分析结果:5个类别"),
            _make_completed_step("3", description="报告生成", result="最终报告已生成"),
        ]
        plan = _make_plan(steps)
        flow = _create_flow(plan=plan)
        result = flow._build_fallback_summary()
        # 最后一步的result应作为核心
        assert "最终报告已生成" in result
        assert "共完成3个步骤" in result

    def test_skips_failed_steps(self):
        """失败步骤不计入兜底摘要"""
        failed_step = _make_completed_step("1", description="失败步骤", result="执行出错")
        failed_step.success = False
        steps = [
            failed_step,
            _make_completed_step("2", description="成功步骤", result="成功完成"),
        ]
        plan = _make_plan(steps)
        flow = _create_flow(plan=plan)
        result = flow._build_fallback_summary()
        assert "成功完成" in result
        assert "执行出错" not in result

    def test_skips_completed_steps_with_empty_result(self):
        """result为空的已完成步骤不计入兜底摘要"""
        steps = [
            _make_completed_step("1", description="空结果步骤", result=""),
            _make_completed_step("2", description="有效步骤", result="有效结果"),
        ]
        plan = _make_plan(steps)
        flow = _create_flow(plan=plan)
        result = flow._build_fallback_summary()
        assert "有效结果" in result

    def test_fallback_contains_file_delivery_hint(self):
        """Phase D简化: 兜底回复包含文件附件提示"""
        steps = [_make_completed_step("1", description="任务", result="完成")]
        plan = _make_plan(steps)
        flow = _create_flow(plan=plan)
        result = flow._build_fallback_summary()
        assert "文件附件" in result
