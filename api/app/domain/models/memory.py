#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : memory.py
工业级记忆系统 - 四层渐进式压缩、关键事实提取、容量管控、工具结果截断、图片清理
参考: Hermes Agent 策展式记忆 + 旧项目四层降级恢复机制
"""
import json
import logging
import hashlib
import re
from datetime import datetime
from enum import IntEnum
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, PrivateAttr

from app.domain.models.memory_config import DEFAULT_MEMORY_CONFIG as _CFG
# 领域解耦: extract_search_candidates 集中在tool_search.py维护mcp_tool_search返回格式知识,
# 避免memory.py硬编码领域格式(向后兼容:解析历史会话中mcp_tool_search工具调用的返回结果)
from app.domain.services.tools.tool_search import extract_search_candidates

logger = logging.getLogger(__name__)


class CompressionLevel(IntEnum):
    """压缩级别 - 两层渐进式(Phase E简化: 四层→两层)

    - NONE: 无需压缩
    - NORMAL: 常规压缩 — 统一工具内容截断 + 连续消息合并
    - EMERGENCY: 紧急压缩 — 保留系统提示 + 会话摘要 + 关键事实 + 最近N条
    """
    NONE = 0
    NORMAL = 1
    EMERGENCY = 2


_BROWSER_VIEW_TOOLS = frozenset({"browser_view", "browser_navigate", "browser_restart"})
_BROWSER_ACTION_TOOLS = frozenset({
    "browser_click", "browser_input", "browser_scroll_up", "browser_scroll_down",
    "browser_scroll_to_text", "browser_press_key", "browser_select_option",
    "browser_move_mouse", "browser_wait",
    "browser_wait_for", "browser_network_requests",
})
# browser_console_exec 返回JS执行结果(LLM需读取的data),与click等纯动作工具不同。
# 压缩为"executed"会丢失返回值(会话e3f0762b根因:LLM反复调用console_exec验证
# radio状态,但返回值被压缩为"(browser_console_exec executed)"不可见,浪费调用预算)。
# 单独处理:保留result字段,仅截断过长结果。
_BROWSER_DATA_TOOLS = frozenset({"browser_console_exec"})
_FILE_TOOLS = frozenset({
    "file_write", "file_read", "file_list", "file_delete",
    "write_file", "read_file", "replace_in_file", "find_files",
})
_SHELL_TOOLS = frozenset({"shell_exec", "shell_execute", "shell_read_output", "shell_wait_process", "shell_write_input"})
_SEARCH_TOOLS = frozenset({"search_web"})
_DEEP_RESEARCH_TOOLS = frozenset({"deep_research"})
_MCP_TOOL_PREFIX = "mcp_"  # MCP工具函数名前缀（动态注册，按前缀识别）
# MCP桥接工具(用于按需加载MCP工具,函数名以mcp_tool_开头)
# mcp_tool_search返回的候选工具列表需特殊处理,保留到key_facts防止emergency_compact后重复搜索
_MCP_SEARCH_TOOLS = frozenset({"mcp_tool_search"})

# 步骤完成状态提取正则: 匹配 build_prior_steps_context 注入的"步骤{id}(已完成)：{result}"格式
# 根因: emergency_compact后执行步骤用户消息被压缩,LLM丢失步骤完成状态,
# 重新执行已完成的describe/导出/验证等步骤(会话6d4f313b根因)。
# 提取为step_completed关键事实,压缩后仍可注入系统提示防丢步。
_STEP_COMPLETED_PATTERN = re.compile(r"步骤\s*(\d+)\s*\(已完成\)\s*[:：]\s*([^\n]+)")
# 当前步骤描述提取正则: 从EXECUTION_PROMPT用户消息中提取"当前步骤：{desc}"
_CURRENT_STEP_PATTERN = re.compile(r"当前步骤[：:]\s*(.+?)(?:\n\n|\n⚠️|$)", re.DOTALL)

# 阈值统一由 MemoryConfig 管理，以下常量引用配置，保持向后兼容
_BROWSER_CONTENT_THRESHOLD = _CFG.browser_content_threshold
_SEARCH_RESULT_THRESHOLD = _CFG.search_result_threshold
_SEARCH_RESULT_KEEP = _CFG.search_result_keep
_SHELL_OUTPUT_KEEP = _CFG.shell_output_keep
_FILE_CONTENT_KEEP = _CFG.file_content_keep

_PROTECT_HEAD_COUNT = _CFG.protect_head_count
_PROTECT_TAIL_COUNT = _CFG.protect_tail_count
_MAX_MESSAGES_SOFT = _CFG.max_messages_soft
_MAX_MESSAGES_HARD = _CFG.max_messages_hard
_KEY_FACTS_MAX = _CFG.key_facts_max

_TOOL_RESULT_MAX_LENGTH = _CFG.tool_result_max_length
_BROWSER_RESULT_MAX_LENGTH = _CFG.browser_result_max_length
_FILE_RESULT_MAX_LENGTH = _CFG.file_result_max_length
_SHELL_RESULT_MAX_LENGTH = _CFG.shell_result_max_length
_SEARCH_RESULT_MAX_LENGTH = _CFG.search_result_max_length
_DEEP_RESEARCH_RESULT_MAX_LENGTH = _CFG.deep_research_result_max_length

_SESSION_SUMMARY_MAX = _CFG.session_summary_max
_SESSION_SUMMARY_INJECT_MAX = _CFG.session_summary_inject_max
_COMPRESSION_SUMMARY_PER_OP = _CFG.compression_summary_per_op

# 主动预测压缩阈值
_PROACTIVE_COMPRESS_THRESHOLD = _CFG.proactive_compress_threshold
_REACTIVE_COMPRESS_THRESHOLD = _CFG.reactive_compress_threshold
_CRITICAL_THRESHOLD = _CFG.critical_threshold
_HIGH_PRESSURE_TRUNCATE_MAX = _CFG.high_pressure_truncate_max

# P4-2: 压缩策略(auto/proactive/reactive),由base.py _should_proactive_compress()使用
_COMPRESSION_STRATEGY = _CFG.compression_strategy


class KeyFact(BaseModel):
    """关键事实条目 - Hermes Agent策展式记忆

    类别覆盖: requirement/url/file/cmd/decision/error/mcp_tool/page_title
    timestamp 为 ISO 格式时间戳，旧记录无此字段时为 None（向后兼容）
    content_hash 基于category+归一化content计算,用于增量去重
    """
    category: str
    content: str
    timestamp: Optional[str] = None
    content_hash: str = ""

    def model_post_init(self, __context) -> None:
        """Pydantic v2后置初始化: 自动计算content_hash(未设置时)"""
        if not self.content_hash:
            self.content_hash = self._compute_hash(self.category, self.content)

    @classmethod
    def _compute_hash(cls, category: str, content: str) -> str:
        """基于category+归一化content计算MD5 hash

        归一化策略:
        - url类: 去除查询参数和fragment,仅保留scheme+host+path
        - file类: 去除沙箱前缀/home/ubuntu/
        - 其他: 压缩内部空白
        """
        normalized = content.strip()
        if category == "url":
            try:
                parsed = urlparse(normalized)
                normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            except Exception as e:
                # F4-2: URL解析失败保留原始内容(降级处理,不阻断去重)
                logger.debug(f"URL去重解析失败,保留原始内容: {e}")
        elif category == "file":
            normalized = normalized.replace("/home/ubuntu/", "")
        else:
            normalized = re.sub(r'\s+', ' ', normalized)
        return hashlib.md5(f"{category}:{normalized}".encode()).hexdigest()


class MemoryMetrics(BaseModel):
    """记忆监控指标(Phase E简化: 移除aggressive_count/minimal_count)"""
    message_count: int = 0
    compact_count: int = 0
    emergency_count: int = 0
    last_compression_level: int = 0  # 最近一次压缩级别（0-2对应CompressionLevel）
    session_summary_chars: int = 0  # 会话进展摘要当前字符数
    last_summary_index: int = 0  # 上次摘要到的消息索引（只摘要新增消息，避免重复堆积）


class Memory(BaseModel):
    """工业级记忆系统

    架构设计（参考Hermes Agent）：
    - L1 短期上下文：messages消息列表，滑动窗口管理
    - L2 关键事实：key_facts策展式记忆，压缩后保留
    - L3 用户关联：通过Session.user_id预留
    - L4 持久归档：通过PostgreSQL JSONB持久化

    两层渐进式压缩(Phase E简化: 四层→两层):
    - compact(): 常规压缩 — 统一工具内容截断 + 连续消息合并
    - emergency_compact(): 紧急压缩 — 保留系统提示+会话摘要+关键事实+最近N条
    """
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    key_facts: List[KeyFact] = Field(default_factory=list)
    session_summary: str = ""  # 跨压缩周期累积的会话进展摘要（永不压缩，压缩前追加）
    metrics: MemoryMetrics = Field(default_factory=MemoryMetrics)
    # Batch 33: 脏标记(PrivateAttr不参与序列化),变更方法置脏,LLM调用边界统一刷盘
    _dirty: bool = PrivateAttr(default=False)

    @property
    def dirty(self) -> bool:
        """记忆是否有未持久化的变更"""
        return self._dirty

    def mark_clean(self) -> None:
        """标记记忆已持久化(刷盘成功后调用)"""
        self._dirty = False

    @classmethod
    def get_message_role(cls, message: Dict[str, Any]) -> str:
        return message.get("role", "")

    def add_message(self, message: Dict[str, Any]) -> None:
        self.messages.append(message)
        self.metrics.message_count = len(self.messages)
        self._dirty = True

    def add_messages(self, messages: List[Dict[str, Any]]) -> None:
        self.messages.extend(messages)
        self.metrics.message_count = len(self.messages)
        self._dirty = True

    def get_messages(self) -> List[Dict[str, Any]]:
        return self.messages

    def get_last_message(self) -> Optional[Dict[str, Any]]:
        return self.messages[-1] if self.messages else None

    def roll_back(self) -> None:
        if self.messages:
            self.messages = self.messages[:-1]
            self.metrics.message_count = len(self.messages)
            self._dirty = True

    @property
    def empty(self) -> bool:
        return len(self.messages) == 0

    def should_compress(self, threshold: float = 0.5) -> bool:
        """判断是否需要压缩（基于消息数量占比）"""
        if len(self.messages) <= _PROTECT_HEAD_COUNT + _PROTECT_TAIL_COUNT:
            return False
        ratio = len(self.messages) / _MAX_MESSAGES_SOFT
        return ratio >= threshold

    def is_context_overflow(self) -> bool:
        """判断是否上下文溢出"""
        return len(self.messages) >= _MAX_MESSAGES_HARD

    def check_token_limit(self, token_counter, context_window: int, threshold: float = 0.7) -> bool:
        """判断是否接近上下文窗口阈值（基于 token 计数）

        与 should_compress/is_context_overflow（基于消息条数）并存，
        token 优先触发压缩，消息条数作为兜底。
        token_counter 为 None 时直接返回 False，兼容未注入场景。

        Args:
            token_counter: TokenCounter 实例（tiktoken）
            context_window: 模型上下文窗口大小(token)
            threshold: 触发阈值比例，默认 0.7（达到 70% 时触发压缩）
        """
        if token_counter is None or context_window <= 0:
            return False
        try:
            current_tokens = token_counter.count_messages(self.messages)
            return current_tokens >= context_window * threshold
        except Exception:
            # tiktoken 异常时不阻断主流程，降级到消息条数判断
            return False

    def predict_token_pressure(
            self, token_counter, context_window: int, pending_tokens: int = 0,
    ) -> Dict[str, Any]:
        """预测token压力等级,指导主动压缩决策

        Args:
            token_counter: TokenCounter实例
            context_window: 上下文窗口大小
            pending_tokens: 即将加入的消息预估token数(如工具结果)

        Returns:
            {
                "current_ratio": float,       # 当前使用比例
                "projected_ratio": float,     # 预测比例(含pending)
                "pressure_level": str,        # safe | moderate | high | critical
                "should_proactive_compress": bool,  # 建议主动压缩
                "should_emergency_compress": bool,  # 建议紧急压缩
            }
        """
        safe_result = {
            "current_ratio": 0.0,
            "projected_ratio": 0.0,
            "pressure_level": "safe",
            "should_proactive_compress": False,
            "should_emergency_compress": False,
        }
        if token_counter is None or context_window <= 0:
            return safe_result
        try:
            current_tokens = token_counter.count_messages(self.messages)
            projected_tokens = current_tokens + pending_tokens
            current_ratio = current_tokens / context_window
            projected_ratio = projected_tokens / context_window

            level = "safe"
            proactive = False
            emergency = False

            if projected_ratio >= _CRITICAL_THRESHOLD:
                level = "critical"
                emergency = True
            elif projected_ratio >= _REACTIVE_COMPRESS_THRESHOLD:
                level = "high"
                proactive = True
            elif projected_ratio >= _PROACTIVE_COMPRESS_THRESHOLD:
                level = "moderate"
                proactive = True

            return {
                "current_ratio": round(current_ratio, 3),
                "projected_ratio": round(projected_ratio, 3),
                "pressure_level": level,
                "should_proactive_compress": proactive,
                "should_emergency_compress": emergency,
            }
        except Exception:
            return safe_result

    @staticmethod
    def truncate_tool_result(content: Any, function_name: str = "") -> Any:
        """工具结果入库前截断 - 防止超长内容撑爆上下文

        不同工具类型有不同截断阈值：
        - 浏览器结果: 保留截图URL，截断DOM内容
        - 文件结果: 保留文件路径，截断文件内容
        - Shell结果: 保留前N字符
        - 搜索结果: 保留前N字符
        - 其他: 通用截断

        兼容多模态list格式：通过_extract_text_from_content统一提取文本后截断。
        F3-2合并: 与truncate_tool_result_dynamic共享_truncate_content_internal截断逻辑,
        统一使用_get_standard_max_len(覆盖deep_research),消除阈值表不一致。
        """
        # 统一提取文本（兼容str和list两种content格式）
        text = Memory._extract_text_from_content(content)
        if not text:
            return content  # 空内容或非str非list，原样返回

        max_len = Memory._get_standard_max_len(function_name)
        return Memory._truncate_content_internal(text, function_name, max_len)

    @staticmethod
    def _get_standard_max_len(function_name: str) -> int:
        """获取工具结果的标准截断阈值"""
        if function_name in _BROWSER_VIEW_TOOLS or function_name in _BROWSER_ACTION_TOOLS:
            return _BROWSER_RESULT_MAX_LENGTH
        if function_name in _FILE_TOOLS:
            return _FILE_RESULT_MAX_LENGTH
        if function_name in _SHELL_TOOLS:
            return _SHELL_RESULT_MAX_LENGTH
        if function_name in _SEARCH_TOOLS:
            return _SEARCH_RESULT_MAX_LENGTH
        if function_name in _DEEP_RESEARCH_TOOLS:
            return _DEEP_RESEARCH_RESULT_MAX_LENGTH
        return _TOOL_RESULT_MAX_LENGTH

    @staticmethod
    def _truncate_content_internal(
        content: str, function_name: str, max_len: int, supports_images: bool = True,
    ) -> str:
        """F3-2合并: 工具结果截断共享内部实现

        统一处理JSON感知截断(浏览器/文件类保留结构化字段)与通用字符截断,
        被 truncate_tool_result(静态,固定阈值) 与 truncate_tool_result_dynamic(动态,token感知阈值)
        共同复用,消除两份重复的截断分支代码。

        Args:
            content: 已提取的纯文本内容(由_extract_text_from_content处理过)
            function_name: 工具名,用于JSON感知分支判定
            max_len: 截断阈值(由调用方决定: 固定/动态)
            supports_images: LLM是否支持图像输入,决定浏览器结果content预算分配
                (False时content 40%,True时25%。会话437cbc75根因修复)

        Returns:
            截断后的字符串(长度<=max_len + 截断标记, 或保留的JSON结构化字段)
        """
        # 浏览器查看类工具: 始终经过优先级截断处理,即使内容未超阈值。
        # 原因1: screenshot base64必须替换为[attached](base64通过多模态image_url块单独传递,不入库文本)
        # 原因2: 确保interactive_elements/ref_map等操作必需字段按优先级保留
        if function_name in _BROWSER_VIEW_TOOLS:
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    return Memory._truncate_browser_view_result(data, max_len, supports_images)
            except (json.JSONDecodeError, TypeError):
                pass

        if len(content) <= max_len:
            return content

        # JSON感知截断: 文件类保留结构化字段
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                if function_name in _FILE_TOOLS:
                    filepath = data.get("filepath", data.get("path", ""))
                    if filepath:
                        return json.dumps({
                            "filepath": filepath,
                            "content": f"(truncated, original {len(content)} chars, use shell_execute+python to read full file)"
                        }, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass

        return content[:max_len] + f"\n...(truncated, original {len(content)} chars)"

    # ==================== 浏览器工具结果优先级截断 ====================

    @staticmethod
    def _truncate_browser_view_result(data: dict, max_len: int, supports_images: bool = True) -> str:
        """浏览器查看类工具(browser_view/navigate/restart)结果优先级截断。

        预算分配策略(会话cd71121a/437cbc75根因修复):
        旧版按优先级顺序添加字段,interactive_elements(389个元素=116KB JSON)占满
        12KB预算后break,导致content字段被完全丢弃。LLM只收到54个交互元素但无
        任何页面文本,误判"browser_view返回空结果"而滥用console_exec探测(10次)。

        自适应预算(会话437cbc75根因修复):
        supports_images=False时(如deepseek-v4-flash),截图被替换为[attached]文本标记,
        零信息价值。content字段成为LLM理解页面内容的唯一通道,需更多预算。
        supports_images=True时(如GLM-5.2),截图通过image_url块传递,可补偿文本截断,
        content预算可较低。
        - supports_images=False: content 40% / interactive_elements 40% / ref_map 20%
        - supports_images=True:  content 25% / interactive_elements 55% / ref_map 20%
        - ref_map: 占用interactive_elements之后的剩余预算(约20%)
        - accessibility_tree: 辅助通道,有剩余空间时才添加

        注意: interactive_elements/ref_map/content即使为空也必须包含在结果中。
        会话3c4debd1暴露: 空值被跳过后LLM看不到字段存在,误判"页面未加载/字段缺失"
        而滥用browser_console_exec查找元素(27次调用)。

        兼容ToolResult包装格式(会话392252b6根因修复):
        _build_tool_message_content 传入的是 text_result.model_dump_json(),
        结构为 {"success":bool,"message":str,"data":{...}}。若不解包,data.get("page_state")
        取顶层返回{},所有字段变空,LLM看到空browser_view结果陷入"view空→console_exec→
        navigate"循环(44次操作 vs 历史会话17次)。此处与_unpack_content_data同源解包。
        """
        # 解包ToolResult外层包装: {"success":..,"data":{...}} → 内层data
        inner = data.get("data")
        if isinstance(inner, dict):
            data = inner
        # 基础结构: 轻量字段,总是完整保留
        result: dict = {
            "screenshot": "[attached]" if data.get("screenshot") else "",
            "page_state": data.get("page_state", {}),
            "pending_dialogs": data.get("pending_dialogs", []),
            "dialog_history": data.get("dialog_history", []),
            "snapshot_version": data.get("snapshot_version", 0),
            # 元素摘要保留(会话1a002224根因修复): element_summary含visible/offscreen/total
            # 计数,让LLM知道interactive_elements列表是否完整。旧版截断时丢弃此字段,
            # LLM看到content被截断后误判"整个页面被压缩",反复抱怨"看不到交互元素"。
            # 保留后LLM可确认"128 total,全部在interactive_elements中",消除压缩焦虑。
            "element_summary": data.get("element_summary", {}),
        }
        # content_hint: SPA空内容防误判提示(轻量,总是保留,引导LLM勿用browser_restart)
        content_hint = data.get("content_hint")
        if content_hint:
            result["content_hint"] = content_hint

        # 计算可用预算(总阈值减去轻量字段的开销)
        base_json = json.dumps(result, ensure_ascii=False)
        available = max_len - len(base_json)
        if available <= 0:
            return json.dumps(result, ensure_ascii=False)

        # 预算分配策略(会话b143f0be/437cbc75/81c801c5根因修复):
        # 旧版35% content保底+interactive_elements吃满剩余→ref_map因break被完全丢弃,
        # LLM看到interactive_elements但无ref_map→无法用@eN点击→退化为text/console_exec。
        # 新策略: 三字段独立预算,ref_map保底20%确保LLM始终能解析ref引用:
        # - supports_images=True:  content 25%(截图补偿文本截断)
        # - supports_images=False: 按content大小自适应(文本LLM无截图通道,但需平衡elements):
        #   · content>8KB(DOM树提取,企业App): content 40%(内容多,需更多预算理解页面)
        #   · content≤8KB(文档容器提取,文档页): content 25%(内容少,elements多,需更多元素预算)
        # - interactive_elements: 剩余预算,操作目标元素列表
        # - ref_map: 20%保底,ref引用映射(LLM通过@eN定位元素的关键)
        # - accessibility_tree: 有剩余空间时才添加(辅助通道)
        content_value = data.get("content", "")
        content_len = len(content_value) if isinstance(content_value, str) else 0
        if not supports_images and content_len > 8000:
            # 内容重型页面(企业App,DOM树提取30K+): content需更多预算
            CONTENT_RESERVE_RATIO = 0.40
        else:
            # 多模态LLM 或 元素重型页面(文档页,文档容器提取≤8KB): elements需更多预算
            CONTENT_RESERVE_RATIO = 0.25
        REF_MAP_RESERVE_RATIO = 0.20
        content_budget = min(content_len, int(available * CONTENT_RESERVE_RATIO))

        # 先添加content(保底预算),确保LLM获得页面文本
        # 方案A: content_truncated标记解锁console_exec护栏(会话437cbc75根因修复)
        # content被截断时,护栏前提"content已包含DOM树文本"失效,
        # 允许LLM用console_exec补偿提取被截断的表格/弹窗文本。
        # 标记始终存在(True/False),避免LLM因字段缺失误判页面未加载。
        #
        # 截断标记增强(会话1a002224根因修复): 旧标记"...(truncated)"仅告知内容被截断,
        # LLM误判"整个页面被压缩"而反复抱怨"看不到交互元素"。新标记明确区分:
        # content文本被截断 ≠ interactive_elements被截断。LLM看到标记后可直接转向
        # interactive_elements列表操作,或用console_exec提取被截断的文本,无需猜测。
        # 措辞中性化(会话dcdf8420根因修复): "truncated"一词引发LLM"信息丢失"焦虑,
        # LLM看到element_summary.total与列表数量不匹配后自行推断"被压缩"。改用"preview"
        # 表述,明确这是预览而非全文,interactive_elements/ref_map完整可用。
        if content_value and content_len > content_budget:
            # content超预算: 截断到保底预算内,标记增强引导LLM转向elements
            _TRUNC_MARK = 80  # 截断标记预留(增强版引导文本)
            _TRUNC_SUFFIX = (
                "...(content preview, interactive_elements and ref_map are complete, "
                "use console_exec for full text if needed)"
            )
            result["content"] = content_value[:max(0, content_budget - _TRUNC_MARK)] + _TRUNC_SUFFIX
            result["content_truncated"] = True
        else:
            result["content"] = content_value or ""
            result["content_truncated"] = False

        # interactive_elements: 截断时为ref_map预留保底空间,避免ref_map被完全丢弃
        elements_value = data.get("interactive_elements", [])
        result["interactive_elements"] = elements_value
        # ref_map为空时不预留预算,interactive_elements可获得全部剩余空间
        ref_map_value = data.get("ref_map", [])
        ref_map_budget = int(available * REF_MAP_RESERVE_RATIO) if ref_map_value else 0
        current_json = json.dumps(result, ensure_ascii=False)
        if len(current_json) > max_len and elements_value:
            # 为ref_map预留预算,interactive_elements最多用到 max_len - ref_map_budget
            # 视口优先截断(会话6a6a0d05根因修复): 识别_format_elements输出的offscreen分隔行,
            # 优先保留visible组(含Dialog表单元素),避免盲目头部截断在预算紧张时丢弃弹窗内表单
            # 元素。旧版_truncate_field_to_fit盲目保留value[:mid],Element Plus Dialog通过Vue
            # teleport渲染,表单元素虽在visible组(P1优先级)但可能因offscreen占用预算被截断。
            result["interactive_elements"] = Memory._truncate_interactive_elements(
                result, elements_value, max_len - ref_map_budget
            )

        # ref_map: 保底预算,确保LLM始终能解析@eN引用(不被interactive_elements挤占)
        result["ref_map"] = ref_map_value
        current_json = json.dumps(result, ensure_ascii=False)
        if len(current_json) > max_len and ref_map_value:
            result["ref_map"] = Memory._truncate_field_to_fit(
                result, "ref_map", ref_map_value, max_len
            )

        # accessibility_tree: 辅助通道,有剩余空间时才添加
        accessibility_value = data.get("accessibility_tree", "")
        if accessibility_value:
            result["accessibility_tree"] = accessibility_value
            current_json = json.dumps(result, ensure_ascii=False)
            if len(current_json) > max_len:
                result["accessibility_tree"] = Memory._truncate_field_to_fit(
                    result, "accessibility_tree", accessibility_value, max_len
                )

        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _truncate_field_to_fit(
        result: dict, field_name: str, value: Any, max_len: int,
    ) -> Any:
        """截断列表或字符串字段,使整体JSON不超过max_len。

        列表: 优先丢弃尾部元素(offscreen元素通常在尾部,且LLM更关注视口内可见元素)。
        字符串: 截断尾部并添加截断标记。
        使用二分查找确定最大可保留量,O(log n)复杂度。
        """
        _TRUNC_MARK = 50  # 截断标记+键名的字符预留

        if isinstance(value, list):
            if not value:
                return value
            # 二分查找最大可保留元素数
            lo, hi = 0, len(value)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                result[field_name] = value[:mid]
                size = len(json.dumps(result, ensure_ascii=False))
                if size <= max_len - _TRUNC_MARK:
                    lo = mid
                else:
                    hi = mid - 1
            truncated = list(value[:lo])
            if lo < len(value):
                truncated.append(f"...({len(value) - lo} more items truncated)")
            return truncated

        if isinstance(value, str):
            if not value:
                return value
            # 二分查找最大可保留字符数
            lo, hi = 0, len(value)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                result[field_name] = value[:mid]
                size = len(json.dumps(result, ensure_ascii=False))
                if size <= max_len - _TRUNC_MARK:
                    lo = mid
                else:
                    hi = mid - 1
            return value[:lo] + "...(truncated)" if lo < len(value) else value[:lo]

        return value

    # ==================== interactive_elements 视口优先截断 ====================

    @staticmethod
    def _truncate_interactive_elements(
        result: dict, elements: list, max_len: int,
    ) -> list:
        """视口优先截断 interactive_elements 文本行列表(会话6a6a0d05根因修复)。

        _format_elements已将visible元素排在前面、offscreen排在后面(以
        "--- offscreen elements"分隔行分隔)。但旧版_truncate_field_to_fit盲目
        二分保留头部value[:mid],预算紧张时存在两个问题:
        ① visible组内部被截断,Dialog表单元素(P1优先级,排在P0状态元素之后)丢失;
        ② mid跨越分隔行时,offscreen元素占用了本该给visible的预算。

        视口优先策略(利用offscreen分隔行识别分区):
        1. 优先保留visible组全部(visible含Dialog表单元素,是当前决策关键)
        2. visible组自身超预算时,二分截断visible组(保留头部=P0状态/P1弹窗元素)
        3. 剩余预算填充offscreen组(保留分隔行+尽可能多的offscreen元素)
        4. 无offscreen分隔行时退化为_truncate_field_to_fit(向后兼容旧格式)

        Args:
            result: 截断结果dict(会原地修改interactive_elements字段用于尺寸测算)
            elements: interactive_elements文本行列表(_format_elements输出)
            max_len: interactive_elements字段的最大字符预算

        Returns:
            截断后的列表(含截断标记行)
        """
        if not elements:
            return elements

        _TRUNC_MARK = 80  # 截断标记预留

        # 定位offscreen分隔行(_format_elements输出 "--- offscreen elements (N total..." )
        offscreen_start = None
        for i, item in enumerate(elements):
            if isinstance(item, str) and item.startswith("--- offscreen elements"):
                offscreen_start = i
                break

        # 无offscreen分隔行: 退化为通用截断(向后兼容无分区格式的旧数据/测试用例)
        if offscreen_start is None:
            return Memory._truncate_field_to_fit(
                result, "interactive_elements", elements, max_len
            )

        visible_part = elements[:offscreen_start]      # visible元素行(含可能的visible omitted标记)
        offscreen_part = elements[offscreen_start:]    # 分隔行+offscreen元素行+原omitted标记

        def _fits(kept_visible: list, kept_offscreen: list) -> bool:
            """测算保留指定内容后整体JSON是否在预算内(原地修改result用于测算)"""
            result["interactive_elements"] = kept_visible + kept_offscreen
            return len(json.dumps(result, ensure_ascii=False)) <= max_len - _TRUNC_MARK

        # Step1: 优先保留visible组全部
        if _fits(visible_part, []):
            kept_visible = list(visible_part)
            # Step2: 剩余预算填充offscreen组
            if _fits(kept_visible, offscreen_part):
                # 全部保留(无需截断),返回完整列表
                return kept_visible + list(offscreen_part)
            # offscreen组需截断: 保留分隔行(让LLM知道存在offscreen元素)+尽可能多的offscreen元素
            separator = [offscreen_part[0]] if offscreen_part else []
            # 实际offscreen元素行(剔除分隔行和原omitted标记,末尾会重新追加新omitted标记)
            offscreen_elems = offscreen_part[1:]
            if offscreen_elems:
                lo, hi = 0, len(offscreen_elems)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if _fits(kept_visible, separator + offscreen_elems[:mid]):
                        lo = mid
                    else:
                        hi = mid - 1
                kept_offscreen = separator + list(offscreen_elems[:lo])
                omitted = len(offscreen_elems) - lo
                if omitted > 0:
                    kept_offscreen.append(
                        f"... ({omitted} more offscreen elements below viewport, scroll to reveal)"
                    )
                return kept_visible + kept_offscreen
            # 仅有分隔行无实际offscreen元素: 保留分隔行
            return kept_visible + separator

        # visible组自身超预算: 二分截断visible组(保留头部=P0状态/P1弹窗优先级元素)
        # _format_elements已按P0>P1>P2排序,保留头部即优先保留Dialog弹窗内表单元素
        lo, hi = 0, len(visible_part)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _fits(visible_part[:mid], []):
                lo = mid
            else:
                hi = mid - 1
        kept_visible = list(visible_part[:lo])
        omitted_visible = len(visible_part) - lo
        if omitted_visible > 0:
            kept_visible.append(
                f"... ({omitted_visible} more visible elements below viewport, "
                f"viewport/dialog elements preserved first)"
            )
        return kept_visible

    def truncate_tool_result_dynamic(
            self, content: Any, function_name: str = "",
            token_counter=None, context_window: int = 0,
    ) -> Any:
        """基于剩余token预算动态截断工具结果

        双向动态策略(会话7720e91d根因修复):
        旧版仅向下缩减(紧张时1/4、中等时1/2、充足时标准),但页面快照遵循
        "只在当前会话上下文临时存在"原则——旧快照由evict_browser_view_content驱逐,
        当前快照是LLM决策的唯一依据。固定12000字符阈值在上下文充足时过度截断,
        LLM看不到完整interactive_elements→误判"被压缩"→反复browser_view循环(24次工具调用)。

        新策略: 上下文充足时向上扩展,让当前快照获得更大预算:
        - 剩余空间充足(>70%): 阈值×2.0(浏览器24000),完整展示页面元素
        - 剩余空间较充足(50%-70%): 阈值×1.5(浏览器18000),兼顾元素覆盖与上下文
        - 剩余空间正常(20%-50%): 标准阈值(浏览器12000)
        - 剩余空间紧张(<20%): 阈值÷4(浏览器3000),仅保留关键信息

        F3-2合并: 与truncate_tool_result共享_truncate_content_internal截断逻辑,
        仅在max_len计算上动态化(基于剩余token预算)。

        兼容多模态list格式：通过_extract_text_from_content统一提取文本后截断。
        """
        # 统一提取文本（兼容str和list两种content格式）
        text = self._extract_text_from_content(content)
        if not text:
            return content  # 空内容或非str非list，原样返回

        max_len = self._get_standard_max_len(function_name)

        # 动态调整: 基于剩余token预算双向伸缩
        if token_counter and context_window > 0:
            try:
                current_tokens = token_counter.count_messages(self.messages)
                remaining_ratio = 1.0 - (current_tokens / context_window)

                if remaining_ratio < 0.2:
                    max_len = max_len // 4  # 紧张: 1/4阈值
                elif remaining_ratio < 0.5:
                    max_len = max_len // 2  # 正常: 1/2阈值
                elif remaining_ratio > 0.7:
                    max_len = int(max_len * 2.0)  # 充足: 2倍阈值(页面快照临时性原则)
                elif remaining_ratio > 0.5:
                    max_len = int(max_len * 1.5)  # 较充足: 1.5倍阈值
            except Exception as e:
                # F4-2: token计数失败降级使用默认max_len(不阻断截断)
                logger.debug(f"动态截断阈值计算失败,使用默认值: {e}")

        return Memory._truncate_content_internal(text, function_name, max_len)

    @staticmethod
    def strip_image_data(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """清理图片base64数据 - 压缩阶段将图片替换为文本标记，避免上下文溢出

        注意: 此方法为压缩专用,不会原地修改messages也不会标记dirty。
        如需在LLM调用后驱逐截图(防止累积),请使用 evict_image_data 实例方法。
        """
        cleaned = []
        for msg in messages:
            m = dict(msg)
            content = m.get("content")
            if isinstance(content, list):
                new_content = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "image_url":
                            url = item.get("image_url", {}).get("url", "")
                            if url.startswith("data:image"):
                                new_content.append({
                                    "type": "text",
                                    "text": "[image data removed for context compression]",
                                })
                            else:
                                new_content.append(item)
                        else:
                            new_content.append(item)
                    else:
                        new_content.append(item)
                m["content"] = new_content
            cleaned.append(m)
        return cleaned

    def evict_image_data(self) -> int:
        """LLM调用后驱逐图片数据,防止截图在记忆中累积。

        浏览器截图(browser_view/navigate/restart的screenshot)仅在当前LLM决策时
        需要,决策完成后应从记忆中移除。旧实现将截图作为image_url块持久化到记忆
        且永不清理(strip_image_data定义但从未调用),导致多次browser_view调用的
        截图累积(~1.5K tokens/张),挤占有效上下文,LLM决策退化。

        根因(会话392252b6 vs 6794ac3c): 10次browser_view截图累积~15K tokens,
        LLM可用上下文缩减,陷入"view空→console_exec扒DOM→navigate重试"循环(44次
        操作 vs 历史会话17次)。

        策略: 原地清理self.messages中所有image_url块,替换为轻量文本标记。
        文本部分(screenshot="[attached]"标记)保留,让LLM知道曾展示过截图。

        Returns:
            被驱逐的图片数量(用于日志观测)
        """
        evicted = 0
        for msg in self.messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            new_content = []
            modified = False
            for item in content:
                if isinstance(item, dict) and item.get("type") == "image_url":
                    url = item.get("image_url", {}).get("url", "")
                    if url.startswith("data:image"):
                        evicted += 1
                        modified = True
                        new_content.append({
                            "type": "text",
                            "text": "[screenshot已驱逐:仅用于上次决策,不持久化]",
                        })
                        continue
                new_content.append(item)
            if modified:
                msg["content"] = new_content
        if evicted > 0:
            self._dirty = True
            logger.debug(f"记忆驱逐{evicted}张截图(防止上下文膨胀)")
        return evicted

    def evict_browser_view_content(self) -> int:
        """LLM调用后驱逐浏览器页面快照内容,防止旧快照累积挤占上下文。

        浏览器页面快照(browser_view/navigate/restart的interactive_elements/ref_map/
        content)仅在当前LLM决策时需要,决策完成后应从记忆中移除。与evict_image_data
        同理:旧快照在页面操作(点击/滚动/导航)后已过期(ref漂移、元素变化),保留会导致
        LLM引用过期元素或误判页面状态。

        设计原则(参考9e1e5363提交节点): 页面快照只在当前会话上下文中临时存在,用于
        本次任务决策,不自动写入长期记忆。evict_image_data已处理截图(base64),本方法
        处理文本部分(interactive_elements/ref_map/content),两者协同实现快照全驱逐。

        策略: 将所有browser_view类tool消息的文本内容替换为压缩摘要(仅保留URL/标题/
        元素计数),复用_compress_tool_content的browser_view压缩逻辑。已是摘要的跳过。
        注: 本方法在evict_image_data之后调用,图片块已替换为文本标记,可安全将list
        content合并为string。

        Returns:
            被驱逐内容的消息数量(用于日志观测)
        """
        evicted = 0
        # 保留最近一条browser_view快照不驱逐: LLM在操作(点击/输入)后需查看最新页面
        # 状态(checked/selected等标记)进行结果验证,驱逐最新快照会迫使LLM重新browser_view
        # 或退化为console_exec查询(会话d1eb3b5c根因)。仅驱逐更早的过期快照。
        last_view_idx = -1
        for i in range(len(self.messages) - 1, -1, -1):
            msg = self.messages[i]
            if self.get_message_role(msg) != "tool":
                continue
            if msg.get("function_name", "") in _BROWSER_VIEW_TOOLS:
                last_view_idx = i
                break
        for i, msg in enumerate(self.messages):
            if self.get_message_role(msg) != "tool":
                continue
            fn = msg.get("function_name", "")
            if fn not in _BROWSER_VIEW_TOOLS:
                continue
            # 跳过最近一条快照: 保留完整内容供LLM验证操作结果
            if i == last_view_idx:
                continue
            content = msg.get("content", "")
            # 提取工具结果文本(兼容str和list两种content格式)
            # list格式: 第一个text项是工具结果JSON,后续是驱逐标记等(不参与压缩)
            if isinstance(content, list):
                text_item = next(
                    (i for i in content if isinstance(i, dict) and i.get("type") == "text"),
                    None,
                )
                text = text_item.get("text", "") if text_item else ""
            elif isinstance(content, str):
                text = content
            else:
                continue
            # 已是压缩摘要或空内容: 跳过(避免重复压缩)
            if not text or text.startswith("(compressed)"):
                continue
            # 压缩为轻量摘要(URL/标题/元素计数),替换原始内容
            msg["content"] = self._compress_tool_content(text, fn)
            evicted += 1
        if evicted > 0:
            self._dirty = True
            logger.debug(f"记忆驱逐{evicted}条浏览器快照内容(防止上下文膨胀)")
        return evicted

    @staticmethod
    def _extract_text_from_content(content: Any) -> str:
        """从消息content中统一提取文本，兼容str和list两种格式

        工具结果在Memory中有两种存储格式：
        - str: 纯文本内容（多数工具）
        - list: 多模态格式 [{"type":"text","text":"..."},{"type":"image_url",...}]
                （browser_view等带截图的工具）

        本方法统一提取文本部分，避免isinstance(content,str)检查跳过list格式，
        导致多模态工具结果不被截断/摘要（session_summary始终为空的根因）。
        """
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # 拼接所有text类型块的text字段，跳过image_url等非文本块
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    if text:
                        parts.append(text)
            return "\n".join(parts)
        return ""

    @staticmethod
    def _unpack_content_data(content: str) -> Optional[dict]:
        """解析工具返回的 content JSON，自动解包 data 子对象。

        工具返回统一格式 {"success":bool,"message":str,"data":{...}}，
        此方法解析后返回 data 子对象（如果存在且为 dict），否则返回整个 dict。
        解析失败或 content 非 JSON 时返回 None。
        """
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                inner = data.get("data")
                if isinstance(inner, dict):
                    return inner
                return data
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def _summarize_tool_operation(self, fn: str, content: Any) -> str:
        """从单个工具消息中提取操作摘要（用于压缩前累积到 session_summary）

        兼容多模态list格式：通过_extract_text_from_content统一提取文本后解析。
        """
        if fn in _BROWSER_VIEW_TOOLS:
            text = self._extract_text_from_content(content)
            if text:
                data = self._unpack_content_data(text)
                if data:
                    page_state = data.get("page_state", {})
                    url = page_state.get("url", "")
                    title = page_state.get("title", "")
                    desc = f"访问页面: {url}" + (f", {title}" if title else "")
                    return desc[:_COMPRESSION_SUMMARY_PER_OP]
            return f"浏览器查看: {fn}"
        if fn in _BROWSER_ACTION_TOOLS:
            return f"浏览器操作: {fn}"
        if fn in _BROWSER_DATA_TOOLS:
            # console_exec: 提取JS返回值摘要,便于会话进展回溯
            text = self._extract_text_from_content(content)
            if text:
                data = self._unpack_content_data(text)
                if data:
                    result = data.get("result", "")
                    if result is not None and str(result).strip():
                        return f"JS执行结果: {str(result)[:_COMPRESSION_SUMMARY_PER_OP]}"
            return f"浏览器JS执行: {fn}"
        if fn in _FILE_TOOLS:
            text = self._extract_text_from_content(content)
            if text:
                data = self._unpack_content_data(text)
                if data:
                    filepath = data.get("filepath", data.get("path", ""))
                    if filepath:
                        return f"文件操作: {filepath}"[:_COMPRESSION_SUMMARY_PER_OP]
            return f"文件操作: {fn}"
        if fn in _SHELL_TOOLS:
            text = self._extract_text_from_content(content)
            if text:
                data = self._unpack_content_data(text)
                if data:
                    console = data.get("console", [])
                    if isinstance(console, list) and console:
                        last = console[-1]
                        if isinstance(last, dict):
                            cmd = last.get("command", "")
                            if cmd:
                                return f"执行命令: {cmd}"[:_COMPRESSION_SUMMARY_PER_OP]
                    cmd = data.get("command", "")
                    if cmd:
                        return f"执行命令: {cmd}"[:_COMPRESSION_SUMMARY_PER_OP]
            return f"执行命令: {fn}"
        if fn in _SEARCH_TOOLS:
            return f"网络搜索: {fn}"
        # MCP工具搜索: 提取候选工具名到摘要,防止emergency_compact后重复搜索
        if fn in _MCP_SEARCH_TOOLS:
            text = self._extract_text_from_content(content)
            if text:
                candidates = extract_search_candidates(text)
                if candidates:
                    return f"MCP工具发现: {','.join(candidates[:3])}"[:_COMPRESSION_SUMMARY_PER_OP]
            return f"MCP工具搜索: {fn}"
        if fn.startswith(_MCP_TOOL_PREFIX):
            return f"MCP工具: {fn}"[:_COMPRESSION_SUMMARY_PER_OP]
        return f"工具调用: {fn}"

    def _build_compression_summary(self) -> str:
        """从新增消息中提取进展摘要（在压缩前调用）

        只遍历 last_summary_index 之后的新消息，避免重复摘要已压缩的旧消息。
        覆盖所有工具类型与assistant决策性文本，每个操作摘要控制在
        _COMPRESSION_SUMMARY_PER_OP 字符内，整体用 ' | ' 连接。
        assistant文本先strip并压缩空白，避免换行/空格污染摘要。
        """
        start = self.metrics.last_summary_index
        if start >= len(self.messages):
            return ""
        parts: List[str] = []
        for msg in self.messages[start:]:
            role = self.get_message_role(msg)
            if role == "tool":
                fn = msg.get("function_name", "")
                content = msg.get("content", "")
                snippet = self._summarize_tool_operation(fn, content)
                if snippet:
                    parts.append(snippet)
            elif role == "assistant":
                text = self._extract_text_from_content(msg.get("content", ""))
                if text and len(text.strip()) > 20:
                    clean = " ".join(text.split())[:80]
                    if clean:
                        parts.append(f"AI: {clean}")
            elif role == "user":
                # 步骤转移记录: 执行步骤用户消息含"当前步骤：{desc}",记录到摘要
                # 防止emergency_compact后session_summary丢失步骤叙事(会话6d4f313b根因)
                text = self._extract_text_from_content(msg.get("content", ""))
                if text and text.strip().startswith("你正在执行任务"):
                    m = _CURRENT_STEP_PATTERN.search(text)
                    if m:
                        step_desc = " ".join(m.group(1).split())[:60]
                        parts.append(f"→步骤: {step_desc}")
        return " | ".join(parts) if parts else ""

    def _append_to_session_summary(self, snippet: str) -> None:
        """将压缩前提取的摘要片段追加到会话进展摘要（累积式，永不压缩）

        超长时保留尾部（最新进展更重要），并同步更新 metrics.session_summary_chars。
        若新片段与上一轮摘要完全相同则跳过，避免重复堆积。
        """
        if not snippet:
            return
        if self.session_summary:
            last_round = self.session_summary.rsplit(" -> ", 1)[-1]
            if snippet == last_round:
                return
            self.session_summary += f" -> {snippet}"
        else:
            self.session_summary = snippet
        if len(self.session_summary) > _SESSION_SUMMARY_MAX:
            self.session_summary = self.session_summary[-_SESSION_SUMMARY_MAX:]
        self.metrics.session_summary_chars = len(self.session_summary)

    def extract_key_facts(self) -> List[KeyFact]:
        """提取关键事实 - 按分类配额保留，时间倒序

        简化策略(按分类配额替代权重评分):
        - 每个分类保留最新N条(按timestamp倒序)
        - 总数不超过_KEY_FACTS_MAX
        - 不用代码层权重决定信息重要性(交给LLM通过提示词判断)

        从历史消息中提取：
        - 用户原始需求（requirement）
        - 已访问URL + 页面标题（url/page_title）
        - 已创建/读取文件（file）
        - 已执行关键命令（cmd）
        - 关键决策（decision）
        - 失败的工具调用（error）
        - MCP工具调用记录（mcp_tool）

        去重策略: 基于content_hash去重,相同归一化内容不重复添加
        """
        now_ts = datetime.now().isoformat()
        # 按分类收集，每分类按时间倒序保留最新N条
        by_category: Dict[str, List[KeyFact]] = {}
        existing_hashes: set = set()

        # 保留已有key_facts(更新timestamp)
        for f in self.key_facts:
            cat = f.category
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(f)
            existing_hashes.add(f.content_hash)

        # 分类配额(不用权重,改为分类配额保证关键类别保留)
        category_quota = {
            "requirement": 2,   # 用户需求最多保留2条
            "error": 3,         # 错误信息最多保留3条(debugging需要)
            "file": 3,          # 文件路径最多保留3条
            "url": 2,           # URL最多保留2条
            "decision": 2,      # 关键决策最多保留2条
            "cmd": 2,           # 命令最多保留2条
            "page_title": 2,    # 页面标题最多保留2条
            "mcp_tool": 5,      # MCP工具最多保留5条(含已发现工具名,防止emergency_compact后重复搜索)
            "step_completed": 4,  # 已完成步骤最多保留4条(防emergency_compact后丢步重执,会话6d4f313b根因)
        }
        default_quota = 1

        def _add_by_category(category: str, content: str) -> None:
            """添加事实到分类集合(基于content_hash去重)"""
            new_fact = KeyFact(
                category=category, content=content, timestamp=now_ts,
            )
            if new_fact.content_hash in existing_hashes:
                # 已存在: 更新timestamp保持时效性
                for f in by_category.get(category, []):
                    if f.content_hash == new_fact.content_hash:
                        f.timestamp = now_ts
                        break
                return
            # 新事实
            if category not in by_category:
                by_category[category] = []
            by_category[category].append(new_fact)
            existing_hashes.add(new_fact.content_hash)

        for msg in self.messages:
            role = self.get_message_role(msg)

            if role == "user":
                content = self._extract_text_from_content(msg.get("content", ""))
                if content and len(content) > 10:
                    stripped = content.strip()
                    if stripped.startswith("你正在执行任务"):
                        # 执行步骤用户消息: 解析注入的"前序步骤完成情况",提取已完成步骤
                        # 根因: emergency_compact后这些用户消息被压缩,步骤完成状态丢失,
                        # LLM重新执行已完成的describe/导出/验证步骤(会话6d4f313b根因)。
                        # build_prior_steps_context 注入格式: "- 步骤{id}(已完成)：{result_brief}"
                        for m in _STEP_COMPLETED_PATTERN.finditer(content):
                            step_id = m.group(1)
                            step_brief = m.group(2).strip()[:80]
                            _add_by_category(
                                "step_completed", f"步骤{step_id}已完成: {step_brief}"
                            )
                        continue
                    if (stripped.startswith("任务已完成，你需要将最终结果")
                            or stripped.startswith("【系统")):
                        continue
                    _add_by_category("requirement", content[:200])

            elif role == "tool":
                fn = msg.get("function_name", "")
                content = msg.get("content", "")
                # 统一提取文本（兼容str和list两种content格式）
                text = self._extract_text_from_content(content)

                if fn in _BROWSER_VIEW_TOOLS:
                    if text:
                        data = self._unpack_content_data(text)
                        if data:
                            page_state = data.get("page_state", {})
                            url = page_state.get("url", "")
                            title = page_state.get("title", "")
                            if url:
                                _add_by_category("url", url)
                            if title:
                                _add_by_category("page_title", title)

                elif fn in _FILE_TOOLS:
                    if text:
                        data = self._unpack_content_data(text)
                        if data:
                            filepath = data.get("filepath", data.get("path", ""))
                            if filepath:
                                _add_by_category("file", filepath)

                elif fn in _SHELL_TOOLS:
                    if text:
                        data = self._unpack_content_data(text)
                        if data:
                            cmd = ""
                            console = data.get("console", [])
                            if isinstance(console, list) and console:
                                last_entry = console[-1]
                                if isinstance(last_entry, dict):
                                    cmd = last_entry.get("command", "")
                            if not cmd:
                                cmd = data.get("command", "")
                            if cmd and len(cmd) < 100:
                                _add_by_category("cmd", cmd)

                # MCP工具调用记录（动态注册，按 mcp_ 前缀识别）
                if fn.startswith(_MCP_TOOL_PREFIX):
                    _add_by_category("mcp_tool", fn)

                # MCP工具搜索历史保留: 解析mcp_tool_search返回的候选工具名列表
                # 防止emergency_compact后LLM忘记已发现的MCP工具,导致重复搜索+描述
                # 根因会话: 9697d98e (5次search/6次describe,其中2次为重复搜索)
                if fn in _MCP_SEARCH_TOOLS and text:
                    for name in extract_search_candidates(text):
                        _add_by_category("mcp_tool", name)

                # 失败的工具调用提取为 error（适用于所有工具类型）
                if text:
                    try:
                        raw = json.loads(text)
                        if isinstance(raw, dict) and raw.get("success") is False:
                            err_msg = raw.get("message", raw.get("error", ""))
                            if err_msg and isinstance(err_msg, str):
                                _add_by_category("error", err_msg[:120])
                    except (json.JSONDecodeError, TypeError):
                        pass

            elif role == "assistant":
                text = self._extract_text_from_content(msg.get("content", ""))
                if text and any(kw in text for kw in ["决定", "方案", "策略", "选择使用", "选择采用"]):
                    _add_by_category("decision", text[:100])

        # 合并各分类，每分类按timestamp倒序取前N条，总数不超过上限
        all_facts: List[KeyFact] = []
        for cat, items in by_category.items():
            quota = category_quota.get(cat, default_quota)
            # 按 timestamp 倒序(最新的在前), None 排最后
            items.sort(key=lambda f: f.timestamp or "", reverse=True)
            all_facts.extend(items[:quota])

        # 总数不超过上限
        self.key_facts = all_facts[:_KEY_FACTS_MAX]
        return self.key_facts

    def get_key_facts_text(self) -> str:
        """生成关键事实文本，用于注入系统提示（含时间戳）"""
        if not self.key_facts:
            return ""
        lines = []
        for fact in self.key_facts:
            ts = f" ({fact.timestamp[:19]})" if fact.timestamp else ""
            lines.append(f"  [{fact.category}]{ts} {fact.content}")
        return "之前操作的关键记录:\n" + "\n".join(lines)

    def get_summary_for_injection(self) -> str:
        """生成可注入系统提示的摘要（Hermes Agent策展式记忆核心方法）

        注入优先级: session_summary（累积叙事，防失忆）> key_facts（结构化事实）> 用户原始需求
        """
        parts = []
        if self.session_summary:
            summary = self.session_summary
            if len(summary) > _SESSION_SUMMARY_INJECT_MAX:
                summary = summary[-_SESSION_SUMMARY_INJECT_MAX:]
            parts.append(f"[会话进展摘要]\n{summary}")

        facts_text = self.get_key_facts_text()
        if facts_text:
            parts.append(facts_text)

        user_msgs = [m for m in self.messages if self.get_message_role(m) == "user"]
        if user_msgs:
            first_user = self._extract_text_from_content(user_msgs[0].get("content", ""))
            if first_user and len(first_user) > 10:
                parts.append(f"用户原始需求: {first_user[:300]}")

        return "\n".join(parts) if parts else ""

    @staticmethod
    def _compress_tool_content(content: Any, fn: str) -> str:
        """统一工具内容压缩 — 按 tool 类型选择策略，JSON 感知

        Phase E简化: 合并原 _compress_browser_view_content/_compress_search_content/
        _compress_shell_content/_compress_file_content 四个分工具方法为一个统一入口。
        保留 JSON 感知能力(browser/shell/file提取结构化字段), 其他工具统一截断。

        兼容多模态list格式：通过_extract_text_from_content统一提取文本后压缩。
        兼容ToolResult包装格式：通过_unpack_content_data解包data子对象（与_summarize_tool_operation一致）。
        """
        # 统一提取文本（兼容str和list两种content格式）
        text = Memory._extract_text_from_content(content)
        if not text:
            return f"({fn} output removed)"
        content = text  # 后续基于提取的文本压缩

        # 兼容ToolResult包装格式：解包data子对象后重新序列化
        # 使browser_view/shell/file分支能直接访问page_state/console/filepath等字段
        unpacked = Memory._unpack_content_data(content)
        if unpacked is not None:
            content = json.dumps(unpacked, ensure_ascii=False)

        # browser_view: 提取页面状态摘要(url/title/元素数/截图标记)
        if fn in _BROWSER_VIEW_TOOLS:
            if len(content) <= _BROWSER_CONTENT_THRESHOLD:
                return content
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    parts = []
                    if "page_state" in data:
                        state = data["page_state"]
                        parts.append(f"[page: {state.get('url', '?')}, title: {state.get('title', '?')}]")
                        if state.get("hasBlockingElement"):
                            parts.append("[has blocking element]")
                    if "interactive_elements" in data:
                        elements = data["interactive_elements"]
                        if isinstance(elements, list):
                            parts.append(f"[{len(elements)} interactive elements]")
                    if data.get("screenshot"):
                        # 截图base64不入压缩文本：已通过多模态image_url块传递，此处仅留标记
                        parts.append("[screenshot: attached]")
                    if parts:
                        return "(compressed) " + " ".join(parts)
            except (json.JSONDecodeError, TypeError):
                pass
            return "(browser content removed for context compression)"

        # browser_console_exec: 保留JS返回值(result字段),仅截断过长结果
        # 与click等动作工具不同,console_exec返回LLM需要读取的数据,
        # 压缩为"executed"会丢失返回值(会话e3f0762b根因)
        if fn in _BROWSER_DATA_TOOLS:
            if len(content) <= _TOOL_RESULT_MAX_LENGTH:
                return content
            return content[:_TOOL_RESULT_MAX_LENGTH] + "\n...(console_exec result truncated)"

        # browser_action: 仅保留操作标记
        if fn in _BROWSER_ACTION_TOOLS:
            return f"({fn} executed)"

        # shell: JSON感知去重(仅保留最后一条console) + 截断
        # 截断标记注明原始长度并引导LLM改用文件方式,避免反复读取(会话ac5503b3根因)
        if fn in _SHELL_TOOLS:
            try:
                data = json.loads(content)
                if isinstance(data, dict) and "console" in data:
                    console = data["console"]
                    if isinstance(console, list) and console:
                        last_entry = console[-1]
                        if isinstance(last_entry, dict):
                            output = last_entry.get("output", "")
                            command = last_entry.get("command", "")
                            if len(output) > _SHELL_OUTPUT_KEEP:
                                output = (
                                    output[:_SHELL_OUTPUT_KEEP]
                                    + f"\n...(shell output truncated, original {len(output)} chars, "
                                    "this is context compression not tool error, "
                                    "write result to file then read_file short summary for full content)"
                                )
                            return json.dumps(
                                {"console": [{"command": command, "output": output}]},
                                ensure_ascii=False,
                            )
            except (json.JSONDecodeError, TypeError):
                pass
            if len(content) <= _SHELL_OUTPUT_KEEP:
                return content
            return (
                content[:_SHELL_OUTPUT_KEEP]
                + f"\n...(shell output truncated, original {len(content)} chars, "
                "this is context compression not tool error, "
                "write result to file then read_file short summary for full content)"
            )

        # file: 提取文件路径 + 截断
        # 截断标记注明原始长度并引导LLM改用脚本方式,避免反复读取(会话5acf5aa2根因:
        # file_content_keep=400截断后标记不明确,LLM误判为工具错误反复拆分文件读取)
        if fn in _FILE_TOOLS:
            if len(content) <= _FILE_CONTENT_KEEP:
                return content
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    filepath = data.get("filepath", data.get("path", ""))
                    if filepath:
                        return (
                            f"(file: {filepath}, content truncated, original {len(content)} chars, "
                            "this is context compression not tool error, "
                            "use shell_execute+python to read full file and generate deliverable)"
                        )
            except (json.JSONDecodeError, TypeError):
                pass
            return (
                content[:_FILE_CONTENT_KEEP]
                + f"\n...(file content truncated, original {len(content)} chars, "
                "this is context compression not tool error, "
                "use shell_execute+python to read full file and generate deliverable)"
            )

        # search: 截断
        if fn in _SEARCH_TOOLS:
            if len(content) <= _SEARCH_RESULT_THRESHOLD:
                return content
            return content[:_SEARCH_RESULT_KEEP] + "\n...(search results truncated)"

        # 其他工具: 通用截断
        if len(content) <= _TOOL_RESULT_MAX_LENGTH:
            return content
        return content[:_TOOL_RESULT_MAX_LENGTH] + "\n...(truncated)"

    def _merge_consecutive_tool_messages(self) -> None:
        """合并连续的tool消息,减少消息数量(仅合并非并行的连续tool消息)

        修复: 原实现仅按function_name合并,会丢失同一assistant(tool_calls)并行调用的
        tool消息结果(如LLM并行调用2个search_web时,第二条结果会被第一条覆盖)。
        丢失后sanitize_tool_message_pairing补全为"(missing)"错误消息,导致LLM反复重试。

        新策略: 跟踪每个tool消息所属的assistant(tool_calls),仅合并不同assistant轮次
        的连续同function_name tool消息,同一assistant的并行tool消息全部保留。

        合并场景示例(合法合并):
            assistant(tool_calls=[shell_exec_1]) → tool(shell_exec_1)
            assistant(tool_calls=[shell_exec_2]) → tool(shell_exec_2)  ← 紧邻上一条tool
            注: 实际上中间隔着assistant,不会触发合并,此场景几乎不发生

        不合并场景示例(并行调用保护):
            assistant(tool_calls=[search_web_A, search_web_B])
              → tool(search_web_A)  ← 与下一条同fn但属同一assistant,不合并
              → tool(search_web_B)
        """
        if len(self.messages) <= _PROTECT_HEAD_COUNT + _PROTECT_TAIL_COUNT:
            return

        # 构建 tool_call_id → assistant_index 映射,用于判断tool消息所属的assistant轮次
        tool_call_to_assistant: Dict[str, int] = {}
        for i, msg in enumerate(self.messages):
            if self.get_message_role(msg) == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id")
                    if tc_id:
                        tool_call_to_assistant[tc_id] = i

        merged: List[Dict[str, Any]] = []
        for msg in self.messages:
            if self.get_message_role(msg) != "tool":
                merged.append(msg)
                continue

            fn = msg.get("function_name", "")
            tc_id = msg.get("tool_call_id", "")
            current_assistant_idx = tool_call_to_assistant.get(tc_id, -1)

            if (
                merged
                and self.get_message_role(merged[-1]) == "tool"
                and merged[-1].get("function_name") == fn
            ):
                # 检查是否属于同一assistant(并行调用),若是则不合并
                prev_tc_id = merged[-1].get("tool_call_id", "")
                prev_assistant_idx = tool_call_to_assistant.get(prev_tc_id, -1)
                if prev_assistant_idx == -1 or current_assistant_idx == -1:
                    # 无法确定归属(历史数据异常): 保守不合并,保留两条
                    merged.append(msg)
                elif prev_assistant_idx != current_assistant_idx:
                    # 不同assistant轮次: 合并(保留最后一条完整内容)
                    merged[-1] = msg
                else:
                    # 同一assistant的并行调用: 不合并,保留两条
                    merged.append(msg)
            else:
                merged.append(msg)

        if len(merged) < len(self.messages):
            reduced = len(self.messages) - len(merged)
            self.messages = merged
            self.metrics.message_count = len(self.messages)
            logger.debug(f"合并连续tool消息: 减少{reduced}条, 剩余{len(self.messages)}条")

    @staticmethod
    def _should_preserve_reasoning(message: Dict[str, Any]) -> bool:
        """判断assistant消息的reasoning_content是否需要在压缩时保留

        DeepSeek V4思考模式约束：当assistant消息包含tool_calls时，
        后续API调用必须回传该消息的reasoning_content字段，否则返回400错误。
        因此压缩时仅删除不含tool_calls的assistant消息的reasoning_content。
        """
        return message.get("role") == "assistant" and bool(message.get("tool_calls"))

    def _prepare_compression(self) -> None:
        """压缩前置准备 — 提取关键事实 + 追加会话摘要(P4-3公共逻辑提取)

        compact与emergency_compact的公共前置逻辑:
        1. 追加压缩摘要到session_summary(防失忆,压缩前保留进展)
        2. 提取关键事实到key_facts(压缩后注入系统提示)

        提取为独立方法避免逻辑重复,确保两层压缩的关键事实/摘要一致性和维护性。
        """
        self._append_to_session_summary(self._build_compression_summary())
        self.extract_key_facts()

    def compact(self) -> None:
        """常规压缩 — 统一工具内容截断 + 连续tool消息合并

        Phase E简化: 使用 _compress_tool_content 统一方法替代4个分工具压缩方法。
        P4-3: 前置逻辑(摘要追加+关键事实提取)提取到_prepare_compression()复用。

        注: 浏览器页面快照(browser_view)的驱逐由 evict_browser_view_content 在
        每次LLM决策后立即执行,不依赖compact()。compact()仅处理其他工具结果的
        常规压缩与连续tool消息合并。
        """
        self._prepare_compression()
        for message in self.messages:
            if self.get_message_role(message) == "tool":
                fn = message.get("function_name", "")
                content = message.get("content", "")
                message["content"] = self._compress_tool_content(content, fn)

            if "reasoning_content" in message and not self._should_preserve_reasoning(message):
                del message["reasoning_content"]  # 非工具调用的reasoning_content可安全删除以节省上下文

        self._merge_consecutive_tool_messages()
        self.metrics.last_summary_index = len(self.messages)
        self.metrics.compact_count += 1
        self.metrics.last_compression_level = CompressionLevel.NORMAL
        self._dirty = True
        logger.debug(f"常规压缩完成, 消息数: {len(self.messages)}")

    def emergency_compact(self) -> None:
        """紧急压缩 — 保留系统提示 + 会话进展摘要 + 关键事实 + 最近N条消息

        Phase E简化: 合并原 aggressive_compact/minimal_compact 为单一紧急压缩层。
        当常规压缩(compact)无法有效降低上下文压力时触发，通过保留 head+summary+
        key_facts+tail 的精简结构大幅减少消息数，同时保留关键上下文防止失忆。

        边界对齐: tail起始位置向前扩展直到tail[0]非tool消息,确保tool消息配对完整。
        根因: head+tail拼接时,若tail[0]是tool消息,其配对的assistant(tool_calls)
        被截断到head之外,中间隔着summary_msg(system),导致tool消息孤立,
        触发OpenAI API 400错误 "Messages with role 'tool' must be a response..."。
        """
        if len(self.messages) <= _PROTECT_HEAD_COUNT + _PROTECT_TAIL_COUNT:
            return

        self._prepare_compression()

        head = self.messages[:_PROTECT_HEAD_COUNT]
        # 边界对齐: 向前扩展tail_start直到tail[0]不是tool消息
        # 避免孤立的tool消息触发OpenAI API 400错误
        tail_start = max(_PROTECT_HEAD_COUNT, len(self.messages) - _PROTECT_TAIL_COUNT)
        while tail_start > _PROTECT_HEAD_COUNT and self.get_message_role(self.messages[tail_start]) == "tool":
            tail_start -= 1
        tail = self.messages[tail_start:]

        # 构造压缩摘要: 会话进展 + 关键事实 + 用户原始需求
        summary_parts = ["[上下文紧急压缩]"]

        # 提取用户原始需求(从所有消息中找第一条有效user消息)
        for msg in self.messages:
            if self.get_message_role(msg) == "user":
                content = self._extract_text_from_content(msg.get("content", ""))
                if content and len(content) > 10:
                    summary_parts.append(f"用户需求: {content[:500]}")
                    break

        if self.session_summary:
            summary_parts.append(f"会话进展摘要:\n{self.session_summary[-_SESSION_SUMMARY_INJECT_MAX:]}")
        facts_text = self.get_key_facts_text()
        if facts_text:
            summary_parts.append(facts_text)
        summary_msg = {
            "role": "system",
            "content": "\n".join(summary_parts),
        }

        self.messages = head + [summary_msg] + tail
        # 兜底防线: 清理任何残余的配对破坏(防御性编程,应对未来未知破坏)
        self.sanitize_tool_message_pairing()
        self.metrics.last_summary_index = len(self.messages)
        self.metrics.emergency_count += 1
        self.metrics.last_compression_level = CompressionLevel.EMERGENCY
        self._dirty = True
        logger.debug(f"紧急压缩完成, 消息数: {len(self.messages)}")

    def sanitize_tool_message_pairing(self) -> int:
        """修复tool消息与assistant(tool_calls)的配对完整性

        OpenAI API双向约束:
        1. role=tool的消息必须紧跟包含匹配tool_call_id的assistant(tool_calls)消息
           (中间不允许其他角色消息,如system/user)
        2. assistant(tool_calls)消息后续必须有tool消息响应每个tool_call_id

        紧急压缩/roll_back等操作可能破坏配对,本方法作为兜底防线:
        - 删除孤立的tool消息(无配对assistant或被其他角色消息隔开)
        - 补全缺失的tool消息(为未响应的tool_call_id生成错误tool消息,
          保留LLM工具调用意图,LLM可基于错误响应决定重试或切换策略)

        Returns:
            修复的消息条数(删除的孤立tool + 补全的缺失tool)
        """
        if not self.messages:
            return 0

        sanitized: List[Dict[str, Any]] = []
        # 当前待响应的assistant(tool_calls)信息
        pending_ids: set = set()
        pending_assistant_idx: int = -1  # 待响应assistant在sanitized中的索引

        def _fill_missing_tool_messages() -> None:
            """补全未响应的tool_call_id: 生成错误tool消息插入assistant之后

            相比降级(删除tool_calls字段),补全保留了LLM的工具调用意图,
            LLM可看到错误响应并决定重试或切换策略。
            同步清理reasoning_content(DeepSeek V4要求tool_calls场景才传reasoning_content)。
            """
            nonlocal pending_ids, pending_assistant_idx
            if pending_ids and pending_assistant_idx >= 0:
                # 删除reasoning_content避免补全tool消息后仍触发DeepSeek V4 400错误
                assistant = sanitized[pending_assistant_idx]
                assistant.pop("reasoning_content", None)
                for missing_id in sorted(pending_ids):
                    sanitized.append({
                        "role": "tool",
                        "tool_call_id": missing_id,
                        "function_name": "(missing)",
                        "content": "错误: 工具调用结果缺失(可能因记忆压缩丢失),请基于已有信息继续或重试该工具。",
                    })
                logger.warning(
                    f"补全缺失的tool消息: missing_ids={pending_ids}"
                )
            pending_ids = set()
            pending_assistant_idx = -1

        removed = 0

        for msg in self.messages:
            role = self.get_message_role(msg)
            if role == "assistant" and msg.get("tool_calls"):
                # 新assistant(tool_calls): 先处理上一个未响应的
                if pending_ids:
                    _fill_missing_tool_messages()
                    removed += 1

                pending_ids = {
                    tc.get("id") for tc in msg["tool_calls"] if tc.get("id")
                }
                pending_assistant_idx = len(sanitized)
                sanitized.append(dict(msg))  # 副本便于后续修改
            elif role == "tool":
                tool_call_id = msg.get("tool_call_id")
                if pending_ids and tool_call_id in pending_ids:
                    # 配对成功: 消费该id(同一id不可重复使用)
                    pending_ids.discard(tool_call_id)
                    sanitized.append(msg)
                    if not pending_ids:
                        pending_ids = set()
                        pending_assistant_idx = -1
                else:
                    # 孤立tool消息(无配对assistant或id不匹配): 删除
                    removed += 1
                    logger.warning(
                        f"清理孤立tool消息: tool_call_id={tool_call_id}, "
                        f"function={msg.get('function_name', '?')}"
                    )
            else:
                # 其他角色消息(user/system/assistant无tool_calls):
                # tool消息不能跨其他消息配对,先处理上一个未响应的assistant
                if pending_ids:
                    _fill_missing_tool_messages()
                    removed += 1
                sanitized.append(msg)

        # 循环结束: 处理最后一个未响应的assistant
        if pending_ids:
            _fill_missing_tool_messages()
            removed += 1

        if removed > 0:
            self.messages = sanitized
            self.metrics.message_count = len(self.messages)
            self._dirty = True
            logger.info(f"消息历史修复: 修复{removed}处配对破坏, 剩余{len(self.messages)}条")
        return removed

    def get_compression_level(self) -> CompressionLevel:
        """根据当前消息数量判断应使用的压缩级别(两层)"""
        count = len(self.messages)
        if count < _MAX_MESSAGES_SOFT:
            return CompressionLevel.NONE
        elif count < _MAX_MESSAGES_HARD:
            return CompressionLevel.NORMAL
        else:
            return CompressionLevel.EMERGENCY

    def auto_compact(self) -> CompressionLevel:
        """自动选择压缩级别并执行压缩(两层)"""
        level = self.get_compression_level()
        if level == CompressionLevel.NONE:
            if self.should_compress(threshold=0.5):
                self.compact()
                return CompressionLevel.NORMAL
            return CompressionLevel.NONE
        elif level == CompressionLevel.NORMAL:
            self.compact()
            return CompressionLevel.NORMAL
        else:
            self.emergency_compact()
            return CompressionLevel.EMERGENCY
