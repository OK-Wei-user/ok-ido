#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : searxng_search.py
SearXNG元搜索引擎适配器 - 聚合Google/Bing/DuckDuckGo等多引擎结果
"""
import logging
import os
from typing import Optional

import httpx

from app.domain.external.search import SearchEngine
from app.domain.models.search import SearchResults, SearchResultItem
from app.domain.models.tool_result import ToolResult

logger = logging.getLogger(__name__)

_SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")
_SEARXNG_TIMEOUT = float(os.environ.get("SEARXNG_TIMEOUT", "15"))
_SEARXNG_MAX_RETRIES = int(os.environ.get("SEARXNG_MAX_RETRIES", "2"))

_DATE_RANGE_MAP = {
    "past_hour": "day",
    "past_day": "day",
    "past_week": "week",
    "past_month": "month",
    "past_year": "year",
}

_MAX_RESULTS = int(os.environ.get("SEARXNG_MAX_RESULTS", "10"))
_MAX_SNIPPET_LENGTH = int(os.environ.get("SEARXNG_MAX_SNIPPET_LENGTH", "200"))


class SearXNGSearchEngine(SearchEngine):
    """SearXNG元搜索引擎 - 聚合多搜索引擎结果提升召回质量"""

    def __init__(self, base_url: Optional[str] = None, timeout: Optional[float] = None):
        self.base_url = (base_url or _SEARXNG_URL).rstrip("/")
        self.timeout = timeout or _SEARXNG_TIMEOUT

    async def invoke(self, query: str, date_range: Optional[str] = None) -> ToolResult[SearchResults]:
        params = {
            "q": query,
            "format": "json",
            "language": "zh-CN",
        }
        if date_range and date_range != "all" and date_range in _DATE_RANGE_MAP:
            params["time_range"] = _DATE_RANGE_MAP[date_range]

        data = await self._request_with_retry(params)
        if data is None:
            return self._error_result(query, date_range, "SearXNG服务不可用")

        results = self._parse_results(data)
        return ToolResult(
            success=True,
            data=SearchResults(
                query=query,
                date_range=date_range,
                total_results=data.get("number_of_results", len(results)),
                results=results[:_MAX_RESULTS],
            ),
        )

    async def _request_with_retry(self, params: dict) -> Optional[dict]:
        last_error: Optional[Exception] = None
        for attempt in range(1, _SEARXNG_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(f"{self.base_url}/search", params=params)
                    response.raise_for_status()
                    return response.json()
            except httpx.HTTPStatusError as e:
                logger.warning(f"SearXNG HTTP {e.response.status_code} (尝试{attempt}/{_SEARXNG_MAX_RETRIES})")
                last_error = e
            except httpx.ConnectError:
                logger.warning(f"SearXNG连接失败 (尝试{attempt}/{_SEARXNG_MAX_RETRIES})")
                last_error = httpx.ConnectError("SearXNG服务不可用")
            except Exception as e:
                logger.error(f"SearXNG搜索出错 (尝试{attempt}/{_SEARXNG_MAX_RETRIES}): {str(e)}")
                last_error = e
        if isinstance(last_error, httpx.HTTPStatusError):
            logger.warning(f"SearXNG重试耗尽: HTTP {last_error.response.status_code}")
        elif isinstance(last_error, httpx.ConnectError):
            logger.warning("SearXNG重试耗尽: 连接失败")
        else:
            logger.error(f"SearXNG重试耗尽: {last_error}")
        return None

    def _parse_results(self, data: dict) -> list:
        results = []
        for item in data.get("results", []):
            url = item.get("url", "")
            title = item.get("title", "")
            snippet = item.get("content", "")
            if not title or not url:
                continue
            snippet = self._truncate_snippet(snippet)
            results.append(SearchResultItem(url=url, title=title, snippet=snippet))
        return results

    @staticmethod
    def _truncate_snippet(snippet: str) -> str:
        if not snippet or len(snippet) <= _MAX_SNIPPET_LENGTH:
            return snippet
        return snippet[:_MAX_SNIPPET_LENGTH] + "..."

    def _error_result(self, query: str, date_range: Optional[str], message: str) -> ToolResult[SearchResults]:
        return ToolResult(
            success=False,
            message=message,
            data=SearchResults(query=query, date_range=date_range, total_results=0, results=[]),
        )
