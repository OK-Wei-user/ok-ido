#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : user.py
用户ORM模型
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, Boolean, DateTime, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

if TYPE_CHECKING:
    # 仅类型检查期导入，避免运行时循环依赖与未使用导入告警
    from app.domain.models.user import User


class UserModel(Base):
    """用户ORM模型"""
    __tablename__ = "users"
    __table_args__ = (
        {"comment": "用户表"},
    )

    id: Mapped[str] = mapped_column(
        String(255), nullable=False, primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    username: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True,
    )
    phone: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("''"), index=True,
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=text("'user'"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        onupdate=datetime.now,
        server_default=text("CURRENT_TIMESTAMP(0)"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        server_default=text("CURRENT_TIMESTAMP(0)"),
    )

    @classmethod
    def from_domain(cls, user: "User") -> "UserModel":
        return cls(
            id=user.id,
            username=user.username,
            phone=user.phone,
            hashed_password=user.hashed_password,
            role=user.role.value if isinstance(user.role, str) is False else user.role,
            is_active=user.is_active,
            updated_at=user.updated_at,
            created_at=user.created_at,
        )

    def to_domain(self) -> "User":
        from app.domain.models.user import User, UserRole
        return User(
            id=self.id,
            username=self.username,
            phone=self.phone,
            hashed_password=self.hashed_password,
            role=UserRole(self.role),
            is_active=self.is_active,
            updated_at=self.updated_at,
            created_at=self.created_at,
        )
