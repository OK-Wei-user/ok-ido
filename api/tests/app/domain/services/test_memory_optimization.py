#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_memory_optimization.py
记忆系统优化单元测试 - Shell去重、关键事实增强、连续tool合并、压缩异常处理
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.models.memory import Memory, KeyFact, CompressionLevel


class TestShellOutputDeduplication:
    """Shell输出压缩：JSON解析去重优先于长度截断

    Phase E: 使用统一方法 _compress_tool_content 替代 _compress_shell_content。
    """

    def test_short_json_console_deduplicated(self):
        content = json.dumps({
            "console": [
                {"command": "ls", "output": "file1.txt"},
                {"command": "cat hello.txt", "output": "Hello World"},
            ]
        }, ensure_ascii=False)
        result = Memory._compress_tool_content(content, "shell_exec")
        parsed = json.loads(result)
        assert len(parsed["console"]) == 1
        assert parsed["console"][0]["command"] == "cat hello.txt"

    def test_long_json_console_deduplicated_and_truncated(self):
        long_output = "x" * 600
        content = json.dumps({
            "console": [
                {"command": "ls", "output": "file1.txt"},
                {"command": "cat big.txt", "output": long_output},
            ]
        }, ensure_ascii=False)
        result = Memory._compress_tool_content(content, "shell_exec")
        parsed = json.loads(result)
        assert len(parsed["console"]) == 1
        assert "truncated" in parsed["console"][0]["output"]

    def test_non_json_content_truncated(self):
        content = "a" * 600
        result = Memory._compress_tool_content(content, "shell_exec")
        assert "truncated" in result

    def test_non_string_content_returns_placeholder(self):
        result = Memory._compress_tool_content(12345, "shell_exec")
        assert "shell_exec" in result
        assert "removed" in result

    def test_empty_console_returns_original(self):
        content = json.dumps({"console": []}, ensure_ascii=False)
        result = Memory._compress_tool_content(content, "shell_exec")
        assert result == content

    def test_short_non_json_content_preserved(self):
        content = "short output"
        result = Memory._compress_tool_content(content, "shell_exec")
        assert result == content


class TestKeyFactsUserRequirement:
    """关键事实提取增强 - 用户原始需求显式记录"""

    def test_user_requirement_extracted_first(self):
        memory = Memory(messages=[
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "请帮我分析中国GDP数据并生成报告"},
            {"role": "assistant", "content": "好的，我来处理"},
            {"role": "tool", "function_name": "write_file", "content": json.dumps({"filepath": "/home/ubuntu/report.md"})},
        ])
        facts = memory.extract_key_facts()
        req_facts = [f for f in facts if f.category == "requirement"]
        assert len(req_facts) >= 1
        assert "中国GDP" in req_facts[0].content

    def test_user_requirement_sorted_first_when_no_existing_facts(self):
        memory = Memory(messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "请帮我分析数据并生成详细报告"},
            {"role": "tool", "function_name": "write_file", "content": json.dumps({"filepath": "/home/ubuntu/report.md"})},
        ])
        facts = memory.extract_key_facts()
        req_facts = [f for f in facts if f.category == "requirement"]
        assert len(req_facts) >= 1

    def test_short_user_message_not_extracted(self):
        memory = Memory(messages=[
            {"role": "user", "content": "hi"},
        ])
        facts = memory.extract_key_facts()
        req_facts = [f for f in facts if f.category == "requirement"]
        assert len(req_facts) == 0

    def test_multiple_user_messages_all_extracted(self):
        memory = Memory(messages=[
            {"role": "user", "content": "第一个需求，分析数据，需要处理很多信息"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "第二个需求，生成图表，包含多种数据"},
        ])
        facts = memory.extract_key_facts()
        req_facts = [f for f in facts if f.category == "requirement"]
        assert len(req_facts) >= 1

    def test_existing_facts_preserved_and_merged(self):
        memory = Memory(
            messages=[{"role": "user", "content": "请分析这个数据文件，需要生成详细报告"}],
            key_facts=[KeyFact(category="file", content="/home/ubuntu/data.csv")],
        )
        facts = memory.extract_key_facts()
        categories = [f.category for f in facts]
        assert "file" in categories


class TestConsecutiveToolMessageMerge:
    """连续tool消息合并 - 减少消息数量"""

    def test_merge_consecutive_same_tool_no_interleaving(self):
        memory = Memory(messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "thinking", "tool_calls": [{"id": "1", "function": {"name": "shell_exec"}}]},
            {"role": "tool", "function_name": "shell_exec", "tool_call_id": "1", "content": "output1"},
            {"role": "assistant", "content": "thinking2", "tool_calls": [{"id": "2", "function": {"name": "shell_exec"}}]},
            {"role": "tool", "function_name": "shell_exec", "tool_call_id": "2", "content": "output2"},
            {"role": "assistant", "content": "thinking3", "tool_calls": [{"id": "3", "function": {"name": "shell_exec"}}]},
            {"role": "tool", "function_name": "shell_exec", "tool_call_id": "3", "content": "output3"},
            {"role": "assistant", "content": "done"},
        ])
        memory._merge_consecutive_tool_messages()
        tool_msgs = [m for m in memory.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 3

    def test_truly_consecutive_same_tool_merged(self):
        """孤儿tool消息(无owning assistant)保守不合并(防止丢失并行调用结果)

        新实现修复了并行工具调用结果丢失bug:
        - 旧实现: 连续同function_name的tool消息总是合并(保留最后一条)
        - 新实现: 无法确定归属的孤儿tool消息保守不合并(避免误删并行调用结果)
        - 仅合并明确属于不同assistant轮次的连续tool消息
        """
        memory = Memory(messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "thinking"},
            {"role": "tool", "function_name": "shell_exec", "tool_call_id": "1", "content": "output1"},
            {"role": "tool", "function_name": "shell_exec", "tool_call_id": "2", "content": "output2"},
            {"role": "tool", "function_name": "shell_exec", "tool_call_id": "3", "content": "output3"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "next"},
        ])
        memory._merge_consecutive_tool_messages()
        tool_msgs = [m for m in memory.messages if m.get("role") == "tool"]
        # 孤儿tool消息(无owning assistant): 保守不合并,保留全部3条
        assert len(tool_msgs) == 3

    def test_parallel_tools_same_assistant_not_merged(self):
        """同一assistant的并行tool调用结果不合并(保护并行结果完整性)"""
        memory = Memory(messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "thinking", "tool_calls": [
                {"id": "1", "function": {"name": "search_web"}},
                {"id": "2", "function": {"name": "search_web"}},
            ]},
            {"role": "tool", "function_name": "search_web", "tool_call_id": "1", "content": "result_A"},
            {"role": "tool", "function_name": "search_web", "tool_call_id": "2", "content": "result_B"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "next"},
        ])
        memory._merge_consecutive_tool_messages()
        tool_msgs = [m for m in memory.messages if m.get("role") == "tool"]
        # 同一assistant的并行调用: 不合并,保留全部2条
        assert len(tool_msgs) == 2
        assert {m["content"] for m in tool_msgs} == {"result_A", "result_B"}

    def test_no_merge_different_tools(self):
        memory = Memory(messages=[
            {"role": "system", "content": "system"},
            {"role": "tool", "function_name": "shell_exec", "tool_call_id": "1", "content": "shell output"},
            {"role": "tool", "function_name": "write_file", "tool_call_id": "2", "content": "file output"},
            {"role": "assistant", "content": "done"},
        ])
        memory._merge_consecutive_tool_messages()
        tool_msgs = [m for m in memory.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 2

    def test_no_merge_interleaved_with_assistant(self):
        memory = Memory(messages=[
            {"role": "system", "content": "system"},
            {"role": "tool", "function_name": "shell_exec", "tool_call_id": "1", "content": "output1"},
            {"role": "assistant", "content": "thinking..."},
            {"role": "tool", "function_name": "shell_exec", "tool_call_id": "2", "content": "output2"},
        ])
        memory._merge_consecutive_tool_messages()
        tool_msgs = [m for m in memory.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 2

    def test_short_messages_no_merge(self):
        memory = Memory(messages=[
            {"role": "system", "content": "system"},
            {"role": "tool", "function_name": "shell_exec", "tool_call_id": "1", "content": "output1"},
            {"role": "tool", "function_name": "shell_exec", "tool_call_id": "2", "content": "output2"},
        ])
        memory._merge_consecutive_tool_messages()
        assert len(memory.messages) == 3

    def test_merge_reduces_message_count(self):
        """孤儿tool消息(无owning assistant)保守不合并(新实现保护并行结果)

        新实现仅合并明确属于不同assistant轮次的连续tool消息;
        孤儿tool消息(无owning assistant)保守保留,避免误删并行调用结果。
        """
        memory = Memory(messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
        ])
        for i in range(10):
            memory.add_message({"role": "tool", "function_name": "shell_exec", "tool_call_id": str(i), "content": f"output{i}"})
        memory.add_message({"role": "tool", "function_name": "write_file", "tool_call_id": "w1", "content": "file written"})
        for i in range(10):
            memory.add_message({"role": "tool", "function_name": "shell_exec", "tool_call_id": f"b{i}", "content": f"output_b{i}"})
        memory.add_message({"role": "assistant", "content": "done"})
        original_count = len(memory.messages)
        memory._merge_consecutive_tool_messages()
        # 孤儿tool消息(无owning assistant): 保守不合并,消息数不变
        assert len(memory.messages) == original_count


class TestCompactMemoryExceptionHandling:
    """compact_memory异常处理 - 压缩失败不中断流程"""

    @pytest.mark.asyncio
    async def test_compact_failure_does_not_raise(self):
        from app.domain.services.agents.base import BaseAgent

        with patch.object(BaseAgent, "__init__", lambda self, **kw: None):
            agent = BaseAgent.__new__(BaseAgent)
            agent._memory = MagicMock()
            agent._memory.auto_compact = MagicMock(side_effect=RuntimeError("compress error"))
            _uow = AsyncMock()
            agent._uow = _uow
            agent._uow_factory = lambda: _uow
            agent._session_id = "test"
            agent.name = "test_agent"

            await agent.compact_memory()

    @pytest.mark.asyncio
    async def test_save_failure_does_not_raise(self):
        from app.domain.services.agents.base import BaseAgent

        with patch.object(BaseAgent, "__init__", lambda self, **kw: None):
            agent = BaseAgent.__new__(BaseAgent)
            agent._memory = MagicMock()
            agent._memory.auto_compact = MagicMock(return_value=CompressionLevel.NORMAL)
            _uow = AsyncMock()
            agent._uow = _uow
            agent._uow_factory = lambda: _uow
            _uow.session = AsyncMock()
            _uow.session.save_memory = AsyncMock(side_effect=Exception("DB error"))
            _uow.__aenter__ = AsyncMock(return_value=_uow)
            _uow.__aexit__ = AsyncMock(return_value=False)
            agent._session_id = "test"
            agent.name = "test_agent"

            await agent.compact_memory()

    @pytest.mark.asyncio
    async def test_successful_compact_saves_memory(self):
        from app.domain.services.agents.base import BaseAgent

        with patch.object(BaseAgent, "__init__", lambda self, **kw: None):
            agent = BaseAgent.__new__(BaseAgent)
            agent._memory = MagicMock()
            agent._memory.auto_compact = MagicMock(return_value=CompressionLevel.NORMAL)
            _uow = AsyncMock()
            agent._uow = _uow
            agent._uow_factory = lambda: _uow
            _uow.session = AsyncMock()
            _uow.__aenter__ = AsyncMock(return_value=_uow)
            _uow.__aexit__ = AsyncMock(return_value=False)
            agent._session_id = "test"
            agent.name = "test_agent"

            await agent.compact_memory()
            agent._uow.session.save_memory.assert_called_once()


class TestCompressionLevelJudgment:
    """压缩级别判断测试"""

    def test_none_level_below_soft_limit(self):
        memory = Memory(messages=[{"role": "system", "content": "system"}])
        assert memory.get_compression_level() == CompressionLevel.NONE

    def test_normal_level_at_soft_limit(self):
        memory = Memory(messages=[{"role": "system", "content": f"msg{i}"} for i in range(40)])
        level = memory.get_compression_level()
        assert level in (CompressionLevel.NONE, CompressionLevel.NORMAL)

    def test_emergency_level_at_hard_limit(self):
        """Phase E: ≥60条消息触发紧急压缩级别"""
        memory = Memory(messages=[{"role": "system", "content": f"msg{i}"} for i in range(60)])
        level = memory.get_compression_level()
        assert level == CompressionLevel.EMERGENCY


class TestStepCompletedKeyFacts:
    """步骤完成状态提取测试(根治 emergency_compact 后丢步重执)

    根因: emergency_compact 后执行步骤用户消息被压缩,LLM 丢失步骤完成状态,
    重新执行已完成的 describe/导出/验证步骤(会话6d4f313b根因)。
    修复: extract_key_facts 新增 step_completed 分类,从用户消息中解析
    build_prior_steps_context 注入的"步骤{id}(已完成)：{result}"格式。
    """

    def test_step_completed_extracted_from_execution_prompt(self):
        """执行步骤用户消息中的前序步骤完成情况应被提取为 step_completed"""
        exec_msg = (
            "你正在执行任务：\n"
            "【前序步骤完成情况（严禁重复执行已完成操作）】\n"
            "- 步骤1(已完成)：已导出5月数据到 /home/ubuntu/data.xlsx\n"
            "- 步骤2(已完成)：已校验数据完整性,共1000条记录\n"
            "注意：上述步骤已完成操作和产出的文件可直接复用。\n\n"
            "当前步骤：分析数据并生成报告\n\n"
            "注意事项：..."
        )
        memory = Memory(messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "请分析5月数据"},
            {"role": "user", "content": exec_msg},
        ])
        facts = memory.extract_key_facts()
        step_facts = [f for f in facts if f.category == "step_completed"]
        assert len(step_facts) >= 2
        contents = [f.content for f in step_facts]
        assert any("步骤1" in c and "data.xlsx" in c for c in contents)
        assert any("步骤2" in c and "1000条" in c for c in contents)

    def test_normal_user_message_not_extracted_as_step_completed(self):
        """普通用户消息不应产生 step_completed 事实"""
        memory = Memory(messages=[
            {"role": "user", "content": "请帮我分析数据并生成报告,需要详细内容"},
        ])
        facts = memory.extract_key_facts()
        step_facts = [f for f in facts if f.category == "step_completed"]
        assert len(step_facts) == 0

    def test_step_completed_dedup_by_content_hash(self):
        """相同步骤完成内容不重复添加(基于 content_hash 去重)"""
        exec_msg = (
            "你正在执行任务：\n"
            "【前序步骤完成情况】\n"
            "- 步骤1(已完成)：已导出数据\n"
            "当前步骤：分析数据"
        )
        memory = Memory(messages=[
            {"role": "user", "content": exec_msg},
            {"role": "assistant", "content": "分析完成"},
            {"role": "user", "content": exec_msg},
        ])
        facts = memory.extract_key_facts()
        step_facts = [f for f in facts if f.category == "step_completed"]
        step1_facts = [f for f in step_facts if "步骤1" in f.content]
        assert len(step1_facts) == 1

    def test_step_completed_quota_enforced(self):
        """step_completed 分类配额生效(最多保留4条)"""
        lines = "\n".join(
            f"- 步骤{i}(已完成)：步骤{i}的执行结果摘要" for i in range(1, 7)
        )
        exec_msg = f"你正在执行任务：\n【前序步骤完成情况】\n{lines}\n当前步骤：汇总"
        memory = Memory(messages=[{"role": "user", "content": exec_msg}])
        facts = memory.extract_key_facts()
        step_facts = [f for f in facts if f.category == "step_completed"]
        assert len(step_facts) <= 4

    def test_step_completed_injected_into_key_facts_text(self):
        """step_completed 事实应出现在 get_key_facts_text 输出中"""
        exec_msg = (
            "你正在执行任务：\n"
            "【前序步骤完成情况】\n"
            "- 步骤1(已完成)：已导出5月数据\n"
            "当前步骤：分析数据"
        )
        memory = Memory(messages=[{"role": "user", "content": exec_msg}])
        memory.extract_key_facts()
        text = memory.get_key_facts_text()
        assert "step_completed" in text
        assert "步骤1" in text


class TestCompressionSummaryStepTransition:
    """压缩摘要步骤转移记录测试(防 emergency_compact 后丢步)"""

    def test_step_transition_captured_in_summary(self):
        """_build_compression_summary 应捕获步骤转移(→步骤: xxx)"""
        exec_msg = (
            "你正在执行任务：\n"
            "当前步骤：导出5月业务数据\n\n"
            "注意事项：..."
        )
        memory = Memory(messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": exec_msg},
        ])
        summary = memory._build_compression_summary()
        assert "→步骤:" in summary
        assert "导出5月业务数据" in summary

    def test_no_step_transition_for_normal_messages(self):
        """普通用户消息不产生步骤转移记录"""
        memory = Memory(messages=[
            {"role": "user", "content": "请帮我分析数据,需要详细内容"},
        ])
        summary = memory._build_compression_summary()
        assert "→步骤:" not in summary


class TestShellTruncationMarkerEnhanced:
    """Shell 输出截断标记增强测试(根治短输出截断致 LLM 困惑)

    根因: shell_output_keep=300 时短输出被过度截断,且截断标记仅含
    "(shell output truncated)"未说明是上下文压缩,LLM 误判为工具错误
    反复重试(会话ac5503b3根因)。
    修复: 1.阈值提升至500 2.截断标记注明原始长度+上下文压缩说明+文件引导
    """

    def test_json_console_truncation_marker_has_original_length(self):
        """JSON console 截断标记应包含原始长度和上下文压缩说明"""
        long_output = "x" * 600
        content = json.dumps({
            "console": [{"command": "cat big.txt", "output": long_output}]
        }, ensure_ascii=False)
        result = Memory._compress_tool_content(content, "shell_exec")
        assert "original 600 chars" in result
        assert "context compression not tool error" in result
        assert "write result to file" in result

    def test_plain_text_truncation_marker_has_original_length(self):
        """纯文本截断标记应包含原始长度和上下文压缩说明"""
        content = "a" * 600
        result = Memory._compress_tool_content(content, "shell_exec")
        assert "original 600 chars" in result
        assert "context compression not tool error" in result

    def test_short_output_not_truncated(self):
        """短于 shell_output_keep(500) 的输出不应被截断"""
        content = json.dumps({
            "console": [{"command": "echo hello", "output": "hello world"}]
        }, ensure_ascii=False)
        result = Memory._compress_tool_content(content, "shell_exec")
        parsed = json.loads(result)
        assert "truncated" not in parsed["console"][0]["output"]
        assert parsed["console"][0]["output"] == "hello world"
