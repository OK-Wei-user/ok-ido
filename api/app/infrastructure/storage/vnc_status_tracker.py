#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""VNC连接状态跟踪器(混合方案核心组件)

进程内共享的VNC连接状态管理,供WebSocket代理和AgentTaskRunner查询。

设计要点:
- 线程安全: asyncio.Lock 保护并发写入(VNC连接/断开并发场景)
- 进程内共享: VNC WebSocket代理与AgentTaskRunner运行在同一进程,
  通过类变量直接共享状态,无需Redis等外部存储
- 优雅降级: 状态查询失败时返回False(不截图降级),不影响主流程

混合方案(会话410949eb优化):
- VNC连接时: 降低截图频率(实时画面已覆盖,截图仅用于历史回放)
- VNC断开时: 恢复完整截图策略(用户依赖截图了解操作结果)
"""
import asyncio
import logging
from typing import Dict

logger = logging.getLogger(__name__)


class VNCStatusTracker:
    """VNC连接状态跟踪器 — 进程内共享,类变量存储"""

    # session_id -> connected 状态映射
    _sessions: Dict[str, bool] = {}
    _lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def set_connected(cls, session_id: str, connected: bool) -> None:
        """更新会话的VNC连接状态

        Args:
            session_id: 会话ID
            connected: True=VNC已连接, False=VNC已断开
        """
        async with cls._lock:
            if connected:
                cls._sessions[session_id] = True
                logger.info(f"VNC状态跟踪: 会话[{session_id}]VNC已连接,截图降级模式启用")
            else:
                cls._sessions.pop(session_id, None)
                logger.info(f"VNC状态跟踪: 会话[{session_id}]VNC已断开,恢复完整截图模式")

    @classmethod
    def is_connected(cls, session_id: str) -> bool:
        """查询会话的VNC连接状态(同步,供AgentTaskRunner调用)

        Args:
            session_id: 会话ID

        Returns:
            True=VNC已连接(截图降级), False=VNC未连接(完整截图)
        """
        return cls._sessions.get(session_id, False)

    @classmethod
    async def clear(cls, session_id: str) -> None:
        """清理会话VNC状态(会话结束时调用)"""
        async with cls._lock:
            cls._sessions.pop(session_id, None)
