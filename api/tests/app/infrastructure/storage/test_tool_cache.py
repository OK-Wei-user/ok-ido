#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_tool_cache.py
ToolResultCache单元测试 - P3优化的工具结果缓存

覆盖场景:
1.缓存key生成(参数顺序无关、会话隔离、工具名隔离)
2.缓存命中/未命中
3.白名单判定(直接白名单 + MCP直接加载模式按mcp_*前缀匹配)
4.非白名单工具不读不写缓存
5.异常静默降级(get/set都不抛出)
6.失败结果不缓存(由调用方保证,这里测试缓存层只负责存取)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.models.tool_result import ToolResult
from app.infrastructure.storage.tool_cache import ToolResultCache


def _make_result(success: bool = True, message: str = "ok", data: dict = None) -> ToolResult:
    """构造测试用ToolResult"""
    return ToolResult(success=success, message=message, data=data or {"key": "value"})


def _make_cache(
        cacheable_tools: list = None,
        cacheable_mcp_tools: list = None,
        ttl: int = 1800,
        key_prefix: str = "tool",
) -> ToolResultCache:
    """构造测试用ToolResultCache,Redis用mock"""
    redis_client = MagicMock()
    redis_client.client = AsyncMock()
    return ToolResultCache(
        redis_client=redis_client,
        ttl=ttl,
        key_prefix=key_prefix,
        cacheable_tools=cacheable_tools,
        cacheable_mcp_tools=cacheable_mcp_tools,
    )


class TestToolCacheKeyGeneration:
    """缓存key生成测试"""

    def test_same_args_same_key(self):
        """相同session_id+tool_name+args 生成相同key"""
        cache = _make_cache()
        k1 = cache._make_key("sess1", "web_search", {"query": "test"})
        k2 = cache._make_key("sess1", "web_search", {"query": "test"})
        assert k1 == k2

    def test_different_session_different_key(self):
        """不同session_id 生成不同key(会话隔离)"""
        cache = _make_cache()
        k1 = cache._make_key("sess1", "web_search", {"query": "test"})
        k2 = cache._make_key("sess2", "web_search", {"query": "test"})
        assert k1 != k2

    def test_different_tool_different_key(self):
        """不同tool_name 生成不同key"""
        cache = _make_cache()
        k1 = cache._make_key("sess1", "web_search", {"query": "test"})
        k2 = cache._make_key("sess1", "deep_research", {"query": "test"})
        assert k1 != k2

    def test_different_args_different_key(self):
        """不同参数 生成不同key"""
        cache = _make_cache()
        k1 = cache._make_key("sess1", "web_search", {"query": "test1"})
        k2 = cache._make_key("sess1", "web_search", {"query": "test2"})
        assert k1 != k2

    def test_args_order_independence(self):
        """参数顺序不同但内容相同,生成相同key(sorted JSON 序列化)"""
        cache = _make_cache()
        k1 = cache._make_key("sess1", "web_search", {"query": "test", "limit": 10})
        k2 = cache._make_key("sess1", "web_search", {"limit": 10, "query": "test"})
        assert k1 == k2

    def test_key_has_prefix(self):
        """key 包含配置的前缀"""
        cache = _make_cache(key_prefix="myprefix")
        key = cache._make_key("sess1", "web_search", {"query": "test"})
        assert key.startswith("myprefix:")


class TestToolCacheIsCacheable:
    """白名单判定测试"""

    def test_direct_whitelist_match_returns_true(self):
        """工具名在cacheable_tools白名单中,返回True"""
        cache = _make_cache(cacheable_tools=["web_search", "file_read"])
        assert cache.is_cacheable("web_search", {}) is True
        assert cache.is_cacheable("file_read", {}) is True

    def test_non_whitelist_tool_returns_false(self):
        """工具名不在白名单中(shell_execute等副作用工具),返回False"""
        cache = _make_cache(cacheable_tools=["web_search"])
        assert cache.is_cacheable("shell_execute", {}) is False
        assert cache.is_cacheable("browser_navigate", {}) is False
        assert cache.is_cacheable("file_write", {}) is False

    def test_empty_whitelist_returns_false(self):
        """白名单为空时,所有工具都不可缓存"""
        cache = _make_cache(cacheable_tools=[])
        assert cache.is_cacheable("web_search", {}) is False

    def test_mcp_direct_loading_matches_full_name(self):
        """MCP直接加载模式: mcp_* 完整工具名匹配白名单"""
        cache = _make_cache(
            cacheable_tools=[],
            cacheable_mcp_tools=["mcp_amap_maps_weather", "mcp_system_getProductMainExport"],
        )
        # 完整名匹配(如 mcp_amap_maps_weather)
        assert cache.is_cacheable("mcp_amap_maps_weather", {}) is True
        assert cache.is_cacheable("mcp_system_getProductMainExport", {}) is True
        # 不匹配的直接MCP工具不可缓存
        assert cache.is_cacheable("mcp_amap_maps_ip_locate", {}) is False

    def test_mcp_direct_loading_matches_short_name(self):
        """MCP直接加载模式: mcp_* 去掉前缀后匹配白名单(短名兼容)"""
        cache = _make_cache(
            cacheable_tools=[],
            cacheable_mcp_tools=["amap_maps_weather", "system_getDownloadTaskList"],
        )
        # 短名匹配(去掉mcp_后)
        assert cache.is_cacheable("mcp_amap_maps_weather", {}) is True
        assert cache.is_cacheable("mcp_system_getDownloadTaskList", {}) is True
        # 不在白名单中的MCP工具不可缓存
        assert cache.is_cacheable("mcp_system_getOther", {}) is False

    def test_non_mcp_prefix_tool_returns_false(self):
        """非mcp_前缀且不在直接白名单的工具不可缓存(如shell_execute)"""
        cache = _make_cache(
            cacheable_tools=["web_search"],
            cacheable_mcp_tools=["mcp_amap_maps_weather"],
        )
        assert cache.is_cacheable("shell_execute", {}) is False
        assert cache.is_cacheable("deep_research", {}) is False


class TestToolCacheGet:
    """缓存读取测试"""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_result(self):
        """命中缓存时返回反序列化的ToolResult"""
        result = _make_result(data={"query": "test", "hits": 5})
        redis_client = MagicMock()
        redis_client.client = AsyncMock()
        redis_client.client.get = AsyncMock(return_value=result.model_dump_json())
        cache = ToolResultCache(redis_client=redis_client)

        cached = await cache.get("sess1", "web_search", {"query": "test"})

        assert cached is not None
        assert cached.success is True
        assert cached.data == {"query": "test", "hits": 5}

    @pytest.mark.asyncio
    async def test_cache_miss_returns_none(self):
        """未命中缓存时返回None"""
        cache = _make_cache()
        cache._redis_client.client.get = AsyncMock(return_value=None)

        cached = await cache.get("sess1", "web_search", {"query": "test"})

        assert cached is None

    @pytest.mark.asyncio
    async def test_cache_exception_returns_none(self):
        """Redis异常时返回None,不抛出"""
        cache = _make_cache()
        cache._redis_client.client.get = AsyncMock(side_effect=Exception("Redis down"))

        cached = await cache.get("sess1", "web_search", {"query": "test"})

        assert cached is None

    @pytest.mark.asyncio
    async def test_cache_invalid_json_returns_none(self):
        """缓存数据非法JSON时返回None(由Pydantic校验失败)"""
        cache = _make_cache()
        cache._redis_client.client.get = AsyncMock(return_value="not valid json {{{")

        cached = await cache.get("sess1", "web_search", {"query": "test"})

        assert cached is None


class TestToolCacheSet:
    """缓存写入测试"""

    @pytest.mark.asyncio
    async def test_set_calls_redis_set_with_ttl(self):
        """写入缓存调用redis.set,且ex参数为TTL"""
        cache = _make_cache(ttl=1800)
        result = _make_result()

        await cache.set("sess1", "web_search", {"query": "test"}, result)

        cache._redis_client.client.set.assert_called_once()
        args, kwargs = cache._redis_client.client.set.call_args
        assert kwargs.get("ex") == 1800

    @pytest.mark.asyncio
    async def test_set_exception_does_not_raise(self):
        """Redis写入异常时仅warning,不抛出"""
        cache = _make_cache()
        cache._redis_client.client.set = AsyncMock(side_effect=Exception("Redis down"))
        result = _make_result()

        # 不应抛出异常
        await cache.set("sess1", "web_search", {"query": "test"}, result)

    @pytest.mark.asyncio
    async def test_set_uses_correct_key(self):
        """写入缓存使用正确的key(与_get_key一致)"""
        cache = _make_cache()
        result = _make_result()
        expected_key = cache._make_key("sess1", "web_search", {"query": "test"})

        await cache.set("sess1", "web_search", {"query": "test"}, result)

        args, kwargs = cache._redis_client.client.set.call_args
        assert args[0] == expected_key

    @pytest.mark.asyncio
    async def test_set_serializes_tool_result(self):
        """写入缓存的value是ToolResult的JSON序列化"""
        cache = _make_cache()
        result = _make_result(data={"foo": "bar"})

        await cache.set("sess1", "web_search", {"query": "test"}, result)

        args, kwargs = cache._redis_client.client.set.call_args
        serialized = args[1]
        assert '"foo":"bar"' in serialized or '"foo": "bar"' in serialized


class TestToolCacheSessionIsolation:
    """会话隔离测试"""

    @pytest.mark.asyncio
    async def test_different_sessions_no_cross_hit(self):
        """不同session_id相同参数,不会命中同一缓存"""
        cache = _make_cache()
        # 第一次调用:sess1未命中
        cache._redis_client.client.get = AsyncMock(return_value=None)
        cached1 = await cache.get("sess1", "web_search", {"query": "test"})
        assert cached1 is None

        # 第二次调用:sess2也未命中(不同key,Redis返回None)
        cached2 = await cache.get("sess2", "web_search", {"query": "test"})
        assert cached2 is None

        # 验证两次调用使用了不同的key
        assert cache._redis_client.client.get.call_count == 2
        k1 = cache._redis_client.client.get.call_args_list[0].args[0]
        k2 = cache._redis_client.client.get.call_args_list[1].args[0]
        assert k1 != k2


class TestToolCacheHitFlow:
    """完整缓存命中流程测试(模拟get→miss→set→hit)"""

    @pytest.mark.asyncio
    async def test_full_cache_flow_first_miss_second_hit(self):
        """首次调用未命中→写入缓存→第二次调用命中"""
        result = _make_result(data={"answer": 42})
        cache = _make_cache(cacheable_tools=["web_search"])

        # 第一次:get未命中
        cache._redis_client.client.get = AsyncMock(return_value=None)
        cached1 = await cache.get("sess1", "web_search", {"query": "test"})
        assert cached1 is None

        # 写入缓存
        await cache.set("sess1", "web_search", {"query": "test"}, result)

        # 第二次:get命中(返回写入的数据)
        cache._redis_client.client.get = AsyncMock(return_value=result.model_dump_json())
        cached2 = await cache.get("sess1", "web_search", {"query": "test"})
        assert cached2 is not None
        assert cached2.data == {"answer": 42}
