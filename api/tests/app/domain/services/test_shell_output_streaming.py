#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_shell_output_streaming.py
改进B Shell 输出流式单元测试 - 验证 is_streaming 跳过DB、CALLED 完整console、_run_flow 队列合并轮询

测试覆盖:
- _put_and_add_event: is_streaming ToolEvent 跳过 DB 写入,非 streaming 正常写 DB
- _handle_tool_event: shell 流式模式 CALLED 携带完整 console / 非流式模式增量(向后兼容)
- _run_flow: shell CALLING 启动轮询产出 is_streaming 事件、CALLED 取消轮询、flow 异常传播
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.models.app_config import AgentConfig
from app.domain.models.event import ToolEvent, ToolEventStatus, ShellToolContent
from app.domain.models.tool_result import ToolResult
from app.domain.services.agent_task_runner import AgentTaskRunner


# ========== 测试辅助函数 ==========

def _build_runner(stream_shell_output: bool = True) -> AgentTaskRunner:
    """构建 AgentTaskRunner 实例(绕过 __init__,仅设置 shell 流式所需属性)"""
    runner = object.__new__(AgentTaskRunner)
    runner._session_id = "test_session"
    runner._agent_config = AgentConfig(
        max_iterations=10,
        stream_shell_output=stream_shell_output,
    )
    runner._shell_console_sent_count = {}
    runner._sandbox = MagicMock()
    runner._flow = MagicMock()
    # mock 非测试路径的辅助方法,避免依赖真实实现
    runner._sync_message_attachments_to_storage = AsyncMock()
    runner._handle_sandbox_scan_event = AsyncMock()
    runner._sync_file_to_storage = AsyncMock(return_value="file_id")
    return runner


def _mk_uow() -> MagicMock:
    """构造 mock UoW(既是 async context manager 又持有 session.add_event)

    返回的对象满足 `async with uow_factory() as uow:` 语义:
    __aenter__ 返回自身,使测试可直接断言 uow.session.add_event。
    """
    uow = MagicMock()
    uow.session.add_event = AsyncMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=None)
    return uow


async def _collect_events(gen) -> list:
    """收集异步生成器的所有事件"""
    events = []
    async for evt in gen:
        events.append(evt)
    return events


def _mk_console_records(n: int) -> list:
    """构造 n 条 console 记录(ps1/command/output 结构)"""
    return [{"ps1": "$", "command": "ls", "output": f"file{i}.txt"} for i in range(n)]


def _mk_shell_tool_event(status: ToolEventStatus,
                         tool_call_id: str = "c1",
                         session_id: str = "s1",
                         is_streaming: bool = False) -> ToolEvent:
    """构造 shell ToolEvent"""
    return ToolEvent(
        tool_call_id=tool_call_id,
        tool_name="shell",
        function_name="shell_execute",
        function_args={"session_id": session_id},
        status=status,
        is_streaming=is_streaming,
    )


# ========== _put_and_add_event 测试 ==========

class TestPutAndAddEventStreamingTool:
    """测试 is_streaming ToolEvent 跳过 DB 写入(改进B 契约)"""

    @pytest.mark.asyncio
    async def test_streaming_tool_not_written_to_db(self):
        """is_streaming=True 的 ToolEvent 仅入 output_stream,不写 DB"""
        runner = _build_runner()
        runner._uow_factory = MagicMock(return_value=_mk_uow())

        task = MagicMock()
        task.output_stream.put = AsyncMock(return_value="evt_id_1")

        event = _mk_shell_tool_event(ToolEventStatus.CALLING, is_streaming=True)
        await runner._put_and_add_event(task, event)

        task.output_stream.put.assert_called_once()
        # is_streaming=True 提前 return,uow 不应被创建
        runner._uow_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_streaming_tool_written_to_db(self):
        """is_streaming=False 的 ToolEvent 正常写 DB(向后兼容)"""
        uow = _mk_uow()
        runner = _build_runner()
        runner._uow_factory = MagicMock(return_value=uow)

        task = MagicMock()
        task.output_stream.put = AsyncMock(return_value="evt_id_2")

        event = _mk_shell_tool_event(ToolEventStatus.CALLED, is_streaming=False)
        await runner._put_and_add_event(task, event)

        task.output_stream.put.assert_called_once()
        runner._uow_factory.assert_called_once()
        uow.session.add_event.assert_called_once()


# ========== _handle_tool_event shell 分支测试 ==========

class TestHandleToolEventShellStreaming:
    """测试 _handle_tool_event shell 分支流式/非流式行为"""

    @pytest.mark.asyncio
    async def test_streaming_mode_called_carries_full_console(self):
        """stream_shell_output=True 时 CALLED 携带完整 console(非增量)"""
        runner = _build_runner(stream_shell_output=True)
        runner._sandbox.read_shell_output = AsyncMock(
            return_value=ToolResult(success=True, data={"console_records": _mk_console_records(3)})
        )

        event = _mk_shell_tool_event(ToolEventStatus.CALLED)
        await runner._handle_tool_event(event)

        assert event.tool_content is not None
        assert isinstance(event.tool_content, ShellToolContent)
        assert len(event.tool_content.console) == 3, "流式模式 CALLED 应携带完整 console"
        # sent_count 更新为全量长度
        assert runner._shell_console_sent_count["s1"] == 3

    @pytest.mark.asyncio
    async def test_non_streaming_mode_called_carries_incremental(self):
        """stream_shell_output=False 时 CALLED 携带增量 console(向后兼容)"""
        runner = _build_runner(stream_shell_output=False)
        # 预置 sent_count=1,模拟之前已发送1条
        runner._shell_console_sent_count["s1"] = 1
        runner._sandbox.read_shell_output = AsyncMock(
            return_value=ToolResult(success=True, data={"console_records": _mk_console_records(3)})
        )

        event = _mk_shell_tool_event(ToolEventStatus.CALLED)
        await runner._handle_tool_event(event)

        assert event.tool_content is not None
        # 非流式模式: 仅推送新增(sent_count=1 → 3,新增2条)
        assert len(event.tool_content.console) == 2, "非流式模式 CALLED 应携带增量 console"
        assert runner._shell_console_sent_count["s1"] == 3


# ========== _run_flow 队列合并测试 ==========

class TestRunFlowShellPolling:
    """测试 _run_flow 队列合并模式下的 shell 轮询"""

    @pytest.mark.asyncio
    async def test_shell_calling_starts_poll_emits_streaming_events(self, monkeypatch):
        """shell CALLING 启动轮询,中间产出 is_streaming ToolEvent,CALLED 携带完整 console"""
        # 缩短轮询间隔,加速测试(原默认1s会导致测试过慢)
        monkeypatch.setattr(
            "app.domain.services.agent_task_runner._SHELL_POLL_INTERVAL_SECONDS", 0.01
        )
        runner = _build_runner(stream_shell_output=True)

        # read_shell_output 返回递增 records(模拟命令持续输出,最多3条)
        read_count = {"n": 0}
        full_records = _mk_console_records(3)

        async def _mock_read(session_id, console=True):
            read_count["n"] += 1
            return ToolResult(
                success=True,
                data={"console_records": full_records[:min(read_count["n"], 3)]},
            )

        runner._sandbox.read_shell_output = _mock_read

        calling = _mk_shell_tool_event(ToolEventStatus.CALLING)
        called = _mk_shell_tool_event(ToolEventStatus.CALLED)

        async def _mock_flow_invoke(message):
            yield calling
            await asyncio.sleep(0.05)  # 让轮询任务有机会执行
            yield called

        runner._flow.invoke = _mock_flow_invoke

        message = MagicMock()
        message.message = "执行ls命令"

        events = await asyncio.wait_for(
            _collect_events(runner._run_flow(message)), timeout=3.0
        )

        # 中间应有 is_streaming ToolEvent(轮询增量)
        streaming = [
            e for e in events
            if isinstance(e, ToolEvent) and getattr(e, "is_streaming", False)
        ]
        assert len(streaming) >= 1, "应产出 is_streaming 中间轮询事件"
        # CALLED 事件应存在且携带完整 console(3条)
        called_events = [
            e for e in events
            if isinstance(e, ToolEvent) and e.status == ToolEventStatus.CALLED
        ]
        assert len(called_events) == 1
        assert called_events[0].tool_content is not None
        assert len(called_events[0].tool_content.console) == 3, "CALLED 应携带完整 console"

    @pytest.mark.asyncio
    async def test_shell_called_cancels_poll(self, monkeypatch):
        """CALLED 到达后轮询任务被取消(无孤儿任务)"""
        monkeypatch.setattr(
            "app.domain.services.agent_task_runner._SHELL_POLL_INTERVAL_SECONDS", 0.01
        )
        runner = _build_runner(stream_shell_output=True)

        # 用自定义 _poll_shell_console 跟踪是否被 cancel
        poll_cancelled = {"v": False}

        async def _tracking_poll(tool_call_id, shell_session_id, queue):
            try:
                await asyncio.sleep(10)  # 长sleep,等待被cancel
            except asyncio.CancelledError:
                poll_cancelled["v"] = True
                raise

        runner._poll_shell_console = _tracking_poll
        runner._sandbox.read_shell_output = AsyncMock(
            return_value=ToolResult(success=True, data={"console_records": _mk_console_records(1)})
        )

        calling = _mk_shell_tool_event(ToolEventStatus.CALLING)
        called = _mk_shell_tool_event(ToolEventStatus.CALLED)

        async def _mock_flow_invoke(message):
            yield calling
            await asyncio.sleep(0.02)  # 让轮询任务启动
            yield called

        runner._flow.invoke = _mock_flow_invoke

        message = MagicMock()
        message.message = "任务"

        await asyncio.wait_for(_collect_events(runner._run_flow(message)), timeout=3.0)
        # 等待 cancel 生效(CancelledError 传播需要事件循环调度)
        await asyncio.sleep(0.05)

        assert poll_cancelled["v"] is True, "CALLED 到达后轮询任务应被 cancel"

    @pytest.mark.asyncio
    async def test_flow_exception_propagates(self, monkeypatch):
        """flow 抛异常时 _run_flow 重新抛出(finally 块清理轮询任务)"""
        monkeypatch.setattr(
            "app.domain.services.agent_task_runner._SHELL_POLL_INTERVAL_SECONDS", 0.01
        )
        runner = _build_runner(stream_shell_output=True)

        poll_cancelled = {"v": False}

        async def _tracking_poll(tool_call_id, shell_session_id, queue):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                poll_cancelled["v"] = True
                raise

        runner._poll_shell_console = _tracking_poll
        runner._sandbox.read_shell_output = AsyncMock(
            return_value=ToolResult(success=True, data={"console_records": []})
        )

        calling = _mk_shell_tool_event(ToolEventStatus.CALLING)

        async def _mock_flow_invoke(message):
            yield calling
            await asyncio.sleep(0.02)  # 让轮询任务启动
            raise RuntimeError("flow内部异常")

        runner._flow.invoke = _mock_flow_invoke

        message = MagicMock()
        message.message = "任务"

        with pytest.raises(RuntimeError, match="flow内部异常"):
            await asyncio.wait_for(_collect_events(runner._run_flow(message)), timeout=3.0)
        await asyncio.sleep(0.05)

        # 异常路径 finally 块仍应 cancel 轮询任务(无孤儿任务)
        assert poll_cancelled["v"] is True, "异常时 finally 块应取消轮询任务"


# ========== 回归测试: __init__ 必须保存 _agent_config ==========
# 背景: 流式优化引用 self._agent_config.stream_shell_output,但 __init__ 漏存该属性,
# 导致运行期 shell 工具触发 AttributeError 中断会话。现有测试均绕过 __init__
# (object.__new__ + 手动赋值),未能捕获此缺陷。本回归测试直接验证 __init__ 赋值行为。

class TestInitSavesAgentConfig:
    """验证 AgentTaskRunner.__init__ 将 agent_config 保存为 self._agent_config

    复现条件: 必须调用真实 __init__(而非 object.__new__ 绕过),
    才能检测构造函数遗漏的属性赋值。
    """

    def test_init_saves_agent_config_reference(self, monkeypatch):
        """__init__ 后 self._agent_config 应为传入的同一对象"""
        # patch __init__ 内部实例化的复杂依赖,仅验证属性赋值
        monkeypatch.setattr(
            "app.domain.services.agent_task_runner.MCPTool", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.agent_task_runner.A2ATool", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.agent_task_runner.SkillTool", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.agent_task_runner.PlannerReActFlow", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.agent_task_runner.MetricsCollector", MagicMock()
        )

        agent_config = AgentConfig(
            max_iterations=10,
            stream_shell_output=True,
            stream_thinking=True,
        )
        runner = AgentTaskRunner(
            uow_factory=MagicMock(),
            llm=MagicMock(),
            agent_config=agent_config,
            mcp_config=MagicMock(),
            a2a_config=MagicMock(),
            session_id="test_session",
            file_storage=MagicMock(),
            json_parser=MagicMock(),
            browser=MagicMock(),
            search_engine=MagicMock(),
            content_fetcher=MagicMock(),
            sandbox=MagicMock(),
            skill_service=MagicMock(),
        )
        # 核心断言: __init__ 必须保存 agent_config 引用
        assert runner._agent_config is agent_config
        # 流式开关可读(避免 AttributeError)
        assert runner._agent_config.stream_shell_output is True
        assert runner._agent_config.stream_thinking is True

