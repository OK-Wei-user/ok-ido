#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_llm_retry.py
OpenAILLM tenacity 重试与异常分类单元测试：
- 429/5xx 触发 RetryableLLMError 并重试 max_retries 次(批次50默认5次,可配置)
- 4xx 非 429 触发 NonRetryableLLMError 不重试
- 超时/网络错误触发 RetryableLLMError
- max_retries 可配置(批次50): 传入自定义值时按配置重试
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from openai import (
    RateLimitError,
    APITimeoutError,
    APIConnectionError,
    APIStatusError,
)

from app.domain.models.app_config import LLMConfig, ThinkingMode
from app.infrastructure.external.llm.exceptions import RetryableLLMError, NonRetryableLLMError
from app.infrastructure.external.llm.openai_llm import OpenAILLM


def _make_llm(max_retries: int = 5) -> OpenAILLM:
    """构造测试用 OpenAILLM 实例（思考模式关闭）

    Args:
        max_retries: LLM 调用最大重试次数(批次50可配置,默认5)
    """
    return OpenAILLM(LLMConfig(
        base_url="https://api.deepseek.com",
        api_key="test-key",
        model_name="deepseek-v4-pro",
        thinking_mode=ThinkingMode.DISABLED,
        max_retries=max_retries,
    ))


def _make_response_obj(message_dict: dict):
    """构造 OpenAI SDK chat.completions.create 的 response mock"""
    response = MagicMock()
    msg_mock = MagicMock()
    msg_mock.model_dump.return_value = message_dict
    choice = MagicMock()
    choice.message = msg_mock
    response.choices = [choice]
    return response


def _make_rate_limit_error() -> RateLimitError:
    """构造 RateLimitError 异常"""
    response = MagicMock()
    response.status_code = 429
    return RateLimitError(message="rate limited", response=response, body=None)


def _make_api_status_error(status_code: int) -> APIStatusError:
    """构造 APIStatusError 异常"""
    response = MagicMock()
    response.status_code = status_code
    return APIStatusError(
        message=f"http {status_code}",
        response=response,
        body=None,
    )


def _make_timeout_error() -> APITimeoutError:
    """构造 APITimeoutError 异常（request 参数必填）"""
    return APITimeoutError(request=MagicMock())


def _make_connection_error() -> APIConnectionError:
    """构造 APIConnectionError 异常"""
    return APIConnectionError(request=MagicMock())


class TestInvokeRetry:
    """invoke 方法的 tenacity 重试行为验证"""

    @pytest.mark.asyncio
    async def test_rate_limit_retried_up_to_max_retries(self):
        """429 限流应重试 max_retries 次(默认5)后抛 RetryableLLMError"""
        llm = _make_llm()
        llm._client = MagicMock()
        llm._client.chat.completions.create = AsyncMock(side_effect=_make_rate_limit_error())

        with pytest.raises(RetryableLLMError) as exc_info:
            await llm.invoke([{"role": "user", "content": "hi"}])
        assert exc_info.value.status_code == 429
        # 批次50: stop_after_attempt(max_retries), 默认5次应调用 5 次
        assert llm._client.chat.completions.create.call_count == 5

    @pytest.mark.asyncio
    async def test_server_error_5xx_retried(self):
        """503 服务端错误应触发重试 max_retries 次(默认5)"""
        llm = _make_llm()
        llm._client = MagicMock()
        llm._client.chat.completions.create = AsyncMock(side_effect=_make_api_status_error(503))

        with pytest.raises(RetryableLLMError) as exc_info:
            await llm.invoke([{"role": "user", "content": "hi"}])
        assert exc_info.value.status_code == 503
        assert llm._client.chat.completions.create.call_count == 5

    @pytest.mark.asyncio
    async def test_client_error_4xx_not_retried(self):
        """400 客户端错误应抛 NonRetryableLLMError 且不重试"""
        llm = _make_llm()
        llm._client = MagicMock()
        llm._client.chat.completions.create = AsyncMock(side_effect=_make_api_status_error(400))

        with pytest.raises(NonRetryableLLMError) as exc_info:
            await llm.invoke([{"role": "user", "content": "hi"}])
        assert exc_info.value.status_code == 400
        # 4xx 不应重试
        assert llm._client.chat.completions.create.call_count == 1

    @pytest.mark.asyncio
    async def test_timeout_retried(self):
        """超时应触发重试 max_retries 次(默认5)"""
        llm = _make_llm()
        llm._client = MagicMock()
        llm._client.chat.completions.create = AsyncMock(side_effect=_make_timeout_error())

        with pytest.raises(RetryableLLMError):
            await llm.invoke([{"role": "user", "content": "hi"}])
        assert llm._client.chat.completions.create.call_count == 5

    @pytest.mark.asyncio
    async def test_connection_error_retried(self):
        """网络连接失败应触发重试 max_retries 次(默认5)"""
        llm = _make_llm()
        llm._client = MagicMock()
        llm._client.chat.completions.create = AsyncMock(side_effect=_make_connection_error())

        with pytest.raises(RetryableLLMError):
            await llm.invoke([{"role": "user", "content": "hi"}])
        assert llm._client.chat.completions.create.call_count == 5

    @pytest.mark.asyncio
    async def test_max_retries_configurable(self):
        """批次50: max_retries 应可配置,传入2则重试2次"""
        llm = _make_llm(max_retries=2)
        llm._client = MagicMock()
        llm._client.chat.completions.create = AsyncMock(side_effect=_make_rate_limit_error())

        with pytest.raises(RetryableLLMError):
            await llm.invoke([{"role": "user", "content": "hi"}])
        # 配置 max_retries=2, 应只调用 2 次
        assert llm._client.chat.completions.create.call_count == 2

    @pytest.mark.asyncio
    async def test_success_returns_message(self):
        """正常返回应直接拿到 message 字典"""
        llm = _make_llm()
        llm._client = MagicMock()
        expected_msg = {"role": "assistant", "content": "hello", "tool_calls": None}
        llm._client.chat.completions.create = AsyncMock(return_value=_make_response_obj(expected_msg))

        result = await llm.invoke([{"role": "user", "content": "hi"}])
        assert result["role"] == "assistant"
        assert result["content"] == "hello"
