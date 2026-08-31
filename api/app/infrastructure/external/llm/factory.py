#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2026/07/06

@File    : factory.py
LLM 实例工厂，按 LLMConfig.provider 创建对应的 LLM 实现

当前项目统一使用 OpenAI 兼容协议（涵盖 DeepSeek/GLM/Qwen/Kimi 等主流国产模型），
后续如需接入非兼容协议的 Provider，在此扩展分支即可。
"""
import logging

from app.domain.external.llm import LLM
from app.domain.models.app_config import LLMConfig
from app.infrastructure.external.llm.openai_llm import OpenAILLM

logger = logging.getLogger(__name__)


def create_llm(config: LLMConfig) -> LLM:
    """按 LLMConfig.provider 创建对应的 LLM 实例

    Args:
        config: LLM 配置

    Returns:
        LLM 实例

    Raises:
        ValueError: 未知 provider
    """
    from app.domain.models.app_config import LLMProvider

    if config.provider == LLMProvider.OPENAI:
        logger.info(f"创建OpenAI兼容LLM实例: model={config.model_name}, base_url={config.base_url}")
        return OpenAILLM(config)

    raise ValueError(f"未知的 LLM Provider: {config.provider}")
