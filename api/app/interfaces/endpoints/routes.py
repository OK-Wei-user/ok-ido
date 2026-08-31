#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/5/17 10:52

@File    : routes.py
"""
from fastapi import APIRouter

from . import (
    status_routes,
    app_config_routes,
    file_routes,
    session_routes,
    chat_routes,
    session_file_routes,
    session_shell_routes,
    session_vnc_routes,
    auth_routes,
    sandbox_callback_routes,
)


def create_api_routes() -> APIRouter:
    """创建API路由，涵盖整个项目的所有路由管理"""
    # 1.创建APIRouter实例
    api_router = APIRouter()

    # 2.将各个模块添加到api_router中
    api_router.include_router(auth_routes.router)
    api_router.include_router(status_routes.router)
    api_router.include_router(app_config_routes.router)
    api_router.include_router(file_routes.router)
    # 会话模块(F2-1路由拆分): 原397行session_routes.py按职责拆分为5个子路由,
    # 共享prefix="/sessions"与tags=["会话模块"],路径与原路由完全一致
    api_router.include_router(session_routes.router)        # 会话CRUD与列表SSE
    api_router.include_router(chat_routes.router)           # 聊天SSE流
    api_router.include_router(session_file_routes.router)   # 会话文件读取
    api_router.include_router(session_shell_routes.router)  # Shell输出读取
    api_router.include_router(session_vnc_routes.router)    # VNC WebSocket代理
    # Batch 40 / 方向1: 沙箱异步任务完成回调(内部端点, Docker 网络隔离)
    api_router.include_router(sandbox_callback_routes.router)

    # 3.返回api路由实例
    return api_router


router = create_api_routes()
