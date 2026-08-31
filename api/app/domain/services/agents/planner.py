#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/20 15:27

@File    : planner.py
"""
import logging
import re
from typing import Optional, AsyncGenerator, Dict, List, Tuple

from app.domain.models.event import BaseEvent, MessageEvent, PlanEvent, PlanEventStatus
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.services.prompts.planner import (
    PLANNER_SYSTEM_PROMPT,
    CREATE_PLAN_PROMPT,
    UPDATE_PLAN_PROMPT,
)
from app.domain.services.prompts.system import SYSTEM_PROMPT_CORE
from .base import BaseAgent

"""
多Agent系统/flow=PlannerAgent+ReActAgent

顺序:
1. PlannerAgent生成规划;
2. 循环取出规划中的子步骤，让ReActAgent执行，依次迭代;
3. ReActAgent执行完每一个子步骤之后，需要将子步骤结果+Plan传递给PlannerAgent让其更新计划/Plan；
4. 循环取出规划中的子步骤，让ReActAgent执行，依次迭代;
5. ...
6. 直到所有子任务/步骤都完成，这时候将子步骤的所有结果汇总进行总结(ReActAgent);

PlannerAgent:
- 功能: 将用户的需求拆解成多个子任务+根据已完成的子任务更新规划
- 提示词: 创建规划的prompt、更新规划的prompt

ReActAgent:
- 功能: 迭代执行完每一个子任务、汇总所有的子任务进行总结
- 提示词: 执行任务的prompt、汇总总结prompt
"""

logger = logging.getLogger(__name__)


class PlannerAgent(BaseAgent):
    """规划Agent，用于将用户的任务/需求拆解成多个子步骤"""
    name: str = "planner"
    # 仅加载核心片段(身份+能力+MCP规则+交付规则+重要提示),不加载执行场景片段
    # (文件/搜索/浏览器/Shell/编码/写作规则+沙箱环境),节省约1500 token/次规划调用
    _system_prompt: str = SYSTEM_PROMPT_CORE + PLANNER_SYSTEM_PROMPT
    _format: Optional[str] = "json_object"
    _tool_choice: Optional[str] = "none"

    # 批次 29: deep_research 纳入基础设施类别(最高优先级),
    # 原代码将其归入"其他工具"导致在工具摘要中被截断或位于底部,LLM 注意力权重低。
    _INFRA_TOOL_NAMES = frozenset({"shell", "browser", "file", "search", "message", "deep_research"})
    _MCP_SOURCE_PATTERN = re.compile(r"\[来源:\s*(.+?)\]")
    _CATEGORY_PRIORITY: Tuple[str, ...] = (
        "基础设施", "MCP专业工具", "技能", "A2A远程Agent", "其他工具",
    )
    _MAX_DESC_LENGTH = 100
    _MAX_TOOLS_PER_CATEGORY = 15
    _MAX_SUMMARY_LENGTH = 4000

    def _build_tools_summary(self) -> str:
        """构建可用工具摘要，按类别分组并限制总长度，供规划时参考

        策略：按类别分组 → 按优先级排序 → 逐类别填充至长度上限
        优势：结构化呈现帮助LLM快速定位工具；智能截断保证重要类别优先保留
        """
        categories = self._group_tools_by_category()
        if not categories:
            logger.debug("工具摘要: 无可用工具")
            return "无可用工具"

        sections = self._build_category_sections(categories)
        summary = self._assemble_summary_with_limit(sections)

        total_tools = sum(len(v) for v in categories.values())
        logger.debug(f"工具摘要: 共{total_tools}个工具, {len(categories)}个类别, 摘要长度{len(summary)}")

        return summary if summary else "无可用工具"

    def _group_tools_by_category(self) -> Dict[str, List[str]]:
        """遍历所有工具包，按类别分组并格式化条目"""
        categories: Dict[str, List[str]] = {}
        for tool_pkg in self._tools:
            pkg_name = tool_pkg.name
            try:
                tools_list = tool_pkg.get_tools()
            except Exception as e:
                logger.warning(f"获取工具包[{pkg_name}]列表失败: {e}")
                continue

            for t in tools_list:
                func = t.get("function", {})
                name = func.get("name", "")
                if not name:
                    continue
                desc = func.get("description", "")
                category = self._classify_tool(pkg_name, name, desc)
                entry = self._format_tool_entry(name, desc)
                categories.setdefault(category, []).append(entry)
        return categories

    @classmethod
    def _format_tool_entry(cls, name: str, desc: str) -> str:
        """格式化单个工具条目：取描述首行，移除冗余来源标签，超长截断"""
        first_line = desc.split("\n")[0].strip() if desc else name
        first_line = cls._MCP_SOURCE_PATTERN.sub("", first_line).strip()
        if not first_line:
            first_line = name
        if len(first_line) > cls._MAX_DESC_LENGTH:
            first_line = first_line[:cls._MAX_DESC_LENGTH - 3] + "..."
        return f"  - {name}: {first_line}"

    @classmethod
    def _classify_tool(cls, pkg_name: str, tool_name: str, description: str = "") -> str:
        """根据工具包名、工具名和描述将工具归入语义类别"""
        if pkg_name in cls._INFRA_TOOL_NAMES:
            return "基础设施"
        if pkg_name == "mcp":
            server_name = cls._extract_mcp_server(description, tool_name)
            return f"MCP专业工具({server_name})" if server_name else "MCP专业工具"
        if pkg_name == "skill":
            return "技能"
        if pkg_name == "a2a":
            return "A2A远程Agent"
        return "其他工具"

    @classmethod
    def _extract_mcp_server(cls, description: str, tool_name: str) -> str:
        """从描述的[来源:xxx]标签提取MCP服务名，兜底从工具名解析"""
        match = cls._MCP_SOURCE_PATTERN.search(description)
        if match:
            return match.group(1).strip()
        if tool_name.startswith("mcp_"):
            parts = tool_name.split("_")
            if len(parts) >= 2:
                return parts[1]
        return ""

    def _build_category_sections(self, categories: Dict[str, List[str]]) -> List[Tuple[str, str]]:
        """将分类字典构建为(类别前缀, 段落文本)列表，每类别限制条目数"""
        sections: List[Tuple[str, str]] = []
        for category, items in categories.items():
            total = len(items)
            if total > self._MAX_TOOLS_PER_CATEGORY:
                items = items[:self._MAX_TOOLS_PER_CATEGORY]
                items.append(f"  - ...等共{total}个工具")
            header = f"{category}({total}):"
            sections.append((category, f"{header}\n" + "\n".join(items)))
        return sections

    def _category_sort_key(self, category: str) -> int:
        """计算分类排序键：匹配优先级前缀越靠前排序值越小"""
        for i, prefix in enumerate(self._CATEGORY_PRIORITY):
            if category.startswith(prefix):
                return i
        return len(self._CATEGORY_PRIORITY)

    def _assemble_summary_with_limit(self, sections: List[Tuple[str, str]]) -> str:
        """按类别优先级组装摘要，智能截断：逐类别填充，超限时在类别边界截断"""
        sorted_sections = sorted(sections, key=lambda s: self._category_sort_key(s[0]))

        result_parts: List[str] = []
        remaining = self._MAX_SUMMARY_LENGTH

        for category, section_text in sorted_sections:
            if remaining <= 0:
                logger.debug(f"工具摘要已达上限，跳过类别: {category}")
                break

            separator_len = 2 if result_parts else 0
            needed = separator_len + len(section_text)

            if needed <= remaining:
                result_parts.append(section_text)
                remaining -= needed
            else:
                available = remaining - separator_len
                if available > 20:
                    result_parts.append(section_text[:available - 3] + "...")
                else:
                    logger.debug(f"工具摘要空间不足，跳过类别: {category}")
                remaining = 0

        return "\n\n".join(result_parts)

    async def create_plan(
            self,
            message: Message,
    ) -> AsyncGenerator[BaseEvent, None]:
        """根据用户传递的消息创建计划/规划，迭代返回对应的事件"""
        tools_summary = self._build_tools_summary()

        query = CREATE_PLAN_PROMPT.format(
            message=message.message,
            attachments="\n".join(message.attachments),
            tools_summary=tools_summary,
            context_block="",
        )

        async for event in self.invoke(query):
            if isinstance(event, MessageEvent) and not getattr(event, "is_thinking", False):
                # 改进A: is_thinking 守卫 — 思考事件落入 else 透传,不被解析为 Plan JSON
                logger.info(f"PlannerAgent生成消息: {event.message}")
                try:
                    parsed_obj = await self._json_parser.invoke(event.message)
                    plan = Plan.model_validate(parsed_obj)
                except Exception as e:
                    logger.warning(f"计划JSON解析失败，创建降级计划: {str(e)}")
                    plan = Plan(
                        title="任务处理",
                        goal=message.message[:200],
                        language="zh",
                        steps=[Step(id="1", description=message.message[:500])],
                        message="我将为您处理这个任务。",
                    )

                yield PlanEvent(plan=plan, status=PlanEventStatus.CREATED)
            else:
                yield event

    async def update_plan(self, plan: Plan, step: Step) -> AsyncGenerator[BaseEvent, None]:
        """根据传递的原始规划+子步骤更新事件

        优化点：当计划发生实质性修改时，先发射 MessageEvent 向用户说明修订原因，
        再发射 PlanEvent(UPDATED)，消除静默更新，提升计划修订的透明度。

        当步骤FAILED时,追加恢复决策指令,引导LLM选择重试/跳过/终止策略。

        批次 27: 注入前序已完成步骤上下文(prior_steps_context),防止 LLM 重建已完成步骤。
        """
        prior_steps_context = self._build_prior_steps_context(plan, step.id)
        query = UPDATE_PLAN_PROMPT.format(
            plan=plan.model_dump_json(),
            step=step.model_dump_json(),
            prior_steps_context=prior_steps_context,
        )

        # 失败步骤追加恢复决策指令,引导LLM选择重试/跳过/终止
        if step.status == ExecutionStatus.FAILED:
            query += self._build_recovery_directive(step)

        async for event in self.invoke(query):
            if isinstance(event, MessageEvent) and not getattr(event, "is_thinking", False):
                # 改进A: is_thinking 守卫 — 思考事件落入 else 透传,不被解析为 Plan JSON
                logger.info(f"PlannerAgent生成消息: {event.message}")
                try:
                    parsed_obj = await self._json_parser.invoke(event.message)
                    updated_plan = Plan.model_validate(parsed_obj)
                except Exception as e:
                    logger.warning(f"计划更新JSON解析失败，保持原计划: {str(e)}")
                    yield PlanEvent(plan=plan, status=PlanEventStatus.UPDATED)
                    continue

                new_steps = [Step.model_validate(s) for s in updated_plan.steps]

                first_pending_index = None
                for idx, s in enumerate(plan.steps):
                    if not s.done:
                        first_pending_index = idx
                        break

                if first_pending_index is not None:
                    # 捕获原始未完成步骤，用于变更检测（在修改plan.steps之前）
                    original_remaining = plan.steps[first_pending_index:]
                    original_count = len(original_remaining)

                    # 提取LLM修订说明：用于判断步骤减少是否为有意行为
                    # 当LLM明确说明取消原因时，应尊重其决策，不再恢复已取消的步骤
                    llm_revision_message = (updated_plan.message or "").strip()

                    # 批次 38: 步骤去重 — 移除与已完成步骤描述重复的新步骤
                    # 根因: 批次 37 E2E 发现 LLM 在 update_plan 时返回重复步骤
                    # (如步骤6和步骤11描述完全相同),导致任务进度显示混乱(已完成步骤显示⏳)
                    new_steps = self._deduplicate_steps(new_steps, plan.steps[:first_pending_index])

                    updated_steps = plan.steps[:first_pending_index]
                    updated_steps.extend(new_steps)
                    # 步骤保护：仅当LLM未提供修订说明时才保留原步骤作为兜底
                    # LLM提供修订说明表示步骤减少是有意行为（如"任务已完成，取消后续步骤"），
                    # 此时恢复已取消步骤会覆盖LLM的决策，导致已取消的任务被重复执行
                    steps_protected = False
                    if len(new_steps) < original_count and not llm_revision_message:
                        logger.warning(
                            f"Plan更新后步骤数减少且无修订说明: {original_count} → {len(new_steps)}, "
                            f"可能为异常响应, 保留原始未完成步骤作为兜底"
                        )
                        preserved = plan.steps[first_pending_index + len(new_steps):]
                        for s in preserved:
                            if not s.done:
                                updated_steps.append(s)
                        steps_protected = True
                    plan.steps = updated_steps

                    # 步骤保护生效时，实际计划未发生变更，无需输出修订说明
                    if not steps_protected:
                        steps_changed = self._detect_step_changes(original_remaining, new_steps)
                        revision_message = llm_revision_message
                        # 兜底机制：LLM未返回修订说明时，自动生成变更摘要
                        if steps_changed and not revision_message:
                            revision_message = self._build_revision_summary(original_remaining, new_steps) or ""
                        if steps_changed and revision_message:
                            logger.info(f"计划已修订，向用户说明变更: {revision_message[:80]}")
                            yield MessageEvent(message=revision_message)

                yield PlanEvent(plan=plan, status=PlanEventStatus.UPDATED)
            else:
                yield event

    @staticmethod
    def _deduplicate_steps(new_steps: List[Step], completed_steps: List[Step]) -> List[Step]:
        """步骤去重 — 移除与已完成步骤描述重复的新步骤,以及新步骤内部的重复

        批次 38 修复: LLM 在 update_plan 时可能返回与已完成步骤描述完全相同的新步骤,
        导致任务进度显示混乱(已完成步骤显示⏳待进行)。

        去重策略:
        1. 收集已完成步骤的描述前缀(前50字符,避免长描述噪声)
        2. 过滤 new_steps,移除描述前缀与已完成步骤重复的步骤
        3. 同时检测 new_steps 内部的重复(保留首次出现的步骤)

        Args:
            new_steps: LLM 返回的新步骤列表
            completed_steps: 已完成的步骤列表(plan.steps[:first_pending_index])

        Returns:
            去重后的新步骤列表
        """
        # 1.收集已完成步骤的描述前缀(前50字符,截断长描述避免噪声)
        completed_prefixes: set = set()
        for s in completed_steps:
            if s.done and s.description:
                prefix = s.description.strip()[:50]
                if prefix:
                    completed_prefixes.add(prefix)

        # 2.过滤 new_steps: 移除与已完成步骤重复的,同时检测内部重复
        seen_prefixes: set = set()
        deduplicated: List[Step] = []
        removed_count = 0

        for s in new_steps:
            if not s.description:
                deduplicated.append(s)
                continue
            prefix = s.description.strip()[:50]
            # 与已完成步骤重复 → 移除
            if prefix in completed_prefixes:
                removed_count += 1
                logger.info(f"步骤去重: 移除与已完成步骤重复的新步骤: {prefix[:30]}...")
                continue
            # 新步骤内部重复 → 移除
            if prefix in seen_prefixes:
                removed_count += 1
                logger.info(f"步骤去重: 移除新步骤内部重复: {prefix[:30]}...")
                continue
            seen_prefixes.add(prefix)
            deduplicated.append(s)

        if removed_count > 0:
            logger.warning(f"步骤去重完成: 移除{removed_count}个重复步骤, 剩余{len(deduplicated)}个")

        return deduplicated

    @staticmethod
    def _detect_step_changes(original_remaining: List[Step], new_steps: List[Step]) -> bool:
        """检测计划步骤是否发生实质性变更

        比较原始未完成步骤与新步骤，判断是否存在增删或描述修改。
        仅在实质性变更时返回True，避免状态推进时产生冗余的修订说明。

        Args:
            original_remaining: 更新前的未完成步骤列表
            new_steps: LLM返回的新步骤列表

        Returns:
            True表示步骤发生实质性变更，False表示仅状态推进
        """
        # 步骤数量变化 → 增删了步骤
        if len(original_remaining) != len(new_steps):
            return True
        # 逐步骤比较描述，任一不同即为实质性修改
        for orig, new in zip(original_remaining, new_steps):
            if orig.description.strip() != new.description.strip():
                return True
        return False

    @staticmethod
    def _build_revision_summary(original_remaining: List[Step], new_steps: List[Step]) -> Optional[str]:
        """当LLM未提供修订说明时，通过对比步骤变更自动生成修订摘要

        兜底机制：LLM可能不遵循提示词返回message字段，此时通过差异分析
        自动生成简明的变更说明，确保用户始终能了解计划修订内容。

        Args:
            original_remaining: 更新前的未完成步骤列表
            new_steps: LLM返回的新步骤列表

        Returns:
            修订摘要文本，无变更时返回None
        """
        if not PlannerAgent._detect_step_changes(original_remaining, new_steps):
            return None

        changes: List[str] = []
        orig_count = len(original_remaining)
        new_count = len(new_steps)

        # 步骤数量变化
        if new_count > orig_count:
            changes.append(f"新增{new_count - orig_count}个步骤")
        elif new_count < orig_count:
            changes.append(f"精简{orig_count - new_count}个步骤")

        # 步骤描述变化
        modified_count = 0
        for orig, new in zip(original_remaining, new_steps):
            if orig.description.strip() != new.description.strip():
                modified_count += 1
        if modified_count > 0:
            changes.append(f"调整{modified_count}个步骤描述")

        return "计划已修订：" + "，".join(changes) + "。"

    @staticmethod
    def _build_recovery_directive(failed_step: Step) -> str:
        """构建失败步骤的恢复决策指令

        当步骤FAILED时追加到update_plan的query中,引导LLM选择恢复策略:
        1. 重试: 修改步骤描述,提供替代执行方案(如换用其他工具或方法)
        2. 跳过: 将步骤标记为skipped,在后续步骤中弥补
        3. 终止: 如果是关键步骤且无法绕过,标记plan为无法完成

        Args:
            failed_step: 失败的步骤(包含error信息和retry_count)

        Returns:
            追加到query的恢复决策指令文本
        """
        error_brief = (failed_step.error or "未知错误")[:200]
        retry_info = f"(已自动重试{failed_step.retry_count}次)" if failed_step.retry_count > 0 else ""
        return (
            "\n\n⚠️ 上述步骤执行失败" + retry_info + f"。错误原因: {error_brief}\n"
            "请在更新计划时选择以下恢复策略之一:\n"
            "1. 重试: 修改步骤描述,提供替代执行方案(如换用其他工具或方法)\n"
            "2. 跳过: 将该步骤从后续步骤中移除,在后续步骤中弥补其缺失的功能\n"
            "3. 终止: 如果该步骤是关键步骤且无法绕过,在message中说明无法完成任务的原因\n"
            "请在更新后的steps中体现所选策略(如修改描述或删除该步骤),"
            "并在message中说明选择的策略和原因。"
        )

    @staticmethod
    def _build_prior_steps_context(plan: Plan, current_step_id: str) -> str:
        """构建前序步骤完成情况摘要(Batch 34 DRY重构,委托共享构建器)"""
        from app.domain.services.agents._step_context_builder import build_prior_steps_context
        return build_prior_steps_context(plan, current_step_id, context_type="planning")
