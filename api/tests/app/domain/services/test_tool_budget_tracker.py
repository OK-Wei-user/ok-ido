#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_tool_budget_tracker.py
工具调用预算会话级追踪器单元测试

验证 project_memory 硬约束:
- search_web=8/deep_research=2/browser_navigate=10 会话级上限
- 100% 阈值硬拦截(返回错误 ToolResult)
- 75% 阈值软告警(每工具仅告警一次)
- 新用户消息时由 Flow 调用 reset() 重置
"""
import logging
from unittest.mock import MagicMock

import pytest

from app.domain.services.tools.budget_tracker import ToolBudgetTracker


class TestToolBudgetTrackerBasic:
    """基础功能测试"""

    def test_default_budgets(self):
        """默认预算: search_web=8/deep_research=2/browser_navigate=10/browser_console_exec=10"""
        tracker = ToolBudgetTracker()
        assert tracker.get_budget("search_web") == 8
        assert tracker.get_budget("deep_research") == 2
        assert tracker.get_budget("browser_navigate") == 10
        assert tracker.get_budget("browser_console_exec") == 10

    def test_custom_budgets(self):
        """自定义预算字典"""
        tracker = ToolBudgetTracker(budgets={"search_web": 3})
        assert tracker.get_budget("search_web") == 3
        # 未配置的工具无预算限制
        assert tracker.get_budget("deep_research") is None
        assert tracker.get_budget("browser_navigate") is None

    def test_empty_budgets_disables_all(self):
        """空字典禁用所有预算检查(向后兼容)"""
        tracker = ToolBudgetTracker(budgets={})
        assert tracker.get_budget("search_web") is None
        assert tracker.is_exceeded("search_web") is False
        tracker.increment("search_web")
        assert tracker.is_exceeded("search_web") is False

    def test_initial_count_zero(self):
        """初始计数为0"""
        tracker = ToolBudgetTracker()
        assert tracker.get_count("search_web") == 0
        assert tracker.get_count("unknown_tool") == 0

    def test_increment_records_count(self):
        """increment 正确累加计数"""
        tracker = ToolBudgetTracker()
        tracker.increment("search_web")
        tracker.increment("search_web")
        tracker.increment("search_web")
        assert tracker.get_count("search_web") == 3

    def test_increment_empty_name_noop(self):
        """increment 空工具名为 no-op"""
        tracker = ToolBudgetTracker()
        tracker.increment("")
        assert all(v == 0 for v in tracker._counts.values())


class TestDecrement:
    """decrement() 预占回退测试(预占式预算失败时调用)"""

    def test_decrement_reduces_count(self):
        """decrement 正确减少计数"""
        tracker = ToolBudgetTracker()
        tracker.increment("search_web")
        tracker.increment("search_web")
        assert tracker.get_count("search_web") == 2

        tracker.decrement("search_web")
        assert tracker.get_count("search_web") == 1

    def test_decrement_to_zero(self):
        """decrement 到0后不再减少(不会变负)"""
        tracker = ToolBudgetTracker()
        tracker.increment("search_web")
        tracker.decrement("search_web")
        assert tracker.get_count("search_web") == 0

        # 再次 decrement 不会变负
        tracker.decrement("search_web")
        assert tracker.get_count("search_web") == 0

    def test_decrement_unknown_tool_noop(self):
        """decrement 未计数工具为 no-op(不会变负)"""
        tracker = ToolBudgetTracker()
        tracker.decrement("search_web")
        assert tracker.get_count("search_web") == 0

    def test_decrement_empty_name_noop(self):
        """decrement 空工具名为 no-op"""
        tracker = ToolBudgetTracker()
        tracker.increment("search_web")
        tracker.decrement("")
        assert tracker.get_count("search_web") == 1

    def test_decrement_allows_retry_after_exceeded(self):
        """decrement 后,之前超限的工具可再次调用(预占回退场景)"""
        tracker = ToolBudgetTracker()
        # search_web 达上限
        for _ in range(8):
            tracker.increment("search_web")
        assert tracker.is_exceeded("search_web") is True

        # 回退1次后不再超限(LLM可重试)
        tracker.decrement("search_web")
        assert tracker.is_exceeded("search_web") is False
        assert tracker.get_count("search_web") == 7

    def test_decrement_independent_per_tool(self):
        """decrement 仅影响指定工具(不干扰其他工具计数)"""
        tracker = ToolBudgetTracker()
        tracker.increment("search_web")
        tracker.increment("deep_research")
        tracker.increment("deep_research")

        tracker.decrement("search_web")

        assert tracker.get_count("search_web") == 0
        assert tracker.get_count("deep_research") == 2


class TestBudgetExceeded:
    """预算上限硬拦截测试"""

    def test_search_web_not_exceeded_below_8(self):
        """search_web 7次未超限"""
        tracker = ToolBudgetTracker()
        for _ in range(7):
            tracker.increment("search_web")
        assert tracker.is_exceeded("search_web") is False

    def test_search_web_exceeded_at_8(self):
        """search_web 8次达上限"""
        tracker = ToolBudgetTracker()
        for _ in range(8):
            tracker.increment("search_web")
        assert tracker.is_exceeded("search_web") is True

    def test_deep_research_exceeded_at_2(self):
        """deep_research 2次达上限"""
        tracker = ToolBudgetTracker()
        tracker.increment("deep_research")
        assert tracker.is_exceeded("deep_research") is False
        tracker.increment("deep_research")
        assert tracker.is_exceeded("deep_research") is True

    def test_browser_navigate_exceeded_at_10(self):
        """browser_navigate 10次达上限"""
        tracker = ToolBudgetTracker()
        for _ in range(10):
            tracker.increment("browser_navigate")
        assert tracker.is_exceeded("browser_navigate") is True

    def test_browser_console_exec_exceeded_at_10(self):
        """browser_console_exec 10次达上限(防止LLM滥用console_exec提取页面内容)"""
        tracker = ToolBudgetTracker()
        for _ in range(9):
            tracker.increment("browser_console_exec")
        assert tracker.is_exceeded("browser_console_exec") is False
        tracker.increment("browser_console_exec")
        assert tracker.is_exceeded("browser_console_exec") is True

    def test_unknown_tool_never_exceeded(self):
        """未配置预算的工具永不超过上限"""
        tracker = ToolBudgetTracker()
        for _ in range(100):
            tracker.increment("unknown_tool")
        assert tracker.is_exceeded("unknown_tool") is False


class TestUsageRatioAndWarn:
    """使用率与75%阈值告警测试"""

    def test_usage_ratio_zero_initially(self):
        """初始使用率0.0"""
        tracker = ToolBudgetTracker()
        assert tracker.get_usage_ratio("search_web") == 0.0

    def test_usage_ratio_half(self):
        """search_web 4次使用率0.5"""
        tracker = ToolBudgetTracker()
        for _ in range(4):
            tracker.increment("search_web")
        assert tracker.get_usage_ratio("search_web") == 0.5

    def test_usage_ratio_capped_at_1(self):
        """使用率上限1.0(超过budget也返回1.0)"""
        tracker = ToolBudgetTracker()
        for _ in range(20):
            tracker.increment("search_web")
        assert tracker.get_usage_ratio("search_web") == 1.0

    def test_usage_ratio_unknown_tool_zero(self):
        """未配置预算的工具使用率0.0"""
        tracker = ToolBudgetTracker()
        tracker.increment("unknown_tool")
        assert tracker.get_usage_ratio("unknown_tool") == 0.0

    def test_check_and_warn_logs_at_75_percent(self, caplog):
        """75%阈值记录INFO日志"""
        tracker = ToolBudgetTracker()
        # search_web budget=8, 75%阈值=6次
        for _ in range(6):
            tracker.increment("search_web")

        with caplog.at_level(logging.INFO, logger="app.domain.services.tools.budget_tracker"):
            tracker.check_and_warn("search_web")

        # 至少一条INFO日志包含告警信息
        assert any("search_web" in r.message and "75%" in r.message for r in caplog.records)

    def test_check_and_warn_only_once_per_tool(self, caplog):
        """每工具仅告警一次(避免日志噪音)"""
        tracker = ToolBudgetTracker()
        for _ in range(7):
            tracker.increment("search_web")

        with caplog.at_level(logging.INFO, logger="app.domain.services.tools.budget_tracker"):
            tracker.check_and_warn("search_web")
            tracker.check_and_warn("search_web")
            tracker.check_and_warn("search_web")

        # 只有一条日志(去重): 匹配 "告警阈值" 关键词
        warn_records = [r for r in caplog.records if "search_web" in r.message and "告警阈值" in r.message]
        assert len(warn_records) == 1

    def test_check_and_warn_below_threshold_no_log(self, caplog):
        """低于75%阈值不记录告警"""
        tracker = ToolBudgetTracker()
        for _ in range(5):
            tracker.increment("search_web")  # 5/8 = 62.5%

        with caplog.at_level(logging.INFO, logger="app.domain.services.tools.budget_tracker"):
            tracker.check_and_warn("search_web")

        assert not any("75%" in r.message for r in caplog.records)

    def test_check_and_warn_empty_name_noop(self, caplog):
        """空工具名 no-op"""
        tracker = ToolBudgetTracker()
        with caplog.at_level(logging.INFO, logger="app.domain.services.tools.budget_tracker"):
            tracker.check_and_warn("")
        assert not caplog.records


class TestReset:
    """reset() 新用户消息重置测试"""

    def test_reset_clears_counts(self):
        """reset 清空所有工具调用计数"""
        tracker = ToolBudgetTracker()
        tracker.increment("search_web")
        tracker.increment("deep_research")
        tracker.increment("browser_navigate")

        tracker.reset()

        assert tracker.get_count("search_web") == 0
        assert tracker.get_count("deep_research") == 0
        assert tracker.get_count("browser_navigate") == 0

    def test_reset_clears_warned_set(self, caplog):
        """reset 清空已告警集合(再次达75%可重新告警)"""
        tracker = ToolBudgetTracker()
        # 第一次告警
        for _ in range(6):
            tracker.increment("search_web")
        with caplog.at_level(logging.INFO, logger="app.domain.services.tools.budget_tracker"):
            tracker.check_and_warn("search_web")
        assert len(caplog.records) == 1

        # reset 后再次达75%应再次告警
        tracker.reset()
        for _ in range(6):
            tracker.increment("search_web")
        with caplog.at_level(logging.INFO, logger="app.domain.services.tools.budget_tracker"):
            tracker.check_and_warn("search_web")
        # 第二次告警存在(reset 清空了 warned 集合)
        assert any("search_web" in r.message and "75%" in r.message for r in caplog.records)

    def test_reset_allows_calls_after_exceeded(self):
        """reset 后,之前超限的工具可再次调用"""
        tracker = ToolBudgetTracker()
        # search_web 达上限
        for _ in range(8):
            tracker.increment("search_web")
        assert tracker.is_exceeded("search_web") is True

        # reset 后不再超限
        tracker.reset()
        assert tracker.is_exceeded("search_web") is False
        assert tracker.get_count("search_web") == 0


class TestUsageReport:
    """get_usage_report() 测试"""

    def test_report_includes_all_budgeted_tools(self):
        """报告包含所有受预算工具(Batch 39: 结构改为 task_type + tools)"""
        tracker = ToolBudgetTracker()
        report = tracker.get_usage_report()
        assert "task_type" in report
        assert "tools" in report
        assert "search_web" in report["tools"]
        assert "deep_research" in report["tools"]
        assert "browser_navigate" in report["tools"]
        assert "browser_console_exec" in report["tools"]

    def test_report_shows_count_and_budget(self):
        """报告显示当前计数与预算上限(Batch 39: 含 usage_ratio)"""
        tracker = ToolBudgetTracker()
        tracker.increment("search_web")
        tracker.increment("search_web")
        report = tracker.get_usage_report()
        sw = report["tools"]["search_web"]
        assert sw["count"] == 2
        assert sw["budget"] == 8
        assert sw["usage_ratio"] == 0.25
        assert report["tools"]["deep_research"]["count"] == 0

    def test_report_custom_budgets(self):
        """自定义预算的报告反映自定义值"""
        tracker = ToolBudgetTracker(budgets={"search_web": 5})
        report = tracker.get_usage_report()
        assert report["tools"]["search_web"]["count"] == 0
        assert report["tools"]["search_web"]["budget"] == 5


class TestRaiseBudget:
    """raise_budget() 动态上调预算测试(方案B/会话437cbc75根因修复)

    复杂企业App(交互元素>200)content易被截断,静态10次console_exec
    硬上限不够用。browser_view检测到复杂页面后调用此方法动态放宽。
    """

    def test_raise_budget_increases_limit(self):
        """raise_budget 正确上调预算上限"""
        tracker = ToolBudgetTracker()
        assert tracker.get_budget("browser_console_exec") == 10
        assert tracker.raise_budget("browser_console_exec", 20) is True
        assert tracker.get_budget("browser_console_exec") == 20

    def test_raise_budget_idempotent(self):
        """重复调用相同值不叠加(幂等安全)"""
        tracker = ToolBudgetTracker()
        tracker.raise_budget("browser_console_exec", 20)
        tracker.raise_budget("browser_console_exec", 20)
        tracker.raise_budget("browser_console_exec", 20)
        assert tracker.get_budget("browser_console_exec") == 20

    def test_raise_budget_never_decreases(self):
        """new_budget小于当前预算时不生效(仅增不减)"""
        tracker = ToolBudgetTracker()
        tracker.raise_budget("browser_console_exec", 20)
        assert tracker.raise_budget("browser_console_exec", 5) is False
        assert tracker.get_budget("browser_console_exec") == 20

    def test_raise_budget_unconfigured_tool_returns_false(self):
        """未配置预算的工具调用raise_budget返回False"""
        tracker = ToolBudgetTracker(budgets={})
        assert tracker.raise_budget("browser_console_exec", 20) is False

    def test_raise_budget_allows_more_calls_before_exceeded(self):
        """上调预算后,LLM可调用更多次才触发硬拦截"""
        tracker = ToolBudgetTracker()
        # 默认10次上限
        for _ in range(10):
            tracker.increment("browser_console_exec")
        assert tracker.is_exceeded("browser_console_exec") is True
        # 上调到20后不再超限
        tracker.raise_budget("browser_console_exec", 20)
        assert tracker.is_exceeded("browser_console_exec") is False
        # 可继续调用至20次
        for _ in range(10):
            tracker.increment("browser_console_exec")
        assert tracker.is_exceeded("browser_console_exec") is True

    def test_raise_budget_logs_change(self, caplog):
        """预算上调时记录INFO日志(可观测)"""
        tracker = ToolBudgetTracker()
        with caplog.at_level(logging.INFO, logger="app.domain.services.tools.budget_tracker"):
            tracker.raise_budget("browser_console_exec", 20)
        assert any(
            "browser_console_exec" in r.message and "20" in r.message
            for r in caplog.records
        ), "应记录预算上调日志"
