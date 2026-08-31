#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_docker_sandbox_ensure.py
DockerSandbox.ensure_sandbox() 单元测试 — 验证一次性程序(EXITED+exitstatus=0)识别

修复点(回归保护):
- python-warmup 等 autorestart=false 的一次性程序, 完成后状态为 EXITED, exitstatus=0
- 原 ensure_sandbox() 仅认可 RUNNING, 导致沙箱就绪判定被一次性程序阻塞 30 次重试后失败
- 修复后: RUNNING 视为正常; EXITED+exitstatus=0 视为一次性程序正常完成; 其他状态视为未就绪

测试策略:
- 通过 mock httpx.AsyncClient.get 返回不同 supervisor 状态组合, 验证判定逻辑
- 覆盖: 全部 RUNNING / 含 EXITED+0 / 含 EXITED+非0 / 含 STARTING / 空服务列表 / 异常路径
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


def _make_service(name: str, statename: str, exitstatus: int = 0, group: str = "services") -> dict:
    """构造 supervisor 服务状态字典"""
    return {
        "name": name,
        "group": group,
        "statename": statename,
        "exitstatus": exitstatus,
        "state": 20 if statename == "RUNNING" else 100,
    }


def _make_response(services: list, success: bool = True) -> MagicMock:
    """构造 supervisor /api/supervisor/status 响应 mock"""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "code": 200 if success else 500,
        "msg": "获取沙箱进程服务成功",
        "data": services,
    }
    resp.raise_for_status = MagicMock()
    return resp


def _make_sandbox() -> DockerSandbox:
    """构造测试用 DockerSandbox 实例(跳过真实连接)"""
    sb = DockerSandbox(ip="127.0.0.1")
    return sb


class TestEnsureSandboxAllRunning:
    """场景: 所有服务 RUNNING → 立即返回, 不抛异常"""

    @pytest.mark.asyncio
    async def test_all_running_returns_immediately(self):
        services = [
            _make_service("app", "RUNNING"),
            _make_service("chrome", "RUNNING"),
            _make_service("xvfb", "RUNNING"),
        ]
        sb = _make_sandbox()
        with patch.object(sb.client, "get", new=AsyncMock(return_value=_make_response(services))):
            # 应在第一次尝试即返回, 不会抛异常
            await sb.ensure_sandbox()


class TestEnsureSandboxOneshotExited:
    """场景: 含 EXITED+exitstatus=0 的一次性程序(python-warmup) → 视为正常"""

    @pytest.mark.asyncio
    async def test_python_warmup_exited_success_treated_as_ready(self):
        """python-warmup EXITED + exitstatus=0 不应阻塞就绪判定"""
        services = [
            _make_service("python-warmup", "EXITED", exitstatus=0, group="python-warmup"),
            _make_service("app", "RUNNING"),
            _make_service("chrome", "RUNNING"),
            _make_service("xvfb", "RUNNING"),
        ]
        sb = _make_sandbox()
        with patch.object(sb.client, "get", new=AsyncMock(return_value=_make_response(services))):
            # 应在第一次尝试即返回, 不等待 30 次重试
            await sb.ensure_sandbox()

    @pytest.mark.asyncio
    async def test_multiple_oneshot_exited_success_treated_as_ready(self):
        """多个一次性程序都 EXITED+0 → 视为正常"""
        services = [
            _make_service("python-warmup", "EXITED", exitstatus=0, group="python-warmup"),
            _make_service("node-warmup", "EXITED", exitstatus=0, group="node-warmup"),
            _make_service("app", "RUNNING"),
        ]
        sb = _make_sandbox()
        with patch.object(sb.client, "get", new=AsyncMock(return_value=_make_response(services))):
            await sb.ensure_sandbox()


class TestEnsureSandboxFailedOneshot:
    """场景: EXITED + exitstatus != 0 → 视为未就绪, 重试 30 次后抛异常"""

    @pytest.mark.asyncio
    async def test_python_warmup_exited_failure_blocks_ready(self):
        """python-warmup EXITED + exitstatus=1(失败) → 不视为正常"""
        services = [
            _make_service("python-warmup", "EXITED", exitstatus=1, group="python-warmup"),
            _make_service("app", "RUNNING"),
        ]
        sb = _make_sandbox()
        # 缩短重试间隔以加速测试
        with patch.object(sb.client, "get", new=AsyncMock(return_value=_make_response(services))):
            with patch("asyncio.sleep", new=AsyncMock()):
                with pytest.raises(Exception, match="30次尝试"):
                    await sb.ensure_sandbox()


class TestEnsureSandboxStarting:
    """场景: 含 STARTING 状态 → 视为未就绪"""

    @pytest.mark.asyncio
    async def test_starting_service_blocks_ready(self):
        services = [
            _make_service("app", "RUNNING"),
            _make_service("chrome", "STARTING"),
        ]
        sb = _make_sandbox()
        with patch.object(sb.client, "get", new=AsyncMock(return_value=_make_response(services))):
            with patch("asyncio.sleep", new=AsyncMock()):
                with pytest.raises(Exception, match="30次尝试"):
                    await sb.ensure_sandbox()


class TestEnsureSandboxEmptyServices:
    """场景: services 列表为空 → 重试 30 次后抛异常"""

    @pytest.mark.asyncio
    async def test_empty_services_raises(self):
        sb = _make_sandbox()
        with patch.object(sb.client, "get", new=AsyncMock(return_value=_make_response([]))):
            with patch("asyncio.sleep", new=AsyncMock()):
                with pytest.raises(Exception, match="30次尝试"):
                    await sb.ensure_sandbox()


class TestEnsureSandboxException:
    """场景: HTTP 调用异常 → 重试 30 次后抛异常"""

    @pytest.mark.asyncio
    async def test_http_exception_raises(self):
        sb = _make_sandbox()
        with patch.object(sb.client, "get", new=AsyncMock(side_effect=Exception("network error"))):
            with patch("asyncio.sleep", new=AsyncMock()):
                with pytest.raises(Exception, match="30次尝试"):
                    await sb.ensure_sandbox()
