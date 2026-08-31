#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批次50单元测试: LLM调用503容错增强

验证:
1. LLMConfig.max_retries 可配置(默认5)
2. OpenAILLM._max_retries 从config读取
3. _select_wait_strategy 503/502使用更长退避(max=30s)
4. invoke() 按max_retries重试RetryableLLMError
5. invoke() 不重试NonRetryableLLMError
6. invoke() max_retries可配置(非硬编码3)
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.app_config import LLMConfig, LLMProvider, ThinkingMode
from app.infrastructure.external.llm.exceptions import RetryableLLMError, NonRetryableLLMError
from app.infrastructure.external.llm.openai_llm import (
    OpenAILLM,
    _select_wait_strategy,
    _SERVICE_UNAVAILABLE_CODES,
    _SERVICE_UNAVAILABLE_BACKOFF,
    _DEFAULT_BACKOFF,
    _RETRYABLE_STATUS_CODES,
)


def _make_llm_config(max_retries: int = 5) -> LLMConfig:
    """构造测试用LLMConfig"""
    return LLMConfig(
        provider=LLMProvider.OPENAI,
        base_url="https://api.test.com",
        api_key="test-key",
        model_name="test-model",
        temperature=0.7,
        max_tokens=8192,
        thinking_mode=ThinkingMode.DISABLED,
        reasoning_effort="low",
        context_window=64000,
        max_retries=max_retries,
    )


def _make_retry_state(status_code=None, exc=None, attempt_number=1):
    """构造模拟的RetryCallState

    attempt_number必须为整数,tenacity的wait_exponential用其计算退避时间。
    """
    state = MagicMock()
    state.attempt_number = attempt_number
    outcome = MagicMock()
    if exc is not None:
        outcome.failed = True
        outcome.exception.return_value = exc
    else:
        outcome.failed = False
    state.outcome = outcome
    return state


# ============================================================
# Fix 1: LLMConfig.max_retries 可配置
# ============================================================

class TestLLMConfigMaxRetries:
    """验证LLMConfig支持max_retries配置"""

    def test_default_max_retries_is_5(self):
        """默认值应为5(批次50从3提升到5)"""
        config = LLMConfig(
            base_url="https://api.test.com",
            api_key="test",
            model_name="test",
        )
        assert config.max_retries == 5

    def test_custom_max_retries(self):
        """支持自定义max_retries"""
        config = _make_llm_config(max_retries=8)
        assert config.max_retries == 8

    def test_max_retries_min_value(self):
        """max_retries最小值为1"""
        config = _make_llm_config(max_retries=1)
        assert config.max_retries == 1

    def test_max_retries_max_value(self):
        """max_retries最大值为10"""
        config = _make_llm_config(max_retries=10)
        assert config.max_retries == 10

    def test_max_retries_below_min_raises(self):
        """max_retries<1应抛出验证错误"""
        with pytest.raises(Exception):
            _make_llm_config(max_retries=0)

    def test_max_retries_above_max_raises(self):
        """max_retries>10应抛出验证错误"""
        with pytest.raises(Exception):
            _make_llm_config(max_retries=11)


# ============================================================
# Fix 2: OpenAILLM._max_retries 从config读取
# ============================================================

class TestOpenAILLMMaxRetriesFromConfig:
    """验证OpenAILLM从LLMConfig读取max_retries"""

    def test_llm_stores_max_retries(self):
        """OpenAILLM应存储config中的max_retries"""
        config = _make_llm_config(max_retries=7)
        llm = OpenAILLM(config)
        assert llm._max_retries == 7

    def test_llm_default_max_retries(self):
        """未指定max_retries时使用默认值5"""
        config = LLMConfig(
            base_url="https://api.test.com",
            api_key="test",
            model_name="test",
        )
        llm = OpenAILLM(config)
        assert llm._max_retries == 5

    def test_llm_max_retries_not_hardcoded_3(self):
        """max_retries不应是硬编码的3(批次50核心修复)"""
        config = _make_llm_config(max_retries=5)
        llm = OpenAILLM(config)
        assert llm._max_retries != 3


# ============================================================
# Fix 3: _select_wait_strategy 503/502专属退避
# ============================================================

class TestSelectWaitStrategy:
    """验证_select_wait_strategy根据状态码选择退避策略"""

    def test_503_uses_service_unavailable_backoff(self):
        """503应使用服务不可用退避(更长等待)"""
        exc = RetryableLLMError("503", status_code=503)
        state = _make_retry_state(exc=exc)
        wait_time = _select_wait_strategy(state)
        assert wait_time > 0
        # 503的退避应 >= 2s(min=2)
        assert wait_time >= 2

    def test_502_uses_service_unavailable_backoff(self):
        """502应使用服务不可用退避(更长等待)"""
        exc = RetryableLLMError("502", status_code=502)
        state = _make_retry_state(exc=exc)
        wait_time = _select_wait_strategy(state)
        assert wait_time > 0
        assert wait_time >= 2

    def test_429_uses_default_backoff(self):
        """429应使用默认退避(较短等待)"""
        exc = RetryableLLMError("429", status_code=429)
        state = _make_retry_state(exc=exc)
        wait_time = _select_wait_strategy(state)
        assert wait_time > 0
        # 429的退避应 >= 1s(min=1)但通常 < 503的退避
        assert wait_time >= 1

    def test_500_uses_default_backoff(self):
        """500应使用默认退避"""
        exc = RetryableLLMError("500", status_code=500)
        state = _make_retry_state(exc=exc)
        wait_time = _select_wait_strategy(state)
        assert wait_time > 0

    def test_timeout_uses_default_backoff(self):
        """超时(无status_code)应使用默认退避"""
        exc = RetryableLLMError("timeout")
        state = _make_retry_state(exc=exc)
        wait_time = _select_wait_strategy(state)
        assert wait_time > 0

    def test_503_backoff_longer_than_default(self):
        """503退避应比429更长(多次重试后差距明显)"""
        # 模拟第3次重试(attempt_number=3)
        exc_503 = RetryableLLMError("503", status_code=503)
        state_503 = _make_retry_state(exc=exc_503, attempt_number=3)

        exc_429 = RetryableLLMError("429", status_code=429)
        state_429 = _make_retry_state(exc=exc_429, attempt_number=3)

        wait_503 = _select_wait_strategy(state_503)
        wait_429 = _select_wait_strategy(state_429)
        # 503的multiplier=2 vs 429的multiplier=1,第3次重试503应更长
        assert wait_503 >= wait_429

    def test_no_exception_uses_default(self):
        """无异常时使用默认退避"""
        state = _make_retry_state(exc=None)
        wait_time = _select_wait_strategy(state)
        assert wait_time > 0


# ============================================================
# Fix 4: invoke() 重试行为验证
# ============================================================

class TestInvokeRetryBehavior:
    """验证invoke()的重试行为"""

    def _make_llm(self, max_retries=5):
        """创建测试用LLM实例(mock掉AsyncOpenAI)"""
        config = _make_llm_config(max_retries=max_retries)
        with patch("app.infrastructure.external.llm.openai_llm.AsyncOpenAI"):
            return OpenAILLM(config)

    @pytest.mark.asyncio
    async def test_invoke_retries_on_retryable_error(self):
        """invoke()应对RetryableLLMError进行重试"""
        llm = self._make_llm(max_retries=3)
        call_count = 0

        async def mock_invoke_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RetryableLLMError("503", status_code=503)
            return {"content": "success", "role": "assistant"}

        llm._invoke_once = mock_invoke_once
        result = await llm.invoke([{"role": "user", "content": "test"}])
        assert result["content"] == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_invoke_does_not_retry_non_retryable(self):
        """invoke()不应重试NonRetryableLLMError"""
        llm = self._make_llm(max_retries=5)
        call_count = 0

        async def mock_invoke_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise NonRetryableLLMError("400 Bad Request", status_code=400)

        llm._invoke_once = mock_invoke_once
        with pytest.raises(NonRetryableLLMError):
            await llm.invoke([{"role": "user", "content": "test"}])
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_invoke_respects_max_retries(self):
        """invoke()应按max_retries次数重试,超限后抛出"""
        llm = self._make_llm(max_retries=4)
        call_count = 0

        async def mock_invoke_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RetryableLLMError("503", status_code=503)

        llm._invoke_once = mock_invoke_once
        with pytest.raises(RetryableLLMError):
            await llm.invoke([{"role": "user", "content": "test"}])
        assert call_count == 4

    @pytest.mark.asyncio
    async def test_invoke_max_retries_not_hardcoded_3(self):
        """max_retries=5时应重试5次(非硬编码3次)"""
        llm = self._make_llm(max_retries=5)
        call_count = 0

        async def mock_invoke_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RetryableLLMError("503", status_code=503)

        llm._invoke_once = mock_invoke_once
        with pytest.raises(RetryableLLMError):
            await llm.invoke([{"role": "user", "content": "test"}])
        assert call_count == 5

    @pytest.mark.asyncio
    async def test_invoke_succeeds_after_retry(self):
        """invoke()在重试后成功应返回结果"""
        llm = self._make_llm(max_retries=5)
        call_count = 0

        async def mock_invoke_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RetryableLLMError("503", status_code=503)
            return {"content": "recovered", "role": "assistant"}

        llm._invoke_once = mock_invoke_once
        result = await llm.invoke([{"role": "user", "content": "test"}])
        assert result["content"] == "recovered"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_invoke_returns_result_on_first_success(self):
        """invoke()首次成功应直接返回,不重试"""
        llm = self._make_llm(max_retries=5)
        call_count = 0

        async def mock_invoke_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return {"content": "immediate", "role": "assistant"}

        llm._invoke_once = mock_invoke_once
        result = await llm.invoke([{"role": "user", "content": "test"}])
        assert result["content"] == "immediate"
        assert call_count == 1


# ============================================================
# 回归: 常量与配置完整性
# ============================================================

class TestRegressionConstants:
    """回归: 验证批次50新增常量完整性"""

    def test_retryable_status_codes_includes_503(self):
        """503应在可重试状态码集合中"""
        assert 503 in _RETRYABLE_STATUS_CODES

    def test_retryable_status_codes_includes_502(self):
        """502应在可重试状态码集合中"""
        assert 502 in _RETRYABLE_STATUS_CODES

    def test_service_unavailable_codes_contains_502_503(self):
        """服务不可用状态码应包含502和503"""
        assert 502 in _SERVICE_UNAVAILABLE_CODES
        assert 503 in _SERVICE_UNAVAILABLE_CODES

    def test_service_unavailable_backoff_max_is_30(self):
        """503/502退避上限应为30s"""
        assert _SERVICE_UNAVAILABLE_BACKOFF["max"] == 30

    def test_default_backoff_max_is_10(self):
        """默认退避上限应为10s"""
        assert _DEFAULT_BACKOFF["max"] == 10

    def test_service_unavailable_backoff_multiplier_is_2(self):
        """503/502退避乘数应为2(更快增长)"""
        assert _SERVICE_UNAVAILABLE_BACKOFF["multiplier"] == 2

    def test_default_backoff_multiplier_is_1(self):
        """默认退避乘数应为1"""
        assert _DEFAULT_BACKOFF["multiplier"] == 1
