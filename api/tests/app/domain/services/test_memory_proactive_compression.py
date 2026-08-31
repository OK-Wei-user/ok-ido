#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_memory_proactive_compression.py
08优化单元测试 - 主动预测压缩 + KeyFact去重 + 动态截断

验证点:
- predict_token_pressure 四等级(safe/moderate/high/critical)
- KeyFact content_hash 增量去重
- truncate_tool_result_dynamic 基于剩余token动态截断
- extract_key_facts 使用content_hash去重
"""
import json
from unittest.mock import MagicMock

import pytest

from app.domain.models.memory import (
    Memory,
    KeyFact,
    _PROACTIVE_COMPRESS_THRESHOLD,
    _REACTIVE_COMPRESS_THRESHOLD,
    _CRITICAL_THRESHOLD,
    _HIGH_PRESSURE_TRUNCATE_MAX,
    _DEEP_RESEARCH_RESULT_MAX_LENGTH,
)


class FakeTokenCounter:
    """简易token计数器: 返回预设值"""

    def __init__(self, token_count: int):
        self._count = token_count

    def count_messages(self, messages):
        return self._count


class TestPredictTokenPressure:
    """token压力预测"""

    def test_safe_level(self):
        """10%使用率: safe"""
        memory = Memory(messages=[{"role": "user", "content": "hi"}])
        counter = FakeTokenCounter(token_count=640)  # 1% of 64000
        result = memory.predict_token_pressure(counter, context_window=64000)
        assert result["pressure_level"] == "safe"
        assert result["should_proactive_compress"] is False
        assert result["should_emergency_compress"] is False

    def test_moderate_level(self):
        """65%使用率: moderate,触发主动压缩"""
        memory = Memory(messages=[{"role": "user", "content": "hi"}])
        counter = FakeTokenCounter(token_count=int(64000 * 0.65))
        result = memory.predict_token_pressure(counter, context_window=64000)
        assert result["pressure_level"] == "moderate"
        assert result["should_proactive_compress"] is True
        assert result["should_emergency_compress"] is False

    def test_high_level(self):
        """75%使用率: high,触发主动压缩"""
        memory = Memory(messages=[{"role": "user", "content": "hi"}])
        counter = FakeTokenCounter(token_count=int(64000 * 0.75))
        result = memory.predict_token_pressure(counter, context_window=64000)
        assert result["pressure_level"] == "high"
        assert result["should_proactive_compress"] is True

    def test_critical_level(self):
        """90%使用率: critical,触发紧急压缩"""
        memory = Memory(messages=[{"role": "user", "content": "hi"}])
        counter = FakeTokenCounter(token_count=int(64000 * 0.90))
        result = memory.predict_token_pressure(counter, context_window=64000)
        assert result["pressure_level"] == "critical"
        assert result["should_emergency_compress"] is True

    def test_pending_tokens_push_to_critical(self):
        """pending_tokens将比例从high推到critical"""
        memory = Memory(messages=[{"role": "user", "content": "hi"}])
        current = int(64000 * 0.70)
        pending = int(64000 * 0.20)  # 投影后 90%
        counter = FakeTokenCounter(token_count=current)
        result = memory.predict_token_pressure(
            counter, context_window=64000, pending_tokens=pending,
        )
        assert result["pressure_level"] == "critical"
        assert result["projected_ratio"] >= _CRITICAL_THRESHOLD

    def test_none_counter_returns_safe(self):
        """token_counter为None时降级返回safe"""
        memory = Memory()
        result = memory.predict_token_pressure(None, context_window=64000)
        assert result["pressure_level"] == "safe"
        assert result["should_proactive_compress"] is False


class TestKeyFactContentHash:
    """KeyFact content_hash去重"""

    def test_hash_computed_on_init(self):
        """构造时自动计算content_hash"""
        fact = KeyFact(category="url", content="https://example.com/path?x=1")
        assert fact.content_hash != ""

    def test_same_url_different_query_same_hash(self):
        """URL归一化: 查询参数不同但scheme+host+path相同则hash一致"""
        fact1 = KeyFact(category="url", content="https://example.com/path?a=1")
        fact2 = KeyFact(category="url", content="https://example.com/path?b=2")
        assert fact1.content_hash == fact2.content_hash

    def test_file_path_normalization(self):
        """file类: 去除/home/ubuntu/前缀后hash一致"""
        fact1 = KeyFact(category="file", content="/home/ubuntu/report.md")
        fact2 = KeyFact(category="file", content="report.md")
        assert fact1.content_hash == fact2.content_hash

    def test_whitespace_normalization(self):
        """其他类: 内部空白压缩后hash一致"""
        fact1 = KeyFact(category="cmd", content="ls   -la\n/home")
        fact2 = KeyFact(category="cmd", content="ls -la /home")
        assert fact1.content_hash == fact2.content_hash

    def test_different_categories_different_hash(self):
        """相同内容不同category则hash不同"""
        fact1 = KeyFact(category="url", content="example.com")
        fact2 = KeyFact(category="file", content="example.com")
        assert fact1.content_hash != fact2.content_hash


class TestExtractKeyFactsDeduplication:
    """extract_key_facts去重逻辑"""

    def test_duplicate_urls_not_added_twice(self):
        """相同URL(归一化后)只记录一次"""
        memory = Memory(messages=[
            {"role": "user", "content": "研究人工智能最新进展"},
            {"role": "assistant", "content": "好的"},
            {"role": "tool", "function_name": "browser_view",
             "content": json.dumps({"page_state": {"url": "https://example.com/ai?ref=1", "title": "AI News"}})},
            {"role": "tool", "function_name": "browser_view",
             "content": json.dumps({"page_state": {"url": "https://example.com/ai?ref=2", "title": "AI News"}})},
        ])
        facts = memory.extract_key_facts()
        url_facts = [f for f in facts if f.category == "url"]
        assert len(url_facts) == 1

    def test_existing_fact_timestamp_updated(self):
        """已存在事实再次出现时更新timestamp而非新增"""
        memory = Memory(messages=[
            {"role": "user", "content": "研究人工智能最新进展"},
            {"role": "tool", "function_name": "browser_view",
             "content": json.dumps({"page_state": {"url": "https://example.com/ai", "title": "AI"}})},
        ])
        memory.extract_key_facts()
        first_ts = memory.key_facts[0].timestamp

        # 再次添加相同URL
        memory.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({"page_state": {"url": "https://example.com/ai", "title": "AI Updated"}}),
        })
        memory.extract_key_facts()
        url_facts = [f for f in memory.key_facts if f.category == "url"]
        assert len(url_facts) == 1
        assert url_facts[0].timestamp >= first_ts

    def test_max_facts_cap_preserves_new_fact(self):
        """超过上限时,新事实按分类配额规则保留"""
        memory = Memory()
        # 填充10条cmd事实至分类配额上限
        for i in range(10):
            memory.add_message({
                "role": "tool",
                "function_name": "shell_exec",
                "content": json.dumps({"console": [{"command": f"cmd_{i}"}]}),
            })
        memory.extract_key_facts()
        assert len(memory.key_facts) <= 10

        # 新增不同分类(url)的事实应被保留
        memory.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({"page_state": {"url": "https://important.com", "title": "Important"}}),
        })
        memory.extract_key_facts()
        assert len(memory.key_facts) <= 10
        assert any(f.content == "https://important.com" for f in memory.key_facts)


class TestTruncateToolResultDynamic:
    """动态截断工具结果"""

    def test_safe_pressure_uses_standard_threshold(self):
        """低压力下使用标准阈值(deep_research=6000)"""
        memory = Memory()
        counter = FakeTokenCounter(token_count=1000)  # 极低压力
        content = "x" * (_DEEP_RESEARCH_RESULT_MAX_LENGTH - 1)
        result = memory.truncate_tool_result_dynamic(
            content, function_name="deep_research",
            token_counter=counter, context_window=64000,
        )
        assert result == content  # 未超阈值,原样返回

    def test_high_pressure_reduces_threshold(self):
        """高压力下阈值减半(<50%剩余)"""
        memory = Memory()
        counter = FakeTokenCounter(token_count=int(64000 * 0.60))  # 40%剩余
        content = "x" * (_DEEP_RESEARCH_RESULT_MAX_LENGTH - 1)
        result = memory.truncate_tool_result_dynamic(
            content, function_name="deep_research",
            token_counter=counter, context_window=64000,
        )
        # 阈值减半后触发截断
        assert "truncated" in result or len(result) < len(content)

    def test_critical_pressure_aggressive_truncation(self):
        """极高压力下阈值降至1/4(<20%剩余)"""
        memory = Memory()
        counter = FakeTokenCounter(token_count=int(64000 * 0.90))  # 10%剩余
        content = "x" * (_DEEP_RESEARCH_RESULT_MAX_LENGTH)
        result = memory.truncate_tool_result_dynamic(
            content, function_name="deep_research",
            token_counter=counter, context_window=64000,
        )
        # 阈值降至1/4后应触发截断
        assert "truncated" in result or len(result) < len(content)

    def test_none_counter_uses_standard_threshold(self):
        """token_counter为None时使用标准阈值"""
        memory = Memory()
        content = "x" * (_DEEP_RESEARCH_RESULT_MAX_LENGTH - 1)
        result = memory.truncate_tool_result_dynamic(
            content, function_name="deep_research",
            token_counter=None, context_window=64000,
        )
        assert result == content

    def test_non_string_content_passthrough(self):
        """非字符串内容原样返回"""
        memory = Memory()
        data = {"key": "value"}
        result = memory.truncate_tool_result_dynamic(
            data, function_name="deep_research",
        )
        assert result == data
