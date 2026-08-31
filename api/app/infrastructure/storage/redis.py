#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/5/14 10:52

@File    : redis.py
"""
import logging
from functools import lru_cache

from redis.asyncio import Redis

from core.config import get_settings, Settings

logger = logging.getLogger(__name__)

# Redis连接池参数 - 针对长时间运行场景（deep_research/browser）优化
_SOCKET_TIMEOUT = 30          # 单次读写超时(秒)，避免长任务中连接被误判超时
_SOCKET_CONNECT_TIMEOUT = 10  # 建立连接超时(秒)
_HEALTH_CHECK_INTERVAL = 30   # 健康检查间隔(秒)，自动发现并重建失效连接
_MAX_CONNECTIONS = 50         # 连接池上限，满足并发工具调用需求
# 注：redis-py 6.0+ 默认启用 TimeoutError 重试，无需 retry_on_timeout 参数


class RedisClient:
    """Redis客户端，用于完成redis缓存连接&使用"""

    def __init__(self):
        """构造函数，完成redis客户端的创建"""
        self._client: Redis | None = None
        self._settings: Settings = get_settings()

    async def init(self) -> None:
        """完成redis客户端的初始化"""
        # 1.判断客户端是否存在，如果存在则表示已连接上，无需重复连接
        if self._client:
            logger.warning("Redis客户端已初始化，无需重复操作")
            return

        try:
            # 2.创建Redis客户端并连接（含超时/健康检查/重试配置）
            self._client = Redis(
                host=self._settings.redis_host,
                port=self._settings.redis_port,
                db=self._settings.redis_db,
                password=self._settings.redis_password,
                decode_responses=True,
                socket_timeout=_SOCKET_TIMEOUT,
                socket_connect_timeout=_SOCKET_CONNECT_TIMEOUT,
                health_check_interval=_HEALTH_CHECK_INTERVAL,
                max_connections=_MAX_CONNECTIONS,
            )

            # 3.测试连接redis缓存
            await self._client.ping()
            logger.info("Redis客户端初始化成功")
        except Exception as e:
            logger.error(f"初始化Redis客户端失败: {str(e)}")
            raise

    async def shutdown(self) -> None:
        """关闭Redis时执行的操作"""
        # 1.客户端存在则关闭客户端并提示
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("Redis客户端成功关闭")

        # 2.清除缓存
        get_redis.cache_clear()

    @property
    def client(self) -> Redis:
        """只读属性，返回redis客户端"""
        if self._client is None:
            raise RuntimeError("Redis客户端未初始化，获取客户端失败")
        return self._client


@lru_cache()
def get_redis() -> RedisClient:
    """获取redis实例"""
    return RedisClient()
