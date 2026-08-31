#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/16 3:16

@File    : planner_react.py
"""
import logging
import os
from typing import AsyncGenerator, Optional, Callable, TYPE_CHECKING

from app.domain.external.browser import Browser
from app.domain.external.json_parser import JSONParser
from app.domain.external.llm import LLM
from app.domain.external.sandbox import Sandbox
from app.domain.external.search import ContentFetcher, SearchEngine
from app.domain.external.task_callback import TaskCallbackManager
from app.domain.models.app_config import AgentConfig, DeepResearchConfig, ToolExecutionConfig
from app.domain.models.event import BaseEvent, PlanEvent, PlanEventStatus, TitleEvent, MessageEvent
from app.domain.models.event import DoneEvent, ErrorEvent, SandboxScanEvent
from app.domain.models.file import File
from app.domain.models.memory import Memory
from app.domain.models.memory_config import DEFAULT_MEMORY_CONFIG as _CFG
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.models.session import SessionStatus
from app.domain.models.skill import Skill
from app.domain.services.agents.planner import PlannerAgent
from app.domain.services.agents.react import ReActAgent
from app.domain.services.agents.task_type_classifier import classify_with_llm
from app.domain.services.experiments.experiment_resolver import ExperimentResolver
from app.domain.services.tools.a2a import A2ATool
from app.domain.services.tools.browser import BrowserTool
from app.domain.services.tools.budget_tracker import ToolBudgetTracker
from app.domain.services.tools.deep_research import DeepResearchTool
from app.domain.services.tools.file import FileTool
from app.domain.services.tools.mcp import MCPTool
from app.domain.services.tools.message import MessageTool
from app.domain.services.tools.search import SearchTool
from app.domain.services.tools.shell import ShellTool
from app.domain.services.tools.skill import SkillTool
from app.domain.services.tools.task_callback import TaskCallbackTool
from app.domain.services.observability import MetricsCollector
from app.domain.services.skill_service import SkillService
from app.domain.services.skills_prompt_cache import SkillsPromptCache
from app.application.services.file_presentation_service import DeliveryValidationResult, FilePresentationService
from app.infrastructure.external.llm.token_counter import TokenCounter
from .base import BaseFlow, FlowStatus
from ._plan_update_policy import should_skip_update_plan, MAX_CONSECUTIVE_SKIPS
from ...repositories.uow import IUnitOfWork

if TYPE_CHECKING:
    from app.infrastructure.storage.search_cache import SearchCache
    from app.infrastructure.storage.tool_cache import ToolResultCache
    from app.infrastructure.storage.idempotent_tool_registry import IdempotentToolRegistry
    from app.infrastructure.storage.session_prompt_cache import SessionPromptCache

logger = logging.getLogger(__name__)

_MAX_INJECT_LENGTH = 1500
_MAX_OSS_KEY_DISPLAY = 120

# 批次45 P0-1: 沙箱交付物主动扫描配置
# 设计原则: 沙箱文件系统是交付物的唯一真相源,不依赖LLM声明(attachments)或文本(result)
_SANDBOX_SCAN_ROOT = "/home/ubuntu"  # 扫描根目录(交付物默认生成位置)
_SANDBOX_SCAN_GLOB_PATTERNS = (  # 交付物扩展名 glob 模式(与 _DELIVERABLE_EXTENSIONS 对齐)
    "*.xlsx", "*.xls", "*.csv",
    "*.docx", "*.doc", "*.pdf", "*.pptx",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp", "*.svg",
    "*.md",  # 批次46: 补充.md(Markdown文档常作为交付大纲/报告)
)
_SANDBOX_SCAN_MAX_FILES = 30  # 扫描结果上限(防爆炸)
_SANDBOX_SCAN_INTERMEDIATE_PREFIXES = (  # 中间产物过滤(与 agent_task_runner._INTERMEDIATE_PATH_PREFIXES 对齐)
    "/tmp/", "/home/ubuntu/workspace/", "/home/ubuntu/.skill",
)

# 批次45 P0-2: 附件门禁配置(done前0附件回退EXECUTING追加生成步骤)
# 仅对强制交付物任务类型生效,避免误伤问答/研究类任务(交付物为文本答案)
_DELIVERABLE_REQUIRED_TASK_TYPES = frozenset({"data_analysis"})  # 强制交付物门禁的任务类型
_DELIVERABLE_RETRY_MAX = 1  # 门禁重试上限(防死循环: LLM再次未生成则放行summarize)
_DELIVERABLE_GUIDANCE_STEP_DESC = (  # 引导步骤描述(确定性步骤,省一次update_plan LLM调用)
    "根据前序步骤已收集的数据与分析结论,使用 python-docx/openpyxl 生成最终交付物文件"
    "(保存到 /home/ubuntu/ 目录,文件名用英文或拼音避免编码问题),"
    "并在步骤结果 attachments 字段显式声明文件完整路径。"
)

# 阈值统一由 MemoryConfig 管理
# F10-8: max_deliverable_files/excluded_extensions已迁移至FilePresentationConfig,
# 此处保留模块级常量仅为向后兼容(test_deliverable_selection.py/test_attachment_delivery.py引用),
# 实际过滤逻辑委托给FilePresentationService(单一数据源)。
_MAX_DELIVERABLE_FILES = _CFG.max_deliverable_files
_NON_RETRYABLE_ERROR_MARKER = _CFG.non_retryable_error_marker
_EXCLUDED_EXTENSIONS = _CFG.excluded_extensions

# F10-8: 模块级默认FilePresentationService实例,供_is_temp_file静态方法委托
# 使用默认配置(FilePresentationConfig默认值与MemoryConfig保持一致),保证向后兼容
_DEFAULT_FILE_PRESENTATION = FilePresentationService()


class PlannerReActFlow(BaseFlow):
    """规划与执行流"""

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],
            llm: LLM,
            agent_config: AgentConfig,
            session_id: str,
            json_parser: JSONParser,
            browser: Browser,
            sandbox: Sandbox,
            search_engine: SearchEngine,
            mcp_tool: MCPTool,
            a2a_tool: A2ATool,
            skill_tool: SkillTool,
            skill_service: SkillService,
            content_fetcher: Optional[ContentFetcher] = None,
            search_cache: Optional["SearchCache"] = None,
            deep_research_config: Optional[DeepResearchConfig] = None,
            token_counter: Optional[TokenCounter] = None,
            context_window: int = 64000,
            planner_llm: Optional[LLM] = None,  # PlanAgent轻量化: 规划Agent专用LLM(可选)
            tool_cache: Optional["ToolResultCache"] = None,  # 工具结果缓存(可选)
            tool_execution_config: Optional[ToolExecutionConfig] = None,  # 工具并行执行配置(可选)
            idempotent_registry: Optional["IdempotentToolRegistry"] = None,  # 幂等工具调用去重注册表(P10-1,可选)
            metrics_collector: Optional[MetricsCollector] = None,  # F10-9可观测性指标收集器(F10-9,可选)
            callback_manager: Optional[TaskCallbackManager] = None,  # F10-7异步任务回调管理器(可选)
            file_presentation: Optional[FilePresentationService] = None,  # F10-8文件展示策略服务(可选,默认FilePresentationService())
            prompt_cache: Optional["SessionPromptCache"] = None,  # 会话级提示词缓存(可选,L1内存+L2 Redis)
    ) -> None:
        """构造函数，完成规划与执行流的初始化"""
        self._uow_factory = uow_factory
        self._session_id = session_id
        self.status = FlowStatus.IDLE
        self.plan: Optional[Plan] = None
        self._skill_service = skill_service
        self._sandbox = sandbox  # 批次45 P0-1: 保存沙箱引用供 SUMMARIZING 阶段主动扫描交付物
        self._mcp_tool = mcp_tool  # 保存引用用于延迟注入MCP摘要
        self._a2a_tool = a2a_tool  # 保存引用用于延迟注入A2A能力摘要
        # 批次45 P0-2: 附件门禁状态(0附件时回退EXECUTING追加生成步骤)
        self._deliverable_retry_count: int = 0  # 门禁已触发次数(上限_DELIVERABLE_RETRY_MAX防死循环)
        self._current_task_type: str = "general"  # 当前任务类型(invoke入口由classify_with_llm设置)
        self._mcp_summary_injected = False  # MCP摘要是否已注入Planner系统提示
        self._a2a_hint_injected = False  # A2A能力摘要是否已注入Planner系统提示
        self._metrics_collector = metrics_collector  # F10-9可观测性: 用于流程级埋点(步骤计数等)
        self._callback_manager = callback_manager  # F10-7异步任务回调管理器
        self._shell_tool: Optional[ShellTool] = None  # F10-7: ShellTool引用,stop_session时用于取消后台任务
        # F10-8: 文件展示策略服务(集中化交付物过滤规则+交付前校验清单)
        # None时使用默认配置实例,保证向后兼容
        self._file_presentation = file_presentation if file_presentation is not None else FilePresentationService()
        # Batch 40 / 方向4: 保存 LLM 引用(供 classify_with_llm 异步分类使用)
        self._llm = llm

        # 工具调用预算会话级追踪器(project_memory硬约束: search_web=8/deep_research=2/browser_navigate=10)
        # 会话级共享实例: 所有工具共用一份计数,新用户消息时由 invoke() 重置
        # 设计原则: 不侵入BaseAgent.invoke(保持核心循环精简),reset 时机由 Flow 控制
        # Batch 39 / 方向2: 支持通过 AgentConfig.tool_budgets 外置配置覆盖默认预算
        # Batch 40 / 方向2: 集成 ExperimentResolver 支持 A/B 实验分组
        budget_tracker = ToolBudgetTracker(budgets=agent_config.tool_budgets or None)
        self._experiment_resolver = ExperimentResolver()
        self._experiment_group: str = "default"

        # 先实例化共享的SearchTool（复用缓存/去重/fetch能力）
        search_tool = SearchTool(
            search_engine=search_engine,
            content_fetcher=content_fetcher,
            cache=search_cache,
            budget_tracker=budget_tracker,
        )
        # DeepResearchTool注入同一SearchTool实例，复用缓存/去重/fetch
        deep_research_tool = DeepResearchTool(
            search_tool=search_tool,
            llm=llm,
            json_parser=json_parser,
            max_depth=deep_research_config.max_depth if deep_research_config else 2,
            results_per_search=deep_research_config.results_per_search if deep_research_config else 5,
            max_insights=deep_research_config.max_insights if deep_research_config else 20,
            time_limit=deep_research_config.time_limit_seconds if deep_research_config else 120,
            budget_tracker=budget_tracker,
        )

        # F10-7: ShellTool注入callback_manager支持async_mode;TaskCallbackTool提供task_wait能力
        shell_tool = ShellTool(sandbox=sandbox, callback_manager=callback_manager)
        self._shell_tool = shell_tool
        task_callback_tool = TaskCallbackTool(callback_manager=callback_manager)
        tools = [
            FileTool(sandbox=sandbox),
            shell_tool,
            BrowserTool(browser=browser, budget_tracker=budget_tracker),
            search_tool,
            deep_research_tool,
            MessageTool(),
            mcp_tool,
            a2a_tool,
            skill_tool,
            task_callback_tool,
        ]
        # 保存 budget_tracker 引用,invoke() 入口重置(新用户消息触发)
        self._budget_tracker = budget_tracker
        # Batch 40 / 方向3: shell 调用画像器引用(从 ReActAgent 获取,供 snapshot 合并)
        self._shell_profiler = None  # 将在 ReActAgent 构造后赋值
        # Batch 30: 条件化计划更新 — 连续跳过计数器,invoke()入口重置
        self._consecutive_skipped_updates = 0

        # 两阶段工具加载: Planner排除MCP完整schema(降token), ReAct保留完整schema(供调用)
        # MCP摘要将在invoke()中延迟注入Planner系统提示(需等MCP initialize完成后才有工具列表)
        planner_tools = [t for t in tools if t.name != "mcp"]

        skills_prompt = SkillsPromptCache.get_prompt(session_id=session_id)
        # PlanAgent轻量化: 规划Agent使用专用轻量化LLM(若配置),否则降级到共享llm
        # PlannerAgent仅做JSON规划输出,无需thinking=high,降级到flash+disabled可节省70%+ token成本
        effective_planner_llm = planner_llm if planner_llm is not None else llm
        if planner_llm is not None:
            logger.info(f"PlannerAgent启用专用LLM(会话[{session_id}]),降级到轻量化模型")
        # P4-5: 保存轻量LLM引用,供classify_with_llm等简单判断复用(避免用主LLM thinking=high浪费~20s)
        self._planner_llm = effective_planner_llm
        self.planner = PlannerAgent(
            uow_factory=uow_factory,
            session_id=session_id,
            agent_config=agent_config,
            llm=effective_planner_llm,
            json_parser=json_parser,
            tools=planner_tools,
            token_counter=token_counter,
            context_window=context_window,
            metrics_collector=metrics_collector,
        )
        if skills_prompt:
            self.planner._system_prompt += "\n\n" + skills_prompt
        logger.debug(f"创建规划Agent成功, 会话id: {self._session_id}")

        self.react = ReActAgent(
            uow_factory=uow_factory,
            session_id=session_id,
            agent_config=agent_config,
            llm=llm,
            json_parser=json_parser,
            tools=tools,
            token_counter=token_counter,
            context_window=context_window,
            tool_cache=tool_cache,  # 工具结果缓存: 透传(PlannerAgent不调用工具,无需传)
            tool_execution_config=tool_execution_config,  # 工具并行执行: 透传配置(仅ReActAgent需要)
            idempotent_registry=idempotent_registry,  # 幂等工具调用去重注册表(P10-1): 透传(仅ReActAgent需要)
            metrics_collector=metrics_collector,  # 可观测性指标收集器(F10-9): 透传
            budget_tracker=budget_tracker,  # Batch 39 / 方向2+3: 预算追踪器透传(75%告警+超限观测+策略切换)
        )
        if skills_prompt:
            self.react._system_prompt += "\n\n" + skills_prompt
        # Batch 40 / 方向3: 从 ReActAgent 获取 shell 画像器引用(供 snapshot 合并)
        self._shell_profiler = self.react._shell_profiler
        logger.debug(f"创建执行Agent成功, 会话id: {self._session_id}")

    async def cancel_background_tasks(self) -> None:
        """取消所有后台异步任务(F10-7/P11,会话停止时调用)

        委托给 ShellTool 和 MCPTool 的 cancel_background_tasks 取消
        shell_execute(async_mode=true) 与 MCP 工具(mcp_* async_mode=true)
        启动的后台任务。异常不抛出(仅记录警告),确保不影响主流程的会话停止。
        """
        # 1. 取消 ShellTool 后台任务
        if self._shell_tool:
            try:
                self._shell_tool.cancel_background_tasks()
            except Exception as e:
                logger.warning(
                    f"会话[{self._session_id}]取消 ShellTool 后台异步任务失败(不影响主流程): {e}"
                )
        # 2. 取消 MCPTool 后台任务(P11)
        if self._mcp_tool:
            try:
                self._mcp_tool.cancel_background_tasks()
            except Exception as e:
                logger.warning(
                    f"会话[{self._session_id}]取消 MCPTool 后台异步任务失败(不影响主流程): {e}"
                )

    async def invoke(self, message: Message) -> AsyncGenerator[BaseEvent, None]:
        """传递消息，运行流，在六中调用planner&react智能体组合完成任务并返回对应事件"""
        # 工具调用预算重置(project_memory: 新用户消息时重置会话级工具调用计数)
        # 放在 Flow 入口而非 BaseAgent.invoke,保持核心循环精简(符合 project_memory 硬约束)
        self._budget_tracker.reset()
        # Batch 39 / 方向2: 按任务类型动态调整预算(研究类+deep_research,数据分析类+search_web)
        # Batch 40 / 方向2: 集成 ExperimentResolver, A/B 实验分组驱动预算调整
        # Batch 40 / 方向4: LLM 增强 — 关键词未命中时调用 LLM 1-token 分类(3层降级)
        # P4-5: 改用planner轻量LLM(thinking=disabled),任务分类无需high reasoning,节省~20s/会话
        task_type = await classify_with_llm(message.message, llm=self._planner_llm)
        self._experiment_group, adjustments = self._experiment_resolver.resolve(self._session_id)
        self._budget_tracker.adjust_for_task_type(task_type, adjustments=adjustments)
        if task_type != "general":
            logger.info(
                f"会话[{self._session_id}]任务类型[{task_type}],实验组[{self._experiment_group}],"
                f"已动态调整工具预算"
            )
        # Batch 30: 重置连续跳过计数器(新用户消息触发新计划周期)
        self._consecutive_skipped_updates = 0
        # 批次45 P0-2: 保存任务类型供附件门禁判断 + 重置门禁重试计数(新用户消息触发新周期)
        self._current_task_type = task_type
        self._deliverable_retry_count = 0

        # 延迟注入MCP工具摘要: MCPTool在AgentTaskRunner.invoke()中initialize后才填充工具列表,
        # 此处确保在首次Planner调用前注入摘要(仅成功注入一次)
        # MCP工具直接加载: ReAct阶段通过F10-6按步骤关键词自动装配MCP工具schema
        if not self._mcp_summary_injected:
            mcp_summary = self._mcp_tool.get_tools_summary()
            if mcp_summary:
                self._mcp_summary_injected = True
                self.planner._system_prompt += (
                    f"\n\n[MCP工具列表]\n{mcp_summary}\n"
                    f"请在步骤描述中引用需要的MCP工具名,"
                    f"ReAct执行阶段会按步骤关键词自动装配对应MCP工具schema。"
                )

        # 延迟注入A2A能力摘要: A2ATool在AgentTaskRunner.invoke()中initialize后才有agent cards,
        # 此处确保在首次Planner调用前注入A2A能力摘要(仅成功注入一次),让Planner能规划委托远程Agent
        # 修复: 仅当get_capability_hint()返回非None时才置_a2a_hint_injected=True,
        # 避免A2A服务未加载完成时标志位被提前置True导致后续agent cards加载后不再注入
        if not self._a2a_hint_injected:
            a2a_hint = self._a2a_tool.get_capability_hint()
            if a2a_hint:
                self._a2a_hint_injected = True  # 仅成功注入时置True
                self.planner._system_prompt += (
                    f"\n\n[A2A远程Agent列表]\n{a2a_hint}\n"
                    f"当任务可委托给远程Agent完成时,在步骤描述中明确"
                    f"'使用call_remote_agent委托XXX Agent完成XXX',"
                    f"ReAct执行阶段通过get_remote_agent_cards查看详情→call_remote_agent调用。"
                )

        async with self._uow_factory() as uow:
            session = await uow.session.get_by_id(self._session_id)
        if not session:
            raise ValueError(f"会话[{self._session_id}]不存在, 请核实后尝试")

        if session.status != SessionStatus.PENDING:
            logger.debug(f"会话[{self._session_id}]未处于空闲状态，回滚数据确保消息列表格式正确")
            await self.planner.roll_back(message)
            await self.react.roll_back(message)

            logger.info(f"会话[{self._session_id}]续接，执行预压缩")
            await self.planner.compact_memory()
            await self.react.compact_memory()

            await self._strip_historical_images()
            await self._inject_key_facts()
            await self._inject_deliverables_context(session)

        if session.status == SessionStatus.RUNNING:
            logger.debug(f"会话[{self._session_id}]处于运行状态并传递了新消息")
            self.status = FlowStatus.PLANNING

        if session.status == SessionStatus.WAITING:
            logger.debug(f"会话[{self._session_id}]处于等待状态并传递了新消息")
            self.status = FlowStatus.EXECUTING

        async with self._uow_factory() as uow:
            await uow.session.update_status(self._session_id, SessionStatus.RUNNING)

        self.plan = session.get_latest_plan()
        logger.info(f"Planner&ReAct流接收消息: {message.message[:50]}...")

        await self._detect_and_inject_attachment_skills(message)

        step = None

        while True:
            if self.status == FlowStatus.IDLE:
                logger.info(f"Planner&ReAct流状态从{FlowStatus.IDLE}变成{FlowStatus.PLANNING}")
                self.status = FlowStatus.PLANNING
            elif self.status == FlowStatus.PLANNING:
                logger.info("Planner&ReAct流开始创建计划/Plan")

                async for event in self.planner.create_plan(message):
                    if isinstance(event, PlanEvent) and event.status == PlanEventStatus.CREATED:
                        self.plan = event.plan
                        logger.info(f"Planner&ReAct流成功创建计划, 共计: {len(event.plan.steps)} 步")

                        yield TitleEvent(title=event.plan.title)
                        yield MessageEvent(role="assistant", message=event.plan.message)

                    yield event

                logger.info(f"Planner&ReAct流状态从{FlowStatus.PLANNING}变成{FlowStatus.EXECUTING}")
                self.status = FlowStatus.EXECUTING

                if not self.plan or len(self.plan.steps) == 0:
                    logger.info("Planner&ReAct流创建计划失败或无子步骤")
                    self.status = FlowStatus.COMPLETED
            elif self.status == FlowStatus.EXECUTING:
                self.plan.status = ExecutionStatus.RUNNING

                step = self.plan.get_next_step()

                if not step:
                    logger.info(f"Planner&ReAct流状态从{FlowStatus.EXECUTING}变成{FlowStatus.SUMMARIZING}")
                    self.status = FlowStatus.SUMMARIZING
                    continue

                logger.info(f"Planner&ReAct流开始执行步骤 {step.id}: {step.description[:50]}...")
                # F10-9可观测性: 步骤执行计数(含重试)
                if self._metrics_collector:
                    self._metrics_collector.increment("step_count")
                    if step.retry_count > 0:
                        self._metrics_collector.increment("step_retry_count")
                async for event in self.react.execute_step(self.plan, step, message):
                    yield event

                logger.info(f"压缩{self.react.name} Agent记忆/上下文")
                # P4-5优化: 步骤后条件化压缩 — 低token压力时跳过compact_memory
                # 原逻辑: 每步骤无条件compact_memory+save_memory,低压力时数据库IO浪费~50-100ms/次
                # 新逻辑: compact_memory_if_needed内部基于predict_token_pressure判断,
                #         safe级别跳过,moderate/high/critical才执行压缩
                await self.react.compact_memory_if_needed()

                # 步骤完成后调用Planner更新计划
                # 例外1: 迭代溢出失败时跳过update_plan,直接进入总结(LLM已无法自主脱困,调用无意义)
                # 例外2(Batch 30): 顺序独立步骤成功且产出未被后续引用时,跳过update_plan节省LLM调用
                if step.status == ExecutionStatus.FAILED and step.error \
                        and _NON_RETRYABLE_ERROR_MARKER in step.error:
                    logger.warning(f"步骤[{step.id}]因迭代溢出失败,跳过计划更新,直接进入总结")
                    self.status = FlowStatus.SUMMARIZING
                elif should_skip_update_plan(step, self.plan, self._consecutive_skipped_updates):
                    # Batch 30: 条件化跳过 — 成功且产出未被引用且未达安全网上限
                    self._consecutive_skipped_updates += 1
                    logger.info(
                        f"步骤[{step.id}]完成且产出未被后续引用,跳过update_plan"
                        f"(连续跳过{self._consecutive_skipped_updates}/{MAX_CONSECUTIVE_SKIPS})"
                    )
                    self.status = FlowStatus.EXECUTING  # 直接执行下一步
                else:
                    # 必须更新: COMPLETED(产出被引用/安全网) 或 FAILED(非溢出,需恢复决策)
                    self._consecutive_skipped_updates = 0  # 重置连续跳过计数
                    self.status = FlowStatus.UPDATING
            elif self.status == FlowStatus.UPDATING:
                logger.info("Planner&ReAct流开始更新计划")
                async for event in self.planner.update_plan(self.plan, step):
                    yield event

                logger.info(f"Planner&ReAct流状态从{FlowStatus.UPDATING}变成{FlowStatus.EXECUTING}")
                self.status = FlowStatus.EXECUTING
            elif self.status == FlowStatus.SUMMARIZING:
                logger.info("PlannerReAct流开始总结")

                # 交付物智能选择 — 从全量文件中筛选与最终答案相关的文件
                all_files = await self._get_session_file_paths()

                # 批次46: 始终扫描沙箱并合并(治本: shell_execute生成的文件不在session.files中)
                # 根因: LLM通过shell_execute运行脚本生成PPT/MD等交付物,这些文件不经过write_file,
                # 因此不被session.files追踪。原逻辑仅在session.files为空时扫描,会漏掉这些文件
                # (session.files有过程文件如.py脚本时不会触发扫描,导致真实交付物未被发现)。
                # 修复: 始终扫描沙箱,将扫描发现的新文件与session.files合并(去重保序),
                # 并通过SandboxScanEvent回传给Runner同步到OSS+DB
                scanned = await self._scan_sandbox_deliverables()
                if scanned:
                    existing_set = set(all_files)
                    new_files = [fp for fp in scanned if fp not in existing_set]
                    if new_files:
                        logger.info(
                            f"会话[{self._session_id}]批次46沙箱扫描生效,"
                            f"发现{len(new_files)}个未同步交付物: {new_files}"
                        )
                        # 通过事件回传给Runner并发同步到OSS+DB
                        yield SandboxScanEvent(file_paths=new_files)
                    # 合并: session.files + 沙箱扫描结果(去重保序,沙箱扫描结果优先)
                    all_files = list(dict.fromkeys(scanned + all_files))

                # 批次45 P0-2: 附件门禁 — data_analysis任务0文件时回退EXECUTING追加生成步骤
                # 触发条件: P0-1扫描后仍0文件 + 强制交付物任务类型 + 未超重试上限
                # 重试上限防死循环: LLM再次未生成则放行summarize(降级文本交付)
                if (not all_files
                        and self._current_task_type in _DELIVERABLE_REQUIRED_TASK_TYPES
                        and self._deliverable_retry_count < _DELIVERABLE_RETRY_MAX):
                    self._deliverable_retry_count += 1
                    logger.warning(
                        f"会话[{self._session_id}]P0-2附件门禁触发: "
                        f"任务类型[{self._current_task_type}]0交付物,"
                        f"回退EXECUTING追加生成步骤(第{self._deliverable_retry_count}次)"
                    )
                    yield MessageEvent(
                        role="assistant",
                        message="检测到尚未生成交付物文件,正在补充生成最终报告...",
                    )
                    # 追加确定性生成步骤(省一次update_plan LLM调用,描述已明确)
                    self.plan.steps.append(Step(description=_DELIVERABLE_GUIDANCE_STEP_DESC))
                    self.status = FlowStatus.EXECUTING
                    continue

                # SUMMARIZING 核心交付逻辑(终极异常兜底: 文件筛选/校验/summarize任一环节抛异常时,
                # 仍须交付已有文件,严禁0附件结束会话。根因: 历史会话因Step未导入/校验抛错/
                # summarize流式异常等导致整个SUMMARIZING块崩溃,异常传播到AgentTaskRunner._emit_degraded_summary,
                # 该方法attachments=[],导致用户"没有最终交付、没有返回交付内容及文件")
                try:
                    relevant_files = await self._get_relevant_files(all_files)
                    if relevant_files:
                        logger.info(f"交付物智能选择: {len(all_files)}个文件 → {len(relevant_files)}个相关")
                    else:
                        relevant_files = all_files

                    # F10-8 交付前校验清单 — 对LLM可见的交付物列表进行完整性/空文件/重复/过程文件/临时文件校验
                    # 校验通过的有效附件作为summarize的known_files,确保用户拿到的是真实可用的交付物
                    # 异常时不阻塞summarize(降级为原始声明,保证交付)
                    # 日志策略: 有过滤时输出完整INFO报告(运维需关注),通过时输出简化INFO(仅声明/有效数,避免噪音)
                    validation_result = await self._validate_deliverables(relevant_files)
                    if validation_result.total_filtered > 0:
                        logger.info(f"会话[{self._session_id}]交付物校验报告(有过滤): {validation_result.log_report()}")
                        # 用校验后的有效附件替换relevant_files,确保summarize仅向用户呈现真实可用的交付物
                        relevant_files = validation_result.valid_attachments if validation_result.valid_attachments else relevant_files
                    else:
                        logger.info(
                            f"会话[{self._session_id}]交付物校验通过: "
                            f"声明={validation_result.total_declared}, 有效={validation_result.total_valid}"
                        )

                    # summarize失败兜底 — 捕获ErrorEvent，构造基于步骤结果的兜底交付
                    # 根因: 流式_invoke_llm_stream空输出 + 降级_invoke_llm空内容/抛RuntimeError时，
                    # summarize会yield ErrorEvent，导致用户完全得不到交付。此处拦截ErrorEvent，
                    # 用最后完成步骤的result构造兜底MessageEvent(is_final=True)，保障交付质量不降级。
                    summarize_failed = False
                    # 批次46: 跟踪summarize是否产出了带附件的最终事件
                    # 根因: summarize可能因LLM返回空attachments或流式事件未写DB,
                    # 导致最终交付消息attachments为空。此处跟踪并在必要时兜底。
                    delivered_with_attachments = False
                    async for event in self.react.summarize(known_files=relevant_files):
                        if isinstance(event, ErrorEvent):
                            logger.warning(f"summarize返回ErrorEvent，启用兜底交付: {event.error}")
                            summarize_failed = True
                            continue
                        # 仅最终答案事件（非流式）需要附件兜底，流式delta不携带附件
                        if isinstance(event, MessageEvent) and not getattr(event, "is_streaming", False):
                            if not event.attachments and relevant_files:
                                # HTML安全网: HTML文件仅在LLM显式声明时交付,不自动填充
                                auto_fill = self._filter_html_from_auto_fill(relevant_files)
                                logger.warning(f"汇总结果attachments为空，自动补充{len(auto_fill)}个相关文件")
                                event.attachments = [File(filepath=fp) for fp in auto_fill]
                            # 标记已产出带附件的最终事件(非空attachments视为有效交付)
                            if event.attachments:
                                delivered_with_attachments = True
                        yield event

                    # 兜底交付: summarize失败时，基于已完成的步骤结果构造最终回复，保障用户始终获得交付
                    if summarize_failed:
                        fallback_message = self._build_fallback_summary()
                        fallback_attachments = [File(filepath=fp) for fp in self._filter_html_from_auto_fill(relevant_files)]
                        logger.warning(
                            f"summarize失败兜底生效，构造兜底交付: "
                            f"消息长度={len(fallback_message)}, 附件数={len(fallback_attachments)}"
                        )
                        yield MessageEvent(
                            role="assistant",
                            message=fallback_message,
                            attachments=fallback_attachments,
                            is_streaming=False,
                            is_final=True,
                        )
                    elif relevant_files and not delivered_with_attachments:
                        # 批次46: summarize成功但未产出带附件的最终事件时,补发交付
                        # 根因: summarize可能仅产出流式事件(is_streaming=True,不写DB),
                        # 或最终事件attachments为空(LLM未返回attachments且known_files为空)。
                        # 此处确保用户始终获得附件交付。
                        fallback_message = self._build_fallback_summary()
                        fallback_attachments = [File(filepath=fp) for fp in self._filter_html_from_auto_fill(relevant_files)]
                        logger.warning(
                            f"会话[{self._session_id}]summarize未产出带附件事件,补发交付: "
                            f"附件数={len(fallback_attachments)}"
                        )
                        yield MessageEvent(
                            role="assistant",
                            message=fallback_message,
                            attachments=fallback_attachments,
                            is_streaming=False,
                            is_final=True,
                        )
                except Exception as summarize_err:
                    # 终极兜底: 文件筛选/校验/summarize任一环节抛异常时,仍交付已有文件
                    # 防止异常传播到AgentTaskRunner._emit_degraded_summary(该方法attachments=[]导致0附件)
                    logger.exception(
                        f"会话[{self._session_id}]SUMMARIZING阶段异常,启用终极兜底交付: {summarize_err}"
                    )
                    fallback_message = self._build_fallback_summary()
                    fallback_attachments = [File(filepath=fp) for fp in self._filter_html_from_auto_fill(all_files)]
                    logger.warning(
                        f"SUMMARIZING终极兜底生效: "
                        f"消息长度={len(fallback_message)}, 附件数={len(fallback_attachments)}"
                    )
                    yield MessageEvent(
                        role="assistant",
                        message=fallback_message,
                        attachments=fallback_attachments,
                        is_streaming=False,
                        is_final=True,
                    )

                logger.info(f"PlannerReAct流状态从{FlowStatus.SUMMARIZING}变成{FlowStatus.COMPLETED}")
                self.status = FlowStatus.COMPLETED
            elif self.status == FlowStatus.COMPLETED:
                self.plan.status = ExecutionStatus.COMPLETED
                self.status = FlowStatus.IDLE
                yield PlanEvent(status=PlanEventStatus.COMPLETED, plan=self.plan)
                break
        yield DoneEvent()
        logger.info("Planner&ReAct流处理任务消息已完毕")

    @property
    def done(self) -> bool:
        """只读属性，返回流是否运行结束"""
        return self.status == FlowStatus.IDLE

    async def _strip_historical_images(self) -> None:
        """清理Agent历史记忆中的图片base64数据

        批次 28 修复: BaseAgent 只有 _uow_factory,不存在 _uow 属性。
        原代码 `async with agent._uow` 在 PlannerAgent 上触发 AttributeError,
        导致续接会话(用户发送"继续")时整个流程崩溃。
        修复为通过 _uow_factory() 创建临时 uow,与 BaseAgent 既有模式(L117)对齐。
        """
        for agent in [self.planner, self.react]:
            await agent._ensure_memory()
            if agent._memory and agent._memory.messages:
                agent._memory.messages = Memory.strip_image_data(agent._memory.messages)
                async with agent._uow_factory() as uow:
                    await uow.session.save_memory(agent._session_id, agent.name, agent._memory)
                agent._memory.mark_clean()

    async def _inject_key_facts(self) -> None:
        """将关键事实注入Agent系统提示（Hermes Agent策展式记忆核心机制）"""
        for agent in [self.planner, self.react]:
            await agent._ensure_memory()
            if agent._memory and agent._memory.messages:
                agent._memory.extract_key_facts()
                summary = agent._memory.get_summary_for_injection()
                if summary and agent._memory.messages:
                    if len(summary) > _MAX_INJECT_LENGTH:
                        summary = summary[:_MAX_INJECT_LENGTH] + "\n...(摘要已截断)"
                    await self._append_to_system_prompt(agent, "[历史操作摘要]", f"\n\n[历史操作摘要]\n{summary}")

    async def _inject_deliverables_context(self, session) -> None:
        """将历史交付物文件列表注入Agent系统提示，防止续接会话时丢失附件上下文"""
        delivered_files = []
        for f in session.files:
            if f.filepath:
                entry = f.filepath
                if f.key:
                    oss_key = f.key if len(f.key) <= _MAX_OSS_KEY_DISPLAY else f.key[:_MAX_OSS_KEY_DISPLAY - 3] + "..."
                    entry += f" (OSS: {oss_key})"
                delivered_files.append(entry)

        if not delivered_files:
            return

        if len(delivered_files) > _MAX_DELIVERABLE_FILES:
            delivered_files = delivered_files[:_MAX_DELIVERABLE_FILES]

        file_list_text = "\n".join(f"  - {fp}" for fp in delivered_files)
        context_block = (
            f"\n\n[历史交付物文件]\n"
            f"以下文件已在之前的对话中生成并交付，请直接读取使用，不要重新生成:\n"
            f"{file_list_text}\n"
            f"注意：如果沙箱文件不存在（沙箱可能已重建），请使用OSS URL重新下载到沙箱后再读取。"
        )
        await self._append_to_system_prompt(self.planner, "[历史交付物文件]", context_block)
        await self._append_to_system_prompt(self.react, "[历史交付物文件]", context_block)

    def _build_fallback_summary(self) -> str:
        """summarize失败时的兜底交付 - 简化版

        策略: 直接列出已完成步骤的result，不做Markdown拼接
        (信任步骤result本身已是LLM生成的结构化文本)
        """
        if not self.plan or not self.plan.steps:
            return "任务已执行完成，AI模型汇总遇到异常。请查看执行步骤与生成的文件。"

        completed = [s for s in self.plan.steps
                     if s.status == ExecutionStatus.COMPLETED and s.success and s.result]
        if not completed:
            return "任务已执行完成，AI模型汇总遇到异常。请查看执行步骤与生成的文件。"

        # 简单拼接: 最后一步result + 文件交付提示
        last = completed[-1]
        parts = [last.result.strip()]
        if len(completed) > 1:
            parts.append(f"\n\n(共完成{len(completed)}个步骤)")
        parts.append("\n\n请查看本次会话生成的文件附件获取完整交付内容。")
        return "\n".join(parts)

    async def _scan_sandbox_deliverables(self) -> list[str]:
        """主动扫描沙箱交付物(批次45 P0-1)

        不依赖 LLM 声明(attachments)或 step.result 文本,直接以沙箱文件系统为真相源。
        遍历交付物扩展名 glob 模式调用 sandbox.find_files,过滤中间产物后返回去重路径。

        触发场景: SUMMARIZING 阶段 session.files 为空时(文件同步断裂兜底)。
        异常容错: 单个glob失败降级跳过;整体异常返回空(不阻塞summarize)。

        Returns:
            去重后的交付物沙箱路径列表(可能为空),上限 _SANDBOX_SCAN_MAX_FILES
        """
        seen: set = set()
        deliverables: list[str] = []
        try:
            for pattern in _SANDBOX_SCAN_GLOB_PATTERNS:
                try:
                    result = await self._sandbox.find_files(_SANDBOX_SCAN_ROOT, pattern)
                except Exception as e:
                    logger.debug(f"会话[{self._session_id}]沙箱扫描glob[{pattern}]失败,跳过: {e}")
                    continue
                if not result or not result.success or not result.data:
                    continue
                # find_files 返回 data 为文件路径列表
                paths = result.data if isinstance(result.data, list) else []
                for path in paths:
                    if not isinstance(path, str) or path in seen:
                        continue
                    # 过滤中间产物路径
                    if any(path.startswith(p) for p in _SANDBOX_SCAN_INTERMEDIATE_PREFIXES):
                        continue
                    seen.add(path)
                    deliverables.append(path)
                    if len(deliverables) >= _SANDBOX_SCAN_MAX_FILES:
                        return deliverables
        except Exception as e:
            logger.warning(f"会话[{self._session_id}]沙箱交付物扫描整体失败(降级返回空): {e}")
            return []
        return deliverables

    async def _get_session_file_paths(self) -> list[str]:
        """查询session.files获取所有已生成文件的沙箱路径列表"""
        try:
            async with self._uow_factory() as uow:
                session = await uow.session.get_by_id(self._session_id)
            if not session or not session.files:
                return []
            return [f.filepath for f in session.files if f.filepath]
        except Exception as e:
            logger.warning(f"查询session文件路径失败: {e}")
            return []

    @staticmethod
    def _is_temp_file(filepath: str) -> bool:
        """判断是否为临时文件(按扩展名过滤)

        F10-8: 委托给FilePresentationService.is_excluded_file,实现规则集中化。
        保留静态方法签名向后兼容(test_deliverable_selection.py等外部调用不破坏)。
        使用模块级默认实例(_DEFAULT_FILE_PRESENTATION),配置与FilePresentationConfig默认值一致。
        """
        return _DEFAULT_FILE_PRESENTATION.is_excluded_file(filepath)

    async def _get_relevant_files(self, all_files: list[str]) -> list[str]:
        """筛选交付物文件(F10-8: 委托给FilePresentationService.select_deliverable_files)

        设计理念(参考5b54ddc): 信任LLM,代码层仅做类型过滤+截断,
        交付质量完全由提示词优化驱动,不引入评分或分组等复杂逻辑。

        策略:
        1. 类型过滤: 排除excluded_extensions中的临时文件(.tmp/.log等)
        2. 截断: 超过max_deliverable_files时取最近N个(按列表末尾)

        F10-8: 实际逻辑已迁移至FilePresentationService.select_deliverable_files,
        此处仅做委托,保持调用点不变(向后兼容)。
        getattr兜底: 测试中patch.object跳过__init__时,使用模块级默认实例,避免AttributeError。
        """
        file_presentation = getattr(self, '_file_presentation', None) or _DEFAULT_FILE_PRESENTATION
        return file_presentation.select_deliverable_files(all_files)

    @staticmethod
    def _filter_html_from_auto_fill(filepaths: list[str]) -> list[str]:
        """HTML自动填充安全网: HTML文件仅在LLM显式声明时交付,不自动填充

        防止浏览器操作/web_search等任务中生成的HTML中间产物(如保存的网页)被
        自动填充为交付物。HTML文件作为合法交付物时应由LLM在summarize中显式声明,
        而非由自动填充逻辑兜底交付。

        策略:
        - 优先返回非HTML文件列表(确保常规交付物优先自动填充)
        - 当文件列表全部为HTML时(用户明确要求HTML交付物场景),返回原列表
          避免因安全网导致0附件结束会话

        Args:
            filepaths: 待自动填充的文件路径列表

        Returns:
            过滤HTML后的文件路径列表(全为HTML时返回原列表)
        """
        non_html = [fp for fp in filepaths if not fp.lower().endswith((".html", ".htm"))]
        return non_html if non_html else filepaths

    async def _validate_deliverables(self, declared_attachments: list[str]) -> DeliveryValidationResult:
        """交付物交付前校验清单(F10-8)

        委托给FilePresentationService.validate_deliverables,对LLM声明的attachments
        进行完整性/空文件/重复/过程文件/临时文件五项校验,产出量化指标(命中率/过滤率)。

        异常容错: 读取session.files失败时返回原始声明(不剔除),保证不阻塞summarize主流程。
        设计原则: 校验为增强项,不应阻塞交付链路;异常时降级为"全部声明有效"。

        Args:
            declared_attachments: 待校验的附件路径列表(通常来自_get_relevant_files筛选结果)

        Returns:
            DeliveryValidationResult: 校验结果(含有效附件列表+过滤统计+命中率)
        """
        # 读取session.files获取File对象列表(含size/sync_status,用于完整性/空文件校验)
        try:
            async with self._uow_factory() as uow:
                session = await uow.session.get_by_id(self._session_id)
            session_files = session.files if (session and session.files) else []
        except Exception as e:
            logger.warning(
                f"会话[{self._session_id}]读取session.files用于交付校验失败,跳过校验(降级为全部有效): {e}"
            )
            return DeliveryValidationResult(
                valid_attachments=list(declared_attachments),
                total_declared=len(declared_attachments),
                total_valid=len(declared_attachments),
            )
        return self._file_presentation.validate_deliverables(declared_attachments, session_files)

    @staticmethod
    async def _append_to_system_prompt(agent, marker: str, content: str) -> None:
        """向Agent已有memory的系统提示追加内容（幂等，marker已存在时跳过）

        用于续接会话场景：Agent的memory已从_system_prompt初始化，需通过此方法回填注入内容。
        首次会话时memory为空，此方法自动跳过（由_system_prompt通道覆盖）。

        批次 28 修复: 与 _strip_historical_images 同样的 AttributeError 问题,
        BaseAgent 不存在 _uow 属性,改用 _uow_factory() 创建临时 uow。
        """
        if not agent._memory or not agent._memory.messages:
            return
        system_msg = agent._memory.messages[0]
        existing = system_msg.get("content", "")
        if marker not in existing:
            system_msg["content"] = existing + content
        async with agent._uow_factory() as uow:
            await uow.session.save_memory(agent._session_id, agent.name, agent._memory)
        agent._memory.mark_clean()

    async def _detect_and_inject_attachment_skills(self, message: Message) -> None:
        """根据用户附件自动检测相关技能，主动注入技能提示和操作指南

        双通道注入策略:
        1. _system_prompt通道: 首次会话时Agent从_system_prompt初始化memory，必须注入
        2. memory通道: 续接会话时memory已存在，需通过_append_to_system_prompt回填
        两个通道均做marker幂等检查，防止重复注入

        注入内容:
        - PlannerAgent: 技能名称+描述+附件路径（用于规划步骤时引用技能）
        - ReActAgent: 技能指南全文+附件路径+脚本路径+强制约束（用于执行时参考操作步骤）
        """
        if not message.attachments:
            return
        try:
            valid_paths = [fp for fp in message.attachments if fp and "." in fp]
            if not valid_paths:
                logger.warning(f"附件技能检测: 所有附件路径无效或无扩展名: {message.attachments}")
                return

            matched_skills = await self._skill_service.detect_skills_from_attachments(valid_paths)
            if not matched_skills:
                logger.info(f"附件技能检测: 未匹配到技能, 附件: {valid_paths}")
                return

            attachment_list = "\n".join(f"  - {fp}" for fp in message.attachments)
            for skill in matched_skills:
                skill_marker = f"[附件技能提示: {skill.name}]"
                guide_marker = f"[技能指南: {skill.name}]"

                # 向Planner注入技能提示（名称+描述，用于规划步骤引用）
                planner_hint = (
                    f"\n\n{skill_marker}\n"
                    f"用户附件涉及技能\"{skill.name}\"({skill.description})。"
                    f"请在步骤描述中引用此技能，例如\"使用{skill.name}技能读取文件\"。\n"
                    f"用户附件文件:\n{attachment_list}"
                )
                if skill_marker not in self.planner._system_prompt:
                    self.planner._system_prompt += planner_hint
                await self._append_to_system_prompt(self.planner, skill_marker, planner_hint)

                # 构建技能脚本路径提示
                scripts_hint = await self._build_scripts_path_hint(skill)

                # 向ReAct注入技能指南（全文+脚本路径+强制约束，用于执行时参考）
                guide = await self._skill_service.get_skill_guide(skill.name)
                if guide:
                    truncated_guide = SkillService.truncate_guide_for_injection(guide)
                    guide_block = (
                        f"\n\n{guide_marker}\n"
                        f"⚠️ 此指南已注入上下文，必须按指南操作，无需再调用get_skill_guide。\n\n"
                        f"{truncated_guide}"
                    )
                    if guide_marker not in self.react._system_prompt:
                        self.react._system_prompt += guide_block
                    await self._append_to_system_prompt(self.react, guide_marker, guide_block)
                    react_hint = (
                        f"\n\n{skill_marker}\n"
                        f"⚠️ 用户附件涉及技能\"{skill.name}\"，相关指南已注入上下文{guide_marker}。\n"
                        f"**必须优先按指南中的方法操作，禁止自行安装替代工具(如pip install/apt-get install)。**\n"
                        f"{scripts_hint}\n"
                        f"用户附件文件:\n{attachment_list}"
                    )
                else:
                    react_hint = (
                        f"\n\n{skill_marker}\n"
                        f"用户附件涉及技能\"{skill.name}\"({skill.description})，请调用get_skill_guide获取操作指南后执行。\n"
                        f"{scripts_hint}\n"
                        f"用户附件文件:\n{attachment_list}"
                    )
                if skill_marker not in self.react._system_prompt:
                    self.react._system_prompt += react_hint
                await self._append_to_system_prompt(self.react, skill_marker, react_hint)

            logger.info(f"附件技能检测: 匹配到{len(matched_skills)}个技能: {[s.name for s in matched_skills]}")
        except Exception as e:
            logger.warning(f"附件技能检测与注入失败(不影响主流程): {e}")

    async def _build_scripts_path_hint(self, skill: Skill) -> str:
        """构建技能脚本在沙箱中的路径提示，引导Agent使用技能脚本而非自行安装工具"""
        scripts_dir = os.path.join(skill.path, "scripts")
        if not os.path.isdir(scripts_dir):
            return ""
        script_paths = []
        for root, dirs, files in os.walk(scripts_dir):
            for filename in files:
                local_path = os.path.join(root, filename)
                rel_path = os.path.relpath(local_path, skill.path).replace("\\", "/")
                sandbox_path = f"/home/ubuntu/{rel_path}"
                script_paths.append(sandbox_path)
        if not script_paths:
            return ""
        path_list = "\n".join(f"  - {p}" for p in script_paths)
        return (
            f"技能脚本(已在沙箱中可用，直接执行即可):\n{path_list}\n"
            f"示例: `python3 {script_paths[0]}`"
        )
