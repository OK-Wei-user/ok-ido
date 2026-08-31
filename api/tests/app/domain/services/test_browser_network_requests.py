#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_browser_network_requests.py
缺口4-browser_network_requests网络监控单元测试
- PlaywrightBrowser._on_network_request/_on_network_response: 仅记xhr/fetch、状态回填
- PlaywrightBrowser.network_requests: 返回/过滤/清空/空日志
- BrowserTool工具层: 参数透传
"""
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.browser import BrowserTool
from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser


# ==================== 网络回调测试 ====================


class TestNetworkCallbacks:
    """_on_network_request/_on_network_response: XHR/fetch捕获与状态回填"""

    def _create_browser(self) -> PlaywrightBrowser:
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser._network_log = deque(maxlen=100)
        return browser

    def test_request_logs_only_xhr_and_fetch(self):
        """仅xhr/fetch类型被记录,排除图片/脚本/样式"""
        browser = self._create_browser()

        for rtype in ("xhr", "fetch", "image", "script", "stylesheet", "media"):
            req = MagicMock()
            req.resource_type = rtype
            req.url = f"https://api.example.com/{rtype}"
            req.method = "GET"
            browser._on_network_request(req)

        assert len(browser._network_log) == 2
        types = [e["resource_type"] for e in browser._network_log]
        assert "xhr" in types
        assert "fetch" in types
        assert "image" not in types

    def test_request_callback_exception_swallowed(self):
        """回调异常绝不传播,不影响页面"""
        browser = self._create_browser()
        # resource_type属性访问抛异常
        req = MagicMock()
        del req.resource_type
        type(req).resource_type = property(lambda self: (_ for _ in ()).throw(RuntimeError("x")))

        # 不应抛异常
        browser._on_network_request(req)
        assert len(browser._network_log) == 0

    def test_response_backfills_status_and_duration(self):
        """响应回调回填同URL最近一条未完成请求的状态与耗时"""
        browser = self._create_browser()
        req = MagicMock()
        req.resource_type = "xhr"
        req.url = "https://api.example.com/list"
        req.method = "GET"
        browser._on_network_request(req)

        resp = MagicMock()
        resp.url = "https://api.example.com/list"
        resp.status = 200
        resp.request.resource_type = "xhr"
        browser._on_network_response(resp)

        entry = browser._network_log[0]
        assert entry["status"] == 200
        assert entry["duration_ms"] is not None
        assert isinstance(entry["duration_ms"], int)

    def test_response_ignores_non_xhr(self):
        """响应回调忽略非xhr/fetch类型"""
        browser = self._create_browser()
        resp = MagicMock()
        resp.url = "https://cdn.example.com/img.png"
        resp.status = 200
        resp.request.resource_type = "image"
        browser._on_network_response(resp)

        assert len(browser._network_log) == 0

    def test_deque_maxlen_prevents_unbounded_growth(self):
        """deque上限防止日志膨胀"""
        browser = self._create_browser()
        for i in range(150):
            req = MagicMock()
            req.resource_type = "xhr"
            req.url = f"https://api.example.com/{i}"
            req.method = "GET"
            browser._on_network_request(req)

        assert len(browser._network_log) == 100  # maxlen=100


# ==================== network_requests方法测试 ====================


class TestNetworkRequestsMethod:
    """PlaywrightBrowser.network_requests: 返回/过滤/清空"""

    def _create_browser(self, entries=None) -> PlaywrightBrowser:
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser._network_log = deque(maxlen=100)
        if entries:
            browser._network_log.extend(entries)
        return browser

    @pytest.mark.asyncio
    async def test_returns_all_entries_by_default(self):
        """默认返回最近20条"""
        entries = [
            {"url": f"https://api.example.com/{i}", "method": "GET",
             "resource_type": "xhr", "status": 200, "duration_ms": 10}
            for i in range(25)
        ]
        browser = self._create_browser(entries)

        result = await browser.network_requests()

        assert result.success is True
        assert len(result.data["requests"]) == 20
        assert result.data["total_captured"] == 25

    @pytest.mark.asyncio
    async def test_url_filter(self):
        """url_filter按子串过滤"""
        entries = [
            {"url": "https://api.example.com/users", "method": "GET",
             "resource_type": "xhr", "status": 200, "duration_ms": 10},
            {"url": "https://api.example.com/orders", "method": "GET",
             "resource_type": "xhr", "status": 200, "duration_ms": 10},
            {"url": "https://cdn.example.com/static", "method": "GET",
             "resource_type": "fetch", "status": 200, "duration_ms": 5},
        ]
        browser = self._create_browser(entries)

        result = await browser.network_requests(url_filter="users")

        assert result.success is True
        assert len(result.data["requests"]) == 1
        assert "users" in result.data["requests"][0]["url"]

    @pytest.mark.asyncio
    async def test_clear_empties_log(self):
        """clear=True获取后清空日志"""
        entries = [{"url": "https://api.example.com/x", "method": "GET",
                    "resource_type": "xhr", "status": 200, "duration_ms": 10}]
        browser = self._create_browser(entries)

        result = await browser.network_requests(clear=True)

        assert result.success is True
        assert len(result.data["requests"]) == 1
        assert len(browser._network_log) == 0

    @pytest.mark.asyncio
    async def test_empty_log_returns_empty_list(self):
        """空日志返回空列表"""
        browser = self._create_browser()

        result = await browser.network_requests()

        assert result.success is True
        assert result.data["requests"] == []
        assert result.data["total_captured"] == 0

    @pytest.mark.asyncio
    async def test_max_entries_limit(self):
        """max_entries限制返回条数"""
        entries = [
            {"url": f"https://api.example.com/{i}", "method": "GET",
             "resource_type": "xhr", "status": 200, "duration_ms": 10}
            for i in range(10)
        ]
        browser = self._create_browser(entries)

        result = await browser.network_requests(max_entries=3)

        assert len(result.data["requests"]) == 3


# ==================== BrowserTool工具层测试 ====================


class TestBrowserToolNetworkRequests:
    """BrowserTool.browser_network_requests: 工具层参数透传"""

    def _create_tool(self):
        mock_browser = MagicMock()
        return BrowserTool(browser=mock_browser), mock_browser

    @pytest.mark.asyncio
    async def test_passes_all_params(self):
        """正确透传max_entries/url_filter/clear"""
        tool, mock_browser = self._create_tool()
        mock_browser.network_requests = AsyncMock(
            return_value=ToolResult(success=True, message="OK")
        )

        await tool.browser_network_requests(max_entries=5, url_filter="api", clear=True)

        mock_browser.network_requests.assert_called_once_with(5, "api", True)

    @pytest.mark.asyncio
    async def test_default_params(self):
        """默认参数透传(max_entries=20/url_filter=None/clear=False)"""
        tool, mock_browser = self._create_tool()
        mock_browser.network_requests = AsyncMock(
            return_value=ToolResult(success=True, message="OK")
        )

        await tool.browser_network_requests()

        mock_browser.network_requests.assert_called_once_with(20, None, False)
