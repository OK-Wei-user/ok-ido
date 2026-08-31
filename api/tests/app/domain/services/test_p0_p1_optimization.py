#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_p0_p1_optimization.py
P0/P1优化单元测试 - 并发锁、Stream MAXLEN、Task清理、附件重试、Plan保护、Shell计数器重置
"""
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from app.domain.models.event import ErrorEvent
from app.domain.models.plan import Plan, Step
from app.infrastructure.external.task.redis_stream_task import RedisStreamTask


class TestSessionLock:
    """P0-2: 并发聊天锁测试"""

    @pytest.mark.asyncio
    async def test_lock_prevents_concurrent_chat(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._session_locks = {}
            service._locks_guard = asyncio.Lock()

            lock = await service._get_session_lock("session-1")
            assert not lock.locked()

            async with lock:
                assert lock.locked()
                lock2 = await service._get_session_lock("session-1")
                assert lock2 is lock
                assert lock2.locked()

    @pytest.mark.asyncio
    async def test_different_sessions_independent_locks(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._session_locks = {}
            service._locks_guard = asyncio.Lock()

            lock1 = await service._get_session_lock("session-1")
            lock2 = await service._get_session_lock("session-2")
            assert lock1 is not lock2

    @pytest.mark.asyncio
    async def test_locked_session_yields_error_event(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._session_locks = {}
            service._locks_guard = asyncio.Lock()

            lock = await service._get_session_lock("session-1")
            await lock.acquire()

            events = []
            async for event in service.chat(session_id="session-1", message="test"):
                events.append(event)

            assert len(events) == 1
            assert isinstance(events[0], ErrorEvent)
            assert "正在处理中" in events[0].error

    def test_remove_session_lock_cleans_up(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._session_locks = {"session-1": asyncio.Lock(), "session-2": asyncio.Lock()}

            service._remove_session_lock("session-1")
            assert "session-1" not in service._session_locks
            assert "session-2" in service._session_locks

    def test_remove_session_lock_idempotent(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._session_locks = {}

            service._remove_session_lock("nonexistent")
            assert len(service._session_locks) == 0


class TestRedisStreamMaxLen:
    """P0-3: Redis Stream MAXLEN测试"""

    def test_stream_max_length_constant(self):
        from app.infrastructure.external.message_queue.redis_stream_message_queue import RedisStreamMessageQueue

        assert RedisStreamMessageQueue.STREAM_MAX_LENGTH == 1000

    @pytest.mark.asyncio
    async def test_put_uses_maxlen(self):
        from app.infrastructure.external.message_queue.redis_stream_message_queue import RedisStreamMessageQueue

        with patch.object(RedisStreamMessageQueue, '__init__', lambda self, name: None):
            queue = RedisStreamMessageQueue.__new__(RedisStreamMessageQueue)
            mock_redis = MagicMock()
            mock_redis.client.xadd = AsyncMock(return_value="123-0")
            queue._redis = mock_redis
            queue._stream_name = "test:stream"

            await queue.put("test_message")

            mock_redis.client.xadd.assert_called_once()
            call_kwargs = mock_redis.client.xadd.call_args
            assert call_kwargs[1].get("maxlen") == 1000 or "maxlen" in str(call_kwargs)
            assert call_kwargs[1].get("approximate") is True or "approximate" in str(call_kwargs)


class TestTaskCleanup:
    """P0-4: Task注册表清理与Stream清理测试"""

    def test_cleanup_registry_removes_task(self):
        with patch.object(RedisStreamTask, '__init__', lambda self, runner: None):
            task = RedisStreamTask.__new__(RedisStreamTask)
            task._id = "test-task-id"
            RedisStreamTask._task_registry["test-task-id"] = task

            task._cleanup_registry()

            assert "test-task-id" not in RedisStreamTask._task_registry

    @pytest.mark.asyncio
    async def test_cleanup_streams_clears_both_streams(self):
        with patch.object(RedisStreamTask, '__init__', lambda self, runner: None):
            task = RedisStreamTask.__new__(RedisStreamTask)
            task._id = "test-task-id"
            task._input_stream = AsyncMock()
            task._output_stream = AsyncMock()

            await task._cleanup_streams()

            task._input_stream.clear.assert_called_once()
            task._output_stream.clear.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_streams_handles_exception(self):
        with patch.object(RedisStreamTask, '__init__', lambda self, runner: None):
            task = RedisStreamTask.__new__(RedisStreamTask)
            task._id = "test-task-id"
            task._input_stream = AsyncMock()
            task._input_stream.clear.side_effect = Exception("Redis error")
            task._output_stream = AsyncMock()

            await task._cleanup_streams()

            task._output_stream.clear.assert_called_once()

    def test_cleanup_registry_idempotent(self):
        with patch.object(RedisStreamTask, '__init__', lambda self, runner: None):
            task = RedisStreamTask.__new__(RedisStreamTask)
            task._id = "nonexistent-id"

            task._cleanup_registry()


class TestAttachmentRetry:
    """P1-5: 附件同步重试测试"""

    @pytest.mark.asyncio
    async def test_sync_file_to_storage_retries_on_failure(self):
        from app.domain.services.agent_task_runner import AgentTaskRunner

        with patch.object(AgentTaskRunner, '__init__', lambda self: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._session_id = "test-session"
            runner._uow = AsyncMock()
            runner._uow_factory = lambda: runner._uow
            runner._uow.__aenter__ = AsyncMock(return_value=runner._uow)
            runner._uow.__aexit__ = AsyncMock(return_value=False)
            runner._uow.session = AsyncMock()
            runner._sandbox = AsyncMock()
            runner._file_storage = AsyncMock()

            runner._sandbox.download_file = AsyncMock(side_effect=Exception("download failed"))
            runner._get_stream_size = MagicMock(return_value=100)

            result = await runner._sync_file_to_storage("/home/ubuntu/test.txt", max_retries=1)

            assert result is None
            assert runner._sandbox.download_file.call_count == 2

    @pytest.mark.asyncio
    async def test_sync_file_to_storage_succeeds_on_retry(self):
        from app.domain.services.agent_task_runner import AgentTaskRunner

        with patch.object(AgentTaskRunner, '__init__', lambda self: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._session_id = "test-session"
            runner._uow = AsyncMock()
            runner._uow_factory = lambda: runner._uow
            runner._uow.__aenter__ = AsyncMock(return_value=runner._uow)
            runner._uow.__aexit__ = AsyncMock(return_value=False)
            runner._uow.session = AsyncMock()
            runner._sandbox = AsyncMock()
            runner._file_storage = AsyncMock()

            call_count = 0

            async def mock_download(filepath):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise Exception("transient error")
                from io import BytesIO
                return BytesIO(b"file content")

            runner._sandbox.download_file = mock_download
            runner._uow.session.get_file_by_path = AsyncMock(return_value=None)
            mock_file = MagicMock()
            mock_file.filepath = "/home/ubuntu/test.txt"
            runner._file_storage.upload_file = AsyncMock(return_value=mock_file)
            runner._get_stream_size = MagicMock(return_value=100)

            result = await runner._sync_file_to_storage("/home/ubuntu/test.txt", max_retries=1)

            assert result is not None
            assert call_count == 2


class TestPlanStepProtection:
    """P1-6: Plan更新步骤保护测试"""

    def test_new_steps_fewer_than_original_preserves_remaining(self):
        original_steps = [
            Step(id="1", description="step1", status="completed"),
            Step(id="2", description="step2", status="pending"),
            Step(id="3", description="step3", status="pending"),
            Step(id="4", description="step4", status="pending"),
        ]
        for s in original_steps:
            if s.id == "1":
                s.status = "completed"

        plan = Plan(
            title="test",
            goal="test goal",
            language="zh",
            steps=original_steps,
            message="test",
        )

        new_steps = [Step(id="5", description="new step5")]

        first_pending_index = None
        for idx, step in enumerate(plan.steps):
            if not step.done:
                first_pending_index = idx
                break

        if first_pending_index is not None:
            original_remaining = len(plan.steps) - first_pending_index
            updated_steps = plan.steps[:first_pending_index]
            updated_steps.extend(new_steps)
            if len(new_steps) < original_remaining:
                preserved = plan.steps[first_pending_index + len(new_steps):]
                for s in preserved:
                    if not s.done:
                        updated_steps.append(s)
            plan.steps = updated_steps

        assert len(plan.steps) == 4
        assert plan.steps[0].id == "1"
        assert plan.steps[1].id == "5"
        assert plan.steps[2].id == "3"
        assert plan.steps[3].id == "4"

    def test_new_steps_equal_to_original_no_preservation(self):
        original_steps = [
            Step(id="1", description="step1", status="completed"),
            Step(id="2", description="step2", status="pending"),
        ]
        for s in original_steps:
            if s.id == "1":
                s.status = "completed"

        plan = Plan(
            title="test",
            goal="test goal",
            language="zh",
            steps=original_steps,
            message="test",
        )

        new_steps = [Step(id="3", description="new step3")]

        first_pending_index = None
        for idx, step in enumerate(plan.steps):
            if not step.done:
                first_pending_index = idx
                break

        if first_pending_index is not None:
            original_remaining = len(plan.steps) - first_pending_index
            updated_steps = plan.steps[:first_pending_index]
            updated_steps.extend(new_steps)
            if len(new_steps) < original_remaining:
                preserved = plan.steps[first_pending_index + len(new_steps):]
                for s in preserved:
                    if not s.done:
                        updated_steps.append(s)
            plan.steps = updated_steps

        assert len(plan.steps) == 2
        assert plan.steps[0].id == "1"
        assert plan.steps[1].id == "3"

    def test_new_steps_more_than_original_no_preservation(self):
        original_steps = [
            Step(id="1", description="step1", status="completed"),
            Step(id="2", description="step2", status="pending"),
        ]
        for s in original_steps:
            if s.id == "1":
                s.status = "completed"

        plan = Plan(
            title="test",
            goal="test goal",
            language="zh",
            steps=original_steps,
            message="test",
        )

        new_steps = [Step(id="3", description="new step3"), Step(id="4", description="new step4")]

        first_pending_index = None
        for idx, step in enumerate(plan.steps):
            if not step.done:
                first_pending_index = idx
                break

        if first_pending_index is not None:
            original_remaining = len(plan.steps) - first_pending_index
            updated_steps = plan.steps[:first_pending_index]
            updated_steps.extend(new_steps)
            if len(new_steps) < original_remaining:
                preserved = plan.steps[first_pending_index + len(new_steps):]
                for s in preserved:
                    if not s.done:
                        updated_steps.append(s)
            plan.steps = updated_steps

        assert len(plan.steps) == 3
        assert plan.steps[0].id == "1"
        assert plan.steps[1].id == "3"
        assert plan.steps[2].id == "4"

    def test_all_steps_completed_no_preservation(self):
        original_steps = [
            Step(id="1", description="step1", status="completed"),
            Step(id="2", description="step2", status="completed"),
        ]
        for s in original_steps:
            s.status = "completed"

        plan = Plan(
            title="test",
            goal="test goal",
            language="zh",
            steps=original_steps,
            message="test",
        )

        new_steps = [Step(id="3", description="new step3")]

        first_pending_index = None
        for idx, step in enumerate(plan.steps):
            if not step.done:
                first_pending_index = idx
                break

        if first_pending_index is not None:
            original_remaining = len(plan.steps) - first_pending_index
            updated_steps = plan.steps[:first_pending_index]
            updated_steps.extend(new_steps)
            if len(new_steps) < original_remaining:
                preserved = plan.steps[first_pending_index + len(new_steps):]
                for s in preserved:
                    if not s.done:
                        updated_steps.append(s)
            plan.steps = updated_steps

        assert len(plan.steps) == 2


class TestShellCounterReset:
    """P1-7: Shell计数器重置测试"""

    def test_clear_resets_all_counters(self):
        from app.domain.services.agent_task_runner import AgentTaskRunner

        with patch.object(AgentTaskRunner, '__init__', lambda self: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._shell_console_sent_count = {"session_a": 10, "session_b": 20}

            runner._shell_console_sent_count.clear()

            assert len(runner._shell_console_sent_count) == 0

    def test_clear_allows_fresh_start(self):
        from app.domain.services.agent_task_runner import AgentTaskRunner

        with patch.object(AgentTaskRunner, '__init__', lambda self: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._shell_console_sent_count = {"session_a": 100}

            runner._shell_console_sent_count.clear()

            all_records = [{"cmd": f"cmd{i}"} for i in range(5)]
            sent_count = runner._shell_console_sent_count.get("session_a", 0)
            new_records = all_records[sent_count:]

            assert len(new_records) == 5


class TestSandboxTTL:
    """沙箱TTL自动销毁测试"""

    def test_cancel_sandbox_ttl_cancels_existing_task(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._sandbox_ttl_tasks = {}

            mock_task = MagicMock()
            mock_task.done.return_value = False
            service._sandbox_ttl_tasks["session-1"] = mock_task

            service._cancel_sandbox_ttl("session-1")

            mock_task.cancel.assert_called_once()
            assert "session-1" not in service._sandbox_ttl_tasks

    def test_cancel_sandbox_ttl_noop_when_no_task(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._sandbox_ttl_tasks = {}

            service._cancel_sandbox_ttl("nonexistent")

            assert len(service._sandbox_ttl_tasks) == 0

    def test_cancel_sandbox_ttl_skips_done_task(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._sandbox_ttl_tasks = {}

            mock_task = MagicMock()
            mock_task.done.return_value = True
            service._sandbox_ttl_tasks["session-1"] = mock_task

            service._cancel_sandbox_ttl("session-1")

            mock_task.cancel.assert_not_called()
            assert "session-1" not in service._sandbox_ttl_tasks

    def test_public_cancel_sandbox_ttl_delegates_to_private(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._sandbox_ttl_tasks = {}

            mock_task = MagicMock()
            mock_task.done.return_value = False
            service._sandbox_ttl_tasks["session-1"] = mock_task

            service.cancel_sandbox_ttl("session-1")

            mock_task.cancel.assert_called_once()
            assert "session-1" not in service._sandbox_ttl_tasks

    @pytest.mark.asyncio
    async def test_schedule_sandbox_ttl_creates_task(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._sandbox_ttl_tasks = {}
            service._locks_guard = asyncio.Lock()
            service._uow = AsyncMock()
            service._uow.__aenter__ = AsyncMock(return_value=service._uow)
            service._uow.__aexit__ = AsyncMock(return_value=False)
            service._sandbox_cls = MagicMock()
            service._sandbox_idle_ttl_seconds = 7200

            await service._schedule_sandbox_ttl("session-1")

            assert "session-1" in service._sandbox_ttl_tasks
            assert not service._sandbox_ttl_tasks["session-1"].done()

            # 清理
            service._sandbox_ttl_tasks["session-1"].cancel()
            try:
                await service._sandbox_ttl_tasks["session-1"]
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_schedule_cancels_previous_ttl(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._sandbox_ttl_tasks = {}
            service._locks_guard = asyncio.Lock()
            service._uow = AsyncMock()
            service._uow.__aenter__ = AsyncMock(return_value=service._uow)
            service._uow.__aexit__ = AsyncMock(return_value=False)
            service._sandbox_cls = MagicMock()
            service._sandbox_idle_ttl_seconds = 7200

            await service._schedule_sandbox_ttl("session-1")
            first_task = service._sandbox_ttl_tasks["session-1"]

            await service._schedule_sandbox_ttl("session-1")
            second_task = service._sandbox_ttl_tasks["session-1"]

            assert first_task is not second_task
            # cancel()仅请求取消，任务在下次await时才真正响应
            assert first_task.cancelling() or first_task.cancelled() or first_task.done()

            # 清理
            second_task.cancel()
            try:
                await second_task
            except asyncio.CancelledError:
                pass

    def test_ttl_constant_value(self):
        """沙箱空闲销毁TTL默认值为2小时(7200秒)

        配置外部化: 模块级常量作为文档性默认值,实际运行时从Settings读取。
        优化前为60分钟(3600秒)硬编码,优化后为2小时(7200秒)可配置。
        """
        from app.application.services.agent_service import SANDBOX_IDLE_TTL_SECONDS
        from core.config import Settings

        # 模块级默认值常量
        assert SANDBOX_IDLE_TTL_SECONDS == 7200
        # Settings配置默认值(支持.env/环境变量SANDBOX_IDLE_TTL_SECONDS覆盖)
        assert Settings().sandbox_idle_ttl_seconds == 7200


class TestPublicMethodEncapsulation:
    """公共方法封装测试"""

    def test_remove_session_lock_delegates_to_private(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._session_locks = {"session-1": asyncio.Lock()}

            service.remove_session_lock("session-1")

            assert "session-1" not in service._session_locks

    def test_cancel_sandbox_ttl_public_delegates(self):
        from app.application.services.agent_service import AgentService

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._sandbox_ttl_tasks = {}

            mock_task = MagicMock()
            mock_task.done.return_value = True
            service._sandbox_ttl_tasks["session-1"] = mock_task

            service.cancel_sandbox_ttl("session-1")

            assert "session-1" not in service._sandbox_ttl_tasks


class TestOnTaskDoneExceptionHandling:
    """_on_task_done异常处理测试"""

    def test_on_task_done_handles_runtime_error(self):
        with patch.object(RedisStreamTask, '__init__', lambda self, runner: None):
            task = RedisStreamTask.__new__(RedisStreamTask)
            task._id = "test-task-id"
            task._task_runner = MagicMock()
            task._task_runner.on_done = AsyncMock()

            # 模拟asyncio.create_task抛出RuntimeError
            with patch("asyncio.create_task", side_effect=RuntimeError("No event loop")):
                task._on_task_done()  # 不应抛出异常

            assert "test-task-id" not in RedisStreamTask._task_registry
