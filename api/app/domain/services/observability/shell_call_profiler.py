#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""shell_execute 调用画像器(Batch 40 / 方向3: 合并引导效果量化)

设计目标:
- 每次 shell_execute 调用记录画像数据,量化脚本合并引导的实际效果
- 区分"批量合并调用"(含循环/批量语义)与"单次调用"
- 记录合并引导是否被注入,便于对比引导前后的调用模式差异

画像字段:
- command_length: 命令长度(长命令更可能包含循环/批量逻辑)
- has_loop_pattern: 命令是否包含 for/while/批量语义
- consolidation_guidance_active: 本次执行时是否注入了合并引导
- call_index: 当前会话内第几次 shell_execute 调用

集成位置:
- BaseAgent._invoke_tool 在 shell_execute 调用成功后记录画像
- 画像数据通过 MetricsCollector 汇总,经 MetricsPersister 持久化到 Redis
"""
import re
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 循环/批量模式正则: 匹配 Python/Shell 中的循环与批量操作语义
_LOOP_PATTERNS = [
    re.compile(r"\bfor\s+\w+\s+in\b", re.IGNORECASE),          # Python for...in
    re.compile(r"\bwhile\s+", re.IGNORECASE),                    # Python/Shell while
    re.compile(r"\bfor\s+\w+\s+in\s+.*do\b", re.IGNORECASE),    # Shell for...do
    re.compile(r"\.map\s*\(", re.IGNORECASE),                    # JS/Python .map()
    re.compile(r"\.apply\s*\(", re.IGNORECASE),                  # pandas .apply()
    re.compile(r"\biterrows\s*\(", re.IGNORECASE),              # pandas iterrows
    re.compile(r"\bitertuples\s*\(", re.IGNORECASE),            # pandas itertuples
    re.compile(r"&&\s*.+&&\s*.+&&", re.IGNORECASE),             # Shell && 链(3+命令合并)
    re.compile(r"\bfor\s+i\s+in\s+range\b", re.IGNORECASE),     # Python for i in range
]


def detect_loop_pattern(command: str) -> bool:
    """检测命令是否包含循环/批量操作语义

    Args:
        command: shell 命令字符串

    Returns:
        True 表示命令包含循环/批量模式(可能是合并脚本)
    """
    if not command:
        return False
    return any(pattern.search(command) for pattern in _LOOP_PATTERNS)


class ShellCallProfiler:
    """shell_execute 调用画像收集器(Batch 40 / 方向3)

    在会话级别收集每次 shell_execute 的调用画像,会话结束时通过
    get_profile_summary() 生成汇总,经 MetricsCollector + MetricsPersister
    持久化到 Redis,供离线分析脚本量化合并引导效果。

    使用方式:
        profiler = ShellCallProfiler()
        profiler.record(command="for i in ...", guidance_active=True)
        summary = profiler.get_profile_summary()
    """

    def __init__(self) -> None:
        """构造函数,初始化画像收集器"""
        self._calls: List[Dict[str, Any]] = []
        self._guidance_active: bool = False

    def set_guidance_active(self, active: bool) -> None:
        """设置当前步骤是否注入了合并引导(由 BaseAgent 在步骤执行前调用)

        Args:
            active: True 表示当前步骤注入了 get_consolidation_guidance
        """
        self._guidance_active = active

    @property
    def total_calls(self) -> int:
        """当前会话累计 shell_execute 调用次数(批次45 P1-2)

        供 BaseAgent._build_shell_execute_guidance 判断是否超频次阈值。
        """
        return len(self._calls)

    def record(self, command: str, success: bool = True) -> None:
        """记录一次 shell_execute 调用画像

        Args:
            command: 执行的 shell 命令
            success: 调用是否成功
        """
        try:
            self._calls.append({
                "call_index": len(self._calls) + 1,
                "command_length": len(command) if command else 0,
                "has_loop_pattern": detect_loop_pattern(command or ""),
                "consolidation_guidance_active": self._guidance_active,
                "success": success,
            })
        except Exception as e:
            logger.debug(f"shell 调用画像记录失败(降级忽略): {e}")

    def get_profile_summary(self) -> Dict[str, Any]:
        """生成会话级 shell 调用画像汇总

        Returns:
            汇总字典,包含:
            - total_calls: 总调用次数
            - batch_calls: 含循环/批量模式的调用次数(合并脚本)
            - singleton_calls: 单次调用次数(未合并)
            - guidance_triggered_calls: 合并引导激活期间的调用次数
            - guidance_triggered: 整个会话是否触发过合并引导
            - avg_command_length: 平均命令长度
            - batch_ratio: 批量调用占比(0.0-1.0)
        """
        total = len(self._calls)
        if total == 0:
            return {
                "total_calls": 0,
                "batch_calls": 0,
                "singleton_calls": 0,
                "guidance_triggered_calls": 0,
                "guidance_triggered": False,
                "avg_command_length": 0,
                "batch_ratio": 0.0,
            }

        batch_calls = sum(1 for c in self._calls if c["has_loop_pattern"])
        guidance_calls = sum(1 for c in self._calls if c["consolidation_guidance_active"])
        avg_length = sum(c["command_length"] for c in self._calls) / total

        return {
            "total_calls": total,
            "batch_calls": batch_calls,
            "singleton_calls": total - batch_calls,
            "guidance_triggered_calls": guidance_calls,
            "guidance_triggered": guidance_calls > 0,
            "avg_command_length": round(avg_length, 1),
            "batch_ratio": round(batch_calls / total, 4),
        }

    def reset(self) -> None:
        """重置画像收集器(新用户消息时调用)"""
        self._calls.clear()
        self._guidance_active = False
