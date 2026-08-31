#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_concurrency.py
ToolConcurrencyClassifier单元测试 - P1工具并发分类器 + P5参数级隔离

测试覆盖:
- 可并行工具(无状态)判定
- 不可并行工具(共享状态)判定: 前缀匹配 + 全名匹配
- 参数级隔离工具判定: shell_execute 按 session_id 隔离
- partition 分组保持原始顺序
- 参数级隔离 partition: 同 session_id 串行,不同 session_id 可并行
- 边界场景: 空配置、大小写敏感
"""
import pytest

from app.domain.services.tools.concurrency import ToolConcurrencyClassifier


def _build_default_classifier():
    """构建默认分类器(与 config.yaml 默认值一致,含P5参数级隔离)"""
    return ToolConcurrencyClassifier(
        stateful_prefixes=["shell_", "browser_"],
        stateful_names=["file_write", "file_delete", "file_move", "file_upload"],
        stateful_arg_keys={"shell_execute": ["session_id"]},
    )


def _build_legacy_classifier():
    """构建无参数级隔离的分类器(向后兼容场景)"""
    return ToolConcurrencyClassifier(
        stateful_prefixes=["shell_", "browser_"],
        stateful_names=["file_write", "file_delete", "file_move", "file_upload"],
    )


class TestIsParallelizable:
    """is_parallelizable 判定测试"""

    def test_parallelizable_tool_returns_true(self):
        """无状态工具应判定为可并行"""
        classifier = _build_default_classifier()
        assert classifier.is_parallelizable("web_search") is True
        assert classifier.is_parallelizable("deep_research") is True
        assert classifier.is_parallelizable("mcp_amap_maps_weather") is True
        assert classifier.is_parallelizable("file_read") is True
        assert classifier.is_parallelizable("skill_list") is True
        assert classifier.is_parallelizable("a2a_search") is True

    def test_stateful_prefix_returns_false(self):
        """共享状态前缀工具应判定为不可并行(shell_read_output/shell_wait_process等子工具)"""
        classifier = _build_default_classifier()
        # shell_execute 走参数级隔离,is_parallelizable 返回 True(由 partition 分组控制)
        assert classifier.is_parallelizable("shell_execute") is True
        # 其他 shell_ 子工具仍按前缀串行
        assert classifier.is_parallelizable("shell_read_output") is False
        assert classifier.is_parallelizable("shell_wait_process") is False
        assert classifier.is_parallelizable("shell_write_input") is False
        assert classifier.is_parallelizable("shell_kill_process") is False
        # browser_ 全部串行
        assert classifier.is_parallelizable("browser_navigate") is False
        assert classifier.is_parallelizable("browser_click") is False
        assert classifier.is_parallelizable("browser_view") is False

    def test_stateful_name_returns_false(self):
        """共享状态全名工具应判定为不可并行(文件写操作)"""
        classifier = _build_default_classifier()
        assert classifier.is_parallelizable("file_write") is False
        assert classifier.is_parallelizable("file_delete") is False
        assert classifier.is_parallelizable("file_move") is False
        assert classifier.is_parallelizable("file_upload") is False

    def test_empty_stateful_lists_all_parallelizable(self):
        """空前缀+空全名时所有工具均可并行(最大化并行)"""
        classifier = ToolConcurrencyClassifier(
            stateful_prefixes=[],
            stateful_names=[],
        )
        assert classifier.is_parallelizable("shell_execute") is True
        assert classifier.is_parallelizable("browser_navigate") is True
        assert classifier.is_parallelizable("file_write") is True
        assert classifier.is_parallelizable("web_search") is True

    def test_case_sensitive_match(self):
        """前缀匹配大小写敏感: Shell_execute(大写S)不被shell_匹配,返回True"""
        classifier = _build_default_classifier()
        assert classifier.is_parallelizable("Shell_execute") is True
        assert classifier.is_parallelizable("BROWSER_navigate") is True
        assert classifier.is_parallelizable("FILE_WRITE") is True

    def test_legacy_classifier_shell_execute_serial(self):
        """无参数级隔离配置时,shell_execute 按前缀串行(向后兼容)"""
        classifier = _build_legacy_classifier()
        assert classifier.is_parallelizable("shell_execute") is False
        assert classifier.is_parallelizable("shell_read_output") is False


class TestPartition:
    """partition 分组测试"""

    def test_partition_preserves_order(self):
        """partition 后 parallel/serial 各自保持原始相对顺序"""
        classifier = _build_default_classifier()
        tool_calls = [
            {"function": {"name": "web_search", "arguments": "{}"}},
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s1"}'}},
            {"function": {"name": "deep_research", "arguments": "{}"}},
            {"function": {"name": "browser_navigate", "arguments": "{}"}},
            {"function": {"name": "file_read", "arguments": "{}"}},
            {"function": {"name": "file_write", "arguments": "{}"}},
        ]
        parallel, serial = classifier.partition(tool_calls)
        # 可并行组: web_search + shell_execute(首次出现) + deep_research + file_read
        assert [tc["function"]["name"] for tc in parallel] == [
            "web_search", "shell_execute", "deep_research", "file_read",
        ]
        # 串行组: browser_navigate + file_write
        assert [tc["function"]["name"] for tc in serial] == [
            "browser_navigate", "file_write",
        ]

    def test_partition_all_parallel(self):
        """全部可并行工具时,serial 为空"""
        classifier = _build_default_classifier()
        tool_calls = [
            {"function": {"name": "web_search", "arguments": "{}"}},
            {"function": {"name": "deep_research", "arguments": "{}"}},
        ]
        parallel, serial = classifier.partition(tool_calls)
        assert len(parallel) == 2
        assert len(serial) == 0

    def test_partition_all_serial(self):
        """全部串行工具时(broswer_+file_write),parallel 为空"""
        classifier = _build_default_classifier()
        tool_calls = [
            {"function": {"name": "browser_navigate", "arguments": "{}"}},
            {"function": {"name": "file_write", "arguments": "{}"}},
        ]
        parallel, serial = classifier.partition(tool_calls)
        assert len(parallel) == 0
        assert len(serial) == 2

    def test_partition_empty_list(self):
        """空列表 partition 返回两个空列表"""
        classifier = _build_default_classifier()
        parallel, serial = classifier.partition([])
        assert parallel == []
        assert serial == []

    def test_partition_handles_missing_function_key(self):
        """缺少 function 键的项被视为可并行(默认安全)"""
        classifier = _build_default_classifier()
        tool_calls = [
            {"function": {"name": "web_search", "arguments": "{}"}},
            {"other_key": "value"},  # 无 function 键
        ]
        parallel, serial = classifier.partition(tool_calls)
        assert len(parallel) == 2
        assert len(serial) == 0


class TestPartitionWithArgIsolation:
    """P5 参数级隔离 partition 测试: shell_execute 按 session_id 隔离"""

    def test_same_session_id_shell_execute_serialized(self):
        """同一 session_id 的多个 shell_execute: 首个并行,后续串行"""
        classifier = _build_default_classifier()
        tool_calls = [
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s1", "command": "ls"}'}},
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s1", "command": "pwd"}'}},
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s1", "command": "whoami"}'}},
        ]
        parallel, serial = classifier.partition(tool_calls)
        # 首个 s1 进入并行组,后续2个 s1 进入串行组
        assert len(parallel) == 1
        assert len(serial) == 2
        assert parallel[0]["function"]["arguments"].__contains__("s1")
        assert all("s1" in tc["function"]["arguments"] for tc in serial)

    def test_different_session_id_shell_execute_parallel(self):
        """不同 session_id 的 shell_execute 全部进入并行组"""
        classifier = _build_default_classifier()
        tool_calls = [
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s1", "command": "ls"}'}},
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s2", "command": "pwd"}'}},
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s3", "command": "whoami"}'}},
        ]
        parallel, serial = classifier.partition(tool_calls)
        # 3个不同 session_id 全部并行
        assert len(parallel) == 3
        assert len(serial) == 0

    def test_mixed_session_id_shell_execute(self):
        """混合 session_id: s1(2个) + s2(1个) + s3(1个),并行3个串行1个"""
        classifier = _build_default_classifier()
        tool_calls = [
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s1", "command": "ls"}'}},
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s2", "command": "pwd"}'}},
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s1", "command": "whoami"}'}},
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s3", "command": "date"}'}},
        ]
        parallel, serial = classifier.partition(tool_calls)
        # s1首次 + s2 + s3 进入并行组(3个),s1第二次进入串行组(1个)
        assert len(parallel) == 3
        assert len(serial) == 1
        # 串行组的应是 s1 的第二次调用
        assert "s1" in serial[0]["function"]["arguments"]

    def test_shell_execute_with_dict_arguments(self):
        """arguments 为字典格式时也应正确解析 session_id"""
        classifier = _build_default_classifier()
        tool_calls = [
            {"function": {"name": "shell_execute", "arguments": {"session_id": "s1", "command": "ls"}}},
            {"function": {"name": "shell_execute", "arguments": {"session_id": "s1", "command": "pwd"}}},
        ]
        parallel, serial = classifier.partition(tool_calls)
        assert len(parallel) == 1
        assert len(serial) == 1

    def test_shell_execute_missing_session_id(self):
        """shell_execute 缺少 session_id 参数时:视为同一隔离组(空key)"""
        classifier = _build_default_classifier()
        tool_calls = [
            {"function": {"name": "shell_execute", "arguments": '{"command": "ls"}'}},
            {"function": {"name": "shell_execute", "arguments": '{"command": "pwd"}'}},
        ]
        parallel, serial = classifier.partition(tool_calls)
        # 都缺 session_id,隔离key相同("shell_execute|"),首个并行,第二个串行
        assert len(parallel) == 1
        assert len(serial) == 1

    def test_shell_execute_mixed_with_other_tools(self):
        """shell_execute 与其他工具混合: web_search + shell_execute(s1) + shell_execute(s1) + file_read"""
        classifier = _build_default_classifier()
        tool_calls = [
            {"function": {"name": "web_search", "arguments": '{"query": "test"}'}},
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s1", "command": "ls"}'}},
            {"function": {"name": "shell_execute", "arguments": '{"session_id": "s1", "command": "pwd"}'}},
            {"function": {"name": "file_read", "arguments": '{"path": "/tmp/test"}'}},
        ]
        parallel, serial = classifier.partition(tool_calls)
        # 并行组: web_search + shell_execute(s1首次) + file_read = 3个
        assert len(parallel) == 3
        # 串行组: shell_execute(s1第二次) = 1个
        assert len(serial) == 1
