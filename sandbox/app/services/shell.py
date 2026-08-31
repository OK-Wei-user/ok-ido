#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/11 23:25

@File    : shell.py
"""
import asyncio
import codecs
import getpass
import logging
import os.path
import re
import shlex
import socket
import time
import uuid
from typing import Dict, Optional, List

from app.interfaces.errors.exceptions import (
    BadRequestException,
    AppException,
    NotFoundException,
)
from app.models.shell import (
    Shell,
    ConsoleRecord,
    ShellWaitResult,
    ShellWriteResult,
    ShellKillResult, ShellReadResult, ShellExecuteResult,
)
from app.core.config import get_settings

logger = logging.getLogger(__name__)

_TIMEOUT_EXIT_CODE = 124
_TIMEOUT_KILL_EXIT_CODE = 137
_PROCESS_MONITOR_INTERVAL = 10


class ShellService:
    """Shell命令服务"""
    active_shells: Dict[str, Shell]

    def __init__(self) -> None:
        self.active_shells = {}
        self._monitor_task: Optional[asyncio.Task] = None

    def _ensure_monitor(self) -> None:
        """确保进程超时监控协程正在运行"""
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.create_task(self._process_timeout_monitor())
        logger.info("Shell进程超时监控已启动")

    async def _process_timeout_monitor(self) -> None:
        """后台协程：定期检查所有活跃Shell会话，终止超时进程"""
        while True:
            try:
                await asyncio.sleep(_PROCESS_MONITOR_INTERVAL)
                now = time.time()
                expired_sessions = []

                for session_id, shell in self.active_shells.items():
                    if shell.timeout is None or shell.process.returncode is not None:
                        continue
                    elapsed = now - shell.started_at
                    if elapsed >= shell.timeout:
                        expired_sessions.append((session_id, shell, elapsed))

                for session_id, shell, elapsed in expired_sessions:
                    logger.warning(
                        f"Shell会话[{session_id}]进程超时({elapsed:.0f}s/{shell.timeout}s)，执行终止"
                    )
                    try:
                        shell.process.terminate()
                        try:
                            await asyncio.wait_for(shell.process.wait(), timeout=3)
                        except asyncio.TimeoutError:
                            shell.process.kill()
                            await shell.process.wait()

                        timeout_msg = (
                            f"\n[进程超时终止] 命令执行超过{shell.timeout}秒已被系统自动终止。"
                            f"请优化命令或使用timeout参数设置更长的超时时间。\n"
                        )
                        shell.output += timeout_msg
                        if shell.console_records:
                            shell.console_records[-1].output += timeout_msg
                    except Exception as e:
                        logger.error(f"终止超时进程[{session_id}]失败: {str(e)}")

            except asyncio.CancelledError:
                logger.info("Shell进程超时监控已停止")
                return
            except Exception as e:
                logger.error(f"Shell进程超时监控异常: {str(e)}")

    @classmethod
    def _get_display_path(cls, path: str) -> str:
        """获取显示路径，将~替换成用户主目录"""
        home_dir = os.path.expanduser("~")
        if path.startswith(home_dir):
            return path.replace(home_dir, "~", 1)
        return path

    def _format_ps1(self, exec_dir: str) -> str:
        """格式化命令结构提示"""
        username = getpass.getuser()
        hostname = socket.gethostname()
        display_dir = self._get_display_path(exec_dir)
        return f"{username}@{hostname}:{display_dir} $"

    @classmethod
    async def _create_process(cls, exec_dir: str, command: str) -> asyncio.subprocess.Process:
        """根据传递的执行目录+命令创建一个asyncio管理的子进程"""
        logger.debug(f"在目录 {exec_dir} 下使用命令 {command} 创建一个子进程")
        shell_exec = "/bin/bash"
        return await asyncio.create_subprocess_shell(
            command,
            executable=shell_exec,
            cwd=exec_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )

    async def _start_output_reader(self, session_id: str, process: asyncio.subprocess.Process) -> None:
        """启动协程以连续读取进程输出并将其存储到会话中"""
        logger.debug(f"正在启用会话输出读取器: {session_id}")
        encoding = "utf-8"
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        shell = self.active_shells.get(session_id)

        while True:
            if process.stdout:
                try:
                    buffer = await process.stdout.read(4096)
                    if not buffer:
                        break
                    output = decoder.decode(buffer, final=False)
                    if shell:
                        shell.output += output
                        if shell.console_records:
                            shell.console_records[-1].output += output
                except Exception as e:
                    logger.error(f"读取进程输出时错误: {str(e)}")
                    break
            else:
                break

        logger.debug(f"会话 {session_id} 的输出读取器已完成")

    @classmethod
    def _remove_ansi_escape_codes(cls, text: str) -> str:
        """从文本中删除ANSI转义字符"""
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub("", text)

    @classmethod
    def create_session_id(cls) -> str:
        """创建会话id"""
        session_id = str(uuid.uuid4())
        logger.info(f"创建一个新的Shell会话ID: {session_id}")
        return session_id

    def _resolve_timeout(self, timeout: Optional[int] = None) -> int:
        """解析超时参数，确保在合法范围内"""
        settings = get_settings()
        default_timeout = settings.shell_default_timeout
        max_timeout = settings.shell_max_timeout

        if timeout is None or timeout <= 0:
            return default_timeout
        return min(timeout, max_timeout)

    def get_console_records(self, session_id: str) -> List[ConsoleRecord]:
        """从指定会话中获取控制台记录"""
        logger.debug(f"正在获取Shell会话的控制台记录: {session_id}")
        if session_id not in self.active_shells:
            logger.error(f"Shell会话不存在: {session_id}")
            raise NotFoundException(f"Shell会话不存在: {session_id}")

        console_records = self.active_shells[session_id].console_records
        clean_console_records = []

        for console_record in console_records:
            clean_console_records.append(ConsoleRecord(
                ps1=console_record.ps1,
                command=console_record.command,
                output=self._remove_ansi_escape_codes(console_record.output),
            ))

        return clean_console_records

    async def wait_process(self, session_id: str, seconds: Optional[int] = None) -> ShellWaitResult:
        """传递会话id+时间，等待子进程结束"""
        logger.debug(f"正在Shell会话中等待进程: {session_id}, 超时: {seconds}s")
        if session_id not in self.active_shells:
            logger.error(f"Shell会话不存在: {session_id}")
            raise NotFoundException(f"Shell会话不存在: {session_id}")

        shell = self.active_shells[session_id]
        process = shell.process

        try:
            seconds = 60 if seconds is None or seconds <= 0 else seconds
            await asyncio.wait_for(process.wait(), timeout=seconds)

            logger.info(f"进程已完成, 返回代码为: {process.returncode}")

            if process.returncode == _TIMEOUT_EXIT_CODE:
                timeout_msg = (
                    f"\n[命令超时] 命令因超时被终止(退出码124)，"
                    f"请优化命令执行效率或通过timeout参数设置更长的超时时间。\n"
                )
                shell.output += timeout_msg
                if shell.console_records:
                    shell.console_records[-1].output += timeout_msg

            return ShellWaitResult(returncode=process.returncode)
        except asyncio.TimeoutError:
            logger.warning(f"Shell会话进程等待超时: {seconds}s")
            raise BadRequestException(f"Shell会话进程等待超时: {seconds}s")
        except Exception as e:
            logger.error(f"Shell会话进程等待过程出错: {str(e)}")
            raise AppException(f"Shell会话进程等待过程出错: {str(e)}")

    async def read_shell_output(self, session_id: str, console: bool = False) -> ShellReadResult:
        """根据传递的会话id+是否输出控制台记录获取Shell命令结果"""
        logger.debug(f"查看Shell会话内容: {session_id}")
        if session_id not in self.active_shells:
            logger.error(f"Shell会话不存在: {session_id}")
            raise NotFoundException(f"Shell会话不存在: {session_id}")

        shell = self.active_shells[session_id]

        raw_output = shell.output
        clean_output = self._remove_ansi_escape_codes(raw_output)

        if console:
            console_records = self.get_console_records(session_id)
        else:
            console_records = []

        return ShellReadResult(
            session_id=session_id,
            output=clean_output,
            console_records=console_records,
        )

    async def exec_command(
            self,
            session_id: str,
            exec_dir: Optional[str],
            command: str,
            timeout: Optional[int] = None,
    ) -> ShellExecuteResult:
        """传递会话id+执行目录+命令在沙箱中执行后返回"""
        logger.info(f"正在会话 {session_id} 中执行命令: {command}, 超时: {timeout}s")
        if not exec_dir or exec_dir == "":
            exec_dir = os.path.expanduser("~")
        if not os.path.exists(exec_dir):
            logger.error(f"当前目录不存在: {exec_dir}")
            raise BadRequestException(f"当前目录不存在: {exec_dir}")

        resolved_timeout = self._resolve_timeout(timeout)

        try:
            ps1 = self._format_ps1(exec_dir)

            wrapped_command = f"timeout {resolved_timeout} bash -c {shlex.quote(command)}"

            if session_id not in self.active_shells:
                logger.debug(f"创建一个新的Shell会话: {session_id}")
                process = await self._create_process(exec_dir, wrapped_command)
                self.active_shells[session_id] = Shell(
                    process=process,
                    exec_dir=exec_dir,
                    output="",
                    console_records=[ConsoleRecord(ps1=ps1, command=command, output="")],
                    started_at=time.time(),
                    timeout=resolved_timeout,
                )

                await asyncio.create_task(self._start_output_reader(session_id, process))
            else:
                logger.debug(f"使用现有的Shell会话: {session_id}")
                shell = self.active_shells[session_id]
                old_process = shell.process

                if old_process.returncode is None:
                    logger.debug(f"正在终止会话中的上一个进程: {session_id}")
                    try:
                        old_process.terminate()
                        await asyncio.wait_for(old_process.wait(), timeout=1)
                    except Exception as e:
                        logger.warning(f"强制终止Shell会话中的进程 {session_id} 失败: {str(e)}")
                        old_process.kill()

                process = await self._create_process(exec_dir, wrapped_command)

                shell.process = process
                shell.exec_dir = exec_dir
                shell.output = ""
                shell.console_records.append(ConsoleRecord(ps1=ps1, command=command, output=""))
                shell.started_at = time.time()
                shell.timeout = resolved_timeout

                await asyncio.create_task(self._start_output_reader(session_id, process))

            self._ensure_monitor()

            try:
                logger.debug(f"正在等待会话中的进程完成: {session_id}")
                wait_result = await self.wait_process(session_id, seconds=5)

                if wait_result.returncode is not None:
                    logger.debug(f"Shell会话进程已结束, 代码: {wait_result.returncode}")
                    view_result = await self.read_shell_output(session_id)

                    return ShellExecuteResult(
                        session_id=session_id,
                        command=command,
                        status="completed",
                        returncode=wait_result.returncode,
                        output=view_result.output,
                    )
            except BadRequestException as _:
                logger.warning(f"进程在会话超时后仍在运行: {session_id}")
                pass
            except Exception as e:
                logger.warning(f"等待进程时出现异常: {str(e)}")
                pass

            return ShellExecuteResult(
                session_id=session_id,
                command=command,
                status="running",
            )
        except Exception as e:
            logger.error(f"命令执行失败: {str(e)}", exc_info=True)
            raise AppException(
                msg=f"命令执行失败: {str(e)}",
                data={"session_id": session_id, "command": command}
            )

    async def write_shell_input(
            self,
            session_id: str,
            input_text: str,
            press_enter: bool
    ) -> ShellWriteResult:
        """根据传递的数据向指定子进程写入数据"""
        logger.debug(f"写入Shell会话中的子进程: {session_id}, 是否按下回车键: {press_enter}")
        if session_id not in self.active_shells:
            logger.error(f"Shell会话不存在: {session_id}")
            raise NotFoundException(f"Shell会话不存在: {session_id}")

        shell = self.active_shells[session_id]
        process = shell.process

        try:
            if process.returncode is not None:
                logger.error(f"子进程已结束, 无法写入输入: {session_id}")
                raise BadRequestException("子进程已结束, 无法写入输入")

            encoding = "utf-8"
            line_ending = "\n"

            text_to_send = input_text
            if press_enter:
                text_to_send += line_ending

            input_data = text_to_send.encode(encoding)

            log_text = input_text + ("\n" if press_enter else "")
            shell.output += log_text
            if shell.console_records:
                shell.console_records[-1].output += log_text

            process.stdin.write(input_data)
            await process.stdin.drain()

            logger.info("成功向子进程写入数据")
            return ShellWriteResult(status="success")
        except UnicodeError as e:
            logger.error(f"编码错误: {str(e)}")
            raise AppException(f"编码错误: {str(e)}")
        except Exception as e:
            logger.error(f"向子进程写入数据出错: {str(e)}")
            raise AppException(f"向子进程写入数据出错: {str(e)}")

    async def kill_process(self, session_id: str) -> ShellKillResult:
        """根据传递的Shell会话id关闭对应进程"""
        logger.debug(f"正在终止会话中的进程: {session_id}")
        if session_id not in self.active_shells:
            logger.error(f"Shell会话不存在: {session_id}")
            raise NotFoundException(f"Shell会话不存在: {session_id}")

        shell = self.active_shells[session_id]
        process = shell.process

        try:
            if process.returncode is None:
                logger.info(f"尝试优雅终止进程: {session_id}")
                process.terminate()

                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError as _:
                    logger.warning(f"尝试强制关闭进程: {session_id}")
                    process.kill()

                logger.info(f"进程已终止, 返回代码为: {process.returncode}")
                return ShellKillResult(status="terminated", returncode=process.returncode)
            else:
                logger.info(f"进程已终止, 返回代码为: {process.returncode}")
                return ShellKillResult(status="already_terminated", returncode=process.returncode)
        except Exception as e:
            logger.error(f"关闭进程失败: {str(e)}", exc_info=True)
            raise AppException(f"关闭进程失败: {str(e)}")
