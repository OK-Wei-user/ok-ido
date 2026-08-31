#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_token_counter_fallback.py
TokenCounter 三级降级单元测试 — 验证 P1-2 防御修复

测试覆盖：
- Level 1: tiktoken.encoding_for_model 正常路径
- Level 2: 未知模型降级到 cl100k_base
- Level 3: cl100k_base 加载失败时降级到 CJK 字符估算
- _estimate_tokens_by_chars: CJK/ASCII/混合文本估算
- count_text/count_message 在 Level 3 下的兜底行为
"""
from unittest.mock import patch, MagicMock

import pytest

from app.infrastructure.external.llm.token_counter import TokenCounter


class TestEstimateTokensByChars:
    """_estimate_tokens_by_chars CJK字符估算测试"""

    def test_pure_cjk_text(self):
        """纯CJK文本: 1.5 token/字"""
        # "你好世界" = 4个CJK字符 = 4 * 1.5 = 6
        result = TokenCounter._estimate_tokens_by_chars("你好世界")
        assert result == 6

    def test_pure_ascii_alpha(self):
        """纯ASCII字母: 0.25 token/字符"""
        # "hello" = 5个ASCII字母 = 5 * 0.25 = 1.25 → int(1.25) = 1
        result = TokenCounter._estimate_tokens_by_chars("hello")
        assert result == 1

    def test_pure_ascii_alnum(self):
        """纯ASCII字母数字: 0.25 token/字符"""
        # "abc123" = 6个ASCII = 6 * 0.25 = 1.5 → int(1.5) = 1
        result = TokenCounter._estimate_tokens_by_chars("abc123")
        assert result == 1

    def test_mixed_cjk_and_ascii(self):
        """混合CJK+ASCII: 按各自系数累加"""
        # "你好world" = 2 CJK + 5 ASCII = 2*1.5 + 5*0.25 = 3 + 1.25 = 4.25 → 4
        result = TokenCounter._estimate_tokens_by_chars("你好world")
        assert result == 4

    def test_punctuation_as_other(self):
        """标点符号: 0.5 token/字符"""
        # "!!!" = 3个other = 3 * 0.5 = 1.5 → 1
        result = TokenCounter._estimate_tokens_by_chars("!!!")
        assert result == 1

    def test_empty_string_returns_zero(self):
        """空字符串: 返回0"""
        assert TokenCounter._estimate_tokens_by_chars("") == 0

    def test_fullwidth_chars_as_cjk(self):
        """全角字符: 按CJK系数(1.5)计算"""
        # 全角感叹号！= U+FF01 在 0xFF00-0xFFEF 范围内
        result = TokenCounter._estimate_tokens_by_chars("！！")
        assert result == 3  # 2 * 1.5 = 3

    def test_spaces_counted_as_ascii(self):
        """空格: 按ASCII系数(0.25)计算"""
        # "a b c" = 3 ASCII字母 + 2 空格 = 5 * 0.25 = 1.25 → 1
        result = TokenCounter._estimate_tokens_by_chars("a b c")
        assert result == 1

    def test_japanese_hiragana_as_cjk(self):
        """日文假名: 按CJK系数(1.5)计算"""
        # ひらがな 在 0x3000-0x30FF 范围内
        result = TokenCounter._estimate_tokens_by_chars("あいう")
        assert result == 4  # 3 * 1.5 = 4.5 → 4


class TestLevel1NormalPath:
    """Level 1: tiktoken.encoding_for_model 正常路径测试"""

    def test_known_model_encoder_not_none(self):
        """已知模型: _encoder 不为 None"""
        counter = TokenCounter("gpt-4o-mini")
        assert counter._encoder is not None

    def test_known_model_count_text_positive(self):
        """已知模型: count_text 返回正值"""
        counter = TokenCounter("gpt-4o-mini")
        assert counter.count_text("hello world") > 0

    def test_known_model_empty_text_zero(self):
        """已知模型: 空文本返回0"""
        counter = TokenCounter("gpt-4o-mini")
        assert counter.count_text("") == 0


class TestLevel2FallbackEncoding:
    """Level 2: 未知模型降级到 cl100k_base 测试"""

    def test_unknown_model_encoder_not_none(self):
        """未知模型: 降级到 cl100k_base，_encoder 不为 None"""
        counter = TokenCounter("totally-unknown-model-xyz-12345")
        assert counter._encoder is not None

    def test_unknown_model_count_text_positive(self):
        """未知模型: count_text 仍返回正值"""
        counter = TokenCounter("totally-unknown-model-xyz-12345")
        assert counter.count_text("hello world") > 0

    def test_unknown_model_no_exception(self):
        """未知模型: 构造不抛异常"""
        # 不应抛出任何异常
        counter = TokenCounter("another-unknown-model-abc-98765")
        assert counter is not None


class TestLevel3CjkEstimation:
    """Level 3: cl100k_base 加载失败时降级到 CJK 字符估算测试"""

    @patch("app.infrastructure.external.llm.token_counter.tiktoken")
    def test_cl100k_load_failure_sets_encoder_none(self, mock_tiktoken):
        """cl100k_base 加载失败: _encoder 设为 None，不抛异常"""
        # encoding_for_model 抛 KeyError（未知模型）
        mock_tiktoken.encoding_for_model.side_effect = KeyError("unknown model")
        # get_encoding 抛异常（模拟 SSL 错误）
        mock_tiktoken.get_encoding.side_effect = RuntimeError("SSL error downloading cl100k_base")

        counter = TokenCounter("test-model")
        assert counter._encoder is None

    @patch("app.infrastructure.external.llm.token_counter.tiktoken")
    def test_level3_count_text_uses_cjk_estimation(self, mock_tiktoken):
        """Level 3: count_text 使用 CJK 字符估算"""
        mock_tiktoken.encoding_for_model.side_effect = KeyError("unknown")
        mock_tiktoken.get_encoding.side_effect = RuntimeError("SSL error")

        counter = TokenCounter("test-model")
        # "你好world" = 2 CJK + 5 ASCII = 4 tokens（见 TestEstimateTokensByChars）
        result = counter.count_text("你好world")
        assert result == 4

    @patch("app.infrastructure.external.llm.token_counter.tiktoken")
    def test_level3_count_text_empty_returns_zero(self, mock_tiktoken):
        """Level 3: 空文本返回0"""
        mock_tiktoken.encoding_for_model.side_effect = KeyError("unknown")
        mock_tiktoken.get_encoding.side_effect = RuntimeError("SSL error")

        counter = TokenCounter("test-model")
        assert counter.count_text("") == 0

    @patch("app.infrastructure.external.llm.token_counter.tiktoken")
    def test_level3_count_message_no_exception(self, mock_tiktoken):
        """Level 3: count_message 不抛异常，返回正值"""
        mock_tiktoken.encoding_for_model.side_effect = KeyError("unknown")
        mock_tiktoken.get_encoding.side_effect = RuntimeError("SSL error")

        counter = TokenCounter("test-model")
        msg = {"role": "user", "content": "你好世界"}
        result = counter.count_message(msg)
        assert result > 0

    @patch("app.infrastructure.external.llm.token_counter.tiktoken")
    def test_level3_count_messages_no_exception(self, mock_tiktoken):
        """Level 3: count_messages 不抛异常，返回正值"""
        mock_tiktoken.encoding_for_model.side_effect = KeyError("unknown")
        mock_tiktoken.get_encoding.side_effect = RuntimeError("SSL error")

        counter = TokenCounter("test-model")
        msgs = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "世界"},
        ]
        result = counter.count_messages(msgs)
        assert result > 0

    @patch("app.infrastructure.external.llm.token_counter.tiktoken")
    def test_level3_count_messages_empty_returns_zero(self, mock_tiktoken):
        """Level 3: 空消息列表返回0"""
        mock_tiktoken.encoding_for_model.side_effect = KeyError("unknown")
        mock_tiktoken.get_encoding.side_effect = RuntimeError("SSL error")

        counter = TokenCounter("test-model")
        assert counter.count_messages([]) == 0

    @patch("app.infrastructure.external.llm.token_counter.tiktoken")
    def test_level3_count_message_handles_list_value(self, mock_tiktoken):
        """Level 3: list 类型字段值（如 tool_calls）不抛异常"""
        mock_tiktoken.encoding_for_model.side_effect = KeyError("unknown")
        mock_tiktoken.get_encoding.side_effect = RuntimeError("SSL error")

        counter = TokenCounter("test-model")
        msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "f"}}]}
        result = counter.count_message(msg)
        assert result > 0

    @patch("app.infrastructure.external.llm.token_counter.tiktoken")
    def test_level3_count_message_handles_none_value(self, mock_tiktoken):
        """Level 3: None 字段值被跳过"""
        mock_tiktoken.encoding_for_model.side_effect = KeyError("unknown")
        mock_tiktoken.get_encoding.side_effect = RuntimeError("SSL error")

        counter = TokenCounter("test-model")
        msg_with_none = {"role": "assistant", "content": "hi", "tool_calls": None}
        msg_without_none = {"role": "assistant", "content": "hi"}
        assert counter.count_message(msg_with_none) == counter.count_message(msg_without_none)


class TestLevel3ConnectionExceptionTypes:
    """Level 3: 各种异常类型降级测试"""

    @patch("app.infrastructure.external.llm.token_counter.tiktoken")
    def test_connection_error_triggers_level3(self, mock_tiktoken):
        """ConnectionError 触发 Level 3 降级"""
        mock_tiktoken.encoding_for_model.side_effect = KeyError("unknown")
        mock_tiktoken.get_encoding.side_effect = ConnectionError("Network unreachable")

        counter = TokenCounter("test-model")
        assert counter._encoder is None
        assert counter.count_text("test") > 0

    @patch("app.infrastructure.external.llm.token_counter.tiktoken")
    def test_timeout_error_triggers_level3(self, mock_tiktoken):
        """TimeoutError 触发 Level 3 降级"""
        mock_tiktoken.encoding_for_model.side_effect = KeyError("unknown")
        mock_tiktoken.get_encoding.side_effect = TimeoutError("Download timeout")

        counter = TokenCounter("test-model")
        assert counter._encoder is None
        assert counter.count_text("test") > 0

    @patch("app.infrastructure.external.llm.token_counter.tiktoken")
    def test_os_error_triggers_level3(self, mock_tiktoken):
        """OSError 触发 Level 3 降级"""
        mock_tiktoken.encoding_for_model.side_effect = KeyError("unknown")
        mock_tiktoken.get_encoding.side_effect = OSError("SSL certificate verification failed")

        counter = TokenCounter("test-model")
        assert counter._encoder is None
        assert counter.count_text("test") > 0
