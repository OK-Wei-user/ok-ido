#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_streaming_output.py
流式输出恢复单元测试：
- LLMStreamChunk 数据结构
- OpenAILLM.astream 流式分块/异常分类/response_format强制移除
- BaseAgent._invoke_llm_stream 流式调用+记忆管理
- ReActAgent.summarize 流式delta事件+最终答案+降级回退
- AgentTaskRunner delta不写DB+跳过附件同步
- MessageEvent/MessageSSEEvent is_streaming 序列化
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import AsyncGenerator

from app.domain.models.app_config import LLMConfig, ThinkingMode, AgentConfig
from app.domain.models.event import MessageEvent
from app.domain.models.file import File
from app.domain.models.memory import Memory
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.agents.react import ReActAgent
from app.domain.services.tools.base import BaseTool, tool
from app.infrastructure.external.llm.exceptions import RetryableLLMError, NonRetryableLLMError
from app.infrastructure.external.llm.openai_llm import OpenAILLM
from app.infrastructure.external.llm.stream_chunk import LLMStreamChunk
from app.interfaces.schemas.event import MessageEventData, MessageSSEEvent


class _StubTool(BaseTool):
    name: str = "stub"

    @tool(name="stub_action", description="测试工具", parameters={}, required=[])
    async def stub_action(self) -> ToolResult:
        return ToolResult(success=True, data="ok")


def _make_llm() -> OpenAILLM:
    """构造测试用 OpenAILLM 实例（思考模式关闭）"""
    return OpenAILLM(LLMConfig(
        base_url="https://api.deepseek.com",
        api_key="test-key",
        model_name="deepseek-v4-pro",
        thinking_mode=ThinkingMode.DISABLED,
    ))


def _make_stream_chunk(content: str = "", reasoning: str = "", finish: str = None):
    """构造单个流式 chunk mock"""
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    delta = MagicMock()
    delta.content = content if content else None
    delta.reasoning_content = reasoning if reasoning else None
    chunk.choices[0].delta = delta
    chunk.choices[0].finish_reason = finish
    return chunk


def _make_stream_response(chunks):
    """构造流式响应的 async iterator mock"""
    response = MagicMock()
    response.__aiter__ = MagicMock(return_value=_AsyncIter(chunks))
    return response


class _AsyncIter:
    """简易异步迭代器，用于 mock 流式响应"""
    def __init__(self, items):
        self._items = list(items)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


def _build_base_agent() -> BaseAgent:
    """构造测试用 BaseAgent 实例（绕过 __init__）"""
    agent = object.__new__(BaseAgent)
    agent._tools = [_StubTool()]
    agent._agent_config = AgentConfig(max_iterations=5, max_retries=3)
    agent._format = None
    agent._tool_choice = "auto"
    agent._system_prompt = "test"
    agent.name = "test_agent"
    agent._session_id = "test_session"
    agent._uow_factory = lambda: MagicMock()
    agent._uow = MagicMock()
    agent._retry_interval = 0.01
    agent._json_parser = MagicMock()
    agent._json_parser.invoke = AsyncMock(return_value={})
    agent._memory = MagicMock()
    agent._memory.empty = False
    agent._memory.should_compress = MagicMock(return_value=False)
    agent._memory.is_context_overflow = MagicMock(return_value=False)
    agent._memory.check_token_limit = MagicMock(return_value=False)
    agent._memory.get_messages = MagicMock(return_value=[])
    agent._add_to_memory = AsyncMock()
    agent._ensure_memory = AsyncMock()
    agent._token_counter = None
    agent._context_window = 64000
    return agent


class TestLLMStreamChunk:
    """LLMStreamChunk 数据结构测试"""

    def test_default_values(self):
        chunk = LLMStreamChunk()
        assert chunk.delta_content == ""
        assert chunk.delta_reasoning == ""
        assert chunk.finish_reason is None

    def test_with_content(self):
        chunk = LLMStreamChunk(delta_content="hello")
        assert chunk.delta_content == "hello"
        assert chunk.delta_reasoning == ""
        assert chunk.finish_reason is None

    def test_with_reasoning(self):
        chunk = LLMStreamChunk(delta_reasoning="thinking...")
        assert chunk.delta_content == ""
        assert chunk.delta_reasoning == "thinking..."

    def test_with_finish_reason(self):
        chunk = LLMStreamChunk(finish_reason="stop")
        assert chunk.finish_reason == "stop"


class TestOpenAILLMStream:
    """OpenAILLM.astream 流式调用测试"""

    @pytest.mark.asyncio
    async def test_stream_yields_content_chunks(self):
        """正常流式输出应逐块 yield delta_content"""
        llm = _make_llm()
        chunks = [
            _make_stream_chunk(content="hello"),
            _make_stream_chunk(content=" world"),
            _make_stream_chunk(finish="stop"),
        ]
        llm._client = MagicMock()
        llm._client.chat.completions.create = AsyncMock(return_value=_make_stream_response(chunks))

        results = []
        async for chunk in llm.astream([{"role": "user", "content": "hi"}]):
            results.append(chunk)

        assert len(results) == 3
        assert results[0].delta_content == "hello"
        assert results[1].delta_content == " world"
        assert results[2].finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_stream_yields_reasoning_chunks(self):
        """DeepSeek V4 思考模式应 yield delta_reasoning"""
        llm = _make_llm()
        chunks = [
            _make_stream_chunk(reasoning="thinking..."),
            _make_stream_chunk(content="answer"),
        ]
        llm._client = MagicMock()
        llm._client.chat.completions.create = AsyncMock(return_value=_make_stream_response(chunks))

        results = []
        async for chunk in llm.astream([{"role": "user", "content": "hi"}]):
            results.append(chunk)

        assert results[0].delta_reasoning == "thinking..."
        assert results[1].delta_content == "answer"

    @pytest.mark.asyncio
    async def test_stream_skips_empty_chunks(self):
        """空 chunk（keep-alive 心跳）应被跳过"""
        llm = _make_llm()
        chunks = [
            _make_stream_chunk(),  # 空chunk
            _make_stream_chunk(content="real"),
            _make_stream_chunk(),  # 空chunk
        ]
        llm._client = MagicMock()
        llm._client.chat.completions.create = AsyncMock(return_value=_make_stream_response(chunks))

        results = []
        async for chunk in llm.astream([{"role": "user", "content": "hi"}]):
            results.append(chunk)

        assert len(results) == 1
        assert results[0].delta_content == "real"

    @pytest.mark.asyncio
    async def test_stream_forces_no_response_format(self):
        """流式调用应强制移除 response_format，即使调用方传入"""
        llm = _make_llm()
        llm._client = MagicMock()
        llm._client.chat.completions.create = AsyncMock(return_value=_make_stream_response([]))

        async for _ in llm.astream(
            [{"role": "user", "content": "hi"}],
            response_format={"type": "json_object"},
        ):
            pass

        call_kwargs = llm._client.chat.completions.create.call_args[1]
        assert "response_format" not in call_kwargs
        assert call_kwargs["stream"] is True

    @pytest.mark.asyncio
    async def test_stream_passes_stream_flag(self):
        """流式调用应设置 stream=True"""
        llm = _make_llm()
        llm._client = MagicMock()
        llm._client.chat.completions.create = AsyncMock(return_value=_make_stream_response([]))

        async for _ in llm.astream([{"role": "user", "content": "hi"}]):
            pass

        call_kwargs = llm._client.chat.completions.create.call_args[1]
        assert call_kwargs["stream"] is True

    @pytest.mark.asyncio
    async def test_stream_rate_limit_raises_retryable(self):
        """429 限流应抛 RetryableLLMError（astream 不重试，由调用方处理）"""
        from openai import RateLimitError
        llm = _make_llm()
        llm._client = MagicMock()
        response = MagicMock()
        response.status_code = 429
        llm._client.chat.completions.create = AsyncMock(
            side_effect=RateLimitError(message="limited", response=response, body=None)
        )

        with pytest.raises(RetryableLLMError) as exc_info:
            async for _ in llm.astream([{"role": "user", "content": "hi"}]):
                pass
        assert exc_info.value.status_code == 429

    @pytest.mark.asyncio
    async def test_stream_timeout_raises_retryable(self):
        """超时应抛 RetryableLLMError"""
        from openai import APITimeoutError
        llm = _make_llm()
        llm._client = MagicMock()
        llm._client.chat.completions.create = AsyncMock(
            side_effect=APITimeoutError(request=MagicMock())
        )

        with pytest.raises(RetryableLLMError):
            async for _ in llm.astream([{"role": "user", "content": "hi"}]):
                pass

    @pytest.mark.asyncio
    async def test_stream_4xx_raises_non_retryable(self):
        """4xx 错误应抛 NonRetryableLLMError"""
        from openai import APIStatusError
        llm = _make_llm()
        llm._client = MagicMock()
        response = MagicMock()
        response.status_code = 400
        llm._client.chat.completions.create = AsyncMock(
            side_effect=APIStatusError(message="bad request", response=response, body=None)
        )

        with pytest.raises(NonRetryableLLMError) as exc_info:
            async for _ in llm.astream([{"role": "user", "content": "hi"}]):
                pass
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_stream_no_retry_on_rate_limit(self):
        """astream 不加 @retry，429 应只调用一次"""
        from openai import RateLimitError
        llm = _make_llm()
        llm._client = MagicMock()
        response = MagicMock()
        response.status_code = 429
        llm._client.chat.completions.create = AsyncMock(
            side_effect=RateLimitError(message="limited", response=response, body=None)
        )

        with pytest.raises(RetryableLLMError):
            async for _ in llm.astream([{"role": "user", "content": "hi"}]):
                pass

        assert llm._client.chat.completions.create.call_count == 1


class TestBaseAgentInvokeLLMStream:
    """BaseAgent._invoke_llm_stream 测试"""

    @pytest.mark.asyncio
    async def test_stream_yields_chunks_from_llm(self):
        """_invoke_llm_stream 应转发 LLM 的流式 chunks"""
        agent = _build_base_agent()
        mock_llm = MagicMock()
        mock_chunks = [
            LLMStreamChunk(delta_content="hello"),
            LLMStreamChunk(delta_content=" world"),
            LLMStreamChunk(finish_reason="stop"),
        ]

        async def mock_astream(**kwargs):
            for chunk in mock_chunks:
                yield chunk

        mock_llm.astream = mock_astream
        agent._llm = mock_llm

        results = []
        async for chunk in agent._invoke_llm_stream([{"role": "user", "content": "hi"}]):
            results.append(chunk)

        assert len(results) == 3
        assert results[0].delta_content == "hello"
        assert results[1].delta_content == " world"

    @pytest.mark.asyncio
    async def test_stream_adds_user_message_to_memory(self):
        """流式调用前应将用户消息加入记忆"""
        agent = _build_base_agent()
        mock_llm = MagicMock()

        async def mock_astream(**kwargs):
            return
            yield  # make it an async generator

        mock_llm.astream = mock_astream
        agent._llm = mock_llm

        async for _ in agent._invoke_llm_stream([{"role": "user", "content": "query"}]):
            pass

        agent._add_to_memory.assert_called_with([{"role": "user", "content": "query"}])

    @pytest.mark.asyncio
    async def test_stream_adds_assistant_message_to_memory_on_success(self):
        """流式成功完成后应将完整assistant消息加入记忆"""
        agent = _build_base_agent()
        mock_llm = MagicMock()

        async def mock_astream(**kwargs):
            yield LLMStreamChunk(delta_content="hello")
            yield LLMStreamChunk(delta_content=" world")

        mock_llm.astream = mock_astream
        agent._llm = mock_llm

        async for _ in agent._invoke_llm_stream([{"role": "user", "content": "hi"}]):
            pass

        # _add_to_memory 调用两次：一次用户消息，一次assistant消息
        assert agent._add_to_memory.call_count == 2
        assistant_call = agent._add_to_memory.call_args_list[1]
        assistant_msg = assistant_call[0][0][0]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["content"] == "hello world"

    @pytest.mark.asyncio
    async def test_stream_adds_reasoning_to_memory(self):
        """流式完成且含reasoning时，assistant消息应携带reasoning_content"""
        agent = _build_base_agent()
        mock_llm = MagicMock()

        async def mock_astream(**kwargs):
            yield LLMStreamChunk(delta_reasoning="thinking...")
            yield LLMStreamChunk(delta_content="answer")

        mock_llm.astream = mock_astream
        agent._llm = mock_llm

        async for _ in agent._invoke_llm_stream([{"role": "user", "content": "hi"}]):
            pass

        assistant_call = agent._add_to_memory.call_args_list[1]
        assistant_msg = assistant_call[0][0][0]
        assert assistant_msg["reasoning_content"] == "thinking..."
        assert assistant_msg["content"] == "answer"

    @pytest.mark.asyncio
    async def test_stream_not_add_assistant_on_exception(self):
        """流式中途异常时不应将assistant消息加入记忆"""
        agent = _build_base_agent()
        mock_llm = MagicMock()

        async def mock_astream(**kwargs):
            yield LLMStreamChunk(delta_content="partial")
            raise RetryableLLMError("stream failed")

        mock_llm.astream = mock_astream
        agent._llm = mock_llm

        with pytest.raises(RetryableLLMError):
            async for _ in agent._invoke_llm_stream([{"role": "user", "content": "hi"}]):
                pass

        # 只有一次_add_to_memory调用（用户消息），没有assistant消息
        assert agent._add_to_memory.call_count == 1

    @pytest.mark.asyncio
    async def test_stream_triggers_compression(self):
        """token 接近阈值时应触发压缩"""
        agent = _build_base_agent()
        # mock predict_token_pressure 返回主动压缩(非紧急)状态
        agent._memory.predict_token_pressure = MagicMock(return_value={
            "current_ratio": 0.65,
            "projected_ratio": 0.70,
            "pressure_level": "high",
            "should_proactive_compress": True,
            "should_emergency_compress": False,
        })
        agent.compact_memory = AsyncMock()
        mock_llm = MagicMock()

        async def mock_astream(**kwargs):
            return
            yield

        mock_llm.astream = mock_astream
        agent._llm = mock_llm

        async for _ in agent._invoke_llm_stream([{"role": "user", "content": "hi"}]):
            pass

        agent.compact_memory.assert_called_once()


class TestReActAgentSummarize:
    """ReActAgent.summarize 流式输出测试"""

    def _build_react_agent(self) -> ReActAgent:
        """构造测试用 ReActAgent 实例"""
        agent = object.__new__(ReActAgent)
        agent._tools = [_StubTool()]
        agent._agent_config = AgentConfig(max_iterations=5, max_retries=3)
        agent._format = "json_object"
        agent._tool_choice = "auto"
        agent._system_prompt = "test"
        agent.name = "react"
        agent._session_id = "test_session"
        agent._uow_factory = lambda: MagicMock()
        agent._uow = MagicMock()
        agent._retry_interval = 0.01
        agent._json_parser = MagicMock()
        agent._json_parser.invoke = AsyncMock(return_value={})
        agent._memory = MagicMock()
        agent._memory.empty = False
        agent._memory.should_compress = MagicMock(return_value=False)
        agent._memory.is_context_overflow = MagicMock(return_value=False)
        agent._memory.check_token_limit = MagicMock(return_value=False)
        agent._memory.get_messages = MagicMock(return_value=[])
        agent._add_to_memory = AsyncMock()
        agent._ensure_memory = AsyncMock()
        agent._token_counter = None
        agent._context_window = 64000
        return agent

    @pytest.mark.asyncio
    async def test_summarize_yields_final_message_event(self):
        """summarize 应产出最终 MessageEvent(is_final=True)

        F10-1 流式输出: 最终答案走 _stream_final_answer 切片推送,
        短文本(< stream_chunk_min_chars)切片为1片 delta + 1片最终事件。
        本测试聚焦最终事件内容(message/is_final/attachments),
        delta 片段数量由 _stream_final_answer 单测覆盖。
        """
        agent = self._build_react_agent()
        agent._invoke_llm = AsyncMock(return_value={
            "role": "assistant",
            "content": '{"message": "任务已完成", "attachments": []}'
        })
        agent._json_parser.invoke = AsyncMock(return_value={
            "message": "任务已完成", "attachments": []
        })

        events = []
        async for event in agent.summarize(known_files=None):
            events.append(event)

        # 短文本切片: 1片delta(is_streaming=True) + 1片最终(is_final=True)
        final_events = [e for e in events if e.is_final]
        assert len(final_events) == 1
        final = final_events[0]
        assert final.is_streaming is False
        assert final.is_final is True
        assert final.message == "任务已完成"

    @pytest.mark.asyncio
    async def test_summarize_final_event_has_attachments(self):
        """最终答案事件应合并 LLM 返回附件与 known_files（去重保序）"""
        agent = self._build_react_agent()
        agent._invoke_llm = AsyncMock(return_value={
            "role": "assistant",
            "content": '{"message": "done", "attachments": ["/home/ubuntu/llm.docx"]}'
        })
        agent._json_parser.invoke = AsyncMock(return_value={
            "message": "done", "attachments": ["/home/ubuntu/llm.docx"]
        })

        events = []
        async for event in agent.summarize(known_files=["/home/ubuntu/report.docx"]):
            events.append(event)

        final_event = events[-1]
        assert final_event.is_final is True
        # LLM附件 + known_files 合并去重保序
        assert len(final_event.attachments) == 2
        assert final_event.attachments[0].filepath == "/home/ubuntu/llm.docx"
        assert final_event.attachments[1].filepath == "/home/ubuntu/report.docx"

    @pytest.mark.asyncio
    async def test_summarize_fallback_on_json_parse_failure(self):
        """JSON解析失败时应降级为原始文本 + known_files 附件

        F10-1 流式输出: 降级文本同样走 _stream_final_answer 切片推送,
        最终事件(is_final=True)携带原始文本与 known_files 附件。
        """
        agent = self._build_react_agent()
        agent._invoke_llm = AsyncMock(return_value={
            "role": "assistant",
            "content": "原始自然语言回复"
        })
        agent._json_parser.invoke = AsyncMock(side_effect=ValueError("invalid json"))

        events = []
        async for event in agent.summarize(known_files=["/home/ubuntu/report.docx"]):
            events.append(event)

        # 聚焦最终事件: 降级文本 + known_files 附件
        final_events = [e for e in events if e.is_final]
        assert len(final_events) == 1
        final = final_events[0]
        assert final.is_final is True
        # 降级时使用原始文本作为消息
        assert final.message == "原始自然语言回复"
        # 降级时使用 known_files 作为附件
        assert len(final.attachments) == 1
        assert final.attachments[0].filepath == "/home/ubuntu/report.docx"

    @pytest.mark.asyncio
    async def test_summarize_returns_error_on_llm_failure(self):
        """LLM调用失败时应返回 ErrorEvent"""
        agent = self._build_react_agent()
        agent._invoke_llm = AsyncMock(side_effect=RetryableLLMError("llm unavailable"))

        from app.domain.models.event import ErrorEvent
        events = []
        async for event in agent.summarize(known_files=None):
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], ErrorEvent)

    @pytest.mark.asyncio
    async def test_summarize_returns_error_on_empty_content(self):
        """_invoke_llm 返回空内容时应返回 ErrorEvent"""
        agent = self._build_react_agent()
        agent._invoke_llm = AsyncMock(return_value={"role": "assistant", "content": ""})

        from app.domain.models.event import ErrorEvent
        events = []
        async for event in agent.summarize(known_files=None):
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], ErrorEvent)

    @pytest.mark.asyncio
    async def test_summarize_uses_json_prompt_with_files_placeholder(self):
        """summarize 应使用 SUMMARIZE_PROMPT（JSON格式，含TypeScript接口与{files}占位符）"""
        agent = self._build_react_agent()
        agent._invoke_llm = AsyncMock(return_value={
            "role": "assistant",
            "content": '{"message": "ok", "attachments": []}'
        })
        agent._json_parser.invoke = AsyncMock(return_value={
            "message": "ok", "attachments": []
        })

        async for _ in agent.summarize(known_files=None):
            pass

        # 验证 _invoke_llm 收到的首条消息为 SUMMARIZE_PROMPT 格式化内容
        call_args = agent._invoke_llm.call_args
        user_msg = call_args[0][0][0]
        assert user_msg["role"] == "user"
        # SUMMARIZE_PROMPT 应包含 JSON 接口定义与交付规范
        assert "TypeScript" in user_msg["content"]
        assert "interface Response" in user_msg["content"]
        # {files} 占位符应被填充（known_files=None 时填"（无）"）
        assert "{files}" not in user_msg["content"]
        assert "（无）" in user_msg["content"]


class TestAgentTaskRunnerStreaming:
    """AgentTaskRunner 流式delta事件处理测试"""

    def _build_runner(self):
        """构造测试用 AgentTaskRunner 实例（绕过 __init__）"""
        from app.domain.services.agent_task_runner import AgentTaskRunner
        runner = object.__new__(AgentTaskRunner)
        runner._session_id = "test_session"

        _uow = MagicMock()
        runner._uow = _uow
        runner._uow_factory = lambda: _uow
        _uow.__aenter__ = AsyncMock(return_value=_uow)
        _uow.__aexit__ = AsyncMock(return_value=False)
        _uow.session = MagicMock()
        _uow.session.add_event = AsyncMock()
        _uow.session.update_latest_message = AsyncMock()
        _uow.session.increment_unread_message_count = AsyncMock()
        return runner

    @pytest.mark.asyncio
    async def test_put_and_add_event_skips_db_for_streaming_delta(self):
        """is_streaming=True 的delta事件不应写入DB"""
        runner = self._build_runner()
        task = MagicMock()
        task.output_stream = MagicMock()
        task.output_stream.put = AsyncMock(return_value="event_id_1")

        delta_event = MessageEvent(
            role="assistant",
            message="delta chunk",
            is_streaming=True,
            is_final=False,
        )

        await runner._put_and_add_event(task, delta_event)

        # 应推送到SSE
        task.output_stream.put.assert_called_once()
        # 不应写入DB
        runner._uow.session.add_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_put_and_add_event_writes_db_for_final(self):
        """is_final=True 的最终答案事件应写入DB"""
        runner = self._build_runner()
        task = MagicMock()
        task.output_stream = MagicMock()
        task.output_stream.put = AsyncMock(return_value="event_id_1")

        final_event = MessageEvent(
            role="assistant",
            message="final answer",
            is_streaming=False,
            is_final=True,
        )

        await runner._put_and_add_event(task, final_event)

        # 应推送到SSE
        task.output_stream.put.assert_called_once()
        # 应写入DB
        runner._uow.session.add_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_put_and_add_event_writes_db_for_normal_message(self):
        """普通消息事件（无is_streaming）应写入DB"""
        runner = self._build_runner()
        task = MagicMock()
        task.output_stream = MagicMock()
        task.output_stream.put = AsyncMock(return_value="event_id_1")

        normal_event = MessageEvent(role="assistant", message="normal")

        await runner._put_and_add_event(task, normal_event)

        task.output_stream.put.assert_called_once()
        runner._uow.session.add_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_flow_skips_attachment_sync_for_streaming(self):
        """_run_flow 应跳过 is_streaming 事件的附件同步"""
        runner = self._build_runner()
        runner._sync_message_attachments_to_storage = AsyncMock()

        # mock _flow.invoke 返回流式delta + 最终答案
        async def mock_invoke(message):
            yield MessageEvent(role="assistant", message="delta1", is_streaming=True, is_final=False)
            yield MessageEvent(role="assistant", message="delta2", is_streaming=True, is_final=False)
            yield MessageEvent(role="assistant", message="final", is_streaming=False, is_final=True)

        runner._flow = MagicMock()
        runner._flow.invoke = mock_invoke

        from app.domain.models.message import Message
        events = []
        async for event in runner._run_flow(Message(message="test")):
            events.append(event)

        # 附件同步只应被调用1次（仅最终答案事件）
        assert runner._sync_message_attachments_to_storage.call_count == 1

    @pytest.mark.asyncio
    async def test_run_flow_syncs_attachment_for_non_streaming(self):
        """_run_flow 应为非流式消息事件同步附件"""
        runner = self._build_runner()
        runner._sync_message_attachments_to_storage = AsyncMock()

        async def mock_invoke(message):
            yield MessageEvent(role="assistant", message="msg1")
            yield MessageEvent(role="assistant", message="msg2")

        runner._flow = MagicMock()
        runner._flow.invoke = mock_invoke

        from app.domain.models.message import Message
        events = []
        async for event in runner._run_flow(Message(message="test")):
            events.append(event)

        # 两个非流式消息事件都应同步附件
        assert runner._sync_message_attachments_to_storage.call_count == 2


class TestMessageEventStreaming:
    """MessageEvent / MessageSSEEvent is_streaming 序列化测试"""

    def test_message_event_default_is_streaming_false(self):
        """MessageEvent 默认 is_streaming=False"""
        event = MessageEvent(role="assistant", message="hello")
        assert event.is_streaming is False

    def test_message_event_with_is_streaming_true(self):
        """MessageEvent 可设置 is_streaming=True"""
        event = MessageEvent(
            role="assistant",
            message="delta",
            is_streaming=True,
            is_final=False,
        )
        assert event.is_streaming is True
        assert event.is_final is False

    def test_message_sse_event_serializes_is_streaming(self):
        """MessageSSEEvent 应正确序列化 is_streaming 字段"""
        event = MessageEvent(
            role="assistant",
            message="delta",
            is_streaming=True,
            is_final=False,
        )
        sse_event = MessageSSEEvent.from_event(event)
        assert sse_event.data.is_streaming is True
        assert sse_event.data.is_final is False

    def test_message_sse_event_serializes_final(self):
        """MessageSSEEvent 应正确序列化 is_final=True 的最终答案"""
        event = MessageEvent(
            role="assistant",
            message="final answer",
            is_streaming=False,
            is_final=True,
        )
        sse_event = MessageSSEEvent.from_event(event)
        assert sse_event.data.is_streaming is False
        assert sse_event.data.is_final is True

    def test_message_event_json_roundtrip(self):
        """MessageEvent JSON 序列化/反序列化应保留 is_streaming"""
        event = MessageEvent(
            role="assistant",
            message="delta",
            is_streaming=True,
            is_final=False,
        )
        json_str = event.model_dump_json()
        restored = MessageEvent.model_validate_json(json_str)
        assert restored.is_streaming is True
        assert restored.is_final is False

    def test_message_event_attachments_preserved_in_final(self):
        """最终答案事件的 attachments 应被保留"""
        event = MessageEvent(
            role="assistant",
            message="done",
            attachments=[File(filepath="/home/ubuntu/report.docx")],
            is_streaming=False,
            is_final=True,
        )
        assert len(event.attachments) == 1
        assert event.attachments[0].filepath == "/home/ubuntu/report.docx"
