#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : config.py
多模态MCP服务配置 - 支持YAML文件与环境变量覆盖
"""
import os
from typing import Optional

from pydantic import BaseModel, Field


class MultimodalConfig(BaseModel):
    base_url: str = Field(default="https://open.bigmodel.cn/api/paas/v4/")
    api_key: str = Field(default="")
    vl_model: str = Field(default="glm-4.6v")
    image_model: str = Field(default="cogview-4")
    asr_model: str = Field(default="glm-asr-2512")
    max_tokens: int = Field(default=4096)
    temperature: float = Field(default=0.7)


class ScreenshotConfig(BaseModel):
    width: int = Field(default=1280, description="截图视口宽度")
    height: int = Field(default=800, description="截图视口高度")
    timeout_ms: int = Field(default=30000, description="页面加载超时(毫秒)")
    full_page: bool = Field(default=False, description="是否全页截图")


class ServerConfig(BaseModel):
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=9000)


class FileStoreConfig(BaseModel):
    ttl_seconds: int = Field(default=600, description="上传文件TTL(秒)")
    cleanup_interval_seconds: int = Field(default=60, description="清理间隔(秒)")


class AppConfig(BaseModel):
    multimodal: MultimodalConfig = Field(default_factory=MultimodalConfig)
    screenshot: ScreenshotConfig = Field(default_factory=ScreenshotConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    file_store: FileStoreConfig = Field(default_factory=FileStoreConfig)

    @classmethod
    def from_yaml(cls, filepath: str = "config.yaml") -> "AppConfig":
        import yaml
        if not os.path.exists(filepath):
            return cls()
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)

    @classmethod
    def load(cls, filepath: Optional[str] = None) -> "AppConfig":
        config = cls.from_yaml(filepath) if filepath else cls.from_yaml()
        config.multimodal.base_url = os.getenv(
            "MULTIMODAL_BASE_URL", config.multimodal.base_url
        )
        config.multimodal.api_key = os.getenv(
            "MULTIMODAL_API_KEY", config.multimodal.api_key
        )
        config.multimodal.vl_model = os.getenv(
            "MULTIMODAL_VL_MODEL", config.multimodal.vl_model
        )
        config.multimodal.image_model = os.getenv(
            "MULTIMODAL_IMAGE_MODEL", config.multimodal.image_model
        )
        config.multimodal.asr_model = os.getenv(
            "MULTIMODAL_ASR_MODEL", config.multimodal.asr_model
        )
        config.server.host = os.getenv("MCP_SERVER_HOST", config.server.host)
        config.server.port = int(os.getenv("MCP_SERVER_PORT", config.server.port))
        return config
