#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/5/20 10:50

@File    : task.py
"""
from abc import ABC, abstractmethod
from typing import Protocol, Optional

from app.domain.external.message_queue import MessageQueue


class TaskRunner(ABC):
    """任务运行器，负责任务的执行、关心的是如何执行任务、销毁任务释放资源"""

    @abstractmethod
    async def invoke(self, task: "Task") -> None:
        """调用任务并执行"""
        raise NotImplementedError

    @abstractmethod
    async def destroy(self) -> None:
        """销毁任务并释放资源，包括：关闭网络链接、释放内存、清理临时内存、清理后台进程等"""
        raise NotImplementedError

    @abstractmethod
    async def on_done(self, task: "Task") -> None:
        """执行任务完成时对应的回调函数"""
        raise NotImplementedError

    async def cleanup_browser(self) -> None:
        """清理浏览器状态(导航到空白页,释放当前页面资源)

        默认空实现，保证无浏览器的TaskRunner子类向后兼容。
        停止会话时调用，防止续接会话时残留上个会话的页面状态。
        子类按需覆盖该方法以实现具体的浏览器清理逻辑。
        """
        return None

    async def cancel_background_tasks(self) -> None:
        """取消所有后台异步任务(F10-7,会话停止时调用)

        默认空实现,保证无后台任务的TaskRunner子类向后兼容。
        停止会话时调用,取消 shell_execute(async_mode=true) 启动的后台命令,
        防止任务运行器实例销毁后仍有孤儿任务在运行。
        子类按需覆盖该方法以实现具体的取消逻辑。
        """
        return None


class Task(Protocol):
    """定义任务相关的操作接口协议"""

    async def invoke(self) -> None:
        """运行当前任务"""
        ...

    def cancel(self) -> bool:
        """取消当前任务"""
        ...

    async def cleanup_browser(self) -> None:
        """清理浏览器状态(导航到空白页,释放当前页面资源)

        停止会话时调用，委托给TaskRunner.cleanup_browser()实现。
        无浏览器的任务该方法为空操作，保证向后兼容。
        """
        ...

    async def cancel_background_tasks(self) -> None:
        """取消所有后台异步任务(F10-7,会话停止时调用)

        停止会话时调用,委托给TaskRunner.cancel_background_tasks()实现。
        无后台任务的TaskRunner该方法为空操作,保证向后兼容。
        """
        ...

    @property
    def input_stream(self) -> MessageQueue:
        """只读属性，返回任务的输入流"""
        ...

    @property
    def output_stream(self) -> MessageQueue:
        """只读属性，返回任务的输出流"""
        ...

    @property
    def id(self) -> str:
        """只读属性，返回任务的id"""
        ...

    @property
    def done(self) -> bool:
        """只读属性，返回任务是否结束"""
        ...

    @classmethod
    def get(cls, task_id: str) -> Optional["Task"]:
        """类方法，根据任务id获取对应任务"""
        ...

    @classmethod
    def create(cls, task_runner: TaskRunner) -> "Task":
        """根据传递的任务运行器创建任务"""
        ...

    @classmethod
    async def destroy(cls) -> None:
        """销毁所有任务实例"""
        ...
