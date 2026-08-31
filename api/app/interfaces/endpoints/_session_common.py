#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : _session_common.py
会话路由共享模块(F2-1路由拆分)

提供所有 /sessions 子路由共享的:
- _validate_session_ownership: 会话归属校验公共函数
- SSE推送/心跳间隔常量

避免在5个路由文件中重复定义,保证策略唯一来源。
"""
from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.application.services.session_service import SessionService

# 会话列表SSE推送间隔(秒)
SESSION_SLEEP_INTERVAL = 5
# SSE心跳保活间隔(秒)
SSE_HEARTBEAT_INTERVAL = 15


async def _validate_session_ownership(
        session_service: SessionService, session_id: str, user_id: str,
) -> None:
    """校验会话是否属于当前用户, 不属于则抛出403

    Args:
        session_service: 会话服务
        session_id: 待校验的会话ID
        user_id: 当前登录用户ID

    Raises:
        NotFoundError: 会话不存在
        BadRequestError: 会话不属于当前用户(无权访问)
    """
    session = await session_service.get_session(session_id)
    if not session:
        raise NotFoundError("该会话不存在，请核实后重试")
    if session.user_id and session.user_id != user_id:
        raise BadRequestError("无权访问该会话")
