#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_file_store.py
临时文件存储单元测试
"""
import pytest

from mcp_multimodal.config import FileStoreConfig
from mcp_multimodal.file_store import FileStore, StoredFile


@pytest.fixture
def file_store():
    config = FileStoreConfig(ttl_seconds=600, cleanup_interval_seconds=60)
    return FileStore(config)


class TestFileStore:
    def test_put_and_get(self, file_store):
        file_id = file_store.put("test.png", b"fake-image-data", "image/png")
        assert file_id is not None
        assert len(file_id) == 12

        stored = file_store.get(file_id)
        assert stored is not None
        assert stored.filename == "test.png"
        assert stored.content == b"fake-image-data"
        assert stored.mime_type == "image/png"

    def test_get_nonexistent(self, file_store):
        result = file_store.get("nonexistent")
        assert result is None

    def test_resolve_upload_ref(self, file_store):
        file_id = file_store.put("doc.pdf", b"pdf-content", "application/pdf")
        upload_ref = f"upload://{file_id}"

        stored = file_store.resolve(upload_ref)
        assert stored is not None
        assert stored.filename == "doc.pdf"
        assert stored.content == b"pdf-content"

    def test_resolve_invalid_ref(self, file_store):
        result = file_store.resolve("upload://nonexistent")
        assert result is None

    def test_resolve_non_upload_ref(self, file_store):
        result = file_store.resolve("https://example.com/file.png")
        assert result is None

    def test_default_mime_type(self, file_store):
        file_id = file_store.put("file.bin", b"data")
        stored = file_store.get(file_id)
        assert stored.mime_type == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_start_and_stop(self, file_store):
        await file_store.start()
        await file_store.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_files(self, file_store):
        file_store.put("test.txt", b"data")
        await file_store.stop()
        assert file_store.get("any") is None
