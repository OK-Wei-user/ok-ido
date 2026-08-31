#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_tools.py
MCP工具注册与文件工具单元测试
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from mcp.server.fastmcp import FastMCP

from mcp_multimodal.config import MultimodalConfig, ScreenshotConfig
from mcp_multimodal.client import BigModelClient
from mcp_multimodal.tools import ALL_TOOL_REGISTRARS
from mcp_multimodal.utils.screenshot import ScreenshotCapture
from mcp_multimodal.utils.file_utils import (
    is_url,
    is_upload_ref,
    is_base64_data,
    is_sandbox_path,
    detect_file_type,
    FileLoadError,
    SandboxFileError,
    decode_base64_data,
)


@pytest.fixture
def mcp_server():
    return FastMCP(name="test-multimodal")


@pytest.fixture
def client():
    config = MultimodalConfig(api_key="test-key")
    return BigModelClient(config)


@pytest.fixture
def screenshot():
    config = ScreenshotConfig()
    return ScreenshotCapture(config)


class TestToolRegistration:
    def test_all_registrars_defined(self):
        assert len(ALL_TOOL_REGISTRARS) == 8

    def test_registrar_names(self):
        expected = {
            "register_image_understand",
            "register_browser_image",
            "register_ocr_extract",
            "register_speech2text",
            "register_video_analyse",
            "register_pdf_parse",
            "register_ppt_parse",
            "register_image_create",
        }
        actual = {r.__name__ for r in ALL_TOOL_REGISTRARS}
        assert actual == expected

    def test_register_all_tools(self, mcp_server, client, screenshot):
        for registrar in ALL_TOOL_REGISTRARS:
            if registrar.__name__ == "register_browser_image":
                registrar(mcp_server, client, screenshot)
            else:
                registrar(mcp_server, client)


class TestFileUtils:
    def test_is_url(self):
        assert is_url("https://example.com/file.png")
        assert is_url("http://example.com/file.png")
        assert not is_url("/local/path/file.png")

    def test_is_upload_ref(self):
        assert is_upload_ref("upload://abc123")
        assert not is_upload_ref("https://example.com/file")

    def test_is_base64_data(self):
        assert is_base64_data("data:image/png;base64,abc123")
        assert not is_base64_data("https://example.com/file")

    def test_is_sandbox_path(self):
        assert is_sandbox_path("/home/ubuntu/upload/test.jpg")
        assert is_sandbox_path("/tmp/test.txt")
        assert is_sandbox_path("/root/file.pdf")
        assert not is_sandbox_path("upload://abc123")
        assert not is_sandbox_path("https://example.com/file")

    def test_detect_file_type(self):
        assert detect_file_type("test.png") == "image"
        assert detect_file_type("test.mp3") == "audio"
        assert detect_file_type("test.mp4") == "video"
        assert detect_file_type("test.pdf") == "pdf"
        assert detect_file_type("test.pptx") == "ppt"
        assert detect_file_type("test.xyz") == "unknown"

    def test_decode_base64_data(self):
        import base64
        original = b"hello world"
        b64 = base64.b64encode(original).decode("utf-8")
        data_uri = f"data:text/plain;base64,{b64}"
        content, filename = decode_base64_data(data_uri)
        assert content == original


class TestSandboxFileError:
    def test_error_message_contains_curl_command(self):
        path = "/home/ubuntu/upload/test.jpg"
        error = SandboxFileError(path)
        error_str = str(error)
        assert path in error_str
        assert "curl -F file=@" in error_str
        assert "http://mcp-multimodal:9000/upload" in error_str
        assert "upload://" in error_str

    def test_sandbox_path_attribute(self):
        path = "/home/ubuntu/upload/test.jpg"
        error = SandboxFileError(path)
        assert error.sandbox_path == path

    def test_is_subclass_of_file_load_error(self):
        error = SandboxFileError("/home/ubuntu/test.jpg")
        assert isinstance(error, FileLoadError)


class TestScreenshotCapture:
    def test_available_when_not_started(self, screenshot):
        assert not screenshot.available

    @pytest.mark.asyncio
    async def test_capture_when_not_started(self, screenshot):
        with pytest.raises(RuntimeError, match="未启动"):
            await screenshot.capture("https://example.com")
