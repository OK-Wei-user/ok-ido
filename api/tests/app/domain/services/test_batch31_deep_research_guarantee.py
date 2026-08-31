#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Batch 31 (F11-2): deep_research 调用保障 单元测试

验证研究类步骤强制装配 deep_research 工具的预校验逻辑,
确保 F10-6 关键词漏命中时 LLM 仍能看到 deep_research 工具。
"""
import pytest
from unittest.mock import MagicMock, patch

from app.domain.services.agents.react import ReActAgent, _RESEARCH_KEYWORDS
from app.domain.services.agents.base import BaseAgent


class TestResearchKeywords:
    """研究类关键词覆盖测试"""

    def test_research_keywords_include_standalone_words(self):
        """Batch 31扩展: "研究"/"分析"独立词应包含在关键词中"""
        assert "研究" in _RESEARCH_KEYWORDS
        assert "分析" in _RESEARCH_KEYWORDS

    def test_research_keywords_include_original_deep_research_terms(self):
        """原 F10-6 deep_research 关键词应全部保留"""
        original_terms = (
            "深度研究", "deep_research", "调研", "深度搜索", "深度分析",
            "深度调研", "深入研究", "综合研究", "趋势研究", "全面分析",
            "多角度分析", "深度挖掘",
        )
        for term in original_terms:
            assert term in _RESEARCH_KEYWORDS, f"原关键词[{term}]应保留"


class TestEnsureResearchToolAssembled:
    """_ensure_research_tool_assembled 预校验测试"""

    def _create_react_agent_mock(self):
        """创建 ReActAgent mock(避免完整初始化依赖)"""
        agent = MagicMock(spec=ReActAgent)
        agent.force_include_tool = MagicMock()
        # 绑定真实方法到 mock 上
        agent._ensure_research_tool_assembled = ReActAgent._ensure_research_tool_assembled.__get__(agent, ReActAgent)
        return agent

    def test_step_with_deep_research_keyword_triggers_force_include(self):
        """步骤含"深度研究" → force_include_tool 被调用"""
        agent = self._create_react_agent_mock()
        agent._ensure_research_tool_assembled("对2026年AI发展趋势进行深度研究")
        agent.force_include_tool.assert_called_once_with("deep_research")

    def test_step_with_standalone_research_keyword_triggers_force_include(self):
        """步骤含"研究"独立词 → force_include_tool 被调用(Batch 31扩展)"""
        agent = self._create_react_agent_mock()
        agent._ensure_research_tool_assembled("研究AI发展趋势")
        agent.force_include_tool.assert_called_once_with("deep_research")

    def test_step_with_standalone_analysis_keyword_triggers_force_include(self):
        """步骤含"分析"独立词 → force_include_tool 被调用(Batch 31扩展)"""
        agent = self._create_react_agent_mock()
        agent._ensure_research_tool_assembled("分析市场数据")
        agent.force_include_tool.assert_called_once_with("deep_research")

    def test_step_with_english_research_keyword_triggers_force_include(self):
        """步骤含"research" → force_include_tool 被调用(大小写不敏感)"""
        agent = self._create_react_agent_mock()
        agent._ensure_research_tool_assembled("Conduct research on AI trends")
        agent.force_include_tool.assert_called_once_with("deep_research")

    def test_non_research_step_does_not_trigger_force_include(self):
        """步骤为"导出数据" → 不调用 force_include_tool"""
        agent = self._create_react_agent_mock()
        agent._ensure_research_tool_assembled("导出库存数据到Excel")
        agent.force_include_tool.assert_not_called()

    def test_empty_description_does_not_trigger_force_include(self):
        """空步骤描述 → 不调用 force_include_tool"""
        agent = self._create_react_agent_mock()
        agent._ensure_research_tool_assembled("")
        agent.force_include_tool.assert_not_called()


class TestForceIncludeTool:
    """force_include_tool + _filter_tools_by_context 集成测试"""

    def _create_base_agent_with_tools(self, step_description: str, tools_schema: list):
        """创建配置了步骤上下文与工具schema的 BaseAgent mock"""
        agent = MagicMock(spec=BaseAgent)
        agent._ALWAYS_ON_TOOLS = BaseAgent._ALWAYS_ON_TOOLS
        agent._TOOL_KEYWORD_MAP = BaseAgent._TOOL_KEYWORD_MAP
        agent._step_description = step_description
        # 绑定真实方法
        agent._filter_tools_by_context = BaseAgent._filter_tools_by_context.__get__(agent, BaseAgent)
        # 模拟 force_include_tool 行为
        agent._force_included_tools = set()
        agent.force_include_tool = lambda name: agent._force_included_tools.add(name)
        # _filter_tools_by_context 内部使用 logger
        with patch.object(BaseAgent, '_filter_tools_by_context', BaseAgent._filter_tools_by_context):
            agent._filter_tools_by_context = lambda all_tools: BaseAgent._filter_tools_by_context(agent, all_tools)
        return agent

    def test_search_step_with_research_keyword_includes_deep_research(self):
        """步骤命中search关键词+含"研究" → filtered含search工具+deep_research"""
        # 构造工具schema: search_web(deep_research包) + deep_research(单工具包) + message_ask_user(始终保留)
        tools_schema = [
            {"function": {"name": "search_web", "description": "搜索"}},
            {"function": {"name": "deep_research", "description": "深度研究"}},
            {"function": {"name": "message_ask_user", "description": "询问用户"}},
            {"function": {"name": "file_read", "description": "读取文件"}},
        ]
        agent = self._create_base_agent_with_tools("搜索并研究AI趋势", tools_schema)
        # 模拟 _ensure_research_tool_assembled 行为
        agent.force_include_tool("deep_research")

        filtered = agent._filter_tools_by_context(tools_schema)
        tool_names = [t["function"]["name"] for t in filtered]

        # search_web 通过关键词命中(search包)
        assert "search_web" in tool_names
        # deep_research 通过 force_include 命中(即使"研究"不在_TOOL_KEYWORD_MAP的search关键词中)
        assert "deep_research" in tool_names
        # message_ask_user 通过 _ALWAYS_ON_TOOLS 始终保留
        assert "message_ask_user" in tool_names
        # file_read 不在命中包中且未被强制包含,应被过滤
        assert "file_read" not in tool_names

    def test_force_include_is_idempotent(self):
        """重复调用 force_include_tool 无副作用(集合存储)"""
        agent = self._create_base_agent_with_tools("研究AI", [])
        agent.force_include_tool("deep_research")
        agent.force_include_tool("deep_research")
        agent.force_include_tool("deep_research")
        assert agent._force_included_tools == {"deep_research"}


class TestSetResetStepContext:
    """set_step_context / reset_step_context 强制包含集合管理测试"""

    def test_set_step_context_initializes_force_included_tools(self):
        """set_step_context 初始化 _force_included_tools 为空集合"""
        agent = MagicMock(spec=BaseAgent)
        # 绑定真实方法
        BaseAgent.set_step_context(agent, "研究AI趋势")
        assert hasattr(agent, '_force_included_tools')
        assert agent._force_included_tools == set()

    def test_reset_step_context_clears_force_included_tools(self):
        """reset_step_context 清空 _force_included_tools"""
        agent = MagicMock(spec=BaseAgent)
        agent._force_included_tools = {"deep_research", "search_web"}
        BaseAgent.reset_step_context(agent)
        assert agent._force_included_tools == set()

    def test_force_include_tool_without_set_step_context(self):
        """未调用 set_step_context 时 force_include_tool 仍可用(hasattr兜底)"""
        agent = MagicMock(spec=BaseAgent)
        # 删除 _force_included_tools 模拟未初始化
        if hasattr(agent, '_force_included_tools'):
            del agent._force_included_tools
        BaseAgent.force_include_tool(agent, "deep_research")
        assert agent._force_included_tools == {"deep_research"}
