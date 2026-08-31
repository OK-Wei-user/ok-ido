#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/5/21 17:35

@File    : redis_stream_task.py
"""
import asyncio
import logging
import uuid
from typing import Optional, Dict

from app.domain.external.message_queue import MessageQueue
from app.domain.external.task import Task, TaskRunner
from app.infrastructure.external.message_queue.redis_stream_message_queue import RedisStreamMessageQueue

logger = logging.getLogger(__name__)


class RedisStreamTask(Task):
    """基于Redis流的任务类"""

    # 类级别任务注册表，用于通过task_id查找任务实例
    _task_registry: Dict[str, "RedisStreamTask"] = {}

    def __init__(self, task_runner: TaskRunner) -> None:
        """构造函数，传递任务运行器完成Task初始化"""
        self._task_runner = task_runner
        self._id = str(uuid.uuid4())
        self._execution_task: Optional[asyncio.Task] = None

        input_stream_name = f"task:input:{self._id}"
        output_stream_name = f"task:output:{self._id}"

        self._input_stream = RedisStreamMessageQueue(input_stream_name)
        self._output_stream = RedisStreamMessageQueue(output_stream_name)

        # 将当前实例注册到类级别注册表
        RedisStreamTask._task_registry[self._id] = self

    def _cleanup_registry(self) -> None:
        """清除类全局变量中当前注册的任务"""
        if self._id in RedisStreamTask._task_registry:
            del RedisStreamTask._task_registry[self._id]
            logger.info(f"任务[{self._id}]从注册中心移除")

    async def _cleanup_streams(self) -> None:
        """清理任务关联的Redis Stream，防止内存泄漏"""
        for stream in (self._input_stream, self._output_stream):
            try:
                await stream.clear()
            except Exception as e:
                logger.warning(f"任务[{self._id}]清理Redis Stream失败: {str(e)}")
        logger.info(f"任务[{self._id}]关联的Redis Stream已清理")

    async def _cleanup_input_stream(self) -> None:
        """仅清理输入流（任务结束后不再需要输入），保留输出流供chat()读取剩余事件"""
        try:
            await self._input_stream.clear()
        except Exception as e:
            logger.warning(f"任务[{self._id}]清理输入流失败: {str(e)}")

    def _on_task_done(self) -> None:
        """任务结束时的回调函数"""
        # 1.检测task_runner是否存在，如果存在则调用task_runner的回调函数
        if self._task_runner:
            try:
                asyncio.create_task(self._task_runner.on_done(self))
            except RuntimeError:
                logger.warning(f"任务[{self._id}]无法创建后台任务执行on_done回调")

        # 2.清除当前任务对应的资源
        self._cleanup_registry()

    async def _execute_task(self) -> None:
        """使用TaskRunner执行任务"""
        try:
            await self._task_runner.invoke(self)
        except asyncio.CancelledError:
            logger.info(f"任务[{self._id}]执行被取消")
            raise
        except Exception as e:
            logger.error(f"任务[{self._id}]执行出现异常: {str(e)}")
        finally:
            self._on_task_done()
            # 仅清理输入流，保留输出流供chat()方法读取剩余事件（如summarize的is_final消息和DoneEvent）
            # 输出流在cancel()或新任务创建时清理，且Redis Stream有maxlen=1000上限防止内存泄漏
            await self._cleanup_input_stream()

    async def invoke(self) -> None:
        """使用提供的task_runner来运行任务"""
        if self.done:
            self._execution_task = asyncio.create_task(self._execute_task())
            logger.info(f"任务[{self._id}]开始执行")

    def cancel(self) -> bool:
        """取消当前执行的任务"""
        if not self.done:
            # 1.取消任务
            self._execution_task.cancel()
            logger.info(f"任务[{self._id}]已取消")

        # 2.清除注册的当前任务
        self._cleanup_registry()

        # 3.异步清理Redis Stream（无论任务是否结束，都需清理输入+输出流）
        try:
            asyncio.create_task(self._cleanup_streams())
        except RuntimeError:
            logger.warning(f"任务[{self._id}]无法创建后台任务清理Stream")
        return True

    async def cleanup_browser(self) -> None:
        """清理浏览器状态(委托给TaskRunner实现)

        停止会话时调用，防止续接会话时残留上个会话的页面状态。
        无TaskRunner或无浏览器时为空操作，保证向后兼容。
        """
        if not self._task_runner:
            return
        try:
            await self._task_runner.cleanup_browser()
        except Exception as e:
            logger.warning(f"任务[{self._id}]清理浏览器状态失败(不影响主流程): {e}")

    async def cancel_background_tasks(self) -> None:
        """取消所有后台异步任务(F10-7,委托给TaskRunner实现)

        停止会话时调用,取消 shell_execute(async_mode=true) 启动的后台命令。
        无TaskRunner时为空操作,保证向后兼容。
        """
        if not self._task_runner:
            return
        try:
            await self._task_runner.cancel_background_tasks()
        except Exception as e:
            logger.warning(f"任务[{self._id}]取消后台异步任务失败(不影响主流程): {e}")

    @property
    def input_stream(self) -> MessageQueue:
        return self._input_stream

    @property
    def output_stream(self) -> MessageQueue:
        return self._output_stream

    @property
    def id(self) -> str:
        return self._id

    @property
    def done(self) -> bool:
        if self._execution_task is None:
            return True
        return self._execution_task.done()

    @classmethod
    def get(cls, task_id: str) -> Optional["Task"]:
        return RedisStreamTask._task_registry.get(task_id)

    @classmethod
    def create(cls, task_runner: TaskRunner) -> "Task":
        return cls(task_runner)

    @classmethod
    async def destroy(cls) -> None:
        for task_id in list(RedisStreamTask._task_registry.keys()):
            # 1.获取对应的任务
            task = RedisStreamTask._task_registry[task_id]
            task.cancel()

            # 2.检测任务是否有任务运行器
            if task._task_runner:
                await task._task_runner.destroy()

            # 3.清理Redis Stream
            await task._cleanup_streams()

        # 4.清除全局变量
        cls._task_registry.clear()
