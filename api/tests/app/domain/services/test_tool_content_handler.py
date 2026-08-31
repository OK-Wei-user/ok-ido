#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_tool_content_handler.py
工具事件内容处理单元测试 - 深度研究内容映射、搜索结果正文剥离
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.models.event import (
    ToolEvent, ToolEventStatus,
    DeepResearchToolContent, SearchToolContent,
)
from app.domain.models.research import ResearchSummary, ResearchInsight
from app.domain.models.search import SearchResultItem, SearchResults
from app.domain.models.tool_result import ToolResult
from app.domain.services.agent_task_runner import AgentTaskRunner


async def _async_iter(items):
    """辅助函数：将列表转为异步迭代器"""
    for item in items:
        yield item


def _make_research_summary() -> ResearchSummary:
    """构造测试用研究摘要"""
    return ResearchSummary(
        query="测试主题",
        key_findings=[ResearchInsight(
            content="核心发现", source_url="https://a.com",
            source_title="A", relevance_score=0.9,
        )],
        additional_findings=[ResearchInsight(
            content="补充发现", source_url="https://b.com",
            source_title="B", relevance_score=0.6,
        )],
        supplementary=[ResearchInsight(
            content="参考信息", source_url="https://c.com",
            source_title="C", relevance_score=0.3,
        )],
        follow_up_queries=["后续查询1"],
        total_sources=3,
    )


def _make_search_results(with_content: bool = True) -> SearchResults:
    """构造测试用搜索结果"""
    return SearchResults(
        query="测试搜索",
        results=[
            SearchResultItem(
                url="https://example.com/1",
                title="结果1",
                snippet="摘要1",
                dedup_key="https://example.com/1",
                content="很长的正文内容..." * 100 if with_content else None,
            ),
            SearchResultItem(
                url="https://example.com/2",
                title="结果2",
                snippet="摘要2",
                dedup_key="https://example.com/2",
                content="另一段很长的正文..." * 100 if with_content else None,
            ),
        ],
    )


class TestDeepResearchToolContent:
    """DeepResearchToolContent 模型测试"""

    def test_create_with_summary(self):
        summary = _make_research_summary()
        content = DeepResearchToolContent(summary=summary)
        assert content.summary.query == "测试主题"
        assert len(content.summary.key_findings) == 1
        assert content.summary.total_sources == 3

    def test_serialize_to_json(self):
        summary = _make_research_summary()
        content = DeepResearchToolContent(summary=summary)
        data = content.model_dump(mode="json")
        assert "summary" in data
        assert data["summary"]["query"] == "测试主题"
        assert len(data["summary"]["key_findings"]) == 1
        assert data["summary"]["key_findings"][0]["relevance_score"] == 0.9

    def test_with_empty_summary(self):
        summary = ResearchSummary(query="空主题")
        content = DeepResearchToolContent(summary=summary)
        assert content.summary.key_findings == []
        assert content.summary.total_sources == 0


class TestSearchContentStripping:
    """搜索结果正文剥离测试"""

    def test_search_results_content_stripped(self):
        """验证构建SearchToolContent时content字段被剥离"""
        search_results = _make_search_results(with_content=True)
        # 模拟agent_task_runner中的剥离逻辑
        display_results = [
            item.model_copy(update={"content": None})
            for item in search_results.results
        ]
        content = SearchToolContent(results=display_results)
        for item in content.results:
            assert item.content is None
            assert item.url is not None
            assert item.title is not None
            assert item.snippet is not None

    def test_original_results_unchanged(self):
        """验证剥离不影响原始结果对象"""
        search_results = _make_search_results(with_content=True)
        original_content = search_results.results[0].content
        # 执行剥离
        display_results = [
            item.model_copy(update={"content": None})
            for item in search_results.results
        ]
        # 原始对象的content应保持不变
        assert search_results.results[0].content == original_content
        assert display_results[0].content is None

    def test_stripped_results_preserve_display_fields(self):
        """验证剥离后保留前端展示所需字段"""
        search_results = _make_search_results(with_content=True)
        display_results = [
            item.model_copy(update={"content": None})
            for item in search_results.results
        ]
        for original, display in zip(search_results.results, display_results):
            assert display.url == original.url
            assert display.title == original.title
            assert display.snippet == original.snippet
            assert display.dedup_key == original.dedup_key


class TestDeepResearchHandler:
    """deep_research 工具事件处理器测试"""

    @pytest.mark.asyncio
    async def test_deep_research_maps_summary(self):
        """验证deep_research工具结果正确映射为DeepResearchToolContent"""
        summary = _make_research_summary()
        event = ToolEvent(
            tool_call_id="call_test",
            tool_name="deep_research",
            function_name="deep_research",
            function_args={"query": "测试主题"},
            function_result=ToolResult(success=True, data=summary),
            status=ToolEventStatus.CALLED,
        )
        # 调用_handle_tool_event（deep_research分支不访问self属性）
        await AgentTaskRunner._handle_tool_event(MagicMock(), event)
        assert event.tool_content is not None
        assert isinstance(event.tool_content, DeepResearchToolContent)
        assert event.tool_content.summary.query == "测试主题"
        assert len(event.tool_content.summary.key_findings) == 1

    @pytest.mark.asyncio
    async def test_deep_research_with_no_result(self):
        """验证deep_research无结果时返回空摘要"""
        event = ToolEvent(
            tool_call_id="call_test",
            tool_name="deep_research",
            function_name="deep_research",
            function_args={"query": "测试主题"},
            function_result=None,
            status=ToolEventStatus.CALLED,
        )
        await AgentTaskRunner._handle_tool_event(MagicMock(), event)
        assert event.tool_content is not None
        assert isinstance(event.tool_content, DeepResearchToolContent)
        assert event.tool_content.summary.query == "测试主题"
        assert event.tool_content.summary.key_findings == []

    @pytest.mark.asyncio
    async def test_deep_research_with_failed_result(self):
        """验证deep_research失败结果时返回空摘要"""
        event = ToolEvent(
            tool_call_id="call_test",
            tool_name="deep_research",
            function_name="deep_research",
            function_args={"query": "测试主题"},
            function_result=ToolResult(success=False, message="搜索失败"),
            status=ToolEventStatus.CALLED,
        )
        await AgentTaskRunner._handle_tool_event(MagicMock(), event)
        # success=False但无data属性 → 走else分支
        assert event.tool_content is not None
        assert isinstance(event.tool_content, DeepResearchToolContent)


class TestSearchHandler:
    """search 工具事件处理器测试"""

    @pytest.mark.asyncio
    async def test_search_strips_content_field(self):
        """验证search工具结果content字段被剥离"""
        search_results = SearchResults(
            query="测试",
            results=[
                SearchResultItem(
                    url="https://a.com", title="A", snippet="摘要A",
                    dedup_key="https://a.com", content="x" * 10000,
                ),
            ],
        )
        event = ToolEvent(
            tool_call_id="call_test",
            tool_name="search",
            function_name="search_web",
            function_args={"query": "测试"},
            function_result=ToolResult(success=True, data=search_results),
            status=ToolEventStatus.CALLED,
        )
        await AgentTaskRunner._handle_tool_event(MagicMock(), event)
        assert event.tool_content is not None
        assert isinstance(event.tool_content, SearchToolContent)
        # content字段应被剥离
        for item in event.tool_content.results:
            assert item.content is None
            assert item.url == "https://a.com"
            assert item.title == "A"

    @pytest.mark.asyncio
    async def test_search_with_no_content(self):
        """验证search结果原本无content时正常处理"""
        search_results = SearchResults(
            query="测试",
            results=[
                SearchResultItem(
                    url="https://b.com", title="B", snippet="摘要B",
                    dedup_key="https://b.com", content=None,
                ),
            ],
        )
        event = ToolEvent(
            tool_call_id="call_test",
            tool_name="search",
            function_name="search_web",
            function_args={"query": "测试"},
            function_result=ToolResult(success=True, data=search_results),
            status=ToolEventStatus.CALLED,
        )
        await AgentTaskRunner._handle_tool_event(MagicMock(), event)
        assert event.tool_content is not None
        assert len(event.tool_content.results) == 1
        assert event.tool_content.results[0].content is None

    @pytest.mark.asyncio
    async def test_search_with_no_result(self):
        """验证search无结果时返回空列表"""
        event = ToolEvent(
            tool_call_id="call_test",
            tool_name="search",
            function_name="search_web",
            function_args={"query": "测试"},
            function_result=None,
            status=ToolEventStatus.CALLED,
        )
        await AgentTaskRunner._handle_tool_event(MagicMock(), event)
        assert event.tool_content is not None
        assert isinstance(event.tool_content, SearchToolContent)
        assert event.tool_content.results == []

    @pytest.mark.asyncio
    async def test_search_with_failed_result(self):
        """验证search失败时返回空列表"""
        event = ToolEvent(
            tool_call_id="call_test",
            tool_name="search",
            function_name="search_web",
            function_args={"query": "测试"},
            function_result=ToolResult(success=False, message="搜索失败"),
            status=ToolEventStatus.CALLED,
        )
        await AgentTaskRunner._handle_tool_event(MagicMock(), event)
        assert event.tool_content is not None
        assert isinstance(event.tool_content, SearchToolContent)
        assert event.tool_content.results == []


class TestStepAttachmentsSync:
    """_run_flow 中 step.attachments 自动同步测试"""

    @pytest.mark.asyncio
    async def test_step_completed_syncs_attachments(self):
        """步骤完成时自动同步 attachments 到存储"""
        from app.domain.models.plan import Step
        from app.domain.models.event import StepEvent, StepEventStatus
        from app.domain.models.message import Message

        step = Step(description="生成报告", attachments=["/home/ubuntu/report.docx"])
        event = StepEvent(step=step, status=StepEventStatus.COMPLETED)

        mock_runner = MagicMock()
        mock_runner._flow = MagicMock()
        mock_runner._flow.invoke = MagicMock(return_value=_async_iter([event]))
        mock_runner._sync_file_to_storage = AsyncMock(return_value=MagicMock())

        results = []
        async for e in AgentTaskRunner._run_flow(mock_runner, Message(message="test")):
            results.append(e)

        mock_runner._sync_file_to_storage.assert_called_once_with("/home/ubuntu/report.docx")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_step_started_does_not_sync(self):
        """步骤开始时不触发同步"""
        from app.domain.models.plan import Step
        from app.domain.models.event import StepEvent, StepEventStatus
        from app.domain.models.message import Message

        step = Step(description="生成报告", attachments=["/home/ubuntu/report.docx"])
        event = StepEvent(step=step, status=StepEventStatus.STARTED)

        mock_runner = MagicMock()
        mock_runner._flow = MagicMock()
        mock_runner._flow.invoke = MagicMock(return_value=_async_iter([event]))
        mock_runner._sync_file_to_storage = AsyncMock()

        results = []
        async for e in AgentTaskRunner._run_flow(mock_runner, Message(message="test")):
            results.append(e)

        mock_runner._sync_file_to_storage.assert_not_called()

    @pytest.mark.asyncio
    async def test_step_empty_attachments_does_not_sync(self):
        """步骤无附件时不触发同步"""
        from app.domain.models.plan import Step
        from app.domain.models.event import StepEvent, StepEventStatus
        from app.domain.models.message import Message

        step = Step(description="分析数据", attachments=[])
        event = StepEvent(step=step, status=StepEventStatus.COMPLETED)

        mock_runner = MagicMock()
        mock_runner._flow = MagicMock()
        mock_runner._flow.invoke = MagicMock(return_value=_async_iter([event]))
        mock_runner._sync_file_to_storage = AsyncMock()

        results = []
        async for e in AgentTaskRunner._run_flow(mock_runner, Message(message="test")):
            results.append(e)

        mock_runner._sync_file_to_storage.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_attachments_all_synced(self):
        """多个附件全部同步"""
        from app.domain.models.plan import Step
        from app.domain.models.event import StepEvent, StepEventStatus
        from app.domain.models.message import Message

        paths = ["/home/ubuntu/report.docx", "/home/ubuntu/data.xlsx", "/home/ubuntu/chart.png"]
        step = Step(description="生成多文件", attachments=paths)
        event = StepEvent(step=step, status=StepEventStatus.COMPLETED)

        mock_runner = MagicMock()
        mock_runner._flow = MagicMock()
        mock_runner._flow.invoke = MagicMock(return_value=_async_iter([event]))
        mock_runner._sync_file_to_storage = AsyncMock(return_value=MagicMock())

        results = []
        async for e in AgentTaskRunner._run_flow(mock_runner, Message(message="test")):
            results.append(e)

        assert mock_runner._sync_file_to_storage.call_count == 3
        called_paths = [call.args[0] for call in mock_runner._sync_file_to_storage.call_args_list]
        assert set(called_paths) == set(paths)

    @pytest.mark.asyncio
    async def test_sync_failure_does_not_block_flow(self):
        """单个附件同步失败不阻断流程"""
        from app.domain.models.plan import Step
        from app.domain.models.event import StepEvent, StepEventStatus
        from app.domain.models.message import Message

        step = Step(
            description="生成文件",
            attachments=["/home/ubuntu/file1.docx", "/home/ubuntu/file2.xlsx"],
        )
        event = StepEvent(step=step, status=StepEventStatus.COMPLETED)

        mock_runner = MagicMock()
        mock_runner._flow = MagicMock()
        mock_runner._flow.invoke = MagicMock(return_value=_async_iter([event]))
        mock_runner._sync_file_to_storage = AsyncMock(side_effect=[None, MagicMock()])

        results = []
        async for e in AgentTaskRunner._run_flow(mock_runner, Message(message="test")):
            results.append(e)

        assert mock_runner._sync_file_to_storage.call_count == 2
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_empty_string_in_attachments_skipped(self):
        """attachments 中的空字符串被跳过"""
        from app.domain.models.plan import Step
        from app.domain.models.event import StepEvent, StepEventStatus
        from app.domain.models.message import Message

        step = Step(description="测试空路径", attachments=["", "/home/ubuntu/valid.docx"])
        event = StepEvent(step=step, status=StepEventStatus.COMPLETED)

        mock_runner = MagicMock()
        mock_runner._flow = MagicMock()
        mock_runner._flow.invoke = MagicMock(return_value=_async_iter([event]))
        mock_runner._sync_file_to_storage = AsyncMock(return_value=MagicMock())

        results = []
        async for e in AgentTaskRunner._run_flow(mock_runner, Message(message="test")):
            results.append(e)

        mock_runner._sync_file_to_storage.assert_called_once_with("/home/ubuntu/valid.docx")


def _make_uow_mock(session_mock):
    """构造支持 async with 的 UoW mock"""
    uow = MagicMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    uow.session = session_mock
    return uow


class TestSyncFileToStorageDedup:
    """_sync_file_to_storage 去重逻辑测试

    验证成功路径调用 remove_files_by_path 传入 filepath（原子移除同路径旧文件），
    确保 session.files 不会出现重复记录。remove_file(按id) 不应在成功路径被调用。
    """

    @pytest.mark.asyncio
    async def test_remove_files_by_path_called_when_exists(self):
        """文件已存在时，remove_files_by_path 传入 filepath 移除所有同路径旧记录"""
        import io
        from app.domain.models.file import File

        session_mock = AsyncMock()
        session_mock.remove_files_by_path = AsyncMock(return_value=1)
        session_mock.add_file = AsyncMock()
        session_mock.remove_file = AsyncMock()

        new_file = File(id="new-uuid", filepath="", filename="report.docx", key="oss-key")
        sandbox_mock = AsyncMock()
        sandbox_mock.get_file_size = AsyncMock(return_value=1024)
        sandbox_mock.download_file = AsyncMock(return_value=io.BytesIO(b"content"))

        file_storage_mock = AsyncMock()
        file_storage_mock.upload_file = AsyncMock(return_value=new_file)

        runner = MagicMock()
        runner._uow = _make_uow_mock(session_mock)
        runner._uow_factory = lambda: runner._uow
        runner._sandbox = sandbox_mock
        runner._file_storage = file_storage_mock
        runner._session_id = "test-session"
        runner._get_stream_size = MagicMock(return_value=7)

        result = await AgentTaskRunner._sync_file_to_storage(runner, "/home/ubuntu/report.docx")

        session_mock.remove_files_by_path.assert_called_once_with("test-session", "/home/ubuntu/report.docx")
        session_mock.remove_file.assert_not_called()
        session_mock.add_file.assert_called_once()
        assert result is not None
        assert result.filepath == "/home/ubuntu/report.docx"

    @pytest.mark.asyncio
    async def test_remove_files_by_path_always_called_even_when_not_exists(self):
        """文件不存在时，remove_files_by_path 仍被调用（返回0），add_file 正常添加"""
        import io
        from app.domain.models.file import File

        session_mock = AsyncMock()
        session_mock.remove_files_by_path = AsyncMock(return_value=0)
        session_mock.add_file = AsyncMock()
        session_mock.remove_file = AsyncMock()

        new_file = File(id="new-uuid", filepath="", filename="report.docx", key="oss-key")
        sandbox_mock = AsyncMock()
        sandbox_mock.get_file_size = AsyncMock(return_value=1024)
        sandbox_mock.download_file = AsyncMock(return_value=io.BytesIO(b"content"))

        file_storage_mock = AsyncMock()
        file_storage_mock.upload_file = AsyncMock(return_value=new_file)

        runner = MagicMock()
        runner._uow = _make_uow_mock(session_mock)
        runner._uow_factory = lambda: runner._uow
        runner._sandbox = sandbox_mock
        runner._file_storage = file_storage_mock
        runner._session_id = "test-session"
        runner._get_stream_size = MagicMock(return_value=7)

        result = await AgentTaskRunner._sync_file_to_storage(runner, "/home/ubuntu/report.docx")

        session_mock.remove_files_by_path.assert_called_once_with("test-session", "/home/ubuntu/report.docx")
        session_mock.remove_file.assert_not_called()
        session_mock.add_file.assert_called_once()
        assert result is not None

    @pytest.mark.asyncio
    async def test_existing_file_replaced_with_new_upload(self):
        """文件已存在时，remove_files_by_path 移除旧文件后 add_file 添加新文件"""
        import io
        from app.domain.models.file import File

        session_mock = AsyncMock()
        session_mock.remove_files_by_path = AsyncMock(return_value=1)
        session_mock.add_file = AsyncMock()
        session_mock.remove_file = AsyncMock()

        new_file = File(id="new-uuid", filepath="", filename="data.xlsx", key="new-oss-key")
        sandbox_mock = AsyncMock()
        sandbox_mock.get_file_size = AsyncMock(return_value=1024)
        sandbox_mock.download_file = AsyncMock(return_value=io.BytesIO(b"new content"))

        file_storage_mock = AsyncMock()
        file_storage_mock.upload_file = AsyncMock(return_value=new_file)

        runner = MagicMock()
        runner._uow = _make_uow_mock(session_mock)
        runner._uow_factory = lambda: runner._uow
        runner._sandbox = sandbox_mock
        runner._file_storage = file_storage_mock
        runner._session_id = "test-session"
        runner._get_stream_size = MagicMock(return_value=11)

        result = await AgentTaskRunner._sync_file_to_storage(runner, "/home/ubuntu/data.xlsx")

        assert session_mock.remove_files_by_path.call_count == 1
        assert session_mock.remove_file.call_count == 0
        assert session_mock.add_file.call_count == 1
        assert result.id == "new-uuid"
        assert result.filepath == "/home/ubuntu/data.xlsx"
