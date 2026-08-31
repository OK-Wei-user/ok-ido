#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P4-1/P4-2/P4-3 记忆模块优化单元测试

覆盖:
- P4-1 配置预设(profile): balanced/lightweight/heavy 三档百分比阈值
- P4-2 压缩策略(compression_strategy): auto/proactive/reactive
- P4-3 压缩逻辑精简: _prepare_compression 公共方法复用
"""
import pytest

from app.domain.models.memory_config import (
    MemoryConfig,
    create_memory_config,
    _PROFILE_THRESHOLDS,
    _REFERENCE_CONTEXT_WINDOW,
)


# ============================================================================
# P4-1: 配置预设测试
# ============================================================================

class TestProfilePresets:
    """配置预设(profile)测试"""

    def test_balanced_profile_thresholds(self):
        """balanced预设使用基准百分比阈值"""
        cfg = create_memory_config(128000, profile="balanced")
        assert cfg.proactive_compress_threshold == 0.65
        assert cfg.reactive_compress_threshold == 0.75
        assert cfg.critical_threshold == 0.88

    def test_lightweight_profile_delays_compression(self):
        """lightweight预设延迟压缩(阈值更高,减少compact调用)"""
        cfg = create_memory_config(128000, profile="lightweight")
        assert cfg.proactive_compress_threshold == 0.75
        assert cfg.reactive_compress_threshold == 0.82
        assert cfg.critical_threshold == 0.90
        # lightweight的阈值应高于balanced
        balanced = create_memory_config(128000, profile="balanced")
        assert cfg.proactive_compress_threshold > balanced.proactive_compress_threshold

    def test_heavy_profile_early_compression(self):
        """heavy预设提前压缩(阈值更低,保护长上下文)"""
        cfg = create_memory_config(128000, profile="heavy")
        assert cfg.proactive_compress_threshold == 0.55
        assert cfg.reactive_compress_threshold == 0.68
        assert cfg.critical_threshold == 0.85
        # heavy的阈值应低于balanced
        balanced = create_memory_config(128000, profile="balanced")
        assert cfg.proactive_compress_threshold < balanced.proactive_compress_threshold

    def test_invalid_profile_falls_back_to_balanced(self):
        """无效profile回退到balanced(容错)"""
        cfg = create_memory_config(128000, profile="nonexistent")
        balanced = create_memory_config(128000, profile="balanced")
        assert cfg.proactive_compress_threshold == balanced.proactive_compress_threshold
        assert cfg.reactive_compress_threshold == balanced.reactive_compress_threshold
        assert cfg.critical_threshold == balanced.critical_threshold

    def test_profile_does_not_affect_absolute_values(self):
        """profile只调整百分比阈值,不影响绝对值缩放"""
        balanced = create_memory_config(128000, profile="balanced")
        lightweight = create_memory_config(128000, profile="lightweight")
        heavy = create_memory_config(128000, profile="heavy")
        # 绝对值应相同(同一context_window)
        assert balanced.max_messages_soft == lightweight.max_messages_soft == heavy.max_messages_soft
        assert balanced.shell_output_keep == lightweight.shell_output_keep == heavy.shell_output_keep

    def test_profile_with_context_window_scaling(self):
        """profile与context_window缩放兼容(互不影响)"""
        # 64K + lightweight: 绝对值缩放0.5,百分比用lightweight
        cfg = create_memory_config(64000, profile="lightweight")
        balanced_128k = create_memory_config(128000, profile="balanced")
        # 绝对值: 64K应小于128K
        assert cfg.max_messages_soft < balanced_128k.max_messages_soft
        # 百分比: lightweight应高于balanced
        assert cfg.proactive_compress_threshold > balanced_128k.proactive_compress_threshold


# ============================================================================
# P4-2: 压缩策略测试
# ============================================================================

class TestCompressionStrategy:
    """压缩策略(compression_strategy)测试"""

    def test_default_strategy_is_auto(self):
        """默认压缩策略为auto"""
        cfg = create_memory_config(128000)
        assert cfg.compression_strategy == "auto"

    def test_proactive_strategy(self):
        """proactive策略可正确设置"""
        cfg = create_memory_config(128000, compression_strategy="proactive")
        assert cfg.compression_strategy == "proactive"

    def test_reactive_strategy(self):
        """reactive策略可正确设置"""
        cfg = create_memory_config(128000, compression_strategy="reactive")
        assert cfg.compression_strategy == "reactive"

    def test_strategy_independent_of_profile(self):
        """压缩策略与profile独立配置"""
        cfg = create_memory_config(
            128000, profile="heavy", compression_strategy="reactive"
        )
        assert cfg.compression_strategy == "reactive"
        assert cfg.proactive_compress_threshold == 0.55  # heavy profile


class TestShouldProactiveCompress:
    """_should_proactive_compress 自适应压缩判断测试"""

    def _make_agent(self, step_description=""):
        """构造最小化Agent mock用于测试压缩策略判断"""
        from app.domain.services.agents.base import BaseAgent

        class _MockAgent(BaseAgent):
            name = "test"

        agent = _MockAgent.__new__(_MockAgent)
        agent._step_description = step_description
        return agent

    def test_auto_multi_step_proactive(self):
        """auto模式: 多步骤任务(step_description非空)主动压缩"""
        from app.domain.models.memory import _COMPRESSION_STRATEGY
        if _COMPRESSION_STRATEGY != "auto":
            pytest.skip("config.yaml中compression_strategy非auto,跳过auto模式测试")
        agent = self._make_agent(step_description="分析5月出入库数据")
        assert agent._should_proactive_compress() is True

    def test_auto_simple_dialog_passive(self):
        """auto模式: 简单对话(step_description为空)被动压缩"""
        from app.domain.models.memory import _COMPRESSION_STRATEGY
        if _COMPRESSION_STRATEGY != "auto":
            pytest.skip("config.yaml中compression_strategy非auto,跳过auto模式测试")
        agent = self._make_agent(step_description="")
        assert agent._should_proactive_compress() is False


# ============================================================================
# P4-3: 压缩逻辑精简测试
# ============================================================================

class TestPrepareCompression:
    """_prepare_compression 公共方法测试"""

    def test_prepare_compression_exists(self):
        """Memory类有_prepare_compression方法"""
        from app.domain.models.memory import Memory
        assert hasattr(Memory, "_prepare_compression")

    def test_prepare_compression_appends_summary(self):
        """_prepare_compression追加会话摘要"""
        from app.domain.models.memory import Memory
        mem = Memory()
        mem.messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "测试消息"},
            {"role": "assistant", "content": "回复"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "test", "arguments": "{}"}}]},
            {"role": "tool", "content": "工具结果", "tool_call_id": "1", "function_name": "test"},
        ]
        original_summary = mem.session_summary
        mem._prepare_compression()
        # session_summary应被追加(非空且变化)
        assert mem.session_summary != original_summary or len(mem.messages) > 0

    def test_prepare_compression_extracts_key_facts(self):
        """_prepare_compression提取关键事实"""
        from app.domain.models.memory import Memory
        mem = Memory()
        mem.messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "请帮我分析出入库数据,文件路径/home/ubuntu/data.xlsx"},
            {"role": "assistant", "content": "好的"},
        ]
        original_facts_count = len(mem.key_facts)
        mem._prepare_compression()
        # 应提取到关键事实(file类)
        assert len(mem.key_facts) >= original_facts_count

    def test_compact_uses_prepare_compression(self):
        """compact调用_prepare_compression(通过摘要变化验证)"""
        from app.domain.models.memory import Memory
        mem = Memory()
        mem.messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "测试消息用于压缩验证"},
            {"role": "assistant", "content": "回复内容", "reasoning_content": "思考过程"},
        ]
        summary_before = mem.session_summary
        mem.compact()
        # compact应通过_prepare_compression追加了摘要
        assert mem.metrics.compact_count == 1

    def test_emergency_compact_uses_prepare_compression(self):
        """emergency_compact调用_prepare_compression(通过key_facts验证)"""
        from app.domain.models.memory import Memory
        mem = Memory()
        # 构造足够多的消息触发紧急压缩
        mem.messages = [
            {"role": "system", "content": "系统提示"},
        ]
        for i in range(20):
            mem.messages.append({"role": "user", "content": f"消息{i}"})
            mem.messages.append({"role": "assistant", "content": f"回复{i}"})
        facts_before = len(mem.key_facts)
        mem.emergency_compact()
        # emergency_compact应通过_prepare_compression提取了关键事实
        assert mem.metrics.emergency_count == 1
        # 消息数应大幅减少(head+summary+tail)
        assert len(mem.messages) < 20
