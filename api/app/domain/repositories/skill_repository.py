#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : skill_repository.py
Skills仓库接口 - 定义技能持久化的抽象协议
"""
from abc import ABC, abstractmethod
from typing import Optional, List

from app.domain.models.skill import Skill


class ISkillRepository(ABC):
    @abstractmethod
    async def get_all(self) -> List[Skill]:
        ...

    @abstractmethod
    async def get_by_name(self, name: str) -> Optional[Skill]:
        ...

    @abstractmethod
    async def refresh(self) -> None:
        ...
