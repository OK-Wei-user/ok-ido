#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : security.py
JWT令牌生成/验证 + 密码哈希/校验 + Token黑名单 + 用户身份校验
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from core.config import get_settings
from app.infrastructure.storage.redis import get_redis

logger = logging.getLogger(__name__)

security = HTTPBearer()

_ACCESS_TOKEN_TYPE = "access"
_REFRESH_TOKEN_TYPE = "refresh"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验密码"""
    plain_bytes = plain_password.encode("utf-8")[:72]
    hashed_bytes = hashed_password.encode("utf-8")
    return bcrypt.checkpw(plain_bytes, hashed_bytes)


def get_password_hash(password: str) -> str:
    """生成密码哈希"""
    password_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode("utf-8")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建JWT访问令牌(短期, 默认2小时)"""
    settings = get_settings()
    to_encode = data.copy()
    to_encode["type"] = _ACCESS_TOKEN_TYPE
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def create_refresh_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建JWT刷新令牌(默认8小时)"""
    settings = get_settings()
    to_encode = data.copy()
    to_encode["type"] = _REFRESH_TOKEN_TYPE
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=settings.refresh_token_expire_hours))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def create_token_pair(user_id: str) -> Tuple[str, str]:
    """创建访问令牌+刷新令牌对"""
    token_data = {"sub": user_id}
    access_token = create_access_token(data=token_data)
    refresh_token = create_refresh_token(data=token_data)
    return access_token, refresh_token


def decode_token(token: str) -> dict:
    """解码JWT令牌, 返回payload字典, 失败抛出HTTPException"""
    settings = get_settings()
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无法验证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        return payload
    except JWTError:
        raise credentials_exception


def _validate_token_type(token: str, expected_type: str) -> str:
    """校验令牌类型并返回用户ID, 类型不匹配或令牌无效则抛出401"""
    payload = decode_token(token)
    token_type = payload.get("type")
    if token_type != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"令牌类型错误, 需要{expected_type}令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id: Optional[str] = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌中缺少用户标识",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """从JWT访问令牌中获取当前用户ID(FastAPI依赖注入), 同时校验黑名单"""
    user_id = _validate_token_type(credentials.credentials, _ACCESS_TOKEN_TYPE)
    # 检查令牌是否已被登出加入黑名单
    blacklist = _get_token_blacklist()
    if blacklist and await blacklist.is_blacklisted(credentials.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌已失效, 请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id


def _get_token_blacklist() -> Optional["TokenBlacklist"]:
    """获取Token黑名单实例(用于认证校验), Redis不可用时返回None(降级fail-open并记录告警)"""
    try:
        redis_client = get_redis()
        return TokenBlacklist(redis_client=redis_client.client)
    except Exception as e:
        # Redis不可用时降级fail-open: 已登出令牌仍可使用直到自然过期。
        # 记录告警以便运维感知Redis故障,避免静默跳过黑名单校验。
        logger.warning(f"Redis不可用,Token黑名单校验被跳过(fail-open): {e}")
        return None


async def get_refresh_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """从JWT刷新令牌中获取用户ID(FastAPI依赖注入)"""
    return _validate_token_type(credentials.credentials, _REFRESH_TOKEN_TYPE)


class TokenBlacklist:
    """基于Redis的Token黑名单, 用于登出时使令牌失效"""

    KEY_PREFIX = "token_blacklist:"

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    async def add(self, token: str, expire_seconds: int) -> None:
        """将令牌加入黑名单, expire_seconds为令牌剩余有效时间"""
        key = f"{self.KEY_PREFIX}{token}"
        await self._redis.set(key, "1", ex=expire_seconds)

    async def is_blacklisted(self, token: str) -> bool:
        """检查令牌是否在黑名单中"""
        key = f"{self.KEY_PREFIX}{token}"
        return await self._redis.exists(key) > 0

    @staticmethod
    def get_remaining_seconds(payload: dict) -> int:
        """从JWT payload中计算令牌剩余有效秒数"""
        exp = payload.get("exp", 0)
        remaining = exp - int(datetime.now(timezone.utc).timestamp())
        return max(remaining, 0)
