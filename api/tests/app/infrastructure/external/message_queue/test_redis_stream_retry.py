#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RedisStreamMessageQueue 重试机制单元测试

验证长运行场景（browser/deep_research）中 Redis 瞬时超时时的重试行为。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.infrastructure.external.message_queue.redis_stream_message_queue import (
    RedisStreamMessageQueue,
)


@pytest.fixture
def mq():
    """创建消息队列实例（mock Redis 客户端）"""
    with patch(
        "app.infrastructure.external.message_queue.redis_stream_message_queue.get_redis"
    ) as mock_get_redis:
        mock_redis = MagicMock()
        mock_redis.client = AsyncMock()
        mock_get_redis.return_value = mock_redis
        queue = RedisStreamMessageQueue("test-stream")
        return queue


class TestRetryRedisOp:
    """_retry_redis_op 重试机制测试"""

    @pytest.mark.asyncio
    async def test_success_on_first_try(self, mq):
        """首次成功不重试"""
        op = AsyncMock(return_value="ok")
        result = await mq._retry_redis_op(op, "test_op")
        assert result == "ok"
        assert op.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_timeout_then_success(self, mq):
        """首次超时，重试后成功"""
        op = AsyncMock(
            side_effect=[RedisTimeoutError("timeout"), "ok"]
        )
        result = await mq._retry_redis_op(op, "test_op")
        assert result == "ok"
        assert op.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises(self, mq):
        """连续超时超过重试次数后抛出异常"""
        op = AsyncMock(side_effect=RedisTimeoutError("timeout"))
        with pytest.raises(RedisTimeoutError):
            await mq._retry_redis_op(op, "test_op")
        assert op.call_count == 3  # _RETRY_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_non_timeout_error_not_retried(self, mq):
        """非超时异常不重试，直接抛出"""
        from redis.exceptions import ConnectionError as RedisConnectionError
        op = AsyncMock(side_effect=RedisConnectionError("conn error"))
        with pytest.raises(RedisConnectionError):
            await mq._retry_redis_op(op, "test_op")
        assert op.call_count == 1


class TestPutWithRetry:
    """put 方法重试测试"""

    @pytest.mark.asyncio
    async def test_put_success(self, mq):
        """正常写入消息"""
        mq._redis.client.xadd = AsyncMock(return_value="12345-0")
        msg_id = await mq.put("test message")
        assert msg_id == "12345-0"
        assert mq._redis.client.xadd.call_count == 1

    @pytest.mark.asyncio
    async def test_put_retry_on_timeout(self, mq):
        """写入超时后重试成功"""
        mq._redis.client.xadd = AsyncMock(
            side_effect=[RedisTimeoutError("timeout"), "12345-0"]
        )
        msg_id = await mq.put("test message")
        assert msg_id == "12345-0"
        assert mq._redis.client.xadd.call_count == 2


class TestGetWithRetry:
    """get 方法重试测试"""

    @pytest.mark.asyncio
    async def test_get_success(self, mq):
        """正常读取消息"""
        mq._redis.client.xread = AsyncMock(
            return_value=[("test-stream", [("12345-0", {"data": "msg"})])]
        )
        msg_id, msg_data = await mq.get(start_id="0", block_ms=0)
        assert msg_id == "12345-0"
        assert msg_data == "msg"

    @pytest.mark.asyncio
    async def test_get_empty_stream(self, mq):
        """空流返回 None"""
        mq._redis.client.xread = AsyncMock(return_value=[])
        msg_id, msg_data = await mq.get(start_id="0", block_ms=0)
        assert msg_id is None
        assert msg_data is None

    @pytest.mark.asyncio
    async def test_get_retry_on_timeout(self, mq):
        """读取超时后重试成功"""
        mq._redis.client.xread = AsyncMock(
            side_effect=[
                RedisTimeoutError("timeout"),
                [("test-stream", [("12345-0", {"data": "msg"})])],
            ]
        )
        msg_id, msg_data = await mq.get(start_id="0", block_ms=0)
        assert msg_id == "12345-0"
        assert msg_data == "msg"
        assert mq._redis.client.xread.call_count == 2

    @pytest.mark.asyncio
    async def test_get_default_start_id(self, mq):
        """未传 start_id 时默认 '$'(F1-1: 避免重读历史事件)"""
        mq._redis.client.xread = AsyncMock(return_value=[])
        await mq.get(block_ms=0)
        call_args = mq._redis.client.xread.call_args
        # F1-1 修复: 默认 '$' 表示只消费新消息,避免会话重连时重读历史事件
        assert call_args[0][0] == {"test-stream": "$"}
