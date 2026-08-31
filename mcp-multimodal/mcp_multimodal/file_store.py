#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : file_store.py
临时文件存储 - 管理Agent通过上传端点提交的文件，支持TTL自动清理
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

from .config import FileStoreConfig

logger = logging.getLogger(__name__)


@dataclass
class StoredFile:
    file_id: str
    filename: str
    content: bytes
    mime_type: str
    created_at: float = field(default_factory=time.time)


class FileStore:
    """临时文件存储，支持TTL自动清理"""

    def __init__(self, config: FileStoreConfig) -> None:
        self._files: Dict[str, StoredFile] = {}
        self._config = config
        self._cleanup_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            f"文件存储已启动, TTL={self._config.ttl_seconds}s, "
            f"清理间隔={self._config.cleanup_interval_seconds}s"
        )

    async def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None
        self._files.clear()
        logger.info("文件存储已关闭")

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._config.cleanup_interval_seconds)
                self._evict_expired()
            except asyncio.CancelledError:
                break

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [
            fid for fid, f in self._files.items()
            if now - f.created_at > self._config.ttl_seconds
        ]
        for fid in expired:
            del self._files[fid]
        if expired:
            logger.info(f"清理过期文件: {len(expired)}个")

    def put(self, filename: str, content: bytes, mime_type: str = "") -> str:
        file_id = uuid.uuid4().hex[:12]
        self._files[file_id] = StoredFile(
            file_id=file_id,
            filename=filename,
            content=content,
            mime_type=mime_type or "application/octet-stream",
        )
        logger.info(f"存储文件: id={file_id}, name={filename}, size={len(content)}")
        return file_id

    def get(self, file_id: str) -> Optional[StoredFile]:
        return self._files.get(file_id)

    def resolve(self, source: str) -> Optional[StoredFile]:
        """解析upload://引用"""
        if source.startswith("upload://"):
            file_id = source[len("upload://"):]
            return self.get(file_id)
        return None


_instance: Optional[FileStore] = None


def set_instance(store: FileStore) -> None:
    global _instance
    _instance = store


def get_instance() -> Optional[FileStore]:
    return _instance
