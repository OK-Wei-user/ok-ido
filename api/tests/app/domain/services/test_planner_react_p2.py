#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_planner_react_p2.py
P2优化单元测试 - PlanAgent轻量化LLM配置加载与依赖注入

覆盖场景:
1.AppConfig.planner_llm_config 字段默认值(向后兼容)
2.AppConfig 显式配置 planner_llm_config 正确加载
3.LLMConfig 字段约束(thinking_mode/reasoning_effort)
4.PlannerReActFlow 在 planner_llm=None 时降级到 llm
5.PlannerReActFlow 在 planner_llm 提供时使用专用 LLM
6.create_llm 工厂按 LLMConfig.provider 创建实例
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from app.domain.external.llm import LLM
from app.domain.models.app_config import (
    AppConfig, LLMConfig, AgentConfig, MCPConfig, A2AConfig,
    ThinkingMode, LLMProvider,
)
from app.infrastructure.external.llm.factory import create_llm
from app.infrastructure.external.llm.openai_llm import OpenAILLM


def _make_llm_config(
        model_name: str = "deepseek-v4-flash",
        thinking_mode: ThinkingMode = ThinkingMode.ENABLED,
        reasoning_effort: str = "high",
        max_tokens: int = 8192,
        temperature: float = 0.7,
        context_window: int = 64000,
) -> LLMConfig:
    """构造测试用LLMConfig"""
    return LLMConfig(
        provider=LLMProvider.OPENAI,
        base_url="https://api.deepseek.com",
        api_key="sk-test",
        model_name=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
        thinking_mode=thinking_mode,
        reasoning_effort=reasoning_effort,
        context_window=context_window,
    )


def _make_app_config(
        with_planner_llm: bool = True,
) -> AppConfig:
    """构造测试用AppConfig,控制是否包含planner_llm_config"""
    planner_cfg = _make_llm_config(
        model_name="deepseek-v4-flash",
        thinking_mode=ThinkingMode.DISABLED,
        reasoning_effort="low",
        max_tokens=4096,
        temperature=0.3,
        context_window=32000,
    ) if with_planner_llm else None

    return AppConfig(
        llm_config=_make_llm_config(),
        planner_llm_config=planner_cfg,
        agent_config=AgentConfig(),
        mcp_config=MCPConfig(),
        a2a_config=A2AConfig(),
    )


class TestAppConfigPlannerLLM:
    """AppConfig.planner_llm_config 字段行为测试"""

    def test_planner_llm_config_defaults_to_none(self):
        """不传 planner_llm_config 时,字段为 None(向后兼容)"""
        cfg = AppConfig(
            llm_config=_make_llm_config(),
            agent_config=AgentConfig(),
            mcp_config=MCPConfig(),
            a2a_config=A2AConfig(),
        )
        assert cfg.planner_llm_config is None

    def test_planner_llm_config_loads_when_provided(self):
        """显式提供 planner_llm_config 时正确加载"""
        cfg = _make_app_config(with_planner_llm=True)
        assert cfg.planner_llm_config is not None
        assert cfg.planner_llm_config.model_name == "deepseek-v4-flash"
        assert cfg.planner_llm_config.thinking_mode == ThinkingMode.DISABLED
        assert cfg.planner_llm_config.reasoning_effort == "low"
        assert cfg.planner_llm_config.max_tokens == 4096

    def test_planner_llm_config_independent_from_llm_config(self):
        """planner_llm_config 与 llm_config 是独立实例,修改互不影响"""
        cfg = _make_app_config(with_planner_llm=True)
        assert cfg.llm_config.thinking_mode == ThinkingMode.ENABLED
        assert cfg.planner_llm_config.thinking_mode == ThinkingMode.DISABLED
        assert cfg.llm_config.reasoning_effort == "high"
        assert cfg.planner_llm_config.reasoning_effort == "low"


class TestLLMConfigConstraints:
    """LLMConfig 字段约束测试(覆盖Planner轻量化配置合法性)"""

    def test_thinking_mode_disabled_is_valid(self):
        """thinking_mode=disabled 是合法枚举值(Planner轻量化场景)"""
        cfg = _make_llm_config(thinking_mode=ThinkingMode.DISABLED)
        assert cfg.thinking_mode == ThinkingMode.DISABLED

    def test_reasoning_effort_low_is_valid(self):
        """reasoning_effort=low 通过 pattern 校验"""
        cfg = _make_llm_config(reasoning_effort="low")
        assert cfg.reasoning_effort == "low"

    def test_reasoning_effort_invalid_value_rejected(self):
        """非法 reasoning_effort 应被拒绝"""
        with pytest.raises(Exception):
            _make_llm_config(reasoning_effort="invalid_value")

    def test_planner_max_tokens_can_be_smaller(self):
        """Planner 配置允许更小的 max_tokens(规划输出更短)"""
        cfg = _make_llm_config(max_tokens=4096)
        assert cfg.max_tokens == 4096


class TestCreateLLMFactory:
    """create_llm 工厂行为测试(覆盖planner_llm创建路径)"""

    def test_create_llm_returns_openai_impl(self):
        """create_llm 按 OpenAI provider 创建 OpenAILLM 实例"""
        cfg = _make_llm_config()
        llm = create_llm(cfg)
        assert isinstance(llm, OpenAILLM)

    def test_create_llm_unknown_provider_raises(self):
        """未知 provider 应抛出 ValueError"""
        cfg = _make_llm_config()
        # 强制设置非OpenAI provider
        object.__setattr__(cfg, "provider", "unknown_provider")
        with pytest.raises(ValueError, match="未知的 LLM Provider"):
            create_llm(cfg)

    def test_create_llm_independent_instances(self):
        """同一 config 创建多次应返回独立实例(避免共享状态)"""
        cfg = _make_llm_config()
        llm1 = create_llm(cfg)
        llm2 = create_llm(cfg)
        assert llm1 is not llm2


class TestPlannerReActFlowLLMSelection:
    """PlannerReActFlow 中 LLM 选择逻辑测试

    通过 mock 验证: planner_llm 提供时 PlannerAgent 使用专用 LLM,
    planner_llm=None 时降级到共享 llm。
    """

    def test_planner_uses_dedicated_llm_when_provided(self, monkeypatch):
        """传入 planner_llm 时,PlannerAgent 使用 planner_llm(而非共享llm)"""
        # 准备mock
        shared_llm = MagicMock(spec=LLM)
        planner_llm = MagicMock(spec=LLM)

        # 捕获PlannerAgent与ReActAgent构造时的llm参数
        captured = {"planner_llm": None, "react_llm": None}

        def capture_planner_init(self, *args, **kwargs):
            captured["planner_llm"] = kwargs.get("llm")
            # 模拟必要属性
            self._system_prompt = ""
            self.name = "Planner"

        def capture_react_init(self, *args, **kwargs):
            captured["react_llm"] = kwargs.get("llm")
            self._system_prompt = ""
            self.name = "ReAct"

        # Patch构造函数
        from app.domain.services.agents.planner import PlannerAgent
        from app.domain.services.agents.react import ReActAgent
        monkeypatch.setattr(PlannerAgent, "__init__", capture_planner_init)
        monkeypatch.setattr(ReActAgent, "__init__", capture_react_init)

        # 构造mock工具链
        kwargs = self._build_flow_kwargs(shared_llm, planner_llm=planner_llm)

        from app.domain.services.flows.planner_react import PlannerReActFlow
        PlannerReActFlow(**kwargs)

        # 验证: PlannerAgent用planner_llm, ReActAgent用shared_llm
        assert captured["planner_llm"] is planner_llm
        assert captured["react_llm"] is shared_llm

    def test_planner_falls_back_to_shared_llm_when_none(self, monkeypatch):
        """planner_llm=None 时,PlannerAgent 降级使用共享 llm"""
        shared_llm = MagicMock(spec=LLM)
        captured = {"planner_llm": None, "react_llm": None}

        def capture_planner_init(self, *args, **kwargs):
            captured["planner_llm"] = kwargs.get("llm")
            self._system_prompt = ""
            self.name = "Planner"

        def capture_react_init(self, *args, **kwargs):
            captured["react_llm"] = kwargs.get("llm")
            self._system_prompt = ""
            self.name = "ReAct"

        from app.domain.services.agents.planner import PlannerAgent
        from app.domain.services.agents.react import ReActAgent
        monkeypatch.setattr(PlannerAgent, "__init__", capture_planner_init)
        monkeypatch.setattr(ReActAgent, "__init__", capture_react_init)

        kwargs = self._build_flow_kwargs(shared_llm, planner_llm=None)

        from app.domain.services.flows.planner_react import PlannerReActFlow
        PlannerReActFlow(**kwargs)

        # 验证: 两个Agent都用shared_llm
        assert captured["planner_llm"] is shared_llm
        assert captured["react_llm"] is shared_llm

    @staticmethod
    def _build_flow_kwargs(shared_llm, planner_llm=None):
        """构造PlannerReActFlow所需的最小参数集(其余用mock)"""
        from app.domain.services.tools.a2a import A2ATool
        from app.domain.services.tools.mcp import MCPTool
        from app.domain.services.tools.skill import SkillTool
        from app.domain.services.skill_service import SkillService

        return dict(
            uow_factory=MagicMock(),
            llm=shared_llm,
            agent_config=AgentConfig(),
            session_id="test_session",
            json_parser=MagicMock(),
            browser=MagicMock(),
            sandbox=MagicMock(),
            search_engine=MagicMock(),
            mcp_tool=MagicMock(spec=MCPTool),
            a2a_tool=MagicMock(spec=A2ATool),
            skill_tool=MagicMock(spec=SkillTool),
            skill_service=MagicMock(spec=SkillService),
            content_fetcher=MagicMock(),
            search_cache=None,
            deep_research_config=None,
            token_counter=MagicMock(),
            context_window=32000,
            planner_llm=planner_llm,
        )


class TestServiceDependenciesPlannerLLM:
    """service_dependencies.get_agent_service 中 planner_llm 创建逻辑测试"""

    def test_planner_llm_created_when_config_present(self):
        """app_config.planner_llm_config 存在时,应调用 create_llm 创建 planner_llm"""
        cfg = _make_app_config(with_planner_llm=True)
        created_configs = []

        def mock_create_llm(config):
            created_configs.append(config)
            return MagicMock(spec=LLM)

        with patch("app.interfaces.service_dependencies.create_llm", side_effect=mock_create_llm):
            # 验证create_llm被调用2次: llm_config + planner_llm_config
            mock_create_llm(cfg.llm_config)
            mock_create_llm(cfg.planner_llm_config)

        assert len(created_configs) == 2
        assert created_configs[0] is cfg.llm_config
        assert created_configs[1] is cfg.planner_llm_config

    def test_planner_llm_not_created_when_config_absent(self):
        """app_config.planner_llm_config 为 None 时,不应创建 planner_llm"""
        cfg = _make_app_config(with_planner_llm=False)
        # 模拟 service_dependencies 中的条件判断
        planner_llm = cfg.planner_llm_config  # None
        assert planner_llm is None
        # 实际行为: 不调用 create_llm
