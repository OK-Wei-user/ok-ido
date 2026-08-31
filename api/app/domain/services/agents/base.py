#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/19 15:31

@File    : base.py
"""
import asyncio
import json
import logging
import uuid
from abc import ABC
from dataclasses import dataclass, field
from typing import Optional, List, AsyncGenerator, Dict, Any, Callable, TYPE_CHECKING

from app.domain.external.json_parser import JSONParser
from app.domain.external.llm import LLM
from app.domain.models.app_config import AgentConfig, ToolExecutionConfig
from app.domain.models.event import ToolEvent, ToolEventStatus, ErrorEvent, MessageEvent, BaseEvent
from app.domain.models.memory import Memory, CompressionLevel, _HIGH_PRESSURE_TRUNCATE_MAX, _COMPRESSION_STRATEGY
from app.domain.models.message import Message
from app.domain.models.tool_result import ToolResult
from app.domain.repositories.uow import IUnitOfWork
from app.domain.services.observability import MetricsCollector
from app.domain.services.tools.base import BaseTool
from app.domain.services.tools.budget_tracker import ToolBudgetTracker
from app.domain.services.tools.concurrency import ToolConcurrencyClassifier
from app.infrastructure.external.llm.dsml_parser import strip_dsml_artifacts
from app.infrastructure.external.llm.stream_chunk import LLMStreamChunk
from app.infrastructure.external.llm.token_counter import TokenCounter

if TYPE_CHECKING:
    from app.infrastructure.storage.tool_cache import ToolResultCache
    from app.infrastructure.storage.idempotent_tool_registry import IdempotentToolRegistry
    from app.domain.models.file import File
    from app.domain.services.observability.shell_call_profiler import ShellCallProfiler

logger = logging.getLogger(__name__)

# 批次45 P1-2: shell_execute 频次主动引导阈值
# 累计调用达此值时事中注入脚本合并建议(每会话仅注入一次)
# 阈值依据: 批次44基线 shell=86,目标<50,15次时注入给LLM充足收敛空间
_SHELL_EXECUTE_GUIDANCE_THRESHOLD = 15


@dataclass
class _ParsedToolCall:
    """单个 tool_call 解析结果(F10-2 重构提取)

    统一串行/并行路径的解析逻辑输出,调用方据此决定:
    - error_content 非 None: 解析失败(畸形/未知工具),生成错误 tool_message,不产出 CALLING 事件
    - error_content 为 None: 正常路径,产出 CALLING 事件并执行工具
    """
    tool_call_id: str
    tool: Optional[BaseTool]  # None 表示解析失败(畸形/未知工具)
    function_name: str
    function_args: Dict[str, Any] = field(default_factory=dict)
    error_content: Optional[str] = None  # 非 None 表示需要生成错误 tool_message


def _merge_streaming_tool_calls(
    accumulated: List[Dict[str, Any]],
    delta_tool_calls: List[Dict[str, Any]],
) -> None:
    """将流式tool_calls增量合并到累积列表(P4-6流式改造)

    OpenAI SDK流式响应中,delta.tool_calls按index分片返回:
    - 首个分片: {index:0, id:"call_xxx", type:"function", function:{name:"web_search", arguments:""}}
    - 后续分片: {index:0, function:{arguments:'{"query":'}}
    - 末尾分片: {index:0, function:{arguments:'"hello"}'}}

    合并逻辑: 按index累积,id/type/function.name首次出现时设置,function.arguments累加。

    Args:
        accumulated: 累积的tool_calls列表(就地修改)
        delta_tool_calls: 本次chunk的tool_calls增量
    """
    for delta_tc in delta_tool_calls:
        idx = delta_tc.get("index", 0)
        # 扩展列表到idx位置(填充空占位)
        while len(accumulated) <= idx:
            accumulated.append({
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
        tc = accumulated[idx]
        if delta_tc.get("id"):
            tc["id"] = delta_tc["id"]
        if delta_tc.get("type"):
            tc["type"] = delta_tc["type"]
        delta_func = delta_tc.get("function", {})
        if delta_func.get("name"):
            tc["function"]["name"] = delta_func["name"]
        if delta_func.get("arguments"):
            tc["function"]["arguments"] += delta_func["arguments"]


class BaseAgent(ABC):
    """基础Agent智能体"""
    name: str = ""  # 智能体名字
    _system_prompt: str = ""  # 系统预设prompt
    _format: Optional[str] = None  # Agent的响应格式
    _retry_interval: float = 1.0  # 重试间隔
    _tool_choice: Optional[str] = None  # 强制选择工具
    # F10-9可观测性: 类级默认值,确保即使绕过__init__(如mock测试)访问也不抛AttributeError
    _metrics: Optional["MetricsCollector"] = None
    # Batch 39 / 方向3: 预算追踪器类级默认值(确保mock测试不抛AttributeError)
    _budget_tracker: Optional["ToolBudgetTracker"] = None
    # Batch 40 / 方向3: shell 调用画像器类级默认值
    _shell_profiler: Optional["ShellCallProfiler"] = None

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],
            session_id: str,  # 会话id
            agent_config: AgentConfig,  # Agent配置
            llm: LLM,  # 语言模型协议
            json_parser: JSONParser,  # JSON输出解析器
            tools: List[BaseTool],  # 工具列表
            token_counter: Optional[TokenCounter] = None,  # token计数器(可选)
            context_window: int = 64000,  # 上下文窗口大小(token)
            tool_cache: Optional["ToolResultCache"] = None,  # 工具结果缓存(可选)
            tool_execution_config: Optional[ToolExecutionConfig] = None,  # 工具并行执行配置(可选)
            idempotent_registry: Optional["IdempotentToolRegistry"] = None,  # 幂等工具调用去重注册表(P10-1,可选)
            metrics_collector: Optional[MetricsCollector] = None,  # 可观测性指标收集器(F10-9,可选)
            budget_tracker: Optional[ToolBudgetTracker] = None,  # Batch 39: 预算追踪器(方向2+3,可选)
    ) -> None:
        """构造函数，完成Agent的初始化"""
        self._uow_factory = uow_factory
        self._session_id = session_id
        self._agent_config = agent_config
        self._llm = llm
        self._memory: Optional[Memory] = None
        self._json_parser = json_parser
        self._tools = tools
        self._token_counter = token_counter
        self._context_window = context_window
        self._tool_cache = tool_cache  # 工具结果缓存,None时不启用缓存
        self._idempotent_registry = idempotent_registry  # 幂等工具调用去重注册表,None时不启用去重
        self._metrics = metrics_collector  # 可观测性指标收集器,None时不埋点
        self._budget_tracker = budget_tracker  # Batch 39: 预算追踪器,None时不启用预算观测
        # Batch 40 / 方向3: shell 调用画像器(记录每次 shell_execute 的调用模式,量化合并引导效果)
        from app.domain.services.observability.shell_call_profiler import ShellCallProfiler
        self._shell_profiler = ShellCallProfiler()
        # 批次45 P1-2: shell_execute 频次引导已注入标志(每会话仅注入一次,防重复)
        self._shell_guidance_injected: bool = False
        # Batch 39 / 方向3: 策略切换追踪(记录上次预算超限的工具,用于检测LLM是否切换策略)
        self._last_exceeded_tool: Optional[str] = None
        # 会话级超时熔断(P10-3): 记录会话开始时间戳,0表示不启用
        self._session_start_ts: float = 0.0
        # F10-6 工具按需装配: 当前步骤描述上下文,空串表示无上下文(走全量装配)
        # 由 ReActAgent.execute_step() 调用 set_step_context() 注入
        self._step_description: str = ""
        # 工具并行执行配置,默认关闭,启用时构建分类器与并发限制
        if tool_execution_config and tool_execution_config.enabled:
            self._concurrency_classifier = ToolConcurrencyClassifier(
                stateful_prefixes=tool_execution_config.stateful_tool_prefixes,
                stateful_names=tool_execution_config.stateful_tool_names,
                stateful_arg_keys=tool_execution_config.stateful_tool_arg_keys,
            )
            self._parallel_enabled = True
            self._max_concurrency = tool_execution_config.max_concurrency
            logger.info(
                f"Agent[{self.name}]启用并行工具执行, 最大并发: {self._max_concurrency}"
            )
        else:
            self._concurrency_classifier = None
            self._parallel_enabled = False
            self._max_concurrency = 1

    @property
    def _llm_supports_images(self) -> bool:
        """LLM是否支持图像输入(多模态)。

        安全访问_llm.supports_images: 测试mock未设置_llm时返回True(默认多模态),
        避免AttributeError破坏工具消息构建链路。
        supports_images=False时(如DeepSeek),工具结果截图不构建image_url块。
        """
        llm = getattr(self, "_llm", None)
        return getattr(llm, "supports_images", True)

    async def _ensure_memory(self) -> None:
        """确保智能体记忆是存在的"""
        if self._memory is None:
            async with self._uow_factory() as uow:
                self._memory = await uow.session.get_memory(self._session_id, self.name)

    def _get_available_tools(self) -> List[Dict[str, Any]]:
        """获取Agent所有可用的工具列表参数声明/Schema

        F10-6 工具按需装配: 当 tool_filter_enabled=True 且已注入步骤描述时,
        基于步骤关键词过滤工具,降低单轮 token 消耗。过滤后工具数低于
        tool_filter_min_tools 时回退全量装配,保证 LLM 可用工具的最低数量。
        """
        available_tools = []
        for tool in self._tools:
            available_tools.extend(tool.get_tools())

        # F10-6: 工具按需装配
        if self._agent_config.tool_filter_enabled and self._step_description:
            filtered = self._filter_tools_by_context(available_tools)
            if len(filtered) >= self._agent_config.tool_filter_min_tools:
                return filtered
            # 过滤后工具数不足,回退全量装配(保证 LLM 选择空间)
            logger.debug(
                f"Agent[{self.name}]工具过滤后仅{len(filtered)}个,低于阈值"
                f"{self._agent_config.tool_filter_min_tools},回退全量装配"
            )
        return available_tools

    def set_step_context(self, description: str) -> None:
        """注入当前步骤描述上下文,供工具按需装配(F10-6)使用

        由 ReActAgent.execute_step() 在执行步骤前调用,传入步骤描述。
        步骤执行完毕后应调用 reset_step_context() 清理,避免污染后续场景。

        Args:
            description: 当前步骤的描述文本,空串等价于清理上下文
        """
        self._step_description = description or ""
        self._force_included_tools: set = set()  # Batch 31: 每次设置上下文时重置强制包含集合

    def reset_step_context(self) -> None:
        """清理步骤上下文,恢复全量装配模式"""
        self._step_description = ""
        self._force_included_tools = set()  # Batch 31: 清理强制包含标记

    def force_include_tool(self, tool_name: str) -> None:
        """强制将指定工具加入当前步骤可用工具集(Batch 31, 非侵入式扩展)

        F10-6关键词过滤可能漏命中研究类步骤,此方法在步骤执行前
        强制注入指定工具(如deep_research),确保LLM可见。

        不侵入invoke循环: 仅扩展_filter_tools_by_context的过滤逻辑。
        """
        if not hasattr(self, '_force_included_tools'):
            self._force_included_tools = set()
        self._force_included_tools.add(tool_name)

    # F10-6 工具关键词映射表: 工具包名 → 触发关键词列表
    # 命中任一关键词则装配该工具包的全部工具
    _TOOL_KEYWORD_MAP: Dict[str, List[str]] = {
        "file": ["文件", "读取", "写入", "上传", "下载", "保存", "加载",
                 "创建文件", "删除文件", "移动文件", "file", "read", "write",
                 "upload", "download", "save", "load", "csv", "excel",
                 "xlsx", "json", "yaml", "txt", "md"],
        "shell": ["shell", "命令", "执行", "脚本", "python", "bash", "terminal",
                  "终端", "命令行", "运行", "pip", "npm", "运行脚本", "代码执行"],
        "browser": ["浏览器", "网页", "点击", "browser", "navigate", "web",
                    "page", "页面", "滚动", "screenshot", "截图", "登录网站",
                    "访问网站", "网页操作"],
        "search": ["搜索", "search", "web_search", "查询", "检索", "网络搜索",
                   "联网", "搜索引擎"],
        "deep_research": ["深度研究", "deep_research", "调研", "深度搜索",
                          "深度分析", "深度调研", "深入研究", "综合研究",
                          "趋势研究", "全面分析", "多角度分析", "深度挖掘"],
        "skill": ["技能", "skill", "应用技能", "脚本技能", "执行技能"],
        "a2a": ["a2a", "远程agent", "远程智能体", "协作agent", "远程协作"],
        # MCP工具直接加载: 步骤涉及外部系统接口或专业领域能力时,
        # 装配全部MCP工具(以mcp_前缀匹配),由F10-6控制单轮token消耗
        "mcp": ["mcp", "导出", "export", "天气", "weather", "地图", "map",
                "位置", "location", "翻译", "translate", "汇率", "exchange",
                "图片识别", "ocr", "视觉", "vision", "语音", "speech",
                "视频", "video", "库存", "入库", "出库", "仓储",
                "订单", "order", "报表", "report", "客户", "customer",
                "商品", "product", "采购", "单据", "业务数据",
                "多模态", "multimodal", "amap", "高德"],
    }

    # F10-6 始终装配的基础工具名(不受关键词过滤影响)
    # message_ask_user: 兜底交互能力(让 LLM 可主动询问用户)
    # 注: MCP桥接工具(mcp_tool_search/describe/call)已移除,
    #     MCP工具直接加载,F10-6通过"mcp"关键词包按需装配
    _ALWAYS_ON_TOOLS: frozenset = frozenset({
        "message_ask_user",
    })

    def _filter_tools_by_context(self, all_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """基于步骤描述关键词过滤工具列表(F10-6)

        策略:
        1. _ALWAYS_ON_TOOLS 始终保留(交互兜底)
        2. 按工具包关键词命中过滤: 步骤描述包含某工具包关键词时,装配该包全部工具
        3. 未命中任何关键词的工具包: 仅保留 _ALWAYS_ON_TOOLS 中的工具

        MCP工具直接加载: 步骤命中"mcp"关键词包(导出/天气/地图/库存等)时,
        装配全部mcp_前缀工具,由LLM从schema中选择合适工具直接调用。

        Args:
            all_tools: 全量工具 Schema 列表

        Returns:
            过滤后的工具 Schema 列表
        """
        if not all_tools:
            return all_tools

        step_desc_lower = self._step_description.lower()
        # 1.检测命中的工具包
        matched_packages: set = set()
        for pkg_name, keywords in self._TOOL_KEYWORD_MAP.items():
            for kw in keywords:
                if kw.lower() in step_desc_lower:
                    matched_packages.add(pkg_name)
                    break

        # 2.无任何命中时返回空列表(由调用方决定是否回退全量)
        if not matched_packages:
            logger.debug(
                f"Agent[{self.name}]步骤描述未命中任何工具关键词,过滤结果为空"
            )
            return []

        # 3.按工具名前缀过滤: 命中工具包的工具保留,_ALWAYS_ON_TOOLS 始终保留
        # Batch 31: force_included_tools 中的工具也始终保留(研究类步骤兜底)
        force_included = getattr(self, '_force_included_tools', set())
        filtered: List[Dict[str, Any]] = []
        for tool_schema in all_tools:
            func = tool_schema.get("function", {}) if isinstance(tool_schema, dict) else {}
            tool_name = func.get("name", "")
            if not tool_name:
                continue
            # 基础工具或强制包含工具始终保留
            if tool_name in self._ALWAYS_ON_TOOLS or tool_name in force_included:
                filtered.append(tool_schema)
                continue
            # 按工具包前缀匹配(file_read → file, shell_execute → shell 等)
            # 批次 29 修复: 单工具包(包名=工具名,如 deep_research)需精确匹配,
            # 原代码 tool_name.startswith(f"{pkg_name}_") 对单工具包永远返回 False,
            # 导致 deep_research 工具在 F10-6 启用时不被装配,LLM 完全看不到该工具。
            for pkg_name in matched_packages:
                if tool_name == pkg_name or tool_name.startswith(f"{pkg_name}_"):
                    filtered.append(tool_schema)
                    break

        logger.info(
            f"Agent[{self.name}]工具按需装配: 命中工具包{sorted(matched_packages)}, "
            f"过滤后工具数={len(filtered)}/{len(all_tools)}"
        )
        return filtered

    def _get_tool(self, tool_name: str) -> BaseTool:
        """获取对应工具所在的工具集/包"""
        # 1.循环遍历所有工具包
        for tool in self._tools:
            # 2.判断工具包中是否存在该工具
            if tool.has_tool(tool_name):
                return tool

        raise ValueError(f"未知工具: {tool_name}")

    async def _invoke_llm(
            self,
            messages: List[Dict[str, Any]],
            format: Optional[str] = None,
            tools_enabled: bool = True,
            tool_choice: Optional[str] = None,
    ) -> Dict[str, Any]:
        """调用语言模型并处理记忆内容

        新增tools_enabled参数,summarize等纯文本场景设为False,
              禁用工具传递,防止LLM返回tool_calls而非content。
        新增tool_choice参数,允许调用方临时覆盖Agent默认的tool_choice,
        用于强制工具调用场景(如多模态步骤LLM在JSON模式下不产出tool_calls时)。
        """
        await self._add_to_memory(messages)

        # 预测token压力,根据压缩策略决定是否主动压缩(P4-2自适应压缩策略)
        pressure = self._memory.predict_token_pressure(
            self._token_counter, self._context_window,
        )
        if pressure["should_emergency_compress"]:
            logger.warning(
                f"Agent[{self.name}]token压力critical"
                f"({pressure['projected_ratio']:.1%}),执行紧急压缩"
            )
            await self._overflow_recovery()
            if self._metrics:
                self._metrics.increment("emergency_compression_count")
        elif pressure["should_proactive_compress"] and self._should_proactive_compress():
            logger.info(
                f"Agent[{self.name}]token压力{pressure['pressure_level']}"
                f"({pressure['projected_ratio']:.1%}),执行主动压缩"
            )
            await self.compact_memory()
            if self._metrics:
                self._metrics.increment("compression_count")
        elif self._memory.should_compress(threshold=0.5):
            # 消息条数兜底
            await self.compact_memory()
            if self._metrics:
                self._metrics.increment("compression_count")

        if self._memory.is_context_overflow():
            await self._overflow_recovery()
            if self._metrics:
                self._metrics.increment("emergency_compression_count")

        response_format = {"type": format} if format else None

        error = ""
        for attempt in range(self._agent_config.max_retries):
            try:
                # tools_enabled=False时(如summarize)不传递tools/tool_choice,
                # 强制LLM生成文本内容而非工具调用
                active_tools = self._get_available_tools() if tools_enabled else None
                # tool_choice优先级: 调用方传入 > Agent默认 > None
                # 用于多模态步骤等需要强制工具调用的场景
                if tools_enabled:
                    active_tool_choice = tool_choice or self._tool_choice
                else:
                    active_tool_choice = None
                # F10-9可观测性: LLM调用计时+计数埋点
                timer = self._metrics.start_timer("llm_invoke") if self._metrics else None
                try:
                    message = await self._llm.invoke(
                        messages=self._memory.get_messages(),
                        tools=active_tools,
                        response_format=response_format,
                        tool_choice=active_tool_choice,
                    )
                finally:
                    if timer:
                        timer.__exit__(None, None, None)

                if message.get("role") == "assistant":
                    if not message.get("content") and not message.get("tool_calls"):
                        logger.warning("LLM回复了空内容，执行重试")
                        await self._add_to_memory([
                            {"role": "assistant", "content": ""},
                            {"role": "user", "content": "AI无响应内容，请继续。"}
                        ])
                        await asyncio.sleep(self._retry_interval)
                        continue

                    # F10-9可观测性: LLM成功响应埋点(计数+token估算)
                    if self._metrics:
                        self._metrics.increment("llm_call_count")
                        if self._token_counter:
                            try:
                                in_tokens = self._token_counter.count_messages(
                                    self._memory.get_messages()
                                )
                                out_tokens = self._token_counter.count_messages(
                                    [{"role": "assistant", "content": message.get("content") or ""}]
                                )
                                self._metrics.increment("llm_token_input_total", in_tokens)
                                self._metrics.increment("llm_token_output_total", out_tokens)
                            except Exception as e:
                                logger.debug(f"token计数埋点失败(降级忽略): {e}")

                    # 构建过滤后的assistant消息，保留必要字段
                    filtered_message = {"role": "assistant", "content": message.get("content")}
                    if message.get("tool_calls"):
                        # 工具调用场景：必须保留reasoning_content，否则DeepSeek V4 API返回400
                        # 工具并行执行: 并行启用时保留全部tool_calls,否则截断为单工具(保持向后兼容)
                        if self._parallel_enabled:
                            filtered_message["tool_calls"] = message.get("tool_calls")
                        else:
                            filtered_message["tool_calls"] = message.get("tool_calls")[:1]
                        if "reasoning_content" in message:
                            filtered_message["reasoning_content"] = message.get("reasoning_content", "")
                    elif "reasoning_content" in message and message.get("reasoning_content"):
                        # 非工具调用场景：仅保留非空的reasoning_content
                        filtered_message["reasoning_content"] = message.get("reasoning_content")
                else:
                    # 10.非AI消息则记录日志并存储message
                    logger.warning(f"LLM响应内容无法确认消息角色: {message.get('role')}")
                    filtered_message = message

                # 11.将消息添加到记忆中
                await self._add_to_memory([filtered_message])
                # LLM调用后驱逐浏览器临时数据: 截图+页面快照仅在当前决策时需要,不持久化到记忆
                # 根因(会话392252b6): 多次browser_view截图累积~15K tokens,
                # 挤占有效上下文,LLM陷入"view空→console_exec→navigate"循环(44次操作)
                # 扩展(参考9e1e5363): 页面快照(interactive_elements/ref_map/content)同样
                # 为临时数据,决策后应驱逐,避免旧快照过期(ref漂移)导致LLM误操作
                if self._memory:
                    self._memory.evict_image_data()
                    self._memory.evict_browser_view_content()
                # Batch 33: LLM调用边界统一刷盘
                await self._flush_memory()
                return filtered_message
            except Exception as e:
                error_detail = str(e) or type(e).__name__
                error = error_detail
                if "context_length_exceeded" in error_detail or "maximum context" in error_detail.lower():
                    logger.warning(f"LLM上下文溢出，执行紧急压缩后重试 (尝试{attempt + 1}/{self._agent_config.max_retries})")
                    await self._overflow_recovery()
                    continue
                # DeepSeek V4思考模式：reasoning_content缺失导致400错误时自动修复
                if "400" in error_detail and "reasoning_content" in error_detail.lower():
                    logger.warning(f"reasoning_content缺失导致400错误，尝试修复记忆后重试 (尝试{attempt + 1}/{self._agent_config.max_retries})")
                    await self._repair_missing_reasoning()
                    continue
                # tool消息配对破坏导致400错误时,清理孤立tool消息后重试
                # 根因: emergency_compact等操作破坏assistant(tool_calls)→tool配对
                # 覆盖两类错误模式:
                # 1. "must be a response to a preceding message" — 孤立tool消息(无配对assistant)
                # 2. "insufficient tool messages following tool_calls message" /
                #    "must be followed by tool messages responding to each 'tool_call_id'"
                #    — assistant(tool_calls)的某些tool_call_id无对应tool消息
                err_lower = error_detail.lower()
                if "400" in error_detail and (
                    "must be a response to a preceding message" in err_lower
                    or "insufficient tool messages" in err_lower
                    or "must be followed by tool messages" in err_lower
                ):
                    logger.warning(
                        f"tool消息配对破坏导致400错误,执行配对修复后重试 "
                        f"(尝试{attempt + 1}/{self._agent_config.max_retries})"
                    )
                    await self._repair_tool_message_pairing()
                    continue
                logger.error(f"调用语言模型发生错误 (尝试{attempt + 1}/{self._agent_config.max_retries}): {error_detail}")
                await asyncio.sleep(self._retry_interval)
                continue

        if not error:
            error = "未知错误"
        # Batch 33: LLM调用边界统一刷盘(抛出前持久化已累积的变更)
        await self._flush_memory()
        raise RuntimeError(f"调用语言模型失败, 已达到最大重试次数({self._agent_config.max_retries}): {error}")

    async def _stream_llm_invoke(
            self,
            messages: List[Dict[str, Any]],
            format: Optional[str] = None,
            tools_enabled: bool = True,
            tool_choice: Optional[str] = None,
    ) -> AsyncGenerator[Any, None]:
        """流式调用LLM，实时推送delta_reasoning，最后yield完整message(P4-6流式改造)

        与_invoke_llm功能一致，但使用流式API调用LLM。
        核心改进：在LLM生成过程中，实时yield MessageEvent(is_thinking=True, is_streaming=True)
        推送delta_reasoning，让用户在LLM生成过程中即可看到思考过程，大幅降低感知时延。

        原_invoke_llm + _stream_thinking的伪流式模式：
        - 阻塞等待LLM完整响应(15-20s) → 切片推送reasoning_content
        - 用户在LLM生成过程中看不到任何输出

        新_stream_llm_invoke的真正流式模式：
        - LLM生成过程中实时推送delta_reasoning(1-2s内首片到达)
        - 流式完成后推送最终聚合事件 + yield完整message

        异常降级: 流式调用失败时，降级到非流式_invoke_llm，通过_stream_thinking推送思考内容。
        降级时传入空messages(避免重复添加),_invoke_llm使用已添加到内存的messages调用LLM。

        Args:
            messages: 要添加到记忆的消息列表
            format: 响应格式(None用Agent默认)
            tools_enabled: 是否启用工具调用
            tool_choice: 工具选择策略覆盖

        Yields:
            MessageEvent(is_thinking=True, is_streaming=True): delta_reasoning增量片段
            MessageEvent(is_thinking=True, is_final=True): 最终聚合思考事件(写DB)
            Dict[str, Any]: 完整的assistant消息(含content/reasoning_content/tool_calls)
        """
        # 1.内存管理（与_invoke_llm一致）
        await self._add_to_memory(messages)

        pressure = self._memory.predict_token_pressure(
            self._token_counter, self._context_window,
        )
        if pressure["should_emergency_compress"]:
            logger.warning(
                f"Agent[{self.name}]token压力critical"
                f"({pressure['projected_ratio']:.1%}),执行紧急压缩"
            )
            await self._overflow_recovery()
            if self._metrics:
                self._metrics.increment("emergency_compression_count")
        elif pressure["should_proactive_compress"] and self._should_proactive_compress():
            logger.info(
                f"Agent[{self.name}]token压力{pressure['pressure_level']}"
                f"({pressure['projected_ratio']:.1%}),执行主动压缩"
            )
            await self.compact_memory()
            if self._metrics:
                self._metrics.increment("compression_count")
        elif self._memory.should_compress(threshold=0.5):
            await self.compact_memory()
            if self._metrics:
                self._metrics.increment("compression_count")

        if self._memory.is_context_overflow():
            await self._overflow_recovery()
            if self._metrics:
                self._metrics.increment("emergency_compression_count")

        # 2.构建调用参数
        response_format = {"type": format} if format else None
        active_tools = self._get_available_tools() if tools_enabled else None
        active_tool_choice = tool_choice or self._tool_choice if tools_enabled else None

        # 3.流式调用LLM
        accumulated_content = ""
        accumulated_reasoning = ""
        accumulated_tool_calls: List[Dict[str, Any]] = []
        stream_cfg = self._agent_config

        timer = self._metrics.start_timer("llm_invoke") if self._metrics else None

        try:
            async for chunk in self._llm.astream(
                messages=self._memory.get_messages(),
                tools=active_tools,
                response_format=response_format,
                tool_choice=active_tool_choice,
                keep_response_format=True,
            ):
                # 实时推送delta_reasoning（真正流式，而非伪流式）
                if chunk.delta_reasoning:
                    accumulated_reasoning += chunk.delta_reasoning
                    if stream_cfg.stream_thinking:
                        yield MessageEvent(
                            message=chunk.delta_reasoning,
                            is_thinking=True,
                            is_streaming=True,
                        )
                # 累积delta_content（不直推前端，避免JSON片段乱码）
                if chunk.delta_content:
                    accumulated_content += chunk.delta_content
                # 累积delta_tool_calls（按index合并分片）
                if chunk.delta_tool_calls:
                    _merge_streaming_tool_calls(accumulated_tool_calls, chunk.delta_tool_calls)

            if timer:
                timer.__exit__(None, None, None)

            # 4.构建完整message
            cleaned_content = strip_dsml_artifacts(accumulated_content) if accumulated_content else ""

            if not cleaned_content and not accumulated_tool_calls:
                logger.warning(f"Agent[{self.name}]LLM流式响应为空内容")

            filtered_message: Dict[str, Any] = {"role": "assistant", "content": cleaned_content}

            if accumulated_tool_calls:
                # P4-6流式诊断: 检测空参数tool_call(流式累积可能丢失arguments分片)
                empty_arg_calls = [
                    tc for tc in accumulated_tool_calls
                    if not tc.get("function", {}).get("arguments")
                ]
                if empty_arg_calls:
                    logger.warning(
                        f"Agent[{self.name}]检测到{len(empty_arg_calls)}个空参数tool_call,"
                        f"函数名: {[tc['function']['name'] for tc in empty_arg_calls]},"
                        f"可能是流式累积丢失arguments分片或LLM异常输出"
                    )
                if self._parallel_enabled:
                    filtered_message["tool_calls"] = accumulated_tool_calls
                else:
                    filtered_message["tool_calls"] = accumulated_tool_calls[:1]
                if accumulated_reasoning:
                    filtered_message["reasoning_content"] = accumulated_reasoning
            elif accumulated_reasoning:
                filtered_message["reasoning_content"] = accumulated_reasoning

            # F10-9可观测性: LLM成功响应埋点
            if self._metrics:
                self._metrics.increment("llm_call_count")
                if self._token_counter:
                    try:
                        in_tokens = self._token_counter.count_messages(
                            self._memory.get_messages()
                        )
                        out_tokens = self._token_counter.count_messages(
                            [{"role": "assistant", "content": cleaned_content or ""}]
                        )
                        self._metrics.increment("llm_token_input_total", in_tokens)
                        self._metrics.increment("llm_token_output_total", out_tokens)
                    except Exception as e:
                        logger.debug(f"token计数埋点失败(降级忽略): {e}")

            # 5.将完整message添加到内存
            await self._add_to_memory([filtered_message])
            if self._memory:
                self._memory.evict_image_data()
                self._memory.evict_browser_view_content()
            await self._flush_memory()

            # 6.推送最终思考事件（is_final=True，写DB，前端替换累积结果）
            if accumulated_reasoning and stream_cfg.stream_thinking:
                yield MessageEvent(
                    message=accumulated_reasoning,
                    is_thinking=True,
                    is_final=True,
                )

            # 7.yield完整message（调用方据此继续处理工具调用/最终答案）
            yield filtered_message

        except Exception as e:
            if timer:
                timer.__exit__(None, None, None)

            # 降级到非流式_invoke_llm
            logger.warning(
                f"Agent[{self.name}]流式LLM调用失败,降级到非流式: {str(e)[:200]}"
            )
            try:
                # 传入空messages: messages已在上方_add_to_memory添加到内存,
                # _invoke_llm([])不会重复添加,直接使用self._memory.get_messages()调用LLM
                message = await self._invoke_llm(
                    [], format, tools_enabled, tool_choice,
                )
                # 降级场景思考内容推送:
                # - 流式已推送部分delta_reasoning → 推送最终聚合事件(标记流式结束)
                # - 流式未推送delta_reasoning → 使用_stream_thinking伪流式推送完整reasoning
                if accumulated_reasoning and stream_cfg.stream_thinking:
                    yield MessageEvent(
                        message=accumulated_reasoning,
                        is_thinking=True,
                        is_final=True,
                    )
                elif not accumulated_reasoning:
                    async for evt in self._stream_thinking(message.get("reasoning_content") or ""):
                        yield evt
                yield message
            except Exception as fallback_err:
                logger.error(f"Agent[{self.name}]非流式降级也失败: {str(fallback_err)}")
                raise

    async def _invoke_llm_stream(self, messages: List[Dict[str, Any]]) -> AsyncGenerator[LLMStreamChunk, None]:
        """流式调用LLM，逐块yield LLMStreamChunk。

        仅用于自然语言输出场景（如summarize），不传tools/tool_choice/response_format。
        负责记忆管理：添加用户消息 + 流式成功完成后添加完整assistant消息。
        流式中途异常时不写入assistant消息，调用方可降级到 _invoke_llm([]) 重试。
        """
        await self._add_to_memory(messages)

        # 预测token压力,主动压缩
        pressure = self._memory.predict_token_pressure(
            self._token_counter, self._context_window,
        )
        if pressure["should_emergency_compress"]:
            await self._overflow_recovery()
        elif pressure["should_proactive_compress"]:
            await self.compact_memory()
        elif self._memory.should_compress(threshold=0.5):
            await self.compact_memory()
        if self._memory.is_context_overflow():
            await self._overflow_recovery()

        accumulated_content = ""
        accumulated_reasoning = ""

        async for chunk in self._llm.astream(messages=self._memory.get_messages()):
            if chunk.delta_content:
                accumulated_content += chunk.delta_content
            if chunk.delta_reasoning:
                accumulated_reasoning += chunk.delta_reasoning
            yield chunk

        # 流式成功完成后将完整assistant消息加入记忆
        # 清洗DSML标记,防止LLM异常输出的工具调用标记污染记忆
        if accumulated_content or accumulated_reasoning:
            cleaned_content = strip_dsml_artifacts(accumulated_content) if accumulated_content else ""
            if cleaned_content or accumulated_reasoning:
                assistant_msg: Dict[str, Any] = {"role": "assistant", "content": cleaned_content}
                if accumulated_reasoning:
                    assistant_msg["reasoning_content"] = accumulated_reasoning
                await self._add_to_memory([assistant_msg])
        # Batch 33: LLM调用边界统一刷盘(流式输出完成后)
        await self._flush_memory()

    async def _stream_final_answer(
            self,
            content: str,
            attachments: Optional[List["File"]] = None,
            content_is_json: bool = False,
    ) -> AsyncGenerator[MessageEvent, None]:
        """将最终答案切片流式推送(F10-1)

        前后端交互契约:
        - 先逐片 yield MessageEvent(is_streaming=True, message=delta):增量片段仅推 SSE,
          AgentTaskRunner._put_and_add_event 检测 is_streaming=True 时跳过 DB 写入
        - 最后 yield MessageEvent(is_final=True, message=完整内容, attachments=附件):
          最终答案写库,前端据此替换或确认流式累积结果,附件随最终答案交付
        - 切片过程异常时降级为一次性返回完整内容(is_final=True)

        切片策略:
        - 优先按句末标点(。!?.\n)切片,保证语义完整
        - 单片字符数区间 [min_chars, max_chars],超过上限时强制断句
        - 切片间 sleep(delay_ms),模拟流式节奏,避免前端渲染压力

        Args:
            content: 完整最终答案文本(已 strip_dsml_artifacts 清洗)
            attachments: 随最终答案交付的附件列表,仅在 is_final=True 的最终片中携带
            content_is_json: content 是否为 JSON 格式。JSON 内容不切片(切片会导致
                下游解析失败,如 PlannerAgent 的 Plan JSON 解析、ReActAgent 的
                Step JSON 解析)。由调用方根据 content 来源明确指定:
                - invoke() 最终答案路径: content 是 LLM 原始 JSON 输出 → True
                - summarize() 路径: content 是解析后的自然语言 → False(默认)
        """
        if not content:
            yield MessageEvent(message="", is_final=True, attachments=attachments or [])
            return

        cfg = self._agent_config
        # 配置关闭时一次性返回完整内容
        if not cfg.stream_final_answer:
            yield MessageEvent(message=content, is_final=True, attachments=attachments or [])
            return

        # JSON 内容不切片: JSON 切片会导致每个 is_streaming 片段不是有效 JSON,
        # 下游解析失败触发降级(如 PlannerAgent 重复创建降级 Plan "我将为您处理这个任务。"),
        # 污染 SSE 事件流并产生大量重复消息
        if content_is_json:
            yield MessageEvent(message=content, is_final=True, attachments=attachments or [])
            return

        try:
            chunks = self._split_content_into_chunks(
                content,
                min_chars=cfg.stream_chunk_min_chars,
                max_chars=cfg.stream_chunk_max_chars,
            )
            delay_seconds = cfg.stream_chunk_delay_ms / 1000.0
            for chunk in chunks:
                if chunk:
                    yield MessageEvent(message=chunk, is_streaming=True)
                    if delay_seconds > 0:
                        await asyncio.sleep(delay_seconds)
            # 最终完整内容(供前端替换累积结果,后端写库),附件随最终片交付
            yield MessageEvent(
                message=content,
                is_final=True,
                attachments=attachments or [],
            )
        except Exception as e:
            logger.warning(
                f"Agent[{self.name}]流式切片异常,降级一次性返回: {e}"
            )
            yield MessageEvent(
                message=content,
                is_final=True,
                attachments=attachments or [],
            )

    async def _stream_thinking(self, reasoning: str) -> AsyncGenerator[MessageEvent, None]:
        """将思考内容(reasoning_content)切片流式推送(改进A)

        前后端交互契约(仿 _stream_final_answer 的「chunk + final」模式):
        - 先逐片 yield MessageEvent(is_thinking=True, is_streaming=True): 增量片段仅推 SSE,
          AgentTaskRunner._put_and_add_event 检测 is_streaming=True 时跳过 DB 写入,
          前端 appendEventWithStreaming 按 is_thinking 分组累积
        - 最后 yield MessageEvent(is_thinking=True, is_final=True): 最终聚合思考事件写 DB,
          前端据此替换流式累积结果; 历史回放时仅此事件可见,实现思考数据永驻
        - is_thinking=True: PlannerAgent/ReActAgent 守卫据此跳过 Plan/Step JSON 解析

        配置关闭(stream_thinking=False)或无内容时静默返回(降级为现状不推送)。
        切片过程异常时静默降级(不抛出,不影响主流程)。
        """
        if not reasoning or not self._agent_config.stream_thinking:
            return
        try:
            cfg = self._agent_config
            chunks = self._split_content_into_chunks(
                reasoning,
                min_chars=cfg.stream_chunk_min_chars,
                max_chars=cfg.stream_chunk_max_chars,
            )
            delay_seconds = cfg.stream_chunk_delay_ms / 1000.0
            for chunk in chunks:
                if chunk:
                    yield MessageEvent(message=chunk, is_thinking=True, is_streaming=True)
                    if delay_seconds > 0:
                        await asyncio.sleep(delay_seconds)
            # 最终聚合思考事件(is_streaming=False → 写DB,历史回放可见; is_final=True → 前端替换累积)
            yield MessageEvent(message=reasoning, is_thinking=True, is_final=True)
        except Exception as e:
            logger.warning(f"Agent[{self.name}]思考切片异常,降级跳过: {e}")

    @staticmethod
    def _split_content_into_chunks(
            content: str, min_chars: int, max_chars: int
    ) -> List[str]:
        """将文本按句末标点切片为符合长度区间的片段

        算法:
        1. 按 sentence_end_chars(。!?\n)切分为句子单元
        2. 累积句子至 >= min_chars 时输出一片
        3. 单句超 max_chars 时按 max_chars 硬切(避免单片过长)

        Args:
            content: 待切片文本
            min_chars: 单片最小字符数
            max_chars: 单片最大字符数

        Returns:
            切片列表,每片为非空字符串
        """
        if not content:
            return []

        # 句末标点:中文句号/问号/感叹号/英文对应/换行符
        sentence_end_chars = "。!?\n"
        # 1.按句末标点切分,保留标点在句尾
        sentences: List[str] = []
        buf = []
        for ch in content:
            buf.append(ch)
            if ch in sentence_end_chars:
                sentences.append("".join(buf))
                buf = []
        if buf:
            sentences.append("".join(buf))

        # 2.累积句子至达到 min_chars 即输出
        chunks: List[str] = []
        current = ""
        for sentence in sentences:
            # 单句超过 max_chars 时硬切(长段落场景)
            if len(sentence) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                for i in range(0, len(sentence), max_chars):
                    piece = sentence[i:i + max_chars]
                    if piece:
                        chunks.append(piece)
                continue

            # 累积句子,达到 min_chars 即输出
            if len(current) + len(sentence) <= max_chars:
                current += sentence
                if len(current) >= min_chars:
                    chunks.append(current)
                    current = ""
            else:
                # 加入当前句会超 max_chars: 先输出已累积内容,再起新片
                if current:
                    chunks.append(current)
                    current = ""
                current = sentence
                if len(current) >= min_chars:
                    chunks.append(current)
                    current = ""

        # 3.收尾: 剩余内容作为最后一片
        if current:
            chunks.append(current)

        return chunks

    async def _invoke_tool(self, tool: BaseTool, tool_name: str, arguments: Dict[str, Any]) -> ToolResult:
        """调用工具,支持幂等工具结果缓存与幂等写操作调用去重

        缓存与去重策略:
        - 幂等查询工具: 命中 ToolResultCache 缓存,跳过实际调用(白名单机制)
        - 幂等写操作工具: 命中 IdempotentToolRegistry 去重,返回上次调用结果(避免重复发起)
        - 缓存 key 包含 session_id+tool_name+sorted_args,会话级隔离
        - 缓存读写异常静默降级,不阻塞主流程
        - 失败结果不缓存/去重,避免脏数据持续命中

        浏览器工具降级重试(1次): 浏览器操作已内置超时保护,重试会叠加超时导致阻塞时间倍增。
        其他工具保持默认重试次数。
        高token压力下对工具结果做预截断,避免加入memory后触发紧急压缩。
        """
        # 工具结果缓存: 命中检查(仅白名单工具)
        if self._tool_cache and self._tool_cache.is_cacheable(tool_name, arguments):
            try:
                cached = await self._tool_cache.get(self._session_id, tool_name, arguments)
                if cached is not None:
                    logger.info(f"Agent[{self.name}]工具[{tool_name}]命中缓存,跳过实际调用")
                    # F10-9可观测性: 缓存命中埋点
                    if self._metrics:
                        self._metrics.increment("tool_cache_hit_count")
                    return cached
            except Exception as e:
                logger.warning(f"工具[{tool_name}]缓存读取异常,降级到实际调用: {e}")
            # F10-9可观测性: 缓存未命中埋点(仅白名单工具可走此路径)
            if self._metrics:
                self._metrics.increment("tool_cache_miss_count")

        # 幂等工具调用去重: 命中检查(仅配置的幂等写操作工具)
        # P10-1: 防止LLM在长会话中重复发起相同参数的幂等写操作(如异步任务发起)
        if self._idempotent_registry and self._idempotent_registry.is_dedupable(tool_name, arguments):
            try:
                dedup_result = await self._idempotent_registry.get(self._session_id, tool_name, arguments)
                if dedup_result is not None:
                    logger.info(f"Agent[{self.name}]工具[{tool_name}]命中幂等去重,返回上次调用结果")
                    # 在message中追加去重提示,引导LLM基于已有结果继续处理
                    dedup_msg = dedup_result.message or ""
                    hint = "[系统提示] 该工具调用已在本次会话中执行过,已返回上次的结果。请勿重复发起,应基于已有结果继续处理(如查询任务状态或复用已生成的文件)。"
                    dedup_result = dedup_result.model_copy()
                    dedup_result.message = f"{dedup_msg}\n{hint}" if dedup_msg else hint
                    # F10-9可观测性: 幂等去重命中埋点
                    if self._metrics:
                        self._metrics.increment("tool_idempotent_dedup_count")
                    return dedup_result
            except Exception as e:
                logger.warning(f"工具[{tool_name}]幂等去重读取异常,降级到实际调用: {e}")

        # 浏览器工具重试1次(超时已包含容错),其他工具重试max_retries次
        is_browser_tool = tool_name.startswith("browser_")
        max_retries = 1 if is_browser_tool else self._agent_config.max_retries

        err = ""
        for attempt in range(max_retries):
            try:
                result = await tool.invoke(tool_name, **arguments)

                # 高token压力下预截断工具结果
                if result.success and self._memory and self._token_counter:
                    result = self._pretruncate_tool_result_if_needed(result, tool_name)

                # 工具结果缓存: 成功结果写入(仅白名单工具,失败结果不缓存避免脏数据)
                if result.success and self._tool_cache and self._tool_cache.is_cacheable(tool_name, arguments):
                    try:
                        await self._tool_cache.set(self._session_id, tool_name, arguments, result)
                    except Exception as e:
                        logger.warning(f"工具[{tool_name}]缓存写入异常,跳过: {e}")

                # 幂等工具调用去重: 成功结果写入注册表(仅配置的幂等写操作工具)
                # P10-1: 记录本次调用结果,后续相同参数调用直接返回此结果
                if result.success and self._idempotent_registry and self._idempotent_registry.is_dedupable(tool_name, arguments):
                    try:
                        await self._idempotent_registry.set(self._session_id, tool_name, arguments, result)
                    except Exception as e:
                        logger.warning(f"工具[{tool_name}]幂等去重写入异常,跳过: {e}")

                # F10-9可观测性: 工具调用成功埋点(区分MCP/普通工具)
                if result.success and self._metrics:
                    self._metrics.increment("tool_call_count")
                    if tool_name.startswith("mcp_"):
                        self._metrics.increment("mcp_call_count")
                    # Batch 39 / 方向4: shell_execute 调用次数分桶指标
                    # 观测脚本合并引导是否生效(shell_execute_count 应随合并引导降低)
                    if tool_name == "shell_execute":
                        self._metrics.increment("shell_execute_count")
                        # Batch 40 / 方向3: shell 调用画像记录(量化合并引导效果)
                        if self._shell_profiler:
                            cmd = arguments.get("command", "")
                            self._shell_profiler.record(cmd, success=result.success)

                # Batch 39 / 方向3: 预算观测 — 75%告警 + 超限事件消费 + 策略切换追踪
                # 修复 check_and_warn 断链: 原定义但从未调用,现统一在工具执行后调用
                if self._budget_tracker:
                    try:
                        self._budget_tracker.check_and_warn(tool_name, self._metrics)
                        self._observe_budget_exceeded(tool_name)
                    except Exception as e:
                        logger.debug(f"预算观测异常(降级忽略): {e}")

                return result
            except Exception as e:
                err = str(e)
                logger.exception(f"调用工具[{tool_name}]出错(第{attempt + 1}次): {str(e)}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(self._retry_interval)
                    continue

        return ToolResult(success=False, message=err)

    def _pretruncate_tool_result_if_needed(self, result: ToolResult, tool_name: str) -> ToolResult:
        """高token压力下预截断工具结果

        当token压力为high/critical时,对工具结果做激进截断(保留前N字符),
        避免工具结果加入memory后触发紧急压缩,导致上下文不连贯。

        浏览器查看类工具(browser_view/navigate/restart)豁免此预截断:
        其结果含interactive_elements/ref_map等操作必需字段,暴力截断会丢弃关键数据
        导致LLM判定"empty state"无法操作(会话9309bba7根因)。
        这些工具由_truncate_browser_view_result按优先级安全截断,无需此层干预。
        """
        try:
            pressure = self._memory.predict_token_pressure(
                self._token_counter, self._context_window,
            )
            if pressure["pressure_level"] not in ("high", "critical"):
                return result

            if not result.data:
                return result

            # 浏览器查看类工具豁免: 其结果由_truncate_browser_view_result按优先级安全截断,
            # 暴力预截断会破坏JSON结构并丢弃interactive_elements/ref_map等操作必需字段
            from app.domain.models.memory import _BROWSER_VIEW_TOOLS
            if tool_name in _BROWSER_VIEW_TOOLS:
                return result

            data_str = result.model_dump_json()
            if len(data_str) <= _HIGH_PRESSURE_TRUNCATE_MAX:
                return result

            logger.info(
                f"Agent[{self.name}]token压力{pressure['pressure_level']},"
                f"预截断工具[{tool_name}]结果: {len(data_str)} -> {_HIGH_PRESSURE_TRUNCATE_MAX}字符"
            )
            truncated = data_str[:_HIGH_PRESSURE_TRUNCATE_MAX]
            # 尝试闭合JSON,保留结构标记
            if truncated.endswith('}'):
                pass
            else:
                truncated += '...,"_truncated": true}'
            try:
                truncated_data = json.loads(truncated)
                return ToolResult(
                    success=result.success,
                    message=result.message,
                    data=truncated_data.get("data", {}),
                )
            except Exception:
                return result
        except Exception:
            return result

    def _get_dynamic_max_len(self, function_name: str) -> Optional[int]:
        """计算基于剩余token预算的动态截断阈值(会话7720e91d根因修复)

        页面快照遵循"只在当前会话临时存在"原则: 旧快照由evict_browser_view_content驱逐,
        当前快照是LLM决策唯一依据。上下文充足时向上扩展阈值,让LLM看到完整页面元素;
        上下文紧张时向下缩减,保障会话不溢出。

        Returns:
            动态max_len(token_counter不可用时返回None,由调用方降级为静态阈值)
        """
        if not self._token_counter or self._context_window <= 0 or not self._memory:
            return None
        try:
            standard_len = Memory._get_standard_max_len(function_name)
            current_tokens = self._token_counter.count_messages(self._memory.get_messages())
            remaining_ratio = 1.0 - (current_tokens / self._context_window)

            if remaining_ratio < 0.2:
                return standard_len // 4  # 紧张: 1/4阈值
            elif remaining_ratio < 0.5:
                return standard_len // 2  # 正常: 1/2阈值
            elif remaining_ratio > 0.7:
                return int(standard_len * 2.0)  # 充足: 2倍阈值
            elif remaining_ratio > 0.5:
                return int(standard_len * 1.5)  # 较充足: 1.5倍阈值
            return standard_len
        except Exception:
            return None

    @staticmethod
    def _build_tool_message_content(
        result: ToolResult, function_name: str, supports_images: bool = True,
        max_len: Optional[int] = None,
    ) -> Any:
        """组装工具响应内容。结果含图片时构建多模态内容数组，
        图片通过image_url内容块传递给多模态LLM(如GLM-5.2)；其余场景返回截断后的文本。
        图片base64始终从文本部分剔除(替换为标记)，避免上下文膨胀；
        strip_image_data会在压缩阶段清理image_url块。

        supports_images=False时(非多模态LLM如DeepSeek),跳过image_url块构建,
        仅返回含[screenshot attached]标记的文本,避免API返回400错误
        (会话a34fcdc1根因: deepseek-v4-flash拒绝image_url变体)。

        支持的图片来源(统一按data字段类型判定，与工具名解耦)：
        - 浏览器工具: result.data["screenshot"] (base64 jpeg字符串)
        - MCP工具: result.data["images"] (List[{"data": base64, "mime_type": str}])
        """
        # 提取图片：浏览器截图字段 / MCP图片列表字段
        screenshot_b64: Optional[str] = None
        extra_images: List[Dict[str, str]] = []
        if isinstance(result.data, dict):
            shot = result.data.get("screenshot")
            if isinstance(shot, str) and shot:
                screenshot_b64 = shot
            imgs = result.data.get("images")
            if isinstance(imgs, list):
                extra_images = [
                    i for i in imgs
                    if isinstance(i, dict) and isinstance(i.get("data"), str) and i["data"]
                ]

        has_images = bool(screenshot_b64 or extra_images)
        # 构造不含图片base64的副本用于文本序列化，确保上下文不膨胀
        text_result = result
        if has_images and isinstance(result.data, dict):
            data_copy = dict(result.data)
            if screenshot_b64:
                data_copy["screenshot"] = "[attached]"
            if extra_images:
                data_copy["images"] = f"[{len(extra_images)} attached]"
            text_result = ToolResult(success=result.success, message=result.message, data=data_copy)

        # 动态截断: max_len由调用方基于剩余token预算计算(上下文充足时扩展,紧张时缩减)
        # max_len=None时降级为静态固定阈值(向后兼容)
        # supports_images透传给_truncate_content_internal,控制浏览器结果content预算分配
        # (会话437cbc75根因修复: 文本LLM无截图通道,content需更多预算)
        raw_json = text_result.model_dump_json()
        if max_len is not None:
            tool_text = Memory._truncate_content_internal(raw_json, function_name, max_len, supports_images)
        else:
            tool_text = Memory.truncate_tool_result(raw_json, function_name)
        # 无图片 或 LLM不支持图像输入时: 仅返回文本(含[attached]标记让LLM知道截图存在)
        if not has_images or not supports_images:
            return tool_text
        content: List[Dict[str, Any]] = [{"type": "text", "text": tool_text}]
        if screenshot_b64:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
            })
        for img in extra_images:
            mime = img.get("mime_type") or "image/png"
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{img['data']}"},
            })
        return content

    async def _add_to_memory(self, messages: List[Dict[str, Any]]) -> None:
        """将对应的信息添加到记忆中"""
        # 1.先检查确保记忆是存在的
        await self._ensure_memory()

        # 2.检查记忆的消息列表是否为空，如果是空则需要添加预设prompt作为初始记忆
        if self._memory.empty:
            self._memory.add_message({
                "role": "system", "content": self._system_prompt,
            })

        # 3.将正常消息添加到记忆中
        self._memory.add_messages(messages)
        # Batch 33: 移除即时save_memory,改为LLM调用边界统一刷盘(_flush_memory)

    async def _flush_memory(self) -> None:
        """Batch 33: 记忆批量化持久化 - 在LLM调用边界统一刷盘

        仅当记忆存在未持久化变更(dirty)时执行save_memory,成功后mark_clean。
        失败仅记录日志不阻断主流程(与compact_memory异常处理语义一致)。
        """
        if not self._memory or not self._memory.dirty:
            return
        try:
            async with self._uow_factory() as uow:
                await uow.session.save_memory(self._session_id, self.name, self._memory)
            self._memory.mark_clean()
        except Exception as e:
            logger.error(f"Agent[{self.name}]记忆批量化持久化失败: {str(e)}")

    def _should_proactive_compress(self) -> bool:
        """根据压缩策略判断是否执行主动压缩(P4-2自适应压缩策略)

        借鉴TRAE Work CN被动压缩理念: 简单场景下被动压缩更省计算。
        compact_memory内部有extract_key_facts等较重操作,简单对话频繁触发浪费资源。

        策略:
        - proactive: 始终主动压缩(当前行为,适用于已知复杂任务)
        - reactive: 始终被动压缩(仅critical触发,适用于已知简单场景)
        - auto(默认): 多步骤任务(_step_description非空)主动压缩,
          简单对话(_step_description为空)被动压缩(省计算)

        Returns:
            True表示执行主动压缩,False表示跳过(等critical阈值触发紧急压缩)
        """
        if _COMPRESSION_STRATEGY == "proactive":
            return True
        if _COMPRESSION_STRATEGY == "reactive":
            return False
        # auto: 多步骤任务主动压缩,简单对话被动压缩
        return bool(getattr(self, "_step_description", ""))

    async def compact_memory(self) -> None:
        """压缩Agent的记忆（使用自动压缩级别），压缩失败不中断流程"""
        await self._ensure_memory()
        try:
            level = self._memory.auto_compact()
            if level != CompressionLevel.NONE:
                logger.info(f"Agent[{self.name}]执行压缩: Level {level.name}, 消息数: {len(self._memory.messages)}")
            else:
                # P4-5优化: 压缩级别为NONE时,记忆内容未实际变更,跳过save_memory数据库写入
                # 原逻辑: 无条件save_memory,每步骤~50-100ms数据库IO浪费
                # 仅当记忆有未持久化修改(_dirty)时才执行save_memory
                if not getattr(self._memory, "_dirty", False):
                    logger.debug(f"Agent[{self.name}]压缩级别NONE且记忆无变更,跳过持久化")
                    return
        except Exception as e:
            logger.error(f"Agent[{self.name}]记忆压缩失败: {str(e)}")
            return
        try:
            async with self._uow_factory() as uow:
                await uow.session.save_memory(self._session_id, self.name, self._memory)
            self._memory.mark_clean()
        except Exception as e:
            logger.error(f"Agent[{self.name}]压缩后持久化失败: {str(e)}")

    async def compact_memory_if_needed(self) -> bool:
        """条件化压缩 — 基于token压力决定是否执行compact_memory(P4-5优化)

        步骤后调用的轻量级压缩入口: 先检查token压力,低压力时跳过压缩和持久化,
        避免每步骤无条件compact_memory的数据库IO开销。

        压缩决策与_invoke_llm中的predict_token_pressure保持一致:
        - critical/high/moderate: 执行压缩(should_proactive_compress=True)
        - safe: 跳过压缩(下一步_invoke_llm会再次检查)

        Returns:
            True表示执行了压缩, False表示跳过
        """
        await self._ensure_memory()
        pressure = self._memory.predict_token_pressure(
            self._token_counter, self._context_window,
        )
        if pressure["should_proactive_compress"]:
            logger.info(
                f"Agent[{self.name}]步骤后token压力{pressure['pressure_level']}"
                f"({pressure['projected_ratio']:.1%}),执行压缩"
            )
            await self.compact_memory()
            return True
        logger.debug(
            f"Agent[{self.name}]步骤后token压力{pressure['pressure_level']}"
            f"({pressure['projected_ratio']:.1%}),跳过压缩"
        )
        return False

    async def _overflow_recovery(self) -> None:
        """上下文溢出恢复 - 两层渐进式降级压缩

        Phase E简化: 四层→两层。常规压缩无法解决时直接使用紧急压缩。
        """
        await self._ensure_memory()
        recovery_layers = [
            ("常规压缩", lambda: self._memory.compact()),
            ("紧急压缩", lambda: self._memory.emergency_compact()),
        ]
        for layer_name, compress_fn in recovery_layers:
            try:
                compress_fn()
                logger.info(f"Agent[{self.name}]溢出恢复: {layer_name}, 消息数: {len(self._memory.messages)}")
                async with self._uow_factory() as uow:
                    await uow.session.save_memory(self._session_id, self.name, self._memory)
                self._memory.mark_clean()
                if not self._memory.is_context_overflow():
                    return
            except Exception as e:
                logger.error(f"Agent[{self.name}]{layer_name}失败: {str(e)}")
                continue

    async def _repair_missing_reasoning(self) -> None:
        """修复因reasoning_content缺失导致的API 400错误

        DeepSeek V4思考模式约束：工具调用场景下assistant消息必须携带reasoning_content，
        当记忆压缩误删了该字段时，此方法遍历记忆为缺失的assistant+tool_calls消息
        补充空字符串reasoning_content=""，修复后持久化到数据仓库。
        """
        await self._ensure_memory()
        repaired = False
        for msg in self._memory.messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls") and "reasoning_content" not in msg:
                msg["reasoning_content"] = ""
                repaired = True
        if repaired:
            logger.info(f"Agent[{self.name}]修复了缺失的reasoning_content字段")
            try:
                async with self._uow_factory() as uow:
                    await uow.session.save_memory(self._session_id, self.name, self._memory)
                self._memory.mark_clean()
            except Exception as e:
                logger.error(f"Agent[{self.name}]reasoning修复后持久化失败: {str(e)}")

    async def _repair_tool_message_pairing(self) -> None:
        """修复tool消息与assistant(tool_calls)的配对完整性

        OpenAI API约束: role=tool的消息必须紧跟包含匹配tool_call_id的
        assistant(tool_calls)消息。emergency_compact等操作可能破坏配对,
        导致API返回400错误 "Messages with role 'tool' must be a response..."。

        本方法委托Memory.sanitize_tool_message_pairing执行实际修复:
        - 删除孤立的tool消息(无配对assistant或被其他角色消息隔开)
        - 降级未响应的assistant(tool_calls)为纯文本(删除tool_calls字段)
        修复后持久化到数据仓库,作为_invoke_llm重试前的兜底防线。
        """
        await self._ensure_memory()
        removed = self._memory.sanitize_tool_message_pairing()
        if removed > 0:
            logger.info(f"Agent[{self.name}]修复了{removed}处tool消息配对破坏")
            try:
                async with self._uow_factory() as uow:
                    await uow.session.save_memory(self._session_id, self.name, self._memory)
                self._memory.mark_clean()
            except Exception as e:
                logger.error(f"Agent[{self.name}]tool配对修复后持久化失败: {str(e)}")

    async def roll_back(self, message: Message) -> None:
        """Agent的状态回滚，该函数用于确保Agent的消息列表状态是正确，用于发送新消息、暂停/停止任务、通知用户"""
        # 1.取出记忆中的最后一条消息，检查是否是工具调用
        await self._ensure_memory()
        last_message = self._memory.get_last_message()
        if (
                not last_message or
                not last_message.get("tool_calls") or
                len(last_message.get("tool_calls")) == 0
        ):
            return

        # 2.取出消息中的工具调用参数
        tool_call = last_message.get("tool_calls")[0]

        # 3.提取工具名字、id
        function_name = tool_call.get("function", {}).get("name")
        tool_call_id = tool_call.get("id")

        # 4.判断下当前的工具是不是通知用户(message_ask_user)
        if function_name == "message_ask_user":
            self._memory.add_message({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "function_name": function_name,
                "content": message.model_dump_json(),
            })
        else:
            # 5.否则直接删除最后一条消息
            self._memory.roll_back()

        # 6.将记忆持久化
        async with self._uow_factory() as uow:
            await uow.session.save_memory(self._session_id, self.name, self._memory)
        self._memory.mark_clean()

    # F10-2 重构: 工具调用解析与执行辅助方法
    # 原invoke()方法约300行,工具调用循环(串行+并行路径)占200行且存在重复解析逻辑。
    # 提取为独立方法后,invoke()主循环聚焦于迭代控制与状态流转,可读性/可测性提升。

    async def _parse_tool_call(self, tool_call: Dict[str, Any]) -> _ParsedToolCall:
        """解析单个 tool_call,返回解析结果(含错误信息)

        统一串行/并行路径的解析逻辑,包括:
        - 畸形 tool_call 检测(无 function 字段): 生成错误消息,不产出 CALLING 事件
        - 参数 JSON 解析 + 类型修正(list→{"items":list}, 其他→{})
        - 工具查找 + 未知工具错误提示(包含可用工具列表前10个)

        Args:
            tool_call: LLM 返回的单个 tool_call 字典

        Returns:
            _ParsedToolCall: 解析结果,error_content 非 None 表示解析失败
        """
        # 1.畸形 tool_call 检测: 无 function 字段时必须生成错误 tool_message
        #    否则 assistant(tool_calls) 的该 tool_call_id 无对应 tool 消息, 触发 API 400
        if not tool_call.get("function"):
            malformed_id = tool_call.get("id") or str(uuid.uuid4())
            logger.warning(f"LLM返回畸形tool_call(无function字段): id={malformed_id}")
            return _ParsedToolCall(
                tool_call_id=malformed_id,
                tool=None,
                function_name="(malformed)",
                error_content="错误: 工具调用格式异常(缺少function字段),请使用正确的工具调用格式重试。",
            )

        # 2.解析 tool_call_id 与 function_name
        tool_call_id = tool_call.get("id") or str(uuid.uuid4())
        function_name = tool_call["function"]["name"]

        # 3.解析参数 JSON,失败时降级为空字典
        # P4-6流式改造诊断: 记录原始参数字符串,便于定位流式累积导致的空参数问题
        raw_arguments = tool_call["function"].get("arguments") or ""
        try:
            function_args = await self._json_parser.invoke(raw_arguments)
        except Exception as e:
            logger.warning(
                f"工具[{function_name}]参数解析失败,使用空字典: {e}"
                f"(原始参数长度: {len(raw_arguments)}, 内容: {raw_arguments[:200]})"
            )
            function_args = {}

        # 4.参数类型修正: list → {"items": list}, 其他非 dict → {}
        if isinstance(function_args, list):
            logger.warning(f"工具[{function_name}]参数为列表格式,自动转换为字典")
            function_args = {"items": function_args}
        elif not isinstance(function_args, dict):
            logger.warning(f"工具[{function_name}]参数类型异常({type(function_args).__name__}),使用空字典")
            function_args = {}

        # 5.工具查找,未知工具时返回错误消息(含可用工具列表提示)
        try:
            tool = self._get_tool(function_name)
        except ValueError as e:
            logger.warning(f"LLM调用了未知工具[{function_name}]: {e}")
            available_names = [t["function"]["name"] for t in self._get_available_tools()]
            hint = f"工具[{function_name}]不存在。可用工具列表: {available_names[:10]}{'...' if len(available_names) > 10 else ''}"
            return _ParsedToolCall(
                tool_call_id=tool_call_id,
                tool=None,
                function_name=function_name,
                function_args=function_args,
                error_content=f"错误: {hint}",
            )

        # 6.正常解析结果
        return _ParsedToolCall(
            tool_call_id=tool_call_id,
            tool=tool,
            function_name=function_name,
            function_args=function_args,
        )

    @staticmethod
    def _build_error_tool_message(parsed: _ParsedToolCall) -> Dict[str, Any]:
        """根据解析失败结果构建错误 tool_message(F10-2 重构提取)

        用于畸形 tool_call 和未知工具场景,确保 assistant(tool_calls) 的每个
        tool_call_id 都有对应 tool 消息,避免触发 API 400。

        Args:
            parsed: 解析失败的 _ParsedToolCall(error_content 非 None)

        Returns:
            错误 tool_message 字典
        """
        return {
            "role": "tool",
            "tool_call_id": parsed.tool_call_id,
            "function_name": parsed.function_name,
            "content": parsed.error_content,
        }

    @staticmethod
    def _build_tool_result_message(
        parsed: _ParsedToolCall, result: ToolResult, supports_images: bool = True,
        max_len: Optional[int] = None,
    ) -> Dict[str, Any]:
        """根据执行结果构建正常 tool_message(F10-2 重构提取)

        supports_images透传给_build_tool_message_content,控制是否构建image_url块。
        max_len透传给_build_tool_message_content,控制动态截断阈值(None=静态降级)。
        """
        return {
            "role": "tool",
            "tool_call_id": parsed.tool_call_id,
            "function_name": parsed.function_name,
            "content": BaseAgent._build_tool_message_content(
                result, parsed.function_name, supports_images=supports_images,
                max_len=max_len,
            ),
        }

    def _observe_budget_exceeded(self, tool_name: str) -> None:
        """预算超限观测与策略切换追踪(Batch 39 / 方向3)

        两阶段逻辑:
        1. 策略切换检测: 若上一轮有工具被预算拦截(_last_exceeded_tool),
           且当前调用的是不同工具 → LLM 成功切换策略 → strategy_switch_count++
           若是相同工具 → LLM 未切换 → strategy_switch_retry_count++
        2. 超限事件消费: 从 budget_tracker 队列消费本轮超限事件,
           记录 budget_exceeded_count 并更新 _last_exceeded_tool

        所有操作异常静默降级,绝不阻断主流程。

        Args:
            tool_name: 当前调用的工具名
        """
        if not self._budget_tracker:
            return
        # 1.策略切换检测: 上一轮有工具被拦截,本轮调用新工具
        if self._last_exceeded_tool is not None:
            if tool_name != self._last_exceeded_tool:
                # LLM 切换到不同工具 → 策略切换成功
                if self._metrics:
                    self._metrics.increment("strategy_switch_count")
                logger.info(
                    f"Agent[{self.name}]策略切换: {self._last_exceeded_tool} → {tool_name}"
                )
            else:
                # LLM 重试相同工具 → 策略未切换(可能未注意到预算超限提示)
                if self._metrics:
                    self._metrics.increment("strategy_switch_retry_count")
            # 清除标记,等待下一次超限事件再触发检测
            self._last_exceeded_tool = None
        # 2.超限事件消费: 检查本轮是否有工具被预算拦截
        exceeded_tool = self._budget_tracker.consume_exceeded_event()
        if exceeded_tool:
            if self._metrics:
                self._metrics.increment("budget_exceeded_count")
            self._last_exceeded_tool = exceeded_tool
            logger.info(
                f"Agent[{self.name}]工具[{exceeded_tool}]预算超限,LLM需切换策略"
            )

    def _inject_budget_warnings(
            self,
            iteration: int,
            tool_messages: List[Dict[str, Any]],
            session_start_ts: float,
            session_timeout_injected: bool,
    ) -> bool:
        """注入迭代预算与会话超时警告指令(F10-2 重构提取)

        两类警告互补:
        - 迭代预算感知: 接近 max_iterations 上限时注入引导 LLM 快速收敛
        - 会话级超时熔断(P10-3): 长会话可能迭代未达阈值但时长已超,需注入超时指令

        Args:
            iteration: 当前迭代序号(0-based)
            tool_messages: 当前轮次工具消息列表,警告指令直接 append 到此列表
            session_start_ts: 会话开始时间戳(0表示不启用超时检测)
            session_timeout_injected: 是否已注入超时指令(防止重复注入)

        Returns:
            更新后的 session_timeout_injected 状态
        """
        max_iter = self._agent_config.max_iterations
        warning_threshold = int(max_iter * 0.8)
        critical_threshold = int(max_iter * 0.9)

        # 1.迭代预算感知: 90% → 紧急停止, 80% → 警告收敛
        if iteration >= critical_threshold:
            tool_messages.append({
                "role": "user",
                "content": "【系统紧急指令】你已使用90%的迭代预算，必须立即停止调用工具，基于已有信息直接输出最终结果！不要再调用任何工具！",
            })
        elif iteration >= warning_threshold:
            tool_messages.append({
                "role": "user",
                "content": "【系统警告】你已使用80%的迭代预算，请尽快总结当前发现并给出最终回答，减少不必要的工具调用。",
            })

        # 2.会话级超时熔断(P10-3)
        session_timeout = self._agent_config.session_timeout_seconds
        session_warning = self._agent_config.session_warning_seconds
        if session_timeout > 0 and session_start_ts > 0.0:
            elapsed = asyncio.get_event_loop().time() - session_start_ts
            if elapsed >= session_timeout:
                if not session_timeout_injected:
                    # 首次触发硬超时: 注入完整强制总结指令
                    logger.warning(
                        f"Agent[{self.name}]会话[{self._session_id}]已运行{int(elapsed)}s, "
                        f"超过硬超时{session_timeout}s, 注入强制总结指令"
                    )
                    tool_messages.append({
                        "role": "user",
                        "content": f"【系统超时指令】本次会话已运行{int(elapsed / 60)}分钟,超过硬超时阈值。必须立即停止调用任何工具(包括等待类命令),基于已收集的信息给出尽可能完整的最终结果。直接输出结果,不要再调用工具。",
                    })
                    return True
                else:
                    # 批次 38: 硬超时后每次迭代注入简短提醒,强化 LLM 遵守超时指令
                    # 根因: 批次 37 E2E 发现硬超时指令仅注入一次后,LLM 后续迭代忽略
                    # 超时约束继续调用工具,导致"连续第N次触发硬超时"(累计32分钟)
                    tool_messages.append({
                        "role": "user",
                        "content": f"【超时提醒】会话已运行{int(elapsed / 60)}分钟,已超过硬超时阈值。立即停止调用工具,基于已有信息输出最终结果。",
                    })
            elif elapsed >= session_warning and not session_timeout_injected:
                logger.info(
                    f"Agent[{self.name}]会话[{self._session_id}]已运行{int(elapsed)}s, "
                    f"超过软阈值{session_warning}s, 注入收敛提示(批次45 P1-3四步交付引导)"
                )
                # 批次45 P1-3: 软警告增强为"停止查询→生成文件→声明路径→总结"四步操作指令
                # 根因: 批次44会话1751s接近超时仍0附件,原"请尽快总结"无附件交付引导
                # 同时设置session_timeout_injected=True防重复注入(原仅硬超时设置)
                tool_messages.append({
                    "role": "user",
                    "content": (
                        f"【系统时间警告】本次会话已运行{int(elapsed / 60)}分钟,接近超时阈值。"
                        f"请立即按以下四步完成交付:"
                        f"1.停止所有数据查询与工具探索;"
                        f"2.基于已收集的数据生成交付物文件(docx/xlsx,保存到/home/ubuntu/,英文或拼音文件名);"
                        f"3.在步骤结果 attachments 字段显式声明文件完整路径;"
                        f"4.输出最终总结回答。不再调用探索类工具。"
                    ),
                })
                session_timeout_injected = True

        # 3.工具调用预算感知(Batch 39 / 方向2): 75% 阈值时注入引导
        # 与 check_and_warn(logger/metrics)互补: check_and_warn 面向运维,
        # 此处面向 LLM,引导其在接近预算上限时切换策略或收敛输出
        if self._budget_tracker:
            try:
                budget_hints = self._build_budget_usage_hints()
                if budget_hints:
                    tool_messages.append({
                        "role": "user",
                        "content": budget_hints,
                    })
            except Exception as e:
                logger.debug(f"工具预算告警注入异常(降级忽略): {e}")

        # 4.shell_execute 频次主动引导(批次45 P1-2)
        # 累计调用达阈值时注入脚本合并建议,每会话仅注入一次
        try:
            shell_guidance = self._build_shell_execute_guidance()
            if shell_guidance:
                tool_messages.append({"role": "user", "content": shell_guidance})
        except Exception as e:
            logger.debug(f"shell_execute频次引导注入异常(降级忽略): {e}")

        return session_timeout_injected

    def _build_shell_execute_guidance(self) -> str:
        """构建 shell_execute 频次合并引导(批次45 P1-2)

        通过 _shell_profiler.total_calls 获取累计调用次数,超阈值且未注入过时
        返回引导文本并设置标志(每会话仅注入一次)。

        Returns:
            合并引导文本,未超阈值/已注入/无profiler时返回空串
        """
        if self._shell_guidance_injected or self._shell_profiler is None:
            return ""
        total = self._shell_profiler.total_calls
        if total < _SHELL_EXECUTE_GUIDANCE_THRESHOLD:
            return ""
        self._shell_guidance_injected = True
        logger.info(
            f"Agent[{self.name}]会话[{self._session_id}]shell_execute累计{total}次,"
            f"注入频次合并引导(批次45 P1-2)"
        )
        return (
            f"【系统效率提示】当前已累计调用 shell_execute {total} 次,建议将后续同类操作"
            f"(数据查询/文件处理/计算)合并为单次 Python 脚本调用,显著减少调用次数与 token 消耗。"
        )

    def _build_budget_usage_hints(self) -> str:
        """构建工具预算使用率提示文本(Batch 39 / 方向2)

        扫描所有受预算工具,对使用率 >= 75% 的工具生成提示文本。
        使用率 100% 的工具(已硬拦截)不重复提示(工具层已返回错误信息)。
        每会话每工具仅注入一次(复用 budget_tracker._warned 集合去重)。

        Returns:
            预算提示文本,无工具达 75% 阈值时返回空串
        """
        if not self._budget_tracker:
            return ""
        from app.domain.services.tools.budget_tracker import _WARNING_RATIO
        hints = []
        for tool_name, budget in self._budget_tracker._budgets.items():
            if budget <= 0:
                continue
            count = self._budget_tracker.get_count(tool_name)
            ratio = count / budget
            # 仅提示 75%~99% 区间(100% 已被工具层硬拦截,LLM 已收到错误)
            if _WARNING_RATIO <= ratio < 1.0:
                hints.append(
                    f"工具[{tool_name}]已使用 {count}/{budget} 次({ratio:.0%}),"
                    f"接近会话级调用上限。请减少该工具的调用,优先复用已有结果或切换替代策略。"
                )
        if not hints:
            return ""
        return "【工具预算提示】" + " ".join(hints)

    async def invoke(self, query: str, format: Optional[str] = None, tool_choice: Optional[str] = None) -> AsyncGenerator[BaseEvent, None]:
        """传递消息+响应格式调用程序生成异步迭代内容

        非流式：LLM 一次性返回完整 message，工具调用走 ToolEvent，
        最终答案走 MessageEvent(is_final=True)。

        Args:
            query: 用户查询文本
            format: 响应格式覆盖(None用Agent默认); tool_choice: 工具选择策略覆盖
            tool_choice: 临时覆盖Agent的tool_choice,用于强制工具调用场景。
                         仅首次LLM调用生效,后续迭代不强制(让LLM自由决定是否继续调用)。
        """
        # 1.需要判断下是否传递了format
        format = format if format else self._format

        # 2.调用语言模型获取响应内容(首次调用可强制工具选择)
        # P4-6流式改造: 使用_stream_llm_invoke代替_invoke_llm+stream_thinking
        # 原伪流式: 阻塞等待LLM完整响应(15-20s) → 切片推送reasoning_content
        # 新真正流式: LLM生成过程中实时推送delta_reasoning(1-2s内首片到达)
        # _stream_llm_invoke内部完成思考推送+yield完整message,无需额外_stream_thinking
        message = None
        async for evt in self._stream_llm_invoke([{"role": "user", "content": query}], format, tool_choice=tool_choice):
            if isinstance(evt, dict):
                message = evt
            else:
                yield evt

        # 3.循环遍历直到最大迭代次数
        max_iter = self._agent_config.max_iterations
        # P10-3: 会话级超时熔断 — 记录起始时间戳,0表示不启用
        # 阈值计算与警告注入逻辑已提取到 _inject_budget_warnings(F10-2)
        if self._agent_config.session_timeout_seconds > 0 and self._session_start_ts == 0.0:
            self._session_start_ts = asyncio.get_event_loop().time()
        session_timeout_injected = False  # 防止重复注入强总结指令

        for iteration in range(max_iter):
            # 4.如果LLM响应为空或无工具调用则表示LLM生成了文本回答，这时候就是最终答案
            if not message or not message.get("tool_calls"):
                break

            # 5.执行工具调用: 并行启用且多工具时走3阶段并行路径,否则保持原串行路径
            tool_messages: List[Dict[str, Any]] = []
            if self._parallel_enabled and len(message["tool_calls"]) > 1:
                # 工具并行执行: 3阶段并行(阶段A: 解析+CALLING+分类 → 阶段B: 并行+串行执行 → 阶段C: CALLED+组装)
                parsed_calls = []  # [(tool_call_id, tool, function_name, function_args, original_index)]
                unknown_results = {}  # {original_index: error_msg_dict} — 未知工具直接进tool_messages,不出事件
                # F10-9可观测性: 并行工具调用对数埋点(记录并行调用量)
                if self._metrics:
                    self._metrics.increment(
                        "parallel_tool_pairs",
                        max(0, len(message["tool_calls"]) - 1),
                    )

                # 阶段A: 解析参数 + 产出所有CALLING事件 + 分类工具
                # F10-2 重构: 解析逻辑提取到 _parse_tool_call,消除重复代码
                for idx, tool_call in enumerate(message["tool_calls"]):
                    parsed = await self._parse_tool_call(tool_call)
                    # 解析失败: 存入 unknown_results(不产出 CALLING 事件,阶段C 直接 append 错误 tool_message)
                    if parsed.error_content is not None:
                        unknown_results[idx] = self._build_error_tool_message(parsed)
                        continue
                    # 正常路径: 产出 CALLING 事件,加入待执行队列
                    yield ToolEvent(
                        tool_call_id=parsed.tool_call_id,
                        tool_name=parsed.tool.name,
                        function_name=parsed.function_name,
                        function_args=parsed.function_args,
                        status=ToolEventStatus.CALLING,
                    )
                    parsed_calls.append((parsed.tool_call_id, parsed.tool, parsed.function_name, parsed.function_args, idx))

                # 阶段B: 分类 + 并行执行可并行 + 串行执行不可并行
                # 使用 partition() 支持参数级隔离(shell_execute 按 session_id 隔离)
                # 将 parsed_calls 转为 partition() 兼容格式,分类后映射回原 entry
                tc_formatted = [
                    {"function": {"name": e[2], "arguments": e[3]}}
                    for e in parsed_calls
                ]
                parallel_tcs, serial_tcs = self._concurrency_classifier.partition(tc_formatted)
                # 按 (function_name, function_args) 反查回原 entry,保持顺序
                parallel_entries, serial_entries = [], []
                serial_lookup = list(parsed_calls)  # 待匹配队列
                for tc in parallel_tcs:
                    name = tc["function"]["name"]
                    args = tc["function"]["arguments"]
                    for i, e in enumerate(serial_lookup):
                        if e[2] == name and e[3] == args:
                            parallel_entries.append(serial_lookup.pop(i))
                            break
                serial_entries = serial_lookup  # 剩余即为串行组

                semaphore = asyncio.Semaphore(self._max_concurrency)

                async def _exec_one(entry):
                    """单个工具并行执行协程,异常隔离返回(不抛出,避免gather取消其他任务)"""
                    tool_call_id, tool, function_name, function_args, idx = entry
                    async with semaphore:
                        try:
                            result = await self._invoke_tool(tool, function_name, function_args)
                            return (idx, tool_call_id, tool, function_name, function_args, result, None)
                        except Exception as e:
                            logger.exception(f"并行执行工具[{function_name}]异常: {e}")
                            return (idx, tool_call_id, tool, function_name, function_args, None, str(e))

                parallel_results = await asyncio.gather(
                    *[_exec_one(e) for e in parallel_entries],
                ) if parallel_entries else []

                serial_results = []
                for entry in serial_entries:
                    tool_call_id, tool, function_name, function_args, idx = entry
                    try:
                        result = await self._invoke_tool(tool, function_name, function_args)
                        serial_results.append((idx, tool_call_id, tool, function_name, function_args, result, None))
                    except Exception as e:
                        logger.exception(f"串行执行工具[{function_name}]异常: {e}")
                        serial_results.append((idx, tool_call_id, tool, function_name, function_args, None, str(e)))

                # 阶段C: 按原始顺序合并结果 + 产出CALLED事件 + 组装tool_messages
                merged = [(r[0], "normal", r) for r in parallel_results]
                merged.extend((r[0], "normal", r) for r in serial_results)
                merged.extend((idx, "unknown", err_msg) for idx, err_msg in unknown_results.items())
                merged.sort(key=lambda x: x[0])

                for idx, kind, payload in merged:
                    if kind == "unknown":
                        tool_messages.append(payload)
                    else:
                        _, tool_call_id, tool, function_name, function_args, result, err = payload
                        if err is not None:
                            result = ToolResult(success=False, message=f"工具执行异常: {err}")
                        yield ToolEvent(
                            tool_call_id=tool_call_id,
                            tool_name=tool.name,
                            function_name=function_name,
                            function_args=function_args,
                            function_result=result,
                            status=ToolEventStatus.CALLED,
                        )
                        tool_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "function_name": function_name,
                            "content": self._build_tool_message_content(
                                result, function_name,
                                supports_images=self._llm_supports_images,
                                max_len=self._get_dynamic_max_len(function_name),
                            ),
                        })
            else:
                # 原串行路径(并行未启用或仅单工具调用) — 保持原语义,向後兼容
                # F10-2 重构: 解析逻辑提取到 _parse_tool_call,消除重复代码
                for tool_call in message["tool_calls"]:
                    parsed = await self._parse_tool_call(tool_call)
                    # 解析失败: 生成错误 tool_message,不产出 CALLING 事件
                    if parsed.error_content is not None:
                        tool_messages.append(self._build_error_tool_message(parsed))
                        continue
                    # 正常路径: CALLING 事件 → 执行 → CALLED 事件
                    yield ToolEvent(
                        tool_call_id=parsed.tool_call_id,
                        tool_name=parsed.tool.name,
                        function_name=parsed.function_name,
                        function_args=parsed.function_args,
                        status=ToolEventStatus.CALLING,
                    )
                    result = await self._invoke_tool(parsed.tool, parsed.function_name, parsed.function_args)
                    yield ToolEvent(
                        tool_call_id=parsed.tool_call_id,
                        tool_name=parsed.tool.name,
                        function_name=parsed.function_name,
                        function_args=parsed.function_args,
                        function_result=result,
                        status=ToolEventStatus.CALLED,
                    )
                    tool_messages.append(self._build_tool_result_message(
                        parsed, result,
                        supports_images=self._llm_supports_images,
                        max_len=self._get_dynamic_max_len(parsed.function_name),
                    ))

            # F10-2 重构: 迭代预算感知 + 会话超时熔断注入提取到 _inject_budget_warnings
            session_timeout_injected = self._inject_budget_warnings(
                iteration=iteration,
                tool_messages=tool_messages,
                session_start_ts=self._session_start_ts,
                session_timeout_injected=session_timeout_injected,
            )

            # 14.所有工具都执行完成后，调用LLM获取汇总消息
            # P4-6流式改造: 使用_stream_llm_invoke,实时推送工具执行后的思考内容
            # 形成「思考→工具→思考→工具→…→最终答案」完整决策链路透传
            message = None
            async for evt in self._stream_llm_invoke(tool_messages):
                if isinstance(evt, dict):
                    message = evt
                else:
                    yield evt
        else:
            # 14.超过最大迭代次数 - 执行强制总结而非硬错误
            logger.warning(f"Agent[{self.name}]达到最大迭代次数({max_iter})，执行强制总结")
            try:
                summary_message = await self._invoke_llm([{
                    "role": "user",
                    "content": "【系统强制指令】你已达到最大迭代次数限制。必须立即停止调用任何工具，基于已收集的信息给出尽可能完整的最终结果。直接输出结果，不要再调用工具。",
                }])
                if summary_message and summary_message.get("content"):
                    # F10-1: 强制总结也走流式切片,统一前后端交互契约
                    # JSON 模式(_format="json_object")的 content 是 JSON,不切片
                    async for evt in self._stream_final_answer(
                            strip_dsml_artifacts(summary_message["content"]),
                            content_is_json=(self._format == "json_object"),
                    ):
                        yield evt
                    return
            except Exception as e:
                logger.error(f"Agent[{self.name}]强制总结失败: {str(e)}")

            yield ErrorEvent(error=f"Agent迭代超过最大迭代次数: {max_iter}, 任务处理失败")
            return

        # 16.在指定步骤内完成了迭代则返回最终答案消息事件
        # F10-1: 最终答案走流式切片推送,前端先接收 is_streaming 增量,最后接收 is_final 完整内容写库
        # JSON 模式(_format="json_object")的 content 是 LLM 原始 JSON 输出,不切片
        if message and message.get("content") is not None:
            async for evt in self._stream_final_answer(
                    strip_dsml_artifacts(message["content"]),
                    content_is_json=(self._format == "json_object"),
            ):
                yield evt
        else:
            yield ErrorEvent(error="Agent未能生成有效回复内容")
