#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/5/16 11:38

@File    : app_config.py
"""
import uuid
from enum import Enum
from typing import Dict, Optional, List, Any

from pydantic import BaseModel, HttpUrl, Field, ConfigDict, model_validator


class ThinkingMode(str, Enum):
    """DeepSeek V4思考模式枚举

    ENABLED: 开启思考模式，模型先推理后回答，响应携带reasoning_content
    DISABLED: 关闭思考模式，模型直接回答，无推理过程
    """
    ENABLED = "enabled"
    DISABLED = "disabled"


class LLMProvider(str, Enum):
    """LLM服务提供商枚举

    OPENAI: OpenAI兼容协议，涵盖 DeepSeek/GLM/Qwen/Kimi 等主流国产模型
    """
    OPENAI = "openai"


class LLMConfig(BaseModel):
    """LLM提供商配置

    模型与思考模式的推荐组合：
    - deepseek-v4-pro + thinking_mode=ENABLED: 深度推理场景（默认）
    - deepseek-v4-flash + thinking_mode=DISABLED: 快速响应场景
    也可自由组合，如v4-pro关闭思考或v4-flash开启思考
    """
    provider: LLMProvider = LLMProvider.OPENAI  # LLM服务提供商，OpenAI兼容协议涵盖 DeepSeek/GLM/Qwen/Kimi
    base_url: HttpUrl = "https://api.deepseek.com"
    api_key: str = ""
    model_name: str = "deepseek-v4-pro"
    temperature: float = Field(0.7)
    max_tokens: int = Field(8192, ge=0)
    thinking_mode: ThinkingMode = ThinkingMode.ENABLED  # 思考模式开关，对应extra_body.thinking.type
    reasoning_effort: str = Field("high", pattern=r"^(low|medium|high|max|xhigh)$")  # 思考强度，仅thinking=enabled时生效
    context_window: int = Field(64000, gt=0)  # 模型上下文窗口大小(token)，用于TokenCounter判定压缩阈值
    # LLM调用最大重试次数(批次50,503容错增强): 503/502服务不可用时自动重试
    # 默认5次,配合指数退避(503/502最长30s),给服务端充分恢复时间
    max_retries: int = Field(default=5, ge=1, le=10)
    # 是否支持图像输入(多模态): 控制工具结果中的截图是否以image_url块发送给LLM。
    # DeepSeek等纯文本模型必须设为false,否则tool消息含image_url时API返回400
    # (会话a34fcdc1根因: deepseek-v4-flash非多模态,browser_view截图构建image_url被拒)。
    # GLM-5.2/qwen-vl-max/gpt-4o等视觉模型可设为true,启用截图视觉辅助决策。
    supports_image_input: bool = False


class AgentConfig(BaseModel):
    """Agent通用配置"""
    max_iterations: int = Field(default=100, gt=0, lt=1000)  # Agent最大迭代次数
    max_retries: int = Field(default=3, gt=1, lt=10)  # 最大重试次数
    max_search_results: int = Field(default=10, gt=1, lt=30)  # 最大搜索结果条数
    # 会话级超时熔断(P10-3): 超过阈值时注入提示引导LLM快速收敛,0表示不启用
    # 单位:秒。生产配置14400(4小时),warning 12000(200分钟,83%阈值);此处default为安全兜底
    # max_iterations 需与超时联动: 生产 max_iterations=300, 平均每次迭代30-60s, 约覆盖4小时
    session_timeout_seconds: int = Field(default=1800, ge=0, le=14400)
    session_warning_seconds: int = Field(default=1500, ge=0, le=14400)
    # 专业能力关键词(F2-4外置): 用于ReAct识别需优先MCP工具的步骤
    # 默认覆盖多模态能力与通用专业领域服务,运维可通过config.yaml调整
    special_capability_keywords: List[str] = Field(default_factory=lambda: [
        # 多模态能力
        "图片", "图像", "多模态", "ocr", "语音", "视频", "视觉",
        "image", "vision", "speech", "video",
        # 专业领域服务(通用能力类别,非特定供应商,通常由MCP工具提供)
        "天气", "weather", "地图", "map", "位置", "location",
        "翻译", "translate", "汇率", "exchange",
    ])
    # F10-1 流式输出开关: 启用后最终答案切片流式推送,降低用户感知时延
    # 默认True(开启)。关闭时保持一次性返回完整MessageEvent(is_final=True)
    stream_final_answer: bool = True
    # F10-1 流式切片配置
    stream_chunk_min_chars: int = Field(default=50, ge=10, le=500)  # 单片最小字符数(避免过细)
    stream_chunk_max_chars: int = Field(default=300, ge=50, le=2000)  # 单片最大字符数(避免过粗)
    stream_chunk_delay_ms: int = Field(default=30, ge=0, le=500)  # 切片推送间隔(毫秒,模拟流式节奏)
    # 思考过程流式开关(改进A): 启用后将 reasoning_content 切片流式推送到「思考中」区域
    # 复用上述 stream_chunk_* 切片配置。默认True(开启)。
    # 关闭时后端不推送思考事件(reasoning_content 仍写 memory,行为同现状),紧急回滚设False
    stream_thinking: bool = True
    # Shell 命令输出流式开关(改进B): 启用后命令执行期间轮询 read_shell_output 增量推送 console
    # 中间事件 is_streaming=True 仅推 SSE,CALLED 携带完整 console 确保回放完整。默认True(开启)。
    # 关闭时 CALLED 一次性返回增量 console(行为同现状),紧急回滚设False
    stream_shell_output: bool = True
    # F10-6 工具按需装配开关: 启用后基于步骤描述关键词过滤工具,降低单轮token消耗
    # 默认False(关闭),保持向后兼容。启用时按需过滤,过滤后工具数<min_tools时回退全量装配
    tool_filter_enabled: bool = False
    tool_filter_min_tools: int = Field(default=3, ge=1, le=20)  # 过滤后最小工具数,低于此值回退全量
    # Batch 39 / 方向2: 工具调用预算外置配置
    # 运维可通过 config.yaml 覆盖默认预算(search_web=8/deep_research=2/browser_navigate=10)
    # 空字典(默认)使用 budget_tracker._DEFAULT_BUDGETS;非空字典覆盖对应工具的预算
    # 示例: {"search_web": 10, "deep_research": 3} 仅覆盖这两项,其余保持默认
    tool_budgets: Dict[str, int] = Field(default_factory=dict)


class FilePresentationConfig(BaseModel):
    """文件展示策略配置(F2-3外置 + F10-8集中化)

    将原本硬编码在SessionService._is_likely_process_file中的过程文件识别模式
    外置到配置,运维可根据业务场景调整,无需改代码重新部署。

    F10-8交付质量校验: 将原本分散在MemoryConfig的excluded_extensions/max_deliverable_files
    集中到本配置,实现交付物过滤规则单一数据源,避免双套规则不一致。
    """
    # 文件类型交付优先级(数值越大越靠前)
    file_type_priority: Dict[str, int] = Field(default_factory=lambda: {
        ".xlsx": 100, ".xls": 100, ".csv": 95,
        ".docx": 90, ".doc": 90,
        ".pdf": 85, ".pptx": 85, ".ppt": 85,
        ".png": 80, ".jpg": 80, ".jpeg": 80, ".gif": 75,
        ".md": 70,
        ".html": 65, ".htm": 65,  # HTML交付物(用户明确要求web页面/HTML时为合法交付物)
        ".txt": 60,
        ".json": 50,
        ".py": 30, ".js": 30, ".sh": 30,
        ".log": 20,
    })
    default_file_priority: int = 40  # 未知扩展名默认优先级

    # 过程文件识别模式(满足任一即视为过程文件,从交付列表中剔除)
    log_extensions: List[str] = Field(default_factory=lambda: [".log"])  # 日志类必定为过程文件
    # 中间产物扩展名(必定过滤不看文件名): AI生成的非交付物中间文件
    # 默认空列表: HTML等扩展名不再一刀切过滤,改由 intermediate_path_prefixes(路径前缀)
    # 独占中间产物拦截职责——/tmp/和/workspace/下的HTML分片由路径过滤,根目录HTML为合法交付物
    # 历史: 批次51曾将.html/.htm加入此列表(会话b6505eb7网页抓取产物误交付),
    # 但会话b30b3e14暴露该一刀切策略误杀用户明确要求的HTML交付物(用户"输出html给我"却收不到)
    intermediate_extensions: List[str] = Field(default_factory=lambda: [])
    # 中间产物路径前缀(批次51,必定过滤): 这些路径下的文件视为过程文件
    # 设计意图: /home/ubuntu/workspace/和/tmp/设计用于存放中间产物,根目录存放最终交付物
    # 最可靠信号: 即使AI未遵守提示词将中间产物放对目录,路径仍是强过滤信号
    intermediate_path_prefixes: List[str] = Field(default_factory=lambda: [
        "/home/ubuntu/workspace/",
        "/tmp/",
    ])
    script_extensions: List[str] = Field(default_factory=lambda: [".py", ".js"])  # 脚本类过程文件扩展名
    script_name_patterns: List[str] = Field(default_factory=lambda: [
        "analysis", "analyze", "report", "generate", "gen",
        "create", "process", "build", "summary",
        # 批次51新增: 覆盖网页抓取/数据处理/校验类工具脚本(会话b6505eb7暴露)
        "extract", "append", "inspect", "verify",
        "fetch", "scrape", "parse", "convert",
        "scan", "download",
    ])  # 脚本类过程文件名匹配模式(子串匹配,大小写不敏感)
    text_process_extension: str = ".txt"  # 文本过程文件扩展名
    text_process_name_patterns: List[str] = Field(default_factory=lambda: [
        "_check", "check_", "_preview", "preview_",
        "_syntax", "syntax_", "_cols", "cols_",
        "_debug", "debug_", "_temp", "temp_",
        "_test", "test_", "_overview", "overview_",
        "section", "_v1", "_v2", "_v3", "_v4", "_v5",
        "data_check", "column_overview", "final_check",
        "syntax_check", "cols_check", "date_check",
        "data_preview", "analysis_output", "analysis_section",
        "extra_clean", "clean_report", "report_data",
        # 扩展过程文件模式(覆盖数据分析场景常见过程文件)
        # 注意: 仅使用精确模式,避免误杀final_result.txt等正常交付物
        # 通用性设计: 默认值仅含通用过程文件模式,业务特定模式(如v4_basic/final_cols)
        # 由业务方在config.yaml中按需声明,确保通用型框架不绑定具体业务
        "_summary", "summary_", "data_summary",
        "analysis_result", "data_analysis",
        "_out", "out_", "_md5", "md5_",
        "_short",
        # 切片文件模式(覆盖 read_file 行切片 + sed -n 'N,Mp' 输出常见命名)
        # 设计动机: LLM 用 read_file 读取大文件后,可能用 write_file 创建
        # lines91_95.txt / lines_91_95.txt 等切片文件暂存内容,误写入 attachments
        # 子串匹配"lines"会覆盖该模式,极少数用户交付物命名为 headlines/timelines
        # 时会被误杀,但通常真正交付物应使用语义化命名(report/summary等)
        "lines",
        # 批次51新增: 覆盖网页抓取/文本提取中间产物(会话b6505eb7暴露)
        # extracted: forrester_extracted.txt/gartner_extracted.txt/ai_trends_extracted.txt
        # headings: headings.txt(标题提取中间产物)
        # fetched/scraped/parsed: 抓取/爬取/解析的文本中间产物
        "extracted", "headings", "raw_", "_raw",
        "fetched", "scraped", "parsed", "content_",
    ])  # 文本过程文件名匹配模式(子串匹配,大小写不敏感)

    # F10-8交付物过滤集中化配置(原MemoryConfig字段迁移至此,单一数据源)
    # 临时文件扩展名: 命中即从交付列表剔除(不看文件名,仅看扩展名)
    # 与log_extensions区别: log_extensions仅过滤.log(过程文件识别),excluded_extensions覆盖更广的临时文件类型
    excluded_extensions: List[str] = Field(default_factory=lambda: [
        ".tmp", ".temp", ".bak", ".log", ".swp", ".cache",
        ".pyc", ".class", ".o", ".obj",
    ])  # 临时文件扩展名(扩展名命中即从交付物中剔除)
    # 交付物最大文件数: 超过此值时按列表末尾截断(保留最近生成的文件)
    max_deliverable_files: int = Field(default=20, ge=1, le=200)  # 交付物最大文件数


class MCPTransport(str, Enum):
    """MCP传输类型枚举"""
    STDIO = "stdio"  # 本地输入输出
    SSE = "sse"  # 流式事件
    STREAMABLE_HTTP = "streamable_http"  # 流式HTTP


class MCPServerConfig(BaseModel):
    """MCP服务配置"""
    # 通用配置字段
    transport: MCPTransport = MCPTransport.STREAMABLE_HTTP  # 传输协议
    enabled: bool = True  # 是否开启，默认为True
    description: Optional[str] = None  # 服务器描述
    env: Optional[Dict[str, Any]] = None  # 环境变量配置

    # stdio配置
    command: Optional[str] = None  # 启用命令
    args: Optional[List[str]] = None  # 命令参数

    # streamable_http&sse配置
    url: Optional[str] = None  # MCP服务URL地址
    headers: Optional[Dict[str, Any]] = None  # MCP服务请求头

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def validate_mcp_server_config(self):
        """校验mcp_server_config的相关信息，包含url+command"""
        # 1.判断transport是否为sse/streamable_http
        if self.transport in [MCPTransport.SSE, MCPTransport.STREAMABLE_HTTP]:
            # 2.这两种模式需要传递url
            if not self.url:
                raise ValueError("在sse或streamable_http模式下必须传递url")

        # 3.判断transport是否为stdio类型
        if self.transport == MCPTransport.STDIO:
            # 4.stdio类型必须传递command
            if not self.command:
                raise ValueError("在stdio模式下必须传递command")

        return self


class MCPConfig(BaseModel):
    """应用MCP配置"""
    mcpServers: Dict[str, MCPServerConfig] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)


class A2AServerConfig(BaseModel):
    """A2A服务配置"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))  # 唯一标识
    base_url: str  # 服务基础URL
    enabled: bool = True  # 服务是否开启


class A2AConfig(BaseModel):
    """A2A配置"""
    a2a_servers: List[A2AServerConfig] = Field(default_factory=list)


class DeepResearchConfig(BaseModel):
    """深度研究配置"""
    max_depth: int = Field(default=2, ge=1, le=5)  # 最大递归深度
    results_per_search: int = Field(default=5, ge=1, le=20)  # 每轮搜索结果数上限
    max_insights: int = Field(default=20, ge=5, le=100)  # 最大洞察数上限
    time_limit_seconds: int = Field(default=120, ge=30, le=600)  # 总超时(秒)


class SearchConfig(BaseModel):
    """搜索能力配置（缓存+正文抓取+深度研究）"""
    cache_enabled: bool = True  # 是否启用Redis缓存
    cache_ttl_seconds: int = Field(default=3600, ge=60, le=86400)  # 缓存TTL(秒)
    cache_key_prefix: str = "search"  # 缓存key前缀
    fetch_timeout: float = Field(default=15, ge=3, le=60)  # 单页抓取超时(秒)
    fetch_max_retries: int = Field(default=2, ge=0, le=5)  # 抓取重试次数
    fetch_max_chars: int = Field(default=10000, ge=1000, le=50000)  # 正文截断阈值(字符)
    fetch_max_concurrency: int = Field(default=5, ge=1, le=20)  # 并发抓取上限
    deep_research_config: DeepResearchConfig = Field(default_factory=DeepResearchConfig)  # 深度研究配置


class ToolCacheConfig(BaseModel):
    """工具结果缓存配置

    仅缓存白名单中的幂等工具结果,避免重复调用相同工具产生冗余开销。
    默认开启,可通过 enabled=False 关闭。TTL 较短(30min),避免长会话脏数据。

    设计决策: 采用白名单机制而非黑名单,只缓存明确声明可缓存的工具,默认不缓存,确保安全。
    误缓存代价(脏数据持续命中)远高于漏缓存代价(重复调用),白名单机制将风险降至最低。
    """
    enabled: bool = True  # 是否启用工具结果缓存
    ttl_seconds: int = Field(default=1800, ge=60, le=7200)  # 缓存TTL(秒),默认30分钟
    key_prefix: str = "tool"  # 缓存key前缀,用于命名空间隔离
    # 可缓存工具白名单(仅幂等查询类工具,写入/副作用类工具不缓存)
    cacheable_tools: List[str] = Field(default_factory=lambda: [
        "web_search",           # 搜索结果幂等(同query返回相同结果)
        "deep_research",        # 深度研究结果幂等
        "file_read",            # 文件读取(沙箱内TTL不变)
        "skill_list",           # 技能列表查询
    ])
    # MCP可缓存工具名(子集,仅查询类;由MCPTool内部桥接工具调用,此处按MCP工具名匹配)
    cacheable_mcp_tools: List[str] = Field(default_factory=lambda: [
        "maps_weather",         # 天气查询(短期幂等)
        "maps_geo",             # 地理编码
        "maps_direction",       # 路线规划(短期幂等)
    ])


class IdempotentToolDedupConfig(BaseModel):
    """幂等工具调用去重配置(P10-1,通用型智能体框架能力)

    防止LLM在长会话中因记忆压缩而重复发起相同参数的幂等写操作(如异步任务发起、
    报表导出、邮件发送等)。与 ToolCacheConfig 互补:
    - ToolCacheConfig 缓存幂等查询结果(读操作)
    - IdempotentToolDedupConfig 去重幂等写操作工具调用,命中时返回上次的调用结果

    通用性设计:
    - 默认白名单为空: 未声明的工具不去重,确保安全;业务方按需在config.yaml中配置
    - 不绑定具体业务语义: 类名/字段/注释均为通用"幂等写操作"抽象,不涉及导出/邮件等业务概念
    - TTL较长(1小时): 覆盖一轮完整会话,避免会话内重复发起造成额外耗时
    """
    enabled: bool = True  # 是否启用心幂等去重
    ttl_seconds: int = Field(default=3600, ge=300, le=14400)  # 去重TTL(秒),默认1小时
    key_prefix: str = "tool_dedup"  # key前缀,用于命名空间隔离
    # 需去重的工具名白名单(幂等写操作,如各业务系统的异步任务发起类MCP工具)
    # 默认为空,由业务方在config.yaml中按需配置,确保通用型框架不绑定具体业务
    idempotent_tools: List[str] = Field(default_factory=list)


class SessionPromptCacheConfig(BaseModel):
    """会话级提示词缓存配置

    持久化MCP搜索/描述结果、Skills技能指南、A2A Agent卡片等提示词片段到Redis,
    避免长会话中LLM上下文压缩遗忘后重复search/describe,降低token消耗。

    设计决策:
    - 默认开启: 提示词缓存为幂等读操作,无副作用风险,默认开启降低token消耗
    - L1内存+L2 Redis两级缓存: L1零延迟覆盖高频场景,L2持久化覆盖实例重建场景
    - TTL对齐会话超时: 默认4小时,覆盖完整会话生命周期
    - 静默降级: Redis异常时降级L1内存,不阻塞主流程
    """
    enabled: bool = True  # 是否启用会话级提示词缓存
    ttl_seconds: int = Field(default=14400, ge=300, le=86400)  # 缓存TTL(秒),默认4小时
    key_prefix: str = "prompt"  # 缓存key前缀,用于命名空间隔离


class ToolExecutionConfig(BaseModel):
    """工具执行策略配置(工具并行执行)

    控制ReActAgent工具并行执行行为。默认关闭以保证向后兼容,
    启用后白名单内工具可并行执行,共享状态工具仍串行。

    设计决策:
    - 默认关闭: 高风险优化,首版不启用,验证后改 enabled=true
    - 黑名单机制: 默认所有工具可并行,仅显式声明的共享状态工具串行
      (与工具结果缓存的白名单机制相反,因为工具并行执行目标是最大化并行收益)
    - 3阶段执行: CALLING→execute→CALLED,保证SSE事件顺序可预测
    """
    enabled: bool = False  # 是否启用并行执行(默认关闭,安全第一)
    max_concurrency: int = Field(default=5, ge=2, le=10)  # 最大并发数
    # 不可并行工具前缀(共享状态,必须串行)
    # 注意: shell_execute 单独按 stateful_tool_arg_keys 参数级隔离,
    # stateful_tool_prefixes 保留 "shell_" 以覆盖 shell_read_output/shell_wait_process 等状态依赖子工具
    stateful_tool_prefixes: List[str] = Field(default_factory=lambda: [
        "shell_",      # 共享 sandbox session(命令依赖前序状态)
        "browser_",    # 共享浏览器实例(单页面焦点)
    ])
    # 不可并行工具全名(写操作,共享文件系统)
    stateful_tool_names: List[str] = Field(default_factory=lambda: [
        "file_write",
        "file_delete",
        "file_move",
        "file_upload",
    ])
    # 参数级隔离工具配置: {tool_name: [arg_key]}
    # 同一 arg_key 值的工具调用串行,不同 arg_key 值的可并行
    # 典型场景: shell_execute 按 session_id 隔离,同 session_id 串行,不同 session_id 可并行
    stateful_tool_arg_keys: Dict[str, List[str]] = Field(default_factory=lambda: {
        "shell_execute": ["session_id"],
    })


class AppConfig(BaseModel):
    """应用配置信息，包含Agent配置、LLM提供商配置、MCP配置、A2A配置、搜索配置"""
    llm_config: LLMConfig  # 语言模型配置
    # PlanAgent轻量化: 规划Agent专用LLM配置(可选,未配置时复用llm_config)
    # 用于将PlanAgent降级到轻量化模型(thinking=disabled/low effort),降低规划阶段token成本与时延
    planner_llm_config: Optional[LLMConfig] = None
    # 多模态LLM配置(可选,未配置时为None): 浏览器visual_click视觉点击兜底专用。
    # 需指向支持图像输入的视觉模型(如qwen-vl-max/gpt-4o);不配置则visual_click自动降级为不可用。
    multimodal_llm_config: Optional[LLMConfig] = None
    agent_config: AgentConfig  # Agent通用配置
    mcp_config: MCPConfig  # MCP服务配置
    a2a_config: A2AConfig  # A2A服务配置
    search_config: SearchConfig = Field(default_factory=SearchConfig)  # 搜索能力配置，默认值保证向后兼容
    # 文件展示策略配置(F2-3外置),默认值保证向后兼容(老config.yaml无此字段时使用默认模式)
    file_presentation_config: FilePresentationConfig = Field(default_factory=FilePresentationConfig)
    # 工具结果缓存配置,默认值保证向后兼容
    tool_cache_config: ToolCacheConfig = Field(default_factory=ToolCacheConfig)
    # 幂等工具调用去重配置(P10-1,通用型智能体框架能力),默认值保证向后兼容
    idempotent_tool_dedup_config: IdempotentToolDedupConfig = Field(default_factory=IdempotentToolDedupConfig)
    # 会话级提示词缓存配置(MCP/Skills/A2A提示词Redis持久化),默认值保证向后兼容
    session_prompt_cache_config: SessionPromptCacheConfig = Field(default_factory=SessionPromptCacheConfig)
    # 工具并行执行配置,默认关闭保证向后兼容
    tool_execution_config: ToolExecutionConfig = Field(default_factory=ToolExecutionConfig)

    # Pydantic配置，允许传递额外的字段初始化
    model_config = ConfigDict(extra="allow")
