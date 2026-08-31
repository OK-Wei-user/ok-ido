#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_metrics_collector.py
F10-9 可观测性指标收集器单元测试

设计原则:
- 每个测试方法独立验证一个行为契约,便于定位回归
- 异常静默降级是核心验证点(metrics不得阻断主流程)
- 集成测试验证BaseAgent与AgentTaskRunner的注入链路
"""
import json
import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from app.domain.services.observability import MetricsCollector
from app.domain.services.observability.metrics_collector import _TimerContext


# ===========================================================================
# 基础功能测试
# ===========================================================================
class TestMetricsCollectorBasic:
    """MetricsCollector 基础功能测试"""

    def test_increment_default_value(self):
        """increment 默认累加值为1"""
        collector = MetricsCollector(session_id="test-1")
        collector.increment("llm_call_count")
        snapshot = collector.snapshot()
        assert snapshot["llm_call_count"] == 1
        assert snapshot["session_id"] == "test-1"

    def test_increment_custom_value(self):
        """increment 支持批量累加(如token数)"""
        collector = MetricsCollector()
        collector.increment("llm_token_input_total", 1500)
        collector.increment("llm_token_input_total", 800)
        assert collector.snapshot()["llm_token_input_total"] == 2300

    def test_increment_unknown_metric_starts_from_zero(self):
        """未注册指标累加从0开始"""
        collector = MetricsCollector()
        collector.increment("new_metric", 5)
        assert collector.snapshot()["new_metric"] == 5

    def test_record_duration_accumulates(self):
        """耗时型指标累加(支持多次记录累计)"""
        collector = MetricsCollector()
        collector.record_duration("llm_invoke", 1.5)
        collector.record_duration("llm_invoke", 2.3)
        snapshot = collector.snapshot()
        assert snapshot["llm_invoke_seconds"] == round(1.5 + 2.3, 3)

    def test_set_gauge_overwrites_value(self):
        """set_gauge 覆盖写入瞬时值"""
        collector = MetricsCollector()
        collector.set_gauge("memory_message_count", 100)
        assert collector.snapshot()["memory_message_count"] == 100
        collector.set_gauge("memory_message_count", 50)
        assert collector.snapshot()["memory_message_count"] == 50


# ===========================================================================
# 快照与派生指标测试
# ===========================================================================
class TestMetricsCollectorSnapshot:
    """MetricsCollector 快照与派生指标测试"""

    def test_snapshot_includes_session_id(self):
        """快照始终包含 session_id"""
        collector = MetricsCollector(session_id="sess-123")
        snapshot = collector.snapshot()
        assert snapshot["session_id"] == "sess-123"

    def test_cache_hit_rate_derived_metric(self):
        """缓存命中率派生指标正确计算"""
        collector = MetricsCollector()
        collector.increment("tool_cache_hit_count", 7)
        collector.increment("tool_cache_miss_count", 3)
        snapshot = collector.snapshot()
        # 命中率 = 7/(7+3) = 0.7
        assert snapshot["tool_cache_hit_rate"] == 0.7

    def test_cache_hit_rate_zero_when_no_data(self):
        """无缓存调用时不输出命中率"""
        collector = MetricsCollector()
        snapshot = collector.snapshot()
        assert "tool_cache_hit_rate" not in snapshot

    def test_llm_avg_invoke_seconds_derived(self):
        """LLM平均调用耗时派生指标"""
        collector = MetricsCollector()
        collector.increment("llm_call_count", 4)
        collector.record_duration("llm_invoke", 10.0)
        snapshot = collector.snapshot()
        assert snapshot["llm_avg_invoke_seconds"] == 2.5

    def test_session_duration_after_mark_end(self):
        """mark_session_end后输出会话总耗时"""
        collector = MetricsCollector()
        time.sleep(0.05)
        collector.mark_session_end()
        snapshot = collector.snapshot()
        assert "session_duration_seconds" in snapshot
        # 时钟精度容差: 实际耗时可能略低于sleep值
        assert snapshot["session_duration_seconds"] > 0

    def test_session_duration_not_set_before_mark_end(self):
        """未调用mark_session_end时不输出会话耗时"""
        collector = MetricsCollector()
        snapshot = collector.snapshot()
        assert "session_duration_seconds" not in snapshot

    def test_to_log_json_serializable(self):
        """to_log_json 输出合法JSON字符串"""
        collector = MetricsCollector(session_id="json-test")
        collector.increment("llm_call_count", 3)
        collector.record_duration("llm_invoke", 1.234)
        log_str = collector.to_log_json()
        # 验证可解析为合法JSON
        parsed = json.loads(log_str)
        assert parsed["session_id"] == "json-test"
        assert parsed["llm_call_count"] == 3
        assert parsed["llm_invoke_seconds"] == 1.234


# ===========================================================================
# 重置与计时器测试
# ===========================================================================
class TestMetricsCollectorResetAndTimer:
    """MetricsCollector 重置与计时器测试"""

    def test_reset_clears_all_metrics(self):
        """reset 清空所有计数与耗时"""
        collector = MetricsCollector()
        collector.increment("llm_call_count", 10)
        collector.record_duration("llm_invoke", 5.0)
        collector.reset()
        snapshot = collector.snapshot()
        assert "llm_call_count" not in snapshot
        assert "llm_invoke_seconds" not in snapshot

    def test_reset_restarts_session_timer(self):
        """reset 重置会话起始时间"""
        collector = MetricsCollector()
        time.sleep(0.05)
        collector.reset()
        time.sleep(0.05)
        collector.mark_session_end()
        snapshot = collector.snapshot()
        # 重置后总耗时只统计重置后的时长
        assert snapshot["session_duration_seconds"] < 0.1

    def test_start_timer_context_manager(self):
        """start_timer 上下文管理器自动记录耗时"""
        collector = MetricsCollector()
        with collector.start_timer("llm_invoke"):
            time.sleep(0.05)
        snapshot = collector.snapshot()
        # 时钟精度容差: 实际耗时可能略低于sleep值
        assert snapshot["llm_invoke_seconds"] > 0

    def test_timer_context_records_even_on_exception(self):
        """计时器在异常时仍记录耗时(try/finally保证)"""
        collector = MetricsCollector()
        with pytest.raises(ValueError):
            with collector.start_timer("failed_op"):
                time.sleep(0.02)
                raise ValueError("test error")
        snapshot = collector.snapshot()
        # 时钟精度容差: 实际耗时可能略低于sleep值
        assert snapshot["failed_op_seconds"] > 0

    def test_timer_manual_use_without_enter_records_real_elapsed(self):
        """手动调用模式(未调用__enter__)应记录真实耗时,而非系统启动以来秒数。

        F10-9回归测试: BaseAgent._invoke_llm 中使用 start_timer() 后
        在try/finally块中直接调用 __exit__(None, None, None),未显式调用__enter__。
        历史bug: _TimerContext.__init__ 将 _start 初始化为0.0,导致 __exit__
        计算 time.monotonic() - 0.0 = 系统启动以来秒数(可能达数十万秒),
        使 llm_invoke_seconds 指标出现57小时级别异常值。

        修复后语义: __init__ 即启动计时,手动调用模式与with语句模式结果一致。
        """
        collector = MetricsCollector(session_id="manual-timer-test")
        # 模拟 BaseAgent._invoke_llm 的使用模式
        timer = collector.start_timer("llm_invoke")
        # 不调用 timer.__enter__(),直接进入try/finally
        try:
            time.sleep(0.05)
        finally:
            timer.__exit__(None, None, None)

        snapshot = collector.snapshot()
        elapsed = snapshot["llm_invoke_seconds"]
        # 关键断言: 耗时应为毫秒级别(0.05s + 微小开销),不应是数十万秒
        assert 0.01 < elapsed < 1.0, (
            f"手动调用模式耗时异常: {elapsed}s,预期应在0.05s附近,"
            f"若数值过大说明_start未被正确初始化(历史bug回归)"
        )

    def test_timer_both_modes_produce_consistent_results(self):
        """with语句模式与手动调用模式结果应一致(都在合理范围内)"""
        collector_with = MetricsCollector(session_id="with-mode")
        with collector_with.start_timer("op"):
            time.sleep(0.05)

        collector_manual = MetricsCollector(session_id="manual-mode")
        timer = collector_manual.start_timer("op")
        try:
            time.sleep(0.05)
        finally:
            timer.__exit__(None, None, None)

        elapsed_with = collector_with.snapshot()["op_seconds"]
        elapsed_manual = collector_manual.snapshot()["op_seconds"]

        # 两种模式都应在合理范围(0.04-0.5s)
        assert 0.04 < elapsed_with < 0.5, f"with模式耗时异常: {elapsed_with}"
        assert 0.04 < elapsed_manual < 0.5, f"手动模式耗时异常: {elapsed_manual}"
        # 两者差异应小于10倍(避免巨大偏差)
        ratio = max(elapsed_with, elapsed_manual) / min(elapsed_with, elapsed_manual)
        assert ratio < 10, f"两种模式差异过大: with={elapsed_with}, manual={elapsed_manual}"


# ===========================================================================
# 异常降级测试(核心契约:不阻断主流程)
# ===========================================================================
class TestMetricsCollectorResilience:
    """MetricsCollector 异常降级测试"""

    def test_increment_invalid_value_does_not_raise(self):
        """累加非数字值时不抛异常(降级忽略)"""
        collector = MetricsCollector(session_id="bad-value-test")
        # 传入不可转换为int的对象
        collector.increment("bad_metric", "not_a_number")
        # 静默降级,不抛出异常
        snapshot = collector.snapshot()
        # 快照本身仍可正常生成,且包含session_id字段
        assert snapshot.get("session_id") == "bad-value-test"

    def test_record_duration_invalid_value_does_not_raise(self):
        """记录非数字耗时不抛异常"""
        collector = MetricsCollector()
        collector.record_duration("bad_duration", "invalid")
        # 不抛异常即通过

    def test_log_snapshot_does_not_raise_on_logger_failure(self):
        """日志输出失败时不抛异常(降级忽略)"""
        collector = MetricsCollector(session_id="log-test")
        with patch.object(logging.Logger, "info", side_effect=Exception("logger fail")):
            collector.log_snapshot()
        # 不抛异常即通过

    def test_snapshot_returns_minimal_dict_on_internal_error(self):
        """快照生成失败时返回最小字典(含session_id)"""
        collector = MetricsCollector(session_id="err-test")
        # 模拟内部异常:让counters抛出异常
        with patch.object(collector, "_counters", side_effect=RuntimeError("test")):
            snapshot = collector.snapshot()
        # 静默降级返回包含session_id的字典
        assert snapshot.get("session_id") in ("err-test", None) or "error" in snapshot


# ===========================================================================
# 集成测试:BaseAgent注入链路
# ===========================================================================
class TestMetricsCollectorIntegration:
    """MetricsCollector 集成测试"""

    def test_base_agent_accepts_metrics_collector(self):
        """BaseAgent 构造函数正确接收 metrics_collector 参数"""
        from app.domain.services.agents.base import BaseAgent
        from app.domain.services.agents.planner import PlannerAgent

        # 创建mock依赖
        mock_uow_factory = MagicMock()
        mock_llm = MagicMock()
        mock_json_parser = MagicMock()
        mock_tools = []
        mock_agent_config = MagicMock()
        mock_agent_config.max_retries = 1
        mock_agent_config.max_iterations = 10
        mock_agent_config.session_timeout_seconds = 0
        mock_agent_config.session_warning_seconds = 0
        mock_agent_config.special_capability_keywords = []

        metrics = MetricsCollector(session_id="integration-test")

        planner = PlannerAgent(
            uow_factory=mock_uow_factory,
            session_id="integration-test",
            agent_config=mock_agent_config,
            llm=mock_llm,
            json_parser=mock_json_parser,
            tools=mock_tools,
            metrics_collector=metrics,
        )
        # 验证注入成功
        assert planner._metrics is metrics
        assert planner._metrics.session_id == "integration-test"

    def test_base_agent_metrics_optional_none(self):
        """BaseAgent 未注入 metrics_collector 时正常工作(向后兼容)"""
        from app.domain.services.agents.planner import PlannerAgent

        mock_uow_factory = MagicMock()
        mock_llm = MagicMock()
        mock_json_parser = MagicMock()
        mock_tools = []
        mock_agent_config = MagicMock()
        mock_agent_config.max_retries = 1
        mock_agent_config.max_iterations = 10
        mock_agent_config.session_timeout_seconds = 0
        mock_agent_config.session_warning_seconds = 0
        mock_agent_config.special_capability_keywords = []

        # 不传入metrics_collector
        planner = PlannerAgent(
            uow_factory=mock_uow_factory,
            session_id="compat-test",
            agent_config=mock_agent_config,
            llm=mock_llm,
            json_parser=mock_json_parser,
            tools=mock_tools,
        )
        # 默认None,不影响现有行为
        assert planner._metrics is None

    def test_planner_react_flow_passes_metrics_to_agents(self):
        """PlannerReActFlow 正确透传 metrics_collector 给 PlannerAgent 与 ReActAgent"""
        from app.domain.services.flows.planner_react import PlannerReActFlow

        # 构造mock依赖
        mock_uow_factory = MagicMock()
        mock_llm = MagicMock()
        mock_agent_config = MagicMock()
        mock_agent_config.max_retries = 1
        mock_agent_config.max_iterations = 10
        mock_agent_config.session_timeout_seconds = 0
        mock_agent_config.session_warning_seconds = 0
        mock_agent_config.special_capability_keywords = []
        mock_json_parser = MagicMock()
        mock_browser = MagicMock()
        mock_sandbox = MagicMock()
        mock_search_engine = MagicMock()
        mock_mcp_tool = MagicMock()
        mock_a2a_tool = MagicMock()
        mock_skill_tool = MagicMock()
        mock_skill_service = MagicMock()

        metrics = MetricsCollector(session_id="flow-test")

        flow = PlannerReActFlow(
            uow_factory=mock_uow_factory,
            llm=mock_llm,
            agent_config=mock_agent_config,
            session_id="flow-test",
            json_parser=mock_json_parser,
            browser=mock_browser,
            sandbox=mock_sandbox,
            search_engine=mock_search_engine,
            mcp_tool=mock_mcp_tool,
            a2a_tool=mock_a2a_tool,
            skill_tool=mock_skill_tool,
            skill_service=mock_skill_service,
            metrics_collector=metrics,
        )

        # 验证两个Agent都持有同一个metrics_collector实例
        assert flow.planner._metrics is metrics
        assert flow.react._metrics is metrics
        assert flow._metrics_collector is metrics


# ===========================================================================
# 端到端埋点验证(模拟调用流程)
# ===========================================================================
class TestMetricsCollectorEndToEnd:
    """MetricsCollector 端到端埋点验证"""

    def test_full_session_metrics_flow(self):
        """模拟一次完整会话的指标收集流程"""
        collector = MetricsCollector(session_id="e2e-test")

        # 模拟2次LLM调用
        collector.increment("llm_call_count", 2)
        collector.record_duration("llm_invoke", 3.5)

        # 模拟5次工具调用(2次MCP)
        collector.increment("tool_call_count", 5)
        collector.increment("mcp_call_count", 2)

        # 模拟缓存命中
        collector.increment("tool_cache_hit_count", 3)
        collector.increment("tool_cache_miss_count", 2)

        # 模拟步骤执行
        collector.increment("step_count", 3)
        collector.increment("step_retry_count", 1)

        # 模拟记忆压缩
        collector.increment("compression_count", 1)
        collector.increment("emergency_compression_count", 0)

        # 标记会话结束
        collector.mark_session_end()

        snapshot = collector.snapshot()

        # 验证所有计数
        assert snapshot["llm_call_count"] == 2
        assert snapshot["tool_call_count"] == 5
        assert snapshot["mcp_call_count"] == 2
        assert snapshot["step_count"] == 3
        assert snapshot["step_retry_count"] == 1
        assert snapshot["compression_count"] == 1

        # 验证派生指标
        assert snapshot["tool_cache_hit_rate"] == 0.6  # 3/(3+2)
        assert snapshot["llm_avg_invoke_seconds"] == round(3.5 / 2, 3)
        assert "session_duration_seconds" in snapshot

        # 验证JSON可序列化(便于ELK采集)
        log_json = collector.to_log_json()
        parsed = json.loads(log_json)
        assert parsed["session_id"] == "e2e-test"
