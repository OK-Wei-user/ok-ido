#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_f10_8_delivery_validation.py
F10-8 交付质量校验单元测试

覆盖范围:
- DeliveryValidationResult: 量化指标(hit_rate/total_filtered/log_report)
- FilePresentationService.is_excluded_file: 临时文件扩展名判断(集中化)
- FilePresentationService.filter_excluded_files: 临时文件过滤
- FilePresentationService.select_deliverable_files: 集中化交付物智能选择(替代_get_relevant_files)
- FilePresentationService.validate_deliverables: 交付前校验清单五项校验

设计原则:
- 每个测试用例独立可读,断言明确
- 覆盖正常路径+边界条件+异常场景
- 验证量化指标正确性,支持运维监控交付质量趋势
"""
import pytest

from app.application.services.file_presentation_service import (
    DeliveryValidationResult,
    FilePresentationService,
)
from app.domain.models.app_config import FilePresentationConfig
from app.domain.models.file import File


# ---------------------------------------------------------------------------
# 辅助函数: 构造测试用File对象
# ---------------------------------------------------------------------------
def _make_file(
    filepath: str,
    size: int = 1024,
    sync_status: str = "SYNCED",
    filename: str = None,
    key: str = "oss://test-bucket/file",
) -> File:
    """构造测试用File对象

    Args:
        filepath: 文件路径
        size: 文件大小(字节),默认1024
        sync_status: 同步状态,默认SYNCED
        filename: 文件名,None时从filepath提取
        key: OSS对象key,默认非空(已同步到OSS);测试未同步场景时传""
    """
    return File(
        filepath=filepath,
        filename=filename or filepath.rsplit("/", 1)[-1],
        size=size,
        sync_status=sync_status,
        key=key,
    )


# ---------------------------------------------------------------------------
# DeliveryValidationResult: 量化指标
# ---------------------------------------------------------------------------
class TestDeliveryValidationResult:
    """DeliveryValidationResult 量化指标测试"""

    def test_hit_rate_zero_declared_returns_one(self):
        """0声明时命中率为1.0(避免除零告警)"""
        result = DeliveryValidationResult(
            valid_attachments=[],
            total_declared=0,
            total_valid=0,
        )
        assert result.hit_rate == 1.0

    def test_hit_rate_all_valid_returns_one(self):
        """全部有效时命中率为1.0"""
        result = DeliveryValidationResult(
            valid_attachments=["/a.docx", "/b.xlsx"],
            total_declared=2,
            total_valid=2,
        )
        assert result.hit_rate == 1.0

    def test_hit_rate_partial(self):
        """部分有效时命中率正确(1/2=0.5)"""
        result = DeliveryValidationResult(
            valid_attachments=["/a.docx"],
            filtered_missing=["/missing.txt"],
            total_declared=2,
            total_valid=1,
        )
        assert result.hit_rate == 0.5

    def test_total_filtered(self):
        """total_filtered = total_declared - total_valid"""
        result = DeliveryValidationResult(
            valid_attachments=["/a.docx"],
            filtered_missing=["/missing1.txt"],
            filtered_empty=["/empty.txt"],
            total_declared=3,
            total_valid=1,
        )
        assert result.total_filtered == 2

    def test_log_report_format(self):
        """log_report输出格式包含所有指标"""
        result = DeliveryValidationResult(
            valid_attachments=["/a.docx"],
            filtered_missing=["/missing.txt"],
            filtered_empty=["/empty.txt"],
            filtered_duplicates=["/dup.txt"],
            filtered_process=["/debug.log"],
            filtered_temp=["/cache.tmp"],
            total_declared=6,
            total_valid=1,
        )
        report = result.log_report()
        assert "声明=6" in report
        assert "有效=1" in report
        assert "命中率=" in report
        assert "缺失=1" in report
        assert "空文件=1" in report
        assert "重复=1" in report
        assert "过程文件=1" in report
        assert "临时文件=1" in report


# ---------------------------------------------------------------------------
# is_excluded_file: 临时文件扩展名判断(F10-8集中化)
# ---------------------------------------------------------------------------
class TestIsExcludedFile:
    """is_excluded_file 临时文件扩展名判断测试"""

    def setup_method(self):
        self.service = FilePresentationService()

    def test_tmp_extension_excluded(self):
        """.tmp扩展名为临时文件"""
        assert self.service.is_excluded_file("/home/ubuntu/cache.tmp") is True

    def test_log_extension_excluded(self):
        """.log扩展名为临时文件"""
        assert self.service.is_excluded_file("/home/ubuntu/debug.log") is True

    def test_pyc_extension_excluded(self):
        """.pyc扩展名为临时文件"""
        assert self.service.is_excluded_file("/home/ubuntu/bytecode.pyc") is True

    def test_bak_extension_excluded(self):
        """.bak扩展名为临时文件"""
        assert self.service.is_excluded_file("/home/ubuntu/backup.bak") is True

    def test_docx_not_excluded(self):
        """.docx不是临时文件"""
        assert self.service.is_excluded_file("/home/ubuntu/report.docx") is False

    def test_py_not_excluded(self):
        """.py不是临时文件(可能是用户交付的分析脚本)"""
        assert self.service.is_excluded_file("/home/ubuntu/analysis.py") is False

    def test_no_extension_not_excluded(self):
        """无扩展名不是临时文件"""
        assert self.service.is_excluded_file("/home/ubuntu/Makefile") is False

    def test_case_insensitive(self):
        """扩展名判断大小写不敏感"""
        assert self.service.is_excluded_file("/home/ubuntu/FILE.TMP") is True
        assert self.service.is_excluded_file("/home/ubuntu/FILE.Log") is True


# ---------------------------------------------------------------------------
# filter_excluded_files: 临时文件过滤
# ---------------------------------------------------------------------------
class TestFilterExcludedFiles:
    """filter_excluded_files 临时文件过滤测试"""

    def setup_method(self):
        self.service = FilePresentationService()

    def test_filter_removes_temp_files(self):
        """过滤临时文件,保留正常文件"""
        files = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/cache.tmp",      # 临时
            "/home/ubuntu/debug.log",      # 临时
            "/home/ubuntu/data.xlsx",
        ]
        result = self.service.filter_excluded_files(files)
        assert "/home/ubuntu/report.docx" in result
        assert "/home/ubuntu/data.xlsx" in result
        assert "/home/ubuntu/cache.tmp" not in result
        assert "/home/ubuntu/debug.log" not in result
        assert len(result) == 2

    def test_filter_preserves_order(self):
        """过滤后保持原顺序"""
        files = [
            "/home/ubuntu/a.docx",
            "/home/ubuntu/b.tmp",   # 被过滤
            "/home/ubuntu/c.xlsx",
        ]
        result = self.service.filter_excluded_files(files)
        assert result == ["/home/ubuntu/a.docx", "/home/ubuntu/c.xlsx"]

    def test_filter_empty_list(self):
        """空列表返回空"""
        assert self.service.filter_excluded_files([]) == []

    def test_filter_all_temp_files(self):
        """全部为临时文件时返回空列表"""
        files = ["/home/ubuntu/a.tmp", "/home/ubuntu/b.log"]
        result = self.service.filter_excluded_files(files)
        assert result == []


# ---------------------------------------------------------------------------
# select_deliverable_files: 集中化交付物智能选择
# ---------------------------------------------------------------------------
class TestSelectDeliverableFiles:
    """select_deliverable_files 集中化交付物智能选择测试"""

    def setup_method(self):
        self.service = FilePresentationService()

    def test_empty_files_returns_empty(self):
        """空文件列表: 返回空"""
        assert self.service.select_deliverable_files([]) == []

    def test_all_files_returned_when_below_limit(self):
        """文件数≤max_deliverable_files: 全部返回(不截断)"""
        files = [f"/home/ubuntu/file{i}.md" for i in range(10)]
        result = self.service.select_deliverable_files(files)
        assert len(result) == 10
        assert set(result) == set(files)

    def test_temp_files_filtered(self):
        """临时文件被过滤,保留正常文件"""
        files = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/cache.tmp",      # 临时
            "/home/ubuntu/debug.log",      # 临时
            "/home/ubuntu/data.xlsx",
        ]
        result = self.service.select_deliverable_files(files)
        assert "/home/ubuntu/report.docx" in result
        assert "/home/ubuntu/data.xlsx" in result
        assert "/home/ubuntu/cache.tmp" not in result
        assert "/home/ubuntu/debug.log" not in result

    def test_truncation_takes_recent_files(self):
        """超过max_deliverable_files: 取最近N个(按列表末尾)"""
        # 使用自定义配置,max_deliverable_files=5便于测试
        config = FilePresentationConfig(max_deliverable_files=5)
        service = FilePresentationService(config=config)
        total = 10
        files = [f"/home/ubuntu/file{i}.md" for i in range(total)]
        result = service.select_deliverable_files(files)
        assert len(result) == 5
        # 取最后5个
        expected = files[-5:]
        assert result == expected

    def test_all_temp_files_fallback_to_original(self):
        """全部为临时文件: 返回原始列表(兜底,避免交付物为空)"""
        files = [
            "/home/ubuntu/cache.tmp",
            "/home/ubuntu/debug.log",
        ]
        result = self.service.select_deliverable_files(files)
        # 全部被过滤后返回原始列表(兜底)
        assert result == files

    def test_custom_config_excluded_extensions(self):
        """自定义配置: 扩展名排除项可调整"""
        # 自定义配置: 将.xlsx也加入排除项
        config = FilePresentationConfig(excluded_extensions=[".xlsx", ".tmp"])
        service = FilePresentationService(config=config)
        files = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/data.xlsx",    # 自定义排除
            "/home/ubuntu/cache.tmp",    # 自定义排除
        ]
        result = service.select_deliverable_files(files)
        assert result == ["/home/ubuntu/report.docx"]


# ---------------------------------------------------------------------------
# validate_deliverables: 交付前校验清单五项校验
# ---------------------------------------------------------------------------
class TestValidateDeliverables:
    """validate_deliverables 交付前校验清单测试"""

    def setup_method(self):
        self.service = FilePresentationService()

    def test_all_valid_attachments(self):
        """全部有效: 无过滤,全部保留"""
        declared = ["/home/ubuntu/report.docx", "/home/ubuntu/data.xlsx"]
        session_files = [
            _make_file("/home/ubuntu/report.docx", size=1024),
            _make_file("/home/ubuntu/data.xlsx", size=2048),
        ]
        result = self.service.validate_deliverables(declared, session_files)
        assert result.total_declared == 2
        assert result.total_valid == 2
        assert result.hit_rate == 1.0
        assert result.total_filtered == 0
        assert set(result.valid_attachments) == set(declared)

    def test_filter_missing_attachments(self):
        """完整性校验: 不在session.files中的附件剔除"""
        declared = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/missing.txt",  # 不在session.files中
        ]
        session_files = [_make_file("/home/ubuntu/report.docx", size=1024)]
        result = self.service.validate_deliverables(declared, session_files)
        assert result.total_declared == 2
        assert result.total_valid == 1
        assert "/home/ubuntu/missing.txt" in result.filtered_missing
        assert result.valid_attachments == ["/home/ubuntu/report.docx"]

    def test_filter_empty_files(self):
        """空文件校验: SYNCED状态size=0的文件剔除"""
        declared = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/empty.txt",  # size=0 SYNCED
        ]
        session_files = [
            _make_file("/home/ubuntu/report.docx", size=1024),
            _make_file("/home/ubuntu/empty.txt", size=0, sync_status="SYNCED"),
        ]
        result = self.service.validate_deliverables(declared, session_files)
        assert result.total_valid == 1
        assert "/home/ubuntu/empty.txt" in result.filtered_empty
        assert result.valid_attachments == ["/home/ubuntu/report.docx"]

    def test_keep_pending_empty_files(self):
        """空文件校验: PENDING状态size=0的文件不归入filtered_empty

        注: PENDING 文件会被第6项未同步校验(filtered_unsynced)剔除,
        而非第2项空文件校验(filtered_empty)剔除。
        本测试验证 PENDING 不归入 filtered_empty(语义保留),
        未同步剔除由 test_filter_unsynced_pending 验证。
        """
        declared = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/pending.txt",  # size=0 PENDING
        ]
        session_files = [
            _make_file("/home/ubuntu/report.docx", size=1024),
            _make_file("/home/ubuntu/pending.txt", size=0, sync_status="PENDING"),
        ]
        result = self.service.validate_deliverables(declared, session_files)
        # PENDING 不归入 filtered_empty(空文件校验仅针对 SYNCED 状态)
        assert result.filtered_empty == []
        # PENDING 由未同步校验剔除
        assert "/home/ubuntu/pending.txt" in result.filtered_unsynced
        assert result.total_valid == 1  # 只保留 report.docx

    def test_filter_duplicates(self):
        """重复校验: 按filepath去重(保留首次出现)"""
        declared = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/report.docx",  # 重复
        ]
        session_files = [_make_file("/home/ubuntu/report.docx", size=1024)]
        result = self.service.validate_deliverables(declared, session_files)
        assert result.total_declared == 2
        assert result.total_valid == 1
        assert "/home/ubuntu/report.docx" in result.filtered_duplicates
        assert result.valid_attachments == ["/home/ubuntu/report.docx"]

    def test_filter_process_files(self):
        """过程文件校验: 识别为过程文件的条目剔除"""
        declared = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/analysis_report.py",  # 脚本类过程文件(analysis+report模式)
        ]
        session_files = [
            _make_file("/home/ubuntu/report.docx", size=1024),
            _make_file("/home/ubuntu/analysis_report.py", size=512),
        ]
        result = self.service.validate_deliverables(declared, session_files)
        assert result.total_valid == 1
        assert "/home/ubuntu/analysis_report.py" in result.filtered_process
        assert result.valid_attachments == ["/home/ubuntu/report.docx"]

    def test_filter_temp_files(self):
        """临时文件校验: 扩展名命中excluded_extensions的条目剔除"""
        declared = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/cache.tmp",  # 临时文件
        ]
        session_files = [
            _make_file("/home/ubuntu/report.docx", size=1024),
            _make_file("/home/ubuntu/cache.tmp", size=128),
        ]
        result = self.service.validate_deliverables(declared, session_files)
        assert result.total_valid == 1
        assert "/home/ubuntu/cache.tmp" in result.filtered_temp
        assert result.valid_attachments == ["/home/ubuntu/report.docx"]

    def test_mixed_scenario(self):
        """混合场景: 多种过滤同时触发"""
        declared = [
            "/home/ubuntu/report.docx",       # 有效
            "/home/ubuntu/missing.txt",        # 缺失
            "/home/ubuntu/empty.txt",         # 空文件
            "/home/ubuntu/report.docx",        # 重复
            "/home/ubuntu/analysis_report.py",  # 过程文件
            "/home/ubuntu/cache.tmp",          # 临时文件
            "/home/ubuntu/data.xlsx",          # 有效
        ]
        session_files = [
            _make_file("/home/ubuntu/report.docx", size=1024),
            _make_file("/home/ubuntu/empty.txt", size=0, sync_status="SYNCED"),
            _make_file("/home/ubuntu/analysis_report.py", size=512),
            _make_file("/home/ubuntu/cache.tmp", size=128),
            _make_file("/home/ubuntu/data.xlsx", size=2048),
        ]
        result = self.service.validate_deliverables(declared, session_files)
        assert result.total_declared == 7
        assert result.total_valid == 2
        assert result.hit_rate == 2 / 7
        assert result.total_filtered == 5
        assert "/home/ubuntu/missing.txt" in result.filtered_missing
        assert "/home/ubuntu/empty.txt" in result.filtered_empty
        assert "/home/ubuntu/report.docx" in result.filtered_duplicates
        assert "/home/ubuntu/analysis_report.py" in result.filtered_process
        assert "/home/ubuntu/cache.tmp" in result.filtered_temp
        assert set(result.valid_attachments) == {
            "/home/ubuntu/report.docx",
            "/home/ubuntu/data.xlsx",
        }

    def test_empty_declared_returns_empty_valid(self):
        """空声明: valid_attachments为空,命中率为1.0"""
        session_files = [_make_file("/home/ubuntu/report.docx", size=1024)]
        result = self.service.validate_deliverables([], session_files)
        assert result.total_declared == 0
        assert result.total_valid == 0
        assert result.valid_attachments == []
        assert result.hit_rate == 1.0  # 0声明视为完全命中
        assert result.total_filtered == 0

    def test_hit_rate_calculation(self):
        """命中率计算: 3声明1有效 → 命中率1/3"""
        declared = [
            "/home/ubuntu/valid.docx",
            "/home/ubuntu/missing1.txt",
            "/home/ubuntu/missing2.txt",
        ]
        session_files = [_make_file("/home/ubuntu/valid.docx", size=1024)]
        result = self.service.validate_deliverables(declared, session_files)
        assert result.total_declared == 3
        assert result.total_valid == 1
        assert abs(result.hit_rate - 1 / 3) < 1e-9

    def test_validation_order_missing_first(self):
        """校验顺序: 完整性校验优先(缺失文件不计入其他过滤类别)"""
        # 缺失文件即使扩展名是.tmp,也只归入filtered_missing,不归入filtered_temp
        declared = [
            "/home/ubuntu/missing.tmp",  # 既缺失又是临时扩展名
        ]
        session_files = []
        result = self.service.validate_deliverables(declared, session_files)
        assert "/home/ubuntu/missing.tmp" in result.filtered_missing
        assert "/home/ubuntu/missing.tmp" not in result.filtered_temp
        assert result.total_valid == 0

    def test_preserves_valid_order(self):
        """valid_attachments保持声明顺序"""
        declared = [
            "/home/ubuntu/b.xlsx",
            "/home/ubuntu/a.docx",
            "/home/ubuntu/c.pdf",
        ]
        session_files = [
            _make_file("/home/ubuntu/b.xlsx", size=1024),
            _make_file("/home/ubuntu/a.docx", size=1024),
            _make_file("/home/ubuntu/c.pdf", size=1024),
        ]
        result = self.service.validate_deliverables(declared, session_files)
        assert result.valid_attachments == [
            "/home/ubuntu/b.xlsx",
            "/home/ubuntu/a.docx",
            "/home/ubuntu/c.pdf",
        ]

    def test_custom_config_validation(self):
        """自定义配置: 自定义excluded_extensions影响校验结果"""
        config = FilePresentationConfig(excluded_extensions=[".xlsx"])
        service = FilePresentationService(config=config)
        declared = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/data.xlsx",  # 自定义排除
        ]
        session_files = [
            _make_file("/home/ubuntu/report.docx", size=1024),
            _make_file("/home/ubuntu/data.xlsx", size=2048),
        ]
        result = service.validate_deliverables(declared, session_files)
        assert result.total_valid == 1
        assert "/home/ubuntu/data.xlsx" in result.filtered_temp
        assert result.valid_attachments == ["/home/ubuntu/report.docx"]

    # ========================================================================
    # F10-8 第6项校验: 未同步校验(file.key 为空或 sync_status != SYNCED)
    # 设计动机: 沙箱生成文件 OSS 上传失败时标记 PENDING/key="",
    # 前端展示该文件但点击下载会 500,污染交付列表。
    # ========================================================================

    def test_filter_unsynced_pending(self):
        """未同步校验: sync_status=PENDING 的文件剔除"""
        declared = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/pending.docx",  # PENDING 未同步
        ]
        session_files = [
            _make_file("/home/ubuntu/report.docx", size=1024, sync_status="SYNCED"),
            _make_file("/home/ubuntu/pending.docx", size=2048, sync_status="PENDING"),
        ]
        result = self.service.validate_deliverables(declared, session_files)
        assert result.total_valid == 1
        assert "/home/ubuntu/pending.docx" in result.filtered_unsynced
        assert result.valid_attachments == ["/home/ubuntu/report.docx"]

    def test_filter_unsynced_failed(self):
        """未同步校验: sync_status=FAILED 的文件剔除"""
        declared = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/failed.docx",  # FAILED 同步失败
        ]
        session_files = [
            _make_file("/home/ubuntu/report.docx", size=1024),
            _make_file("/home/ubuntu/failed.docx", size=2048, sync_status="FAILED"),
        ]
        result = self.service.validate_deliverables(declared, session_files)
        assert result.total_valid == 1
        assert "/home/ubuntu/failed.docx" in result.filtered_unsynced

    def test_filter_unsynced_empty_key(self):
        """未同步校验: file.key 为空(未上传 OSS)的文件剔除"""
        declared = [
            "/home/ubuntu/report.docx",
            "/home/ubuntu/no_key.docx",  # key=""
        ]
        session_files = [
            _make_file("/home/ubuntu/report.docx", size=1024, key="oss://bucket/report"),
            _make_file("/home/ubuntu/no_key.docx", size=2048, key=""),  # key 为空
        ]
        result = self.service.validate_deliverables(declared, session_files)
        assert result.total_valid == 1
        assert "/home/ubuntu/no_key.docx" in result.filtered_unsynced
        assert result.valid_attachments == ["/home/ubuntu/report.docx"]

    def test_log_report_includes_unsynced(self):
        """log_report 输出包含未同步指标"""
        result = DeliveryValidationResult(
            valid_attachments=["/a.docx"],
            filtered_unsynced=["/pending.docx", "/no_key.docx"],
            total_declared=3,
            total_valid=1,
        )
        report = result.log_report()
        assert "未同步=2" in report
