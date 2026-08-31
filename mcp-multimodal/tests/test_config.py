#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_config.py
配置管理单元测试
"""
import os
import tempfile

import pytest
import yaml

from mcp_multimodal.config import (
    AppConfig,
    FileStoreConfig,
    MultimodalConfig,
    ScreenshotConfig,
    ServerConfig,
)


class TestMultimodalConfig:
    def test_default_values(self):
        config = MultimodalConfig()
        assert config.base_url == "https://open.bigmodel.cn/api/paas/v4/"
        assert config.vl_model == "glm-4.6v"
        assert config.image_model == "cogview-4"
        assert config.asr_model == "glm-asr-2512"
        assert config.max_tokens == 4096
        assert config.temperature == 0.7

    def test_custom_values(self):
        config = MultimodalConfig(
            base_url="https://custom.api.com/",
            api_key="test-key",
            vl_model="custom-vl",
            image_model="custom-img",
        )
        assert config.base_url == "https://custom.api.com/"
        assert config.api_key == "test-key"
        assert config.vl_model == "custom-vl"
        assert config.image_model == "custom-img"


class TestScreenshotConfig:
    def test_default_values(self):
        config = ScreenshotConfig()
        assert config.width == 1280
        assert config.height == 800
        assert config.timeout_ms == 30000
        assert config.full_page is False


class TestServerConfig:
    def test_default_values(self):
        config = ServerConfig()
        assert config.host == "0.0.0.0"
        assert config.port == 9000


class TestFileStoreConfig:
    def test_default_values(self):
        config = FileStoreConfig()
        assert config.ttl_seconds == 600
        assert config.cleanup_interval_seconds == 60


class TestAppConfig:
    def test_from_yaml(self):
        data = {
            "multimodal": {
                "base_url": "https://test.api.com/",
                "api_key": "yaml-key",
                "vl_model": "test-vl",
            },
            "server": {
                "host": "127.0.0.1",
                "port": 8080,
            },
            "screenshot": {
                "width": 1920,
                "height": 1080,
            },
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            yaml.dump(data, f)
            filepath = f.name

        try:
            config = AppConfig.from_yaml(filepath)
            assert config.multimodal.base_url == "https://test.api.com/"
            assert config.multimodal.api_key == "yaml-key"
            assert config.multimodal.vl_model == "test-vl"
            assert config.server.host == "127.0.0.1"
            assert config.server.port == 8080
            assert config.screenshot.width == 1920
            assert config.screenshot.height == 1080
        finally:
            os.unlink(filepath)

    def test_from_yaml_missing_file(self):
        config = AppConfig.from_yaml("nonexistent.yaml")
        assert config.multimodal.base_url == "https://open.bigmodel.cn/api/paas/v4/"

    def test_load_with_env_override(self):
        os.environ["MULTIMODAL_API_KEY"] = "env-key"
        os.environ["MCP_SERVER_PORT"] = "7000"
        try:
            config = AppConfig.load()
            assert config.multimodal.api_key == "env-key"
            assert config.server.port == 7000
        finally:
            del os.environ["MULTIMODAL_API_KEY"]
            del os.environ["MCP_SERVER_PORT"]

    def test_default_screenshot_config(self):
        config = AppConfig()
        assert config.screenshot.width == 1280
        assert config.screenshot.height == 800

    def test_default_file_store_config(self):
        config = AppConfig()
        assert config.file_store.ttl_seconds == 600
