#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : session_file_routes.py
会话文件路由 - 文件列表与文件内容读取

F2-1路由拆分: 从session_routes.py拆出,聚焦会话沙箱文件读取职责。
- GET  /sessions/{session_id}/files: 获取会话文件列表(交付文件优先排序)
- POST /sessions/{session_id}/file:   读取沙箱中指定文件内容
"""
import logging

from fastapi import APIRouter, Depends

from app.application.services.session_service import SessionService
from app.core.security import get_current_user_id
from app.interfaces.schemas import Response
from app.interfaces.schemas.session import (
    GetSessionFilesResponse,
    FileReadResponse,
    FileReadRequest,
)
from app.interfaces.service_dependencies import get_session_service

from ._session_common import _validate_session_ownership

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["会话模块"])


@router.get(
    path="/{session_id}/files",
    response_model=Response[GetSessionFilesResponse],
    summary="获取指定任务会话文件列表信息",
    description="获取指定任务会话文件列表信息",
)
async def get_session_files(
        session_id: str,
        current_user_id: str = Depends(get_current_user_id),
        session_service: SessionService = Depends(get_session_service),
) -> Response[GetSessionFilesResponse]:
    await _validate_session_ownership(session_service, session_id, current_user_id)
    files = await session_service.get_session_files(session_id)
    return Response.success(
        msg="获取会话文件列表成功",
        data=GetSessionFilesResponse(files=files)
    )


@router.post(
    path="/{session_id}/file",
    response_model=Response[FileReadResponse],
    summary="查看会话沙箱中指定文件的内容",
    description="根据传递的会话id+文件路径查看沙箱中文件的内容信息"
)
async def read_file(
        session_id: str,
        request: FileReadRequest,
        current_user_id: str = Depends(get_current_user_id),
        session_service: SessionService = Depends(get_session_service),
) -> Response[FileReadResponse]:
    await _validate_session_ownership(session_service, session_id, current_user_id)
    result = await session_service.read_file(session_id, request.filepath)
    return Response.success(
        msg="获取会话文件内容成功",
        data=result
    )
