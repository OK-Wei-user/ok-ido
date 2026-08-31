#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/5/14 10:44

@File    : config.py
"""
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """I-DO后端中控配置信息，从.env或者环境变量中加载数据"""

    # 项目基础配置
    env: str = "development"
    log_level: str = "INFO"
    app_config_filepath: str = "config.yaml"

    # 数据库相关配置
    sqlalchemy_database_uri: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/manus"

    # Redis缓存配置
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None

    # OSS对象存储配置
    oss_base_url: str = ""
    oss_bucket: str = ""

    # JWT认证配置
    secret_key: str = "IDO-secret-key-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 2  # 2小时
    refresh_token_expire_hours: int = 8  # 8小时

    # Sandbox配置
    sandbox_address: Optional[str] = None
    sandbox_image: Optional[str] = None
    sandbox_name_prefix: Optional[str] = None
    # 容器内supervisord服务超时(分钟): 传给沙箱容器作为SERVICE_TIMEOUT_MINUTES环境变量
    sandbox_ttl_minutes: Optional[int] = 60
    # 沙箱空闲销毁TTL(秒): 会话结束后沙箱保留时长,超时自动销毁释放资源。
    # TTL内续接会话可复用沙箱(_cancel_sandbox_ttl取消延迟销毁任务)。
    # 与sandbox_ttl_minutes区分:后者为容器内服务超时(分钟),本项为中控侧空闲销毁(秒)。
    # 默认2小时(7200秒),可通过.env的SANDBOX_IDLE_TTL_SECONDS覆盖。
    sandbox_idle_ttl_seconds: int = 7200
    sandbox_network: Optional[str] = None
    sandbox_chrome_args: Optional[str] = ""
    sandbox_https_proxy: Optional[str] = None
    sandbox_http_proxy: Optional[str] = None
    sandbox_no_proxy: Optional[str] = None

    # 使用pydantic v2的写法来完成环境变量信息的告知
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    """获取当前I-DO项目的配置信息，并对内容进行缓存，避免重复读取"""
    settings = Settings()
    return settings
