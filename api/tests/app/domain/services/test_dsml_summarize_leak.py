#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_dsml_summarize_leak.py
DSML泄露与0B空文件修复的单元测试

覆盖两个核心修复:
1. OpenAILLM._normalize_dsml_response 在tools=None时(summarize场景)清理DSML标记
   根因: summarize调用LLM时tools_enabled=False,旧逻辑因`not tools`提前返回,
   DSML标记直接泄漏到最终MessageEvent(is_final=True)
2. AgentTaskRunner._sync_message_attachments_to_storage 过滤同步失败的0B占位文件
   根因: _sync_file_to_storage返回None时,占位File(size=0,filename="")被保留,
   前端渲染为"· 0 B"空文件条目
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.models.event import MessageEvent
from app.domain.models.file import File
from app.infrastructure.external.llm.openai_llm import OpenAILLM

# 全角竖线U+FF5C,DeepSeek DSML标记的分隔符
_DSML = "\uff5c"
_D = _DSML * 2


def _make_dsml_tool_calls(function_name: str, params: dict) -> str:
    """构造完整的DSML tool_calls块(模拟DeepSeek异常输出)"""
    param_parts = []
    for pname, pval in params.items():
        param_parts.append(
            f"<{_D}DSML{_D}parameter name=\"{pname}\" string=\"true\">"
            f"{pval}</{_D}DSML{_D}parameter>"
        )
    invoke = f"<{_D}DSML{_D}invoke name=\"{function_name}\">{''.join(param_parts)}</{_D}DSML{_D}invoke>"
    return f"<{_D}DSML{_D}tool_calls>{invoke}</{_D}DSML{_D}tool_calls>"


class TestNormalizeDsmlResponseSummarizeScenario:
    """_normalize_dsml_response在summarize场景(tools=None)的DSML清理测试

    根因: 旧逻辑 `if not content or not tools: return message` 导致
    summarize场景(tools_enabled=False→tools=None)下DSML标记未被清理。
    """

    def test_strips_dsml_when_tools_is_none(self):
        """tools=None(summarize场景)时应清理DSML标记,防止泄漏到用户输出"""
        dsml_content = _make_dsml_tool_calls("shell_execute", {
            "command": "ls -lh /home/ubuntu/",
            "exec_dir": "/home/ubuntu",
        })
        message = {"role": "assistant", "content": dsml_content}

        result = OpenAILLM._normalize_dsml_response(message, tools=None)

        assert "DSML" not in result["content"]
        assert "shell_execute" not in result["content"]
        assert result.get("tool_calls") is None

    def test_strips_dsml_when_tools_is_empty_list(self):
        """tools为空列表时也应清理DSML标记"""
        dsml_content = _make_dsml_tool_calls("read_file", {"filepath": "/a.txt"})
        message = {"role": "assistant", "content": dsml_content}

        result = OpenAILLM._normalize_dsml_response(message, tools=[])

        assert "DSML" not in result["content"]

    def test_preserves_normal_text_when_tools_none(self):
        """tools=None时正常文本(无DSML)应原样返回"""
        message = {"role": "assistant", "content": "任务已完成,报告已生成。"}

        result = OpenAILLM._normalize_dsml_response(message, tools=None)

        assert result["content"] == "任务已完成,报告已生成。"

    def test_strips_dsml_preserving_surrounding_text(self):
        """tools=None时DSML块前后的正常文本应保留"""
        dsml = _make_dsml_tool_calls("get_skill_guide", {"skill": "docx"})
        message = {"role": "assistant", "content": f"分析完成。{dsml} 报告已生成。"}

        result = OpenAILLM._normalize_dsml_response(message, tools=None)

        assert "分析完成" in result["content"]
        assert "报告已生成" in result["content"]
        assert "DSML" not in result["content"]

    def test_strips_dsml_only_content_returns_empty_string(self):
        """tools=None时纯DSML内容清理后应为空字符串"""
        dsml = _make_dsml_tool_calls("shell_execute", {"command": "ls"})
        message = {"role": "assistant", "content": dsml}

        result = OpenAILLM._normalize_dsml_response(message, tools=None)

        assert result["content"] == ""

    def test_does_not_create_tool_calls_when_tools_none(self):
        """tools=None时不应将DSML解析为tool_calls(summarize不执行工具)"""
        dsml = _make_dsml_tool_calls("shell_execute", {"command": "ls"})
        message = {"role": "assistant", "content": dsml}

        result = OpenAILLM._normalize_dsml_response(message, tools=None)

        assert "tool_calls" not in result or result["tool_calls"] is None

    def test_none_content_returns_unchanged(self):
        """content为None时应原样返回(不抛异常)"""
        message = {"role": "assistant", "content": None}

        result = OpenAILLM._normalize_dsml_response(message, tools=None)

        assert result["content"] is None

    def test_empty_content_returns_unchanged(self):
        """content为空字符串时应原样返回"""
        message = {"role": "assistant", "content": ""}

        result = OpenAILLM._normalize_dsml_response(message, tools=None)

        assert result["content"] == ""


class TestNormalizeDsmlResponseToolCallScenario:
    """_normalize_dsml_response在工具调用场景(tools非空)的行为测试"""

    def test_parses_dsml_to_tool_calls_when_tools_provided(self):
        """tools非空时应将DSML解析为标准tool_calls"""
        dsml = _make_dsml_tool_calls("read_file", {"filepath": "/home/ubuntu/data.txt"})
        message = {"role": "assistant", "content": dsml}
        tools = [{"type": "function", "function": {"name": "read_file"}}]

        result = OpenAILLM._normalize_dsml_response(message, tools=tools)

        assert result.get("tool_calls") is not None
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["function"]["name"] == "read_file"

    def test_does_not_override_existing_tool_calls(self):
        """已有标准tool_calls时不应DSML解析,但仍清理content中残余DSML"""
        dsml = _make_dsml_tool_calls("read_file", {"filepath": "/a.txt"})
        existing_tool_calls = [{"id": "call_123", "type": "function",
                                "function": {"name": "shell_execute", "arguments": "{}"}}]
        message = {"role": "assistant", "content": dsml, "tool_calls": existing_tool_calls}
        tools = [{"type": "function", "function": {"name": "read_file"}}]

        result = OpenAILLM._normalize_dsml_response(message, tools=tools)

        assert result["tool_calls"] == existing_tool_calls
        assert "DSML" not in result.get("content", "")

    def test_normal_content_unchanged_when_tools_provided(self):
        """tools非空但content无DSML时应原样返回"""
        message = {"role": "assistant", "content": "正常回复"}
        tools = [{"type": "function", "function": {"name": "read_file"}}]

        result = OpenAILLM._normalize_dsml_response(message, tools=tools)

        assert result["content"] == "正常回复"
        assert "tool_calls" not in result or result.get("tool_calls") is None


class TestSyncMessageAttachmentsFiltersZeroByteFiles:
    """_sync_message_attachments_to_storage过滤0B占位文件测试

    根因: _sync_file_to_storage返回None(0B文件/沙箱中不存在)时,
    旧逻辑保留占位File(filepath=fp, size=0, filename=""),前端渲染为"· 0 B"。
    修复: 同步失败的附件被过滤丢弃,不展示给用户。
    """

    @pytest.mark.asyncio
    async def test_filters_out_zero_byte_attachments(self):
        """0B文件的占位File应被过滤,不出现在最终attachments中"""
        from app.domain.services.agent_task_runner import AgentTaskRunner

        with patch.object(AgentTaskRunner, "__init__", lambda self, **kw: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._uow = AsyncMock()
            runner._uow_factory = lambda: runner._uow
            runner._session_id = "test-session"
            runner._file_storage = AsyncMock()
            runner._sandbox = AsyncMock()

            call_count = 0

            async def mock_sync(filepath, max_retries=2):
                nonlocal call_count
                call_count += 1
                if filepath == "/home/ubuntu/empty.txt":
                    return None
                return File(filepath=filepath, filename="report.md", size=1024)

            runner._sync_file_to_storage = mock_sync

            event = MessageEvent(
                role="assistant",
                message="任务完成",
                attachments=[
                    File(filepath="/home/ubuntu/report.md"),
                    File(filepath="/home/ubuntu/empty.txt"),
                ],
                is_final=True,
            )

            await runner._sync_message_attachments_to_storage(event)

            assert len(event.attachments) == 1
            assert event.attachments[0].filepath == "/home/ubuntu/report.md"
            assert event.attachments[0].size == 1024
            assert call_count == 2

    @pytest.mark.asyncio
    async def test_filters_out_all_when_all_syncs_fail(self):
        """所有附件同步失败时attachments应为空列表"""
        from app.domain.services.agent_task_runner import AgentTaskRunner

        with patch.object(AgentTaskRunner, "__init__", lambda self, **kw: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._uow = AsyncMock()
            runner._uow_factory = lambda: runner._uow
            runner._session_id = "test-session"

            async def mock_sync(filepath, max_retries=2):
                return None

            runner._sync_file_to_storage = mock_sync

            event = MessageEvent(
                role="assistant",
                message="任务完成",
                attachments=[
                    File(filepath="/home/ubuntu/a.txt"),
                    File(filepath="/home/ubuntu/b.txt"),
                ],
                is_final=True,
            )

            await runner._sync_message_attachments_to_storage(event)

            assert event.attachments == []

    @pytest.mark.asyncio
    async def test_preserves_successfully_synced_files(self):
        """同步成功的文件应保留在attachments中,携带完整元数据"""
        from app.domain.services.agent_task_runner import AgentTaskRunner

        with patch.object(AgentTaskRunner, "__init__", lambda self, **kw: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._uow = AsyncMock()
            runner._uow_factory = lambda: runner._uow
            runner._session_id = "test-session"

            async def mock_sync(filepath, max_retries=2):
                return File(
                    filepath=filepath,
                    filename="data.xlsx",
                    size=2048,
                    extension="xlsx",
                    sync_status="SYNCED",
                )

            runner._sync_file_to_storage = mock_sync

            event = MessageEvent(
                role="assistant",
                message="任务完成",
                attachments=[File(filepath="/home/ubuntu/data.xlsx")],
                is_final=True,
            )

            await runner._sync_message_attachments_to_storage(event)

            assert len(event.attachments) == 1
            assert event.attachments[0].filename == "data.xlsx"
            assert event.attachments[0].size == 2048
            assert event.attachments[0].sync_status == "SYNCED"

    @pytest.mark.asyncio
    async def test_empty_attachments_noop(self):
        """空attachments列表时应直接返回,不报错"""
        from app.domain.services.agent_task_runner import AgentTaskRunner

        with patch.object(AgentTaskRunner, "__init__", lambda self, **kw: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)

            event = MessageEvent(
                role="assistant",
                message="任务完成",
                attachments=[],
                is_final=True,
            )

            await runner._sync_message_attachments_to_storage(event)

            assert event.attachments == []

    @pytest.mark.asyncio
    async def test_exception_during_sync_does_not_crash(self):
        """单个附件同步抛异常时不应崩溃,该附件被过滤"""
        from app.domain.services.agent_task_runner import AgentTaskRunner

        with patch.object(AgentTaskRunner, "__init__", lambda self, **kw: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._uow = AsyncMock()
            runner._uow_factory = lambda: runner._uow
            runner._session_id = "test-session"

            async def mock_sync(filepath, max_retries=2):
                if filepath == "/crash.txt":
                    raise RuntimeError("sandbox连接失败")
                return File(filepath=filepath, filename="ok.md", size=100)

            runner._sync_file_to_storage = mock_sync

            event = MessageEvent(
                role="assistant",
                message="任务完成",
                attachments=[
                    File(filepath="/home/ubuntu/ok.md"),
                    File(filepath="/crash.txt"),
                ],
                is_final=True,
            )

            await runner._sync_message_attachments_to_storage(event)

            assert len(event.attachments) == 1
            assert event.attachments[0].filepath == "/home/ubuntu/ok.md"


# 注: TestSummarizePromptDeliverableFiltering 测试类已移除
# 设计理念(参考5b54ddc): SUMMARIZE_PROMPT 不做交付物筛选约束,
# 信任 LLM 基于用户原始需求判断哪些文件是交付物(如"写个爬虫脚本"时 .py 就是交付物)。
# 代码层 _is_temp_file + _get_relevant_files 已做最小化过滤(.tmp/.log/.pyc 等中间产物),
# 详见 test_deliverable_selection.py 覆盖代码层筛选逻辑。
