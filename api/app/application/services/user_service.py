#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : user_service.py
用户服务 - 注册/登录/查询/刷新令牌/修改密码
"""
import logging
from typing import Optional, Tuple, Callable

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.core.security import (
    verify_password, get_password_hash,
    create_token_pair, decode_token,
    TokenBlacklist,
)
from app.domain.models.user import User, UserRole
from app.domain.repositories.uow import IUnitOfWork

logger = logging.getLogger(__name__)


class UserService:
    """用户服务"""

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],
            token_blacklist: TokenBlacklist,
    ) -> None:
        """构造函数,完成用户服务初始化

        Args:
            uow_factory: 工作单元工厂(用于创建独立的UoW实例)
            token_blacklist: 令牌黑名单(用于登出令牌失效管理)
        """
        self._uow_factory = uow_factory
        self._token_blacklist = token_blacklist

    async def register(self, username: str, phone: str, password: str) -> User:
        """用户注册"""
        async with self._uow_factory() as uow:
            existing = await uow.user.get_by_username(username)
            if existing:
                raise BadRequestError(f"用户名[{username}]已存在")

            existing_phone = await uow.user.get_by_phone(phone)
            if existing_phone:
                raise BadRequestError(f"手机号[{phone}]已注册")

            user = User(
                username=username,
                phone=phone,
                hashed_password=get_password_hash(password),
                role=UserRole.USER,
            )
            await uow.user.save(user)
        logger.info(f"用户注册成功: {username}")
        return user

    async def login(self, username: str, password: str) -> Tuple[User, str, str]:
        """用户登录, 返回(用户, 访问令牌, 刷新令牌)"""
        async with self._uow_factory() as uow:
            user = await uow.user.get_by_username(username)
        if not user:
            raise BadRequestError("用户名或密码错误")
        if not verify_password(password, user.hashed_password):
            raise BadRequestError("用户名或密码错误")
        if not user.is_active:
            raise BadRequestError("用户已被禁用")

        access_token, refresh_token = create_token_pair(user.id)
        logger.info(f"用户登录成功: {username}")
        return user, access_token, refresh_token

    async def refresh_tokens(self, refresh_token: str) -> Tuple[str, str]:
        """刷新令牌对, 返回(新访问令牌, 新刷新令牌)"""
        if await self._token_blacklist.is_blacklisted(refresh_token):
            raise BadRequestError("刷新令牌已失效, 请重新登录")

        payload = decode_token(refresh_token)
        user_id = payload.get("sub")
        if not user_id:
            raise BadRequestError("刷新令牌无效")

        async with self._uow_factory() as uow:
            user = await uow.user.get_by_id(user_id)
        if not user:
            raise NotFoundError("用户不存在")
        if not user.is_active:
            raise BadRequestError("用户已被禁用")

        remaining = TokenBlacklist.get_remaining_seconds(payload)
        await self._token_blacklist.add(refresh_token, remaining)

        new_access, new_refresh = create_token_pair(user.id)
        logger.info(f"用户刷新令牌成功: {user.username}")
        return new_access, new_refresh

    async def logout(self, access_token: str, refresh_token: Optional[str] = None) -> None:
        """用户登出, 将当前令牌加入黑名单"""
        access_payload = decode_token(access_token)
        remaining = TokenBlacklist.get_remaining_seconds(access_payload)
        if remaining > 0:
            await self._token_blacklist.add(access_token, remaining)

        if refresh_token:
            try:
                refresh_payload = decode_token(refresh_token)
                refresh_remaining = TokenBlacklist.get_remaining_seconds(refresh_payload)
                if refresh_remaining > 0:
                    await self._token_blacklist.add(refresh_token, refresh_remaining)
            except Exception as e:
                # F4-2: refresh_token可能已过期或格式错误,登出仍应成功(仅记录调试日志)
                logger.debug(f"登出时处理refresh_token失败(忽略,不阻断登出): {e}")

        logger.info("用户登出成功")

    async def change_password(self, user_id: str, old_password: str, new_password: str) -> None:
        """修改密码"""
        async with self._uow_factory() as uow:
            user = await uow.user.get_by_id(user_id)
            if not user:
                raise NotFoundError("用户不存在")
            if not verify_password(old_password, user.hashed_password):
                raise BadRequestError("原密码错误")

            user.hashed_password = get_password_hash(new_password)
            await uow.user.save(user)
        logger.info(f"用户修改密码成功: {user.username}")

    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """根据用户ID查询用户"""
        async with self._uow_factory() as uow:
            return await uow.user.get_by_id(user_id)
