#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2026/07/28

@File    : session_prompt_cache.py
会话级提示词Redis缓存 - 按session_id隔离,持久化提示词片段到Redis,降低token消耗

设计要点(对齐ToolResultCache/SearchCache/IdempotentToolRegistry模式):
1.会话隔离: key含session_id,避免跨会话污染
2.分层缓存: L1内存(热数据快取) + L2 Redis(持久化,跨实例恢复)
3.静默降级: Redis异常仅warning,回退L1内存,不阻塞主流程
4.TTL对齐会话超时: 14400s(4小时),覆盖完整会话生命周期
5.可选注入: redis_client为None时降级为纯内存缓存(向后兼容)
6.类型隔离: prompt_type区分mcp_search/mcp_describe/skill_guide/a2a_card等

应用场景:
- MCP工具搜索/描述结果会话级持久化(避免LLM上下文压缩遗忘后重复search/describe)
- Skills技能指南会话级缓存(避免重复读文件)
- A2A Agent卡片实例级缓存(避免重复网络请求)
"""
import hashlib
import logging
from typing import Dict, Optional

from app.infrastructure.storage.redis import RedisClient

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 14400          # 默认缓存TTL(秒),4小时,对齐session_timeout_seconds
_DEFAULT_KEY_PREFIX = "prompt"  # 默认缓存key前缀,用于命名空间隔离
_L1_MAX_ENTRIES = 200         # L1内存缓存每会话最大条目数(防内存膨胀)


class SessionPromptCache:
    """会话级提示词缓存,封装RedisClient实现提示词片段按session_id+prompt_type+key持久化

    L1内存缓存: 按session_id分桶的字典,零延迟,覆盖单次LLM迭代内高频重复调用
    L2 Redis缓存: 持久化,覆盖长会话中实例重建/服务重启场景
    读取链路: L1 → L2 → (调用方计算) → 回写L1+L2
    """

    def __init__(
            self,
            redis_client: Optional[RedisClient] = None,
            ttl: int = _DEFAULT_TTL,
            key_prefix: str = _DEFAULT_KEY_PREFIX,
            enabled: bool = True,
    ) -> None:
        """构造函数,完成缓存参数初始化

        Args:
            redis_client: RedisClient实例(可选,None时降级纯内存模式)
            ttl: 缓存存活时间(秒),默认4小时对齐会话超时
            key_prefix: 缓存key前缀,用于命名空间隔离
            enabled: 是否启用缓存,False时所有操作no-op(配置开关)
        """
        self._redis_client = redis_client
        self._ttl = ttl
        self._key_prefix = key_prefix
        self._enabled = enabled
        # L1内存缓存: {session_id: {cache_key: value}},按会话分桶便于clear_session
        self._l1_cache: Dict[str, Dict[str, str]] = {}

    @property
    def enabled(self) -> bool:
        """缓存是否启用(配置开关 + Redis可用性双重判定)"""
        return self._enabled

    def _make_key(self, session_id: str, prompt_type: str, key: str) -> str:
        """生成确定性缓存key: {prefix}:{session_id}:{prompt_type}:{sha256(key)}

        sha256保证key的确定性(相同语义key命中同一缓存),支持任意长度key。

        Args:
            session_id: 会话ID(用于会话级隔离)
            prompt_type: 提示词类型(mcp_search/mcp_describe/skill_guide/a2a_card)
            key: 业务key(如搜索query/工具名/技能名)

        Returns:
            确定性缓存key字符串
        """
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"{self._key_prefix}:{session_id}:{prompt_type}:{key_hash}"

    def _l1_get(self, session_id: str, cache_key: str) -> Optional[str]:
        """L1内存缓存读取,未命中返回None"""
        bucket = self._l1_cache.get(session_id)
        if not bucket:
            return None
        return bucket.get(cache_key)

    def _l1_set(self, session_id: str, cache_key: str, value: str) -> None:
        """L1内存缓存写入,超限时淘汰最早条目(LRU近似: dict插入顺序)"""
        bucket = self._l1_cache.setdefault(session_id, {})
        bucket[cache_key] = value
        # L1条目超限时淘汰最早插入的条目(防内存膨胀)
        if len(bucket) > _L1_MAX_ENTRIES:
            oldest_key = next(iter(bucket))
            bucket.pop(oldest_key, None)
            logger.debug(f"L1缓存淘汰(session={session_id}): {oldest_key[:32]}...")

    async def get(
            self,
            session_id: str,
            prompt_type: str,
            key: str,
    ) -> Optional[str]:
        """查询缓存: L1内存 → L2 Redis,命中返回值,未命中/异常返回None(不阻塞主流程)

        Args:
            session_id: 会话ID
            prompt_type: 提示词类型
            key: 业务key

        Returns:
            命中返回缓存值,未命中/异常返回None
        """
        if not self._enabled:
            return None

        cache_key = self._make_key(session_id, prompt_type, key)

        # 1.L1内存缓存读取(零延迟)
        l1_value = self._l1_get(session_id, cache_key)
        if l1_value is not None:
            logger.debug(f"提示词缓存L1命中: type={prompt_type}, session={session_id}")
            return l1_value

        # 2.L2 Redis缓存读取(无Redis或未启用时跳过)
        if self._redis_client is None:
            return None
        try:
            data = await self._redis_client.client.get(cache_key)
            if data:
                # L2命中 → 回写L1(加速后续读取)
                self._l1_set(session_id, cache_key, data)
                logger.debug(f"提示词缓存L2命中: type={prompt_type}, session={session_id}")
                return data
        except Exception as e:
            logger.warning(
                f"提示词缓存L2读取失败,降级L1: type={prompt_type}, "
                f"session={session_id}, error={str(e)}"
            )
        return None

    async def set(
            self,
            session_id: str,
            prompt_type: str,
            key: str,
            value: str,
    ) -> None:
        """写入缓存: L1内存 + L2 Redis,异常仅warning,不影响主流程

        Args:
            session_id: 会话ID
            prompt_type: 提示词类型
            key: 业务key
            value: 缓存值(提示词文本)
        """
        if not self._enabled:
            return

        cache_key = self._make_key(session_id, prompt_type, key)

        # 1.写入L1内存缓存
        self._l1_set(session_id, cache_key, value)

        # 2.写入L2 Redis缓存(无Redis时跳过)
        if self._redis_client is None:
            return
        try:
            await self._redis_client.client.set(cache_key, value, ex=self._ttl)
        except Exception as e:
            logger.warning(
                f"提示词缓存L2写入失败,仅保留L1: type={prompt_type}, "
                f"session={session_id}, error={str(e)}"
            )

    async def delete(
            self,
            session_id: str,
            prompt_type: str,
            key: str,
    ) -> None:
        """删除指定缓存条目(L1+L2),异常静默

        Args:
            session_id: 会话ID
            prompt_type: 提示词类型
            key: 业务key
        """
        if not self._enabled:
            return

        cache_key = self._make_key(session_id, prompt_type, key)

        # 1.删除L1内存缓存
        bucket = self._l1_cache.get(session_id)
        if bucket:
            bucket.pop(cache_key, None)

        # 2.删除L2 Redis缓存
        if self._redis_client is None:
            return
        try:
            await self._redis_client.client.delete(cache_key)
        except Exception as e:
            logger.warning(
                f"提示词缓存L2删除失败: type={prompt_type}, "
                f"session={session_id}, error={str(e)}"
            )

    async def clear_session(self, session_id: str) -> None:
        """清除指定会话的所有L1缓存(L2由TTL自动过期,无需主动清除)

        会话结束时调用,释放L1内存。L2 Redis缓存由TTL自动过期,无需主动清除,
        避免会话结束时批量删除造成Redis压力。

        Args:
            session_id: 会话ID
        """
        self._l1_cache.pop(session_id, None)
        logger.debug(f"已清除会话L1缓存: session={session_id}")

    async def clear_type(self, session_id: str, prompt_type: str) -> None:
        """清除指定会话+类型的所有缓存条目(L1+L2),用于技能文件变更等主动失效场景

        与 clear_session 不同,clear_type 精确到 prompt_type 级别,不清除同会话下
        其他类型的缓存。适用于全局共享缓存(如技能指南)的主动失效:
        技能文件修改后,调用 clear_type("__global__", "skill_guide") 清除旧指南,
        避免L2 Redis缓存(TTL=4小时)持续返回过期内容。

        L1: 按key前缀过滤删除(键格式 {prefix}:{session_id}:{prompt_type}:{hash})
        L2: Redis SCAN 匹配前缀,批量DELETE,避免大key扫描

        Args:
            session_id: 会话ID(全局共享缓存使用固定值如"__global__")
            prompt_type: 提示词类型(如"skill_guide")
        """
        if not self._enabled:
            return

        type_prefix = f"{self._key_prefix}:{session_id}:{prompt_type}:"

        # 1.清除L1内存缓存中匹配的条目
        bucket = self._l1_cache.get(session_id)
        if bucket:
            keys_to_remove = [k for k in bucket if k.startswith(type_prefix)]
            for k in keys_to_remove:
                bucket.pop(k, None)
            if keys_to_remove:
                logger.debug(
                    f"已清除L1缓存: session={session_id}, type={prompt_type}, "
                    f"count={len(keys_to_remove)}"
                )

        # 2.清除L2 Redis缓存中匹配的条目(SCAN + 批量DELETE)
        if self._redis_client is None:
            return
        try:
            pattern = f"{type_prefix}*"
            deleted_count = 0
            cursor = 0
            while True:
                cursor, keys = await self._redis_client.client.scan(
                    cursor, match=pattern, count=100
                )
                if keys:
                    await self._redis_client.client.delete(*keys)
                    deleted_count += len(keys)
                if cursor == 0:
                    break
            if deleted_count:
                logger.info(
                    f"已清除L2缓存: session={session_id}, type={prompt_type}, "
                    f"count={deleted_count}"
                )
        except Exception as e:
            logger.warning(
                f"提示词缓存L2 clear_type失败: type={prompt_type}, "
                f"session={session_id}, error={str(e)}"
            )
