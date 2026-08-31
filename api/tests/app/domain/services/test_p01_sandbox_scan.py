# -*- coding: utf-8 -*-
"""批次45 P0-1: 沙箱交付物主动扫描单元测试

覆盖:
- 断裂点1修复: _STEP_RESULT_FILE_PATH_PATTERN 支持中文文件名
- SandboxScanEvent 事件类型
- PlannerReActFlow._scan_sandbox_deliverables 沙箱主动扫描
- AgentTaskRunner 处理 SandboxScanEvent 并发同步
"""
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from app.domain.models.event import SandboxScanEvent
from app.domain.models.tool_result import ToolResult


# ============================================================
# 断裂点1: 正则支持中文文件名
# ============================================================

class TestRegexChineseFilename:
    """P0-1 断裂点1: _STEP_RESULT_FILE_PATH_PATTERN 支持中文文件名"""

    def test_chinese_filename_extracted(self):
        """中文文件名'2026年1-5月出入库深度经营分析报告.docx'应能被提取"""
        from app.domain.services.agent_task_runner import _STEP_RESULT_FILE_PATH_PATTERN
        text = "已生成报告: /home/ubuntu/2026年1-5月出入库深度经营分析报告.docx"
        matches = _STEP_RESULT_FILE_PATH_PATTERN.findall(text)
        assert "/home/ubuntu/2026年1-5月出入库深度经营分析报告.docx" in matches

    def test_english_filename_still_works(self):
        """英文文件名仍能正常提取(向后兼容)"""
        from app.domain.services.agent_task_runner import _STEP_RESULT_FILE_PATH_PATTERN
        text = "已生成: /home/ubuntu/report.xlsx"
        matches = _STEP_RESULT_FILE_PATH_PATTERN.findall(text)
        assert "/home/ubuntu/report.xlsx" in matches

    def test_chinese_filename_in_extract_deliverable_paths(self):
        """_extract_deliverable_paths 应能从含中文文件名的result中提取路径"""
        from app.domain.services.agent_task_runner import AgentTaskRunner
        from app.domain.models.plan import Step
        step = Step(result="生成文件 /home/ubuntu/2026年1-5月出入库深度经营分析报告.docx 完成")
        paths = AgentTaskRunner._extract_deliverable_paths(step)
        assert "/home/ubuntu/2026年1-5月出入库深度经营分析报告.docx" in paths

    def test_multiple_chinese_filenames_extracted(self):
        """多个中文文件名应全部提取"""
        from app.domain.services.agent_task_runner import AgentTaskRunner
        from app.domain.models.plan import Step
        step = Step(result=(
            "生成 /home/ubuntu/出入库统计.xlsx 和 "
            "/home/ubuntu/经营分析报告.docx"
        ))
        paths = AgentTaskRunner._extract_deliverable_paths(step)
        assert "/home/ubuntu/出入库统计.xlsx" in paths
        assert "/home/ubuntu/经营分析报告.docx" in paths


# ============================================================
# SandboxScanEvent 事件类型
# ============================================================

class TestSandboxScanEvent:
    """P0-1: SandboxScanEvent 事件类型"""

    def test_event_type(self):
        """事件type应为sandbox_scan"""
        event = SandboxScanEvent(file_paths=["/home/ubuntu/x.docx"])
        assert event.type == "sandbox_scan"

    def test_file_paths_default_empty(self):
        """file_paths默认应为空列表"""
        event = SandboxScanEvent()
        assert event.file_paths == []

    def test_file_paths_stored(self):
        """file_paths应正确存储"""
        paths = ["/home/ubuntu/a.docx", "/home/ubuntu/b.xlsx"]
        event = SandboxScanEvent(file_paths=paths)
        assert event.file_paths == paths

    def test_event_in_event_union(self):
        """SandboxScanEvent应在Event Union中(可被TypeAdapter解析)"""
        from pydantic import TypeAdapter
        from app.domain.models.event import Event
        adapter = TypeAdapter(Event)
        event = adapter.validate_python({
            "type": "sandbox_scan",
            "file_paths": ["/home/ubuntu/x.docx"],
        })
        assert isinstance(event, SandboxScanEvent)


# ============================================================
# PlannerReActFlow._scan_sandbox_deliverables
# ============================================================

class TestScanSandboxDeliverables:
    """P0-1: PlannerReActFlow._scan_sandbox_deliverables 沙箱主动扫描"""

    def _build_flow(self, sandbox=None):
        """构建PlannerReActFlow实例(绕过__init__)用于测试扫描方法"""
        from app.domain.services.flows.planner_react import PlannerReActFlow
        flow = object.__new__(PlannerReActFlow)
        flow._sandbox = sandbox
        flow._session_id = "test_session"
        return flow

    def test_scan_finds_deliverables(self):
        """扫描应发现沙箱中的交付物文件"""
        sandbox = MagicMock()
        # find_files 对 *.docx 返回docx文件
        async def mock_find_files(dir_path, glob_pattern):
            if glob_pattern == "*.docx":
                return ToolResult(success=True, data=["/home/ubuntu/report.docx"])
            return ToolResult(success=True, data=[])
        sandbox.find_files = mock_find_files
        flow = self._build_flow(sandbox=sandbox)
        result = asyncio.get_event_loop().run_until_complete(flow._scan_sandbox_deliverables())
        assert "/home/ubuntu/report.docx" in result

    def test_scan_finds_chinese_filename(self):
        """扫描应发现中文文件名交付物(批次44会话场景)"""
        sandbox = MagicMock()
        async def mock_find_files(dir_path, glob_pattern):
            if glob_pattern == "*.docx":
                return ToolResult(success=True, data=["/home/ubuntu/2026年1-5月出入库深度经营分析报告.docx"])
            return ToolResult(success=True, data=[])
        sandbox.find_files = mock_find_files
        flow = self._build_flow(sandbox=sandbox)
        result = asyncio.get_event_loop().run_until_complete(flow._scan_sandbox_deliverables())
        assert "/home/ubuntu/2026年1-5月出入库深度经营分析报告.docx" in result

    def test_scan_filters_intermediate(self):
        """扫描应过滤中间产物(/tmp/ /workspace/ .skill)"""
        sandbox = MagicMock()
        async def mock_find_files(dir_path, glob_pattern):
            if glob_pattern == "*.docx":
                return ToolResult(success=True, data=[
                    "/home/ubuntu/report.docx",
                    "/tmp/temp.docx",  # 中间产物,应过滤
                    "/home/ubuntu/workspace/draft.docx",  # 中间产物,应过滤
                ])
            return ToolResult(success=True, data=[])
        sandbox.find_files = mock_find_files
        flow = self._build_flow(sandbox=sandbox)
        result = asyncio.get_event_loop().run_until_complete(flow._scan_sandbox_deliverables())
        assert "/home/ubuntu/report.docx" in result
        assert "/tmp/temp.docx" not in result
        assert "/home/ubuntu/workspace/draft.docx" not in result

    def test_scan_empty_when_no_files(self):
        """沙箱无交付物时应返回空列表"""
        sandbox = MagicMock()
        async def mock_find_files(dir_path, glob_pattern):
            return ToolResult(success=True, data=[])
        sandbox.find_files = mock_find_files
        flow = self._build_flow(sandbox=sandbox)
        result = asyncio.get_event_loop().run_until_complete(flow._scan_sandbox_deliverables())
        assert result == []

    def test_scan_exception_returns_empty(self):
        """扫描整体异常时应返回空列表(不抛异常)"""
        sandbox = MagicMock()
        async def mock_find_files(dir_path, glob_pattern):
            raise Exception("sandbox error")
        sandbox.find_files = mock_find_files
        flow = self._build_flow(sandbox=sandbox)
        result = asyncio.get_event_loop().run_until_complete(flow._scan_sandbox_deliverables())
        assert result == []

    def test_scan_deduplicates(self):
        """扫描应去重(多个glob命中同一文件)"""
        sandbox = MagicMock()
        async def mock_find_files(dir_path, glob_pattern):
            # *.docx 和 *.doc 都返回同一文件(假设扩展名重叠场景)
            if glob_pattern in ("*.docx", "*.doc"):
                return ToolResult(success=True, data=["/home/ubuntu/report.docx"])
            return ToolResult(success=True, data=[])
        sandbox.find_files = mock_find_files
        flow = self._build_flow(sandbox=sandbox)
        result = asyncio.get_event_loop().run_until_complete(flow._scan_sandbox_deliverables())
        assert result.count("/home/ubuntu/report.docx") == 1

    def test_scan_max_files_limit(self):
        """扫描结果应受_SANDBOX_SCAN_MAX_FILES上限约束"""
        from app.domain.services.flows.planner_react import _SANDBOX_SCAN_MAX_FILES
        sandbox = MagicMock()
        async def mock_find_files(dir_path, glob_pattern):
            # 返回超过上限的文件数
            return ToolResult(success=True, data=[
                f"/home/ubuntu/file_{i}.docx" for i in range(_SANDBOX_SCAN_MAX_FILES + 10)
            ])
        sandbox.find_files = mock_find_files
        flow = self._build_flow(sandbox=sandbox)
        result = asyncio.get_event_loop().run_until_complete(flow._scan_sandbox_deliverables())
        assert len(result) <= _SANDBOX_SCAN_MAX_FILES

    def test_scan_single_glob_failure_skipped(self):
        """单个glob失败应跳过(降级),不影响其他glob"""
        sandbox = MagicMock()
        call_count = {"n": 0}
        async def mock_find_files(dir_path, glob_pattern):
            call_count["n"] += 1
            if glob_pattern == "*.xlsx":
                raise Exception("xlsx glob error")
            if glob_pattern == "*.docx":
                return ToolResult(success=True, data=["/home/ubuntu/report.docx"])
            return ToolResult(success=True, data=[])
        sandbox.find_files = mock_find_files
        flow = self._build_flow(sandbox=sandbox)
        result = asyncio.get_event_loop().run_until_complete(flow._scan_sandbox_deliverables())
        # xlsx失败但docx成功,应返回docx
        assert "/home/ubuntu/report.docx" in result


# ============================================================
# AgentTaskRunner 处理 SandboxScanEvent
# ============================================================

class TestSandboxScanEventHandling:
    """P0-1: AgentTaskRunner._handle_sandbox_scan_event 并发同步"""

    def _build_runner(self, sync_results=None):
        """构建AgentTaskRunner实例(绕过__init__)用于测试_handle_sandbox_scan_event"""
        from app.domain.services.agent_task_runner import AgentTaskRunner
        runner = object.__new__(AgentTaskRunner)
        runner._session_id = "test_session"

        synced_files = []
        async def mock_sync(filepath, max_retries=2):
            synced_files.append(filepath)
            if sync_results is not None:
                return sync_results.get(filepath)
            return MagicMock()
        runner._sync_file_to_storage = mock_sync
        return runner, synced_files

    def test_handles_scan_event_syncs_all_files(self):
        """收到SandboxScanEvent应并发同步所有文件"""
        runner, synced = self._build_runner()
        event = SandboxScanEvent(file_paths=["/home/ubuntu/a.docx", "/home/ubuntu/b.xlsx"])
        asyncio.get_event_loop().run_until_complete(runner._handle_sandbox_scan_event(event))
        assert "/home/ubuntu/a.docx" in synced
        assert "/home/ubuntu/b.xlsx" in synced

    def test_handles_empty_file_paths(self):
        """file_paths为空时应直接返回(不调用同步)"""
        runner, synced = self._build_runner()
        event = SandboxScanEvent(file_paths=[])
        asyncio.get_event_loop().run_until_complete(runner._handle_sandbox_scan_event(event))
        assert synced == []

    def test_handles_sync_failure_continues(self):
        """单个文件同步失败不应影响其他文件(gather return_exceptions)"""
        runner, synced = self._build_runner()
        # 第一个文件同步抛异常,第二个正常
        async def mock_sync(filepath, max_retries=2):
            synced.append(filepath)
            if filepath == "/home/ubuntu/a.docx":
                raise Exception("sync failed")
            return MagicMock()
        runner._sync_file_to_storage = mock_sync
        event = SandboxScanEvent(file_paths=["/home/ubuntu/a.docx", "/home/ubuntu/b.xlsx"])
        # 不应抛异常
        asyncio.get_event_loop().run_until_complete(runner._handle_sandbox_scan_event(event))
        assert len(synced) == 2  # 两个文件都尝试同步
