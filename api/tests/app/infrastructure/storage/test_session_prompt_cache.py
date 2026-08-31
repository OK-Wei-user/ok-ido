#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_session_prompt_cache.py
SessionPromptCache单元测试 - 会话级提示词Redis缓存

覆盖场景:
1.缓存key生成(会话隔离、类型隔离、业务key隔离)
2.L1内存缓存命中/未命中
3.L2 Redis缓存命中/未命中 + 回写L1
4.写入链路(L1+L2双写)
5.异常静默降级(get/set/delete都不抛出)
6.enabled=False时所有操作no-op
7.redis_client=None时降级纯L1内存模式
8.clear_session清除L1
9.clear_type主动失效指定会话+类型的缓存(L1+L2,技能文件变更场景)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.infrastructure.storage.session_prompt_cache import SessionPromptCache


def _make_redis_mock() -> MagicMock:
    """构造mock RedisClient,client属性为AsyncMock"""
    redis_client = MagicMock()
    redis_client.client = AsyncMock()
    return redis_client


def _make_cache(
        redis_client: MagicMock = None,
        ttl: int = 14400,
        key_prefix: str = "prompt",
        enabled: bool = True,
) -> SessionPromptCache:
    """构造测试用SessionPromptCache"""
    return SessionPromptCache(
        redis_client=redis_client,
        ttl=ttl,
        key_prefix=key_prefix,
        enabled=enabled,
    )


class TestSessionPromptCacheKeyGeneration:
    """缓存key生成测试"""

    def test_same_params_same_key(self):
        """相同session_id+prompt_type+key 生成相同key"""
        cache = _make_cache()
        k1 = cache._make_key("sess1", "mcp_search", "weather")
        k2 = cache._make_key("sess1", "mcp_search", "weather")
        assert k1 == k2

    def test_different_session_different_key(self):
        """不同session_id 生成不同key(会话隔离)"""
        cache = _make_cache()
        k1 = cache._make_key("sess1", "mcp_search", "weather")
        k2 = cache._make_key("sess2", "mcp_search", "weather")
        assert k1 != k2

    def test_different_type_different_key(self):
        """不同prompt_type 生成不同key(类型隔离)"""
        cache = _make_cache()
        k1 = cache._make_key("sess1", "mcp_search", "weather")
        k2 = cache._make_key("sess1", "mcp_describe", "weather")
        assert k1 != k2

    def test_different_key_different_key(self):
        """不同业务key 生成不同key"""
        cache = _make_cache()
        k1 = cache._make_key("sess1", "mcp_search", "weather")
        k2 = cache._make_key("sess1", "mcp_search", "map")
        assert k1 != k2

    def test_key_format_contains_prefix(self):
        """key格式应包含prefix:session_id:prompt_type:hash"""
        cache = _make_cache(key_prefix="myprompt")
        key = cache._make_key("sess1", "mcp_search", "weather")
        assert key.startswith("myprompt:sess1:mcp_search:")


class TestSessionPromptCacheL1:
    """L1内存缓存测试"""

    @pytest.mark.asyncio
    async def test_l1_hit_returns_value(self):
        """L1命中时直接返回,不访问Redis"""
        redis = _make_redis_mock()
        cache = _make_cache(redis_client=redis)
        await cache.set("sess1", "mcp_search", "weather", "result_text")

        # L1应命中,Redis get不应被调用
        result = await cache.get("sess1", "mcp_search", "weather")
        assert result == "result_text"
        redis.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_l1_miss_l2_miss_returns_none(self):
        """L1未命中 + L2未命中 返回None"""
        redis = _make_redis_mock()
        redis.client.get.return_value = None
        cache = _make_cache(redis_client=redis)

        result = await cache.get("sess1", "mcp_search", "weather")
        assert result is None
        redis.client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_l1_eviction_when_exceeding_limit(self):
        """L1超过最大条目数时淘汰最早条目"""
        cache = _make_cache(redis_client=None)
        # 写入超过_L1_MAX_ENTRIES条目
        from app.infrastructure.storage.session_prompt_cache import _L1_MAX_ENTRIES
        for i in range(_L1_MAX_ENTRIES + 10):
            await cache.set("sess1", "mcp_search", f"key_{i}", f"value_{i}")
        # L1条目数不应超过上限
        assert len(cache._l1_cache["sess1"]) <= _L1_MAX_ENTRIES


class TestSessionPromptCacheL2:
    """L2 Redis缓存测试"""

    @pytest.mark.asyncio
    async def test_l2_hit_writes_back_to_l1(self):
        """L2命中时回写L1,加速后续读取"""
        redis = _make_redis_mock()
        redis.client.get.return_value = "cached_from_redis"
        cache = _make_cache(redis_client=redis)

        # 首次get: L1未命中 → L2命中 → 回写L1
        result1 = await cache.get("sess1", "mcp_describe", "tool_a")
        assert result1 == "cached_from_redis"
        redis.client.get.assert_called_once()

        # 第二次get: L1应命中,不再访问Redis
        redis.client.get.reset_mock()
        result2 = await cache.get("sess1", "mcp_describe", "tool_a")
        assert result2 == "cached_from_redis"
        redis.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_writes_both_l1_and_l2(self):
        """set应同时写入L1和L2"""
        redis = _make_redis_mock()
        cache = _make_cache(redis_client=redis)

        await cache.set("sess1", "skill_guide", "pdf", "guide_content")

        # 验证L2写入
        redis.client.set.assert_called_once()
        args, kwargs = redis.client.set.call_args
        assert "guide_content" in args
        assert kwargs.get("ex") == 14400

        # 验证L1写入(直接读取应命中L1)
        redis.client.get.reset_mock()
        result = await cache.get("sess1", "skill_guide", "pdf")
        assert result == "guide_content"
        redis.client.get.assert_not_called()


class TestSessionPromptCacheDegradation:
    """异常静默降级测试"""

    @pytest.mark.asyncio
    async def test_l2_get_exception_returns_none(self):
        """L2 get异常时返回None,不抛出"""
        redis = _make_redis_mock()
        redis.client.get.side_effect = Exception("Redis连接失败")
        cache = _make_cache(redis_client=redis)

        result = await cache.get("sess1", "mcp_search", "weather")
        assert result is None

    @pytest.mark.asyncio
    async def test_l2_set_exception_silent(self):
        """L2 set异常时静默,L1仍写入"""
        redis = _make_redis_mock()
        redis.client.set.side_effect = Exception("Redis写入失败")
        cache = _make_cache(redis_client=redis)

        # 不应抛出异常
        await cache.set("sess1", "mcp_search", "weather", "value")

        # L1应已写入(通过L1命中验证)
        redis.client.get.reset_mock()
        result = await cache.get("sess1", "mcp_search", "weather")
        assert result == "value"
        redis.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_l2_delete_exception_silent(self):
        """L2 delete异常时静默"""
        redis = _make_redis_mock()
        redis.client.delete.side_effect = Exception("Redis删除失败")
        cache = _make_cache(redis_client=redis)

        # 不应抛出异常
        await cache.delete("sess1", "mcp_search", "weather")

    @pytest.mark.asyncio
    async def test_no_redis_client_degrades_to_l1_only(self):
        """redis_client=None时降级为纯L1内存模式"""
        cache = _make_cache(redis_client=None)

        # set应正常工作(仅L1)
        await cache.set("sess1", "mcp_search", "weather", "l1_only_value")

        # get应从L1命中
        result = await cache.get("sess1", "mcp_search", "weather")
        assert result == "l1_only_value"

    @pytest.mark.asyncio
    async def test_disabled_cache_noop(self):
        """enabled=False时所有操作no-op"""
        redis = _make_redis_mock()
        cache = _make_cache(redis_client=redis, enabled=False)

        # set应为no-op
        await cache.set("sess1", "mcp_search", "weather", "value")
        redis.client.set.assert_not_called()

        # get应为no-op,返回None
        result = await cache.get("sess1", "mcp_search", "weather")
        assert result is None
        redis.client.get.assert_not_called()

        # delete应为no-op
        await cache.delete("sess1", "mcp_search", "weather")
        redis.client.delete.assert_not_called()


class TestSessionPromptCacheClearSession:
    """clear_session测试"""

    @pytest.mark.asyncio
    async def test_clear_session_removes_l1(self):
        """clear_session应清除指定会话的L1缓存"""
        cache = _make_cache(redis_client=None)
        await cache.set("sess1", "mcp_search", "key1", "value1")
        await cache.set("sess1", "mcp_describe", "key2", "value2")
        await cache.set("sess2", "mcp_search", "key3", "value3")

        # 清除sess1
        await cache.clear_session("sess1")

        # sess1的L1应被清除
        assert "sess1" not in cache._l1_cache
        # sess2的L1应保留
        assert "sess2" in cache._l1_cache

    @pytest.mark.asyncio
    async def test_clear_nonexistent_session_noop(self):
        """清除不存在的会话应no-op,不抛出"""
        cache = _make_cache(redis_client=None)
        await cache.clear_session("nonexistent")  # 不应抛出


class TestSessionPromptCacheDelete:
    """delete测试"""

    @pytest.mark.asyncio
    async def test_delete_removes_from_l1_and_l2(self):
        """delete应同时删除L1和L2"""
        redis = _make_redis_mock()
        cache = _make_cache(redis_client=redis)
        await cache.set("sess1", "mcp_search", "weather", "value")

        await cache.delete("sess1", "mcp_search", "weather")

        # L2删除应被调用
        redis.client.delete.assert_called_once()

        # L1应已删除(get将访问L2)
        redis.client.get.return_value = None
        result = await cache.get("sess1", "mcp_search", "weather")
        assert result is None


class TestSessionPromptCacheClearType:
    """clear_type测试(优化A: 主动失效指定会话+类型的缓存)

    覆盖场景:
    1.L1精确清除(指定类型,保留同会话其他类型)
    2.L1会话隔离(不影响其他会话同类型缓存)
    3.L2 SCAN分页批量DELETE
    4.L2异常静默降级
    5.无Redis仅清L1
    6.enabled=False no-op
    7.不存在的会话/类型 no-op
    8.SCAN match pattern 正确性
    9.SkillService.refresh 集成验证
    """

    @pytest.mark.asyncio
    async def test_clear_type_removes_l1_entries_of_type(self):
        """clear_type应清除指定会话+类型的L1缓存,保留同会话其他类型"""
        cache = _make_cache(redis_client=None)
        await cache.set("__global__", "skill_guide", "pdf", "pdf_guide")
        await cache.set("__global__", "mcp_search", "weather", "weather_result")

        # 校验前置: 两条数据已写入L1
        bucket_before = cache._l1_cache.get("__global__", {})
        assert len(bucket_before) == 2

        await cache.clear_type("__global__", "skill_guide")

        bucket_after = cache._l1_cache.get("__global__", {})
        # skill_guide应被清除
        skill_keys = [k for k in bucket_after if "skill_guide" in k]
        assert len(skill_keys) == 0
        # mcp_search应保留
        mcp_keys = [k for k in bucket_after if "mcp_search" in k]
        assert len(mcp_keys) == 1

    @pytest.mark.asyncio
    async def test_clear_type_preserves_other_sessions(self):
        """clear_type应只清除指定会话,不影响其他会话同类型缓存"""
        cache = _make_cache(redis_client=None)
        await cache.set("__global__", "skill_guide", "pdf", "global_guide")
        await cache.set("sess1", "skill_guide", "pdf", "session_guide")

        await cache.clear_type("__global__", "skill_guide")

        # __global__的skill_guide应被清除
        global_bucket = cache._l1_cache.get("__global__", {})
        global_skill_keys = [k for k in global_bucket if "skill_guide" in k]
        assert len(global_skill_keys) == 0
        # sess1的skill_guide应保留
        sess1_bucket = cache._l1_cache.get("sess1", {})
        sess1_skill_keys = [k for k in sess1_bucket if "skill_guide" in k]
        assert len(sess1_skill_keys) == 1

    @pytest.mark.asyncio
    async def test_clear_type_scans_and_deletes_l2_paginated(self):
        """clear_type应SCAN匹配前缀并分页批量DELETE L2缓存"""
        redis = _make_redis_mock()
        # 模拟SCAN分两页返回(cursor=1 → cursor=0 表示扫描结束)
        redis.client.scan.side_effect = [
            (1, ["prompt:__global__:skill_guide:hash1",
                  "prompt:__global__:skill_guide:hash2"]),  # 第一页
            (0, ["prompt:__global__:skill_guide:hash3"]),   # 第二页(cursor=0结束)
        ]
        cache = _make_cache(redis_client=redis)

        await cache.clear_type("__global__", "skill_guide")

        # SCAN应被调用2次(分页扫描直到cursor=0)
        assert redis.client.scan.call_count == 2
        # DELETE应被调用2次(每页批量删除)
        assert redis.client.delete.call_count == 2
        # 验证第一页删除的key
        first_delete_args = redis.client.delete.call_args_list[0][0]
        assert "prompt:__global__:skill_guide:hash1" in first_delete_args
        assert "prompt:__global__:skill_guide:hash2" in first_delete_args
        # 验证第二页删除的key
        second_delete_args = redis.client.delete.call_args_list[1][0]
        assert "prompt:__global__:skill_guide:hash3" in second_delete_args

    @pytest.mark.asyncio
    async def test_clear_type_l2_exception_silent(self):
        """clear_type在L2 SCAN异常时应静默降级,不影响L1清除"""
        redis = _make_redis_mock()
        redis.client.scan.side_effect = Exception("Redis SCAN失败")
        cache = _make_cache(redis_client=redis)

        # 先写入L1(通过set,但L2会失败,这里直接操作L1)
        await cache.set("__global__", "skill_guide", "pdf", "guide")

        # 不应抛出异常
        await cache.clear_type("__global__", "skill_guide")

        # L1应已清除(L2异常不影响L1清除)
        bucket = cache._l1_cache.get("__global__", {})
        skill_keys = [k for k in bucket if "skill_guide" in k]
        assert len(skill_keys) == 0

    @pytest.mark.asyncio
    async def test_clear_type_no_redis_only_l1(self):
        """无Redis时clear_type仅清除L1,不抛出"""
        cache = _make_cache(redis_client=None)
        await cache.set("__global__", "skill_guide", "pdf", "guide")

        # 不应抛出
        await cache.clear_type("__global__", "skill_guide")

        # L1应已清除
        bucket = cache._l1_cache.get("__global__", {})
        skill_keys = [k for k in bucket if "skill_guide" in k]
        assert len(skill_keys) == 0

    @pytest.mark.asyncio
    async def test_clear_type_disabled_noop(self):
        """enabled=False时clear_type为no-op"""
        redis = _make_redis_mock()
        cache = _make_cache(redis_client=redis, enabled=False)

        await cache.clear_type("__global__", "skill_guide")

        # SCAN和DELETE都不应被调用
        redis.client.scan.assert_not_called()
        redis.client.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_type_nonexistent_noop(self):
        """清除不存在的会话/类型应no-op,不抛出"""
        redis = _make_redis_mock()
        redis.client.scan.side_effect = [(0, [])]  # 空结果,cursor=0直接结束
        cache = _make_cache(redis_client=redis)

        await cache.clear_type("nonexistent", "nonexistent_type")

        # SCAN被调用(扫描一次即结束)
        redis.client.scan.assert_called_once()
        # 但DELETE不应被调用(无key匹配)
        redis.client.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_type_scan_match_pattern_correct(self):
        """clear_type应使用正确的SCAN匹配前缀和count参数"""
        redis = _make_redis_mock()
        redis.client.scan.side_effect = [(0, [])]
        cache = _make_cache(redis_client=redis, key_prefix="myprompt")

        await cache.clear_type("__global__", "skill_guide")

        # 验证SCAN使用的match pattern和count
        _, kwargs = redis.client.scan.call_args
        assert kwargs.get("match") == "myprompt:__global__:skill_guide:*"
        assert kwargs.get("count") == 100

    @pytest.mark.asyncio
    async def test_skill_service_refresh_clears_l2_cache(self):
        """SkillService.refresh应调用clear_type清除技能指南L2缓存(优化A集成验证)"""
        from app.domain.services.skill_service import (
            SkillService,
            _SKILL_GUIDE_GLOBAL_SESSION,
        )

        # 构造mock repository和prompt_cache
        repo = MagicMock()
        repo.refresh = AsyncMock()
        repo.get_all = AsyncMock(return_value=[])
        prompt_cache = MagicMock()
        prompt_cache.clear_type = AsyncMock()

        service = SkillService(repository=repo, prompt_cache=prompt_cache)
        await service.refresh()

        # 应调用clear_type清除skill_guide类型的L2缓存(全局共享session_id)
        prompt_cache.clear_type.assert_called_once_with(
            _SKILL_GUIDE_GLOBAL_SESSION, "skill_guide"
        )
        # repository.refresh也应被调用(重新加载仓库)
        repo.refresh.assert_called_once()
