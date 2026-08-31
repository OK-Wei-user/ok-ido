#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_tool_message_pairing.py
tool消息配对完整性单元测试

验证 emergency_compact 边界对齐 + sanitize_tool_message_pairing 兜底修复,
确保 OpenAI API 约束 "role=tool 消息必须紧跟 assistant(tool_calls) 消息" 始终满足。

根因会话 22b7faad: emergency_compact 的 head+tail 拼接破坏配对,
tail[0] 为 tool 消息时其配对 assistant 被截断到 head 之外,
中间隔着 summary_msg(system),触发 API 400 错误。
"""
import pytest

from app.domain.models.memory import Memory


def _build_assistant_with_tool_calls(tool_call_id: str, function_name: str = "shell_execute") -> dict:
    """构建带tool_calls的assistant消息"""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": tool_call_id,
            "type": "function",
            "function": {"name": function_name, "arguments": "{}"},
        }],
        "reasoning_content": "",
    }


def _build_tool_message(tool_call_id: str, function_name: str = "shell_execute", content: str = "ok") -> dict:
    """构建tool消息"""
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "function_name": function_name,
        "content": content,
    }


class TestSanitizeToolMessagePairing:
    """sanitize_tool_message_pairing 兜底修复"""

    def test_no_messages_noop(self):
        """空消息列表: 无操作"""
        memory = Memory(messages=[])
        assert memory.sanitize_tool_message_pairing() == 0
        assert memory.messages == []

    def test_paired_messages_preserved(self):
        """正常配对: assistant(tool_calls)→tool 全部保留"""
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "hello"},
            _build_assistant_with_tool_calls("call_A"),
            _build_tool_message("call_A"),
        ]
        memory = Memory(messages=messages)
        removed = memory.sanitize_tool_message_pairing()
        assert removed == 0
        assert len(memory.messages) == 4

    def test_orphan_tool_message_at_start_removed(self):
        """开头孤立tool消息: 删除(前面无assistant(tool_calls))"""
        messages = [
            {"role": "system", "content": "system"},
            _build_tool_message("call_orphan"),  # 孤立tool消息
            {"role": "user", "content": "hi"},
        ]
        memory = Memory(messages=messages)
        removed = memory.sanitize_tool_message_pairing()
        assert removed == 1
        assert len(memory.messages) == 2
        # 孤立tool消息被删除
        roles = [m["role"] for m in memory.messages]
        assert roles == ["system", "user"]

    def test_orphan_tool_message_after_system_removed(self):
        """system隔断后的tool消息: 删除(被其他角色消息隔开)"""
        messages = [
            _build_assistant_with_tool_calls("call_A"),
            _build_tool_message("call_A"),  # 正常配对
            {"role": "system", "content": "summary"},  # 隔断
            _build_tool_message("call_A"),  # 孤立(被system隔开)
        ]
        memory = Memory(messages=messages)
        removed = memory.sanitize_tool_message_pairing()
        assert removed == 1
        # 第一个tool保留,第二个孤立tool删除
        tool_msgs = [m for m in memory.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_A"

    def test_mismatched_tool_call_id_filled(self):
        """tool_call_id不匹配: 删除孤立tool + 补全未响应assistant(共2处)"""
        messages = [
            _build_assistant_with_tool_calls("call_A"),
            _build_tool_message("call_B"),  # id不匹配,孤立tool
        ]
        memory = Memory(messages=messages)
        removed = memory.sanitize_tool_message_pairing()
        # 2处修复: tool(B)孤立删除 + assistant(call_A)未响应补全
        assert removed == 2
        # 补全的tool消息存在(为call_A生成错误tool消息)
        tool_msgs = [m for m in memory.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_A"
        # assistant保留tool_calls(LLM可看到错误响应并决定重试)
        assistant = memory.messages[0]
        assert "tool_calls" in assistant

    def test_unresponded_assistant_filled(self):
        """未响应的assistant(tool_calls): 补全错误tool消息(保留tool_calls字段)"""
        messages = [
            _build_assistant_with_tool_calls("call_A"),  # 无后续tool响应
            {"role": "user", "content": "next question"},
        ]
        memory = Memory(messages=messages)
        removed = memory.sanitize_tool_message_pairing()
        assert removed == 1
        # assistant保留tool_calls: LLM可看到错误响应并决定重试
        assistant = memory.messages[0]
        assert assistant["role"] == "assistant"
        assert "tool_calls" in assistant
        # reasoning_content被同步删除(避免DeepSeek V4 400错误)
        assert "reasoning_content" not in assistant
        # 补全的tool消息存在
        tool_msgs = [m for m in memory.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_A"

    def test_unresponded_assistant_at_end_filled(self):
        """末尾未响应的assistant(tool_calls): 补全错误tool消息"""
        messages = [
            {"role": "user", "content": "hi"},
            _build_assistant_with_tool_calls("call_A"),  # 末尾无tool响应
        ]
        memory = Memory(messages=messages)
        removed = memory.sanitize_tool_message_pairing()
        assert removed == 1
        # 末尾补全后: [..., assistant(call_A), tool(call_A)错误消息]
        # assistant保留tool_calls
        assistant = memory.messages[-2]
        assert assistant["role"] == "assistant"
        assert "tool_calls" in assistant
        # 末尾是补全的tool消息
        assert memory.messages[-1]["role"] == "tool"
        assert memory.messages[-1]["tool_call_id"] == "call_A"

    def test_assistant_with_content_preserved_on_fill(self):
        """补全时保留assistant的content(若非None)与tool_calls"""
        messages = [
            {
                "role": "assistant",
                "content": "thinking...",
                "tool_calls": [{"id": "call_A", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
                "reasoning_content": "reasoning",
            },
            {"role": "user", "content": "next"},
        ]
        memory = Memory(messages=messages)
        removed = memory.sanitize_tool_message_pairing()
        assert removed == 1
        assistant = memory.messages[0]
        assert assistant["content"] == "thinking..."  # content保留
        assert "tool_calls" in assistant  # tool_calls保留
        assert "reasoning_content" not in assistant  # reasoning被清理

    def test_multiple_tool_calls_pairing(self):
        """assistant(tool_calls=[A,B])→tool(A)→tool(B): 全部保留"""
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_A", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
                {"id": "call_B", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
            ],
            "reasoning_content": "",
        }
        messages = [
            assistant,
            _build_tool_message("call_A"),
            _build_tool_message("call_B"),
        ]
        memory = Memory(messages=messages)
        removed = memory.sanitize_tool_message_pairing()
        assert removed == 0
        assert len(memory.messages) == 3

    def test_partial_response_then_other_role_fills(self):
        """assistant(tool_calls=[A,B])→tool(A)→user: B未响应,补全B的错误tool消息"""
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_A", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
                {"id": "call_B", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
            ],
            "reasoning_content": "",
        }
        messages = [
            assistant,
            _build_tool_message("call_A"),  # 仅响应A
            {"role": "user", "content": "next"},  # B未响应
        ]
        memory = Memory(messages=messages)
        removed = memory.sanitize_tool_message_pairing()
        # B未响应: 补全B的错误tool消息
        assert removed >= 1
        # assistant保留tool_calls(LLM可看到A结果+B错误,决定重试B或切换策略)
        for m in memory.messages:
            if m["role"] == "assistant":
                assert "tool_calls" in m
        # 补全的tool消息存在(为B生成)
        tool_msgs = [m for m in memory.messages if m["role"] == "tool"]
        tool_ids = {t["tool_call_id"] for t in tool_msgs}
        assert "call_A" in tool_ids  # 原始A保留
        assert "call_B" in tool_ids  # 补全的B存在

    def test_idempotent(self):
        """幂等性: 已配对完整的消息多次调用不变"""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            _build_assistant_with_tool_calls("call_A"),
            _build_tool_message("call_A"),
        ]
        memory = Memory(messages=messages)
        first = memory.sanitize_tool_message_pairing()
        second = memory.sanitize_tool_message_pairing()
        assert first == 0
        assert second == 0
        assert len(memory.messages) == 4


class TestEmergencyCompactBoundaryAlignment:
    """emergency_compact 边界对齐 - 根因修复"""

    def _build_long_messages(self, count: int) -> list:
        """构建长消息列表(模拟触发紧急压缩)

        结构: system, user, [assistant(tool_calls)→tool] * N, user(末尾)
        末尾user使总长度为奇数(2N+3),让默认tail_start位置落在tool消息上,
        以验证边界对齐向前扩展到配对assistant的场景。
        """
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "task description"},
        ]
        for i in range(count):
            call_id = f"call_{i}"
            messages.append(_build_assistant_with_tool_calls(call_id))
            messages.append(_build_tool_message(call_id, content=f"result_{i}"))
        messages.append({"role": "user", "content": "final user message"})
        return messages

    def test_tail_starts_with_tool_aligned(self):
        """tail[0]是tool消息时,边界向前扩展到配对的assistant"""
        # 构建足够长的消息触发紧急压缩
        messages = self._build_long_messages(30)  # 2 + 60 = 62条
        memory = Memory(messages=messages)

        # 计算默认tail_start位置,确认该位置是tool消息
        from app.domain.models.memory_config import DEFAULT_MEMORY_CONFIG as _CFG
        default_tail_start = len(messages) - _CFG.protect_tail_count
        assert messages[default_tail_start]["role"] == "tool", "测试前置: 默认tail[0]应为tool消息"

        memory.emergency_compact()

        # 压缩后tail[0]不应是tool消息(边界对齐生效)
        # 找到summary_msg后的第一条消息
        summary_idx = None
        for i, m in enumerate(memory.messages):
            if m["role"] == "system" and "[上下文紧急压缩]" in m.get("content", ""):
                summary_idx = i
                break
        assert summary_idx is not None, "应存在紧急压缩摘要消息"
        tail_first_idx = summary_idx + 1
        assert tail_first_idx < len(memory.messages), "tail部分应非空"
        # 核心断言: tail[0]不是tool消息(边界对齐生效)
        assert memory.messages[tail_first_idx]["role"] != "tool", \
            "边界对齐后tail[0]不应是tool消息"

    def test_no_orphan_tool_after_emergency_compact(self):
        """emergency_compact后无孤立tool消息(配对完整)"""
        messages = self._build_long_messages(30)
        memory = Memory(messages=messages)
        memory.emergency_compact()

        # 验证所有tool消息都有配对的assistant(tool_calls)在前
        pending_ids = set()
        for msg in memory.messages:
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                pending_ids = {tc["id"] for tc in msg["tool_calls"] if tc.get("id")}
            elif msg["role"] == "tool":
                assert msg["tool_call_id"] in pending_ids, \
                    f"孤立tool消息: tool_call_id={msg['tool_call_id']}"
                pending_ids.discard(msg["tool_call_id"])
            else:
                pending_ids.clear()

    def test_short_messages_no_emergency_compact(self):
        """短消息列表不触发紧急压缩"""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        memory = Memory(messages=messages)
        original_count = len(memory.messages)
        memory.emergency_compact()
        assert len(memory.messages) == original_count, "短消息不应压缩"

    def test_tail_starts_with_assistant_no_realignment(self):
        """tail[0]是assistant时无需边界对齐(本就配对完整)"""
        # 构建tail[0]是assistant的消息序列
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        # 添加足够多的消息使tail起始位置是assistant
        from app.domain.models.memory_config import DEFAULT_MEMORY_CONFIG as _CFG
        # 总长度 = head + tail + 中间, 确保tail[0]是assistant
        # tail_count=4, 我们让倒数第4条是assistant
        for i in range(30):
            call_id = f"call_{i}"
            messages.append(_build_assistant_with_tool_calls(call_id))
            messages.append(_build_tool_message(call_id))
        # 现在 messages = [sys, user, asst, tool, asst, tool, ...]
        # 长度 = 2 + 60 = 62, tail_start = 62-4 = 58, messages[58]是assistant(偶数索引)
        memory = Memory(messages=messages)
        memory.emergency_compact()
        # 验证无孤立tool消息
        pending_ids = set()
        for msg in memory.messages:
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                pending_ids = {tc["id"] for tc in msg["tool_calls"] if tc.get("id")}
            elif msg["role"] == "tool":
                assert msg["tool_call_id"] in pending_ids, "孤立tool消息"
                pending_ids.discard(msg["tool_call_id"])
            else:
                pending_ids.clear()

    def test_emergency_compact_preserves_head(self):
        """紧急压缩保留head(前N条消息)"""
        from app.domain.models.memory_config import DEFAULT_MEMORY_CONFIG as _CFG
        messages = self._build_long_messages(30)
        memory = Memory(messages=messages)
        head_original = messages[:_CFG.protect_head_count]
        memory.emergency_compact()
        # head部分应保留
        for i, orig in enumerate(head_original):
            assert memory.messages[i] == orig, f"head[{i}]应保留"


class TestRepairToolMessagePairingInBaseAgent:
    """BaseAgent._repair_tool_message_pairing 集成测试"""

    @pytest.mark.asyncio
    async def test_repair_persists_memory(self):
        """修复后持久化memory(使用mock uow)"""
        from unittest.mock import AsyncMock, MagicMock
        from app.domain.services.agents.base import BaseAgent

        # 构建含孤立tool消息的memory
        memory = Memory(messages=[
            {"role": "system", "content": "sys"},
            _build_tool_message("call_orphan"),  # 孤立tool
            {"role": "user", "content": "hi"},
        ])

        # mock uow
        uow = AsyncMock()
        uow.__aenter__.return_value = uow
        uow.__aexit__.return_value = None
        uow.session = MagicMock()
        uow.session.save_memory = AsyncMock()

        # 构造BaseAgent实例(绕过__init__)
        agent = BaseAgent.__new__(BaseAgent)
        agent._memory = memory
        agent._uow = uow
        agent._session_id = "test-session"
        agent.name = "test"
        agent._uow_factory = lambda: uow

        # 执行修复
        await agent._repair_tool_message_pairing()

        # 验证memory被修复(孤立tool删除)
        assert len(memory.messages) == 2
        roles = [m["role"] for m in memory.messages]
        assert roles == ["system", "user"]
        # 验证持久化被调用
        uow.session.save_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_repair_no_change_skips_persist(self):
        """无修复时跳过持久化(性能优化)"""
        from unittest.mock import AsyncMock, MagicMock
        from app.domain.services.agents.base import BaseAgent

        # 已配对完整的memory
        memory = Memory(messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            _build_assistant_with_tool_calls("call_A"),
            _build_tool_message("call_A"),
        ])

        uow = AsyncMock()
        uow.__aenter__.return_value = uow
        uow.__aexit__.return_value = None
        uow.session = MagicMock()
        uow.session.save_memory = AsyncMock()

        agent = BaseAgent.__new__(BaseAgent)
        agent._memory = memory
        agent._uow = uow
        agent._session_id = "test-session"
        agent.name = "test"
        agent._uow_factory = lambda: uow

        await agent._repair_tool_message_pairing()

        # 无修复时不持久化
        uow.session.save_memory.assert_not_called()
        assert len(memory.messages) == 4  # 未变化