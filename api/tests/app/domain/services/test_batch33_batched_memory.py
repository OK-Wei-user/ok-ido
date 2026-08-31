#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Batch 33: 记忆持久化批量化单元测试

验证脏标记机制(PrivateAttr)的正确性,确保:
- _dirty 初始为 False,变更方法置脏,mark_clean 复位
- _dirty 不参与序列化(不影响 DB JSONB 存储)
- BaseAgent._flush_memory 在 dirty 时刷盘+复位,clean 时跳过
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.models.memory import Memory
from app.domain.services.agents.base import BaseAgent


class _FakeUow:
    """模拟 UoW async context manager, 记录 save_memory 调用"""

    def __init__(self):
        self.session = MagicMock()
        self.session.save_memory = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _build_flush_agent(memory: Memory):
    """构建 BaseAgent 实例(绕过__init__),注入真实 Memory 与 mock uow_factory"""
    agent = object.__new__(BaseAgent)
    agent.name = "test_agent"
    agent._session_id = "test_session"
    agent._memory = memory
    fake_uow = _FakeUow()
    agent._uow_factory = MagicMock(return_value=fake_uow)
    return agent, fake_uow


class TestDirtyFlagMechanism:
    """脏标记机制测试"""

    def test_dirty_flag_initial_and_mark_clean(self):
        """初始 _dirty=False, mark_clean 可复位, dirty 属性正确暴露"""
        memory = Memory()
        assert memory.dirty is False
        # 置脏后复位
        memory.add_message({"role": "user", "content": "hi"})
        assert memory.dirty is True
        memory.mark_clean()
        assert memory.dirty is False
        # 重复复位无副作用
        memory.mark_clean()
        assert memory.dirty is False

    def test_add_message_sets_dirty(self):
        """add_message 置脏"""
        memory = Memory()
        memory.add_message({"role": "user", "content": "hello"})
        assert memory.dirty is True

    def test_add_messages_sets_dirty(self):
        """add_messages 置脏"""
        memory = Memory()
        memory.add_messages([
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ])
        assert memory.dirty is True

    def test_roll_back_sets_dirty(self):
        """roll_back 在有消息时置脏,空记忆不置脏"""
        # 有消息: 回滚置脏
        memory = Memory()
        memory.add_message({"role": "user", "content": "x"})
        memory.mark_clean()
        memory.roll_back()
        assert memory.dirty is True
        # 空记忆: 回滚不置脏
        empty_memory = Memory()
        empty_memory.roll_back()
        assert empty_memory.dirty is False

    def test_compact_sets_dirty(self):
        """compact 置脏"""
        memory = Memory()
        memory.add_message({"role": "system", "content": "sys"})
        memory.add_message({"role": "user", "content": "hi"})
        memory.mark_clean()
        memory.compact()
        assert memory.dirty is True

    def test_emergency_compact_sets_dirty(self):
        """emergency_compact 在消息数超过保护阈值时置脏"""
        memory = Memory()
        # 消息数需 > protect_head_count(2) + protect_tail_count(4) = 6 才不提前返回
        for i in range(10):
            memory.add_message({"role": "user", "content": f"msg-{i}"})
        memory.mark_clean()
        memory.emergency_compact()
        assert memory.dirty is True

    def test_sanitize_sets_dirty_only_when_removed(self):
        """sanitize_tool_message_pairing 仅在 removed>0 时置脏"""
        # 无配对破坏: 不置脏
        memory = Memory()
        memory.add_message({"role": "system", "content": "sys"})
        memory.add_message({"role": "user", "content": "hi"})
        memory.add_message({"role": "assistant", "content": "hello"})
        memory.mark_clean()
        removed = memory.sanitize_tool_message_pairing()
        assert removed == 0
        assert memory.dirty is False
        # 孤立 tool 消息(无配对 assistant): removed>0, 置脏
        memory2 = Memory()
        memory2.add_message({"role": "system", "content": "sys"})
        memory2.add_message({
            "role": "tool", "tool_call_id": "orphan",
            "function_name": "test", "content": "x",
        })
        memory2.mark_clean()
        removed2 = memory2.sanitize_tool_message_pairing()
        assert removed2 > 0
        assert memory2.dirty is True

    def test_dirty_excluded_from_serialization(self):
        """_dirty 不出现在 model_dump(), 不影响 DB JSONB 存储"""
        memory = Memory()
        memory.add_message({"role": "user", "content": "hi"})
        assert memory.dirty is True
        dumped = memory.model_dump()
        assert "_dirty" not in dumped
        assert "dirty" not in dumped
        # 核心字段仍正常序列化
        assert "messages" in dumped
        assert len(dumped["messages"]) == 1


class TestFlushMemory:
    """BaseAgent._flush_memory 刷盘行为测试"""

    @pytest.mark.asyncio
    async def test_flush_memory_persists_and_marks_clean(self):
        """dirty 时刷盘+复位, clean 时跳过"""
        # Case A: dirty=True → 刷盘一次并复位
        memory = Memory()
        memory.add_message({"role": "user", "content": "hi"})
        assert memory.dirty is True
        agent, fake_uow = _build_flush_agent(memory)

        await agent._flush_memory()
        fake_uow.session.save_memory.assert_called_once_with(
            "test_session", "test_agent", memory,
        )
        assert memory.dirty is False

        # Case B: dirty=False → 跳过(不调用 save_memory)
        agent2, fake_uow2 = _build_flush_agent(memory)
        await agent2._flush_memory()
        fake_uow2.session.save_memory.assert_not_called()
