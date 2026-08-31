#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : budget_tracker.py
工具调用预算会话级追踪器

设计目标:
- 按 project_memory 硬约束,限制高风险工具的会话级调用次数
  (search_web=8, deep_research=2, browser_navigate=10)
- 新用户消息时由 BaseAgent 重置(避免跨轮次累积)
- 100% 阈值硬拦截: 超过预算时工具返回错误,引导 LLM 切换策略
- 75% 阈值软告警: 通过 metrics/logger 提示运维,辅助 prompt 层引导

Batch 39 扩展(方向2 预算精细化 + 方向3 策略切换观测):
- 支持外部 budgets 配置(AgentConfig.tool_budgets),按任务类型动态调整
- mark_exceeded/consume_exceeded_event 事件队列: 工具硬拦截时记录,
  BaseAgent 消费后联动 metrics(方向3 预算超限事件可观测)
- check_and_warn 增加 metrics 参数: 75% 告警联动 metrics_collector
- adjust_for_task_type: 按任务类型上调预算(研究类 deep_research +1)

设计原则:
- 单一职责: 仅追踪计数与阈值判断,不涉及 prompt 注入(由 BaseAgent 负责)
- 无状态隔离: 每会话独立实例,避免跨会话累加
- 向后兼容: 默认关闭(空 budgets),老调用方不破坏
"""
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.services.observability.metrics_collector import MetricsCollector

logger = logging.getLogger(__name__)

# 默认会话级工具调用预算(project_memory 硬约束)
# - search_web=8: 防止 LLM 无限循环搜索同一主题
# - deep_research=2: 深度研究单次即多轮递归,2 次足够覆盖主题
# - browser_navigate=10: 防止浏览器导航死循环
# - browser_console_exec=10: 防止 LLM 滥用 console_exec 提取页面内容(content字段已有)
_DEFAULT_BUDGETS: Dict[str, int] = {
    "search_web": 8,
    "deep_research": 2,
    "browser_navigate": 10,
    "browser_console_exec": 10,
}

# 75% 阈值告警比例(达到时记录 INFO 日志,辅助运维监控)
_WARNING_RATIO = 0.75

# 任务类型 → 预算调整增量(方向2: 按任务类型动态调整预算)
# 仅上调不下调: 研究类任务允许更多 deep_research 调用,避免预算过严误伤
_TASK_TYPE_BUDGET_ADJUSTMENTS: Dict[str, Dict[str, int]] = {
    "research": {"deep_research": 1},  # 研究类: deep_research 2→3
    "data_analysis": {"search_web": 2},  # 数据分析: search_web 8→10
    "browser": {"browser_navigate": 5},  # 浏览器密集: 10→15
}


class ToolBudgetTracker:
    """工具调用预算会话级追踪器

    追踪高风险工具的会话级调用次数,超过预算时由工具层硬拦截。
    新用户消息时由 BaseAgent 调用 reset() 重置,避免跨轮次累积。

    Batch 39 扩展:
    - mark_exceeded/consume_exceeded_event: 事件队列机制,工具硬拦截时标记,
      BaseAgent 消费后联动 metrics 实现预算超限可观测(方向3)
    - adjust_for_task_type: 按任务类型上调预算(方向2)
    - check_and_warn(metrics): 75% 告警联动 metrics_collector(方向3)

    使用方式:
        tracker = ToolBudgetTracker()
        if tracker.is_exceeded("search_web"):
            tracker.mark_exceeded("search_web")  # Batch 39: 标记超限事件
            return ToolResult(success=False, message="search_web 调用次数已达上限...")
        tracker.increment("search_web")
        # 执行工具调用...
    """

    def __init__(self, budgets: Optional[Dict[str, int]] = None) -> None:
        """构造函数

        Args:
            budgets: 自定义预算字典,None 时使用默认 _DEFAULT_BUDGETS,
                    空字典表示禁用所有预算检查(向后兼容)
        """
        self._budgets: Dict[str, int] = dict(budgets) if budgets is not None else dict(_DEFAULT_BUDGETS)
        self._counts: Dict[str, int] = defaultdict(int)
        # 已告警的工具集合(75% 阈值仅告警一次,避免日志噪音)
        self._warned: set = set()
        # Batch 39 / 方向3: 预算超限事件队列(工具硬拦截时入队,BaseAgent 消费)
        # 设计: 队列而非标志位,因单轮迭代可能并行调用多个工具,需逐一消费
        self._exceeded_events: List[str] = []
        # Batch 39 / 方向2: 任务类型(用于 adjust_for_task_type 后的快照记录)
        self._task_type: str = "general"

    def increment(self, tool_name: str) -> None:
        """记录工具调用次数(在工具执行前调用)

        Args:
            tool_name: 工具名(如 search_web/deep_research/browser_navigate)
        """
        if not tool_name:
            return
        self._counts[tool_name] += 1

    def decrement(self, tool_name: str) -> None:
        """回退工具调用计数(预占式预算失败时调用)

        使用场景: search_web 采用预占式预算(check+increment原子操作),
        当搜索引擎调用失败时回退预占的名额,保证失败不消耗预算(让LLM可重试)。

        Args:
            tool_name: 工具名(如 search_web)
        """
        if not tool_name:
            return
        current = self._counts.get(tool_name, 0)
        if current > 0:
            self._counts[tool_name] = current - 1

    def get_count(self, tool_name: str) -> int:
        """获取当前工具调用次数"""
        return self._counts.get(tool_name, 0)

    def get_budget(self, tool_name: str) -> Optional[int]:
        """获取工具的会话级预算上限,None 表示无限制"""
        return self._budgets.get(tool_name)

    def is_exceeded(self, tool_name: str) -> bool:
        """检查工具调用是否已达预算上限

        Returns:
            True 表示已达上限,工具应硬拦截(返回错误 ToolResult)
        """
        budget = self._budgets.get(tool_name)
        if budget is None or budget <= 0:
            return False
        return self._counts.get(tool_name, 0) >= budget

    def get_usage_ratio(self, tool_name: str) -> float:
        """获取工具使用率(0.0-1.0)

        无预算配置时返回 0.0,用于 BaseAgent 75% 阈值告警判断。
        """
        budget = self._budgets.get(tool_name)
        if not budget or budget <= 0:
            return 0.0
        return min(self._counts.get(tool_name, 0) / budget, 1.0)

    def check_and_warn(
            self,
            tool_name: str,
            metrics: Optional["MetricsCollector"] = None,
    ) -> bool:
        """检查使用率并在达到 75% 阈值时记录告警(每工具仅告警一次)

        Batch 39 / 方向3: 新增 metrics 参数,75% 告警时联动 metrics_collector
        记录 budget_warning_count 指标,实现预算告警可观测。

        由 BaseAgent._invoke_tool 在工具执行后调用(修复原 check_and_warn 断链),
        辅助运维监控 LLM 行为趋势。

        Args:
            tool_name: 工具名
            metrics: 可选的指标收集器,传入时联动记录 budget_warning_count

        Returns:
            True 表示本次触发了告警(达 75% 阈值且首次),False 表示未触发
        """
        if not tool_name or tool_name in self._warned:
            return False
        ratio = self.get_usage_ratio(tool_name)
        if ratio >= _WARNING_RATIO:
            budget = self._budgets.get(tool_name, 0)
            count = self._counts.get(tool_name, 0)
            logger.info(
                f"工具[{tool_name}]调用次数达告警阈值: {count}/{budget} "
                f"({ratio:.0%}), LLM 接近预算上限"
            )
            self._warned.add(tool_name)
            # Batch 39 / 方向3: 联动 metrics 记录预算告警事件
            if metrics is not None:
                try:
                    metrics.increment("budget_warning_count")
                    metrics.set_gauge(
                        f"budget_usage_ratio_{tool_name}",
                        round(ratio, 4),
                    )
                except Exception as e:
                    logger.debug(f"预算告警 metrics 联动失败(降级忽略): {e}")
            return True
        return False

    def mark_exceeded(self, tool_name: str) -> None:
        """标记工具预算超限事件(Batch 39 / 方向3)

        由工具层(SearchTool/DeepResearchTool/BrowserTool)在 is_exceeded 返回 True 时调用,
        将超限事件入队。BaseAgent._invoke_tool 通过 consume_exceeded_event 消费,
        消费后联动 metrics 记录 budget_exceeded_count,实现预算超限可观测。

        设计: 队列而非标志位,支持单轮迭代并行调用多工具时逐一消费。

        Args:
            tool_name: 超限的工具名
        """
        if tool_name:
            self._exceeded_events.append(tool_name)

    def consume_exceeded_event(self) -> Optional[str]:
        """消费一个预算超限事件(Batch 39 / 方向3)

        由 BaseAgent._invoke_tool 在工具执行后调用,获取刚发生的超限事件。
        消费后 BaseAgent 联动 metrics 并追踪策略切换。

        Returns:
            超限的工具名,无待消费事件时返回 None
        """
        if self._exceeded_events:
            return self._exceeded_events.pop(0)
        return None

    def adjust_for_task_type(
            self,
            task_type: str,
            adjustments: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> None:
        """按任务类型动态调整预算(Batch 39 / 方向2, Batch 40 扩展 A/B 实验支持)

        仅上调不下调: 研究类任务允许更多 deep_research,数据分析类允许更多 search_web,
        浏览器密集类允许更多 browser_navigate。避免预算过严误伤合法场景。

        幂等设计: 多次调用同一 task_type 不会叠加调整(先记录原始预算基准)。
        仅在首次调用时生效(基于 _task_type != task_type 判断),防止重复调整。

        Batch 40 扩展(方向2 A/B 测试):
        - 新增 adjustments 参数,支持从 ExperimentResolver 传入实验组配置
        - adjustments 为 None 时使用默认 _TASK_TYPE_BUDGET_ADJUSTMENTS(向后兼容)

        Args:
            task_type: 任务类型(research/data_analysis/browser/general)
            adjustments: 可选的预算调整字典(来自实验配置), None 时使用默认值
        """
        if not task_type or task_type == self._task_type:
            return
        # Batch 40 / 方向2: 优先使用外部传入的实验配置,降级到默认硬编码值
        source_adjustments = adjustments or _TASK_TYPE_BUDGET_ADJUSTMENTS
        task_adjustments = source_adjustments.get(task_type)
        if not task_adjustments:
            self._task_type = task_type
            return
        for tool_name, increment in task_adjustments.items():
            current = self._budgets.get(tool_name)
            if current and current > 0:
                self._budgets[tool_name] = current + increment
                logger.info(
                    f"任务类型[{task_type}]调整工具[{tool_name}]预算: "
                    f"{current} → {current + increment}"
                )
        self._task_type = task_type

    def raise_budget(self, tool_name: str, new_budget: int) -> bool:
        """动态上调工具预算(仅增不减,方案B/会话437cbc75根因修复)

        复杂企业App(交互元素>200)content易被截断,LLM需更多console_exec
        次数提取被截断的表格/弹窗文本。静态10次硬上限在content截断时不够用,
        由BrowserTool在browser_view检测到复杂页面后调用此方法放宽。

        幂等安全: 仅当new_budget > 当前预算时生效,不下调、不重复叠加。

        Args:
            tool_name: 工具名(如 browser_console_exec)
            new_budget: 新预算上限(必须大于当前值才生效)

        Returns:
            True表示预算已上调,False表示无变化(未配置/不大于当前值)
        """
        current = self._budgets.get(tool_name)
        if current is None or new_budget <= current:
            return False
        self._budgets[tool_name] = new_budget
        logger.info(
            f"工具[{tool_name}]预算动态上调: {current} → {new_budget} "
            f"(复杂页面自适应放宽)"
        )
        return True

    @property
    def task_type(self) -> str:
        """当前任务类型(只读,供快照记录)"""
        return self._task_type

    def reset(self) -> None:
        """重置所有工具调用计数(新用户消息时由 BaseAgent 调用)

        project_memory: "Tool call budgets are session-level and reset on new user messages"

        注意: 预算上限(_budgets)不重置,仅重置计数与告警状态。
        任务类型调整(adjust_for_task_type)也不重置,因同一会话内任务类型不变。
        """
        self._counts.clear()
        self._warned.clear()
        self._exceeded_events.clear()

    def get_usage_report(self) -> Dict[str, Any]:
        """获取所有受预算工具的使用情况快照(供日志/调试/指标合并)

        Batch 39 / 方向3: 扩展返回结构,增加 usage_ratio 与 task_type,
        供 AgentTaskRunner snapshot 合并实现预算可观测。
        """
        return {
            "task_type": self._task_type,
            "tools": {
                name: {
                    "count": self._counts.get(name, 0),
                    "budget": budget,
                    "usage_ratio": round(
                        min(self._counts.get(name, 0) / budget, 1.0), 4
                    ) if budget > 0 else 0.0,
                }
                for name, budget in self._budgets.items()
            },
        }
