# -*- coding: utf-8 -*-
"""批次45 P1-1: MCP 同步超时自动转异步单元测试

覆盖 _auto_fallback_to_async 方法:
- 同步超时后代码层直接启动后台任务(不依赖LLM follow hint)
- 无 callback_manager 时降级返回 None
- 异常时降级返回 None(向后兼容)
- 返回含 task_id 的 ToolResult 引导 LLM 调用 task_wait
"""
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from app.domain.models.tool_result import ToolResult


class TestP11AutoFallbackToAsync:
    """P1-1: MCP同步超时自动转异步"""

    def _build_mcp_tool(self, callback_manager=None, poll_stats=None):
        """构建MCPTool实例(绕过__init__)用于测试_auto_fallback_to_async"""
        from app.domain.services.tools.mcp import MCPTool
        tool = object.__new__(MCPTool)
        tool._callback_manager = callback_manager
        tool._background_tasks = {}
        tool._poll_stats = poll_stats
        tool._session_id = "test_session"
        return tool

    def test_no_callback_manager_returns_none(self):
        """无callback_manager时应返回None(降级原超时引导)"""
        tool = self._build_mcp_tool(callback_manager=None)
        result = asyncio.get_event_loop().run_until_complete(
            tool._auto_fallback_to_async("test_tool", {"arg": "val"})
        )
        assert result is None

    def test_returns_toolresult_with_task_id(self):
        """成功时应返回含task_id的ToolResult"""
        callback_manager = MagicMock()
        callback_manager.register = AsyncMock()
        tool = self._build_mcp_tool(callback_manager=callback_manager, poll_stats=MagicMock())
        with patch.object(tool, "_run_mcp_async_polling", new=AsyncMock()):
            result = asyncio.get_event_loop().run_until_complete(
                tool._auto_fallback_to_async("test_tool", {"arg": "val"})
            )
        assert result is not None
        assert result.success is True
        assert "task_id" in result.data
        assert result.data["task_id"].startswith("mcp_")
        assert result.data["auto_async"] is True
        assert "task_wait" in result.message

    def test_registers_task_with_callback_manager(self):
        """应向callback_manager注册task_id"""
        callback_manager = MagicMock()
        callback_manager.register = AsyncMock()
        tool = self._build_mcp_tool(callback_manager=callback_manager)
        with patch.object(tool, "_run_mcp_async_polling", new=AsyncMock()):
            result = asyncio.get_event_loop().run_until_complete(
                tool._auto_fallback_to_async("test_tool", {})
            )
        callback_manager.register.assert_called_once()
        registered_task_id = callback_manager.register.call_args[0][0]
        assert registered_task_id == result.data["task_id"]

    def test_starts_background_task(self):
        """应启动后台任务并存入_background_tasks"""
        callback_manager = MagicMock()
        callback_manager.register = AsyncMock()
        tool = self._build_mcp_tool(callback_manager=callback_manager)
        with patch.object(tool, "_run_mcp_async_polling", new=AsyncMock()):
            result = asyncio.get_event_loop().run_until_complete(
                tool._auto_fallback_to_async("test_tool", {})
            )
        task_id = result.data["task_id"]
        assert task_id in tool._background_tasks
        # 清理后台任务
        task = tool._background_tasks[task_id]
        task.cancel()

    def test_records_poll_stats(self):
        """有poll_stats时应记录异步任务"""
        callback_manager = MagicMock()
        callback_manager.register = AsyncMock()
        poll_stats = MagicMock()
        tool = self._build_mcp_tool(callback_manager=callback_manager, poll_stats=poll_stats)
        with patch.object(tool, "_run_mcp_async_polling", new=AsyncMock()):
            result = asyncio.get_event_loop().run_until_complete(
                tool._auto_fallback_to_async("test_tool", {})
            )
        poll_stats.record_async_task.assert_called_once_with("test_tool")
        # 清理
        tool._background_tasks[result.data["task_id"]].cancel()

    def test_exception_returns_none(self):
        """callback_manager.register抛异常时应返回None(降级)"""
        callback_manager = MagicMock()
        callback_manager.register = AsyncMock(side_effect=Exception("register failed"))
        tool = self._build_mcp_tool(callback_manager=callback_manager)
        result = asyncio.get_event_loop().run_until_complete(
            tool._auto_fallback_to_async("test_tool", {})
        )
        assert result is None

    def test_auto_async_flag_constant_enabled(self):
        """_MCP_SYNC_TIMEOUT_AUTO_ASYNC 常量应为True(开关默认启用)"""
        from app.domain.services.tools.mcp import _MCP_SYNC_TIMEOUT_AUTO_ASYNC
        assert _MCP_SYNC_TIMEOUT_AUTO_ASYNC is True
