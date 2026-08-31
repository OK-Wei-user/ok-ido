#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : task_callback.py
异步任务回调通知协议(F10-7)

设计目标:
- LLM 通过 task_wait 工具等待异步任务完成,避免轮询浪费 token
- 沙箱复用部分仅做"接口预留",异步回调通知独立落地(不依赖 F10-4)

核心协议:
1. register(task_id): 注册回调任务,返回可等待的 stream
2. notify(task_id, payload): 任务完成时推送结果(payload 含 success/message/data)
3. wait(task_id, timeout): 阻塞等待任务回调,超时返回 None
4. cancel(task_id): 取消等待并清理资源

应用场景:
- shell_execute(async_mode=True): 启动后台命令后立即返回 task_id,
  LLM 后续调用 task_wait(task_id) 等待完成
- deep_research 异步模式: 启动研究后立即返回 task_id,
  LLM 调用 task_wait 等待(后续扩展)
"""
from abc import ABC, abstractmethod
from typing import Any, Optional, Dict


class TaskCallbackManager(ABC):
    """异步任务回调通知管理器协议(F10-7)

    基于 Redis Stream 实现回调通知,避免 LLM 反复 sleep 轮询:
    - LLM 调用 shell_execute(async_mode=True) 启动后台任务 → 立即返回 task_id
    - LLM 调用 task_wait(task_id) 阻塞等待 → 任务完成时回调 stream 收到通知
    - 单次等待替代多次 sleep+read_output 轮询,显著节省 token
    """

    @abstractmethod
    async def register(self, task_id: str) -> None:
        """注册异步任务回调

        在启动后台任务前调用,创建回调 stream。
        重复 register 同一 task_id 时幂等(已存在则跳过)。

        Args:
            task_id: 任务唯一标识(通常与 shell_session_id 关联)
        """
        raise NotImplementedError

    @abstractmethod
    async def notify(self, task_id: str, payload: Dict[str, Any]) -> bool:
        """通知异步任务完成

        后台任务执行完毕后调用,推送完成事件到回调 stream。
        等待中的 task_wait 调用将被唤醒并返回 payload。

        Args:
            task_id: 任务唯一标识
            payload: 完成事件载荷,约定字段:
                - success: bool 任务是否成功
                - message: str 完成消息(可含错误信息)
                - data: Any 任务结果数据(如 shell 命令输出)

        Returns:
            True 表示通知成功,False 表示任务未注册或通知失败
        """
        raise NotImplementedError

    @abstractmethod
    async def wait(self, task_id: str, timeout: float) -> Optional[Dict[str, Any]]:
        """等待异步任务完成

        阻塞当前协程直到任务完成或超时。
        内部从回调 stream 读取完成事件,返回 payload。

        Args:
            task_id: 任务唯一标识
            timeout: 最大等待时长(秒),<=0 表示不等待(立即返回)

        Returns:
            完成事件 payload(同 notify 的 payload 字段),超时返回 None
        """
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, task_id: str) -> None:
        """取消任务回调,清理资源

        在任务取消或会话结束时调用,清理回调 stream。
        幂等操作,任务不存在时静默返回。
        """
        raise NotImplementedError
