#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_browser_include_diff.py
缺口2-includeDiff SPA快照差异检测单元测试
- _compute_snapshot_diff: 无前次/added/removed/changed/混合
- _extract_interactive_elements: 刷新前保存_prev_ref_map
- view_page(include_diff): 返回diff字段/默认不返回
- _navigate_impl: 导航清空_prev_ref_map
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.models.tool_result import ToolResult
from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser


# ==================== _compute_snapshot_diff 测试 ====================


class TestComputeSnapshotDiff:
    """_compute_snapshot_diff: 前后ref_map差异计算"""

    def _create_browser(self) -> PlaywrightBrowser:
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        return browser

    def test_no_prev_map_returns_has_diff_false(self):
        """_prev_ref_map为空(导航后首次快照)返回has_diff=False"""
        browser = self._create_browser()
        browser._prev_ref_map = {}
        browser._ref_map = {"@e0": {"role": "button", "name": "提交"}}

        diff = browser._compute_snapshot_diff()

        assert diff["has_diff"] is False
        assert diff["added"] == []
        assert diff["removed"] == []
        assert diff["changed"] == []

    def test_added_elements_detected(self):
        """新快照新增的ref元素被识别为added"""
        browser = self._create_browser()
        browser._prev_ref_map = {"@e0": {"role": "button", "name": "A"}}
        browser._ref_map = {
            "@e0": {"role": "button", "name": "A"},
            "@e1": {"role": "button", "name": "B"},  # 新增
        }

        diff = browser._compute_snapshot_diff()

        assert diff["has_diff"] is True
        assert len(diff["added"]) == 1
        assert diff["added"][0]["name"] == "B"
        assert diff["removed"] == []
        assert diff["changed"] == []

    def test_removed_elements_detected(self):
        """新快照消失的ref元素被识别为removed"""
        browser = self._create_browser()
        browser._prev_ref_map = {
            "@e0": {"role": "button", "name": "A"},
            "@e1": {"role": "button", "name": "B"},  # 将消失
        }
        browser._ref_map = {"@e0": {"role": "button", "name": "A"}}

        diff = browser._compute_snapshot_diff()

        assert diff["has_diff"] is True
        assert len(diff["removed"]) == 1
        assert diff["removed"][0]["name"] == "B"
        assert diff["added"] == []

    def test_changed_elements_detected(self):
        """同ref但text/role/name变化的元素被识别为changed"""
        browser = self._create_browser()
        browser._prev_ref_map = {"@e0": {"role": "button", "name": "提交", "text": "提交"}}
        browser._ref_map = {"@e0": {"role": "button", "name": "已提交", "text": "已提交"}}

        diff = browser._compute_snapshot_diff()

        assert diff["has_diff"] is True
        assert len(diff["changed"]) == 1
        assert diff["changed"][0]["ref"] == "@e0"
        assert diff["changed"][0]["old"]["name"] == "提交"
        assert diff["changed"][0]["new"]["name"] == "已提交"

    def test_role_change_detected(self):
        """role变化也被识别为changed"""
        browser = self._create_browser()
        browser._prev_ref_map = {"@e0": {"role": "button", "name": "A"}}
        browser._ref_map = {"@e0": {"role": "link", "name": "A"}}

        diff = browser._compute_snapshot_diff()

        assert diff["has_diff"] is True
        assert len(diff["changed"]) == 1

    def test_identical_snapshots_no_diff(self):
        """前后快照完全相同时has_diff=False"""
        browser = self._create_browser()
        same = {"@e0": {"role": "button", "name": "A", "text": "A"}}
        browser._prev_ref_map = dict(same)
        browser._ref_map = dict(same)

        diff = browser._compute_snapshot_diff()

        assert diff["has_diff"] is False

    def test_mixed_diff_all_categories(self):
        """added/removed/changed同时存在的混合场景"""
        browser = self._create_browser()
        browser._prev_ref_map = {
            "@e0": {"role": "button", "name": "A", "text": "A"},
            "@e1": {"role": "button", "name": "B", "text": "B"},  # 将消失(removed)
            "@e2": {"role": "button", "name": "C", "text": "C"},  # 将变化(changed)
        }
        browser._ref_map = {
            "@e0": {"role": "button", "name": "A", "text": "A"},
            "@e2": {"role": "button", "name": "C-done", "text": "C-done"},
            "@e3": {"role": "button", "name": "D", "text": "D"},  # 新增(added)
        }

        diff = browser._compute_snapshot_diff()

        assert diff["has_diff"] is True
        assert len(diff["added"]) == 1
        assert len(diff["removed"]) == 1
        assert len(diff["changed"]) == 1


# ==================== _extract_interactive_elements 保存前次ref_map 测试 ====================


class TestExtractInteractiveElementsSavesPrev:
    """_extract_interactive_elements刷新前保存_prev_ref_map"""

    @pytest.mark.asyncio
    async def test_refresh_saves_prev_ref_map(self):
        """刷新交互元素缓存时,旧_ref_map保存到_prev_ref_map"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser.page = MagicMock()
        browser.page.evaluate = AsyncMock(return_value=[])
        browser.page.accessibility = MagicMock()
        browser.page.accessibility.snapshot = AsyncMock(return_value=None)
        browser._ensure_page = AsyncMock()  # 避免触发真实CDP连接
        # 预填充旧ref_map
        browser._ref_map = {"@e0": {"role": "button", "name": "旧元素"}}
        old_ref_map = dict(browser._ref_map)

        await browser._extract_interactive_elements()

        # _prev_ref_map应保存刷新前的ref_map
        assert browser._prev_ref_map == old_ref_map

    @pytest.mark.asyncio
    async def test_refresh_increments_snapshot_version(self):
        """刷新交互元素缓存时,快照版本号递增"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser.page = MagicMock()
        browser.page.evaluate = AsyncMock(return_value=[])
        browser.page.accessibility = MagicMock()
        browser.page.accessibility.snapshot = AsyncMock(return_value=None)
        browser._ensure_page = AsyncMock()  # 避免触发真实CDP连接
        version_before = browser._snapshot_version

        await browser._extract_interactive_elements()

        assert browser._snapshot_version == version_before + 1


# ==================== view_page(include_diff) 测试 ====================


class TestViewPageIncludeDiff:
    """view_page的include_diff参数行为"""

    def _create_browser(self) -> PlaywrightBrowser:
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser.page = MagicMock()
        browser.page.url = "https://example.com"
        browser._ensure_page = AsyncMock()
        browser.wait_for_page_load = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        browser._auto_dismiss_blocking_elements = AsyncMock(return_value=[])
        browser._extract_interactive_elements = AsyncMock(return_value=[])
        browser._extract_content = AsyncMock(return_value="content")
        browser.page.evaluate = AsyncMock(return_value={"url": "https://example.com", "title": "T"})
        browser._take_view_screenshot = AsyncMock(return_value=None)
        browser._extract_accessibility_tree = AsyncMock(return_value="")
        browser._format_elements = AsyncMock(return_value=[])
        browser._format_ref_map_for_llm = MagicMock(return_value=[])
        browser._get_pending_dialogs = MagicMock(return_value=[])
        browser._get_dialog_history = MagicMock(return_value=[])
        return browser

    @pytest.mark.asyncio
    async def test_include_diff_false_omits_diff_field(self):
        """include_diff=False(默认)时data不含diff字段"""
        browser = self._create_browser()
        browser._compute_snapshot_diff = MagicMock(
            return_value={"has_diff": False, "added": [], "removed": [], "changed": []}
        )

        result = await browser.view_page(include_diff=False)

        assert result.success is True
        assert "diff" not in result.data
        browser._compute_snapshot_diff.assert_not_called()

    @pytest.mark.asyncio
    async def test_include_diff_true_returns_diff_field(self):
        """include_diff=True时data含diff字段"""
        browser = self._create_browser()
        expected_diff = {"has_diff": True, "added": [{"name": "new"}], "removed": [], "changed": []}
        browser._compute_snapshot_diff = MagicMock(return_value=expected_diff)

        result = await browser.view_page(include_diff=True)

        assert result.success is True
        assert result.data["diff"] == expected_diff
        browser._compute_snapshot_diff.assert_called_once()

    @pytest.mark.asyncio
    async def test_include_diff_default_no_diff(self):
        """默认调用(不传参)不含diff字段"""
        browser = self._create_browser()
        browser._compute_snapshot_diff = MagicMock()

        result = await browser.view_page()

        assert result.success is True
        assert "diff" not in result.data
        browser._compute_snapshot_diff.assert_not_called()


# ==================== 导航清空_prev_ref_map 测试 ====================


class TestNavigateClearsPrevRefMap:
    """_navigate_impl导航时清空_prev_ref_map(无前次可比)"""

    @pytest.mark.asyncio
    async def test_navigate_clears_prev_ref_map(self):
        """导航前清空_prev_ref_map,避免旧页面差异残留"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser._prev_ref_map = {"@e0": {"role": "button", "name": "旧页面元素"}}
        browser._ref_map = {"@e0": {"selector": "old"}}
        browser._network_log.append({"url": "old"})
        # mock导航流程
        browser._ensure_page = AsyncMock()
        browser.page = MagicMock()
        browser.page.goto = AsyncMock()
        browser.page.interactive_elements_cache = []
        browser._wait_dom_stable = AsyncMock()
        browser._wait_for_loading_disappear = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        browser._auto_dismiss_blocking_elements = AsyncMock(return_value=[])
        browser._extract_interactive_elements = AsyncMock(return_value=[])
        browser.page.evaluate = AsyncMock(return_value={"url": "https://new.com", "title": "New"})
        browser._format_elements = AsyncMock(return_value=[])
        browser._format_ref_map_for_llm = MagicMock(return_value=[])
        browser._get_pending_dialogs = MagicMock(return_value=[])
        browser._get_dialog_history = MagicMock(return_value=[])

        await browser._navigate_impl("https://new.com")

        # 导航后_prev_ref_map应被清空(_extract_interactive_elements是mock不会重建)
        assert browser._prev_ref_map == {}
        assert browser._ref_map == {}
        assert len(browser._network_log) == 0
