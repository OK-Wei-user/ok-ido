#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : metrics_collector.py
会话级指标收集器(F10-9 P1,运维可观测性基础)

设计原则:
1. 轻量: 纯内存累加,无IO开销;asyncio单协程切换点上下文不需要额外锁
2. 非侵入: 所有方法异常静默降级,绝不阻断主流程
3. 可扩展: 支持任意指标名注册,不限制预定义清单
4. 复用解耦: 独立模块,通过依赖注入传递给AgentTaskRunner/BaseAgent

集成位置:
- AgentTaskRunner.invoke() try/finally块输出snapshot到结构化日志(便于ELK采集)
- BaseAgent._invoke_llm() 埋点LLM调用次数与token数
- BaseAgent._invoke_tool() 埋点工具调用数与缓存命中/未命中

指标命名约定(语义化):
- llm_call_count: LLM调用次数
- llm_token_input_total: LLM输入token累计
- llm_token_output_total: LLM输出token累计
- tool_call_count: 工具调用次数
- tool_cache_hit_count: 工具缓存命中次数
- tool_cache_miss_count: 工具缓存未命中次数
- mcp_call_count: MCP工具调用次数
- mcp_polling_count: MCP重复轮询次数
- parallel_tool_pairs: 并行工具调用对数(单次invoke多工具时累加)
- step_count: 步骤执行次数
- step_retry_count: 步骤重试次数
- compression_count: 常规记忆压缩次数
- emergency_compression_count: 紧急记忆压缩次数
- session_duration_seconds: 会话总耗时(秒)
- Batch 39 新增(方向3 预算观测 + 方向4 shell调用观测):
  - shell_execute_count: shell_execute 调用次数(方向4: 观测脚本合并引导效果)
  - budget_warning_count: 工具预算75%告警次数(方向3: 预算告警可观测)
  - budget_exceeded_count: 工具预算超限次数(方向3: 硬拦截事件可观测)
  - strategy_switch_count: LLM策略切换次数(方向3: 超限后切换不同工具)
  - strategy_switch_retry_count: LLM策略未切换重试次数(方向3: 超限后重试相同工具)
  - budget_usage_ratio_{tool}: 各工具预算使用率瞬时值(方向3: gauge)
  - tool_budget_report: 工具预算使用报告快照(方向3: gauge,含task_type与各工具count/budget/ratio)
"""
import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class MetricsCollector:
    """会话级指标收集器

    收集范围: LLM调用/工具调用/缓存命中/记忆压缩/步骤执行/会话耗时
    输出方式: snapshot()返回扁平字典, 便于结构化日志输出或Redis持久化

    线程安全说明:
    - asyncio单协程切换点(Python await处)上下文,简单dict读写无竞态
    - 复合操作(如读-改-写)的中间状态在单一await前完成,无需额外锁
    """

    def __init__(self, session_id: str = "") -> None:
        """构造函数,初始化指标收集器

        Args:
            session_id: 会话ID,用于日志标识
        """
        self._session_id = session_id
        self._counters: Dict[str, int] = {}
        self._durations: Dict[str, float] = {}
        self._start_ts: float = time.monotonic()
        self._end_ts: Optional[float] = None

    @property
    def session_id(self) -> str:
        """会话ID只读属性"""
        return self._session_id

    def increment(self, name: str, value: int = 1) -> None:
        """累加计数型指标

        Args:
            name: 指标名(语义化命名,如llm_call_count)
            value: 累加值(默认1,支持批量累加如统计token数)
        """
        try:
            self._counters[name] = self._counters.get(name, 0) + int(value)
        except Exception as e:
            logger.debug(f"指标[{name}]累加失败(降级忽略): {e}")

    def record_duration(self, name: str, seconds: float) -> None:
        """记录耗时型指标(累加,适合多次调用累计耗时)

        Args:
            name: 指标名(如llm_invoke_seconds)
            seconds: 耗时(秒)
        """
        try:
            self._durations[name] = self._durations.get(name, 0.0) + float(seconds)
        except Exception as e:
            logger.debug(f"指标[{name}]耗时记录失败(降级忽略): {e}")

    def set_gauge(self, name: str, value: Any) -> None:
        """设置瞬时值型指标(覆盖,适合快照类指标如当前消息数)

        Args:
            name: 指标名(如memory_message_count)
            value: 指标值
        """
        try:
            self._counters[name] = value
        except Exception as e:
            logger.debug(f"指标[{name}]瞬时值设置失败(降级忽略): {e}")

    def mark_session_end(self) -> None:
        """标记会话结束,计算会话总耗时"""
        self._end_ts = time.monotonic()

    def snapshot(self) -> Dict[str, Any]:
        """生成指标快照(扁平字典)

        包含:
        - 所有计数型指标(_counters)
        - 所有耗时型指标(_durations)
        - 派生指标(缓存命中率、平均LLM耗时等)
        - 会话总耗时(若已mark_session_end)

        Returns:
            扁平字典,适合json.dumps输出到日志
        """
        try:
            result: Dict[str, Any] = {
                "session_id": self._session_id,
                **dict(self._counters),
            }
            # 耗时指标加_seconds后缀,与count区分
            for name, value in self._durations.items():
                result[f"{name}_seconds"] = round(value, 3)

            # 派生指标: 缓存命中率
            cache_hit = self._counters.get("tool_cache_hit_count", 0)
            cache_miss = self._counters.get("tool_cache_miss_count", 0)
            total_cache = cache_hit + cache_miss
            if total_cache > 0:
                result["tool_cache_hit_rate"] = round(cache_hit / total_cache, 4)

            # 派生指标: 平均LLM耗时
            llm_total_seconds = self._durations.get("llm_invoke", 0.0)
            llm_call_count = self._counters.get("llm_call_count", 0)
            if llm_call_count > 0:
                result["llm_avg_invoke_seconds"] = round(llm_total_seconds / llm_call_count, 3)

            # 派生指标: 会话总耗时
            if self._end_ts is not None:
                result["session_duration_seconds"] = round(self._end_ts - self._start_ts, 3)

            return result
        except Exception as e:
            logger.debug(f"指标快照生成失败(降级返回空字典): {e}")
            return {"session_id": self._session_id, "error": "snapshot_failed"}

    def to_log_json(self) -> str:
        """生成结构化日志JSON字符串(便于ELK采集)

        Returns:
            JSON格式字符串,含所有指标
        """
        try:
            return json.dumps(self.snapshot(), ensure_ascii=False, default=str)
        except Exception as e:
            logger.debug(f"指标JSON序列化失败(降级返回空对象): {e}")
            return "{}"

    def reset(self) -> None:
        """重置所有指标(用于会话续接时复用)"""
        self._counters.clear()
        self._durations.clear()
        self._start_ts = time.monotonic()
        self._end_ts = None

    def log_snapshot(self) -> None:
        """输出指标快照到结构化日志

        在AgentTaskRunner.invoke()的finally块调用,便于ELK采集与会话审计
        """
        try:
            logger.info(
                f"会话[{self._session_id}]指标快照: {self.to_log_json()}",
                extra={"metrics": self.snapshot(), "event_type": "session_metrics"},
            )
        except Exception as e:
            logger.debug(f"指标日志输出失败(降级忽略): {e}")

    def start_timer(self, name: str) -> "_TimerContext":
        """启动计时上下文(用于精准测量代码块耗时)

        Args:
            name: 指标名(如llm_invoke)

        Returns:
            上下文管理器,退出时自动record_duration

        使用示例:
            with metrics.start_timer("llm_invoke"):
                await llm.invoke(...)
        """
        return _TimerContext(self, name)


class _TimerContext:
    """计时上下文管理器(私有,仅供MetricsCollector.start_timer使用)

    语义说明:
    - 构造时即启动计时(__init__设置_start),匹配start_timer()的方法名
    - __enter__重置_start,使with语句语义符合直觉(with块进入时重新计时)
    - __exit__计算elapsed并记录,无论是否调用过__enter__都能正确工作

    兼容两种使用模式:
    1. with语句(推荐): `with metrics.start_timer("x"): ...`
    2. 手动调用(适用于try/finally结构):
       ```
       timer = metrics.start_timer("x")
       try:
           ...
       finally:
           timer.__exit__(None, None, None)
       ```
    """

    def __init__(self, collector: MetricsCollector, name: str) -> None:
        self._collector = collector
        self._name = name
        # F10-9修复: 构造时即启动计时,使手动调用模式(未调用__enter__)也能正确计时。
        # 否则_start保持0.0,__exit__计算time.monotonic() - 0.0 = 系统启动以来秒数,
        # 导致耗时指标出现57小时级别异常值。
        self._start: float = time.monotonic()

    def __enter__(self) -> "_TimerContext":
        # with语句进入时重置_start,确保with块之前到start_timer()调用的耗时
        # 不被计入(更精确地测量with块内代码)
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            elapsed = time.monotonic() - self._start
            self._collector.record_duration(self._name, elapsed)
        except Exception as e:
            logger.debug(f"计时器[{self._name}]退出失败(降级忽略): {e}")
