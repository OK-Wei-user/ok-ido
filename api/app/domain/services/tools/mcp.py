#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/27 9:43

@File    : mcp.py
"""
import asyncio
import json
import logging
import os
import uuid
from contextlib import AsyncExitStack
from typing import Optional, Dict, List, Any
from urllib.parse import urlparse

import anyio
import httpx

from mcp import ClientSession, Tool, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from app.application.errors.exceptions import NotFoundError
from app.domain.external.task_callback import TaskCallbackManager
from app.domain.models.app_config import MCPConfig, MCPServerConfig, MCPTransport
from app.domain.models.tool_result import ToolResult
from app.infrastructure.metrics.poll_stats import PollStatsCollector
from .base import BaseTool

"""
MCP客户端管理器的开发思路:
1.在Agent执行的过程中，有可能需要调用多次工具,
  但是因为MCP工具的每次获取都需要调用客户端会话的list_tools()方法,
  非常耗时, 所以需要我们缓存工具的参数信息, 只有在初始化的时候才调用一次,
  并且在销毁MCP客户端管理器的时候一并清除;
2.在前端UI交互中, 无论MCP服务是否启动, 都会显示工具列表信息,
  但是在Agent执行的过程中, 我们只会传递已启动的MCP服务,
  所以对于MCP客户端管理器来说, 可以根据接收的MCP配置的差异加载不同的服务器,
  而不是仅从配置文件中读取数据;
3.MCP客户端管理器会同时管理多个MCP服务, 有可能有stdio、sse、streamable_http等传输协议.
  需要根据传输协议的不同来创建客户端会话(ClientSession), 同时缓存会话;
4.另外有可能有一些环境变量是存储在我们整个系统中的, 在初始化MCP服务的时候，需要将传递进来的
  环境变量与系统的环境变量进行合并后传递给MCP服务;
5.使用AsyncExitStack异步上下文管理器来管理上下文，避免使用with多层嵌套;
6.MCPClientManager的初始化非常耗时, 所以需要有机制可以判断避免重复初始化;
7.由于config.yaml是直接暴露在项目中的, 所以在使用config.yaml进行初始化的时候必须二次校验;
8.同时缓存ClientSession+Tool-Schema, 一个是客户端会话, 一个是工具参数声明;
9.MCP客户端管理器在清除/停止使用的时候, 必须关闭异步上下文管理器、清除资源(ClientSession、Tool-Schema)、
  初始化标识等, 从而避免资源泄露;
"""

logger = logging.getLogger(__name__)

_MCP_CONNECT_TIMEOUT = 10  # MCP服务器HTTP连接超时(秒)
_MCP_HEALTH_CHECK_TIMEOUT = 5  # MCP服务器健康检查超时(秒)

# MCP调用层超时常量（使用anyio.fail_after，asyncio.wait_for无法取消anyio cancel scope内的协程）
_MCP_INIT_TIMEOUT = 30        # session.initialize() 握手超时(秒)
_MCP_LIST_TOOLS_TIMEOUT = 15  # session.list_tools() 工具列表获取超时(秒)
_MCP_INVOKE_TIMEOUT = 120     # session.call_tool() 工具调用超时(秒)

# 批次45 P1-1: 同步超时自动转异步开关
# True时同步调用超时后代码层直接启动后台任务,不依赖LLM follow hint文本(批次44验证task_wait=0)
_MCP_SYNC_TIMEOUT_AUTO_ASYNC = True

# 工具响应最大长度(字符): 超限截断保留头尾,避免大响应撑爆上下文窗口
_MAX_TOOL_RESPONSE_LENGTH = 30000


def _truncate_tool_response(text: str, max_length: int = _MAX_TOOL_RESPONSE_LENGTH) -> str:
    """截断过长的工具响应文本,保留头尾并插入省略提示

    大响应(如全量数据导出)直接返回会撑爆LLM上下文窗口,
    截断后保留前半段(关键信息通常在开头)和后半段(汇总/结论通常在末尾)。
    """
    if len(text) <= max_length:
        return text
    half = max_length // 2
    return (
        f"{text[:half]}\n\n"
        f"...(已截断,原始长度{len(text)}字符)...\n\n"
        f"{text[-half:]}"
    )


def _append_hint_to_result(data: Any, hint: str) -> Any:
    """在工具结果末尾追加提示文本(提示文本自带前缀,如[系统提示])

    Args:
        data: 原始结果(str或dict或None)
        hint: 追加的提示文本(含前缀)

    Returns:
        追加提示后的结果(保持原类型):
        - str: 末尾追加提示
        - dict且含text字段: text字段末尾追加提示
        - 其他: 原样返回(防御性,不破坏非标准格式)
    """
    if isinstance(data, str):
        return data + f"\n\n{hint}"
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        result = dict(data)
        result["text"] = result["text"] + f"\n\n{hint}"
        return result
    return data


# ---------------------------------------------------------------------------
# 查询类MCP工具重复轮询检测(三重感知:工具类型+参数+结果内容)
# ---------------------------------------------------------------------------

# 查询类工具关键词(名称含这些词的工具才可能触发轮询检测)
_QUERY_TOOL_KEYWORDS = frozenset({"list", "query", "status", "task", "search", "check", "fetch"})
# 排除关键词(名称含这些词的工具不视为查询类,即使包含查询关键词)
# 根因会话72d71cc6: 含get前缀的导出类工具被误判为查询类,导致按月提交被加退避提示
_NON_QUERY_TOOL_KEYWORDS = frozenset({
    "export", "create", "submit", "delete", "update",
    "add", "remove", "modify", "insert", "save", "upload",
})
# 异步任务未完成状态词(结果含这些词时才追加退避提示,避免"狼来了"效应)
_PENDING_STATE_KEYWORDS = frozenset({
    "处理中", "进行中", "未完成", "等待中", "排队中", "生成中",
    "pending", "processing", "running", "queued", "in_progress", "in-progress",
})
# 递增退避等待时间(秒),索引对应(调用次数-3),第3/4/5次分别建议60/120/180秒
_BACKOFF_SECONDS = (60, 120, 180)
# 参数级轮询触发阈值:同一查询(工具+参数完全相同)连续3次触发退避提示
_PARAM_BACKOFF_THRESHOLD = 3
# 工具级轮询触发阈值:同一查询类工具(不论参数)累计4次pending结果触发退避提示
# 根因会话d71e315f: LLM交替查询status=0/status=1规避参数级检测,需工具级累计计数
_TOOL_BACKOFF_THRESHOLD = 4
# 最大轮询次数提示阈值,达到此次数后强制建议停止轮询
_MAX_POLL_THRESHOLD = 10

# MCP 异步任务轮询常量(P11,与退避检测常量对齐)
# 后台轮询递增退避序列(秒),与 _BACKOFF_SECONDS 一致
_MCP_POLL_BACKOFF_SECONDS = (60, 120, 180)
# 后台轮询最大尝试次数,与 _MAX_POLL_THRESHOLD 一致
_MCP_POLL_MAX_ATTEMPTS = 10


def _is_query_tool(tool_name: str) -> bool:
    """判断MCP工具是否为查询类工具(可触发轮询检测)

    精确化检测: 名称含查询关键词且不含排除关键词。
    排除导出/创建/删除类工具,避免对提交异步任务的操作误加退避提示。
    """
    name_lower = tool_name.lower()
    # 先检查排除关键词(优先级高)
    if any(kw in name_lower for kw in _NON_QUERY_TOOL_KEYWORDS):
        return False
    # 再检查查询关键词
    return any(kw in name_lower for kw in _QUERY_TOOL_KEYWORDS)


def _extract_result_text(data: Any) -> str:
    """从ToolResult.data中提取文本内容(用于状态检测)"""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        text = data.get("text") or data.get("result") or data.get("data") or ""
        if isinstance(text, str):
            return text
        try:
            return json.dumps(text, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(text)
    return str(data) if data else ""


def _contains_pending_state(text: str) -> bool:
    """检测结果文本是否包含异步任务未完成状态词

    仅在任务确实未完成时返回True,触发退避提示。
    任务已完成或返回正常列表数据时返回False,不追加提示(避免"狼来了"效应)。
    """
    if not text:
        return False
    text_lower = text.lower()
    for keyword in _PENDING_STATE_KEYWORDS:
        if keyword in text_lower:
            return True
    return False


def _build_backoff_hint(call_count: int, tool_name: str, is_tool_level: bool = False) -> str:
    """构建递增退避提示文本

    Args:
        call_count: 当前连续查询次数(>=触发阈值)
        tool_name: MCP工具名称
        is_tool_level: 是否为工具级触发(频繁查询同一工具,含参数切换),True时优先引导更换查询参数

    Returns:
        退避提示文本,包含参数更换建议(工具级)或递增等待建议(参数级)
    """
    if call_count >= _MAX_POLL_THRESHOLD:
        stop_hint = "基于已有数据推进任务,或向用户报告当前进度与等待原因"
        if is_tool_level:
            stop_hint = (
                "基于已有数据推进任务,或向用户报告当前进度与等待原因。"
                "如仍需查询,必须更换查询参数(不传status或按fileName精确查询)"
            )
        return (
            f"[系统提示] 这是第{call_count}次查询'{tool_name}'且任务仍处理中。"
            f"已超过最大轮询次数({_MAX_POLL_THRESHOLD}次),请**停止轮询**,{stop_hint}。"
        )

    # 递增退避: 第3次→60s, 第4次→120s, 第5次→180s
    backoff_idx = min(call_count - _PARAM_BACKOFF_THRESHOLD, len(_BACKOFF_SECONDS) - 1)
    wait_seconds = _BACKOFF_SECONDS[backoff_idx]
    next_wait = _BACKOFF_SECONDS[min(backoff_idx + 1, len(_BACKOFF_SECONDS) - 1)]

    if is_tool_level:
        # 工具级触发:频繁查询同一工具(含参数切换),优先引导更换查询参数而非强制sleep
        # 根因: LLM查status=0发现目标任务消失后,想查status=1确认完成,却被强制sleep 120s
        # 修复: 将参数更换建议作为主要引导,sleep作为次要选项
        return (
            f"[系统提示] 这是第{call_count}次查询'{tool_name}'且任务仍处理中"
            f"(检测到频繁查询同一工具)。**建议立即更换查询参数**: "
            f"推荐不传status一次查询所有状态,或按fileName精确查询目标任务——"
            f"任务完成后会从处理中列表消失,继续用相同参数查询无法发现已完成的任务。"
            f"如仍需等待,执行 shell_execute(sleep {wait_seconds}) 后再用**新参数**查询。"
            f"超过{_MAX_POLL_THRESHOLD}次后请停止轮询并报告进度。"
        )

    # 参数级触发:相同参数重复查询,引导退避等待
    return (
        f"[系统提示] 这是第{call_count}次查询'{tool_name}'且任务仍处理中。"
        f"请执行 shell_execute(sleep {wait_seconds}) 等待后再查询,"
        f"下次建议等待{next_wait}秒。超过{_MAX_POLL_THRESHOLD}次后请停止轮询并报告进度。"
    )


class MCPClientManager:
    """MCP客户端管理器"""

    def __init__(self, mcp_config: Optional[MCPConfig] = None) -> None:
        """构造函数，完成MCP客户端管理器的初步初始化"""
        self._mcp_config: MCPConfig = mcp_config  # mcp配置信息
        self._exit_stack: AsyncExitStack = AsyncExitStack()  # 异步上下文管理器
        self._clients: Dict[str, ClientSession] = {}  # 缓存的客户端会话
        self._tools: Dict[str, List[Tool]] = {}  # 缓存的MCP工具参数声明
        self._initialized: bool = False  # 是否初始化标识

    @property
    def tools(self) -> Dict[str, List[Tool]]:
        """只读属性，返回缓存的MCP工具参数声明，键就是服务名字，值就是服务对应的工具声明"""
        return self._tools

    async def initialize(self) -> None:
        """初始化函数，用于连接所有配置的MCP服务器"""
        # 1.检查下是否已经初始化成功
        if self._initialized:
            return

        try:
            # 2.记录日志并连接MCP服务器
            logger.info(f"从config.yaml中加载了{len(self._mcp_config.mcpServers)}个MCP服务器")
            await self._connect_mcp_servers()
            self._initialized = True
            logger.info("MCP客户端管理器加载成功")
        except Exception as e:
            # 3.记录错误信息并直接抛出
            logger.error(f"MCP客户端管理器加载失败: {str(e)}")
            raise

    @staticmethod
    async def _check_server_reachable(url: str, timeout: float = _MCP_HEALTH_CHECK_TIMEOUT) -> bool:
        """快速检查MCP服务器是否网络可达，避免不可达服务器导致anyio任务组异常"""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                await client.get(url)
            return True
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError):
            return False
        except Exception:
            # 收到HTTP响应（即使是错误状态码）也说明服务器可达
            return True

    async def _connect_mcp_servers(self) -> None:
        """根据配置连接所有MCP服务,先并行预检可达性再顺序连接

        并行预检: 多个HTTP可达性检查同时进行,避免串行等待超时。
        顺序连接: AsyncExitStack要求enter_async_context和aclose在同一Task中,
        因此连接操作不能通过asyncio.gather并行(会创建子Task导致anyio cancel scope冲突)。
        """
        if not self._mcp_config or not self._mcp_config.mcpServers:
            return

        # 1.并行预检所有HTTP类服务的可达性
        reachability_tasks = {}
        for server_name, server_config in self._mcp_config.mcpServers.items():
            if server_config.url and server_config.transport != MCPTransport.STDIO:
                reachability_tasks[server_name] = self._check_server_reachable(server_config.url)

        reachable_results: Dict[str, Any] = {}
        if reachability_tasks:
            results = await asyncio.gather(*reachability_tasks.values(), return_exceptions=True)
            reachable_results = dict(zip(reachability_tasks.keys(), results))

        # 2.顺序连接可达的服务(AsyncExitStack要求同一Task内连接和清理)
        failed_servers: List[str] = []
        for server_name, server_config in self._mcp_config.mcpServers.items():
            # 跳过预检不可达的HTTP服务
            if server_name in reachable_results:
                result = reachable_results[server_name]
                if isinstance(result, Exception) or result is False:
                    logger.warning(f"MCP服务器[{server_name}]预检不可达,跳过连接")
                    failed_servers.append(server_name)
                    continue

            try:
                await self._connect_mcp_server(server_name, server_config)
            except asyncio.CancelledError:
                logger.error(f"连接MCP服务器[{server_name}]时任务被取消（通常是服务不可达）")
                failed_servers.append(server_name)
                continue
            except Exception as e:
                logger.error(f"连接MCP服务器[{server_name}]出错: {str(e)}")
                failed_servers.append(server_name)
                continue

        # 3.输出连接汇总日志
        total = len(self._mcp_config.mcpServers)
        succeeded = total - len(failed_servers)
        if failed_servers:
            logger.warning(f"MCP服务器连接完成: 成功{succeeded}/{total}, 失败: {failed_servers}")
        else:
            logger.info(f"MCP服务器连接完成: 全部{succeeded}个服务器连接成功")

    async def _connect_mcp_server(self, server_name: str, server_config: MCPServerConfig) -> None:
        """根据传递的服务名字+服务配置连接到单个MCP服务"""
        try:
            # 1.获取mcp服务的传输协议
            transport = server_config.transport

            # 2.根据不同的传输协议调用不同的方法连接MCP服务器
            if transport == MCPTransport.STDIO:
                await self._connect_stdio_server(server_name, server_config)
            elif transport == MCPTransport.SSE:
                await self._connect_sse_server(server_name, server_config)
            elif transport == MCPTransport.STREAMABLE_HTTP:
                await self._connect_streamable_http_server(server_name, server_config)
            else:
                raise ValueError(f"MCP服务[{server_name}]使用了不支持的传输协议: {transport}")
        except Exception as e:
            # 3.记录日志并抛出异常
            logger.error(f"连接MCP服务器[{server_name}]出错: {str(e)}")
            raise

    async def _connect_stdio_server(self, server_name: str, server_config: MCPServerConfig) -> None:
        """根据服务名字+配置连接stdio服务"""
        # 1.从配置中提取相关命令信息
        command = server_config.command
        args = server_config.args
        env = server_config.env

        # 2.检查command是否存在
        if not command:
            raise ValueError("连接stdio-mcp服务器需要配置command命令")

        # 3.构建stdio连接参数
        server_parameters = StdioServerParameters(
            command=command,
            args=args,
            env={**os.environ, **env},
        )

        try:
            # 4.使用异步上下文管理器创建传输协议
            stdio_transport = await self._exit_stack.enter_async_context(
                stdio_client(server_parameters),
            )
            read_stream, write_stream = stdio_transport

            # 5.根据读取与写入流构建会话
            session: ClientSession = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream),
            )

            # 6.初始化MCP服务会话（握手超时保护）
            try:
                with anyio.fail_after(_MCP_INIT_TIMEOUT):
                    await session.initialize()
            except TimeoutError:
                raise RuntimeError(f"stdio-mcp服务器[{server_name}]握手超时(>{_MCP_INIT_TIMEOUT}s)")

            # 7.缓存对应的mcp连接客户端
            self._clients[server_name] = session

            # 8.缓存对应mcp服务的工具列表
            await self._cache_mcp_server_tools(server_name, session)
            logger.info(f"连接stdio-mcp服务器成功: {server_name}")
        except Exception as e:
            # 记录错误日志并直接抛出异常
            logger.error(f"连接stdio-mcp服务器失败: {str(e)}")
            raise

    async def _connect_sse_server(self, server_name: str, server_config: MCPServerConfig) -> None:
        """根据服务名字+配置连接sse服务"""
        # 1.提取sse服务器的连接url并判断是否存在
        url = server_config.url
        if not url:
            raise ValueError("连接sse-mcp服务器需要配置url")

        # 2.预检：快速检查服务器是否网络可达，避免不可达时anyio任务组抛出CancelledError
        if not await self._check_server_reachable(url):
            raise ConnectionError(f"sse-mcp服务器[{server_name}]不可达: {url}")

        try:
            # 3.建立sse连接（设置较短连接超时）
            sse_transport = await self._exit_stack.enter_async_context(
                sse_client(url=url, headers=server_config.headers, timeout=_MCP_CONNECT_TIMEOUT),
            )
            read_stream, write_stream = sse_transport

            # 4.创建客户端会话
            session: ClientSession = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream),
            )

            # 5.初始化MCP服务会话（握手超时保护）
            try:
                with anyio.fail_after(_MCP_INIT_TIMEOUT):
                    await session.initialize()
            except TimeoutError:
                raise RuntimeError(f"sse-mcp服务器[{server_name}]握手超时(>{_MCP_INIT_TIMEOUT}s)")

            # 6.缓存对应的mcp连接客户端
            self._clients[server_name] = session

            # 7.缓存对应mcp服务的工具列表
            await self._cache_mcp_server_tools(server_name, session)
            logger.info(f"连接sse-mcp服务器成功: {server_name}")
        except Exception as e:
            # 8.记录错误日志并直接抛出异常
            logger.error(f"连接sse-mcp服务器失败: {str(e)}")
            raise

    async def _connect_streamable_http_server(self, server_name: str, server_config: MCPServerConfig) -> None:
        """根据服务名字+配置连接streamable-http服务"""
        # 1.提取streamable-http服务器的连接url并判断是否存在
        url = server_config.url
        if not url:
            raise ValueError("连接streamable-http-mcp服务器需要配置url")

        # 2.预检：快速检查服务器是否网络可达，避免不可达时anyio任务组抛出CancelledError
        if not await self._check_server_reachable(url):
            raise ConnectionError(f"streamable-http-mcp服务器[{server_name}]不可达: {url}")

        try:
            # 3.连接streamable-http服务（设置较短连接超时）
            streamable_http_transport = await self._exit_stack.enter_async_context(
                streamablehttp_client(url=url, headers=server_config.headers, timeout=_MCP_CONNECT_TIMEOUT),
            )

            # 4.streamable-http模型需要解包获取输入与输出流
            if len(streamable_http_transport) == 3:
                read_stream, write_stream, _ = streamable_http_transport
            else:
                read_stream, write_stream = streamable_http_transport

            # 5.创建客户端会话
            session: ClientSession = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream),
            )

            # 6.初始化MCP服务会话（握手超时保护）
            try:
                with anyio.fail_after(_MCP_INIT_TIMEOUT):
                    await session.initialize()
            except TimeoutError:
                raise RuntimeError(f"streamable-http-mcp服务器[{server_name}]握手超时(>{_MCP_INIT_TIMEOUT}s)")

            # 7.缓存对应的mcp连接客户端
            self._clients[server_name] = session

            # 8.缓存对应mcp服务的工具列表
            await self._cache_mcp_server_tools(server_name, session)
            logger.info(f"连接streamable-http-mcp服务器成功: {server_name}")
        except Exception as e:
            # 9.记录错误日志并直接抛出异常
            logger.error(f"连接streamable-http-mcp服务器失败: {str(e)}")
            raise

    async def _cache_mcp_server_tools(self, server_name: str, session: ClientSession) -> None:
        """根据传递的服务名字+会话缓存mcp服务工具列表

        超时时不阻断连接，设置空工具列表允许连接成功（后续可重试获取）。
        """
        try:
            with anyio.fail_after(_MCP_LIST_TOOLS_TIMEOUT):
                tools_response = await session.list_tools()
            tools = tools_response.tools if tools_response else []
            self._tools[server_name] = tools
            logger.info(f"MCP服务器[{server_name}]提供了{len(tools)}个工具")
        except TimeoutError:
            # 超时设置空工具列表，允许连接成功（不阻断整个连接流程）
            logger.warning(f"获取MCP服务器[{server_name}]工具列表超时(>{_MCP_LIST_TOOLS_TIMEOUT}s)，设置为空列表")
            self._tools[server_name] = []
        except Exception as e:
            # 记录日志并将缓存设置为空
            logger.error(f"获取MCP服务器[{server_name}]工具列表失败: {str(e)}")
            self._tools[server_name] = []

    async def get_all_tools(self) -> List[Dict[str, Any]]:
        """获取所有MCP工具列表，返回LLM可以使用的工具参数声明列表并处理MCP的名字"""
        # 1.定义一个变量存储所有结果
        all_tools = []

        # 2.循环遍历所有缓存的工具
        for server_name, tools in self._tools.items():
            # 3.循环取出每个MCP服务的工具列表
            for tool in tools:
                # 4.修改工具名字加上mcp_前缀+服务名字
                if server_name.startswith("mcp_"):
                    tool_name = f"{server_name}_{tool.name}"
                else:
                    tool_name = f"mcp_{server_name}_{tool.name}"

                # 5.生成OpenAI工具描述
                tool_schema = {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": f"{tool.description or tool.name} [来源: {server_name}]",
                        "parameters": tool.inputSchema,
                    }
                }
                all_tools.append(tool_schema)

        return all_tools

    async def invoke(self, tool_name: str, arguments: Dict[str, Any]) -> ToolResult:
        """根据传递的工具名字+参数调用MCP工具"""
        try:
            # 1.定义变量存储原始的服务名字+工具
            original_server_name = None
            original_tool_name = None

            # 2.循环遍历当前的所有mcp服务配置
            for server_name in self._mcp_config.mcpServers.keys():
                # 3.为server_name组装前缀
                expected_prefix = server_name if server_name.startswith("mcp_") else f"mcp_{server_name}"

                # 4.判断工具名字是否以该服务名字为开头
                if tool_name.startswith(f"{expected_prefix}_"):
                    # 5.取出原始的服务名字+工具名字
                    original_server_name = server_name
                    original_tool_name = tool_name[len(expected_prefix) + 1:]
                    break

            # 6.判断服务名字+工具是否都存在
            if not original_server_name or not original_tool_name:
                raise NotFoundError(f"服务器解析MCP工具不存在: {tool_name}")

            # 7.获取该工具所属的会话
            session = self._clients.get(original_server_name)
            if not session:
                return ToolResult(success=False, message=f"MCP服务器[{original_server_name}]未连接")

            # 8.强制转换LLM参数类型，匹配工具inputSchema声明
            #    防止LLM传thoughtNumber="1"(字符串)导致MCP类型校验失败无限重试(会话154ee551: 235次连续失败)
            tool_schema = None
            for cached_tool in self._tools.get(original_server_name, []):
                if cached_tool.name == original_tool_name:
                    tool_schema = cached_tool.inputSchema
                    break
            if tool_schema:
                arguments = self._coerce_arguments_by_schema(arguments, tool_schema)

            # 9.使用会话调用工具（超时保护：anyio.fail_after可取消anyio cancel scope内的协程，
            #    asyncio.wait_for无法取消，会话184bcad0验证3min+不超时）
            #    超时返回含 _timeout 标记的 ToolResult,由上层 MCPTool.invoke() 决策异步回退
            #    (MCPClientManager 不持有 callback_manager/background_tasks,无法自行转异步)
            try:
                with anyio.fail_after(_MCP_INVOKE_TIMEOUT):
                    result = await session.call_tool(original_tool_name, arguments)
            except TimeoutError:
                logger.error(f"调用MCP工具[{tool_name}]超时(>{_MCP_INVOKE_TIMEOUT}s)")
                # 返回含超时标记的 ToolResult,由 MCPTool.invoke() 调用 _auto_fallback_to_async
                return ToolResult(
                    success=False,
                    message=(
                        f"调用MCP工具[{tool_name}]超时(>{_MCP_INVOKE_TIMEOUT}s)。"
                        f"此工具可能为异步任务,系统将自动转异步并返回task_id,"
                        f"请用 task_wait(task_id) 等待完成,避免同步阻塞。"
                    ),
                    data={
                        "_timeout": True,
                        "_tool_name": original_tool_name,
                        "_arguments": arguments,
                    },
                )

            # 10.判断结果是否存在执行不同的操作
            if result:
                # 10.处理MCP工具生成的content：分离文本与图片
                text_parts, images = self._parse_mcp_content(result)
                text_data = "\n".join(text_parts) if text_parts else "工具执行成功"
                # 截断大响应,避免撑爆上下文窗口(保留头尾+省略提示)
                text_data = _truncate_tool_response(text_data)
                if images:
                    # 含图片：返回结构化data，供多模态消息构建器提取image_url块
                    return ToolResult(success=True, data={
                        "text": text_data,
                        "images": images,
                    })
                return ToolResult(success=True, data=text_data)
            else:
                return ToolResult(success=True, data="工具执行成功")
        except Exception as e:
            # 记录错误日志并返回失败的工具结果
            logger.error(f"调用MCP工具[{tool_name}]失败: {str(e)}")
            return ToolResult(
                success=False,
                message=f"调用MCP工具[{tool_name}]失败: {str(e)}",
            )

    def _coerce_arguments_by_schema(
        self, arguments: Dict[str, Any], input_schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        """根据工具inputSchema强制转换LLM参数类型

        LLM频繁将数值/布尔参数传为字符串(如thoughtNumber="1"而非1)，
        导致MCP服务器类型校验失败。本方法按schema properties的类型声明做转换。

        转换规则：
        - string→integer: 仅当字符串为纯数字
        - string→number: 仅当字符串为合法数值
        - string→boolean: "true"/"false"(不区分大小写)→True/False
        - string→array: JSON数组字符串→list
        - string→object: JSON对象字符串→dict
        - schema无type字段时: 按参数名关键词推断数值类型
        """
        if not arguments or not input_schema:
            return arguments

        properties = input_schema.get("properties", {})
        if not properties:
            return arguments

        coerced = dict(arguments)
        for key, value in coerced.items():
            if key not in properties:
                continue
            prop_schema = properties[key]
            coerced[key] = self._coerce_single_value(key, value, prop_schema)
        return coerced

    def _coerce_single_value(
        self, key: str, value: Any, prop_schema: Dict[str, Any]
    ) -> Any:
        """转换单个参数值"""
        # 非字符串值无需转换
        if not isinstance(value, str):
            return value

        prop_type = prop_schema.get("type")
        if not prop_type:
            # schema无type字段：按参数名关键词推断
            return self._infer_untyped_value(key, value)

        try:
            if prop_type == "integer":
                if value.strip().lstrip("-").isdigit():
                    return int(value)
            elif prop_type == "number":
                return float(value)
            elif prop_type == "boolean":
                if value.lower() == "true":
                    return True
                if value.lower() == "false":
                    return False
            elif prop_type == "array":
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            elif prop_type == "object":
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
        except (ValueError, TypeError, json.JSONDecodeError):
            # 转换失败保留原值，让MCP服务器报具体错误
            pass
        return value

    @staticmethod
    def _infer_untyped_value(key: str, value: str) -> Any:
        """schema无type字段时，按参数名关键词推断数值类型

        参数名含number/count/total/index/seq/thought等关键词时推断为整数。
        """
        numeric_keywords = ("number", "count", "total", "index", "seq", "thought", "step", "limit", "size")
        if any(kw in key.lower() for kw in numeric_keywords):
            try:
                if value.strip().lstrip("-").isdigit():
                    return int(value)
                return float(value)
            except ValueError:
                pass
        return value

    @staticmethod
    def _parse_mcp_content(result: Any) -> tuple[List[str], List[Dict[str, str]]]:
        """解析MCP工具返回的content列表，分离文本与图片内容。
        MCP协议支持TextContent(type=text)与ImageContent(type=image,data=base64,mimeType)。
        图片以{"data": base64, "mime_type": str}结构返回，供多模态消息构建器使用。"""
        text_parts: List[str] = []
        images: List[Dict[str, str]] = []
        if not hasattr(result, "content") or not result.content:
            return text_parts, images
        for item in result.content:
            item_type = getattr(item, "type", None)
            if item_type == "image" or (hasattr(item, "data") and not hasattr(item, "text")):
                img_data = getattr(item, "data", "")
                img_mime = getattr(item, "mimeType", None) or getattr(item, "mime_type", None) or "image/png"
                if isinstance(img_data, str) and img_data:
                    images.append({"data": img_data, "mime_type": img_mime})
            elif hasattr(item, "text"):
                text_parts.append(item.text)
            else:
                text_parts.append(str(item))
        return text_parts, images

    async def cleanup(self) -> None:
        """当退出MCP服务时，清除对应资源

        该方法是幂等的，多次调用不会产生副作用。
        注意：必须在初始化MCP的同一个asyncio Task中调用此方法，
        否则anyio会因cancel scope上下文不匹配而抛出RuntimeError。
        """
        # 幂等检查：如果未初始化则跳过清理
        if not self._initialized:
            return

        try:
            await self._exit_stack.aclose()
            logger.info("清除MCP客户端管理器成功")
        except RuntimeError as e:
            # 防御性处理：anyio.create_task_group() 在不同任务中退出的已知问题
            if "Attempted to exit cancel scope in a different task" in str(e):
                logger.warning(f"清理MCP客户端管理器时遇到任务上下文切换警告（可忽略）: {str(e)}")
            else:
                logger.error(f"清理MCP客户端管理器失败: {str(e)}")
        except Exception as e:
            logger.error(f"清理MCP客户端管理器失败: {str(e)}")
        finally:
            # 无论aclose()是否成功，都必须清除缓存并重置状态
            self._clients.clear()
            self._tools.clear()
            self._initialized = False


_SANDBOX_PATH_PREFIXES = ("/home/ubuntu/", "/tmp/", "/root/")


def _is_sandbox_path(value: str) -> bool:
    """检测字符串是否为沙箱环境路径"""
    return isinstance(value, str) and any(value.startswith(p) for p in _SANDBOX_PATH_PREFIXES)


class MCPTool(BaseTool):
    """MCP工具包，包含所有已配置+已启动的MCP工具

    直接加载模式:
        MCP工具在初始化时全量加载,直接作为LLM可用工具暴露。
        配合F10-6工具按需装配机制(base.py的_TOOL_KEYWORD_MAP),
        按步骤描述关键词过滤MCP工具,控制单轮token消耗。

    设计权衡(移除懒加载桥接架构):
        原桥接架构每次MCP调用需search→describe→call三步,
        额外LLM轮次token远大于schema节省量(会话7ae8a99f验证)。
        F10-6按步骤过滤已能有效控制token,无需桥接工具间接调用。
        直接加载让LLM一眼看到工具schema,减少调用步骤与认知负担。
    """

    name: str = "mcp"

    def __init__(
        self,
        sandbox=None,
        callback_manager: Optional[TaskCallbackManager] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """构造函数,完成MCP工具包初始化

        Args:
            sandbox: 沙箱实例(用于沙箱路径自动上传)
            callback_manager: 异步任务回调管理器(P11,可选)
                - 已注入: 同步超时自动转异步,后台轮询完成时通知 task_wait
                - None: 同步超时降级为超时引导文本(向后兼容)
            session_id: 会话 ID(批次 26,用于 PollStatsCollector 会话级统计)
                - 已注入: 启用 MCP_POLL_STATS 结构化日志与统计收集
                - None: 不收集统计(向后兼容,不影响核心功能)
        """
        super().__init__()
        self._initialized: bool = False
        self._tools = []
        self._manager: MCPClientManager = None
        self._sandbox = sandbox
        self._mcp_config: Optional[MCPConfig] = None
        # F10-7/P11: 异步任务回调管理器(用于同步超时自动转异步)
        self._callback_manager = callback_manager
        # 批次 26: 会话 ID 与统计收集器(用于退避机制覆盖率统计)
        self._session_id = session_id
        self._poll_stats = PollStatsCollector.get_or_create(session_id) if session_id else None
        # 后台异步任务追踪表: task_id -> asyncio.Task(用于会话停止时取消)
        self._background_tasks: Dict[str, "asyncio.Task"] = {}
        # 重复轮询检测,记录最近一次查询类MCP调用的工具名+参数key
        # 参数级:同一工具+同一参数的连续调用计数(精确相同查询)
        self._last_query_key: Optional[str] = None
        self._query_call_count: int = 0
        # 工具级:同一查询类工具的pending结果累计计数(不论参数)
        # 根因会话d71e315f: LLM交替查询status=0/status=1规避参数级检测
        self._last_poll_tool: Optional[str] = None
        self._tool_pending_count: int = 0
        # 连续非pending结果计数,连续2次非pending才认为任务完成并重置工具级计数
        # 避免单次非pending查询(如查status=1已完成任务)误重置计数器
        self._consecutive_non_pending: int = 0
        # 批次 29 新增: 非查询类工具(导出/生成类)的连续失败重试检测
        # 根因会话0e57b5a4: getWarehousingDetailExport 同步重试 32 次无任何系统提示
        self._last_fail_key: Optional[str] = None
        self._non_query_fail_count: int = 0

    async def initialize(self, mcp_config: Optional[MCPConfig] = None) -> None:
        """初始化MCP工具包，仅连接enabled=true的MCP服务"""
        if not self._initialized:
            self._mcp_config = mcp_config
            enabled_servers = {
                name: cfg for name, cfg in mcp_config.mcpServers.items()
                if cfg.enabled
            }
            skipped = set(mcp_config.mcpServers.keys()) - set(enabled_servers.keys())
            if skipped:
                logger.info(f"跳过已禁用的MCP服务: {skipped}")
            filtered_config = MCPConfig(mcpServers=enabled_servers)

            self._manager = MCPClientManager(mcp_config=filtered_config)
            await self._manager.initialize()

            self._tools = await self._manager.get_all_tools()
            self._initialized = True
            logger.info(f"MCP工具直接加载完成: {len(self._tools)}个工具")

    def get_tools(self) -> List[Dict[str, Any]]:
        """获取工具包下的所有工具列表(直接加载,返回全部MCP工具schema)"""
        return self._tools

    def get_tools_summary(self) -> str:
        """获取MCP工具摘要文本(用于Planner系统提示,不含完整schema)

        直接加载模式下返回完整工具名+描述摘要,
        ReAct阶段通过F10-6按需装配机制加载匹配步骤的工具schema。
        """
        if not self._tools:
            return ""
        lines = []
        for tool_schema in self._tools:
            fn = tool_schema["function"]
            name = fn["name"]
            # 去除[来源: xxx]后缀,摘要更简洁
            desc = (fn["description"] or "").split("[来源:")[0].strip()
            lines.append(f"  - {name}: {desc}")
        return "可用MCP工具:\n" + "\n".join(lines)

    def has_tool(self, tool_name: str) -> bool:
        """传递工具名字判断工具是否存在"""
        for tool in self._tools:
            if tool["function"]["name"] == tool_name:
                return True
        return False

    async def invoke(self, tool_name: str, **kwargs) -> ToolResult:
        """调用MCP工具(直接调用,含沙箱路径解析与重复轮询检测)

        直接加载模式下,LLM通过工具名直接调用MCP工具,无需桥接工具中转。
        保留重复轮询检测与退避提示,防止LLM对pending状态任务无效轮询。
        同步超时自动转异步机制(本方法内处理)保障长耗时任务不阻塞会话。

        超时处理流程(批次45 P1-1):
            MCPClientManager.invoke() 用 anyio.fail_after 检测超时,
            返回含 _timeout 标记的 ToolResult;
            本方法检测标记后调用 _auto_fallback_to_async 启动后台轮询,
            返回 task_id + task_wait 引导(不依赖LLM follow hint文本)。
        """
        # 自动解析沙箱路径(如参数含/home/ubuntu/xxx路径,自动上传到MCP服务)
        kwargs = await self._resolve_sandbox_paths(tool_name, kwargs)
        # 直接调用MCP工具
        result = await self._manager.invoke(tool_name, kwargs)
        # 超时检测: MCPClientManager返回_timeout标记时,尝试自动转异步
        if (
            isinstance(result.data, dict)
            and result.data.get("_timeout") is True
            and _MCP_SYNC_TIMEOUT_AUTO_ASYNC
        ):
            timeout_tool = result.data.get("_tool_name", tool_name)
            timeout_args = result.data.get("_arguments", kwargs)
            async_result = await self._auto_fallback_to_async(timeout_tool, timeout_args)
            if async_result is not None:
                return async_result
            # 自动转异步失败(无callback_manager或异常),降级为原超时引导
        # 重复轮询检测:查询类工具的pending状态连续查询时追加退避提示
        self._detect_and_hint_repeated_query(tool_name, kwargs, result)
        # 记录MCP工具调用统计(批次26)
        self._record_invoke_stats(tool_name, result)
        return result

    def _detect_and_hint_repeated_query(
        self, tool_name: str, args: Dict[str, Any], result: ToolResult
    ) -> None:
        """检测查询类MCP工具的重复轮询,在结果中追加递增退避提示

        四重感知机制(根因会话72d71cc6/5c8d9c88/d71e315f):
        1. 工具类型感知: 仅查询类工具(list/query/status/task等)生效,
           排除导出/创建类工具(含export/create等),避免误判含get前缀的导出类工具
        2. 结果内容感知: 仅当返回结果含"处理中/pending/processing"等未完成状态词时
           才追加退避提示,任务已完成时不追加(避免"狼来了"效应降低提示信任度)
        3. 参数级感知: 工具名+参数序列化作为key,相同key的连续调用计数,
           达3次触发退避提示(精确相同查询的重复检测)
        4. 工具级感知: 同一查询类工具的pending结果累计计数(不论参数),
           达4次触发退避提示(参数切换轮询检测)
           根因会话d71e315f: LLM交替查询status=0/status=1规避参数级检测

        计数器重置策略:
        - 参数级: 不同参数或非pending结果立即重置
        - 工具级: 工具切换时重置;非pending结果时不立即重置,
          需连续2次非pending才认为任务完成并重置(避免查status=1误重置)

        递增退避: 第3次起提示,建议等待时间递增(60s→120s→180s),
        第5次起强制建议停止轮询并基于已有数据推进任务。

        Args:
            tool_name: MCP工具名称
            args: 调用参数(用于参数感知的重复检测)
            result: 工具调用结果(会被原地修改,追加退避提示)
        """
        # 1.工具类型感知: 仅查询类工具的 pending 状态检测生效
        if not _is_query_tool(tool_name):
            # 批次 29 修复: 非查询类工具(导出/生成类)的连续失败也需检测,
            # 避免同步重试陷入循环(会话1 getWarehousingDetailExport 重复调用 32 次无任何提示)。
            # 仅对失败结果计数,成功结果重置计数。
            self._maybe_hint_failed_retry(tool_name, args, result)
            return
        if not result.success or not result.data:
            return

        # 工具切换时重置工具级计数(不同工具的轮询独立计数)
        if tool_name != self._last_poll_tool:
            self._last_poll_tool = tool_name
            self._tool_pending_count = 0
            self._consecutive_non_pending = 0

        # 2.结果内容感知
        result_text = _extract_result_text(result.data)
        is_pending = _contains_pending_state(result_text)

        # 3.参数级key构建
        args_key = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        query_key = f"{tool_name}:{args_key}"

        if is_pending:
            # pending结果:重置连续非pending计数,累计工具级计数
            self._consecutive_non_pending = 0
            self._tool_pending_count += 1

            # 参数级计数:相同key连续调用才累计
            if query_key == self._last_query_key:
                self._query_call_count += 1
            else:
                self._last_query_key = query_key
                self._query_call_count = 1

            # 4.退避提示触发:参数级3次或工具级4次,取较大值
            if self._query_call_count >= _PARAM_BACKOFF_THRESHOLD:
                hint = _build_backoff_hint(self._query_call_count, tool_name)
                result.data = _append_hint_to_result(result.data, hint)
                # 批次 26: 记录参数级退避触发统计
                if self._poll_stats:
                    self._poll_stats.record_backoff_trigger(tool_name, "param")
                logger.info(
                    f"MCP_POLL_STATS: session={self._session_id} tool={tool_name} "
                    f"level=param call_count={self._query_call_count}"
                )
            elif self._tool_pending_count >= _TOOL_BACKOFF_THRESHOLD:
                hint = _build_backoff_hint(
                    self._tool_pending_count, tool_name, is_tool_level=True
                )
                result.data = _append_hint_to_result(result.data, hint)
                # 批次 26: 记录工具级退避触发统计
                if self._poll_stats:
                    self._poll_stats.record_backoff_trigger(tool_name, "tool")
                logger.info(
                    f"MCP_POLL_STATS: session={self._session_id} tool={tool_name} "
                    f"level=tool call_count={self._tool_pending_count}"
                )
        else:
            # 非pending结果:重置参数级计数
            self._last_query_key = None
            self._query_call_count = 0
            # 工具级计数:连续2次非pending才重置(避免查status=1误重置)
            self._consecutive_non_pending += 1
            if self._consecutive_non_pending >= 2:
                self._tool_pending_count = 0
                self._consecutive_non_pending = 0

    def _maybe_hint_failed_retry(
        self, tool_name: str, args: Dict[str, Any], result: ToolResult
    ) -> None:
        """对非查询类工具(导出/生成类)的连续失败调用注入 task_wait 异步回退引导

        批次 29 新增: 原代码对导出/生成类工具完全跳过退避检测,
        导致 getWarehousingDetailExport 同步重试 32 次无任何系统提示,LLM 陷入循环。

        策略:
        - 成功结果: 重置计数,不提示
        - 失败结果: 累计计数,第3次起注入 task_wait 异步回退引导
        - 参数变化: 重置计数(不同参数是不同业务调用)

        Args:
            tool_name: MCP工具名称
            args: 调用参数(用于参数感知的重复检测)
            result: 工具调用结果(会被原地修改,追加引导提示)
        """
        # 成功结果: 重置计数,不提示
        if result.success:
            self._non_query_fail_count = 0
            self._last_fail_key = None
            return

        # 参数级 key 构建: 相同工具+相同参数才累计(不同参数是不同业务调用)
        args_key = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        fail_key = f"{tool_name}:{args_key}"

        if fail_key != self._last_fail_key:
            # 参数变化,重置计数
            self._last_fail_key = fail_key
            self._non_query_fail_count = 1
        else:
            self._non_query_fail_count += 1

        # 第3次起注入异步重试引导
        if self._non_query_fail_count >= 3:
            hint = (
                f"\n\n[系统提示] 工具[{tool_name}]已连续失败"
                f"{self._non_query_fail_count}次。同步超时后系统会自动转异步并返回task_id,"
                f"请用 task_wait(task_id) 等待完成,避免同步重试循环。"
            )
            result.message = (result.message or "") + hint
            if self._poll_stats:
                self._poll_stats.record_backoff_trigger(tool_name, "non_query_retry")
            logger.info(
                f"MCP_POLL_STATS: session={self._session_id} tool={tool_name} "
                f"level=non_query_retry fail_count={self._non_query_fail_count}"
            )

    def _record_invoke_stats(self, tool_name: str, result: ToolResult) -> None:
        """记录 MCP 工具调用统计(批次 26)

        根据 result.data 内容判断是否为 pending 状态,记录到 PollStatsCollector。
        仅当 session_id 已注入(即 _poll_stats 非 None)时生效。

        Args:
            tool_name: MCP 工具名
            result: 工具调用结果
        """
        if not self._poll_stats:
            return
        result_text = _extract_result_text(result.data)
        is_pending = _contains_pending_state(result_text)
        self._poll_stats.record_invoke(tool_name, is_pending)

    async def _auto_fallback_to_async(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Optional[ToolResult]:
        """同步超时自动转异步(批次45 P1-1)

        同步调用超时后,代码层直接启动后台轮询任务,返回 task_id + task_wait 引导,
        不依赖 LLM follow hint 文本(批次44验证 LLM 不主动使用 async_mode)。

        复用 _run_mcp_async_polling 后台轮询逻辑(与同步超时自动转异步路径一致)。

        Args:
            tool_name: MCP工具名(已解析后的 original_tool_name)
            arguments: 工具参数

        Returns:
            含 task_id 的成功 ToolResult; 无 callback_manager 或异常时返回 None,
            调用方降级为原超时引导(向后兼容)。
        """
        if not self._callback_manager:
            logger.info(f"P1-1自动转异步跳过: 无callback_manager, tool={tool_name}")
            return None
        try:
            mcp_task_id = f"mcp_{uuid.uuid4().hex[:12]}"
            await self._callback_manager.register(mcp_task_id)
            background_task = asyncio.create_task(
                self._run_mcp_async_polling(
                    task_id=mcp_task_id, target_name=tool_name, target_args=arguments,
                ),
                name=f"mcp_async_{mcp_task_id}",
            )
            self._background_tasks[mcp_task_id] = background_task
            if self._poll_stats:
                self._poll_stats.record_async_task(tool_name)
            logger.info(
                f"MCP_POLL_STATS: session={self._session_id} tool={tool_name} "
                f"event=auto_async_fallback task_id={mcp_task_id}"
            )
            return ToolResult(
                success=True,
                message=(
                    f"调用MCP工具[{tool_name}]同步超时,已自动转为异步任务(task_id={mcp_task_id})。"
                    f"请调用 task_wait(task_id='{mcp_task_id}') 等待完成并获取结果。"
                ),
                data={
                    "task_id": mcp_task_id,
                    "status": "running",
                    "tool_name": tool_name,
                    "auto_async": True,
                },
            )
        except Exception as e:
            logger.warning(f"P1-1自动转异步失败,降级原超时引导: tool={tool_name}, error={e}")
            return None

    async def _run_mcp_async_polling(
        self,
        task_id: str,
        target_name: str,
        target_args: Dict[str, Any],
    ) -> None:
        """后台轮询 MCP 异步任务,完成时通知 task_wait(P11)

        策略:
        1. 立即调用MCP工具获取初始结果
        2. 若含 pending 状态,按递增退避(60/120/180s)再次调用相同参数查询
        3. 当返回非 pending 状态时,任务完成,notify task_wait
        4. 超过 _MCP_POLL_MAX_ATTEMPTS 次仍 pending,notify 失败状态

        异常隔离: 任何异常都不会抛出,统一转为失败 payload 通知等待方
        """
        payload: Dict[str, Any]
        try:
            attempt = 0
            result = await self._manager.invoke(target_name, target_args)
            result_text = _extract_result_text(result.data)
            is_pending = _contains_pending_state(result_text)

            while is_pending and attempt < _MCP_POLL_MAX_ATTEMPTS:
                wait_seconds = _MCP_POLL_BACKOFF_SECONDS[
                    min(attempt, len(_MCP_POLL_BACKOFF_SECONDS) - 1)
                ]
                logger.info(
                    f"MCP 异步任务轮询: task_id={task_id}, "
                    f"attempt={attempt + 1}/{_MCP_POLL_MAX_ATTEMPTS}, "
                    f"wait={wait_seconds}s, tool={target_name}"
                )
                await asyncio.sleep(wait_seconds)
                attempt += 1
                result = await self._manager.invoke(target_name, target_args)
                result_text = _extract_result_text(result.data)
                is_pending = _contains_pending_state(result_text)

            if is_pending:
                logger.warning(
                    f"MCP 异步任务超时: task_id={task_id}, tool={target_name}, "
                    f"已轮询{_MCP_POLL_MAX_ATTEMPTS}次仍处理中"
                )
                payload = {
                    "success": False,
                    "message": (
                        f"MCP 异步任务[{target_name}]已轮询"
                        f"{_MCP_POLL_MAX_ATTEMPTS}次仍处理中,"
                        f"建议向用户报告进度或基于已有数据推进任务。"
                    ),
                    "data": result.data,
                }
            else:
                logger.info(
                    f"MCP 异步任务完成: task_id={task_id}, tool={target_name}"
                )
                payload = {
                    "success": result.success,
                    "message": result.message or "",
                    "data": result.data,
                }
        except asyncio.CancelledError:
            logger.info(f"MCP 异步任务被取消: task_id={task_id}")
            payload = {"success": False, "message": "任务已取消", "data": None}
            raise
        except Exception as e:
            logger.exception(
                f"MCP 异步任务异常: task_id={task_id}, tool={target_name}, error={e}"
            )
            payload = {
                "success": False,
                "message": f"MCP 异步任务异常: {str(e)}",
                "data": None,
            }
        finally:
            try:
                await self._callback_manager.notify(task_id, payload)
            except Exception as notify_err:
                logger.error(
                    f"MCP 异步任务通知失败: task_id={task_id}, error={notify_err}"
                )
            self._background_tasks.pop(task_id, None)

    def cancel_background_tasks(self) -> None:
        """取消所有后台异步任务(会话停止时调用,P11)

        同步操作,不等待任务完成。已取消的任务会通过 _run_mcp_async_polling
        的 finally 块通知等待方(若存在)。
        """
        if not self._background_tasks:
            return
        for task_id, task in list(self._background_tasks.items()):
            if not task.done():
                task.cancel()
                logger.info(f"取消 MCP 异步任务: task_id={task_id}")
        self._background_tasks.clear()

    async def _resolve_sandbox_paths(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """检测并解析参数中的沙箱路径，自动上传文件到MCP服务

        当MCP工具参数包含沙箱路径时，MCP服务无法直接访问。
        此方法自动将沙箱文件上传到对应MCP服务的/upload端点，
        并将参数中的沙箱路径替换为upload://引用，对调用方透明。
        """
        if not self._sandbox or not self._mcp_config:
            return arguments

        resolved = dict(arguments)
        for key, value in resolved.items():
            if not _is_sandbox_path(value):
                continue

            upload_url = self._get_upload_url(tool_name)
            if not upload_url:
                logger.warning(f"无法确定MCP工具[{tool_name}]的上传端点，跳过沙箱路径解析")
                continue

            try:
                upload_ref = await self._upload_sandbox_file(value, upload_url)
                if upload_ref:
                    logger.info(f"沙箱文件自动上传: {value} -> {upload_ref}")
                    resolved[key] = upload_ref
            except Exception as e:
                logger.error(f"沙箱文件上传失败[{value}]: {e}")

        return resolved

    def _get_upload_url(self, tool_name: str) -> Optional[str]:
        """根据工具名推导对应MCP服务的上传端点URL"""
        if not self._mcp_config:
            return None

        for server_name, server_config in self._mcp_config.mcpServers.items():
            expected_prefix = server_name if server_name.startswith("mcp_") else f"mcp_{server_name}"
            if tool_name.startswith(f"{expected_prefix}_") and server_config.url:
                parsed = urlparse(server_config.url)
                return f"{parsed.scheme}://{parsed.netloc}/upload"

        return None

    async def _upload_sandbox_file(self, sandbox_path: str, upload_url: str) -> Optional[str]:
        """从沙箱下载文件并上传到MCP服务，返回upload://引用"""
        file_data = await self._sandbox.download_file(sandbox_path)
        if not file_data:
            raise RuntimeError(f"沙箱文件下载失败: {sandbox_path}")

        filename = os.path.basename(sandbox_path)
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                upload_url,
                files={"file": (filename, file_data)},
            )
            resp.raise_for_status()
            data = resp.json()

        upload_ref = data.get("upload_ref")
        if not upload_ref:
            raise RuntimeError(f"上传响应缺少upload_ref: {data}")
        return upload_ref

    async def cleanup(self) -> None:
        """清除MCP工具资源"""
        if self._manager:
            await self._manager.cleanup()
