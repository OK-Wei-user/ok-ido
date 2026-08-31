#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/21 1:15

@File    : file_service.py
"""
from typing import Tuple, BinaryIO, Callable

from fastapi import UploadFile

from app.application.errors.exceptions import NotFoundError
from app.domain.external.file_storage import FileStorage
from app.domain.models.file import File
from app.domain.repositories.uow import IUnitOfWork


class FileService:
    """I-DO文件系统服务"""

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],
            file_storage: FileStorage,
    ) -> None:
        """构造函数，完成文件服务的初始化"""
        self.file_storage = file_storage
        self._uow_factory = uow_factory

    async def upload_file(self, upload_file: UploadFile) -> File:
        """将传递的文件上传到腾讯云cos并记录上传数据"""
        return await self.file_storage.upload_file(upload_file=upload_file)

    async def get_file_info(self, file_id: str) -> File:
        """根据传递的文件id获取文件信息"""
        # UoW并发安全: 每次创建独立UoW实例,避免共享实例在并发查询时session状态错乱
        # (与 AgentTaskRunner / CosFileStorage 保持一致)
        async with self._uow_factory() as uow:
            file = await uow.file.get_by_id(file_id)
        if not file:
            raise NotFoundError(f"该文件[{file_id}]不存在")
        return file

    async def download_file(self, file_id: str) -> Tuple[BinaryIO, File]:
        """根据传递的文件id下载文件"""
        return await self.file_storage.download_file(file_id)
