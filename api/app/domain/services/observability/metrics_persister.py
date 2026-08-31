#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""指标持久化层(Batch 40 / 方向2+3: 指标 Redis 持久化)

设计目标:
- 将 MetricsCollector 的内存快照持久化到 Redis Hash,支持跨会话聚合分析
- 为 A/B 实验分组(方向2)和 shell 合并引导效果量化(方向3)提供数据基础
- 轻量非侵入: 异常静默降级,绝不阻断主流程

Redis 数据结构:
- Hash: metrics:{YYYYMMDD}:{session_id} — 单会话指标快照, TTL 30天
- Set: metrics:{YYYYMMDD}:index — 当日所有 session_id,便于聚合查询, TTL 31天

集成位置:
- AgentTaskRunner.invoke() finally 块, log_snapshot() 后调用 persist()
"""
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 指标持久化 TTL(秒): 30天,足够 A/B 实验数据积累
_METRICS_TTL_SECONDS = 30 * 24 * 3600
# 索引 TTL(秒): 31天,比单条多1天确保聚合时索引仍在
_INDEX_TTL_SECONDS = 31 * 24 * 3600


class MetricsPersister:
    """指标 Redis 持久化器(Batch 40 / 方向2+3)

    将会话级指标快照写入 Redis Hash,支持:
    - A/B 实验分组效果对比(方向2): 按实验组聚合完成率/预算使用率
    - shell 合并引导效果量化(方向3): 对比引导触发/未触发会话的 shell_execute_count

    使用方式:
        persister = MetricsPersister()
        await persister.persist(session_id, snapshot_dict, experiment_group="control")
    """

    def __init__(self, redis_client: Optional[Any] = None) -> None:
        """构造函数

        Args:
            redis_client: 可选的 Redis 客户端, None 时延迟初始化(首次 persist 时获取)
        """
        self._redis = redis_client

    def _get_redis(self) -> Optional[Any]:
        """延迟获取 Redis 客户端(避免模块加载时连接)"""
        if self._redis is not None:
            return self._redis
        try:
            from app.infrastructure.storage.redis import get_redis
            self._redis = get_redis()
            return self._redis
        except Exception as e:
            logger.debug(f"MetricsPersister 获取 Redis 客户端失败(降级禁用持久化): {e}")
            return None

    async def persist(
            self,
            session_id: str,
            snapshot: Dict[str, Any],
            experiment_group: Optional[str] = None,
    ) -> bool:
        """持久化指标快照到 Redis

        Args:
            session_id: 会话 ID
            snapshot: MetricsCollector.snapshot() 返回的指标字典
            experiment_group: 可选的实验组标识(方向2: control/variant_a 等)

        Returns:
            True 表示持久化成功, False 表示失败或降级跳过
        """
        if not session_id or not snapshot:
            return False

        redis = self._get_redis()
        if redis is None:
            return False

        try:
            now = datetime.now()
            date_key = now.strftime("%Y%m%d")

            # 补充元数据
            enriched = {
                **snapshot,
                "persisted_at": now.isoformat(),
                "experiment_group": experiment_group or "default",
            }

            hash_key = f"metrics:{date_key}:{session_id}"
            index_key = f"metrics:{date_key}:index"

            # 写入 Hash + 设置 TTL
            await redis.client.hset(
                hash_key,
                mapping={k: json.dumps(v, ensure_ascii=False, default=str) for k, v in enriched.items()},
            )
            await redis.client.expire(hash_key, _METRICS_TTL_SECONDS)

            # 写入索引 Set + 设置 TTL
            await redis.client.sadd(index_key, session_id)
            await redis.client.expire(index_key, _INDEX_TTL_SECONDS)

            logger.debug(f"指标快照已持久化: session={session_id}, group={experiment_group}")
            return True
        except Exception as e:
            logger.debug(f"指标持久化失败(降级忽略): {e}")
            return False

    async def query_by_date(self, date_str: str) -> list:
        """查询指定日期的所有会话指标(供离线分析脚本使用)

        Args:
            date_str: 日期字符串,格式 YYYYMMDD

        Returns:
            指标字典列表,每项为一个会话的快照
        """
        redis = self._get_redis()
        if redis is None:
            return []

        try:
            index_key = f"metrics:{date_str}:index"
            session_ids = await redis.client.smembers(index_key)
            results = []
            for sid in session_ids:
                hash_key = f"metrics:{date_str}:{sid}"
                raw = await redis.client.hgetall(hash_key)
                if raw:
                    results.append({
                        k: json.loads(v) if v.startswith(("{", "[")) else v
                        for k, v in raw.items()
                    })
            return results
        except Exception as e:
            logger.debug(f"指标查询失败: {e}")
            return []
