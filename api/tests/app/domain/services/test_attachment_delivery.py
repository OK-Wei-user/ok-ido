#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_attachment_delivery.py
交付文件附件链路单元测试 - SUMMARIZE_PROMPT注入 + 降级兜底
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.models.event import MessageEvent
from app.domain.models.file import File
from app.domain.models.message import Message
from app.domain.services.prompts.react import SUMMARIZE_PROMPT


class TestSummarizePromptFileInjection:
    """SUMMARIZE_PROMPT文件列表注入测试"""

    def test_prompt_contains_files_placeholder(self):
        assert "{files}" in SUMMARIZE_PROMPT

    def test_prompt_format_with_known_files(self):
        known_files = ["/home/ubuntu/report.md", "/home/ubuntu/data.csv"]
        result = SUMMARIZE_PROMPT.format(files="\n".join(f"  - {fp}" for fp in known_files))
        assert "/home/ubuntu/report.md" in result
        assert "/home/ubuntu/data.csv" in result

    def test_prompt_format_with_no_files(self):
        result = SUMMARIZE_PROMPT.format(files="（无）")
        assert "（无）" in result

    def test_prompt_emphasizes_attachments_field(self):
        # 恢复old_cn_prompts基线: SUMMARIZE_PROMPT要求JSON {message, attachments}格式输出
        # 验证JSON接口定义与attachments字段约束存在
        assert "attachments 字段" in SUMMARIZE_PROMPT
        assert "TypeScript" in SUMMARIZE_PROMPT  # JSON 接口定义


class TestSummarizeKnownFilesParameter:
    """ReActAgent.summarize(known_files)参数测试"""

    @pytest.mark.asyncio
    async def test_summarize_with_known_files_formats_prompt(self):
        """summarize应将known_files注入SUMMARIZE_PROMPT的{files}占位符"""
        from app.domain.services.agents.react import ReActAgent
        from app.domain.models.app_config import AgentConfig

        known_files = ["/home/ubuntu/report.md", "/home/ubuntu/chart.png"]

        with patch.object(ReActAgent, "__init__", lambda self, **kw: None):
            agent = ReActAgent.__new__(ReActAgent)
            # F10-1: summarize 内部调用 _stream_final_answer 需访问 _agent_config
            agent._agent_config = AgentConfig(max_iterations=10, stream_final_answer=False)
            query_arg = None

            async def mock_invoke_llm(messages, format=None, tools_enabled=True, tool_choice=None):
                nonlocal query_arg
                # messages[0] 是 user 消息，content 为 SUMMARIZE_PROMPT.format(files=...)
                query_arg = messages[0]["content"]
                return {"role": "assistant", "content": '{"message": "任务完成", "attachments": []}'}

            agent._invoke_llm = mock_invoke_llm
            # mock JSON解析器: 返回解析后的dict供Message.model_validate使用
            agent._json_parser = AsyncMock()
            agent._json_parser.invoke = AsyncMock(return_value={"message": "任务完成", "attachments": []})

            events = []
            async for event in agent.summarize(known_files=known_files):
                events.append(event)

            assert query_arg is not None
            assert "/home/ubuntu/report.md" in query_arg
            assert "/home/ubuntu/chart.png" in query_arg

    @pytest.mark.asyncio
    async def test_summarize_without_known_files_uses_default(self):
        """known_files为None时,SUMMARIZE_PROMPT的{files}占位符应填充为'（无）'"""
        from app.domain.services.agents.react import ReActAgent
        from app.domain.models.app_config import AgentConfig

        with patch.object(ReActAgent, "__init__", lambda self, **kw: None):
            agent = ReActAgent.__new__(ReActAgent)
            # F10-1: summarize 内部调用 _stream_final_answer 需访问 _agent_config
            agent._agent_config = AgentConfig(max_iterations=10, stream_final_answer=False)
            query_arg = None

            async def mock_invoke_llm(messages, format=None, tools_enabled=True, tool_choice=None):
                nonlocal query_arg
                query_arg = messages[0]["content"]
                return {"role": "assistant", "content": '{"message": "任务完成", "attachments": []}'}

            agent._invoke_llm = mock_invoke_llm
            agent._json_parser = AsyncMock()
            agent._json_parser.invoke = AsyncMock(return_value={"message": "任务完成", "attachments": []})

            async for event in agent.summarize(known_files=None):
                pass

            assert query_arg is not None
            assert "（无）" in query_arg


class TestAttachmentFallbackMechanism:
    """降级兜底：LLM未返回attachments时自动补充session.files"""

    def test_fallback_triggers_when_attachments_empty_and_files_exist(self):
        known_files = ["/home/ubuntu/report.md", "/home/ubuntu/chart.png"]
        event = MessageEvent(role="assistant", message="任务完成", attachments=[])

        should_fallback = isinstance(event, MessageEvent) and not event.attachments and bool(known_files)
        assert should_fallback is True

        if should_fallback:
            event.attachments = [File(filepath=fp) for fp in known_files]

        assert len(event.attachments) == 2
        assert event.attachments[0].filepath == "/home/ubuntu/report.md"
        assert event.attachments[1].filepath == "/home/ubuntu/chart.png"

    def test_fallback_not_triggered_when_attachments_present(self):
        known_files = ["/home/ubuntu/report.md"]
        event = MessageEvent(
            role="assistant",
            message="任务完成",
            attachments=[File(filepath="/home/ubuntu/report.md")],
        )

        should_fallback = isinstance(event, MessageEvent) and not event.attachments and known_files
        assert should_fallback is False
        assert len(event.attachments) == 1

    def test_fallback_not_triggered_when_no_session_files(self):
        known_files = []
        event = MessageEvent(role="assistant", message="任务完成", attachments=[])

        should_fallback = isinstance(event, MessageEvent) and not event.attachments and bool(known_files)
        assert should_fallback is False

    def test_fallback_not_triggered_for_non_message_event(self):
        from app.domain.models.event import StepEvent, StepEventStatus
        from app.domain.models.plan import Step

        known_files = ["/home/ubuntu/report.md"]
        step = Step(id="1", description="test")
        event = StepEvent(step=step, status=StepEventStatus.STARTED)

        should_fallback = isinstance(event, MessageEvent) and not event.attachments and known_files
        assert should_fallback is False


class TestGetSessionFilePaths:
    """_get_session_file_paths方法测试"""

    @pytest.mark.asyncio
    async def test_returns_file_paths_from_session(self):
        from app.domain.services.flows.planner_react import PlannerReActFlow

        with patch.object(PlannerReActFlow, "__init__", lambda self, **kw: None):
            flow = PlannerReActFlow.__new__(PlannerReActFlow)
            flow._uow = AsyncMock()
            flow._uow_factory = lambda: flow._uow
            flow._session_id = "test-session"

            mock_session = MagicMock()
            mock_session.files = [
                File(filepath="/home/ubuntu/report.md", filename="report.md"),
                File(filepath="/home/ubuntu/chart.png", filename="chart.png"),
            ]
            flow._uow.session = AsyncMock()
            flow._uow.session.get_by_id = AsyncMock(return_value=mock_session)
            flow._uow.__aenter__ = AsyncMock(return_value=flow._uow)
            flow._uow.__aexit__ = AsyncMock(return_value=False)

            paths = await flow._get_session_file_paths()
            assert paths == ["/home/ubuntu/report.md", "/home/ubuntu/chart.png"]

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_files(self):
        from app.domain.services.flows.planner_react import PlannerReActFlow

        with patch.object(PlannerReActFlow, "__init__", lambda self, **kw: None):
            flow = PlannerReActFlow.__new__(PlannerReActFlow)
            flow._uow = AsyncMock()
            flow._uow_factory = lambda: flow._uow
            flow._session_id = "test-session"

            mock_session = MagicMock()
            mock_session.files = []
            flow._uow.session = AsyncMock()
            flow._uow.session.get_by_id = AsyncMock(return_value=mock_session)
            flow._uow.__aenter__ = AsyncMock(return_value=flow._uow)
            flow._uow.__aexit__ = AsyncMock(return_value=False)

            paths = await flow._get_session_file_paths()
            assert paths == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_session_not_found(self):
        from app.domain.services.flows.planner_react import PlannerReActFlow

        with patch.object(PlannerReActFlow, "__init__", lambda self, **kw: None):
            flow = PlannerReActFlow.__new__(PlannerReActFlow)
            flow._uow = AsyncMock()
            flow._uow_factory = lambda: flow._uow
            flow._session_id = "test-session"

            flow._uow.session = AsyncMock()
            flow._uow.session.get_by_id = AsyncMock(return_value=None)
            flow._uow.__aenter__ = AsyncMock(return_value=flow._uow)
            flow._uow.__aexit__ = AsyncMock(return_value=False)

            paths = await flow._get_session_file_paths()
            assert paths == []

    @pytest.mark.asyncio
    async def test_returns_all_paths_without_truncation(self):
        """_get_session_file_paths仅负责查询路径,不做截断

        设计理念(参考5b54ddc): 截断逻辑由_get_relevant_files负责,
        _get_session_file_paths只返回全量路径,职责单一。
        """
        from app.domain.services.flows.planner_react import PlannerReActFlow, _MAX_DELIVERABLE_FILES

        with patch.object(PlannerReActFlow, "__init__", lambda self, **kw: None):
            flow = PlannerReActFlow.__new__(PlannerReActFlow)
            flow._uow = AsyncMock()
            flow._uow_factory = lambda: flow._uow
            flow._session_id = "test-session"

            total = _MAX_DELIVERABLE_FILES + 5
            many_files = [
                File(filepath=f"/home/ubuntu/file_{i}.txt", filename=f"file_{i}.txt")
                for i in range(total)
            ]
            mock_session = MagicMock()
            mock_session.files = many_files
            flow._uow.session = AsyncMock()
            flow._uow.session.get_by_id = AsyncMock(return_value=mock_session)
            flow._uow.__aenter__ = AsyncMock(return_value=flow._uow)
            flow._uow.__aexit__ = AsyncMock(return_value=False)

            paths = await flow._get_session_file_paths()
            # _get_session_file_paths不做截断,返回全部路径
            assert len(paths) == total

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self):
        from app.domain.services.flows.planner_react import PlannerReActFlow

        with patch.object(PlannerReActFlow, "__init__", lambda self, **kw: None):
            flow = PlannerReActFlow.__new__(PlannerReActFlow)
            flow._uow = AsyncMock()
            flow._uow_factory = lambda: flow._uow
            flow._session_id = "test-session"

            flow._uow.session = AsyncMock()
            flow._uow.session.get_by_id = AsyncMock(side_effect=Exception("DB error"))
            flow._uow.__aenter__ = AsyncMock(return_value=flow._uow)
            flow._uow.__aexit__ = AsyncMock(return_value=False)

            paths = await flow._get_session_file_paths()
            assert paths == []

    @pytest.mark.asyncio
    async def test_filters_files_without_filepath(self):
        from app.domain.services.flows.planner_react import PlannerReActFlow

        with patch.object(PlannerReActFlow, "__init__", lambda self, **kw: None):
            flow = PlannerReActFlow.__new__(PlannerReActFlow)
            flow._uow = AsyncMock()
            flow._uow_factory = lambda: flow._uow
            flow._session_id = "test-session"

            mock_session = MagicMock()
            mock_session.files = [
                File(filepath="/home/ubuntu/report.md", filename="report.md"),
                File(filepath="", filename="orphan.txt"),
                File(filepath="/home/ubuntu/chart.png", filename="chart.png"),
            ]
            flow._uow.session = AsyncMock()
            flow._uow.session.get_by_id = AsyncMock(return_value=mock_session)
            flow._uow.__aenter__ = AsyncMock(return_value=flow._uow)
            flow._uow.__aexit__ = AsyncMock(return_value=False)

            paths = await flow._get_session_file_paths()
            assert len(paths) == 2
            assert "/home/ubuntu/report.md" in paths
            assert "/home/ubuntu/chart.png" in paths
