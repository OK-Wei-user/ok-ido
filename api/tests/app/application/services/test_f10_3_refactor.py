#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_f10_3_refactor.py
F10-3 AgentService.chat 重构单元测试 - 验证 _consume_output_stream 行为等价性

测试覆盖:
- 正常消费事件: task未完成 → 读取事件 → yield
- task完成+输出流空: 退出循环
- task完成+输出流有数据: 继续 drain(竞态修复)
- 结束事件(DoneEvent/ErrorEvent/WaitEvent): 注册沙箱TTL → 退出
- 超时无数据(event_str=None)+task未完成: continue
- 超时无数据(event_str=None)+task完成: break

循环退出条件(必须命中其一,否则会无限循环):
1. task.done=True 且 is_empty()=True(正常退出)
2. 遇到 DoneEvent/ErrorEvent/WaitEvent(break 退出)
3. event_str=None 且 task.done=True(break 退出)
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.services.agent_service import AgentService
from app.domain.models.event import (
    BaseEvent, DoneEvent, ErrorEvent, WaitEvent,
    MessageEvent,
)


# ========== 测试辅助函数 ==========

def _build_service() -> AgentService:
    """构建AgentService实例(mock __init__)"""
    service = object.__new__(AgentService)
    service._schedule_sandbox_ttl = AsyncMock()
    return service


def _make_event_str(event: BaseEvent) -> str:
    """将事件序列化为JSON字符串(模拟output_stream中的格式)"""
    return event.model_dump_json()


def _make_mock_task(events: list, done: bool = False) -> MagicMock:
    """构建mock Task实例

    get()按顺序返回事件,耗尽后持续返回(None,None)表示超时无数据,
    避免side_effect列表耗尽后抛StopIteration(在async generator中
    会转为非法的StopAsyncIteration)。

    is_empty()默认持续返回False(表示有数据),测试用例可按需覆盖
    side_effect以模拟"消费完事件后流变空"的语义。

    Args:
        events: 事件列表(按顺序返回)
        done: task.done 属性值
    """
    task = MagicMock()
    task.done = done
    task.output_stream = MagicMock()
    # 默认有数据,测试用例按需覆盖
    task.output_stream.is_empty = AsyncMock(return_value=False)

    # 闭包: 按顺序返回事件,耗尽后持续返回 (None, None)
    state = {"idx": 0}
    event_strs = [_make_event_str(e) for e in events]

    async def _get_effect(*args, **kwargs):
        i = state["idx"]
        state["idx"] += 1
        if i < len(event_strs):
            return (f"evt_{i}", event_strs[i])
        return (None, None)

    task.output_stream.get = AsyncMock(side_effect=_get_effect)
    return task


# ========== _consume_output_stream 测试 ==========

class TestConsumeOutputStream:
    """_consume_output_stream 方法测试"""

    @pytest.mark.asyncio
    async def test_consume_normal_event(self):
        """正常消费事件: task完成+输出流有数据 → 读取MessageEvent → yield → drain完成退出"""
        service = _build_service()
        msg_event = MessageEvent(role="assistant", message="你好")
        task = _make_mock_task([msg_event], done=True)
        # 第一次is_empty=False(有数据),第二次is_empty=True(drain完成)
        task.output_stream.is_empty = AsyncMock(side_effect=[False, True])

        events = []
        async for event in service._consume_output_stream("s1", task, None):
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], MessageEvent)
        assert events[0].id == "evt_0"
        # 非结束事件,不应触发沙箱TTL
        service._schedule_sandbox_ttl.assert_not_called()

    @pytest.mark.asyncio
    async def test_consume_message_event(self):
        """消费MessageEvent: 正常转发(通过DoneEvent触发退出)"""
        service = _build_service()
        msg_event = MessageEvent(role="assistant", message="你好", is_final=True)
        done_event = DoneEvent()
        task = _make_mock_task([msg_event, done_event], done=False)

        events = []
        async for event in service._consume_output_stream("s1", task, None):
            events.append(event)

        assert len(events) == 2
        assert isinstance(events[0], MessageEvent)
        assert isinstance(events[1], DoneEvent)
        # DoneEvent触发沙箱TTL
        service._schedule_sandbox_ttl.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_done_event_triggers_sandbox_ttl(self):
        """DoneEvent: yield事件 → 注册沙箱TTL → break退出"""
        service = _build_service()
        done_event = DoneEvent()
        # done=True + is_empty=False → 进入循环(drain模式)
        task = _make_mock_task([done_event], done=True)

        events = []
        async for event in service._consume_output_stream("s1", task, None):
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], DoneEvent)
        service._schedule_sandbox_ttl.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_error_event_triggers_sandbox_ttl(self):
        """ErrorEvent: yield事件 → 注册沙箱TTL → break退出"""
        service = _build_service()
        error_event = ErrorEvent(error="测试错误")
        task = _make_mock_task([error_event], done=True)

        events = []
        async for event in service._consume_output_stream("s1", task, None):
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], ErrorEvent)
        service._schedule_sandbox_ttl.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_wait_event_triggers_sandbox_ttl(self):
        """WaitEvent: yield事件 → 注册沙箱TTL → break退出"""
        service = _build_service()
        wait_event = WaitEvent()
        # done=False + is_empty=False → 进入循环
        task = _make_mock_task([wait_event], done=False)

        events = []
        async for event in service._consume_output_stream("s1", task, None):
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], WaitEvent)
        service._schedule_sandbox_ttl.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_task_done_and_stream_empty(self):
        """task完成 + 输出流空: while条件为False → 直接退出(无事件产出)"""
        service = _build_service()
        # task.done=True, is_empty=True → while条件 False or False = False → 不进入
        task = MagicMock()
        task.done = True
        task.output_stream = MagicMock()
        task.output_stream.is_empty = AsyncMock(return_value=True)
        task.output_stream.get = AsyncMock(return_value=(None, None))

        events = []
        async for event in service._consume_output_stream("s1", task, None):
            events.append(event)

        assert len(events) == 0
        # 不应调用沙箱TTL(无结束事件)
        service._schedule_sandbox_ttl.assert_not_called()

    @pytest.mark.asyncio
    async def test_task_done_but_stream_has_data_drain(self):
        """task完成 + 输出流有数据: 继续 drain(竞态修复)

        场景: task.done=True但输出流还有未读事件(MessageEvent+DoneEvent),
        循环应继续读取直到遇到DoneEvent触发break
        """
        service = _build_service()
        msg_event = MessageEvent(role="assistant", message="最终答案", is_final=True)
        done_event = DoneEvent()
        # done=True + is_empty=False → 进入循环(drain模式)
        task = _make_mock_task([msg_event, done_event], done=True)

        events = []
        async for event in service._consume_output_stream("s1", task, None):
            events.append(event)

        # 应消费2个事件: MessageEvent + DoneEvent
        assert len(events) == 2
        assert isinstance(events[0], MessageEvent)
        assert isinstance(events[1], DoneEvent)
        # DoneEvent 触发沙箱TTL
        service._schedule_sandbox_ttl.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_timeout_no_data_task_not_done(self):
        """超时无数据(event_str=None) + task未完成: continue → 继续读取

        验证: 第一次get返回(None,None)且task.done=False时,
        应continue而非break,后续事件能正常消费
        """
        service = _build_service()
        # 先超时(None,None),然后正常事件,最后DoneEvent触发退出
        # _make_mock_task的_get_effect会先返回events列表中的事件
        # 这里利用特殊构造: events列表第一项为None表示超时
        task = MagicMock()
        task.done = False
        task.output_stream = MagicMock()
        task.output_stream.is_empty = AsyncMock(return_value=False)

        # 顺序: 超时(None,None) → 正常事件 → DoneEvent触发break
        msg_event = MessageEvent(role="assistant", message="延迟消息")
        done_event = DoneEvent()
        call_seq = [
            (None, None),  # 第一次超时
            ("id1", _make_event_str(msg_event)),
            ("id2", _make_event_str(done_event)),
        ]
        state = {"idx": 0}

        async def _get_effect(*args, **kwargs):
            i = state["idx"]
            state["idx"] += 1
            if i < len(call_seq):
                return call_seq[i]
            return (None, None)

        task.output_stream.get = AsyncMock(side_effect=_get_effect)

        events = []
        async for event in service._consume_output_stream("s1", task, None):
            events.append(event)

        # 超时被continue跳过,应消费2个事件: MessageEvent + DoneEvent
        assert len(events) == 2
        assert isinstance(events[0], MessageEvent)
        assert isinstance(events[1], DoneEvent)
        # DoneEvent触发沙箱TTL
        service._schedule_sandbox_ttl.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_timeout_no_data_task_done_breaks(self):
        """超时无数据(event_str=None) + task完成: break 退出"""
        service = _build_service()
        task = MagicMock()
        task.done = True
        task.output_stream = MagicMock()
        task.output_stream.is_empty = AsyncMock(return_value=False)
        task.output_stream.get = AsyncMock(return_value=(None, None))

        events = []
        async for event in service._consume_output_stream("s1", task, None):
            events.append(event)

        # 无事件产出,直接break
        assert len(events) == 0
        # 不应调用沙箱TTL(无结束事件)
        service._schedule_sandbox_ttl.assert_not_called()

    @pytest.mark.asyncio
    async def test_latest_event_id_not_overwritten_on_timeout(self):
        """超时(event_id=None)时不覆盖latest_event_id(防御性逻辑验证)

        场景: 第一次get返回(evt_id_1, event_str)→latest_event_id=evt_id_1,
        第二次get返回(None,None)超时→latest_event_id应保持evt_id_1
        """
        service = _build_service()
        msg_event = MessageEvent(role="assistant", message="事件1")
        done_event = DoneEvent()
        task = MagicMock()
        task.done = False
        task.output_stream = MagicMock()
        task.output_stream.is_empty = AsyncMock(return_value=False)

        call_seq = [
            ("evt_id_1", _make_event_str(msg_event)),
            (None, None),  # 超时,event_id=None
            ("evt_id_2", _make_event_str(done_event)),
        ]
        state = {"idx": 0}

        async def _get_effect(*args, **kwargs):
            i = state["idx"]
            state["idx"] += 1
            if i < len(call_seq):
                return call_seq[i]
            return (None, None)

        task.output_stream.get = AsyncMock(side_effect=_get_effect)

        events = []
        async for event in service._consume_output_stream("s1", task, "initial_id"):
            events.append(event)

        # 应消费2个事件(超时被continue跳过)
        assert len(events) == 2
        # 第一个事件的id应为evt_id_1
        assert events[0].id == "evt_id_1"
        # 第二个事件的id应为evt_id_2(超时后继续读取,latest_event_id未被None覆盖)
        assert events[1].id == "evt_id_2"
