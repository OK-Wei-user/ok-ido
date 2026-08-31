#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_browser_wait_for.py
缺口3-browser_wait_for增量等待单元测试
- PlaywrightBrowser.wait_for: 文本出现/文本消失/选择器可见/超时/无参校验
- BrowserTool工具层: 参数透传、无参拦截、超时兜底
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.browser import BrowserTool
from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser


# ==================== PlaywrightBrowser.wait_for 测试 ====================


class TestPlaywrightBrowserWaitFor:
    """PlaywrightBrowser.wait_for: 增量等待(文本出现/消失/选择器可见)"""

    def _create_browser(self) -> PlaywrightBrowser:
        """创建mock PlaywrightBrowser实例(不连接真实浏览器)"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser.page = MagicMock()
        browser.page.url = "https://example.com/page"
        browser._ensure_page = AsyncMock()
        browser._refresh_interactive_cache = AsyncMock()
        browser.page.evaluate = AsyncMock(return_value={"url": "https://example.com", "title": "Demo"})
        return browser

    def _mock_locator(self) -> MagicMock:
        """构造get_by_text返回的mock locator链"""
        locator = MagicMock()
        locator.first = locator
        locator.wait_for = AsyncMock()
        return locator

    @pytest.mark.asyncio
    async def test_wait_for_text_appear_success(self):
        """等待文本出现成功: get_by_text.wait_for(state=visible)"""
        browser = self._create_browser()
        locator = self._mock_locator()
        browser.page.get_by_text = MagicMock(return_value=locator)

        result = await browser.wait_for(text="加载完成", timeout=5.0)

        assert result.success is True
        locator.wait_for.assert_called_once_with(state="visible", timeout=5000)
        assert result.data["waited_for"]["text"] == "加载完成"

    @pytest.mark.asyncio
    async def test_wait_for_disappear_text_success(self):
        """等待文本消失成功: wait_for(state=hidden),异常静默吞掉(已消失)"""
        browser = self._create_browser()
        locator = self._mock_locator()
        browser.page.get_by_text = MagicMock(return_value=locator)

        result = await browser.wait_for(disappear_text="加载中", timeout=3.0)

        assert result.success is True
        locator.wait_for.assert_called_once_with(state="hidden", timeout=3000)
        assert result.data["waited_for"]["disappear_text"] == "加载中"

    @pytest.mark.asyncio
    async def test_wait_for_disappear_text_already_gone_treated_as_satisfied(self):
        """disappear_text元素不存在时(异常)视为已消失,返回成功"""
        browser = self._create_browser()
        locator = self._mock_locator()
        # 元素不存在抛异常,应被静默吞掉视为已消失
        locator.wait_for = AsyncMock(side_effect=RuntimeError("element not found"))
        browser.page.get_by_text = MagicMock(return_value=locator)

        result = await browser.wait_for(disappear_text="加载中", timeout=2.0)

        assert result.success is True

    @pytest.mark.asyncio
    async def test_wait_for_selector_visible_success(self):
        """等待选择器可见成功: page.wait_for_selector(state=visible)"""
        browser = self._create_browser()
        browser.page.wait_for_selector = AsyncMock()

        result = await browser.wait_for(selector=".result-list", timeout=4.0)

        assert result.success is True
        browser.page.wait_for_selector.assert_called_once_with(
            ".result-list", state="visible", timeout=4000,
        )

    @pytest.mark.asyncio
    async def test_wait_for_timeout_returns_friendly_error(self):
        """超时返回友好错误(含等待条件信息)"""
        browser = self._create_browser()
        locator = self._mock_locator()
        locator.wait_for = AsyncMock(side_effect=asyncio.TimeoutError())
        browser.page.get_by_text = MagicMock(return_value=locator)

        result = await browser.wait_for(text="永不出现", timeout=1.0)

        assert result.success is False
        assert "1.0s" in result.message
        assert "永不出现" in result.message

    @pytest.mark.asyncio
    async def test_wait_for_no_params_still_runs(self):
        """无参数时协议层不拦截(工具层拦截),返回成功(仅刷新缓存)"""
        browser = self._create_browser()

        result = await browser.wait_for(timeout=1.0)

        # 协议层无参不报错,正常返回(工具层负责参数校验)
        assert result.success is True
        assert result.data["waited_for"] == {"text": None, "disappear_text": None, "selector": None}

    @pytest.mark.asyncio
    async def test_wait_for_multiple_conditions_all_checked(self):
        """同时指定多个条件时全部检查(text+selector)"""
        browser = self._create_browser()
        text_locator = self._mock_locator()
        browser.page.get_by_text = MagicMock(return_value=text_locator)
        browser.page.wait_for_selector = AsyncMock()

        result = await browser.wait_for(text="完成", selector=".done", timeout=5.0)

        assert result.success is True
        text_locator.wait_for.assert_called_once()
        browser.page.wait_for_selector.assert_called_once()

    @pytest.mark.asyncio
    async def test_wait_for_refreshes_interactive_cache(self):
        """等待条件满足后刷新交互元素缓存,捕获异步渲染新元素"""
        browser = self._create_browser()
        locator = self._mock_locator()
        browser.page.get_by_text = MagicMock(return_value=locator)

        await browser.wait_for(text="就绪", timeout=2.0)

        browser._refresh_interactive_cache.assert_called_once()


# ==================== BrowserTool工具层测试 ====================


class TestBrowserToolWaitFor:
    """BrowserTool.browser_wait_for: 工具层参数透传与边界校验"""

    def _create_tool(self):
        """创建带mock browser的BrowserTool"""
        mock_browser = MagicMock()
        return BrowserTool(browser=mock_browser), mock_browser

    @pytest.mark.asyncio
    async def test_browser_wait_for_passes_all_params(self):
        """browser_wait_for正确透传text/disappear_text/selector/timeout"""
        tool, mock_browser = self._create_tool()
        mock_browser.wait_for = AsyncMock(
            return_value=ToolResult(success=True, message="OK")
        )

        await tool.browser_wait_for(
            text="完成", disappear_text="加载", selector=".list", timeout=8.0,
        )

        mock_browser.wait_for.assert_called_once_with("完成", "加载", ".list", 8.0)

    @pytest.mark.asyncio
    async def test_browser_wait_for_no_condition_returns_error(self):
        """无任何条件参数时工具层直接返回失败,避免空等"""
        tool, mock_browser = self._create_tool()
        mock_browser.wait_for = AsyncMock()

        result = await tool.browser_wait_for()

        assert result.success is False
        assert "至少需要提供一个条件" in result.message
        mock_browser.wait_for.assert_not_called()

    @pytest.mark.asyncio
    async def test_browser_wait_for_timeout_fallback(self):
        """browser层超时由_with_timeout捕获,返回失败"""
        tool, mock_browser = self._create_tool()
        mock_browser.wait_for = AsyncMock(side_effect=asyncio.TimeoutError())

        result = await tool.browser_wait_for(text="x", timeout=1.0)

        assert result.success is False
        assert "超时" in result.message

    @pytest.mark.asyncio
    async def test_browser_wait_for_partial_params(self):
        """仅传selector时其他参数为None"""
        tool, mock_browser = self._create_tool()
        mock_browser.wait_for = AsyncMock(
            return_value=ToolResult(success=True, message="OK")
        )

        await tool.browser_wait_for(selector=".modal")

        mock_browser.wait_for.assert_called_once_with(None, None, ".modal", 10.0)
