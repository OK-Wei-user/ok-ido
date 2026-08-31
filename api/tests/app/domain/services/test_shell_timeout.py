#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Shell命令超时机制单元测试 - 覆盖超时解析、命令包装、HTTP超时降级
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.shell import ShellTool
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


class TestShellToolTimeoutParameter:
    """ShellTool的timeout参数传递测试"""

    def setup_method(self):
        self.sandbox = AsyncMock(spec=DockerSandbox)
        self.shell_tool = ShellTool(sandbox=self.sandbox)

    def test_shell_execute_has_timeout_in_schema(self):
        tools = self.shell_tool.get_tools()
        exec_tool = next(t for t in tools if t["function"]["name"] == "shell_execute")
        params = exec_tool["function"]["parameters"]["properties"]
        assert "timeout" in params
        assert params["timeout"]["type"] == "integer"

    def test_shell_execute_timeout_not_required(self):
        tools = self.shell_tool.get_tools()
        exec_tool = next(t for t in tools if t["function"]["name"] == "shell_execute")
        required = exec_tool["function"]["parameters"]["required"]
        assert "timeout" not in required

    @pytest.mark.asyncio
    async def test_shell_execute_passes_timeout_to_sandbox(self):
        self.sandbox.exec_command = AsyncMock(return_value=ToolResult(success=True))
        await self.shell_tool.shell_execute(
            session_id="test-session",
            exec_dir="/home/ubuntu",
            command="echo hello",
            timeout=60,
        )
        self.sandbox.exec_command.assert_called_once_with(
            "test-session", "/home/ubuntu", "echo hello", 60
        )

    @pytest.mark.asyncio
    async def test_shell_execute_without_timeout_passes_none(self):
        self.sandbox.exec_command = AsyncMock(return_value=ToolResult(success=True))
        await self.shell_tool.shell_execute(
            session_id="test-session",
            exec_dir="/home/ubuntu",
            command="echo hello",
        )
        self.sandbox.exec_command.assert_called_once_with(
            "test-session", "/home/ubuntu", "echo hello", None
        )

    def test_shell_execute_description_mentions_timeout(self):
        tools = self.shell_tool.get_tools()
        exec_tool = next(t for t in tools if t["function"]["name"] == "shell_execute")
        description = exec_tool["function"]["description"]
        assert "超时" in description
        assert "300" in description


class TestDockerSandboxTimeoutHandling:
    """DockerSandbox的HTTP超时处理测试"""

    def test_exec_command_includes_timeout_in_request(self):
        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.client = AsyncMock()
        sandbox._base_url = "http://localhost:8080"

        mock_response = MagicMock()
        mock_response.json.return_value = {"code": 200, "msg": "ok", "data": None}
        sandbox.client.post = AsyncMock(return_value=mock_response)

        import asyncio

        async def run():
            result = await sandbox.exec_command(
                session_id="s1", exec_dir="/tmp", command="ls", timeout=120
            )
            call_kwargs = sandbox.client.post.call_args
            body = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1]
            assert body["timeout"] == 120
            return result

        asyncio.run(run())

    def test_exec_command_omits_timeout_when_none(self):
        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.client = AsyncMock()
        sandbox._base_url = "http://localhost:8080"

        mock_response = MagicMock()
        mock_response.json.return_value = {"code": 200, "msg": "ok", "data": None}
        sandbox.client.post = AsyncMock(return_value=mock_response)

        import asyncio
        import httpx

        async def run():
            result = await sandbox.exec_command(
                session_id="s1", exec_dir="/tmp", command="ls", timeout=None
            )
            call_kwargs = sandbox.client.post.call_args
            body = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1]
            assert "timeout" not in body
            return result

        asyncio.run(run())

    def test_exec_command_handles_http_timeout_gracefully(self):
        import httpx

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.client = AsyncMock()
        sandbox._base_url = "http://localhost:8080"
        sandbox.client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        import asyncio

        async def run():
            result = await sandbox.exec_command(
                session_id="s1", exec_dir="/tmp", command="ls"
            )
            assert result.success is False
            assert "超时" in result.message
            return result

        asyncio.run(run())

    def test_exec_command_handles_connect_error_gracefully(self):
        import httpx

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.client = AsyncMock()
        sandbox._base_url = "http://localhost:8080"
        sandbox.client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        import asyncio

        async def run():
            result = await sandbox.exec_command(
                session_id="s1", exec_dir="/tmp", command="ls"
            )
            assert result.success is False
            assert "连接失败" in result.message
            return result

        asyncio.run(run())


class TestSandboxShellTimeoutConfig:
    """沙箱Shell超时配置测试"""

    def test_default_timeout_values(self):
        from sandbox.app.core.config import Settings

        settings = Settings()
        assert settings.shell_default_timeout == 300
        assert settings.shell_max_timeout == 600

    def test_timeout_from_env(self):
        import os

        os.environ["SHELL_DEFAULT_TIMEOUT"] = "120"
        os.environ["SHELL_MAX_TIMEOUT"] = "300"
        try:
            from sandbox.app.core import config

            config.get_settings.cache_clear()
            settings = config.Settings()
            assert settings.shell_default_timeout == 120
            assert settings.shell_max_timeout == 300
        finally:
            os.environ.pop("SHELL_DEFAULT_TIMEOUT", None)
            os.environ.pop("SHELL_MAX_TIMEOUT", None)
            config.get_settings.cache_clear()


class TestSandboxShellServiceTimeout:
    """沙箱ShellService超时逻辑测试"""

    def test_resolve_timeout_returns_default_when_none(self):
        from sandbox.app.services.shell import ShellService
        from sandbox.app.core.config import get_settings

        service = ShellService()
        result = service._resolve_timeout(None)
        settings = get_settings()
        assert result == settings.shell_default_timeout

    def test_resolve_timeout_returns_default_when_zero(self):
        from sandbox.app.services.shell import ShellService

        service = ShellService()
        result = service._resolve_timeout(0)
        assert result == 300

    def test_resolve_timeout_returns_default_when_negative(self):
        from sandbox.app.services.shell import ShellService

        service = ShellService()
        result = service._resolve_timeout(-10)
        assert result == 300

    def test_resolve_timeout_caps_at_max(self):
        from sandbox.app.services.shell import ShellService

        service = ShellService()
        result = service._resolve_timeout(9999)
        assert result == 600

    def test_resolve_timeout_passes_valid_value(self):
        from sandbox.app.services.shell import ShellService

        service = ShellService()
        result = service._resolve_timeout(120)
        assert result == 120


class TestShellModelTimeoutFields:
    """Shell模型超时字段测试"""

    def test_shell_has_started_at(self):
        import time

        from sandbox.app.models.shell import Shell
        import asyncio.subprocess

        shell = Shell(
            process=MagicMock(spec=asyncio.subprocess.Process),
            exec_dir="/tmp",
            output="",
        )
        assert shell.started_at > 0
        assert abs(shell.started_at - time.time()) < 1

    def test_shell_has_timeout_default_none(self):
        from sandbox.app.models.shell import Shell
        import asyncio.subprocess

        shell = Shell(
            process=MagicMock(spec=asyncio.subprocess.Process),
            exec_dir="/tmp",
            output="",
        )
        assert shell.timeout is None

    def test_shell_timeout_can_be_set(self):
        from sandbox.app.models.shell import Shell
        import asyncio.subprocess

        shell = Shell(
            process=MagicMock(spec=asyncio.subprocess.Process),
            exec_dir="/tmp",
            output="",
            timeout=300,
        )
        assert shell.timeout == 300


class TestShellExecuteRequestTimeout:
    """ShellExecuteRequest超时参数测试"""

    def test_request_has_timeout_field(self):
        from sandbox.app.interfaces.schemas.shell import ShellExecuteRequest

        req = ShellExecuteRequest(command="ls", timeout=60)
        assert req.timeout == 60

    def test_request_timeout_defaults_to_none(self):
        from sandbox.app.interfaces.schemas.shell import ShellExecuteRequest

        req = ShellExecuteRequest(command="ls")
        assert req.timeout is None
