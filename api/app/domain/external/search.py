#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/24 14:55

@File    : search.py
"""
from typing import Protocol, Optional, List

from app.domain.models.search import SearchResults
from app.domain.models.tool_result import ToolResult


class SearchEngine(Protocol):
    """搜索引擎API接口协议"""

    async def invoke(self, query: str, date_range: Optional[str] = None) -> ToolResult[SearchResults]:
        """调用搜索引擎并传递query+date_range(日期检索范围)调用搜索引擎获取数据"""
        ...


class ContentFetcher(Protocol):
    """网页正文抓取器接口协议，用于深度研究场景抓取搜索结果URL的全文内容"""

    async def fetch(self, url: str) -> ToolResult[str]:
        """抓取单个URL的网页正文，返回纯文本(已去脚本/样式/导航)"""
        ...

    async def fetch_many(self, urls: List[str]) -> List[ToolResult[str]]:
        """批量抓取多个URL，单失败不影响其他，返回与urls等长的结果列表"""
        ...
