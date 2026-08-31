#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_mcp_tool_history_retention.py
MCP工具发现历史保留单元测试(向后兼容:历史会话mcp_tool_search数据解析)

根因会话: 9697d98e (5次mcp_tool_search/6次mcp_tool_describe,其中2次为重复搜索)
根因分析: emergency_compact丢失mcp_tool_search返回的候选工具列表,导致LLM
          在压缩后忘记已发现的MCP工具,重复执行search+describe。

修复策略:
1. extract_key_facts: 解析mcp_tool_search返回的matches,将候选工具名保留到mcp_tool分类
2. _summarize_tool_operation: 对mcp_tool_search特殊处理,提取候选工具名到session_summary
3. mcp_tool分类配额从2提升到5,确保能保留多个已发现工具名
4. 领域解耦: extract_search_candidates集中在tool_search.py维护返回格式知识

注: MCP已恢复直接加载模式,新会话不再产生mcp_tool_search工具调用。
    本测试验证旧会话数据的向后兼容性(历史session memory中仍可能包含mcp_tool_search结果)。

本测试覆盖:
- extract_search_candidates: 防御性解析各种格式的mcp_tool_search返回
- extract_key_facts: 保留MCP工具发现历史到key_facts
- _summarize_tool_operation: 生成MCP工具发现摘要
- emergency_compact后key_facts仍保留已发现工具名
"""
import json

from app.domain.models.memory import Memory
from app.domain.services.tools.tool_search import extract_search_candidates


def _make_search_result_text(names: list[str], query: str = "test") -> str:
    """构造mcp_tool_search的直接返回格式(JSON字符串,历史格式)"""
    return json.dumps({
        "query": query,
        "total_available": 30,
        "matches": [
            {"name": n, "description": f"desc for {n}"}
            for n in names
        ],
    }, ensure_ascii=False)


def _make_wrapped_search_result(names: list[str], query: str = "test") -> str:
    """构造mcp_tool_search的包装返回格式(ToolResult序列化后)

    Memory中tool消息content可能被包装为 {"success": true, "data": "{JSON字符串}"}
    """
    inner = _make_search_result_text(names, query)
    return json.dumps({"success": True, "data": inner}, ensure_ascii=False)


class TestExtractSearchCandidates:
    """extract_search_candidates 防御性解析测试

    函数位置: app.domain.services.tools.tool_search.extract_search_candidates
    集中维护mcp_tool_search返回格式知识(向后兼容历史会话数据)。
    """

    def test_parse_direct_json_format(self):
        """直接JSON格式: {"query": "...", "matches": [...]}"""
        text = _make_search_result_text(["mcp_a", "mcp_b"])
        result = extract_search_candidates(text)
        assert result == ["mcp_a", "mcp_b"]

    def test_parse_wrapped_format(self):
        """包装格式: {"success": true, "data": "{JSON字符串}"}"""
        text = _make_wrapped_search_result(["mcp_x", "mcp_y", "mcp_z"])
        result = extract_search_candidates(text)
        assert result == ["mcp_x", "mcp_y", "mcp_z"]

    def test_empty_matches_returns_empty(self):
        """matches为空列表时返回空"""
        text = _make_search_result_text([])
        result = extract_search_candidates(text)
        assert result == []

    def test_invalid_json_returns_empty(self):
        """非JSON字符串返回空列表(不抛异常)"""
        result = extract_search_candidates("not a json")
        assert result == []

    def test_none_text_returns_empty(self):
        """None输入返回空列表(不抛异常)"""
        result = extract_search_candidates(None)
        assert result == []

    def test_empty_string_returns_empty(self):
        """空字符串返回空列表"""
        result = extract_search_candidates("")
        assert result == []

    def test_non_string_text_returns_empty(self):
        """非字符串输入返回空列表(防御性)"""
        assert extract_search_candidates(123) == []  # type: ignore[arg-type]
        assert extract_search_candidates([]) == []  # type: ignore[arg-type]

    def test_missing_matches_field_returns_empty(self):
        """缺少matches字段时返回空"""
        text = json.dumps({"query": "test", "total_available": 0})
        result = extract_search_candidates(text)
        assert result == []

    def test_matches_with_non_dict_items_skipped(self):
        """matches中非dict元素被跳过"""
        text = json.dumps({
            "matches": ["not_dict", {"name": "mcp_ok"}, 123, None]
        })
        result = extract_search_candidates(text)
        assert result == ["mcp_ok"]

    def test_matches_with_missing_name_skipped(self):
        """matches中缺少name字段的元素被跳过"""
        text = json.dumps({
            "matches": [{"description": "no name"}, {"name": "mcp_ok"}]
        })
        result = extract_search_candidates(text)
        assert result == ["mcp_ok"]

    def test_matches_with_non_string_name_skipped(self):
        """matches中name非字符串时被跳过(防御性)"""
        text = json.dumps({
            "matches": [{"name": 123}, {"name": "mcp_ok"}]
        })
        result = extract_search_candidates(text)
        assert result == ["mcp_ok"]

    def test_preserves_match_order(self):
        """候选工具名顺序保持matches中的顺序"""
        text = _make_search_result_text(["mcp_c", "mcp_a", "mcp_b"])
        result = extract_search_candidates(text)
        assert result == ["mcp_c", "mcp_a", "mcp_b"]

    def test_real_world_format_from_session_9697d98e(self):
        """真实会话9697d98e的mcp_tool_search返回格式"""
        text = json.dumps({
            "query": "mcp_system_getOutboundDetailExport 出库明细导出",
            "total_available": 30,
            "matches": [
                {"name": "mcp_system_getOutboundDetailExport", "description": "导出出库明细数据"},
                {"name": "mcp_system_getDownloadTaskList", "description": "查询下载任务列表"},
            ],
        }, ensure_ascii=False)
        result = extract_search_candidates(text)
        assert "mcp_system_getOutboundDetailExport" in result
        assert "mcp_system_getDownloadTaskList" in result


class TestExtractKeyFactsMcpSearchHistory:
    """extract_key_facts 保留MCP工具发现历史测试"""

    def test_mcp_tool_search_candidates_added_to_key_facts(self):
        """mcp_tool_search返回的候选工具名应保留到key_facts的mcp_tool分类"""
        search_result = _make_search_result_text([
            "mcp_system_getOutboundDetailExport",
            "mcp_system_getDownloadTaskList",
        ])
        memory = Memory(messages=[
            {"role": "user", "content": "导出2026年出库数据"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "mcp_tool_search", "arguments": "{}"}
            }]},
            {"role": "tool", "function_name": "mcp_tool_search", "content": search_result},
        ])

        memory.extract_key_facts()

        mcp_facts = [f for f in memory.key_facts if f.category == "mcp_tool"]
        fact_contents = [f.content for f in mcp_facts]
        assert "mcp_system_getOutboundDetailExport" in fact_contents
        assert "mcp_system_getDownloadTaskList" in fact_contents

    def test_mcp_tool_call_also_recorded(self):
        """mcp_tool_call调用的工具名也应保留(原有逻辑)"""
        memory = Memory(messages=[
            {"role": "user", "content": "查询任务状态"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "mcp_tool_call", "arguments": "{}"}
            }]},
            {"role": "tool", "function_name": "mcp_system_getDownloadTaskList", "content": "{}"},
        ])

        memory.extract_key_facts()

        mcp_facts = [f for f in memory.key_facts if f.category == "mcp_tool"]
        fact_contents = [f.content for f in mcp_facts]
        assert "mcp_system_getDownloadTaskList" in fact_contents

    def test_dedup_when_search_and_call_same_tool(self):
        """搜索和调用同一工具时,key_facts去重(基于content_hash)"""
        search_result = _make_search_result_text(["mcp_system_getDownloadTaskList"])
        memory = Memory(messages=[
            {"role": "user", "content": "查询任务"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "mcp_tool_search", "arguments": "{}"}}
            ]},
            {"role": "tool", "function_name": "mcp_tool_search", "content": search_result},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_2", "type": "function",
                 "function": {"name": "mcp_tool_call", "arguments": "{}"}}
            ]},
            {"role": "tool", "function_name": "mcp_system_getDownloadTaskList", "content": "{}"},
        ])

        memory.extract_key_facts()

        mcp_facts = [f for f in memory.key_facts if f.category == "mcp_tool"]
        fact_contents = [f.content for f in mcp_facts]
        # 同一工具名只出现一次(去重)
        assert fact_contents.count("mcp_system_getDownloadTaskList") == 1

    def test_multiple_searches_preserve_all_discovered_tools(self):
        """多次搜索不同工具时,所有已发现工具名都保留"""
        search1 = _make_search_result_text(["mcp_a"], query="query_a")
        search2 = _make_search_result_text(["mcp_b", "mcp_c"], query="query_b")
        memory = Memory(messages=[
            {"role": "user", "content": "执行多步任务"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "mcp_tool_search", "arguments": "{}"}}
            ]},
            {"role": "tool", "function_name": "mcp_tool_search", "content": search1},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_2", "type": "function",
                 "function": {"name": "mcp_tool_search", "arguments": "{}"}}
            ]},
            {"role": "tool", "function_name": "mcp_tool_search", "content": search2},
        ])

        memory.extract_key_facts()

        mcp_facts = [f for f in memory.key_facts if f.category == "mcp_tool"]
        fact_contents = [f.content for f in mcp_facts]
        assert "mcp_a" in fact_contents
        assert "mcp_b" in fact_contents
        assert "mcp_c" in fact_contents

    def test_mcp_tool_quota_increased_to_5(self):
        """mcp_tool分类配额提升到5,可保留多个已发现工具"""
        # 搜索返回5个候选工具
        search_result = _make_search_result_text([
            "mcp_tool_1", "mcp_tool_2", "mcp_tool_3", "mcp_tool_4", "mcp_tool_5"
        ])
        memory = Memory(messages=[
            {"role": "user", "content": "搜索多个工具"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "mcp_tool_search", "arguments": "{}"}
            }]},
            {"role": "tool", "function_name": "mcp_tool_search", "content": search_result},
        ])

        memory.extract_key_facts()

        mcp_facts = [f for f in memory.key_facts if f.category == "mcp_tool"]
        # 配额为5时,5个工具名都应保留
        assert len(mcp_facts) == 5


class TestSummarizeToolOperationMcpSearch:
    """_summarize_tool_operation 对mcp_tool_search的特殊处理测试"""

    def test_mcp_search_summary_contains_discovered_tools(self):
        """mcp_tool_search的摘要应包含已发现的工具名"""
        search_result = _make_search_result_text([
            "mcp_system_getOutboundDetailExport",
            "mcp_system_getDownloadTaskList",
        ])
        memory = Memory(messages=[])

        summary = memory._summarize_tool_operation("mcp_tool_search", search_result)

        assert "MCP工具发现" in summary
        assert "mcp_system_getOutboundDetailExport" in summary

    def test_mcp_search_summary_limits_to_3_tools(self):
        """摘要最多展示3个工具名(防止超出_COMPRESSION_SUMMARY_PER_OP限制)"""
        search_result = _make_search_result_text([
            "mcp_a", "mcp_b", "mcp_c", "mcp_d", "mcp_e"
        ])
        memory = Memory(messages=[])

        summary = memory._summarize_tool_operation("mcp_tool_search", search_result)

        assert "mcp_a" in summary
        assert "mcp_b" in summary
        assert "mcp_c" in summary
        # 第4、5个可能被截断(取决于_COMPRESSION_SUMMARY_PER_OP)
        # 但至少前3个必须在

    def test_mcp_search_empty_result_fallback(self):
        """mcp_tool_search空结果时回退到通用摘要"""
        memory = Memory(messages=[])
        summary = memory._summarize_tool_operation("mcp_tool_search", "not a json")
        assert "MCP工具搜索" in summary

    def test_mcp_call_unchanged(self):
        """mcp_tool_call(以mcp_前缀)的摘要保持原有逻辑"""
        memory = Memory(messages=[])
        summary = memory._summarize_tool_operation("mcp_system_getDownloadTaskList", "{}")
        assert "MCP工具" in summary
        assert "mcp_system_getDownloadTaskList" in summary


class TestEmergencyCompactMcpHistoryRetention:
    """emergency_compact后MCP工具发现历史保留测试

    核心场景: emergency_compact会删除middle区的tool消息,
    但key_facts和session_summary应保留已发现的MCP工具名,
    让LLM在压缩后仍知道哪些工具可用,避免重复搜索。
    """

    def test_key_facts_survive_emergency_compact(self):
        """emergency_compact后key_facts保留已发现的MCP工具名"""
        # 构造足够多的消息触发emergency_compact(max_messages_hard=60)
        search_result = _make_search_result_text([
            "mcp_system_getOutboundDetailExport",
            "mcp_system_getDownloadTaskList",
        ])
        messages = [{"role": "system", "content": "system prompt"}]
        messages.append({"role": "user", "content": "导出出库数据"})
        # 添加搜索历史
        messages.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "mcp_tool_search", "arguments": "{}"}
        }]})
        messages.append({"role": "tool", "function_name": "mcp_tool_search", "content": search_result})
        # 填充大量消息达到emergency_compact阈值
        for i in range(60):
            messages.append({"role": "assistant", "content": f"step {i}"})
            messages.append({"role": "user", "content": f"progress {i}"})

        memory = Memory(messages=messages)
        memory.extract_key_facts()

        # 记录压缩前的key_facts
        facts_before = [f.content for f in memory.key_facts if f.category == "mcp_tool"]
        assert "mcp_system_getOutboundDetailExport" in facts_before

        # 执行emergency_compact
        memory.emergency_compact()

        # 压缩后key_facts应保留(虽然消息被删除,但key_facts独立存储)
        facts_after = [f.content for f in memory.key_facts if f.category == "mcp_tool"]
        assert "mcp_system_getOutboundDetailExport" in facts_after
        assert "mcp_system_getDownloadTaskList" in facts_after

    def test_session_summary_contains_mcp_discovery(self):
        """session_summary应包含MCP工具发现摘要"""
        search_result = _make_search_result_text([
            "mcp_system_getOutboundDetailExport",
        ])
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "导出数据"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "mcp_tool_search", "arguments": "{}"}
            }]},
            {"role": "tool", "function_name": "mcp_tool_search", "content": search_result},
        ]

        memory = Memory(messages=messages)
        memory._append_to_session_summary(memory._build_compression_summary())

        assert "MCP工具发现" in memory.session_summary
        assert "mcp_system_getOutboundDetailExport" in memory.session_summary