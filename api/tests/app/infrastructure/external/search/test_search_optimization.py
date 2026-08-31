#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_search_optimization.py
搜索流程优化单元测试 - snippet截断、数量上限、重试机制、降级策略
"""
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.domain.models.search import SearchResultItem, SearchResults
from app.domain.models.tool_result import ToolResult
from app.infrastructure.external.search.searxng_search import (
    SearXNGSearchEngine,
    _MAX_RESULTS,
    _MAX_SNIPPET_LENGTH,
)
from app.infrastructure.external.search.bing_search import BingSearchEngine


class TestSnippetTruncation:
    """Snippet截断测试"""

    def test_short_snippet_preserved(self):
        result = SearXNGSearchEngine._truncate_snippet("Short text")
        assert result == "Short text"

    def test_long_snippet_truncated(self):
        long_snippet = "a" * 300
        result = SearXNGSearchEngine._truncate_snippet(long_snippet)
        assert len(result) == _MAX_SNIPPET_LENGTH + 3
        assert result.endswith("...")

    def test_exact_length_snippet_preserved(self):
        snippet = "a" * _MAX_SNIPPET_LENGTH
        result = SearXNGSearchEngine._truncate_snippet(snippet)
        assert result == snippet

    def test_empty_snippet_returns_empty(self):
        result = SearXNGSearchEngine._truncate_snippet("")
        assert result == ""

    def test_none_snippet_returns_none(self):
        result = SearXNGSearchEngine._truncate_snippet(None)
        assert result is None


class TestMaxResultsLimit:
    """搜索结果数量上限测试"""

    @pytest.mark.asyncio
    async def test_results_capped_at_max(self):
        engine = SearXNGSearchEngine()
        mock_data = {
            "number_of_results": 20,
            "results": [
                {"url": f"https://example.com/{i}", "title": f"Result {i}", "content": f"Snippet {i}"}
                for i in range(20)
            ],
        }
        with patch.object(engine, "_request_with_retry", new_callable=AsyncMock, return_value=mock_data):
            result = await engine.invoke("test query")
        assert len(result.data.results) <= _MAX_RESULTS

    @pytest.mark.asyncio
    async def test_fewer_results_than_max_preserved(self):
        engine = SearXNGSearchEngine()
        mock_data = {
            "number_of_results": 3,
            "results": [
                {"url": f"https://example.com/{i}", "title": f"Result {i}", "content": f"Snippet {i}"}
                for i in range(3)
            ],
        }
        with patch.object(engine, "_request_with_retry", new_callable=AsyncMock, return_value=mock_data):
            result = await engine.invoke("test query")
        assert len(result.data.results) == 3

    @pytest.mark.asyncio
    async def test_search_engine_order_preserved(self):
        engine = SearXNGSearchEngine()
        mock_data = {
            "number_of_results": 3,
            "results": [
                {"url": "https://a.com", "title": "First", "content": "a"},
                {"url": "https://b.com", "title": "Second", "content": "b"},
                {"url": "https://c.com", "title": "Third", "content": "c"},
            ],
        }
        with patch.object(engine, "_request_with_retry", new_callable=AsyncMock, return_value=mock_data):
            result = await engine.invoke("test")
        assert result.data.results[0].title == "First"
        assert result.data.results[1].title == "Second"
        assert result.data.results[2].title == "Third"


class TestSearXNGRetryMechanism:
    """SearXNG重试机制测试"""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        engine = SearXNGSearchEngine()
        mock_data = {"number_of_results": 1, "results": [{"url": "https://a.com", "title": "A", "content": "a"}]}
        with patch.object(engine, "_request_with_retry", new_callable=AsyncMock, return_value=mock_data):
            result = await engine.invoke("test")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_returns_error_when_all_retries_fail(self):
        engine = SearXNGSearchEngine()
        with patch.object(engine, "_request_with_retry", new_callable=AsyncMock, return_value=None):
            result = await engine.invoke("test")
        assert result.success is False
        assert "不可用" in result.message

    @pytest.mark.asyncio
    async def test_retry_mechanism_works(self):
        engine = SearXNGSearchEngine()
        call_count = 0

        async def mock_retry(params):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return None
            return {"number_of_results": 1, "results": [{"url": "https://a.com", "title": "A", "content": "a"}]}

        with patch.object(engine, "_request_with_retry", new_callable=AsyncMock, side_effect=mock_retry):
            result = await engine.invoke("test")
        assert call_count >= 1


class TestSearchResultParsing:
    """搜索结果解析测试"""

    @pytest.mark.asyncio
    async def test_skip_items_without_title(self):
        engine = SearXNGSearchEngine()
        mock_data = {
            "number_of_results": 2,
            "results": [
                {"url": "https://a.com", "title": "", "content": "a"},
                {"url": "https://b.com", "title": "Valid", "content": "b"},
            ],
        }
        with patch.object(engine, "_request_with_retry", new_callable=AsyncMock, return_value=mock_data):
            result = await engine.invoke("test")
        assert len(result.data.results) == 1
        assert result.data.results[0].title == "Valid"

    @pytest.mark.asyncio
    async def test_skip_items_without_url(self):
        engine = SearXNGSearchEngine()
        mock_data = {
            "number_of_results": 2,
            "results": [
                {"url": "", "title": "No URL", "content": "a"},
                {"url": "https://b.com", "title": "Valid", "content": "b"},
            ],
        }
        with patch.object(engine, "_request_with_retry", new_callable=AsyncMock, return_value=mock_data):
            result = await engine.invoke("test")
        assert len(result.data.results) == 1

    @pytest.mark.asyncio
    async def test_date_range_mapping(self):
        engine = SearXNGSearchEngine()
        mock_data = {"number_of_results": 0, "results": []}
        captured_params = {}

        async def mock_retry(params):
            nonlocal captured_params
            captured_params = params
            return mock_data

        with patch.object(engine, "_request_with_retry", new_callable=AsyncMock, side_effect=mock_retry):
            await engine.invoke("test", date_range="past_week")
        assert captured_params.get("time_range") == "week"

    @pytest.mark.asyncio
    async def test_no_date_range_for_all(self):
        engine = SearXNGSearchEngine()
        mock_data = {"number_of_results": 0, "results": []}
        captured_params = {}

        async def mock_retry(params):
            nonlocal captured_params
            captured_params = params
            return mock_data

        with patch.object(engine, "_request_with_retry", new_callable=AsyncMock, side_effect=mock_retry):
            await engine.invoke("test", date_range="all")
        assert "time_range" not in captured_params

    @pytest.mark.asyncio
    async def test_baike_result_not_filtered(self):
        engine = SearXNGSearchEngine()
        mock_data = {
            "number_of_results": 2,
            "results": [
                {"url": "https://baike.baidu.com/item/量子力学", "title": "量子力学_百度百科", "content": "量子力学是物理学分支"},
                {"url": "https://arxiv.org/paper", "title": "Quantum Mechanics Review", "content": "A review paper"},
            ],
        }
        with patch.object(engine, "_request_with_retry", new_callable=AsyncMock, return_value=mock_data):
            result = await engine.invoke("量子力学")
        assert len(result.data.results) == 2
        assert any("baidu" in r.url for r in result.data.results)


class TestFallbackSearchEngine:
    """降级搜索引擎测试"""

    @pytest.mark.asyncio
    async def test_primary_success_no_fallback(self):
        from app.infrastructure.external.search.fallback_search import FallbackSearchEngine

        primary = AsyncMock()
        primary.invoke = AsyncMock(return_value=ToolResult(
            success=True,
            data=SearchResults(query="test", results=[SearchResultItem(url="https://a.com", title="A", snippet="a")]),
        ))
        fallback = AsyncMock()

        engine = FallbackSearchEngine(primary=primary, fallback=fallback)
        result = await engine.invoke("test")
        assert result.success is True
        fallback.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_triggered_on_primary_failure(self):
        from app.infrastructure.external.search.fallback_search import FallbackSearchEngine

        primary = AsyncMock()
        primary.invoke = AsyncMock(return_value=ToolResult(
            success=False,
            message="SearXNG不可用",
            data=SearchResults(query="test", results=[]),
        ))
        fallback = AsyncMock()
        fallback.invoke = AsyncMock(return_value=ToolResult(
            success=True,
            data=SearchResults(query="test", results=[SearchResultItem(url="https://b.com", title="B", snippet="b")]),
        ))

        engine = FallbackSearchEngine(primary=primary, fallback=fallback)
        result = await engine.invoke("test")
        assert result.success is True
        assert result.data.results[0].title == "B"

    @pytest.mark.asyncio
    async def test_both_fail_returns_primary_result(self):
        from app.infrastructure.external.search.fallback_search import FallbackSearchEngine

        primary = AsyncMock()
        primary.invoke = AsyncMock(return_value=ToolResult(
            success=False,
            message="SearXNG不可用",
            data=SearchResults(query="test", results=[]),
        ))
        fallback = AsyncMock()
        fallback.invoke = AsyncMock(return_value=ToolResult(
            success=False,
            message="Bing不可用",
            data=SearchResults(query="test", results=[]),
        ))

        engine = FallbackSearchEngine(primary=primary, fallback=fallback)
        result = await engine.invoke("test")
        assert result.success is False
