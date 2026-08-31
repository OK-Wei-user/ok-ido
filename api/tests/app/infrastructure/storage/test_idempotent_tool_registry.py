#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_idempotent_tool_registry.py
IdempotentToolRegistry单元测试 - P10-1 幂等工具调用去重(通用型框架能力)

覆盖场景:
1.去重key生成(参数顺序无关、会话隔离、工具名隔离)
2.is_dedupable判定(MCP直接加载模式: mcp_* 前缀匹配白名单)
3.去重命中/未命中
4.异常静默降级(get/set都不抛出)
5.会话隔离
6.完整去重流程(miss→set→hit)

通用性设计: 测试用工具名使用通用占位名(mcp_test_asyncTask/mcp_test_generateReport),
不绑定任何具体业务场景,确保测试本身符合通用型智能体框架定位。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.models.tool_result import ToolResult
from app.infrastructure.storage.idempotent_tool_registry import IdempotentToolRegistry


def _make_result(success: bool = True, message: str = "ok", data: dict = None) -> ToolResult:
    """构造测试用ToolResult(模拟幂等写操作工具返回)"""
    return ToolResult(
        success=success,
        message=message,
        data=data or {"taskId": "task_001", "status": "pending"},
    )


def _make_registry(
        dedup_tools: list = None,
        ttl: int = 3600,
        key_prefix: str = "tool_dedup",
) -> IdempotentToolRegistry:
    """构造测试用IdempotentToolRegistry,Redis用mock"""
    redis_client = MagicMock()
    redis_client.client = AsyncMock()
    return IdempotentToolRegistry(
        redis_client=redis_client,
        ttl=ttl,
        key_prefix=key_prefix,
        dedup_tools=dedup_tools if dedup_tools is not None else [
            "mcp_test_asyncTask",
            "mcp_test_generateReport",
        ],
    )


# 直接加载模式下,工具名=实际MCP工具名(如 mcp_test_asyncTask),参数=实际业务参数
_TOOL_NAME = "mcp_test_asyncTask"
_TOOL_ARGS = {"month": "202601", "format": "csv"}


class TestRegistryKeyGeneration:
    """去重key生成测试"""

    def test_same_args_same_key(self):
        """相同session_id+tool_name+args 生成相同key"""
        registry = _make_registry()
        k1 = registry._make_key("sess1", _TOOL_NAME, _TOOL_ARGS)
        k2 = registry._make_key("sess1", _TOOL_NAME, _TOOL_ARGS)
        assert k1 == k2

    def test_different_session_different_key(self):
        """不同session_id 生成不同key(会话隔离)"""
        registry = _make_registry()
        k1 = registry._make_key("sess1", _TOOL_NAME, _TOOL_ARGS)
        k2 = registry._make_key("sess2", _TOOL_NAME, _TOOL_ARGS)
        assert k1 != k2

    def test_different_tool_name_different_key(self):
        """不同工具名 生成不同key"""
        registry = _make_registry()
        k1 = registry._make_key("sess1", "mcp_test_asyncTask", _TOOL_ARGS)
        k2 = registry._make_key("sess1", "mcp_test_generateReport", _TOOL_ARGS)
        assert k1 != k2

    def test_different_args_different_key(self):
        """不同调用参数 生成不同key(防止不同参数的去重互相干扰)"""
        registry = _make_registry()
        k1 = registry._make_key("sess1", _TOOL_NAME, {"month": "202601"})
        k2 = registry._make_key("sess1", _TOOL_NAME, {"month": "202602"})
        assert k1 != k2

    def test_args_order_independence(self):
        """参数顺序不同但内容相同,生成相同key(sorted JSON 序列化)"""
        registry = _make_registry()
        k1 = registry._make_key("sess1", _TOOL_NAME, {"month": "202601", "format": "csv"})
        k2 = registry._make_key("sess1", _TOOL_NAME, {"format": "csv", "month": "202601"})
        assert k1 == k2

    def test_key_has_prefix(self):
        """key 包含配置的前缀"""
        registry = _make_registry(key_prefix="myprefix")
        key = registry._make_key("sess1", _TOOL_NAME, _TOOL_ARGS)
        assert key.startswith("myprefix:")


class TestRegistryIsDedupable:
    """is_dedupable 判定测试(MCP直接加载模式)"""

    def test_mcp_direct_loading_full_name_returns_true(self):
        """MCP直接加载模式: mcp_* 完整工具名匹配白名单 → 可去重"""
        registry = _make_registry(dedup_tools=["mcp_test_asyncTask", "mcp_test_generateReport"])
        # 直接传入 mcp_* 工具名
        assert registry.is_dedupable("mcp_test_asyncTask", {}) is True
        assert registry.is_dedupable("mcp_test_generateReport", {}) is True

    def test_mcp_direct_loading_short_name_returns_true(self):
        """MCP直接加载模式: mcp_* 去掉前缀后匹配白名单(短名兼容) → 可去重"""
        registry = _make_registry(dedup_tools=["test_asyncTask", "system_getWarehousingDetailExport"])
        # 去掉 mcp_ 前缀后匹配
        assert registry.is_dedupable("mcp_test_asyncTask", {}) is True
        assert registry.is_dedupable("mcp_system_getWarehousingDetailExport", {}) is True

    def test_mcp_direct_loading_non_whitelisted_returns_false(self):
        """MCP直接加载模式: mcp_* 但不在白名单中 → 不去重"""
        registry = _make_registry(dedup_tools=["mcp_test_asyncTask"])
        assert registry.is_dedupable("mcp_test_queryTask", {}) is False
        assert registry.is_dedupable("mcp_amap_maps_weather", {}) is False

    def test_non_mcp_tool_returns_false(self):
        """非 mcp_* 工具(如 shell_execute/file_read) → 不去重"""
        registry = _make_registry(dedup_tools=["mcp_test_asyncTask"])
        assert registry.is_dedupable("shell_execute", {}) is False
        assert registry.is_dedupable("file_read", {}) is False
        assert registry.is_dedupable("browser_navigate", {}) is False

    def test_empty_dedup_tools_returns_false(self):
        """dedup_tools为空时,所有工具都不去重(默认安全)"""
        registry = _make_registry(dedup_tools=[])
        assert registry.is_dedupable("mcp_test_asyncTask", {}) is False


class TestRegistryGet:
    """去重记录读取测试"""

    @pytest.mark.asyncio
    async def test_get_hit_returns_cached_result(self):
        """命中去重记录时返回反序列化的ToolResult(上次调用结果)"""
        result = _make_result(data={"taskId": "task_001", "status": "completed", "downloadUrl": "http://x/y.zip"})
        redis_client = MagicMock()
        redis_client.client = AsyncMock()
        redis_client.client.get = AsyncMock(return_value=result.model_dump_json())
        registry = IdempotentToolRegistry(redis_client=redis_client)

        cached = await registry.get("sess1", _TOOL_NAME, _TOOL_ARGS)

        assert cached is not None
        assert cached.success is True
        assert cached.data["taskId"] == "task_001"
        assert cached.data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self):
        """未命中去重记录时返回None"""
        registry = _make_registry()
        registry._redis_client.client.get = AsyncMock(return_value=None)

        cached = await registry.get("sess1", _TOOL_NAME, _TOOL_ARGS)

        assert cached is None

    @pytest.mark.asyncio
    async def test_get_redis_exception_returns_none(self):
        """Redis异常时返回None,放行实际调用(静默降级)"""
        registry = _make_registry()
        registry._redis_client.client.get = AsyncMock(side_effect=Exception("Redis down"))

        cached = await registry.get("sess1", _TOOL_NAME, _TOOL_ARGS)

        assert cached is None

    @pytest.mark.asyncio
    async def test_get_invalid_json_returns_none(self):
        """缓存数据非法JSON时返回None(由Pydantic校验失败,触发异常分支)"""
        registry = _make_registry()
        registry._redis_client.client.get = AsyncMock(return_value="not valid json {{{")

        cached = await registry.get("sess1", _TOOL_NAME, _TOOL_ARGS)

        assert cached is None


class TestRegistrySet:
    """去重记录写入测试"""

    @pytest.mark.asyncio
    async def test_set_calls_redis_set_with_ttl(self):
        """写入去重记录调用redis.set,且ex参数为TTL"""
        registry = _make_registry(ttl=3600)
        result = _make_result()

        await registry.set("sess1", _TOOL_NAME, _TOOL_ARGS, result)

        registry._redis_client.client.set.assert_called_once()
        args, kwargs = registry._redis_client.client.set.call_args
        assert kwargs.get("ex") == 3600

    @pytest.mark.asyncio
    async def test_set_exception_does_not_raise(self):
        """Redis写入异常时仅warning,不抛出(保障主流程返回)"""
        registry = _make_registry()
        registry._redis_client.client.set = AsyncMock(side_effect=Exception("Redis down"))
        result = _make_result()

        # 不应抛出异常
        await registry.set("sess1", _TOOL_NAME, _TOOL_ARGS, result)

    @pytest.mark.asyncio
    async def test_set_uses_correct_key(self):
        """写入去重记录使用与get相同的key(保证后续命中)"""
        registry = _make_registry()
        result = _make_result()
        expected_key = registry._make_key("sess1", _TOOL_NAME, _TOOL_ARGS)

        await registry.set("sess1", _TOOL_NAME, _TOOL_ARGS, result)

        args_call, kwargs = registry._redis_client.client.set.call_args
        assert args_call[0] == expected_key

    @pytest.mark.asyncio
    async def test_set_serializes_tool_result(self):
        """写入去重记录的value是ToolResult的JSON序列化"""
        registry = _make_registry()
        result = _make_result(data={"taskId": "task_999"})

        await registry.set("sess1", _TOOL_NAME, _TOOL_ARGS, result)

        args_call, kwargs = registry._redis_client.client.set.call_args
        serialized = args_call[1]
        assert '"taskId":"task_999"' in serialized or '"taskId": "task_999"' in serialized


class TestRegistrySessionIsolation:
    """会话隔离测试"""

    @pytest.mark.asyncio
    async def test_different_sessions_no_cross_hit(self):
        """不同session_id相同调用参数,不会命中同一去重记录"""
        registry = _make_registry()
        registry._redis_client.client.get = AsyncMock(return_value=None)

        cached1 = await registry.get("sess1", _TOOL_NAME, _TOOL_ARGS)
        cached2 = await registry.get("sess2", _TOOL_NAME, _TOOL_ARGS)

        assert cached1 is None
        assert cached2 is None
        # 两次调用使用了不同的key
        assert registry._redis_client.client.get.call_count == 2
        k1 = registry._redis_client.client.get.call_args_list[0].args[0]
        k2 = registry._redis_client.client.get.call_args_list[1].args[0]
        assert k1 != k2


class TestRegistryFullFlow:
    """完整去重流程测试(模拟首次发起→写入→二次命中)"""

    @pytest.mark.asyncio
    async def test_full_flow_first_miss_then_hit(self):
        """首次发起未命中→写入去重→第二次发起命中(返回上次调用结果)"""
        result = _make_result(data={"taskId": "task_001", "status": "completed"})
        registry = _make_registry(dedup_tools=["mcp_test_asyncTask"])

        # 首次调用:未命中
        registry._redis_client.client.get = AsyncMock(return_value=None)
        cached1 = await registry.get("sess1", _TOOL_NAME, _TOOL_ARGS)
        assert cached1 is None

        # 写入去重记录(模拟工具调用成功后写入)
        await registry.set("sess1", _TOOL_NAME, _TOOL_ARGS, result)

        # 二次调用:命中(返回上次的调用结果)
        registry._redis_client.client.get = AsyncMock(return_value=result.model_dump_json())
        cached2 = await registry.get("sess1", _TOOL_NAME, _TOOL_ARGS)
        assert cached2 is not None
        assert cached2.data["taskId"] == "task_001"
        assert cached2.data["status"] == "completed"
