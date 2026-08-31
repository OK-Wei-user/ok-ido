#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_sse_resilience.py
SSE断连恢复与增量推送单元测试
- replay_missed_events: 断连事件补发逻辑
- stream_sessions签名: 会话列表变更检测
- stop_session浏览器清理: 停止会话时浏览器状态重置
"""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.services.agent_service import AgentService
from app.domain.models.event import MessageEvent, DoneEvent, ErrorEvent
from app.domain.models.session import Session, SessionStatus


def _make_event(event_id: str, content: str = "test") -> MessageEvent:
    """构造带指定ID的MessageEvent"""
    event = MessageEvent(role="assistant", message=content)
    event.id = event_id
    return event


def _make_get_events_after_mock(events):
    """构建模拟repository.get_events_after的AsyncMock(F3-3流式读取优化)

    基于预设events列表实现切片逻辑,与DBSessionRepository.get_events_after行为一致。
    events=None模拟会话不存在,返回None;空列表返回([], False)。
    """
    async def _mock(session_id, last_event_id, limit, fallback_limit):
        if events is None:
            return None  # 会话不存在
        if not events:
            return ([], False)
        found_index = -1
        for i, ev in enumerate(events):
            if ev.id == last_event_id:
                found_index = i
                break
        if found_index >= 0:
            return (events[found_index + 1: found_index + 1 + limit], True)
        return (events[-fallback_limit:] if fallback_limit > 0 else [], False)
    return _mock


@asynccontextmanager
async def _mock_uow_session(session: Session):
    """模拟UoW上下文,返回预设的session对象"""
    yield MagicMock(session=MagicMock(
        get_by_id=AsyncMock(return_value=session),
    ))


class TestReplayMissedEvents:
    """replay_missed_events 断连事件补发测试"""

    @pytest.mark.asyncio
    async def test_replay_after_last_event_id(self):
        """last_event_id在事件列表中: 补发其后所有事件"""
        events = [_make_event(f"100-{i}") for i in range(5)]
        service = self._create_service(events)

        replayed = []
        async for event in service.replay_missed_events("s1", "100-2"):
            replayed.append(event)

        # last_event_id="100-2" 之后应有 100-3, 100-4 共2条
        assert len(replayed) == 2
        assert replayed[0].id == "100-3"
        assert replayed[1].id == "100-4"

    @pytest.mark.asyncio
    async def test_replay_last_event_id_not_found_fallback(self):
        """last_event_id不在列表中: 回退补发最近10条"""
        events = [_make_event(f"200-{i}") for i in range(15)]
        service = self._create_service(events)

        replayed = []
        async for event in service.replay_missed_events("s1", "nonexistent-id"):
            replayed.append(event)

        # 回退补发最近10条: 200-5 ~ 200-14
        assert len(replayed) == 10
        assert replayed[0].id == "200-5"
        assert replayed[-1].id == "200-14"

    @pytest.mark.asyncio
    async def test_replay_empty_events(self):
        """会话无事件: 不补发任何事件"""
        service = self._create_service([])

        replayed = []
        async for event in service.replay_missed_events("s1", "any-id"):
            replayed.append(event)

        assert len(replayed) == 0

    @pytest.mark.asyncio
    async def test_replay_session_not_found(self):
        """会话不存在: 不抛异常,不补发"""
        service = self._create_service(None)

        replayed = []
        async for event in service.replay_missed_events("s1", "any-id"):
            replayed.append(event)

        assert len(replayed) == 0

    @pytest.mark.asyncio
    async def test_replay_respects_max_limit(self):
        """补发数量受_MAX_REPLAY_COUNT上限约束"""
        from app.application.services.agent_service import _MAX_REPLAY_COUNT
        events = [_make_event(f"300-{i}") for i in range(_MAX_REPLAY_COUNT + 10)]
        service = self._create_service(events)

        replayed = []
        async for event in service.replay_missed_events("s1", "300-0"):
            replayed.append(event)

        # 应补发 _MAX_REPLAY_COUNT 条,而非全部
        assert len(replayed) == _MAX_REPLAY_COUNT

    @pytest.mark.asyncio
    async def test_replay_last_event_id_at_tail(self):
        """last_event_id是最后一条: 不补发任何事件"""
        events = [_make_event(f"400-{i}") for i in range(3)]
        service = self._create_service(events)

        replayed = []
        async for event in service.replay_missed_events("s1", "400-2"):
            replayed.append(event)

        assert len(replayed) == 0

    @pytest.mark.asyncio
    async def test_replay_fallback_with_fewer_events(self):
        """last_event_id未找到且事件数<10: 补发全部"""
        events = [_make_event(f"500-{i}") for i in range(3)]
        service = self._create_service(events)

        replayed = []
        async for event in service.replay_missed_events("s1", "nonexistent"):
            replayed.append(event)

        assert len(replayed) == 3

    def _create_service(self, events) -> AgentService:
        """创建mock的AgentService实例,预设uow.session.get_events_after返回切片结果

        F3-3流式读取优化: replay_missed_events改用get_events_after,
        不再依赖get_by_id加载完整Session。
        """
        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._uow = MagicMock()
            service._uow.__aenter__ = AsyncMock(return_value=service._uow)
            service._uow.__aexit__ = AsyncMock(return_value=False)
            service._uow.session = MagicMock()
            # get_events_after模拟DBSessionRepository切片逻辑
            service._uow.session.get_events_after = _make_get_events_after_mock(events)
            return service


class TestSessionListSignature:
    """stream_sessions 签名变更检测测试"""

    def test_signature_changes_on_new_message(self):
        """新消息导致latest_message_at变化,签名应不同"""
        sig1 = self._compute_signature([
            Session(id="s1", latest_message_at=None, status=SessionStatus.COMPLETED, unread_message_count=0),
        ])
        sig2 = self._compute_signature([
            Session(id="s1", latest_message_at="2026-07-11T10:00:00", status=SessionStatus.COMPLETED, unread_message_count=1),
        ])
        assert sig1 != sig2

    def test_signature_changes_on_status(self):
        """会话状态变化,签名应不同"""
        sig1 = self._compute_signature([
            Session(id="s1", latest_message_at="2026-07-11T10:00:00", status=SessionStatus.RUNNING, unread_message_count=0),
        ])
        sig2 = self._compute_signature([
            Session(id="s1", latest_message_at="2026-07-11T10:00:00", status=SessionStatus.COMPLETED, unread_message_count=0),
        ])
        assert sig1 != sig2

    def test_signature_changes_on_unread_count(self):
        """未读数变化,签名应不同"""
        sig1 = self._compute_signature([
            Session(id="s1", latest_message_at="2026-07-11T10:00:00", status=SessionStatus.RUNNING, unread_message_count=0),
        ])
        sig2 = self._compute_signature([
            Session(id="s1", latest_message_at="2026-07-11T10:00:00", status=SessionStatus.RUNNING, unread_message_count=3),
        ])
        assert sig1 != sig2

    def test_signature_changes_on_title(self):
        """标题变化,签名应不同"""
        sig1 = self._compute_signature([
            Session(id="s1", title="新对话", latest_message_at="2026-07-11T10:00:00", status=SessionStatus.RUNNING, unread_message_count=0),
        ])
        sig2 = self._compute_signature([
            Session(id="s1", title="AI技术趋势总结", latest_message_at="2026-07-11T10:00:00", status=SessionStatus.RUNNING, unread_message_count=0),
        ])
        assert sig1 != sig2

    def test_signature_same_when_no_change(self):
        """无变更时签名应相同"""
        from datetime import datetime
        ts = datetime(2026, 7, 11, 10, 0, 0)
        sessions = [
            Session(id="s1", latest_message_at=ts, status=SessionStatus.COMPLETED, unread_message_count=0),
            Session(id="s2", latest_message_at=ts, status=SessionStatus.RUNNING, unread_message_count=1),
        ]
        sig1 = self._compute_signature(sessions)
        sig2 = self._compute_signature(sessions)
        assert sig1 == sig2

    def test_signature_changes_on_session_added(self):
        """新增会话,签名应不同"""
        ts = "2026-07-11T10:00:00"
        sig1 = self._compute_signature([
            Session(id="s1", latest_message_at=ts, status=SessionStatus.COMPLETED, unread_message_count=0),
        ])
        sig2 = self._compute_signature([
            Session(id="s1", latest_message_at=ts, status=SessionStatus.COMPLETED, unread_message_count=0),
            Session(id="s2", latest_message_at=ts, status=SessionStatus.PENDING, unread_message_count=0),
        ])
        assert sig1 != sig2

    def test_signature_changes_on_session_removed(self):
        """删除会话,签名应不同"""
        ts = "2026-07-11T10:00:00"
        sig1 = self._compute_signature([
            Session(id="s1", latest_message_at=ts, status=SessionStatus.COMPLETED, unread_message_count=0),
            Session(id="s2", latest_message_at=ts, status=SessionStatus.RUNNING, unread_message_count=0),
        ])
        sig2 = self._compute_signature([
            Session(id="s1", latest_message_at=ts, status=SessionStatus.COMPLETED, unread_message_count=0),
        ])
        assert sig1 != sig2

    @staticmethod
    def _compute_signature(sessions) -> str:
        """模拟stream_sessions中的签名计算逻辑"""
        return "|".join(
            f"{s.id}:{s.title}:{s.latest_message_at.isoformat() if s.latest_message_at else ''}:{s.status}:{s.unread_message_count}"
            for s in sessions
        )


class TestStopSessionBrowserCleanup:
    """stop_session 浏览器清理测试

    架构变更(F1-3): stop_session改用task.cleanup_browser()公开接口,
    不再反射访问_task_runner._browser私有属性。测试同步更新。
    """

    @pytest.mark.asyncio
    async def test_cleanup_browser_called_on_stop(self):
        """停止会话时调用task.cleanup_browser()公开接口"""
        service, mock_task = self._create_service_with_task()
        await service.stop_session("s1")
        mock_task.cleanup_browser.assert_awaited_once()
        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_browser_failure_does_not_block(self):
        """cleanup_browser失败不阻塞主流程,仍执行task.cancel()"""
        service, mock_task = self._create_service_with_task()
        mock_task.cleanup_browser = AsyncMock(side_effect=Exception("browser error"))

        await service.stop_session("s1")
        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_browser_timeout_does_not_block(self):
        """cleanup_browser超时不阻塞主流程"""
        service, mock_task = self._create_service_with_task()

        async def _slow_cleanup():
            await asyncio.sleep(10)
        mock_task.cleanup_browser = _slow_cleanup

        await service.stop_session("s1")
        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_without_task(self):
        """无运行中任务时正常停止,不报错"""
        session = Session(id="s1", status=SessionStatus.RUNNING)
        service = self._create_service_with_session(session, task=None)

        # 不应抛出异常
        await service.stop_session("s1")

    def _create_service_with_task(self):
        """创建带mock task(含cleanup_browser接口)的AgentService"""
        session = Session(id="s1", status=SessionStatus.RUNNING, task_id="t1")
        mock_task = MagicMock()
        mock_task.cancel = MagicMock()
        mock_task.cleanup_browser = AsyncMock()
        return self._create_service_with_session(session, mock_task), mock_task

    def _create_service_with_session(self, session: Session, task=None) -> AgentService:
        """创建mock AgentService,预设session和task"""
        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            # UoW需同时支持 async with 上下文和 .session 异步方法调用
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
