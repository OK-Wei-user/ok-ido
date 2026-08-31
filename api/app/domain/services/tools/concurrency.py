#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2026/07/20

@File    : concurrency.py
工具并发分类器 - 工具并行执行:判定工具是否可并行执行。

设计原则:
1.默认可并行: 仅显式声明为共享状态的工具才串行(黑名单机制)
2.前缀匹配: shell_*/browser_* 等共享状态工具自动识别为串行
3.全名匹配: file_write/file_delete 等写操作工具显式声明为串行
4.参数级隔离: shell_execute 按 session_id 隔离,同 session_id 串行,不同 session_id 可并行
5.配置驱动: 串行工具列表可通过 config.yaml 调整,无需改代码
6.与工具结果缓存白名单互补: 工具并行执行用黑名单(最大化并行),工具结果缓存用白名单(最大化安全)
"""
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


class ToolConcurrencyClassifier:
    """工具并发分类器:判定工具是否可并行执行

    基于配置的不可并行工具前缀+全名+参数级隔离键,判定工具是否共享状态。
    共享状态工具必须串行执行,避免竞态条件。
    参数级隔离: shell_execute 按 session_id 隔离,同 session_id 串行,不同 session_id 可并行。
    """

    def __init__(
            self,
            stateful_prefixes: List[str],
            stateful_names: List[str],
            stateful_arg_keys: Dict[str, List[str]] = None,
    ) -> None:
        """构造函数,完成分类器初始化

        Args:
            stateful_prefixes: 不可并行工具前缀列表(如 ["shell_", "browser_"])
                注意: shell_execute 单独按参数级隔离,stateful_prefixes 仍保留 "shell_"
                以覆盖 shell_read_output/shell_wait_process 等状态依赖子工具
            stateful_names: 不可并行工具全名列表(如 ["file_write", "file_delete"])
            stateful_arg_keys: 参数级隔离配置 {tool_name: [arg_key]},
                如 {"shell_execute": ["session_id"]} 表示 shell_execute 按 session_id 隔离,
                同一 session_id 串行,不同 session_id 可并行
        """
        # tuple用于 startswith 多前缀匹配优化
        self._stateful_prefixes = tuple(stateful_prefixes) if stateful_prefixes else ()
        self._stateful_names = set(stateful_names) if stateful_names else set()
        self._stateful_arg_keys = stateful_arg_keys or {}

    def is_parallelizable(self, tool_name: str, function_args: Dict[str, Any] = None) -> bool:
        """判断工具是否可并行执行: 不在前缀黑名单且不在全名黑名单

        参数级隔离工具(如 shell_execute)不视为完全不可并行,
        由 partition() 按 session_id 分组实现同组串行、跨组并行。

        Args:
            tool_name: 工具名
            function_args: 工具参数(可选,用于参数级隔离判断)

        Returns:
            True 表示可并行, False 表示必须串行
        """
        # 参数级隔离工具: 由 partition() 分组控制,此处返回 True 让其进入并行组
        if tool_name in self._stateful_arg_keys:
            return True
        # 前缀匹配(shell_read_output/shell_wait_process 等状态依赖子工具)
        if self._stateful_prefixes and tool_name.startswith(self._stateful_prefixes):
            return False
        # 全名匹配(file_write 等写操作工具)
        if tool_name in self._stateful_names:
            return False
        return True

    def _get_isolation_key(self, tool_name: str, function_args: Dict[str, Any]) -> str:
        """生成参数级隔离key: tool_name + 指定参数值的组合

        Args:
            tool_name: 工具名
            function_args: 工具参数

        Returns:
            隔离key字符串,相同key的工具必须串行;未配置参数级隔离的工具返回空串
        """
        arg_keys = self._stateful_arg_keys.get(tool_name)
        if not arg_keys:
            return ""
        parts = [tool_name]
        for key in arg_keys:
            parts.append(str(function_args.get(key, "")))
        return "|".join(parts)

    def partition(self, tool_calls: List[dict]) -> Tuple[List[dict], List[dict]]:
        """将工具调用列表划分为(可并行, 串行)两组,保持原始顺序

        参数级隔离工具(如 shell_execute)按 session_id 分组:
        - 同一 session_id 的 shell_execute 进入串行组(保持顺序)
        - 不同 session_id 的 shell_execute 进入并行组(可并行)
        - 首次出现的 session_id 的 shell_execute 进入并行组,后续同 session_id 的进入串行组

        Args:
            tool_calls: LLM返回的工具调用列表,每项含 function.name 和 function.arguments

        Returns:
            (parallel_calls, serial_calls) 两个列表,各自保持原始相对顺序
        """
        parallel, serial = [], []
        seen_isolation_keys = set()  # 已进入并行组的参数级隔离key
        for tc in tool_calls:
            func = tc.get("function", {})
            name = func.get("name", "")
            args_str = func.get("arguments", "{}")
            # 解析arguments(JSON字符串或字典)
            if isinstance(args_str, str):
                try:
                    import json
                    args = json.loads(args_str) if args_str else {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
            else:
                args = args_str or {}

            # 参数级隔离工具: 首次出现进入并行组,后续同key进入串行组
            if name in self._stateful_arg_keys:
                isolation_key = self._get_isolation_key(name, args)
                if isolation_key in seen_isolation_keys:
                    serial.append(tc)  # 同session_id后续调用串行
                else:
                    parallel.append(tc)  # 首次出现可并行
                    seen_isolation_keys.add(isolation_key)
                continue

            # 普通工具: 按前缀/全名判断
            if self.is_parallelizable(name):
                parallel.append(tc)
            else:
                serial.append(tc)
        return parallel, serial
