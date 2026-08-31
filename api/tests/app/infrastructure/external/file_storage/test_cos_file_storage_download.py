#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_cos_file_storage_download.py
CosFileStorage.download_file 单元测试

覆盖批次17修复: 文件下载 500 错误 → 明确错误码转换
- 404 NotFoundError: DB 中无此 file_id 记录
- 422 ValidationError: sync_status=PENDING/FAILED 或 file.key 为空
- 500 ServerRequestsError: OSS 下载异常

设计原则:
- 不依赖真实 OSS/COS 服务,全部 Mock
- 每个测试用例独立可读,断言明确
- 覆盖正常路径+边界条件+异常场景
"""
import io
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.errors.exceptions import (
    NotFoundError,
    ValidationError,
    ServerRequestsError,
)
from app.domain.models.file import File
from app.infrastructure.external.file_storage.cos_file_storage import CosFileStorage


# ---------------------------------------------------------------------------
# 辅助函数: 构造测试用 File 对象 + CosFileStorage 实例
# ---------------------------------------------------------------------------
def _make_file(
    file_id: str = "file-001",
    filename: str = "report.docx",
    filepath: str = "/home/ubuntu/report.docx",
    key: str = "oss://bucket/report.docx",
    size: int = 1024,
    sync_status: str = "SYNCED",
) -> File:
    """构造测试用 File 对象"""
    return File(
        id=file_id,
        filename=filename,
        filepath=filepath,
        key=key,
        extension="docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size=size,
        sync_status=sync_status,
    )


def _make_storage(file_record: File = None, oss_stream=None, oss_raise=None) -> CosFileStorage:
    """构造测试用 CosFileStorage 实例

    Args:
        file_record: DB 查询返回的 File 记录,None 表示文件不存在
        oss_stream: OSS 下载返回的文件流
        oss_raise: OSS 下载抛出的异常(模拟 OSS 故障)
    """
    # Mock UoW + file repository
    uow = AsyncMock()
    uow.__aenter__.return_value = uow
    uow.file = MagicMock()
    uow.file.get_by_id = AsyncMock(return_value=file_record)

    uow_factory = MagicMock(return_value=uow)

    # Mock Cos 客户端
    cos = MagicMock()
    if oss_raise is not None:
        cos.download_file = AsyncMock(side_effect=oss_raise)
    else:
        cos.download_file = AsyncMock(return_value=oss_stream or io.BytesIO(b"file content"))

    return CosFileStorage(
        bucket="test-bucket",
        cos=cos,
        uow_factory=uow_factory,
    )


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
class TestDownloadFileSuccess:
    """download_file 正常路径测试"""

    @pytest.mark.asyncio
    async def test_download_success(self):
        """正常下载: 返回 (stream, file)"""
        file_record = _make_file(sync_status="SYNCED", key="oss://bucket/report.docx")
        storage = _make_storage(file_record=file_record)

        stream, file = await storage.download_file("file-001")

        assert file is file_record
        assert stream is not None


# ---------------------------------------------------------------------------
# 404 NotFoundError: DB 中无此 file_id 记录
# ---------------------------------------------------------------------------
class TestDownloadFileNotFound:
    """download_file 文件不存在测试"""

    @pytest.mark.asyncio
    async def test_file_not_in_db_raises_404(self):
        """DB 中无此 file_id 记录 → NotFoundError (404)"""
        storage = _make_storage(file_record=None)

        with pytest.raises(NotFoundError) as exc_info:
            await storage.download_file("nonexistent-id")

        assert "不存在" in str(exc_info.value.msg)


# ---------------------------------------------------------------------------
# 422 ValidationError: sync_status != SYNCED
# ---------------------------------------------------------------------------
class TestDownloadFileNotSynced:
    """download_file 未同步状态测试"""

    @pytest.mark.asyncio
    async def test_pending_status_raises_422(self):
        """sync_status=PENDING → ValidationError (422)"""
        file_record = _make_file(sync_status="PENDING")
        storage = _make_storage(file_record=file_record)

        with pytest.raises(ValidationError) as exc_info:
            await storage.download_file("file-001")

        assert "正在同步" in exc_info.value.msg
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_failed_status_raises_422(self):
        """sync_status=FAILED → ValidationError (422)"""
        file_record = _make_file(sync_status="FAILED")
        storage = _make_storage(file_record=file_record)

        with pytest.raises(ValidationError) as exc_info:
            await storage.download_file("file-001")

        assert "同步对象存储失败" in exc_info.value.msg
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_key_raises_422(self):
        """file.key 为空(未上传 OSS) → ValidationError (422)

        设计动机: 沙箱生成的文件若 OSS 上传失败,会被标记 sync_status=PENDING/key="",
        前端展示该文件但点击下载会 500,用户体验差。
        """
        file_record = _make_file(key="", sync_status="SYNCED")
        storage = _make_storage(file_record=file_record)

        with pytest.raises(ValidationError) as exc_info:
            await storage.download_file("file-001")

        assert "未同步到对象存储" in exc_info.value.msg
        assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# 500 ServerRequestsError: OSS 下载异常
# ---------------------------------------------------------------------------
class TestDownloadFileOSSException:
    """download_file OSS 异常测试"""

    @pytest.mark.asyncio
    async def test_oss_download_exception_raises_500(self):
        """OSS 下载异常 → ServerRequestsError (500)

        设计动机: 原实现直接 raise,被 FastAPI 默认异常处理器转为 500,
        但日志只记录"下载文件失败",排障困难。新实现抛 ServerRequestsError,
        全局异常处理器统一处理,日志中包含 file_id/key 便于排障。
        """
        file_record = _make_file(sync_status="SYNCED", key="oss://bucket/report.docx")
        storage = _make_storage(
            file_record=file_record,
            oss_raise=ConnectionError("OSS network unreachable"),
        )

        with pytest.raises(ServerRequestsError) as exc_info:
            await storage.download_file("file-001")

        assert "对象存储下载文件失败" in exc_info.value.msg
        assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# 校验顺序: PENDING 优先于 key 为空(避免误报)
# ---------------------------------------------------------------------------
class TestDownloadFileValidationOrder:
    """download_file 校验顺序测试"""

    @pytest.mark.asyncio
    async def test_pending_status_checked_before_empty_key(self):
        """sync_status=PENDING + key 为空时,优先报 PENDING(更具体的错误)"""
        file_record = _make_file(sync_status="PENDING", key="")
        storage = _make_storage(file_record=file_record)

        with pytest.raises(ValidationError) as exc_info:
            await storage.download_file("file-001")

        # 优先报 PENDING 错误(更具体)
        assert "正在同步" in exc_info.value.msg
