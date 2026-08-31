#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""任务类型分类器(Batch 39 / 方向2: 工具调用预算精细化, Batch 40 / 方向4: LLM 增强)

根据用户消息/步骤描述关键词,识别任务类型,供 ToolBudgetTracker.adjust_for_task_type
动态调整高风险工具的会话级预算。

Batch 40 / 方向4 增强(3层降级):
- Layer 1: 关键词快路径(高置信度, < 1ms) — 直接返回
- Layer 2: LLM 1-token 分类(中置信度, 200-500ms) — 关键词未命中时调用
- Layer 3: general 降级 — LLM 超时/失败时兜底

设计原则:
- 纯函数模块,无状态,便于单元测试
- 复用 react.py _RESEARCH_KEYWORDS 语义,保持关键词一致性
- 分类优先级: research > data_analysis > browser > general(互斥,首匹配优先)
- 保守分类: 仅高置信度关键词命中才分类,避免误调整预算
- LLM 增强可选: 未传入 LLM 时降级到纯关键词分类(向后兼容)

分类策略:
- research: 深度搜索/深度研究/趋势研究/调研 等关键词 → deep_research 预算上调
- data_analysis: 出入库/数据分析/统计/报表 等关键词 → search_web 预算上调
- browser: 浏览器操作/网页抓取/页面截图 等关键词 → browser_navigate 预算上调
- general: 无高置信度关键词命中,保持默认预算

注意: "深度分析"不纳入研究类关键词——该词在数据分析场景(如"出入库深度分析")
中高频出现,纳入 research 会与 data_analysis 优先级冲突导致误分类。
研究意图由"深度研究/深度搜索/深度调研"等更具体的词承载。
"""
import asyncio
import hashlib
import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.external.llm import LLM

logger = logging.getLogger(__name__)

# 任务类型常量(与 budget_tracker._TASK_TYPE_BUDGET_ADJUSTMENTS key 对齐)
TASK_TYPE_RESEARCH = "research"
TASK_TYPE_DATA_ANALYSIS = "data_analysis"
TASK_TYPE_BROWSER = "browser"
TASK_TYPE_GENERAL = "general"

# 研究类关键词(与 react.py _RESEARCH_KEYWORDS 语义对齐,取子集避免过度召回)
# 注: "深度分析"不纳入——该词在数据分析场景高频出现,会导致"出入库深度分析"误分类
_RESEARCH_KEYWORDS = (
    "深度研究", "深度搜索", "深度调研", "深入研究",
    "综合研究", "趋势研究", "调研", "research", "deep search",
)

# 数据分析类关键词(覆盖出入库/经营分析/统计报表等业务场景)
# 注: "深度分析"纳入此处——"分析"语义对齐 data_analysis,避免与 research 优先级冲突
_DATA_ANALYSIS_KEYWORDS = (
    "出入库", "数据分析", "深度分析", "经营分析", "统计分析", "数据统计",
    "报表", "库存分析", "销售分析", "财务分析", "生产分析",
    "data analysis", "inventory", "statistics",
)

# 浏览器密集类关键词(覆盖网页抓取/页面操作等场景)
_BROWSER_KEYWORDS = (
    "网页抓取", "页面截图", "网站操作", "浏览器操作", "网页采集",
    "web scraping", "page screenshot", "browser automation",
)

# Batch 40 / 方向4: LLM 分类 prompt(极简,1-token 输出)
_LLM_CLASSIFY_PROMPT = """将以下用户消息分类为任务类型,仅输出一个英文单词(不要其他内容):
- research: 深度搜索/研究/调研/趋势分析/搜索最新信息
- data_analysis: 数据分析/统计/报表/出入库/经营分析/库存分析
- browser: 网页操作/抓取/截图/网站交互
- general: 以上都不符合

用户消息: {message}

分类(仅输出一个英文单词):"""

# LLM 分类超时(秒),超时降级到 general
_LLM_CLASSIFY_TIMEOUT = 5.0

# LLM 分类结果缓存 TTL(秒),相同消息不重复调用
_LLM_CACHE_TTL = 3600


def classify_task_type(text: str) -> str:
    """根据文本内容分类任务类型(Batch 39 / 方向2)

    分类优先级: research > data_analysis > browser > general
    互斥分类: 首匹配优先,如"深度研究出入库数据"分类为 research(优先级更高)

    Args:
        text: 用户消息或步骤描述文本

    Returns:
        任务类型字符串(research/data_analysis/browser/general)
    """
    if not text:
        return TASK_TYPE_GENERAL
    text_lower = text.lower()
    # 优先级: research > data_analysis > browser
    # 研究类优先: "深度研究出入库"应分类为 research(需更多 deep_research 预算)
    for kw in _RESEARCH_KEYWORDS:
        if kw in text_lower:
            return TASK_TYPE_RESEARCH
    for kw in _DATA_ANALYSIS_KEYWORDS:
        if kw in text_lower:
            return TASK_TYPE_DATA_ANALYSIS
    for kw in _BROWSER_KEYWORDS:
        if kw in text_lower:
            return TASK_TYPE_BROWSER
    return TASK_TYPE_GENERAL


async def classify_with_llm(
        text: str,
        llm: Optional["LLM"] = None,
        redis_client: Optional[object] = None,
) -> str:
    """3层降级分类: 关键词 → LLM → general(Batch 40 / 方向4)

    Layer 1: 关键词快路径 — 高置信度命中直接返回(< 1ms)
    Layer 2: LLM 1-token 分类 — 关键词未命中时调用 LLM(200-500ms)
    Layer 3: general 降级 — LLM 未传入/超时/失败时兜底

    Args:
        text: 用户消息文本
        llm: 可选的 LLM 实例,None 时跳过 Layer 2
        redis_client: 可选的 Redis 客户端,用于缓存 LLM 分类结果

    Returns:
        任务类型字符串
    """
    # Layer 1: 关键词快路径
    result = classify_task_type(text)
    if result != TASK_TYPE_GENERAL:
        return result

    # 无 LLM 时直接降级
    if llm is None:
        return TASK_TYPE_GENERAL

    # Layer 2: LLM 分类(带缓存)
    cache_key = f"task_type_llm:{hashlib.md5(text.encode()).hexdigest()}"
    cached = _get_cached_result(redis_client, cache_key)
    if cached is not None:
        logger.debug(f"LLM 分类缓存命中: {cached}")
        return cached

    try:
        prompt = _LLM_CLASSIFY_PROMPT.format(message=text[:500])
        # P4-5修复: LLM Protocol 无 chat 方法,改用 invoke(tools=None 纯文本,无JSON格式化)
        # planner_llm 为 thinking=disabled 轻量LLM,单次调用预期 < 2s
        # asyncio.wait_for 超时降级,防止网络抖动阻塞主流程
        response = await asyncio.wait_for(
            llm.invoke(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                response_format=None,
            ),
            timeout=_LLM_CLASSIFY_TIMEOUT,
        )
        # 解析 1-token 响应
        raw = (response.get("content") or "").strip().lower()
        if "research" in raw:
            result = TASK_TYPE_RESEARCH
        elif "data" in raw or "analysis" in raw:
            result = TASK_TYPE_DATA_ANALYSIS
        elif "browser" in raw:
            result = TASK_TYPE_BROWSER
        else:
            result = TASK_TYPE_GENERAL

        # 缓存结果
        _set_cached_result(redis_client, cache_key, result)
        logger.info(f"LLM 任务分类: text='{text[:50]}', result={result}")
        return result

    except Exception as e:
        logger.debug(f"LLM 分类失败(降级到 general): {e}")
        return TASK_TYPE_GENERAL


def _get_cached_result(redis_client: Optional[object], key: str) -> Optional[str]:
    """从 Redis 获取缓存的分类结果(降级安全)"""
    if redis_client is None:
        return None
    try:
        # 同步调用(Redis 客户端可能是异步的,这里做兼容)
        if asyncio.iscoroutinefunction(getattr(redis_client, "get", None)):
            # 异步 Redis 客户端: 在已有事件循环中无法直接 await,返回 None 降级
            return None
        return redis_client.get(key)
    except Exception:
        return None


def _set_cached_result(redis_client: Optional[object], key: str, value: str) -> None:
    """缓存分类结果到 Redis(降级安全)"""
    if redis_client is None:
        return
    try:
        if asyncio.iscoroutinefunction(getattr(redis_client, "set", None)):
            return
        redis_client.set(key, value, ex=_LLM_CACHE_TTL)
    except Exception as e:
        # 缓存写入失败不影响分类主流程,仅记录warning(降级安全)
        logger.warning(f"任务类型缓存写入失败,降级跳过: key={key}, error={e}")


def classify_from_message(message_text: str, plan_steps: Optional[list] = None) -> str:
    """从用户消息和计划步骤综合分类任务类型

    优先基于用户消息分类(反映用户核心意图),消息无法分类时扫描计划步骤描述。
    扫描计划步骤时取首个非 general 分类(计划步骤通常按执行顺序排列,首个分类
    代表主要任务类型)。

    Args:
        message_text: 用户消息文本
        plan_steps: 计划步骤列表(可选),每项需有 description 属性或为字符串

    Returns:
        任务类型字符串
    """
    # 1.优先基于用户消息分类
    task_type = classify_task_type(message_text)
    if task_type != TASK_TYPE_GENERAL:
        return task_type
    # 2.消息无法分类时扫描计划步骤
    if plan_steps:
        for step in plan_steps:
            desc = step.description if hasattr(step, "description") else str(step)
            step_type = classify_task_type(desc)
            if step_type != TASK_TYPE_GENERAL:
                return step_type
    return TASK_TYPE_GENERAL
