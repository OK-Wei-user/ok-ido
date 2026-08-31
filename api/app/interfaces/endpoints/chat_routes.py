#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : chat_routes.py
会话聊天路由 - SSE流式聊天与断连恢复

F2-1路由拆分: 从session_routes.py拆出,职责单一聚焦聊天SSE流。
- POST /sessions/{session_id}/chat: 发起聊天请求,SSE流式返回事件
- 内部支持SSE标准Last-Event-ID header实现断连恢复
"""
import logging
from datetime import datetime
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Request as FastAPIRequest
from sse_starlette import EventSourceResponse, ServerSentEvent

from app.application.services.agent_service import AgentService
from app.application.services.session_service import SessionService
from app.core.security import get_current_user_id
from app.interfaces.schemas.event import EventMapper
from app.interfaces.schemas.session import ChatRequest
from app.interfaces.service_dependencies import get_session_service, get_agent_service

from ._session_common import SSE_HEARTBEAT_INTERVAL, _validate_session_ownership

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["会话模块"])


@router.post(
    path="/{session_id}/chat",
    summary="向指定任务会话发起聊天请求",
    description="向指定任务会话发起聊天请求"
)
async def chat(
        session_id: str,
        request: ChatRequest,
        http_request: FastAPIRequest,
        current_user_id: str = Depends(get_current_user_id),
        session_service: SessionService = Depends(get_session_service),
        agent_service: AgentService = Depends(get_agent_service),
) -> EventSourceResponse:
    await _validate_session_ownership(session_service, session_id, current_user_id)

    # 支持SSE标准Last-Event-ID header(浏览器自动携带) + 请求体event_id(兼容)
    last_event_id = http_request.headers.get("Last-Event-ID") or request.event_id

    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        # 1.断连恢复: 先补发断连期间的事件(从session.events中按ID恢复)
        replay_last_id = last_event_id
        if last_event_id:
            async for event in agent_service.replay_missed_events(session_id, last_event_id):
                sse_event = EventMapper.event_to_sse_event(event)
                if sse_event:
                    yield ServerSentEvent(
                        event=sse_event.event,
                        data=sse_event.data.model_dump_json(),
                        id=event.id,
                    )
                replay_last_id = event.id

        # 2.接入实时流: 用replay最后的ID作为latest_event_id,避免与补发事件重复
        async for event in agent_service.chat(
                session_id=session_id,
                message=request.message,
                attachments=request.attachments,
                latest_event_id=replay_last_id,
                timestamp=datetime.fromtimestamp(request.timestamp) if request.timestamp else None,
        ):
            sse_event = EventMapper.event_to_sse_event(event)
            if sse_event:
                yield ServerSentEvent(
                    event=sse_event.event,
                    data=sse_event.data.model_dump_json(),
                    id=event.id,
                )

    return EventSourceResponse(event_generator(), ping=SSE_HEARTBEAT_INTERVAL)
