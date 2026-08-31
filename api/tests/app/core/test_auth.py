#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_auth.py
JWT认证单元测试 - 密码哈希/令牌生成/令牌校验/黑名单/刷新令牌/手机号
"""
import pytest
from datetime import timedelta
from unittest.mock import AsyncMock

from app.core.security import (
    verify_password, get_password_hash,
    create_access_token, create_refresh_token,
    create_token_pair, decode_token,
    _validate_token_type, TokenBlacklist,
    _ACCESS_TOKEN_TYPE, _REFRESH_TOKEN_TYPE,
)
from app.domain.models.user import User, UserRole
from app.application.services.user_service import UserService
from app.application.errors.exceptions import BadRequestError, NotFoundError
from core.config import get_settings


class TestPasswordHashing:
    """密码哈希与校验测试"""

    def test_hash_and_verify(self):
        hashed = get_password_hash("test123456")
        assert verify_password("test123456", hashed)

    def test_wrong_password_fails(self):
        hashed = get_password_hash("correct")
        assert not verify_password("wrong", hashed)

    def test_different_hashes_for_same_password(self):
        h1 = get_password_hash("same123")
        h2 = get_password_hash("same123")
        assert h1 != h2
        assert verify_password("same123", h1)
        assert verify_password("same123", h2)

    def test_long_password_truncated(self):
        long_password = "a" * 100
        hashed = get_password_hash(long_password)
        assert verify_password(long_password, hashed)


class TestAccessToken:
    """访问令牌测试"""

    def test_create_and_decode_token(self):
        token = create_access_token(data={"sub": "user123"})
        payload = decode_token(token)
        assert payload["sub"] == "user123"
        assert payload["type"] == _ACCESS_TOKEN_TYPE

    def test_token_has_expiry(self):
        token = create_access_token(data={"sub": "user456"})
        payload = decode_token(token)
        assert "exp" in payload

    def test_expired_token_fails(self):
        token = create_access_token(data={"sub": "expired"}, expires_delta=timedelta(seconds=-1))
        with pytest.raises(Exception):
            decode_token(token)

    def test_validate_access_token_type(self):
        token = create_access_token(data={"sub": "user789"})
        user_id = _validate_token_type(token, _ACCESS_TOKEN_TYPE)
        assert user_id == "user789"

    def test_wrong_token_type_rejected(self):
        from fastapi import HTTPException
        token = create_access_token(data={"sub": "user789"})
        with pytest.raises(HTTPException) as exc_info:
            _validate_token_type(token, _REFRESH_TOKEN_TYPE)
        assert exc_info.value.status_code == 401


class TestRefreshToken:
    """刷新令牌测试"""

    def test_create_refresh_token(self):
        token = create_refresh_token(data={"sub": "user123"})
        payload = decode_token(token)
        assert payload["sub"] == "user123"
        assert payload["type"] == _REFRESH_TOKEN_TYPE

    def test_validate_refresh_token_type(self):
        token = create_refresh_token(data={"sub": "user456"})
        user_id = _validate_token_type(token, _REFRESH_TOKEN_TYPE)
        assert user_id == "user456"

    def test_refresh_token_rejected_as_access(self):
        from fastapi import HTTPException
        token = create_refresh_token(data={"sub": "user789"})
        with pytest.raises(HTTPException) as exc_info:
            _validate_token_type(token, _ACCESS_TOKEN_TYPE)
        assert exc_info.value.status_code == 401


class TestTokenPair:
    """令牌对测试"""

    def test_create_token_pair(self):
        access, refresh = create_token_pair("user123")
        access_payload = decode_token(access)
        refresh_payload = decode_token(refresh)
        assert access_payload["sub"] == "user123"
        assert access_payload["type"] == _ACCESS_TOKEN_TYPE
        assert refresh_payload["sub"] == "user123"
        assert refresh_payload["type"] == _REFRESH_TOKEN_TYPE

    def test_access_expires_before_refresh(self):
        access, refresh = create_token_pair("user123")
        access_payload = decode_token(access)
        refresh_payload = decode_token(refresh)
        assert access_payload["exp"] < refresh_payload["exp"]


class TestTokenBlacklist:
    """令牌黑名单测试"""

    def test_add_and_check_blacklist(self):
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock()
        mock_redis.exists = AsyncMock(return_value=1)
        blacklist = TokenBlacklist(redis_client=mock_redis)

        import asyncio
        asyncio.run(blacklist.add("test_token", 3600))
        mock_redis.set.assert_called_once()
        result = asyncio.run(blacklist.is_blacklisted("test_token"))
        assert result is True

    def test_not_blacklisted(self):
        mock_redis = AsyncMock()
        mock_redis.exists = AsyncMock(return_value=0)
        blacklist = TokenBlacklist(redis_client=mock_redis)

        import asyncio
        result = asyncio.run(blacklist.is_blacklisted("clean_token"))
        assert result is False

    def test_get_remaining_seconds(self):
        token = create_access_token(data={"sub": "user123"})
        payload = decode_token(token)
        remaining = TokenBlacklist.get_remaining_seconds(payload)
        assert remaining > 0

    def test_get_remaining_seconds_expired(self):
        remaining = TokenBlacklist.get_remaining_seconds({"exp": 0})
        assert remaining == 0


class TestUserModel:
    """用户领域模型测试"""

    def test_create_user(self):
        user = User(username="testuser", phone="13800138000", hashed_password="hashed")
        assert user.username == "testuser"
        assert user.phone == "13800138000"
        assert user.role == UserRole.USER
        assert user.is_active is True
        assert user.id is not None

    def test_create_user_default_phone(self):
        user = User(username="testuser", hashed_password="hashed")
        assert user.phone == ""

    def test_user_role_enum(self):
        assert UserRole.ADMIN.value == "admin"
        assert UserRole.USER.value == "user"

    def test_user_serialization(self):
        user = User(username="testuser", phone="13800138000", hashed_password="hashed")
        data = user.model_dump(mode="json")
        restored = User(**data)
        assert restored.username == user.username
        assert restored.phone == user.phone
        assert restored.id == user.id


class TestUserService:
    """用户服务测试"""

    def _make_service(self) -> tuple:
        mock_uow = AsyncMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=None)
        mock_uow.user = AsyncMock()

        def uow_factory():
            return mock_uow

        mock_blacklist = AsyncMock()
        mock_blacklist.is_blacklisted = AsyncMock(return_value=False)
        mock_blacklist.add = AsyncMock()

        return UserService(uow_factory=uow_factory, token_blacklist=mock_blacklist), mock_uow

    @pytest.mark.asyncio
    async def test_register_success(self):
        service, mock_uow = self._make_service()
        mock_uow.user.get_by_username = AsyncMock(return_value=None)
        mock_uow.user.get_by_phone = AsyncMock(return_value=None)
        mock_uow.user.save = AsyncMock()

        user = await service.register("newuser", "13800138000", "password123")
        assert user.username == "newuser"
        assert user.phone == "13800138000"
        assert user.role == UserRole.USER

    @pytest.mark.asyncio
    async def test_register_duplicate_username(self):
        service, mock_uow = self._make_service()
        existing_user = User(username="existing", phone="13900139000", hashed_password="hashed")
        mock_uow.user.get_by_username = AsyncMock(return_value=existing_user)

        with pytest.raises(BadRequestError):
            await service.register("existing", "13800138000", "password123")

    @pytest.mark.asyncio
    async def test_register_duplicate_phone(self):
        service, mock_uow = self._make_service()
        mock_uow.user.get_by_username = AsyncMock(return_value=None)
        existing_user = User(username="other", phone="13800138000", hashed_password="hashed")
        mock_uow.user.get_by_phone = AsyncMock(return_value=existing_user)

        with pytest.raises(BadRequestError):
            await service.register("newuser", "13800138000", "password123")

    @pytest.mark.asyncio
    async def test_login_success(self):
        service, mock_uow = self._make_service()
        hashed = get_password_hash("password123")
        user = User(username="testuser", phone="13800138000", hashed_password=hashed)
        mock_uow.user.get_by_username = AsyncMock(return_value=user)

        result_user, access_token, refresh_token = await service.login("testuser", "password123")
        assert result_user.username == "testuser"
        assert access_token is not None
        assert refresh_token is not None

    @pytest.mark.asyncio
    async def test_login_wrong_password(self):
        service, mock_uow = self._make_service()
        hashed = get_password_hash("correct")
        user = User(username="testuser", hashed_password=hashed)
        mock_uow.user.get_by_username = AsyncMock(return_value=user)

        with pytest.raises(BadRequestError):
            await service.login("testuser", "wrongpassword")

    @pytest.mark.asyncio
    async def test_login_user_not_found(self):
        service, mock_uow = self._make_service()
        mock_uow.user.get_by_username = AsyncMock(return_value=None)

        with pytest.raises(BadRequestError):
            await service.login("nonexistent", "password123")

    @pytest.mark.asyncio
    async def test_login_inactive_user(self):
        service, mock_uow = self._make_service()
        hashed = get_password_hash("password123")
        user = User(username="testuser", hashed_password=hashed, is_active=False)
        mock_uow.user.get_by_username = AsyncMock(return_value=user)

        with pytest.raises(BadRequestError):
            await service.login("testuser", "password123")

    @pytest.mark.asyncio
    async def test_change_password_success(self):
        service, mock_uow = self._make_service()
        hashed = get_password_hash("oldpassword")
        user = User(id="user123", username="testuser", hashed_password=hashed)
        mock_uow.user.get_by_id = AsyncMock(return_value=user)
        mock_uow.user.save = AsyncMock()

        await service.change_password("user123", "oldpassword", "newpassword123")

    @pytest.mark.asyncio
    async def test_change_password_wrong_old(self):
        service, mock_uow = self._make_service()
        hashed = get_password_hash("oldpassword")
        user = User(id="user123", username="testuser", hashed_password=hashed)
        mock_uow.user.get_by_id = AsyncMock(return_value=user)

        with pytest.raises(BadRequestError):
            await service.change_password("user123", "wrongold", "newpassword123")

    @pytest.mark.asyncio
    async def test_logout_blacklists_tokens(self):
        service, mock_uow = self._make_service()
        access_token = create_access_token(data={"sub": "user123"})
        refresh_token = create_refresh_token(data={"sub": "user123"})

        await service.logout(access_token, refresh_token)
        service._token_blacklist.add.assert_called()

    @pytest.mark.asyncio
    async def test_refresh_tokens_success(self):
        service, mock_uow = self._make_service()
        user = User(id="user123", username="testuser", hashed_password="hashed", is_active=True)
        mock_uow.user.get_by_id = AsyncMock(return_value=user)

        refresh_token = create_refresh_token(data={"sub": "user123"})
        new_access, new_refresh = await service.refresh_tokens(refresh_token)
        assert new_access is not None
        assert new_refresh is not None

    @pytest.mark.asyncio
    async def test_refresh_blacklisted_token(self):
        service, mock_uow = self._make_service()
        service._token_blacklist.is_blacklisted = AsyncMock(return_value=True)

        refresh_token = create_refresh_token(data={"sub": "user123"})
        with pytest.raises(BadRequestError):
            await service.refresh_tokens(refresh_token)
