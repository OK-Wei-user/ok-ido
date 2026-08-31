#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : auth.py
认证请求/响应Schema
"""
from typing import Optional

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    """用户注册请求"""
    username: str = Field(..., min_length=3, max_length=50, description="用户名")
    phone: str = Field(..., pattern=r"^1[3-9]\d{9}$", description="手机号")
    password: str = Field(..., min_length=6, max_length=50, description="密码")


class LoginRequest(BaseModel):
    """用户登录请求"""
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class LoginResponse(BaseModel):
    """用户登录响应"""
    access_token: str = Field(..., description="访问令牌")
    refresh_token: str = Field(..., description="刷新令牌")
    token_type: str = Field(default="bearer", description="令牌类型")
    user_id: str = Field(..., description="用户ID")
    username: str = Field(..., description="用户名")
    role: str = Field(..., description="用户角色")


class RefreshTokenRequest(BaseModel):
    """刷新令牌请求"""
    refresh_token: str = Field(..., description="刷新令牌")


class RefreshTokenResponse(BaseModel):
    """刷新令牌响应"""
    access_token: str = Field(..., description="新的访问令牌")
    refresh_token: str = Field(..., description="新的刷新令牌")
    token_type: str = Field(default="bearer", description="令牌类型")


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str = Field(..., min_length=6, max_length=50, description="原密码")
    new_password: str = Field(..., min_length=6, max_length=50, description="新密码")


class UserResponse(BaseModel):
    """用户信息响应"""
    user_id: str = Field(..., description="用户ID")
    username: str = Field(..., description="用户名")
    phone: str = Field(default="", description="手机号")
    role: str = Field(..., description="用户角色")
    is_active: bool = Field(..., description="是否激活")
    created_at: Optional[str] = Field(None, description="创建时间")
