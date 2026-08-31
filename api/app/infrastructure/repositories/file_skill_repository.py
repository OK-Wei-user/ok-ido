#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : file_skill_repository.py
基于文件系统的Skills仓库实现 - 扫描skills目录，解析SKILL.md，自动构建扩展名映射

核心职责:
- 扫描skills_dir下所有子目录，查找SKILL.md文件
- 解析YAML frontmatter提取技能元数据，Markdown body作为操作指南
- 自动从file_extensions字段构建扩展名→技能名映射表
- 依赖门控: 解析requires字段，运行时检测bins/env，不满足时自动禁用
- 内存缓存+手动refresh策略，支持热更新
"""
import logging
import os
import re
import shutil
from typing import Optional, List, Dict, Tuple

import yaml

from app.domain.models.skill import Skill, SkillDependency, SkillMetadata, SkillRequires
from app.domain.repositories.skill_repository import ISkillRepository

logger = logging.getLogger(__name__)

_YAML_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class FileSkillRepository(ISkillRepository):
    """文件系统Skills仓库 - 从磁盘SKILL.md文件加载技能定义"""

    def __init__(self, skills_dir: str):
        self._skills_dir = skills_dir
        self._cache: Dict[str, Skill] = {}           # name -> Skill
        self._extension_map: Dict[str, str] = {}      # .ext -> skill_name

    async def get_all(self) -> List[Skill]:
        """获取所有技能（含enabled=False），懒加载"""
        if not self._cache:
            await self._load_skills()
        return list(self._cache.values())

    async def get_by_name(self, name: str) -> Optional[Skill]:
        """按名称查询技能，返回None表示不存在"""
        if not self._cache:
            await self._load_skills()
        return self._cache.get(name)

    async def refresh(self) -> None:
        """清空缓存并重新加载，用于技能目录变更后的热更新"""
        self._cache.clear()
        self._extension_map.clear()
        await self._load_skills()
        logger.info(f"Skills缓存已刷新，共{len(self._cache)}个技能")

    def get_extension_map(self) -> Dict[str, str]:
        """返回扩展名→技能名映射（由SKILL.md的file_extensions字段自动构建）"""
        return dict(self._extension_map)

    # ── 内部方法 ──────────────────────────────────────────

    async def _load_skills(self) -> None:
        """扫描skills目录，解析所有SKILL.md并构建缓存和映射"""
        if not os.path.isdir(self._skills_dir):
            logger.warning(f"Skills目录不存在: {self._skills_dir}")
            return

        for item in os.listdir(self._skills_dir):
            skill_path = os.path.join(self._skills_dir, item)
            if not os.path.isdir(skill_path):
                continue
            skill = await self._parse_skill_dir(skill_path)
            if skill:
                self._cache[skill.name] = skill
                if skill.enabled:
                    self._build_extension_map(skill)

        logger.info(f"加载{len(self._cache)}个技能: {list(self._cache.keys())}")

    def _build_extension_map(self, skill: Skill) -> None:
        """根据技能的file_extensions字段构建扩展名映射，自动补全前导点号"""
        for ext in skill.file_extensions:
            normalized = ext.lower()
            if not normalized.startswith("."):
                normalized = f".{normalized}"
            self._extension_map[normalized] = skill.name

    async def _parse_skill_dir(self, skill_path: str) -> Optional[Skill]:
        """解析单个技能目录，返回Skill对象或None"""
        md_path = os.path.join(skill_path, "SKILL.md")
        if not os.path.isfile(md_path):
            return None
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                content = f.read()
            frontmatter, body = self._parse_frontmatter(content)
            if not frontmatter:
                logger.warning(f"SKILL.md缺少YAML前置元数据: {md_path}")
                return None
            return self._build_skill(frontmatter, body, skill_path)
        except Exception as e:
            logger.error(f"解析技能目录失败[{skill_path}]: {str(e)}")
            return None

    @staticmethod
    def _parse_frontmatter(content: str) -> Tuple[Optional[dict], str]:
        """解析SKILL.md的YAML frontmatter，返回(frontmatter_dict, body_text)"""
        match = _YAML_FRONTMATTER_PATTERN.match(content)
        if not match:
            return None, content
        try:
            frontmatter = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            return None, content
        body = content[match.end():]
        return frontmatter, body

    @staticmethod
    def _build_skill(frontmatter: dict, body: str, skill_path: str) -> Skill:
        """从frontmatter字典构建Skill对象，含依赖门控检测"""
        dependency = _parse_dependency(frontmatter.get("dependency"))
        metadata = _parse_metadata(frontmatter.get("metadata"))
        requires = _parse_requires(frontmatter)
        file_extensions = _parse_file_extensions(frontmatter.get("file_extensions", []))

        enabled = frontmatter.get("enabled", True)
        gating_reason = ""

        if enabled and requires:
            gating_reason = _check_requires(requires)
            if gating_reason:
                enabled = False
                logger.info(f"技能[{frontmatter.get('name', '')}]依赖门控禁用: {gating_reason}")

        return Skill(
            name=frontmatter.get("name", os.path.basename(skill_path)),
            version=frontmatter.get("version") or (metadata.version if metadata else None) or "1.0.0",
            description=frontmatter.get("description", ""),
            keywords=frontmatter.get("keywords"),
            file_extensions=file_extensions,
            enabled=enabled,
            path=skill_path,
            dependency=dependency,
            requires=requires,
            permissions=frontmatter.get("permissions"),
            content=body.strip(),
            category=frontmatter.get("category") or (metadata.category if metadata else None),
            metadata=metadata,
            gating_reason=gating_reason,
        )


# ── 模块级解析与门控函数 ──────────────────────────────────

def _parse_dependency(dep_data) -> Optional[SkillDependency]:
    """解析dependency字段（Python/系统包依赖声明）"""
    if not dep_data or not isinstance(dep_data, dict):
        return None
    return SkillDependency(
        python=dep_data.get("python", []),
        system=dep_data.get("system", []),
    )


def _parse_metadata(meta_data) -> Optional[SkillMetadata]:
    """解析metadata兼容块"""
    if not meta_data or not isinstance(meta_data, dict):
        return None
    return SkillMetadata(
        version=meta_data.get("version"),
        category=meta_data.get("category"),
        sources=meta_data.get("sources"),
    )


def _parse_requires(frontmatter: dict) -> Optional[SkillRequires]:
    """解析requires字段，兼容两种声明格式:
    1. 顶层requires字段: requires: {bins: [...], env: [...]}
    2. metadata.openclaw.requires: metadata: {"openclaw": {"requires": {"bins": [...]}}}
    """
    requires_data = frontmatter.get("requires")
    if not requires_data or not isinstance(requires_data, dict):
        requires_data = _extract_requires_from_metadata(frontmatter.get("metadata"))
    if not requires_data:
        return None
    return SkillRequires(
        bins=requires_data.get("bins", []),
        any_bins=requires_data.get("anyBins", requires_data.get("any_bins", [])),
        env=requires_data.get("env", []),
    )


def _extract_requires_from_metadata(meta_data) -> Optional[dict]:
    """从metadata.openclaw.requires中提取依赖声明（兼容OpenClaw格式）"""
    if not meta_data or not isinstance(meta_data, dict):
        return None
    openclaw = meta_data.get("openclaw")
    if not openclaw or not isinstance(openclaw, dict):
        return None
    requires = openclaw.get("requires")
    if requires and isinstance(requires, dict):
        return requires
    return None


def _check_requires(requires: SkillRequires) -> str:
    """检查依赖门控条件，返回空字符串表示通过，否则返回禁用原因"""
    reasons = []

    if requires.bins:
        missing = [b for b in requires.bins if not shutil.which(b)]
        if missing:
            reasons.append(f"缺少命令: {', '.join(missing)}")

    if requires.any_bins:
        if not any(shutil.which(b) for b in requires.any_bins):
            reasons.append(f"缺少任一命令: {', '.join(requires.any_bins)}")

    if requires.env:
        missing_env = [e for e in requires.env if not os.environ.get(e)]
        if missing_env:
            reasons.append(f"缺少环境变量: {', '.join(missing_env)}")

    return "; ".join(reasons)


def _parse_file_extensions(file_extensions) -> List[str]:
    """解析file_extensions字段，支持列表和逗号分隔字符串两种格式"""
    if isinstance(file_extensions, str):
        return [e.strip() for e in file_extensions.split(",") if e.strip()]
    return file_extensions or []
