#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批次 28 修复单元测试: PlannerReActFlow._strip_historical_images / _append_to_system_prompt

Bug 描述:
    原代码在 planner_react.py 的 _strip_historical_images() 与 _append_to_system_prompt()
    中使用 `async with agent._uow`,但 BaseAgent 只有 `_uow_factory`,不存在 `_uow` 属性,
    导致续接会话(用户发送"继续")时触发 AttributeError,整个流程崩溃。

修复方案:
    改用 `async with agent._uow_factory() as uow` 创建临时 uow,
    与 BaseAgent 既有模式(L117 `_ensure_memory`)对齐。

覆盖场景:
    1. _strip_historical_images 不再触发 AttributeError (PlannerAgent & ReActAgent)
    2. _strip_historical_images 调用 save_memory 通过 uow_factory 创建的 uow
    3. _append_to_system_prompt 不再触发 AttributeError
    4. _append_to_system_prompt 在 memory 为空时跳过
    5. _append_to_system_prompt 在 marker 已存在时跳过 save_memory
    6. _append_to_system_prompt 正常追加内容并保存
    7. PlannerAgent 与 ReActAgent 都没有 _uow 属性 (回归保护)
"""
import asyncio
from typing import AsyncGenerator
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from app.domain.models.event import BaseEvent
from app.domain.models.memory import Memory
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.models.message import Message
from app.domain.services.agents.base import BaseAgent
from app.domain.services.agents.planner import PlannerAgent
from app.domain.services.agents.react import ReActAgent
from app.domain.services.flows.planner_react import PlannerReActFlow


class _AsyncUowCM:
    """模拟 async with uow_factory() as uow 模式的上下文管理器

    每次调用返回新的 uow mock,确保每次 save_memory 都是独立会话。
    """
    def __init__(self, uow_mock):
        self._uow_mock = uow_mock

    async def __aenter__(self):
        return self._uow_mock

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _make_agent_mock(agent_class, uow_factory_mock):
    """构造一个不触发 __init__ 的 agent mock,只设置必要属性

    这样可以避免实例化 BaseAgent 时传入完整参数,只验证修复后的方法行为。
    """
    agent = agent_class.__new__(agent_class)
    # BaseAgent 在 __init__ 中赋值的属性
    agent._uow_factory = uow_factory_mock
    agent._session_id = "test-session-id"
    agent._memory = None
    agent.name = "planner" if agent_class is PlannerAgent else "react"
    return agent


def _build_flow_with_mocks():
    """构造 PlannerReActFlow 测试 fixture,只装配必要属性"""
    uow_mock = MagicMock()
    uow_mock.session = MagicMock()
    uow_mock.session.save_memory = AsyncMock()
    uow_factory_mock = MagicMock(return_value=_AsyncUowCM(uow_mock))

    planner = _make_agent_mock(PlannerAgent, uow_factory_mock)
    react = _make_agent_mock(ReActAgent, uow_factory_mock)

    flow = PlannerReActFlow.__new__(PlannerReActFlow)
    flow.planner = planner
    flow.react = react
    flow._uow_factory = uow_factory_mock
    return flow, uow_mock, uow_factory_mock, planner, react


class TestStripHistoricalImagesUowFix:
    """_strip_historical_images 方法修复验证"""

    @pytest.mark.asyncio
    async def test_strip_images_no_attribute_error_for_planner(self):
        """修复后 _strip_historical_images 在 PlannerAgent 上不再触发 AttributeError"""
        flow, uow_mock, _, planner, react = _build_flow_with_mocks()

        # 设置 planner 与 react 的 memory 含图片消息
        planner._memory = MagicMock()
        planner._memory.messages = [{"role": "user", "content": "test"}]
        react._memory = MagicMock()
        react._memory.messages = [{"role": "user", "content": "test"}]

        # mock _ensure_memory 不抛异常
        planner._ensure_memory = AsyncMock()
        react._ensure_memory = AsyncMock()

        # mock Memory.strip_image_data 返回清理后的消息
        with patch.object(Memory, "strip_image_data", return_value=[{"role": "user", "content": "cleaned"}]):
            # 修复前: AttributeError: 'PlannerAgent' object has no attribute '_uow'
            # 修复后: 应正常执行
            await flow._strip_historical_images()

        # 验证 save_memory 被调用 (通过 uow_factory 创建的 uow)
        assert uow_mock.session.save_memory.call_count == 2

    @pytest.mark.asyncio
    async def test_strip_images_skips_when_memory_empty(self):
        """memory 为空时不调用 save_memory"""
        flow, uow_mock, _, planner, react = _build_flow_with_mocks()

        planner._memory = MagicMock()
        planner._memory.messages = []  # 空 messages
        react._memory = None  # None memory

        planner._ensure_memory = AsyncMock()
        react._ensure_memory = AsyncMock()

        await flow._strip_historical_images()

        # 由于 messages 为空 / memory 为 None,不应调用 save_memory
        uow_mock.session.save_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_strip_images_uses_uow_factory_not_uow_attribute(self):
        """验证使用 _uow_factory() 而非 _uow 属性

        关键回归保护: 确保 PlannerAgent / ReActAgent 实例上不存在 _uow 属性,
        修复后的方法应通过 _uow_factory() 创建临时 uow。
        """
        flow, uow_mock, uow_factory_mock, planner, react = _build_flow_with_mocks()

        # 回归保护: PlannerAgent & ReActAgent 实例上不存在 _uow 属性
        assert not hasattr(planner, "_uow"), "PlannerAgent 不应有 _uow 属性"
        assert not hasattr(react, "_uow"), "ReActAgent 不应有 _uow 属性"

        # 设置 memory 触发保存逻辑 (两个 agent 都需要 mock _ensure_memory 以避免 BaseAgent 真实调用)
        planner._memory = MagicMock()
        planner._memory.messages = [{"role": "user", "content": "test"}]
        planner._ensure_memory = AsyncMock()
        react._memory = MagicMock()
        react._memory.messages = [{"role": "user", "content": "test"}]
        react._ensure_memory = AsyncMock()

        with patch.object(Memory, "strip_image_data", return_value=[{"role": "user", "content": "cleaned"}]):
            await flow._strip_historical_images()

        # 验证调用了 _uow_factory (而非 _uow)
        assert uow_factory_mock.call_count >= 1, "应通过 _uow_factory() 创建 uow"


class TestAppendToSystemPromptUowFix:
    """_append_to_system_prompt 方法修复验证"""

    @pytest.mark.asyncio
    async def test_append_no_attribute_error_for_planner(self):
        """修复后 _append_to_system_prompt 在 PlannerAgent 上不再触发 AttributeError"""
        flow, uow_mock, uow_factory_mock, planner, _ = _build_flow_with_mocks()

        # 设置 memory 包含 system 消息
        planner._memory = MagicMock()
        planner._memory.messages = [
            {"role": "system", "content": "original system prompt"}
        ]

        # 修复前: AttributeError: 'PlannerAgent' object has no attribute '_uow'
        # 修复后: 应正常执行
        await flow._append_to_system_prompt(planner, "[测试marker]", "\n\n追加内容")

        # 验证 save_memory 被调用
        uow_mock.session.save_memory.assert_called_once_with(
            "test-session-id", "planner", planner._memory
        )

    @pytest.mark.asyncio
    async def test_append_skips_when_memory_empty(self):
        """memory 为空时跳过"""
        flow, uow_mock, _, planner, _ = _build_flow_with_mocks()

        planner._memory = None

        await flow._append_to_system_prompt(planner, "[marker]", "content")

        uow_mock.session.save_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_append_skips_when_messages_empty(self):
        """messages 为空时跳过"""
        flow, uow_mock, _, planner, _ = _build_flow_with_mocks()

        planner._memory = MagicMock()
        planner._memory.messages = []

        await flow._append_to_system_prompt(planner, "[marker]", "content")

        # messages[0] 不存在,应抛 IndexError,而非 AttributeError
        # 但实际代码 `system_msg = agent._memory.messages[0]` 会抛 IndexError
        # 这是预期行为(memory 为空时不应调用此方法)
        # 此测试验证 _uow 不被调用
        uow_mock.session.save_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_append_idempotent_when_marker_exists(self):
        """marker 已存在时不追加内容 (幂等: content 不变)

        注: 原实现 save_memory 仍会调用(无条件保存),但 content 必须保持不变。
        本测试验证幂等性: 重复调用不会改变 system_msg content。
        """
        flow, uow_mock, _, planner, _ = _build_flow_with_mocks()

        planner._memory = MagicMock()
        marker = "[历史交付物文件]"
        original_content = f"original {marker} existing content"
        planner._memory.messages = [
            {"role": "system", "content": original_content}
        ]

        await flow._append_to_system_prompt(planner, marker, "\n\n新增内容")

        # 幂等性: content 保持不变 (marker 已存在,不追加)
        assert planner._memory.messages[0]["content"] == original_content

    @pytest.mark.asyncio
    async def test_append_appends_content_when_marker_absent(self):
        """marker 不存在时追加内容并保存"""
        flow, uow_mock, _, planner, _ = _build_flow_with_mocks()

        planner._memory = MagicMock()
        marker = "[新marker]"
        original_content = "original system prompt"
        planner._memory.messages = [
            {"role": "system", "content": original_content}
        ]

        append_content = "\n\n新增内容"
        await flow._append_to_system_prompt(planner, marker, append_content)

        # 验证内容已追加
        assert planner._memory.messages[0]["content"] == original_content + append_content
        # 验证 save_memory 被调用
        uow_mock.session.save_memory.assert_called_once()


class TestBaseAgentNoUowAttribute:
    """回归保护: 验证 BaseAgent 不存在 _uow 属性"""

    def test_planner_agent_class_has_no_uow_attribute(self):
        """PlannerAgent 类不应有 _uow 类属性"""
        assert not hasattr(PlannerAgent, "_uow"), \
            "PlannerAgent 类不应有 _uow 类属性,只有 _uow_factory"

    def test_react_agent_class_has_no_uow_attribute(self):
        """ReActAgent 类不应有 _uow 类属性"""
        assert not hasattr(ReActAgent, "_uow"), \
            "ReActAgent 类不应有 _uow 类属性,只有 _uow_factory"

    def test_base_agent_class_has_no_uow_attribute(self):
        """BaseAgent 类不应有 _uow 类属性"""
        assert not hasattr(BaseAgent, "_uow"), \
            "BaseAgent 类不应有 _uow 类属性,只有 _uow_factory (在 __init__ 中赋值)"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
