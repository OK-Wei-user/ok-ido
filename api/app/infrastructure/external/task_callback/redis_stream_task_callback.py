#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : redis_stream_task_callback.py
基于 Redis Stream 的异步任务回调通知实现(F10-7)

设计:
- 每个注册的任务对应一个独立的 Redis Stream `task:callback:{task_id}`
- notify() 通过 xadd 推送一条完成事件到 stream
- wait() 通过 xread 阻塞读取完成事件(支持 timeout)
- cancel() 通过 delete 清理 stream
- stream maxlen=10,避免异常重复 notify 导致内存泄漏

为何不复用 RedisStreamMessageQueue:
- MessageQueue 设计为通用消息队列(put/get/pop/clear),无 notify/wait 语义
- TaskCallbackManager 是单向通知模型(register→notify→wait),语义更聚焦
- 独立实现便于后续扩展(如多订阅者、过滤器等)
"""
import asyncio
import json
import logging
import uuid
from typing import Any, Optional, Dict

from app.domain.external.task_callback import TaskCallbackManager
from app.infrastructure.storage.redis import get_redis

logger = logging.getLogger(__name__)

# 回调 stream 最大保留消息数(防止异常重复 notify 导致内存泄漏)
_CALLBACK_STREAM_MAX_LENGTH = 10

# wait() 内部 xread 的单次阻塞时长(毫秒)
# 选择 1000ms 平衡响应延迟与 CPU 开销:
# - 过短(如100ms): CPU 占用高,无意义轮询
# - 过长(如30000ms): timeout 精度差,任务完成后可能多等待最长30s
_WAIT_BLOCK_MS = 1000


class RedisStreamTaskCallbackManager(TaskCallbackManager):
    """基于 Redis Stream 的异步任务回调通知实现"""

    def __init__(self) -> None:
        """构造函数,复用全局 Redis 客户端"""
        self._redis = get_redis()

    def _stream_name(self, task_id: str) -> str:
        """构建回调 stream 名"""
        return f"task:callback:{task_id}"

    async def register(self, task_id: str) -> None:
        """注册异步任务回调

        幂等操作:即使 stream 已存在也不报错(Redis xadd 自动创建 stream)。
        此处显式 register 仅用于日志追踪与语义清晰,实际 stream 在首次 notify 时自动创建。
        """
        if not task_id:
            logger.warning("注册任务回调失败: task_id 为空")
            return
        logger.info(f"注册任务回调: task_id={task_id}")

    async def notify(self, task_id: str, payload: Dict[str, Any]) -> bool:
        """通知异步任务完成

        通过 xadd 推送完成事件到回调 stream,等待中的 wait() 会被唤醒。
        """
        if not task_id:
            logger.warning("通知任务回调失败: task_id 为空")
            return False
        try:
            stream_name = self._stream_name(task_id)
            await self._redis.client.xadd(
                stream_name,
                {"data": json.dumps(payload, ensure_ascii=False)},
                maxlen=_CALLBACK_STREAM_MAX_LENGTH,
                approximate=True,
            )
            logger.info(
                f"任务回调通知已推送: task_id={task_id}, success={payload.get('success')}"
            )
            return True
        except Exception as e:
            logger.error(f"推送任务回调通知失败: task_id={task_id}, error={str(e)}")
            return False

    async def wait(self, task_id: str, timeout: float) -> Optional[Dict[str, Any]]:
        """等待异步任务完成

        阻塞当前协程直到任务完成或超时。
        实现细节:
        - timeout<=0: 不等待,立即返回 None
        - 内部循环 xread 阻塞 _WAIT_BLOCK_MS,累计耗时不超过 timeout
        - 读到完成事件后立即返回 payload,并清理 stream
        - 超时返回 None,但不清理 stream(任务可能在稍后完成)
        """
        if not task_id:
            logger.warning("等待任务回调失败: task_id 为空")
            return None
        if timeout <= 0:
            return None

        stream_name = self._stream_name(task_id)
        deadline = asyncio.get_event_loop().time() + timeout

        # 先检查 stream 是否已有完成事件(任务可能在 wait 前就已完成)
        existing = await self._redis.client.xrange(stream_name, "-", "+", count=1)
        if existing:
            _, data = existing[0]
            payload_str = data.get("data") if isinstance(data, dict) else None
            if payload_str:
                try:
                    payload = json.loads(payload_str)
                    logger.info(
                        f"任务回调命中已存在事件: task_id={task_id}, "
                        f"success={payload.get('success')}"
                    )
                    await self._cleanup_stream(stream_name)
                    return payload
                except Exception as e:
                    logger.warning(f"解析任务回调 payload 失败: task_id={task_id}, error={e}")

        # 循环阻塞读取,直到完成事件或超时
        while True:
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                logger.info(f"等待任务回调超时: task_id={task_id}, timeout={timeout}s")
                return None

            # 单次阻塞时长不超过剩余等待时间
            remaining_ms = int((deadline - now) * 1000)
            block_ms = min(_WAIT_BLOCK_MS, remaining_ms)
            if block_ms <= 0:
                return None

            try:
                messages = await self._redis.client.xread(
                    {stream_name: "$"},  # 仅消费新到达的消息
                    count=1,
                    block=block_ms,
                )
            except Exception as e:
                logger.warning(
                    f"等待任务回调 xread 异常(继续等待): task_id={task_id}, error={e}"
                )
                await asyncio.sleep(0.1)
                continue

            if not messages:
                continue

            stream_messages = messages[0][1]
            if not stream_messages:
                continue

            _, data = stream_messages[0]
            payload_str = data.get("data") if isinstance(data, dict) else None
            if not payload_str:
                continue

            try:
                payload = json.loads(payload_str)
                logger.info(
                    f"任务回调已收到: task_id={task_id}, "
                    f"success={payload.get('success')}"
                )
                await self._cleanup_stream(stream_name)
                return payload
            except Exception as e:
                logger.warning(f"解析任务回调 payload 失败: task_id={task_id}, error={e}")
                continue

    async def cancel(self, task_id: str) -> None:
        """取消任务回调,清理 stream

        幂等操作:stream 不存在时静默返回。
        """
        if not task_id:
            return
        stream_name = self._stream_name(task_id)
        await self._cleanup_stream(stream_name)
        logger.info(f"任务回调已取消: task_id={task_id}")

    async def _cleanup_stream(self, stream_name: str) -> None:
        """清理指定回调 stream"""
        try:
            await self._redis.client.delete(stream_name)
        except Exception as e:
            logger.warning(f"清理回调 stream 失败: stream={stream_name}, error={e}")
