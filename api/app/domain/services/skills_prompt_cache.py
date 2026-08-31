#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : skills_prompt_cache.py
Skills提示词缓存 - 应用启动时预生成技能索引基础提示词，运行时按需追加会话级上下文

设计:
- 单例模式，应用生命周期内共享一份基础提示词
- 基础提示词包含技能索引和使用原则，启动时由SkillService.generate_skills_prompt生成
- 会话级上下文（最近使用的技能）通过替换</skills_index>标签动态注入
- refresh()方法支持技能目录变更后重新生成
"""
import logging
from typing import Optional

from app.domain.services.skill_service import SkillService

logger = logging.getLogger(__name__)


class SkillsPromptCache:
    """Skills提示词缓存单例"""

    _instance: Optional["SkillsPromptCache"] = None
    _base_prompt: str = ""
    _initialized: bool = False
    _skill_service: Optional[SkillService] = None

    def __new__(cls) -> "SkillsPromptCache":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    async def initialize(cls, skill_service: SkillService) -> None:
        """初始化缓存，生成基础提示词（应用启动时调用）"""
        instance = cls()
        try:
            instance._base_prompt = await skill_service.generate_skills_prompt()
            instance._skill_service = skill_service
            instance._initialized = True
            logger.info("Skills提示词缓存初始化成功")
        except Exception as e:
            logger.error(f"Skills提示词缓存初始化失败: {str(e)}")
            instance._base_prompt = ""
            instance._initialized = False

    @classmethod
    def get_prompt(cls, session_id: Optional[str] = None) -> str:
        """获取提示词，可选追加会话级最近使用信息"""
        instance = cls()
        if not instance._base_prompt:
            return ""
        if not session_id or not instance._skill_service:
            return instance._base_prompt
        recent = instance._skill_service.get_recent_skills(session_id)
        if not recent:
            return instance._base_prompt
        recent_block = f"\n## 最近使用的Skills\n本对话最近使用: {', '.join(f'`{s}`' for s in recent)}\n"
        return instance._base_prompt.replace("</skills_index>", f"{recent_block}</skills_index>")

    @classmethod
    async def refresh(cls, skill_service: SkillService) -> None:
        """刷新缓存（技能目录变更后调用）"""
        await cls.initialize(skill_service)

    @classmethod
    def is_initialized(cls) -> bool:
        """缓存是否已初始化"""
        return cls._instance._initialized if cls._instance else False

    @classmethod
    def reset(cls) -> None:
        """重置缓存（仅用于测试）"""
        if cls._instance:
            instance = cls._instance
            instance._base_prompt = ""
            instance._initialized = False
            instance._skill_service = None
        cls._instance = None
