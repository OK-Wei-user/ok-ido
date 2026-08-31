#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_f10_1_streaming.py
F10-1 流式输出单元测试 - 验证切片算法、流式推送契约、降级策略

测试覆盖:
- _split_content_into_chunks: 空内容/短内容/句号切片/长段落硬切/混合标点
- _stream_final_answer: 配置开关/切片推送顺序/附件携带/空内容/异常降级
- 前后端交互契约: is_streaming=True 增量不写库, is_final=True 完整内容写库
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.models.app_config import AgentConfig
from app.domain.models.event import MessageEvent
from app.domain.services.agents.base import BaseAgent


# ========== 测试辅助函数 ==========

def _build_agent(stream_final_answer: bool = True,
                 min_chars: int = 50,
                 max_chars: int = 300,
                 delay_ms: int = 0) -> BaseAgent:
    """构建BaseAgent实例(绕过__init__,仅设置流式切片所需属性)"""
    agent = object.__new__(BaseAgent)
    agent.name = "test_agent"
    agent._agent_config = AgentConfig(
        max_iterations=10,
        stream_final_answer=stream_final_answer,
        stream_chunk_min_chars=min_chars,
        stream_chunk_max_chars=max_chars,
        stream_chunk_delay_ms=delay_ms,
    )
    return agent


async def _collect_events(gen) -> list:
    """收集异步生成器的所有事件"""
    events = []
    async for evt in gen:
        events.append(evt)
    return events


# ========== _split_content_into_chunks 单元测试 ==========

class TestSplitContentIntoChunks:
    """测试切片算法的各种边界场景"""

    def test_empty_content_returns_empty_list(self):
        """空内容应返回空列表"""
        chunks = BaseAgent._split_content_into_chunks("", min_chars=50, max_chars=300)
        assert chunks == []

    def test_short_content_returns_single_chunk(self):
        """短内容(不足min_chars)应作为单一片返回"""
        content = "这是一段简短的文本。"
        chunks = BaseAgent._split_content_into_chunks(content, min_chars=50, max_chars=300)
        assert len(chunks) == 1
        assert chunks[0] == content

    def test_split_by_chinese_period(self):
        """按中文句号切片,每片达到min_chars即输出"""
        # 5个句子,每句20字符,总长100字符
        sentence = "这是第X个测试句子用于验证切片。"  # 16字符
        content = sentence * 5  # 80字符
        chunks = BaseAgent._split_content_into_chunks(content, min_chars=30, max_chars=100)
        # 应至少切成2片(80字符/30字符阈值)
        assert len(chunks) >= 2
        # 拼接后应等于原文
        assert "".join(chunks) == content

    def test_split_by_newline(self):
        """按换行符切片"""
        content = "第一行内容足够长用于测试切片算法。\n第二行内容也足够长用于测试切片算法。\n"
        chunks = BaseAgent._split_content_into_chunks(content, min_chars=20, max_chars=100)
        assert len(chunks) >= 1
        assert "".join(chunks) == content

    def test_split_by_question_mark(self):
        """按问号切片"""
        content = "这是第一个问题?这是第二个问题?这是第三个问题?"
        chunks = BaseAgent._split_content_into_chunks(content, min_chars=10, max_chars=50)
        assert len(chunks) >= 1
        assert "".join(chunks) == content

    def test_long_sentence_hard_split(self):
        """超长单句应按max_chars硬切"""
        content = "a" * 250  # 250字符无标点
        chunks = BaseAgent._split_content_into_chunks(content, min_chars=50, max_chars=100)
        # 250字符按100硬切应得3片(100+100+50)
        assert len(chunks) == 3
        assert len(chunks[0]) == 100
        assert len(chunks[1]) == 100
        assert len(chunks[2]) == 50
        assert "".join(chunks) == content

    def test_mixed_punctuation(self):
        """混合标点(。!?\\n)切片"""
        content = (
            "这是句子一。"
            "这是句子二用于增加长度足够长足够长足够长足够长?!"
            "这是句子三\n"
            "这是句子四用于测试。"
        )
        chunks = BaseAgent._split_content_into_chunks(content, min_chars=20, max_chars=200)
        assert len(chunks) >= 1
        # 拼接完整性校验
        assert "".join(chunks) == content

    def test_chunk_length_within_bounds(self):
        """切片长度应在[min_chars, max_chars]区间内(末尾片除外)"""
        content = "。".join(["句子内容足够长用于测试" * 3] * 10) + "。"
        min_chars, max_chars = 50, 100
        chunks = BaseAgent._split_content_into_chunks(content, min_chars=min_chars, max_chars=max_chars)
        for chunk in chunks[:-1]:  # 末尾片可能短于min_chars
            assert len(chunk) <= max_chars, f"切片长度{len(chunk)}超过max_chars{max_chars}"


# ========== _stream_final_answer 单元测试 ==========

class TestStreamFinalAnswer:
    """测试流式推送契约与降级策略"""

    @pytest.mark.asyncio
    async def test_streaming_disabled_returns_single_final_event(self):
        """配置关闭时一次性返回完整内容(is_final=True)"""
        agent = _build_agent(stream_final_answer=False)
        content = "这是一段完整的内容,不应被切片。"
        events = await _collect_events(agent._stream_final_answer(content))

        assert len(events) == 1
        assert events[0].is_final is True
        assert events[0].is_streaming is False
        assert events[0].message == content

    @pytest.mark.asyncio
    async def test_streaming_enabled_emits_delta_then_final(self):
        """配置开启时先发增量片,最后发完整片"""
        agent = _build_agent(stream_final_answer=True, min_chars=10, max_chars=50, delay_ms=0)
        content = "这是第一句足够长的内容用于测试切片。这是第二句足够长的内容用于测试切片。"
        events = await _collect_events(agent._stream_final_answer(content))

        # 应有多个增量片 + 1个最终片
        streaming_events = [e for e in events if e.is_streaming]
        final_events = [e for e in events if e.is_final]
        assert len(streaming_events) >= 1, "应至少有一个增量片"
        assert len(final_events) == 1, "应有且仅有一个最终片"

        # 增量片标记校验
        for evt in streaming_events:
            assert evt.is_streaming is True
            assert evt.is_final is False

        # 最终片内容校验
        final_evt = final_events[0]
        assert final_evt.message == content
        assert final_evt.is_streaming is False

    @pytest.mark.asyncio
    async def test_empty_content_returns_single_final_event(self):
        """空内容应返回单个is_final事件"""
        agent = _build_agent(stream_final_answer=True)
        events = await _collect_events(agent._stream_final_answer(""))

        assert len(events) == 1
        assert events[0].is_final is True
        assert events[0].message == ""

    @pytest.mark.asyncio
    async def test_attachments_carried_only_in_final_event(self):
        """附件应仅在最终片(is_final=True)携带"""
        from app.domain.models.file import File
        agent = _build_agent(stream_final_answer=True, min_chars=10, max_chars=50, delay_ms=0)
        attachments = [File(filepath="/tmp/test.txt")]
        content = "这是足够长的内容用于触发切片测试。这是足够长的内容用于触发切片测试。"

        events = await _collect_events(agent._stream_final_answer(content, attachments=attachments))

        # 增量片不应携带附件
        for evt in events:
            if evt.is_streaming:
                assert len(evt.attachments) == 0, "增量片不应携带附件"
        # 最终片应携带附件
        final_evt = [e for e in events if e.is_final][0]
        assert len(final_evt.attachments) == 1
        assert final_evt.attachments[0].filepath == "/tmp/test.txt"

    @pytest.mark.asyncio
    async def test_streaming_disabled_with_attachments(self):
        """配置关闭时附件随单一片返回"""
        from app.domain.models.file import File
        agent = _build_agent(stream_final_answer=False)
        attachments = [File(filepath="/tmp/data.csv")]
        events = await _collect_events(agent._stream_final_answer("短内容", attachments=attachments))

        assert len(events) == 1
        assert events[0].is_final is True
        assert len(events[0].attachments) == 1

    @pytest.mark.asyncio
    async def test_delta_events_not_written_to_db(self):
        """契约校验: is_streaming=True 的事件不应被AgentTaskRunner写库

        本测试验证 MessageEvent 的 is_streaming 标记正确设置,
        AgentTaskRunner._put_and_add_event 据此跳过 DB 写入(line 147)
        """
        agent = _build_agent(stream_final_answer=True, min_chars=10, max_chars=50, delay_ms=0)
        content = "这是足够长的内容用于触发切片测试。这是足够长的内容用于触发切片测试。"
        events = await _collect_events(agent._stream_final_answer(content))

        # 增量片标记校验: is_streaming=True, is_final=False
        streaming_events = [e for e in events if e.is_streaming]
        for evt in streaming_events:
            assert evt.is_streaming is True
            assert evt.is_final is False, "增量片不应携带 is_final=True"

        # 最终片标记校验: is_final=True, is_streaming=False
        final_events = [e for e in events if e.is_final]
        assert len(final_events) == 1
        assert final_events[0].is_streaming is False

    @pytest.mark.asyncio
    async def test_json_format_skips_streaming(self):
        """JSON 内容不切片: PlannerAgent/ReActAgent 等 JSON 模式 Agent 的输出需整体解析

        场景: PlannerAgent._format="json_object", 其输出为完整 JSON(如 Plan JSON)。
        若切片推送,每个 is_streaming 片段都不是有效 JSON,会导致:
        - _json_parser.invoke 解析失败
        - 触发降级 Plan 创建("我将为您处理这个任务。")
        - SSE 事件流被重复的降级 PlanEvent 污染

        契约: content_is_json=True 时应一次性返回完整内容(is_final=True), 不切片。
        """
        agent = _build_agent(stream_final_answer=True, min_chars=10, max_chars=50, delay_ms=0)
        # 模拟 PlannerAgent 输出的 Plan JSON(长度足以触发切片,但不应被切片)
        content = '{"title":"任务处理","goal":"测试目标","steps":[{"id":"1","description":"步骤一"}]}'

        events = await _collect_events(agent._stream_final_answer(content, content_is_json=True))

        # 应仅有1个事件且为最终片(不切片)
        assert len(events) == 1, "JSON 内容不应被切片"
        assert events[0].is_final is True
        assert events[0].is_streaming is False
        assert events[0].message == content, "应保留完整 JSON 内容"

    @pytest.mark.asyncio
    async def test_json_format_with_attachments(self):
        """JSON 内容 + 附件: 附件随单一片返回"""
        from app.domain.models.file import File
        agent = _build_agent(stream_final_answer=True)
        attachments = [File(filepath="/tmp/plan.json")]
        content = '{"steps":[]}'

        events = await _collect_events(agent._stream_final_answer(content, attachments=attachments, content_is_json=True))

        assert len(events) == 1
        assert events[0].is_final is True
        assert len(events[0].attachments) == 1

    @pytest.mark.asyncio
    async def test_natural_language_content_streaming(self):
        """自然语言内容(如 summarize 解析后的 message)正常切片

        场景: ReActAgent.summarize() 解析 LLM JSON 输出后,将 message 字段(自然语言)
        传给 _stream_final_answer。此时 content_is_json=False(默认),应正常切片。
        这验证了 JSON 模式 Agent 仍能对自然语言内容进行流式切片。
        """
        agent = _build_agent(stream_final_answer=True, min_chars=10, max_chars=50, delay_ms=0)
        # 模拟 summarize 解析后的自然语言最终答案
        content = "任务已完成。这是详细的执行结果说明，包含了关键信息和分析结论。"

        events = await _collect_events(agent._stream_final_answer(content))  # content_is_json 默认 False

        # 应有增量片 + 最终片
        streaming_events = [e for e in events if e.is_streaming]
        final_events = [e for e in events if e.is_final]
        assert len(streaming_events) >= 1, "自然语言内容应被切片"
        assert len(final_events) == 1
        assert final_events[0].message == content
