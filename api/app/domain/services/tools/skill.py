#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : skill.py
SkillTool - Agent可调用的技能工具，提供技能列表、匹配、指南获取能力

工具说明:
- list_skills: 列出所有可用技能，仅在需要了解系统全部能力时调用
- match_skills: 根据任务描述智能匹配技能，不确定用哪个时调用
- get_skill_guide: 获取技能详细操作指南，仅当系统未自动注入指南时调用
"""
import logging

from app.domain.models.tool_result import ToolResult
from app.domain.services.skill_service import SkillService
from app.domain.services.tools.base import BaseTool, tool

logger = logging.getLogger(__name__)


class SkillTool(BaseTool):
    """技能工具集 - 桥接SkillService与Agent工具调用协议"""

    name: str = "skill"

    def __init__(self, skill_service: SkillService) -> None:
        super().__init__()
        self._skill_service = skill_service

    @tool(
        name="list_skills",
        description="列出所有可用的专业技能。仅在需要了解系统全部能力时调用，不要每次任务都调用。",
        parameters={},
        required=[],
    )
    async def list_skills(self) -> ToolResult:
        try:
            skills = await self._skill_service.list_skills()
            if not skills:
                return ToolResult(success=True, message="当前无可用技能", data=[])
            skill_list = [s.to_dict_for_prompt(lightweight=True) for s in skills]
            return ToolResult(success=True, data=skill_list)
        except Exception as e:
            logger.error(f"列出技能失败: {str(e)}")
            return ToolResult(success=False, message=f"列出技能失败: {str(e)}")

    @tool(
        name="match_skills",
        description="根据任务描述智能匹配最相关的技能，返回匹配评分和原因。当不确定应使用哪个技能时调用此工具。",
        parameters={
            "query": {"type": "string", "description": "任务描述或查询内容，用于匹配相关技能"},
            "top_n": {"type": "integer", "description": "返回的最大匹配数量，默认3"},
        },
        required=["query"],
    )
    async def match_skills(self, query: str, top_n: int = 3) -> ToolResult:
        try:
            matches = await self._skill_service.match_skills(query, top_n)
            if not matches:
                return ToolResult(success=True, message="未匹配到相关技能", data=[])
            result = [{
                "name": m.skill.name,
                "description": m.skill.description,
                "category": m.skill.category,
                "score": round(m.score, 2),
                "match_reason": m.match_reason,
                "matched_keywords": m.matched_keywords,
            } for m in matches]
            return ToolResult(success=True, data=result)
        except Exception as e:
            logger.error(f"匹配技能失败: {str(e)}")
            return ToolResult(success=False, message=f"匹配技能失败: {str(e)}")

    @tool(
        name="get_skill_guide",
        description="获取指定技能的详细操作指南(SKILL.md正文)。仅在需要具体操作步骤时调用。",
        parameters={
            "skill_name": {"type": "string", "description": "技能名称，如pdf、docx、xlsx、pptx等"},
        },
        required=["skill_name"],
    )
    async def get_skill_guide(self, skill_name: str) -> ToolResult:
        try:
            content = await self._skill_service.get_skill_guide(skill_name)
            if content is None:
                return ToolResult(success=False, message=f"技能[{skill_name}]不存在")
            return ToolResult(success=True, data={"skill_name": skill_name, "guide": content})
        except Exception as e:
            logger.error(f"获取技能指南失败[{skill_name}]: {str(e)}")
            return ToolResult(success=False, message=f"获取技能指南失败: {str(e)}")

    def record_skill_usage(self, session_id: str, skill_name: str, task_context: str = "", success: bool = True) -> None:
        """记录技能使用历史（供AgentTaskRunner调用）"""
        self._skill_service.record_usage(session_id, skill_name, task_context, success)

    def get_recent_skills(self, session_id: str, limit: int = 3) -> list:
        """获取会话最近使用的技能（供AgentTaskRunner调用）"""
        return self._skill_service.get_recent_skills(session_id, limit)
