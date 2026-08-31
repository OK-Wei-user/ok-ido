#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_find_files_protection.py
FileTool.find_files 三层防护单元测试

背景:会话 f2611353 卡死 - LLM 调用 find_files(dir_path="/", glob_pattern="**/pptxgenjs.md")
导致 sandbox glob.glob('/**/...') 进入 /proc /sys 虚拟文件系统挂起,会话停留 RUNNING 无法恢复。

三层防护:
    L1 工具层(FileTool.find_files):路径验证 + 空结果引导
    L2 HTTP 客户端层(DockerSandbox.find_files):30s 超时
    L3 沙箱服务层(FileService.find_files):15s/30s glob 超时 + 路径验证

本测试覆盖 L1 工具层路径验证与空结果引导 + L2 HTTP 客户端超时。
L3 沙箱服务层测试见 sandbox 项目(部署后在容器内运行)。
"""
import asyncio
import os.path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.file import FileTool, _FORBIDDEN_SCAN_ROOTS
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


# ============================================================
# L1 工具层:FileTool.find_files 路径验证
# ============================================================

def _build_file_tool_with_mock_sandbox() -> tuple[FileTool, MagicMock]:
    """构造带 mock sandbox 的 FileTool,返回(tool, sandbox_mock)"""
    sandbox = MagicMock()
    sandbox.find_files = AsyncMock(
        return_value=ToolResult(success=True, message="查找完毕", data={"files": ["/tmp/x.txt"]})
    )
    tool_obj = FileTool(sandbox=sandbox)
    return tool_obj, sandbox


class TestFindFilesPathValidation:
    """L1 路径验证:拒绝系统目录扫描"""

    @pytest.mark.asyncio
    async def test_root_dir_rejected(self):
        """根目录 / 被拒绝"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.find_files(dir_path="/", glob_pattern="**/*.md")
        assert result.success is False
        assert "禁止扫描系统目录" in result.message
        assert "/home" in result.message
        sandbox.find_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_proc_dir_rejected(self):
        """/proc 被拒绝(虚拟文件系统)"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.find_files(dir_path="/proc", glob_pattern="*")
        assert result.success is False
        assert "禁止扫描系统目录" in result.message
        sandbox.find_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_sys_dir_rejected(self):
        """/sys 被拒绝(虚拟文件系统)"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.find_files(dir_path="/sys", glob_pattern="*")
        assert result.success is False
        sandbox.find_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_dev_dir_rejected(self):
        """/dev 被拒绝"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.find_files(dir_path="/dev", glob_pattern="*")
        assert result.success is False
        sandbox.find_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_forbidden_roots_rejected(self):
        """黑名单中所有系统目录均被拒绝"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        for forbidden in _FORBIDDEN_SCAN_ROOTS:
            sandbox.find_files.reset_mock()
            result = await tool_obj.find_files(dir_path=forbidden, glob_pattern="*")
            assert result.success is False, f"未拦截黑名单目录: {forbidden}"
            sandbox.find_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_trailing_slash_normalized(self):
        """尾部斜杠的根目录也被拒绝(规范化后比对)"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.find_files(dir_path="/proc/", glob_pattern="*")
        assert result.success is False
        sandbox.find_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_workspace_dir_allowed(self):
        """工作区目录 /home 允许扫描(转发至 sandbox)"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.find_files(dir_path="/home", glob_pattern="**/*.md")
        assert result.success is True
        sandbox.find_files.assert_called_once_with(dir_path="/home", glob_pattern="**/*.md")

    @pytest.mark.asyncio
    async def test_tmp_dir_allowed(self):
        """/tmp 允许扫描"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.find_files(dir_path="/tmp", glob_pattern="*")
        assert result.success is True
        sandbox.find_files.assert_called_once()

    @pytest.mark.asyncio
    async def test_sandbox_dir_allowed(self):
        """/sandbox 允许扫描"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.find_files(dir_path="/sandbox", glob_pattern="**/*.py")
        assert result.success is True
        sandbox.find_files.assert_called_once()


class TestFindFilesEmptyResultGuidance:
    """L1 空结果引导:成功但未找到文件时附加引导消息"""

    @pytest.mark.asyncio
    async def test_empty_files_adds_guidance(self):
        """空文件列表附加引导消息"""
        sandbox = MagicMock()
        sandbox.find_files = AsyncMock(
            return_value=ToolResult(success=True, message="查找完毕", data={"files": []})
        )
        tool_obj = FileTool(sandbox=sandbox)
        result = await tool_obj.find_files(dir_path="/tmp", glob_pattern="*.nonexist")
        assert result.success is True
        assert "未找到" in result.message
        assert "shell_execute" in result.message

    @pytest.mark.asyncio
    async def test_non_empty_files_preserves_original_message(self):
        """非空文件列表保留 sandbox 原始消息"""
        sandbox = MagicMock()
        sandbox.find_files = AsyncMock(
            return_value=ToolResult(
                success=True,
                message="查找完毕, 检索到2个文件",
                data={"files": ["/tmp/a.txt", "/tmp/b.txt"]},
            )
        )
        tool_obj = FileTool(sandbox=sandbox)
        result = await tool_obj.find_files(dir_path="/tmp", glob_pattern="*.txt")
        assert result.success is True
        assert result.message == "查找完毕, 检索到2个文件"

    @pytest.mark.asyncio
    async def test_failed_result_preserves_error(self):
        """失败结果保留 sandbox 错误消息,不附加引导"""
        sandbox = MagicMock()
        sandbox.find_files = AsyncMock(
            return_value=ToolResult(success=False, message="目录不存在")
        )
        tool_obj = FileTool(sandbox=sandbox)
        result = await tool_obj.find_files(dir_path="/tmp", glob_pattern="*")
        assert result.success is False
        assert result.message == "目录不存在"

    @pytest.mark.asyncio
    async def test_none_data_treated_as_empty(self):
        """data=None 时视为空结果附加引导"""
        sandbox = MagicMock()
        sandbox.find_files = AsyncMock(
            return_value=ToolResult(success=True, message="ok", data=None)
        )
        tool_obj = FileTool(sandbox=sandbox)
        result = await tool_obj.find_files(dir_path="/tmp", glob_pattern="*")
        assert result.success is True
        assert "未找到" in result.message


# ============================================================
# L2 HTTP 客户端层:DockerSandbox.find_files 超时
# ============================================================

class TestDockerSandboxFindFilesTimeout:
    """L2 HTTP 超时:30s 超时返回失败 ToolResult"""

    @pytest.mark.asyncio
    async def test_timeout_returns_failed_result(self):
        """HTTP 超时返回 success=False 的 ToolResult"""
        sandbox = DockerSandbox(ip="127.0.0.1", container_name="test")

        with patch.object(sandbox.client, "post", new=AsyncMock(
            side_effect=httpx.TimeoutException(" simulated timeout")
        )):
            result = await sandbox.find_files(dir_path="/tmp", glob_pattern="**/*.md")

        assert result.success is False
        assert "超时" in result.message
        assert "30s" in result.message
        await sandbox.client.aclose()

    @pytest.mark.asyncio
    async def test_timeout_constant_is_30s(self):
        """超时常量固定为 30s(覆盖默认 read=660s)"""
        assert DockerSandbox._FIND_FILES_TIMEOUT == 30

    @pytest.mark.asyncio
    async def test_normal_response_passes_through(self):
        """正常响应透传 ToolResult.from_sandbox"""
        sandbox = DockerSandbox(ip="127.0.0.1", container_name="test")

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "code": 200,
            "msg": "查找完毕, 检索到1个文件",
            "data": {"dir_path": "/tmp", "files": ["/tmp/a.txt"]},
        }
        with patch.object(sandbox.client, "post", new=AsyncMock(return_value=mock_response)):
            result = await sandbox.find_files(dir_path="/tmp", glob_pattern="*.txt")

        assert result.success is True
        assert result.data["files"] == ["/tmp/a.txt"]
        await sandbox.client.aclose()

    @pytest.mark.asyncio
    async def test_timeout_message_includes_path_and_pattern(self):
        """超时消息包含 dir_path 和 glob_pattern 帮助 LLM 诊断"""
        sandbox = DockerSandbox(ip="127.0.0.1", container_name="test")
        with patch.object(sandbox.client, "post", new=AsyncMock(
            side_effect=httpx.TimeoutException("timeout")
        )):
            result = await sandbox.find_files(
                dir_path="/workspace/deep", glob_pattern="**/config.yaml"
            )
        assert "/workspace/deep" in result.message
        assert "**/config.yaml" in result.message
        await sandbox.client.aclose()


# ============================================================
# 集成场景:模拟会话 f2611353 的复现路径
# ============================================================

class TestStuckSessionReproduction:
    """复现会话 f2611353 场景:LLM 调用 find_files(dir_path="/", ...)"""

    @pytest.mark.asyncio
    async def test_root_scan_blocked_before_sandbox_call(self):
        """根目录扫描在到达 sandbox 前被拦截(不会触发 glob 挂起)"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.find_files(
            dir_path="/",
            glob_pattern="**/pptxgenjs.md",
        )
        assert result.success is False
        assert "禁止扫描系统目录" in result.message
        sandbox.find_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_workspace_scan_succeeds(self):
        """工作区目录扫描正常转发(用户正确用法)"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.find_files(
            dir_path="/sandbox",
            glob_pattern="**/pptxgenjs.md",
        )
        assert result.success is True
        sandbox.find_files.assert_called_once_with(
            dir_path="/sandbox", glob_pattern="**/pptxgenjs.md"
        )
