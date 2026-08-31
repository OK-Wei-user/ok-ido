#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : user.py
用户领域模型
"""
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class UserRole(str, Enum):
    """用户角色枚举"""
    ADMIN = "admin"
    USER = "user"


class User(BaseModel):
    """用户领域模型"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    username: str = Field(...)
    phone: str = Field(default="")
    hashed_password: str = Field(...)
    role: UserRole = Field(default=UserRole.USER)
    is_active: bool = Field(default=True)
    updated_at: Optional[datetime] = Field(default=None)
    created_at: Optional[datetime] = Field(default_factory=datetime.now)

    model_config = {"from_attributes": True}
