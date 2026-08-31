#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : task_callback.py
异步任务回调通知工具(F10-7)

提供 task_wait 工具,LLM 调用此工具等待异步任务完成,
替代反复 sleep + read_output 轮询,节省 token。

使用场景:
1. LLM 调用 shell_execute(async_mode=True) 启动长耗时命令 → 立即返回 task_id
2. LLM 调用 task_wait(task_id, timeout=300) 阻塞等待命令完成
3. 命令完成时通过 TaskCallbackManager 通知 task_wait 返回结果

注意:
- task_wait 工具依赖 TaskCallbackManager 实例,未注入时返回错误引导 LLM 改用其他方式
- 工具自动清理回调资源(等待完成后 cancel)
"""
import logging
from typing import Optional

from app.domain.external.task_callback import TaskCallbackManager
from app.domain.models.tool_result import ToolResult
from .base import BaseTool, tool

logger = logging.getLogger(__name__)

# task_wait 默认超时(秒)
_DEFAULT_WAIT_TIMEOUT = 300
# task_wait 最大超时(秒),超过则截断
_MAX_WAIT_TIMEOUT = 600


class TaskCallbackTool(BaseTool):
    """异步任务回调通知工具箱(F10-7)

    提供 task_wait 工具,让 LLM 在等待异步任务完成时通过回调通知机制阻塞等待,
    避免反复调用 shell_wait_process + sleep 轮询浪费 token。
    """
    name: str = "task_callback"

    def __init__(self, callback_manager: Optional[TaskCallbackManager] = None) -> None:
        """构造函数

        Args:
            callback_manager: 异步任务回调管理器实例
                - 已注入: 支持 task_wait 等待异步任务完成
                - None: task_wait 返回错误,引导 LLM 改用 shell_wait_process
                (向后兼容:旧调用方未注入时不破坏现有流程)
        """
        super().__init__()
        self._callback_manager = callback_manager

    @tool(
        name="task_wait",
        description=(
            "等待异步任务完成并返回结果。用于 shell_execute(async_mode=true) "
            "启动的后台命令、MCP 工具(mcp_* async_mode=true) 启动的 MCP 异步任务、"
            "deep_research 异步模式等场景。"
            "调用此工具后会阻塞直到任务完成或超时,期间不消耗 LLM token。"
            "默认超时300秒,最大600秒。"
        ),
        parameters={
            "task_id": {
                "type": "string",
                "description": (
                    "异步任务的唯一标识符(由 shell_execute async_mode=true 或 "
                    "MCP 工具 mcp_* async_mode=true 等异步工具返回,形如 'shell_xxx' 或 'mcp_xxx')"
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "(可选)等待超时时间(秒),默认300,最大600。"
                    "超时后返回未完成状态,可继续调用 task_wait 等待。"
                ),
            },
        },
        required=["task_id"],
    )
    async def task_wait(
            self,
            task_id: str,
            timeout: Optional[int] = None,
    ) -> ToolResult:
        """等待异步任务完成

        通过 TaskCallbackManager.wait 阻塞等待任务回调,
        任务完成时返回完整结果 payload(success/message/data)。
        """
        if not self._callback_manager:
            logger.warning("task_wait 调用失败: 未注入 TaskCallbackManager")
            return ToolResult(
                success=False,
                message=(
                    "异步任务回调未启用,请改用 shell_wait_process 或 shell_read_output "
                    "轮询等待命令完成。"
                ),
            )

        if not task_id:
            return ToolResult(success=False, message="task_id 不能为空")

        # 截断超时到最大值,防止 LLM 设置过长导致会话卡死
        effective_timeout = _DEFAULT_WAIT_TIMEOUT if timeout is None else timeout
        if effective_timeout <= 0:
            effective_timeout = _DEFAULT_WAIT_TIMEOUT
        effective_timeout = min(effective_timeout, _MAX_WAIT_TIMEOUT)

        logger.info(f"task_wait 开始等待: task_id={task_id}, timeout={effective_timeout}s")
        try:
            payload = await self._callback_manager.wait(task_id, effective_timeout)
        except Exception as e:
            logger.exception(f"task_wait 等待异常: task_id={task_id}, error={e}")
            return ToolResult(
                success=False,
                message=f"等待异步任务异常: {str(e)}",
            )

        if payload is None:
            logger.info(f"task_wait 超时: task_id={task_id}, timeout={effective_timeout}s")
            return ToolResult(
                success=True,
                message=(
                    f"任务[{task_id}]等待超时({effective_timeout}秒),任务仍在执行中(非失败)。"
                    f"请继续调用 task_wait(task_id=\"{task_id}\", timeout=300) 等待完成,"
                    f"不要使用 sleep 轮询。大文件下载等场景可能需要多次 task_wait。"
                ),
                data={"task_id": task_id, "status": "timeout"},
            )

        # 任务完成,返回 payload
        logger.info(
            f"task_wait 完成: task_id={task_id}, success={payload.get('success')}"
        )
        return ToolResult(
            success=payload.get("success", True),
            message=payload.get("message", ""),
            data=payload.get("data"),
        )
