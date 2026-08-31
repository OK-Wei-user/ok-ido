#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_skills.py
Skills系统单元测试 - 覆盖模型、仓库、匹配器、追踪器、服务、工具、缓存、附件注入

测试分层:
- TestSkillModel: 领域模型字段默认值与序列化
- TestFileSkillRepository: 仓库加载、缓存、扩展名映射、disabled技能
- TestSkillMatcher: 多维度匹配评分、扩展名匹配、大小写不敏感
- TestSkillContextTracker: 使用历史记录、去重、会话隔离
- TestSkillService: 服务层查询、过滤、检测、截断、动态映射
- TestSkillsPromptCache: 缓存初始化、会话上下文、刷新
- TestDetectAndInjectAttachmentSkills: 附件技能注入（双通道+幂等+异常安全）
- TestAppendToSystemPrompt: memory通道注入（幂等+空memory保护）
- TestSkillTool: Agent工具协议（list/match/guide/usage）
- TestDynamicExtension: 声明式file_extensions动态扩展验证
- TestAttachmentPathFallback: 空路径过滤与filename回退
- TestRegression: 回归测试
"""
import os
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.models.skill import Skill, SkillDependency, SkillMetadata, SkillRequires
from app.domain.models.message import Message
from app.domain.services.skill_service import (
    SkillService, SkillMatcher, SkillContextTracker, SkillMatch,
    _MAX_GUIDE_LENGTH, _MAX_INJECTION_LENGTH,
)
from app.domain.services.skills_prompt_cache import SkillsPromptCache
from app.domain.services.flows.planner_react import PlannerReActFlow
from app.infrastructure.repositories.file_skill_repository import FileSkillRepository


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def sample_skill():
    return Skill(
        name="pdf", version="1.1.0",
        description="Comprehensive PDF processing including text extraction and form filling",
        keywords=["pdf", "form", "extraction", "annotation"],
        file_extensions=[".pdf"], enabled=True, path="/skills/pdf",
        dependency=SkillDependency(python=["pypdf>=3.0", "pdfplumber>=0.9"], system=["poppler-utils"]),
        permissions=["file_read", "file_write", "shell_exec"],
        content="# PDF Processing Guide\n\nDetailed instructions...",
        category="document",
    )


def _write_skill_md(tmpdir, name, description, file_extensions=None, enabled=True, keywords=None, category="document", extra=""):
    """辅助函数: 在tmpdir下创建一个技能目录和SKILL.md"""
    d = os.path.join(tmpdir, name)
    os.makedirs(d, exist_ok=True)
    ext_str = str(file_extensions or [])
    kw_str = str(keywords or [])
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"""---
name: {name}
description: {description}
keywords: {kw_str}
file_extensions: {ext_str}
enabled: {str(enabled).lower()}
category: {category}
{extra}
---

# {name} Guide
""")
    return d


@pytest.fixture
def skills_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_skill_md(tmpdir, "pdf", "Comprehensive PDF processing",
                        file_extensions=[".pdf"], keywords=["pdf", "form", "extraction"],
                        extra="dependency:\n  python: [pypdf>=3.0]\n  system: [poppler-utils]\npermissions: [file_read, file_write]")
        _write_skill_md(tmpdir, "docx", "Create and edit Word documents",
                        file_extensions=[".docx", ".doc"], keywords=["docx", "word", "document"])
        yield tmpdir


@pytest.fixture
def skills_dir_with_disabled():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_skill_md(tmpdir, "pdf", "PDF processing", file_extensions=[".pdf"], keywords=["pdf"])
        _write_skill_md(tmpdir, "Agent Browser", "Browser automation", file_extensions=[], enabled=False, keywords=["browser", "web"], category="automation")
        yield tmpdir


@pytest.fixture(autouse=True)
def reset_cache():
    SkillsPromptCache.reset()
    yield
    SkillsPromptCache.reset()


# ── TestSkillModel ────────────────────────────────────────

class TestSkillModel:
    def test_skill_creation(self, sample_skill):
        assert sample_skill.name == "pdf"
        assert sample_skill.version == "1.1.0"
        assert sample_skill.category == "document"
        assert sample_skill.file_extensions == [".pdf"]
        assert sample_skill.enabled is True
        assert len(sample_skill.dependency.python) == 2

    def test_skill_default_enabled(self):
        assert Skill(name="test", description="test", path="/s/test").enabled is True

    def test_skill_default_file_extensions(self):
        assert Skill(name="test", description="test", path="/s/test").file_extensions == []

    def test_to_dict_lightweight(self, sample_skill):
        result = sample_skill.to_dict_for_prompt(lightweight=True)
        assert "name" in result and "description" in result
        assert "dependency" not in result and "file_extensions" not in result

    def test_to_dict_full(self, sample_skill):
        result = sample_skill.to_dict_for_prompt(lightweight=False)
        assert all(k in result for k in ["name", "version", "file_extensions", "dependency", "permissions"])


# ── TestFileSkillRepository ───────────────────────────────

class TestFileSkillRepository:
    @pytest.mark.asyncio
    async def test_load_skills(self, skills_dir):
        skills = await FileSkillRepository(skills_dir=skills_dir).get_all()
        assert len(skills) == 2
        assert {"pdf", "docx"} == {s.name for s in skills}

    @pytest.mark.asyncio
    async def test_get_by_name(self, skills_dir):
        skill = await FileSkillRepository(skills_dir=skills_dir).get_by_name("pdf")
        assert skill is not None and skill.name == "pdf" and skill.file_extensions == [".pdf"] and skill.enabled is True

    @pytest.mark.asyncio
    async def test_get_by_name_not_found(self, skills_dir):
        assert await FileSkillRepository(skills_dir=skills_dir).get_by_name("nonexistent") is None

    @pytest.mark.asyncio
    async def test_refresh(self, skills_dir):
        repo = FileSkillRepository(skills_dir=skills_dir)
        await repo.get_all()
        await repo.refresh()
        assert len(await repo.get_all()) == 2

    @pytest.mark.asyncio
    async def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assert len(await FileSkillRepository(skills_dir=tmpdir).get_all()) == 0

    @pytest.mark.asyncio
    async def test_nonexistent_dir(self):
        assert len(await FileSkillRepository(skills_dir="/nonexistent/path").get_all()) == 0

    @pytest.mark.asyncio
    async def test_extension_map_auto_built(self, skills_dir):
        repo = FileSkillRepository(skills_dir=skills_dir)
        await repo.get_all()
        ext_map = repo.get_extension_map()
        assert ext_map == {".pdf": "pdf", ".docx": "docx", ".doc": "docx"}

    @pytest.mark.asyncio
    async def test_extension_map_cleared_on_refresh(self, skills_dir):
        repo = FileSkillRepository(skills_dir=skills_dir)
        await repo.get_all()
        await repo.refresh()
        assert ".pdf" in repo.get_extension_map()

    @pytest.mark.asyncio
    async def test_disabled_skill_still_loaded_in_cache(self, skills_dir_with_disabled):
        skills = await FileSkillRepository(skills_dir=skills_dir_with_disabled).get_all()
        assert "Agent Browser" in {s.name for s in skills}

    @pytest.mark.asyncio
    async def test_disabled_skill_not_in_extension_map(self, skills_dir_with_disabled):
        repo = FileSkillRepository(skills_dir=skills_dir_with_disabled)
        await repo.get_all()
        assert len(repo.get_extension_map()) == 1  # only .pdf

    @pytest.mark.asyncio
    async def test_parse_file_extensions_string(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "test-skill", "Test", file_extensions=None)
            d = os.path.join(tmpdir, "test-skill")
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write('---\nname: test-skill\ndescription: Test\nfile_extensions: ".csv, .tsv"\n---\n\n# Test\n')
            skills = await FileSkillRepository(skills_dir=tmpdir).get_all()
            assert ".csv" in skills[0].file_extensions and ".tsv" in skills[0].file_extensions


# ── TestSkillMatcher ──────────────────────────────────────

class TestSkillMatcher:
    def test_match_by_name(self):
        skills = [Skill(name="pdf", description="PDF processing", path="/s/pdf"), Skill(name="docx", description="Word editing", path="/s/docx")]
        assert SkillMatcher().match("I need to process a PDF file", skills)[0].skill.name == "pdf"

    def test_match_by_extension(self):
        skills = [Skill(name="xlsx", description="Excel", file_extensions=[".xlsx"], path="/s/xlsx")]
        assert SkillMatcher().match_by_extension("report.xlsx", {".xlsx": "xlsx"}, skills).name == "xlsx"

    def test_match_by_extension_no_match(self):
        assert SkillMatcher().match_by_extension("photo.xyz", {}, [Skill(name="pdf", description="PDF", path="/s/pdf")]) is None

    def test_match_no_results_below_threshold(self):
        assert len(SkillMatcher().match("cooking recipe", [Skill(name="pdf", description="PDF", path="/s/pdf")], min_score=10.0)) == 0

    def test_match_default_top_n(self):
        skills = [Skill(name=f"s{i}", description=f"skill {i}", path=f"/s/s{i}") for i in range(10)]
        assert len(SkillMatcher().match("skill", skills, min_score=0.0)) == 5

    def test_extract_extension(self):
        assert SkillMatcher._extract_extension("report.pdf") == ".pdf"
        assert SkillMatcher._extract_extension("my.report.final.xlsx") == ".xlsx"
        assert SkillMatcher._extract_extension("filename") is None

    def test_match_by_extension_full_map(self):
        matcher = SkillMatcher()
        skills = [
            Skill(name="pdf", description="PDF", file_extensions=[".pdf"], path="/s/pdf"),
            Skill(name="docx", description="Word", file_extensions=[".docx", ".doc"], path="/s/docx"),
            Skill(name="xlsx", description="Excel", file_extensions=[".xlsx", ".xls", ".csv"], path="/s/xlsx"),
            Skill(name="pptx", description="PPT", file_extensions=[".pptx", ".ppt"], path="/s/pptx"),
            Skill(name="markdown-converter", description="Markdown", file_extensions=[".md", ".markdown", ".html", ".htm"], path="/s/md"),
        ]
        ext_map = {".pdf": "pdf", ".docx": "docx", ".doc": "docx", ".xlsx": "xlsx", ".xls": "xlsx", ".csv": "xlsx",
                   ".pptx": "pptx", ".ppt": "pptx", ".md": "markdown-converter", ".markdown": "markdown-converter",
                   ".html": "markdown-converter", ".htm": "markdown-converter"}
        for filename, expected in [("file.pdf", "pdf"), ("file.doc", "docx"), ("file.xls", "xlsx"), ("file.ppt", "pptx"), ("file.htm", "markdown-converter")]:
            result = matcher.match_by_extension(filename, ext_map, skills)
            assert result is not None and result.name == expected, f"{filename} should match {expected}"

    def test_match_by_extension_case_insensitive(self):
        assert SkillMatcher().match_by_extension("report.PDF", {".pdf": "pdf"}, [Skill(name="pdf", description="PDF", path="/s/pdf")]).name == "pdf"

    def test_match_returns_score_and_keywords(self):
        matches = SkillMatcher().match("process a PDF form", [Skill(name="pdf", description="PDF processing", keywords=["pdf", "form"], path="/s/pdf")])
        assert len(matches) == 1 and matches[0].score > 0 and matches[0].match_reason != ""

    def test_match_by_file_extensions_field(self):
        matches = SkillMatcher().match("I have a .xlsx file", [Skill(name="xlsx", description="Excel", file_extensions=[".xlsx", ".xls"], path="/s/xlsx")], min_score=0.0)
        assert len(matches) > 0 and matches[0].skill.name == "xlsx"


# ── TestSkillContextTracker ───────────────────────────────

class TestSkillContextTracker:
    def test_record_and_get_recent(self):
        tracker = SkillContextTracker()
        tracker.record_usage("s1", "pdf", "extract")
        tracker.record_usage("s1", "docx", "edit")
        tracker.record_usage("s1", "pdf", "fill")
        recent = tracker.get_recent_skills("s1", limit=2)
        assert recent == ["pdf", "docx"]

    def test_clear_session(self):
        tracker = SkillContextTracker()
        tracker.record_usage("s1", "pdf")
        tracker.clear_session("s1")
        assert len(tracker.get_recent_skills("s1")) == 0

    def test_max_history(self):
        tracker = SkillContextTracker()
        tracker.MAX_HISTORY = 5
        for i in range(10):
            tracker.record_usage("s1", f"skill_{i}")
        assert len(tracker.get_recent_skills("s1", limit=10)) <= 5

    def test_get_recent_empty_session(self):
        assert len(SkillContextTracker().get_recent_skills("nonexistent")) == 0

    def test_dedup_preserves_order(self):
        tracker = SkillContextTracker()
        tracker.record_usage("s1", "xlsx")
        tracker.record_usage("s1", "pdf")
        tracker.record_usage("s1", "xlsx")
        tracker.record_usage("s1", "docx")
        assert tracker.get_recent_skills("s1", limit=10) == ["docx", "xlsx", "pdf"]


# ── TestSkillService ──────────────────────────────────────

class TestSkillService:
    @pytest.mark.asyncio
    async def test_list_skills(self, skills_dir):
        assert len(await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).list_skills()) == 2

    @pytest.mark.asyncio
    async def test_list_skills_filters_disabled(self, skills_dir_with_disabled):
        names = [s.name for s in await SkillService(repository=FileSkillRepository(skills_dir=skills_dir_with_disabled)).list_skills()]
        assert "pdf" in names and "Agent Browser" not in names

    @pytest.mark.asyncio
    async def test_match_skills(self, skills_dir):
        matches = await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).match_skills("edit a Word document")
        assert any(m.skill.name == "docx" for m in matches)

    @pytest.mark.asyncio
    async def test_get_skill_guide(self, skills_dir):
        guide = await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).get_skill_guide("pdf")
        assert guide is not None and "pdf Guide" in guide

    @pytest.mark.asyncio
    async def test_get_skill_guide_not_found(self, skills_dir):
        assert await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).get_skill_guide("nonexistent") is None

    @pytest.mark.asyncio
    async def test_get_skill_guide_truncation(self, skills_dir):
        guide = await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).get_skill_guide("pdf", max_length=10)
        assert guide is not None and "截断" in guide

    @pytest.mark.asyncio
    async def test_get_skill_guide_no_truncation_when_short(self, skills_dir):
        guide = await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).get_skill_guide("pdf", max_length=10000)
        assert guide is not None and "截断" not in guide

    @pytest.mark.asyncio
    async def test_get_skill_guide_default_max_length(self):
        assert _MAX_GUIDE_LENGTH == 16000

    @pytest.mark.asyncio
    async def test_generate_skills_prompt(self, skills_dir):
        prompt = await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).generate_skills_prompt()
        assert "<skills_index>" in prompt and "pdf" in prompt and "docx" in prompt

    @pytest.mark.asyncio
    async def test_generate_skills_prompt_with_session(self, skills_dir):
        svc = SkillService(repository=FileSkillRepository(skills_dir=skills_dir))
        svc.record_usage("s1", "pdf")
        prompt = await svc.generate_skills_prompt(session_id="s1")
        assert "最近使用" in prompt and "pdf" in prompt

    @pytest.mark.asyncio
    async def test_generate_skills_prompt_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assert await SkillService(repository=FileSkillRepository(skills_dir=tmpdir)).generate_skills_prompt() == ""

    @pytest.mark.asyncio
    async def test_generate_skills_prompt_has_all_principles(self, skills_dir):
        prompt = await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).generate_skills_prompt()
        for keyword in ["附件驱动", "无需再调用get_skill_guide", "直接使用", "智能匹配", "避免冗余"]:
            assert keyword in prompt

    @pytest.mark.asyncio
    async def test_detect_skills_from_attachments_dedup(self, skills_dir):
        _write_skill_md(skills_dir, "xlsx", "Excel processing", file_extensions=[".xlsx", ".xls", ".csv"])
        svc = SkillService(repository=FileSkillRepository(skills_dir=skills_dir))
        result = await svc.detect_skills_from_attachments(["/home/ubuntu/upload/a.xlsx", "/home/ubuntu/upload/b.xls"])
        names = [s.name for s in result]
        assert "xlsx" in names and names.count("xlsx") == 1

    @pytest.mark.asyncio
    async def test_detect_skills_from_attachments_no_match(self, skills_dir):
        assert len(await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).detect_skills_from_attachments(["/home/ubuntu/upload/photo.xyz"])) == 0

    @pytest.mark.asyncio
    async def test_detect_skills_from_attachments_empty(self, skills_dir):
        assert len(await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).detect_skills_from_attachments([])) == 0

    @pytest.mark.asyncio
    async def test_detect_skills_from_attachments_multiple(self, skills_dir):
        _write_skill_md(skills_dir, "xlsx", "Excel processing", file_extensions=[".xlsx", ".xls", ".csv"])
        result = await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).detect_skills_from_attachments(["/home/ubuntu/upload/a.pdf", "/home/ubuntu/upload/b.xlsx"])
        assert {"pdf", "xlsx"} == {s.name for s in result}

    @pytest.mark.asyncio
    async def test_detect_skills_filters_disabled(self, skills_dir_with_disabled):
        result = await SkillService(repository=FileSkillRepository(skills_dir=skills_dir_with_disabled)).detect_skills_from_attachments(["/home/ubuntu/upload/file.pdf"])
        assert "pdf" in [s.name for s in result] and "Agent Browser" not in [s.name for s in result]

    @pytest.mark.asyncio
    async def test_match_by_filename(self, skills_dir):
        skill = await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).match_by_filename("report.pdf")
        assert skill is not None and skill.name == "pdf"

    @pytest.mark.asyncio
    async def test_match_by_filename_no_match(self, skills_dir):
        assert await SkillService(repository=FileSkillRepository(skills_dir=skills_dir)).match_by_filename("photo.xyz") is None

    @pytest.mark.asyncio
    async def test_match_skills_filters_disabled(self, skills_dir_with_disabled):
        matches = await SkillService(repository=FileSkillRepository(skills_dir=skills_dir_with_disabled)).match_skills("browse the web")
        assert all(m.skill.enabled for m in matches)

    @pytest.mark.asyncio
    async def test_truncate_guide_short(self):
        assert SkillService.truncate_guide_for_injection("Short") == "Short"

    @pytest.mark.asyncio
    async def test_truncate_guide_long(self):
        result = SkillService.truncate_guide_for_injection("x" * (_MAX_INJECTION_LENGTH + 1000))
        assert len(result) < _MAX_INJECTION_LENGTH + 1000 and "截断" in result

    @pytest.mark.asyncio
    async def test_extension_map_dynamically_built(self, skills_dir):
        ext_map = await SkillService(repository=FileSkillRepository(skills_dir=skills_dir))._get_extension_map()
        assert ext_map[".pdf"] == "pdf" and ext_map[".doc"] == "docx"

    @pytest.mark.asyncio
    async def test_refresh_clears_ext_map(self, skills_dir):
        svc = SkillService(repository=FileSkillRepository(skills_dir=skills_dir))
        await svc._get_extension_map()
        await svc.refresh()
        assert svc._ext_map is None


# ── TestSkillsPromptCache ─────────────────────────────────

class TestSkillsPromptCache:
    @pytest.mark.asyncio
    async def test_initialize_and_get_prompt(self, skills_dir):
        await SkillsPromptCache.initialize(SkillService(repository=FileSkillRepository(skills_dir=skills_dir)))
        prompt = SkillsPromptCache.get_prompt()
        assert "<skills_index>" in prompt and "pdf" in prompt

    @pytest.mark.asyncio
    async def test_get_prompt_with_session_id(self, skills_dir):
        svc = SkillService(repository=FileSkillRepository(skills_dir=skills_dir))
        svc.record_usage("s1", "pdf")
        await SkillsPromptCache.initialize(svc)
        assert "最近使用" in SkillsPromptCache.get_prompt(session_id="s1")

    @pytest.mark.asyncio
    async def test_get_prompt_without_session_no_recent(self, skills_dir):
        svc = SkillService(repository=FileSkillRepository(skills_dir=skills_dir))
        svc.record_usage("s1", "pdf")
        await SkillsPromptCache.initialize(svc)
        assert "最近使用" not in SkillsPromptCache.get_prompt()

    @pytest.mark.asyncio
    async def test_get_prompt_empty_when_not_initialized(self):
        assert SkillsPromptCache.get_prompt() == ""

    @pytest.mark.asyncio
    async def test_reset_clears_cache(self, skills_dir):
        await SkillsPromptCache.initialize(SkillService(repository=FileSkillRepository(skills_dir=skills_dir)))
        SkillsPromptCache.reset()
        assert SkillsPromptCache.get_prompt() == ""

    @pytest.mark.asyncio
    async def test_refresh(self, skills_dir):
        await SkillsPromptCache.initialize(SkillService(repository=FileSkillRepository(skills_dir=skills_dir)))
        await SkillsPromptCache.refresh(SkillService(repository=FileSkillRepository(skills_dir=skills_dir)))
        assert "<skills_index>" in SkillsPromptCache.get_prompt()

    @pytest.mark.asyncio
    async def test_is_initialized(self, skills_dir):
        assert not SkillsPromptCache.is_initialized()
        await SkillsPromptCache.initialize(SkillService(repository=FileSkillRepository(skills_dir=skills_dir)))
        assert SkillsPromptCache.is_initialized()

    @pytest.mark.asyncio
    async def test_get_prompt_session_no_recent(self, skills_dir):
        await SkillsPromptCache.initialize(SkillService(repository=FileSkillRepository(skills_dir=skills_dir)))
        prompt = SkillsPromptCache.get_prompt(session_id="nonexistent")
        assert "最近使用" not in prompt and "<skills_index>" in prompt


# ── TestDetectAndInjectAttachmentSkills ───────────────────

class TestDetectAndInjectAttachmentSkills:
    @pytest.mark.asyncio
    async def test_no_attachments_returns_early(self):
        mock_svc = AsyncMock(spec=SkillService)
        flow = MagicMock(_skill_service=mock_svc)
        await PlannerReActFlow._detect_and_inject_attachment_skills(flow, Message(message="test", attachments=[]))
        mock_svc.detect_skills_from_attachments.assert_not_called()

    @pytest.mark.asyncio
    async def test_with_attachments_no_matched_skills(self):
        mock_svc = AsyncMock(spec=SkillService)
        mock_svc.detect_skills_from_attachments = AsyncMock(return_value=[])
        flow = MagicMock(_skill_service=mock_svc, planner=MagicMock(), react=MagicMock())
        with patch.object(PlannerReActFlow, "_append_to_system_prompt", new_callable=AsyncMock):
            await PlannerReActFlow._detect_and_inject_attachment_skills(flow, Message(message="test", attachments=["/path/to/file.xyz"]))
            mock_svc.detect_skills_from_attachments.assert_called_once_with(["/path/to/file.xyz"])

    @pytest.mark.asyncio
    async def test_with_matched_skills_injects_to_both(self):
        pdf_skill = Skill(name="pdf", description="PDF processing", path="/s/pdf", content="# PDF Guide", file_extensions=[".pdf"])
        mock_svc = AsyncMock(spec=SkillService)
        mock_svc.detect_skills_from_attachments = AsyncMock(return_value=[pdf_skill])
        mock_svc.get_skill_guide = AsyncMock(return_value="# PDF Guide")
        flow = MagicMock(_skill_service=mock_svc, _append_to_system_prompt=AsyncMock(),
                         planner=MagicMock(_system_prompt="original"), react=MagicMock(_system_prompt="original"))
        # _build_scripts_path_hint是async方法,MagicMock默认返回非可await对象,需显式AsyncMock
        flow._build_scripts_path_hint = AsyncMock(return_value="")
        await PlannerReActFlow._detect_and_inject_attachment_skills(flow, Message(message="read pdf", attachments=["/path/to/file.pdf"]))
        assert "[附件技能提示: pdf]" in flow.planner._system_prompt
        assert "[技能指南: pdf]" in flow.react._system_prompt
        assert "无需再调用get_skill_guide" in flow.react._system_prompt

    @pytest.mark.asyncio
    async def test_exception_does_not_propagate(self):
        mock_svc = AsyncMock(spec=SkillService)
        mock_svc.detect_skills_from_attachments = AsyncMock(side_effect=RuntimeError("db error"))
        flow = MagicMock(_skill_service=mock_svc)
        try:
            await PlannerReActFlow._detect_and_inject_attachment_skills(flow, Message(message="test", attachments=["/path/to/file.pdf"]))
        except Exception:
            pytest.fail("Exception should not propagate")

    @pytest.mark.asyncio
    async def test_no_guide_still_injects_hint(self):
        pdf_skill = Skill(name="pdf", description="PDF processing", path="/s/pdf", content="")
        mock_svc = AsyncMock(spec=SkillService)
        mock_svc.detect_skills_from_attachments = AsyncMock(return_value=[pdf_skill])
        mock_svc.get_skill_guide = AsyncMock(return_value=None)
        flow = MagicMock(_skill_service=mock_svc, _append_to_system_prompt=AsyncMock(),
                         planner=MagicMock(_system_prompt="original"), react=MagicMock(_system_prompt="original"))
        flow._build_scripts_path_hint = AsyncMock(return_value="")
        await PlannerReActFlow._detect_and_inject_attachment_skills(flow, Message(message="read pdf", attachments=["/path/to/file.pdf"]))
        assert "[附件技能提示: pdf]" in flow.planner._system_prompt
        assert "[技能指南: pdf]" not in flow.react._system_prompt

    @pytest.mark.asyncio
    async def test_system_prompt_idempotent(self):
        pdf_skill = Skill(name="pdf", description="PDF processing", path="/s/pdf", content="# Guide")
        mock_svc = AsyncMock(spec=SkillService)
        mock_svc.detect_skills_from_attachments = AsyncMock(return_value=[pdf_skill])
        mock_svc.get_skill_guide = AsyncMock(return_value="# Guide")
        flow = MagicMock(_skill_service=mock_svc, _append_to_system_prompt=AsyncMock(),
                         planner=MagicMock(_system_prompt="original"), react=MagicMock(_system_prompt="original"))
        flow._build_scripts_path_hint = AsyncMock(return_value="")
        msg = Message(message="read pdf", attachments=["/path/to/file.pdf"])
        await PlannerReActFlow._detect_and_inject_attachment_skills(flow, msg)
        first_planner, first_react = flow.planner._system_prompt, flow.react._system_prompt
        await PlannerReActFlow._detect_and_inject_attachment_skills(flow, msg)
        assert flow.planner._system_prompt == first_planner and flow.react._system_prompt == first_react

    @pytest.mark.asyncio
    async def test_attachment_paths_injected(self):
        pdf_skill = Skill(name="pdf", description="PDF", path="/s/pdf", content="# Guide", file_extensions=[".pdf"])
        mock_svc = AsyncMock(spec=SkillService)
        mock_svc.detect_skills_from_attachments = AsyncMock(return_value=[pdf_skill])
        mock_svc.get_skill_guide = AsyncMock(return_value="# Guide")
        flow = MagicMock(_skill_service=mock_svc, _append_to_system_prompt=AsyncMock(),
                         planner=MagicMock(_system_prompt="original"), react=MagicMock(_system_prompt="original"))
        flow._build_scripts_path_hint = AsyncMock(return_value="")
        await PlannerReActFlow._detect_and_inject_attachment_skills(flow, Message(
            message="read pdf", attachments=["/home/ubuntu/upload/report.pdf", "/home/ubuntu/upload/data.pdf"]))
        assert "/home/ubuntu/upload/report.pdf" in flow.planner._system_prompt


# ── TestAppendToSystemPrompt ──────────────────────────────

class TestAppendToSystemPrompt:
    @pytest.mark.asyncio
    async def test_inject_new_marker(self):
        agent = MagicMock(_memory=MagicMock(messages=[{"role": "system", "content": "base"}]), _uow=AsyncMock(), _session_id="test")
        await PlannerReActFlow._append_to_system_prompt(agent, "[m]", "\n\n[m] injected")
        assert "[m] injected" in agent._memory.messages[0]["content"]

    @pytest.mark.asyncio
    async def test_skip_existing_marker(self):
        agent = MagicMock(_memory=MagicMock(messages=[{"role": "system", "content": "base [m] already"}]), _uow=AsyncMock(), _session_id="test")
        await PlannerReActFlow._append_to_system_prompt(agent, "[m]", "\n\n[m] new")
        assert agent._memory.messages[0]["content"] == "base [m] already"

    @pytest.mark.asyncio
    async def test_no_memory_returns_early(self):
        await PlannerReActFlow._append_to_system_prompt(MagicMock(_memory=None), "[m]", "\n\ncontent")

    @pytest.mark.asyncio
    async def test_empty_messages_returns_early(self):
        await PlannerReActFlow._append_to_system_prompt(MagicMock(_memory=MagicMock(messages=[])), "[m]", "\n\ncontent")


# ── TestSkillTool ─────────────────────────────────────────

class TestSkillTool:
    @pytest.mark.asyncio
    async def test_list_skills(self, skills_dir):
        from app.domain.services.tools.skill import SkillTool
        result = await SkillTool(skill_service=SkillService(repository=FileSkillRepository(skills_dir=skills_dir))).list_skills()
        assert result.success and len(result.data) == 2

    @pytest.mark.asyncio
    async def test_list_skills_empty(self):
        from app.domain.services.tools.skill import SkillTool
        with tempfile.TemporaryDirectory() as tmpdir:
            result = await SkillTool(skill_service=SkillService(repository=FileSkillRepository(skills_dir=tmpdir))).list_skills()
            assert result.success and result.data == []

    @pytest.mark.asyncio
    async def test_match_skills(self, skills_dir):
        from app.domain.services.tools.skill import SkillTool
        result = await SkillTool(skill_service=SkillService(repository=FileSkillRepository(skills_dir=skills_dir))).match_skills(query="edit a Word document")
        assert result.success and any(d["name"] == "docx" for d in result.data)

    @pytest.mark.asyncio
    async def test_get_skill_guide(self, skills_dir):
        from app.domain.services.tools.skill import SkillTool
        result = await SkillTool(skill_service=SkillService(repository=FileSkillRepository(skills_dir=skills_dir))).get_skill_guide(skill_name="pdf")
        assert result.success and "pdf Guide" in result.data["guide"]

    @pytest.mark.asyncio
    async def test_get_skill_guide_not_found(self, skills_dir):
        from app.domain.services.tools.skill import SkillTool
        result = await SkillTool(skill_service=SkillService(repository=FileSkillRepository(skills_dir=skills_dir))).get_skill_guide(skill_name="nonexistent")
        assert not result.success and "不存在" in result.message

    @pytest.mark.asyncio
    async def test_record_skill_usage(self, skills_dir):
        from app.domain.services.tools.skill import SkillTool
        tool = SkillTool(skill_service=SkillService(repository=FileSkillRepository(skills_dir=skills_dir)))
        tool.record_skill_usage("s1", "pdf")
        assert "pdf" in tool.get_recent_skills("s1")


# ── TestDynamicExtension ──────────────────────────────────

class TestDynamicExtension:
    """验证声明式file_extensions的动态扩展能力"""

    @pytest.mark.asyncio
    async def test_new_skill_auto_registered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "json-handler", "JSON processing", file_extensions=[".json", ".jsonl"])
            svc = SkillService(repository=FileSkillRepository(skills_dir=tmpdir))
            ext_map = await svc._get_extension_map()
            assert ext_map[".json"] == "json-handler"
            skill = await svc.match_by_filename("data.json")
            assert skill is not None and skill.name == "json-handler"

    @pytest.mark.asyncio
    async def test_disabled_skill_not_in_extension_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "legacy", "Legacy handler", file_extensions=[".legacy"], enabled=False)
            svc = SkillService(repository=FileSkillRepository(skills_dir=tmpdir))
            assert len(await svc.list_skills()) == 0
            assert ".legacy" not in await svc._get_extension_map()

    @pytest.mark.asyncio
    async def test_extension_map_built_from_multiple_skills(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for name, exts in [("pdf", [".pdf"]), ("docx", [".docx", ".doc"]), ("xlsx", [".xlsx", ".xls", ".csv"])]:
                _write_skill_md(tmpdir, name, f"{name} processing", file_extensions=exts)
            ext_map = await SkillService(repository=FileSkillRepository(skills_dir=tmpdir))._get_extension_map()
            assert len(ext_map) == 6 and ext_map[".csv"] == "xlsx"


# ── TestAttachmentPathFallback ────────────────────────────

class TestAttachmentPathFallback:
    """验证附件路径回退和空路径过滤机制"""

    @pytest.mark.asyncio
    async def test_empty_filepath_filtered(self):
        mock_svc = AsyncMock(spec=SkillService)
        flow = MagicMock(_skill_service=mock_svc)
        await PlannerReActFlow._detect_and_inject_attachment_skills(flow, Message(message="test", attachments=["", "   "]))
        mock_svc.detect_skills_from_attachments.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_extension_filtered(self):
        mock_svc = AsyncMock(spec=SkillService)
        flow = MagicMock(_skill_service=mock_svc)
        await PlannerReActFlow._detect_and_inject_attachment_skills(flow, Message(message="test", attachments=["/home/ubuntu/upload/README"]))
        mock_svc.detect_skills_from_attachments.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_paths_passed_to_detect(self):
        mock_svc = AsyncMock(spec=SkillService)
        mock_svc.detect_skills_from_attachments = AsyncMock(return_value=[])
        flow = MagicMock(_skill_service=mock_svc, planner=MagicMock(), react=MagicMock())
        with patch.object(PlannerReActFlow, "_append_to_system_prompt", new_callable=AsyncMock):
            await PlannerReActFlow._detect_and_inject_attachment_skills(flow, Message(
                message="test", attachments=["", "/home/ubuntu/upload/report.xlsx", "/home/ubuntu/upload/data.pdf"]))
            mock_svc.detect_skills_from_attachments.assert_called_once_with(
                ["/home/ubuntu/upload/report.xlsx", "/home/ubuntu/upload/data.pdf"])

    @pytest.mark.asyncio
    async def test_chinese_filename_detects_skill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "xlsx", "Excel processing", file_extensions=[".xlsx", ".xls", ".csv"])
            result = await SkillService(repository=FileSkillRepository(skills_dir=tmpdir)).detect_skills_from_attachments([
                "/home/ubuntu/upload/餐厅记录表.xls", "/home/ubuntu/upload/订单消费记录表.xlsx"])
            assert "xlsx" in [s.name for s in result]


# ── TestRegression ────────────────────────────────────────

class TestRegression:
    @pytest.mark.asyncio
    async def test_skill_model_default_version(self):
        assert Skill(name="test", description="test", path="/s/test").version == "1.0.0"

    @pytest.mark.asyncio
    async def test_skill_model_default_keywords(self):
        assert Skill(name="test", description="test", path="/s/test").keywords is None

    @pytest.mark.asyncio
    async def test_skill_model_default_dependency(self):
        assert Skill(name="test", description="test", path="/s/test").dependency is None

    @pytest.mark.asyncio
    async def test_repository_caches_skills(self, skills_dir):
        repo = FileSkillRepository(skills_dir=skills_dir)
        assert len(await repo.get_all()) == len(await repo.get_all())

    @pytest.mark.asyncio
    async def test_context_tracker_different_sessions(self):
        tracker = SkillContextTracker()
        tracker.record_usage("s1", "pdf")
        tracker.record_usage("s2", "docx")
        assert tracker.get_recent_skills("s1") == ["pdf"] and tracker.get_recent_skills("s2") == ["docx"]

    @pytest.mark.asyncio
    async def test_service_record_and_get_recent(self, skills_dir):
        svc = SkillService(repository=FileSkillRepository(skills_dir=skills_dir))
        svc.record_usage("s1", "pdf")
        svc.record_usage("s1", "docx")
        recent = svc.get_recent_skills("s1")
        assert "docx" in recent and "pdf" in recent

    @pytest.mark.asyncio
    async def test_prompt_cache_injects_into_agents(self, skills_dir):
        await SkillsPromptCache.initialize(SkillService(repository=FileSkillRepository(skills_dir=skills_dir)))
        prompt = SkillsPromptCache.get_prompt()
        assert "<skills_index>" in prompt and "pdf" in prompt


# ── TestRequiresGating ────────────────────────────────────

class TestRequiresGating:
    """验证依赖门控机制: requires字段声明→运行时检测→自动禁用"""

    @pytest.mark.asyncio
    async def test_requires_bins_missing_disables_skill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "summarize", "Summarize content", file_extensions=[],
                            extra="requires:\n  bins: [nonexistent_cli_tool_xyz]")
            skills = await FileSkillRepository(skills_dir=tmpdir).get_all()
            assert len(skills) == 1
            assert skills[0].enabled is False
            assert "nonexistent_cli_tool_xyz" in skills[0].gating_reason

    @pytest.mark.asyncio
    async def test_requires_bins_existing_keeps_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "pdf", "PDF processing", file_extensions=[".pdf"],
                            extra="requires:\n  bins: [cmd]")
            skills = await FileSkillRepository(skills_dir=tmpdir).get_all()
            assert len(skills) == 1
            assert skills[0].enabled is True
            assert skills[0].gating_reason == ""

    @pytest.mark.asyncio
    async def test_requires_any_bins_at_least_one(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "browser", "Browser automation", file_extensions=[],
                            extra="requires:\n  any_bins: [cmd, nonexistent_xyz]")
            skills = await FileSkillRepository(skills_dir=tmpdir).get_all()
            assert skills[0].enabled is True

    @pytest.mark.asyncio
    async def test_requires_any_bins_none_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "browser", "Browser automation", file_extensions=[],
                            extra="requires:\n  any_bins: [nonexistent_a, nonexistent_b]")
            skills = await FileSkillRepository(skills_dir=tmpdir).get_all()
            assert skills[0].enabled is False
            assert "nonexistent_a" in skills[0].gating_reason

    @pytest.mark.asyncio
    async def test_requires_env_missing_disables_skill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "gemini", "Gemini API", file_extensions=[],
                            extra='requires:\n  env: [NONEXISTENT_API_KEY_XYZ]')
            skills = await FileSkillRepository(skills_dir=tmpdir).get_all()
            assert skills[0].enabled is False
            assert "NONEXISTENT_API_KEY_XYZ" in skills[0].gating_reason

    @pytest.mark.asyncio
    async def test_requires_env_existing_keeps_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["TEST_SKILL_KEY_123"] = "value"
            try:
                _write_skill_md(tmpdir, "gemini", "Gemini API", file_extensions=[],
                                extra='requires:\n  env: [TEST_SKILL_KEY_123]')
                skills = await FileSkillRepository(skills_dir=tmpdir).get_all()
                assert skills[0].enabled is True
            finally:
                del os.environ["TEST_SKILL_KEY_123"]

    @pytest.mark.asyncio
    async def test_no_requires_always_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "pdf", "PDF processing", file_extensions=[".pdf"])
            skills = await FileSkillRepository(skills_dir=tmpdir).get_all()
            assert skills[0].enabled is True
            assert skills[0].requires is None

    @pytest.mark.asyncio
    async def test_explicit_enabled_false_not_overridden_by_gating(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "legacy", "Legacy skill", file_extensions=[], enabled=False,
                            extra="requires:\n  bins: [python3]")
            skills = await FileSkillRepository(skills_dir=tmpdir).get_all()
            assert skills[0].enabled is False
            assert skills[0].gating_reason == ""

    @pytest.mark.asyncio
    async def test_gated_skill_not_in_visible_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "pdf", "PDF processing", file_extensions=[".pdf"])
            _write_skill_md(tmpdir, "summarize", "Summarize", file_extensions=[],
                            extra="requires:\n  bins: [nonexistent_cli_xyz]")
            svc = SkillService(repository=FileSkillRepository(skills_dir=tmpdir))
            visible = await svc.list_skills()
            assert len(visible) == 1
            assert visible[0].name == "pdf"

    @pytest.mark.asyncio
    async def test_gated_skill_not_in_extension_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "pdf", "PDF processing", file_extensions=[".pdf"])
            _write_skill_md(tmpdir, "gated-xlsx", "Excel", file_extensions=[".xlsx"],
                            extra="requires:\n  bins: [nonexistent_cli_xyz]")
            repo = FileSkillRepository(skills_dir=tmpdir)
            await repo.get_all()
            ext_map = repo.get_extension_map()
            assert ".pdf" in ext_map
            assert ".xlsx" not in ext_map

    @pytest.mark.asyncio
    async def test_metadata_openclaw_requires_parsed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = os.path.join(tmpdir, "gemini")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write('---\nname: gemini\ndescription: Gemini CLI\nenabled: true\nmetadata: {"openclaw":{"requires":{"bins":["nonexistent_gemini_cli"]}}}\n---\n\n# Gemini\n')
            skills = await FileSkillRepository(skills_dir=tmpdir).get_all()
            assert skills[0].enabled is False
            assert "nonexistent_gemini_cli" in skills[0].gating_reason

    @pytest.mark.asyncio
    async def test_skill_requires_model(self):
        req = SkillRequires(bins=["summarize"], any_bins=["node", "npm"], env=["API_KEY"])
        assert req.bins == ["summarize"]
        assert req.any_bins == ["node", "npm"]
        assert req.env == ["API_KEY"]

    @pytest.mark.asyncio
    async def test_to_dict_full_includes_requires(self):
        skill = Skill(name="test", description="test", path="/s/test",
                      requires=SkillRequires(bins=["summarize"]))
        result = skill.to_dict_for_prompt(lightweight=False)
        assert result["requires"] is not None
        assert result["requires"]["bins"] == ["summarize"]

    @pytest.mark.asyncio
    async def test_multiple_gating_reasons(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_skill_md(tmpdir, "complex", "Complex skill", file_extensions=[],
                            extra="requires:\n  bins: [nonexistent_a]\n  env: [NONEXISTENT_KEY_B]")
            skills = await FileSkillRepository(skills_dir=tmpdir).get_all()
            assert skills[0].enabled is False
            assert "nonexistent_a" in skills[0].gating_reason
            assert "NONEXISTENT_KEY_B" in skills[0].gating_reason
