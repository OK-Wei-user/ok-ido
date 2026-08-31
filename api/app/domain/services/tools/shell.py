#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/10 9:48

@File    : shell.py
"""
import asyncio
import logging
import os
import uuid
from typing import Optional

from app.domain.external.sandbox import Sandbox
from app.domain.external.task_callback import TaskCallbackManager
from app.domain.models.tool_result import ToolResult
from .base import BaseTool, tool

logger = logging.getLogger(__name__)

# 异步任务默认超时(秒),与 exec_command 默认 300s 一致
_DEFAULT_ASYNC_TIMEOUT = 300

# Batch 40 / 方向1: P11 沙箱回调模式配置
# API 回调端点 URL(沙箱 → API 方向, Docker 网络内通信)
_API_CALLBACK_URL = os.getenv("SANDBOX_CALLBACK_URL", "http://api:8000/api/internal/sandbox/callback")
# P11 模式开关(True 时优先使用沙箱侧执行+回调,False 时降级到 asyncio.Task 轮询)
_P11_ENABLED = os.getenv("P11_SANDBOX_CALLBACK_ENABLED", "true").lower() == "true"

# P11 沙箱内 wrapper 脚本模板(在沙箱内运行实际命令并写入状态文件)
_P11_WRAPPER_TEMPLATE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P11 沙箱回调 wrapper(Batch 40 / 方向1, 由 API 自动生成)"""
import json, os, subprocess, sys, time
from pathlib import Path

task_id = sys.argv[1]
exec_dir = sys.argv[2]
command = sys.argv[3]
timeout = int(sys.argv[4]) if len(sys.argv) > 4 else 300
callback_url = sys.argv[5] if len(sys.argv) > 5 else ""

status_dir = Path("/tmp/task_status")
status_dir.mkdir(parents=True, exist_ok=True)
status_file = status_dir / f"{task_id}.json"

# 写入初始状态(含 callback_url, 供 callback_agent 使用)
initial = {{"task_id": task_id, "api_callback_url": callback_url, "started_at": time.time()}}
status_file.write_text(json.dumps(initial, ensure_ascii=False), encoding="utf-8")

try:
    result = subprocess.run(
        command, shell=True, cwd=exec_dir, capture_output=True,
        text=True, timeout=timeout,
    )
    payload = {{
        "task_id": task_id,
        "result": {{
            "success": result.returncode == 0,
            "message": result.stdout[-5000:] if result.stdout else "",
            "data": {{"stdout": result.stdout, "stderr": result.stderr[-2000:]}},
            "exit_code": result.returncode,
        }},
    }}
except subprocess.TimeoutExpired:
    payload = {{
        "task_id": task_id,
        "result": {{
            "success": False, "message": f"命令超时({timeout}s)",
            "data": None, "exit_code": -1,
        }},
    }}
except Exception as e:
    payload = {{
        "task_id": task_id,
        "result": {{
            "success": False, "message": str(e),
            "data": None, "exit_code": -1,
        }},
    }}

# 合并初始状态(保留 api_callback_url)
final = {{**initial, **payload}}
status_file.write_text(json.dumps(final, ensure_ascii=False), encoding="utf-8")
'''


class ShellTool(BaseTool):
    """Shell工具箱，提供Shell交互相关功能

    F10-7 异步回调通知:
    - shell_execute 支持 async_mode=true 参数
    - 启用后立即返回 task_id,命令在后台 asyncio Task 中执行
    - 命令完成时通过 TaskCallbackManager.notify 推送结果
    - LLM 调用 task_wait(task_id) 等待完成并获取结果
    """
    name: str = "shell"

    def __init__(
            self,
            sandbox: Sandbox,
            callback_manager: Optional[TaskCallbackManager] = None,
    ) -> None:
        """构造函数，完成Shell工具箱初始化

        Args:
            sandbox: 沙箱实例
            callback_manager: 异步任务回调管理器(F10-7,可选)
                - 已注入: shell_execute(async_mode=true) 可用
                - None: async_mode=true 时降级为同步执行(向后兼容)
        """
        super().__init__()
        self.sandbox = sandbox
        self._callback_manager = callback_manager
        # 后台任务追踪表: task_id -> asyncio.Task(用于会话停止时取消)
        self._background_tasks: dict[str, asyncio.Task] = {}

    @tool(
        name="shell_execute",
        description=(
            "在指定 Shell 会话中执行命令。可用于运行代码、安装依赖包或文件管理。"
            "命令默认超时300秒，超时后自动终止；可通过timeout参数自定义超时时间（最大600秒）。"
            "支持 async_mode=true 异步执行: 启动后立即返回 task_id,后续通过 task_wait 工具等待完成,"
            "避免长时间阻塞影响其他步骤的规划与执行。"
            "session_id 和 exec_dir 为可选参数,省略时自动创建会话并使用默认工作目录。"
        ),
        parameters={
            "session_id": {
                "type": "string",
                "description": "(可选)目标 Shell 会话的唯一标识符,省略时自动创建新会话",
            },
            "exec_dir": {
                "type": "string",
                "description": "(可选)执行命令的工作目录(绝对路径),省略时使用默认目录 /home/ubuntu",
            },
            "command": {
                "type": "string",
                "description": "要执行的 Shell 命令。支持通过 && 或 heredoc 合并多个命令;批量同类操作(如多工作表写入)建议合并为单次调用(在 Python 脚本内用循环完成),减少调用次数节省 token。",
            },
            "timeout": {
                "type": "integer",
                "description": "命令超时时间（秒），超时后自动终止进程。默认300秒，最大600秒。对于已知耗时较长的命令（如编译、下载大文件）可适当增大",
            },
            "async_mode": {
                "type": "boolean",
                "description": (
                    "(可选)是否异步执行,默认false同步阻塞。"
                    "设为true时立即返回task_id,后续调用task_wait工具等待完成。"
                    "适用于长耗时命令(>30秒),让LLM在等待期间可继续规划后续步骤。"
                ),
            },
        },
        required=["command"],
    )
    async def shell_execute(
            self,
            command: str,
            session_id: Optional[str] = None,
            exec_dir: Optional[str] = None,
            timeout: Optional[int] = None,
            async_mode: bool = False,
    ) -> ToolResult:
        """执行shell脚本

        session_id/exec_dir 可选(批次41修复):
        - LLM 常遗漏 session_id 导致 TypeError,进而触发 100 次迭代循环超时
        - 省略时自动生成 session_id 并使用默认 exec_dir,沙箱侧已支持按需创建会话

        async_mode=true(F10-7)时:
        - 生成 task_id 并注册到 TaskCallbackManager
        - 启动 asyncio.Task 后台执行 sandbox.exec_command
        - 立即返回 ToolResult(success=True, data={"task_id": ..., "status": "running"})
        - 后台任务完成时调用 callback_manager.notify(task_id, payload)
        - LLM 后续调用 task_wait(task_id) 阻塞等待结果

        async_mode=false(默认)时:
        - 保持原同步阻塞行为,直接返回 exec_command 结果
        """
        # 批次41: session_id/exec_dir 自动填充,避免 LLM 遗漏参数导致 TypeError
        # 沙箱端已支持按需创建会话(sandbox/interfaces/endpoints/shell.py),此处仅需保证非空
        if not session_id:
            session_id = f"shell_{uuid.uuid4().hex[:12]}"
            logger.debug(f"shell_execute 未传入 session_id,自动生成: {session_id}")
        if not exec_dir:
            exec_dir = "/home/ubuntu"

        # 同步模式: 保持原行为,向后兼容
        if not async_mode:
            return await self._exec_sync_and_inject_session(session_id, exec_dir, command, timeout)

        # 异步模式: 需要回调管理器,未注入时降级为同步
        if not self._callback_manager:
            logger.info(
                f"shell_execute async_mode=true 但未注入 TaskCallbackManager, "
                f"降级为同步执行: session_id={session_id}, command={command[:50]}"
            )
            return await self._exec_sync_and_inject_session(session_id, exec_dir, command, timeout)

        # 生成 task_id 并注册
        task_id = f"shell_{uuid.uuid4().hex[:12]}"
        await self._callback_manager.register(task_id)
        effective_timeout = timeout if timeout is not None else _DEFAULT_ASYNC_TIMEOUT

        # Batch 40 / 方向1: P11 沙箱回调模式(优先,命令在沙箱侧独立执行)
        # 降级: P11 失败时回退到 asyncio.Task 模式(向后兼容)
        if _P11_ENABLED:
            p11_success = await self._try_p11_mode(
                task_id, session_id, exec_dir, command, effective_timeout
            )
            if p11_success:
                logger.info(
                    f"shell_execute P11 异步任务已启动: task_id={task_id}, "
                    f"session_id={session_id}, command={command[:50]}"
                )
                return ToolResult(
                    success=True,
                    message=(
                        f"命令已异步启动(P11沙箱回调模式),task_id={task_id}。"
                        f"请调用 task_wait 工具等待完成并获取结果(task_id 参数填 '{task_id}')。"
                    ),
                    data={
                        "task_id": task_id,
                        "status": "running",
                        "mode": "p11_sandbox_callback",
                        "session_id": session_id,
                        "command": command,
                    },
                )
            logger.info(f"shell_execute P11 模式失败,降级到 asyncio.Task: task_id={task_id}")

        # 降级模式: asyncio.Task 后台执行(F10-7 原始路径)
        background_task = asyncio.create_task(
            self._run_shell_background(
                task_id=task_id,
                session_id=session_id,
                exec_dir=exec_dir,
                command=command,
                timeout=effective_timeout,
            ),
            name=f"shell_async_{task_id}",
        )
        self._background_tasks[task_id] = background_task

        logger.info(
            f"shell_execute 异步任务已启动(asyncio.Task模式): task_id={task_id}, "
            f"session_id={session_id}, command={command[:50]}, timeout={effective_timeout}s"
        )

        return ToolResult(
            success=True,
            message=(
                f"命令已异步启动,task_id={task_id}。"
                f"请调用 task_wait 工具等待完成并获取结果(task_id 参数填 '{task_id}')。"
            ),
            data={
                "task_id": task_id,
                "status": "running",
                "session_id": session_id,
                "command": command,
            },
        )

    async def _exec_sync_and_inject_session(
            self,
            session_id: str,
            exec_dir: str,
            command: str,
            timeout: Optional[int],
    ) -> ToolResult:
        """同步执行 shell 命令并回注 session_id 到结果(批次42修复)

        背景: 批次41 让 session_id/exec_dir 可选后,LLM 省略 session_id 时
        `AgentTaskRunner._handle_tool_event` 无法回读控制台输出,导致前端
        永远显示"等待命令输出..."。本方法在同步执行后将 session_id 注入到
        ToolResult.data,供 _handle_tool_event 在 function_args 缺失时回读。

        注入策略(不破坏沙箱原始返回结构):
        - data 为 dict: 直接添加 session_id key(不覆盖已有值)
        - data 为 None: 设置 data = {"session_id": session_id}
        - data 为其他类型: 包装为 {"session_id": ..., "output": data}
        """
        result = await self.sandbox.exec_command(session_id, exec_dir, command, timeout)
        # 回注 session_id 供前端控制台读取(批次42)
        try:
            if isinstance(result.data, dict):
                result.data.setdefault("session_id", session_id)
            elif result.data is None:
                result.data = {"session_id": session_id}
            else:
                # 沙箱返回非 dict 类型(如纯字符串),包装保留原数据
                result.data = {"session_id": session_id, "output": result.data}
        except Exception as e:
            logger.warning(f"shell_execute 注入 session_id 失败(不影响主流程): {e}")
        return result

    async def _try_p11_mode(
            self,
            task_id: str,
            session_id: str,
            exec_dir: str,
            command: str,
            timeout: int,
    ) -> bool:
        """P11 沙箱回调模式(Batch 40 / 方向1)

        在沙箱内运行 wrapper 脚本,命令独立于 API 进程执行:
        1. 写入 wrapper 脚本到沙箱 /tmp/p11_wrapper_{task_id}.py
        2. 后台启动 wrapper(nohup ... &),立即返回
        3. wrapper 运行命令 → 写结果到 /tmp/task_status/{task_id}.json
        4. callback_agent 读取状态文件 → HTTP POST 回调 API
        5. API 回调端点 → TaskCallbackManager.notify() → task_wait 唤醒

        Returns:
            True 表示 P11 启动成功, False 表示失败(调用方降级到 asyncio.Task)
        """
        wrapper_path = f"/tmp/p11_wrapper_{task_id}.py"
        try:
            # 1.写入 wrapper 脚本到沙箱
            await self.sandbox.write_file(wrapper_path, _P11_WRAPPER_TEMPLATE)

            # 2.后台启动 wrapper(引号转义命令参数)
            # 使用 nohup + & 实现后台运行, exec_command 应立即返回
            escaped_command = command.replace("'", "'\"'\"'")
            bg_command = (
                f"nohup python3 {wrapper_path} '{task_id}' '{exec_dir}' "
                f"'{escaped_command}' '{timeout}' '{_API_CALLBACK_URL}' "
                f"> /dev/null 2>&1 &"
            )
            result = await self.sandbox.exec_command(
                session_id, exec_dir, bg_command, timeout=10,
            )
            # exec_command 返回即表示后台启动成功(不等待命令完成)
            if not result.success:
                logger.warning(
                    f"P11 wrapper 启动失败: task_id={task_id}, msg={result.message}"
                )
                return False

            logger.info(f"P11 wrapper 已后台启动: task_id={task_id}, wrapper={wrapper_path}")
            return True

        except Exception as e:
            logger.warning(f"P11 模式异常(降级到 asyncio.Task): task_id={task_id}, error={e}")
            return False

    async def _run_shell_background(
            self,
            task_id: str,
            session_id: str,
            exec_dir: str,
            command: str,
            timeout: int,
    ) -> None:
        """后台执行 shell 命令,完成后通知回调管理器

        异常隔离: 任何异常都不会抛出,统一转为失败 payload 通知等待方
        """
        payload: dict
        try:
            result = await self.sandbox.exec_command(session_id, exec_dir, command, timeout)
            payload = {
                "success": result.success,
                "message": result.message or "",
                "data": result.data,
            }
            logger.info(
                f"shell_execute 异步任务完成: task_id={task_id}, "
                f"success={result.success}"
            )
        except asyncio.CancelledError:
            logger.info(f"shell_execute 异步任务被取消: task_id={task_id}")
            payload = {
                "success": False,
                "message": "任务已取消",
                "data": None,
            }
            raise
        except Exception as e:
            logger.exception(
                f"shell_execute 异步任务异常: task_id={task_id}, error={e}"
            )
            payload = {
                "success": False,
                "message": f"命令执行异常: {str(e)}",
                "data": None,
            }
        finally:
            # 通知等待方(无论成功/失败/取消)
            try:
                await self._callback_manager.notify(task_id, payload)
            except Exception as notify_err:
                logger.error(
                    f"shell_execute 异步任务通知失败: task_id={task_id}, "
                    f"error={notify_err}"
                )
            # 清理后台任务追踪表
            self._background_tasks.pop(task_id, None)

    def cancel_background_tasks(self) -> None:
        """取消所有后台异步任务(会话停止时调用)

        同步操作,不等待任务完成。已取消的任务会通过 _run_shell_background
        的 finally 块通知等待方(若存在)。
        """
        if not self._background_tasks:
            return
        for task_id, task in list(self._background_tasks.items()):
            if not task.done():
                task.cancel()
                logger.info(f"取消 shell 异步任务: task_id={task_id}")
        self._background_tasks.clear()

    @tool(
        name="shell_read_output",
        description="查看指定 Shell 会话的内容。用于检查命令执行结果或监控输出。",
        parameters={
            "session_id": {
                "type": "string",
                "description": "目标 Shell 会话的唯一标识符",
            },
        },
        required=["session_id"],
    )
    async def shell_read_output(self, session_id: str) -> ToolResult:
        """根据会话id查看Shell会话内容"""
        return await self.sandbox.read_shell_output(session_id)

    @tool(
        name="shell_wait_process",
        description="等待指定 Shell 会话中正在运行的进程返回。在运行耗时较长的命令后使用。",
        parameters={
            "session_id": {
                "type": "string",
                "description": "目标 Shell 会话的唯一标识符",
            },
            "seconds": {
                "type": "integer",
                "description": "可选参数, 等待时长（秒）",
            }
        },
        required=["session_id"],
    )
    async def shell_wait_process(self, session_id: str, seconds: Optional[int] = None) -> ToolResult:
        """等待指定shell会话中正在运行的进程返回"""
        return await self.sandbox.wait_process(session_id, seconds)

    @tool(
        name="shell_write_input",
        description="向指定 Shell 会话中正在运行的进程写入输入。用于响应交互式命令提示符。",
        parameters={
            "session_id": {
                "type": "string",
                "description": "目标 Shell 会话的唯一标识符",
            },
            "input_text": {
                "type": "string",
                "description": "要写入进程的输入内容",
            },
            "press_enter": {
                "type": "boolean",
                "description": "输入后是否按下回车键",
            }
        },
        required=["session_id", "input_text", "press_enter"],
    )
    async def shell_write_input(
            self,
            session_id: str,
            input_text: str,
            press_enter: str,
    ) -> ToolResult:
        """向指定shell会话正在运行的进程写入输入"""
        return await self.sandbox.write_shell_input(session_id, input_text, press_enter)

    @tool(
        name="shell_kill_process",
        description="在指定 Shell 会话中终止正在运行的进程。用于停止长时间运行的进程或处理卡死的命令。",
        parameters={
            "session_id": {
                "type": "string",
                "description": "目标 Shell 会话的唯一标识符",
            },
        },
        required=["session_id"],
    )
    async def shell_kill_process(self, session_id: str) -> ToolResult:
        """在指定Shell会话中终止正在运行的进程"""
        return await self.sandbox.kill_process(session_id)
