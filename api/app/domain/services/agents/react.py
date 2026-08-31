#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/21 10:26

@File    : react.py
"""
import logging
import re
from typing import AsyncGenerator, List, Optional

from app.domain.models.event import (
    StepEventStatus,
    StepEvent,
    ToolEvent,
    MessageEvent,
    ErrorEvent,
    ToolEventStatus,
    WaitEvent,
    BaseEvent
)
from app.domain.models.file import File
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.services.prompts.react import REACT_SYSTEM_PROMPT, EXECUTION_PROMPT, SUMMARIZE_PROMPT
from app.domain.services.prompts.system import SYSTEM_PROMPT
from .base import BaseAgent
from ._batch_verifier import verify_batch_completeness, get_consolidation_guidance
from ._tool_call_verifier import step_requires_tool_call, build_missing_tool_error

logger = logging.getLogger(__name__)

# 失败恢复策略常量
_MAX_RETRY_COUNT = 2  # 最大自动重试次数(不含首次执行)
_NON_RETRYABLE_ERROR_MARKERS = ("迭代超过最大迭代次数",)  # 不可重试的错误标记(会导致无限循环)

# 研究类步骤关键词(比 _TOOL_KEYWORD_MAP["deep_research"] 更宽泛,召回优先)
# 新增"研究"/"分析"独立词,覆盖"研究AI趋势"/"分析市场数据"等措辞偏差场景
# 误判代价仅为多装配一个工具(token开销微增),漏判代价是deep_research不可见
_RESEARCH_KEYWORDS: tuple = (
    "深度研究", "deep_research", "调研", "深度搜索", "深度分析",
    "深度调研", "深入研究", "综合研究", "趋势研究", "全面分析",
    "多角度分析", "深度挖掘", "research", "deep search",
    "研究", "分析",  # 扩展: 独立词召回
)

# 专业能力关键词默认值(F2-4外置): 当AgentConfig未提供special_capability_keywords时使用
# 与AgentConfig.special_capability_keywords默认值保持一致,保证向后兼容
_DEFAULT_SPECIAL_CAPABILITY_KEYWORDS: tuple = (
    # 多模态能力
    "图片", "图像", "多模态", "ocr", "语音", "视频", "视觉",
    "image", "vision", "speech", "video",
    # 专业领域服务(通用能力类别,非特定供应商,通常由MCP工具提供)
    "天气", "weather", "地图", "map", "位置", "location",
    "翻译", "translate", "汇率", "exchange",
)

# 内置工具与能力标记(步骤已指定时跳过MCP调用引导,避免与内置工具优先原则冲突)
# 与 TOOL_SELECTION_GUIDE_CN / BUILT_IN_CAPABILITY_CN 提示词对齐:
#   - 内置研究/搜索工具直接可用,无需额外注入MCP调用引导
#   - 内置编程能力库由沙箱预装,走 shell_execute,非 MCP 专业能力
#   - 已知 MCP 工具名(含 mcp_ 前缀)说明规划阶段已确认工具存在,跳过引导
# 设计动机: AI 趋势研究步骤"使用 deep_research 研究...多模态/视觉方向"中,
# "多模态/视觉"命中专业能力关键词,误注入"必须优先使用MCP工具",
# 与步骤已指定的内置工具 deep_research 冲突,导致 LLM 困惑空转(见 思考.md)。
_BUILT_IN_TOOL_MARKERS: tuple = (
    # 内置研究/搜索工具(步骤已指定时,该能力无需 MCP 调用引导)
    "deep_research", "search_web",
    # 内置编程能力库(沙箱预装,走 shell_execute,非 MCP 专业能力)
    "python-docx", "openpyxl", "python-pptx", "pdfplumber", "reportlab",
    "pandas", "numpy", "matplotlib", "seaborn", "scipy", "scikit-learn",
    "pillow", "beautifulsoup4", "requests",
)
_MCP_TOOL_NAME_PREFIX: str = "mcp_"  # 已知 MCP 工具名前缀,规划阶段已确认,跳过调用引导

# 提问文本识别(P0-8): 检测文本是否为真正的提问而非声明性通知
# 防御LLM误用message_ask_user发送声明性文本(如"已完成图片识别与天气查询"),
# 导致会话永久卡在WAITING状态(用户无问题可回复)
# 中文提问标志词:疑问语气词+疑问代词+请求输入指令
_CN_QUESTION_MARKERS: tuple = (
    "吗", "呢", "啥", "么",  # 疑问语气词
    "什么", "怎么", "怎样", "如何", "是否", "能否", "能不能",  # 疑问代词/副词
    "哪个", "哪些", "哪里", "哪儿", "多少", "为什么", "为何", "谁", "何时",  # 疑问代词
    "是不是", "对吗", "行吗", "可以吗", "好不好",  # 正反问句
    "请输入", "请提供", "请选择", "请确认", "请告诉我", "请回复", "请说明",  # 请求输入指令
    "需要您", "请问您", "麻烦您", "希望您",  # 礼貌请求输入
)
# 英文提问标志词(用词边界匹配避免误匹配如"whatever"包含"what")
_EN_QUESTION_PATTERN: re.Pattern = re.compile(
    r'\b(what|how|why|when|where|who|which|whose|whom|'
    r'could you|would you|can you|do you|are you|is it|'
    r'should|may i|shall|please enter|please provide|'
    r'please select|please confirm|please tell|please reply)\b',
    re.IGNORECASE,
)


def _is_likely_question(text: str) -> bool:
    """检测文本是否为提问而非声明性通知

    通过提问标志词和问号标点判断,覆盖中英文常见提问模式。
    用于防御LLM误用message_ask_user发送声明性通知(如"已完成xxx查询"),
    导致会话永久卡在WAITING状态(会话93b442bd根因)。

    判定逻辑(任一命中即为提问):
    1. 包含问号(最可靠信号)
    2. 包含中文提问标志词
    3. 匹配英文提问标志词模式

    Args:
        text: message_ask_user的text参数

    Returns:
        True表示文本是提问,应触发WaitEvent等待用户回复
        False表示文本是声明性通知,应跳过WaitEvent继续执行
    """
    if not text:
        return False
    text = text.strip()
    if not text:
        return False
    # 1. 问号判定(最可靠)
    if "？" in text or "?" in text:
        return True
    # 2. 中文提问标志词判定
    if any(marker in text for marker in _CN_QUESTION_MARKERS):
        return True
    # 3. 英文提问标志词模式匹配
    if _EN_QUESTION_PATTERN.search(text):
        return True
    return False


class ReActAgent(BaseAgent):
    """基于ReAct架构的执行Agent"""
    name: str = "react"
    _system_prompt: str = SYSTEM_PROMPT + REACT_SYSTEM_PROMPT
    _format: str = "json_object"  # format控制的是content、工具调用控制的是tool_calls两者不冲突

    async def execute_step(self, plan: Plan, step: Step, message: Message) -> AsyncGenerator[BaseEvent, None]:
        """根据传递的消息+规划+子步骤，执行相应的子步骤(含失败自动重试)

        F10-6 工具按需装配: 执行步骤前注入步骤描述上下文,步骤完成后清理上下文。
        通过 try/finally 保证异常退出时也能清理,避免污染后续 PlannerAgent.invoke()
        的工具装配(PlannerAgent 始终使用全量工具构建摘要,不应受步骤上下文影响)。

        失败恢复策略:
        1. 步骤FAILED且错误可重试(非迭代溢出)时,自动重试,上限_Max_RETRY_COUNT次
        2. 重试时在query中注入失败原因,引导LLM尝试替代方案
        3. 迭代溢出或重试耗尽后返回失败,交由Planner决策恢复策略
        4. 首次执行未调用任何工具时,注入引导提示重试一次(工具调用保障)
        """
        # F10-6: 注入步骤描述上下文,供 _get_available_tools() 按需过滤
        self.set_step_context(step.description)
        # 同步合并引导激活状态到 ShellCallProfiler(方向3量化修复)
        # _build_execution_query 内 get_consolidation_guidance 会注入合并引导,
        # 此处同步画像器标志,使 guidance_triggered 指标真实反映引导是否实际注入,
        # 修复此前 set_guidance_active 未被调用导致 guidance_triggered 恒为 False 的缺陷
        if self._shell_profiler is not None:
            self._shell_profiler.set_guidance_active(
                bool(get_consolidation_guidance(step.description))
            )
        # 研究类步骤强制装配 deep_research 工具(F10-6关键词漏命中兜底)
        self._ensure_research_tool_assembled(step.description)
        try:
            async for event in self._execute_step_impl(plan, step, message):
                yield event
        finally:
            # 清理步骤上下文,避免污染后续 PlannerAgent.invoke() 调用
            self.reset_step_context()
            # 重置合并引导激活状态,避免污染后续步骤的 shell 调用画像
            if self._shell_profiler is not None:
                self._shell_profiler.set_guidance_active(False)

    def _ensure_research_tool_assembled(self, step_description: str) -> None:
        """研究类步骤强制装配 deep_research 工具(, F10-6兜底)

        F10-6 关键词过滤可能因措辞偏差漏命中研究类步骤(如步骤命中"搜索"
        但用户实际需要深度研究),导致 deep_research 被过滤掉。此方法在
        步骤执行前检测研究意图,强制注入 deep_research 到可用工具集。

        非侵入式: 仅扩展 _filter_tools_by_context 的保留逻辑,不改 invoke 循环。
        幂等: force_include_tool 使用集合存储,重复调用无副作用。
        """
        desc_lower = step_description.lower()
        if not any(kw in desc_lower for kw in _RESEARCH_KEYWORDS):
            return  # 非研究类步骤不干预
        self.force_include_tool("deep_research")

    async def _execute_step_impl(self, plan: Plan, step: Step, message: Message) -> AsyncGenerator[BaseEvent, None]:
        """execute_step 的实际实现(原逻辑)

        由 execute_step 包装器调用,负责步骤执行的核心逻辑(含失败重试)。
        拆分为包装器+实现方法是为了让上下文管理(set/reset_step_context)
        与业务逻辑解耦,保证 try/finally 的可靠性。

        F10-5 进度状态扩展:
        - 维护步骤内工具调用计数器,每次工具 CALLED 后产出进度事件
        - 进度估算公式 _estimate_step_progress:前几次工具调用快速推进,
          后续渐近收敛(避免无限接近100误导用户认为即将完成)
        - 失败重试时不重置计数器(累计推进,反映已完成的工具调用)
        """
        # 1.构建初始执行query
        # F2-4外置: 专业能力关键词从AgentConfig注入,运维可通过config.yaml调整
        query = ReActAgent._build_execution_query(
            plan, step, message,
            special_capability_keywords=self._agent_config.special_capability_keywords,
        )

        # 2.更新步骤的执行状态为运行中并返回Step事件
        step.status = ExecutionStatus.RUNNING
        yield self._make_step_event(step, StepEventStatus.STARTED, progress=0)

        # 3.执行(含自动重试循环)
        # F10-5: 工具调用计数器(步骤级累计,重试不重置)
        tool_call_count = 0
        no_tool_retry_done = False  # 首次无工具调用重试标志(仅重试一次)
        force_tool_choice = None  # 重试时强制工具选择(多模态步骤LLM在JSON模式下不产出tool_calls时使用)
        # 工具调用幻觉防护: 步骤级工具调用跟踪(跨重试累计)
        # 用于重试后仍无工具调用时,对动作类步骤强制标记 FAILED,拒绝 LLM 幻觉结果
        step_tool_called = False
        # 进度去重: 跟踪上次产出的进度值,避免 progress 触顶后产出冗余 StepEvent
        # 长步骤(如130次工具调用)在 progress 达到上限(90%)后,每次工具 CALLED 都会
        # 产出相同 progress 的冗余事件,污染 SSE 流并造成 DB 持久化冗余。
        # 去重后仅产出进度变化事件,同时用于 FAILED 事件保持上次进度(避免进度条回退)。
        last_progress = 0
        while True:
            should_retry = False
            tool_called = False  # 跟踪本次执行是否调用了任何工具
            # format与tool_choice协同: 强制工具调用时覆盖format为text,
            # 解除json_object内容约束(该约束与tool_choice=required冲突,
            # 导致LLM倾向返回JSON content而非tool_calls,会话c4f138b6暴露)。
            # 非强制时format=None回退到Agent默认(json_object),保持步骤结果JSON解析。
            retry_format = "text" if force_tool_choice else None
            async for event in self.invoke(query, format=retry_format, tool_choice=force_tool_choice):
                if isinstance(event, ToolEvent):
                    tool_called = True
                    step_tool_called = True  # 步骤级累计(跨重试),用于幻觉防护门禁
                    if event.function_name == "message_ask_user":
                        # 提取提问文本,防御空文本调用(P4-6流式改造后LLM偶发产生空参数tool_call)
                        ask_text = (event.function_args.get("text") or "").strip()
                        if event.status == ToolEventStatus.CALLING:
                            if ask_text:
                                yield MessageEvent(role="assistant", message=ask_text)
                            else:
                                # 空文本调用不产出MessageEvent,避免空消息污染事件流
                                logger.warning(
                                    f"步骤[{step.id}]message_ask_user被调用但text为空,"
                                    f"跳过消息产出(参数: {event.function_args})"
                                )
                        elif event.status == ToolEventStatus.CALLED:
                            if ask_text and _is_likely_question(ask_text):
                                # 正常路径:有提问文本且语义为提问时进入WAITING等待用户回复
                                yield WaitEvent()
                                return
                            # 防御性修复(P0-8): 空文本或声明性文本时不yield WaitEvent
                            # 根因1: P4-6流式改造后,LLM偶发产生空参数的message_ask_user tool_call,
                            #        参数JSON解析失败降级为空字典{},导致text缺失。
                            # 根因2: LLM语义误用message_ask_user发送声明性通知(如"已完成图片识别与天气查询"),
                            #        而非真正的提问(会话93b442bd根因)。声明性文本不需要用户回复,
                            #        若yield WaitEvent,会话将因无问题内容而永久卡死。
                            # 修复: 跳过WaitEvent,让invoke循环继续,LLM将收到工具结果并重新生成响应。
                            logger.warning(
                                f"步骤[{step.id}]message_ask_user以非提问文本完成调用,"
                                f"跳过WaitEvent防止会话卡死"
                                f"(文本: {ask_text[:100] if ask_text else '空'})"
                            )
                        # 仅message_ask_user工具跳过后续yield(已转换为Message/Wait事件)
                        continue
                    # F10-5: 工具调用完成(CALLED)时产出进度事件,让前端展示步骤进度
                    # 仅对实际工具执行(非 message_ask_user)计数,避免询问用户场景误推进度
                    if event.status == ToolEventStatus.CALLED:
                        tool_call_count += 1
                        progress = ReActAgent._estimate_step_progress(tool_call_count)
                        # 进度去重: progress 值未变化时不产出新 StepEvent
                        # 长步骤在 progress 触顶(如90%)后,后续工具调用的 progress 不变,
                        # 跳过冗余事件产出,减少 SSE 推送和 DB 持久化负担
                        if progress != last_progress:
                            last_progress = progress
                            yield self._make_step_event(
                                step, StepEventStatus.STARTED,
                                progress=progress,
                                message=f"执行中(已完成{tool_call_count}次工具调用)",
                            )
                    # 其他ToolEvent fall through到yield event,供前端展示工具调用详情
                elif isinstance(event, MessageEvent) and not getattr(event, "is_thinking", False):
                    # 改进A: is_thinking 守卫 — 思考事件透传到末尾 yield event,不被解析为 Step JSON
                    # 工具调用保障: 首次未调用工具时注入引导重试一次
                    if not tool_called and not no_tool_retry_done:
                        logger.info(f"步骤[{step.id}]首次执行未调用工具,注入引导重试并强制工具调用")
                        no_tool_retry_done = True
                        query = ReActAgent._build_execution_query(
                            plan, step, message,
                            special_capability_keywords=self._agent_config.special_capability_keywords,
                        ) + ReActAgent._build_no_tool_retry_guidance(
                            step.description, self._agent_config.special_capability_keywords
                        )
                        # 重试时强制tool_choice="required"确保产出tool_calls
                        # LLM在json_object模式下倾向输出JSON content而非tool_calls,
                        # 对所有步骤(含非多模态)统一强制工具调用,防止LLM跳过工具直接返回结果
                        force_tool_choice = "required"
                        should_retry = True
                        break
                    # 工具调用幻觉防护门禁: 动作类步骤重试后仍无工具调用 → 拒绝幻觉结果
                    # 根因: LLM 在 json_object 模式下可能不产出 tool_calls,
                    # 直接在 content 返回 {"success": true, "attachments": [...]} 幻觉 JSON,
                    # 导致文件未实际创建却标记完成。此门禁拦截该场景,强制 FAILED。
                    # 认知类步骤(无动作类关键词)豁免,允许无工具调用完成。
                    if not step_tool_called and step_requires_tool_call(step.description):
                        logger.warning(
                            f"步骤[{step.id}]动作类步骤未调用任何工具即声明完成,"
                            f"拒绝幻觉结果(防止工具调用幻觉)"
                        )
                        step.status = ExecutionStatus.FAILED
                        step.success = False
                        step.error = build_missing_tool_error(step.description)
                        yield self._make_step_event(
                            step, StepEventStatus.FAILED,
                            progress=last_progress,
                            message="步骤未调用工具即声明完成,已拒绝(防止工具调用幻觉)",
                        )
                        yield ErrorEvent(error=step.error)
                        return
                    step.status = ExecutionStatus.COMPLETED
                    try:
                        parsed_obj = await self._json_parser.invoke(event.message)
                        new_step = Step.model_validate(parsed_obj)
                        step.success = new_step.success
                        step.result = new_step.result
                        step.attachments = new_step.attachments
                    except Exception as e:
                        logger.warning(f"步骤结果JSON解析失败，使用降级结果: {str(e)}")
                        # 降级时根据步骤级工具调用情况判断 success:
                        # - 有工具调用(step_tool_called=True): 执行已发生,允许降级成功
                        # - 无工具调用且非动作类步骤: 纯推理步骤,允许降级成功
                        # - 无工具调用且动作类步骤: 已被上方门禁拦截,此处不会走到(防御性处理)
                        step.success = step_tool_called or not step_requires_tool_call(step.description)
                        step.result = event.message[:2000] if event.message else "任务执行完成（结果解析降级）"
                    # 批量任务完整性校验门禁(复用no_tool_retry_done,每步最多一次重试)
                    # 修复: 原代码在step.result尚未设置时调用verify_batch_completeness,
                    #   导致count_completed_items始终返回0(attachments/result均为空),
                    #   无罪推定逻辑(completed==0 and step.result)因step.result为空而失效,
                    #   误报"目标N项完成0项",LLM被强制重试浪费多轮token(会话6a9c2c12根因)。
                    #   现移到step.result解析之后,确保校验时step.result和attachments已填充,
                    #   无罪推定可正确生效。
                    is_complete, guidance = verify_batch_completeness(step)
                    if not is_complete and not no_tool_retry_done:
                        logger.warning(f"步骤[{step.id}]批量任务完整性校验未通过: {guidance}")
                        no_tool_retry_done = True
                        query = ReActAgent._build_execution_query(
                            plan, step, message,
                            special_capability_keywords=self._agent_config.special_capability_keywords,
                        ) + f"\n\n{guidance}"
                        force_tool_choice = "required"
                        # 重试前重置步骤状态(上方已设置COMPLETED,需回退为RUNNING)
                        step.status = ExecutionStatus.RUNNING
                        should_retry = True
                        break
                    # 部分完成附警告(允许COMPLETED但标记,供前端/日志可观测)
                    if not is_complete and guidance:
                        step.result = f"⚠️ {guidance}\n\n{step.result}" if step.result else guidance
                    # F10-5: 步骤完成时 progress=100,前端据此完成进度条
                    yield self._make_step_event(step, StepEventStatus.COMPLETED, progress=100)
                    if step.result:
                        yield MessageEvent(role="assistant", message=step.result)
                    continue
                elif isinstance(event, ErrorEvent):
                    step.status = ExecutionStatus.FAILED
                    step.error = event.error
                    step.retry_count += 1

                    if self._can_retry_step(step):
                        logger.warning(
                            f"步骤[{step.id}]第{step.retry_count}次失败,自动重试: {event.error[:100]}"
                        )
                        # F10-5: 失败重试时保持上次进度,前端可看到中断位置
                        yield self._make_step_event(
                            step, StepEventStatus.FAILED,
                            progress=last_progress,
                            message=f"步骤失败(将重试{step.retry_count}/{_MAX_RETRY_COUNT}): {event.error[:100]}"
                        )
                        # 构建重试query(注入失败原因,引导LLM换策略)
                        query = ReActAgent._build_execution_query(
                            plan, step, message, failure_error=event.error,
                            special_capability_keywords=self._agent_config.special_capability_keywords,
                        )
                        step.status = ExecutionStatus.RUNNING
                        should_retry = True
                        break
                    # 不可重试或重试耗尽: 返回失败,交由Planner决策
                    yield self._make_step_event(step, StepEventStatus.FAILED)
                yield event

            if not should_retry:
                break

        # 4.循环迭代完成后，若步骤未被显式标记则降级为完成
        if step.status not in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED):
            # 工具调用幻觉防护: 动作类步骤迭代结束未调用工具 → 标记 FAILED
            # 覆盖 invoke 循环因 max_iterations 或异常退出但未产出 MessageEvent 的场景
            if not step_tool_called and step_requires_tool_call(step.description):
                logger.warning(
                    f"步骤[{step.id}]迭代结束未调用任何工具,动作类步骤标记失败(防止工具调用幻觉)"
                )
                step.status = ExecutionStatus.FAILED
                step.success = False
                step.error = build_missing_tool_error(step.description)
                yield self._make_step_event(
                    step, StepEventStatus.FAILED,
                    progress=last_progress,
                    message="步骤迭代结束未调用工具,标记失败(防止工具调用幻觉)",
                )
                return
            step.status = ExecutionStatus.COMPLETED
            if not step.result:
                step.success = True
                step.result = "任务执行完成（Agent迭代结束未生成结构化结果）"

    @staticmethod
    def _make_step_event(
        step: Step,
        status: StepEventStatus,
        progress: int = 0,
        message: str = "",
    ) -> StepEvent:
        """创建步骤事件(深拷贝step快照,防止后续step状态变更污染已产出事件)

        根因修复(会话bffcb4ae): StepEvent.step 持有共享可变引用,
        react.py 中 step.status = COMPLETED 赋值会污染所有已产出但尚未持久化的事件,
        导致 DB 快照错误捕获 completed(38/72事件受影响)。
        深拷贝隔离后,事件中的 step 快照不再受后续状态变更影响。
        """
        return StepEvent(
            step=step.model_copy(deep=True),
            status=status,
            progress=progress,
            message=message,
        )

    @staticmethod
    def _estimate_step_progress(tool_call_count: int) -> int:
        """基于工具调用计数估算步骤进度(F10-5)

        估算策略:
        - 1次工具调用: 25% (初期快速推进,让用户感知到任务已启动)
        - 2次工具调用: 45%
        - 3次工具调用: 60%
        - 4次工具调用: 70%
        - 5次工具调用: 78%
        - 后续每次 +2%,上限 90% (保留 10% 余量给最终总结,避免接近100误导用户)

        算法不依赖 max_iterations(不同步骤迭代次数差异大),保证:
        - 少工具调用步骤(1-3次): 进度推进明显,用户体验好
        - 多工具调用步骤(5+次): 进度收敛,不会过早接近100

        Args:
            tool_call_count: 步骤内已完成的工具调用次数(>=1)

        Returns:
            进度百分比 0-100,永远不会返回100(100 由 COMPLETED 事件显式设置)
        """
        if tool_call_count <= 0:
            return 0
        # 渐进推进表: [N次工具调用] -> 进度百分比
        progress_table = [25, 45, 60, 70, 78]
        if tool_call_count <= len(progress_table):
            return progress_table[tool_call_count - 1]
        # 超过5次后,每次推进2%,上限90%
        extra = tool_call_count - len(progress_table)
        return min(90, 78 + extra * 2)

    @staticmethod
    def _can_retry_step(step: Step) -> bool:
        """判断失败步骤是否可以自动重试

        不可重试条件:
        1. 重试次数已达上限(_MAX_RETRY_COUNT)
        2. 错误信息包含不可重试标记(如迭代溢出,重试会导致无限循环)
        """
        if step.retry_count > _MAX_RETRY_COUNT:
            return False
        if step.error:
            for marker in _NON_RETRYABLE_ERROR_MARKERS:
                if marker in step.error:
                    return False
        return True

    @staticmethod
    def _build_execution_query(
            plan: Plan, step: Step, message: Message,
            failure_error: Optional[str] = None,
            special_capability_keywords: Optional[List[str]] = None,
    ) -> str:
        """构建执行query,可选注入失败原因用于重试引导

        注入前序步骤完成结果,防止LLM重复执行已完成的操作(如重复下载)。
        对专业领域能力步骤(天气/地图/位置/多模态等),主动注入MCP工具搜索引导,
        防止LLM首调即退化到search_web而跳过MCP工具(根因)。

        Args:
            plan: 当前计划
            step: 当前步骤
            message: 用户消息
            failure_error: 上次执行的失败原因(重试时注入,引导LLM尝试替代方案)
            special_capability_keywords: 专业能力关键词(F2-4外置),
                None时使用模块默认值;生产环境由调用方传入self._agent_config.special_capability_keywords
        """
        step_description = step.description

        # 注入前序步骤完成情况,让LLM知晓已完成的操作和产出的文件
        prior_context = ReActAgent._build_prior_steps_context(plan, step)
        if prior_context:
            step_description = f"{prior_context}\n\n当前步骤：{step_description}"

        # 专业能力步骤: 主动注入MCP工具直接调用引导(预防性,首调即生效)
        # 根因: 专业能力查询步骤首调即用search_web,因search_web也是工具
        # 未触发no_tool_retry,导致MCP工具从未被调用。此处主动提示确保首调优先MCP。
        # MCP直接加载: 工具已全量加载到工具列表,F10-6按步骤关键词装配对应MCP工具schema
        # F2-4外置: 关键词从AgentConfig注入,运维可通过config.yaml调整
        mcp_hint = ReActAgent._build_mcp_capability_hint(
            step.description, special_capability_keywords
        )
        if mcp_hint:
            step_description = f"{step_description}\n\n{mcp_hint}"

        #  / 方向4: 事前合并引导(量化目标感知)
        # 仅当步骤含量化目标 N(>=5)时注入,引导 LLM 合并批量同类操作为单次 shell_execute
        consolidation_hint = get_consolidation_guidance(step.description)
        if consolidation_hint:
            step_description = f"{step_description}{consolidation_hint}"

        if failure_error:
            # 重试场景: 在步骤描述中注入失败原因,引导LLM换策略
            truncated_error = failure_error[:200]
            step_description = (
                f"{step_description}\n\n"
                f"⚠️ 上次执行失败(第{step.retry_count}次),失败原因: {truncated_error}\n"
                f"请尝试替代方案。"
            )
        return EXECUTION_PROMPT.format(
            message=message.message,
            attachments="\n".join(message.attachments),
            language=plan.language,
            step=step_description,
        )

    @staticmethod
    def _build_prior_steps_context(plan: Plan, current_step: Step) -> str:
        """构建前序步骤完成情况摘要(DRY重构,委托共享构建器)"""
        from app.domain.services.agents._step_context_builder import build_prior_steps_context
        return build_prior_steps_context(plan, current_step, context_type="execution")

    @staticmethod
    def _is_special_capability_step(
            step_description: str,
            keywords: Optional[List[str]] = None,
    ) -> bool:
        """判断步骤是否涉及专业领域能力(需优先使用MCP工具)

        涵盖两类通用能力类别(非特定供应商或业务场景,保持通用型智能体定位):
        1. 多模态能力: 图片/OCR/语音/视频/视觉等
        2. 专业领域服务: 天气/地图/位置/翻译/汇率等(通常由MCP工具提供)

        用于触发针对性的MCP工具直接调用引导,防止LLM在json_object模式下
        直接假设专业能力工具不可用而退化到search_web。

        内置工具/能力排除(与TOOL_SELECTION_GUIDE_CN/BUILT_IN_CAPABILITY_CN对齐):
        步骤已指定内置工具(deep_research/search_web)、内置编程能力库
        (pandas/openpyxl/python-docx等)或已知MCP工具名(含mcp_前缀)时,
        返回False,不注入MCP调用引导。避免"使用deep_research研究...多模态方向"
        这类步骤因"多模态"命中关键词而误注入MCP引导,与已指定的
        内置工具冲突导致LLM困惑空转。

        英文关键词大小写不敏感。召回优先于精确:误判的代价仅为"多提示一次MCP",
        漏判的代价是"LLM用search_web替代MCP专业工具"。

        关键词来源(F2-4外置):
        - keywords参数显式传入时使用该列表(生产环境由AgentConfig注入)
        - keywords为None时使用模块级_DEFAULT_SPECIAL_CAPABILITY_KEYWORDS默认值(兼容旧调用)

        Args:
            step_description: 步骤描述文本
            keywords: 专业能力关键词列表,None时使用默认值

        Returns:
            True表示步骤涉及专业领域能力,应优先使用MCP工具
        """
        special_keywords = keywords if keywords is not None else _DEFAULT_SPECIAL_CAPABILITY_KEYWORDS
        desc_lower = step_description.lower()
        # 步骤已指定内置工具/能力库/已知MCP工具名时,无需MCP调用引导
        # (避免与内置工具优先原则冲突,见_BUILT_IN_TOOL_MARKERS设计动机)
        if any(marker in desc_lower for marker in _BUILT_IN_TOOL_MARKERS):
            return False
        if _MCP_TOOL_NAME_PREFIX in desc_lower:
            return False
        return any(kw in desc_lower for kw in special_keywords)

    @staticmethod
    def _build_mcp_capability_hint(
            step_description: str,
            keywords: Optional[List[str]] = None,
    ) -> Optional[str]:
        """构建专业能力MCP工具直接调用引导(步骤开始时主动注入,预防性)

        当步骤涉及专业领域能力时,主动注入MCP工具直接调用引导,
        防止LLM直接退化到search_web而跳过MCP工具。

        MCP直接加载模式: MCP工具已全量加载到工具列表(以mcp_前缀标识),
        F10-6按步骤关键词自动装配对应MCP工具schema,LLM可直接从工具列表中
        选择匹配的MCP工具调用,无需搜索/描述中间步骤。

        与_build_no_tool_retry_guidance的区别:
        - 本方法在步骤开始时主动注入(预防性,首调即生效)
        - _build_no_tool_retry_guidance在未调用工具重试时注入(纠正性,兜底)

        根因: 天气查询步骤ReAct首调即用search_web,
        未触发no_tool_retry(因为search_web也是工具),导致MCP工具从未被调用。
        本方法在步骤开始时主动提示,确保首调即优先使用MCP工具。

        Args:
            step_description: 当前步骤的描述文本
            keywords: 专业能力关键词列表,None时使用默认值(F2-4外置)

        Returns:
            MCP工具直接调用引导文本,非专业能力步骤返回None
        """
        if not ReActAgent._is_special_capability_step(step_description, keywords):
            return None
        return (
            "【系统提示】本步骤涉及专业领域能力,"
            "**必须优先在工具列表中查找以`mcp_`前缀开头的工具直接调用**"
            "(如mcp_amap_weather查天气、mcp_mcp-multimodal_vl_image_understand做视觉理解),"
            "工具schema已全量加载,直接传入参数调用即可。"
            "只有工具列表中无匹配的MCP工具时,才可改用search_web兜底。"
        )

    @staticmethod
    def _build_no_tool_retry_guidance(
            step_description: str,
            keywords: Optional[List[str]] = None,
    ) -> str:
        """构建无工具调用重试引导提示

        分析步骤描述,生成针对性的工具调用引导。
        对涉及专业领域能力的步骤,额外提示优先使用MCP工具直接调用,
        防止LLM直接假设工具不可用而跳过工具调用。

        Args:
            step_description: 当前步骤的描述文本
            keywords: 专业能力关键词列表,None时使用默认值(F2-4外置)

        Returns:
            引导提示文本,追加到执行query末尾
        """
        guidance = (
            "\n\n【系统提示】上一次执行未调用任何工具就直接返回了结果。"
            "步骤执行必须先通过工具调用获取信息或执行操作。"
            "请使用合适的工具(如shell_execute/read_file/write_file/"
            "mcp_前缀MCP工具/search_web等)完成任务后,再输出最终结果。"
        )
        # 专业能力关键词检测,提示优先使用MCP工具直接调用
        if ReActAgent._is_special_capability_step(step_description, keywords):
            guidance += (
                "\n特别注意:本步骤涉及专业领域能力,"
                "**必须优先在工具列表中查找以`mcp_`前缀开头的工具直接调用**"
                "(如mcp_amap_weather/mcp_mcp-multimodal_vl_image_understand等),"
                "而非直接假设工具不可用或退化到search_web。"
            )
        return guidance

    async def summarize(self, known_files: list[str] | None = None) -> AsyncGenerator[BaseEvent, None]:
        """非流式汇总历史消息并生成最终回复（JSON解析）。

        SUMMARIZE_PROMPT要求LLM返回JSON {message, attachments},
        解析后构造最终MessageEvent(is_final=True)交付。
        JSON解析失败时降级为原始文本+已知文件列表。

        Args:
            known_files: 已知需要交付给用户的文件路径列表
        """
        files_text = "（无）"
        if known_files:
            files_text = "\n".join(f"  - {fp}" for fp in known_files)
        query = SUMMARIZE_PROMPT.format(files=files_text)

        try:
            message = await self._invoke_llm(
                [{"role": "user", "content": query}],
                format=None,
                tools_enabled=False,
            )
        except Exception as e:
            logger.error(f"汇总LLM调用失败: {str(e)}")
            yield ErrorEvent(error=f"AI模型汇总失败，请重试: {str(e)[:100]}")
            return

        if not message or not message.get("content"):
            yield ErrorEvent(error="Agent未能生成有效汇总内容")
            return

        raw_content = message["content"]
        try:
            parsed_obj = await self._json_parser.invoke(raw_content)
            parsed_message = Message.model_validate(parsed_obj)
            final_message = parsed_message.message or "任务已完成。"
            llm_attachments = parsed_message.attachments or []
            # 合并LLM返回的附件与已知文件列表,去重保序
            all_files = list(dict.fromkeys(llm_attachments + (known_files or [])))
            attachments = [File(filepath=fp) for fp in all_files]
        except Exception as e:
            logger.warning(f"汇总JSON解析失败，使用原始文本降级: {str(e)}")
            final_message = raw_content or "任务已完成，但无法生成结构化总结。"
            attachments = [File(filepath=fp) for fp in (known_files or [])]

        # F10-1: 汇总最终答案走流式切片,统一前后端交互契约
        # 附件随最终片(is_final=True)交付,增量片段(is_streaming=True)不携带附件
        async for evt in self._stream_final_answer(final_message, attachments=attachments):
            yield evt
