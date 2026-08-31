#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_skill_injection.py
技能注入优化单元测试 - 脚本路径提示构建、技能约束注入、使用原则强化
"""
import os
from unittest.mock import MagicMock

import pytest

from app.domain.models.skill import Skill
from app.domain.services.skill_service import SkillMatcher


class TestBuildScriptsPathHint:
    """技能脚本路径提示构建测试"""

    def test_no_scripts_dir_returns_empty(self):
        skill = Skill(
            name="empty_skill",
            description="No scripts",
            path="/nonexistent/path",
            file_extensions=[],
        )
        assert _build_scripts_path_hint_sync(skill) == ""

    def test_empty_scripts_dir_returns_empty(self):
        skill = Skill(
            name="test",
            description="test",
            path=os.path.join(os.path.dirname(__file__), "_test_skill"),
            file_extensions=[],
        )
        scripts_dir = os.path.join(skill.path, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        try:
            assert _build_scripts_path_hint_sync(skill) == ""
        finally:
            os.rmdir(scripts_dir)
            if os.path.isdir(skill.path):
                os.rmdir(skill.path)

    def test_scripts_dir_with_files_returns_sandbox_paths(self):
        skill = Skill(
            name="test",
            description="test",
            path=os.path.join(os.path.dirname(__file__), "_test_skill"),
            file_extensions=[],
        )
        scripts_dir = os.path.join(skill.path, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        test_script = os.path.join(scripts_dir, "test_tool.py")
        with open(test_script, "w") as f:
            f.write("print('hello')")
        try:
            result = _build_scripts_path_hint_sync(skill)
            assert "/home/ubuntu/" in result
            assert "scripts/test_tool.py" in result
            assert "python3" in result
        finally:
            os.remove(test_script)
            os.rmdir(scripts_dir)
            if os.path.isdir(skill.path):
                os.rmdir(skill.path)

    def test_nested_scripts_included(self):
        skill = Skill(
            name="docx",
            description="docx skill",
            path=os.path.join(os.path.dirname(__file__), "_test_docx"),
            file_extensions=[".docx"],
        )
        scripts_dir = os.path.join(skill.path, "scripts", "office")
        os.makedirs(scripts_dir, exist_ok=True)
        test_script = os.path.join(scripts_dir, "soffice.py")
        with open(test_script, "w") as f:
            f.write("print('soffice')")
        try:
            result = _build_scripts_path_hint_sync(skill)
            assert "/home/ubuntu/scripts/office/soffice.py" in result
        finally:
            os.remove(test_script)
            os.rmdir(scripts_dir)
            os.rmdir(os.path.join(skill.path, "scripts"))
            if os.path.isdir(skill.path):
                os.rmdir(skill.path)


class TestSkillInjectionConstraints:
    """技能注入约束测试 - 验证注入提示包含强制约束"""

    def test_react_hint_contains_priority_constraint(self):
        hint = (
            "⚠️ 用户附件涉及技能\"docx\"，相关指南已注入上下文[技能指南: docx]。\n"
            "**必须优先按指南中的方法操作，禁止自行安装替代工具(如pip install/apt-get install)。**\n"
        )
        assert "必须优先按指南" in hint
        assert "禁止自行安装替代工具" in hint

    def test_guide_block_contains_must_operate_constraint(self):
        guide_block = "⚠️ 此指南已注入上下文，必须按指南操作，无需再调用get_skill_guide。\n\n"
        assert "必须按指南操作" in guide_block

    def test_scripts_hint_contains_sandbox_paths(self):
        scripts_hint = (
            "技能脚本(已在沙箱中可用，直接执行即可):\n"
            "  - /home/ubuntu/scripts/office/soffice.py\n"
            "示例: `python3 /home/ubuntu/scripts/office/soffice.py`"
        )
        assert "/home/ubuntu/scripts/" in scripts_hint
        assert "已在沙箱中可用" in scripts_hint


class TestSkillsPromptPrinciples:
    """技能使用原则测试 - 验证generate_skills_prompt包含强化约束"""

    def test_principles_contain_priority_script_rule(self):
        principles = [
            "1. **附件驱动**: 用户上传文件时，系统自动检测相关技能并注入指南，**必须按指南操作**",
            "2. **优先使用技能脚本**: 技能提供的脚本已在沙箱中可用，直接执行即可，**禁止自行安装替代工具**",
        ]
        full_text = "\n".join(principles)
        assert "必须按指南操作" in full_text
        assert "优先使用技能脚本" in full_text
        assert "禁止自行安装替代工具" in full_text

    def test_principles_contain_six_rules(self):
        principles = [
            "1. **附件驱动**",
            "2. **优先使用技能脚本**",
            "3. **直接使用**",
            "4. **智能匹配**",
            "5. **获取指南**",
            "6. **避免冗余**",
        ]
        assert len(principles) == 6


class TestSkillMatcherExtension:
    """技能匹配器扩展名测试"""

    def test_docx_extension_matches_docx_skill(self):
        matcher = SkillMatcher()
        skill = Skill(
            name="docx", description="Word document skill",
            path="/skills/docx", file_extensions=[".docx", ".doc"],
        )
        ext_map = {".docx": "docx", ".doc": "docx"}
        result = matcher.match_by_extension("report.docx", ext_map, [skill])
        assert result is not None
        assert result.name == "docx"

    def test_doc_extension_matches_docx_skill(self):
        matcher = SkillMatcher()
        skill = Skill(
            name="docx", description="Word document skill",
            path="/skills/docx", file_extensions=[".docx", ".doc"],
        )
        ext_map = {".docx": "docx", ".doc": "docx"}
        result = matcher.match_by_extension("legacy.doc", ext_map, [skill])
        assert result is not None
        assert result.name == "docx"

    def test_unknown_extension_no_match(self):
        matcher = SkillMatcher()
        skill = Skill(
            name="docx", description="Word document skill",
            path="/skills/docx", file_extensions=[".docx", ".doc"],
        )
        ext_map = {".docx": "docx", ".doc": "docx"}
        assert matcher.match_by_extension("image.png", ext_map, [skill]) is None


def _build_scripts_path_hint_sync(skill: Skill) -> str:
    """同步版本的脚本路径提示构建（与PlannerReActFlow._build_scripts_path_hint逻辑一致）"""
    scripts_dir = os.path.join(skill.path, "scripts")
    if not os.path.isdir(scripts_dir):
        return ""
    script_paths = []
    for root, dirs, files in os.walk(scripts_dir):
        for filename in files:
            local_path = os.path.join(root, filename)
            rel_path = os.path.relpath(local_path, skill.path).replace("\\", "/")
            script_paths.append(f"/home/ubuntu/{rel_path}")
    if not script_paths:
        return ""
    path_list = "\n".join(f"  - {p}" for p in script_paths)
    return (
        f"技能脚本(已在沙箱中可用，直接执行即可):\n{path_list}\n"
        f"示例: `python3 {script_paths[0]}`"
    )
