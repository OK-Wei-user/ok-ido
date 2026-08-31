#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_search_tool.py
SearchTool单元测试 - URL去重、缓存命中、按需抓取正文
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.models.search import SearchResultItem, SearchResults
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.search import SearchTool


def _make_item(url, title, snippet=""):
    return SearchResultItem(url=url, title=title, snippet=snippet)


def _make_results(items, query="test", date_range=None):
    return SearchResults(query=query, date_range=date_range, total_results=len(items), results=items)


class TestSearchToolDedup:
    """URL去重测试"""

    def test_dedup_keeps_first_occurrence(self):
        tool = SearchTool(search_engine=MagicMock())
        items = [
            _make_item("https://example.com/page", "A"),
            _make_item("https://example.com/page", "B"),
        ]
        unique = tool._dedup_results(items)
        assert len(unique) == 1
        assert unique[0].title == "A"

    def test_dedup_with_tracking_params(self):
        tool = SearchTool(search_engine=MagicMock())
        items = [
            _make_item("https://example.com/page?utm_source=x", "A"),
            _make_item("https://example.com/page", "B"),
        ]
        unique = tool._dedup_results(items)
        assert len(unique) == 1

    def test_dedup_with_fragment(self):
        tool = SearchTool(search_engine=MagicMock())
        items = [
            _make_item("https://example.com/page#sec1", "A"),
            _make_item("https://example.com/page#sec2", "B"),
        ]
        unique = tool._dedup_results(items)
        assert len(unique) == 1

    def test_dedup_preserves_unique_items(self):
        tool = SearchTool(search_engine=MagicMock())
        items = [
            _make_item("https://example.com/page1", "A"),
            _make_item("https://example.com/page2", "B"),
            _make_item("https://example.com/page3", "C"),
        ]
        unique = tool._dedup_results(items)
        assert len(unique) == 3

    def test_dedup_empty_list(self):
        tool = SearchTool(search_engine=MagicMock())
        assert tool._dedup_results([]) == []


class TestSearchToolCache:
    """缓存集成测试"""

    @pytest.mark.asyncio
    async def test_cache_hit_skips_search_engine(self):
        cached_results = _make_results([_make_item("https://x.com", "X")])
        cache = MagicMock()
        cache.get = AsyncMock(return_value=cached_results)
        engine = MagicMock()
        engine.invoke = AsyncMock()
        tool = SearchTool(search_engine=engine, cache=cache)

        result = await tool.search_web("test")

        assert result.success
        assert result.data is cached_results
        engine.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_miss_calls_search_engine_then_sets(self):
        engine_results = ToolResult(
            success=True,
            data=_make_results([_make_item("https://x.com", "X")]),
        )
        engine = MagicMock()
        engine.invoke = AsyncMock(return_value=engine_results)
        cache = MagicMock()
        cache.get = AsyncMock(return_value=None)
        cache.set = AsyncMock()
        tool = SearchTool(search_engine=engine, cache=cache)

        result = await tool.search_web("test")

        assert result.success
        engine.invoke.assert_called_once()
        cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_cache_skips_cache_logic(self):
        engine_results = ToolResult(
            success=True,
            data=_make_results([_make_item("https://x.com", "X")]),
        )
        engine = MagicMock()
        engine.invoke = AsyncMock(return_value=engine_results)
        tool = SearchTool(search_engine=engine, cache=None)

        result = await tool.search_web("test")

        assert result.success
        engine.invoke.assert_called_once()


class TestSearchToolFetchContent:
    """正文抓取测试"""

    @pytest.mark.asyncio
    async def test_fetch_content_false_skips_fetcher(self):
        engine_results = ToolResult(
            success=True,
            data=_make_results([_make_item("https://x.com", "X")]),
        )
        engine = MagicMock()
        engine.invoke = AsyncMock(return_value=engine_results)
        fetcher = MagicMock()
        fetcher.fetch_many = AsyncMock()
        tool = SearchTool(search_engine=engine, content_fetcher=fetcher)

        await tool.search_web("test", fetch_content=False)

        fetcher.fetch_many.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_content_true_fetches_and_fills(self):
        items = [_make_item("https://x.com", "X"), _make_item("https://y.com", "Y")]
        engine_results = ToolResult(success=True, data=_make_results(items))
        engine = MagicMock()
        engine.invoke = AsyncMock(return_value=engine_results)
        fetcher = MagicMock()
        fetcher.fetch_many = AsyncMock(return_value=[
            ToolResult(success=True, data="content for x"),
            ToolResult(success=True, data="content for y"),
        ])
        tool = SearchTool(search_engine=engine, content_fetcher=fetcher)

        result = await tool.search_web("test", fetch_content=True)

        fetcher.fetch_many.assert_called_once()
        assert result.data.results[0].content == "content for x"
        assert result.data.results[1].content == "content for y"

    @pytest.mark.asyncio
    async def test_fetch_content_no_fetcher_skips(self):
        items = [_make_item("https://x.com", "X")]
        engine_results = ToolResult(success=True, data=_make_results(items))
        engine = MagicMock()
        engine.invoke = AsyncMock(return_value=engine_results)
        tool = SearchTool(search_engine=engine, content_fetcher=None)

        result = await tool.search_web("test", fetch_content=True)

        assert result.data.results[0].content is None

    @pytest.mark.asyncio
    async def test_fetch_failure_keeps_content_none(self):
        items = [_make_item("https://x.com", "X")]
        engine_results = ToolResult(success=True, data=_make_results(items))
        engine = MagicMock()
        engine.invoke = AsyncMock(return_value=engine_results)
        fetcher = MagicMock()
        fetcher.fetch_many = AsyncMock(return_value=[ToolResult(success=False, message="404")])
        tool = SearchTool(search_engine=engine, content_fetcher=fetcher)

        result = await tool.search_web("test", fetch_content=True)

        assert result.data.results[0].content is None

    @pytest.mark.asyncio
    async def test_fetch_respects_max_fetch_limit(self):
        items = [_make_item(f"https://x{i}.com", f"X{i}") for i in range(10)]
        engine_results = ToolResult(success=True, data=_make_results(items))
        engine = MagicMock()
        engine.invoke = AsyncMock(return_value=engine_results)
        fetcher = MagicMock()
        fetcher.fetch_many = AsyncMock(return_value=[ToolResult(success=True, data="c")] * 5)
        tool = SearchTool(search_engine=engine, content_fetcher=fetcher, max_fetch=3)

        await tool.search_web("test", fetch_content=True)

        # 只对前3个URL抓取
        fetcher.fetch_many.assert_called_once_with(["https://x0.com", "https://x1.com", "https://x2.com"])
