#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_deepseek_v4_compat.py
DeepSeek V4新模型兼容性单元测试
覆盖: LLMConfig扩展、OpenAILLM参数传递、Memory压缩reasoning_content保留、Agent修复逻辑
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.models.app_config import LLMConfig, ThinkingMode
from app.domain.models.memory import Memory
from app.domain.services.agents.base import BaseAgent
from app.domain.models.app_config import AgentConfig
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.base import BaseTool, tool
from app.infrastructure.external.llm.openai_llm import OpenAILLM


class _StubTool(BaseTool):
    name: str = "stub"

    @tool(name="stub_action", description="测试工具", parameters={}, required=[])
    async def stub_action(self) -> ToolResult:
        return ToolResult(success=True, data="ok")


def _build_base_agent() -> BaseAgent:
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
    agent._memory.predict_token_pressure = MagicMock(return_value={
        "current_ratio": 0.0, "projected_ratio": 0.0, "pressure_level": "safe",
        "should_proactive_compress": False, "should_emergency_compress": False,
    })
    agent._memory.get_messages = MagicMock(return_value=[])
    agent._add_to_memory = AsyncMock()
    agent._ensure_memory = AsyncMock()
    agent._token_counter = None
    agent._context_window = 64000
    return agent


class TestLLMConfigV4:
    """LLMConfig V4扩展字段测试"""

    def test_default_model_is_v4_pro(self):
        config = LLMConfig()
        assert config.model_name == "deepseek-v4-pro"

    def test_default_thinking_mode_enabled(self):
        config = LLMConfig()
        assert config.thinking_mode == ThinkingMode.ENABLED

    def test_default_reasoning_effort_high(self):
        config = LLMConfig()
        assert config.reasoning_effort == "high"

    def test_thinking_mode_disabled(self):
        config = LLMConfig(thinking_mode=ThinkingMode.DISABLED)
        assert config.thinking_mode == ThinkingMode.DISABLED

    def test_reasoning_effort_max(self):
        config = LLMConfig(reasoning_effort="max")
        assert config.reasoning_effort == "max"

    def test_invalid_reasoning_effort_raises_error(self):
        with pytest.raises(Exception):
            LLMConfig(reasoning_effort="invalid")

    def test_v4_flash_model_name(self):
        config = LLMConfig(model_name="deepseek-v4-flash")
        assert config.model_name == "deepseek-v4-flash"


class TestOpenAILLMV4Params:
    """OpenAILLM V4参数传递测试"""

    def test_v4_pro_thinking_enabled(self):
        llm = OpenAILLM(LLMConfig(
            base_url="https://api.deepseek.com",
            api_key="test",
            model_name="deepseek-v4-pro",
            thinking_mode=ThinkingMode.ENABLED,
        ))
        assert llm._model_name == "deepseek-v4-pro"
        assert llm._thinking_enabled is True

    def test_v4_flash_thinking_disabled(self):
        llm = OpenAILLM(LLMConfig(
            base_url="https://api.deepseek.com",
            api_key="test",
            model_name="deepseek-v4-flash",
            thinking_mode=ThinkingMode.DISABLED,
        ))
        assert llm._model_name == "deepseek-v4-flash"
        assert llm._thinking_enabled is False

    def test_v4_pro_thinking_disabled_by_config(self):
        llm = OpenAILLM(LLMConfig(
            base_url="https://api.deepseek.com",
            api_key="test",
            model_name="deepseek-v4-pro",
            thinking_mode=ThinkingMode.DISABLED,
        ))
        assert llm._model_name == "deepseek-v4-pro"
        assert llm._thinking_enabled is False

    def test_v4_flash_thinking_enabled_by_config(self):
        llm = OpenAILLM(LLMConfig(
            base_url="https://api.deepseek.com",
            api_key="test",
            model_name="deepseek-v4-flash",
            thinking_mode=ThinkingMode.ENABLED,
        ))
        assert llm._model_name == "deepseek-v4-flash"
        assert llm._thinking_enabled is True

    def test_build_extra_body_enabled(self):
        llm = OpenAILLM(LLMConfig(
            base_url="https://api.deepseek.com",
            api_key="test",
            model_name="deepseek-v4-pro",
            thinking_mode=ThinkingMode.ENABLED,
        ))
        extra = llm._build_extra_body()
        assert extra == {"thinking": {"type": "enabled"}}

    def test_build_extra_body_disabled(self):
        llm = OpenAILLM(LLMConfig(
            base_url="https://api.deepseek.com",
            api_key="test",
            model_name="deepseek-v4-flash",
            thinking_mode=ThinkingMode.DISABLED,
        ))
        extra = llm._build_extra_body()
        assert extra == {"thinking": {"type": "disabled"}}

    def test_thinking_enabled_sends_reasoning_effort(self):
        llm = OpenAILLM(LLMConfig(
            base_url="https://api.deepseek.com",
            api_key="test",
            model_name="deepseek-v4-pro",
            thinking_mode=ThinkingMode.ENABLED,
            reasoning_effort="max",
        ))
        assert llm._thinking_enabled is True
        assert llm._reasoning_effort == "max"

    @pytest.mark.asyncio
    async def test_invoke_passes_extra_body_and_reasoning_effort(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.model_dump.return_value = {
            "role": "assistant", "content": "hello"
        }
        mock_response.model_dump.return_value = {}

        with patch.object(OpenAILLM, '__init__', lambda self, *a, **k: None):
            llm = OpenAILLM.__new__(OpenAILLM)
            llm._client = MagicMock()
            llm._model_name = "deepseek-v4-pro"
            llm._temperature = 0.7
            llm._max_tokens = 8192
            llm._timeout = 3600
            llm._thinking_enabled = True
            llm._reasoning_effort = "high"

            llm._client.chat.completions.create = AsyncMock(return_value=mock_response)

            await llm.invoke([{"role": "user", "content": "hi"}])

            call_kwargs = llm._client.chat.completions.create.call_args[1]
            assert call_kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
            assert call_kwargs["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_invoke_disabled_thinking_no_reasoning_effort(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.model_dump.return_value = {"role": "assistant", "content": "hi"}
        mock_response.model_dump.return_value = {}

        with patch.object(OpenAILLM, '__init__', lambda self, *a, **k: None):
            llm = OpenAILLM.__new__(OpenAILLM)
            llm._client = MagicMock()
            llm._model_name = "deepseek-v4-flash"
            llm._temperature = 0.7
            llm._max_tokens = 8192
            llm._timeout = 3600
            llm._thinking_enabled = False
            llm._reasoning_effort = "high"

            llm._client.chat.completions.create = AsyncMock(return_value=mock_response)

            await llm.invoke([{"role": "user", "content": "hi"}])

            call_kwargs = llm._client.chat.completions.create.call_args[1]
            assert "reasoning_effort" not in call_kwargs
            assert call_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}

    def test_model_name_used_directly(self):
        llm = OpenAILLM(LLMConfig(
            base_url="https://api.deepseek.com",
            api_key="test",
            model_name="deepseek-v4-flash",
        ))
        assert llm._model_name == "deepseek-v4-flash"


class TestMemoryReasoningPreservation:
    """Memory压缩时reasoning_content保留策略测试"""

    def _build_memory_with_tool_call(self) -> Memory:
        return Memory(messages=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "let me check", "reasoning_content": "I need to use a tool", "tool_calls": [{"id": "c1", "function": {"name": "shell_exec", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "function_name": "shell_exec", "content": '{"success": true}'},
            {"role": "assistant", "content": "done", "reasoning_content": "task completed"},
        ])

    def test_compact_preserves_reasoning_with_tool_calls(self):
        memory = self._build_memory_with_tool_call()
        memory.compact()
        tool_call_msg = memory.messages[2]
        assert "reasoning_content" in tool_call_msg
        assert tool_call_msg["reasoning_content"] == "I need to use a tool"

    def test_compact_removes_reasoning_without_tool_calls(self):
        memory = self._build_memory_with_tool_call()
        memory.compact()
        no_tool_msg = memory.messages[4]
        assert "reasoning_content" not in no_tool_msg

    def test_should_preserve_reasoning_with_tool_calls(self):
        msg = {"role": "assistant", "content": "x", "reasoning_content": "r", "tool_calls": [{"id": "1"}]}
        assert Memory._should_preserve_reasoning(msg) is True

    def test_should_not_preserve_reasoning_without_tool_calls(self):
        msg = {"role": "assistant", "content": "x", "reasoning_content": "r"}
        assert Memory._should_preserve_reasoning(msg) is False

    def test_should_not_preserve_non_assistant(self):
        msg = {"role": "tool", "content": "x", "reasoning_content": "r"}
        assert Memory._should_preserve_reasoning(msg) is False


class TestAgentReasoningRepair:
    """Agent reasoning_content修复逻辑测试"""

    @pytest.mark.asyncio
    async def test_repair_adds_missing_reasoning(self):
        agent = _build_base_agent()
        memory = Memory(messages=[
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "x", "tool_calls": [{"id": "1"}]},
        ])
        agent._memory = memory
        agent._uow.__aenter__ = AsyncMock(return_value=agent._uow)
        agent._uow.__aexit__ = AsyncMock(return_value=False)
        agent._uow.session = MagicMock()
        agent._uow.session.save_memory = AsyncMock()

        await agent._repair_missing_reasoning()

        assert "reasoning_content" in memory.messages[1]
        assert memory.messages[1]["reasoning_content"] == ""

    @pytest.mark.asyncio
    async def test_repair_skips_when_reasoning_present(self):
        agent = _build_base_agent()
        memory = Memory(messages=[
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "x", "reasoning_content": "existing", "tool_calls": [{"id": "1"}]},
        ])
        agent._memory = memory
        agent._uow.__aenter__ = AsyncMock(return_value=agent._uow)
        agent._uow.__aexit__ = AsyncMock(return_value=False)
        agent._uow.session = MagicMock()
        agent._uow.session.save_memory = AsyncMock()

        await agent._repair_missing_reasoning()

        assert memory.messages[1]["reasoning_content"] == "existing"

    @pytest.mark.asyncio
    async def test_invoke_llm_handles_400_reasoning_error(self):
        agent = _build_base_agent()
        agent._llm = MagicMock()
        agent._repair_missing_reasoning = AsyncMock()

        call_count = 0

        async def mock_invoke(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("400 Bad Request: reasoning_content is required")
            return {"role": "assistant", "content": "recovered", "tool_calls": None}

        agent._llm.invoke = mock_invoke

        result = await agent._invoke_llm([{"role": "user", "content": "test"}])
        assert result["content"] == "recovered"
        assert agent._repair_missing_reasoning.call_count == 1
