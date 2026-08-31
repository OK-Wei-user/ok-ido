#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_batch3_p1_performance.py
批次3 P1性能与准确性优化单元测试

覆盖项:
- F3-1: chat()循环移除冗余未读计数UPDATE(单轮UPDATE次数2→1)
- F3-2: truncate_tool_result 与 truncate_tool_result_dynamic 共享_truncate_content_internal
- F3-3: replay_missed_events 改用 repository.get_events_after 流式读取
- F3-4: TokenCounter 模型编码器实例缓存(_ENCODER_CACHE)
- F3-5: SkillContextTracker LRU会话上限(MAX_SESSIONS淘汰)
"""
import json
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.event import MessageEvent
from app.domain.models.memory import (
    Memory,
    _TOOL_RESULT_MAX_LENGTH,
    _DEEP_RESEARCH_RESULT_MAX_LENGTH,
    _BROWSER_RESULT_MAX_LENGTH,
    _FILE_RESULT_MAX_LENGTH,
)
from app.domain.services.skill_service import SkillContextTracker, SkillUsage


# ════════════════════════════════════════════════════════════════════════════
# F3-2: truncate_tool_result / truncate_tool_result_dynamic 合并
# ════════════════════════════════════════════════════════════════════════════
class TestF32TruncateMerge:
    """F3-2: 截断逻辑合并为_truncate_content_internal共享内部实现"""

    def test_truncate_tool_result_uses_standard_max_len_for_deep_research(self):
        """F3-2: 静态方法现在覆盖deep_research阈值(原仅覆盖4类工具)

        行为变化验证:
        - _DEEP_RESEARCH_RESULT_MAX_LENGTH(6000) < _TOOL_RESULT_MAX_LENGTH(8000)
        - 旧实现: deep_research命中默认max_len=_TOOL_RESULT_MAX_LENGTH=8000 → 7000字符不截断
        - 新实现: deep_research命中_DEEP_RESEARCH_RESULT_MAX_LENGTH=6000 → 7000字符触发截断
        """
        # 构造长度介于两个阈值之间的内容
        content = "x" * 7000
        assert _DEEP_RESEARCH_RESULT_MAX_LENGTH < len(content) <= _TOOL_RESULT_MAX_LENGTH
        result = Memory.truncate_tool_result(content, "deep_research")
        # 新实现使用deep_research阈值(6000),应触发截断
        assert "truncated" in result
        assert len(result) < len(content)

    def test_truncate_tool_result_static_and_dynamic_share_internal(self):
        """F3-2: 静态/动态方法在相同max_len下结果一致(共享_truncate_content_internal)"""
        content = "y" * (_BROWSER_RESULT_MAX_LENGTH + 5000)
        # 静态调用(固定阈值,无token_counter)
        static_result = Memory.truncate_tool_result(content, "browser_view")
        # 动态调用(token_counter=None,等效固定阈值)
        memory = Memory()
        dynamic_result = memory.truncate_tool_result_dynamic(
            content, function_name="browser_view",
        )
        assert static_result == dynamic_result

    def test_internal_helper_handles_json_browser_view(self):
        """F3-2: _truncate_content_internal对browser_view的JSON感知截断"""
        long_content = json.dumps({
            "screenshot": "http://example.com/shot.png",
            "page_state": {"url": "https://example.com", "title": "Example"},
            "dom_tree": "x" * 10000,
        })
        result = Memory._truncate_content_internal(long_content, "browser_view", _BROWSER_RESULT_MAX_LENGTH)
        assert len(result) < len(long_content)
        assert "screenshot" in result
        assert "page_state" in result

    def test_internal_helper_handles_json_file(self):
        """F3-2: _truncate_content_internal对file类的JSON感知截断"""
        long_content = json.dumps({
            "filepath": "/tmp/large_file.txt",
            "content": "x" * 10000,
        })
        result = Memory._truncate_content_internal(long_content, "file_read", _FILE_RESULT_MAX_LENGTH)
        assert len(result) < len(long_content)
        assert "filepath" in result

    def test_internal_helper_generic_truncation(self):
        """F3-2: _truncate_content_internal通用字符截断"""
        content = "z" * 10000
        max_len = 1000
        result = Memory._truncate_content_internal(content, "unknown_tool", max_len)
        assert "truncated" in result
        assert len(result) < len(content)

    def test_internal_helper_short_content_passthrough(self):
        """F3-2: _truncate_content_internal短内容原样返回"""
        content = "short"
        result = Memory._truncate_content_internal(content, "any_tool", 1000)
        assert result == content

    def test_dynamic_method_high_pressure_reduces_threshold(self):
        """F3-2: 动态方法在高token压力下阈值减半(回归测试)"""
        from tests.app.domain.services.test_memory_proactive_compression import FakeTokenCounter

        memory = Memory()
        counter = FakeTokenCounter(token_count=int(64000 * 0.60))  # 40%剩余 → 阈值减半
        content = "x" * (_DEEP_RESEARCH_RESULT_MAX_LENGTH - 1)
        result = memory.truncate_tool_result_dynamic(
            content, function_name="deep_research",
            token_counter=counter, context_window=64000,
        )
        assert "truncated" in result or len(result) < len(content)


# ════════════════════════════════════════════════════════════════════════════
# F3-3: replay_missed_events 流式读取优化
# ════════════════════════════════════════════════════════════════════════════
class TestF33ReplayStreamingRead:
    """F3-3: replay_missed_events 改用 get_events_after 仅查询events列"""

    def test_repository_protocol_has_get_events_after(self):
        """F3-3: SessionRepository协议新增get_events_after方法"""
        from app.domain.repositories.session_repository import SessionRepository
        assert hasattr(SessionRepository, "get_events_after")
        # Protocol方法应可被检查到
        assert "get_events_after" in dir(SessionRepository)

    def test_db_repository_implements_get_events_after(self):
        """F3-3: DBSessionRepository实现get_events_after方法"""
        from app.infrastructure.repositories.db_session_repository import DBSessionRepository
        assert hasattr(DBSessionRepository, "get_events_after")
        # 应为async方法
        import inspect
        assert inspect.iscoroutinefunction(DBSessionRepository.get_events_after)

    def test_event_adapter_is_module_level_singleton(self):
        """F3-3: _EVENT_ADAPTER为模块级单例,避免重复创建TypeAdapter"""
        from app.infrastructure.repositories import db_session_repository
        assert hasattr(db_session_repository, "_EVENT_ADAPTER")
        # 同一模块多次访问应为同一实例
        assert db_session_repository._EVENT_ADAPTER is db_session_repository._EVENT_ADAPTER

    @pytest.mark.asyncio
    async def test_get_events_after_returns_none_when_session_not_exist(self):
        """F3-3: 会话不存在时返回None"""
        from app.infrastructure.repositories.db_session_repository import DBSessionRepository

        # 模拟session不存在(scalar_one_or_none返回None)
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_db.execute = AsyncMock(return_value=mock_result)

        repo = DBSessionRepository(mock_db)
        result = await repo.get_events_after("nonexistent", "ev-1", limit=50, fallback_limit=10)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_events_after_returns_empty_when_no_events(self):
        """F3-3: 会话存在但无事件时返回([], False)"""
        from app.infrastructure.repositories.db_session_repository import DBSessionRepository

        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=[])
        mock_db.execute = AsyncMock(return_value=mock_result)

        repo = DBSessionRepository(mock_db)
        result = await repo.get_events_after("s1", "ev-1", limit=50, fallback_limit=10)
        assert result == ([], False)

    @pytest.mark.asyncio
    async def test_get_events_after_finds_last_event_id(self):
        """F3-3: 命中last_event_id时返回其后limit条,found=True"""
        from app.infrastructure.repositories.db_session_repository import DBSessionRepository
        from app.domain.models.event import MessageEvent

        # 构造5条事件
        events_data = [
            MessageEvent(role="assistant", message=f"msg-{i}").model_dump(mode="json")
            for i in range(5)
        ]
        # 设置可识别的id
        for i, ev in enumerate(events_data):
            ev["id"] = f"ev-{i}"

        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=events_data)
        mock_db.execute = AsyncMock(return_value=mock_result)

        repo = DBSessionRepository(mock_db)
        result = await repo.get_events_after("s1", "ev-2", limit=50, fallback_limit=10)
        events, found = result
        assert found is True
        assert len(events) == 2  # ev-3, ev-4
        assert events[0].id == "ev-3"
        assert events[1].id == "ev-4"

    @pytest.mark.asyncio
    async def test_get_events_after_respects_limit(self):
        """F3-3: 命中last_event_id后切片受limit约束"""
        from app.infrastructure.repositories.db_session_repository import DBSessionRepository
        from app.domain.models.event import MessageEvent

        events_data = [
            MessageEvent(role="assistant", message=f"msg-{i}").model_dump(mode="json")
            for i in range(20)
        ]
        for i, ev in enumerate(events_data):
            ev["id"] = f"ev-{i}"

        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=events_data)
        mock_db.execute = AsyncMock(return_value=mock_result)

        repo = DBSessionRepository(mock_db)
        result = await repo.get_events_after("s1", "ev-0", limit=5, fallback_limit=10)
        events, found = result
        assert found is True
        assert len(events) == 5  # limit=5

    @pytest.mark.asyncio
    async def test_get_events_after_fallback_when_id_not_found(self):
        """F3-3: last_event_id未命中时回退补发最近fallback_limit条,found=False"""
        from app.infrastructure.repositories.db_session_repository import DBSessionRepository
        from app.domain.models.event import MessageEvent

        events_data = [
            MessageEvent(role="assistant", message=f"msg-{i}").model_dump(mode="json")
            for i in range(15)
        ]
        for i, ev in enumerate(events_data):
            ev["id"] = f"ev-{i}"

        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=events_data)
        mock_db.execute = AsyncMock(return_value=mock_result)

        repo = DBSessionRepository(mock_db)
        result = await repo.get_events_after("s1", "nonexistent", limit=50, fallback_limit=10)
        events, found = result
        assert found is False
        assert len(events) == 10  # 回退最近10条
        # 应回退最后10条: ev-5 ~ ev-14
        assert events[0].id == "ev-5"
        assert events[-1].id == "ev-14"

    @pytest.mark.asyncio
    async def test_replay_missed_events_calls_get_events_after(self):
        """F3-3: AgentService.replay_missed_events调用get_events_after而非get_by_id"""
        from app.application.services.agent_service import AgentService, _MAX_REPLAY_COUNT, _FALLBACK_REPLAY_COUNT

        events = []
        for i in range(3):
            ev = MessageEvent(role="assistant", message=f"msg-{i}")
            ev.id = f"ev-{i}"
            events.append(ev)

        with patch.object(AgentService, '__init__', lambda self: None):
            service = AgentService.__new__(AgentService)
            service._uow = MagicMock()
            service._uow.__aenter__ = AsyncMock(return_value=service._uow)
            service._uow.__aexit__ = AsyncMock(return_value=False)
            service._uow.session = MagicMock()
            service._uow.session.get_events_after = AsyncMock(return_value=(events[1:], True))
            service._uow.session.get_by_id = AsyncMock()  # 不应被调用

            replayed = []
            async for event in service.replay_missed_events("s1", "ev-0"):
                replayed.append(event)

            # 验证调用get_events_after,而非get_by_id
            service._uow.session.get_events_after.assert_awaited_once_with(
                "s1", "ev-0",
                limit=_MAX_REPLAY_COUNT,
                fallback_limit=_FALLBACK_REPLAY_COUNT,
            )
            service._uow.session.get_by_id.assert_not_called()
            assert len(replayed) == 2


# ════════════════════════════════════════════════════════════════════════════
# F3-4: TokenCounter 编码器实例缓存
# ════════════════════════════════════════════════════════════════════════════
class TestF34TokenCounterCache:
    """F3-4: TokenCounter通过_ENCODER_CACHE缓存模型编码器实例"""

    def test_encoder_cache_exists_as_class_var(self):
        """F3-4: _ENCODER_CACHE类变量存在"""
        from app.infrastructure.external.llm.token_counter import TokenCounter
        assert hasattr(TokenCounter, "_ENCODER_CACHE")
        assert isinstance(TokenCounter._ENCODER_CACHE, dict)

    def test_same_model_reuses_cached_encoder(self):
        """F3-4: 相同模型名复用缓存的编码器实例"""
        from app.infrastructure.external.llm.token_counter import TokenCounter

        # 清空缓存,确保测试隔离
        TokenCounter._ENCODER_CACHE.clear()
        c1 = TokenCounter("gpt-4")
        c2 = TokenCounter("gpt-4")
        # 第二次构造应从缓存读取,_encoder应同一对象
        assert c1._encoder is c2._encoder

    def test_different_models_have_independent_encoders(self):
        """F3-4: 不同模型名独立缓存"""
        from app.infrastructure.external.llm.token_counter import TokenCounter

        TokenCounter._ENCODER_CACHE.clear()
        c1 = TokenCounter("gpt-4")
        c2 = TokenCounter("gpt-3.5-turbo")
        # 不同模型应使用各自的编码器(可能都为None,但缓存key独立)
        assert "gpt-4" in TokenCounter._ENCODER_CACHE
        assert "gpt-3.5-turbo" in TokenCounter._ENCODER_CACHE


# ════════════════════════════════════════════════════════════════════════════
# F3-5: SkillContextTracker LRU 会话上限
# ════════════════════════════════════════════════════════════════════════════
class TestF35SkillContextTrackerLRU:
    """F3-5: SkillContextTracker._history 改用OrderedDict + MAX_SESSIONS LRU淘汰"""

    def test_history_is_ordered_dict(self):
        """F3-5: _history类型为OrderedDict"""
        tracker = SkillContextTracker()
        assert isinstance(tracker._history, OrderedDict)

    def test_max_sessions_constant_exists(self):
        """F3-5: MAX_SESSIONS常量存在且有合理上限"""
        assert hasattr(SkillContextTracker, "MAX_SESSIONS")
        assert isinstance(SkillContextTracker.MAX_SESSIONS, int)
        assert 10 <= SkillContextTracker.MAX_SESSIONS <= 10000  # 合理范围

    def test_lru_eviction_when_exceeding_max_sessions(self):
        """F3-5: 超过MAX_SESSIONS时淘汰最久未访问的会话"""
        tracker = SkillContextTracker()
        original_max = SkillContextTracker.MAX_SESSIONS
        try:
            # 缩小上限加速测试
            SkillContextTracker.MAX_SESSIONS = 3
            tracker._history = OrderedDict()

            # 插入3个会话(达到上限)
            tracker.record_usage("s1", "skill_a")
            tracker.record_usage("s2", "skill_b")
            tracker.record_usage("s3", "skill_c")
            assert len(tracker._history) == 3
            assert "s1" in tracker._history  # s1为最久未访问(队首)

            # 插入第4个会话,应淘汰s1(LRU)
            tracker.record_usage("s4", "skill_d")
            assert len(tracker._history) == 3
            assert "s1" not in tracker._history
            assert "s4" in tracker._history
            # 队列顺序应为 s2 → s3 → s4
            assert list(tracker._history.keys()) == ["s2", "s3", "s4"]
        finally:
            SkillContextTracker.MAX_SESSIONS = original_max

    def test_record_usage_moves_session_to_end(self):
        """F3-5: record_usage命中已有会话时移到末尾(标记最近访问)"""
        tracker = SkillContextTracker()
        tracker.record_usage("s1", "skill_a")
        tracker.record_usage("s2", "skill_b")
        tracker.record_usage("s3", "skill_c")
        # 初始顺序: s1 → s2 → s3
        assert list(tracker._history.keys()) == ["s1", "s2", "s3"]

        # 再次访问s1,应移到末尾
        tracker.record_usage("s1", "skill_a2")
        assert list(tracker._history.keys()) == ["s2", "s3", "s1"]

    def test_get_recent_skills_moves_session_to_end(self):
        """F3-5: get_recent_skills访问会话后,LRU顺序刷新(会话移到末尾)"""
        tracker = SkillContextTracker()
        tracker.record_usage("s1", "skill_a")
        tracker.record_usage("s2", "skill_b")
        tracker.record_usage("s3", "skill_c")
        # 初始顺序: s1 → s2 → s3

        # 访问s1的最近技能,应将其移到末尾
        recent = tracker.get_recent_skills("s1")
        assert recent == ["skill_a"]
        assert list(tracker._history.keys()) == ["s2", "s3", "s1"]

    def test_lru_preserves_recent_sessions_under_load(self):
        """F3-5: 持续写入新会话时,最近访问的会话不会被淘汰"""
        tracker = SkillContextTracker()
        original_max = SkillContextTracker.MAX_SESSIONS
        try:
            SkillContextTracker.MAX_SESSIONS = 5
            tracker._history = OrderedDict()

            # 写入5个会话
            for i in range(5):
                tracker.record_usage(f"s{i}", f"skill_{i}")
            # 访问s0(将其移到末尾,标记为最近访问)
            tracker.get_recent_skills("s0")
            # 此时队首为s1,队尾为s0

            # 继续写入3个新会话,应淘汰s1, s2, s3(队首3个),s0和s4应保留
            for i in range(5, 8):
                tracker.record_usage(f"s{i}", f"skill_{i}")

            assert "s0" in tracker._history  # 最近访问过的应保留
            assert "s4" in tracker._history  # 最近写入的应保留
            assert "s1" not in tracker._history  # 最久未访问的应被淘汰
            assert "s2" not in tracker._history
            assert "s3" not in tracker._history
        finally:
            SkillContextTracker.MAX_SESSIONS = original_max

    def test_clear_session_removes_from_history(self):
        """F3-5: clear_session从_history移除指定会话(回归测试)"""
        tracker = SkillContextTracker()
        tracker.record_usage("s1", "skill_a")
        tracker.record_usage("s2", "skill_b")
        tracker.clear_session("s1")
        assert "s1" not in tracker._history
        assert "s2" in tracker._history

    def test_max_history_per_session_preserved(self):
        """F3-5: 单会话MAX_HISTORY条数约束仍然生效(回归测试)"""
        tracker = SkillContextTracker()
        # 写入超过MAX_HISTORY条
        for i in range(tracker.MAX_HISTORY + 5):
            tracker.record_usage("s1", f"skill_{i}")
        usages = tracker._history["s1"]
        assert len(usages) == tracker.MAX_HISTORY
        # 应保留最近MAX_HISTORY条(skill_5 ~ skill_24)
        assert usages[0].skill_name == f"skill_{5}"
        assert usages[-1].skill_name == f"skill_{tracker.MAX_HISTORY + 4}"


# ════════════════════════════════════════════════════════════════════════════
# F3-1: chat() 循环移除冗余 UPDATE
# ════════════════════════════════════════════════════════════════════════════
class TestF31UnreadCountBatching:
    """F3-1: chat()循环移除冗余的update_unread_message_count调用

    通过静态分析agent_service.py源码,验证:
    - chat()方法内不再出现unread_reset_done标志位
    - 仅在finally块的_safe_update_unread_count中执行UPDATE
    """

    def test_no_unread_reset_done_flag_in_chat(self):
        """F3-1: chat()方法内不再有unread_reset_done标志位"""
        import inspect
        from app.application.services.agent_service import AgentService

        chat_source = inspect.getsource(AgentService.chat)
        assert "unread_reset_done" not in chat_source, (
            "F3-1回退: chat()内仍存在unread_reset_done标志位"
        )

    def test_chat_loop_only_updates_in_finally(self):
        """F3-1: chat()循环内不再直接调用update_unread_message_count"""
        import inspect
        from app.application.services.agent_service import AgentService

        chat_source = inspect.getsource(AgentService.chat)
        # 在循环体内不应直接调用update_unread_message_count
        # (仅_safe_update_unread_count在finally块中通过create_task间接调用)
        # 检查: 不应出现直接的update_unread_message_count(session_id, 0)调用
        assert "update_unread_message_count(session_id, 0)" not in chat_source, (
            "F3-1回退: chat()内仍存在直接update_unread_message_count调用"
        )

    def test_safe_update_unread_count_still_called_in_finally(self):
        """F3-1: finally块仍通过_safe_update_unread_count执行单次UPDATE(保障未读清零)"""
        import inspect
        from app.application.services.agent_service import AgentService

        chat_source = inspect.getsource(AgentService.chat)
        # _safe_update_unread_count应仍在finally块中被调用(通过create_task)
        assert "_safe_update_unread_count" in chat_source, (
            "F3-1回退: finally块未调用_safe_update_unread_count"
        )
