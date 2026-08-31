#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : tool_search.py
MCP工具搜索结果解析工具模块

历史背景:
    原MCP懒加载桥接架构(mcp_tool_search/mcp_tool_describe/mcp_tool_call)已移除,
    恢复MCP工具直接加载模式。本模块仅保留 extract_search_candidates 函数,
    用于解析历史会话中mcp_tool_search工具调用的返回结果(memory.py向后兼容)。

    新会话不再产生mcp_tool_search工具调用,但旧会话的内存数据可能仍包含
    此格式的工具返回结果,extract_search_candidates确保旧数据可被正确解析。
"""
import json
import logging
from typing import List

logger = logging.getLogger(__name__)


def extract_search_candidates(text: str) -> List[str]:
    """从mcp_tool_search返回内容中提取候选MCP工具名列表

    防御性解析两种存储形态:
    - 直接JSON字符串: format_search_result的原始输出(历史格式)
    - 包装格式: {"success": true, "data": "{JSON字符串}"} (ToolResult序列化后)

    用于key_facts保留MCP工具发现历史,防止emergency_compact后重复搜索。
    新会话不再产生mcp_tool_search调用,但旧会话数据仍需此函数解析。

    Args:
        text: mcp_tool_search返回的文本内容

    Returns:
        候选工具名列表(matches中的name字段);解析失败返回空列表
    """
    candidates: List[str] = []
    if not text or not isinstance(text, str):
        return candidates
    try:
        outer = json.loads(text)
        if not isinstance(outer, dict):
            return candidates
        # 解包: 优先取data字段(若为dict直接用,若为str再解析一层)
        inner = outer.get("data", outer)
        if isinstance(inner, str) and inner:
            try:
                inner = json.loads(inner)
            except (json.JSONDecodeError, TypeError):
                inner = outer
        if not isinstance(inner, dict):
            return candidates
        # 提取matches中的工具名
        matches = inner.get("matches", [])
        if isinstance(matches, list):
            for m in matches:
                if isinstance(m, dict):
                    name = m.get("name", "")
                    if name and isinstance(name, str):
                        candidates.append(name)
    except (json.JSONDecodeError, TypeError):
        pass
    return candidates
