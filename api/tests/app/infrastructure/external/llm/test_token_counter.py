#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_token_counter.py
TokenCounter 单元测试：验证 tiktoken 计数、未知模型兜底、空输入处理
"""
import pytest

from app.infrastructure.external.llm.token_counter import TokenCounter


class TestTokenCounter:
    """TokenCounter 行为验证"""

    def test_count_text_returns_positive_for_non_empty(self):
        """非空文本应返回正整数 token 数"""
        counter = TokenCounter("gpt-4o-mini")
        assert counter.count_text("hello world") > 0

    def test_count_text_empty_returns_zero(self):
        """空字符串应返回 0"""
        counter = TokenCounter("gpt-4o-mini")
        assert counter.count_text("") == 0

    def test_count_message_includes_overhead(self):
        """单条消息计数应包含固定开销（>纯文本 token 数）"""
        counter = TokenCounter("gpt-4o-mini")
        text = "hello world"
        text_tokens = counter.count_text(text)
        msg_tokens = counter.count_message({"role": "user", "content": text})
        # 固定开销 4 + 字段名开销 1 + 1 = 6
        assert msg_tokens >= text_tokens + 4

    def test_count_message_skips_none_value(self):
        """None 字段值应被完全跳过（含字段名开销）"""
        counter = TokenCounter("gpt-4o-mini")
        msg_with_none = {"role": "assistant", "content": "hi", "tool_calls": None}
        msg_without_none = {"role": "assistant", "content": "hi"}
        # None 字段应跳过 value token + 字段名开销 1 token，与无该字段的消息计数一致
        assert counter.count_message(msg_with_none) == counter.count_message(msg_without_none)

    def test_count_messages_empty_returns_zero(self):
        """空消息列表应返回 0"""
        counter = TokenCounter("gpt-4o-mini")
        assert counter.count_messages([]) == 0

    def test_count_messages_includes_separator_overhead(self):
        """多消息列表应包含分隔符开销（>单条累加）"""
        counter = TokenCounter("gpt-4o-mini")
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        single_sum = sum(counter.count_message(m) for m in msgs)
        total = counter.count_messages(msgs)
        # 2 条分隔符 * 2 token + 结尾 3 token = 7
        assert total == single_sum + 7

    def test_unknown_model_falls_back_to_cl100k(self):
        """未知模型应降级到 cl100k_base 编码，不抛异常"""
        counter = TokenCounter("totally-unknown-model-xyz-12345")
        assert counter.count_text("hello world") > 0

    def test_count_message_handles_list_value(self):
        """list 类型字段值（如 tool_calls）应被序列化后计 token"""
        counter = TokenCounter("gpt-4o-mini")
        msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "f"}}]}
        # 不抛异常且返回正整数
        assert counter.count_message(msg) > 0

    def test_count_message_handles_non_string_non_list_value(self):
        """非字符串/列表类型字段值（如 int）应转为字符串后计 token"""
        counter = TokenCounter("gpt-4o-mini")
        msg = {"role": "user", "content": "hi", "timestamp": 12345}
        # 不抛异常且返回正整数
        assert counter.count_message(msg) > 0
