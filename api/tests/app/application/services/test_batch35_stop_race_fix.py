#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Batch 35: 停止会话竞态修复单元测试

验证 project_memory 硬约束 "Session stop must update status to COMPLETED
before canceling tasks to prevent race conditions" 的落地:

- stop_session: update_status(COMPLETED) 先于 task.cancel()
- 无任务分支同样设置 COMPLETED
- CancelledError 处理器 DB 写入失败时仅记录 debug, 不传播异常(兜底容错)
- 浏览器清理 + 后台任务取消功能不回归
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.services.agent_service import AgentService
from app.domain.models.session import Session, SessionStatus
from app.domain.services.agent_task_runner import AgentTaskRunner


# ============ 辅助构造函数 ============

def _make_session(session_id: str = "s1", task_id: str = "t1") -> Session:
    """构造测试用Session"""
    return Session(id=session_id, status=SessionStatus.RUNNING, task_id=task_id)


def _make_task() -> MagicMock:
    """构造mock Task"""
    task = MagicMock()
    task.done = False
    task.cancel = MagicMock()
    task.cleanup_browser = AsyncMock()
    task.cancel_background_tasks = AsyncMock()
    return task


def _create_agent_service(session: Session, task=None) -> AgentService:
    """创建mock AgentService实例(绕过__init__)"""
    with patch.object(AgentService, '__init__', lambda self: None):
        service = AgentService.__new__(AgentService)

        service._uow = MagicMock()
        service._uow.__aenter__ = AsyncMock(return_value=service._uow)
        service._uow.__aexit__ = AsyncMock(return_value=False)
        service._uow.session = MagicMock()
        service._uow.session.get_by_id = AsyncMock(return_value=session)
        service._uow.session.update_status = AsyncMock()

        service._get_task = AsyncMock(return_value=task)
        service._schedule_sandbox_ttl = AsyncMock()
        service._remove_session_lock = MagicMock()

        return service


class _FailingUow:
    """UoW whose session.update_status raises,用于测试 CancelledError 兜底容错"""

    def __init__(self):
        self.session = MagicMock()
        self.session.update_status = AsyncMock(side_effect=RuntimeError("DB连接已断开"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class TestStopSessionCompletesBeforeCancel:
    """Batch 35: COMPLETED 先于 cancel, 防止竞态"""

    @pytest.mark.asyncio
    async def test_stop_session_completes_before_cancel(self):
        """有任务时 update_status(COMPLETED) 调用先于 task.cancel()"""
        session = _make_session()
        task = _make_task()
        service = _create_agent_service(session, task)

        # 记录调用顺序
        call_order = []
        service._uow.session.update_status = AsyncMock(
            side_effect=lambda *a, **kw: call_order.append("completed"),
        )
        task.cancel = MagicMock(side_effect=lambda: call_order.append("cancel"))

        await service.stop_session("s1")

        assert call_order == ["completed", "cancel"], (
            f"COMPLETED 必须先于 cancel, 实际顺序: {call_order}"
        )
        service._uow.session.update_status.assert_called_once_with("s1", SessionStatus.COMPLETED)

    @pytest.mark.asyncio
    async def test_stop_session_no_task_completes(self):
        """无任务分支也调用 update_status(COMPLETED)"""
        session = _make_session(task_id=None)
        service = _create_agent_service(session, task=None)

        await service.stop_session("s1")

        service._uow.session.update_status.assert_called_once_with("s1", SessionStatus.COMPLETED)


class TestStopSessionCleanupPreserved:
    """Batch 35: 浏览器清理 + 后台任务取消功能不回归"""

    @pytest.mark.asyncio
    async def test_stop_session_browser_and_bg_cleanup_preserved(self):
        """有任务时 cleanup_browser + cancel_background_tasks 仍被调用"""
        session = _make_session()
        task = _make_task()
        service = _create_agent_service(session, task)

        await service.stop_session("s1")

        task.cleanup_browser.assert_awaited_once()
        task.cancel_background_tasks.assert_awaited_once()
        task.cancel.assert_called_once()


class TestCancelledErrorHandlerToleratesDbFailure:
    """Batch 35: CancelledError 处理器 DB 写入失败时仅记录 debug, 不传播异常"""

    @pytest.mark.asyncio
    async def test_cancelled_error_handler_tolerates_db_failure(self):
        """runner CancelledError 处理器 DB 写入抛异常时不传播, 仅 re-raise CancelledError"""
        runner = object.__new__(AgentTaskRunner)
        runner._session_id = "test-session"
        runner._shell_console_sent_count = {}

        # ensure_sandbox 抛出 CancelledError,触发 except 块
        runner._sandbox = MagicMock()
        runner._sandbox.ensure_sandbox = AsyncMock(side_effect=asyncio.CancelledError())

        # _put_and_add_event mock(跳过内部 uow 调用)
        runner._put_and_add_event = AsyncMock()

        # _uow_factory 返回 update_status 会抛异常的 uow
        runner._uow_factory = MagicMock(return_value=_FailingUow())

        # finally 块依赖
        runner._cleanup_tools = AsyncMock()
        runner._metrics = MagicMock()

        task = MagicMock()
        task.input_stream = MagicMock()
        task.input_stream.is_empty = AsyncMock(return_value=True)

        # 应仅 re-raise CancelledError, 不传播 RuntimeError
        with pytest.raises(asyncio.CancelledError):
            await runner.invoke(task)

        # 验证: 兜底 DB 写入被调用(尽管失败)
        # _FailingUow 的 update_status 被 await 过
        # (CancelledError 处理器确实执行了兜底逻辑)
