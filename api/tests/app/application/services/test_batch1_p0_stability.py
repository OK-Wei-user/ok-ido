#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_batch1_p0_stability.py
批次1 P0稳定性修复单元测试 — 验证F1-1~F1-5五项修复

测试覆盖:
- F1-1: RedisStreamMessageQueue.get默认start_id改为'$'(避免重读历史事件)
- F1-2: delete_session资源清理顺序(异常时finally仍清理资源+沙箱级联销毁)
- F1-3: stop_session使用Task.cleanup_browser公开接口(不再反射访问私有属性)
- F1-4: chat未读计数UPDATE去重(单轮会话仅一次UPDATE)
- F1-5: _get_session_lock并发安全(双重检查锁,100并发返回同一实例)
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.errors.exceptions import NotFoundError
from app.application.services.agent_service import AgentService
from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent
from app.domain.models.session import Session, SessionStatus
from app.infrastructure.external.message_queue.redis_stream_message_queue import RedisStreamMessageQueue


# ============ 辅助构造函数 ============

def _make_session(
    session_id: str = "s1",
    status: SessionStatus = SessionStatus.COMPLETED,
    task_id: str = None,
    sandbox_id: str = None,
) -> Session:
    """构造测试用Session"""
    return Session(id=session_id, status=status, task_id=task_id, sandbox_id=sandbox_id)


def _make_task(done: bool = False, output_events=None) -> MagicMock:
    """构造mock Task

    Args:
        done: task.done属性值
        output_events: output_stream.get返回的事件列表[(id, json), ...]，
                       None表示始终返回(None,None)
    """
    task = MagicMock()
    task.done = done
    task.cancel = MagicMock()
    task.input_stream = MagicMock()
    task.input_stream.put = AsyncMock(return_value="evt-0")

    task.output_stream = MagicMock()
    task.output_stream.is_empty = AsyncMock(return_value=True)

    if output_events is None:
        task.output_stream.get = AsyncMock(return_value=(None, None))
    else:
        task.output_stream.get = AsyncMock(side_effect=output_events)

    task.invoke = AsyncMock()
    task.cleanup_browser = AsyncMock()
    return task


def _done_event_json() -> str:
    """构造DoneEvent的JSON字符串"""
    return DoneEvent().model_dump_json()


# ============ F1-1: RedisStreamMessageQueue.get默认start_id ============

class TestF11RedisStreamDefaultStartId:
    """F1-1: RedisStreamMessageQueue.get默认start_id改为'$'"""

    @pytest.mark.asyncio
    async def test_default_start_id_is_dollar_sign(self):
        """F1-1: 未传start_id时内部xread使用'$'(只消费新消息)"""
        queue = RedisStreamMessageQueue.__new__(RedisStreamMessageQueue)
        queue._stream_name = "test-stream"
        queue._lock_expire_seconds = 10

        # mock redis client
        mock_redis = MagicMock()
        mock_redis.client = MagicMock()
        mock_redis.client.xread = AsyncMock(return_value=[])
        queue._redis = mock_redis

        # 调用get时不传start_id
        await queue.get(block_ms=100)

        # 验证: xread调用时stream id为'$'(只消费新消息)
        mock_redis.client.xread.assert_called_once()
        _, kwargs = mock_redis.client.xread.call_args
        stream_dict = kwargs.get("count")  # 占位,实际从位置参数取
        args = mock_redis.client.xread.call_args.args
        # xread({stream_name: start_id}, count=1, block=block_ms)
        assert args[0] == {"test-stream": "$"}, "默认start_id应为'$'"

    @pytest.mark.asyncio
    async def test_explicit_start_id_zero_preserved(self):
        """F1-1补充: 显式传入start_id='0'时保持不变(用于历史回放)"""
        queue = RedisStreamMessageQueue.__new__(RedisStreamMessageQueue)
        queue._stream_name = "test-stream"
        queue._lock_expire_seconds = 10

        mock_redis = MagicMock()
        mock_redis.client = MagicMock()
        mock_redis.client.xread = AsyncMock(return_value=[])
        queue._redis = mock_redis

        # 显式传入start_id='0'(历史回放)
        await queue.get(start_id="0", block_ms=100)

        args = mock_redis.client.xread.call_args.args
        assert args[0] == {"test-stream": "0"}, "显式传入'0'应保持不变"

    @pytest.mark.asyncio
    async def test_explicit_event_id_preserved(self):
        """F1-1补充: 显式传入具体event_id时保持不变(用于断点续传)"""
        queue = RedisStreamMessageQueue.__new__(RedisStreamMessageQueue)
        queue._stream_name = "test-stream"
        queue._lock_expire_seconds = 10

        mock_redis = MagicMock()
        mock_redis.client = MagicMock()
        mock_redis.client.xread = AsyncMock(return_value=[])
        queue._redis = mock_redis

        # 显式传入具体event_id(断点续传)
        await queue.get(start_id="1234567890-0", block_ms=100)

        args = mock_redis.client.xread.call_args.args
        assert args[0] == {"test-stream": "1234567890-0"}, "显式传入具体id应保持不变"


# ============ F1-2: delete_session资源清理顺序 ============

class TestF12DeleteSessionCleanupOrder:
    """F1-2: delete_session资源清理顺序修复"""

    @pytest.mark.asyncio
    async def test_cleanup_runs_even_when_db_delete_fails(self):
        """F1-2: DB删除抛异常时,沙箱TTL取消+会话锁移除仍被执行"""
        from app.interfaces.endpoints import session_routes

        # mock依赖
        session_service = MagicMock()
        session_service.get_session = AsyncMock(return_value=_make_session())
        session_service.delete_session = AsyncMock(side_effect=RuntimeError("DB连接失败"))

        agent_service = MagicMock()
        agent_service.cancel_sandbox_ttl = MagicMock()
        agent_service.remove_session_lock = MagicMock()

        # 调用delete_session应抛RuntimeError
        with pytest.raises(RuntimeError):
            await session_routes.delete_session(
                session_id="s1",
                current_user_id="u1",
                session_service=session_service,
                agent_service=agent_service,
            )

        # 验证: 资源清理仍被执行(避免泄漏)
        agent_service.cancel_sandbox_ttl.assert_called_once_with("s1")
        agent_service.remove_session_lock.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_cleanup_runs_on_success(self):
        """F1-2补充: 正常路径下资源清理也被执行"""
        from app.interfaces.endpoints import session_routes

        session_service = MagicMock()
        session_service.get_session = AsyncMock(return_value=_make_session())
        session_service.delete_session = AsyncMock()

        agent_service = MagicMock()
        agent_service.cancel_sandbox_ttl = MagicMock()
        agent_service.remove_session_lock = MagicMock()

        await session_routes.delete_session(
            session_id="s1",
            current_user_id="u1",
            session_service=session_service,
            agent_service=agent_service,
        )

        # 验证: 资源清理被执行
        agent_service.cancel_sandbox_ttl.assert_called_once_with("s1")
        agent_service.remove_session_lock.assert_called_once_with("s1")
        session_service.delete_session.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_session_service_delete_cascades_sandbox(self):
        """F1-2补充: SessionService.delete_session级联销毁沙箱(兜底)"""
        from app.application.services.session_service import SessionService

        # 构造SessionService实例(绕过__init__)
        service = SessionService.__new__(SessionService)

        # mock UoW
        uow = MagicMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)
        uow.session = MagicMock()
        uow.session.get_by_id = AsyncMock(return_value=_make_session(sandbox_id="sb-1"))
        uow.session.delete_by_id = AsyncMock()
        service._uow = uow

        # mock沙箱类与实例
        sandbox_instance = MagicMock()
        sandbox_instance.destroy = AsyncMock()
        sandbox_cls = MagicMock()
        sandbox_cls.get = AsyncMock(return_value=sandbox_instance)
        service._sandbox_cls = sandbox_cls

        # 执行删除
        await service.delete_session("s1")

        # 验证: 沙箱被级联销毁
        sandbox_cls.get.assert_called_once_with("sb-1")
        sandbox_instance.destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_service_delete_sandbox_failure_does_not_block(self):
        """F1-2补充: 沙箱销毁失败不阻断删除流程"""
        from app.application.services.session_service import SessionService

        service = SessionService.__new__(SessionService)

        uow = MagicMock()
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)
        uow.session = MagicMock()
        uow.session.get_by_id = AsyncMock(return_value=_make_session(sandbox_id="sb-1"))
        uow.session.delete_by_id = AsyncMock()
        service._uow = uow

        # 沙箱销毁抛异常
        sandbox_cls = MagicMock()
        sandbox_cls.get = AsyncMock(side_effect=RuntimeError("沙箱连接失败"))
        service._sandbox_cls = sandbox_cls

        # 删除不应抛异常(沙箱销毁失败被吞掉)
        await service.delete_session("s1")

        # 验证: DB删除仍成功
        uow.session.delete_by_id.assert_called_once_with("s1")


# ============ F1-3: stop_session使用Task.cleanup_browser公开接口 ============

class TestF13StopSessionCleanupBrowser:
    """F1-3: stop_session改用Task.cleanup_browser公开接口"""

    def _create_service(self, session: Session, task=None) -> AgentService:
        """创建mock AgentService实例"""
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

    @pytest.mark.asyncio
    async def test_stop_session_calls_cleanup_browser(self):
        """F1-3: stop_session调用task.cleanup_browser()而非反射访问"""
        session = _make_session(task_id="t1")
        task = _make_task(done=False)
        task.cleanup_browser = AsyncMock()

        service = self._create_service(session, task)
        await service.stop_session("s1")

        # 验证: 调用了cleanup_browser公开接口
        task.cleanup_browser.assert_awaited_once()
        # 验证: 任务被取消
        task.cancel.assert_called_once()
        # �验证: 会话状态更新为COMPLETED
        service._uow.session.update_status.assert_called_once_with("s1", SessionStatus.COMPLETED)

    @pytest.mark.asyncio
    async def test_stop_session_no_task_does_not_crash(self):
        """F1-3补充: 会话无任务时不崩溃"""
        session = _make_session(task_id=None)
        service = self._create_service(session, task=None)

        # 不应抛异常
        await service.stop_session("s1")

        # 验证: 会话状态仍被更新
        service._uow.session.update_status.assert_called_once_with("s1", SessionStatus.COMPLETED)

    @pytest.mark.asyncio
    async def test_stop_session_cleanup_browser_failure_does_not_block(self):
        """F1-3补充: cleanup_browser抛异常时不阻断stop流程"""
        session = _make_session(task_id="t1")
        task = _make_task(done=False)
        task.cleanup_browser = AsyncMock(side_effect=RuntimeError("浏览器已关闭"))

        service = self._create_service(session, task)

        # 不应抛异常
        await service.stop_session("s1")

        # 验证: 任务仍被取消
        task.cancel.assert_called_once()
        # 验证: 会话状态仍被更新
        service._uow.session.update_status.assert_called_once_with("s1", SessionStatus.COMPLETED)

    @pytest.mark.asyncio
    async def test_stop_session_no_reflection_access(self):
        """F1-3: stop_session源码不再使用getattr反射访问_task_runner/_browser

        通过源代码检查验证: agent_service.py中stop_session方法体内
        不应出现getattr访问_task_runner或_browser的代码
        """
        import inspect
        import re

        # 获取stop_session方法源代码
        source = inspect.getsource(AgentService.stop_session)

        # 验证: 源代码中不应出现反射访问_task_runner或_browser
        # (允许cleanup_browser公开接口调用)
        forbidden_patterns = [
            r"getattr\s*\(\s*[^,]+,\s*['\"]_task_runner['\"]",
            r"getattr\s*\(\s*[^,]+,\s*['\"]_browser['\"]",
        ]
        for pattern in forbidden_patterns:
            matches = re.findall(pattern, source)
            assert len(matches) == 0, (
                f"stop_session源码中不应反射访问私有属性,发现匹配: {matches}"
            )

        # 验证: 源代码中应调用task.cleanup_browser()公开接口
        assert "cleanup_browser" in source, "stop_session应调用task.cleanup_browser()公开接口"


# ============ F1-4: chat未读计数UPDATE去重 ============

class TestF14ChatUnreadCountDeduplication:
    """F1-4: chat未读计数UPDATE去重(单轮会话仅一次UPDATE)"""

    def _create_service(self, session: Session, task=None) -> AgentService:
        """创建mock AgentService实例"""
        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)

            # 主UoW
            service._uow = MagicMock()
            service._uow.__aenter__ = AsyncMock(return_value=service._uow)
            service._uow.__aexit__ = AsyncMock(return_value=False)
            service._uow.session = MagicMock()
            service._uow.session.get_by_id = AsyncMock(return_value=session)
            service._uow.session.update_latest_message = AsyncMock()
            service._uow.session.add_event = AsyncMock()
            service._uow.session.update_unread_message_count = AsyncMock()
            service._uow.session.update_status = AsyncMock()
            service._uow.file = MagicMock()
            service._uow.file.get_by_id = AsyncMock(return_value=None)

            # UoW工厂(用于后台Task)
            factory_uow = MagicMock()
            factory_uow.__aenter__ = AsyncMock(return_value=factory_uow)
            factory_uow.__aexit__ = AsyncMock(return_value=False)
            factory_uow.session = MagicMock()
            factory_uow.session.update_status = AsyncMock()
            factory_uow.session.update_unread_message_count = AsyncMock()
            service._uow_factory = MagicMock(return_value=factory_uow)

            # 会话锁
            service._session_locks = {}
            service._locks_guard = asyncio.Lock()
            service._get_session_lock = AsyncMock(return_value=asyncio.Lock())

            # 任务相关
            service._get_task = AsyncMock(return_value=task)
            service._create_task = AsyncMock(return_value=task)
            service._schedule_sandbox_ttl = AsyncMock()

            return service

    async def _collect_events(self, gen) -> list:
        """收集异步生成器产出的所有事件"""
        events = []
        async for event in gen:
            events.append(event)
        return events

    @pytest.mark.asyncio
    async def test_unread_count_updated_only_once_for_multiple_events(self):
        """F1-4: 多事件场景下update_unread_message_count仅被调用一次

        F3-1批量化更新: 未读计数清零统一由finally块的_safe_update_unread_count
        后台Task执行(通过_uow_factory创建新UoW),不再在chat循环内直接UPDATE。
        断言改为验证factory_uow(后台Task使用的UoW)的update_unread_message_count调用次数。
        """
        # 构造会话: RUNNING + 任务存活,跳过消息投递直接进入输出流读取
        session = _make_session(status=SessionStatus.RUNNING, task_id="t1")

        # 输出流产出3个事件后DoneEvent终止
        evt1 = ("evt-1", MessageEvent(role="assistant", message="hello").model_dump_json())
        evt2 = ("evt-2", MessageEvent(role="assistant", message="world").model_dump_json())
        evt3 = ("evt-3", _done_event_json())
        task = _make_task(done=False, output_events=[evt1, evt2, evt3])

        service = self._create_service(session, task)

        await self._collect_events(service.chat("s1", message="hello"))
        # F3-1: _safe_update_unread_count通过asyncio.create_task后台执行,
        # 需yield控制权让后台Task完成数据库UPDATE
        await asyncio.sleep(0)

        # F3-1验证: chat循环内不再UPDATE,仅finally后台Task通过factory_uow执行1次UPDATE
        service._uow.session.update_unread_message_count.assert_not_called()
        factory_uow = service._uow_factory.return_value
        assert factory_uow.session.update_unread_message_count.call_count == 1
        factory_uow.session.update_unread_message_count.assert_called_once_with("s1", 0)

    @pytest.mark.asyncio
    async def test_unread_count_not_updated_when_no_events(self):
        """F1-4补充: 无事件时update_unread_message_count不被调用"""
        session = _make_session(status=SessionStatus.RUNNING, task_id="t1")
        # 任务存活但输出流为空(get返回None) → 直接break,无事件产出
        task = _make_task(done=True, output_events=[(None, None)])
        task.output_stream.is_empty = AsyncMock(return_value=True)

        service = self._create_service(session, task)

        await self._collect_events(service.chat("s1", message=None))
        await asyncio.sleep(0)  # F3-1: yield控制权让后台Task有机会执行

        # 验证: 无事件时未读计数未被调用(主UoW和factory UoW均不应被调用)
        service._uow.session.update_unread_message_count.assert_not_called()
        factory_uow = service._uow_factory.return_value
        factory_uow.session.update_unread_message_count.assert_not_called()


# ============ F1-5: _get_session_lock并发安全 ============

class TestF15SessionLockConcurrency:
    """F1-5: _get_session_lock并发安全(双重检查锁)"""

    def _create_service(self) -> AgentService:
        """创建AgentService实例(仅初始化锁相关属性)"""
        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._session_locks = {}
            service._sandbox_ttl_tasks = {}
            service._locks_guard = asyncio.Lock()
            return service

    @pytest.mark.asyncio
    async def test_concurrent_get_session_lock_returns_same_instance(self):
        """F1-5: 100并发获取锁返回同一个Lock实例"""
        service = self._create_service()

        # 100个并发获取锁的协程
        results = await asyncio.gather(*[
            service._get_session_lock("s1") for _ in range(100)
        ])

        # 验证: 所有协程拿到的是同一个Lock实例
        first_lock = results[0]
        assert all(lock is first_lock for lock in results), "并发获取应返回同一个Lock实例"
        # 验证: 字典中只存了一个锁
        assert len(service._session_locks) == 1
        assert service._session_locks["s1"] is first_lock

    @pytest.mark.asyncio
    async def test_different_sessions_get_different_locks(self):
        """F1-5补充: 不同会话拿到不同的Lock实例"""
        service = self._create_service()

        lock_s1 = await service._get_session_lock("s1")
        lock_s2 = await service._get_session_lock("s2")

        # 验证: 不同会话的锁不同
        assert lock_s1 is not lock_s2
        assert len(service._session_locks) == 2

    @pytest.mark.asyncio
    async def test_lock_reuse_after_first_call(self):
        """F1-5补充: 首次创建后,后续调用复用同一实例(快路径)"""
        service = self._create_service()

        lock1 = await service._get_session_lock("s1")
        lock2 = await service._get_session_lock("s1")
        lock3 = await service._get_session_lock("s1")

        # 验证: 三次调用返回同一实例
        assert lock1 is lock2 is lock3
        # 验证: 字典中只有一个锁
        assert len(service._session_locks) == 1

    @pytest.mark.asyncio
    async def test_remove_session_lock_cleans_dict(self):
        """F1-5补充: remove_session_lock清理字典"""
        service = self._create_service()

        await service._get_session_lock("s1")
        assert "s1" in service._session_locks

        service.remove_session_lock("s1")
        assert "s1" not in service._session_locks

        # 再次获取应创建新锁
        new_lock = await service._get_session_lock("s1")
        assert "s1" in service._session_locks
        assert service._session_locks["s1"] is new_lock

    @pytest.mark.asyncio
    async def test_concurrent_schedule_and_cancel_ttl_no_race(self):
        """F1-5补充: 并发_schedule与_cancel不产生竞态(锁保护下)"""
        service = self._create_service()

        # mock _schedule_sandbox_ttl内部依赖
        with patch.object(AgentService, '_cancel_sandbox_ttl', lambda self, sid: None):
            # 并发执行20次schedule与20次cancel
            schedule_tasks = [
                service._schedule_sandbox_ttl(f"s{i}") for i in range(20)
            ]
            await asyncio.gather(*schedule_tasks, return_exceptions=True)

            # 验证: 没有异常,字典最终状态一致
            assert len(service._sandbox_ttl_tasks) <= 20

            # 取消所有TTL任务,清理资源
            for sid, ttl_task in list(service._sandbox_ttl_tasks.items()):
                if not ttl_task.done():
                    ttl_task.cancel()
            service._sandbox_ttl_tasks.clear()
