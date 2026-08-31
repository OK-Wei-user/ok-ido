#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_docker_sandbox_messages.py
DockerSandbox 消息字面量回归测试 — 验证 F4-代码质量修复: 移除无占位符的 f-string

修复点(回归保护):
- L263: "Supervisor进程中未发现任何服务" 应为纯字符串(原为无效 f-string)
- L501: "沙箱通信超时..." 应为纯字符串
- L507: "沙箱连接失败..." 应为纯字符串

测试策略:
- 通过源码扫描验证 3 处字符串不以 f 前缀开头(防止回退)
- 通过 mock httpx 验证错误路径返回的 ToolResult.message 内容正确
"""
import inspect
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.domain.models.tool_result import ToolResult
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


# ============ 源码扫描: 防止 f-string 回退 ============

class TestFStringRemovalRegression:
    """验证 3 处无效 f-string 已移除(源码扫描)"""

    def _get_source(self) -> str:
        """获取 DockerSandbox 模块源码"""
        return inspect.getsource(DockerSandbox)

    def test_no_fstring_for_supervisor_no_services(self):
        """L263: 'Supervisor进程中未发现任何服务' 不应使用 f-string"""
        source = self._get_source()
        # 源码中应存在纯字符串形式,不存在 f"Supervisor进程中未发现任何服务"
        assert 'logger.warning("Supervisor进程中未发现任何服务")' in source
        assert 'f"Supervisor进程中未发现任何服务"' not in source

    def test_no_fstring_for_sandbox_timeout_message(self):
        """L501: '沙箱通信超时...' 不应使用 f-string(message= 路径)"""
        source = self._get_source()
        # message= 应为纯字符串(message=f"..." 不应出现)
        assert 'message="沙箱通信超时' in source
        assert 'message=f"沙箱通信超时' not in source

    def test_no_fstring_for_sandbox_connection_failure(self):
        """L507: '沙箱连接失败...' 不应使用 f-string(message= 路径)"""
        source = self._get_source()
        # 注意: logger.error(f"沙箱连接失败: {str(e)}") 是合法 f-string(有占位符)
        # 仅验证 message= 路径不为 f-string
        assert 'message="沙箱连接失败' in source
        assert 'message=f"沙箱连接失败' not in source


# ============ 错误路径返回的 ToolResult 内容验证 ============

class TestErrorMessageContent:
    """验证错误路径返回的 ToolResult.message 内容完整"""

    def _make_sandbox_with_mock_client(self) -> DockerSandbox:
        """构造带 mock httpx 客户端的 DockerSandbox"""
        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.client = MagicMock()
        sandbox._ip = "127.0.0.1"
        sandbox._container_name = "test-sandbox"
        sandbox._base_url = "http://127.0.0.1:8080"
        sandbox._vnc_url = "ws://127.0.0.1:5901"
        sandbox._cdp_url = "http://127.0.0.1:9222"
        return sandbox

    @pytest.mark.asyncio
    async def test_timeout_returns_correct_message(self):
        """httpx.TimeoutException 应返回完整超时提示"""
        sandbox = self._make_sandbox_with_mock_client()

        # 构造 mock 响应链: post -> raise TimeoutException
        sandbox.client.post = AsyncMock(side_effect=httpx.TimeoutException("read timeout"))

        # 调用 exec_command(应在超时分支返回 ToolResult)
        result = await sandbox.exec_command(
            session_id="test-session",
            exec_dir="/",
            command="ls",
        )

        assert isinstance(result, ToolResult)
        assert result.success is False
        assert "沙箱通信超时" in result.message
        assert "shell_read_output" in result.message
        assert "shell_kill_process" in result.message

    @pytest.mark.asyncio
    async def test_connect_error_returns_correct_message(self):
        """httpx.ConnectError 应返回完整连接失败提示"""
        sandbox = self._make_sandbox_with_mock_client()

        sandbox.client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        result = await sandbox.exec_command(
            session_id="test-session",
            exec_dir="/",
            command="ls",
        )

        assert isinstance(result, ToolResult)
        assert result.success is False
        assert "沙箱连接失败" in result.message
        assert "容器可能正在重启" in result.message


# ============ 模块导入完整性 ============

class TestModuleIntegrity:
    """模块导入完整性验证"""

    def test_module_imports_without_unused_dependencies(self):
        """模块应可正常导入(无 unused import 告警)"""
        import app.infrastructure.external.sandbox.docker_sandbox as module

        assert hasattr(module, "DockerSandbox")
        assert hasattr(module, "logger")

    def test_module_source_has_no_fstring_without_placeholders(self):
        """模块源码不应存在无占位符的 f-string(pyflakes 回归保护)"""
        import app.infrastructure.external.sandbox.docker_sandbox as module

        source = open(module.__file__, encoding="utf-8").read()
        # 简易检测: f"..." 中不含 { 的行视为可疑(可能为无效 f-string)
        # 注:此检测为辅助手段,真正的守护靠 pyflakes CI
        lines = source.splitlines()
        suspicious_lines = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # 检测 f"..." 形式但不含 {
            if 'f"' in stripped and "{" not in stripped:
                # 排除注释
                if not stripped.startswith("#"):
                    suspicious_lines.append((i, stripped))

        # 允许少量误报(如字符串内包含 f"),但目标 3 行不应在其中
        for line_no, line_text in suspicious_lines:
            for forbidden in [
                "Supervisor进程中未发现任何服务",
                "沙箱通信超时",
                "沙箱连接失败",
            ]:
                assert forbidden not in line_text, \
                    f"L{line_no} 不应使用无占位符 f-string: {line_text}"
