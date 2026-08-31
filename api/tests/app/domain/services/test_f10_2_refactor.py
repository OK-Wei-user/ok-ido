#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_f10_2_refactor.py
F10-2 BaseAgent.invoke 重构单元测试 - 验证提取的辅助方法行为等价性

测试覆盖:
- _parse_tool_call: 正常解析/畸形tool_call/参数异常/未知工具
- _build_error_tool_message: 错误消息结构
- _build_tool_result_message: 正常消息结构
- _inject_budget_warnings: 迭代预算80%/90%阈值/会话超时硬软阈值/防重复注入
"""
from unittest.mock import MagicMock, AsyncMock

import pytest

from app.domain.models.app_config import AgentConfig
from app.domain.models.event import ToolEventStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent, _ParsedToolCall
from app.domain.services.tools.base import BaseTool, tool


# ========== Stub工具定义 ==========

class _StubTool(BaseTool):
    """测试用工具包"""
    name: str = "stub"

    @tool(name="stub_action", description="测试动作", parameters={}, required=[])
    async def stub_action(self) -> ToolResult:
        return ToolResult(success=True, data="ok")


# ========== 测试辅助函数 ==========

def _build_agent(max_iter: int = 10,
                 session_timeout: int = 0,
                 session_warning: int = 0) -> BaseAgent:
    """构建BaseAgent实例(mock __init__)"""
    agent = object.__new__(BaseAgent)
    agent.name = "test_agent"
    agent._agent_config = AgentConfig(
        max_iterations=max_iter,
        session_timeout_seconds=session_timeout,
        session_warning_seconds=session_warning,
    )
    agent._session_id = "test_session"
    agent._session_start_ts = 0.0
    agent._tools = [_StubTool()]
    agent._json_parser = AsyncMock()
    return agent


# ========== _parse_tool_call 测试 ==========

class TestParseToolCall:
    """_parse_tool_call 方法测试"""

    @pytest.mark.asyncio
    async def test_normal_parse(self):
        """正常解析: 有效function字段 + 有效JSON参数 + 已知工具"""
        agent = _build_agent()
        agent._json_parser.invoke = AsyncMock(return_value={"key": "value"})

        tool_call = {
            "id": "call_001",
            "function": {"name": "stub_action", "arguments": '{"key": "value"}'},
        }
        parsed = await agent._parse_tool_call(tool_call)

        assert parsed.error_content is None
        assert parsed.tool_call_id == "call_001"
        assert parsed.function_name == "stub_action"
        assert parsed.function_args == {"key": "value"}
        assert parsed.tool is not None

    @pytest.mark.asyncio
    async def test_malformed_no_function(self):
        """畸形tool_call: 无function字段"""
        agent = _build_agent()
        tool_call = {"id": "call_002"}

        parsed = await agent._parse_tool_call(tool_call)

        assert parsed.error_content is not None
        assert "格式异常" in parsed.error_content
        assert parsed.tool is None
        assert parsed.function_name == "(malformed)"

    @pytest.mark.asyncio
    async def test_malformed_no_id(self):
        """畸形tool_call: 无id时自动生成UUID"""
        agent = _build_agent()
        tool_call = {"function": {"name": "stub_action", "arguments": "{}"}}

        parsed = await agent._parse_tool_call(tool_call)

        assert parsed.error_content is None
        assert parsed.tool_call_id  # 应自动生成非空ID

    @pytest.mark.asyncio
    async def test_args_parse_failure_fallback_empty(self):
        """参数JSON解析失败: 降级为空字典"""
        agent = _build_agent()
        agent._json_parser.invoke = AsyncMock(side_effect=Exception("JSON解析失败"))

        tool_call = {
            "id": "call_003",
            "function": {"name": "stub_action", "arguments": "invalid_json"},
        }
        parsed = await agent._parse_tool_call(tool_call)

        assert parsed.error_content is None
        assert parsed.function_args == {}

    @pytest.mark.asyncio
    async def test_args_list_converted_to_dict(self):
        """参数为list: 自动转为 {"items": list}"""
        agent = _build_agent()
        agent._json_parser.invoke = AsyncMock(return_value=[1, 2, 3])

        tool_call = {
            "id": "call_004",
            "function": {"name": "stub_action", "arguments": "[1,2,3]"},
        }
        parsed = await agent._parse_tool_call(tool_call)

        assert parsed.function_args == {"items": [1, 2, 3]}

    @pytest.mark.asyncio
    async def test_args_non_dict_non_list_fallback_empty(self):
        """参数为非dict/list(如str/int): 降级为空字典"""
        agent = _build_agent()
        agent._json_parser.invoke = AsyncMock(return_value="not_a_dict")

        tool_call = {
            "id": "call_005",
            "function": {"name": "stub_action", "arguments": "\"str\""},
        }
        parsed = await agent._parse_tool_call(tool_call)

        assert parsed.function_args == {}

    @pytest.mark.asyncio
    async def test_unknown_tool_error_with_hint(self):
        """未知工具: 错误消息包含可用工具列表提示"""
        agent = _build_agent()
        agent._json_parser.invoke = AsyncMock(return_value={})

        tool_call = {
            "id": "call_006",
            "function": {"name": "nonexistent_tool", "arguments": "{}"},
        }
        parsed = await agent._parse_tool_call(tool_call)

        assert parsed.error_content is not None
        assert "不存在" in parsed.error_content
        assert "可用工具列表" in parsed.error_content
        assert "stub_action" in parsed.error_content  # 应列出可用工具名


# ========== _build_error_tool_message 测试 ==========

class TestBuildErrorToolMessage:
    """_build_error_tool_message 静态方法测试"""

    def test_error_message_structure(self):
        """错误tool_message结构: role/tool_call_id/function_name/content"""
        parsed = _ParsedToolCall(
            tool_call_id="call_err",
            tool=None,
            function_name="(malformed)",
            error_content="错误: 工具调用格式异常",
        )
        msg = BaseAgent._build_error_tool_message(parsed)

        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "call_err"
        assert msg["function_name"] == "(malformed)"
        assert msg["content"] == "错误: 工具调用格式异常"


# ========== _build_tool_result_message 测试 ==========

class TestBuildToolResultMessage:
    """_build_tool_result_message 静态方法测试"""

    def test_result_message_structure(self):
        """正常tool_message结构: role/tool_call_id/function_name/content"""
        parsed = _ParsedToolCall(
            tool_call_id="call_ok",
            tool=None,
            function_name="stub_action",
        )
        result = ToolResult(success=True, data="执行结果")
        msg = BaseAgent._build_tool_result_message(parsed, result)

        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "call_ok"
        assert msg["function_name"] == "stub_action"
        assert "执行结果" in msg["content"]


# ========== _inject_budget_warnings 测试 ==========

class TestInjectBudgetWarnings:
    """_inject_budget_warnings 方法测试"""

    def test_no_warning_below_threshold(self):
        """迭代<80%阈值: 不注入任何警告"""
        agent = _build_agent(max_iter=10)
        tool_messages = []

        agent._inject_budget_warnings(
            iteration=3, tool_messages=tool_messages,
            session_start_ts=0.0, session_timeout_injected=False,
        )
        assert len(tool_messages) == 0

    def test_warning_at_80_percent(self):
        """迭代>=80%阈值: 注入收敛警告"""
        agent = _build_agent(max_iter=10)
        tool_messages = []

        agent._inject_budget_warnings(
            iteration=8, tool_messages=tool_messages,
            session_start_ts=0.0, session_timeout_injected=False,
        )
        assert len(tool_messages) == 1
        assert "80%" in tool_messages[0]["content"]

    def test_critical_at_90_percent(self):
        """迭代>=90%阈值: 注入紧急停止指令(优先于80%警告)"""
        agent = _build_agent(max_iter=10)
        tool_messages = []

        agent._inject_budget_warnings(
            iteration=9, tool_messages=tool_messages,
            session_start_ts=0.0, session_timeout_injected=False,
        )
        assert len(tool_messages) == 1
        assert "90%" in tool_messages[0]["content"]
        assert "立即停止" in tool_messages[0]["content"]

    def test_session_timeout_hard_threshold(self):
        """会话超时硬阈值: 注入超时指令并返回True"""
        from unittest.mock import patch
        agent = _build_agent(max_iter=10, session_timeout=60, session_warning=30)
        tool_messages = []
        # 模拟已运行超过60s: start_ts=0, current_time=100 → elapsed=100
        # 注意: session_start_ts 必须 > 0.0 才会触发超时检测
        mock_loop = MagicMock()
        mock_loop.time.return_value = 100.0

        with patch("app.domain.services.agents.base.asyncio.get_event_loop", return_value=mock_loop):
            result = agent._inject_budget_warnings(
                iteration=1, tool_messages=tool_messages,
                session_start_ts=1.0, session_timeout_injected=False,
            )
        # 应注入超时指令
        assert any("超时" in m["content"] for m in tool_messages)
        assert result is True

    def test_session_timeout_injected_not_repeated(self):
        """已注入超时指令: 不重复注入"""
        from unittest.mock import patch
        agent = _build_agent(max_iter=10, session_timeout=60, session_warning=30)
        tool_messages = []
        mock_loop = MagicMock()
        mock_loop.time.return_value = 100.0

        with patch("app.domain.services.agents.base.asyncio.get_event_loop", return_value=mock_loop):
            agent._inject_budget_warnings(
                iteration=1, tool_messages=tool_messages,
                session_start_ts=1.0, session_timeout_injected=True,
            )
        # 已注入过,不应再注入超时指令
        assert not any("超时指令" in m["content"] for m in tool_messages)

    def test_session_warning_soft_threshold(self):
        """会话超时软阈值: 注入收敛提示并设置injected=True防重复(批次45 P1-3)"""
        from unittest.mock import patch
        agent = _build_agent(max_iter=10, session_timeout=60, session_warning=30)
        tool_messages = []
        # 模拟已运行35s: start_ts=1, current_time=36 → elapsed=35(>=30软阈值, <60硬阈值)
        mock_loop = MagicMock()
        mock_loop.time.return_value = 36.0

        with patch("app.domain.services.agents.base.asyncio.get_event_loop", return_value=mock_loop):
            result = agent._inject_budget_warnings(
                iteration=1, tool_messages=tool_messages,
                session_start_ts=1.0, session_timeout_injected=False,
            )
        # 批次45 P1-3: 软阈值注入收敛提示并设injected=True防重复注入
        # (原设计不设True允许后续硬阈值再注入,批次45改为防重复避免软警告多次触发)
        assert any("时间警告" in m["content"] for m in tool_messages)
        assert result is True
