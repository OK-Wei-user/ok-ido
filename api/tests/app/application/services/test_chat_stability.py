#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_chat_stability.py
chat()方法会话稳定性单元测试 — 验证P0-1~P0-6六项防御修复

测试覆盖:
- P0-1: 输出流有限阻塞(OUTPUT_STREAM_BLOCK_MS而非block_ms=0)
- P0-2: latest_event_id None保护(超时无数据时不覆盖)
- P0-3: 死任务检测与重建(task.done=True时重建)
- P0-4: 异常处理器取消任务+完结会话(避免僵尸会话)
- P0-5: RUNNING+task存活跳过消息投递(避免SSE重连重复处理)
- P0-6: attachments None保护(防止None崩溃)
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.services.agent_service import AgentService, OUTPUT_STREAM_BLOCK_MS
from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent
from app.domain.models.session import Session, SessionStatus


def _make_session(
    session_id: str = "s1",
    status: SessionStatus = SessionStatus.COMPLETED,
    task_id: str = None,
) -> Session:
    """构造测试用Session"""
    return Session(id=session_id, status=status, task_id=task_id)


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
    return task


def _done_event_json() -> str:
    """构造DoneEvent的JSON字符串，用于output_stream.get返回以正常终止while循环"""
    return DoneEvent().model_dump_json()


class TestChatSessionStability:
    """chat()方法会话稳定性测试"""

    def _create_service(
        self,
        session: Session,
        task=None,
    ) -> AgentService:
        """创建mock AgentService实例，预设chat()所需的所有依赖

        Args:
            session: chat()获取到的会话
            task: _get_task返回的任务(可为None)
        """
        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)

            # 主UoW: 支持async with上下文 + session/file异步方法
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

            # UoW工厂: 用于_safe_complete_session/_safe_update_unread_count
            # 创建独立UoW实例(完整mock所有异步方法，避免后台Task警告)
            factory_uow = MagicMock()
            factory_uow.__aenter__ = AsyncMock(return_value=factory_uow)
            factory_uow.__aexit__ = AsyncMock(return_value=False)
            factory_uow.session = MagicMock()
            factory_uow.session.update_status = AsyncMock()
            factory_uow.session.update_unread_message_count = AsyncMock()
            service._uow_factory = MagicMock(return_value=factory_uow)

            # 会话锁
            service._session_locks = {}
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

    # ===== P0-1: 输出流有限阻塞 =====

    @pytest.mark.asyncio
    async def test_output_stream_uses_finite_block_ms(self):
        """P0-1: output_stream.get使用OUTPUT_STREAM_BLOCK_MS而非block_ms=0"""
        session = _make_session(status=SessionStatus.COMPLETED)
        # 让while循环执行一次: get返回(None,None) → event_str=None → done=True → break
        task = _make_task(done=False, output_events=[(None, None)])
        task.output_stream.is_empty = AsyncMock(side_effect=[False, True])
        task.done = True  # 在get返回后done变为True(模拟任务完成)

        service = self._create_service(session, task)
        service._create_task = AsyncMock(return_value=task)

        # 不传message → 跳过消息投递，直接进入输出流读取
        await self._collect_events(service.chat("s1", message=None))

        # 验证get调用使用了OUTPUT_STREAM_BLOCK_MS
        assert task.output_stream.get.called
        _, kwargs = task.output_stream.get.call_args
        assert kwargs.get("block_ms") == OUTPUT_STREAM_BLOCK_MS
        assert kwargs.get("block_ms") != 0

    # ===== P0-2: latest_event_id None保护 =====

    @pytest.mark.asyncio
    async def test_latest_event_id_not_overwritten_on_none(self):
        """P0-2: get()返回(None,None)时不覆盖latest_event_id"""
        session = _make_session(status=SessionStatus.COMPLETED)
        # 第一次get返回(None,None) → latest_event_id应保持不变
        # 第二次get返回DoneEvent → 正常处理并终止循环
        task = _make_task(done=False, output_events=[(None, None), ("evt-1", _done_event_json())])
        task.output_stream.is_empty = AsyncMock(return_value=False)

        service = self._create_service(session, task)
        service._create_task = AsyncMock(return_value=task)

        # 传入latest_event_id="123-0"
        await self._collect_events(
            service.chat("s1", message=None, latest_event_id="123-0")
        )

        # 验证: 第一次get使用start_id="123-0"
        first_call = task.output_stream.get.call_args_list[0]
        assert first_call.kwargs.get("start_id") == "123-0"

        # 验证: 第二次get仍使用start_id="123-0"(未被None覆盖)
        second_call = task.output_stream.get.call_args_list[1]
        assert second_call.kwargs.get("start_id") == "123-0"

    # ===== P0-3: 死任务检测与重建 =====

    @pytest.mark.asyncio
    async def test_dead_task_detection_triggers_rebuild(self):
        """P0-3: RUNNING状态+task.done=True时重建任务"""
        session = _make_session(status=SessionStatus.RUNNING, task_id="old-task")
        dead_task = _make_task(done=True)

        service = self._create_service(session, dead_task)
        new_task = _make_task(done=True)
        service._create_task = AsyncMock(return_value=new_task)

        await self._collect_events(
            service.chat("s1", message="hello", attachments=[])
        )

        # 验证: 死任务被取消
        dead_task.cancel.assert_called_once()
        # 验证: 新任务被创建
        service._create_task.assert_called_once()
        # 验证: 消息被投递到新任务
        new_task.input_stream.put.assert_called_once()
        new_task.invoke.assert_called_once()

    # ===== P0-4: 异常处理器取消任务+完结会话 =====

    @pytest.mark.asyncio
    async def test_exception_handler_cancels_task_and_completes_session(self):
        """P0-4: 异常时取消后台任务+独立Task更新会话为COMPLETED"""
        session = _make_session(status=SessionStatus.COMPLETED)
        # COMPLETED状态_get_task返回None(无活跃任务)，避免create_task块内cancel
        # _create_task返回新任务(done=False)，invoke抛异常 → 异常处理器cancel一次
        new_task = _make_task(done=False)
        new_task.invoke = AsyncMock(side_effect=RuntimeError("LLM调用失败"))

        service = self._create_service(session, task=None)
        service._create_task = AsyncMock(return_value=new_task)

        # mock asyncio.create_task以捕获_safe_complete_session调用
        created_coros = []
        original_create_task = asyncio.create_task

        def _capture_create_task(coro):
            created_coros.append(coro)
            return original_create_task(coro)

        with patch('asyncio.create_task', side_effect=_capture_create_task):
            events = await self._collect_events(
                service.chat("s1", message="hello", attachments=[])
            )

        # 验证: 后台任务被取消(仅异常处理器调用一次)
        new_task.cancel.assert_called_once()
        # 验证: 产出了ErrorEvent
        assert any(isinstance(e, ErrorEvent) for e in events)
        # 验证: _safe_complete_session被创建为独立Task
        assert len(created_coros) > 0
        # 等待后台任务完成
        await asyncio.sleep(0.1)
        # 验证: 会话状态被更新为COMPLETED
        factory_uow = service._uow_factory.return_value
        factory_uow.session.update_status.assert_called_once_with("s1", SessionStatus.COMPLETED)

    # ===== P0-5: RUNNING+task存活跳过消息投递 =====

    @pytest.mark.asyncio
    async def test_running_alive_task_skips_message_delivery(self):
        """P0-5: RUNNING+task存活(task.done=False)时跳过消息投递"""
        session = _make_session(status=SessionStatus.RUNNING, task_id="running-task")
        # 任务存活(done=False)，输出流产出DoneEvent后正常终止(避免无限循环)
        alive_task = _make_task(done=False, output_events=[("evt-1", _done_event_json())])

        service = self._create_service(session, alive_task)

        await self._collect_events(
            service.chat("s1", message="hello", attachments=[])
        )

        # 验证: 未创建新任务(任务仍在运行)
        service._create_task.assert_not_called()
        # 验证: 未投递消息
        alive_task.input_stream.put.assert_not_called()
        # 验证: 未调用invoke
        alive_task.invoke.assert_not_called()
        # 验证: 未更新最新消息
        service._uow.session.update_latest_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_waiting_status_delivers_message(self):
        """P0-5补充: WAITING状态(task.done=True)正常投递消息"""
        session = _make_session(status=SessionStatus.WAITING, task_id="old-task")
        dead_task = _make_task(done=True)

        service = self._create_service(session, dead_task)
        new_task = _make_task(done=True)
        service._create_task = AsyncMock(return_value=new_task)

        await self._collect_events(
            service.chat("s1", message="continue", attachments=[])
        )

        # 验证: 创建了新任务(WAITING→重建)
        service._create_task.assert_called_once()
        # 验证: 消息被投递
        new_task.input_stream.put.assert_called_once()
        new_task.invoke.assert_called_once()
        # 验证: 更新了最新消息
        service._uow.session.update_latest_message.assert_called_once()

    # ===== P0-6: attachments None保护 =====

    @pytest.mark.asyncio
    async def test_none_attachments_does_not_crash(self):
        """P0-6: attachments=None时不崩溃"""
        session = _make_session(status=SessionStatus.COMPLETED)
        new_task = _make_task(done=True)

        service = self._create_service(session, task=None)
        service._create_task = AsyncMock(return_value=new_task)

        # attachments=None不应抛异常
        events = await self._collect_events(
            service.chat("s1", message="hello", attachments=None)
        )

        # 验证: 正常执行，消息被投递
        new_task.input_stream.put.assert_called_once()
        new_task.invoke.assert_called_once()
        # 验证: file.get_by_id未被调用(attachments为None→空列表)
        service._uow.file.get_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_attachments_does_not_crash(self):
        """P0-6补充: attachments=[]正常工作"""
        session = _make_session(status=SessionStatus.COMPLETED)
        new_task = _make_task(done=True)

        service = self._create_service(session, task=None)
        service._create_task = AsyncMock(return_value=new_task)

        await self._collect_events(
            service.chat("s1", message="hello", attachments=[])
        )

        new_task.input_stream.put.assert_called_once()
        new_task.invoke.assert_called_once()

    # ===== 综合场景 =====

    @pytest.mark.asyncio
    async def test_completed_status_creates_new_task(self):
        """COMPLETED状态: 创建新任务并投递消息"""
        session = _make_session(status=SessionStatus.COMPLETED)
        # _get_task返回None(COMPLETED状态无活跃任务)
        service = self._create_service(session, task=None)
        new_task = _make_task(done=True)
        service._create_task = AsyncMock(return_value=new_task)

        await self._collect_events(
            service.chat("s1", message="new task", attachments=[])
        )

        service._create_task.assert_called_once()
        new_task.input_stream.put.assert_called_once()
        new_task.invoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_not_found_raises_error(self):
        """会话不存在: 产出ErrorEvent且不崩溃(task未初始化场景)"""
        service = self._create_service(None, task=None)

        events = await self._collect_events(
            service.chat("nonexistent", message="hello")
        )

        # 验证: 产出了ErrorEvent(不因task未定义而NameError)
        assert any(isinstance(e, ErrorEvent) for e in events)

    # ===== P0-7: 空流恢复时task为None的空转循环防御 =====

    @pytest.mark.asyncio
    async def test_running_session_with_none_task_completes_session(self):
        """P0-7: RUNNING状态+task为None(空流恢复)时更新状态为COMPLETED并发送DoneEvent

        根因: 恢复卡住的WAITING会话时手动改状态为RUNNING,但旧task_id对应的
        Redis Stream已过期;或API重启后内存task实例丢失但DB状态未更新。
        原bug: task为None时_consume_output_stream循环不执行,SSE流立即关闭,
        但会话状态仍RUNNING → 前端SSE_STREAM_END后500ms无限重连(高频空转)。
        修复: 检测task为None+RUNNING时更新状态为COMPLETED+发送DoneEvent。
        """
        session = _make_session(status=SessionStatus.RUNNING, task_id="expired-task")
        service = self._create_service(session, task=None)

        events = await self._collect_events(
            service.chat("s1", message=None)
        )

        # 核心断言1: 会话状态被更新为COMPLETED
        service._uow.session.update_status.assert_called_once_with("s1", SessionStatus.COMPLETED)
        # 核心断言2: 产出了DoneEvent通知前端正常结束
        assert any(isinstance(e, DoneEvent) for e in events), \
            "task为None+RUNNING时应发送DoneEvent,否则前端会无限重连"
        # 核心断言3: DoneEvent被持久化到事件表(断线重连时可恢复)
        service._uow.session.add_event.assert_called_once()
        added_event = service._uow.session.add_event.call_args[0][1]
        assert isinstance(added_event, DoneEvent)

    @pytest.mark.asyncio
    async def test_completed_session_with_none_task_returns_silently(self):
        """P0-7补充: COMPLETED状态+task为None(空流恢复)时直接返回,不产出事件

        COMPLETED状态前端不会启动空流(L222的!completed检查),但防御性测试
        确保即使收到请求也不产出多余事件。
        """
        session = _make_session(status=SessionStatus.COMPLETED, task_id=None)
        service = self._create_service(session, task=None)

        events = await self._collect_events(
            service.chat("s1", message=None)
        )

        # 验证: 不产出任何事件(COMPLETED无活跃任务,直接结束)
        assert len(events) == 0
        # 验证: 不更新会话状态(已是COMPLETED)
        service._uow.session.update_status.assert_not_called()
        # 验证: 不添加事件
        service._uow.session.add_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_waiting_session_with_none_task_returns_silently(self):
        """P0-7补充: WAITING状态+task为None(空流恢复)时直接返回,不产出事件

        WAITING状态task通常已done,空流恢复不会收到新事件。
        防御性测试确保不误发DoneEvent导致状态不一致。
        """
        session = _make_session(status=SessionStatus.WAITING, task_id=None)
        service = self._create_service(session, task=None)

        events = await self._collect_events(
            service.chat("s1", message=None)
        )

        # 验证: 不产出任何事件
        assert len(events) == 0
        # 验证: 不更新为COMPLETED(WAITING是有效状态,不应被空流请求改变)
        service._uow.session.update_status.assert_not_called()
        # 验证: 不添加DoneEvent
        service._uow.session.add_event.assert_not_called()