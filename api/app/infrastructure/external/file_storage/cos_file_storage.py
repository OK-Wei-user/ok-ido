#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/21 1:02
@File    : cos_file_storage.py
"""
import logging
import os.path
import uuid
from typing import Tuple, BinaryIO, Callable

from fastapi import UploadFile

from app.application.errors.exceptions import NotFoundError, ValidationError, ServerRequestsError
from app.domain.external.file_storage import FileStorage
from app.domain.models.file import File
from app.domain.repositories.uow import IUnitOfWork
from app.domain.services.oss_filename_utils.filename_utils import FilenameUtils, OSSExtensionMapper
from app.infrastructure.storage.cos import Cos

logger = logging.getLogger(__name__)


class CosFileStorage(FileStorage):
    """基于OSS的文件存储扩展（兼容原COS接口命名）"""

    def __init__(
            self,
            bucket: str,
            cos: Cos,
            uow_factory: Callable[[], IUnitOfWork],
    ) -> None:
        self.bucket = bucket
        self.cos = cos
        self._uow_factory = uow_factory

    async def upload_file(self, upload_file: UploadFile) -> File:
        try:
            file_id = str(uuid.uuid4())
            original_filename = FilenameUtils.normalize_filename(upload_file.filename or "unnamed")
            _, file_extension = os.path.splitext(original_filename)
            if not file_extension:
                file_extension = ""

            oss_ext = OSSExtensionMapper.map_extension(file_extension) if file_extension else ""
            oss_upload_filename = f"{file_id}{oss_ext}"

            await upload_file.seek(0)
            file_content = await upload_file.read()

            result = await self.cos.upload_file(
                file_data=file_content,
                filename=oss_upload_filename,
                bucket_name=self.bucket,
                is_random=True,
            )

            logger.info(
                f"文件上传成功: {original_filename} "
                f"(OSS: {oss_upload_filename}, ID: {file_id})"
            )

            file_url = result.get("url", "")

            file = File(
                id=file_id,
                filename=original_filename,
                key=file_url,
                extension=file_extension.lstrip("."),
                mime_type=upload_file.content_type or "",
                size=len(file_content),
            )
            # UoW并发安全: 每次创建独立UoW实例,避免asyncio.gather并发时db_session相互覆盖
            # (与 AgentTaskRunner 保持一致;共享实例在并发上传时会丢失DB记录)
            async with self._uow_factory() as uow:
                await uow.file.save(file)

            return file
        except Exception as e:
            logger.error(f"上传文件[{upload_file.filename}]失败: {str(e)}")
            raise

    async def download_file(self, file_id: str) -> Tuple[BinaryIO, File]:
        """根据传递的文件id下载文件

        错误码规范(替代原 raise ValueError → 500):
        - 404 NotFoundError: DB 中无此 file_id 记录
        - 422 ValidationError: file.key 为空(未同步到 OSS) 或 sync_status 为 PENDING/FAILED
        - 500 ServerRequestsError: OSS 下载异常(网络/权限/对象不存在)

        设计动机:
        - 沙箱生成的文件若 OSS 上传失败,会被标记 sync_status=PENDING/file.key="",
          前端展示该文件但点击下载会 500,用户体验差。
          明确返回 422 引导用户稍后重试或反馈给管理员。
        - 区分"DB 不存在"与"OSS 不存在"避免混淆排障方向。
        """
        # 1.DB 查询 file 记录
        # UoW并发安全: 每次创建独立UoW实例,避免共享实例在并发下载时session状态错乱
        async with self._uow_factory() as uow:
            file = await uow.file.get_by_id(file_id)
        if not file:
            raise NotFoundError(f"该文件[{file_id}]不存在")

        # 2.同步状态校验: PENDING/FAILED 表示 OSS 未就绪,无法下载
        sync_status = getattr(file, "sync_status", "SYNCED")
        if sync_status == "PENDING":
            logger.warning(f"文件[{file_id}]同步中,无法下载: filepath={file.filepath}")
            raise ValidationError(
                f"文件[{file.filename or file_id}]正在同步到对象存储,请稍后重试下载"
            )
        if sync_status == "FAILED":
            logger.warning(f"文件[{file_id}]同步失败,无法下载: filepath={file.filepath}")
            raise ValidationError(
                f"文件[{file.filename or file_id}]同步对象存储失败,请联系管理员"
            )

        # 3.OSS key 校验: key 为空表示未上传 OSS
        if not file.key:
            logger.warning(
                f"文件[{file_id}]key 为空(未同步到 OSS),无法下载: "
                f"filepath={file.filepath}, sync_status={sync_status}"
            )
            raise ValidationError(
                f"文件[{file.filename or file_id}]未同步到对象存储,无法下载"
            )

        # 4.OSS 下载
        try:
            file_stream = await self.cos.download_file(file.key)
            return file_stream, file
        except Exception as e:
            logger.error(
                f"OSS 下载文件失败: file_id={file_id}, key={file.key}, error={str(e)}"
            )
            raise ServerRequestsError(
                f"对象存储下载文件失败,请稍后重试"
            )
