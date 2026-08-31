#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_batch51_deliverable_filter.py
批次51: 交付物过滤增强单元测试

会话b6505eb7暴露: 13个中间产物(.html网页抓取/.py工具脚本/.txt文本提取)被误交付。
根因: is_likely_process_file规则不全面 + 无路径过滤。
修复: 新增intermediate_extensions(.html)+intermediate_path_prefixes(workspace/tmp)+
      扩展script_name_patterns(extract/verify/inspect等)+扩展text_process_name_patterns(extracted/headings等)

测试覆盖:
1. 中间产物扩展名过滤(.html/.htm)
2. 扩展的脚本名模式(extract/append/inspect/verify等)
3. 扩展的文本名模式(extracted/headings等)
4. 路径过滤(workspace/tmp)
5. select_deliverable_files 纵深防御
6. validate_deliverables 交付前校验
7. 会话b6505eb7真实文件列表端到端验证
8. 正常交付物不被误杀(无假阳性)
"""
from unittest.mock import MagicMock

import pytest

from app.application.services.file_presentation_service import (
    DeliveryValidationResult,
    FilePresentationService,
)
from app.domain.models.app_config import FilePresentationConfig


@pytest.fixture
def service() -> FilePresentationService:
    """默认配置的FilePresentationService实例"""
    return FilePresentationService()


# ============================================================
# 会话b6505eb7真实中间产物(应全部被过滤)
# ============================================================
_REAL_INTERMEDIATE_FILES = [
    # 网页抓取产物(.html,批次51新增intermediate_extensions)
    "/home/ubuntu/gartner_ai.html",
    "/home/ubuntu/stateofai.html",
    "/home/ubuntu/ieee_ai.html",
    # 工具脚本(.py,批次51扩展script_name_patterns)
    "/home/ubuntu/extract_text.py",
    "/home/ubuntu/extract_all.py",
    "/home/ubuntu/append_market_data.py",
    "/home/ubuntu/extract_insights.py",
    "/home/ubuntu/inspect_xlsx.py",
    "/home/ubuntu/verify_docx.py",
    # 文本提取产物(.txt,批次51扩展text_process_name_patterns)
    "/home/ubuntu/forrester_extracted.txt",
    "/home/ubuntu/gartner_extracted.txt",
    "/home/ubuntu/ai_trends_extracted.txt",
    "/home/ubuntu/headings.txt",
]

# 会话b6505eb7真实交付物(应全部保留)
_REAL_DELIVERABLES = [
    "/home/ubuntu/2026年AI发展趋势深度研究报告.docx",
    "/home/ubuntu/2026年AI趋势关键发现与洞察摘要.md",
    "/home/ubuntu/2026年AI趋势数据明细表.xlsx",
]


class TestBatch51IntermediateExtensions:
    """中间产物扩展名过滤(.html/.htm,批次51新增)"""

    def test_html_is_process_file(self, service: FilePresentationService):
        """.html网页抓取产物应被识别为过程文件"""
        assert service.is_likely_process_file("/home/ubuntu/gartner_ai.html") is True

    def test_htm_is_process_file(self, service: FilePresentationService):
        """.htm网页抓取产物应被识别为过程文件"""
        assert service.is_likely_process_file("/home/ubuntu/page.htm") is True

    def test_all_real_html_files_filtered(self, service: FilePresentationService):
        """会话b6505eb7的3个.html文件应全部被识别为过程文件"""
        html_files = [f for f in _REAL_INTERMEDIATE_FILES if f.endswith(".html")]
        assert len(html_files) == 3
        for fp in html_files:
            assert service.is_likely_process_file(fp) is True, f"未过滤: {fp}"


class TestBatch51ScriptNamePatterns:
    """扩展的脚本名模式(批次51新增extract/append/inspect/verify等)"""

    @pytest.mark.parametrize("filepath,expected_pattern", [
        ("/home/ubuntu/extract_text.py", "extract"),
        ("/home/ubuntu/extract_all.py", "extract"),
        ("/home/ubuntu/extract_insights.py", "extract"),
        ("/home/ubuntu/append_market_data.py", "append"),
        ("/home/ubuntu/inspect_xlsx.py", "inspect"),
        ("/home/ubuntu/verify_docx.py", "verify"),
    ])
    def test_new_script_patterns_filtered(
        self, service: FilePresentationService, filepath: str, expected_pattern: str
    ):
        """批次51新增的脚本模式应被识别为过程文件"""
        assert service.is_likely_process_file(filepath) is True, (
            f"模式'{expected_pattern}'未过滤: {filepath}"
        )

    def test_all_real_py_files_filtered(self, service: FilePresentationService):
        """会话b6505eb7的6个.py脚本应全部被识别为过程文件"""
        py_files = [f for f in _REAL_INTERMEDIATE_FILES if f.endswith(".py")]
        assert len(py_files) == 6
        for fp in py_files:
            assert service.is_likely_process_file(fp) is True, f"未过滤: {fp}"


class TestBatch51TextProcessPatterns:
    """扩展的文本名模式(批次51新增extracted/headings等)"""

    @pytest.mark.parametrize("filepath,expected_pattern", [
        ("/home/ubuntu/forrester_extracted.txt", "extracted"),
        ("/home/ubuntu/gartner_extracted.txt", "extracted"),
        ("/home/ubuntu/ai_trends_extracted.txt", "extracted"),
        ("/home/ubuntu/headings.txt", "headings"),
    ])
    def test_new_text_patterns_filtered(
        self, service: FilePresentationService, filepath: str, expected_pattern: str
    ):
        """批次51新增的文本模式应被识别为过程文件"""
        assert service.is_likely_process_file(filepath) is True, (
            f"模式'{expected_pattern}'未过滤: {filepath}"
        )

    def test_all_real_txt_files_filtered(self, service: FilePresentationService):
        """会话b6505eb7的4个.txt文件应全部被识别为过程文件"""
        txt_files = [f for f in _REAL_INTERMEDIATE_FILES if f.endswith(".txt")]
        assert len(txt_files) == 4
        for fp in txt_files:
            assert service.is_likely_process_file(fp) is True, f"未过滤: {fp}"


class TestBatch51PathFilter:
    """路径过滤(批次51新增intermediate_path_prefixes)"""

    @pytest.mark.parametrize("filepath", [
        "/home/ubuntu/workspace/analysis.py",
        "/home/ubuntu/workspace/gen_report.py",
        "/home/ubuntu/workspace/any_file.txt",
        "/home/ubuntu/workspace/sub/deep.json",
        "/tmp/temp_data.csv",
        "/tmp/scratch.py",
    ])
    def test_workspace_and_tmp_filtered(
        self, service: FilePresentationService, filepath: str
    ):
        """workspace/tmp目录下的文件应被识别为过程文件(不看扩展名)"""
        assert service.is_likely_process_file(filepath) is True, (
            f"路径未过滤: {filepath}"
        )

    def test_root_deliverable_not_path_filtered(
        self, service: FilePresentationService
    ):
        """根目录下的正常交付物不应被路径过滤误杀"""
        assert service.is_likely_process_file("/home/ubuntu/report.docx") is False
        assert service.is_likely_process_file("/home/ubuntu/data.xlsx") is False


class TestBatch51FilterIntermediateFiles:
    """filter_intermediate_files方法(批次51新增,纵深防御)"""

    def test_filters_html_files(self, service: FilePresentationService):
        """应过滤.html文件,保留其他文件"""
        files = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/page.html",
            "/home/ubuntu/data.xlsx",
            "/home/ubuntu/scraped.htm",
        ]
        result = service.filter_intermediate_files(files)
        assert "/home/ubuntu/page.html" not in result
        assert "/home/ubuntu/scraped.htm" not in result
        assert "/home/ubuntu/report.docx" in result
        assert "/home/ubuntu/data.xlsx" in result

    def test_preserves_order(self, service: FilePresentationService):
        """过滤后应保持原顺序"""
        files = ["/home/ubuntu/a.html", "/home/ubuntu/b.docx", "/home/ubuntu/c.html"]
        result = service.filter_intermediate_files(files)
        assert result == ["/home/ubuntu/b.docx"]

    def test_empty_input(self, service: FilePresentationService):
        """空输入应返回空列表"""
        assert service.filter_intermediate_files([]) == []


class TestBatch51SelectDeliverableFiles:
    """select_deliverable_files纵深防御(批次51增强)"""

    def test_filters_intermediate_in_select(self, service: FilePresentationService):
        """select_deliverable_files应过滤.html等中间产物"""
        all_files = _REAL_DELIVERABLES + [
            "/home/ubuntu/gartner_ai.html",
            "/home/ubuntu/extract_text.py",
        ]
        result = service.select_deliverable_files(all_files)
        # .html应被过滤
        assert "/home/ubuntu/gartner_ai.html" not in result
        # 正常交付物应保留
        for fp in _REAL_DELIVERABLES:
            assert fp in result

    def test_fallback_when_all_filtered(self, service: FilePresentationService):
        """全部为中间产物时返回原始列表(兜底,避免交付物为空)"""
        all_html = ["/home/ubuntu/a.html", "/home/ubuntu/b.html"]
        result = service.select_deliverable_files(all_html)
        # 兜底: 返回原始列表
        assert result == all_html


class TestBatch51ValidateDeliverables:
    """validate_deliverables交付前校验(批次51增强)"""

    @staticmethod
    def _make_file(filepath: str, size: int = 1024, sync_status: str = "SYNCED"):
        """构造测试用File mock对象"""
        f = MagicMock()
        f.filepath = filepath
        f.filename = filepath.rsplit("/", 1)[-1]
        f.size = size
        f.sync_status = sync_status
        f.key = f"oss-key-{filepath}"
        return f

    def test_filters_all_intermediate_from_attachments(
        self, service: FilePresentationService
    ):
        """会话b6505eb7: 13个中间产物应全部被validate_deliverables过滤"""
        # LLM声明了全部16个文件(3交付物+13中间产物)作为attachments
        declared = _REAL_DELIVERABLES + _REAL_INTERMEDIATE_FILES
        # session.files包含全部文件(均已同步)
        session_files = [self._make_file(fp) for fp in declared]

        result = service.validate_deliverables(declared, session_files)

        # 有效附件应仅含3个交付物
        assert len(result.valid_attachments) == 3
        for fp in _REAL_DELIVERABLES:
            assert fp in result.valid_attachments
        # 13个中间产物应全部被过滤
        assert result.total_filtered == 13
        assert result.total_valid == 3

    def test_process_files_in_filtered_category(
        self, service: FilePresentationService
    ):
        """中间产物应被归入filtered_process或filtered_temp类别"""
        declared = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/gartner_ai.html",       # → filtered_temp(intermediate_extensions)
            "/home/ubuntu/extract_text.py",        # → filtered_process(script pattern)
            "/home/ubuntu/forrester_extracted.txt",  # → filtered_process(text pattern)
        ]
        session_files = [self._make_file(fp) for fp in declared]

        result = service.validate_deliverables(declared, session_files)

        assert len(result.valid_attachments) == 1
        assert result.valid_attachments == ["/home/ubuntu/report.docx"]
        # .html命中intermediate_extensions → filtered_temp(excluded_file逻辑)
        # .py命中script pattern → filtered_process
        # .txt命中text pattern → filtered_process
        assert result.total_filtered == 3


class TestBatch51RealSessionE2E:
    """会话b6505eb7真实文件列表端到端验证"""

    def test_all_13_intermediate_files_filtered(
        self, service: FilePresentationService
    ):
        """会话b6505eb7的13个中间产物应全部被is_likely_process_file识别"""
        filtered = [fp for fp in _REAL_INTERMEDIATE_FILES if service.is_likely_process_file(fp)]
        assert len(filtered) == 13, (
            f"仅过滤了{len(filtered)}/13个中间产物,未过滤: "
            f"{set(_REAL_INTERMEDIATE_FILES) - set(filtered)}"
        )

    def test_all_3_deliverables_preserved(
        self, service: FilePresentationService
    ):
        """会话b6505eb7的3个交付物应全部不被is_likely_process_file误杀"""
        preserved = [fp for fp in _REAL_DELIVERABLES if not service.is_likely_process_file(fp)]
        assert len(preserved) == 3, (
            f"仅保留了{len(preserved)}/3个交付物,被误杀: "
            f"{set(_REAL_DELIVERABLES) - set(preserved)}"
        )


class TestBatch51NoFalsePositive:
    """正常交付物不被误杀(无假阳性)"""

    @pytest.mark.parametrize("filepath", [
        "/home/ubuntu/经营分析报告.docx",
        "/home/ubuntu/2026年AI趋势数据明细表.xlsx",
        "/home/ubuntu/趋势图.png",
        "/home/ubuntu/研究摘要.md",
        "/home/ubuntu/演示文稿.pptx",
        "/home/ubuntu/数据.csv",
        "/home/ubuntu/报告.pdf",
    ])
    def test_normal_deliverables_not_filtered(
        self, service: FilePresentationService, filepath: str
    ):
        """常见交付物格式不应被识别为过程文件"""
        assert service.is_likely_process_file(filepath) is False, (
            f"正常交付物被误杀: {filepath}"
        )

    def test_existing_patterns_still_work(self, service: FilePresentationService):
        """批次51之前的过滤模式应仍然有效(无回归)"""
        # 日志文件
        assert service.is_likely_process_file("/home/ubuntu/app.log") is True
        # 原有脚本模式
        assert service.is_likely_process_file("/home/ubuntu/analysis.py") is True
        assert service.is_likely_process_file("/home/ubuntu/generate_report.py") is True
        # 原有文本模式
        assert service.is_likely_process_file("/home/ubuntu/data_check.txt") is True
        assert service.is_likely_process_file("/home/ubuntu/lines91_95.txt") is True


class TestBatch51ConfigBackwardCompat:
    """配置向后兼容性验证"""

    def test_default_config_has_intermediate_extensions(self):
        """默认配置应包含.html/.htm"""
        cfg = FilePresentationConfig()
        assert ".html" in cfg.intermediate_extensions
        assert ".htm" in cfg.intermediate_extensions

    def test_default_config_has_path_prefixes(self):
        """默认配置应包含workspace/tmp路径前缀"""
        cfg = FilePresentationConfig()
        assert "/home/ubuntu/workspace/" in cfg.intermediate_path_prefixes
        assert "/tmp/" in cfg.intermediate_path_prefixes

    def test_default_config_has_new_script_patterns(self):
        """默认配置应包含批次51新增的脚本模式"""
        cfg = FilePresentationConfig()
        new_patterns = ["extract", "append", "inspect", "verify", "fetch", "scrape"]
        for p in new_patterns:
            assert p in cfg.script_name_patterns, f"缺失脚本模式: {p}"

    def test_default_config_has_new_text_patterns(self):
        """默认配置应包含批次51新增的文本模式"""
        cfg = FilePresentationConfig()
        new_patterns = ["extracted", "headings", "raw_", "fetched", "scraped", "parsed"]
        for p in new_patterns:
            assert p in cfg.text_process_name_patterns, f"缺失文本模式: {p}"

    def test_config_customizable(self):
        """配置应支持自定义(移除.html过滤)"""
        cfg = FilePresentationConfig(intermediate_extensions=[])
        svc = FilePresentationService(cfg)
        # 移除.html过滤后,根目录的.html不应被扩展名过滤(但路径过滤仍生效)
        assert svc.is_likely_process_file("/home/ubuntu/page.html") is False
        # workspace下的.html仍被路径过滤
        assert svc.is_likely_process_file("/home/ubuntu/workspace/page.html") is True


# ============================================================
# present_files 方法中间产物过滤测试(批次51核心修复)
# 验证 /files API 返回的文件列表不含中间产物
# ============================================================
from unittest.mock import MagicMock
from app.domain.models.session import File as SessionFile, Session
from app.domain.models.event import MessageEvent


def _make_session_file(filepath: str, size: int = 1024) -> SessionFile:
    """构造测试用的File对象"""
    f = MagicMock(spec=SessionFile)
    f.filepath = filepath
    f.filename = filepath.rsplit("/", 1)[-1]
    f.size = size
    f.sync_status = "SYNCED"
    return f


def _make_session_with_deliverables(delivered_paths: set) -> Session:
    """构造含最终交付消息的Session对象"""
    session = MagicMock(spec=Session)
    session.files = []
    # 构造最终答案消息事件(含attachments)
    msg_event = MagicMock(spec=MessageEvent)
    msg_event.is_final = True
    msg_event.is_streaming = False
    msg_event.attachments = [MagicMock(filepath=p) for p in delivered_paths]
    session.events = [msg_event]
    return session


class TestBatch51PresentFilesFilter:
    """present_files方法中间产物过滤(批次51核心修复)"""

    def test_present_files_filters_process_files(self, service: FilePresentationService):
        """present_files应过滤非交付文件中的中间产物"""
        delivered_paths = {"/home/ubuntu/report.docx"}
        session = _make_session_with_deliverables(delivered_paths)
        all_files = [
            _make_session_file("/home/ubuntu/report.docx"),              # 交付物
            _make_session_file("/home/ubuntu/workspace/gen_report.py"),  # 中间产物
            _make_session_file("/tmp/ai_trends.html"),                   # 中间产物
            _make_session_file("/home/ubuntu/趋势图.png"),               # 辅助文件(保留)
        ]
        session.files = all_files

        result = service.present_files(session, all_files)
        result_paths = {f.filepath for f in result}

        # 交付物和辅助文件保留,中间产物过滤
        assert "/home/ubuntu/report.docx" in result_paths
        assert "/home/ubuntu/趋势图.png" in result_paths
        assert "/home/ubuntu/workspace/gen_report.py" not in result_paths
        assert "/tmp/ai_trends.html" not in result_paths

    def test_present_files_preserves_all_deliverables(self, service: FilePresentationService):
        """present_files应保留所有交付物"""
        delivered_paths = {
            "/home/ubuntu/2026年AI发展趋势深度研究报告.docx",
            "/home/ubuntu/2026年AI趋势关键发现与洞察摘要.md",
            "/home/ubuntu/2026年AI趋势数据明细表.xlsx",
        }
        session = _make_session_with_deliverables(delivered_paths)
        all_files = [_make_session_file(fp) for fp in delivered_paths]
        session.files = all_files

        result = service.present_files(session, all_files)
        result_paths = {f.filepath for f in result}

        assert result_paths == delivered_paths

    def test_present_files_real_session_b6505eb7(self, service: FilePresentationService):
        """会话b6505eb7真实场景: 3交付物+13中间产物 → 仅返回3交付物"""
        delivered_paths = set(_REAL_DELIVERABLES)
        session = _make_session_with_deliverables(delivered_paths)
        all_files = [_make_session_file(fp) for fp in _REAL_DELIVERABLES + _REAL_INTERMEDIATE_FILES]
        session.files = all_files

        result = service.present_files(session, all_files)
        result_paths = {f.filepath for f in result}

        # 3个交付物全部保留
        for dp in delivered_paths:
            assert dp in result_paths, f"交付物丢失: {dp}"
        # 13个中间产物全部过滤
        for ip in _REAL_INTERMEDIATE_FILES:
            assert ip not in result_paths, f"中间产物未过滤: {ip}"
        # 结果应仅含3个交付物
        assert len(result_paths) == 3, f"结果数量异常: {len(result_paths)}, 文件: {result_paths}"

    def test_present_files_no_deliverables_filters_all(self, service: FilePresentationService):
        """无交付文件时,中间产物仍应被过滤"""
        session = MagicMock(spec=Session)
        session.files = []
        session.events = []  # 无最终答案消息
        all_files = [
            _make_session_file("/home/ubuntu/workspace/analysis.py"),
            _make_session_file("/tmp/debug.txt"),
            _make_session_file("/home/ubuntu/待处理数据.xlsx"),  # 非过程文件,保留
        ]

        result = service.present_files(session, all_files)
        result_paths = {f.filepath for f in result}

        assert "/home/ubuntu/待处理数据.xlsx" in result_paths
        assert "/home/ubuntu/workspace/analysis.py" not in result_paths
        assert "/tmp/debug.txt" not in result_paths

    def test_present_files_preserves_png_charts(self, service: FilePresentationService):
        """根目录下的.png图表应保留(非过程文件)"""
        delivered_paths = {"/home/ubuntu/库存分析报告.docx"}
        session = _make_session_with_deliverables(delivered_paths)
        all_files = [
            _make_session_file("/home/ubuntu/库存分析报告.docx"),
            _make_session_file("/home/ubuntu/月度趋势图.png"),
            _make_session_file("/home/ubuntu/workspace/gen_charts.py"),  # 过滤
        ]
        session.files = all_files

        result = service.present_files(session, all_files)
        result_paths = {f.filepath for f in result}

        assert "/home/ubuntu/库存分析报告.docx" in result_paths
        assert "/home/ubuntu/月度趋势图.png" in result_paths
        assert "/home/ubuntu/workspace/gen_charts.py" not in result_paths

    def test_filter_process_files_method(self, service: FilePresentationService):
        """_filter_process_files应正确过滤中间产物"""
        files = [
            _make_session_file("/home/ubuntu/workspace/verify.py"),
            _make_session_file("/tmp/output.txt"),
            _make_session_file("/home/ubuntu/报告.docx"),
        ]
        result = service._filter_process_files(files)
        result_paths = {f.filepath for f in result}

        assert "/home/ubuntu/报告.docx" in result_paths
        assert "/home/ubuntu/workspace/verify.py" not in result_paths
        assert "/tmp/output.txt" not in result_paths
