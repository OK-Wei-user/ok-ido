#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : session_vnc_routes.py
会话VNC路由 - WebSocket代理

F2-1路由拆分: 从session_routes.py拆出,聚焦VNC WebSocket双向代理职责。
- WS /sessions/{session_id}/vnc: VNC WebSocket代理(双向转发Web<->沙箱VNC)

设计要点:
- 双向转发: 启动两个forward任务(Web->Sandbox + Sandbox->Web),任一完成即关闭
- 协议协商: 支持binary与base64子协议
- 异常隔离: forward任务异常不影响主连接,记录日志后正常关闭
"""
import asyncio
import logging

import websockets
from fastapi import APIRouter, Depends
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets import ConnectionClosed

from app.application.services.session_service import SessionService
from app.infrastructure.storage.vnc_status_tracker import VNCStatusTracker
from app.interfaces.service_dependencies import get_session_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["会话模块"])


@router.websocket(
    path="/{session_id}/vnc",
)
async def vnc_websocket(
        websocket: WebSocket,
        session_id: str,
        session_service: SessionService = Depends(get_session_service),
) -> None:
    # 1.协议协商: 优先binary,次选base64
    protocols_str = websocket.headers.get("sec-websocket-protocol", "")
    protocols = [p.strip() for p in protocols_str.split(",")]

    selected_protocol = None
    if "binary" in protocols:
        selected_protocol = "binary"
    elif "base64" in protocols:
        selected_protocol = "base64"

    logger.info(f"为会话[{session_id}]开启WebSocket连接")
    await websocket.accept(subprotocol=selected_protocol)

    # 混合方案: 标记VNC已连接,AgentTaskRunner据此降低截图频率
    await VNCStatusTracker.set_connected(session_id, True)

    try:
        # 2.建立到沙箱VNC的后端WebSocket连接
        sandbox_vnc_url = await session_service.get_vnc_url(session_id)
        logger.info(f"为会话[{session_id}]建立VNC后端连接: {sandbox_vnc_url}")

        async with websockets.connect(sandbox_vnc_url) as sandbox_ws:
            # 3.启动双向转发任务: Web->Sandbox + Sandbox->Web
            async def forward_to_sandbox():
                """Web客户端 -> 沙箱VNC方向转发"""
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        await sandbox_ws.send(data)
                except WebSocketDisconnect:
                    logger.info(f"会话[{session_id}]Web->VNC前端连接关闭")
                except Exception as forward_e:
                    logger.error(f"会话[{session_id}]Web->VNC转发异常: {forward_e}")

            async def forward_from_sandbox():
                """沙箱VNC -> Web客户端方向转发"""
                try:
                    while True:
                        data = await sandbox_ws.recv()
                        await websocket.send_bytes(data)
                except ConnectionClosed:
                    logger.info(f"会话[{session_id}]VNC->Web后端连接关闭")
                except Exception as forward_e:
                    logger.error(f"会话[{session_id}]VNC->Web转发异常: {forward_e}")

            forward_task1 = asyncio.create_task(forward_to_sandbox())
            forward_task2 = asyncio.create_task(forward_from_sandbox())

            # 4.任一方向完成即关闭连接,取消另一方向未完成任务
            done, pending = await asyncio.wait(
                [forward_task1, forward_task2],
                return_when=asyncio.FIRST_COMPLETED,
            )
            logger.info("WebSocket连接已关闭")

            for task in pending:
                task.cancel()
    except ConnectionError as connection_e:
        logger.error(f"连接沙箱环境失败: {str(connection_e)}")
        await websocket.close(code=1011, reason=f"连接沙箱环境失败: {str(connection_e)}")
    except Exception as e:
        logger.error(f"WebSocket异常: {str(e)}")
        await websocket.close(code=1011, reason=f"WebSocket异常: {str(e)}")
    finally:
        # 混合方案: VNC断开时恢复完整截图模式
        await VNCStatusTracker.set_connected(session_id, False)
