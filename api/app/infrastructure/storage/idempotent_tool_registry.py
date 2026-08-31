#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2026/07/20

@File    : idempotent_tool_registry.py
幂等工具调用去重注册表 - 防止LLM在会话中重复发起相同参数的幂等写操作

设计要点:
1.场景: 部分工具(如异步任务发起、报表导出、邮件发送等)是幂等写操作,不在结果缓存白名单,
  但LLM在长会话中可能因记忆压缩而重复发起相同调用,造成额外耗时(任务发起+轮询等待)
2.去重策略: 按 (session_id + tool_name + sorted_args) 计算签名,在TTL窗口内
  命中时直接返回上次的调用结果(任务ID/状态/下载链接),不发起实际调用
3.隔离粒度: 会话级(session_id),不同会话互不影响
4.静默降级: 注册表读写异常一律 warning + 放行实际调用,不阻塞主流程
5.参考实现: 复用 ToolResultCache 的 Redis + SHA256 + Pydantic 模式

通用性设计:
- 类名/字段/注释均不绑定具体业务场景(导出/邮件/订单等),由配置层声明需去重的工具白名单
- 默认白名单为空,确保安全(未声明的工具不去重);业务方在config.yaml中按需配置
- TTL为全局统一值,覆盖一轮完整会话即可,无需按工具差异化配置
"""
import hashlib
import json
import logging
from typing import Any, Dict, Optional

from app.domain.models.tool_result import ToolResult
from app.infrastructure.storage.redis import RedisClient

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 3600  # 默认去重TTL(秒),1小时覆盖一轮会话
_DEFAULT_KEY_PREFIX = "tool_dedup"  # 默认key前缀,命名空间隔离


class IdempotentToolRegistry:
    """幂等工具调用去重注册表

    封装RedisClient实现对幂等写操作工具的会话级调用去重。
    命中时返回上次的ToolResult(含任务ID/状态),避免LLM重复发起相同调用。

    与 ToolResultCache 的区别:
    - ToolResultCache: 缓存幂等查询工具结果(读操作),避免重复查询开销
    - IdempotentToolRegistry: 去重幂等写操作工具调用(如异步任务发起),避免重复发起任务
    """

    def __init__(
            self,
            redis_client: RedisClient,
            ttl: int = _DEFAULT_TTL,
            key_prefix: str = _DEFAULT_KEY_PREFIX,
            dedup_tools: Optional[list] = None,
    ) -> None:
        """构造函数,完成去重注册表参数初始化

        Args:
            redis_client: RedisClient实例(复用其连接池,便于测试mock)
            ttl: 去重存活时间(秒),默认1小时覆盖一轮会话
            key_prefix: key前缀,用于命名空间隔离
            dedup_tools: 需去重的工具名列表(幂等写操作,如异步任务发起类工具)
        """
        self._redis_client = redis_client
        self._ttl = ttl
        self._key_prefix = key_prefix
        self._dedup_tools = set(dedup_tools or [])

    def is_dedupable(self, tool_name: str, function_args: Dict[str, Any]) -> bool:
        """判断工具调用是否需要去重: 仅对配置的幂等写操作工具生效

        MCP直接加载模式: MCP工具以 mcp_* 前缀直接作为 tool_name 传入,
        直接匹配 dedup_tools 白名单(支持完整名或去掉mcp_前缀的短名匹配)。

        注: 旧版桥接工具(mcp_tool_call)已彻底移除,不再注册为可用工具,
        LLM无法产生该工具调用,故无需保留其向后兼容分支。

        Args:
            tool_name: ReAct调用入口工具名(如 mcp_amap_maps_weather)
            function_args: 工具参数字典

        Returns:
            True 表示需要去重, False 表示不去重
        """
        # MCP直接加载模式: mcp_* 工具名直接匹配白名单(两种匹配)
        if tool_name.startswith("mcp_"):
            if tool_name in self._dedup_tools:
                return True
            short_name = tool_name[4:]
            if short_name and short_name in self._dedup_tools:
                return True
            return False
        return False

    def _make_key(self, session_id: str, tool_name: str, function_args: Dict[str, Any]) -> str:
        """生成确定性去重key: sha256(session_id|tool_name|sorted_args_json)

        sorted 序列化保证参数顺序无关,语义相同的参数命中同一去重记录。
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
        """查询去重记录: 命中返回上次的ToolResult,未命中或异常返回None

        Args:
            session_id: 会话ID(用于会话级隔离)
            tool_name: 工具名(如 mcp_* 直接MCP工具名)
            function_args: 工具参数

        Returns:
            命中返回ToolResult(上次调用结果),未命中/异常返回None
        """
        try:
            key = self._make_key(session_id, tool_name, function_args)
            data = await self._redis_client.client.get(key)
            if not data:
                return None
            return ToolResult.model_validate_json(data)
        except Exception as e:
            logger.warning(
                f"幂等去重记录读取失败,放行实际调用: tool={tool_name}, session={session_id}, error={str(e)}"
            )
            return None

    async def set(
            self,
            session_id: str,
            tool_name: str,
            function_args: Dict[str, Any],
            result: ToolResult,
    ) -> None:
        """写入去重记录: 异常仅warning,不影响工具结果返回

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
                f"幂等去重记录写入失败,跳过: tool={tool_name}, session={session_id}, error={str(e)}"
            )
