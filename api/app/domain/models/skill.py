#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : skill.py
Skills领域模型 - 技能核心数据结构，支持声明式元数据驱动的动态扩展

设计原则:
- 技能通过SKILL.md frontmatter声明自身能力(file_extensions/enabled/keywords/requires)
- 新增技能只需创建目录+SKILL.md，无需修改框架代码
- enabled字段控制技能可见性，requires门控自动检测运行时依赖
- 依赖不满足时技能自动禁用，无需手动设置enabled: false
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class SkillDependency(BaseModel):
    """技能运行时依赖声明（dependency字段，面向Python/系统包管理）"""
    python: List[str] = Field(default_factory=list, description="Python包依赖，如['pypdf>=3.0']")
    system: List[str] = Field(default_factory=list, description="系统命令依赖，如['poppler-utils']")


class SkillRequires(BaseModel):
    """技能运行时依赖门控（requires字段，兼容OpenClaw AgentSkills规范）

    加载时检查:
    - bins: 所有命令必须存在于PATH中
    - any_bins: 至少一个命令存在于PATH中
    - env: 所有环境变量必须已设置
    不满足时技能自动禁用（enabled=False），无需手动配置
    """
    bins: List[str] = Field(default_factory=list, description="必须全部存在的命令，如['summarize']")
    any_bins: List[str] = Field(default_factory=list, description="至少一个存在的命令，如['node', 'bun']")
    env: List[str] = Field(default_factory=list, description="必须全部存在的环境变量，如['GEMINI_API_KEY']")


class SkillMetadata(BaseModel):
    """技能兼容元数据（保留字段，用于SKILL.md中metadata块）"""
    version: Optional[str] = None
    category: Optional[str] = None
    sources: Optional[List[str]] = None


class Skill(BaseModel):
    """技能领域模型 - 对应一个skills子目录下的SKILL.md"""
    name: str = Field(description="技能名称，与SKILL.md中name字段一致")
    version: str = Field(default="1.0.0", description="版本号")
    description: str = Field(description="技能功能描述")
    keywords: Optional[List[str]] = Field(default=None, description="语义匹配关键词")
    file_extensions: List[str] = Field(default_factory=list, description="支持的文件扩展名，如['.pdf','.doc']")
    enabled: bool = Field(default=True, description="是否启用，False则对Agent不可见且不参与匹配")
    path: str = Field(description="技能根目录绝对路径")
    dependency: Optional[SkillDependency] = Field(default=None, description="运行时依赖")
    requires: Optional[SkillRequires] = Field(default=None, description="依赖门控声明，不满足时自动禁用")
    permissions: Optional[List[str]] = Field(default=None, description="所需权限列表")
    content: str = Field(default="", description="SKILL.md正文（操作指南）")
    category: Optional[str] = Field(default=None, description="技能分类，如document/automation")
    metadata: Optional[SkillMetadata] = Field(default=None, description="兼容元数据块")
    gating_reason: str = Field(default="", description="依赖门控禁用原因，空字符串表示未被门控禁用")

    def to_dict_for_prompt(self, lightweight: bool = True) -> Dict[str, Any]:
        """转换为提示词用的字典，lightweight模式仅含名称/描述/分类"""
        if lightweight:
            return {"name": self.name, "description": self.description, "category": self.category}
        return {
            "name": self.name, "version": self.version,
            "description": self.description, "keywords": self.keywords,
            "file_extensions": self.file_extensions, "category": self.category,
            "dependency": self.dependency.model_dump() if self.dependency else None,
            "requires": self.requires.model_dump() if self.requires else None,
            "permissions": self.permissions,
        }
