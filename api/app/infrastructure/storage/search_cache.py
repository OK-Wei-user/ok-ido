#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : search_cache.py
搜索结果Redis缓存 - 基于RedisClient实现查询结果缓存，避免相同query重复调用搜索引擎。
缓存失败一律静默（warning），保证不阻塞搜索主流程。
"""
import hashlib
import logging
from typing import Optional

from app.domain.models.search import SearchResults
from app.infrastructure.storage.redis import RedisClient

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 3600  # 默认缓存TTL(秒)，1小时
_DEFAULT_KEY_PREFIX = "search"  # 默认缓存key前缀


class SearchCache:
    """搜索结果缓存，封装RedisClient实现查询结果按query+date_range缓存"""

    def __init__(
            self,
            redis_client: RedisClient,
            ttl: int = _DEFAULT_TTL,
            key_prefix: str = _DEFAULT_KEY_PREFIX,
    ) -> None:
        """构造函数，完成缓存参数初始化

        Args:
            redis_client: RedisClient实例（复用其连接池，便于测试mock）
            ttl: 缓存存活时间(秒)
            key_prefix: 缓存key前缀，用于命名空间隔离
        """
        self._redis_client = redis_client
        self._ttl = ttl
        self._key_prefix = key_prefix

    def _make_key(self, query: str, date_range: Optional[str]) -> str:
        """根据query+date_range生成确定性缓存key：sha256(f"{query}|{date_range or 'all'}")"""
        normalized_date_range = date_range or "all"
        raw = f"{query}|{normalized_date_range}"
        key_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"{self._key_prefix}:{key_hash}"

    async def get(self, query: str, date_range: Optional[str] = None) -> Optional[SearchResults]:
        """查询缓存：命中返回SearchResults，未命中或异常返回None（不阻塞主流程）"""
        try:
            key = self._make_key(query, date_range)
            data = await self._redis_client.client.get(key)
            if not data:
                return None
            return SearchResults.model_validate_json(data)
        except Exception as e:
            logger.warning(f"搜索缓存读取失败，跳过缓存: query={query}, error={str(e)}")
            return None

    async def set(self, results: SearchResults) -> None:
        """写入缓存：异常仅warning，不影响搜索结果返回"""
        try:
            key = self._make_key(results.query, results.date_range)
            await self._redis_client.client.set(key, results.model_dump_json(), ex=self._ttl)
        except Exception as e:
            logger.warning(f"搜索缓存写入失败，跳过缓存: query={results.query}, error={str(e)}")
