#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/14 9:28

@File    : db_session_repository.py
"""
import logging
from datetime import datetime
from typing import List, Optional, Tuple

from pydantic import TypeAdapter
from sqlalchemy import select, delete, update, func, cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models.event import BaseEvent, Event
from app.domain.models.file import File
from app.domain.models.memory import Memory
from app.domain.models.session import Session, SessionStatus
from app.domain.repositories.session_repository import SessionRepository
from app.infrastructure.db_sanitize import sanitize_for_postgres
from app.infrastructure.models import SessionModel

logger = logging.getLogger(__name__)

# F3-3: Event类型适配器模块级单例,避免每次反序列化重建schema缓存
_EVENT_ADAPTER = TypeAdapter(Event)


class DBSessionRepository(SessionRepository):
    """基于Postgres数据库的会话仓库"""

    def __init__(self, db_session: AsyncSession) -> None:
        """构造函数，完成数据仓库的初始化"""
        self.db_session = db_session

    async def save(self, session: Session) -> None:
        """根据传递的领域模型更新或者新增会话"""
        # 1.根据id查询会话是否存在
        stmt = select(SessionModel).where(SessionModel.id == session.id)
        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()

        # 2.如果会话不存在则新建会话
        if not record:
            record = SessionModel.from_domain(session)
            self.db_session.add(record)
            return

        # 3.会话存在则更新会话
        record.update_from_domain(session)

    async def get_all(self) -> List[Session]:
        """获取所有会话列表(轻量查询,排除events/files/memories大字段)

        列表场景只需展示元数据(标题/状态/时间等),加载完整JSONB大字段
        会导致内存激增和查询变慢。此处仅SELECT必要列构建轻量Session。
        """
        stmt = (
            select(
                SessionModel.id,
                SessionModel.user_id,
                SessionModel.sandbox_id,
                SessionModel.task_id,
                SessionModel.title,
                SessionModel.unread_message_count,
                SessionModel.latest_message,
                SessionModel.latest_message_at,
                SessionModel.status,
                SessionModel.updated_at,
                SessionModel.created_at,
            )
            .order_by(SessionModel.latest_message_at.desc())
        )
        result = await self.db_session.execute(stmt)
        records = result.all()
        return [
            Session(
                id=r.id,
                user_id=r.user_id,
                sandbox_id=r.sandbox_id,
                task_id=r.task_id,
                title=r.title,
                unread_message_count=r.unread_message_count,
                latest_message=r.latest_message,
                latest_message_at=r.latest_message_at,
                status=r.status,
                updated_at=r.updated_at,
                created_at=r.created_at,
            )
            for r in records
        ]

    async def get_by_id(self, session_id: str) -> Optional[Session]:
        """根据id查询会话"""
        # 1.根据id查询会话是否存在
        stmt = select(SessionModel).where(SessionModel.id == session_id)
        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()

        # 2.判断会话记录是否存在并返回
        return record.to_domain() if record is not None else None

    async def get_events_after(
        self, session_id: str, last_event_id: str, limit: int, fallback_limit: int,
    ) -> Optional[Tuple[List[BaseEvent], bool]]:
        """F3-3流式读取: 只查询events JSONB列并按需切片反序列化

        相比get_by_id加载完整Session(含memories/files),本方法:
        - 仅SELECT events列,显著减少传输与ORM反序列化开销
        - 只对需要的limit条事件做BaseEvent反序列化,而非全部事件
        - 用于SSE断连补发场景,replay_missed_events热路径

        Returns:
            None: 会话不存在
            (events, found): found=True表示last_event_id命中,events为其后limit条;
                found=False表示未命中,events为最近fallback_limit条(可能为空列表)
        """
        # 1.仅查询events列(避免加载memories/files等大字段)
        stmt = select(SessionModel.events).where(SessionModel.id == session_id)
        result = await self.db_session.execute(stmt)
        events_data = result.scalar_one_or_none()
        if events_data is None:
            return None  # 会话不存在

        if not events_data:
            return ([], False)  # 会话存在但无事件

        # 2.在事件列表中定位last_event_id
        found_index = -1
        for i, ev in enumerate(events_data):
            if ev.get("id") == last_event_id:
                found_index = i
                break

        # 3.按定位结果切片
        if found_index >= 0:
            sliced = events_data[found_index + 1: found_index + 1 + limit]
            found = True
        else:
            # 未命中: 回退补发最近fallback_limit条
            sliced = events_data[-fallback_limit:] if fallback_limit > 0 else []
            found = False

        # 4.反序列化为BaseEvent(仅对需要的切片做转换)
        events: List[BaseEvent] = []
        for ev_dict in sliced:
            try:
                events.append(_EVENT_ADAPTER.validate_python(ev_dict))
            except Exception:
                # 单条事件损坏不阻断补发,跳过并记录
                logger.warning(f"会话[{session_id}]事件反序列化失败,跳过: {ev_dict.get('id')}")
        return (events, found)

    async def delete_by_id(self, session_id: str) -> None:
        """根据传递的id删除会话"""
        # 1.构建删除语句
        stmt = delete(SessionModel).where(SessionModel.id == session_id)

        # 2.执行sql无需检查是否删除
        await self.db_session.execute(stmt)

    async def update_title(self, session_id: str, title: str) -> None:
        """更新会话标题"""
        # 1.构建更新语句并执行
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(title=title)
        )
        result = await self.db_session.execute(stmt)

        # 2.检查是否更新成功
        if result.rowcount == 0:
            raise ValueError(f"会话[{session_id}]不存在，请核实后重试")

    async def update_latest_message(self, session_id: str, message: str, timestamp: datetime) -> None:
        """更新会话最新消息"""
        # 1.构建更新语句并执行
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(
                latest_message=message,
                latest_message_at=timestamp,
            )
        )
        result = await self.db_session.execute(stmt)

        # 2.检查是否更新成功
        if result.rowcount == 0:
            raise ValueError(f"会话[{session_id}]不存在，请核实后重试")

    async def add_event(self, session_id: str, event: BaseEvent) -> None:
        """往会话中新增事件"""
        # 1.将event序列化为json并清洗PostgreSQL不支持的字符
        event_data = sanitize_for_postgres(event.model_dump(mode="json"))

        # 2.构建原子更新语句并执行
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(
                events=func.coalesce(SessionModel.events, cast([], JSONB)) + cast([event_data], JSONB),
            )
        )
        result = await self.db_session.execute(stmt)

        # 3.检查是否新增成功
        if result.rowcount == 0:
            raise ValueError(f"会话[{session_id}]不存在，请核实后重试")

    async def add_file(self, session_id: str, file: File) -> None:
        """往会话中新增文件"""
        # 1.将file序列化为json并清洗PostgreSQL不支持的字符
        file_data = sanitize_for_postgres(file.model_dump(mode="json"))

        # 2.构建原子更新语句并执行
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(
                files=func.coalesce(SessionModel.files, cast([], JSONB)) + cast([file_data], JSONB),
            )
        )
        result = await self.db_session.execute(stmt)

        # 3.检查是否新增成功
        if result.rowcount == 0:
            raise ValueError(f"会话[{session_id}]不存在，请核实后重试")

    async def remove_file(self, session_id: str, file_id: str) -> None:
        """移除会话中的指定文件"""
        # 1.查询会话记录并加锁
        stmt = select(SessionModel).where(SessionModel.id == session_id).with_for_update()
        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()

        # 2.检查会话记录是否存在
        if not record:
            raise ValueError(f"会话[{session_id}]不存在，请核实后重试")

        # 3.会话记录存在在，则在内存中过滤files
        if not record.files:
            return
        original_length = len(record.files)
        new_files = [file for file in record.files if file.get("id") != file_id]

        # 4.判断文件长度是否有变化
        if len(new_files) == original_length:
            return

        # 5.更新数据
        record.files = new_files

    async def remove_files_by_path(self, session_id: str, filepath: str) -> int:
        """根据文件路径移除会话中所有匹配的文件,返回移除的数量"""
        # 1.查询会话文件列表并加锁
        stmt = select(SessionModel.files).where(SessionModel.id == session_id).with_for_update()
        result = await self.db_session.execute(stmt)
        files = result.scalar_one_or_none()

        # 2.判断是否有文件
        if not files:
            return 0

        # 3.过滤掉所有匹配filepath的文件
        original_length = len(files)
        new_files = [f for f in files if f.get("filepath", "") != filepath]
        removed_count = original_length - len(new_files)

        if removed_count == 0:
            return 0

        # 4.直接SQL更新(避免ORM JSONB变更追踪问题)
        update_stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(files=cast(new_files, JSONB))
        )
        await self.db_session.execute(update_stmt)

        return removed_count

    async def get_file_by_path(self, session_id: str, filepath: str) -> Optional[File]:
        """根据文件路径获取文件信息"""
        # 1.构建语句查询文件列表
        stmt = select(SessionModel.files).where(SessionModel.id == session_id)
        result = await self.db_session.execute(stmt)
        files = result.scalar_one_or_none()

        # 2.判断是否为空，如果不存在则返回None
        if not files:
            return None

        # 3.遍历查找数据，如果最后没找到则返回空
        for file in files:
            if file.get("filepath", "") == filepath:
                return File(**file)

        return None

    async def update_status(self, session_id: str, status: SessionStatus) -> None:
        """更新会话状态"""
        # 1.构建更新语句并执行
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(status=status.value)
        )
        result = await self.db_session.execute(stmt)

        # 2.检查是否更新成功
        if result.rowcount == 0:
            raise ValueError(f"会话[{session_id}]不存在，请核实后重试")

    async def update_unread_message_count(self, session_id: str, count: int) -> None:
        """更新会话的未读消息数"""
        # 1.构建更新语句并执行
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(unread_message_count=count)
        )
        result = await self.db_session.execute(stmt)

        # 2.检查是否更新成功
        if result.rowcount == 0:
            raise ValueError(f"会话[{session_id}]不存在，请核实后重试")

    async def increment_unread_message_count(self, session_id: str) -> None:
        """新增会话的未读消息数"""
        # 1.构建新增未读消息数语句并更新
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(
                unread_message_count=func.coalesce(SessionModel.unread_message_count, 0) + 1,
            )
        )
        result = await self.db_session.execute(stmt)

        # 2.检查是否更新成功
        if result.rowcount == 0:
            raise ValueError(f"会话[{session_id}]不存在，请核实后重试")

    async def decrement_unread_message_count(self, session_id: str) -> None:
        """将会话中的未读消息数-1"""
        # 1.构建新增未读消息数语句并更新
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(
                # 2.核心逻辑：GREATEST((当前值-1), 0)避免出现负数
                unread_message_count=func.greatest(
                    func.coalesce(SessionModel.unread_message_count, 0) - 1,
                    0
                )
            )
        )
        result = await self.db_session.execute(stmt)

        # 3.检查是否更新成功
        if result.rowcount == 0:
            raise ValueError(f"会话[{session_id}]不存在，请核实后重试")

    async def save_memory(self, session_id: str, agent_name: str, memory: Memory) -> None:
        """存储或者更新会话中的记忆(字典直接覆盖)

        持久化前清理图片base64: 浏览器截图/MCP图片通过image_url块传递,
        base64体积大(单张数百KB)且属瞬时页面状态,持久化入库会导致数据库膨胀。
        内存中的Memory对象保留image_url块供当前会话多模态LLM决策(感知→执行闭环),
        仅在持久化副本上替换为文本标记,实现"快照不入持久记忆"。
        """
        # 1.将memory转换成为json结构
        memory_data = memory.model_dump(mode="json")
        # 清理image_url块的base64数据(替换为文本标记),仅作用于持久化副本
        messages = memory_data.get("messages")
        if isinstance(messages, list):
            memory_data["messages"] = Memory.strip_image_data(messages)
        # 清洗PostgreSQL不支持的字符
        memory_data = sanitize_for_postgres(memory_data)

        # 2.构建要打补丁的字典
        patch_data = {agent_name: memory_data}

        # 3.执行合并更新
        stmt = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(
                memories=func.coalesce(SessionModel.memories, cast({}, JSONB)) + cast(patch_data, JSONB),
            )
        )
        result = await self.db_session.execute(stmt)

        # 4.检查是否更新成功
        if result.rowcount == 0:
            raise ValueError(f"会话[{session_id}]不存在，请核实后重试")

    async def get_memory(self, session_id: str, agent_name: str) -> Memory:
        """获取指定会话的agent记忆信息"""
        # 1.查询会话记忆信息
        stmt = (
            select(SessionModel.memories[agent_name])
            .where(SessionModel.id == session_id)
        )
        result = await self.db_session.execute(stmt)
        memory_data = result.scalar_one_or_none()

        # 2.如果存在记忆则直接返回
        if memory_data:
            return Memory(**memory_data)

        # 3.如果记忆不存在，则构建一个空记忆后返回
        return Memory(messages=[])
