#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : auth_routes.py
认证路由 - 注册/登录/登出/刷新令牌/修改密码/获取当前用户
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header

from app.application.services.user_service import UserService
from app.core.security import get_current_user_id
from app.interfaces.schemas import Response
from app.interfaces.schemas.auth import (
    RegisterRequest, LoginRequest, LoginResponse,
    RefreshTokenRequest, RefreshTokenResponse,
    ChangePasswordRequest, UserResponse,
)
from app.interfaces.service_dependencies import get_user_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["认证模块"])


@router.post(
    path="/register",
    response_model=Response[UserResponse],
    summary="用户注册",
    description="注册新用户账号",
)
async def register(
    request: RegisterRequest,
    user_service: UserService = Depends(get_user_service),
) -> Response[UserResponse]:
    user = await user_service.register(request.username, request.phone, request.password)
    return Response.success(
        msg="注册成功",
        data=UserResponse(
            user_id=user.id, username=user.username, phone=user.phone,
            role=user.role.value, is_active=user.is_active,
            created_at=user.created_at.isoformat() if user.created_at else None,
        ),
    )


@router.post(
    path="/login",
    response_model=Response[LoginResponse],
    summary="用户登录",
    description="用户登录获取访问令牌和刷新令牌",
)
async def login(
    request: LoginRequest,
    user_service: UserService = Depends(get_user_service),
) -> Response[LoginResponse]:
    user, access_token, refresh_token = await user_service.login(
        request.username, request.password,
    )
    return Response.success(
        msg="登录成功",
        data=LoginResponse(
            access_token=access_token, refresh_token=refresh_token,
            user_id=user.id, username=user.username, role=user.role.value,
        ),
    )


@router.post(
    path="/logout",
    response_model=Response[Optional[dict]],
    summary="用户登出",
    description="将当前访问令牌和刷新令牌加入黑名单使其失效",
)
async def logout(
    current_user_id: str = Depends(get_current_user_id),
    authorization: str = Header(default="", description="Bearer access_token"),
    x_refresh_token: str = Header(default="", description="刷新令牌"),
    user_service: UserService = Depends(get_user_service),
) -> Response[Optional[dict]]:
    access_token = authorization.removeprefix("Bearer ").strip()
    refresh_token = x_refresh_token.strip() or None
    await user_service.logout(access_token, refresh_token)
    return Response.success(msg="登出成功")


@router.post(
    path="/refresh",
    response_model=Response[RefreshTokenResponse],
    summary="刷新令牌",
    description="使用刷新令牌获取新的访问令牌和刷新令牌",
)
async def refresh_token(
    request: RefreshTokenRequest,
    user_service: UserService = Depends(get_user_service),
) -> Response[RefreshTokenResponse]:
    new_access, new_refresh = await user_service.refresh_tokens(request.refresh_token)
    return Response.success(
        msg="刷新令牌成功",
        data=RefreshTokenResponse(
            access_token=new_access, refresh_token=new_refresh,
        ),
    )


@router.post(
    path="/change-password",
    response_model=Response[Optional[dict]],
    summary="修改密码",
    description="修改当前用户密码, 修改后需重新登录",
)
async def change_password(
    request: ChangePasswordRequest,
    current_user_id: str = Depends(get_current_user_id),
    user_service: UserService = Depends(get_user_service),
) -> Response[Optional[dict]]:
    await user_service.change_password(
        current_user_id, request.old_password, request.new_password,
    )
    return Response.success(msg="修改密码成功, 请重新登录")


@router.get(
    path="/me",
    response_model=Response[UserResponse],
    summary="获取当前用户信息",
    description="根据JWT令牌获取当前登录用户信息",
)
async def get_current_user_info(
    current_user_id: str = Depends(get_current_user_id),
    user_service: UserService = Depends(get_user_service),
) -> Response[UserResponse]:
    user = await user_service.get_user_by_id(current_user_id)
    if not user:
        return Response.fail(code=404, msg="用户不存在")
    return Response.success(
        msg="获取用户信息成功",
        data=UserResponse(
            user_id=user.id, username=user.username, phone=user.phone,
            role=user.role.value, is_active=user.is_active,
            created_at=user.created_at.isoformat() if user.created_at else None,
        ),
    )
