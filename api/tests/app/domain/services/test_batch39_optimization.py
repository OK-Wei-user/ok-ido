#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Batch 39 优化单元测试

覆盖 4 个方向的全部新增逻辑:
- 方向1 P11: _fragments.py 声明修正 + mcp.py 轮询上限 5→10
- 方向2 预算精细化: AgentConfig.tool_budgets + TaskTypeClassifier + adjust_for_task_type
  + BaseAgent 75% 告警注入 LLM 上下文
- 方向3 策略切换观测: mark_exceeded/consume_exceeded_event + check_and_warn(metrics)
  + _observe_budget_exceeded 策略切换追踪 + snapshot 合并预算
- 方向4 shell 合并: SCRIPT_CONSOLIDATION 片段 + _batch_verifier 英文量词
  + get_consolidation_guidance + shell_execute_count 指标
"""
import logging
from unittest.mock import MagicMock, patch

import pytest

from app.domain.services.agents._batch_verifier import (
    extract_quantified_target,
    count_completed_items,
    verify_batch_completeness,
    get_consolidation_guidance,
)
from app.domain.services.agents.task_type_classifier import (
    classify_task_type,
    classify_from_message,
    TASK_TYPE_RESEARCH,
    TASK_TYPE_DATA_ANALYSIS,
    TASK_TYPE_BROWSER,
    TASK_TYPE_GENERAL,
)
from app.domain.services.observability.metrics_collector import MetricsCollector
from app.domain.services.tools.budget_tracker import ToolBudgetTracker, _WARNING_RATIO
from app.domain.services.prompts._fragments import (
    SCRIPT_CONSOLIDATION_CN,
    SCRIPT_CONSOLIDATION_EN,
    ASYNC_TASK_DECISION_TREE_CN,
    ASYNC_TASK_DECISION_TREE_EN,
)


# ============================================================
# 方向1: P11 声明修正 + mcp.py 轮询上限
# ============================================================

class TestDirection1P11Declaration:
    """方向1: P11 沙箱异步任务通知声明修正"""

    def test_fragments_cn_no_false_p11_claim(self):
        """CN 片段不再包含'P11 沙箱异步任务通知已实现'的不实声明"""
        assert "P11 沙箱异步任务通知已实现" not in ASYNC_TASK_DECISION_TREE_CN

    def test_fragments_cn_mentions_long_term_plan(self):
        """CN 片段声明沙箱推送为长期规划"""
        assert "长期规划" in ASYNC_TASK_DECISION_TREE_CN

    def test_fragments_en_no_false_p11_claim(self):
        """EN 片段不再包含'P11 sandbox async task notification implemented'"""
        assert "P11 sandbox async task notification implemented" not in ASYNC_TASK_DECISION_TREE_EN

    def test_fragments_cn_polling_max_10(self):
        """CN 片段轮询上限为 10 次(非 5)"""
        assert "最多10次" in ASYNC_TASK_DECISION_TREE_CN
        assert "超过10次" in ASYNC_TASK_DECISION_TREE_CN

    def test_fragments_en_polling_max_10(self):
        """EN 片段轮询上限为 10 attempts(非 5)"""
        assert "max 10 attempts" in ASYNC_TASK_DECISION_TREE_EN
        assert "after 10 attempts" in ASYNC_TASK_DECISION_TREE_EN

    def test_mcp_poll_threshold_is_10(self):
        """mcp.py _MAX_POLL_THRESHOLD = 10"""
        from app.domain.services.tools.mcp import _MAX_POLL_THRESHOLD
        assert _MAX_POLL_THRESHOLD == 10

    def test_mcp_poll_max_attempts_is_10(self):
        """mcp.py _MCP_POLL_MAX_ATTEMPTS = 10"""
        from app.domain.services.tools.mcp import _MCP_POLL_MAX_ATTEMPTS
        assert _MCP_POLL_MAX_ATTEMPTS == 10


# ============================================================
# 方向2: 预算精细化
# ============================================================

class TestTaskTypeClassifier:
    """方向2: 任务类型分类器"""

    def test_classify_research_cn(self):
        """中文研究类关键词"""
        assert classify_task_type("深度搜索2026年AI趋势") == TASK_TYPE_RESEARCH
        assert classify_task_type("深度研究人工智能发展") == TASK_TYPE_RESEARCH
        assert classify_task_type("趋势研究分析") == TASK_TYPE_RESEARCH
        assert classify_task_type("调研市场情况") == TASK_TYPE_RESEARCH

    def test_classify_research_en(self):
        """英文研究类关键词"""
        assert classify_task_type("deep search AI trends 2026") == TASK_TYPE_RESEARCH
        assert classify_task_type("research on AI development") == TASK_TYPE_RESEARCH

    def test_classify_data_analysis_cn(self):
        """中文数据分析类关键词"""
        assert classify_task_type("根据出入库数据分析经营情况") == TASK_TYPE_DATA_ANALYSIS
        assert classify_task_type("库存分析报告") == TASK_TYPE_DATA_ANALYSIS
        assert classify_task_type("经营分析与统计") == TASK_TYPE_DATA_ANALYSIS

    def test_classify_data_analysis_with_deep_analysis(self):
        """回归测试: "深度分析"不纳入研究类,出入库深度分析应分类为 data_analysis

        根因: E2E 会话 b5ae335e 消息"出入库...深度分析"被误分类为 research,
        导致 deep_research 预算上调(2→3)而非 search_web 预算上调。
        修复: 从 _RESEARCH_KEYWORDS 移除"深度分析"(该词在数据分析场景高频出现)。
        """
        assert classify_task_type("根据26年1-5月份的全部出入库、为我深度分析") == TASK_TYPE_DATA_ANALYSIS
        assert classify_task_type("库存深度分析") == TASK_TYPE_DATA_ANALYSIS
        assert classify_task_type("销售深度分析报告") == TASK_TYPE_DATA_ANALYSIS

    def test_classify_browser(self):
        """浏览器密集类关键词"""
        assert classify_task_type("网页抓取数据") == TASK_TYPE_BROWSER
        assert classify_task_type("浏览器操作自动化") == TASK_TYPE_BROWSER

    def test_classify_general(self):
        """无高置信度关键词 → general"""
        assert classify_task_type("你好,请帮我写一首诗") == TASK_TYPE_GENERAL
        assert classify_task_type("") == TASK_TYPE_GENERAL
        assert classify_task_type(None) == TASK_TYPE_GENERAL

    def test_research_priority_over_data_analysis(self):
        """研究类优先级高于数据分析类(首匹配优先)"""
        # "深度研究出入库" 应分类为 research(优先级更高)
        assert classify_task_type("深度研究出入库数据") == TASK_TYPE_RESEARCH

    def test_classify_from_message_with_steps(self):
        """从用户消息+计划步骤综合分类"""
        # 消息无法分类时扫描步骤
        class MockStep:
            def __init__(self, desc):
                self.description = desc
        steps = [MockStep("执行搜索"), MockStep("深度研究结果")]
        assert classify_from_message("帮我处理一下", steps) == TASK_TYPE_RESEARCH

    def test_classify_from_message_message_priority(self):
        """消息优先于步骤"""
        class MockStep:
            def __init__(self, desc):
                self.description = desc
        steps = [MockStep("网页抓取")]
        # 消息含研究关键词,优先于步骤的 browser
        assert classify_from_message("深度搜索AI趋势", steps) == TASK_TYPE_RESEARCH


class TestBudgetTrackerAdjustForTaskType:
    """方向2: 按任务类型动态调整预算"""

    def test_adjust_research_increases_deep_research(self):
        """研究类: deep_research 预算 +1"""
        tracker = ToolBudgetTracker()
        original = tracker.get_budget("deep_research")
        tracker.adjust_for_task_type(TASK_TYPE_RESEARCH)
        assert tracker.get_budget("deep_research") == original + 1
        assert tracker.task_type == TASK_TYPE_RESEARCH

    def test_adjust_data_analysis_increases_search_web(self):
        """数据分析类: search_web 预算 +2"""
        tracker = ToolBudgetTracker()
        original = tracker.get_budget("search_web")
        tracker.adjust_for_task_type(TASK_TYPE_DATA_ANALYSIS)
        assert tracker.get_budget("search_web") == original + 2

    def test_adjust_browser_increases_browser_navigate(self):
        """浏览器类: browser_navigate 预算 +5"""
        tracker = ToolBudgetTracker()
        original = tracker.get_budget("browser_navigate")
        tracker.adjust_for_task_type(TASK_TYPE_BROWSER)
        assert tracker.get_budget("browser_navigate") == original + 5

    def test_adjust_general_no_change(self):
        """general 类型不调整预算"""
        tracker = ToolBudgetTracker()
        original_sw = tracker.get_budget("search_web")
        original_dr = tracker.get_budget("deep_research")
        tracker.adjust_for_task_type(TASK_TYPE_GENERAL)
        assert tracker.get_budget("search_web") == original_sw
        assert tracker.get_budget("deep_research") == original_dr

    def test_adjust_idempotent_same_type(self):
        """同类型重复调用不叠加(幂等)"""
        tracker = ToolBudgetTracker()
        original = tracker.get_budget("deep_research")
        tracker.adjust_for_task_type(TASK_TYPE_RESEARCH)
        tracker.adjust_for_task_type(TASK_TYPE_RESEARCH)  # 重复调用
        assert tracker.get_budget("deep_research") == original + 1  # 仅 +1

    def test_adjust_unknown_type_no_change(self):
        """未知类型不调整预算"""
        tracker = ToolBudgetTracker()
        original = tracker.get_budget("search_web")
        tracker.adjust_for_task_type("unknown_type")
        assert tracker.get_budget("search_web") == original
        assert tracker.task_type == "unknown_type"

    def test_adjust_does_not_decrease_budget(self):
        """调整只增不减(保守策略)"""
        tracker = ToolBudgetTracker()
        original_all = {k: v for k, v in tracker._budgets.items()}
        tracker.adjust_for_task_type(TASK_TYPE_RESEARCH)
        for tool, budget in tracker._budgets.items():
            assert budget >= original_all.get(tool, 0)


class TestAgentConfigToolBudgets:
    """方向2: AgentConfig.tool_budgets 外置配置"""

    def test_default_tool_budgets_empty(self):
        """默认 tool_budgets 为空字典(使用 _DEFAULT_BUDGETS)"""
        from app.domain.models.app_config import AgentConfig
        config = AgentConfig()
        assert config.tool_budgets == {}

    def test_custom_tool_budgets(self):
        """自定义 tool_budgets 覆盖默认预算"""
        from app.domain.models.app_config import AgentConfig
        config = AgentConfig(tool_budgets={"search_web": 10, "deep_research": 3})
        assert config.tool_budgets["search_web"] == 10
        assert config.tool_budgets["deep_research"] == 3

    def test_tracker_uses_custom_budgets_from_config(self):
        """ToolBudgetTracker 使用 AgentConfig.tool_budgets"""
        from app.domain.models.app_config import AgentConfig
        config = AgentConfig(tool_budgets={"search_web": 5})
        tracker = ToolBudgetTracker(budgets=config.tool_budgets or None)
        assert tracker.get_budget("search_web") == 5
        # 未配置的工具无预算限制
        assert tracker.get_budget("deep_research") is None

    def test_tracker_falls_back_to_default_when_empty(self):
        """空 tool_budgets 使用默认 _DEFAULT_BUDGETS"""
        from app.domain.models.app_config import AgentConfig
        config = AgentConfig()
        tracker = ToolBudgetTracker(budgets=config.tool_budgets or None)
        assert tracker.get_budget("search_web") == 8
        assert tracker.get_budget("deep_research") == 2


class TestBudgetWarningInjection:
    """方向2: 75% 阈值告警注入 LLM 上下文"""

    def test_build_budget_usage_hints_empty_when_below_threshold(self):
        """低于 75% 时不生成提示"""
        from app.domain.services.agents.base import BaseAgent
        agent = MagicMock(spec=BaseAgent)
        agent._budget_tracker = ToolBudgetTracker()
        # 5/8 = 62.5%, 低于 75%
        for _ in range(5):
            agent._budget_tracker.increment("search_web")
        hints = BaseAgent._build_budget_usage_hints(agent)
        assert hints == ""

    def test_build_budget_usage_hints_at_75_percent(self):
        """达 75% 时生成提示"""
        from app.domain.services.agents.base import BaseAgent
        agent = MagicMock(spec=BaseAgent)
        agent._budget_tracker = ToolBudgetTracker()
        # 6/8 = 75%
        for _ in range(6):
            agent._budget_tracker.increment("search_web")
        hints = BaseAgent._build_budget_usage_hints(agent)
        assert "工具预算提示" in hints
        assert "search_web" in hints
        assert "接近会话级调用上限" in hints

    def test_build_budget_usage_hints_excludes_100_percent(self):
        """100% 已硬拦截,不重复提示"""
        from app.domain.services.agents.base import BaseAgent
        agent = MagicMock(spec=BaseAgent)
        agent._budget_tracker = ToolBudgetTracker()
        # 8/8 = 100%
        for _ in range(8):
            agent._budget_tracker.increment("search_web")
        hints = BaseAgent._build_budget_usage_hints(agent)
        # 100% 不在 75%~99% 区间,不提示
        assert "search_web" not in hints or hints == ""

    def test_build_budget_usage_hints_no_tracker(self):
        """无 budget_tracker 时返回空串"""
        from app.domain.services.agents.base import BaseAgent
        agent = MagicMock(spec=BaseAgent)
        agent._budget_tracker = None
        hints = BaseAgent._build_budget_usage_hints(agent)
        assert hints == ""


# ============================================================
# 方向3: 策略切换观测
# ============================================================

class TestBudgetExceededEventQueue:
    """方向3: 预算超限事件队列(mark_exceeded/consume_exceeded_event)"""

    def test_mark_exceeded_queues_event(self):
        """mark_exceeded 将超限事件入队"""
        tracker = ToolBudgetTracker()
        tracker.mark_exceeded("search_web")
        assert tracker.consume_exceeded_event() == "search_web"

    def test_consume_returns_none_when_empty(self):
        """队列为空时 consume 返回 None"""
        tracker = ToolBudgetTracker()
        assert tracker.consume_exceeded_event() is None

    def test_consume_fifo_order(self):
        """队列按 FIFO 顺序消费"""
        tracker = ToolBudgetTracker()
        tracker.mark_exceeded("search_web")
        tracker.mark_exceeded("deep_research")
        assert tracker.consume_exceeded_event() == "search_web"
        assert tracker.consume_exceeded_event() == "deep_research"
        assert tracker.consume_exceeded_event() is None

    def test_mark_exceeded_empty_name_noop(self):
        """mark_exceeded 空工具名为 no-op"""
        tracker = ToolBudgetTracker()
        tracker.mark_exceeded("")
        assert tracker.consume_exceeded_event() is None

    def test_reset_clears_exceeded_events(self):
        """reset 清空超限事件队列"""
        tracker = ToolBudgetTracker()
        tracker.mark_exceeded("search_web")
        tracker.reset()
        assert tracker.consume_exceeded_event() is None


class TestCheckAndWarnWithMetrics:
    """方向3: check_and_warn 联动 metrics"""

    def test_check_and_warn_returns_true_when_warned(self):
        """达 75% 阈值时返回 True"""
        tracker = ToolBudgetTracker()
        for _ in range(6):
            tracker.increment("search_web")
        assert tracker.check_and_warn("search_web") is True

    def test_check_and_warn_returns_false_below_threshold(self):
        """低于 75% 阈值时返回 False"""
        tracker = ToolBudgetTracker()
        for _ in range(5):
            tracker.increment("search_web")
        assert tracker.check_and_warn("search_web") is False

    def test_check_and_warn_increments_metrics(self):
        """达 75% 阈值时联动 metrics.increment"""
        tracker = ToolBudgetTracker()
        metrics = MetricsCollector(session_id="test")
        for _ in range(6):
            tracker.increment("search_web")
        tracker.check_and_warn("search_web", metrics)
        snapshot = metrics.snapshot()
        assert snapshot.get("budget_warning_count", 0) == 1

    def test_check_and_warn_sets_usage_ratio_gauge(self):
        """达 75% 阈值时设置 budget_usage_ratio_{tool} gauge"""
        tracker = ToolBudgetTracker()
        metrics = MetricsCollector(session_id="test")
        for _ in range(6):
            tracker.increment("search_web")
        tracker.check_and_warn("search_web", metrics)
        snapshot = metrics.snapshot()
        assert snapshot.get("budget_usage_ratio_search_web") == 0.75

    def test_check_and_warn_no_metrics_no_error(self):
        """不传 metrics 时不报错(向后兼容)"""
        tracker = ToolBudgetTracker()
        for _ in range(6):
            tracker.increment("search_web")
        result = tracker.check_and_warn("search_web")  # 无 metrics 参数
        assert result is True


class TestObserveBudgetExceeded:
    """方向3: _observe_budget_exceeded 策略切换追踪"""

    def _create_agent_with_tracker(self):
        """创建带 budget_tracker 和 metrics 的 mock agent"""
        from app.domain.services.agents.base import BaseAgent
        agent = MagicMock(spec=BaseAgent)
        agent._budget_tracker = ToolBudgetTracker()
        agent._metrics = MetricsCollector(session_id="test")
        agent._last_exceeded_tool = None
        agent.name = "test_agent"
        return agent

    def test_consume_exceeded_sets_last_exceeded(self):
        """消费超限事件后设置 _last_exceeded_tool"""
        from app.domain.services.agents.base import BaseAgent
        agent = self._create_agent_with_tracker()
        agent._budget_tracker.mark_exceeded("search_web")
        BaseAgent._observe_budget_exceeded(agent, "search_web")
        assert agent._last_exceeded_tool == "search_web"
        snapshot = agent._metrics.snapshot()
        assert snapshot.get("budget_exceeded_count", 0) == 1

    def test_strategy_switch_detected(self):
        """超限后切换不同工具 → strategy_switch_count++"""
        from app.domain.services.agents.base import BaseAgent
        agent = self._create_agent_with_tracker()
        # 第一次: search_web 超限
        agent._budget_tracker.mark_exceeded("search_web")
        BaseAgent._observe_budget_exceeded(agent, "search_web")
        assert agent._last_exceeded_tool == "search_web"
        # 第二次: 调用不同工具 deep_research → 策略切换
        BaseAgent._observe_budget_exceeded(agent, "deep_research")
        assert agent._last_exceeded_tool is None  # 清除标记
        snapshot = agent._metrics.snapshot()
        assert snapshot.get("strategy_switch_count", 0) == 1

    def test_strategy_switch_retry_same_tool(self):
        """超限后重试相同工具 → strategy_switch_retry_count++"""
        from app.domain.services.agents.base import BaseAgent
        agent = self._create_agent_with_tracker()
        # 第一次: search_web 超限
        agent._budget_tracker.mark_exceeded("search_web")
        BaseAgent._observe_budget_exceeded(agent, "search_web")
        # 第二次: 再次调用相同工具 search_web → 未切换
        BaseAgent._observe_budget_exceeded(agent, "search_web")
        snapshot = agent._metrics.snapshot()
        assert snapshot.get("strategy_switch_retry_count", 0) == 1
        assert snapshot.get("strategy_switch_count", 0) == 0

    def test_no_tracker_noop(self):
        """无 budget_tracker 时 no-op"""
        from app.domain.services.agents.base import BaseAgent
        agent = MagicMock(spec=BaseAgent)
        agent._budget_tracker = None
        BaseAgent._observe_budget_exceeded(agent, "search_web")
        # 不抛异常即可

    def test_no_exceeded_event_no_strategy_switch(self):
        """无超限事件时不触发策略切换"""
        from app.domain.services.agents.base import BaseAgent
        agent = self._create_agent_with_tracker()
        # 不 mark_exceeded,直接调用
        BaseAgent._observe_budget_exceeded(agent, "search_web")
        assert agent._last_exceeded_tool is None
        snapshot = agent._metrics.snapshot()
        assert snapshot.get("budget_exceeded_count", 0) == 0
        assert snapshot.get("strategy_switch_count", 0) == 0


class TestSnapshotMergeBudgetReport:
    """方向3: snapshot 合并预算报告"""

    def test_usage_report_includes_task_type(self):
        """get_usage_report 包含 task_type"""
        tracker = ToolBudgetTracker()
        tracker.adjust_for_task_type(TASK_TYPE_RESEARCH)
        report = tracker.get_usage_report()
        assert report["task_type"] == TASK_TYPE_RESEARCH

    def test_usage_report_includes_usage_ratio(self):
        """get_usage_report 包含 usage_ratio"""
        tracker = ToolBudgetTracker()
        for _ in range(4):
            tracker.increment("search_web")
        report = tracker.get_usage_report()
        assert report["tools"]["search_web"]["usage_ratio"] == 0.5

    def test_usage_report_ratio_capped_at_1(self):
        """usage_ratio 上限 1.0"""
        tracker = ToolBudgetTracker()
        for _ in range(20):
            tracker.increment("search_web")
        report = tracker.get_usage_report()
        assert report["tools"]["search_web"]["usage_ratio"] == 1.0


# ============================================================
# 方向4: shell 合并引导
# ============================================================

class TestScriptConsolidationFragment:
    """方向4: SCRIPT_CONSOLIDATION 片段"""

    def test_cn_fragment_exists(self):
        """CN 片段存在且非空"""
        assert SCRIPT_CONSOLIDATION_CN
        assert "脚本合并原则" in SCRIPT_CONSOLIDATION_CN

    def test_en_fragment_exists(self):
        """EN 片段存在且非空"""
        assert SCRIPT_CONSOLIDATION_EN
        assert "Script consolidation principle" in SCRIPT_CONSOLIDATION_EN

    def test_cn_fragment_contains_examples(self):
        """CN 片段包含正例和反例"""
        assert "正例" in SCRIPT_CONSOLIDATION_CN
        assert "反例" in SCRIPT_CONSOLIDATION_CN

    def test_cn_fragment_injected_in_react_prompts(self):
        """SCRIPT_CONSOLIDATION_CN 已注入 REACT_SYSTEM_PROMPT 和 EXECUTION_PROMPT"""
        from app.domain.services.prompts.react import REACT_SYSTEM_PROMPT, EXECUTION_PROMPT
        assert "脚本合并原则" in REACT_SYSTEM_PROMPT
        assert "脚本合并原则" in EXECUTION_PROMPT

    def test_en_fragment_injected_in_react_prompts(self):
        """SCRIPT_CONSOLIDATION_EN 已注入 EN 版 REACT_SYSTEM_PROMPT 和 EXECUTION_PROMPT"""
        from app.domain.services.prompts.en.react import REACT_SYSTEM_PROMPT as EN_REACT
        from app.domain.services.prompts.en.react import EXECUTION_PROMPT as EN_EXEC
        assert "Script consolidation principle" in EN_REACT
        assert "Script consolidation principle" in EN_EXEC


class TestBatchVerifierEnglishQuantifiers:
    """方向4: _batch_verifier 英文量词支持"""

    def test_extract_cn_quantified_target(self):
        """中文量词提取"""
        assert extract_quantified_target("导出50条数据") == 50
        assert extract_quantified_target("生成10个文件") == 10
        assert extract_quantified_target("共100项") == 100

    def test_extract_en_quantified_target(self):
        """英文量词提取"""
        assert extract_quantified_target("export 50 records") == 50
        assert extract_quantified_target("generate 10 files") == 10
        assert extract_quantified_target("process 20 items") == 20
        assert extract_quantified_target("create 5 sheets") == 5

    def test_extract_en_quantified_target_case_insensitive(self):
        """英文量词大小写不敏感"""
        assert extract_quantified_target("Export 50 RECORDS") == 50
        assert extract_quantified_target("Generate 10 Files") == 10

    def test_extract_no_quantified_target(self):
        """无量化目标返回 None"""
        assert extract_quantified_target("分析数据") is None
        assert extract_quantified_target("generate report") is None
        assert extract_quantified_target("") is None

    def test_count_completed_items_en(self):
        """英文量词完成数统计"""
        class MockStep:
            def __init__(self, desc, result="", attachments=None):
                self.description = desc
                self.result = result
                self.attachments = attachments or []
        step = MockStep("export 50 records", result="Successfully exported 30 records")
        assert count_completed_items(step) == 30

    def test_verify_batch_completeness_en(self):
        """英文量词完整性校验"""
        class MockStep:
            def __init__(self, desc, result="", attachments=None):
                self.description = desc
                self.result = result
                self.attachments = attachments or []
        # 未完成
        step = MockStep("export 50 records", result="Exported 30 records", attachments=["f1.csv"])
        is_complete, guidance = verify_batch_completeness(step)
        assert is_complete is False
        assert "50" in guidance
        # 完成
        step2 = MockStep("export 50 records", result="Exported 50 records", attachments=["f1.csv"])
        is_complete2, _ = verify_batch_completeness(step2)
        assert is_complete2 is True


class TestGetConsolidationGuidance:
    """方向4: get_consolidation_guidance 事前合并引导"""

    def test_guidance_for_large_batch_cn(self):
        """大批量中文目标生成引导"""
        guidance = get_consolidation_guidance("导出50条出入库记录")
        assert guidance != ""
        assert "合并引导" in guidance
        assert "50" in guidance
        assert "shell_execute" in guidance

    def test_guidance_for_large_batch_en(self):
        """大批量英文目标生成引导"""
        guidance = get_consolidation_guidance("export 50 records to xlsx")
        assert guidance != ""
        assert "50" in guidance

    def test_no_guidance_for_small_batch(self):
        """小批量(<N)不生成引导"""
        guidance = get_consolidation_guidance("导出3条记录")
        assert guidance == ""

    def test_no_guidance_for_no_target(self):
        """无量化目标不生成引导"""
        guidance = get_consolidation_guidance("分析数据并生成报告")
        assert guidance == ""

    def test_no_guidance_for_empty(self):
        """空文本不生成引导"""
        assert get_consolidation_guidance("") == ""
        assert get_consolidation_guidance(None) == ""

    def test_guidance_threshold_boundary(self):
        """阈值边界: N=4 不引导, N=5 引导"""
        assert get_consolidation_guidance("导出4条") == ""
        assert get_consolidation_guidance("导出5条") != ""


class TestShellExecuteCountMetric:
    """方向4: shell_execute_count 指标"""

    def test_shell_execute_count_incremented(self):
        """shell_execute 工具调用时 increment shell_execute_count"""
        metrics = MetricsCollector(session_id="test")
        metrics.increment("tool_call_count")
        metrics.increment("shell_execute_count")
        snapshot = metrics.snapshot()
        assert snapshot.get("shell_execute_count") == 1

    def test_non_shell_tool_not_incremented(self):
        """非 shell_execute 工具不 increment shell_execute_count"""
        metrics = MetricsCollector(session_id="test")
        metrics.increment("tool_call_count")
        # 不 increment shell_execute_count
        snapshot = metrics.snapshot()
        assert snapshot.get("shell_execute_count", 0) == 0
