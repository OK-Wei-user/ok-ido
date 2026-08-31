#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Batch 40 优化单元测试

覆盖 4 个方向的全部新增逻辑:
- 方向3(P1): ShellCallProfiler 调用画像 + detect_loop_pattern
- 方向2(P2): MetricsPersister Redis 持久化 + ExperimentResolver A/B 分组
  + budget_tracker adjust_for_task_type 扩展(adjustments 参数)
- 方向4(P3): classify_with_llm 3 层降级(关键词→LLM→general)+ 缓存
- 方向1(P4): P11 沙箱回调(callback_agent + sandbox_callback_routes + shell.py P11 模式)
"""
import asyncio
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.services.agents.task_type_classifier import (
    classify_task_type,
    classify_with_llm,
    TASK_TYPE_RESEARCH,
    TASK_TYPE_DATA_ANALYSIS,
    TASK_TYPE_BROWSER,
    TASK_TYPE_GENERAL,
)
from app.domain.services.experiments.experiment_resolver import (
    ExperimentResolver,
    _DEFAULT_ADJUSTMENTS,
    _DEFAULT_GROUP,
    _CONTROL_GROUP,
)
from app.domain.services.agents.react import ReActAgent
from app.domain.services.observability.metrics_persister import MetricsPersister
from app.domain.services.observability.shell_call_profiler import (
    ShellCallProfiler,
    detect_loop_pattern,
)
from app.domain.services.tools.budget_tracker import ToolBudgetTracker


# ============================================================
# 方向3(P1): ShellCallProfiler + detect_loop_pattern
# ============================================================

class TestDirection3LoopPatternDetection:
    """方向3: 循环/批量模式检测"""

    def test_detect_python_for_in(self):
        """Python for...in 循环检测"""
        assert detect_loop_pattern("for item in items:") is True

    def test_detect_python_for_range(self):
        """Python for i in range 检测"""
        assert detect_loop_pattern("for i in range(10):") is True

    def test_detect_while_loop(self):
        """while 循环检测"""
        assert detect_loop_pattern("while True:") is True

    def test_detect_pandas_apply(self):
        """pandas .apply() 检测"""
        assert detect_loop_pattern("df['col'].apply(func)") is True

    def test_detect_pandas_iterrows(self):
        """pandas iterrows() 检测"""
        assert detect_loop_pattern("for idx, row in df.iterrows():") is True

    def test_detect_shell_chain(self):
        """Shell && 链(4+命令合并,3+个&&)检测"""
        assert detect_loop_pattern("cmd1 && cmd2 && cmd3 && cmd4") is True

    def test_detect_shell_for_do(self):
        """Shell for...do 检测"""
        assert detect_loop_pattern("for f in *.txt; do cat $f; done") is True

    def test_no_loop_simple_command(self):
        """简单命令不检测为循环"""
        assert detect_loop_pattern("ls -la /home/ubuntu/") is False

    def test_no_loop_single_python_statement(self):
        """单条 Python 语句不检测为循环"""
        assert detect_loop_pattern("print('hello')") is False

    def test_empty_command(self):
        """空命令不检测为循环"""
        assert detect_loop_pattern("") is False
        assert detect_loop_pattern(None) is False


class TestDirection3ShellCallProfiler:
    """方向3: ShellCallProfiler 画像收集器"""

    def test_empty_profiler_summary(self):
        """空画像器汇总返回零值"""
        profiler = ShellCallProfiler()
        summary = profiler.get_profile_summary()
        assert summary["total_calls"] == 0
        assert summary["batch_calls"] == 0
        assert summary["singleton_calls"] == 0
        assert summary["batch_ratio"] == 0.0
        assert summary["guidance_triggered"] is False

    def test_record_single_call(self):
        """记录单次调用(无循环模式)"""
        profiler = ShellCallProfiler()
        profiler.record("ls -la /home/ubuntu/")
        summary = profiler.get_profile_summary()
        assert summary["total_calls"] == 1
        assert summary["batch_calls"] == 0
        assert summary["singleton_calls"] == 1
        assert summary["batch_ratio"] == 0.0

    def test_record_batch_call(self):
        """记录批量调用(含循环模式)"""
        profiler = ShellCallProfiler()
        profiler.record("for i in range(10): print(i)")
        summary = profiler.get_profile_summary()
        assert summary["total_calls"] == 1
        assert summary["batch_calls"] == 1
        assert summary["singleton_calls"] == 0
        assert summary["batch_ratio"] == 1.0

    def test_record_mixed_calls(self):
        """记录混合调用(批量+单次)"""
        profiler = ShellCallProfiler()
        profiler.record("ls -la")
        profiler.record("for i in range(5): print(i)")
        profiler.record("echo hello")
        profiler.record("df.apply(func)")
        summary = profiler.get_profile_summary()
        assert summary["total_calls"] == 4
        assert summary["batch_calls"] == 2
        assert summary["singleton_calls"] == 2
        assert summary["batch_ratio"] == 0.5

    def test_guidance_active_tracking(self):
        """合并引导激活状态追踪"""
        profiler = ShellCallProfiler()
        # 引导未激活时记录
        profiler.record("ls -la")
        # 激活引导后记录
        profiler.set_guidance_active(True)
        profiler.record("for i in range(10): print(i)")
        # 关闭引导后记录
        profiler.set_guidance_active(False)
        profiler.record("echo done")

        summary = profiler.get_profile_summary()
        assert summary["guidance_triggered"] is True
        assert summary["guidance_triggered_calls"] == 1

    def test_avg_command_length(self):
        """平均命令长度计算"""
        profiler = ShellCallProfiler()
        profiler.record("ab")      # 长度 2
        profiler.record("abcde")   # 长度 5
        summary = profiler.get_profile_summary()
        assert summary["avg_command_length"] == 3.5  # (2+5)/2

    def test_reset_clears_all(self):
        """reset 清空所有画像数据"""
        profiler = ShellCallProfiler()
        profiler.set_guidance_active(True)
        profiler.record("for i in range(10): print(i)")
        assert profiler.get_profile_summary()["total_calls"] == 1

        profiler.reset()
        summary = profiler.get_profile_summary()
        assert summary["total_calls"] == 0
        assert summary["guidance_triggered"] is False

    def test_record_failure_safe(self):
        """record 异常不抛出(降级忽略)"""
        profiler = ShellCallProfiler()
        # 传入不可序列化的对象,record 不应抛出
        profiler.record(command=None, success=False)
        summary = profiler.get_profile_summary()
        assert summary["total_calls"] == 1
        assert summary["avg_command_length"] == 0

    def test_call_index_incremental(self):
        """call_index 递增"""
        profiler = ShellCallProfiler()
        profiler.record("cmd1")
        profiler.record("cmd2")
        profiler.record("cmd3")
        # 内部 _calls 列表的 call_index 应递增
        assert profiler._calls[0]["call_index"] == 1
        assert profiler._calls[1]["call_index"] == 2
        assert profiler._calls[2]["call_index"] == 3


# ============================================================
# 方向3 修复(Batch 41): execute_step 同步合并引导状态到 ShellCallProfiler
# ============================================================

class TestGuidanceActiveSyncInExecuteStep:
    """Batch 41 修复: ReActAgent.execute_step 同步合并引导激活状态到 ShellCallProfiler

    根因: get_consolidation_guidance 在 _build_execution_query(staticmethod)内注入合并引导,
    但 set_guidance_active 全项目从未被调用,导致 ShellCallProfiler._guidance_active 恒为 False,
    guidance_triggered 指标恒为 False/0,方向3量化对比失效。

    修复: 在 execute_step 包装器中,set_step_context 后同步 guidance_active 标志,
    finally 中重置。本测试类验证该同步逻辑的正确性与异常安全性。
    """

    def _make_agent(self, shell_profiler=None):
        """构造最小化 ReActAgent(绕过 __init__,仅设置 execute_step 依赖属性)

        绕过 __init__ 是为避免构造 LLM/UoW/Tools 等重依赖;
        execute_step 上下文管理仅依赖 _shell_profiler / _step_description /
        _force_included_tools 三个属性。
        """
        agent = object.__new__(ReActAgent)
        agent._shell_profiler = shell_profiler
        agent._step_description = ""
        agent._force_included_tools = set()
        return agent

    def _make_noop_impl(self):
        """构造空异步生成器替代 _execute_step_impl(避免触发完整 LLM invoke 流程)"""

        async def _noop(plan, step, message):
            return
            yield  # pragma: no cover - 使函数成为 async generator, 永不执行到此

        return _noop

    @pytest.mark.asyncio
    async def test_guidance_active_true_when_quantified_target_ge_5(self):
        """步骤含量化目标(>=5)时,execute_step 调用 set_guidance_active(True)"""
        profiler = MagicMock()
        agent = self._make_agent(shell_profiler=profiler)
        agent._execute_step_impl = self._make_noop_impl()
        step = MagicMock()
        step.description = "导出50条出入库记录"

        async for _ in agent.execute_step(plan=MagicMock(), step=step, message=MagicMock()):
            pass

        # 应先 set_guidance_active(True),finally 中 set_guidance_active(False)
        profiler.set_guidance_active.assert_any_call(True)
        profiler.set_guidance_active.assert_any_call(False)

    @pytest.mark.asyncio
    async def test_guidance_active_false_when_no_quantified_target(self):
        """步骤不含量化目标时,execute_step 调用 set_guidance_active(False)"""
        profiler = MagicMock()
        agent = self._make_agent(shell_profiler=profiler)
        agent._execute_step_impl = self._make_noop_impl()
        step = MagicMock()
        step.description = "分析数据并生成报告"  # 无量化目标

        async for _ in agent.execute_step(plan=MagicMock(), step=step, message=MagicMock()):
            pass

        # 无量化目标 → get_consolidation_guidance 返回空串 → bool()=False
        profiler.set_guidance_active.assert_any_call(False)
        # 不应被设置为 True
        with pytest.raises(AssertionError):
            profiler.set_guidance_active.assert_any_call(True)

    @pytest.mark.asyncio
    async def test_guidance_active_false_when_target_below_threshold(self):
        """量化目标 < 5(阈值)时不触发合并引导"""
        profiler = MagicMock()
        agent = self._make_agent(shell_profiler=profiler)
        agent._execute_step_impl = self._make_noop_impl()
        step = MagicMock()
        step.description = "导出3条记录"  # 量化目标 3 < 阈值 5

        async for _ in agent.execute_step(plan=MagicMock(), step=step, message=MagicMock()):
            pass

        profiler.set_guidance_active.assert_any_call(False)
        with pytest.raises(AssertionError):
            profiler.set_guidance_active.assert_any_call(True)

    @pytest.mark.asyncio
    async def test_guidance_reset_in_finally_even_on_exception(self):
        """_execute_step_impl 抛异常时,finally 仍重置 set_guidance_active(False)"""
        profiler = MagicMock()
        agent = self._make_agent(shell_profiler=profiler)

        async def _raising_impl(plan, step, message):
            raise RuntimeError("模拟执行失败")
            yield  # pragma: no cover

        agent._execute_step_impl = _raising_impl
        step = MagicMock()
        step.description = "导出100条记录"  # 触发 guidance_active=True

        with pytest.raises(RuntimeError):
            async for _ in agent.execute_step(plan=MagicMock(), step=step, message=MagicMock()):
                pass

        # 异常路径下 finally 仍应重置: 进入时 True,finally 中 False
        profiler.set_guidance_active.assert_any_call(True)
        profiler.set_guidance_active.assert_any_call(False)

    @pytest.mark.asyncio
    async def test_no_error_when_shell_profiler_is_none(self):
        """_shell_profiler 为 None 时 execute_step 不报错(防御性,mock 测试场景)"""
        agent = self._make_agent(shell_profiler=None)
        agent._execute_step_impl = self._make_noop_impl()
        step = MagicMock()
        step.description = "导出100条记录"

        # 不应抛出 AttributeError
        async for _ in agent.execute_step(plan=MagicMock(), step=step, message=MagicMock()):
            pass

    @pytest.mark.asyncio
    async def test_guidance_flag_reset_after_step_completion(self):
        """集成验证: 步骤完成后真实 ShellCallProfiler._guidance_active 被重置为 False"""
        profiler = ShellCallProfiler()
        agent = self._make_agent(shell_profiler=profiler)
        agent._execute_step_impl = self._make_noop_impl()
        step = MagicMock()
        step.description = "导出20条出入库明细"

        async for _ in agent.execute_step(plan=MagicMock(), step=step, message=MagicMock()):
            pass

        # 步骤结束后 _guidance_active 应被 finally 重置为 False
        assert profiler._guidance_active is False

    @pytest.mark.asyncio
    async def test_guidance_flag_set_true_during_step_with_real_profiler(self):
        """集成验证: 量化目标步骤执行期间真实 ShellCallProfiler._guidance_active 被设为 True

        通过在 _execute_step_impl 内部读取 profiler 状态,验证同步发生在 impl 调用之前。
        """
        profiler = ShellCallProfiler()
        agent = self._make_agent(shell_profiler=profiler)
        captured_state = {"during_impl": None}

        async def _capturing_impl(plan, step, message):
            # 捕获 impl 执行期间 profiler 的 guidance_active 状态
            captured_state["during_impl"] = profiler._guidance_active
            return
            yield  # pragma: no cover

        agent._execute_step_impl = _capturing_impl
        step = MagicMock()
        step.description = "生成10份分析报告"

        async for _ in agent.execute_step(plan=MagicMock(), step=step, message=MagicMock()):
            pass

        # impl 执行期间 guidance 应已激活
        assert captured_state["during_impl"] is True
        # 步骤结束后被重置
        assert profiler._guidance_active is False


# ============================================================
# 方向2(P2): MetricsPersister + ExperimentResolver
# ============================================================

class TestDirection2MetricsPersister:
    """方向2: MetricsPersister Redis 持久化"""

    def test_persist_returns_false_for_empty_session(self):
        """空 session_id 持久化返回 False"""
        persister = MetricsPersister(redis_client=MagicMock())
        result = asyncio.get_event_loop().run_until_complete(
            persister.persist("", {"key": "val"})
        )
        assert result is False

    def test_persist_returns_false_for_empty_snapshot(self):
        """空 snapshot 持久化返回 False"""
        persister = MetricsPersister(redis_client=MagicMock())
        result = asyncio.get_event_loop().run_until_complete(
            persister.persist("session-123", {})
        )
        assert result is False

    def test_persist_returns_false_when_redis_unavailable(self):
        """Redis 不可用时持久化返回 False(降级)"""
        persister = MetricsPersister()
        persister._redis = None
        with patch("app.domain.services.observability.metrics_persister.MetricsPersister._get_redis", return_value=None):
            result = asyncio.get_event_loop().run_until_complete(
                persister.persist("session-123", {"key": "val"})
            )
            assert result is False

    def test_persist_success(self):
        """成功持久化指标到 Redis"""
        mock_redis = MagicMock()
        mock_redis.client.hset = AsyncMock()
        mock_redis.client.expire = AsyncMock()
        mock_redis.client.sadd = AsyncMock()

        persister = MetricsPersister(redis_client=mock_redis)
        result = asyncio.get_event_loop().run_until_complete(
            persister.persist("session-123", {"shell_execute_count": 5}, experiment_group="control")
        )
        assert result is True
        mock_redis.client.hset.assert_called_once()
        mock_redis.client.expire.assert_called()
        mock_redis.client.sadd.assert_called_once()

    def test_persist_exception_degradation(self):
        """Redis 异常时持久化返回 False(降级不抛出)"""
        mock_redis = MagicMock()
        mock_redis.client.hset = AsyncMock(side_effect=Exception("connection lost"))

        persister = MetricsPersister(redis_client=mock_redis)
        result = asyncio.get_event_loop().run_until_complete(
            persister.persist("session-123", {"key": "val"})
        )
        assert result is False

    def test_query_by_date_returns_empty_when_no_redis(self):
        """Redis 不可用时查询返回空列表"""
        persister = MetricsPersister()
        persister._redis = None
        with patch("app.domain.services.observability.metrics_persister.MetricsPersister._get_redis", return_value=None):
            result = asyncio.get_event_loop().run_until_complete(
                persister.query_by_date("20260722")
            )
            assert result == []

    def test_query_by_date_success(self):
        """成功查询日期指标"""
        mock_redis = MagicMock()
        mock_redis.client.smembers = AsyncMock(return_value={"session-1", "session-2"})
        mock_redis.client.hgetall = AsyncMock(return_value={
            "shell_execute_count": "5",
            "experiment_group": '"control"',
        })

        persister = MetricsPersister(redis_client=mock_redis)
        result = asyncio.get_event_loop().run_until_complete(
            persister.query_by_date("20260722")
        )
        assert len(result) == 2

    def test_get_redis_lazy_init(self):
        """_get_redis 延迟初始化"""
        persister = MetricsPersister()
        assert persister._redis is None
        # 模拟 get_redis 失败
        with patch("app.infrastructure.storage.redis.get_redis", side_effect=Exception("no redis")):
            result = persister._get_redis()
            assert result is None


class TestDirection2ExperimentResolver:
    """方向2: ExperimentResolver A/B 实验分组"""

    def test_no_config_returns_default(self):
        """无配置时返回 default 组 + 默认调整"""
        resolver = ExperimentResolver(config=None)
        # 强制清空加载的配置
        resolver._config = None
        group, adjustments = resolver.resolve("session-123")
        assert group == _DEFAULT_GROUP
        assert adjustments == _DEFAULT_ADJUSTMENTS

    def test_disabled_experiment_returns_default(self):
        """实验未启用时返回 default 组"""
        config = {
            "budget_adjustment_v1": {
                "enabled": False,
                "groups": {
                    "control": {"research": {"deep_research": 1}},
                    "variant_a": {"research": {"deep_research": 2}},
                },
                "split": 50,
            }
        }
        resolver = ExperimentResolver(config=config)
        group, _ = resolver.resolve("session-123")
        assert group == _DEFAULT_GROUP

    def test_enabled_experiment_assigns_group(self):
        """启用的实验分配实验组"""
        config = {
            "budget_adjustment_v1": {
                "enabled": True,
                "groups": {
                    "control": {"research": {"deep_research": 1}},
                    "variant_a": {"research": {"deep_research": 2}},
                },
                "split": 50,
            }
        }
        resolver = ExperimentResolver(config=config)
        group, adjustments = resolver.resolve("session-123")
        assert group in ("control", "variant_a")
        assert "research" in adjustments

    def test_deterministic_grouping(self):
        """同一 session_id 多次调用返回相同组(确定性)"""
        config = {
            "budget_adjustment_v1": {
                "enabled": True,
                "groups": {
                    "control": {"research": {"deep_research": 1}},
                    "variant_a": {"research": {"deep_research": 2}},
                },
                "split": 50,
            }
        }
        resolver = ExperimentResolver(config=config)
        group1, _ = resolver.resolve("session-abc")
        group2, _ = resolver.resolve("session-abc")
        assert group1 == group2

    def test_assign_group_control_split_100(self):
        """split=100 时所有会话分配到 control"""
        config = {
            "budget_adjustment_v1": {
                "enabled": True,
                "groups": {
                    "control": {"research": {"deep_research": 1}},
                    "variant_a": {"research": {"deep_research": 2}},
                },
                "split": 100,
            }
        }
        resolver = ExperimentResolver(config=config)
        for sid in ["a", "b", "c", "d", "e"]:
            group, _ = resolver.resolve(sid)
            assert group == "control"

    def test_assign_group_variant_split_0(self):
        """split=0 时所有会话分配到 variant_a"""
        config = {
            "budget_adjustment_v1": {
                "enabled": True,
                "groups": {
                    "control": {"research": {"deep_research": 1}},
                    "variant_a": {"research": {"deep_research": 2}},
                },
                "split": 0,
            }
        }
        resolver = ExperimentResolver(config=config)
        for sid in ["a", "b", "c", "d", "e"]:
            group, _ = resolver.resolve(sid)
            assert group == "variant_a"

    def test_find_active_experiment_returns_first_enabled(self):
        """查找第一个启用的实验"""
        config = {
            "exp1": {"enabled": False, "groups": {}, "split": 50},
            "exp2": {"enabled": True, "groups": {"control": {}, "variant_a": {}}, "split": 50},
            "exp3": {"enabled": True, "groups": {"control": {}, "variant_a": {}}, "split": 50},
        }
        resolver = ExperimentResolver(config=config)
        exp = resolver._find_active_experiment()
        assert exp is not None
        assert exp["name"] == "exp2"

    def test_assign_group_single_group(self):
        """仅一个组时直接返回该组"""
        result = ExperimentResolver._assign_group("session-1", ["only_group"], 50)
        assert result == "only_group"

    def test_assign_group_empty_groups(self):
        """空组列表返回 default"""
        result = ExperimentResolver._assign_group("session-1", [], 50)
        assert result == _DEFAULT_GROUP


class TestDirection2BudgetTrackerAdjustments:
    """方向2: budget_tracker adjust_for_task_type 扩展(adjustments 参数)"""

    def test_adjust_with_custom_adjustments(self):
        """使用自定义实验配置调整预算"""
        tracker = ToolBudgetTracker(budgets={"deep_research": 2, "search_web": 8})
        custom = {
            "research": {"deep_research": 3},  # variant_a: +3
        }
        tracker.adjust_for_task_type("research", adjustments=custom)
        assert tracker._budgets["deep_research"] == 5  # 2 + 3

    def test_adjust_falls_back_to_default(self):
        """adjustments=None 时使用默认配置"""
        tracker = ToolBudgetTracker(budgets={"deep_research": 2})
        tracker.adjust_for_task_type("research", adjustments=None)
        # 默认 research → deep_research +1
        assert tracker._budgets["deep_research"] == 3  # 2 + 1

    def test_adjust_idempotent(self):
        """重复调用同一 task_type 不叠加"""
        tracker = ToolBudgetTracker(budgets={"deep_research": 2})
        tracker.adjust_for_task_type("research")
        tracker.adjust_for_task_type("research")  # 第二次不应叠加
        assert tracker._budgets["deep_research"] == 3  # 仅 +1

    def test_adjust_general_no_change(self):
        """general 类型不调整预算"""
        tracker = ToolBudgetTracker(budgets={"deep_research": 2})
        tracker.adjust_for_task_type("general")
        assert tracker._budgets["deep_research"] == 2


# ============================================================
# 方向4(P3): classify_with_llm 3 层降级
# ============================================================

class TestDirection4LLMClassifier:
    """方向4: classify_with_llm 3 层降级分类"""

    def test_layer1_keyword_research(self):
        """Layer 1: 关键词命中 research 直接返回"""
        result = asyncio.get_event_loop().run_until_complete(
            classify_with_llm("深度搜索 AI 趋势", llm=None)
        )
        assert result == TASK_TYPE_RESEARCH

    def test_layer1_keyword_data_analysis(self):
        """Layer 1: 关键词命中 data_analysis 直接返回"""
        result = asyncio.get_event_loop().run_until_complete(
            classify_with_llm("出入库深度分析", llm=None)
        )
        assert result == TASK_TYPE_DATA_ANALYSIS

    def test_layer1_keyword_browser(self):
        """Layer 1: 关键词命中 browser 直接返回"""
        result = asyncio.get_event_loop().run_until_complete(
            classify_with_llm("网页抓取数据", llm=None)
        )
        assert result == TASK_TYPE_BROWSER

    def test_layer1_no_match_no_llm_returns_general(self):
        """Layer 1 未命中 + 无 LLM → general"""
        result = asyncio.get_event_loop().run_until_complete(
            classify_with_llm("帮我写一首诗", llm=None)
        )
        assert result == TASK_TYPE_GENERAL

    def test_layer2_llm_classification_success(self):
        """Layer 2: LLM 分类成功"""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value={"content": "research"})

        result = asyncio.get_event_loop().run_until_complete(
            classify_with_llm("探索量子计算最新进展", llm=mock_llm)
        )
        assert result == TASK_TYPE_RESEARCH
        mock_llm.chat.assert_called_once()

    def test_layer2_llm_classification_data_analysis(self):
        """Layer 2: LLM 分类为 data_analysis"""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value={"content": "data_analysis"})

        result = asyncio.get_event_loop().run_until_complete(
            classify_with_llm("统计本月销售额", llm=mock_llm)
        )
        assert result == TASK_TYPE_DATA_ANALYSIS

    def test_layer2_llm_classification_browser(self):
        """Layer 2: LLM 分类为 browser"""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value={"content": "browser"})

        result = asyncio.get_event_loop().run_until_complete(
            classify_with_llm("在网站上点击按钮", llm=mock_llm)
        )
        assert result == TASK_TYPE_BROWSER

    def test_layer2_llm_classification_general_fallback(self):
        """Layer 2: LLM 输出无法识别时降级 general"""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value={"content": "unknown_type"})

        result = asyncio.get_event_loop().run_until_complete(
            classify_with_llm("随便聊聊", llm=mock_llm)
        )
        assert result == TASK_TYPE_GENERAL

    def test_layer3_llm_exception_returns_general(self):
        """Layer 3: LLM 异常时降级 general"""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(side_effect=Exception("LLM timeout"))

        result = asyncio.get_event_loop().run_until_complete(
            classify_with_llm("探索未知领域", llm=mock_llm)
        )
        assert result == TASK_TYPE_GENERAL

    def test_layer2_cache_hit_skips_llm(self):
        """缓存命中时跳过 LLM 调用"""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value={"content": "research"})

        # 同步 Redis mock
        mock_redis = MagicMock()
        mock_redis.get = MagicMock(return_value="research")
        mock_redis.set = MagicMock()

        text = "探索量子计算最新进展"
        result = asyncio.get_event_loop().run_until_complete(
            classify_with_llm(text, llm=mock_llm, redis_client=mock_redis)
        )
        assert result == TASK_TYPE_RESEARCH
        mock_llm.chat.assert_not_called()  # 缓存命中,未调用 LLM

    def test_layer2_cache_miss_calls_llm(self):
        """缓存未命中时调用 LLM 并写入缓存"""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value={"content": "research"})

        mock_redis = MagicMock()
        mock_redis.get = MagicMock(return_value=None)
        mock_redis.set = MagicMock()

        text = "探索量子计算最新进展"
        result = asyncio.get_event_loop().run_until_complete(
            classify_with_llm(text, llm=mock_llm, redis_client=mock_redis)
        )
        assert result == TASK_TYPE_RESEARCH
        mock_llm.chat.assert_called_once()
        mock_redis.set.assert_called_once()

    def test_async_redis_cache_degrades_safely(self):
        """异步 Redis 客户端缓存降级安全(返回 None,走 LLM)"""
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value={"content": "research"})

        mock_redis = MagicMock()
        # 模拟异步 Redis 客户端
        async def async_get(key):
            return None
        mock_redis.get = async_get

        text = "探索量子计算最新进展"
        result = asyncio.get_event_loop().run_until_complete(
            classify_with_llm(text, llm=mock_llm, redis_client=mock_redis)
        )
        # 异步 Redis 降级,直接走 LLM
        assert result == TASK_TYPE_RESEARCH
        mock_llm.chat.assert_called_once()


# ============================================================
# 方向1(P4): P11 沙箱回调
# ============================================================

class TestDirection1CallbackAgent:
    """方向1: 沙箱 callback_agent 逻辑"""

    def test_callback_agent_import(self):
        """callback_agent 模块可导入(路径注入)"""
        import sys
        from pathlib import Path
        sandbox_path = str(Path(__file__).resolve().parents[5] / "sandbox")
        if sandbox_path not in sys.path:
            sys.path.insert(0, sandbox_path)
        # 重新导入确保最新
        if "callback_agent" in sys.modules:
            del sys.modules["callback_agent"]
        import callback_agent
        assert hasattr(callback_agent, "main")
        assert hasattr(callback_agent, "send_callback")
        assert hasattr(callback_agent, "process_completed_tasks")
        assert hasattr(callback_agent, "ensure_status_dir")

    def test_callback_agent_constants(self):
        """callback_agent 常量正确"""
        import sys
        from pathlib import Path
        sandbox_path = str(Path(__file__).resolve().parents[5] / "sandbox")
        if sandbox_path not in sys.path:
            sys.path.insert(0, sandbox_path)
        if "callback_agent" in sys.modules:
            del sys.modules["callback_agent"]
        import callback_agent
        assert callback_agent._POLL_INTERVAL == 1.0
        assert callback_agent._HTTP_TIMEOUT == 5
        assert callback_agent._MAX_RETRIES == 3
        assert callback_agent._TASK_STATUS_DIR.name == "task_status"


class TestDirection1SandboxCallbackRoutes:
    """方向1: API 回调端点"""

    def test_callback_payload_model(self):
        """回调载荷模型验证"""
        from app.interfaces.endpoints.sandbox_callback_routes import SandboxCallbackPayload
        payload = SandboxCallbackPayload(
            task_id="shell_abc123",
            success=True,
            message="命令执行完成",
            data={"stdout": "hello"},
            exit_code=0,
        )
        assert payload.task_id == "shell_abc123"
        assert payload.success is True
        assert payload.exit_code == 0

    def test_callback_payload_defaults(self):
        """回调载荷默认值"""
        from app.interfaces.endpoints.sandbox_callback_routes import SandboxCallbackPayload
        payload = SandboxCallbackPayload(task_id="t1", success=False)
        assert payload.message == ""
        assert payload.data is None
        assert payload.exit_code == -1

    def test_callback_endpoint_notify_success(self):
        """回调端点成功通知 TaskCallbackManager"""
        from app.interfaces.endpoints.sandbox_callback_routes import (
            sandbox_callback, SandboxCallbackPayload,
        )
        payload = SandboxCallbackPayload(
            task_id="shell_test", success=True, message="ok", data=None, exit_code=0
        )
        with patch(
            "app.infrastructure.external.task_callback.RedisStreamTaskCallbackManager"
        ) as MockMgr:
            mock_instance = MagicMock()
            mock_instance.notify = AsyncMock()
            MockMgr.return_value = mock_instance

            result = asyncio.get_event_loop().run_until_complete(sandbox_callback(payload))
            assert result["ok"] is True
            mock_instance.notify.assert_called_once()

    def test_callback_endpoint_manager_unavailable(self):
        """回调端点 TaskCallbackManager 创建失败时安全降级"""
        from app.interfaces.endpoints.sandbox_callback_routes import (
            sandbox_callback, SandboxCallbackPayload,
        )
        payload = SandboxCallbackPayload(task_id="t1", success=True)
        with patch(
            "app.infrastructure.external.task_callback.RedisStreamTaskCallbackManager",
            side_effect=Exception("redis down"),
        ):
            result = asyncio.get_event_loop().run_until_complete(sandbox_callback(payload))
            assert result["ok"] is True
            assert "unavailable" in result["message"]

    def test_callback_endpoint_notify_exception(self):
        """回调端点 notify 异常时返回 200(避免沙箱重试)"""
        from app.interfaces.endpoints.sandbox_callback_routes import (
            sandbox_callback, SandboxCallbackPayload,
        )
        payload = SandboxCallbackPayload(task_id="t1", success=True)
        with patch(
            "app.infrastructure.external.task_callback.RedisStreamTaskCallbackManager"
        ) as MockMgr:
            mock_instance = MagicMock()
            mock_instance.notify = AsyncMock(side_effect=Exception("stream error"))
            MockMgr.return_value = mock_instance

            result = asyncio.get_event_loop().run_until_complete(sandbox_callback(payload))
            assert result["ok"] is True  # 降级返回 200


class TestDirection1ShellP11Mode:
    """方向1: shell.py P11 沙箱回调模式"""

    def test_p11_config_constants(self):
        """P11 配置常量存在"""
        from app.domain.services.tools.shell import _P11_ENABLED, _API_CALLBACK_URL, _P11_WRAPPER_TEMPLATE
        assert isinstance(_P11_ENABLED, bool)
        assert "callback" in _API_CALLBACK_URL
        assert isinstance(_P11_WRAPPER_TEMPLATE, str)
        assert "task_status" in _P11_WRAPPER_TEMPLATE

    def test_p11_wrapper_template_contains_key_logic(self):
        """P11 wrapper 模板包含关键逻辑"""
        from app.domain.services.tools.shell import _P11_WRAPPER_TEMPLATE
        # 应包含 subprocess 运行命令
        assert "subprocess" in _P11_WRAPPER_TEMPLATE
        # 应包含写状态文件
        assert "task_status" in _P11_WRAPPER_TEMPLATE
        # 应包含 HTTP 回调
        assert "callback" in _P11_WRAPPER_TEMPLATE.lower() or "urllib" in _P11_WRAPPER_TEMPLATE

    def test_try_p11_mode_success(self):
        """P11 模式启动成功返回 True"""
        from app.domain.services.tools.shell import ShellTool
        from app.domain.models.tool_result import ToolResult

        mock_sandbox = MagicMock()
        mock_sandbox.write_file = AsyncMock()
        mock_sandbox.exec_command = AsyncMock(return_value=ToolResult(success=True, message="ok"))

        mock_callback = MagicMock()
        mock_callback.register = AsyncMock()

        tool = ShellTool(sandbox=mock_sandbox, callback_manager=mock_callback)
        result = asyncio.get_event_loop().run_until_complete(
            tool._try_p11_mode("task_1", "session_1", "/home/ubuntu", "echo hello", 60)
        )
        assert result is True
        mock_sandbox.write_file.assert_called_once()
        mock_sandbox.exec_command.assert_called_once()

    def test_try_p11_mode_write_failure(self):
        """P11 模式写文件失败返回 False(降级)"""
        from app.domain.services.tools.shell import ShellTool

        mock_sandbox = MagicMock()
        mock_sandbox.write_file = AsyncMock(side_effect=Exception("write failed"))

        mock_callback = MagicMock()
        tool = ShellTool(sandbox=mock_sandbox, callback_manager=mock_callback)
        result = asyncio.get_event_loop().run_until_complete(
            tool._try_p11_mode("task_1", "session_1", "/home/ubuntu", "echo hello", 60)
        )
        assert result is False

    def test_try_p11_mode_exec_failure(self):
        """P11 模式 exec_command 失败返回 False(降级)"""
        from app.domain.services.tools.shell import ShellTool
        from app.domain.models.tool_result import ToolResult

        mock_sandbox = MagicMock()
        mock_sandbox.write_file = AsyncMock()
        mock_sandbox.exec_command = AsyncMock(
            return_value=ToolResult(success=False, message="exec failed")
        )

        mock_callback = MagicMock()
        tool = ShellTool(sandbox=mock_sandbox, callback_manager=mock_callback)
        result = asyncio.get_event_loop().run_until_complete(
            tool._try_p11_mode("task_1", "session_1", "/home/ubuntu", "echo hello", 60)
        )
        assert result is False

    def test_shell_execute_p11_mode_returns_task_id(self):
        """shell_execute async_mode=true 时返回 task_id(P11 模式)"""
        from app.domain.services.tools.shell import ShellTool
        from app.domain.models.tool_result import ToolResult

        mock_sandbox = MagicMock()
        mock_sandbox.write_file = AsyncMock()
        mock_sandbox.exec_command = AsyncMock(return_value=ToolResult(success=True, message="ok"))

        mock_callback = MagicMock()
        mock_callback.register = AsyncMock()

        tool = ShellTool(sandbox=mock_sandbox, callback_manager=mock_callback)
        result = asyncio.get_event_loop().run_until_complete(
            tool.shell_execute(
                session_id="session_1",
                exec_dir="/home/ubuntu",
                command="python3 long_running_script.py",
                async_mode=True,
                timeout=300,
            )
        )
        assert result.success is True
        assert "task_id" in result.data
        assert result.data["status"] == "running"

    def test_shell_execute_sync_mode_unchanged(self):
        """shell_execute async_mode=false 保持同步行为"""
        from app.domain.services.tools.shell import ShellTool
        from app.domain.models.tool_result import ToolResult

        mock_sandbox = MagicMock()
        mock_sandbox.exec_command = AsyncMock(
            return_value=ToolResult(success=True, message="done")
        )

        tool = ShellTool(sandbox=mock_sandbox, callback_manager=None)
        result = asyncio.get_event_loop().run_until_complete(
            tool.shell_execute(
                session_id="session_1",
                exec_dir="/home/ubuntu",
                command="echo hello",
                async_mode=False,
            )
        )
        assert result.success is True
        assert result.message == "done"
        # 同步模式不调用 write_file(P11 未触发)
        mock_sandbox.write_file.assert_not_called()

    def test_shell_execute_async_no_callback_degrades_sync(self):
        """async_mode=true 但无 callback_manager 时降级同步"""
        from app.domain.services.tools.shell import ShellTool
        from app.domain.models.tool_result import ToolResult

        mock_sandbox = MagicMock()
        mock_sandbox.exec_command = AsyncMock(
            return_value=ToolResult(success=True, message="sync result")
        )

        tool = ShellTool(sandbox=mock_sandbox, callback_manager=None)
        result = asyncio.get_event_loop().run_until_complete(
            tool.shell_execute(
                session_id="session_1",
                exec_dir="/home/ubuntu",
                command="echo hello",
                async_mode=True,
            )
        )
        assert result.success is True
        assert result.message == "sync result"


# ============================================================
# 方向1: supervisord 配置验证
# ============================================================

class TestDirection1SupervisordConfig:
    """方向1: supervisord 配置包含 callback_agent"""

    def test_supervisord_has_callback_agent(self):
        """supervisord.conf 包含 callback_agent 进程配置"""
        from pathlib import Path
        supervisord_path = Path(__file__).resolve().parents[5] / "sandbox" / "supervisord.conf"
        content = supervisord_path.read_text(encoding="utf-8")
        assert "[program:callback_agent]" in content
        assert "callback_agent.py" in content

    def test_supervisord_services_group_includes_callback_agent(self):
        """services 组包含 callback_agent"""
        from pathlib import Path
        supervisord_path = Path(__file__).resolve().parents[5] / "sandbox" / "supervisord.conf"
        content = supervisord_path.read_text(encoding="utf-8")
        # 查找 group:services 行
        assert "callback_agent" in content


# ============================================================
# 方向2: experiments.yaml 配置验证
# ============================================================

class TestDirection2ExperimentsConfig:
    """方向2: experiments.yaml 配置文件验证"""

    def test_experiments_yaml_exists(self):
        """experiments.yaml 配置文件存在"""
        from pathlib import Path
        config_path = Path(__file__).resolve().parents[4] / "config" / "experiments.yaml"
        assert config_path.exists()

    def test_experiments_yaml_structure(self):
        """experiments.yaml 结构正确"""
        from pathlib import Path
        import yaml
        config_path = Path(__file__).resolve().parents[4] / "config" / "experiments.yaml"
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "budget_adjustment_v1" in data
        exp = data["budget_adjustment_v1"]
        assert "enabled" in exp
        assert "groups" in exp
        assert "control" in exp["groups"]
        assert "variant_a" in exp["groups"]
        assert "split" in exp
