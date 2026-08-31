#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_file_system_consistency.py
07优化单元测试 - 文件系统一致性保障

验证点:
- _sync_file_to_storage: 大小预检查、写入顺序(先OSS后DB)、重试、PENDING标记
- _sync_message_attachments_to_storage: 并发同步,部分失败容错(失败附件被过滤,避免0B文件交付)
- _sync_message_attachments_to_sandbox: 并发同步,部分失败保留原始附件
- _read_file_content_with_protection: 超大/中等/小文件分级SSE保护
"""
import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.file import File
from app.domain.models.event import MessageEvent
from app.domain.services.agent_task_runner import (
    AgentTaskRunner,
    _FILE_SIZE_BLOCK_THRESHOLD,
    _FILE_CONTENT_SSE_MAX,
    _ATTACHMENT_SYNC_CONCURRENCY,
)


def _build_runner() -> AgentTaskRunner:
    """构造AgentTaskRunner实例(绕过__init__,仅设置测试所需属性)"""
    runner = object.__new__(AgentTaskRunner)
    runner._session_id = "test-session"
    runner._sandbox = AsyncMock()
    runner._file_storage = AsyncMock()
    _uow = AsyncMock()
    runner._uow = _uow
    runner._uow_factory = lambda: _uow
    _uow.__aenter__.return_value = _uow
    _uow.session = AsyncMock()
    return runner


class TestSyncFileToStorage:
    """_sync_file_to_storage 写入顺序与重试"""

    @pytest.mark.asyncio
    async def test_normal_sync_success(self):
        """正常流程: 预检查→下载→上传OSS→更新DB"""
        runner = _build_runner()
        runner._sandbox.get_file_size = AsyncMock(return_value=1024)
        runner._sandbox.download_file = AsyncMock(return_value=io.BytesIO(b"file content"))
        runner._file_storage.upload_file = AsyncMock(return_value=File(
            filename="test.txt", filepath="/home/ubuntu/test.txt", key="oss-key-123",
        ))
        runner._uow.session.add_file = AsyncMock()
        runner._uow.session.remove_files_by_path = AsyncMock(return_value=0)

        result = await runner._sync_file_to_storage("/home/ubuntu/test.txt")

        assert result is not None
        assert result.sync_status == "SYNCED"
        assert result.filepath == "/home/ubuntu/test.txt"
        runner._file_storage.upload_file.assert_awaited_once()
        runner._uow.session.remove_files_by_path.assert_awaited_once()
        runner._uow.session.add_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_oversize_skip_and_mark_pending(self):
        """超大文件(>500MB)跳过同步,标记PENDING"""
        runner = _build_runner()
        runner._sandbox.get_file_size = AsyncMock(return_value=_FILE_SIZE_BLOCK_THRESHOLD + 1)
        runner._uow.session.get_file_by_path = AsyncMock(return_value=None)
        runner._uow.session.add_file = AsyncMock()

        result = await runner._sync_file_to_storage("/home/ubuntu/big.bin")

        assert result is None
        runner._sandbox.download_file.assert_not_called()
        runner._file_storage.upload_file.assert_not_called()
        runner._uow.session.add_file.assert_awaited_once()
        added_file = runner._uow.session.add_file.await_args.args[1]
        assert added_file.sync_status == "PENDING"

    @pytest.mark.asyncio
    async def test_oss_fail_retry_then_success(self):
        """OSS上传失败重试1次后成功"""
        runner = _build_runner()
        runner._sandbox.get_file_size = AsyncMock(return_value=1024)
        runner._sandbox.download_file = AsyncMock(return_value=io.BytesIO(b"content"))
        runner._uow.session.get_file_by_path = AsyncMock(return_value=None)
        runner._file_storage.upload_file = AsyncMock(
            side_effect=[
                RuntimeError("OSS限流"),
                File(filename="test.txt", filepath="/home/ubuntu/test.txt", key="k"),
            ],
        )
        runner._uow.session.add_file = AsyncMock()

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await runner._sync_file_to_storage("/home/ubuntu/test.txt", max_retries=2)

        assert result is not None
        assert result.sync_status == "SYNCED"
        assert runner._file_storage.upload_file.await_count == 2

    @pytest.mark.asyncio
    async def test_oss_fail_all_retries_mark_pending(self):
        """OSS上传全部重试失败后标记PENDING"""
        runner = _build_runner()
        runner._sandbox.get_file_size = AsyncMock(return_value=1024)
        runner._sandbox.download_file = AsyncMock(return_value=io.BytesIO(b"content"))
        runner._uow.session.get_file_by_path = AsyncMock(return_value=None)
        runner._file_storage.upload_file = AsyncMock(side_effect=RuntimeError("OSS不可用"))
        runner._uow.session.add_file = AsyncMock()

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await runner._sync_file_to_storage("/home/ubuntu/test.txt", max_retries=2)

        assert result is None
        # PENDING标记应被添加
        add_calls = runner._uow.session.add_file.await_args_list
        assert any(
            call.args[1].sync_status == "PENDING"
            for call in add_calls
        )


class TestSyncAttachmentsToStorage:
    """_sync_message_attachments_to_storage 并发同步"""

    @pytest.mark.asyncio
    async def test_concurrent_sync_all_success(self):
        """3个附件并发同步,全部成功"""
        runner = _build_runner()

        async def mock_sync(filepath):
            return File(filename=filepath.split("/")[-1], filepath=filepath, key=f"oss-{filepath}")

        with patch.object(runner, "_sync_file_to_storage", new=mock_sync):
            event = MessageEvent(
                role="assistant", content="done",
                attachments=[
                    File(filepath="/home/ubuntu/a.txt"),
                    File(filepath="/home/ubuntu/b.txt"),
                    File(filepath="/home/ubuntu/c.txt"),
                ],
            )
            await runner._sync_message_attachments_to_storage(event)

        assert len(event.attachments) == 3
        assert all(a.key.startswith("oss-") for a in event.attachments)

    @pytest.mark.asyncio
    async def test_partial_failure_filters_out_failed(self):
        """部分附件同步失败,失败附件被过滤丢弃(防止0B文件交付)

        设计原因: 同步失败的占位File默认size=0,filename="",若保留会被前端
        渲染为"· 0 B"的空文件条目,影响交付质量,故从交付列表中过滤。
        """
        runner = _build_runner()

        async def mock_sync(filepath):
            if "fail" in filepath:
                return None
            return File(filename=filepath.split("/")[-1], filepath=filepath, key=f"oss-{filepath}")

        with patch.object(runner, "_sync_file_to_storage", new=mock_sync):
            event = MessageEvent(
                role="assistant", content="done",
                attachments=[
                    File(filepath="/home/ubuntu/ok1.txt"),
                    File(filepath="/home/ubuntu/fail.txt"),
                    File(filepath="/home/ubuntu/ok2.txt"),
                ],
            )
            await runner._sync_message_attachments_to_storage(event)

        # 失败附件被过滤,仅保留成功同步的2个文件
        assert len(event.attachments) == 2
        ok_files = [a for a in event.attachments if a.key.startswith("oss-")]
        assert len(ok_files) == 2
        assert {a.filepath for a in ok_files} == {
            "/home/ubuntu/ok1.txt",
            "/home/ubuntu/ok2.txt",
        }

    @pytest.mark.asyncio
    async def test_empty_attachments_noop(self):
        """无附件时不执行任何操作"""
        runner = _build_runner()
        event = MessageEvent(role="assistant", content="done", attachments=[])

        with patch.object(runner, "_sync_file_to_storage") as mock_sync:
            await runner._sync_message_attachments_to_storage(event)
            mock_sync.assert_not_called()


class TestSyncAttachmentsToSandbox:
    """_sync_message_attachments_to_sandbox 并发同步"""

    @pytest.mark.asyncio
    async def test_concurrent_sync_all_success(self):
        """3个附件并发同步到沙箱,全部成功"""
        runner = _build_runner()

        async def mock_sync(file_id):
            return File(id=file_id, filename=f"{file_id}.txt", filepath=f"/home/ubuntu/{file_id}.txt")

        with patch.object(runner, "_sync_file_to_sandbox", new=mock_sync):
            event = MessageEvent(
                role="user", content="upload",
                attachments=[
                    File(id="f1", filename="f1.txt"),
                    File(id="f2", filename="f2.txt"),
                    File(id="f3", filename="f3.txt"),
                ],
            )
            await runner._sync_message_attachments_to_sandbox(event)

        assert len(event.attachments) == 3
        assert all(a.filepath.startswith("/home/ubuntu/") for a in event.attachments)

    @pytest.mark.asyncio
    async def test_partial_failure_preserves_original(self):
        """部分附件同步失败,失败附件保留原始信息"""
        runner = _build_runner()

        async def mock_sync(file_id):
            if file_id == "fail":
                return None
            return File(id=file_id, filename=f"{file_id}.txt", filepath=f"/home/ubuntu/{file_id}.txt")

        with patch.object(runner, "_sync_file_to_sandbox", new=mock_sync):
            event = MessageEvent(
                role="user", content="upload",
                attachments=[
                    File(id="ok1", filename="ok1.txt", filepath="local/ok1.txt"),
                    File(id="fail", filename="fail.txt", filepath="local/fail.txt"),
                ],
            )
            await runner._sync_message_attachments_to_sandbox(event)

        assert len(event.attachments) == 2
        ok_files = [a for a in event.attachments if a.filepath.startswith("/home/ubuntu/")]
        fail_files = [a for a in event.attachments if not a.filepath.startswith("/home/ubuntu/")]
        assert len(ok_files) == 1
        assert len(fail_files) == 1
        assert fail_files[0].id == "fail"

    @pytest.mark.asyncio
    async def test_empty_attachments_noop(self):
        """无附件时不执行任何操作"""
        runner = _build_runner()
        event = MessageEvent(role="user", content="upload", attachments=[])

        with patch.object(runner, "_sync_file_to_sandbox") as mock_sync:
            await runner._sync_message_attachments_to_sandbox(event)
            mock_sync.assert_not_called()


class TestReadFileContentWithProtection:
    """_read_file_content_with_protection SSE载荷保护"""

    @pytest.mark.asyncio
    async def test_small_file_full_content(self):
        """小文件(<8KB)完整回传"""
        runner = _build_runner()
        runner._sandbox.get_file_size = AsyncMock(return_value=100)
        runner._sandbox.read_file = AsyncMock(return_value=MagicMock(
            data={"content": "small file content"},
        ))

        result = await runner._read_file_content_with_protection("/home/ubuntu/small.txt")

        assert result == "small file content"

    @pytest.mark.asyncio
    async def test_medium_file_truncated(self):
        """中等文件(>8KB)截断回传前N字符"""
        runner = _build_runner()
        runner._sandbox.get_file_size = AsyncMock(return_value=20 * 1024)  # 20KB
        long_content = "x" * (20 * 1024)
        runner._sandbox.read_file = AsyncMock(return_value=MagicMock(
            data={"content": long_content},
        ))

        result = await runner._read_file_content_with_protection("/home/ubuntu/medium.txt")

        assert "truncated" in result
        assert "total" in result
        assert len(result) < len(long_content)

    @pytest.mark.asyncio
    async def test_oversize_file_only_metadata(self):
        """超大文件(>500MB)仅回传元信息,不读取content"""
        runner = _build_runner()
        runner._sandbox.get_file_size = AsyncMock(return_value=_FILE_SIZE_BLOCK_THRESHOLD + 1)
        runner._sandbox.read_file = AsyncMock()

        result = await runner._read_file_content_with_protection("/home/ubuntu/big.bin")

        assert "文件过大" in result
        assert "shell" in result  # 提示用shell工具
        runner._sandbox.read_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_file_size_fail_degrade_to_full_read(self):
        """get_file_size失败时降级到完整读取"""
        runner = _build_runner()
        runner._sandbox.get_file_size = AsyncMock(side_effect=RuntimeError("stat失败"))
        runner._sandbox.read_file = AsyncMock(return_value=MagicMock(
            data={"content": "degraded content"},
        ))

        result = await runner._read_file_content_with_protection("/home/ubuntu/test.txt")

        assert result == "degraded content"
        runner._sandbox.read_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_read_file_fail_returns_placeholder(self):
        """read_file失败时返回占位符"""
        runner = _build_runner()
        runner._sandbox.get_file_size = AsyncMock(return_value=100)
        runner._sandbox.read_file = AsyncMock(side_effect=RuntimeError("读取失败"))

        result = await runner._read_file_content_with_protection("/home/ubuntu/test.txt")

        assert "失败" in result


class TestFileModelSyncStatus:
    """File模型sync_status字段"""

    def test_default_is_synced(self):
        """默认值为SYNCED"""
        f = File(filename="test.txt")
        assert f.sync_status == "SYNCED"

    def test_pending_status(self):
        """可设置为PENDING"""
        f = File(filename="test.txt", sync_status="PENDING")
        assert f.sync_status == "PENDING"

    def test_failed_status(self):
        """可设置为FAILED"""
        f = File(filename="test.txt", sync_status="FAILED")
        assert f.sync_status == "FAILED"

    def test_backward_compatibility_without_field(self):
        """旧数据(无sync_status字段)反序列化时默认SYNCED"""
        raw = {"filename": "old.txt", "filepath": "/old"}
        f = File(**raw)
        assert f.sync_status == "SYNCED"
