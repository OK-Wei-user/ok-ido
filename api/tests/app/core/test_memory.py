#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_memory.py
工业级记忆系统单元测试 - 四层压缩/关键事实提取/工具结果截断/图片清理
"""
import json
import pytest

from app.domain.models.memory import (
    Memory, KeyFact, MemoryMetrics, CompressionLevel,
    _BROWSER_VIEW_TOOLS, _BROWSER_ACTION_TOOLS, _BROWSER_DATA_TOOLS,
    _FILE_TOOLS, _SHELL_TOOLS, _SEARCH_TOOLS,
)


class TestMemoryBasic:
    """记忆基本操作测试"""

    def test_add_and_get_messages(self):
        mem = Memory()
        mem.add_message({"role": "system", "content": "hello"})
        mem.add_message({"role": "user", "content": "hi"})
        assert len(mem.get_messages()) == 2
        assert mem.get_last_message()["content"] == "hi"

    def test_empty_memory(self):
        mem = Memory()
        assert mem.empty is True
        assert mem.get_last_message() is None

    def test_roll_back(self):
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "hi"})
        mem.roll_back()
        assert len(mem.get_messages()) == 1

    def test_add_messages_batch(self):
        mem = Memory()
        mem.add_messages([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ])
        assert len(mem.get_messages()) == 2

    def test_metrics_updated(self):
        mem = Memory()
        mem.add_message({"role": "user", "content": "hi"})
        assert mem.metrics.message_count == 1


class TestShouldCompress:
    """压缩判断测试"""

    def test_no_compress_when_few_messages(self):
        mem = Memory()
        for i in range(5):
            mem.add_message({"role": "user", "content": f"msg{i}"})
        assert mem.should_compress() is False

    def test_compress_when_many_messages(self):
        mem = Memory()
        for i in range(25):
            mem.add_message({"role": "user", "content": f"msg{i}"})
        assert mem.should_compress() is True

    def test_context_overflow(self):
        mem = Memory()
        for i in range(60):
            mem.add_message({"role": "user", "content": f"msg{i}"})
        assert mem.is_context_overflow() is True


class TestCompressionLevel:
    """压缩级别判断测试"""

    def test_none_level(self):
        mem = Memory()
        for i in range(10):
            mem.add_message({"role": "user", "content": f"msg{i}"})
        assert mem.get_compression_level() == CompressionLevel.NONE

    def test_normal_level(self):
        mem = Memory()
        for i in range(45):
            mem.add_message({"role": "user", "content": f"msg{i}"})
        assert mem.get_compression_level() == CompressionLevel.NORMAL

    def test_emergency_level(self):
        """Phase E: 75条消息(≥60)触发紧急压缩级别"""
        mem = Memory()
        for i in range(75):
            mem.add_message({"role": "user", "content": f"msg{i}"})
        assert mem.get_compression_level() == CompressionLevel.EMERGENCY


class TestCompact:
    """Layer1 常规压缩测试"""

    def test_browser_view_compressed(self):
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({
                "screenshot": "http://example.com/shot.png",
                "page_state": {"url": "https://example.com", "title": "Example"},
                "interactive_elements": [{"type": "button", "text": "Click"}] * 50,
            }),
        })
        original_len = len(mem.messages[1]["content"])
        mem.compact()
        compressed_len = len(mem.messages[1]["content"])
        assert compressed_len < original_len

    def test_browser_action_compressed(self):
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "browser_click",
            "content": "clicked on button",
        })
        mem.compact()
        assert "executed" in mem.messages[0]["content"]

    def test_console_exec_result_preserved(self):
        """console_exec返回值不被压缩为"executed"(会话e3f0762b根因修复)

        console_exec返回JS执行结果(LLM需读取的data),与click等纯动作工具不同。
        压缩为"executed"会丢失返回值,导致LLM反复调用console_exec验证状态。
        """
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "browser_console_exec",
            "content": json.dumps({"success": True, "data": {"result": "left"}}),
        })
        mem.compact()
        # 返回值应保留(含result字段),不应被压缩为"executed"
        assert "executed" not in mem.messages[0]["content"]
        assert "left" in mem.messages[0]["content"]

    def test_console_exec_long_result_truncated(self):
        """console_exec超长返回值截断但保留前部内容(非"executed"标记)"""
        mem = Memory()
        long_result = "x" * 10000
        mem.add_message({
            "role": "tool",
            "function_name": "browser_console_exec",
            "content": json.dumps({"success": True, "data": {"result": long_result}}),
        })
        mem.compact()
        content = mem.messages[0]["content"]
        assert "executed" not in content
        assert "truncated" in content
        assert len(content) < 10000

    def test_console_exec_not_in_action_tools(self):
        """console_exec不在_BROWSER_ACTION_TOOLS中(数据返回型工具单独管理)"""
        assert "browser_console_exec" not in _BROWSER_ACTION_TOOLS
        assert "browser_console_exec" in _BROWSER_DATA_TOOLS

    def test_search_result_truncated(self):
        mem = Memory()
        long_content = "x" * 2000
        mem.add_message({
            "role": "tool",
            "function_name": "search_web",
            "content": long_content,
        })
        mem.compact()
        assert len(mem.messages[0]["content"]) < 2000

    def test_shell_output_truncated(self):
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "shell_exec",
            "content": "x" * 1000,
        })
        mem.compact()
        assert len(mem.messages[0]["content"]) < 1000

    def test_reasoning_content_removed(self):
        mem = Memory()
        mem.add_message({
            "role": "assistant",
            "content": "hello",
            "reasoning_content": "thinking...",
        })
        mem.compact()
        assert "reasoning_content" not in mem.messages[0]

    def test_compact_compresses_all_in_small_session(self):
        """小会话compact()全量压缩所有tool消息(向后兼容)"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({
            "role": "tool",
            "function_name": "browser_click",
            "content": "clicked button",
        })
        mem.compact()
        # 所有tool消息应被压缩
        assert "executed" in mem.messages[1]["content"]

    def test_evict_browser_view_content_replaces_with_summary(self):
        """evict_browser_view_content将页面快照替换为压缩摘要

        核心架构原则: 页面快照只在当前决策时需要,决策后驱逐(参考9e1e5363)。
        旧快照在页面操作后过期(ref漂移),保留会导致LLM引用过期元素。
        """
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        # 添加一个完整的browser_view结果(含interactive_elements等大字段)
        full_content = json.dumps({
            "success": True,
            "data": {
                "page_state": {"url": "https://example.com", "title": "Example"},
                "content": "页面文本内容" * 100,
                "interactive_elements": [{"type": "button", "text": f"btn{i}"} for i in range(50)],
                "ref_map": [{"ref": f"@e{i}", "role": "button"} for i in range(50)],
                "screenshot": "[attached]",
            },
        })
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": full_content,
        })
        # 添加更新的browser_view(成为latest,使前一条被驱逐)
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({
                "success": True,
                "data": {"page_state": {"url": "https://latest.com", "title": "Latest"}},
            }),
        })

        evicted = mem.evict_browser_view_content()

        assert evicted == 1  # 仅驱逐较早的那条,保留latest
        # 内容应被替换为压缩摘要(含URL/标题,不含interactive_elements详情)
        compressed = mem.messages[1]["content"]
        assert "compressed" in compressed
        assert "example.com" in compressed
        assert "interactive_elements" not in compressed or "btn0" not in compressed

    def test_evict_browser_view_content_skips_already_compressed(self):
        """已是压缩摘要的browser_view消息跳过,避免重复压缩"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": "(compressed) [page: url, title: Test]",
        })

        evicted = mem.evict_browser_view_content()

        assert evicted == 0  # 已是摘要,跳过
        assert mem.messages[0]["content"] == "(compressed) [page: url, title: Test]"

    def test_evict_browser_view_content_skips_non_browser_tools(self):
        """非browser_view类工具消息不受驱逐影响"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "browser_click",
            "content": "clicked button",
        })
        mem.add_message({
            "role": "tool",
            "function_name": "shell_exec",
            "content": "command output",
        })

        evicted = mem.evict_browser_view_content()

        assert evicted == 0
        assert mem.messages[0]["content"] == "clicked button"
        assert mem.messages[1]["content"] == "command output"

    def test_evict_browser_view_content_handles_multimodal_list(self):
        """多模态content(list格式)的browser_view消息正确驱逐"""
        mem = Memory()
        # 模拟_build_tool_message_content生成的多模态格式
        tool_text = json.dumps({
            "success": True,
            "data": {
                "page_state": {"url": "https://test.com", "title": "Test"},
                "interactive_elements": [{"type": "button", "text": "btn"}] * 30,
                "screenshot": "[attached]",
            },
        })
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": [
                {"type": "text", "text": tool_text},
                {"type": "text", "text": "[screenshot已驱逐:仅用于上次决策,不持久化]"},
            ],
        })
        # 添加更新的browser_view(成为latest,使前一条被驱逐)
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({
                "success": True,
                "data": {"page_state": {"url": "https://latest.com", "title": "Latest"}},
            }),
        })

        evicted = mem.evict_browser_view_content()

        assert evicted == 1  # 仅驱逐较早的那条,保留latest
        # list content应被替换为string格式的压缩摘要
        assert isinstance(mem.messages[0]["content"], str)
        assert "compressed" in mem.messages[0]["content"]
        assert "test.com" in mem.messages[0]["content"]

    def test_evict_browser_view_content_multiple_messages(self):
        """多条browser_view消息: 仅驱逐较早的,保留最近一条供LLM验证操作结果

        会话d1eb3b5c根因: 驱逐全部快照后LLM无法查看[checked]等状态标记验证操作结果,
        被迫退化为console_exec查询。保留最近一条快照解决此问题。
        """
        mem = Memory()
        for i in range(3):
            mem.add_message({
                "role": "tool",
                "function_name": "browser_view",
                "content": json.dumps({
                    "success": True,
                    "data": {
                        "page_state": {"url": f"https://page{i}.com", "title": f"Page{i}"},
                        "interactive_elements": [{"type": "button", "text": f"btn{i}"}] * 20,
                    },
                }),
            })

        evicted = mem.evict_browser_view_content()

        # 仅驱逐前2条(保留最近一条page2供LLM验证状态)
        assert evicted == 2
        # 前2条被压缩为摘要
        for i in range(2):
            assert "compressed" in mem.messages[i]["content"]
            assert f"page{i}.com" in mem.messages[i]["content"]
        # 最后一条保留完整内容(未压缩)
        assert "compressed" not in mem.messages[2]["content"]
        assert "page2.com" in mem.messages[2]["content"]

    def test_evict_browser_view_content_keeps_latest_single(self):
        """仅一条browser_view消息时: 不驱逐(无更早快照需清理)"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({
                "success": True,
                "data": {
                    "page_state": {"url": "https://only.com", "title": "Only"},
                    "interactive_elements": [{"type": "button", "text": "btn"}] * 20,
                },
            }),
        })

        evicted = mem.evict_browser_view_content()

        assert evicted == 0
        assert "compressed" not in mem.messages[0]["content"]


class TestEmergencyCompact:
    """紧急压缩测试(Phase E: 合并原aggressive/minimal为单一紧急层)"""

    def test_emergency_compress_reduces_messages(self):
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "system", "content": "sys2"})
        for i in range(20):
            mem.add_message({"role": "user", "content": f"msg{i}"})
        mem.add_message({"role": "assistant", "content": "last"})
        mem.add_message({"role": "assistant", "content": "last2"})
        mem.add_message({"role": "assistant", "content": "last3"})
        mem.add_message({"role": "assistant", "content": "last4"})

        original_count = len(mem.messages)
        mem.emergency_compact()
        assert len(mem.messages) < original_count
        assert mem.messages[0]["content"] == "sys"
        assert mem.messages[-1]["content"] == "last4"

    def test_emergency_preserves_user_requirement(self):
        """Phase E: 紧急压缩保留用户原始需求(原minimal_compact能力)"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "请帮我搜索GitHub热门库"})
        for i in range(30):
            mem.add_message({"role": "tool", "function_name": "browser_view", "content": "data"})
        mem.emergency_compact()

        assert mem.messages[0]["role"] == "system"
        # summary_msg中应包含用户需求
        summary_msg = mem.messages[2]  # head(2) + summary(1) + tail
        assert "用户需求" in summary_msg["content"]
        assert "GitHub" in summary_msg["content"]


class TestAutoCompact:
    """自动压缩测试"""

    def test_auto_compact_selects_correct_level(self):
        """Phase E: 50条消息(≥40)触发常规压缩"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        for i in range(50):
            mem.add_message({"role": "user", "content": f"msg{i}"})
        level = mem.auto_compact()
        assert level == CompressionLevel.NORMAL


class TestTruncateToolResult:
    """工具结果截断测试"""

    def test_short_content_not_truncated(self):
        result = Memory.truncate_tool_result("short content", "browser_view")
        assert result == "short content"

    def test_browser_view_truncated(self):
        """浏览器查看类工具超限时保留关键操作字段,截断大体积辅助字段"""
        long_content = json.dumps({
            "screenshot": "http://example.com/shot.png",
            "page_state": {"url": "https://example.com", "title": "Example"},
            "interactive_elements": [f"{i}: <a>link{i}</a>" for i in range(200)],
            "ref_map": [f"[@e{i}] <a>link{i}</a>" for i in range(200)],
            "content": "x" * 20000,
            "accessibility_tree": "y" * 5000,
            "pending_dialogs": [],
            "dialog_history": [],
            "snapshot_version": 1,
        })
        result = Memory.truncate_tool_result(long_content, "browser_view")
        assert len(result) < len(long_content)
        # screenshot 标记为 [attached]
        assert "screenshot" in result
        # interactive_elements 和 ref_map 必须保留(LLM操作依据)
        assert "interactive_elements" in result
        assert "ref_map" in result
        # page_state 必须完整保留
        assert "example.com" in result
        # 解析JSON验证结构完整性
        parsed = json.loads(result)
        assert isinstance(parsed.get("interactive_elements"), list)
        assert isinstance(parsed.get("ref_map"), list)
        assert parsed["page_state"]["url"] == "https://example.com"

    def test_browser_view_preserves_elements_when_content_large(self):
        """content超长时优先截断content,完整保留interactive_elements和ref_map"""
        elements = [f"{i}: <a>link{i}</a>" for i in range(50)]
        ref_map = [f"[@e{i}] <a>link{i}</a>" for i in range(50)]
        long_content = json.dumps({
            "screenshot": "base64data",
            "page_state": {"url": "https://example.com", "title": "Test"},
            "interactive_elements": elements,
            "ref_map": ref_map,
            "content": "x" * 20000,
            "pending_dialogs": [],
            "dialog_history": [],
            "snapshot_version": 5,
        })
        result = Memory.truncate_tool_result(long_content, "browser_view")
        parsed = json.loads(result)
        # interactive_elements 和 ref_map 完整保留
        assert len(parsed["interactive_elements"]) == 50
        assert len(parsed["ref_map"]) == 50
        # content 被截断(措辞中性化: "preview"而非"truncated",减少LLM压缩焦虑)
        assert "preview" in parsed.get("content", "")
        # 截断后总长度不超限
        assert len(result) <= 12000 + 100  # 允许少量超出(截断标记)

    def test_browser_view_truncates_elements_when_extremely_large(self):
        """interactive_elements自身超限时按尾部截断(优先丢弃offscreen元素)"""
        # 构造超大的interactive_elements,使基础+elements就超限
        elements = [f"{i}: <a>{'x' * 200}</a>" for i in range(500)]
        long_content = json.dumps({
            "screenshot": "base64data",
            "page_state": {"url": "https://example.com", "title": "Huge"},
            "interactive_elements": elements,
            "ref_map": [],
            "content": "",
            "pending_dialogs": [],
            "dialog_history": [],
            "snapshot_version": 1,
        })
        result = Memory.truncate_tool_result(long_content, "browser_view")
        parsed = json.loads(result)
        # interactive_elements 被截断但保留前N个(视口内可见元素优先)
        assert len(parsed["interactive_elements"]) < 500
        assert len(parsed["interactive_elements"]) > 0
        # 截断标记存在
        assert any("truncated" in str(item) for item in parsed["interactive_elements"])

    def test_browser_view_content_preserved_when_elements_huge(self):
        """content保底预算: interactive_elements超大时content仍被保留(会话cd71121a根因)

        场景: VitePress页面有389个交互元素(116KB JSON),DOM文本118KB。
        旧版截断逻辑先添加interactive_elements,占满12KB预算后break,
        content字段被完全丢弃,LLM误判"browser_view返回空结果"。
        修复: 为content预留25%保底预算,确保LLM始终获得页面文本。
        """
        # 模拟VitePress页面: 超大interactive_elements + 超大content
        elements = [{"index": i, "tag": "a", "text": f"link{i}", "selector": f"[data-manus-id=\"manus-element-{i}\"]"} for i in range(389)]
        large_content = json.dumps({
            "screenshot": "base64data",
            "page_state": {"url": "https://element-plus.org/zh-CN/component/form", "title": "Form"},
            "interactive_elements": elements,
            "ref_map": [],
            "content": "x" * 118608,  # 118KB DOM文本
            "pending_dialogs": [],
            "dialog_history": [],
            "snapshot_version": 2,
        })
        result = Memory.truncate_tool_result(large_content, "browser_view")
        parsed = json.loads(result)

        # 核心断言: content字段必须存在且非空(旧版被break丢弃)
        assert "content" in parsed, "content字段被截断丢弃(会话cd71121a根因)"
        assert len(parsed["content"]) > 100, f"content过短: {len(parsed['content'])}"

        # interactive_elements也必须存在(可能被截断但非空)
        assert "interactive_elements" in parsed
        assert len(parsed["interactive_elements"]) > 0

        # 截断后总长度不超限
        assert len(result) <= 12000 + 100

        # page_state完整保留
        assert parsed["page_state"]["url"] == "https://element-plus.org/zh-CN/component/form"

    def test_browser_view_content_empty_all_budget_to_elements(self):
        """content为空时,全部预算分配给interactive_elements(无浪费)"""
        elements = [{"index": i, "tag": "a", "text": f"link{i}"} for i in range(500)]
        large_content = json.dumps({
            "screenshot": "base64data",
            "page_state": {"url": "https://example.com", "title": "Test"},
            "interactive_elements": elements,
            "ref_map": [],
            "content": "",
            "pending_dialogs": [],
            "dialog_history": [],
            "snapshot_version": 1,
        })
        result = Memory.truncate_tool_result(large_content, "browser_view")
        parsed = json.loads(result)

        # content为空但字段存在
        assert parsed["content"] == ""
        # interactive_elements获得全部预算,保留更多元素
        assert len(parsed["interactive_elements"]) > 0
        assert len(parsed["interactive_elements"]) < 500

    def test_browser_view_ref_map_preserved_when_elements_huge(self):
        """ref_map保底预算: interactive_elements超大时ref_map仍被保留

        会话b143f0be根因: 旧版截断逻辑interactive_elements超限时break,
        ref_map被完全丢弃,LLM看到interactive_elements但无法用@eN点击。
        修复: 为ref_map预留20%保底预算,确保LLM始终能解析ref引用。
        """
        # 构造: 超大interactive_elements + 非空ref_map + 超大content
        elements = [f"{i}: <a>{'x' * 200}</a>" for i in range(500)]
        ref_map = [f"@e{i}: a[link{i}]" for i in range(200)]
        large_content = json.dumps({
            "screenshot": "base64data",
            "page_state": {"url": "https://example.com", "title": "Huge"},
            "interactive_elements": elements,
            "ref_map": ref_map,
            "content": "x" * 50000,
            "pending_dialogs": [],
            "dialog_history": [],
            "snapshot_version": 1,
        })
        result = Memory.truncate_tool_result(large_content, "browser_view")
        parsed = json.loads(result)

        # 核心断言: ref_map必须存在且非空(旧版被break完全丢弃)
        assert "ref_map" in parsed, "ref_map被截断丢弃(会话b143f0be根因)"
        assert len(parsed["ref_map"]) > 0, "ref_map不应为空"

        # interactive_elements也被保留(可能被截断但非空)
        assert "interactive_elements" in parsed
        assert len(parsed["interactive_elements"]) > 0
        assert len(parsed["interactive_elements"]) < 500, "interactive_elements应被截断"

        # content也被保留(保底预算)
        assert "content" in parsed
        assert len(parsed["content"]) > 0

        # 截断后总长度不超限
        assert len(result) <= 12000 + 100

    def test_file_result_truncated(self):
        long_content = json.dumps({
            "filepath": "/tmp/large_file.txt",
            "content": "x" * 10000,
        })
        result = Memory.truncate_tool_result(long_content, "file_read")
        assert len(result) < len(long_content)
        assert "truncated" in result

    def test_generic_truncation(self):
        long_content = "x" * 10000
        result = Memory.truncate_tool_result(long_content, "unknown_tool")
        assert "truncated" in result

    def test_non_string_content_unchanged(self):
        result = Memory.truncate_tool_result(12345, "browser_view")
        assert result == 12345


class TestDynamicTruncationExpansion:
    """动态截断双向伸缩测试(会话7720e91d根因修复)

    页面快照遵循"只在当前会话临时存在"原则: 上下文充足时向上扩展阈值,
    让LLM看到完整interactive_elements; 紧张时向下缩减保障不溢出。
    """

    def _make_mock_token_counter(self, current_tokens: int):
        """构造模拟token计数器,返回预设token数"""
        class _MockCounter:
            def count_messages(self, messages):
                return current_tokens
        return _MockCounter()

    def _make_browser_view_content(self, elements_count: int, content_len: int = 1000):
        """构造browser_view结果JSON(含interactive_elements和content)"""
        return json.dumps({
            "success": True,
            "data": {
                "page_state": {"url": "https://test.com", "title": "Test"},
                "content": "x" * content_len,
                "interactive_elements": [
                    f"{i}: <a>link{i}</a>" for i in range(elements_count)
                ],
                "ref_map": [f"@e{i}: a[link{i}]" for i in range(elements_count)],
            },
        })

    def test_expand_preserves_elements_when_abundant(self):
        """剩余>70%时阈值扩展为2倍,interactive_elements不被截断

        会话7720e91d根因: 80个元素(每行~20字符=1600字符)在标准12000阈值下
        被content(25%预算=3000)挤压后截断。扩展到24000后元素列表完整保留。
        """
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        # 80个元素(总~1600字符)+ 1000字符content = 适中的browser_view结果
        large_content = self._make_browser_view_content(elements_count=80, content_len=1000)
        # 剩余90%(current_tokens=100, context_window=1000)→扩展2x=24000
        counter = self._make_mock_token_counter(100)
        result = mem.truncate_tool_result_dynamic(
            large_content, "browser_view",
            token_counter=counter, context_window=1000,
        )
        parsed = json.loads(result)
        # interactive_elements应完整保留(80个元素不丢失)
        assert len(parsed["interactive_elements"]) == 80, \
            "上下文充足时interactive_elements应完整保留"

    def test_expand_1_5x_preserves_elements_when_moderate(self):
        """剩余50%-70%时阈值扩展为1.5倍,interactive_elements完整保留"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        large_content = self._make_browser_view_content(elements_count=50, content_len=1000)
        # 剩余60%(current_tokens=400, context_window=1000)→扩展1.5x=18000
        counter = self._make_mock_token_counter(400)
        result = mem.truncate_tool_result_dynamic(
            large_content, "browser_view",
            token_counter=counter, context_window=1000,
        )
        parsed = json.loads(result)
        assert len(parsed["interactive_elements"]) == 50, \
            "上下文较充足时interactive_elements应完整保留"

    def test_295_elements_preserved_when_abundant(self):
        """295元素(Element Plus文档页)在上下文充足时完整保留

        核心回归测试(会话9b0bf463): 旧版源头_MAX_VISIBLE_ELEMENTS=80截断295元素,
        导致"215 more visible elements omitted"。移除源头限流后,动态截断在上下文
        充足时(2x扩展=24000字符)应完整保留全部295个元素。
        """
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        # 295个元素(模拟Element Plus Form表单文档页)+ 适中content
        large_content = self._make_browser_view_content(elements_count=295, content_len=2000)
        # 剩余90%(current_tokens=100, context_window=1000)→扩展2x=24000
        counter = self._make_mock_token_counter(100)
        result = mem.truncate_tool_result_dynamic(
            large_content, "browser_view",
            token_counter=counter, context_window=1000,
        )
        parsed = json.loads(result)
        # 295个元素应完整保留(2x扩展阈值足够容纳)
        assert len(parsed["interactive_elements"]) == 295, \
            "上下文充足时295元素(Element Plus文档页)应完整保留,不应被截断"

    def test_shrink_truncates_elements_when_tight(self):
        """剩余<20%时阈值缩减为1/4,interactive_elements被截断"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        # 构造大量元素(200个),在3000字符阈值下必然被截断
        large_content = self._make_browser_view_content(elements_count=200, content_len=500)
        # 剩余10%(current_tokens=900, context_window=1000)→缩减1/4=3000
        counter = self._make_mock_token_counter(900)
        result = mem.truncate_tool_result_dynamic(
            large_content, "browser_view",
            token_counter=counter, context_window=1000,
        )
        parsed = json.loads(result)
        # 200个元素在3000阈值下应被截断
        assert len(parsed["interactive_elements"]) < 200, \
            "上下文紧张时interactive_elements应被截断"

    def test_no_counter_falls_back_to_standard(self):
        """无token_counter时降级为标准阈值(向后兼容)"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        # 500个元素+ref_map(总~17500字符)在标准12000阈值下必然截断
        large_content = self._make_browser_view_content(elements_count=500, content_len=5000)
        result = mem.truncate_tool_result_dynamic(large_content, "browser_view")
        parsed = json.loads(result)
        # 标准阈值12000下,500个元素应被截断
        assert len(parsed["interactive_elements"]) < 500, \
            "无token_counter时应降级为标准阈值截断"

    def test_adaptive_budget_text_llm_gets_more_content(self):
        """文本LLM(supports_images=False)+大content(>8KB)获得更多content预算(40% vs 25%)

        会话437cbc75根因修复: deepseek-v4-flash不支持图像输入,截图为[attached]占位符
        零信息价值。content字段成为LLM理解页面内容的唯一通道。
        当content>8KB(企业App DOM树提取)时,content预算从25%提升到40%。
        """
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        # 大content(20000字符,>8KB阈值)+ 少量元素,触发content截断
        large_content = self._make_browser_view_content(elements_count=20, content_len=20000)

        # 文本LLM: supports_images=False, content>8KB → content预算40%
        result_text = Memory._truncate_content_internal(
            large_content, "browser_view", max_len=10000, supports_images=False,
        )
        parsed_text = json.loads(result_text)

        # 多模态LLM: supports_images=True → content预算25%
        result_mm = Memory._truncate_content_internal(
            large_content, "browser_view", max_len=10000, supports_images=True,
        )
        parsed_mm = json.loads(result_mm)

        # 文本LLM的content应比多模态LLM更长(40% > 25%)
        text_content_len = len(parsed_text.get("content", ""))
        mm_content_len = len(parsed_mm.get("content", ""))
        assert text_content_len > mm_content_len, \
            f"文本LLM content({text_content_len})应大于多模态LLM({mm_content_len})"
        # 文本LLM content不应被完全截断(40%预算≈4000字符)
        assert "preview" in parsed_text.get("content", ""), \
            "20000字符content在10000阈值下应触发截断标记"

    def test_adaptive_budget_small_content_keeps_elements_budget(self):
        """文本LLM + 小content(≤8KB,文档页)保持25% content预算,elements获更多预算

        会话81c801c5根因修复: Element Plus文档页content小(文档容器提取≤8KB)但元素多(295个),
        若content预算固定40%则elements预算从55%降到40%导致元素截断→"被压缩"。
        自适应策略: content≤8KB时content预算保持25%,elements获55%预算,避免元素截断。
        """
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        # 小content(5000字符,≤8KB阈值)+ 大量元素(295个,模拟Element Plus文档页)
        small_content = self._make_browser_view_content(elements_count=295, content_len=5000)

        # 文本LLM: supports_images=False, content≤8KB → content预算25%(不是40%)
        result_text = Memory._truncate_content_internal(
            small_content, "browser_view", max_len=10000, supports_images=False,
        )
        parsed_text = json.loads(result_text)

        # 多模态LLM: supports_images=True → content预算25%
        result_mm = Memory._truncate_content_internal(
            small_content, "browser_view", max_len=10000, supports_images=True,
        )
        parsed_mm = json.loads(result_mm)

        # 文本LLM和多模态LLM的content预算应相同(都是25%,因content≤8KB)
        text_content_len = len(parsed_text.get("content", ""))
        mm_content_len = len(parsed_mm.get("content", ""))
        assert text_content_len == mm_content_len, \
            f"小content时文本LLM({text_content_len})应与多模态LLM({mm_content_len})预算相同"
        # 文本LLM的interactive_elements不应比多模态LLM少(都是55%预算)
        text_elem_count = len(parsed_text.get("interactive_elements", []))
        mm_elem_count = len(parsed_mm.get("interactive_elements", []))
        assert text_elem_count == mm_elem_count, \
            f"小content时元素数应相同(text={text_elem_count}, mm={mm_elem_count})"

    def test_adaptive_budget_default_is_multimodal(self):
        """默认supports_images=True(向后兼容),content预算为25%"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        large_content = self._make_browser_view_content(elements_count=20, content_len=20000)
        # 不传supports_images,默认True(多模态)
        result_default = Memory._truncate_content_internal(
            large_content, "browser_view", max_len=10000,
        )
        result_explicit = Memory._truncate_content_internal(
            large_content, "browser_view", max_len=10000, supports_images=True,
        )
        # 默认行为应与显式supports_images=True一致
        assert len(json.loads(result_default).get("content", "")) == \
               len(json.loads(result_explicit).get("content", "")), \
            "默认supports_images应与显式True一致(向后兼容)"


class TestContentTruncatedFlag:
    """content_truncated标记测试(方案A/会话437cbc75根因修复)

    content被截断时注入content_truncated=true,解锁console_exec护栏,
    允许LLM用console_exec补偿提取被截断的表格/弹窗文本。
    标记始终存在(True/False),避免LLM因字段缺失误判页面未加载。
    """

    def _make_browser_view_content(self, elements_count: int = 10, content_len: int = 1000):
        """构造browser_view结果JSON"""
        return json.dumps({
            "success": True,
            "data": {
                "page_state": {"url": "https://test.com", "title": "Test"},
                "content": "x" * content_len,
                "interactive_elements": [
                    f"{i}: <a>link{i}</a>" for i in range(elements_count)
                ],
                "ref_map": [f"@e{i}: a[link{i}]" for i in range(elements_count)],
                "element_summary": {
                    "visible": elements_count,
                    "offscreen": 0,
                    "total": elements_count,
                },
            },
        })

    def test_truncated_when_content_exceeds_budget(self):
        """content超预算截断时,content_truncated=True"""
        large_content = self._make_browser_view_content(elements_count=10, content_len=20000)
        result = Memory._truncate_content_internal(
            large_content, "browser_view", max_len=10000, supports_images=False,
        )
        parsed = json.loads(result)
        assert parsed.get("content_truncated") is True, \
            "content被截断时content_truncated应为True"
        assert "preview" in parsed.get("content", ""), \
            "content应包含截断标记(措辞中性化: preview)"

    def test_not_truncated_when_content_within_budget(self):
        """content在预算内未截断时,content_truncated=False"""
        small_content = self._make_browser_view_content(elements_count=5, content_len=500)
        result = Memory._truncate_content_internal(
            small_content, "browser_view", max_len=10000, supports_images=False,
        )
        parsed = json.loads(result)
        assert parsed.get("content_truncated") is False, \
            "content未截断时content_truncated应为False"
        assert "preview" not in parsed.get("content", ""), \
            "content不应包含截断标记(未截断时无preview标记)"

    def test_not_truncated_when_content_empty(self):
        """content为空时,content_truncated=False(空内容非截断)"""
        empty_content = json.dumps({
            "success": True,
            "data": {
                "page_state": {"url": "https://test.com", "title": "Test"},
                "content": "",
                "interactive_elements": ["0: <a>link</a>"],
                "ref_map": ["@e0: a[link]"],
            },
        })
        result = Memory._truncate_content_internal(
            empty_content, "browser_view", max_len=10000, supports_images=False,
        )
        parsed = json.loads(result)
        assert parsed.get("content_truncated") is False, \
            "空内容时content_truncated应为False(非截断,是页面本身无文本)"
        assert parsed.get("content") == "", "空内容应保持为空字符串"

    def test_flag_always_present_in_browser_view_result(self):
        """browser_view截断结果始终包含content_truncated字段(不因截断而丢失)"""
        # 无论content是否被截断,content_truncated字段都必须存在
        for content_len in [0, 100, 5000, 20000]:
            content = self._make_browser_view_content(elements_count=10, content_len=content_len)
            result = Memory._truncate_content_internal(
                content, "browser_view", max_len=10000, supports_images=False,
            )
            parsed = json.loads(result)
            assert "content_truncated" in parsed, \
                f"content_len={content_len}: content_truncated字段必须始终存在"

    def test_element_summary_preserved_in_truncated_result(self):
        """element_summary在截断结果中保留(会话1a002224根因修复)

        旧版_truncate_browser_view_result创建新result dict时未复制element_summary,
        LLM不知道interactive_elements列表是否完整,看到content被截断后误判"整个
        页面被压缩"。保留element_summary后LLM可确认"128 total,全部在列表中"。
        """
        large_content = self._make_browser_view_content(elements_count=128, content_len=30000)
        result = Memory._truncate_content_internal(
            large_content, "browser_view", max_len=12000, supports_images=False,
        )
        parsed = json.loads(result)
        # element_summary必须保留(含visible/offscreen/total计数)
        assert "element_summary" in parsed, \
            "element_summary必须在截断结果中保留(让LLM知道元素总数)"
        summary = parsed["element_summary"]
        assert summary.get("total") == 128, \
            f"element_summary.total应为128,实际={summary.get('total')}"
        assert "visible" in summary, "element_summary应包含visible字段"
        assert "offscreen" in summary, "element_summary应包含offscreen字段"

    def test_element_summary_preserved_even_when_empty(self):
        """element_summary为空时仍保留字段(空字典而非缺失)"""
        content_without_summary = json.dumps({
            "success": True,
            "data": {
                "page_state": {"url": "https://test.com", "title": "Test"},
                "content": "x" * 100,
                "interactive_elements": ["0: <a>link</a>"],
                "ref_map": ["@e0: a[link]"],
            },
        })
        result = Memory._truncate_content_internal(
            content_without_summary, "browser_view", max_len=10000, supports_images=False,
        )
        parsed = json.loads(result)
        # 即使原始数据没有element_summary,截断结果也应包含该字段(空字典)
        assert "element_summary" in parsed, \
            "element_summary字段必须始终存在(即使原始数据无此字段)"

    def test_content_truncation_marker_guides_to_interactive_elements(self):
        """content截断标记引导LLM转向interactive_elements(会话1a002224根因修复)

        旧标记"...(truncated)"仅告知内容被截断,LLM误判"整个页面被压缩"。
        新标记明确区分: content文本被截断 ≠ interactive_elements被截断,
        引导LLM直接转向interactive_elements列表操作或用console_exec提取文本。
        """
        large_content = self._make_browser_view_content(elements_count=10, content_len=20000)
        result = Memory._truncate_content_internal(
            large_content, "browser_view", max_len=10000, supports_images=False,
        )
        parsed = json.loads(result)
        content = parsed.get("content", "")
        # 截断标记应包含引导信息(非仅"...(truncated)")
        assert "interactive_elements" in content, \
            "截断标记应引导LLM转向interactive_elements(而非仅说truncated)"
        assert "complete" in content or " intact" in content, \
            "截断标记应告知interactive_elements完整可用"

    def test_viewport_priority_preserves_dialog_elements(self):
        """视口优先截断: 预算紧张时优先保留visible组(含Dialog表单元素),截断offscreen组

        会话6a6a0d05根因修复: Element Plus Dialog通过Vue teleport渲染到body末尾,
        Dialog表单元素(promotion name/zones)位于interactive_elements的visible组
        (因inDialog=True,P1优先级)。旧版_truncate_field_to_fit盲目二分保留头部,
        当offscreen元素也占用预算时,visible组被截断,Dialog表单元素丢失,LLM无法
        用@eN ref操作→退化为console_exec探测。
        视口优先截断识别_format_elements输出的offscreen分隔行,优先保留visible组全部。
        """
        # 模拟_format_elements输出: P1弹窗(Dialog) → P2普通(visible) → offscreen分隔 → offscreen元素
        visible_elements = [
            "0: <input>[Label:Promotion name] [text]</input> [dialog]",
            "1: <select>[Zones]</select> [dialog]",
            "2: <button>Open a Form nested Dialog</button> [dialog]",
        ]
        # P2普通元素(侧边栏导航,visible)
        visible_elements.extend(f"{i}: <a>nav-link{i}</a>" for i in range(3, 53))
        # offscreen分隔行 + offscreen元素(页脚)
        offscreen_separator = "--- offscreen elements (40 total, showing 40) ---"
        offscreen_elements = [
            f"{i}: <a>footer-link{i}</a> [offscreen]" for i in range(53, 93)
        ]
        elements = visible_elements + [offscreen_separator] + offscreen_elements

        # 构造result dict(模拟截断中间状态: content已处理,现截断interactive_elements)
        result = {
            "screenshot": "",
            "page_state": {"url": "https://element-plus.org/zh-CN/component/dialog", "title": "Dialog"},
            "element_summary": {"visible": 50, "offscreen": 40, "total": 90},
            "content": "页面文本内容",
            "content_truncated": False,
            "interactive_elements": elements,
            "ref_map": [],
        }
        # 紧张预算: visible组(~2000字符)可全保留,offscreen组需截断
        truncated = Memory._truncate_interactive_elements(result, elements, max_len=3000)

        truncated_str = json.dumps(truncated, ensure_ascii=False)
        # Dialog表单元素必须完整保留(视口优先截断保护visible组)
        assert "Promotion name" in truncated_str, \
            "Dialog表单元素(Promotion name)必须保留(视口优先截断保护visible组)"
        assert "[dialog]" in truncated_str, "Dialog弹窗标记元素必须保留"
        assert "Zones" in truncated_str, "Dialog表单元素(Zones)必须保留"
        # offscreen元素应被部分截断(预算紧张时优先丢弃)
        footer_count = truncated_str.count("footer-link")
        assert footer_count < 40, \
            f"offscreen页脚元素应被部分截断(40个中仅保留部分),实际保留{footer_count}"
        # offscreen分隔行应保留(让LLM知道存在offscreen元素)
        assert any(isinstance(item, str) and item.startswith("--- offscreen elements") for item in truncated), \
            "offscreen分隔行应保留(让LLM感知offscreen元素存在)"
        # 截断标记应存在(措辞中性化: "below viewport"而非"truncated")
        assert any("below viewport" in str(item) for item in truncated), "截断标记应存在"

    def test_viewport_priority_falls_back_without_separator(self):
        """无offscreen分隔行时退化为通用_truncate_field_to_fit(向后兼容)

        旧格式/测试用例的interactive_elements无"--- offscreen elements"分隔行
        (如全部visible或未分区),视口优先截断应退化为通用截断,行为与之前一致。
        """
        # 无offscreen分隔行的元素列表(旧格式)
        elements = [f"{i}: <a>link{i}</a>" for i in range(200)]
        result = {
            "page_state": {"url": "https://test.com", "title": "Test"},
            "interactive_elements": elements,
        }
        truncated = Memory._truncate_interactive_elements(result, elements, max_len=2000)
        # 应被截断(200个元素超过预算)
        assert len(truncated) < 200
        assert len(truncated) > 0
        # 截断标记存在(通用截断的标记格式)
        assert any("truncated" in str(item) for item in truncated)

    def test_viewport_priority_preserves_all_when_budget_sufficient(self):
        """预算充足时visible组和offscreen组全部保留(不截断)"""
        visible_elements = [
            "0: <input>[Label:Promotion name] [text]</input> [dialog]",
            "1: <button>Submit</button>",
        ]
        offscreen_separator = "--- offscreen elements (3 total, showing 3) ---"
        offscreen_elements = [
            "2: <a>footer-1</a> [offscreen]",
            "3: <a>footer-2</a> [offscreen]",
            "4: <a>footer-3</a> [offscreen]",
        ]
        elements = visible_elements + [offscreen_separator] + offscreen_elements
        result = {
            "page_state": {"url": "https://test.com", "title": "Test"},
            "interactive_elements": elements,
        }
        # 充足预算: 全部保留
        truncated = Memory._truncate_interactive_elements(result, elements, max_len=5000)
        # 完整保留(无截断标记)
        assert len(truncated) == len(elements)
        assert not any("truncated" in str(item) for item in truncated)

    def test_viewport_priority_visible_overflow_preserves_head(self):
        """visible组自身超预算时保留头部(P0状态/P1弹窗元素优先)

        极端场景: visible元素数量极大,即使丢弃全部offscreen仍超预算。
        此时二分截断visible组,保留头部(P0状态/P1弹窗优先级元素),尾部P2普通元素被丢弃。
        _format_elements已按P0>P1>P2排序,保留头部即优先保留Dialog弹窗内表单元素。
        """
        # visible组超大: P1弹窗元素在前 + 大量P2普通元素
        visible_elements = ["0: <input>[Label:Promotion name] [text]</input> [dialog]"]
        visible_elements.extend(
            f"{i}: <a>{'x' * 200}</a>" for i in range(1, 201)  # 每行~210字符,200行~42KB
        )
        offscreen_separator = "--- offscreen elements (10 total, showing 10) ---"
        offscreen_elements = [f"{i}: <a>off{i}</a> [offscreen]" for i in range(201, 211)]
        elements = visible_elements + [offscreen_separator] + offscreen_elements
        result = {
            "page_state": {"url": "https://test.com", "title": "Huge"},
            "interactive_elements": elements,
        }
        # 极小预算: visible组自身就超预算
        truncated = Memory._truncate_interactive_elements(result, elements, max_len=3000)
        truncated_str = json.dumps(truncated, ensure_ascii=False)
        # P1弹窗元素(头部)必须保留
        assert "Promotion name" in truncated_str, \
            "visible组超预算时,P1弹窗元素(头部)应优先保留"
        # 应有visible截断标记(措辞中性化: "below viewport")
        assert any("visible elements below viewport" in str(item) for item in truncated), \
            "visible组截断时应附加visible截断标记"


class TestStripImageData:
    """图片数据清理测试"""

    def test_base64_image_replaced(self):
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR..."}},
            ]},
        ]
        cleaned = Memory.strip_image_data(messages)
        content = cleaned[0]["content"]
        assert any(item.get("type") == "text" and "removed" in item.get("text", "") for item in content)

    def test_url_image_kept(self):
        messages = [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "http://example.com/img.png"}},
            ]},
        ]
        cleaned = Memory.strip_image_data(messages)
        content = cleaned[0]["content"]
        assert content[0]["type"] == "image_url"

    def test_text_only_messages_unchanged(self):
        messages = [{"role": "user", "content": "hello"}]
        cleaned = Memory.strip_image_data(messages)
        assert cleaned[0]["content"] == "hello"


class TestKeyFacts:
    """关键事实提取测试"""

    def test_extract_url_from_browser(self):
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({"page_state": {"url": "https://github.com", "title": "GitHub"}}),
        })
        facts = mem.extract_key_facts()
        assert any(f.category == "url" and "github.com" in f.content for f in facts)

    def test_extract_file_from_tool(self):
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "file_read",
            "content": json.dumps({"filepath": "/tmp/data.csv", "content": "1,2,3"}),
        })
        facts = mem.extract_key_facts()
        assert any(f.category == "file" and "/tmp/data.csv" in f.content for f in facts)

    def test_extract_command_from_shell(self):
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "shell_exec",
            "content": json.dumps({"command": "pip install requests", "output": "ok"}),
        })
        facts = mem.extract_key_facts()
        assert any(f.category == "cmd" for f in facts)

    def test_key_facts_deduplication(self):
        mem = Memory()
        for _ in range(3):
            mem.add_message({
                "role": "tool",
                "function_name": "browser_view",
                "content": json.dumps({"page_state": {"url": "https://same.com"}}),
            })
        facts = mem.extract_key_facts()
        url_facts = [f for f in facts if f.category == "url"]
        assert len(url_facts) == 1

    def test_key_facts_max_limit(self):
        mem = Memory()
        for i in range(20):
            mem.add_message({
                "role": "tool",
                "function_name": "browser_view",
                "content": json.dumps({"page_state": {"url": f"https://page{i}.com"}}),
            })
        facts = mem.extract_key_facts()
        assert len(facts) <= 10

    def test_get_key_facts_text(self):
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({"page_state": {"url": "https://test.com"}}),
        })
        mem.extract_key_facts()
        text = mem.get_key_facts_text()
        assert "test.com" in text

    def test_get_summary_for_injection(self):
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "请帮我搜索Python教程"})
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({"page_state": {"url": "https://python.org"}}),
        })
        mem.extract_key_facts()
        summary = mem.get_summary_for_injection()
        assert "python.org" in summary
        assert "Python教程" in summary


class TestSessionSummary:
    """P1 - 会话进展摘要累积器测试"""

    def test_summary_accumulates_across_compacts(self):
        """多次压缩后摘要应累积，而非覆盖"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "搜索Python教程"})
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({"page_state": {"url": "https://python.org", "title": "Python"}}),
        })
        mem.compact()
        first_summary = mem.session_summary
        assert first_summary != ""
        assert "python.org" in first_summary

        # 第二次压缩前追加新操作
        mem.add_message({
            "role": "tool",
            "function_name": "shell_exec",
            "content": json.dumps({"console": [{"command": "pip install requests", "output": "ok"}]}),
        })
        mem.compact()
        assert "python.org" in mem.session_summary  # 旧摘要保留
        assert "pip install requests" in mem.session_summary  # 新操作追加
        assert len(mem.session_summary) > len(first_summary)

    def test_summary_truncated_when_exceeds_max(self):
        """摘要超长时保留尾部（最新进展更重要）"""
        from app.domain.models.memory import _SESSION_SUMMARY_MAX
        mem = Memory()
        mem.session_summary = "x" * (_SESSION_SUMMARY_MAX + 500)
        mem._append_to_session_summary("new_operation")
        assert len(mem.session_summary) <= _SESSION_SUMMARY_MAX
        assert mem.session_summary.endswith("new_operation")

    def test_summary_injected_into_system_prompt(self):
        """get_summary_for_injection 应优先注入会话进展摘要"""
        mem = Memory()
        mem.session_summary = "访问页面: https://example.com | 执行命令: ls"
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "测试需求内容较长"})
        summary = mem.get_summary_for_injection()
        assert "[会话进展摘要]" in summary
        assert "example.com" in summary

    def test_summary_injection_truncated_when_too_long(self):
        """注入时摘要超长应截断保留尾部"""
        from app.domain.models.memory import _SESSION_SUMMARY_INJECT_MAX
        mem = Memory()
        long_summary = "a" * (_SESSION_SUMMARY_INJECT_MAX + 200) + "TAIL_MARKER"
        mem.session_summary = long_summary
        mem.add_message({"role": "system", "content": "sys"})
        summary = mem.get_summary_for_injection()
        assert "TAIL_MARKER" in summary
        # 摘要部分不应超过注入上限 + 标签开销
        assert summary.count("a") < len(long_summary)

    def test_summary_preserved_after_emergency_compact(self):
        """emergency_compact 后摘要应保留并注入到 summary_msg"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "system", "content": "sys2"})
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({"page_state": {"url": "https://test.com", "title": "Test"}}),
        })
        for i in range(20):
            mem.add_message({"role": "user", "content": f"msg{i}"})
        mem.add_message({"role": "assistant", "content": "last"})
        mem.add_message({"role": "assistant", "content": "last2"})
        mem.add_message({"role": "assistant", "content": "last3"})
        mem.add_message({"role": "assistant", "content": "last4"})

        mem.emergency_compact()
        assert mem.session_summary != ""
        assert "test.com" in mem.session_summary
        # summary_msg 应包含会话进展摘要
        summary_msgs = [m for m in mem.messages if "上下文紧急压缩" in m.get("content", "")]
        assert len(summary_msgs) >= 1
        assert "会话进展摘要" in summary_msgs[0]["content"]

    def test_summary_preserved_after_emergency_with_user_requirement(self):
        """紧急压缩后摘要和用户需求应保留并注入到 summary_msg

        Phase E: 合并原minimal_compact能力到emergency_compact，验证用户需求保留。
        """
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "请帮我分析数据并生成详细报告"})
        mem.add_message({
            "role": "tool",
            "function_name": "file_write",
            "content": json.dumps({"filepath": "/tmp/report.md"}),
        })
        for i in range(30):
            mem.add_message({"role": "tool", "function_name": "shell_exec", "content": "data"})

        mem.emergency_compact()
        assert mem.session_summary != ""
        assert "/tmp/report.md" in mem.session_summary
        # summary_msg 应包含会话进展摘要(Phase E: 紧急压缩统一标记)
        summary_msgs = [m for m in mem.messages if "上下文紧急压缩" in m.get("content", "")]
        assert len(summary_msgs) >= 1
        assert "会话进展摘要" in summary_msgs[0]["content"]

    def test_metrics_updated_after_compact(self):
        """压缩后 metrics 应更新 last_compression_level 和 session_summary_chars"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({"page_state": {"url": "https://test.com"}}),
        })
        mem.compact()
        assert mem.metrics.last_compression_level == CompressionLevel.NORMAL
        assert mem.metrics.session_summary_chars > 0

    def test_old_memory_without_summary_deserializes(self):
        """向后兼容: 旧DB记录无 session_summary 字段时应正常反序列化

        Phase E: 移除 aggressive_count/minimal_count 后，旧DB记录中的这些字段
        应被pydantic忽略(默认值0)，不影响反序列化。
        """
        old_data = {
            "messages": [{"role": "system", "content": "sys"}],
            "key_facts": [{"category": "url", "content": "https://old.com", "importance": 0.7}],
            "metrics": {
                "message_count": 1,
                "compact_count": 0,
                "emergency_count": 0,
            },
        }
        mem = Memory(**old_data)
        assert mem.session_summary == ""
        assert mem.key_facts[0].timestamp is None
        assert mem.metrics.last_compression_level == 0
        assert mem.metrics.session_summary_chars == 0


class TestEnhancedKeyFacts:
    """P2 - key_facts 增强分类与时间戳测试"""

    def test_error_fact_extracted_from_failed_tool(self):
        """失败工具调用应提取为 error 类别"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "shell_exec",
            "content": json.dumps({"success": False, "message": "command not found"}),
        })
        facts = mem.extract_key_facts()
        error_facts = [f for f in facts if f.category == "error"]
        assert len(error_facts) >= 1
        assert "command not found" in error_facts[0].content

    def test_mcp_tool_fact_extracted(self):
        """MCP工具调用（mcp_前缀）应提取为 mcp_tool 类别"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "mcp_multimodal_vl_image_understand",
            "content": json.dumps({"success": True, "data": "result"}),
        })
        facts = mem.extract_key_facts()
        mcp_facts = [f for f in facts if f.category == "mcp_tool"]
        assert len(mcp_facts) >= 1
        assert "mcp_multimodal" in mcp_facts[0].content

    def test_page_title_fact_extracted(self):
        """browser_view 结果中的页面标题应提取为 page_title 类别"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({
                "page_state": {"url": "https://github.com", "title": "GitHub: Let's build from here"},
            }),
        })
        facts = mem.extract_key_facts()
        title_facts = [f for f in facts if f.category == "page_title"]
        assert len(title_facts) >= 1
        assert "GitHub" in title_facts[0].content

    def test_timestamp_set_on_new_facts(self):
        """新提取的 key_facts 应自动设置 ISO 时间戳"""
        mem = Memory()
        mem.add_message({"role": "user", "content": "请帮我搜索Python教程"})
        facts = mem.extract_key_facts()
        req_facts = [f for f in facts if f.category == "requirement"]
        assert len(req_facts) >= 1
        assert req_facts[0].timestamp is not None
        # 验证是 ISO 格式
        from datetime import datetime
        datetime.fromisoformat(req_facts[0].timestamp)

    def test_existing_facts_preserved_without_timestamp(self):
        """已存在无 timestamp 的 key_facts 应被保留，不补充 timestamp"""
        mem = Memory(
            messages=[{"role": "user", "content": "请分析数据并生成报告"}],
            key_facts=[KeyFact(category="file", content="/tmp/data.csv")],
        )
        facts = mem.extract_key_facts()
        file_facts = [f for f in facts if f.category == "file"]
        assert len(file_facts) >= 1
        assert file_facts[0].timestamp is None  # 保留原值

    def test_key_facts_text_includes_timestamp(self):
        """get_key_facts_text 应展示时间戳"""
        mem = Memory()
        mem.add_message({"role": "user", "content": "请帮我搜索Python教程"})
        mem.extract_key_facts()
        text = mem.get_key_facts_text()
        assert "(" in text  # 包含时间戳括号

    def test_error_and_success_not_both_extracted(self):
        """成功工具调用不应提取为 error"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "shell_exec",
            "content": json.dumps({"success": True, "console": [{"command": "ls", "output": "file.txt"}]}),
        })
        facts = mem.extract_key_facts()
        error_facts = [f for f in facts if f.category == "error"]
        assert len(error_facts) == 0


class TestCompressionSummaryBuilder:
    """P1 - 压缩摘要构建逻辑测试"""

    def test_browser_operation_summarized(self):
        """浏览器操作应被正确摘要"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({
                "page_state": {"url": "https://example.com", "title": "Example"},
            }),
        })
        summary = mem._build_compression_summary()
        assert "example.com" in summary
        assert "Example" in summary

    def test_browser_action_summarized(self):
        """浏览器操作（click等）应被摘要为操作名"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "browser_click",
            "content": "clicked button",
        })
        summary = mem._build_compression_summary()
        assert "browser_click" in summary

    def test_shell_command_summarized(self):
        """Shell命令应被摘要，包含命令内容"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "shell_exec",
            "content": json.dumps({
                "console": [{"command": "pip install requests", "output": "ok"}],
            }),
        })
        summary = mem._build_compression_summary()
        assert "pip install requests" in summary

    def test_file_operation_summarized(self):
        """文件操作应被摘要，包含文件路径"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "file_write",
            "content": json.dumps({"filepath": "/tmp/report.md", "content": "data"}),
        })
        summary = mem._build_compression_summary()
        assert "/tmp/report.md" in summary

    def test_mcp_tool_summarized(self):
        """MCP工具调用应被摘要"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "mcp_multimodal_ocr_extract",
            "content": json.dumps({"success": True, "data": "text"}),
        })
        summary = mem._build_compression_summary()
        assert "mcp_multimodal_ocr_extract" in summary

    def test_search_operation_summarized(self):
        """搜索操作应被摘要"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "search_web",
            "content": "search results...",
        })
        summary = mem._build_compression_summary()
        assert "search_web" in summary or "网络搜索" in summary

    def test_assistant_text_summarized(self):
        """assistant决策性文本应被摘要"""
        mem = Memory()
        mem.add_message({"role": "assistant", "content": "我决定使用方案A来处理这个问题，因为它更高效"})
        summary = mem._build_compression_summary()
        assert "AI:" in summary
        assert "方案A" in summary

    def test_empty_messages_produce_empty_summary(self):
        """空messages应产生空摘要"""
        mem = Memory()
        summary = mem._build_compression_summary()
        assert summary == ""

    def test_append_to_summary_preserves_existing(self):
        """追加摘要时应保留已有内容"""
        mem = Memory()
        mem.session_summary = "之前操作"
        mem._append_to_session_summary("新操作")
        assert "之前操作" in mem.session_summary
        assert "新操作" in mem.session_summary
        assert " -> " in mem.session_summary


class TestToolNameFix:
    """工具名常量修复测试 - 确保实际函数名被正确识别"""

    def test_shell_execute_in_shell_tools(self):
        """shell_execute 必须在 _SHELL_TOOLS 中（实际函数名，非旧名 shell_exec）"""
        assert "shell_execute" in _SHELL_TOOLS

    def test_read_file_in_file_tools(self):
        """read_file 必须在 _FILE_TOOLS 中（实际函数名，非旧名 file_read）"""
        assert "read_file" in _FILE_TOOLS

    def test_replace_in_file_in_file_tools(self):
        """replace_in_file 必须在 _FILE_TOOLS 中"""
        assert "replace_in_file" in _FILE_TOOLS

    def test_shell_execute_summarized_as_command(self):
        """shell_execute 应提取为 '执行命令: xxx'，而非退化 '工具调用: shell_execute'"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "shell_execute",
            "content": json.dumps({
                "console": [{"ps1": "$ ", "command": "ls -la", "output": "file1"}]
            }),
        })
        snippet = mem._summarize_tool_operation("shell_execute", mem.messages[0]["content"])
        assert "执行命令" in snippet
        assert "ls -la" in snippet
        assert "工具调用" not in snippet

    def test_read_file_summarized_as_file_op(self):
        """read_file 应提取为 '文件操作: xxx'，而非退化 '工具调用: read_file'"""
        mem = Memory()
        mem.add_message({
            "role": "tool",
            "function_name": "read_file",
            "content": json.dumps({"filepath": "/home/ubuntu/test.py", "content": "code"}),
        })
        snippet = mem._summarize_tool_operation("read_file", mem.messages[0]["content"])
        assert "文件操作" in snippet or "/home/ubuntu/test.py" in snippet
        assert "工具调用" not in snippet

    def test_shell_execute_compressed_in_compact(self):
        """compact 应压缩 shell_execute 的 content（而非保持原样）"""
        mem = Memory()
        long_content = json.dumps({
            "console": [{"command": "ls", "output": "x" * 5000}]
        })
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "do something"})
        mem.add_message({
            "role": "tool",
            "function_name": "shell_execute",
            "content": long_content,
        })
        mem.compact()
        assert len(mem.messages[-1]["content"]) < len(long_content)

    def test_read_file_compressed_in_compact(self):
        """compact 应压缩 read_file 的 content"""
        mem = Memory()
        long_content = json.dumps({
            "filepath": "/test.py",
            "content": "x" * 6000,
        })
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "do something"})
        mem.add_message({
            "role": "tool",
            "function_name": "read_file",
            "content": long_content,
        })
        mem.compact()
        assert len(mem.messages[-1]["content"]) < len(long_content)


class TestSessionSummaryQuality:
    """session_summary 质量增强测试"""

    def test_assistant_text_stripped_in_summary(self):
        """assistant 文本中的换行和多余空格应被压缩"""
        mem = Memory()
        mem.add_message({
            "role": "assistant",
            "content": "\n\n  分析完成，现在开始处理数据并生成可视化报告文档  \n\n",
        })
        summary = mem._build_compression_summary()
        assert "AI:" in summary
        assert "\n\n" not in summary
        assert "分析完成" in summary

    def test_duplicate_snippet_not_appended(self):
        """与上一轮完全相同的 snippet 应被跳过，不重复追加"""
        mem = Memory()
        snippet = "工具调用: shell_execute | AI: 正在处理"
        mem._append_to_session_summary(snippet)
        first_len = len(mem.session_summary)
        mem._append_to_session_summary(snippet)
        assert len(mem.session_summary) == first_len

    def test_different_snippet_appended(self):
        """不同的 snippet 应正常追加"""
        mem = Memory()
        mem._append_to_session_summary("操作A")
        mem._append_to_session_summary("操作B")
        assert "操作A" in mem.session_summary
        assert "操作B" in mem.session_summary

    def test_summary_max_increased(self):
        """_SESSION_SUMMARY_MAX 应为 3000"""
        from app.domain.models.memory import _SESSION_SUMMARY_MAX
        assert _SESSION_SUMMARY_MAX == 3000


class TestKeyFactsExtractionFix:
    """key_facts 提取逻辑修复测试"""

    def test_shell_command_extracted_from_console(self):
        """shell 命令应从 console 数组中提取（而非顶层 command 字段）"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "请执行命令"})
        mem.add_message({
            "role": "tool",
            "function_name": "shell_execute",
            "content": json.dumps({
                "console": [{"ps1": "$ ", "command": "pip install flask", "output": "done"}]
            }),
        })
        facts = mem.extract_key_facts()
        cmd_facts = [f for f in facts if f.category == "cmd"]
        assert len(cmd_facts) == 1
        assert "pip install flask" in cmd_facts[0].content

    def test_planner_system_prompt_not_extracted_as_requirement(self):
        """planner 系统提示 '你正在执行任务' 不应被提取为 requirement"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({
            "role": "user",
            "content": "\n你正在执行任务：\n使用 search_web 工具搜索关键词",
        })
        facts = mem.extract_key_facts()
        req_facts = [f for f in facts if f.category == "requirement"]
        assert len(req_facts) == 0

    def test_budget_warning_not_extracted_as_requirement(self):
        """系统预算警告 '【系统警告】' 不应被提取为 requirement"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({
            "role": "user",
            "content": "【系统警告】你已使用80%的迭代预算，请尽快总结当前发现",
        })
        facts = mem.extract_key_facts()
        req_facts = [f for f in facts if f.category == "requirement"]
        assert len(req_facts) == 0

    def test_delivery_prompt_not_extracted_as_requirement(self):
        """交付规范提示 '任务已完成，你需要将最终结果' 不应被提取为 requirement"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({
            "role": "user",
            "content": "\n任务已完成，你需要将最终结果交付给用户。\n\n交付规范：",
        })
        facts = mem.extract_key_facts()
        req_facts = [f for f in facts if f.category == "requirement"]
        assert len(req_facts) == 0

    def test_real_user_requirement_still_extracted(self):
        """真实用户需求应正常提取为 requirement"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "请帮我搜索 Python 3.13 的新特性"})
        facts = mem.extract_key_facts()
        req_facts = [f for f in facts if f.category == "requirement"]
        assert len(req_facts) == 1
        assert "Python 3.13" in req_facts[0].content

    def test_decision_keyword_not_matching_selector(self):
        """'选择器' 中的 '选择' 不应触发 decision 提取"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "测试"})
        mem.add_message({
            "role": "assistant",
            "content": "看起来对话框内容异常，可能是一个分类选择器展开了",
        })
        facts = mem.extract_key_facts()
        decision_facts = [f for f in facts if f.category == "decision"]
        assert len(decision_facts) == 0

    def test_decision_keyword_still_matches_real_decision(self):
        """真正的决策文本仍应被提取为 decision"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "测试"})
        mem.add_message({
            "role": "assistant",
            "content": "我决定使用 pandas 来处理数据，因为它的性能更好",
        })
        facts = mem.extract_key_facts()
        decision_facts = [f for f in facts if f.category == "decision"]
        assert len(decision_facts) == 1
        assert "pandas" in decision_facts[0].content


class TestSummaryIncrementalExtraction:
    """session_summary 增量提取测试 - 只摘要新增消息，避免重复堆积"""

    def test_no_duplicate_on_repeated_compact_without_new_messages(self):
        """无新消息时多次 compact 不应追加重复摘要"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "do something"})
        mem.add_message({
            "role": "tool",
            "function_name": "shell_execute",
            "content": json.dumps({"console": [{"command": "ls", "output": "file1"}]}),
        })
        mem.compact()
        first_len = len(mem.session_summary)

        # 无新消息，再次 compact
        mem.compact()
        assert len(mem.session_summary) == first_len

        # 再来一次
        mem.compact()
        assert len(mem.session_summary) == first_len

    def test_only_new_messages_summarized_on_second_compact(self):
        """第二次 compact 只摘要新增消息，不重复旧消息"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "task1"})
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({"page_state": {"url": "https://first.com", "title": "First"}}),
        })
        mem.compact()
        first_summary = mem.session_summary
        assert "first.com" in first_summary

        # 追加新消息
        mem.add_message({
            "role": "tool",
            "function_name": "shell_execute",
            "content": json.dumps({"console": [{"command": "pip install requests", "output": "ok"}]}),
        })
        mem.compact()
        second_summary = mem.session_summary

        # 新操作应出现
        assert "pip install requests" in second_summary
        # 旧操作应保留（来自第一次摘要）
        assert "first.com" in second_summary
        # 不应出现旧消息的重复标记（如 "执行命令: shell_execute" 来自已压缩的旧消息）
        assert second_summary.count("first.com") == 1

    def test_last_summary_index_updated_after_compact(self):
        """compact 后 last_summary_index 应更新为当前消息数"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "task"})
        mem.add_message({
            "role": "tool",
            "function_name": "shell_execute",
            "content": json.dumps({"console": [{"command": "ls", "output": "f"}]}),
        })
        assert mem.metrics.last_summary_index == 0
        mem.compact()
        assert mem.metrics.last_summary_index == len(mem.messages)

    def test_last_summary_index_persisted_in_metrics(self):
        """last_summary_index 应持久化在 metrics 中（跨会话恢复）"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "task"})
        mem.compact()
        # 序列化/反序列化后 last_summary_index 应保留
        data = mem.model_dump()
        restored = Memory(**data)
        assert restored.metrics.last_summary_index == mem.metrics.last_summary_index

    def test_summary_empty_when_no_new_messages(self):
        """无新消息时 _build_compression_summary 应返回空字符串"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "task"})
        mem.compact()
        # 此时 last_summary_index == len(messages)，无新消息
        assert mem._build_compression_summary() == ""


class TestKeyFactsExtractedBeforeCompression:
    """key_facts 在压缩前提取测试 - 确保 url/file/cmd 从原始 JSON 提取"""

    def test_url_extracted_before_compression(self):
        """compact 应在压缩 content 前提取 url"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "浏览网页"})
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({
                "page_state": {"url": "https://example.com/page", "title": "Example Page"},
            }),
        })
        mem.compact()
        url_facts = [f for f in mem.key_facts if f.category == "url"]
        assert len(url_facts) >= 1
        assert "example.com" in url_facts[0].content

    def test_file_extracted_before_compression(self):
        """compact 应在压缩 content 前提取 filepath"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "读取文件"})
        mem.add_message({
            "role": "tool",
            "function_name": "read_file",
            "content": json.dumps({"filepath": "/home/ubuntu/data.csv", "content": "x" * 5000}),
        })
        mem.compact()
        file_facts = [f for f in mem.key_facts if f.category == "file"]
        assert len(file_facts) >= 1
        assert "/home/ubuntu/data.csv" in file_facts[0].content

    def test_cmd_extracted_before_compression(self):
        """compact 应在压缩 content 前提取 shell 命令"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "执行命令"})
        mem.add_message({
            "role": "tool",
            "function_name": "shell_execute",
            "content": json.dumps({
                "console": [{"command": "python3 script.py --flag", "output": "x" * 5000}],
            }),
        })
        mem.compact()
        cmd_facts = [f for f in mem.key_facts if f.category == "cmd"]
        assert len(cmd_facts) >= 1
        assert "python3 script.py" in cmd_facts[0].content


class TestContentDataUnpacking:
    """data 子对象解包测试 - 工具返回 {"success":bool,"data":{...}} 格式的提取"""

    def test_unpack_extracts_data_subobject(self):
        """_unpack_content_data 应解包 data 子对象"""
        content = json.dumps({"success": True, "message": "ok", "data": {"filepath": "/tmp/a.txt"}})
        result = Memory._unpack_content_data(content)
        assert result == {"filepath": "/tmp/a.txt"}

    def test_unpack_returns_whole_dict_without_data_key(self):
        """无 data 子对象时应返回整个 dict"""
        content = json.dumps({"filepath": "/tmp/a.txt", "content": "hello"})
        result = Memory._unpack_content_data(content)
        assert result == {"filepath": "/tmp/a.txt", "content": "hello"}

    def test_unpack_returns_none_for_non_json(self):
        """非 JSON content 应返回 None"""
        result = Memory._unpack_content_data("not json")
        assert result is None

    def test_file_extracted_from_wrapped_format(self):
        """file 工具返回 {"success":true,"data":{"filepath":...}} 应正确提取 filepath"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "写入文件"})
        mem.add_message({
            "role": "tool",
            "function_name": "write_file",
            "content": json.dumps({
                "success": True,
                "message": "文件内容写入成功",
                "data": {"filepath": "/home/ubuntu/report.md", "bytes_written": 6340},
            }),
        })
        mem.compact()
        file_facts = [f for f in mem.key_facts if f.category == "file"]
        assert len(file_facts) >= 1
        assert "/home/ubuntu/report.md" in file_facts[0].content

    def test_cmd_extracted_from_wrapped_format(self):
        """shell 工具返回 {"success":true,"data":{"command":...}} 应正确提取 command"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "执行命令"})
        mem.add_message({
            "role": "tool",
            "function_name": "shell_execute",
            "content": json.dumps({
                "success": True,
                "message": "success",
                "data": {
                    "session_id": "default",
                    "command": "python3 --version",
                    "status": "completed",
                    "returncode": 0,
                    "output": "Python 3.10.12\n",
                },
            }),
        })
        mem.compact()
        cmd_facts = [f for f in mem.key_facts if f.category == "cmd"]
        assert len(cmd_facts) >= 1
        assert "python3 --version" in cmd_facts[0].content

    def test_url_extracted_from_wrapped_format(self):
        """browser 工具返回 {"success":true,"data":{"page_state":...}} 应正确提取 url"""
        mem = Memory()
        mem.add_message({"role": "system", "content": "sys"})
        mem.add_message({"role": "user", "content": "浏览网页"})
        mem.add_message({
            "role": "tool",
            "function_name": "browser_view",
            "content": json.dumps({
                "success": True,
                "message": "ok",
                "data": {
                    "page_state": {"url": "https://example.com", "title": "Example"},
                },
            }),
        })
        mem.compact()
        url_facts = [f for f in mem.key_facts if f.category == "url"]
        assert len(url_facts) >= 1
        assert "example.com" in url_facts[0].content

    def test_summary_filepath_from_wrapped_format(self):
        """_summarize_tool_operation 应从 data 子对象提取 filepath"""
        mem = Memory()
        content = json.dumps({
            "success": True,
            "message": "ok",
            "data": {"filepath": "/home/ubuntu/data.csv"},
        })
        snippet = mem._summarize_tool_operation("write_file", content)
        assert "/home/ubuntu/data.csv" in snippet
        assert "write_file" not in snippet

    def test_summary_command_from_wrapped_format(self):
        """_summarize_tool_operation 应从 data 子对象提取 command"""
        mem = Memory()
        content = json.dumps({
            "success": True,
            "message": "ok",
            "data": {"command": "pip install flask"},
        })
        snippet = mem._summarize_tool_operation("shell_execute", content)
        assert "pip install flask" in snippet
        assert "shell_execute" not in snippet
