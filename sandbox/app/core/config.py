#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/10 17:21

@File    : config.py
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """沙箱API服务基础配置信息"""
    log_level: str = "INFO"
    server_timeout_minutes: int = 60
    shell_default_timeout: int = 300
    shell_max_timeout: int = 600

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
