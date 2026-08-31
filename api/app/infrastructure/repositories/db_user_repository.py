#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : db_user_repository.py
用户仓库PostgreSQL实现
"""
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models.user import User
from app.infrastructure.models.user import UserModel

logger = logging.getLogger(__name__)


class DBUserRepository:
    """基于PostgreSQL的用户仓库实现"""

    def __init__(self, db_session: AsyncSession) -> None:
        self._session = db_session

    async def save(self, user: User) -> None:
        stmt = select(UserModel).where(UserModel.id == user.id)
        result = await self._session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            existing.username = user.username
            existing.phone = user.phone
            existing.hashed_password = user.hashed_password
            existing.role = user.role.value if not isinstance(user.role, str) else user.role
            existing.is_active = user.is_active
            existing.updated_at = user.updated_at
        else:
            self._session.add(UserModel.from_domain(user))
        await self._session.flush()

    async def get_by_id(self, user_id: str) -> Optional[User]:
        stmt = select(UserModel).where(UserModel.id == user_id)
        result = await self._session.execute(stmt)
        user_model = result.scalar_one_or_none()
        return user_model.to_domain() if user_model else None

    async def get_by_username(self, username: str) -> Optional[User]:
        stmt = select(UserModel).where(UserModel.username == username)
        result = await self._session.execute(stmt)
        user_model = result.scalar_one_or_none()
        return user_model.to_domain() if user_model else None

    async def get_by_phone(self, phone: str) -> Optional[User]:
        stmt = select(UserModel).where(UserModel.phone == phone)
        result = await self._session.execute(stmt)
        user_model = result.scalar_one_or_none()
        return user_model.to_domain() if user_model else None

    async def delete_by_id(self, user_id: str) -> None:
        stmt = select(UserModel).where(UserModel.id == user_id)
        result = await self._session.execute(stmt)
        user_model = result.scalar_one_or_none()
        if user_model:
            await self._session.delete(user_model)
            await self._session.flush()
