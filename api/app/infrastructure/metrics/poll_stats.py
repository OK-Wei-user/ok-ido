#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2026/07/21 23:00

@File    : poll_stats.py
MCP 工具轮询统计收集器(批次 26)

为 mcp.py 运行时退避机制补充结构化统计指标,便于运维监控 LLM 行为,
为后续 P11 退役决策提供数据支撑。

工业级生产标准:
- 纯内存设计,不持久化,不增加 Redis/DB 负担
- 线程安全(threading.Lock 保护实例字典)
- 会话级隔离(session_id → PollStatsCollector 单例)
- 运维通过 logger.info 结构化日志查看(MCP_POLL_STATS: ...)
"""
import threading
from typing import Dict, Any, Optional


class PollStatsCollector:
    """MCP 工具轮询统计收集器(会话级隔离)

    收集维度:
    - session_id → tool_name → invoke_count / pending_count / completed_count
    - 退避触发次数(参数级 _PARAM_BACKOFF_THRESHOLD / 工具级 _TOOL_BACKOFF_THRESHOLD)
    - 后台异步任务总数(_run_mcp_async_polling 启动数)

    使用方式:
        collector = PollStatsCollector.get_or_create(session_id)
        collector.record_invoke("mcp_xxx_query", is_pending=True)
        collector.record_backoff_trigger("mcp_xxx_query", level="param")
        collector.record_async_task("mcp_xxx_image_create")
        snapshot = collector.snapshot()  # 用于日志输出
    """

    _instances: Dict[str, "PollStatsCollector"] = {}
    _lock = threading.Lock()

    @classmethod
    def get_or_create(cls, session_id: Optional[str]) -> "PollStatsCollector":
        """获取或创建会话级统计收集器(线程安全单例)

        Args:
            session_id: 会话 ID,None 或空字符串时使用 "_default_" 桶

        Returns:
            该会话专用的 PollStatsCollector 实例
        """
        key = session_id if session_id else "_default_"
        with cls._lock:
            if key not in cls._instances:
                instance = cls()
                instance._session_id = key
                cls._instances[key] = instance
            return cls._instances[key]

    def __init__(self) -> None:
        """构造函数,初始化所有计数器

        注意:外部应通过 get_or_create() 获取实例,避免直接构造。
        """
        self._session_id: str = ""
        self._invoke_counts: Dict[str, int] = {}          # tool_name → 调用次数
        self._pending_counts: Dict[str, int] = {}          # tool_name → pending 次数
        self._completed_counts: Dict[str, int] = {}        # tool_name → completed 次数
        self._backoff_param_level: Dict[str, int] = {}     # tool_name → 参数级退避触发次数
        self._backoff_tool_level: Dict[str, int] = {}      # tool_name → 工具级退避触发次数
        self._async_task_total: int = 0                    # 后台异步任务启动总数
        self._async_task_by_tool: Dict[str, int] = {}       # tool_name → 异步任务启动数

    def record_invoke(self, tool_name: str, is_pending: bool) -> None:
        """记录一次 MCP 工具调用

        Args:
            tool_name: MCP 工具名(如 mcp_xxx_query)
            is_pending: True 表示返回 pending 状态,False 表示返回完成
        """
        self._invoke_counts[tool_name] = self._invoke_counts.get(tool_name, 0) + 1
        if is_pending:
            self._pending_counts[tool_name] = self._pending_counts.get(tool_name, 0) + 1
        else:
            self._completed_counts[tool_name] = self._completed_counts.get(tool_name, 0) + 1

    def record_backoff_trigger(self, tool_name: str, level: str) -> None:
        """记录退避触发

        Args:
            tool_name: MCP 工具名
            level: 退避级别,'param' 表示参数级(同参数3次),
                   'tool' 表示工具级(同工具不同参数累计4次)
        """
        if level == "param":
            self._backoff_param_level[tool_name] = self._backoff_param_level.get(tool_name, 0) + 1
        elif level == "tool":
            self._backoff_tool_level[tool_name] = self._backoff_tool_level.get(tool_name, 0) + 1

    def record_async_task(self, tool_name: str) -> None:
        """记录一次后台异步任务启动(_run_mcp_async_polling)

        Args:
            tool_name: 触发异步任务的 MCP 工具名
        """
        self._async_task_total += 1
        self._async_task_by_tool[tool_name] = self._async_task_by_tool.get(tool_name, 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        """返回当前统计快照(用于日志输出)

        Returns:
            包含所有统计字段的字典,可直接 json.dumps
        """
        return {
            "session_id": self._session_id,
            "invoke_counts": dict(self._invoke_counts),
            "pending_counts": dict(self._pending_counts),
            "completed_counts": dict(self._completed_counts),
            "backoff_param_level": dict(self._backoff_param_level),
            "backoff_tool_level": dict(self._backoff_tool_level),
            "async_task_total": self._async_task_total,
            "async_task_by_tool": dict(self._async_task_by_tool),
        }

    def reset(self) -> None:
        """清空所有计数器(会话结束时调用)"""
        self._invoke_counts.clear()
        self._pending_counts.clear()
        self._completed_counts.clear()
        self._backoff_param_level.clear()
        self._backoff_tool_level.clear()
        self._async_task_total = 0
        self._async_task_by_tool.clear()

    @classmethod
    def cleanup_session(cls, session_id: str) -> None:
        """清理指定会话的统计实例(防止内存泄漏)

        Args:
            session_id: 会话 ID
        """
        with cls._lock:
            cls._instances.pop(session_id, None)
