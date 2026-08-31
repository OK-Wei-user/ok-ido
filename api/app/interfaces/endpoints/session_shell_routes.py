#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : session_shell_routes.py
会话Shell路由 - Shell输出读取

F2-1路由拆分: 从session_routes.py拆出,聚焦会话Shell输出读取职责。
- POST /sessions/{session_id}/shell: 读取会话Shell输出内容
"""
import logging

from fastapi import APIRouter, Depends

from app.application.services.session_service import SessionService
from app.core.security import get_current_user_id
from app.interfaces.schemas import Response
from app.interfaces.schemas.session import ShellReadResponse, ShellReadRequest
from app.interfaces.service_dependencies import get_session_service

from ._session_common import _validate_session_ownership

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["会话模块"])


@router.post(
    path="/{session_id}/shell",
    response_model=Response[ShellReadResponse],
    summary="查看会话的shell内容输出",
    description="传递指定会话id与shell会话标识，查看shell内容输出",
)
async def read_shell_output(
        session_id: str,
        request: ShellReadRequest,
        current_user_id: str = Depends(get_current_user_id),
        session_service: SessionService = Depends(get_session_service),
) -> Response[ShellReadResponse]:
    await _validate_session_ownership(session_service, session_id, current_user_id)
    result = await session_service.read_shell_output(session_id, request.session_id)
    return Response.success(
        msg="获取Shell内容输出结果成功",
        data=result,
    )
