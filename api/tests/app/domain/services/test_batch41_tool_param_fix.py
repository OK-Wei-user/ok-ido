# -*- coding: utf-8 -*-
"""批次41 工具参数缺失修复单元测试

覆盖两个修复:
1. Fix 1(根因): ShellTool.shell_execute 将 session_id/exec_dir 改为可选,自动生成默认值
   - 根因: DeepSeek 偶发遗漏 session_id → TypeError → 100次迭代循环 → 10分钟超时
   - 修复: 省略时自动生成 session_id 并使用默认 exec_dir,沙箱侧已支持按需创建会话

2. Fix 2(防御): BaseTool.invoke 调用前校验必填参数,返回清晰错误而非原始 TypeError
   - 防御: 所有工具受益,缺失必填参数时返回可读错误,避免 LLM 因不可读 TypeError 循环重试
"""
import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.base import BaseTool, tool
from app.domain.services.tools.shell import ShellTool
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


# ============================================================================
# Fix 1: ShellTool.shell_execute 可选 session_id/exec_dir
# ============================================================================

class TestShellExecuteOptionalSessionId:
    """ShellTool.shell_execute session_id/exec_dir 可选参数测试(批次41 Fix 1)"""

    def setup_method(self):
        self.sandbox = AsyncMock(spec=DockerSandbox)
        self.shell_tool = ShellTool(sandbox=self.sandbox)

    def test_session_id_not_in_required(self):
        """session_id 不应在 required 列表中(LLM 可省略)"""
        tools = self.shell_tool.get_tools()
        exec_tool = next(t for t in tools if t["function"]["name"] == "shell_execute")
        required = exec_tool["function"]["parameters"]["required"]
        assert "session_id" not in required, "session_id 应为可选参数"

    def test_exec_dir_not_in_required(self):
        """exec_dir 不应在 required 列表中(LLM 可省略)"""
        tools = self.shell_tool.get_tools()
        exec_tool = next(t for t in tools if t["function"]["name"] == "shell_execute")
        required = exec_tool["function"]["parameters"]["required"]
        assert "exec_dir" not in required, "exec_dir 应为可选参数"

    def test_command_still_required(self):
        """command 仍应在 required 列表中(核心参数不可省略)"""
        tools = self.shell_tool.get_tools()
        exec_tool = next(t for t in tools if t["function"]["name"] == "shell_execute")
        required = exec_tool["function"]["parameters"]["required"]
        assert "command" in required, "command 应为必填参数"

    def test_description_mentions_optional_session_id(self):
        """工具描述应告知 LLM session_id 为可选"""
        tools = self.shell_tool.get_tools()
        exec_tool = next(t for t in tools if t["function"]["name"] == "shell_execute")
        description = exec_tool["function"]["description"]
        assert "可选" in description or "省略" in description

    @pytest.mark.asyncio
    async def test_auto_generate_session_id_when_omitted(self):
        """省略 session_id 时自动生成并传给沙箱"""
        self.sandbox.exec_command = AsyncMock(return_value=ToolResult(success=True))
        await self.shell_tool.shell_execute(
            exec_dir="/home/ubuntu",
            command="echo hello",
        )
        # 沙箱应被调用,且 session_id 非空(自动生成)
        self.sandbox.exec_command.assert_called_once()
        call_args = self.sandbox.exec_command.call_args
        session_id = call_args[0][0]  # 第一个位置参数
        assert session_id and session_id.startswith("shell_"), \
            f"自动生成的 session_id 应以 'shell_' 开头,实际: {session_id}"

    @pytest.mark.asyncio
    async def test_default_exec_dir_when_omitted(self):
        """省略 exec_dir 时使用默认值 /home/ubuntu"""
        self.sandbox.exec_command = AsyncMock(return_value=ToolResult(success=True))
        await self.shell_tool.shell_execute(
            session_id="test-session",
            command="echo hello",
        )
        self.sandbox.exec_command.assert_called_once()
        call_args = self.sandbox.exec_command.call_args
        exec_dir = call_args[0][1]  # 第二个位置参数
        assert exec_dir == "/home/ubuntu", f"默认 exec_dir 应为 /home/ubuntu,实际: {exec_dir}"

    @pytest.mark.asyncio
    async def test_both_omitted_auto_fill(self):
        """同时省略 session_id 和 exec_dir 时两者都自动填充"""
        self.sandbox.exec_command = AsyncMock(return_value=ToolResult(success=True))
        await self.shell_tool.shell_execute(command="echo hello")
        self.sandbox.exec_command.assert_called_once()
        call_args = self.sandbox.exec_command.call_args
        session_id = call_args[0][0]
        exec_dir = call_args[0][1]
        command = call_args[0][2]
        assert session_id and session_id.startswith("shell_")
        assert exec_dir == "/home/ubuntu"
        assert command == "echo hello"

    @pytest.mark.asyncio
    async def test_empty_string_session_id_auto_generated(self):
        """空字符串 session_id 也应触发自动生成(防御性)"""
        self.sandbox.exec_command = AsyncMock(return_value=ToolResult(success=True))
        await self.shell_tool.shell_execute(
            session_id="",
            exec_dir="/home/ubuntu",
            command="echo hello",
        )
        call_args = self.sandbox.exec_command.call_args
        session_id = call_args[0][0]
        assert session_id and session_id.startswith("shell_"), \
            "空字符串 session_id 应触发自动生成"

    @pytest.mark.asyncio
    async def test_provided_session_id_preserved(self):
        """显式传入的 session_id 应被保留(不覆盖)"""
        self.sandbox.exec_command = AsyncMock(return_value=ToolResult(success=True))
        await self.shell_tool.shell_execute(
            session_id="my-custom-session",
            exec_dir="/home/ubuntu",
            command="echo hello",
        )
        call_args = self.sandbox.exec_command.call_args
        assert call_args[0][0] == "my-custom-session"

    @pytest.mark.asyncio
    async def test_invoke_via_base_tool_without_session_id(self):
        """通过 BaseTool.invoke 调用(模拟 LLM 调用路径)省略 session_id 时不报错"""
        self.sandbox.exec_command = AsyncMock(return_value=ToolResult(success=True))
        # LLM 只传 command,省略 session_id 和 exec_dir
        result = await self.shell_tool.invoke(
            "shell_execute",
            command="ls -la",
        )
        assert result.success is True
        self.sandbox.exec_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_invoke_via_base_tool_with_only_command_arg(self):
        """LLM 传 list 格式参数被转为 {"items": [...]} 后, _filter_parameters 过滤掉 items,
        仅剩 command(如有)时不应崩溃 — command 缺失时由 Fix 2 返回清晰错误"""
        self.sandbox.exec_command = AsyncMock(return_value=ToolResult(success=True))
        # 模拟 LLM 只传了一个无效参数(如 items),command 也缺失
        result = await self.shell_tool.invoke(
            "shell_execute",
            items=["some", "list"],
        )
        # command 缺失 → Fix 2 返回清晰错误(非 TypeError)
        assert result.success is False
        assert "command" in result.message


# ============================================================================
# Fix 2: BaseTool.invoke 必填参数校验
# ============================================================================

class TestBaseToolRequiredParamValidation:
    """BaseTool.invoke 必填参数校验测试(批次41 Fix 2)"""

    def setup_method(self):
        """构建带测试工具的 BaseTool 子类"""

        class _TestTool(BaseTool):
            name = "test"

            @tool(
                name="test_action",
                description="测试工具",
                parameters={
                    "required_param": {"type": "string", "description": "必填"},
                    "optional_param": {"type": "string", "description": "可选"},
                },
                required=["required_param"],
            )
            async def test_action(self, required_param: str, optional_param: str = "default"):
                return ToolResult(success=True, data={"required_param": required_param, "optional_param": optional_param})

            @tool(
                name="no_param_action",
                description="无参数工具",
                parameters={},
                required=[],
            )
            async def no_param_action(self):
                return ToolResult(success=True, data="ok")

        self.tool = _TestTool()

    def test_find_missing_required_params_all_present(self):
        """所有必填参数都存在时返回空列表"""
        method = None
        for _, m in inspect.getmembers(self.tool, inspect.ismethod):
            if getattr(m, "_tool_name", None) == "test_action":
                method = m
                break
        assert method is not None
        missing = BaseTool._find_missing_required_params(method, {"required_param": "value"})
        assert missing == []

    def test_find_missing_required_params_one_missing(self):
        """缺少必填参数时返回缺失参数名"""
        method = None
        for _, m in inspect.getmembers(self.tool, inspect.ismethod):
            if getattr(m, "_tool_name", None) == "test_action":
                method = m
                break
        missing = BaseTool._find_missing_required_params(method, {"optional_param": "value"})
        assert missing == ["required_param"]

    def test_find_missing_required_params_optional_not_flagged(self):
        """有默认值的参数不视为缺失"""
        method = None
        for _, m in inspect.getmembers(self.tool, inspect.ismethod):
            if getattr(m, "_tool_name", None) == "test_action":
                method = m
                break
        # 只传 required_param,optional_param 有默认值不应被标记
        missing = BaseTool._find_missing_required_params(method, {"required_param": "value"})
        assert missing == []

    def test_find_missing_required_params_no_required(self):
        """无必填参数的工具始终返回空列表"""
        method = None
        for _, m in inspect.getmembers(self.tool, inspect.ismethod):
            if getattr(m, "_tool_name", None) == "no_param_action":
                method = m
                break
        missing = BaseTool._find_missing_required_params(method, {})
        assert missing == []

    @pytest.mark.asyncio
    async def test_invoke_returns_clear_error_when_required_missing(self):
        """缺少必填参数时返回清晰错误(非 TypeError)"""
        result = await self.tool.invoke("test_action", optional_param="value")
        assert result.success is False
        assert "required_param" in result.message
        assert "缺少" in result.message or "必需" in result.message

    @pytest.mark.asyncio
    async def test_invoke_works_when_all_required_provided(self):
        """提供所有必填参数时正常执行"""
        result = await self.tool.invoke("test_action", required_param="value")
        assert result.success is True
        assert result.data["required_param"] == "value"
        assert result.data["optional_param"] == "default"

    @pytest.mark.asyncio
    async def test_invoke_works_with_extra_params_filtered(self):
        """多余参数被过滤,不影响正常执行"""
        result = await self.tool.invoke(
            "test_action",
            required_param="value",
            unknown_param="should_be_filtered",
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_invoke_no_required_params_works_empty(self):
        """无必填参数的工具空参数调用正常"""
        result = await self.tool.invoke("no_param_action")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_invoke_error_message_is_llm_readable(self):
        """错误消息应包含工具名和参数名,供 LLM 理解并修正"""
        result = await self.tool.invoke("test_action")
        assert result.success is False
        # 错误消息应包含工具名和缺失的参数名
        assert "test_action" in result.message
        assert "required_param" in result.message
        # 不应包含原始 Python TypeError 文本
        assert "positional argument" not in result.message
        assert "TypeError" not in result.message
