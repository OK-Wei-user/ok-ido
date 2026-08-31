#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/14 10:48

@File    : session_repository.py
"""
from datetime import datetime
from typing import Protocol, List, Optional, Tuple

from app.domain.models.event import BaseEvent
from app.domain.models.file import File
from app.domain.models.memory import Memory
from app.domain.models.session import Session, SessionStatus


class SessionRepository(Protocol):
    """会话仓库协议定义"""

    async def save(self, session: Session) -> None:
        """存储或更新传递进来的会话"""
        ...

    async def get_all(self) -> List[Session]:
        """获取所有会话列表信息"""
        ...

    async def get_by_id(self, session_id: str) -> Optional[Session]:
        """根据传递的会话id查询会话"""
        ...

    async def get_events_after(
        self, session_id: str, last_event_id: str, limit: int, fallback_limit: int,
    ) -> Optional[Tuple[List[BaseEvent], bool]]:
        """查询 last_event_id 之后的事件,用于SSE断连补发(F3-3流式读取优化)

        只查询events JSONB列并按需切片反序列化,避免加载完整Session
        (含可能巨大的memories/files)造成内存与反序列化开销。

        Returns:
            None: 会话不存在
            (events, found): found=True表示last_event_id命中,
                events为其后limit条; found=False表示未命中,
                events为最近fallback_limit条(可能为空列表)
        """
        ...

    async def delete_by_id(self, session_id: str) -> None:
        """根据传递的会话id删除会话"""
        ...

    async def update_title(self, session_id: str, title: str) -> None:
        """根据传递的会话id+标题更新会话信息"""
        ...

    async def update_latest_message(self, session_id: str, message: str, timestamp: datetime) -> None:
        """根据传递的信息更新最新消息"""
        ...

    async def update_unread_message_count(self, session_id: str, count: int) -> None:
        """根据传递的信息更新未读消息数"""
        ...

    async def increment_unread_message_count(self, session_id: str) -> None:
        """根据传递的会话id新增未读消息数"""
        ...

    async def decrement_unread_message_count(self, session_id: str) -> None:
        """根据传递的会话id减少未读消息数"""
        ...

    async def update_status(self, session_id: str, status: SessionStatus) -> None:
        """根据传递的会话id更新会话状态"""
        ...

    async def add_event(self, session_id: str, event: BaseEvent) -> None:
        """往会话中新增事件"""
        ...

    async def add_file(self, session_id: str, file: File) -> None:
        """往会话中新增文件"""
        ...

    async def remove_file(self, session_id: str, file_id: str) -> None:
        """根据传递的会话id+文件id移除文件"""
        ...

    async def remove_files_by_path(self, session_id: str, filepath: str) -> int:
        """根据文件路径移除会话中所有匹配的文件,返回移除的数量"""
        ...

    async def get_file_by_path(self, session_id: str, filepath: str) -> Optional[File]:
        """查询会话中的文件信息"""
        ...

    async def save_memory(self, session_id: str, agent_name: str, memory: Memory) -> None:
        """更新or创建会话中指定Agent的记忆"""
        ...

    async def get_memory(self, session_id: str, agent_name: str) -> Memory:
        """根据传递的会话id+Agent名字获取记忆"""
        ...
