#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_mcp_invoke_safety.py
MCP调用安全单元测试 — 验证P0-7~P0-9三项防御修复

测试覆盖:
- P0-7: session.call_tool() 超时保护(anyio.fail_after, 120s)
- P0-8: session.list_tools() 超时保护(15s, 超时设空列表不阻断连接)
- P0-9: _coerce_arguments_by_schema 参数类型强制转换
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.app_config import MCPConfig, MCPServerConfig, MCPTransport
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.mcp import (
    MCPClientManager,
    _MCP_INVOKE_TIMEOUT,
    _MCP_LIST_TOOLS_TIMEOUT,
    _MCP_INIT_TIMEOUT,
)


def _make_server_config(
    name: str = "test_server",
    transport: MCPTransport = MCPTransport.STREAMABLE_HTTP,
    url: str = "http://localhost:8080/mcp",
) -> MCPConfig:
    """构造含单个MCP服务的配置"""
    return MCPConfig(mcpServers={
        name: MCPServerConfig(
            command="", args=[], env={}, url=url, transport=transport, enabled=True,
        )
    })


def _make_tool_mock(name: str = "tool", input_schema: dict = None) -> MagicMock:
    """构造mock Tool对象(模拟mcp.Tool)"""
    tool = MagicMock()
    tool.name = name
    tool.description = f"test tool {name}"
    tool.inputSchema = input_schema or {"type": "object", "properties": {}}
    return tool


class TestCoerceArgumentsBySchema:
    """P0-9: _coerce_arguments_by_schema 参数类型强制转换测试"""

    def _make_manager(self) -> MCPClientManager:
        """创建MCPClientManager实例(无需初始化)"""
        return MCPClientManager(mcp_config=_make_server_config())

    def test_string_to_integer(self):
        """string→integer: thoughtNumber="1" → 1"""
        manager = self._make_manager()
        schema = {"type": "object", "properties": {
            "thoughtNumber": {"type": "integer"}
        }}
        result = manager._coerce_arguments_by_schema(
            {"thoughtNumber": "1"}, schema
        )
        assert result["thoughtNumber"] == 1
        assert isinstance(result["thoughtNumber"], int)

    def test_string_to_number(self):
        """string→number: ratio="3.14" → 3.14"""
        manager = self._make_manager()
        schema = {"type": "object", "properties": {
            "ratio": {"type": "number"}
        }}
        result = manager._coerce_arguments_by_schema(
            {"ratio": "3.14"}, schema
        )
        assert result["ratio"] == 3.14
        assert isinstance(result["ratio"], float)

    def test_string_to_boolean_true(self):
        """string→boolean: enabled="true" → True"""
        manager = self._make_manager()
        schema = {"type": "object", "properties": {
            "enabled": {"type": "boolean"}
        }}
        assert manager._coerce_arguments_by_schema(
            {"enabled": "true"}, schema
        )["enabled"] is True
        assert manager._coerce_arguments_by_schema(
            {"enabled": "TRUE"}, schema
        )["enabled"] is True

    def test_string_to_boolean_false(self):
        """string→boolean: enabled="false" → False"""
        manager = self._make_manager()
        schema = {"type": "object", "properties": {
            "enabled": {"type": "boolean"}
        }}
        assert manager._coerce_arguments_by_schema(
            {"enabled": "false"}, schema
        )["enabled"] is False

    def test_string_to_array(self):
        """string→array: items='[1,2,3]' → [1,2,3]"""
        manager = self._make_manager()
        schema = {"type": "object", "properties": {
            "items": {"type": "array"}
        }}
        result = manager._coerce_arguments_by_schema(
            {"items": "[1,2,3]"}, schema
        )
        assert result["items"] == [1, 2, 3]
        assert isinstance(result["items"], list)

    def test_string_to_object(self):
        """string→object: config='{"a":1}' → {"a":1}"""
        manager = self._make_manager()
        schema = {"type": "object", "properties": {
            "config": {"type": "object"}
        }}
        result = manager._coerce_arguments_by_schema(
            {"config": '{"a":1}'}, schema
        )
        assert result["config"] == {"a": 1}
        assert isinstance(result["config"], dict)

    def test_infer_untyped_integer(self):
        """schema无type字段: count="5" → 5(按参数名推断)"""
        manager = self._make_manager()
        schema = {"type": "object", "properties": {
            "count": {}
        }}
        result = manager._coerce_arguments_by_schema(
            {"count": "5"}, schema
        )
        assert result["count"] == 5
        assert isinstance(result["count"], int)

    def test_infer_untyped_thought(self):
        """schema无type字段: thoughtNumber="3" → 3(按参数名thought推断)"""
        manager = self._make_manager()
        schema = {"type": "object", "properties": {
            "thoughtNumber": {}
        }}
        result = manager._coerce_arguments_by_schema(
            {"thoughtNumber": "3"}, schema
        )
        assert result["thoughtNumber"] == 3

    def test_invalid_integer_keeps_original(self):
        """无效值: thoughtNumber="abc" + schema{type:integer} → 保留原值"""
        manager = self._make_manager()
        schema = {"type": "object", "properties": {
            "thoughtNumber": {"type": "integer"}
        }}
        result = manager._coerce_arguments_by_schema(
            {"thoughtNumber": "abc"}, schema
        )
        assert result["thoughtNumber"] == "abc"

    def test_non_string_value_unchanged(self):
        """非字符串值: thoughtNumber=1(已是int) → 保持不变"""
        manager = self._make_manager()
        schema = {"type": "object", "properties": {
            "thoughtNumber": {"type": "integer"}
        }}
        result = manager._coerce_arguments_by_schema(
            {"thoughtNumber": 1}, schema
        )
        assert result["thoughtNumber"] == 1

    def test_empty_arguments(self):
        """空参数: 返回空字典"""
        manager = self._make_manager()
        result = manager._coerce_arguments_by_schema({}, {"properties": {}})
        assert result == {}

    def test_empty_schema(self):
        """空schema: 返回原参数"""
        manager = self._make_manager()
        result = manager._coerce_arguments_by_schema({"a": "1"}, {})
        assert result == {"a": "1"}

    def test_unknown_key_unchanged(self):
        """参数名不在schema properties中: 保持原值"""
        manager = self._make_manager()
        schema = {"type": "object", "properties": {
            "known": {"type": "integer"}
        }}
        result = manager._coerce_arguments_by_schema(
            {"known": "1", "unknown": "2"}, schema
        )
        assert result["known"] == 1
        assert result["unknown"] == "2"


class TestCallToolTimeout:
    """P0-7: session.call_tool() 超时保护测试"""

    @pytest.mark.asyncio
    async def test_call_tool_timeout_returns_failure(self):
        """call_tool超时: 返回ToolResult(success=False)而非抛异常"""
        manager = MCPClientManager(mcp_config=_make_server_config())
        # 构造mock session: call_tool为慢调用
        mock_session = MagicMock()

        async def _slow_call_tool(name, args):
            await asyncio.sleep(10)
            return MagicMock()

        mock_session.call_tool = _slow_call_tool
        manager._clients = {"test_server": mock_session}
        manager._tools = {"test_server": [_make_tool_mock(name="tool")]}

        # 将超时设为极小值(0.1s)加速测试
        with patch("app.domain.services.tools.mcp._MCP_INVOKE_TIMEOUT", 0.1):
            result = await manager.invoke("mcp_test_server_tool", {"param": "value"})

        assert isinstance(result, ToolResult)
        assert result.success is False
        assert "超时" in result.message
        # 直接加载模式: 超时结果含 _timeout 标记,供 MCPTool.invoke() 检测并转异步
        assert isinstance(result.data, dict)
        assert result.data.get("_timeout") is True
        assert result.data.get("_tool_name") == "tool"
        assert result.data.get("_arguments") == {"param": "value"}

    @pytest.mark.asyncio
    async def test_call_tool_timeout_injects_task_wait_hint(self):
        """批次 29: call_tool超时返回 message 应包含 task_wait 异步回退引导

        根因: 原超时返回"请重试或使用替代方案",LLM 不知道可切换异步模式,
        导致同步重试陷入循环(会话1 重复调用 32 次)。
        直接加载模式: 引导 task_wait 而非 async_mode(桥接工具已移除)。
        """
        manager = MCPClientManager(mcp_config=_make_server_config())
        mock_session = MagicMock()

        async def _slow_call_tool(name, args):
            await asyncio.sleep(10)
            return MagicMock()

        mock_session.call_tool = _slow_call_tool
        manager._clients = {"test_server": mock_session}
        manager._tools = {"test_server": [_make_tool_mock(name="tool")]}

        with patch("app.domain.services.tools.mcp._MCP_INVOKE_TIMEOUT", 0.1):
            result = await manager.invoke("mcp_test_server_tool", {"param": "value"})

        assert result.success is False
        # 直接加载模式: 超时返回必须包含 task_wait + 异步引导
        assert "task_wait" in result.message
        assert "异步" in result.message
        assert "task_id" in result.message

    @pytest.mark.asyncio
    async def test_call_tool_success_on_fast_response(self):
        """call_tool正常响应: 返回成功结果"""
        manager = MCPClientManager(mcp_config=_make_server_config())
        # 构造mock session和返回结果
        mock_result = MagicMock()
        mock_result.content = [MagicMock(type="text", text="success output")]
        mock_session = MagicMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        manager._clients = {"test_server": mock_session}
        manager._tools = {"test_server": [_make_tool_mock(name="tool")]}

        result = await manager.invoke("mcp_test_server_tool", {"param": "value"})

        assert result.success is True
        assert "success output" in result.data

    @pytest.mark.asyncio
    async def test_call_tool_coerces_args_before_call(self):
        """call_tool前执行参数类型强制转换"""
        manager = MCPClientManager(mcp_config=_make_server_config())
        mock_result = MagicMock()
        mock_result.content = [MagicMock(type="text", text="ok")]
        mock_session = MagicMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        manager._clients = {"test_server": mock_session}
        # 工具schema声明thoughtNumber为integer
        manager._tools = {"test_server": [_make_tool_mock(
            name="tool",
            input_schema={"type": "object", "properties": {
                "thoughtNumber": {"type": "integer"}
            }}
        )]}

        # LLM传字符串"2"
        await manager.invoke("mcp_test_server_tool", {"thoughtNumber": "2"})

        # 验证call_tool收到的是int 2(已强制转换)
        call_args = mock_session.call_tool.call_args
        # call_tool(original_tool_name, arguments) — arguments是第二个位置参数
        assert call_args.args[1]["thoughtNumber"] == 2
        assert isinstance(call_args.args[1]["thoughtNumber"], int)


class TestListToolsTimeout:
    """P0-8: session.list_tools() 超时保护测试"""

    @pytest.mark.asyncio
    async def test_list_tools_timeout_sets_empty_list(self):
        """list_tools超时: 设置空工具列表，不抛异常"""
        manager = MCPClientManager(mcp_config=_make_server_config())
        mock_session = MagicMock()

        async def _slow_list_tools():
            await asyncio.sleep(10)

        mock_session.list_tools = _slow_list_tools

        with patch("app.domain.services.tools.mcp._MCP_LIST_TOOLS_TIMEOUT", 0.1):
            await manager._cache_mcp_server_tools("test_server", mock_session)

        # 超时后应设置空列表(不阻断连接)
        assert manager._tools["test_server"] == []

    @pytest.mark.asyncio
    async def test_list_tools_success_caches_tools(self):
        """list_tools正常: 缓存工具列表"""
        manager = MCPClientManager(mcp_config=_make_server_config())
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.tools = [_make_tool_mock("tool1"), _make_tool_mock("tool2")]
        mock_session.list_tools = AsyncMock(return_value=mock_response)

        await manager._cache_mcp_server_tools("test_server", mock_session)

        assert len(manager._tools["test_server"]) == 2

    @pytest.mark.asyncio
    async def test_list_tools_exception_sets_empty_list(self):
        """list_tools异常: 设置空工具列表"""
        manager = MCPClientManager(mcp_config=_make_server_config())
        mock_session = MagicMock()
        mock_session.list_tools = AsyncMock(side_effect=RuntimeError("connection reset"))

        await manager._cache_mcp_server_tools("test_server", mock_session)

        assert manager._tools["test_server"] == []
