#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2026/07/20

@File    : tool_cache.py
工具结果Redis缓存 - 工具结果缓存:对幂等工具调用结果做短期缓存,避免重复调用开销。

设计要点:
1.白名单机制: 仅缓存 ToolCacheConfig.cacheable_tools 中声明的工具,默认不缓存
2.会话隔离: key 包含 session_id,避免跨会话污染
3.参数确定性: 用 sorted JSON 序列化参数,保证相同语义参数命中同一缓存
4.静默降级: 缓存读写异常一律 warning + 跳过,不阻塞主流程
5.参考实现: 复用 SearchCache 的 Redis + SHA256 + Pydantic 模式
"""
import hashlib
import json
import logging
from typing import Any, Dict, Optional

from app.domain.models.tool_result import ToolResult
from app.infrastructure.storage.redis import RedisClient

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 1800  # 默认缓存TTL(秒),30分钟
_DEFAULT_KEY_PREFIX = "tool"  # 默认缓存key前缀


class ToolResultCache:
    """工具结果缓存,封装RedisClient实现幂等工具结果按 session_id+tool_name+args 缓存"""

    def __init__(
            self,
            redis_client: RedisClient,
            ttl: int = _DEFAULT_TTL,
            key_prefix: str = _DEFAULT_KEY_PREFIX,
            cacheable_tools: Optional[list] = None,
            cacheable_mcp_tools: Optional[list] = None,
    ) -> None:
        """构造函数,完成缓存参数初始化

        Args:
            redis_client: RedisClient实例(复用其连接池,便于测试mock)
            ttl: 缓存存活时间(秒)
            key_prefix: 缓存key前缀,用于命名空间隔离
            cacheable_tools: 可缓存工具白名单(仅幂等查询类工具)
            cacheable_mcp_tools: 可缓存的MCP实际工具名白名单(支持 mcp_* 完整名或去掉前缀的子名)
        """
        self._redis_client = redis_client
        self._ttl = ttl
        self._key_prefix = key_prefix
        self._cacheable_tools = set(cacheable_tools or [])
        self._cacheable_mcp_tools = set(cacheable_mcp_tools or [])

    def is_cacheable(self, tool_name: str, function_args: Dict[str, Any]) -> bool:
        """判断工具调用是否可缓存: 白名单匹配,MCP工具按实际工具名匹配

        MCP直接加载模式: MCP工具以 mcp_* 前缀直接作为 tool_name 传入,
        匹配 cacheable_mcp_tools 白名单(支持完整名或去掉mcp_前缀的短名匹配)。

        注: 旧版桥接工具(mcp_tool_call)已彻底移除,不再注册为可用工具,
        LLM无法产生该工具调用,故无需保留其向后兼容分支。

        Args:
            tool_name: ReAct调用入口工具名(如 web_search/mcp_amap_weather)
            function_args: 工具参数字典

        Returns:
            True 表示可缓存, False 表示不可缓存
        """
        # 1.直接匹配白名单
        if tool_name in self._cacheable_tools:
            return True
        # 2.MCP直接加载模式: tool_name以mcp_开头,匹配缓存白名单(支持两种匹配)
        if tool_name.startswith("mcp_"):
            # 完整名匹配: mcp_system_getWarehousingDetailExport
            if tool_name in self._cacheable_mcp_tools:
                return True
            # 短名匹配: system_getWarehousingDetailExport(去掉mcp_前缀)
            short_name = tool_name[4:]
            if short_name and short_name in self._cacheable_mcp_tools:
                return True
            return False
        return False

    def _make_key(self, session_id: str, tool_name: str, function_args: Dict[str, Any]) -> str:
        """生成确定性缓存key: sha256(session_id|tool_name|sorted_args_json)

        sorted 序列化保证参数顺序无关,语义相同的参数命中同一缓存。
        """
        stable_args = json.dumps(function_args, sort_keys=True, ensure_ascii=False)
        raw = f"{session_id}|{tool_name}|{stable_args}"
        key_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"{self._key_prefix}:{key_hash}"

    async def get(
            self,
            session_id: str,
            tool_name: str,
            function_args: Dict[str, Any],
    ) -> Optional[ToolResult]:
        """查询缓存: 命中返回ToolResult,未命中或异常返回None(不阻塞主流程)

        Args:
            session_id: 会话ID(用于会话级隔离)
            tool_name: 工具名
            function_args: 工具参数

        Returns:
            命中返回ToolResult,未命中/异常返回None
        """
        try:
            key = self._make_key(session_id, tool_name, function_args)
            data = await self._redis_client.client.get(key)
            if not data:
                return None
            return ToolResult.model_validate_json(data)
        except Exception as e:
            logger.warning(
                f"工具缓存读取失败,跳过缓存: tool={tool_name}, session={session_id}, error={str(e)}"
            )
            return None

    async def set(
            self,
            session_id: str,
            tool_name: str,
            function_args: Dict[str, Any],
            result: ToolResult,
    ) -> None:
        """写入缓存: 异常仅warning,不影响工具结果返回

        Args:
            session_id: 会话ID
            tool_name: 工具名
            function_args: 工具参数
            result: 工具执行结果(仅成功结果应写入)
        """
        try:
            key = self._make_key(session_id, tool_name, function_args)
            await self._redis_client.client.set(key, result.model_dump_json(), ex=self._ttl)
        except Exception as e:
            logger.warning(
                f"工具缓存写入失败,跳过缓存: tool={tool_name}, session={session_id}, error={str(e)}"
            )
