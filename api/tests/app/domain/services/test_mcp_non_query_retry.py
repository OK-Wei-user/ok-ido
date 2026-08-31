#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_mcp_non_query_retry.py
批次 29 非查询类工具(导出/生成类)连续失败重试检测单元测试

测试覆盖:
- _maybe_hint_failed_retry 方法行为
- 成功结果重置计数
- 首次失败不注入提示
- 第3次起注入 async_mode 引导
- 参数变化重置计数(不同参数是不同业务调用)
- 引导文本包含 async_mode + task_wait 关键词

根因会话0e57b5a4: getWarehousingDetailExport 同步重试 32 次无任何系统提示,
原代码对非查询类工具(export/create/submit等)完全跳过退避检测。
"""
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.mcp import MCPTool


# ========== 测试辅助函数 ==========

def _make_mcp_tool() -> MCPTool:
    """创建 MCPTool 实例(绕过 initialize,无需真实 MCP 服务)

    MCPTool.__init__ 不调用 initialize(),_manager 默认 None,
    可直接测试 _maybe_hint_failed_retry 方法(不依赖 _manager)。
    """
    return MCPTool(sandbox=None, callback_manager=None, session_id="test-session")


def _make_failed_result(message: str = "导出失败") -> ToolResult:
    """构造失败的工具结果"""
    return ToolResult(success=False, message=message)


def _make_success_result() -> ToolResult:
    """构造成功的工具结果"""
    return ToolResult(success=True, data="导出成功")


# ========== _maybe_hint_failed_retry 行为测试 ==========

class TestMaybeHintFailedRetry:
    """非查询类工具连续失败重试检测测试"""

    def test_success_resets_count(self):
        """成功结果应重置计数和 last_fail_key"""
        tool = _make_mcp_tool()
        args = {"startDate": "2026-01-01", "endDate": "2026-01-31"}

        # 先制造一次失败
        tool._maybe_hint_failed_retry(
            "getWarehousingDetailExport", args, _make_failed_result()
        )
        assert tool._non_query_fail_count == 1

        # 成功结果应重置
        tool._maybe_hint_failed_retry(
            "getWarehousingDetailExport", args, _make_success_result()
        )
        assert tool._non_query_fail_count == 0
        assert tool._last_fail_key is None

    def test_first_failure_no_hint(self):
        """首次失败应设置计数为1,不注入提示"""
        tool = _make_mcp_tool()
        args = {"startDate": "2026-01-01", "endDate": "2026-01-31"}
        result = _make_failed_result()
        original_message = result.message

        tool._maybe_hint_failed_retry("getWarehousingDetailExport", args, result)

        assert tool._non_query_fail_count == 1
        # 首次失败不注入提示(message 不变)
        assert result.message == original_message

    def test_second_failure_no_hint(self):
        """第二次失败仍不注入提示(阈值=3)"""
        tool = _make_mcp_tool()
        args = {"startDate": "2026-01-01", "endDate": "2026-01-31"}

        # 第一次失败
        result1 = _make_failed_result()
        tool._maybe_hint_failed_retry("getWarehousingDetailExport", args, result1)
        assert tool._non_query_fail_count == 1

        # 第二次失败(相同参数)
        result2 = _make_failed_result()
        original_message2 = result2.message
        tool._maybe_hint_failed_retry("getWarehousingDetailExport", args, result2)
        assert tool._non_query_fail_count == 2
        # 第二次仍不注入提示
        assert result2.message == original_message2

    def test_third_failure_injects_hint(self):
        """第3次失败应注入 task_wait 异步回退引导(直接加载模式)"""
        tool = _make_mcp_tool()
        args = {"startDate": "2026-01-01", "endDate": "2026-01-31"}
        tool_name = "getWarehousingDetailExport"

        # 连续3次相同参数失败
        for i in range(3):
            result = _make_failed_result()
            tool._maybe_hint_failed_retry(tool_name, args, result)
            if i < 2:
                # 前两次不注入提示
                assert result.message == "导出失败", f"第{i+1}次不应注入提示"
            else:
                # 第3次注入提示(直接加载模式: 引导 task_wait 而非 async_mode)
                assert "导出失败" in result.message
                assert "[系统提示]" in result.message
                assert "task_wait" in result.message
                assert "异步" in result.message

        assert tool._non_query_fail_count == 3

    def test_hint_contains_failure_count(self):
        """注入的提示应包含连续失败次数"""
        tool = _make_mcp_tool()
        args = {"date": "2026-01-01"}
        tool_name = "generateReport"

        # 连续4次失败
        for i in range(4):
            result = _make_failed_result()
            tool._maybe_hint_failed_retry(tool_name, args, result)
            if i >= 2:  # 第3次起
                assert f"{i+1}次" in result.message, f"第{i+1}次应包含失败次数"

        assert tool._non_query_fail_count == 4

    def test_param_change_resets_count(self):
        """参数变化应重置计数(不同参数是不同业务调用)"""
        tool = _make_mcp_tool()
        tool_name = "getWarehousingDetailExport"

        # 两次相同参数失败
        args1 = {"startDate": "2026-01-01", "endDate": "2026-01-31"}
        tool._maybe_hint_failed_retry(tool_name, args1, _make_failed_result())
        tool._maybe_hint_failed_retry(tool_name, args1, _make_failed_result())
        assert tool._non_query_fail_count == 2

        # 参数变化:应重置为1
        args2 = {"startDate": "2026-02-01", "endDate": "2026-02-28"}
        result = _make_failed_result()
        tool._maybe_hint_failed_retry(tool_name, args2, result)
        assert tool._non_query_fail_count == 1
        # 参数变化后首次失败不注入提示
        assert result.message == "导出失败"

    def test_different_tools_independent_count(self):
        """不同工具的失败计数应独立"""
        tool = _make_mcp_tool()
        args = {"date": "2026-01-01"}

        # 工具A失败2次
        for _ in range(2):
            tool._maybe_hint_failed_retry("exportDataA", args, _make_failed_result())
        assert tool._non_query_fail_count == 2

        # 切换到工具B:fail_key不同,应重置为1
        result_b = _make_failed_result()
        tool._maybe_hint_failed_retry("exportDataB", args, result_b)
        assert tool._non_query_fail_count == 1
        assert result_b.message == "导出失败"  # 不注入提示

    def test_hint_includes_tool_name(self):
        """注入的提示应包含工具名"""
        tool = _make_mcp_tool()
        args = {"date": "2026-01-01"}
        tool_name = "getInventoryExport"

        for _ in range(3):
            result = _make_failed_result()
            tool._maybe_hint_failed_retry(tool_name, args, result)

        assert tool_name in result.message

    def test_success_after_failures_resets(self):
        """连续失败后成功应重置计数,后续失败重新从1开始"""
        tool = _make_mcp_tool()
        args = {"date": "2026-01-01"}
        tool_name = "exportData"

        # 2次失败
        for _ in range(2):
            tool._maybe_hint_failed_retry(tool_name, args, _make_failed_result())
        assert tool._non_query_fail_count == 2

        # 成功重置
        tool._maybe_hint_failed_retry(tool_name, args, _make_success_result())
        assert tool._non_query_fail_count == 0

        # 再次失败:应从1开始,不注入提示
        result = _make_failed_result()
        tool._maybe_hint_failed_retry(tool_name, args, result)
        assert tool._non_query_fail_count == 1
        assert result.message == "导出失败"
