#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_cos_file_storage_uow_safety.py
CosFileStorage / FileService UoW 并发安全单元测试

覆盖批次30修复: 共享 self._uow 实例 → 每次 uow_factory() 创建独立实例
根因: asyncio.gather 并发上传时,共享 _uow 的 db_session/file 属性被相互覆盖,
      导致 INSERT 已执行但 COMMIT 丢失,DB 记录缺失,下载返回 404。

设计原则:
- 不依赖真实 OSS/DB,全部 Mock
- 重点验证 uow_factory() 调用次数与独立实例语义
- 并发场景模拟 _sync_message_attachments_to_storage 的 asyncio.gather 模式
"""
import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import UploadFile

from app.application.errors.exceptions import NotFoundError
from app.application.services.file_service import FileService
from app.domain.models.file import File
from app.infrastructure.external.file_storage.cos_file_storage import CosFileStorage


# ---------------------------------------------------------------------------
# 辅助函数: 构造 Mock UoW 与 UploadFile
# ---------------------------------------------------------------------------

def _make_uow(file_record: File = None):
    """构造一个独立 Mock UoW 实例(模拟 DBUnitOfWork 上下文管理器)"""
    uow = AsyncMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=None)
    uow.file = MagicMock()
    uow.file.get_by_id = AsyncMock(return_value=file_record)
    uow.file.save = AsyncMock(return_value=None)
    return uow


def _make_uow_factory(uow_instances):
    """构造 uow_factory,每次调用返回列表中下一个 UoW(模拟独立实例)

    Args:
        uow_instances: UoW 实例列表,按调用顺序消费
    """
    iterator = iter(uow_instances)
    factory = MagicMock(side_effect=lambda: next(iterator))
    return factory


def _make_upload_file(filename: str = "report.docx", content: bytes = b"file content") -> UploadFile:
    """构造测试用 UploadFile"""
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        size=len(content),
    )


def _make_cos_mock(
    oss_url: str = "oss://bucket/file.docx",
    download_stream: io.BytesIO = None,
) -> MagicMock:
    """构造 Mock Cos 客户端(支持 upload 与 download)"""
    cos = MagicMock()
    cos.upload_file = AsyncMock(return_value={"url": oss_url})
    cos.download_file = AsyncMock(return_value=download_stream or io.BytesIO(b"file content"))
    return cos


# ---------------------------------------------------------------------------
# CosFileStorage: 不再持有共享 _uow 实例
# ---------------------------------------------------------------------------

class TestCosFileStorageNoSharedUoW:
    """验证 CosFileStorage 不再持有共享 self._uow 实例"""

    def test_no_shared_uow_attribute(self):
        """构造后不应存在 self._uow 属性(批次30修复核心断言)"""
        cos = _make_cos_mock()
        factory = MagicMock(return_value=_make_uow())
        storage = CosFileStorage(bucket="bkt", cos=cos, uow_factory=factory)

        # 关键断言: 不应存在共享 _uow 实例
        assert not hasattr(storage, "_uow"), \
            "CosFileStorage 不应持有共享 self._uow 实例(批次30修复)"
        assert storage._uow_factory is factory

    def test_constructor_does_not_invoke_uow_factory(self):
        """构造函数不应调用 uow_factory()(避免启动时创建无用实例)"""
        cos = _make_cos_mock()
        factory = MagicMock(return_value=_make_uow())
        CosFileStorage(bucket="bkt", cos=cos, uow_factory=factory)

        # 构造阶段不应调用 factory
        factory.assert_not_called()


# ---------------------------------------------------------------------------
# CosFileStorage.upload_file: 每次调用创建独立 UoW
# ---------------------------------------------------------------------------

class TestUploadFileUoWPerCall:
    """验证 upload_file 每次调用都通过 uow_factory() 创建独立 UoW"""

    @pytest.mark.asyncio
    async def test_single_upload_calls_factory_once(self):
        """单次上传: uow_factory 被调用恰好 1 次"""
        uow = _make_uow()
        factory = MagicMock(return_value=uow)
        storage = CosFileStorage(
            bucket="bkt",
            cos=_make_cos_mock(),
            uow_factory=factory,
        )

        await storage.upload_file(_make_upload_file())

        assert factory.call_count == 1, \
            f"单次上传应调用 uow_factory 1 次, 实际 {factory.call_count}"

    @pytest.mark.asyncio
    async def test_upload_saves_file_via_uow(self):
        """上传后通过独立 UoW 的 file.save 持久化 File 记录"""
        uow = _make_uow()
        factory = MagicMock(return_value=uow)
        storage = CosFileStorage(
            bucket="bkt",
            cos=_make_cos_mock(oss_url="oss://bucket/abc.docx"),
            uow_factory=factory,
        )

        file = await storage.upload_file(_make_upload_file(filename="分析报告.docx"))

        # 验证 save 被调用,且传入 File 对象
        uow.file.save.assert_awaited_once()
        saved_file = uow.file.save.await_args.args[0]
        assert saved_file.filename == "分析报告.docx"
        assert saved_file.key == "oss://bucket/abc.docx"
        assert file.filename == "分析报告.docx"

    @pytest.mark.asyncio
    async def test_two_uploads_create_independent_uow_instances(self):
        """两次上传: 各自创建独立 UoW 实例(不共享)"""
        uow1 = _make_uow()
        uow2 = _make_uow()
        factory = MagicMock(side_effect=[uow1, uow2])
        storage = CosFileStorage(
            bucket="bkt",
            cos=_make_cos_mock(),
            uow_factory=factory,
        )

        await storage.upload_file(_make_upload_file())
        await storage.upload_file(_make_upload_file())

        # 两次调用,每次返回不同实例
        assert factory.call_count == 2
        assert uow1.file.save.await_count == 1
        assert uow2.file.save.await_count == 1


# ---------------------------------------------------------------------------
# CosFileStorage 并发安全: 模拟 asyncio.gather 并发上传
# ---------------------------------------------------------------------------

class TestUploadFileConcurrencySafety:
    """验证并发上传场景下不产生共享状态竞态(批次30核心修复场景)

    复现场景: AgentTaskRunner._sync_message_attachments_to_storage
    使用 asyncio.gather 并发同步 3 个附件,触发共享 _uow 竞态。
    """

    @pytest.mark.asyncio
    async def test_concurrent_uploads_use_independent_uow(self):
        """3 个并发上传: 各自使用独立 UoW,save 互不干扰"""
        uow_list = [_make_uow() for _ in range(3)]
        factory = MagicMock(side_effect=list(uow_list))
        storage = CosFileStorage(
            bucket="bkt",
            cos=_make_cos_mock(),
            uow_factory=factory,
        )

        # 模拟 asyncio.gather 并发上传 3 个文件
        await asyncio.gather(
            storage.upload_file(_make_upload_file(filename="f1.docx")),
            storage.upload_file(_make_upload_file(filename="f2.docx")),
            storage.upload_file(_make_upload_file(filename="f3.docx")),
        )

        # 每个 UoW 的 save 恰好被调用 1 次(无相互覆盖)
        for i, uow in enumerate(uow_list):
            assert uow.file.save.await_count == 1, \
                f"UoW[{i}] 的 save 应被调用 1 次, 实际 {uow.file.save.await_count}"

        # factory 被调用 3 次(每次上传独立创建)
        assert factory.call_count == 3

    @pytest.mark.asyncio
    async def test_concurrent_uploads_all_files_persisted(self):
        """并发上传后所有 File 记录均通过独立 UoW 持久化(无丢失)"""
        uow_list = [_make_uow() for _ in range(5)]
        factory = MagicMock(side_effect=list(uow_list))
        storage = CosFileStorage(
            bucket="bkt",
            cos=_make_cos_mock(),
            uow_factory=factory,
        )

        filenames = [f"file_{i}.docx" for i in range(5)]
        await asyncio.gather(*[
            storage.upload_file(_make_upload_file(filename=fn))
            for fn in filenames
        ])

        # 收集所有 save 调用的 File 对象
        saved_filenames = set()
        for uow in uow_list:
            call_args = uow.file.save.await_args
            if call_args:
                saved_filenames.add(call_args.args[0].filename)

        # 所有文件均被持久化(无丢失)
        assert saved_filenames == set(filenames), \
            f"并发上传后部分文件丢失: 期望 {set(filenames)}, 实际 {saved_filenames}"


# ---------------------------------------------------------------------------
# CosFileStorage.download_file: 每次调用创建独立 UoW
# ---------------------------------------------------------------------------

class TestDownloadFileUoWPerCall:
    """验证 download_file 每次调用都通过 uow_factory() 创建独立 UoW"""

    @pytest.mark.asyncio
    async def test_download_calls_factory_once(self):
        """单次下载: uow_factory 被调用恰好 1 次"""
        file_record = File(
            id="fid-1", filename="r.docx", key="oss://b/r.docx",
            size=100, sync_status="SYNCED",
        )
        uow = _make_uow(file_record=file_record)
        factory = MagicMock(return_value=uow)
        storage = CosFileStorage(
            bucket="bkt",
            cos=_make_cos_mock(),
            uow_factory=factory,
        )

        stream, file = await storage.download_file("fid-1")

        assert factory.call_count == 1
        assert file is file_record

    @pytest.mark.asyncio
    async def test_concurrent_downloads_use_independent_uow(self):
        """并发下载: 各自使用独立 UoW 实例"""
        file_records = [
            File(id=f"fid-{i}", filename=f"r{i}.docx", key=f"oss://b/r{i}.docx",
                 size=100, sync_status="SYNCED")
            for i in range(3)
        ]
        uow_list = [_make_uow(file_record=fr) for fr in file_records]
        factory = MagicMock(side_effect=list(uow_list))
        storage = CosFileStorage(
            bucket="bkt",
            cos=_make_cos_mock(),
            uow_factory=factory,
        )

        results = await asyncio.gather(
            storage.download_file("fid-0"),
            storage.download_file("fid-1"),
            storage.download_file("fid-2"),
        )

        assert factory.call_count == 3
        # 每个下载返回对应的 file_record(无串扰)
        for i, (_, file) in enumerate(results):
            assert file.id == f"fid-{i}", \
                f"并发下载结果串扰: 期望 fid-{i}, 实际 {file.id}"


# ---------------------------------------------------------------------------
# FileService: 不再持有共享 _uow 实例
# ---------------------------------------------------------------------------

class TestFileServiceNoSharedUoW:
    """验证 FileService 不再持有共享 self._uow 实例"""

    def test_no_shared_uow_attribute(self):
        """构造后不应存在 self._uow 属性(批次30修复核心断言)"""
        file_storage = MagicMock()
        factory = MagicMock(return_value=_make_uow())
        svc = FileService(uow_factory=factory, file_storage=file_storage)

        assert not hasattr(svc, "_uow"), \
            "FileService 不应持有共享 self._uow 实例(批次30修复)"
        assert svc._uow_factory is factory
        # 构造阶段不应调用 factory
        factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_file_info_uses_factory_per_call(self):
        """get_file_info 每次调用通过 uow_factory() 创建独立 UoW"""
        file_record = File(id="fid-1", filename="r.docx", key="oss://b/r.docx", size=100)
        uow = _make_uow(file_record=file_record)
        factory = MagicMock(return_value=uow)
        svc = FileService(uow_factory=factory, file_storage=MagicMock())

        result = await svc.get_file_info("fid-1")

        assert factory.call_count == 1
        assert result is file_record

    @pytest.mark.asyncio
    async def test_get_file_info_not_found(self):
        """文件不存在时抛 NotFoundError"""
        uow = _make_uow(file_record=None)
        factory = MagicMock(return_value=uow)
        svc = FileService(uow_factory=factory, file_storage=MagicMock())

        with pytest.raises(NotFoundError) as exc_info:
            await svc.get_file_info("nonexistent")

        assert "不存在" in str(exc_info.value.msg)

    @pytest.mark.asyncio
    async def test_get_file_info_concurrent_independent(self):
        """并发查询: 各自使用独立 UoW,结果无串扰"""
        file_records = [
            File(id=f"fid-{i}", filename=f"r{i}.docx", key=f"oss://b/r{i}.docx", size=100)
            for i in range(3)
        ]
        uow_list = [_make_uow(file_record=fr) for fr in file_records]
        factory = MagicMock(side_effect=list(uow_list))
        svc = FileService(uow_factory=factory, file_storage=MagicMock())

        results = await asyncio.gather(
            svc.get_file_info("fid-0"),
            svc.get_file_info("fid-1"),
            svc.get_file_info("fid-2"),
        )

        assert factory.call_count == 3
        for i, result in enumerate(results):
            assert result.id == f"fid-{i}", \
                f"并发查询结果串扰: 期望 fid-{i}, 实际 {result.id}"

    @pytest.mark.asyncio
    async def test_upload_delegates_to_file_storage(self):
        """upload_file 委托给 file_storage,不直接使用 UoW"""
        file_storage = MagicMock()
        file_storage.upload_file = AsyncMock(return_value=File(
            id="fid-1", filename="r.docx", key="oss://b/r.docx", size=100
        ))
        factory = MagicMock()
        svc = FileService(uow_factory=factory, file_storage=file_storage)

        await svc.upload_file(_make_upload_file())

        file_storage.upload_file.assert_awaited_once()
        # FileService.upload_file 不应直接调用 uow_factory
        factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_delegates_to_file_storage(self):
        """download_file 委托给 file_storage,不直接使用 UoW"""
        file_storage = MagicMock()
        file_storage.download_file = AsyncMock(
            return_value=(io.BytesIO(b"data"), File(
                id="fid-1", filename="r.docx", key="oss://b/r.docx", size=100
            ))
        )
        factory = MagicMock()
        svc = FileService(uow_factory=factory, file_storage=file_storage)

        await svc.download_file("fid-1")

        file_storage.download_file.assert_awaited_once_with("fid-1")
        # FileService.download_file 不应直接调用 uow_factory
        factory.assert_not_called()
