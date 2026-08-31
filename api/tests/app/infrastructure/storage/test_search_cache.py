#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_search_cache.py
SearchCache单元测试 - key生成、缓存命中/未命中、序列化、异常隔离
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.models.search import SearchResults, SearchResultItem
from app.infrastructure.storage.search_cache import SearchCache


def _make_results(query="test query", date_range=None):
    return SearchResults(
        query=query,
        date_range=date_range,
        total_results=1,
        results=[SearchResultItem(url="https://example.com", title="Example")],
    )


class TestSearchCacheKeyGeneration:
    """缓存key生成测试"""

    def test_same_query_same_key(self):
        cache = SearchCache(redis_client=MagicMock())
        k1 = cache._make_key("query", None)
        k2 = cache._make_key("query", None)
        assert k1 == k2

    def test_different_query_different_key(self):
        cache = SearchCache(redis_client=MagicMock())
        k1 = cache._make_key("query1", None)
        k2 = cache._make_key("query2", None)
        assert k1 != k2

    def test_date_range_none_same_as_all(self):
        cache = SearchCache(redis_client=MagicMock())
        k1 = cache._make_key("query", None)
        k2 = cache._make_key("query", "all")
        assert k1 == k2

    def test_different_date_range_different_key(self):
        cache = SearchCache(redis_client=MagicMock())
        k1 = cache._make_key("query", "past_day")
        k2 = cache._make_key("query", "past_week")
        assert k1 != k2

    def test_key_has_prefix(self):
        cache = SearchCache(redis_client=MagicMock(), key_prefix="myprefix")
        key = cache._make_key("query", None)
        assert key.startswith("myprefix:")


class TestSearchCacheGet:
    """缓存读取测试"""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_search_results(self):
        results = _make_results()
        redis_client = MagicMock()
        redis_client.client = AsyncMock()
        redis_client.client.get = AsyncMock(return_value=results.model_dump_json())
        cache = SearchCache(redis_client=redis_client)

        cached = await cache.get("test query")

        assert cached is not None
        assert cached.query == "test query"
        assert len(cached.results) == 1

    @pytest.mark.asyncio
    async def test_cache_miss_returns_none(self):
        redis_client = MagicMock()
        redis_client.client = AsyncMock()
        redis_client.client.get = AsyncMock(return_value=None)
        cache = SearchCache(redis_client=redis_client)

        cached = await cache.get("test query")

        assert cached is None

    @pytest.mark.asyncio
    async def test_cache_exception_returns_none(self):
        redis_client = MagicMock()
        redis_client.client = AsyncMock()
        redis_client.client.get = AsyncMock(side_effect=Exception("Redis down"))
        cache = SearchCache(redis_client=redis_client)

        cached = await cache.get("test query")

        assert cached is None

    @pytest.mark.asyncio
    async def test_cache_invalid_json_returns_none(self):
        redis_client = MagicMock()
        redis_client.client = AsyncMock()
        redis_client.client.get = AsyncMock(return_value="not valid json {{{")
        cache = SearchCache(redis_client=redis_client)

        cached = await cache.get("test query")

        assert cached is None


class TestSearchCacheSet:
    """缓存写入测试"""

    @pytest.mark.asyncio
    async def test_set_calls_redis_set_with_ttl(self):
        results = _make_results()
        redis_client = MagicMock()
        redis_client.client = AsyncMock()
        cache = SearchCache(redis_client=redis_client, ttl=1800)

        await cache.set(results)

        redis_client.client.set.assert_called_once()
        args, kwargs = redis_client.client.set.call_args
        assert kwargs.get("ex") == 1800

    @pytest.mark.asyncio
    async def test_set_exception_does_not_raise(self):
        results = _make_results()
        redis_client = MagicMock()
        redis_client.client = AsyncMock()
        redis_client.client.set = AsyncMock(side_effect=Exception("Redis down"))
        cache = SearchCache(redis_client=redis_client)

        # 不应抛出异常
        await cache.set(results)

    @pytest.mark.asyncio
    async def test_set_uses_correct_key(self):
        results = _make_results(query="test", date_range="past_day")
        redis_client = MagicMock()
        redis_client.client = AsyncMock()
        cache = SearchCache(redis_client=redis_client)
        expected_key = cache._make_key("test", "past_day")

        await cache.set(results)

        args, kwargs = redis_client.client.set.call_args
        assert args[0] == expected_key
