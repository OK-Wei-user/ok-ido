#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_poll_stats.py
批次 26 - PollStatsCollector 单元测试

验证 MCP 工具轮询统计收集器的核心行为:
1. 会话级单例隔离(同一 session_id 返回同一实例)
2. record_invoke 正确递增 invoke/pending/completed 计数
3. record_backoff_trigger 区分参数级与工具级退避
4. record_async_task 递增后台异步任务总数
5. snapshot 返回包含所有字段的字典
6. reset 清空所有计数器
"""
import pytest

from app.infrastructure.metrics.poll_stats import PollStatsCollector


class TestPollStatsCollectorSingleton:
    """会话级单例隔离测试"""

    def test_singleton_per_session(self):
        """同一 session_id 应返回同一实例"""
        # Given
        PollStatsCollector._instances.clear()  # 重置类状态

        # When
        instance1 = PollStatsCollector.get_or_create("session_001")
        instance2 = PollStatsCollector.get_or_create("session_001")

        # Then
        assert instance1 is instance2, "同一 session_id 应返回同一实例"

    def test_different_sessions_have_different_instances(self):
        """不同 session_id 应返回不同实例"""
        # Given
        PollStatsCollector._instances.clear()

        # When
        instance1 = PollStatsCollector.get_or_create("session_A")
        instance2 = PollStatsCollector.get_or_create("session_B")

        # Then
        assert instance1 is not instance2, "不同 session_id 应返回不同实例"

    def test_none_session_uses_default_bucket(self):
        """session_id=None 应使用 _default_ 桶(向后兼容)"""
        # Given
        PollStatsCollector._instances.clear()

        # When
        instance1 = PollStatsCollector.get_or_create(None)
        instance2 = PollStatsCollector.get_or_create(None)

        # Then
        assert instance1 is instance2, "None session_id 应共享同一 _default_ 桶"


class TestPollStatsCollectorRecordInvoke:
    """record_invoke 计数测试"""

    def test_record_invoke_increments_counts(self):
        """调用 record_invoke 后 invoke_counts 应正确递增"""
        # Given
        PollStatsCollector._instances.clear()
        collector = PollStatsCollector.get_or_create("test_invoke")

        # When
        collector.record_invoke("mcp_xxx_query", is_pending=True)
        collector.record_invoke("mcp_xxx_query", is_pending=True)
        collector.record_invoke("mcp_xxx_query", is_pending=False)
        collector.record_invoke("mcp_yyy_status", is_pending=False)

        # Then
        snapshot = collector.snapshot()
        assert snapshot["invoke_counts"]["mcp_xxx_query"] == 3
        assert snapshot["invoke_counts"]["mcp_yyy_status"] == 1
        assert snapshot["pending_counts"]["mcp_xxx_query"] == 2
        assert snapshot["completed_counts"]["mcp_xxx_query"] == 1
        assert snapshot["completed_counts"]["mcp_yyy_status"] == 1


class TestPollStatsCollectorBackoff:
    """record_backoff_trigger 退避级别测试"""

    def test_record_backoff_trigger_tracks_levels(self):
        """参数级与工具级退避应分别记录"""
        # Given
        PollStatsCollector._instances.clear()
        collector = PollStatsCollector.get_or_create("test_backoff")

        # When
        collector.record_backoff_trigger("mcp_xxx_query", level="param")
        collector.record_backoff_trigger("mcp_xxx_query", level="param")
        collector.record_backoff_trigger("mcp_xxx_query", level="tool")
        collector.record_backoff_trigger("mcp_yyy_status", level="param")

        # Then
        snapshot = collector.snapshot()
        assert snapshot["backoff_param_level"]["mcp_xxx_query"] == 2
        assert snapshot["backoff_tool_level"]["mcp_xxx_query"] == 1
        assert snapshot["backoff_param_level"]["mcp_yyy_status"] == 1
        assert "mcp_yyy_status" not in snapshot["backoff_tool_level"]

    def test_record_backoff_trigger_invalid_level_ignored(self):
        """非法 level 应被忽略,不影响计数"""
        # Given
        PollStatsCollector._instances.clear()
        collector = PollStatsCollector.get_or_create("test_invalid_level")

        # When
        collector.record_backoff_trigger("mcp_xxx", level="invalid_level")

        # Then
        snapshot = collector.snapshot()
        assert "mcp_xxx" not in snapshot["backoff_param_level"]
        assert "mcp_xxx" not in snapshot["backoff_tool_level"]


class TestPollStatsCollectorAsyncTask:
    """record_async_task 异步任务计数测试"""

    def test_record_async_task_increments_total(self):
        """后台异步任务总数应正确递增"""
        # Given
        PollStatsCollector._instances.clear()
        collector = PollStatsCollector.get_or_create("test_async")

        # When
        collector.record_async_task("mcp_xxx_image_create")
        collector.record_async_task("mcp_xxx_image_create")
        collector.record_async_task("mcp_yyy_video_analyse")

        # Then
        snapshot = collector.snapshot()
        assert snapshot["async_task_total"] == 3
        assert snapshot["async_task_by_tool"]["mcp_xxx_image_create"] == 2
        assert snapshot["async_task_by_tool"]["mcp_yyy_video_analyse"] == 1


class TestPollStatsCollectorSnapshot:
    """snapshot 结构测试"""

    def test_snapshot_returns_correct_structure(self):
        """snapshot 应返回包含所有字段的字典"""
        # Given
        PollStatsCollector._instances.clear()
        collector = PollStatsCollector.get_or_create("test_snapshot")
        collector.record_invoke("mcp_xxx_query", is_pending=True)
        collector.record_backoff_trigger("mcp_xxx_query", level="param")
        collector.record_async_task("mcp_xxx_image_create")

        # When
        snapshot = collector.snapshot()

        # Then
        expected_keys = {
            "session_id", "invoke_counts", "pending_counts", "completed_counts",
            "backoff_param_level", "backoff_tool_level",
            "async_task_total", "async_task_by_tool",
        }
        assert set(snapshot.keys()) == expected_keys
        assert snapshot["session_id"] == "test_snapshot"


class TestPollStatsCollectorReset:
    """reset 清空测试"""

    def test_reset_clears_all_counters(self):
        """reset 后所有计数器应归零"""
        # Given
        PollStatsCollector._instances.clear()
        collector = PollStatsCollector.get_or_create("test_reset")
        collector.record_invoke("mcp_xxx_query", is_pending=True)
        collector.record_backoff_trigger("mcp_xxx_query", level="param")
        collector.record_async_task("mcp_xxx_image_create")

        # When
        collector.reset()

        # Then
        snapshot = collector.snapshot()
        assert snapshot["invoke_counts"] == {}
        assert snapshot["pending_counts"] == {}
        assert snapshot["completed_counts"] == {}
        assert snapshot["backoff_param_level"] == {}
        assert snapshot["backoff_tool_level"] == {}
        assert snapshot["async_task_total"] == 0
        assert snapshot["async_task_by_tool"] == {}


class TestPollStatsCollectorCleanupSession:
    """cleanup_session 会话清理测试"""

    def test_cleanup_session_removes_instance(self):
        """cleanup_session 应从实例字典中移除会话"""
        # Given
        PollStatsCollector._instances.clear()
        collector = PollStatsCollector.get_or_create("session_to_cleanup")
        assert "session_to_cleanup" in PollStatsCollector._instances

        # When
        PollStatsCollector.cleanup_session("session_to_cleanup")

        # Then
        assert "session_to_cleanup" not in PollStatsCollector._instances

        # 验证重新获取会创建新实例
        new_collector = PollStatsCollector.get_or_create("session_to_cleanup")
        assert new_collector is not collector
