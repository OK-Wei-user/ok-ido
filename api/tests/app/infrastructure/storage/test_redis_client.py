#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_redis_client.py
RedisClient单元测试 - 验证连接池参数、初始化、关闭、单例行为

测试重点:
- socket_timeout / socket_connect_timeout 配置正确传递
- health_check_interval / max_connections 配置生效
- 已弃用的 retry_on_timeout 参数不再传递（redis-py 6.0+ 默认启用 TimeoutError 重试）
- init() 幂等性（重复初始化不重建连接）
- shutdown() 清理资源
- get_redis() 单例缓存
- client 属性未初始化时抛出 RuntimeError
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.infrastructure.storage.redis import RedisClient, get_redis


class TestRedisClientInit:
    """Redis客户端初始化测试"""

    @pytest.mark.asyncio
    async def test_init_passes_timeout_params(self):
        """验证socket_timeout和socket_connect_timeout正确传递给Redis"""
        with patch("app.infrastructure.storage.redis.Redis") as mock_redis_cls:
            mock_instance = MagicMock()
            mock_instance.ping = AsyncMock()
            mock_redis_cls.return_value = mock_instance

            client = RedisClient()
            await client.init()

            mock_redis_cls.assert_called_once()
            kwargs = mock_redis_cls.call_args.kwargs
            assert kwargs["socket_timeout"] == 30
            assert kwargs["socket_connect_timeout"] == 10

    @pytest.mark.asyncio
    async def test_init_passes_health_check_interval(self):
        """验证health_check_interval配置传递"""
        with patch("app.infrastructure.storage.redis.Redis") as mock_redis_cls:
            mock_instance = MagicMock()
            mock_instance.ping = AsyncMock()
            mock_redis_cls.return_value = mock_instance

            client = RedisClient()
            await client.init()

            kwargs = mock_redis_cls.call_args.kwargs
            assert kwargs["health_check_interval"] == 30

    @pytest.mark.asyncio
    async def test_init_omits_deprecated_retry_on_timeout(self):
        """验证已弃用的retry_on_timeout参数不再传递（redis-py 6.0+ 默认启用 TimeoutError 重试）"""
        with patch("app.infrastructure.storage.redis.Redis") as mock_redis_cls:
            mock_instance = MagicMock()
            mock_instance.ping = AsyncMock()
            mock_redis_cls.return_value = mock_instance

            client = RedisClient()
            await client.init()

            kwargs = mock_redis_cls.call_args.kwargs
            assert "retry_on_timeout" not in kwargs

    @pytest.mark.asyncio
    async def test_init_passes_max_connections(self):
        """验证max_connections配置传递"""
        with patch("app.infrastructure.storage.redis.Redis") as mock_redis_cls:
            mock_instance = MagicMock()
            mock_instance.ping = AsyncMock()
            mock_redis_cls.return_value = mock_instance

            client = RedisClient()
            await client.init()

            kwargs = mock_redis_cls.call_args.kwargs
            assert kwargs["max_connections"] == 50

    @pytest.mark.asyncio
    async def test_init_idempotent(self):
        """重复init不应重建连接"""
        with patch("app.infrastructure.storage.redis.Redis") as mock_redis_cls:
            mock_instance = MagicMock()
            mock_instance.ping = AsyncMock()
            mock_redis_cls.return_value = mock_instance

            client = RedisClient()
            await client.init()
            await client.init()

            mock_redis_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_init_ping_called(self):
        """init后应调用ping验证连接"""
        with patch("app.infrastructure.storage.redis.Redis") as mock_redis_cls:
            mock_instance = MagicMock()
            mock_instance.ping = AsyncMock()
            mock_redis_cls.return_value = mock_instance

            client = RedisClient()
            await client.init()

            mock_instance.ping.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_init_failure_raises(self):
        """init时ping失败应抛出异常"""
        with patch("app.infrastructure.storage.redis.Redis") as mock_redis_cls:
            mock_instance = MagicMock()
            mock_instance.ping = AsyncMock(side_effect=ConnectionError("连接失败"))
            mock_redis_cls.return_value = mock_instance

            client = RedisClient()
            with pytest.raises(ConnectionError):
                await client.init()


class TestRedisClientShutdown:
    """Redis客户端关闭测试"""

    @pytest.mark.asyncio
    async def test_shutdown_closes_connection(self):
        """shutdown应调用aclose关闭连接"""
        with patch("app.infrastructure.storage.redis.Redis") as mock_redis_cls:
            mock_instance = MagicMock()
            mock_instance.ping = AsyncMock()
            mock_instance.aclose = AsyncMock()
            mock_redis_cls.return_value = mock_instance

            client = RedisClient()
            await client.init()
            await client.shutdown()

            mock_instance.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_clears_client(self):
        """shutdown后client应为None"""
        with patch("app.infrastructure.storage.redis.Redis") as mock_redis_cls:
            mock_instance = MagicMock()
            mock_instance.ping = AsyncMock()
            mock_instance.aclose = AsyncMock()
            mock_redis_cls.return_value = mock_instance

            client = RedisClient()
            await client.init()
            await client.shutdown()

            assert client._client is None

    @pytest.mark.asyncio
    async def test_shutdown_without_init_no_error(self):
        """未初始化时shutdown不应报错"""
        client = RedisClient()
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_clears_singleton_cache(self):
        """shutdown应清除get_redis的单例缓存"""
        with patch("app.infrastructure.storage.redis.Redis") as mock_redis_cls:
            mock_instance = MagicMock()
            mock_instance.ping = AsyncMock()
            mock_instance.aclose = AsyncMock()
            mock_redis_cls.return_value = mock_instance

            r1 = get_redis()
            await r1.init()
            await r1.shutdown()

            r2 = get_redis()
            assert r1 is not r2


class TestRedisClientProperty:
    """Redis客户端属性测试"""

    def test_client_uninitialized_raises(self):
        """未初始化时访问client属性应抛出RuntimeError"""
        client = RedisClient()
        with pytest.raises(RuntimeError, match="Redis客户端未初始化"):
            _ = client.client

    @pytest.mark.asyncio
    async def test_client_returns_instance_after_init(self):
        """init后client属性应返回Redis实例"""
        with patch("app.infrastructure.storage.redis.Redis") as mock_redis_cls:
            mock_instance = MagicMock()
            mock_instance.ping = AsyncMock()
            mock_redis_cls.return_value = mock_instance

            client = RedisClient()
            await client.init()

            assert client.client is mock_instance


class TestGetRedisSingleton:
    """get_redis单例测试"""

    def test_get_redis_returns_same_instance(self):
        """get_redis应返回同一实例（单例）"""
        r1 = get_redis()
        r2 = get_redis()
        assert r1 is r2

    def test_get_redis_returns_redis_client(self):
        """get_redis应返回RedisClient实例"""
        r = get_redis()
        assert isinstance(r, RedisClient)
