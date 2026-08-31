#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_mcp_sandbox.py
MCPTool沙箱路径自动解析功能的单元测试
"""
import asyncio
import io
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.domain.services.tools.mcp import (
    MCPTool,
    MCPClientManager,
    _is_sandbox_path,
    _MCP_CONNECT_TIMEOUT,
    _MCP_HEALTH_CHECK_TIMEOUT,
)
from app.domain.models.app_config import MCPConfig, MCPServerConfig, MCPTransport


class TestIsSandboxPath:
    def test_home_ubuntu(self):
        assert _is_sandbox_path("/home/ubuntu/test.png") is True

    def test_tmp(self):
        assert _is_sandbox_path("/tmp/output.pdf") is True

    def test_root(self):
        assert _is_sandbox_path("/root/data.csv") is True

    def test_url_not_sandbox(self):
        assert _is_sandbox_path("https://example.com/image.png") is False

    def test_upload_ref_not_sandbox(self):
        assert _is_sandbox_path("upload://abc123") is False

    def test_relative_path_not_sandbox(self):
        assert _is_sandbox_path("data/test.png") is False

    def test_empty_string(self):
        assert _is_sandbox_path("") is False

    def test_none(self):
        assert _is_sandbox_path(None) is False

    def test_integer(self):
        assert _is_sandbox_path(123) is False


class TestGetUploadUrl:
    def _make_mcp_tool(self, servers):
        tool = MCPTool()
        tool._mcp_config = MCPConfig(mcpServers=servers)
        return tool

    def test_streamable_http_server(self):
        servers = {
            "mcp-multimodal": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://mcp-multimodal:9100/mcp",
            ),
        }
        tool = self._make_mcp_tool(servers)
        url = tool._get_upload_url("mcp_mcp-multimodal_image_create")
        assert url == "http://mcp-multimodal:9100/upload"

    def test_amap_server(self):
        servers = {
            "amap": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="https://mcp.amap.com/mcp?key=xxx",
            ),
        }
        tool = self._make_mcp_tool(servers)
        # 服务名简化为amap后,工具名前缀为mcp_amap_(无连字符,更简洁)
        url = tool._get_upload_url("mcp_amap_maps_direction")
        assert url == "https://mcp.amap.com/upload"
        # 注:连字符服务名的解析回归测试由test_streamable_http_server(mcp-multimodal)覆盖

    def test_unknown_tool_returns_none(self):
        servers = {
            "mcp-multimodal": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://mcp-multimodal:9100/mcp",
            ),
        }
        tool = self._make_mcp_tool(servers)
        url = tool._get_upload_url("mcp_unknown_tool")
        assert url is None

    def test_no_config_returns_none(self):
        tool = MCPTool()
        tool._mcp_config = None
        url = tool._get_upload_url("mcp_mcp-multimodal_image_create")
        assert url is None


class TestResolveSandboxPaths:
    def _make_mcp_tool(self, sandbox=None, servers=None):
        tool = MCPTool(sandbox=sandbox)
        if servers is not None:
            tool._mcp_config = MCPConfig(mcpServers=servers)
        return tool

    @pytest.mark.asyncio
    async def test_no_sandbox_returns_original(self):
        tool = self._make_mcp_tool(sandbox=None)
        args = {"image_source": "/home/ubuntu/test.png"}
        result = await tool._resolve_sandbox_paths("mcp_mcp-multimodal_vl_image_understand", args)
        assert result == args

    @pytest.mark.asyncio
    async def test_no_config_returns_original(self):
        sandbox = MagicMock()
        tool = self._make_mcp_tool(sandbox=sandbox, servers=None)
        tool._mcp_config = None
        args = {"image_source": "/home/ubuntu/test.png"}
        result = await tool._resolve_sandbox_paths("mcp_mcp-multimodal_vl_image_understand", args)
        assert result == args

    @pytest.mark.asyncio
    async def test_url_not_resolved(self):
        sandbox = MagicMock()
        servers = {
            "mcp-multimodal": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://mcp-multimodal:9100/mcp",
            ),
        }
        tool = self._make_mcp_tool(sandbox=sandbox, servers=servers)
        args = {"image_source": "https://example.com/image.png"}
        result = await tool._resolve_sandbox_paths("mcp_mcp-multimodal_vl_image_understand", args)
        assert result["image_source"] == "https://example.com/image.png"

    @pytest.mark.asyncio
    async def test_upload_ref_not_resolved(self):
        sandbox = MagicMock()
        servers = {
            "mcp-multimodal": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://mcp-multimodal:9100/mcp",
            ),
        }
        tool = self._make_mcp_tool(sandbox=sandbox, servers=servers)
        args = {"image_source": "upload://abc123"}
        result = await tool._resolve_sandbox_paths("mcp_mcp-multimodal_vl_image_understand", args)
        assert result["image_source"] == "upload://abc123"

    @pytest.mark.asyncio
    async def test_sandbox_path_auto_uploaded(self):
        sandbox = MagicMock()
        sandbox.download_file = AsyncMock(return_value=io.BytesIO(b"fake image data"))

        servers = {
            "mcp-multimodal": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://mcp-multimodal:9100/mcp",
            ),
        }
        tool = self._make_mcp_tool(sandbox=sandbox, servers=servers)

        mock_response = MagicMock()
        mock_response.json.return_value = {"upload_ref": "upload://auto123", "file_id": "auto123"}
        mock_response.raise_for_status = MagicMock()

        with patch("app.domain.services.tools.mcp.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            args = {"image_source": "/home/ubuntu/chart.png"}
            result = await tool._resolve_sandbox_paths("mcp_mcp-multimodal_vl_image_understand", args)
            assert result["image_source"] == "upload://auto123"

    @pytest.mark.asyncio
    async def test_mixed_args_partial_sandbox(self):
        sandbox = MagicMock()
        sandbox.download_file = AsyncMock(return_value=io.BytesIO(b"data"))

        servers = {
            "mcp-multimodal": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://mcp-multimodal:9100/mcp",
            ),
        }
        tool = self._make_mcp_tool(sandbox=sandbox, servers=servers)

        mock_response = MagicMock()
        mock_response.json.return_value = {"upload_ref": "upload://xyz789", "file_id": "xyz789"}
        mock_response.raise_for_status = MagicMock()

        with patch("app.domain.services.tools.mcp.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            args = {
                "image_source": "/home/ubuntu/test.png",
                "prompt": "描述图片内容",
            }
            result = await tool._resolve_sandbox_paths("mcp_mcp-multimodal_vl_image_understand", args)
            assert result["image_source"] == "upload://xyz789"
            assert result["prompt"] == "描述图片内容"

    @pytest.mark.asyncio
    async def test_upload_failure_keeps_original(self):
        sandbox = MagicMock()
        sandbox.download_file = AsyncMock(side_effect=RuntimeError("下载失败"))

        servers = {
            "mcp-multimodal": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://mcp-multimodal:9100/mcp",
            ),
        }
        tool = self._make_mcp_tool(sandbox=sandbox, servers=servers)

        args = {"image_source": "/home/ubuntu/test.png"}
        result = await tool._resolve_sandbox_paths("mcp_mcp-multimodal_vl_image_understand", args)
        assert result["image_source"] == "/home/ubuntu/test.png"

    @pytest.mark.asyncio
    async def test_unknown_tool_no_upload_url(self):
        sandbox = MagicMock()
        servers = {
            "mcp-multimodal": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://mcp-multimodal:9100/mcp",
            ),
        }
        tool = self._make_mcp_tool(sandbox=sandbox, servers=servers)

        args = {"image_source": "/home/ubuntu/test.png"}
        result = await tool._resolve_sandbox_paths("mcp_unknown_tool", args)
        assert result["image_source"] == "/home/ubuntu/test.png"


class TestMCPClientManagerTimeout:
    """MCP客户端管理器超时容错测试"""

    def _make_config(self, servers=None):
        if servers is None:
            servers = {
                "test-server": MCPServerConfig(
                    transport=MCPTransport.STREAMABLE_HTTP,
                    enabled=True,
                    url="http://unreachable:8080/mcp",
                ),
            }
        return MCPConfig(mcpServers=servers)

    @pytest.mark.asyncio
    async def test_check_server_reachable_returns_false_on_connect_error(self):
        """服务器不可达时_check_server_reachable返回False"""
        with patch("app.domain.services.tools.mcp.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Network is unreachable"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await MCPClientManager._check_server_reachable("http://unreachable:8080/mcp")
            assert result is False

    @pytest.mark.asyncio
    async def test_check_server_reachable_returns_true_on_success(self):
        """服务器可达时_check_server_reachable返回True"""
        with patch("app.domain.services.tools.mcp.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await MCPClientManager._check_server_reachable("http://good:9100/mcp")
            assert result is True

    @pytest.mark.asyncio
    async def test_check_server_reachable_returns_false_on_timeout(self):
        """服务器连接超时时_check_server_reachable返回False"""
        with patch("app.domain.services.tools.mcp.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await MCPClientManager._check_server_reachable("http://slow:9100/mcp")
            assert result is False

    @pytest.mark.asyncio
    async def test_connect_mcp_servers_skips_failed_server(self):
        """单个MCP服务器连接失败不影响其他服务器"""
        servers = {
            "good-server": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://good:9100/mcp",
            ),
            "bad-server": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://bad:8080/mcp",
            ),
        }
        manager = MCPClientManager(mcp_config=self._make_config(servers))
        call_log = []

        async def mock_connect(name, config):
            call_log.append(name)
            if name == "bad-server":
                raise ConnectionError("Network is unreachable")

        manager._connect_mcp_server = mock_connect
        # 预检可达性返回True,使连接逻辑能执行到mock_connect
        # (测试URL非真实服务,不mock预检会因不可达被跳过)
        with patch.object(MCPClientManager, "_check_server_reachable", return_value=True):
            await manager._connect_mcp_servers()

        assert "good-server" in call_log
        assert "bad-server" in call_log
        # 两个服务器都被尝试连接
        assert len(call_log) == 2

    @pytest.mark.asyncio
    async def test_connect_mcp_servers_all_fail_still_completes(self):
        """所有MCP服务器连接失败时_connect_mcp_servers仍正常返回"""
        servers = {
            "bad1": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://bad1:8080/mcp",
            ),
            "bad2": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://bad2:8080/mcp",
            ),
        }
        manager = MCPClientManager(mcp_config=self._make_config(servers))

        async def mock_connect(name, config):
            raise ConnectionError(f"unreachable: {name}")

        manager._connect_mcp_server = mock_connect
        # 不应抛出异常
        await manager._connect_mcp_servers()

    @pytest.mark.asyncio
    async def test_streamable_http_passes_connect_timeout(self):
        """streamable_http连接时传递timeout参数"""
        manager = MCPClientManager(mcp_config=self._make_config())
        config = MCPServerConfig(
            transport=MCPTransport.STREAMABLE_HTTP,
            enabled=True,
            url="http://test:9100/mcp",
        )

        with patch.object(MCPClientManager, "_check_server_reachable", return_value=True):
            with patch("app.domain.services.tools.mcp.streamablehttp_client") as mock_factory:
                mock_ctx = AsyncMock()
                mock_factory.return_value = mock_ctx
                manager._exit_stack.enter_async_context = AsyncMock(side_effect=ConnectionError("skip"))
                try:
                    await manager._connect_streamable_http_server("test", config)
                except ConnectionError:
                    pass

                mock_factory.assert_called_once_with(
                    url="http://test:9100/mcp",
                    headers=None,
                    timeout=_MCP_CONNECT_TIMEOUT,
                )

    @pytest.mark.asyncio
    async def test_sse_passes_connect_timeout(self):
        """SSE连接时传递timeout参数"""
        manager = MCPClientManager(mcp_config=self._make_config())
        config = MCPServerConfig(
            transport=MCPTransport.SSE,
            enabled=True,
            url="http://test:9100/sse",
        )

        with patch.object(MCPClientManager, "_check_server_reachable", return_value=True):
            with patch("app.domain.services.tools.mcp.sse_client") as mock_factory:
                mock_factory.return_value = AsyncMock()
                manager._exit_stack.enter_async_context = AsyncMock(side_effect=ConnectionError("skip"))
                try:
                    await manager._connect_sse_server("test", config)
                except ConnectionError:
                    pass

                mock_factory.assert_called_once_with(
                    url="http://test:9100/sse",
                    headers=None,
                    timeout=_MCP_CONNECT_TIMEOUT,
                )

    @pytest.mark.asyncio
    async def test_streamable_http_unreachable_raises_connection_error(self):
        """streamable_http服务器不可达时抛出ConnectionError而非CancelledError"""
        manager = MCPClientManager(mcp_config=self._make_config())
        config = MCPServerConfig(
            transport=MCPTransport.STREAMABLE_HTTP,
            enabled=True,
            url="http://unreachable:8080/mcp",
        )

        with patch.object(MCPClientManager, "_check_server_reachable", return_value=False):
            with patch("app.domain.services.tools.mcp.streamablehttp_client") as mock_factory:
                with pytest.raises(ConnectionError, match="不可达"):
                    await manager._connect_streamable_http_server("test", config)
                # 不可达时不应调用streamablehttp_client
                mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_sse_unreachable_raises_connection_error(self):
        """SSE服务器不可达时抛出ConnectionError而非CancelledError"""
        manager = MCPClientManager(mcp_config=self._make_config())
        config = MCPServerConfig(
            transport=MCPTransport.SSE,
            enabled=True,
            url="http://unreachable:8080/sse",
        )

        with patch.object(MCPClientManager, "_check_server_reachable", return_value=False):
            with patch("app.domain.services.tools.mcp.sse_client") as mock_factory:
                with pytest.raises(ConnectionError, match="不可达"):
                    await manager._connect_sse_server("test", config)
                mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_mcp_servers_catches_cancelled_error(self):
        """_connect_mcp_servers捕获CancelledError防止单个服务不可达中断整个流程"""
        servers = {
            "bad-cancel": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://bad:8080/mcp",
            ),
            "good": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://good:9100/mcp",
            ),
        }
        manager = MCPClientManager(mcp_config=self._make_config(servers))
        call_log = []

        async def mock_connect(name, config):
            call_log.append(name)
            if name == "bad-cancel":
                raise asyncio.CancelledError()

        manager._connect_mcp_server = mock_connect
        # 预检可达性返回True,使连接逻辑能执行到mock_connect
        # (测试URL非真实服务,不mock预检会因不可达被跳过)
        with patch.object(MCPClientManager, "_check_server_reachable", return_value=True):
            # 不应抛出CancelledError
            await manager._connect_mcp_servers()
        assert "bad-cancel" in call_log
        assert "good" in call_log

    def test_timeout_constants_are_reasonable(self):
        """超时常量值在合理范围内"""
        assert 5 <= _MCP_CONNECT_TIMEOUT <= 30
        assert 1 <= _MCP_HEALTH_CHECK_TIMEOUT <= 10
        assert _MCP_CONNECT_TIMEOUT >= _MCP_HEALTH_CHECK_TIMEOUT


class TestMCPClientManagerIntegration:
    """MCP客户端管理器集成容错测试"""

    @pytest.mark.asyncio
    async def test_initialize_succeeds_with_partial_failures(self):
        """即使部分MCP服务器连接失败，initialize仍标记为已初始化"""
        servers = {
            "good": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://good:9100/mcp",
            ),
            "bad": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://bad:8080/mcp",
            ),
        }
        config = MCPConfig(mcpServers=servers)
        manager = MCPClientManager(mcp_config=config)

        async def mock_connect(name, cfg):
            if name == "bad":
                raise ConnectionError("unreachable")

        manager._connect_mcp_server = mock_connect
        await manager.initialize()

        assert manager._initialized is True

    @pytest.mark.asyncio
    async def test_initialize_succeeds_even_all_fail(self):
        """即使所有MCP服务器连接失败，initialize仍标记为已初始化（降级运行）"""
        servers = {
            "bad1": MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://bad1:8080/mcp",
            ),
        }
        config = MCPConfig(mcpServers=servers)
        manager = MCPClientManager(mcp_config=config)

        async def mock_connect(name, cfg):
            raise RuntimeError("all failed")

        manager._connect_mcp_server = mock_connect
        await manager.initialize()

        assert manager._initialized is True


class _FakeTextContent:
    """模拟MCP TextContent(type=text)"""
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeImageContent:
    """模拟MCP ImageContent(type=image)，兼容camelCase与snake_case mimeType"""
    def __init__(self, data: str, mime: str = "image/png", use_snake: bool = False):
        self.type = "image"
        self.data = data
        if use_snake:
            self.mime_type = mime
        else:
            self.mimeType = mime


class _FakeCallResult:
    """模拟session.call_tool返回的CallToolResult"""
    def __init__(self, content):
        self.content = content


class TestMcpContentParsing:
    """_parse_mcp_content 多模态内容解析测试"""

    def test_parse_text_only_content(self):
        """纯文本content：返回text_parts列表，images为空"""
        result = _FakeCallResult([
            _FakeTextContent("hello"),
            _FakeTextContent("world"),
        ])
        text_parts, images = MCPClientManager._parse_mcp_content(result)
        assert text_parts == ["hello", "world"]
        assert images == []

    def test_parse_image_content_with_camel_mime(self):
        """ImageContent(type=image, mimeType=...)：提取base64与mime_type"""
        result = _FakeCallResult([_FakeImageContent("AAA", "image/jpeg")])
        text_parts, images = MCPClientManager._parse_mcp_content(result)
        assert text_parts == []
        assert len(images) == 1
        assert images[0] == {"data": "AAA", "mime_type": "image/jpeg"}

    def test_parse_image_content_with_snake_mime(self):
        """ImageContent兼容snake_case mime_type字段"""
        result = _FakeCallResult([_FakeImageContent("BBB", "image/webp", use_snake=True)])
        _, images = MCPClientManager._parse_mcp_content(result)
        assert images[0] == {"data": "BBB", "mime_type": "image/webp"}

    def test_parse_image_content_default_mime(self):
        """ImageContent缺少mimeType时回退为image/png"""
        item = _FakeImageContent("CCC", "")
        # 移除mimeType属性模拟缺失场景
        if hasattr(item, "mimeType"):
            del item.mimeType
        if hasattr(item, "mime_type"):
            del item.mime_type
        result = _FakeCallResult([item])
        _, images = MCPClientManager._parse_mcp_content(result)
        assert images[0] == {"data": "CCC", "mime_type": "image/png"}

    def test_parse_mixed_content(self):
        """混合文本与图片content：text_parts与images分别填充"""
        result = _FakeCallResult([
            _FakeTextContent("caption"),
            _FakeImageContent("IMG1", "image/png"),
            _FakeTextContent("footer"),
            _FakeImageContent("IMG2", "image/jpeg"),
        ])
        text_parts, images = MCPClientManager._parse_mcp_content(result)
        assert text_parts == ["caption", "footer"]
        assert len(images) == 2
        assert images[0]["data"] == "IMG1"
        assert images[1]["data"] == "IMG2"

    def test_parse_empty_content_returns_empty(self):
        """content为空列表：返回空text_parts与images"""
        result = _FakeCallResult([])
        text_parts, images = MCPClientManager._parse_mcp_content(result)
        assert text_parts == []
        assert images == []

    def test_parse_result_without_content_attribute(self):
        """result对象无content属性：返回空列表，不抛异常"""
        class EmptyResult:
            pass
        result = EmptyResult()  # 普通对象，无content属性
        text_parts, images = MCPClientManager._parse_mcp_content(result)
        assert text_parts == []
        assert images == []

    def test_parse_result_with_none_content(self):
        """result.content为None：返回空列表，不抛异常"""
        result = _FakeCallResult(None)
        text_parts, images = MCPClientManager._parse_mcp_content(result)
        assert text_parts == []
        assert images == []

    def test_parse_image_with_empty_data_skipped(self):
        """ImageContent的data为空字符串时跳过"""
        result = _FakeCallResult([_FakeImageContent("", "image/png")])
        _, images = MCPClientManager._parse_mcp_content(result)
        assert images == []

    def test_parse_image_with_non_string_data_skipped(self):
        """ImageContent的data非字符串(如bytes)时跳过"""
        item = _FakeImageContent("placeholder", "image/png")
        item.data = b"bytes_data"  # 覆盖为非字符串
        result = _FakeCallResult([item])
        _, images = MCPClientManager._parse_mcp_content(result)
        assert images == []

    def test_parse_unknown_item_with_data_no_text_falls_to_image_branch(self):
        """未知type但带data且无text属性的item进入图片分支；
        data非字符串时被静默丢弃，images与text_parts均为空"""
        class AudioItem:
            type = "audio"
            data = b"audio_bytes"  # 非字符串
            # 无text属性
        result = _FakeCallResult([AudioItem()])
        text_parts, images = MCPClientManager._parse_mcp_content(result)
        # 无text属性但有data → 进入图片分支；data非字符串 → 跳过
        assert images == []
        assert text_parts == []

    def test_parse_unknown_item_no_data_no_text_falls_back_to_str(self):
        """既无data也无text属性的未知项转字符串后进入text_parts"""
        class UnknownItem:
            type = "audio"
            # 无text也无data属性
            def __str__(self):
                return "audio_item"
        result = _FakeCallResult([UnknownItem()])
        text_parts, images = MCPClientManager._parse_mcp_content(result)
        # 既无text也无data → 进入else分支，转字符串
        assert images == []
        assert len(text_parts) == 1
        assert text_parts[0] == "audio_item"


class TestMcpInvokeMultimodal:
    """invoke方法多模态返回测试"""

    def _make_manager(self, server_name: str = "test-server") -> MCPClientManager:
        servers = {
            server_name: MCPServerConfig(
                transport=MCPTransport.STREAMABLE_HTTP,
                enabled=True,
                url="http://test:9100/mcp",
            ),
        }
        return MCPClientManager(mcp_config=MCPConfig(mcpServers=servers))

    @pytest.mark.asyncio
    async def test_invoke_text_only_returns_string_data(self):
        """MCP工具返回纯文本时，ToolResult.data为字符串"""
        manager = self._make_manager()
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=_FakeCallResult([
            _FakeTextContent("done"),
        ]))
        manager._clients["test-server"] = mock_session

        result = await manager.invoke("mcp_test-server_tool", {})
        assert result.success is True
        assert isinstance(result.data, str)
        assert result.data == "done"

    @pytest.mark.asyncio
    async def test_invoke_with_image_returns_structured_data(self):
        """MCP工具返回含图片时，ToolResult.data为dict含text与images字段"""
        manager = self._make_manager()
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=_FakeCallResult([
            _FakeTextContent("image generated"),
            _FakeImageContent("BASE64DATA", "image/png"),
        ]))
        manager._clients["test-server"] = mock_session

        result = await manager.invoke("mcp_test-server_gen_image", {})
        assert result.success is True
        assert isinstance(result.data, dict)
        assert result.data["text"] == "image generated"
        assert len(result.data["images"]) == 1
        assert result.data["images"][0] == {"data": "BASE64DATA", "mime_type": "image/png"}

    @pytest.mark.asyncio
    async def test_invoke_image_only_uses_default_text(self):
        """MCP工具仅返回图片时，text字段回退为默认提示"""
        manager = self._make_manager()
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=_FakeCallResult([
            _FakeImageContent("IMG", "image/jpeg"),
        ]))
        manager._clients["test-server"] = mock_session

        result = await manager.invoke("mcp_test-server_only_image", {})
        assert result.success is True
        assert isinstance(result.data, dict)
        assert result.data["text"] == "工具执行成功"
        assert result.data["images"][0]["data"] == "IMG"

    @pytest.mark.asyncio
    async def test_invoke_multiple_images_preserves_order(self):
        """多张图片按返回顺序保留"""
        manager = self._make_manager()
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=_FakeCallResult([
            _FakeImageContent("IMG1", "image/png"),
            _FakeImageContent("IMG2", "image/jpeg"),
            _FakeImageContent("IMG3", "image/webp"),
        ]))
        manager._clients["test-server"] = mock_session

        result = await manager.invoke("mcp_test-server_multi_image", {})
        assert result.success is True
        images = result.data["images"]
        assert len(images) == 3
        assert [img["data"] for img in images] == ["IMG1", "IMG2", "IMG3"]
        assert [img["mime_type"] for img in images] == ["image/png", "image/jpeg", "image/webp"]

    @pytest.mark.asyncio
    async def test_invoke_empty_result_returns_default(self):
        """MCP工具返回空结果时回退为默认提示"""
        manager = self._make_manager()
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=None)
        manager._clients["test-server"] = mock_session

        result = await manager.invoke("mcp_test-server_empty", {})
        assert result.success is True
        assert result.data == "工具执行成功"

    @pytest.mark.asyncio
    async def test_invoke_unknown_tool_returns_failure(self):
        """调用未注册的工具名时返回失败结果"""
        manager = self._make_manager()
        result = await manager.invoke("mcp_unknown_tool", {})
        assert result.success is False
        # AppException.msg不通过str(e)暴露，message仅含工具名前缀
        assert "mcp_unknown_tool" in result.message
