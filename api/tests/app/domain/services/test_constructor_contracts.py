#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_constructor_contracts.py
构造契约测试 - 验证关键类 __init__ 正确保存关键属性

背景: AgentTaskRunner.__init__ 漏存 _agent_config 缺陷被 object.__new__ 绕过 __init__
的测试模式掩盖(所有现有单测均绕过构造函数手动赋值)。本测试对持有运行期状态的关键类
补充「真实 __init__ + mock 依赖」的构造契约测试,断言属性完整性,防止同类缺陷复发。

复用 test_shell_output_streaming.py::TestInitSavesAgentConfig 模式:
- patch 构造函数内部实例化的复杂依赖(MagicMock)
- 调用真实 __init__
- 断言关键属性已保存为传入对象(或正确实例化)
"""
from unittest.mock import MagicMock

import pytest

from app.domain.models.app_config import AgentConfig, MCPConfig, A2AConfig
from app.domain.services.flows.planner_react import PlannerReActFlow
from app.application.services.agent_service import AgentService


# ========== PlannerReActFlow 构造契约测试 ==========

class TestPlannerReActFlowInitContract:
    """验证 PlannerReActFlow.__init__ 保存关键属性

    PlannerReActFlow 构造函数内部实例化大量工具与两个 Agent(Planner/ReAct),
    任一属性漏存会导致运行期 AttributeError。本测试 patch 内部依赖后调用真实 __init__。
    """

    @pytest.fixture(autouse=True)
    def _patch_internal_deps(self, monkeypatch):
        """patch __init__ 内部实例化的所有类,仅验证属性赋值"""
        # 工具与辅助类
        monkeypatch.setattr(
            "app.domain.services.flows.planner_react.ToolBudgetTracker", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.flows.planner_react.ExperimentResolver", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.flows.planner_react.SearchTool", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.flows.planner_react.DeepResearchTool", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.flows.planner_react.ShellTool", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.flows.planner_react.TaskCallbackTool", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.flows.planner_react.FileTool", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.flows.planner_react.BrowserTool", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.flows.planner_react.MessageTool", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.flows.planner_react.PlannerAgent", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.flows.planner_react.ReActAgent", MagicMock()
        )
        monkeypatch.setattr(
            "app.domain.services.flows.planner_react.SkillsPromptCache", MagicMock()
        )

    def test_init_saves_core_attributes(self):
        """__init__ 后核心属性应为传入对象或正确实例化"""
        agent_config = AgentConfig(max_iterations=10)
        sandbox = MagicMock()
        mcp_tool = MagicMock()
        uow_factory = MagicMock()

        flow = PlannerReActFlow(
            uow_factory=uow_factory,
            llm=MagicMock(),
            agent_config=agent_config,
            session_id="test_session",
            json_parser=MagicMock(),
            browser=MagicMock(),
            sandbox=sandbox,
            search_engine=MagicMock(),
            mcp_tool=mcp_tool,
            a2a_tool=MagicMock(),
            skill_tool=MagicMock(),
            skill_service=MagicMock(),
        )

        # 核心属性断言(防漏存)
        assert flow._session_id == "test_session"
        assert flow._sandbox is sandbox
        assert flow._mcp_tool is mcp_tool
        assert flow._uow_factory is uow_factory
        assert flow._skill_service is not None
        # 状态属性应初始化为默认值
        assert flow.status is not None  # FlowStatus.IDLE
        assert flow.plan is None
        # 内部实例化的属性应存在(非 None)
        assert flow._budget_tracker is not None
        assert flow.planner is not None
        assert flow.react is not None
        assert flow._shell_tool is not None


# ========== AgentService 构造契约测试 ==========

class TestAgentServiceInitContract:
    """验证 AgentService.__init__ 保存关键属性

    AgentService 是会话编排核心,持有配置/工具/锁等运行期状态。
    本测试 mock 依赖后调用真实 __init__,断言关键属性已保存。
    """

    def test_init_saves_core_attributes(self, monkeypatch):
        """__init__ 后核心配置/工具属性应为传入对象"""
        # patch FilePresentationService(构造函数内部实例化)
        monkeypatch.setattr(
            "app.application.services.agent_service.FilePresentationService",
            MagicMock(),
        )

        agent_config = AgentConfig(max_iterations=10)
        mcp_config = MCPConfig()
        a2a_config = A2AConfig()
        llm = MagicMock()
        uow_factory = MagicMock()

        service = AgentService(
            uow_factory=uow_factory,
            llm=llm,
            agent_config=agent_config,
            mcp_config=mcp_config,
            a2a_config=a2a_config,
            sandbox_cls=MagicMock(),
            task_cls=MagicMock(),
            json_parser=MagicMock(),
            search_engine=MagicMock(),
            content_fetcher=MagicMock(),
            file_storage=MagicMock(),
            skill_service=MagicMock(),
        )

        # 配置属性断言(防漏存,正是 AgentTaskRunner 缺陷的同类风险点)
        assert service._agent_config is agent_config
        assert service._mcp_config is mcp_config
        assert service._a2a_config is a2a_config
        assert service._llm is llm
        assert service._uow_factory is uow_factory
        # 内部实例化属性应存在
        assert service._file_presentation is not None
        # 并发控制属性应初始化为空字典
        assert service._session_locks == {}
        assert service._sandbox_ttl_tasks == {}
        assert service._locks_guard is not None
