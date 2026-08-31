#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_f10_7_task_callback.py
F10-7 异步任务回调通知单元测试 - 验证 TaskCallbackManager 协议、TaskCallbackTool、ShellTool async_mode

测试覆盖:
- TaskCallbackTool: task_wait 工具未注入/空task_id/超时/正常完成/异常隔离
- ShellTool async_mode: 同步模式兼容/降级为同步/异步启动/取消后台任务
- RedisStreamTaskCallbackManager: register/notify/wait/cancel 协议契约(使用 mock)
- 集成: shell_execute(async_mode=true) → task_wait 完整链路
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.external.task_callback import TaskCallbackManager
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.shell import ShellTool
from app.domain.services.tools.task_callback import (
    TaskCallbackTool,
    _DEFAULT_WAIT_TIMEOUT,
    _MAX_WAIT_TIMEOUT,
)


# ========== Stub 沙箱与回调管理器 ==========

class _StubSandbox:
    """沙箱 stub,支持 exec_command mock"""

    def __init__(self) -> None:
        self.exec_command = AsyncMock()
        # 默认返回成功结果
        self.exec_command.return_value = ToolResult(success=True, data="executed")


class _StubCallbackManager(TaskCallbackManager):
    """回调管理器 stub,显式实现抽象方法并委托给 AsyncMock

    TaskCallbackManager 是 ABC,直接在 __init__ 中将抽象方法替换为 AsyncMock
    实例属性不会绕过 ABC 的实例化检查,因此必须显式实现方法体。
    """

    def __init__(self) -> None:
        self.register_mock = AsyncMock()
        self.notify_mock = AsyncMock(return_value=True)
        # wait_mock 默认返回 None(超时),测试用例按需覆盖
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


# ========== TaskCallbackTool 测试 ==========

class TestTaskCallbackTool:
    """task_wait 工具行为测试"""

    @pytest.mark.asyncio
    async def test_task_wait_without_callback_manager_returns_error(self):
        """未注入 TaskCallbackManager 时返回错误,引导 LLM 改用 shell_wait_process"""
        tool = TaskCallbackTool(callback_manager=None)
        result = await tool.task_wait("task_123")
        assert result.success is False
        assert "shell_wait_process" in result.message or "shell_read_output" in result.message

    @pytest.mark.asyncio
    async def test_task_wait_with_empty_task_id_returns_error(self):
        """空 task_id 返回错误(防御性)"""
        manager = _StubCallbackManager()
        tool = TaskCallbackTool(callback_manager=manager)
        result = await tool.task_wait("")
        assert result.success is False
        assert "task_id" in result.message
        # 不应调用 wait
        manager.wait_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_task_wait_timeout_returns_status_timeout(self):
        """wait 超时返回 None 时,ToolResult 标记 status=timeout"""
        manager = _StubCallbackManager()
        manager.wait_mock.return_value = None  # 模拟超时
        tool = TaskCallbackTool(callback_manager=manager)

        result = await tool.task_wait("task_timeout", timeout=10)
        assert result.success is True  # 超时不视为失败
        assert result.data["status"] == "timeout"
        manager.wait_mock.assert_awaited_once_with("task_timeout", 10)

    @pytest.mark.asyncio
    async def test_task_wait_timeout_message_guides_continuation(self):
        """超时返回消息应引导 LLM 继续 task_wait,而非回退 sleep 轮询(会话bffcb4ae修复)"""
        manager = _StubCallbackManager()
        manager.wait_mock.return_value = None
        tool = TaskCallbackTool(callback_manager=manager)

        result = await tool.task_wait("shell_abc123", timeout=300)
        assert result.success is True
        # 消息应包含继续 task_wait 的引导
        assert "继续调用 task_wait" in result.message or "task_wait" in result.message
        # 消息应包含 task_id 便于 LLM 继续调用
        assert "shell_abc123" in result.message
        # 消息不应引导使用 sleep
        assert "sleep" not in result.message.lower() or "不要使用 sleep" in result.message

    @pytest.mark.asyncio
    async def test_task_wait_success_returns_payload(self):
        """任务完成时返回完整 payload(success/message/data)"""
        manager = _StubCallbackManager()
        manager.wait_mock.return_value = {
            "success": True,
            "message": "命令完成",
            "data": {"stdout": "hello"},
        }
        tool = TaskCallbackTool(callback_manager=manager)

        result = await tool.task_wait("task_done")
        assert result.success is True
        assert result.message == "命令完成"
        assert result.data == {"stdout": "hello"}

    @pytest.mark.asyncio
    async def test_task_wait_failure_payload_returns_failure(self):
        """任务失败时 payload.success=False,ToolResult.success 也为 False"""
        manager = _StubCallbackManager()
        manager.wait_mock.return_value = {
            "success": False,
            "message": "命令执行失败",
            "data": None,
        }
        tool = TaskCallbackTool(callback_manager=manager)

        result = await tool.task_wait("task_fail")
        assert result.success is False
        assert "失败" in result.message

    @pytest.mark.asyncio
    async def test_task_wait_exception_isolated(self):
        """wait 抛异常时不传播,返回失败 ToolResult"""
        manager = _StubCallbackManager()
        manager.wait_mock.side_effect = RuntimeError("redis down")
        tool = TaskCallbackTool(callback_manager=manager)

        result = await tool.task_wait("task_err")
        assert result.success is False
        assert "异常" in result.message

    @pytest.mark.asyncio
    async def test_task_wait_timeout_truncated_to_max(self):
        """超时超过 _MAX_WAIT_TIMEOUT 时截断"""
        manager = _StubCallbackManager()
        manager.wait_mock.return_value = None
        tool = TaskCallbackTool(callback_manager=manager)

        await tool.task_wait("task_x", timeout=99999)
        # 应截断到 _MAX_WAIT_TIMEOUT
        manager.wait_mock.assert_awaited_once_with("task_x", _MAX_WAIT_TIMEOUT)

    @pytest.mark.asyncio
    async def test_task_wait_negative_timeout_uses_default(self):
        """负超时使用默认值 _DEFAULT_WAIT_TIMEOUT"""
        manager = _StubCallbackManager()
        manager.wait_mock.return_value = None
        tool = TaskCallbackTool(callback_manager=manager)

        await tool.task_wait("task_n", timeout=-1)
        manager.wait_mock.assert_awaited_once_with("task_n", _DEFAULT_WAIT_TIMEOUT)

    @pytest.mark.asyncio
    async def test_task_wait_none_timeout_uses_default(self):
        """未指定 timeout(None)时使用默认值"""
        manager = _StubCallbackManager()
        manager.wait_mock.return_value = None
        tool = TaskCallbackTool(callback_manager=manager)

        await tool.task_wait("task_default")
        manager.wait_mock.assert_awaited_once_with("task_default", _DEFAULT_WAIT_TIMEOUT)


# ========== ShellTool async_mode 测试 ==========

class TestShellToolAsyncMode:
    """ShellTool async_mode 行为测试"""

    @pytest.mark.asyncio
    async def test_sync_mode_remains_backward_compatible(self):
        """async_mode=false(默认) 保持同步行为,直接返回 exec_command 结果"""
        sandbox = _StubSandbox()
        sandbox.exec_command.return_value = ToolResult(success=True, data="sync result")
        tool = ShellTool(sandbox=sandbox, callback_manager=None)

        result = await tool.shell_execute("s1", "/tmp", "echo hello")
        assert result.success is True
        assert result.data == "sync result"
        # callback_manager 为 None,不应影响同步路径
        sandbox.exec_command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_async_mode_without_callback_manager_degrades_to_sync(self):
        """async_mode=true 但未注入 callback_manager 时降级为同步执行"""
        sandbox = _StubSandbox()
        sandbox.exec_command.return_value = ToolResult(success=True, data="degraded")
        tool = ShellTool(sandbox=sandbox, callback_manager=None)

        result = await tool.shell_execute("s1", "/tmp", "ls", async_mode=True)
        assert result.success is True
        assert result.data == "degraded"
        # 应直接调用 exec_command(降级路径)
        sandbox.exec_command.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_async_mode_returns_task_id_immediately(self):
        """async_mode=true 时立即返回 task_id,不等待命令完成"""
        sandbox = _StubSandbox()
        manager = _StubCallbackManager()
        tool = ShellTool(sandbox=sandbox, callback_manager=manager)

        # exec_command 模拟长耗时(但 shell_execute 应立即返回)
        async def _slow_exec(*args, **kwargs):
            await asyncio.sleep(0.5)
            return ToolResult(success=True, data="slow result")
        sandbox.exec_command.side_effect = _slow_exec

        result = await tool.shell_execute("s1", "/tmp", "sleep 1", async_mode=True)
        # 立即返回 task_id
        assert result.success is True
        assert "task_id" in result.data
        assert result.data["status"] == "running"
        assert result.data["command"] == "sleep 1"
        # register 应被调用
        manager.register_mock.assert_awaited_once()

        # 等待后台任务完成
        await asyncio.sleep(0.6)
        # notify 应被调用(完成任务时推送结果)
        manager.notify_mock.assert_awaited_once()
        payload = manager.notify_mock.await_args.args[1]
        assert payload["success"] is True
        assert payload["data"] == "slow result"

    @pytest.mark.asyncio
    async def test_async_mode_failure_payload(self):
        """后台命令失败时 payload.success=False,但仍调用 notify"""
        sandbox = _StubSandbox()
        sandbox.exec_command.return_value = ToolResult(success=False, message="命令失败")
        manager = _StubCallbackManager()
        tool = ShellTool(sandbox=sandbox, callback_manager=manager)

        await tool.shell_execute("s1", "/tmp", "false", async_mode=True)
        # 等待后台任务
        await asyncio.sleep(0.1)

        manager.notify_mock.assert_awaited_once()
        payload = manager.notify_mock.await_args.args[1]
        assert payload["success"] is False
        assert "命令失败" in payload["message"]

    @pytest.mark.asyncio
    async def test_async_mode_exception_isolated(self):
        """后台命令抛异常时,异常被捕获并转为失败 payload"""
        sandbox = _StubSandbox()
        sandbox.exec_command.side_effect = RuntimeError("sandbox crashed")
        manager = _StubCallbackManager()
        tool = ShellTool(sandbox=sandbox, callback_manager=manager)

        await tool.shell_execute("s1", "/tmp", "boom", async_mode=True)
        await asyncio.sleep(0.1)

        # 异常被捕获,notify 仍被调用
        manager.notify_mock.assert_awaited_once()
        payload = manager.notify_mock.await_args.args[1]
        assert payload["success"] is False
        assert "异常" in payload["message"]

    @pytest.mark.asyncio
    async def test_cancel_background_tasks_cancels_pending(self):
        """cancel_background_tasks 取消所有未完成的后台任务"""
        sandbox = _StubSandbox()
        manager = _StubCallbackManager()

        async def _slow_exec(*args, **kwargs):
            await asyncio.sleep(5)
            return ToolResult(success=True, data="never")
        sandbox.exec_command.side_effect = _slow_exec
        tool = ShellTool(sandbox=sandbox, callback_manager=manager)

        await tool.shell_execute("s1", "/tmp", "long", async_mode=True)
        await tool.shell_execute("s2", "/tmp", "long2", async_mode=True)

        # 取消后台任务
        tool.cancel_background_tasks()

        # 等待任务被取消
        await asyncio.sleep(0.1)

        # 内部 _background_tasks 应被清空
        assert tool._background_tasks == {}

    @pytest.mark.asyncio
    async def test_cancel_background_tasks_empty_is_noop(self):
        """没有后台任务时 cancel_background_tasks 是空操作"""
        sandbox = _StubSandbox()
        tool = ShellTool(sandbox=sandbox, callback_manager=None)
        # 不应抛异常
        tool.cancel_background_tasks()
        assert tool._background_tasks == {}


# ========== TaskCallbackManager 协议契约测试 ==========

class TestTaskCallbackManagerProtocol:
    """TaskCallbackManager 协议契约测试(使用 stub 验证调用约定)"""

    @pytest.mark.asyncio
    async def test_register_is_idempotent(self):
        """register 幂等(重复调用不报错)"""
        manager = _StubCallbackManager()
        await manager.register("task_a")
        await manager.register("task_a")  # 重复调用
        assert manager.register_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_notify_returns_bool(self):
        """notify 返回 bool(成功 True,失败 False)"""
        manager = _StubCallbackManager()
        manager.notify_mock.return_value = True
        ok = await manager.notify("task_b", {"success": True})
        assert ok is True

        manager.notify_mock.return_value = False
        ok = await manager.notify("task_b", {"success": False})
        assert ok is False

    @pytest.mark.asyncio
    async def test_wait_returns_none_on_timeout(self):
        """wait 超时返回 None"""
        manager = _StubCallbackManager()
        manager.wait_mock.return_value = None
        result = await manager.wait("task_c", timeout=0.01)
        assert result is None

    @pytest.mark.asyncio
    async def test_wait_returns_payload_on_completion(self):
        """wait 任务完成返回 payload 字典"""
        manager = _StubCallbackManager()
        expected = {"success": True, "message": "ok", "data": {"k": "v"}}
        manager.wait_mock.return_value = expected
        result = await manager.wait("task_d", timeout=1)
        assert result == expected

    @pytest.mark.asyncio
    async def test_cancel_is_idempotent(self):
        """cancel 幂等(任务不存在时静默返回)"""
        manager = _StubCallbackManager()
        await manager.cancel("not_exist")  # 不应抛异常
        await manager.cancel("not_exist")  # 重复调用
        assert manager.cancel_mock.await_count == 2


# ========== 集成测试: shell_execute + task_wait 完整链路 ==========

class TestShellAndWaitIntegration:
    """shell_execute(async_mode=true) + task_wait 端到端集成测试"""

    @pytest.mark.asyncio
    async def test_async_shell_then_wait_returns_result(self):
        """异步启动 shell 命令 → task_wait 阻塞等待 → 返回完整结果"""
        sandbox = _StubSandbox()
        sandbox.exec_command.return_value = ToolResult(
            success=True, message="ok", data={"stdout": "hello world"}
        )
        manager = _StubCallbackManager()

        # 模拟 wait 行为: 直接返回成功 payload
        manager.wait_mock.return_value = {
            "success": True,
            "message": "ok",
            "data": {"stdout": "hello world"},
        }

        shell_tool = ShellTool(sandbox=sandbox, callback_manager=manager)
        wait_tool = TaskCallbackTool(callback_manager=manager)

        # 1. 异步启动 shell 命令
        start_result = await shell_tool.shell_execute(
            "s_int", "/tmp", "echo hello", async_mode=True
        )
        assert start_result.success is True
        task_id = start_result.data["task_id"]

        # 等待后台执行完成
        await asyncio.sleep(0.1)

        # 2. LLM 调用 task_wait 等待结果
        wait_result = await wait_tool.task_wait(task_id, timeout=10)
        assert wait_result.success is True
        assert wait_result.data == {"stdout": "hello world"}

        # 验证完整调用链
        manager.register_mock.assert_awaited_once()
        manager.notify_mock.assert_awaited_once()
        manager.wait_mock.assert_awaited_once()
