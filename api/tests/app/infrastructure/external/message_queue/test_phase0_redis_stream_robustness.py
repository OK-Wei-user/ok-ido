#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase 0: RedisStreamMessageQueue 流式读取健壮性单元测试

验证 get() 对空串/纯空白 start_id 的防御性清洗,以及 get_latest_id() 空流返回值。
根因: 前端SSE重连时 Last-Event-ID 可能为空字符串,原样透传 xread 触发
"Invalid stream ID" 错误。Phase 0 将 None/""/纯空白 统一兜底为 '$'。
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infrastructure.external.message_queue.redis_stream_message_queue import (
    RedisStreamMessageQueue,
)


@pytest.fixture
def mq():
    """创建消息队列实例(mock Redis 客户端)"""
    with patch(
        "app.infrastructure.external.message_queue.redis_stream_message_queue.get_redis"
    ) as mock_get_redis:
        mock_redis = MagicMock()
        mock_redis.client = AsyncMock()
        mock_get_redis.return_value = mock_redis
        queue = RedisStreamMessageQueue("test-stream")
        return queue


class TestGetStartIdSanitization:
    """get() 方法 start_id 空值/空白清洗测试(Phase 0)"""

    @pytest.mark.asyncio
    async def test_get_empty_string_sanitized_to_dollar(self, mq):
        """空字符串 start_id 兜底为 '$',不透传给 xread(Phase 0 核心修复)"""
        mq._redis.client.xread = AsyncMock(return_value=[])
        await mq.get(start_id="", block_ms=0)
        call_args = mq._redis.client.xread.call_args
        # 空串应被清洗为 '$',而非原样透传(原样透传会触发 Invalid stream ID)
        assert call_args[0][0] == {"test-stream": "$"}

    @pytest.mark.asyncio
    async def test_get_whitespace_string_sanitized_to_dollar(self, mq):
        """纯空白字符串 start_id 兜底为 '$'(覆盖 '  ' 场景)"""
        mq._redis.client.xread = AsyncMock(return_value=[])
        await mq.get(start_id="   ", block_ms=0)
        call_args = mq._redis.client.xread.call_args
        assert call_args[0][0] == {"test-stream": "$"}

    @pytest.mark.asyncio
    async def test_get_none_still_sanitized_to_dollar(self, mq):
        """None start_id 兜底为 '$'(回归保障,与空串一致)"""
        mq._redis.client.xread = AsyncMock(return_value=[])
        await mq.get(start_id=None, block_ms=0)
        call_args = mq._redis.client.xread.call_args
        assert call_args[0][0] == {"test-stream": "$"}

    @pytest.mark.asyncio
    async def test_get_valid_start_id_not_sanitized(self, mq):
        """合法 start_id 不受清洗影响(向后兼容保障)"""
        mq._redis.client.xread = AsyncMock(return_value=[])
        await mq.get(start_id="1234567890-0", block_ms=0)
        call_args = mq._redis.client.xread.call_args
        assert call_args[0][0] == {"test-stream": "1234567890-0"}


class TestGetLatestIdReturnValue:
    """get_latest_id() 空流返回值测试(Phase 0)"""

    @pytest.mark.asyncio
    async def test_get_latest_id_empty_stream_returns_zero_zero(self, mq):
        """空流返回规范的 '0-0'(非 '0'),符合 Redis Stream ID 格式"""
        mq._redis.client.xrevrange = AsyncMock(return_value=[])
        result = await mq.get_latest_id()
        assert result == "0-0"

    @pytest.mark.asyncio
    async def test_get_latest_id_non_empty_returns_actual_id(self, mq):
        """非空流返回实际消息 ID(回归保障)"""
        mq._redis.client.xrevrange = AsyncMock(
            return_value=[("1234567890-0", {"data": "msg"})]
        )
        result = await mq.get_latest_id()
        assert result == "1234567890-0"
