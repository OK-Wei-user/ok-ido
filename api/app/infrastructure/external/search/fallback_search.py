#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : fallback_search.py
搜索引擎降级策略 - SearXNG优先，Bing兜底
"""
import logging
from typing import Optional

from app.domain.external.search import SearchEngine
from app.domain.models.search import SearchResults
from app.domain.models.tool_result import ToolResult

logger = logging.getLogger(__name__)


class FallbackSearchEngine(SearchEngine):
    """搜索引擎降级策略：主引擎失败时自动切换到备用引擎"""

    def __init__(self, primary: SearchEngine, fallback: SearchEngine, min_results: int = 1):
        self._primary = primary
        self._fallback = fallback
        self._min_results = min_results

    async def invoke(self, query: str, date_range: Optional[str] = None) -> ToolResult[SearchResults]:
        result = await self._primary.invoke(query, date_range)

        if result.success and result.data and len(result.data.results) >= self._min_results:
            return result

        primary_info = f"主引擎返回{len(result.data.results) if result.data else 0}条结果" if result.success else f"主引擎失败: {result.message}"
        logger.info(f"搜索降级触发 - {primary_info}，切换备用引擎")

        fallback_result = await self._fallback.invoke(query, date_range)

        if fallback_result.success and fallback_result.data and len(fallback_result.data.results) >= self._min_results:
            return fallback_result

        if result.success and result.data and result.data.results:
            return result

        return fallback_result
