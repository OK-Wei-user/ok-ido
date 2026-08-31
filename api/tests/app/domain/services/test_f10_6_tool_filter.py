#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_f10_6_tool_filter.py
F10-6 工具按需装配单元测试 - 验证步骤上下文注入、关键词过滤、兜底回退

直接加载模式(原桥接工具已移除):
    MCP工具全量加载,通过 _TOOL_KEYWORD_MAP["mcp"] 关键词按步骤过滤装配。
    _ALWAYS_ON_TOOLS 仅保留 message_ask_user(兜底交互能力)。

测试覆盖:
- set_step_context / reset_step_context: 上下文注入与清理
- _filter_tools_by_context: 关键词命中/多包合并/未命中/基础工具保留
- _get_available_tools 集成: 配置开关/无上下文/阈值回退/正常过滤
- MCP工具直接装配: 命中"mcp"关键词(导出/天气/地图等)时装配全部MCP工具
"""
from unittest.mock import MagicMock

import pytest

from app.domain.models.app_config import AgentConfig
from app.domain.services.agents.base import BaseAgent
from app.domain.services.tools.base import BaseTool, tool
from app.domain.models.tool_result import ToolResult


# ========== Stub工具定义(覆盖各工具包前缀) ==========

class _StubFileTool(BaseTool):
    """文件工具包(file_*)"""
    name: str = "file"

    @tool(name="file_read", description="读取文件", parameters={}, required=[])
    async def file_read(self) -> ToolResult:
        return ToolResult(success=True, data="read")

    @tool(name="file_write", description="写入文件", parameters={}, required=[])
    async def file_write(self) -> ToolResult:
        return ToolResult(success=True, data="write")


class _StubShellTool(BaseTool):
    """Shell工具包(shell_*)"""
    name: str = "shell"

    @tool(name="shell_execute", description="执行命令", parameters={}, required=[])
    async def shell_execute(self) -> ToolResult:
        return ToolResult(success=True, data="exec")


class _StubBrowserTool(BaseTool):
    """浏览器工具包(browser_*)"""
    name: str = "browser"

    @tool(name="browser_navigate", description="导航", parameters={}, required=[])
    async def browser_navigate(self) -> ToolResult:
        return ToolResult(success=True, data="nav")


class _StubSearchTool(BaseTool):
    """搜索工具包(search_*/web_search)"""
    name: str = "search"

    @tool(name="web_search", description="搜索", parameters={}, required=[])
    async def web_search(self) -> ToolResult:
        return ToolResult(success=True, data="search")


class _StubMessageTool(BaseTool):
    """消息工具包(message_*) - 含基础工具 message_ask_user"""
    name: str = "message"

    @tool(name="message_ask_user", description="询问用户", parameters={}, required=[])
    async def message_ask_user(self) -> ToolResult:
        return ToolResult(success=True, data="ask")


class _StubMCPTool(BaseTool):
    """MCP工具包(mcp_*) - 直接加载模式: 含业务工具,无桥接工具

    直接加载模式移除了 mcp_tool_search/mcp_tool_describe/mcp_tool_call 桥接工具,
    MCP工具(如天气查询/数据导出)直接作为 LLM 可用工具暴露,
    通过 _TOOL_KEYWORD_MAP["mcp"] 关键词按步骤过滤装配。
    """
    name: str = "mcp"

    @tool(name="mcp_weather_query", description="查询天气", parameters={}, required=[])
    async def mcp_weather_query(self) -> ToolResult:
        return ToolResult(success=True, data="weather")

    @tool(name="mcp_data_export", description="导出业务数据", parameters={}, required=[])
    async def mcp_data_export(self) -> ToolResult:
        return ToolResult(success=True, data="export")


class _StubDeepResearchTool(BaseTool):
    """深度研究工具包(单工具,包名=工具名)

    批次 29 新增: 验证单工具包(包名=工具名)的前缀匹配修复。
    原代码 tool_name.startswith(f"{pkg_name}_") 对单工具包永远返回 False,
    导致 deep_research 工具在 F10-6 启用时不被装配。
    """
    name: str = "deep_research"

    @tool(name="deep_research", description="深度研究工具", parameters={}, required=[])
    async def deep_research(self) -> ToolResult:
        return ToolResult(success=True, data="research")


# ========== 测试辅助函数 ==========

# 全量工具数: file(2) + shell(1) + browser(1) + search(1) + message(1) + mcp(2) + deep_research(1) = 9
_TOTAL_TOOL_COUNT = 9


def _build_agent(tool_filter_enabled: bool = True,
                 min_tools: int = 3) -> BaseAgent:
    """构建BaseAgent实例,装配全部Stub工具"""
    agent = object.__new__(BaseAgent)
    agent.name = "test_agent"
    agent._agent_config = AgentConfig(
        max_iterations=10,
        tool_filter_enabled=tool_filter_enabled,
        tool_filter_min_tools=min_tools,
    )
    agent._step_description = ""
    agent._tools = [
        _StubFileTool(),
        _StubShellTool(),
        _StubBrowserTool(),
        _StubSearchTool(),
        _StubMessageTool(),
        _StubMCPTool(),
        _StubDeepResearchTool(),
    ]
    return agent


def _get_tool_names(tools_schema: list) -> list:
    """从工具Schema列表提取工具名"""
    names = []
    for t in tools_schema:
        func = t.get("function", {}) if isinstance(t, dict) else {}
        name = func.get("name", "")
        if name:
            names.append(name)
    return names


# ========== set_step_context / reset_step_context 单元测试 ==========

class TestStepContext:
    """测试步骤上下文注入与清理"""

    def test_set_step_context_stores_description(self):
        """set_step_context 应存储步骤描述"""
        agent = _build_agent()
        agent.set_step_context("读取文件并分析内容")
        assert agent._step_description == "读取文件并分析内容"

    def test_set_step_context_empty_string_clears(self):
        """set_step_context 传入空串应清理上下文"""
        agent = _build_agent()
        agent.set_step_context("读取文件")
        agent.set_step_context("")
        assert agent._step_description == ""

    def test_reset_step_context_clears(self):
        """reset_step_context 应清理上下文"""
        agent = _build_agent()
        agent.set_step_context("执行shell命令")
        agent.reset_step_context()
        assert agent._step_description == ""


# ========== _filter_tools_by_context 单元测试 ==========

class TestFilterToolsByContext:
    """测试工具过滤逻辑(直接加载模式)"""

    def test_filter_returns_all_when_no_step_description(self):
        """无步骤描述时 _get_available_tools 应返回全量(不调用过滤)"""
        agent = _build_agent(tool_filter_enabled=True)
        agent._step_description = ""
        tools = agent._get_available_tools()
        # 全量工具: file_read, file_write, shell_execute, browser_navigate,
        # web_search, message_ask_user, mcp_weather_query, mcp_data_export,
        # deep_research = 9个
        assert len(tools) == _TOTAL_TOOL_COUNT

    def test_filter_file_keywords(self):
        """命中文件关键词时,应装配 file_* 工具 + 基础工具"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()  # 先获取全量(此时无上下文)
        agent.set_step_context("请读取CSV文件并分析数据")

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        # 应包含 file_* 工具
        assert "file_read" in names
        assert "file_write" in names
        # 应包含基础工具(仅 message_ask_user)
        assert "message_ask_user" in names
        # 不应包含未命中工具包的工具
        assert "shell_execute" not in names
        assert "browser_navigate" not in names
        assert "web_search" not in names
        # 桥接工具已移除,不应出现
        assert "mcp_tool_search" not in names
        assert "mcp_tool_describe" not in names
        assert "mcp_tool_call" not in names

    def test_filter_shell_keywords(self):
        """命中shell关键词时,应装配 shell_* 工具"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("执行Python脚本完成数据分析")

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        assert "shell_execute" in names
        assert "message_ask_user" in names
        # 不应包含未命中的工具
        assert "file_read" not in names
        assert "browser_navigate" not in names

    def test_filter_browser_keywords(self):
        """命中浏览器关键词时,应装配 browser_* 工具"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("使用浏览器访问网页并截图")

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        assert "browser_navigate" in names
        assert "message_ask_user" in names

    def test_filter_multiple_packages(self):
        """命中多个工具包关键词时,应合并装配"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("读取文件后执行shell命令处理数据")

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        # file + shell 都应装配
        assert "file_read" in names
        assert "file_write" in names
        assert "shell_execute" in names
        # 基础工具
        assert "message_ask_user" in names
        # 未命中工具不装配
        assert "browser_navigate" not in names

    def test_filter_no_match_returns_empty(self):
        """未命中任何关键词时,过滤结果为空(由调用方决定回退)"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("这是一个普通的问候消息,不涉及任何工具操作")

        filtered = agent._filter_tools_by_context(all_tools)
        # 未命中关键词,过滤结果应为空
        # 注意: 基础工具(message_ask_user)在过滤逻辑中保留,
        # 但前提是 matched_packages 非空。无匹配时返回空列表
        assert len(filtered) == 0

    def test_filter_empty_tools_returns_empty(self):
        """空工具列表传入应返回空"""
        agent = _build_agent(tool_filter_enabled=True)
        agent.set_step_context("读取文件")
        assert agent._filter_tools_by_context([]) == []

    def test_always_on_tools_preserved(self):
        """基础工具(message_ask_user)应在命中任一工具包时保留"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("执行shell命令")  # 仅命中shell

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        # 基础工具始终保留(直接加载模式: 仅 message_ask_user)
        assert "message_ask_user" in names
        # 桥接工具已移除,不在 _ALWAYS_ON_TOOLS 中
        assert "mcp_tool_search" not in names
        assert "mcp_tool_describe" not in names
        assert "mcp_tool_call" not in names
        # mcp_weather_query 不在基础工具列表,不应保留(未命中mcp关键词)
        assert "mcp_weather_query" not in names

    def test_filter_case_insensitive(self):
        """关键词匹配应大小写不敏感"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("Please READ the FILE and analyze")  # 英文大写

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))
        # "read" 和 "file" 应命中(小写转换后匹配)
        assert "file_read" in names
        assert "file_write" in names


# ========== _get_available_tools 集成测试 ==========

class TestGetAvailableToolsIntegration:
    """测试 _get_available_tools 的开关、回退、过滤集成行为"""

    def test_filter_disabled_returns_all(self):
        """tool_filter_enabled=False 时应返回全量工具"""
        agent = _build_agent(tool_filter_enabled=False)
        agent.set_step_context("读取文件")
        tools = agent._get_available_tools()
        assert len(tools) == _TOTAL_TOOL_COUNT

    def test_filter_enabled_no_context_returns_all(self):
        """tool_filter_enabled=True 但无步骤描述时应返回全量"""
        agent = _build_agent(tool_filter_enabled=True)
        agent._step_description = ""
        tools = agent._get_available_tools()
        assert len(tools) == _TOTAL_TOOL_COUNT

    def test_filter_fallback_when_below_min_tools(self):
        """过滤后工具数低于 min_tools 时应回退全量装配"""
        # min_tools=10,总工具数9,过滤后file(2)+基础(1)=3,低于10,应回退全量
        agent = _build_agent(tool_filter_enabled=True, min_tools=10)
        all_tools = agent._get_available_tools()
        agent.set_step_context("读取文件")  # 命中file包(2工具)+基础(1)=3,低于10

        tools = agent._get_available_tools()
        # 应回退为全量(9个)
        assert len(tools) == _TOTAL_TOOL_COUNT

    def test_filter_normal_case(self):
        """正常过滤场景:命中工具包后工具数>=min_tools,返回过滤结果"""
        # min_tools=3,命中file包(2工具)+基础(1)=3,>=3,应返回过滤结果
        agent = _build_agent(tool_filter_enabled=True, min_tools=3)
        agent.set_step_context("读取CSV文件并分析")

        tools = agent._get_available_tools()
        names = set(_get_tool_names(tools))
        # 应返回3个工具(file_read, file_write + message_ask_user)
        assert len(tools) == 3
        assert "file_read" in names
        assert "file_write" in names
        assert "message_ask_user" in names

    def test_context_cleared_after_reset(self):
        """reset_step_context 后应恢复全量装配"""
        agent = _build_agent(tool_filter_enabled=True, min_tools=3)
        agent.set_step_context("读取文件")
        agent.reset_step_context()
        tools = agent._get_available_tools()
        assert len(tools) == _TOTAL_TOOL_COUNT  # 恢复全量


# ========== 单工具包(包名=工具名)回归测试 - 批次 29 P0 根因修复 ==========

class TestSingleToolPackage:
    """测试单工具包(包名=工具名,如 deep_research)的前缀匹配修复

    根因: 原代码 tool_name.startswith(f"{pkg_name}_") 对单工具包永远返回 False,
    导致 deep_research 工具在 F10-6 启用时不被装配,LLM 完全看不到该工具。
    修复: 增加 tool_name == pkg_name 精确匹配分支。
    """

    def test_single_tool_package_deep_research_assembled(self):
        """命中"深度搜索"关键词时,deep_research 工具应被装配"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("深度搜索 2026 年人工智能发展趋势")

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        # deep_research 应被装配(单工具包精确匹配)
        assert "deep_research" in names
        # 基础工具始终保留
        assert "message_ask_user" in names

    def test_single_tool_package_deep_analysis_keyword(self):
        """命中"深度分析"关键词时,deep_research 工具应被装配"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("深度分析出入库数据用于生产把控")

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        # "深度分析"命中 deep_research 关键词列表
        assert "deep_research" in names

    def test_single_tool_package_research_keyword(self):
        """命中"调研"关键词时,deep_research 工具应被装配"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("调研 2026 年 AI 发展趋势")

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        assert "deep_research" in names

    def test_single_tool_package_not_assembled_without_keyword(self):
        """未命中 deep_research 关键词时,该工具不应被装配"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("读取CSV文件并统计行数")  # 仅命中 file 包

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        # deep_research 不应被装配(步骤不涉及深度研究)
        assert "deep_research" not in names
        # file 工具应被装配
        assert "file_read" in names

    def test_single_tool_package_coexists_with_other_packages(self):
        """deep_research 与其他工具包关键词同时命中时应合并装配"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("深度搜索后用浏览器截图记录结果")

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        # deep_research + browser 都应装配
        assert "deep_research" in names
        assert "browser_navigate" in names


# ========== MCP 工具直接装配测试 - 直接加载模式 ==========

class TestMcpDirectLoadingFilter:
    """测试 MCP 工具通过 _TOOL_KEYWORD_MAP["mcp"] 关键词按需装配

    直接加载模式: MCP工具(如天气查询/数据导出)全量加载,
    通过 F10-6 按步骤关键词过滤装配,控制单轮 token 消耗。
    触发关键词: 导出/天气/地图/入库/出库/订单/报表 等(见 base.py _TOOL_KEYWORD_MAP["mcp"])。
    """

    def test_always_on_tools_only_contains_message_ask_user(self):
        """_ALWAYS_ON_TOOLS 仅含 message_ask_user(桥接工具已移除)"""
        assert "message_ask_user" in BaseAgent._ALWAYS_ON_TOOLS
        # 桥接工具不在 ALWAYS_ON 中
        assert "mcp_tool_search" not in BaseAgent._ALWAYS_ON_TOOLS
        assert "mcp_tool_describe" not in BaseAgent._ALWAYS_ON_TOOLS
        assert "mcp_tool_call" not in BaseAgent._ALWAYS_ON_TOOLS

    def test_mcp_tools_assembled_with_export_keyword(self):
        """步骤含"导出"关键词时,MCP工具应被装配"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("使用MCP工具导出业务数据")

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        # MCP工具应被装配(命中"导出"关键词)
        assert "mcp_data_export" in names
        assert "mcp_weather_query" in names
        # 基础工具始终保留
        assert "message_ask_user" in names

    def test_mcp_tools_assembled_with_weather_keyword(self):
        """步骤含"天气"关键词时,MCP工具应被装配"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("查询广州天气")

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        # MCP工具应被装配(命中"天气"关键词)
        assert "mcp_weather_query" in names
        assert "mcp_data_export" in names

    def test_mcp_tools_assembled_with_inventory_keyword(self):
        """步骤含"入库/出库"关键词时,MCP工具应被装配"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("导出5月全部出入库数据用于经营分析")

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        # MCP工具应被装配(命中"出库"/"导出"关键词)
        assert "mcp_data_export" in names
        assert "mcp_weather_query" in names

    def test_mcp_tools_not_assembled_without_keyword(self):
        """步骤不含MCP关键词时,MCP工具不应被装配"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("读取CSV文件并统计行数")  # 仅命中 file 包

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        # MCP工具不应被装配(步骤不涉及MCP关键词)
        assert "mcp_data_export" not in names
        assert "mcp_weather_query" not in names
        # file 工具应被装配
        assert "file_read" in names

    def test_mcp_tools_coexist_with_file_tools(self):
        """MCP工具与file工具关键词同时命中时应合并装配"""
        agent = _build_agent(tool_filter_enabled=True)
        all_tools = agent._get_available_tools()
        agent.set_step_context("导出数据后保存到CSV文件")

        filtered = agent._filter_tools_by_context(all_tools)
        names = set(_get_tool_names(filtered))

        # MCP + file 都应装配
        assert "mcp_data_export" in names
        assert "file_write" in names
