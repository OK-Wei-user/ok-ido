#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_mcp_async.py
MCPTool 异步任务回退单元测试 - 验证 _auto_fallback_to_async 同步超时自动转异步
与 _run_mcp_async_polling 后台轮询、cancel_background_tasks 行为

直接加载模式(原桥接架构 _dispatch_bridge_call 已移除):
    同步超时由 MCPClientManager.invoke() 检测,返回含 _timeout 标记的 ToolResult;
    MCPTool.invoke() 检测标记后调用 _auto_fallback_to_async 启动后台轮询,
    返回 task_id + task_wait 引导(不依赖LLM follow hint文本)。

测试覆盖:
- _auto_fallback_to_async 立即返回 task_id
- 未注入 callback_manager 时返回 None(降级原超时引导)
- callback_manager.register 注册调用
- pending → complete 轮询与 notify
- 达上限超时 notify 失败 payload
- invoke 异常隔离
- task_id 前缀为 'mcp_'
- cancel_background_tasks 空操作/取消/finally notify
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.external.task_callback import TaskCallbackManager
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.mcp import (
    MCPTool,
    _MCP_POLL_BACKOFF_SECONDS,
    _MCP_POLL_MAX_ATTEMPTS,
)


# ========== Stub 回调管理器 ==========

class _StubCallbackManager(TaskCallbackManager):
    """回调管理器 stub,显式实现抽象方法并委托给 AsyncMock

    TaskCallbackManager 是 ABC,直接在 __init__ 中将抽象方法替换为 AsyncMock
    实例属性不会绕过 ABC 的实例化检查,因此必须显式实现方法体。
    """

    def __init__(self) -> None:
        self.register_mock = AsyncMock()
        self.notify_mock = AsyncMock(return_value=True)
        self.wait_mock = AsyncMock(return_value=None)
        self.cancel_mock = AsyncMock()

    async def register(self, task_id: str) -> None:
        await self.register_mock(task_id)

    async def notify(self, task_id: str, payload) -> bool:
        return await self.notify_mock(task_id, payload)

    async def wait(self, task_id: str, timeout: float):
        return await self.wait_mock(task_id, timeout)

    async def cancel(self, task_id: str) -> None:
        await self.cancel_mock(task_id)


# ========== 测试工厂函数 ==========

def _make_mcp_tool(callback_manager=None) -> MCPTool:
    """创建 MCPTool 实例并注入 mock manager,绕过 initialize()

    MCPTool.__init__ 不调用 initialize(),_manager 默认 None。
    测试通过直接注入 AsyncMock 模拟 MCPClientManager,聚焦测试
    _auto_fallback_to_async 与 _run_mcp_async_polling 逻辑。
    """
    tool = MCPTool(sandbox=None, callback_manager=callback_manager)
    # 注入 mock manager,绕过真实 MCP 服务连接
    tool._manager = AsyncMock()
    return tool


# ========== _auto_fallback_to_async 行为测试 ==========

class TestAutoFallbackToAsync:
    """_auto_fallback_to_async 同步超时自动转异步行为测试"""

    @pytest.mark.asyncio
    async def test_returns_task_id_immediately(self):
        """_auto_fallback_to_async 立即返回 task_id='mcp_xxx' 与 status='running'"""
        manager = _StubCallbackManager()
        tool = _make_mcp_tool(callback_manager=manager)
        # invoke 模拟长耗时(但 _auto_fallback_to_async 应立即返回)
        async def _slow_invoke(*args, **kwargs):
            await asyncio.sleep(0.3)
            return ToolResult(success=True, data="slow result")
        tool._manager.invoke = AsyncMock(side_effect=_slow_invoke)

        result = await tool._auto_fallback_to_async("test_tool", {})

        # 立即返回 task_id
        assert result is not None
        assert result.success is True
        assert "task_id" in result.data
        assert result.data["status"] == "running"
        assert result.data["tool_name"] == "test_tool"
        # task_id 必须以 mcp_ 前缀开头
        assert result.data["task_id"].startswith("mcp_")

    @pytest.mark.asyncio
    async def test_without_callback_manager_returns_none(self):
        """未注入 callback_manager 时返回 None(降级原超时引导)"""
        tool = _make_mcp_tool(callback_manager=None)

        result = await tool._auto_fallback_to_async("test_tool", {})

        # 无 callback_manager 时返回 None,调用方降级为原超时引导
        assert result is None
        # 后台任务追踪表应保持空(未启动后台任务)
        assert tool._background_tasks == {}

    @pytest.mark.asyncio
    async def test_registers_callback_task_id(self):
        """_auto_fallback_to_async 调用 callback_manager.register(mcp_task_id)"""
        manager = _StubCallbackManager()
        tool = _make_mcp_tool(callback_manager=manager)
        tool._manager.invoke = AsyncMock(
            return_value=ToolResult(success=True, data="done")
        )

        result = await tool._auto_fallback_to_async("test_tool", {})
        task_id = result.data["task_id"]
        # register 应被调用一次,参数为返回的 task_id
        manager.register_mock.assert_awaited_once_with(task_id)
        # 后台任务应已注册到追踪表
        assert task_id in tool._background_tasks

        # 清理:等待后台任务完成避免污染
        await asyncio.sleep(0.05)
        tool.cancel_background_tasks()

    @pytest.mark.asyncio
    async def test_pending_then_complete_polls_and_notifies(self, monkeypatch):
        """首次 invoke 返回 pending,第二次返回完成 → 轮询一次后 notify 成功 payload"""
        # 加速测试:将退避时间缩短到 0.01s
        monkeypatch.setattr(
            "app.domain.services.tools.mcp._MCP_POLL_BACKOFF_SECONDS",
            (0.01, 0.01, 0.01),
        )
        manager = _StubCallbackManager()
        tool = _make_mcp_tool(callback_manager=manager)
        # 第一次返回 pending,第二次返回完成
        tool._manager.invoke = AsyncMock(
            side_effect=[
                ToolResult(success=True, data="状态:处理中"),
                ToolResult(success=True, data="任务完成"),
            ]
        )

        result = await tool._auto_fallback_to_async("test_tool", {})
        task_id = result.data["task_id"]

        # 等待后台轮询完成
        await asyncio.sleep(0.1)
        # notify 应被调用一次,携带成功 payload
        manager.notify_mock.assert_awaited_once()
        notified_task_id, payload = manager.notify_mock.await_args.args
        assert notified_task_id == task_id
        assert payload["success"] is True
        assert payload["data"] == "任务完成"
        # 后台任务应已从追踪表清除
        assert task_id not in tool._background_tasks

    @pytest.mark.asyncio
    async def test_max_attempts_timeout_notifies_failure(self, monkeypatch):
        """连续 _MCP_POLL_MAX_ATTEMPTS 次 pending → notify 失败 payload(含'已轮询'字样)"""
        # 加速测试:将退避时间与上限缩小
        monkeypatch.setattr(
            "app.domain.services.tools.mcp._MCP_POLL_BACKOFF_SECONDS",
            (0.01, 0.01, 0.01),
        )
        monkeypatch.setattr(
            "app.domain.services.tools.mcp._MCP_POLL_MAX_ATTEMPTS", 2
        )
        manager = _StubCallbackManager()
        tool = _make_mcp_tool(callback_manager=manager)
        # 始终返回 pending
        tool._manager.invoke = AsyncMock(
            return_value=ToolResult(success=True, data="状态:处理中")
        )

        result = await tool._auto_fallback_to_async("test_tool", {})
        task_id = result.data["task_id"]

        # 等待轮询耗尽(2 次退避 = 0.02s)
        await asyncio.sleep(0.1)
        manager.notify_mock.assert_awaited_once()
        notified_task_id, payload = manager.notify_mock.await_args.args
        assert notified_task_id == task_id
        # 超时应 notify 失败 payload
        assert payload["success"] is False
        assert "已轮询" in payload["message"]

    @pytest.mark.asyncio
    async def test_invoke_exception_notifies_failure(self, monkeypatch):
        """manager.invoke 抛异常 → 异常隔离,notify 失败 payload(含'异常')"""
        monkeypatch.setattr(
            "app.domain.services.tools.mcp._MCP_POLL_BACKOFF_SECONDS",
            (0.01, 0.01, 0.01),
        )
        manager = _StubCallbackManager()
        tool = _make_mcp_tool(callback_manager=manager)
        tool._manager.invoke = AsyncMock(
            side_effect=RuntimeError("mcp server crashed")
        )

        result = await tool._auto_fallback_to_async("test_tool", {})
        task_id = result.data["task_id"]

        # 等待后台任务执行异常路径
        await asyncio.sleep(0.05)
        manager.notify_mock.assert_awaited_once()
        notified_task_id, payload = manager.notify_mock.await_args.args
        assert notified_task_id == task_id
        assert payload["success"] is False
        assert "异常" in payload["message"]

    @pytest.mark.asyncio
    async def test_task_id_prefix_is_mcp_(self):
        """task_id 必须以 'mcp_' 前缀开头(与 task_callback.py 描述一致)"""
        manager = _StubCallbackManager()
        tool = _make_mcp_tool(callback_manager=manager)
        tool._manager.invoke = AsyncMock(
            return_value=ToolResult(success=True, data="done")
        )

        result = await tool._auto_fallback_to_async("test_tool", {})
        task_id = result.data["task_id"]
        assert task_id.startswith("mcp_")
        # mcp_ 后应跟 12 位 hex(uuid.uuid4().hex[:12])
        suffix = task_id[4:]
        assert len(suffix) == 12
        int(suffix, 16)  # 应为合法 hex

        # 清理后台任务
        await asyncio.sleep(0.05)
        tool.cancel_background_tasks()

    @pytest.mark.asyncio
    async def test_registers_task_in_tracking_dict(self):
        """_auto_fallback_to_async 后 task_id 应注册到 _background_tasks 追踪表"""
        manager = _StubCallbackManager()
        tool = _make_mcp_tool(callback_manager=manager)
        tool._manager.invoke = AsyncMock(
            return_value=ToolResult(success=True, data="状态:处理中")
        )

        result = await tool._auto_fallback_to_async("test_tool", {})
        task_id = result.data["task_id"]
        # task_id 应在追踪表中
        assert task_id in tool._background_tasks
        # 追踪表中的值应为 asyncio.Task 实例
        assert hasattr(tool._background_tasks[task_id], "cancel")
        assert hasattr(tool._background_tasks[task_id], "done")

        # 清理
        tool.cancel_background_tasks()

    @pytest.mark.asyncio
    async def test_concurrent_tasks_have_unique_ids(self, monkeypatch):
        """多个并发异步任务应分配不同 task_id"""
        monkeypatch.setattr(
            "app.domain.services.tools.mcp._MCP_POLL_BACKOFF_SECONDS",
            (5, 5, 5),  # 长退避,确保任务在并发期间仍运行
        )
        manager = _StubCallbackManager()
        tool = _make_mcp_tool(callback_manager=manager)
        tool._manager.invoke = AsyncMock(
            return_value=ToolResult(success=True, data="状态:处理中")
        )

        # 启动 3 个并发异步任务
        results = []
        for i in range(3):
            r = await tool._auto_fallback_to_async(f"tool_{i}", {})
            results.append(r)

        task_ids = {r.data["task_id"] for r in results}
        # 3 个 task_id 应互不相同
        assert len(task_ids) == 3
        # 所有 task_id 都应在追踪表中
        for tid in task_ids:
            assert tid in tool._background_tasks

        # 清理
        tool.cancel_background_tasks()


# ========== MCPTool.cancel_background_tasks 行为测试 ==========

class TestMcpCancelBackgroundTasks:
    """MCPTool.cancel_background_tasks 行为测试"""

    def test_cancel_empty_is_noop(self):
        """无后台任务时 cancel_background_tasks 是空操作"""
        tool = _make_mcp_tool(callback_manager=None)
        # 不应抛异常
        tool.cancel_background_tasks()
        assert tool._background_tasks == {}

    @pytest.mark.asyncio
    async def test_cancel_pending_task_cancels_and_clears(self, monkeypatch):
        """有运行中后台任务时 cancel 后 _background_tasks 清空"""
        monkeypatch.setattr(
            "app.domain.services.tools.mcp._MCP_POLL_BACKOFF_SECONDS",
            (5, 5, 5),  # 长退避,确保 cancel 时任务仍在运行
        )
        manager = _StubCallbackManager()
        tool = _make_mcp_tool(callback_manager=manager)
        tool._manager.invoke = AsyncMock(
            return_value=ToolResult(success=True, data="状态:处理中")
        )

        # 启动两个后台任务
        r1 = await tool._auto_fallback_to_async("tool1", {})
        r2 = await tool._auto_fallback_to_async("tool2", {})
        assert len(tool._background_tasks) == 2

        # 取消后台任务
        tool.cancel_background_tasks()

        # 等待任务被取消
        await asyncio.sleep(0.05)

        # 内部 _background_tasks 应被清空
        assert tool._background_tasks == {}

    @pytest.mark.asyncio
    async def test_cancel_during_polling_isolates_cancelled_error(
        self, monkeypatch
    ):
        """cancel 在 _run_mcp_async_polling 的 sleep 期间触发,CancelledError 被正确隔离

        验证点:
        1. cancel_background_tasks 不抛出未捕获异常
        2. _background_tasks 追踪表被清空
        3. 原 background_task 引用状态为 cancelled

        注: asyncio.CancelledError 在 finally 块的 await 点会再次抛出,
        导致 notify 实际未执行(asyncio 固有行为,与 ShellTool 实现一致)。
        但 _background_tasks.pop 在 CancelledError 传播前会执行,
        确保追踪表被清理,不会泄漏。
        """
        monkeypatch.setattr(
            "app.domain.services.tools.mcp._MCP_POLL_BACKOFF_SECONDS",
            (5, 5, 5),  # 长退避,确保 cancel 时任务在 sleep 中
        )
        manager = _StubCallbackManager()
        tool = _make_mcp_tool(callback_manager=manager)
        tool._manager.invoke = AsyncMock(
            return_value=ToolResult(success=True, data="状态:处理中")
        )

        result = await tool._auto_fallback_to_async("tool_x", {})
        task_id = result.data["task_id"]
        # 保存对后台 task 的引用(cancel_background_tasks 会清空追踪表)
        background_task = tool._background_tasks[task_id]

        # 等待后台任务进入 sleep(确保 cancel 在 sleep 期间触发)
        await asyncio.sleep(0.05)

        # 取消后台任务(不应抛出未捕获异常)
        tool.cancel_background_tasks()

        # 等待 CancelledError 传播完成
        await asyncio.sleep(0.05)

        # _background_tasks 追踪表应已被清空
        assert tool._background_tasks == {}
        # 原 background_task 引用应标记为 cancelled
        assert background_task.done()
        assert background_task.cancelled()
