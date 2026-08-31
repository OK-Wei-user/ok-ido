#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : session_routes.py
会话路由 - CRUD(创建/列表/详情/删除/停止/未读清除/列表SSE)

F2-1路由拆分: 原397行session_routes.py按职责拆分为5个子路由文件:
- session_routes.py(本文件): 会话CRUD与列表SSE
- chat_routes.py: 聊天SSE流
- session_file_routes.py: 会话文件读取
- session_shell_routes.py: Shell输出读取
- session_vnc_routes.py: VNC WebSocket代理

所有子路由共享prefix="/sessions"与tags=["会话模块"],
通过routes.py统一注册到主APIRouter。
"""
import asyncio
import logging
from typing import Optional, Dict, AsyncGenerator

from fastapi import APIRouter, Depends
from sse_starlette import EventSourceResponse, ServerSentEvent

from app.application.errors.exceptions import NotFoundError
from app.application.services.agent_service import AgentService
from app.application.services.session_service import SessionService
from app.core.security import get_current_user_id
from app.interfaces.schemas import Response
from app.interfaces.schemas.event import EventMapper
from app.interfaces.schemas.session import (
    CreateSessionResponse,
    ListSessionResponse,
    ListSessionItem,
    GetSessionResponse,
)
from app.interfaces.service_dependencies import get_session_service, get_agent_service

from ._session_common import (
    SESSION_SLEEP_INTERVAL,
    SSE_HEARTBEAT_INTERVAL,
    _validate_session_ownership,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["会话模块"])


@router.post(
    path="",
    response_model=Response[CreateSessionResponse],
    summary="创建新任务会话",
    description="创建一个空白的新任务会话",
)
async def create_session(
        current_user_id: str = Depends(get_current_user_id),
        session_service: SessionService = Depends(get_session_service),
) -> Response[CreateSessionResponse]:
    session = await session_service.create_session(user_id=current_user_id)
    return Response.success(
        msg="创建任务会话成功",
        data=CreateSessionResponse(session_id=session.id)
    )


@router.post(
    path="/stream",
    summary="流式获取所有会话基础信息列表",
    description="间隔指定时间流式获取所有会话基础信息列表",
)
async def stream_sessions(
        current_user_id: str = Depends(get_current_user_id),
        session_service: SessionService = Depends(get_session_service),
) -> EventSourceResponse:
    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        last_signature = None  # 基于会话列表内容的签名,用于变更检测

        while True:
            try:
                sessions = await session_service.get_all_sessions(user_id=current_user_id)
            except Exception as e:
                logger.warning(f"会话列表查询失败, {SESSION_SLEEP_INTERVAL}s后重试: {str(e)}")
                await asyncio.sleep(SESSION_SLEEP_INTERVAL)
                continue

            # 构建内容签名: session_id + title + latest_message_at + status + unread_count
            # 任一会话任一字段变化,签名即变化,触发推送
            current_signature = "|".join(
                f"{s.id}:{s.title}:{s.latest_message_at.isoformat() if s.latest_message_at else ''}:{s.status}:{s.unread_message_count}"
                for s in sessions
            )

            # 仅在签名变化时推送(新增/删除/状态变更/标题变更/新消息/未读数变化)
            if current_signature != last_signature:
                last_signature = current_signature
                session_items = [
                    ListSessionItem(
                        session_id=session.id,
                        title=session.title,
                        latest_message=session.latest_message,
                        latest_message_at=session.latest_message_at,
                        status=session.status,
                        unread_message_count=session.unread_message_count,
                    )
                    for session in sessions
                ]
                yield ServerSentEvent(
                    event="sessions",
                    data=ListSessionResponse(sessions=session_items).model_dump_json(),
                )

            await asyncio.sleep(SESSION_SLEEP_INTERVAL)

    return EventSourceResponse(event_generator(), ping=SSE_HEARTBEAT_INTERVAL)


@router.get(
    path="",
    response_model=Response[ListSessionResponse],
    summary="获取会话列表基础信息",
    description="获取当前用户所有任务会话基础信息列表",
)
async def get_all_sessions(
        current_user_id: str = Depends(get_current_user_id),
        session_service: SessionService = Depends(get_session_service),
) -> Response[ListSessionResponse]:
    sessions = await session_service.get_all_sessions(user_id=current_user_id)
    session_items = [
        ListSessionItem(
            session_id=session.id,
            title=session.title,
            latest_message=session.latest_message,
            latest_message_at=session.latest_message_at,
            status=session.status,
            unread_message_count=session.unread_message_count,
        )
        for session in sessions
    ]
    return Response.success(
        msg="获取任务会话列表成功",
        data=ListSessionResponse(sessions=session_items)
    )


@router.post(
    path="/{session_id}/clear-unread-message-count",
    response_model=Response[Optional[Dict]],
    summary="清除指定任务会话未读消息数",
    description="清除指定任务会话未读消息数",
)
async def clear_unread_message_count(
        session_id: str,
        current_user_id: str = Depends(get_current_user_id),
        session_service: SessionService = Depends(get_session_service),
) -> Response[Optional[Dict]]:
    await _validate_session_ownership(session_service, session_id, current_user_id)
    await session_service.clear_unread_message_count(session_id)
    return Response.success(msg="清除未读消息数成功")


@router.post(
    path="/{session_id}/delete",
    response_model=Response[Optional[Dict]],
    summary="删除指定任务会话",
    description="根据传递的会话id删除指定任务会话",
)
async def delete_session(
        session_id: str,
        current_user_id: str = Depends(get_current_user_id),
        session_service: SessionService = Depends(get_session_service),
        agent_service: AgentService = Depends(get_agent_service),
) -> Response[Optional[Dict]]:
    # 1.校验会话归属(失败则直接抛出403/404，无需清理)
    await _validate_session_ownership(session_service, session_id, current_user_id)

    # 2.删除会话(DB操作)，资源清理放入finally确保异常时仍能释放
    try:
        await session_service.delete_session(session_id)
    finally:
        # 无论DB删除是否成功，都需取消沙箱TTL(避免对已删除会话执行延迟销毁)
        # 并移除会话锁(避免锁字典无限增长导致同会话无法再次发起)
        # 沙箱本身的销毁由SessionService.delete_session内级联触发(兜底)
        agent_service.cancel_sandbox_ttl(session_id)
        agent_service.remove_session_lock(session_id)

    return Response.success(msg="删除任务会话成功")


@router.get(
    path="/{session_id}",
    response_model=Response[GetSessionResponse],
    summary="获取指定会话详情信息",
    description="根据传递的会话id获取该会话的对话详情",
)
async def get_session(
        session_id: str,
        current_user_id: str = Depends(get_current_user_id),
        session_service: SessionService = Depends(get_session_service),
) -> Response[GetSessionResponse]:
    await _validate_session_ownership(session_service, session_id, current_user_id)
    session = await session_service.get_session(session_id)
    if not session:
        raise NotFoundError("该会话不存在，请核实后重试")
    return Response.success(
        msg="获取会话详情成功",
        data=GetSessionResponse(
            session_id=session.id,
            title=session.title,
            status=session.status,
            events=EventMapper.events_to_sse_events(session.events),
            sandbox_id=session.sandbox_id,
        )
    )


@router.post(
    path="/{session_id}/stop",
    response_model=Response[Optional[Dict]],
    summary="停止指定任务会话",
    description="根据传递的指定会话id停止对应任务会话",
)
async def stop_session(
        session_id: str,
        current_user_id: str = Depends(get_current_user_id),
        session_service: SessionService = Depends(get_session_service),
        agent_service: AgentService = Depends(get_agent_service),
) -> Response[Optional[Dict]]:
    await _validate_session_ownership(session_service, session_id, current_user_id)
    await agent_service.stop_session(session_id)
    return Response.success(msg="停止任务会话成功")
