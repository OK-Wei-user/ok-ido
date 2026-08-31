#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""沙箱回调端点(Batch 40 / 方向1: P11 沙箱异步任务通知)

设计目标:
- 接收沙箱 callback_agent 的 HTTP POST 回调,联动 TaskCallbackManager
- 沙箱任务完成后主动推送,替代 API 层轮询,延迟 < 1s

端点: POST /api/internal/sandbox/callback
载荷: {task_id, success, message, data, exit_code}

安全:
- 仅 Docker 内网可访问(api 容器仅暴露 8000 端口,沙箱在同一 Docker 网络)
- 不需要 JWT 认证(内部端点),但通过 Docker 网络隔离保障安全
"""
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/sandbox", tags=["内部接口"])


class SandboxCallbackPayload(BaseModel):
    """沙箱回调载荷模型"""
    task_id: str
    success: bool
    message: str = ""
    data: Optional[Any] = None
    exit_code: int = -1


@router.post(
    path="/callback",
    summary="沙箱异步任务完成回调(内部端点)",
    description="接收沙箱 callback_agent 的任务完成通知,联动 TaskCallbackManager 唤醒等待方",
)
async def sandbox_callback(
        payload: SandboxCallbackPayload,
) -> Dict[str, Any]:
    """处理沙箱回调,通知等待中的 task_wait

    流程:
    1. 接收沙箱 callback_agent 的 POST 回调
    2. 调用 TaskCallbackManager.notify(task_id, payload) 唤醒等待方
    3. 返回确认(沙箱侧删除状态文件)

    降级: TaskCallbackManager 未注入或 notify 失败时返回 200(避免沙箱重试),
         等待方通过 task_wait 超时兜底
    """
    callback_manager = None
    try:
        from app.infrastructure.external.task_callback import RedisStreamTaskCallbackManager
        callback_manager = RedisStreamTaskCallbackManager()
    except Exception as e:
        logger.warning(
            f"沙箱回调到达但 TaskCallbackManager 创建失败, task_id={payload.task_id}: {e}"
        )
        return {"ok": True, "message": "callback_manager_unavailable"}

    try:
        await callback_manager.notify(
            payload.task_id,
            {
                "success": payload.success,
                "message": payload.message,
                "data": payload.data,
            },
        )
        logger.info(
            f"沙箱回调处理成功: task_id={payload.task_id}, "
            f"success={payload.success}, exit_code={payload.exit_code}"
        )
    except Exception as e:
        logger.error(
            f"沙箱回调 notify 失败(降级返回200避免沙箱重试): "
            f"task_id={payload.task_id}, error={e}"
        )

    return {"ok": True}
