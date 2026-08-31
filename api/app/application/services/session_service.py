#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/04 10:18

@File    : session_service.py
"""
import logging
from typing import List, Callable, Type

from app.application.errors.exceptions import NotFoundError, ServerRequestsError
from app.application.services.file_presentation_service import FilePresentationService
from app.domain.external.sandbox import Sandbox
from app.domain.models.app_config import FilePresentationConfig
from app.domain.models.file import File
from app.domain.models.session import Session
from app.domain.repositories.uow import IUnitOfWork
from app.interfaces.schemas.session import FileReadResponse, ShellReadResponse

logger = logging.getLogger(__name__)


class SessionService:
    """会话服务"""

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],
            sandbox_cls: Type[Sandbox],
            file_presentation_config: FilePresentationConfig = None,
    ) -> None:
        """构造函数，完成会话服务初始化

        Args:
            uow_factory: 工作单元工厂
            sandbox_cls: 沙箱类型(用于级联销毁)
            file_presentation_config: 文件展示策略配置(F2-3外置),None时使用默认配置
        """
        self._uow_factory = uow_factory
        self._uow = uow_factory()
        self._sandbox_cls = sandbox_cls
        # 文件展示策略服务(F2-2抽离): 通过组合持有,委托文件展示相关逻辑
        # 注入config保证策略可运维调整,默认配置保证老调用方无需传参
        self._file_presentation = FilePresentationService(config=file_presentation_config)

    async def create_session(self, user_id: str = None) -> Session:
        """创建一个空白的新任务会话"""
        logger.info(f"创建一个空白新任务会话, user_id={user_id}")
        session = Session(title="新对话", user_id=user_id)
        async with self._uow:
            await self._uow.session.save(session)
        logger.info(f"成功创建一个新任务会话: {session.id}")
        return session

    async def get_all_sessions(self, user_id: str = None) -> List[Session]:
        """获取项目所有任务会话列表（支持按用户过滤）"""
        async with self._uow:
            sessions = await self._uow.session.get_all()
        if user_id:
            sessions = [s for s in sessions if s.user_id == user_id]
        return sessions

    async def clear_unread_message_count(self, session_id: str) -> None:
        """清空指定会话未读消息数"""
        logger.info(f"清除会话[{session_id}]未读消息数")
        async with self._uow:
            await self._uow.session.update_unread_message_count(session_id, 0)

    async def delete_session(self, session_id: str) -> None:
        """根据传递的会话id删除任务会话

        资源清理策略(级联销毁兜底):
        1. 先查询会话获取sandbox_id(用于级联销毁)
        2. 删除DB记录
        3. 删除DB成功后,主动销毁沙箱(作为TTL取消后的兜底,
           避免会话已删除但沙箱仍残留导致资源泄漏)
        4. 沙箱销毁失败不阻断主流程(仅记录警告,沙箱有独立TTL自动清理)
        """
        # 1.先检查会话是否存在并取出sandbox_id(用于级联销毁)
        logger.info(f"正在删除会话, 会话id: {session_id}")
        async with self._uow:
            session = await self._uow.session.get_by_id(session_id)
        if not session:
            logger.error(f"会话[{session_id}]不存在, 删除失败")
            raise NotFoundError(f"会话[{session_id}]不存在, 删除失败")

        # 2.根据传递的会话id删除会话(DB操作)
        async with self._uow:
            await self._uow.session.delete_by_id(session_id)
        logger.info(f"删除会话[{session_id}]成功")

        # 3.级联销毁沙箱(兜底): 会话已从DB删除,沙箱不应再残留
        #    此处与agent_service.cancel_sandbox_ttl配合:
        #    - cancel_sandbox_ttl取消延迟销毁任务
        #    - 这里立即主动销毁沙箱,确保资源释放
        sandbox_id = session.sandbox_id
        if sandbox_id:
            try:
                sandbox = await self._sandbox_cls.get(sandbox_id)
                if sandbox:
                    await sandbox.destroy()
                    logger.info(f"会话[{session_id}]沙箱[{sandbox_id}]已级联销毁")
            except Exception as e:
                # 沙箱销毁失败不阻断删除流程(沙箱有独立TTL兜底自动清理)
                logger.warning(f"会话[{session_id}]沙箱[{sandbox_id}]级联销毁失败(不阻断): {e}")

    async def get_session(self, session_id: str) -> Session:
        """获取指定会话详情信息"""
        async with self._uow:
            return await self._uow.session.get_by_id(session_id)

    async def get_session_files(self, session_id: str) -> List[File]:
        """根据传递的会话id获取指定会话的文件列表信息

        文件展示策略委托给FilePresentationService执行(F2-2抽离):
        - 交付文件优先: 最终答案消息attachments中的文件排在前面
        - 全量文件展示: 非交付文件也展示(排在后面),确保用户看到所有文件
        - 空文件过滤: SYNCED状态且size为0的文件不返回(写入失败或空文件)
        - PENDING文件保留: 尚未同步完成的文件保留(可能size暂为0)
        - 优先级排序: 每组内按文件类型重要性排序(xlsx>png>txt>py>log)
        """
        logger.info(f"获取指定会话[{session_id}]下的文件列表信息")
        async with self._uow:
            session = await self._uow.session.get_by_id(session_id)
        if not session:
            raise RuntimeError(f"当前会话不存在[{session_id}], 请核实后重试")

        # 委托FilePresentationService执行去重+过滤+分组+排序
        all_files = self._file_presentation.deduplicate_files(
            self._file_presentation.filter_empty_files(list(session.files))
        )
        return self._file_presentation.present_files(session, all_files)

    async def read_file(self, session_id: str, filepath: str) -> FileReadResponse:
        """根据传递的信息查看会话中指定文件的内容"""
        # 1.检查会话是否存在
        logger.info(f"获取会话[{session_id}]中的文件内容, 文件路径: {filepath}")
        async with self._uow:
            session = await self._uow.session.get_by_id(session_id)
        if not session:
            raise RuntimeError(f"当前会话不存在[{session_id}], 请核实后重试")

        # 2.根据沙箱id获取沙箱并判断是否存在
        if not session.sandbox_id:
            raise NotFoundError("当前会话无沙箱环境")
        sandbox = await self._sandbox_cls.get(session.sandbox_id)
        if not sandbox:
            raise NotFoundError("当前会话沙箱不存在或已销毁")

        # 3.调用沙箱读取文件内容
        result = await sandbox.read_file(filepath)
        if result.success:
            return FileReadResponse(**result.data)

        raise ServerRequestsError(result.message)

    async def read_shell_output(self, session_id: str, shell_session_id: str) -> ShellReadResponse:
        """根据传递的任务会话id+Shell会话id获取Shell执行结果"""
        # 1.检查会话是否存在
        logger.info(f"获取会话[{session_id}]中的Shell内容输出, Shell标识符: {shell_session_id}")
        async with self._uow:
            session = await self._uow.session.get_by_id(session_id)
        if not session:
            raise RuntimeError(f"当前会话不存在[{session_id}], 请核实后重试")

        # 2.根据沙箱id获取沙箱并判断是否存在
        if not session.sandbox_id:
            raise NotFoundError("当前会话无沙箱环境")
        sandbox = await self._sandbox_cls.get(session.sandbox_id)
        if not sandbox:
            raise NotFoundError("当前会话沙箱不存在或已销毁")

        # 3.调用沙箱查看shell内容
        result = await sandbox.read_shell_output(session_id=shell_session_id, console=True)
        if result.success:
            return ShellReadResponse(**result.data)

        raise ServerRequestsError(result.message)

    async def get_vnc_url(self, session_id: str) -> str:
        """获取指定会话的vnc链接"""
        # 1.检查会话是否存在
        logger.info(f"获取会话[{session_id}]的VNC链接")
        async with self._uow:
            session = await self._uow.session.get_by_id(session_id)
        if not session:
            raise RuntimeError(f"当前会话不存在[{session_id}], 请核实后重试")

        # 2.根据沙箱id获取沙箱并判断是否存在
        if not session.sandbox_id:
            raise NotFoundError("当前会话无沙箱环境")
        sandbox = await self._sandbox_cls.get(session.sandbox_id)
        if not sandbox:
            raise NotFoundError("当前会话沙箱不存在或已销毁")

        return sandbox.vnc_url
