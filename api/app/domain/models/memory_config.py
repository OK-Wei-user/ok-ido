#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : memory_config.py
记忆系统统一配置 - 管理压缩/截断/KeyFact/交付物等阈值

将散落在 memory.py / base.py / planner_react.py 的 30+ 硬编码阈值
统一为单一配置类，支持运行时调优，避免魔法数字散落。

P3-8 自动缩放: 所有绝对值根据 context_window 自动缩放,
切换模型只需修改 config.yaml 的 context_window,无需手动调整阈值。
"""
import logging
import os
from dataclasses import dataclass, field
from typing import FrozenSet

import yaml

logger = logging.getLogger(__name__)

# P3-8 自动缩放基准上下文窗口: P3优化的参考值,所有绝对值基于此校准
# 切换模型时,create_memory_config() 按 context_window / 128000 比例缩放绝对值
_REFERENCE_CONTEXT_WINDOW = 128000

# P4-1 配置预设: 三档百分比阈值预设,覆盖大多数调优场景(借鉴TRAE Work CN轻量化理念)
# 日常运维只需选择 profile,无需逐项调整3个百分比阈值
# - balanced(默认): 平衡压缩频率与信息保留,适用于通用智能体场景
# - lightweight: 提高阈值减少压缩,适用于简单对话场景(省计算,被动压缩为主)
# - heavy: 降低阈值提前压缩,适用于复杂多步骤任务场景(保护上下文优先)
_PROFILE_THRESHOLDS = {
    "balanced":   (0.65, 0.75, 0.88),  # (proactive, reactive, critical)
    "lightweight": (0.75, 0.82, 0.90),  # 简单场景: 延迟压缩,减少compact调用
    "heavy":       (0.55, 0.68, 0.85),  # 复杂任务: 提前压缩,保护长上下文
}


@dataclass(frozen=True)
class MemoryConfig:
    """记忆系统配置 - 统一管理所有压缩/截断阈值

    设计原则:
    - 容量管控留在代码层(基础设施职责)
    - 信息重要性判断交给提示词(LLM职责)
    - 阈值调优回到配置层(运维职责)

    自动缩放(P3-8):
    - 百分比阈值(proactive/reactive/critical)天然适配任意context_window,无需缩放
    - 绝对值(消息容量/截断/保留/摘要)由create_memory_config()按比例缩放
    - 切换模型只需修改config.yaml的context_window,DEFAULT_MEMORY_CONFIG自动适配
    """

    # === 消息容量阈值 ===
    # P3-4: 多步骤任务(如数据分析)消息数增长快,原40/60过小导致频繁压缩丢失上下文
    max_messages_soft: int = 60        # 常规压缩触发阈值(原40,P3-4提升至60)
    max_messages_hard: int = 90        # 紧急压缩触发阈值(原60,P3-4提升至90)
    protect_head_count: int = 3        # 紧急压缩时保护头部消息数(原2,P3-4提升至3)
    protect_tail_count: int = 8        # 紧急压缩时保护尾部消息数(原4,P3-4提升至8,保留更多近期上下文)

    # === 工具结果截断(加入memory前) ===
    # P3-3: 配合context_window提升(128K),入库前截断阈值同步提升,避免过度截断丢失关键数据
    tool_result_max_length: int = 12000      # 通用工具结果截断(原8000,P3-3提升至12000)
    # 浏览器工具结果截断: 需容纳 interactive_elements + ref_map + page_state 等操作必需字段。
    # 原值3000过小,element-plus等组件库页面常有100+交互元素+50+ref引用,
    # 截断后丢弃ref_map/interactive_elements导致LLM判定"empty state"无法操作(会话9309bba7根因)。
    # _truncate_content_internal会按优先级保留操作必需字段,仅截断content/accessibility_tree等大体积辅助字段。
    browser_result_max_length: int = 12000   # 浏览器工具结果截断(保持不变,已足够)
    file_result_max_length: int = 10000      # 文件工具结果截断(原5000,P3-3提升至10000)
    shell_result_max_length: int = 6000      # Shell工具结果截断(原3000,P3-3提升至6000)
    search_result_max_length: int = 4000     # 搜索工具结果截断(保持不变)
    deep_research_result_max_length: int = 6000  # 深度研究结果截断(保持不变)

    # === 压缩阶段分工具阈值(常规压缩时使用) ===
    # P3-2: 压缩保留值过小是截断问题根因,shell/file仅500字符导致关键数据丢失
    # 提升保留值确保compact()后仍保留足够信息供LLM继续多步骤任务
    browser_content_threshold: int = 500     # 浏览器内容压缩阈值(原200,P3-2提升至500)
    search_result_threshold: int = 1000      # 搜索结果压缩阈值(保持不变)
    search_result_keep: int = 1000           # 搜索结果保留长度(原500,P3-2提升至1000)
    shell_output_keep: int = 2000            # Shell输出压缩保留长度(原500,P3-2提升至2000,4倍保留脚本结果)
    file_content_keep: int = 2000            # 文件内容压缩保留长度(原500,P3-2提升至2000,4倍保留文件数据)

    # === KeyFact ===
    key_facts_max: int = 10                  # KeyFact最大保留数

    # === 会话摘要 ===
    # P3-6: 会话摘要容量提升,确保emergency_compact后摘要信息更完整
    session_summary_max: int = 5000          # 会话进展摘要最大字符数(原3000,P3-6提升至5000)
    session_summary_inject_max: int = 2000   # 注入系统提示时的截断长度(原1200,P3-6提升至2000)
    compression_summary_per_op: int = 200    # 单个工具操作摘要最大字符数(原120,P3-6提升至200)

    # === Token压力预测 ===
    # P3-5: 配合context_window提升(128K),压缩阈值稍作延迟,给多步骤任务更多运行空间
    # 百分比阈值天然适配任意context_window,无需缩放:
    # context_window=128K时proactive在83K触发;context_window=32K时proactive在20.8K触发
    proactive_compress_threshold: float = 0.65   # 65% 主动压缩阈值(原0.6,P3-5延迟)
    reactive_compress_threshold: float = 0.75    # 75% 被动压缩阈值(原0.7,P3-5延迟)
    critical_threshold: float = 0.88             # 88% 危险阈值(原0.85,P3-5延迟),触发紧急压缩
    high_pressure_truncate_max: int = 4000       # 高压力下工具结果预截断阈值(原2000,P3-7提升至4000)

    # === 交付物智能选择 ===
    # F10-8: 以下两个字段已迁移至 FilePresentationConfig(app_config.py),实现交付物过滤规则单一数据源。
    # 此处保留是为了向后兼容(planner_react.py模块级常量_CFG仍引用),新代码应使用FilePresentationConfig。
    max_deliverable_files: int = 20              # 交付物最大文件数(已迁移至FilePresentationConfig)
    excluded_extensions: FrozenSet[str] = field(
        default_factory=lambda: frozenset({
            ".tmp", ".temp", ".bak", ".log", ".swp", ".cache",
            ".pyc", ".class", ".o", ".obj",
        })
    )  # 临时文件扩展名(已迁移至FilePresentationConfig.excluded_extensions)

    # === 不可重试错误标记 ===
    non_retryable_error_marker: str = "迭代超过最大迭代次数"

    # === P4-2 自适应压缩策略(借鉴TRAE Work CN被动压缩理念) ===
    # auto(默认): 多步骤任务(step_description非空)主动压缩,简单对话被动压缩(省计算)
    # proactive: 始终主动预测+三级阈值(当前行为,适用于已知复杂任务场景)
    # reactive: 始终被动压缩(仅critical阈值触发,适用于已知简单场景)
    compression_strategy: str = "auto"


# ---------------------------------------------------------------------------
# P3-8: 基于context_window的自动缩放
# ---------------------------------------------------------------------------

def _scaled(base_value: int, scale: float, min_value: int) -> int:
    """按context_window比例缩放绝对值,保留最小保底值

    Args:
        base_value: 基准值(对应128K上下文窗口)
        scale: 缩放比例 = actual_context_window / 128000
        min_value: 最小保底值(防止小模型过度压缩导致信息丢失)
    """
    return max(min_value, int(base_value * scale))


def create_memory_config(
        context_window: int,
        profile: str = "balanced",
        compression_strategy: str = "auto",
) -> MemoryConfig:
    """根据模型上下文窗口大小创建适配的MemoryConfig(自动缩放+配置预设)

    P3-8: 切换模型时只需修改config.yaml的context_window,本函数自动:
    - 按比例缩放15项绝对值(消息容量/截断/保留/摘要等)
    - 保留最小保底值防止小模型过度压缩

    P4-1: 配置预设(profile)调整3个百分比阈值,覆盖大多数调优场景:
    - balanced(默认): 平衡压缩频率与信息保留
    - lightweight: 延迟压缩,适用于简单对话(省计算)
    - heavy: 提前压缩,适用于复杂多步骤任务

    P4-2: 压缩策略(compression_strategy)控制主动/被动压缩:
    - auto(默认): 多步骤任务主动压缩,简单对话被动压缩
    - proactive: 始终主动压缩
    - reactive: 始终被动压缩(仅critical触发)

    Args:
        context_window: 模型上下文窗口大小(token数)
        profile: 配置预设名称(balanced/lightweight/heavy)
        compression_strategy: 压缩策略(auto/proactive/reactive)

    Returns:
        适配指定context_window和profile的MemoryConfig实例
    """
    scale = context_window / _REFERENCE_CONTEXT_WINDOW

    # P4-1: 根据profile选择百分比阈值(无效profile回退balanced)
    proactive_th, reactive_th, critical_th = _PROFILE_THRESHOLDS.get(
        profile, _PROFILE_THRESHOLDS["balanced"]
    )

    return MemoryConfig(
        # === 消息容量阈值(按比例缩放,保底防止过小) ===
        max_messages_soft=_scaled(60, scale, 20),       # 保底20条
        max_messages_hard=_scaled(90, scale, 30),       # 保底30条
        protect_head_count=_scaled(3, scale, 2),        # 保底2条
        protect_tail_count=_scaled(8, scale, 4),        # 保底4条

        # === 工具结果截断-入库前(按比例缩放) ===
        tool_result_max_length=_scaled(12000, scale, 4000),       # 保底4000字符
        browser_result_max_length=_scaled(12000, scale, 6000),    # 保底6000(浏览器操作必需字段最小要求)
        file_result_max_length=_scaled(10000, scale, 3000),       # 保底3000字符
        shell_result_max_length=_scaled(6000, scale, 2000),       # 保底2000字符
        search_result_max_length=_scaled(4000, scale, 1500),      # 保底1500字符
        deep_research_result_max_length=_scaled(6000, scale, 2000),  # 保底2000字符

        # === 压缩阶段保留值(按比例缩放,保底防止信息丢失) ===
        browser_content_threshold=_scaled(500, scale, 150),       # 保底150字符
        search_result_threshold=_scaled(1000, scale, 300),        # 保底300字符
        search_result_keep=_scaled(1000, scale, 300),             # 保底300字符
        shell_output_keep=_scaled(2000, scale, 500),              # 保底500字符
        file_content_keep=_scaled(2000, scale, 500),              # 保底500字符

        # === 会话摘要(按比例缩放) ===
        session_summary_max=_scaled(5000, scale, 1500),           # 保底1500字符
        session_summary_inject_max=_scaled(2000, scale, 600),     # 保底600字符
        compression_summary_per_op=_scaled(200, scale, 80),       # 保底80字符

        # === 高压截断(按比例缩放) ===
        high_pressure_truncate_max=_scaled(4000, scale, 1000),    # 保底1000字符

        # === P4-1: 百分比阈值由profile决定(不随context_window缩放) ===
        proactive_compress_threshold=proactive_th,
        reactive_compress_threshold=reactive_th,
        critical_threshold=critical_th,

        # === P4-2: 压缩策略(不随context_window缩放) ===
        compression_strategy=compression_strategy,

        # 以下字段不随context_window缩放(与上下文容量无关):
        # - key_facts_max=10 (KeyFact条数,与上下文窗口无关)
        # - max_deliverable_files/excluded_extensions (交付物过滤,与上下文无关)
        # - non_retryable_error_marker (错误标记,与上下文无关)
    )


def _load_context_window_from_config() -> int:
    """从config.yaml读取context_window(模块加载时调用)

    尝试读取config.yaml中的llm_config.context_window,
    失败时返回默认值128000(P3优化基准值),确保模块在任何环境下都能正常加载。

    读取顺序:
    1. 环境变量APP_CONFIG_FILEPATH指定的路径
    2. 当前目录下的config.yaml
    3. 默认值128000
    """
    config_path = os.environ.get("APP_CONFIG_FILEPATH", "config.yaml")
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        cw = config.get('llm_config', {}).get('context_window', _REFERENCE_CONTEXT_WINDOW)
        if isinstance(cw, int) and cw > 0:
            return cw
        logger.warning(
            f"config.yaml中context_window值无效({cw}),使用默认值{_REFERENCE_CONTEXT_WINDOW}"
        )
        return _REFERENCE_CONTEXT_WINDOW
    except FileNotFoundError:
        logger.debug(
            f"未找到配置文件[{config_path}],使用默认context_window={_REFERENCE_CONTEXT_WINDOW}"
        )
        return _REFERENCE_CONTEXT_WINDOW
    except Exception as e:
        logger.warning(
            f"读取config.yaml的context_window失败({e}),使用默认值{_REFERENCE_CONTEXT_WINDOW}"
        )
        return _REFERENCE_CONTEXT_WINDOW


def _load_memory_profile_from_config() -> str:
    """从config.yaml读取memory_config.profile(P4-1配置预设)

    读取config.yaml中的memory_config.profile(balanced/lightweight/heavy),
    失败时返回默认值"balanced"。

    示例config.yaml:
        memory_config:
          profile: lightweight  # 简单场景用轻量预设
    """
    config_path = os.environ.get("APP_CONFIG_FILEPATH", "config.yaml")
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        profile = config.get('memory_config', {}).get('profile', 'balanced')
        if profile in _PROFILE_THRESHOLDS:
            return profile
        logger.warning(
            f"config.yaml中memory_config.profile值无效({profile}),使用默认值balanced"
        )
        return 'balanced'
    except Exception:
        return 'balanced'


def _load_compression_strategy_from_config() -> str:
    """从config.yaml读取memory_config.compression_strategy(P4-2压缩策略)

    读取config.yaml中的memory_config.compression_strategy(auto/proactive/reactive),
    失败时返回默认值"auto"。

    示例config.yaml:
        memory_config:
          compression_strategy: reactive  # 已知简单场景用被动压缩
    """
    config_path = os.environ.get("APP_CONFIG_FILEPATH", "config.yaml")
    valid_strategies = {"auto", "proactive", "reactive"}
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        strategy = config.get('memory_config', {}).get('compression_strategy', 'auto')
        if strategy in valid_strategies:
            return strategy
        logger.warning(
            f"config.yaml中compression_strategy值无效({strategy}),使用默认值auto"
        )
        return 'auto'
    except Exception:
        return 'auto'


# 模块级单例 - P3-8/P4-1/P4-2: 根据 config.yaml 自动配置
# 切换模型时只需修改 config.yaml 的 context_window,所有阈值自动适配:
#   - 百分比阈值由 profile 决定(balanced/lightweight/heavy)
#   - 绝对值按 context_window/128K 比例缩放,保底值兜底
#   - 压缩策略 auto 下: 多步骤任务主动压缩,简单对话被动压缩(省计算)
_CONTEXT_WINDOW = _load_context_window_from_config()
_PROFILE = _load_memory_profile_from_config()
_COMPRESSION_STRATEGY = _load_compression_strategy_from_config()
DEFAULT_MEMORY_CONFIG = create_memory_config(
    _CONTEXT_WINDOW, _PROFILE, _COMPRESSION_STRATEGY
)
