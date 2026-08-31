#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_browser_dialog.py
阶段3-JS原生对话框监督器单元测试
- DialogSupervisor模块: attach绑定、auto_dismiss/auto_accept/must_respond三种策略、
  respond响应、pending/history查询、clear清理、超时保护、历史截断
- PlaywrightBrowser集成: initialize创建supervisor、cleanup清理、view_page/navigate返回
  pending_dialogs/dialog_history、respond_dialog代理调用
- BrowserTool工具层: browser_respond_dialog参数透传与超时处理
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.browser import BrowserTool
from app.infrastructure.external.browser.dialog_supervisor import (
    DialogSupervisor,
    PendingDialog,
    DialogRecord,
    POLICY_AUTO_DISMISS,
    POLICY_AUTO_ACCEPT,
    POLICY_MUST_RESPOND,
    _MUST_RESPOND_TIMEOUT,
    _HISTORY_MAX_SIZE,
)
from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser


# ==================== DialogSupervisor 模块测试 ====================


class TestDialogSupervisorAttach:
    """DialogSupervisor.attach: 绑定到Page并注册dialog事件处理器"""

    @pytest.mark.asyncio
    async def test_attach_registers_dialog_handler(self):
        """attach成功注册page.on('dialog')处理器"""
        page = MagicMock()
        page.on = MagicMock()
        supervisor = DialogSupervisor(policy=POLICY_AUTO_DISMISS)

        await supervisor.attach(page)

        page.on.assert_called_once()
        assert page.on.call_args[0][0] == "dialog"
        assert supervisor._attached is True

    @pytest.mark.asyncio
    async def test_attach_idempotent(self):
        """重复attach不会重复注册(幂等)"""
        page = MagicMock()
        page.on = MagicMock()
        supervisor = DialogSupervisor()
        supervisor._attached = True

        await supervisor.attach(page)

        page.on.assert_not_called()

    @pytest.mark.asyncio
    async def test_attach_failure_does_not_raise(self):
        """attach失败不抛异常(不影响主流程)"""
        page = MagicMock()
        page.on = MagicMock(side_effect=RuntimeError("page closed"))
        supervisor = DialogSupervisor()

        # 不应抛出异常
        await supervisor.attach(page)

        assert supervisor._attached is False


class TestDialogSupervisorAutoDismiss:
    """auto_dismiss策略: 立即dismiss对话框"""

    @pytest.mark.asyncio
    async def test_auto_dismiss_calls_dismiss(self):
        """auto_dismiss策略下对话框被立即dismiss"""
        supervisor = DialogSupervisor(policy=POLICY_AUTO_DISMISS)
        dialog = MagicMock()
        dialog.type = "alert"
        dialog.message = "提示信息"
        dialog.default_prompt = ""
        dialog.dismiss = AsyncMock()
        dialog.accept = AsyncMock()

        # 触发_on_dialog回调(同步函数,内部ensure_future调度异步处理)
        supervisor._on_dialog(dialog)
        # 等待ensure_future调度的协程完成
        await asyncio.sleep(0.05)

        dialog.dismiss.assert_called_once()
        dialog.accept.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_dismiss_adds_to_history(self):
        """auto_dismiss处理后对话框进入history(标记auto=True)"""
        supervisor = DialogSupervisor(policy=POLICY_AUTO_DISMISS)
        dialog = MagicMock()
        dialog.type = "confirm"
        dialog.message = "确认删除?"
        dialog.default_prompt = ""
        dialog.dismiss = AsyncMock()

        supervisor._on_dialog(dialog)
        await asyncio.sleep(0.05)

        history = supervisor.get_dialog_history()
        assert len(history) == 1
        assert history[0]["kind"] == "confirm"
        assert history[0]["accept"] is False
        assert history[0]["auto"] is True
        # pending应为空(已处理)
        assert supervisor.get_pending_dialogs() == []

    @pytest.mark.asyncio
    async def test_auto_dismiss_failure_still_records_history(self):
        """dismiss异常时仍记录history(防内存泄漏)"""
        supervisor = DialogSupervisor(policy=POLICY_AUTO_DISMISS)
        dialog = MagicMock()
        dialog.type = "alert"
        dialog.message = "msg"
        dialog.default_prompt = ""
        dialog.dismiss = AsyncMock(side_effect=RuntimeError("dialog gone"))

        supervisor._on_dialog(dialog)
        await asyncio.sleep(0.05)

        # 异常不应传播,history仍记录
        assert len(supervisor.get_dialog_history()) == 1


class TestDialogSupervisorAutoAccept:
    """auto_accept策略: 立即accept对话框"""

    @pytest.mark.asyncio
    async def test_auto_accept_calls_accept_with_default_prompt(self):
        """auto_accept策略下prompt对话框被accept并填入default_prompt"""
        supervisor = DialogSupervisor(policy=POLICY_AUTO_ACCEPT)
        dialog = MagicMock()
        dialog.type = "prompt"
        dialog.message = "请输入姓名:"
        dialog.default_prompt = "张三"
        dialog.accept = AsyncMock()
        dialog.dismiss = AsyncMock()

        supervisor._on_dialog(dialog)
        await asyncio.sleep(0.05)

        dialog.accept.assert_called_once_with("张三")
        dialog.dismiss.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_accept_alert_calls_accept_empty(self):
        """auto_accept策略下alert对话框accept(无prompt_text)"""
        supervisor = DialogSupervisor(policy=POLICY_AUTO_ACCEPT)
        dialog = MagicMock()
        dialog.type = "alert"
        dialog.message = "通知"
        dialog.default_prompt = ""
        dialog.accept = AsyncMock()

        supervisor._on_dialog(dialog)
        await asyncio.sleep(0.05)

        dialog.accept.assert_called_once_with("")


class TestDialogSupervisorMustRespond:
    """must_respond策略: 通过Future等待LLM响应,超时自动dismiss"""

    @pytest.mark.asyncio
    async def test_must_respond_waits_for_llm_response(self):
        """must_respond策略下对话框进入pending,等待LLM调用respond"""
        supervisor = DialogSupervisor(policy=POLICY_MUST_RESPOND)
        dialog = MagicMock()
        dialog.type = "confirm"
        dialog.message = "确认提交?"
        dialog.default_prompt = ""
        dialog.accept = AsyncMock()
        dialog.dismiss = AsyncMock()

        supervisor._on_dialog(dialog)
        await asyncio.sleep(0.05)

        # 对话框应处于pending状态
        pending = supervisor.get_pending_dialogs()
        assert len(pending) == 1
        assert pending[0]["kind"] == "confirm"
        dialog_id = pending[0]["id"]

        # LLM调用respond接受对话框
        ok = await supervisor.respond(dialog_id, accept=True)
        assert ok is True
        await asyncio.sleep(0.05)

        dialog.accept.assert_called_once()
        # 处理后pending清空,history记录
        assert supervisor.get_pending_dialogs() == []
        history = supervisor.get_dialog_history()
        assert len(history) == 1
        assert history[0]["accept"] is True
        assert history[0]["auto"] is False

    @pytest.mark.asyncio
    async def test_must_respond_respond_with_prompt_text(self):
        """must_respond策略下prompt对话框响应时携带prompt_text"""
        supervisor = DialogSupervisor(policy=POLICY_MUST_RESPOND)
        dialog = MagicMock()
        dialog.type = "prompt"
        dialog.message = "输入邮箱:"
        dialog.default_prompt = ""
        dialog.accept = AsyncMock()

        supervisor._on_dialog(dialog)
        await asyncio.sleep(0.05)

        dialog_id = supervisor.get_pending_dialogs()[0]["id"]
        await supervisor.respond(dialog_id, accept=True, prompt_text="test@example.com")
        await asyncio.sleep(0.05)

        dialog.accept.assert_called_once_with("test@example.com")

    @pytest.mark.asyncio
    async def test_must_respond_dismiss_via_respond(self):
        """must_respond策略下LLM可调用respond拒绝对话框(accept=False)"""
        supervisor = DialogSupervisor(policy=POLICY_MUST_RESPOND)
        dialog = MagicMock()
        dialog.type = "confirm"
        dialog.message = "确认?"
        dialog.default_prompt = ""
        dialog.dismiss = AsyncMock()

        supervisor._on_dialog(dialog)
        await asyncio.sleep(0.05)

        dialog_id = supervisor.get_pending_dialogs()[0]["id"]
        await supervisor.respond(dialog_id, accept=False)
        await asyncio.sleep(0.05)

        dialog.dismiss.assert_called_once()

    @pytest.mark.asyncio
    async def test_must_respond_timeout_auto_dismiss(self):
        """must_respond超时后自动dismiss(防页面卡死)"""
        # 使用极短超时加速测试
        with patch(
            "app.infrastructure.external.browser.dialog_supervisor._MUST_RESPOND_TIMEOUT",
            0.1,
        ):
            supervisor = DialogSupervisor(policy=POLICY_MUST_RESPOND)
            dialog = MagicMock()
            dialog.type = "alert"
            dialog.message = "超时测试"
            dialog.default_prompt = ""
            dialog.dismiss = AsyncMock()

            supervisor._on_dialog(dialog)
            # 等待超时触发(0.1s超时 + 缓冲)
            await asyncio.sleep(0.3)

            dialog.dismiss.assert_called_once()
            history = supervisor.get_dialog_history()
            assert len(history) == 1
            assert history[0]["accept"] is False
            assert history[0]["auto"] is True

    @pytest.mark.asyncio
    async def test_respond_nonexistent_dialog_returns_false(self):
        """respond对不存在的dialog_id返回False"""
        supervisor = DialogSupervisor(policy=POLICY_MUST_RESPOND)

        ok = await supervisor.respond("dialog_999", accept=True)

        assert ok is False

    @pytest.mark.asyncio
    async def test_respond_already_done_dialog_returns_false(self):
        """respond对已响应过的对话框返回False(不可重复响应)"""
        supervisor = DialogSupervisor(policy=POLICY_MUST_RESPOND)
        dialog = MagicMock()
        dialog.type = "confirm"
        dialog.message = "msg"
        dialog.default_prompt = ""
        dialog.accept = AsyncMock()

        supervisor._on_dialog(dialog)
        await asyncio.sleep(0.05)

        dialog_id = supervisor.get_pending_dialogs()[0]["id"]
        first_ok = await supervisor.respond(dialog_id, accept=True)
        await asyncio.sleep(0.05)

        # 再次响应应失败
        second_ok = await supervisor.respond(dialog_id, accept=True)

        assert first_ok is True
        assert second_ok is False


class TestDialogSupervisorHistoryAndClear:
    """DialogSupervisor历史记录与clear方法"""

    @pytest.mark.asyncio
    async def test_history_truncation(self):
        """history超过上限时截断保留最近记录"""
        supervisor = DialogSupervisor(policy=POLICY_AUTO_DISMISS)
        # 制造超过上限数量的对话框
        for i in range(_HISTORY_MAX_SIZE + 5):
            dialog = MagicMock()
            dialog.type = "alert"
            dialog.message = f"msg_{i}"
            dialog.default_prompt = ""
            dialog.dismiss = AsyncMock()
            supervisor._on_dialog(dialog)
            await asyncio.sleep(0.01)

        history = supervisor.get_dialog_history()
        # 截断到上限
        assert len(history) == _HISTORY_MAX_SIZE
        # 保留最近的记录(最后一条是msg_对应最大序号)
        assert history[-1]["message"] == "msg_24"

    @pytest.mark.asyncio
    async def test_clear_empties_all_state(self):
        """clear清空pending/history/futures"""
        supervisor = DialogSupervisor(policy=POLICY_MUST_RESPOND)
        dialog = MagicMock()
        dialog.type = "confirm"
        dialog.message = "msg"
        dialog.default_prompt = ""
        dialog.accept = AsyncMock()
        supervisor._on_dialog(dialog)
        await asyncio.sleep(0.05)

        # 此时pending非空
        assert len(supervisor.get_pending_dialogs()) == 1

        supervisor.clear()

        assert supervisor.get_pending_dialogs() == []
        assert supervisor.get_dialog_history() == []
        assert supervisor._futures == {}

    @pytest.mark.asyncio
    async def test_clear_cancels_pending_futures(self):
        """clear取消未完成的Future(防止must_respond悬挂)"""
        supervisor = DialogSupervisor(policy=POLICY_MUST_RESPOND)
        dialog = MagicMock()
        dialog.type = "alert"
        dialog.message = "msg"
        dialog.default_prompt = ""
        dialog.dismiss = AsyncMock()
        supervisor._on_dialog(dialog)
        await asyncio.sleep(0.05)

        dialog_id = supervisor.get_pending_dialogs()[0]["id"]
        future = supervisor._futures.get(dialog_id)
        assert future is not None
        assert not future.done()

        supervisor.clear()

        assert future.cancelled() or future.done()

    def test_pending_dialog_to_dict(self):
        """PendingDialog.to_dict返回完整字段"""
        pending = PendingDialog("dialog_1", "prompt", "输入:", "默认值")

        result = pending.to_dict()

        assert result == {
            "id": "dialog_1",
            "kind": "prompt",
            "message": "输入:",
            "default_prompt": "默认值",
        }

    def test_dialog_record_to_dict(self):
        """DialogRecord.to_dict返回完整字段"""
        record = DialogRecord("dialog_2", "confirm", "确认?", True, "", False)

        result = record.to_dict()

        assert result == {
            "id": "dialog_2",
            "kind": "confirm",
            "message": "确认?",
            "accept": True,
            "prompt_text": "",
            "auto": False,
        }


class TestDialogSupervisorOnDialogFallback:
    """_on_dialog异常回退场景"""

    @pytest.mark.asyncio
    async def test_on_dialog_runtime_error_falls_back_to_sync_dismiss(self):
        """无事件循环时回退到同步dismiss(防止页面卡死)"""
        supervisor = DialogSupervisor(policy=POLICY_AUTO_DISMISS)
        dialog = MagicMock()
        dialog.type = "alert"
        dialog.message = "msg"
        dialog.default_prompt = ""
        dialog.dismiss = MagicMock()

        # 模拟ensure_future抛RuntimeError(无事件循环)
        with patch("asyncio.ensure_future", side_effect=RuntimeError("no loop")):
            supervisor._on_dialog(dialog)

        # 应回退到同步dismiss
        dialog.dismiss.assert_called_once()


# ==================== PlaywrightBrowser 集成测试 ====================


class TestPlaywrightBrowserDialogIntegration:
    """PlaywrightBrowser与DialogSupervisor集成测试"""

    def _create_browser(self) -> PlaywrightBrowser:
        """创建带mock page的PlaywrightBrowser实例(supervisor未初始化)"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser.page = MagicMock()
        browser.page.url = "https://example.com/page"
        browser.page.query_selector = AsyncMock(return_value=None)
        browser.page.evaluate = AsyncMock(return_value={})
        browser.page.accessibility = MagicMock()
        browser.page.accessibility.snapshot = AsyncMock(return_value=None)
        browser.page.screenshot = AsyncMock(return_value=b"")
        browser.page.content = AsyncMock(return_value="<html></html>")
        browser.browser = MagicMock()
        browser._ensure_page = AsyncMock()
        browser._wait_dom_stable = AsyncMock()
        browser._wait_for_loading_disappear = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        browser._auto_dismiss_blocking_elements = AsyncMock(return_value=[])
        browser._extract_interactive_elements = AsyncMock(return_value=[])
        browser._format_elements = AsyncMock(return_value=[])
        browser._format_ref_map_for_llm = MagicMock(return_value={})
        browser._extract_content = AsyncMock(return_value="")
        browser._take_view_screenshot = AsyncMock(return_value=None)
        browser._extract_accessibility_tree = AsyncMock(return_value="")
        browser.wait_for_page_load = AsyncMock(return_value=True)
        return browser

    def test_get_pending_dialogs_no_supervisor(self):
        """supervisor未绑定时_get_pending_dialogs返回空列表"""
        browser = self._create_browser()

        assert browser._get_pending_dialogs() == []

    def test_get_dialog_history_no_supervisor(self):
        """supervisor未绑定时_get_dialog_history返回空列表"""
        browser = self._create_browser()

        assert browser._get_dialog_history() == []

    @pytest.mark.asyncio
    async def test_view_page_returns_dialog_fields(self):
        """view_page返回结果包含pending_dialogs和dialog_history字段"""
        browser = self._create_browser()
        # 手动注入supervisor并填充测试数据
        browser._dialog_supervisor = MagicMock()
        browser._dialog_supervisor.get_pending_dialogs = MagicMock(
            return_value=[{"id": "dialog_1", "kind": "alert", "message": "hi"}]
        )
        browser._dialog_supervisor.get_dialog_history = MagicMock(return_value=[])

        result = await browser.view_page()

        assert result.success is True
        assert result.data["pending_dialogs"] == [
            {"id": "dialog_1", "kind": "alert", "message": "hi"}
        ]
        assert result.data["dialog_history"] == []

    @pytest.mark.asyncio
    async def test_navigate_clears_supervisor_and_returns_dialog_fields(self):
        """navigate导航前清空supervisor,返回结果包含对话框字段"""
        browser = self._create_browser()
        browser.page.goto = AsyncMock()
        browser.page.interactive_elements_cache = []
        # 注入mock supervisor验证clear被调用
        browser._dialog_supervisor = MagicMock()
        browser._dialog_supervisor.clear = MagicMock()
        browser._dialog_supervisor.get_pending_dialogs = MagicMock(return_value=[])
        browser._dialog_supervisor.get_dialog_history = MagicMock(return_value=[])

        result = await browser._navigate_impl("https://example.com")

        assert result.success is True
        browser._dialog_supervisor.clear.assert_called_once()
        assert "pending_dialogs" in result.data
        assert "dialog_history" in result.data

    @pytest.mark.asyncio
    async def test_respond_dialog_proxies_to_supervisor(self):
        """respond_dialog代理调用supervisor.respond"""
        browser = self._create_browser()
        browser._dialog_supervisor = MagicMock()
        browser._dialog_supervisor.respond = AsyncMock(return_value=True)

        result = await browser.respond_dialog("dialog_1", accept=True, prompt_text="ok")

        assert result.success is True
        assert result.data["dialog_id"] == "dialog_1"
        assert result.data["accept"] is True
        browser._dialog_supervisor.respond.assert_called_once_with("dialog_1", True, "ok")

    @pytest.mark.asyncio
    async def test_respond_dialog_supervisor_not_initialized(self):
        """supervisor未初始化时respond_dialog返回失败"""
        browser = self._create_browser()

        result = await browser.respond_dialog("dialog_1", accept=True)

        assert result.success is False
        assert "未初始化" in result.message

    @pytest.mark.asyncio
    async def test_respond_dialog_invalid_id(self):
        """respond_dialog对不存在的dialog_id返回失败"""
        browser = self._create_browser()
        browser._dialog_supervisor = MagicMock()
        browser._dialog_supervisor.respond = AsyncMock(return_value=False)

        result = await browser.respond_dialog("dialog_x", accept=False)

        assert result.success is False
        assert "不存在" in result.message


class TestPlaywrightBrowserDialogLifecycle:
    """PlaywrightBrowser生命周期中supervisor的创建与清理"""

    @pytest.mark.asyncio
    async def test_initialize_creates_and_attaches_supervisor(self):
        """initialize成功后supervisor被创建并绑定到page"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        mock_playwright = MagicMock()
        mock_browser_obj = MagicMock()
        mock_context = MagicMock()
        mock_page = MagicMock()

        mock_playwright.chromium = MagicMock()
        mock_playwright.chromium.connect_over_cdp = AsyncMock(return_value=mock_browser_obj)
        mock_browser_obj.contexts = [mock_context]
        mock_context.pages = []  # 触发 new_page 分支
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_page.url = "about:blank"

        with patch(
            "app.infrastructure.external.browser.playwright_browser.async_playwright"
        ) as mock_async_pw:
            mock_async_pw.return_value.start = AsyncMock(return_value=mock_playwright)
            ok = await browser.initialize()

        assert ok is True
        assert browser._dialog_supervisor is not None
        assert browser._dialog_supervisor._attached is True

    @pytest.mark.asyncio
    async def test_cleanup_clears_supervisor(self):
        """cleanup清理supervisor状态并置None"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        # 模拟已绑定的supervisor(保存引用以便cleanup后断言)
        mock_supervisor = MagicMock()
        browser._dialog_supervisor = mock_supervisor
        browser.playwright = MagicMock()
        browser.browser = MagicMock()
        browser.page = MagicMock()
        browser.browser.contexts = []
        browser.browser.close = AsyncMock()
        browser.playwright.stop = AsyncMock()
        browser.page.is_closed = MagicMock(return_value=False)
        browser.page.close = AsyncMock()

        await browser.cleanup()

        mock_supervisor.clear.assert_called_once()
        assert browser._dialog_supervisor is None


# ==================== BrowserTool 工具层测试 ====================


class TestBrowserToolRespondDialog:
    """BrowserTool.browser_respond_dialog参数透传与异常处理"""

    def _create_tool(self):
        """创建带mock browser的BrowserTool"""
        mock_browser = MagicMock()
        return BrowserTool(browser=mock_browser), mock_browser

    @pytest.mark.asyncio
    async def test_browser_respond_dialog_passes_params(self):
        """browser_respond_dialog正确透传dialog_id/accept/prompt_text"""
        tool, mock_browser = self._create_tool()
        mock_browser.respond_dialog = AsyncMock(
            return_value=ToolResult(success=True, data={"dialog_id": "dialog_1"})
        )

        await tool.browser_respond_dialog(
            dialog_id="dialog_1", accept=True, prompt_text="hello",
        )

        mock_browser.respond_dialog.assert_called_once_with("dialog_1", True, "hello")

    @pytest.mark.asyncio
    async def test_browser_respond_dialog_default_prompt_text(self):
        """browser_respond_dialog不传prompt_text时默认空字符串"""
        tool, mock_browser = self._create_tool()
        mock_browser.respond_dialog = AsyncMock(
            return_value=ToolResult(success=True)
        )

        await tool.browser_respond_dialog(dialog_id="dialog_2", accept=False)

        mock_browser.respond_dialog.assert_called_once_with("dialog_2", False, "")

    @pytest.mark.asyncio
    async def test_browser_respond_dialog_handles_timeout(self):
        """browser_respond_dialog超时时返回失败结果"""
        tool, mock_browser = self._create_tool()
        mock_browser.respond_dialog = AsyncMock(side_effect=asyncio.TimeoutError())

        result = await tool.browser_respond_dialog(
            dialog_id="dialog_3", accept=True,
        )

        assert result.success is False
        assert "超时" in result.message

    @pytest.mark.asyncio
    async def test_browser_respond_dialog_handles_exception(self):
        """browser_respond_dialog异常时返回失败结果"""
        tool, mock_browser = self._create_tool()
        mock_browser.respond_dialog = AsyncMock(
            side_effect=RuntimeError("unexpected error")
        )

        result = await tool.browser_respond_dialog(
            dialog_id="dialog_4", accept=False,
        )

        assert result.success is False
        assert "失败" in result.message

    @pytest.mark.asyncio
    async def test_browser_respond_dialog_propagates_failure_result(self):
        """browser_respond_dialog透传browser层返回的失败结果"""
        tool, mock_browser = self._create_tool()
        mock_browser.respond_dialog = AsyncMock(
            return_value=ToolResult(success=False, message="对话框[dialog_5]不存在")
        )

        result = await tool.browser_respond_dialog(
            dialog_id="dialog_5", accept=True,
        )

        assert result.success is False
        assert "不存在" in result.message
