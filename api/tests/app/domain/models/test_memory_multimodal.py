#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_memory_multimodal.py
Memory 多模态 list 内容提取单元测试 — 验证 P1-1 防御修复

测试覆盖：
- _extract_text_from_content: 统一文本提取(str/list/None/其他类型)
- truncate_tool_result: list 格式 content 截断
- _summarize_tool_operation: list 格式 content 摘要生成
- extract_key_facts: list 格式 content 事实提取
- _compress_tool_content: list 格式 content 压缩
- 集成测试: 长会话(含浏览器截图) session_summary 非空
"""
import json

import pytest

from app.domain.models.memory import Memory, KeyFact


def _make_browser_view_content(url: str = "https://example.com", title: str = "Example Page") -> list:
    """构造 browser_view 工具结果的 list 格式 content（含 text + image_url 块）

    模拟实际场景：browser_view 返回 ToolResult，序列化为 list 格式存入 Memory：
    [{"type":"text","text":"{json}"},{"type":"image_url","image_url":{"url":"data:..."}}]
    """
    tool_result_json = json.dumps({
        "success": True,
        "message": "",
        "data": {
            "page_state": {"url": url, "title": title},
            "interactive_elements": [{"id": "btn1", "text": "点击"}],
            "screenshot": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
        }
    }, ensure_ascii=False)
    return [
        {"type": "text", "text": tool_result_json},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="}},
    ]


def _make_file_content(filepath: str = "/home/ubuntu/test.txt", content: str = "file content") -> list:
    """构造 file 工具结果的 list 格式 content"""
    tool_result_json = json.dumps({
        "success": True,
        "message": "",
        "data": {"filepath": filepath, "content": content}
    }, ensure_ascii=False)
    return [{"type": "text", "text": tool_result_json}]


def _make_shell_content(command: str = "ls -la", output: str = "total 0") -> list:
    """构造 shell 工具结果的 list 格式 content"""
    tool_result_json = json.dumps({
        "success": True,
        "message": "",
        "data": {"console": [{"command": command, "output": output}]}
    }, ensure_ascii=False)
    return [{"type": "text", "text": tool_result_json}]


def _make_failed_tool_content(error_msg: str = "执行失败") -> list:
    """构造失败工具结果的 list 格式 content"""
    tool_result_json = json.dumps({
        "success": False,
        "message": error_msg,
        "data": None,
    }, ensure_ascii=False)
    return [{"type": "text", "text": tool_result_json}]


class TestExtractTextFromContent:
    """_extract_text_from_content 统一文本提取测试"""

    def test_str_content_returns_as_is(self):
        """str 格式 content: 原样返回"""
        assert Memory._extract_text_from_content("hello world") == "hello world"

    def test_empty_str_returns_empty(self):
        """空字符串: 返回空字符串"""
        assert Memory._extract_text_from_content("") == ""

    def test_list_with_text_and_image(self):
        """list 格式(含 text + image_url): 仅提取 text 块"""
        content = [
            {"type": "text", "text": "page info"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
        ]
        assert Memory._extract_text_from_content(content) == "page info"

    def test_list_with_multiple_text_blocks(self):
        """list 格式(多个 text 块): 用换行拼接"""
        content = [
            {"type": "text", "text": "first"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
            {"type": "text", "text": "second"},
        ]
        result = Memory._extract_text_from_content(content)
        assert "first" in result
        assert "second" in result
        assert "\n" in result

    def test_list_with_only_image_url(self):
        """list 格式(仅 image_url): 返回空字符串"""
        content = [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
        ]
        assert Memory._extract_text_from_content(content) == ""

    def test_list_with_empty_text(self):
        """list 格式(text 为空字符串): 跳过空 text 块"""
        content = [
            {"type": "text", "text": ""},
            {"type": "text", "text": "valid"},
        ]
        assert Memory._extract_text_from_content(content) == "valid"

    def test_none_returns_empty(self):
        """None: 返回空字符串"""
        assert Memory._extract_text_from_content(None) == ""

    def test_integer_returns_empty(self):
        """int 类型: 返回空字符串"""
        assert Memory._extract_text_from_content(123) == ""

    def test_empty_list_returns_empty(self):
        """空 list: 返回空字符串"""
        assert Memory._extract_text_from_content([]) == ""

    def test_list_with_non_dict_items(self):
        """list 含非 dict 项: 跳过非 dict 项"""
        content = ["raw string", {"type": "text", "text": "valid"}, 42]
        assert Memory._extract_text_from_content(content) == "valid"


class TestTruncateToolResultListFormat:
    """truncate_tool_result 对 list 格式 content 的截断测试"""

    def test_list_browser_view_content_truncated(self):
        """list 格式 browser_view content(超长): 截断interactive_elements并移除base64截图"""
        # 构造超长 content: 大量interactive_elements触发预算截断(超过12000字符阈值)
        long_elements = [{"id": f"el{i}", "text": f"元素{i}", "tag": "div"} for i in range(500)]
        tool_result_json = json.dumps({
            "success": True,
            "message": "",
            "data": {
                "page_state": {"url": "https://example.com", "title": "Long Page"},
                "interactive_elements": long_elements,
                "screenshot": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
            }
        }, ensure_ascii=False)
        content = [{"type": "text", "text": tool_result_json}]
        result = Memory.truncate_tool_result(content, "browser_view")
        # 应返回截断后的字符串（非 list）
        assert isinstance(result, str)
        # base64截图必须被移除(替换为[attached]标记)
        assert "iVBORw0KGgo" not in result
        assert "[attached]" in result
        # page_state轻量字段应保留
        assert "https://example.com" in result
        # 截断后结果应远小于原始内容
        assert len(result) < len(tool_result_json)

    def test_list_file_content_truncated(self):
        """list 格式 file content(超长): 提取文本后截断"""
        long_content = "x" * 5000
        content = _make_file_content(content=long_content)
        result = Memory.truncate_tool_result(content, "write_file")
        assert isinstance(result, str)
        # 截断后应包含 filepath
        assert "filepath" in result or "(truncated)" in result

    def test_short_list_content_not_truncated(self):
        """list 格式 content(短): 原样返回提取的文本"""
        content = _make_browser_view_content(url="https://a.com", title="short")
        result = Memory.truncate_tool_result(content, "browser_view")
        # 短内容应返回提取的文本（JSON字符串）
        assert isinstance(result, str)
        assert "https://a.com" in result

    def test_none_content_returns_none(self):
        """None content: 原样返回"""
        assert Memory.truncate_tool_result(None, "browser_view") is None

    def test_empty_list_returns_empty_list(self):
        """空 list content: 原样返回（提取文本为空）"""
        result = Memory.truncate_tool_result([], "browser_view")
        assert result == []

    def test_toolresult_wrapper_unwrapped(self):
        """ToolResult包装格式解包: {"success":..,"data":{...}} → 内层data字段可见

        会话392252b6根因: _truncate_browser_view_result未解包ToolResult外层,
        data.get("page_state")取顶层返回{},LLM看到空browser_view结果陷入循环。
        """
        # 模拟 _build_tool_message_content 传入的 model_dump_json() 格式
        wrapped_json = json.dumps({
            "success": True,
            "message": "",
            "data": {
                "page_state": {"url": "https://wrapped.com", "title": "Wrapped"},
                "interactive_elements": [{"id": "el1", "text": "按钮"}],
                "screenshot": "data:image/jpeg;base64,abc==",
            }
        }, ensure_ascii=False)
        result = Memory.truncate_tool_result(wrapped_json, "browser_view")
        assert isinstance(result, str)
        # 解包后page_state字段可见(非空)
        assert "https://wrapped.com" in result
        assert "Wrapped" in result
        # 解包后interactive_elements可见
        assert "el1" in result
        # base64截图被移除
        assert "abc==" not in result


class TestSummarizeToolOperationListFormat:
    """_summarize_tool_operation 对 list 格式 content 的摘要生成测试"""

    def _make_memory(self) -> Memory:
        return Memory()

    def test_list_browser_view_generates_summary(self):
        """list 格式 browser_view content: 生成 "访问页面: url, title" 摘要"""
        mem = self._make_memory()
        content = _make_browser_view_content(url="https://example.com", title="Example")
        result = mem._summarize_tool_operation("browser_view", content)
        assert "访问页面" in result
        assert "https://example.com" in result
        assert "Example" in result

    def test_list_file_generates_summary(self):
        """list 格式 file content: 生成 "文件操作: filepath" 摘要"""
        mem = self._make_memory()
        content = _make_file_content(filepath="/home/ubuntu/test.txt")
        result = mem._summarize_tool_operation("write_file", content)
        assert "文件操作" in result
        assert "/home/ubuntu/test.txt" in result

    def test_list_shell_generates_summary(self):
        """list 格式 shell content: 生成 "执行命令: cmd" 摘要"""
        mem = self._make_memory()
        content = _make_shell_content(command="ls -la")
        result = mem._summarize_tool_operation("shell_execute", content)
        assert "执行命令" in result
        assert "ls -la" in result

    def test_str_browser_view_still_works(self):
        """str 格式 browser_view content: 向后兼容，仍正常生成摘要"""
        mem = self._make_memory()
        tool_result_json = json.dumps({
            "success": True,
            "data": {"page_state": {"url": "https://str.com", "title": "Str Page"}}
        }, ensure_ascii=False)
        result = mem._summarize_tool_operation("browser_view", tool_result_json)
        assert "访问页面" in result
        assert "https://str.com" in result

    def test_empty_list_browser_view_returns_default(self):
        """空 list browser_view content: 返回默认摘要"""
        mem = self._make_memory()
        result = mem._summarize_tool_operation("browser_view", [])
        assert "浏览器查看" in result


class TestExtractKeyFactsListFormat:
    """extract_key_facts 对 list 格式 content 的事实提取测试"""

    def test_list_browser_view_extracts_url_and_title(self):
        """list 格式 browser_view content: 提取 url 和 page_title 事实"""
        mem = Memory()
        mem.messages = [
            {"role": "user", "content": "请访问 https://example.com 查看页面"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "browser_view"}}]},
            {
                "role": "tool",
                "function_name": "browser_view",
                "tool_call_id": "1",
                "content": _make_browser_view_content(url="https://example.com", title="Example"),
            },
        ]
        facts = mem.extract_key_facts()
        categories = [f.category for f in facts]
        assert "requirement" in categories
        assert "url" in categories
        assert "page_title" in categories

    def test_list_file_extracts_filepath(self):
        """list 格式 file content: 提取 file 事实"""
        mem = Memory()
        mem.messages = [
            {"role": "user", "content": "请读取文件 /home/ubuntu/test.txt"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "read_file"}}]},
            {
                "role": "tool",
                "function_name": "read_file",
                "tool_call_id": "1",
                "content": _make_file_content(filepath="/home/ubuntu/test.txt"),
            },
        ]
        facts = mem.extract_key_facts()
        file_facts = [f for f in facts if f.category == "file"]
        assert len(file_facts) == 1
        assert "/home/ubuntu/test.txt" in file_facts[0].content

    def test_list_shell_extracts_cmd(self):
        """list 格式 shell content: 提取 cmd 事实"""
        mem = Memory()
        mem.messages = [
            {"role": "user", "content": "请执行 ls -la 命令"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "shell_execute"}}]},
            {
                "role": "tool",
                "function_name": "shell_execute",
                "tool_call_id": "1",
                "content": _make_shell_content(command="ls -la"),
            },
        ]
        facts = mem.extract_key_facts()
        cmd_facts = [f for f in facts if f.category == "cmd"]
        assert len(cmd_facts) == 1
        assert "ls -la" in cmd_facts[0].content

    def test_list_failed_tool_extracts_error(self):
        """list 格式失败工具 content: 提取 error 事实"""
        mem = Memory()
        mem.messages = [
            {"role": "user", "content": "请执行操作"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "browser_click"}}]},
            {
                "role": "tool",
                "function_name": "browser_click",
                "tool_call_id": "1",
                "content": _make_failed_tool_content("元素未找到"),
            },
        ]
        facts = mem.extract_key_facts()
        error_facts = [f for f in facts if f.category == "error"]
        assert len(error_facts) == 1
        assert "元素未找到" in error_facts[0].content

    def test_list_user_content_extracts_requirement(self):
        """list 格式 user content: 提取 requirement 事实"""
        mem = Memory()
        mem.messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "请帮我分析这个网站的数据"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
            ]},
        ]
        facts = mem.extract_key_facts()
        req_facts = [f for f in facts if f.category == "requirement"]
        assert len(req_facts) == 1
        assert "分析这个网站" in req_facts[0].content


class TestCompressToolContentListFormat:
    """_compress_tool_content 对 list 格式 content 的压缩测试"""

    def test_list_browser_view_compressed(self):
        """list 格式 browser_view content(超长): 压缩为摘要"""
        # 构造超长 content（interactive_elements 超长触发压缩）
        long_elements = [{"id": f"el{i}", "text": f"元素{i}"} for i in range(200)]
        tool_result_json = json.dumps({
            "success": True,
            "data": {
                "page_state": {"url": "https://example.com", "title": "Long Page"},
                "interactive_elements": long_elements,
                "screenshot": "data:image/png;base64,xxx",
            }
        }, ensure_ascii=False)
        content = [
            {"type": "text", "text": tool_result_json},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
        ]
        result = Memory._compress_tool_content(content, "browser_view")
        assert isinstance(result, str)
        assert "compressed" in result or "page" in result
        # 不应包含原始 base64 数据
        assert "iVBORw0KGgo" not in result

    def test_list_shell_compressed(self):
        """list 格式 shell content: 压缩为摘要"""
        content = _make_shell_content(command="ls -la", output="total 0\ndrwxr-xr-x 2 root root 4096")
        result = Memory._compress_tool_content(content, "shell_execute")
        assert isinstance(result, str)
        # 应包含命令信息
        assert "ls -la" in result or "console" in result

    def test_empty_list_returns_removed_marker(self):
        """空 list content: 返回 "(fn output removed)" 标记"""
        result = Memory._compress_tool_content([], "browser_view")
        assert "output removed" in result

    def test_none_returns_removed_marker(self):
        """None content: 返回 "(fn output removed)" 标记"""
        result = Memory._compress_tool_content(None, "browser_view")
        assert "output removed" in result


class TestSessionSummaryIntegration:
    """集成测试: 长会话(含浏览器截图) session_summary 非空"""

    def test_session_summary_non_empty_with_list_content(self):
        """模拟长会话(含 list 格式 browser_view content): compact 后 session_summary 非空

        这是 P1-1 修复的核心验证点：修复前 session_summary 始终为空(即使 compact_count=56)，
        修复后应能正确从 list 格式 content 提取操作摘要。
        """
        mem = Memory()
        # 构造含 list 格式 content 的会话消息
        mem.messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "请访问 https://example.com 并分析页面"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "browser_view"}}]},
            {
                "role": "tool",
                "function_name": "browser_view",
                "tool_call_id": "1",
                "content": _make_browser_view_content(url="https://example.com", title="Example"),
            },
            {"role": "assistant", "content": "我已访问页面，现在执行命令分析"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "2", "function": {"name": "shell_execute"}}]},
            {
                "role": "tool",
                "function_name": "shell_execute",
                "tool_call_id": "2",
                "content": _make_shell_content(command="cat /home/ubuntu/data.txt"),
            },
        ]
        mem.metrics.message_count = len(mem.messages)

        # 执行压缩
        mem.compact()

        # 验证 session_summary 非空（核心断言）
        assert mem.session_summary != "", "session_summary 不应为空（P1-1修复核心验证点）"
        assert "访问页面" in mem.session_summary or "执行命令" in mem.session_summary
        assert mem.metrics.compact_count >= 1

    def test_session_summary_accumulates_across_compacts(self):
        """多次 compact: session_summary 累积式增长"""
        mem = Memory()
        mem.messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "访问页面1"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "browser_view"}}]},
            {
                "role": "tool",
                "function_name": "browser_view",
                "tool_call_id": "1",
                "content": _make_browser_view_content(url="https://page1.com", title="Page1"),
            },
        ]
        mem.metrics.message_count = len(mem.messages)

        # 第一次 compact
        mem.compact()
        first_summary = mem.session_summary
        assert first_summary != ""

        # 添加新消息后再次 compact
        mem.messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [{"id": "2", "function": {"name": "browser_view"}}]},
            {
                "role": "tool",
                "function_name": "browser_view",
                "tool_call_id": "2",
                "content": _make_browser_view_content(url="https://page2.com", title="Page2"),
            },
        ])
        mem.metrics.message_count = len(mem.messages)
        mem.compact()

        # session_summary 应包含两次访问记录
        assert "page1.com" in mem.session_summary or "Page1" in mem.session_summary
        assert "page2.com" in mem.session_summary or "Page2" in mem.session_summary

    def test_str_content_still_works_after_fix(self):
        """str 格式 content: 修复后向后兼容，session_summary 仍正常生成"""
        mem = Memory()
        tool_result_json = json.dumps({
            "success": True,
            "data": {"page_state": {"url": "https://str.com", "title": "Str Page"}}
        }, ensure_ascii=False)
        mem.messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "访问页面"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "browser_view"}}]},
            {
                "role": "tool",
                "function_name": "browser_view",
                "tool_call_id": "1",
                "content": tool_result_json,  # str 格式
            },
        ]
        mem.metrics.message_count = len(mem.messages)
        mem.compact()
        assert mem.session_summary != ""
        assert "str.com" in mem.session_summary or "Str Page" in mem.session_summary


class TestEvictImageData:
    """evict_image_data: LLM调用后驱逐截图,防止在记忆中累积

    根因(会话392252b6 vs 6794ac3c): 浏览器截图作为image_url块持久化到记忆
    且永不清理,10次browser_view累积~15K tokens,挤占有效上下文,LLM决策退化。
    evict_image_data在每次LLM调用后原地清理image_url块,替换为轻量文本标记。
    """

    def test_evicts_image_url_from_tool_message(self):
        """工具消息含image_url块: 驱逐后替换为文本标记,保留text块"""
        mem = Memory()
        mem.messages = [
            {"role": "tool", "content": _make_browser_view_content()},
        ]
        evicted = mem.evict_image_data()

        assert evicted == 1
        content = mem.messages[0]["content"]
        # text块保留
        assert any(item.get("type") == "text" and "page_state" in item.get("text", "") for item in content)
        # image_url块被替换为text标记
        assert not any(item.get("type") == "image_url" for item in content)
        assert any("screenshot已驱逐" in item.get("text", "") for item in content)

    def test_evicts_multiple_images_across_messages(self):
        """多条消息各含截图: 一次性驱逐所有image_url块"""
        mem = Memory()
        mem.messages = [
            {"role": "tool", "content": _make_browser_view_content(url="https://a.com")},
            {"role": "assistant", "content": "分析中"},
            {"role": "tool", "content": _make_browser_view_content(url="https://b.com")},
        ]
        evicted = mem.evict_image_data()

        assert evicted == 2
        # 所有消息的image_url块都已清理
        for msg in mem.messages:
            content = msg.get("content")
            if isinstance(content, list):
                assert not any(item.get("type") == "image_url" for item in content)

    def test_str_content_no_op(self):
        """str格式content(无图片): 驱逐操作无影响,返回0"""
        mem = Memory()
        mem.messages = [
            {"role": "user", "content": "请访问页面"},
            {"role": "assistant", "content": "好的"},
            {"role": "tool", "content": '{"success": true}'},
        ]
        evicted = mem.evict_image_data()

        assert evicted == 0
        # 消息内容不变
        assert mem.messages[0]["content"] == "请访问页面"
        assert mem.messages[2]["content"] == '{"success": true}'

    def test_marks_dirty_when_evicted(self):
        """驱逐成功时标记记忆为dirty(触发持久化)"""
        mem = Memory()
        mem.messages = [
            {"role": "tool", "content": _make_browser_view_content()},
        ]
        mem.mark_clean()
        assert not mem.dirty

        mem.evict_image_data()
        assert mem.dirty, "驱逐截图后应标记dirty以触发持久化"

    def test_no_dirty_when_nothing_evicted(self):
        """无图片可驱逐时: 不标记dirty"""
        mem = Memory()
        mem.messages = [
            {"role": "user", "content": "普通消息"},
        ]
        mem.mark_clean()

        mem.evict_image_data()
        assert not mem.dirty, "无截图驱逐时不应标记dirty"

    def test_preserves_text_block_with_screenshot_marker(self):
        """驱逐后text块中screenshot="[attached]"标记保留,LLM知道曾展示过截图"""
        mem = Memory()
        mem.messages = [
            {"role": "tool", "content": _make_browser_view_content()},
        ]
        mem.evict_image_data()

        text_block = mem.messages[0]["content"][0]
        text_json = json.loads(text_block["text"])
        # text部分中screenshot字段仍为"[attached]"标记(由_build_tool_message_content设置)
        # 实际_make_browser_view_content中screenshot是base64,但_build_tool_message_content
        # 会将其替换为"[attached]"。这里直接验证text块保留即可。
        assert "page_state" in text_json["data"]

    def test_empty_messages_returns_zero(self):
        """空记忆: 返回0,不报错"""
        mem = Memory()
        assert mem.evict_image_data() == 0

    def test_non_data_image_url_preserved(self):
        """非data:image前缀的image_url(如http URL): 保留不驱逐"""
        mem = Memory()
        mem.messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
            ]},
        ]
        evicted = mem.evict_image_data()

        assert evicted == 0
        # http URL的image_url保留
        content = mem.messages[0]["content"]
        assert any(item.get("type") == "image_url" for item in content)

    def test_idempotent_multiple_calls(self):
        """多次调用幂等: 第二次无图可驱逐,返回0"""
        mem = Memory()
        mem.messages = [
            {"role": "tool", "content": _make_browser_view_content()},
        ]
        first = mem.evict_image_data()
        second = mem.evict_image_data()

        assert first == 1
        assert second == 0
