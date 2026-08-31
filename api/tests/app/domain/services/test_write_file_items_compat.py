#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_write_file_items_compat.py
FileTool.write_file 的 items 数组格式兼容性单元测试

背景:LLM 偶尔会误传 {"items":[{"filepath":...,"content":...}]} 数组格式,
而非标准的扁平参数结构。本测试覆盖 _normalize_write_file_items 规范化逻辑
以及 write_file 在缺失参数时返回 ToolResult(success=False) 而非抛异常的兜底行为。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.file import FileTool


def _build_file_tool_with_mock_sandbox() -> tuple[FileTool, MagicMock]:
    """构造带 mock sandbox 的 FileTool,返回(tool, sandbox_mock)"""
    sandbox = MagicMock()
    sandbox.write_file = AsyncMock(
        return_value=ToolResult(success=True, message="文件内容写入成功", data={"filepath": "/tmp/x"})
    )
    tool_obj = FileTool(sandbox=sandbox)
    return tool_obj, sandbox


class TestNormalizeWriteFileItems:
    """_normalize_write_file_items 静态方法单元测试"""

    def test_flat_params_priority_over_items(self):
        """扁平参数优先于 items 数组(扁平参数已完整时无需规范化)"""
        items = [{"filepath": "/items/path", "content": "items_content"}]
        fp, ct = FileTool._normalize_write_file_items(
            items=items, filepath="/flat/path", content="flat_content"
        )
        assert fp == "/flat/path"
        assert ct == "flat_content"

    def test_items_array_extracts_first_filepath(self):
        """items 数组中提取首个 filepath"""
        items = [
            {"filepath": "/path/from/items", "content": "first"},
            {"filepath": "/other/path", "content": "second"},
        ]
        fp, ct = FileTool._normalize_write_file_items(items=items, filepath=None, content=None)
        assert fp == "/path/from/items"

    def test_items_array_merges_all_content(self):
        """items 数组中合并所有 content(以 \\n 分隔)"""
        items = [
            {"filepath": "/path", "content": "part1"},
            {"filepath": "/other", "content": "part2"},
            {"filepath": "/third", "content": "part3"},
        ]
        fp, ct = FileTool._normalize_write_file_items(items=items, filepath=None, content=None)
        assert fp == "/path"
        assert ct == "part1\npart2\npart3"

    def test_items_array_handles_missing_content_field(self):
        """items 数组中部分元素缺失 content 字段时跳过"""
        items = [
            {"filepath": "/path", "content": "first"},
            {"filepath": "/other"},  # 无 content
            {"content": "third"},  # 无 filepath
        ]
        fp, ct = FileTool._normalize_write_file_items(items=items, filepath=None, content=None)
        assert fp == "/path"
        assert "first" in ct
        assert "third" in ct

    def test_empty_items_returns_originals(self):
        """空 items 数组返回原始 filepath/content"""
        fp, ct = FileTool._normalize_write_file_items(items=[], filepath="/orig", content="orig")
        assert fp == "/orig"
        assert ct == "orig"

    def test_none_items_returns_originals(self):
        """items=None 返回原始 filepath/content"""
        fp, ct = FileTool._normalize_write_file_items(items=None, filepath="/orig", content="orig")
        assert fp == "/orig"
        assert ct == "orig"

    def test_non_list_items_treated_as_none(self):
        """items 为非 list 类型(如 dict)时安全降级"""
        fp, ct = FileTool._normalize_write_file_items(
            items={"filepath": "/x", "content": "y"},  # type: ignore
            filepath=None,
            content=None,
        )
        assert fp is None
        assert ct is None

    def test_partial_flat_params_filled_from_items(self):
        """扁平 filepath 已传但 content 缺失时,从 items 补全 content"""
        items = [{"filepath": "/ignored", "content": "from_items"}]
        fp, ct = FileTool._normalize_write_file_items(items=items, filepath="/flat", content=None)
        assert fp == "/flat"
        assert ct == "from_items"


class TestWriteFileItemsCompat:
    """write_file 方法端到端 items 兼容性测试"""

    @pytest.mark.asyncio
    async def test_standard_flat_call_works(self):
        """标准扁平参数调用正常工作"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.write_file(filepath="/tmp/test.txt", content="hello")
        assert result.success is True
        sandbox.write_file.assert_called_once()
        call_kwargs = sandbox.write_file.call_args.kwargs
        assert call_kwargs["filepath"] == "/tmp/test.txt"
        assert call_kwargs["content"] == "hello"

    @pytest.mark.asyncio
    async def test_items_array_call_normalizes_to_flat(self):
        """items 数组调用自动规范化为扁平参数,正常写入"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        items = [{"filepath": "/tmp/items.txt", "content": "items content"}]
        result = await tool_obj.write_file(items=items)
        assert result.success is True
        sandbox.write_file.assert_called_once()
        call_kwargs = sandbox.write_file.call_args.kwargs
        assert call_kwargs["filepath"] == "/tmp/items.txt"
        assert call_kwargs["content"] == "items content"

    @pytest.mark.asyncio
    async def test_items_array_merges_multiple_content(self):
        """items 数组合并多个 content 后写入"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        items = [
            {"filepath": "/tmp/merged.txt", "content": "section1"},
            {"filepath": "/tmp/merged.txt", "content": "section2"},
        ]
        result = await tool_obj.write_file(items=items)
        assert result.success is True
        call_kwargs = sandbox.write_file.call_args.kwargs
        assert call_kwargs["filepath"] == "/tmp/merged.txt"
        assert "section1" in call_kwargs["content"]
        assert "section2" in call_kwargs["content"]

    @pytest.mark.asyncio
    async def test_missing_filepath_returns_failure_not_raises(self):
        """缺失 filepath 时返回 ToolResult(success=False),不抛异常"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.write_file(content="content only")
        assert result.success is False
        assert "filepath" in result.message
        # sandbox 不应被调用
        sandbox.write_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_content_returns_failure_not_raises(self):
        """缺失 content 时返回 ToolResult(success=False),不抛异常"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.write_file(filepath="/tmp/x.txt")
        assert result.success is False
        assert "content" in result.message
        sandbox.write_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_items_and_no_flat_returns_failure(self):
        """空 items 数组且无扁平参数时返回失败,不抛异常"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        result = await tool_obj.write_file(items=[])
        assert result.success is False
        sandbox.write_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_args_at_all_returns_failure_not_raises(self):
        """完全无参数调用返回失败,不抛 missing positional args 异常"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        # 模拟 BaseTool._filter_parameters 过滤后空 kwargs 调用
        result = await tool_obj.write_file()
        assert result.success is False
        sandbox.write_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_invoke_route_with_items_kwargs(self):
        """通过 BaseTool.invoke 路由调用 write_file 时 items 参数被正确传递"""
        tool_obj, sandbox = _build_file_tool_with_mock_sandbox()
        # 模拟 LLM 误传 items 数组格式
        items_arg = [{"filepath": "/tmp/route.txt", "content": "routed"}]
        result = await tool_obj.invoke("write_file", items=items_arg)
        assert result.success is True
        sandbox.write_file.assert_called_once()
        call_kwargs = sandbox.write_file.call_args.kwargs
        assert call_kwargs["filepath"] == "/tmp/route.txt"
        assert call_kwargs["content"] == "routed"


class TestWriteFileSchemaIntegrity:
    """验证 items 参数未在工具 schema 中暴露(避免诱导 LLM 误用)"""

    def test_items_not_in_tool_schema(self):
        """items 参数不应出现在 write_file 工具的 schema parameters 中"""
        tool_obj, _ = _build_file_tool_with_mock_sandbox()
        tools_schema = tool_obj.get_tools()
        write_file_schema = None
        for s in tools_schema:
            if s["function"]["name"] == "write_file":
                write_file_schema = s
                break
        assert write_file_schema is not None, "write_file 工具未注册"
        # items 不应作为公开参数出现在 schema 中
        properties = write_file_schema["function"]["parameters"]["properties"]
        assert "items" not in properties, "items 参数不应暴露在 write_file schema 中(会诱导 LLM 误用)"
        # 必填字段仍为 filepath + content
        assert set(write_file_schema["function"]["parameters"]["required"]) == {"filepath", "content"}
