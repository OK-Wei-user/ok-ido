#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_factory.py
LLM 实例工厂单元测试：验证按 provider 字段路由到正确实现
"""
import pytest

from app.domain.models.app_config import LLMConfig, LLMProvider, ThinkingMode
from app.infrastructure.external.llm.factory import create_llm
from app.infrastructure.external.llm.openai_llm import OpenAILLM


def _make_config(provider: LLMProvider = LLMProvider.OPENAI) -> LLMConfig:
    """构造测试用 LLMConfig"""
    return LLMConfig(
        provider=provider,
        base_url="https://api.deepseek.com",
        api_key="test-key",
        model_name="deepseek-v4-pro",
        thinking_mode=ThinkingMode.DISABLED,
    )


class TestCreateLLMFactory:
    """LLM 实例工厂行为验证"""

    def test_openai_provider_returns_openai_llm(self):
        """provider=openai 应返回 OpenAILLM 实例"""
        llm = create_llm(_make_config(LLMProvider.OPENAI))
        assert isinstance(llm, OpenAILLM)
        assert llm.model_name == "deepseek-v4-pro"

    def test_unknown_provider_raises_value_error(self):
        """未知 provider 应抛 ValueError"""
        config = _make_config()
        config.provider = "unknown_provider"
        with pytest.raises(ValueError, match="未知"):
            create_llm(config)
