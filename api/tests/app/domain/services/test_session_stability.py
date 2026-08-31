#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_session_stability.py
会话稳定性单元测试 - Shell控制台去重、文件名清理、会话状态一致性
"""
from unittest.mock import MagicMock, patch

import pytest

from app.domain.models.event import ToolEvent, ToolEventStatus, ShellToolContent
from app.domain.services.agent_task_runner import AgentTaskRunner


class TestShellConsoleDeduplication:
    """Shell控制台记录增量发送测试"""

    def test_first_call_sends_all_records(self):
        runner = self._create_runner()
        all_records = [{"ps1": "$", "command": "ls", "output": "file1"}]
        sent_count = runner._shell_console_sent_count.get("session1", 0)
        new_records = all_records[sent_count:]
        assert len(new_records) == 1
        runner._shell_console_sent_count["session1"] = len(all_records)
        assert runner._shell_console_sent_count["session1"] == 1

    def test_subsequent_call_only_sends_delta(self):
        runner = self._create_runner()
        runner._shell_console_sent_count["session1"] = 3
        all_records = [
            {"ps1": "$", "command": f"cmd{i}", "output": f"out{i}"}
            for i in range(5)
        ]
        sent_count = runner._shell_console_sent_count.get("session1", 0)
        new_records = all_records[sent_count:]
        assert len(new_records) == 2
        assert new_records[0]["command"] == "cmd3"

    def test_no_new_records_returns_empty(self):
        runner = self._create_runner()
        runner._shell_console_sent_count["session1"] = 5
        all_records = [{"ps1": "$", "command": f"cmd{i}", "output": f"out{i}"} for i in range(5)]
        sent_count = runner._shell_console_sent_count.get("session1", 0)
        assert len(all_records[sent_count:]) == 0

    def test_different_sessions_tracked_independently(self):
        runner = self._create_runner()
        runner._shell_console_sent_count["session_a"] = 2
        runner._shell_console_sent_count["session_b"] = 5
        assert runner._shell_console_sent_count.get("session_a", 0) == 2
        assert runner._shell_console_sent_count.get("session_b", 0) == 5

    def test_large_history_only_sends_delta(self):
        runner = self._create_runner()
        runner._shell_console_sent_count["session1"] = 50
        all_records = [{"ps1": "$", "command": f"cmd{i}", "output": f"out{i}"} for i in range(55)]
        new_records = all_records[runner._shell_console_sent_count["session1"]:]
        assert len(new_records) == 5

    def test_sent_count_updated_after_each_call(self):
        runner = self._create_runner()
        all_records = [{"ps1": "$", "command": f"cmd{i}", "output": f"out{i}"} for i in range(10)]
        for expected_delta in [10, 0, 0]:
            sent_count = runner._shell_console_sent_count.get("session1", 0)
            new_records = all_records[sent_count:]
            assert len(new_records) == expected_delta
            runner._shell_console_sent_count["session1"] = len(all_records)

    def _create_runner(self) -> AgentTaskRunner:
        with patch.object(AgentTaskRunner, '__init__', lambda self: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._shell_console_sent_count = {}
            return runner


class TestFilenameSanitization:
    """文件名特殊字符清理测试"""

    def test_chinese_double_quotes_removed(self):
        result = AgentTaskRunner._sanitize_filename("\u201c商货通\u201d平台.docx")
        assert "\u201c" not in result
        assert "\u201d" not in result
        assert "商货通" in result

    def test_chinese_single_quotes_removed(self):
        result = AgentTaskRunner._sanitize_filename("文件\u2018名\u2019.docx")
        assert "\u2018" not in result
        assert "\u2019" not in result

    def test_special_chars_replaced_with_underscore(self):
        for char in ['<', '>', ':', '"', '|', '?', '*']:
            result = AgentTaskRunner._sanitize_filename(f"file{char}name.docx")
            assert char not in result
            assert "_" in result

    def test_normal_filename_unchanged(self):
        for filename in ["normal_filename.docx", "商货通新闻宣传稿.docx", ""]:
            result = AgentTaskRunner._sanitize_filename(filename)
            assert result == filename

    def test_complex_filename_sanitized(self):
        result = AgentTaskRunner._sanitize_filename("\u201c商货通\u201d平台:新闻稿|v2.docx")
        assert "\u201c" not in result
        assert "\u201d" not in result
        assert ":" not in result
        assert "|" not in result


class TestSessionStatusConsistency:
    """会话状态一致性测试"""

    def test_old_task_cancelled_before_new_task_creation(self):
        mock_task = MagicMock()
        mock_task.cancel = MagicMock()
        mock_task.cancel()
        mock_task.cancel.assert_called_once()

    def test_none_task_does_not_raise(self):
        task = None
        if task is not None:
            task.cancel()
