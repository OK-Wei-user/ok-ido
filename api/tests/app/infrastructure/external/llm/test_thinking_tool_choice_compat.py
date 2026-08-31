#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_thinking_tool_choice_compat.py
思考模式与tool_choice兼容性降级单元测试

背景:
GLM/DeepSeek等模型思考模式(thinking=enabled)不支持tool_choice参数,
同时传递会返回400错误: "Thinking mode does not support this tool_choice"。
OpenAILLM适配器层在_build_create_kwargs中自动降级tool_choice为None,
让LLM自主决定工具调用,领域层无感知。

测试覆盖:
1. 思考模式开启 + tool_choice非None + tools非空 → 降级为None
2. 思考模式开启 + tool_choice=None + tools非空 → 保持None(不触发降级)
3. 思考模式关闭 + tool_choice非None + tools非空 → 保持原值(不降级)
4. 思考模式开启 + tool_choice非None + tools为空 → 不进入工具分支(无tool_choice参数)
5. 降级后thinking/reasoning_effort参数仍正确传递
"""
import pytest

from app.domain.models.app_config import LLMConfig, ThinkingMode
from app.infrastructure.external.llm.openai_llm import OpenAILLM


def _make_llm(thinking_mode: ThinkingMode = ThinkingMode.ENABLED) -> OpenAILLM:
    """构造测试用 OpenAILLM 实例

    Args:
        thinking_mode: 思考模式开关,默认ENABLED
    """
    return OpenAILLM(LLMConfig(
        base_url="https://api.deepseek.com",
        api_key="test-key",
        model_name="deepseek-v4-pro",
        thinking_mode=thinking_mode,
        reasoning_effort="high",
    ))


def _make_tools() -> list:
    """构造测试用 tools 列表"""
    return [{
        "type": "function",
        "function": {
            "name": "browser_view",
            "description": "查看浏览器当前页面",
            "parameters": {"type": "object", "properties": {}},
        },
    }]


class TestThinkingToolChoiceCompat:
    """思考模式与tool_choice兼容性降级测试"""

    def test_thinking_enabled_tool_choice_required_downgrades_to_none(self):
        """思考模式开启 + tool_choice='required' → 降级为None"""
        llm = _make_llm(thinking_mode=ThinkingMode.ENABLED)
        kwargs = llm._build_create_kwargs(
            messages=[{"role": "user", "content": "测试"}],
            tools=_make_tools(),
            tool_choice="required",
        )
        # tool_choice应被降级为None
        assert kwargs["tool_choice"] is None
        # tools仍正常传递
        assert kwargs["tools"] is not None
        assert len(kwargs["tools"]) == 1
        # thinking仍开启
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
        # reasoning_effort仍传递
        assert kwargs["reasoning_effort"] == "high"

    def test_thinking_enabled_tool_choice_auto_downgrades_to_none(self):
        """思考模式开启 + tool_choice='auto' → 降级为None"""
        llm = _make_llm(thinking_mode=ThinkingMode.ENABLED)
        kwargs = llm._build_create_kwargs(
            messages=[{"role": "user", "content": "测试"}],
            tools=_make_tools(),
            tool_choice="auto",
        )
        assert kwargs["tool_choice"] is None

    def test_thinking_enabled_tool_choice_none_stays_none(self):
        """思考模式开启 + tool_choice=None → 保持None(不触发降级)"""
        llm = _make_llm(thinking_mode=ThinkingMode.ENABLED)
        kwargs = llm._build_create_kwargs(
            messages=[{"role": "user", "content": "测试"}],
            tools=_make_tools(),
            tool_choice=None,
        )
        assert kwargs["tool_choice"] is None
        assert kwargs["tools"] is not None

    def test_thinking_disabled_tool_choice_required_preserved(self):
        """思考模式关闭 + tool_choice='required' → 保持原值(不降级)"""
        llm = _make_llm(thinking_mode=ThinkingMode.DISABLED)
        kwargs = llm._build_create_kwargs(
            messages=[{"role": "user", "content": "测试"}],
            tools=_make_tools(),
            tool_choice="required",
        )
        # 思考模式关闭时tool_choice不降级
        assert kwargs["tool_choice"] == "required"
        assert kwargs["tools"] is not None
        # thinking关闭
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        # reasoning_effort不传递(思考模式关闭时不需要)
        assert "reasoning_effort" not in kwargs

    def test_thinking_enabled_no_tools_no_tool_choice_in_kwargs(self):
        """思考模式开启 + tools为空 → 不进入工具分支,kwargs不含tool_choice/tools"""
        llm = _make_llm(thinking_mode=ThinkingMode.ENABLED)
        kwargs = llm._build_create_kwargs(
            messages=[{"role": "user", "content": "测试"}],
            tools=None,
            tool_choice="required",  # 即使传了也无效,因为tools为空
        )
        # tools为空时不进入工具分支
        assert "tools" not in kwargs
        assert "tool_choice" not in kwargs
        # thinking仍开启
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
        assert kwargs["reasoning_effort"] == "high"

    def test_downgrade_preserves_parallel_tool_calls_false(self):
        """降级后parallel_tool_calls仍为False(防止多工具并发)"""
        llm = _make_llm(thinking_mode=ThinkingMode.ENABLED)
        kwargs = llm._build_create_kwargs(
            messages=[{"role": "user", "content": "测试"}],
            tools=_make_tools(),
            tool_choice="required",
        )
        assert kwargs["parallel_tool_calls"] is False

    def test_downgrade_preserves_basic_params(self):
        """降级不影响基础参数(model/temperature/max_tokens/messages)"""
        llm = _make_llm(thinking_mode=ThinkingMode.ENABLED)
        messages = [{"role": "user", "content": "你好"}]
        kwargs = llm._build_create_kwargs(
            messages=messages,
            tools=_make_tools(),
            tool_choice="required",
        )
        assert kwargs["model"] == "deepseek-v4-pro"
        assert kwargs["messages"] == messages
        assert "temperature" in kwargs
        assert "max_tokens" in kwargs
        assert "timeout" in kwargs

    def test_downgrade_logs_warning(self, caplog):
        """降级时记录warning日志便于运维追踪"""
        import logging
        llm = _make_llm(thinking_mode=ThinkingMode.ENABLED)
        with caplog.at_level(logging.WARNING, logger="app.infrastructure.external.llm.openai_llm"):
            llm._build_create_kwargs(
                messages=[{"role": "user", "content": "测试"}],
                tools=_make_tools(),
                tool_choice="required",
            )
        # 应记录warning日志,包含关键词"降级"
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("降级" in msg for msg in warning_msgs), \
            f"期望warning日志包含'降级',实际: {warning_msgs}"

    def test_no_warning_when_tool_choice_already_none(self, caplog):
        """tool_choice本就是None时不记录降级warning"""
        import logging
        llm = _make_llm(thinking_mode=ThinkingMode.ENABLED)
        with caplog.at_level(logging.WARNING, logger="app.infrastructure.external.llm.openai_llm"):
            llm._build_create_kwargs(
                messages=[{"role": "user", "content": "测试"}],
                tools=_make_tools(),
                tool_choice=None,
            )
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("降级" in msg for msg in warning_msgs), \
            f"tool_choice=None时不应记录降级warning,实际: {warning_msgs}"
