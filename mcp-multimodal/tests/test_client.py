#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_client.py
BigModel API客户端单元测试
"""
import pytest
from unittest.mock import AsyncMock, patch

from mcp_multimodal.client import BigModelClient, BigModelAPIError
from mcp_multimodal.config import MultimodalConfig


@pytest.fixture
def client():
    config = MultimodalConfig(api_key="test-api-key")
    return BigModelClient(config)


class TestBigModelClientProperties:
    def test_base_url(self, client):
        assert client.base_url == "https://open.bigmodel.cn/api/paas/v4"

    def test_chat_url(self, client):
        assert client.chat_url == "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    def test_images_url(self, client):
        assert client.images_url == "https://open.bigmodel.cn/api/paas/v4/images/generations"

    def test_ocr_url(self, client):
        assert client.ocr_url == "https://open.bigmodel.cn/api/paas/v4/files/ocr"

    def test_config_property(self, client):
        assert client.config.api_key == "test-api-key"


class TestBuildImageContent:
    def test_with_url(self, client):
        result = BigModelClient.build_image_content(image_url="https://example.com/img.png")
        assert result["type"] == "image_url"
        assert result["image_url"]["url"] == "https://example.com/img.png"

    def test_with_base64(self, client):
        result = BigModelClient.build_image_content(image_base64="abc123")
        assert result["type"] == "image_url"
        assert result["image_url"]["url"] == "data:image/png;base64,abc123"

    def test_no_input(self, client):
        with pytest.raises(ValueError, match="image_url和image_base64至少提供一个"):
            BigModelClient.build_image_content()


class TestVlChat:
    @pytest.mark.asyncio
    async def test_with_url(self, client):
        mock_response = {
            "choices": [{"message": {"content": "这是一张风景图片"}}]
        }
        with patch.object(client, "_post_json", new_callable=AsyncMock, return_value=mock_response):
            result = await client.vl_chat(
                prompt="描述图片",
                image_urls=["https://example.com/img.png"],
            )
            assert result == "这是一张风景图片"

    @pytest.mark.asyncio
    async def test_with_base64(self, client):
        mock_response = {
            "choices": [{"message": {"content": "图片内容分析结果"}}]
        }
        with patch.object(client, "_post_json", new_callable=AsyncMock, return_value=mock_response):
            result = await client.vl_chat(
                prompt="分析图片",
                image_base64_list=["abc123"],
            )
            assert result == "图片内容分析结果"


class TestOCRExtract:
    @pytest.mark.asyncio
    async def test_success(self, client):
        mock_response = {
            "words_result": [
                {"words": "第一行文字"},
                {"words": "第二行文字"},
            ]
        }
        with patch.object(client, "_post_bytes", new_callable=AsyncMock, return_value=mock_response):
            result = await client.ocr_extract(b"fake-image", "test.png")
            assert "第一行文字" in result
            assert "第二行文字" in result

    @pytest.mark.asyncio
    async def test_empty_result(self, client):
        mock_response = {"words_result": []}
        with patch.object(client, "_post_bytes", new_callable=AsyncMock, return_value=mock_response):
            result = await client.ocr_extract(b"fake-image", "test.png")
            assert "未识别到文字内容" in result


class TestImageGenerate:
    @pytest.mark.asyncio
    async def test_success(self, client):
        mock_response = {
            "data": [{"url": "https://example.com/generated.png"}]
        }
        with patch.object(client, "_post_json", new_callable=AsyncMock, return_value=mock_response):
            result = await client.image_generate(prompt="一只猫")
            assert result == "https://example.com/generated.png"

    @pytest.mark.asyncio
    async def test_empty_result(self, client):
        mock_response = {"data": []}
        with patch.object(client, "_post_json", new_callable=AsyncMock, return_value=mock_response):
            with pytest.raises(BigModelAPIError, match="图像生成失败"):
                await client.image_generate(prompt="一只猫")


class TestBigModelAPIError:
    def test_error_with_status_code(self):
        error = BigModelAPIError("test error", status_code=429)
        assert str(error) == "test error"
        assert error.status_code == 429

    def test_error_without_status_code(self):
        error = BigModelAPIError("test error")
        assert error.status_code is None
