# -*- coding: utf-8 -*-
"""批次45 P1-2: shell_execute 频次主动引导增强单元测试

覆盖双通道合并引导:
- 通道1(原有): get_consolidation_guidance 量化目标 N>=5 触发
- 通道2(批次45新增): 数据分析关键词触发(覆盖无量化目标的数据分析任务)
- ShellCallProfiler.total_calls 属性(供 BaseAgent 频次阈值判断)
- BaseAgent._build_shell_execute_guidance 频次阈值事中注入
"""
from unittest.mock import MagicMock

import pytest

from app.domain.models.app_config import AgentConfig
from app.domain.services.agents._batch_verifier import (
    get_consolidation_guidance,
    _DATA_ANALYSIS_KEYWORDS,
    _CONSOLIDATION_MIN_TARGET,
)
from app.domain.services.observability.shell_call_profiler import ShellCallProfiler


# ============================================================
# 通道2: 数据分析关键词触发合并引导
# ============================================================

class TestConsolidationGuidanceDataAnalysisKeywords:
    """P1-2 通道2: 数据分析关键词触发合并引导"""

    def test_data_analysis_keyword_triggers(self):
        """'数据分析'关键词应触发合并引导"""
        guidance = get_consolidation_guidance("对数据库进行数据分析并生成报表")
        assert guidance != ""
        assert "合并引导" in guidance
        assert "Python 脚本" in guidance

    def test_churuku_keyword_triggers(self):
        """'出入库'关键词应触发合并引导(覆盖批次44会话场景)"""
        guidance = get_consolidation_guidance("统计1-5月出入库数据")
        assert guidance != ""
        assert "合并引导" in guidance

    def test_operating_analysis_keyword_triggers(self):
        """'经营分析'关键词应触发合并引导"""
        guidance = get_consolidation_guidance("完成经营分析报告")
        assert guidance != ""

    def test_general_step_no_trigger(self):
        """非数据分析通用步骤不应触发"""
        guidance = get_consolidation_guidance("查询今天北京的天气")
        assert guidance == ""

    def test_empty_string_no_trigger(self):
        """空字符串不应触发"""
        assert get_consolidation_guidance("") == ""

    def test_none_like_no_trigger(self):
        """None入参不应触发(防御性)"""
        assert get_consolidation_guidance(None) == ""


class TestConsolidationGuidanceQuantifiedTargetPriority:
    """P1-2 通道1优先级: 量化目标 N>=5 时返回量化引导(而非关键词引导)"""

    def test_quantified_target_priority_over_keyword(self):
        """含量化目标N>=5 + 数据分析关键词时,应返回量化引导(通道1优先)"""
        guidance = get_consolidation_guidance("导出10条数据分析结果")
        assert guidance != ""
        # 通道1的量化引导含具体数字
        assert "10" in guidance
        assert "量化目标" in guidance

    def test_quantified_target_below_threshold_falls_to_keyword(self):
        """量化目标N<5时,若含数据分析关键词,应走通道2关键词引导"""
        guidance = get_consolidation_guidance("导出3条数据分析结果")
        assert guidance != ""
        # 通道2的关键词引导(不含"量化目标")
        assert "数据分析任务" in guidance

    def test_data_analysis_keywords_defined(self):
        """_DATA_ANALYSIS_KEYWORDS 应包含核心数据分析关键词"""
        assert "数据分析" in _DATA_ANALYSIS_KEYWORDS
        assert "出入库" in _DATA_ANALYSIS_KEYWORDS
        assert "经营分析" in _DATA_ANALYSIS_KEYWORDS


# ============================================================
# ShellCallProfiler.total_calls 属性
# ============================================================

class TestShellCallProfilerTotalCalls:
    """P1-2: ShellCallProfiler.total_calls 只读属性"""

    def test_total_calls_initial_zero(self):
        """新建profiler total_calls应为0"""
        profiler = ShellCallProfiler()
        assert profiler.total_calls == 0

    def test_total_calls_after_record(self):
        """record后total_calls应递增"""
        profiler = ShellCallProfiler()
        profiler.record("ls -la")
        profiler.record("pwd")
        assert profiler.total_calls == 2

    def test_total_calls_after_reset(self):
        """reset后total_calls应归0"""
        profiler = ShellCallProfiler()
        profiler.record("ls")
        profiler.reset()
        assert profiler.total_calls == 0


# ============================================================
# BaseAgent._build_shell_execute_guidance 频次阈值注入
# ============================================================

class TestBaseAgentShellExecuteGuidance:
    """P1-2: BaseAgent._build_shell_execute_guidance 频次阈值事中注入"""

    def _build_agent(self) -> MagicMock:
        """构建BaseAgent实例(绕过__init__)用于测试_build_shell_execute_guidance"""
        from app.domain.services.agents.base import BaseAgent
        agent = object.__new__(BaseAgent)
        agent.name = "test_agent"
        agent._session_id = "test_session"
        agent._shell_guidance_injected = False
        agent._shell_profiler = ShellCallProfiler()
        agent._budget_tracker = None
        agent._agent_config = AgentConfig(max_iterations=100)
        return agent

    def test_below_threshold_no_guidance(self):
        """累计调用<15次时不应注入引导"""
        agent = self._build_agent()
        for _ in range(14):
            agent._shell_profiler.record("cmd")
        assert agent._build_shell_execute_guidance() == ""
        assert agent._shell_guidance_injected is False

    def test_at_threshold_triggers(self):
        """累计调用>=15次时应注入引导"""
        agent = self._build_agent()
        for _ in range(15):
            agent._shell_profiler.record("cmd")
        guidance = agent._build_shell_execute_guidance()
        assert guidance != ""
        assert "系统效率提示" in guidance
        assert "15" in guidance
        assert agent._shell_guidance_injected is True

    def test_injected_only_once(self):
        """引导仅注入一次,第二次调用返回空"""
        agent = self._build_agent()
        for _ in range(20):
            agent._shell_profiler.record("cmd")
        first = agent._build_shell_execute_guidance()
        second = agent._build_shell_execute_guidance()
        assert first != ""
        assert second == ""

    def test_no_profiler_no_guidance(self):
        """无_shell_profiler时不应注入(防御性)"""
        from app.domain.services.agents.base import BaseAgent
        agent = object.__new__(BaseAgent)
        agent._shell_guidance_injected = False
        agent._shell_profiler = None
        assert agent._build_shell_execute_guidance() == ""
