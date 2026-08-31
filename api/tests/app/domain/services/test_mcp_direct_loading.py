#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_mcp_direct_loading.py
MCP直接加载模式单元测试

覆盖范围:
- tool_search模块: extract_search_candidates(仍被memory.py使用)
- MCPTool直接加载模式: get_tools/has_tool/get_tools_summary(全量返回schema)
- _append_hint_to_result: 工具结果追加提示(退避/系统提示)
- MCPTool重复轮询检测: 参数级+工具级+结果内容感知(直接调用MCP工具名)
- BaseAgent F10-6工具按需装配: MCP工具按关键词过滤
- PlannerReActFlow: MCP摘要延迟注入(直接加载模式)
"""
import json
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from app.domain.services.tools.mcp import MCPTool, _append_hint_to_result
from app.domain.services.tools.tool_search import extract_search_candidates


# ---------------------------------------------------------------------------
# extract_search_candidates 单元测试 (tool_search.py,仍被memory.py使用)
# ---------------------------------------------------------------------------


class TestExtractSearchCandidates:
    """extract_search_candidates 候选工具名提取测试

    函数位置: app.domain.services.tools.tool_search.extract_search_candidates
    作用: 从mcp_tool_search返回内容中提取候选MCP工具名列表。
    消费者: memory.py的key_facts保留逻辑(防止emergency_compact后重复搜索)。
    """

    def test_extract_from_direct_json(self):
        """直接JSON字符串格式提取"""
        text = json.dumps({
            "query": "天气",
            "total_available": 2,
            "matches": [
                {"name": "mcp_amap_weather", "description": "查询天气"},
                {"name": "mcp_openweather", "description": "天气预报"},
            ],
        }, ensure_ascii=False)
        candidates = extract_search_candidates(text)
        assert candidates == ["mcp_amap_weather", "mcp_openweather"]

    def test_extract_from_wrapped_format(self):
        """包装格式 {"success": true, "data": "{JSON}"} 提取"""
        inner = json.dumps({
            "matches": [{"name": "mcp_test_tool", "description": "测试"}],
        }, ensure_ascii=False)
        text = json.dumps({"success": True, "data": inner}, ensure_ascii=False)
        candidates = extract_search_candidates(text)
        assert candidates == ["mcp_test_tool"]

    def test_extract_empty_text(self):
        """空文本返回空列表"""
        assert extract_search_candidates("") == []
        assert extract_search_candidates(None) == []

    def test_extract_invalid_json(self):
        """无效JSON返回空列表"""
        assert extract_search_candidates("not a json") == []

    def test_extract_no_matches_field(self):
        """无matches字段返回空列表"""
        text = json.dumps({"query": "test", "total": 0})
        assert extract_search_candidates(text) == []

    def test_extract_empty_matches(self):
        """空matches列表返回空列表"""
        text = json.dumps({"matches": []})
        assert extract_search_candidates(text) == []

    def test_extract_skips_empty_names(self):
        """空name的条目被跳过"""
        text = json.dumps({
            "matches": [
                {"name": "", "description": "无名"},
                {"name": "mcp_valid", "description": "有效"},
            ]
        })
        candidates = extract_search_candidates(text)
        assert candidates == ["mcp_valid"]

    def test_extract_non_string_text(self):
        """非字符串输入返回空列表"""
        assert extract_search_candidates(123) == []
        assert extract_search_candidates([]) == []
        assert extract_search_candidates({}) == []


# ---------------------------------------------------------------------------
# MCPTool 直接加载模式测试
# ---------------------------------------------------------------------------


class TestMCPToolDirectLoading:
    """MCPTool直接加载模式测试(全量加载MCP工具schema)"""

    def _create_mcp_tool(self, tools=None) -> MCPTool:
        """创建带预设工具列表的MCPTool实例(直接加载模式)"""
        with patch.object(MCPTool, '__init__', lambda self: None):
            tool = MCPTool.__new__(MCPTool)
            tool._tools = tools or []
            return tool

    def test_get_tools_returns_full_schema(self):
        """get_tools始终返回全部MCP工具schema(直接加载)"""
        tools = [
            {"type": "function", "function": {"name": "mcp_amap_weather", "description": "天气"}},
            {"type": "function", "function": {"name": "mcp_lhsc_export", "description": "导出"}},
        ]
        tool = self._create_mcp_tool(tools=tools)
        result = tool.get_tools()
        assert len(result) == 2
        assert result[0]["function"]["name"] == "mcp_amap_weather"

    def test_get_tools_empty_when_no_tools(self):
        """无工具时返回空列表"""
        tool = self._create_mcp_tool(tools=[])
        assert tool.get_tools() == []

    def test_has_tool_recognizes_loaded_tools(self):
        """has_tool正确识别已加载的MCP工具"""
        tools = [
            {"type": "function", "function": {"name": "mcp_amap_weather", "description": "天气"}},
        ]
        tool = self._create_mcp_tool(tools=tools)
        assert tool.has_tool("mcp_amap_weather") is True
        assert tool.has_tool("mcp_nonexistent") is False

    def test_has_tool_empty_list(self):
        """空工具列表时has_tool返回False"""
        tool = self._create_mcp_tool(tools=[])
        assert tool.has_tool("mcp_any") is False


class TestMCPToolsSummary:
    """MCPTool.get_tools_summary 摘要生成测试(直接加载模式)"""

    def _create_mcp_tool(self, tools=None) -> MCPTool:
        """创建带预设工具列表的MCPTool实例"""
        with patch.object(MCPTool, '__init__', lambda self: None):
            tool = MCPTool.__new__(MCPTool)
            tool._tools = tools or []
            return tool

    def test_summary_empty_when_no_tools(self):
        """无工具时返回空字符串"""
        tool = self._create_mcp_tool(tools=[])
        assert tool.get_tools_summary() == ""

    def test_summary_contains_tool_names(self):
        """摘要包含工具名称"""
        tools = [
            {"type": "function", "function": {"name": "mcp_amap_maps_search", "description": "搜索地点 [来源: amap-maps]"}},
            {"type": "function", "function": {"name": "mcp_multimodal_ocr", "description": "OCR识别 [来源: mcp-multimodal]"}},
        ]
        tool = self._create_mcp_tool(tools=tools)
        summary = tool.get_tools_summary()

        assert "mcp_amap_maps_search" in summary
        assert "mcp_multimodal_ocr" in summary

    def test_summary_strips_source_suffix(self):
        """摘要去除[来源: xxx]后缀"""
        tools = [
            {"type": "function", "function": {"name": "mcp_test_tool", "description": "测试工具 [来源: test-server]"}},
        ]
        tool = self._create_mcp_tool(tools=tools)
        summary = tool.get_tools_summary()

        assert "[来源:" not in summary
        assert "测试工具" in summary

    def test_summary_has_header(self):
        """摘要包含标题行"""
        tools = [
            {"type": "function", "function": {"name": "mcp_test", "description": "测试"}},
        ]
        tool = self._create_mcp_tool(tools=tools)
        summary = tool.get_tools_summary()

        assert summary.startswith("可用MCP工具:")

    def test_summary_handles_missing_description(self):
        """工具无描述时不报错"""
        tools = [
            {"type": "function", "function": {"name": "mcp_no_desc", "description": None}},
        ]
        tool = self._create_mcp_tool(tools=tools)
        summary = tool.get_tools_summary()

        assert "mcp_no_desc" in summary


# ---------------------------------------------------------------------------
# _append_hint_to_result 单元测试
# ---------------------------------------------------------------------------


class TestAppendHintToResult:
    """_append_hint_to_result 辅助函数测试

    函数位置: app.domain.services.tools.mcp._append_hint_to_result
    作用: 在工具结果末尾追加提示文本(退避提示/系统提示)。
    防御性设计: 保持原类型(str/dict),非标准格式原样返回不破坏。
    """

    def test_str_input_appends_hint(self):
        """str输入: 末尾追加提示,返回str"""
        result = _append_hint_to_result("原始结果", "[系统提示] 请等待")
        assert isinstance(result, str)
        assert result == "原始结果\n\n[系统提示] 请等待"

    def test_dict_with_text_field_appends_to_text(self):
        """dict且含text字段: text末尾追加提示,其他字段保留"""
        data = {"text": "原始文本", "other": "保留字段"}
        result = _append_hint_to_result(data, "[系统提示] 提示")
        assert isinstance(result, dict)
        assert result["text"] == "原始文本\n\n[系统提示] 提示"
        assert result["other"] == "保留字段"

    def test_dict_without_text_field_returns_original(self):
        """dict但无text字段: 原样返回(防御性,不破坏非标准格式)"""
        data = {"data": "原始数据", "other": "字段"}
        result = _append_hint_to_result(data, "提示")
        assert result is data  # 原样返回同一对象

    def test_dict_with_non_str_text_returns_original(self):
        """dict但text非字符串: 原样返回(防御性)"""
        data = {"text": 123}
        result = _append_hint_to_result(data, "提示")
        assert result is data

    def test_none_input_returns_none(self):
        """None输入: 原样返回None"""
        assert _append_hint_to_result(None, "提示") is None

    def test_int_input_returns_original(self):
        """非str/dict类型: 原样返回(防御性)"""
        assert _append_hint_to_result(123, "提示") == 123

    def test_list_input_returns_original(self):
        """list输入: 原样返回(防御性)"""
        original = [1, 2, 3]
        result = _append_hint_to_result(original, "提示")
        assert result is original

    def test_empty_str_appends_hint(self):
        """空字符串: 仍追加提示"""
        result = _append_hint_to_result("", "提示文本")
        assert result == "\n\n提示文本"

    def test_dict_with_text_not_mutated(self):
        """dict输入不应被原地修改(返回新dict)"""
        data = {"text": "原始"}
        original_text = data["text"]
        _ = _append_hint_to_result(data, "提示")
        # 原对象不应被修改
        assert data["text"] == original_text


# ---------------------------------------------------------------------------
# MCPTool 重复轮询检测测试 (直接调用MCP工具名)
# ---------------------------------------------------------------------------


class TestMCPPollingDetection:
    """MCP查询类工具重复轮询检测测试 (四重感知:工具类型+参数+结果内容+工具级累计)

    直接加载模式: LLM直接调用MCP工具名(如mcp_getDownloadTaskList),
    不再通过mcp_tool_call桥接工具中转。轮询检测逻辑不变,仅调用方式改变。

    根因会话72d71cc6/5c8d9c88: 旧版退避提示12+11次仍被LLM忽略,因为:
    1. "get"关键词误判getOutboundDetailExport为查询类工具(按月提交被加提示)
    2. 不区分参数,不同taskId/status的查询被误计为重复
    3. 不检测结果内容,任务已完成的查询也被加提示("狼来了"效应)

    根因会话d71e315f: 三重感知优化后,LLM通过参数切换(status=0/status=1交替)规避参数级检测,
    每个参数只查2次未达3次阈值,但整体在轮询同一任务状态,且sleep次数超过3次上限。
    新增工具级pending累计计数(不论参数),达4次触发退避提示。
    """

    def _create_polling_tool(self, result_data: str = "任务状态: 处理中") -> MCPTool:
        """创建带轮询检测属性的MCPTool实例(直接加载模式)

        Args:
            result_data: mock返回的结果数据(用于测试结果内容感知)
        """
        with patch.object(MCPTool, '__init__', lambda self: None):
            tool = MCPTool.__new__(MCPTool)
            tool._tools = []
            tool._sandbox = None
            tool._mcp_config = None
            # 参数级计数(精确相同查询)
            tool._last_query_key = None
            tool._query_call_count = 0
            # 工具级计数(参数切换轮询检测)
            tool._last_poll_tool = None
            tool._tool_pending_count = 0
            tool._consecutive_non_pending = 0
            # 统计与回调属性
            tool._poll_stats = None
            tool._callback_manager = None
            tool._background_tasks = {}
            tool._session_id = None
            # 非查询类工具失败重试检测
            tool._last_fail_key = None
            tool._non_query_fail_count = 0

            # mock _resolve_sandbox_paths 返回原参数(保留实际参数用于参数感知检测)
            tool._resolve_sandbox_paths = AsyncMock(side_effect=lambda name, args: args)

            # mock manager.invoke 返回带data的结果
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.data = result_data
            mock_result.message = ""
            tool._manager = MagicMock()
            tool._manager.invoke = AsyncMock(return_value=mock_result)
            return tool

    @pytest.mark.asyncio
    async def test_first_query_no_hint(self):
        """第1次查询不追加退避提示(直接调用MCP工具名)"""
        tool = self._create_polling_tool()
        await tool.invoke("mcp_getDownloadTaskList")
        assert tool._query_call_count == 1
        assert "系统提示" not in tool._manager.invoke.return_value.data

    @pytest.mark.asyncio
    async def test_second_query_no_hint(self):
        """第2次查询不追加退避提示"""
        tool = self._create_polling_tool()
        await tool.invoke("mcp_getDownloadTaskList")
        await tool.invoke("mcp_getDownloadTaskList")
        assert tool._query_call_count == 2

    @pytest.mark.asyncio
    async def test_third_query_adds_hint(self):
        """第3次查询追加退避提示(递增退避:建议sleep 60s)"""
        tool = self._create_polling_tool()
        for _ in range(3):
            await tool.invoke("mcp_getDownloadTaskList")
        assert tool._query_call_count == 3
        assert "系统提示" in tool._manager.invoke.return_value.data
        assert "sleep 60" in tool._manager.invoke.return_value.data

    @pytest.mark.asyncio
    async def test_fourth_query_backoff_increases(self):
        """第4次查询退避时间递增(建议sleep 120s)"""
        tool = self._create_polling_tool()
        for _ in range(4):
            await tool.invoke("mcp_getDownloadTaskList")
        assert tool._query_call_count == 4
        assert "sleep 120" in tool._manager.invoke.return_value.data

    @pytest.mark.asyncio
    async def test_fifth_query_force_stop(self):
        """第5次查询强制建议停止轮询"""
        tool = self._create_polling_tool()
        for _ in range(5):
            await tool.invoke("mcp_getDownloadTaskList")
        assert tool._query_call_count == 5
        assert "停止轮询" in tool._manager.invoke.return_value.data

    @pytest.mark.asyncio
    async def test_non_query_tool_not_tracked(self):
        """导出类工具(含export)不触发轮询检测(精确化检测)"""
        tool = self._create_polling_tool()
        await tool.invoke("mcp_getOutboundDetailExport")
        assert tool._query_call_count == 0
        assert tool._last_query_key is None

    @pytest.mark.asyncio
    async def test_different_query_tool_resets_counter(self):
        """切换到不同的查询类工具时重置计数器(参数感知)"""
        tool = self._create_polling_tool()
        # 先调用工具A 2次
        await tool.invoke("mcp_getTaskList")
        await tool.invoke("mcp_getTaskList")
        assert tool._query_call_count == 2

        # 切换到工具B,计数器应重置为1
        await tool.invoke("mcp_getStatus")
        assert tool._query_call_count == 1
        assert "mcp_getStatus" in tool._last_query_key

    @pytest.mark.asyncio
    async def test_different_args_resets_counter(self):
        """相同工具但不同参数时重置计数器(参数感知)"""
        tool = self._create_polling_tool()
        # 相同工具+相同参数 2次
        await tool.invoke("mcp_getDownloadTaskList", taskId=1179)
        await tool.invoke("mcp_getDownloadTaskList", taskId=1179)
        assert tool._query_call_count == 2

        # 相同工具但不同参数,计数器应重置为1
        await tool.invoke("mcp_getDownloadTaskList", taskId=1185)
        assert tool._query_call_count == 1

    @pytest.mark.asyncio
    async def test_completed_task_no_hint(self):
        """任务已完成的结果不追加退避提示(结果内容感知,避免"狼来了"效应)"""
        tool = self._create_polling_tool(result_data="任务状态: 已完成,下载链接: http://example.com/file.xlsx")
        for _ in range(5):
            await tool.invoke("mcp_getDownloadTaskList")
        # 任务已完成,计数器被重置,不追加提示
        assert "系统提示" not in tool._manager.invoke.return_value.data

    @pytest.mark.asyncio
    async def test_normal_list_data_no_hint(self):
        """正常列表数据(不含未完成状态词)不追加退避提示"""
        tool = self._create_polling_tool(result_data='{"tasks": [{"id": 1, "name": "task1", "status": "done"}]}')
        for _ in range(5):
            await tool.invoke("mcp_getDownloadTaskList")
        assert "系统提示" not in tool._manager.invoke.return_value.data

    @pytest.mark.asyncio
    async def test_pending_state_english_triggers_hint(self):
        """英文pending状态词也触发退避提示"""
        tool = self._create_polling_tool(result_data='{"status": "processing", "message": "task is running"}')
        for _ in range(3):
            await tool.invoke("mcp_getDownloadTaskList")
        assert "系统提示" in tool._manager.invoke.return_value.data

    @pytest.mark.asyncio
    async def test_hint_count_increments(self):
        """退避提示中的次数应随调用递增"""
        tool = self._create_polling_tool()
        for _ in range(5):
            await tool.invoke("mcp_getDownloadTaskList")
        assert tool._query_call_count == 5
        assert "第5次" in tool._manager.invoke.return_value.data

    # ------------------------------------------------------------------
    # 工具级pending累计计数测试 (根因会话d71e315f: 参数切换轮询规避检测)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_param_switching_polling_triggers_hint(self):
        """参数切换轮询4次触发退避提示(工具级pending累计计数)

        根因会话d71e315f: LLM交替查询status=0和status=1,每个参数只查2次,
        参数级计数未达3次阈值,但整体在轮询同一任务状态。
        工具级pending累计达4次时触发退避提示。
        """
        tool = self._create_polling_tool()
        # 交替查询不同参数(模拟d71e315f会话的参数切换轮询)
        await tool.invoke("mcp_getDownloadTaskList", status="0")
        await tool.invoke("mcp_getDownloadTaskList", status="1")
        await tool.invoke("mcp_getDownloadTaskList", status="0")
        # 第3次pending:工具级=3,参数级status=0=2,均未达阈值,不触发
        assert "系统提示" not in tool._manager.invoke.return_value.data
        assert tool._tool_pending_count == 3

        # 第4次pending:工具级=4,触发退避提示
        await tool.invoke("mcp_getDownloadTaskList", status="1")
        assert tool._tool_pending_count == 4
        assert "系统提示" in tool._manager.invoke.return_value.data
        # 工具级退避提示应优先引导更换查询参数,而非强制sleep
        assert "建议立即更换查询参数" in tool._manager.invoke.return_value.data
        assert "推荐不传status一次查询所有状态" in tool._manager.invoke.return_value.data

    @pytest.mark.asyncio
    async def test_tool_switch_resets_tool_count(self):
        """切换到不同的查询类工具时重置工具级pending计数"""
        tool = self._create_polling_tool()
        # 工具A调用3次(工具级=3,未触发)
        for _ in range(3):
            await tool.invoke("mcp_getDownloadTaskList")
        assert tool._tool_pending_count == 3

        # 切换到工具B,工具级计数应重置为0
        await tool.invoke("mcp_getTaskStatus")
        assert tool._tool_pending_count == 1  # 新工具第1次pending

    @pytest.mark.asyncio
    async def test_consecutive_non_pending_resets_tool_count(self):
        """连续2次非pending结果重置工具级pending计数(任务完成确认)"""
        tool = self._create_polling_tool()
        # 3次pending查询(工具级=3)
        for _ in range(3):
            await tool.invoke("mcp_getDownloadTaskList")
        assert tool._tool_pending_count == 3

        # 切换到非pending结果(模拟任务完成)
        completed_result = MagicMock()
        completed_result.success = True
        completed_result.data = "任务状态: 已完成"
        completed_result.message = ""
        tool._manager.invoke = AsyncMock(return_value=completed_result)

        # 第1次非pending:连续非pending=1,工具级保持3(不立即重置)
        await tool.invoke("mcp_getDownloadTaskList")
        assert tool._consecutive_non_pending == 1
        assert tool._tool_pending_count == 3

        # 第2次非pending:连续非pending=2,工具级重置为0
        await tool.invoke("mcp_getDownloadTaskList")
        assert tool._tool_pending_count == 0
        assert tool._consecutive_non_pending == 0

    @pytest.mark.asyncio
    async def test_single_non_pending_not_reset_tool_count(self):
        """单次非pending结果不重置工具级计数(避免查status=1误重置)

        根因会话d71e315f: LLM查询status=1(已完成任务)返回非pending,
        但任务实际未完成(status=0仍有处理中任务),不应重置工具级计数。
        """
        tool = self._create_polling_tool()
        # 2次pending查询(工具级=2)
        await tool.invoke("mcp_getDownloadTaskList", status="0")
        await tool.invoke("mcp_getDownloadTaskList", status="0")
        assert tool._tool_pending_count == 2

        # 1次非pending查询(查status=1已完成任务)
        completed_result = MagicMock()
        completed_result.success = True
        completed_result.data = '{"total": 100, "rows": [{"status": "1", "statusDesc": "已完成"}]}'
        completed_result.message = ""
        tool._manager.invoke = AsyncMock(return_value=completed_result)
        await tool.invoke("mcp_getDownloadTaskList", status="1")

        # 单次非pending不应重置工具级计数(连续非pending=1)
        assert tool._consecutive_non_pending == 1
        assert tool._tool_pending_count == 2  # 保持不变

        # 再次pending查询,工具级应继续累计为3
        pending_result = MagicMock()
        pending_result.success = True
        pending_result.data = '{"total": 5, "rows": [{"status": "0", "statusDesc": "处理中"}]}'
        pending_result.message = ""
        tool._manager.invoke = AsyncMock(return_value=pending_result)
        await tool.invoke("mcp_getDownloadTaskList", status="0")
        assert tool._tool_pending_count == 3
        assert tool._consecutive_non_pending == 0

    @pytest.mark.asyncio
    async def test_tool_level_hint_includes_mode_description(self):
        """工具级触发的退避提示应优先引导更换查询参数"""
        tool = self._create_polling_tool()
        # 4次不同参数的pending查询,触发工具级退避提示
        await tool.invoke("mcp_getDownloadTaskList", status="0")
        await tool.invoke("mcp_getDownloadTaskList", status="1")
        await tool.invoke("mcp_getDownloadTaskList", status="0")
        await tool.invoke("mcp_getDownloadTaskList", status="1")

        result_data = tool._manager.invoke.return_value.data
        assert "系统提示" in result_data
        assert "建议立即更换查询参数" in result_data
        assert "推荐不传status一次查询所有状态" in result_data
        assert "按fileName精确查询目标任务" in result_data
        assert "第4次" in result_data


# ---------------------------------------------------------------------------
# BaseAgent F10-6 工具按需装配测试 (MCP工具关键词过滤)
# ---------------------------------------------------------------------------


class TestF10ToolFilter:
    """BaseAgent._filter_tools_by_context F10-6工具按需装配测试

    验证MCP工具按步骤关键词自动装配:
    - 步骤命中"mcp"关键词包(导出/天气/地图/库存等)时,装配全部mcp_前缀工具
    - 步骤未命中任何关键词时,仅保留_ALWAYS_ON_TOOLS
    """

    def _make_agent_with_step(self, step_description: str):
        """创建带步骤描述的BaseAgent实例(mock)"""
        from app.domain.services.agents.base import BaseAgent
        agent = object.__new__(BaseAgent)
        agent._step_description = step_description
        agent.name = "test_agent"
        return agent

    def _make_tool_schema(self, name: str, description: str = "") -> dict:
        """构造工具schema"""
        return {"type": "function", "function": {"name": name, "description": description}}

    def test_mcp_keyword_export_triggers_mcp_tools(self):
        """步骤含'导出'关键词时装配MCP工具"""
        agent = self._make_agent_with_step("导出5月出入库数据到Excel")
        all_tools = [
            self._make_tool_schema("shell_execute"),
            self._make_tool_schema("mcp_lhsc_export"),
            self._make_tool_schema("mcp_amap_weather"),
            self._make_tool_schema("message_ask_user"),
        ]
        filtered = agent._filter_tools_by_context(all_tools)
        tool_names = [t["function"]["name"] for t in filtered]
        # MCP工具应被装配
        assert "mcp_lhsc_export" in tool_names
        assert "mcp_amap_weather" in tool_names
        # ALWAYS_ON工具应保留
        assert "message_ask_user" in tool_names

    def test_mcp_keyword_weather_triggers_mcp_tools(self):
        """步骤含'天气'关键词时装配MCP工具"""
        agent = self._make_agent_with_step("查询广州今天天气")
        all_tools = [
            self._make_tool_schema("mcp_amap_weather"),
            self._make_tool_schema("message_ask_user"),
        ]
        filtered = agent._filter_tools_by_context(all_tools)
        tool_names = [t["function"]["name"] for t in filtered]
        assert "mcp_amap_weather" in tool_names

    def test_mcp_keyword_inventory_triggers_mcp_tools(self):
        """步骤含'库存'关键词时装配MCP工具"""
        agent = self._make_agent_with_step("查询当前库存状态")
        all_tools = [
            self._make_tool_schema("mcp_lhsc_getInventory"),
            self._make_tool_schema("message_ask_user"),
        ]
        filtered = agent._filter_tools_by_context(all_tools)
        tool_names = [t["function"]["name"] for t in filtered]
        assert "mcp_lhsc_getInventory" in tool_names

    def test_no_keyword_returns_empty(self):
        """步骤未命中任何关键词时返回空列表(由调用方决定是否回退全量)

        F10-6设计: 无关键词命中时返回空列表,调用方可根据场景决定回退到全量工具
        或保持空列表(强制LLM声明能力不可用)。_ALWAYS_ON_TOOLS仅在有关键词命中时保留。
        """
        agent = self._make_agent_with_step("这是一个普通步骤无关键词")
        all_tools = [
            self._make_tool_schema("shell_execute"),
            self._make_tool_schema("mcp_amap_weather"),
            self._make_tool_schema("message_ask_user"),
        ]
        filtered = agent._filter_tools_by_context(all_tools)
        # 无关键词命中时返回空列表(由调用方决定是否回退全量)
        assert filtered == []

    def test_shell_keyword_assembles_shell_tools(self):
        """步骤含'shell'关键词时装配shell工具(非MCP)"""
        agent = self._make_agent_with_step("执行shell命令查看目录")
        all_tools = [
            self._make_tool_schema("shell_execute"),
            self._make_tool_schema("mcp_amap_weather"),
            self._make_tool_schema("message_ask_user"),
        ]
        filtered = agent._filter_tools_by_context(all_tools)
        tool_names = [t["function"]["name"] for t in filtered]
        assert "shell_execute" in tool_names
        # 未命中mcp关键词,MCP工具不装配
        assert "mcp_amap_weather" not in tool_names

    def test_empty_tools_returns_empty(self):
        """空工具列表返回空"""
        agent = self._make_agent_with_step("导出数据")
        assert agent._filter_tools_by_context([]) == []

    def test_multiple_keyword_packages_assembled(self):
        """步骤命中多个工具包时全部装配"""
        agent = self._make_agent_with_step("执行shell脚本导出数据到Excel文件")
        all_tools = [
            self._make_tool_schema("shell_execute"),
            self._make_tool_schema("file_read"),
            self._make_tool_schema("mcp_lhsc_export"),
            self._make_tool_schema("message_ask_user"),
        ]
        filtered = agent._filter_tools_by_context(all_tools)
        tool_names = [t["function"]["name"] for t in filtered]
        # shell和mcp工具包都应装配
        assert "shell_execute" in tool_names
        assert "mcp_lhsc_export" in tool_names


# ---------------------------------------------------------------------------
# PlannerReActFlow 摘要注入测试
# ---------------------------------------------------------------------------


class TestPlannerExcludesMCP:
    """PlannerReActFlow两阶段工具注入测试"""

    def test_planner_tools_excludes_mcp(self):
        """Planner工具列表不含MCP完整schema(摘要注入,降token)"""
        file_tool = MagicMock(name="file")
        file_tool.name = "file"
        shell_tool = MagicMock(name="shell")
        shell_tool.name = "shell"
        mcp_tool = MagicMock(name="mcp")
        mcp_tool.name = "mcp"
        a2a_tool = MagicMock(name="a2a")
        a2a_tool.name = "a2a"

        tools = [file_tool, shell_tool, mcp_tool, a2a_tool]
        planner_tools = [t for t in tools if t.name != "mcp"]

        assert mcp_tool not in planner_tools
        assert file_tool in planner_tools
        assert shell_tool in planner_tools
        assert a2a_tool in planner_tools
        assert len(planner_tools) == 3

    def test_react_tools_includes_mcp(self):
        """ReAct工具列表包含MCP(供实际调用)"""
        file_tool = MagicMock(name="file")
        file_tool.name = "file"
        mcp_tool = MagicMock(name="mcp")
        mcp_tool.name = "mcp"

        tools = [file_tool, mcp_tool]
        react_tools = tools

        assert mcp_tool in react_tools
        assert len(react_tools) == 2


class TestMCPSummaryInjection:
    """MCP摘要延迟注入测试(直接加载模式)"""

    def test_summary_injected_into_planner_prompt(self):
        """直接加载模式: MCP摘要注入Planner系统提示"""
        mcp_tool = MagicMock()
        mcp_tool.get_tools_summary = MagicMock(return_value="可用MCP工具:\n  - mcp_test: 测试工具")

        planner = MagicMock()
        planner._system_prompt = "你是规划Agent"

        mcp_summary_injected = False
        if not mcp_summary_injected:
            mcp_summary_injected = True
            summary = mcp_tool.get_tools_summary()
            if summary:
                planner._system_prompt += (
                    f"\n\n[MCP工具列表]\n{summary}\n"
                    f"请在步骤描述中引用需要的MCP工具名,"
                    f"ReAct执行阶段会按步骤关键词自动装配对应MCP工具schema。"
                )

        assert "[MCP工具列表]" in planner._system_prompt
        assert "mcp_test" in planner._system_prompt
        assert mcp_summary_injected is True

    def test_summary_not_injected_when_empty(self):
        """MCP无工具时不注入摘要"""
        mcp_tool = MagicMock()
        mcp_tool.get_tools_summary = MagicMock(return_value="")

        planner = MagicMock()
        planner._system_prompt = "你是规划Agent"

        original_prompt = planner._system_prompt
        mcp_summary_injected = False

        if not mcp_summary_injected:
            mcp_summary_injected = True
            summary = mcp_tool.get_tools_summary()
            if summary:
                planner._system_prompt += f"\n\n[MCP工具列表]\n{summary}"

        assert planner._system_prompt == original_prompt
        assert "[MCP工具列表]" not in planner._system_prompt

    def test_summary_only_injected_once(self):
        """MCP摘要仅注入一次(多次invoke不重复注入)"""
        mcp_tool = MagicMock()
        mcp_tool.get_tools_summary = MagicMock(return_value="可用MCP工具:\n  - mcp_test: 测试")

        planner = MagicMock()
        planner._system_prompt = "你是规划Agent"

        mcp_summary_injected = False

        for _ in range(3):
            if not mcp_summary_injected:
                mcp_summary_injected = True
                summary = mcp_tool.get_tools_summary()
                if summary:
                    planner._system_prompt += f"\n\n[MCP工具列表]\n{summary}"

        assert mcp_tool.get_tools_summary.call_count == 1
