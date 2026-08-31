# -*- coding: utf-8 -*-
"""批次45 P1-3: 会话超时前附件交付保障单元测试

覆盖软警告(1500s)增强为四步操作指令:
- 软警告内容含"停止查询→生成文件→声明路径→总结"四步
- 软警告触发后设 session_timeout_injected=True 防重复注入(原仅硬超时设置)
- 硬超时行为不受影响(向后兼容)
"""
from unittest.mock import patch

import pytest

from app.domain.models.app_config import AgentConfig


class TestP13SoftWarningFourStepDelivery:
    """P1-3: 软警告四步交付引导"""

    def _build_agent(self, session_timeout: int = 1800, session_warning: int = 1500):
        """构建BaseAgent实例(绕过__init__)用于测试_inject_budget_warnings"""
        from app.domain.services.agents.base import BaseAgent
        agent = object.__new__(BaseAgent)
        agent.name = "test_agent"
        agent._session_id = "test_session"
        agent._agent_config = AgentConfig(
            max_iterations=100,
            session_timeout_seconds=session_timeout,
            session_warning_seconds=session_warning,
        )
        agent._budget_tracker = None
        agent._shell_profiler = None
        agent._shell_guidance_injected = False
        return agent

    def test_soft_warning_contains_four_steps(self):
        """软警告内容应含四步操作指令"""
        agent = self._build_agent()
        tool_messages = []
        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 2600.0  # 运行1600秒(>1500软警告)
            agent._inject_budget_warnings(
                iteration=1,
                tool_messages=tool_messages,
                session_start_ts=1000.0,
                session_timeout_injected=False,
            )
        warning_msgs = [m for m in tool_messages if "系统时间警告" in m["content"]]
        assert len(warning_msgs) == 1
        content = warning_msgs[0]["content"]
        # 四步操作指令
        assert "停止所有数据查询" in content
        assert "生成交付物文件" in content
        assert "attachments" in content
        assert "最终总结回答" in content

    def test_soft_warning_sets_injected_flag(self):
        """软警告触发后应返回True(设session_timeout_injected标志防重复)"""
        agent = self._build_agent()
        tool_messages = []
        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 2600.0
            result = agent._inject_budget_warnings(
                iteration=1,
                tool_messages=tool_messages,
                session_start_ts=1000.0,
                session_timeout_injected=False,
            )
        # 批次45: 软警告也设标志(原仅硬超时设置),返回True防重复注入
        assert result is True

    def test_soft_warning_not_repeated(self):
        """软警告已注入后不应重复注入(session_timeout_injected=True时跳过)"""
        agent = self._build_agent()
        tool_messages = []
        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 2600.0
            agent._inject_budget_warnings(
                iteration=1,
                tool_messages=tool_messages,
                session_start_ts=1000.0,
                session_timeout_injected=True,  # 已注入
            )
        warning_msgs = [m for m in tool_messages if "系统时间警告" in m["content"]]
        assert len(warning_msgs) == 0

    def test_hard_timeout_still_works(self):
        """硬超时行为不受影响(向后兼容)"""
        agent = self._build_agent(session_timeout=1800)
        tool_messages = []
        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 3000.0  # 运行2000秒(>1800硬超时)
            result = agent._inject_budget_warnings(
                iteration=1,
                tool_messages=tool_messages,
                session_start_ts=1000.0,
                session_timeout_injected=False,
            )
        assert result is True
        timeout_msgs = [m for m in tool_messages if "系统超时指令" in m["content"]]
        assert len(timeout_msgs) == 1

    def test_no_timeout_no_warning(self):
        """未达软警告时不应注入软警告"""
        agent = self._build_agent()
        tool_messages = []
        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 1200.0  # 运行200秒(未达1500)
            result = agent._inject_budget_warnings(
                iteration=1,
                tool_messages=tool_messages,
                session_start_ts=1000.0,
                session_timeout_injected=False,
            )
        warning_msgs = [m for m in tool_messages if "系统时间警告" in m["content"]]
        assert len(warning_msgs) == 0
