#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_browser_ref.py
阶段2-ref引用机制单元测试
- accessibility_snapshot模块: ref_map构建、ref解析、LLM格式化、a11y语义增强
- PlaywrightBrowser集成: ref_map生命周期管理、click/input的ref分支、参数优先级
- BrowserTool工具层: ref/text_locator参数透传
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.browser import BrowserTool
from app.infrastructure.external.browser.accessibility_snapshot import (
    build_ref_map,
    resolve_ref,
    format_refs_for_llm,
    _match_a11y_node,
    _INTERACTIVE_ROLES,
    _MAX_A11Y_NODES,
    _DEFAULT_MAX_REF_ELEMENTS,
    _OFFSCREEN_MAX_DISPLAY,
    _VISIBLE_ELEMENTS_SAFETY_CEILING,
)
from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser


# ==================== accessibility_snapshot 模块测试 ====================


class TestBuildRefMap:
    """build_ref_map: 为可交互元素构建ref映射表"""

    @pytest.mark.asyncio
    async def test_empty_elements_returns_empty_map(self):
        """空交互元素列表返回空ref_map"""
        page = MagicMock()
        page.accessibility.snapshot = AsyncMock(return_value=None)

        ref_map = await build_ref_map(page, [])

        assert ref_map == {}

    @pytest.mark.asyncio
    async def test_build_with_semantic_attrs_only(self):
        """semanticAttrs优先填充role/name(无accessibility tree)"""
        page = MagicMock()
        page.accessibility.snapshot = AsyncMock(return_value=None)
        elements = [
            {
                "index": 0, "tag": "button", "text": "提交",
                "selector": '[data-manus-id="manus-element-0"]',
                "semanticAttrs": {"role": "button", "aria-label": "提交表单"},
                "inViewport": True,
            },
            {
                "index": 1, "tag": "input", "text": "",
                "selector": '[data-manus-id="manus-element-1"]',
                "semanticAttrs": {"role": "textbox", "title": "用户名"},
                "inViewport": True,
            },
        ]

        ref_map = await build_ref_map(page, elements)

        assert "@e0" in ref_map
        assert ref_map["@e0"]["role"] == "button"
        assert ref_map["@e0"]["name"] == "提交表单"
        assert ref_map["@e0"]["selector"] == '[data-manus-id="manus-element-0"]'
        assert "@e1" in ref_map
        assert ref_map["@e1"]["role"] == "textbox"
        assert ref_map["@e1"]["name"] == "用户名"

    @pytest.mark.asyncio
    async def test_build_with_a11y_enhancement(self):
        """accessibility tree补充缺失的role/name"""
        page = MagicMock()
        # 模拟accessibility tree: button节点匹配text "登录"
        page.accessibility.snapshot = AsyncMock(return_value={
            "role": "WebArea",
            "name": "",
            "children": [
                {"role": "button", "name": "登录", "value": ""},
                {"role": "textbox", "name": "搜索框", "value": ""},
            ],
        })
        elements = [
            {
                "index": 0, "tag": "button", "text": "登录",
                "selector": '[data-manus-id="manus-element-0"]',
                "semanticAttrs": {},  # 无语义属性,需a11y补充
                "inViewport": True,
            },
        ]

        ref_map = await build_ref_map(page, elements)

        assert ref_map["@e0"]["role"] == "button"
        assert ref_map["@e0"]["name"] == "登录"

    @pytest.mark.asyncio
    async def test_build_a11y_failure_degrades_gracefully(self):
        """accessibility.snapshot抛异常时退化到semanticAttrs,不影响主流程"""
        page = MagicMock()
        page.accessibility.snapshot = AsyncMock(side_effect=RuntimeError("page detached"))
        elements = [
            {
                "index": 0, "tag": "a", "text": "首页",
                "selector": '[data-manus-id="manus-element-0"]',
                "semanticAttrs": {"role": "link"},
                "inViewport": True,
            },
        ]

        ref_map = await build_ref_map(page, elements)

        # a11y失败后仍能构建ref_map,只是name为空
        assert ref_map["@e0"]["role"] == "link"
        assert ref_map["@e0"]["name"] == ""

    @pytest.mark.asyncio
    async def test_build_preserves_inviewport_flag(self):
        """inViewport=False的元素在ref_map中保留标记"""
        page = MagicMock()
        page.accessibility.snapshot = AsyncMock(return_value=None)
        elements = [
            {
                "index": 0, "tag": "button", "text": "底部按钮",
                "selector": '[data-manus-id="manus-element-0"]',
                "semanticAttrs": {}, "inViewport": False,
            },
        ]

        ref_map = await build_ref_map(page, elements)

        assert ref_map["@e0"]["inViewport"] is False

    @pytest.mark.asyncio
    async def test_build_a11y_truncates_large_tree(self):
        """巨型accessibility tree截断到_MAX_A11Y_NODES防止膨胀"""
        page = MagicMock()
        # 构造超过_MAX_A11Y_NODES个节点的树
        children = [
            {"role": "button", "name": f"btn-{i}", "value": ""}
            for i in range(_MAX_A11Y_NODES + 50)
        ]
        page.accessibility.snapshot = AsyncMock(return_value={
            "role": "WebArea", "name": "", "children": children,
        })
        elements = [
            {
                "index": 0, "tag": "button", "text": "btn-0",
                "selector": '[data-manus-id="manus-element-0"]',
                "semanticAttrs": {}, "inViewport": True,
            },
        ]

        ref_map = await build_ref_map(page, elements)

        # 仍能正确匹配到btn-0(在截断范围内)
        assert ref_map["@e0"]["name"] == "btn-0"


class TestResolveRef:
    """resolve_ref: 通过ref解析为ElementHandle"""

    @pytest.mark.asyncio
    async def test_resolve_success(self):
        """ref存在且selector有效时返回ElementHandle"""
        page = MagicMock()
        expected_element = MagicMock()
        page.query_selector = AsyncMock(return_value=expected_element)
        ref_map = {"@e5": {"selector": '[data-manus-id="manus-element-5"]'}}

        element = await resolve_ref(page, "@e5", ref_map)

        assert element is expected_element
        page.query_selector.assert_called_once_with('[data-manus-id="manus-element-5"]')

    @pytest.mark.asyncio
    async def test_resolve_ref_not_in_map(self):
        """ref不在映射表中返回None"""
        page = MagicMock()
        page.query_selector = AsyncMock()
        ref_map = {"@e1": {"selector": "sel"}}

        element = await resolve_ref(page, "@e99", ref_map)

        assert element is None
        page.query_selector.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_missing_selector(self):
        """ref存在但selector为空返回None"""
        page = MagicMock()
        page.query_selector = AsyncMock()
        ref_map = {"@e0": {"selector": ""}}

        element = await resolve_ref(page, "@e0", ref_map)

        assert element is None
        page.query_selector.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_query_selector_exception(self):
        """query_selector抛异常返回None而非传播异常"""
        page = MagicMock()
        page.query_selector = AsyncMock(side_effect=RuntimeError("page closed"))
        ref_map = {"@e0": {"selector": "div"}}

        element = await resolve_ref(page, "@e0", ref_map)

        assert element is None


class TestFormatRefsForLlm:
    """format_refs_for_llm: 格式化ref映射为LLM可读文本"""

    def test_empty_map_returns_empty_list(self):
        """空ref_map返回空列表"""
        assert format_refs_for_llm({}) == []

    def test_full_fields_format(self):
        """完整字段(role+name+tag+text)格式化"""
        ref_map = {
            "@e0": {
                "role": "button", "name": "提交",
                "tag": "button", "text": "提交",
                "inViewport": True,
            },
        }

        lines = format_refs_for_llm(ref_map)

        assert len(lines) == 1
        assert "[@e0]" in lines[0]
        assert "button" in lines[0]
        assert '"提交"' in lines[0]
        assert "<button>提交</button>" in lines[0]
        assert "[offscreen]" not in lines[0]

    def test_offscreen_marker(self):
        """inViewport=False时显示[offscreen]标记"""
        ref_map = {
            "@e1": {
                "role": "link", "name": "底部链接",
                "tag": "a", "text": "更多",
                "inViewport": False,
            },
        }

        lines = format_refs_for_llm(ref_map)

        # offscreen元素行(非分隔符行)应包含[offscreen]标记
        ref_lines = [l for l in lines if "[@e1]" in l]
        assert len(ref_lines) == 1
        assert "[offscreen]" in ref_lines[0]

    def test_truncation(self):
        """超过max_elements时截断并提示剩余可见元素用text_locator定位"""
        ref_map = {}
        for i in range(10):
            ref_map[f"@e{i}"] = {
                "role": "button", "name": f"btn-{i}",
                "tag": "button", "text": f"btn-{i}",
                "inViewport": True,
            }

        lines = format_refs_for_llm(ref_map, max_elements=3)

        # 3个ref + 1个截断提示(引导text_locator定位,保证信息流转闭环)
        assert len(lines) == 4
        hint_lines = [l for l in lines if "text_locator" in l and "more visible" in l]
        assert len(hint_lines) == 1, "超阈值的可见元素应有text_locator定位提示"
        assert "7 more visible" in hint_lines[0]

    def test_default_max_elements_constant(self):
        """默认截断阈值为_DEFAULT_MAX_REF_ELEMENTS,超出添加text_locator提示"""
        ref_map = {}
        for i in range(_DEFAULT_MAX_REF_ELEMENTS + 10):
            ref_map[f"@e{i}"] = {
                "role": "button", "name": "",
                "tag": "button", "text": "",
                "inViewport": True,
            }

        lines = format_refs_for_llm(ref_map)

        # _DEFAULT_MAX_REF_ELEMENTS个ref + 1个截断提示
        assert len(lines) == _DEFAULT_MAX_REF_ELEMENTS + 1
        assert any("10 more visible" in l and "text_locator" in l for l in lines)

    def test_visible_under_threshold_no_hint(self):
        """可见元素数<=阈值时无截断提示(保持完整ref覆盖)"""
        ref_map = {}
        for i in range(_DEFAULT_MAX_REF_ELEMENTS):
            ref_map[f"@e{i}"] = {
                "role": "button", "name": f"btn-{i}",
                "tag": "button", "text": f"btn-{i}",
                "inViewport": True,
            }

        lines = format_refs_for_llm(ref_map)

        # 恰好等于阈值,无截断提示
        assert len(lines) == _DEFAULT_MAX_REF_ELEMENTS
        assert not any("more visible" in l for l in lines)

    def test_missing_fields_handled(self):
        """缺失role/name/tag/text时不报错"""
        ref_map = {
            "@e0": {"selector": "div", "inViewport": True},
        }

        lines = format_refs_for_llm(ref_map)

        assert len(lines) == 1
        assert "[@e0]" in lines[0]

    # ===== 可见性优先排序测试(会话1146286e优化) =====

    def test_visible_refs_before_offscreen(self):
        """可见元素ref排在offscreen元素ref之前(核心优化)"""
        ref_map = {
            "@e0": {"role": "link", "name": "offscreen-link", "tag": "a", "text": "底部", "inViewport": False},
            "@e1": {"role": "button", "name": "visible-btn", "tag": "button", "text": "提交", "inViewport": True},
            "@e2": {"role": "link", "name": "offscreen-link2", "tag": "a", "text": "更多", "inViewport": False},
            "@e3": {"role": "button", "name": "visible-btn2", "tag": "button", "text": "取消", "inViewport": True},
        }

        lines = format_refs_for_llm(ref_map)

        # 可见元素(@e1, @e3)应出现在offscreen元素(@e0, @e2)之前
        e1_idx = next(i for i, line in enumerate(lines) if "@e1" in line)
        e3_idx = next(i for i, line in enumerate(lines) if "@e3" in line)
        e0_idx = next(i for i, line in enumerate(lines) if "@e0" in line)
        e2_idx = next(i for i, line in enumerate(lines) if "@e2" in line)
        assert e1_idx < e0_idx, "可见元素@e1应在offscreen元素@e0之前"
        assert e3_idx < e2_idx, "可见元素@e3应在offscreen元素@e2之前"

    def test_offscreen_section_separator(self):
        """offscreen元素前有分区分隔符"""
        ref_map = {
            "@e0": {"role": "button", "name": "v1", "tag": "button", "text": "v1", "inViewport": True},
            "@e1": {"role": "link", "name": "o1", "tag": "a", "text": "o1", "inViewport": False},
        }

        lines = format_refs_for_llm(ref_map)

        # 应包含offscreen分隔符
        separator_found = any("--- offscreen elements" in line for line in lines)
        assert separator_found, "offscreen元素区应有分隔符标识"

    def test_offscreen_truncation_with_count(self):
        """offscreen元素超过_OFFSCREEN_MAX_DISPLAY时截断并显示计数"""
        ref_map = {}
        # 添加5个可见元素
        for i in range(5):
            ref_map[f"@e{i}"] = {"role": "button", "name": f"v{i}", "tag": "button", "text": f"v{i}", "inViewport": True}
        # 添加超过_OFFSCREEN_MAX_DISPLAY个offscreen元素
        total_offscreen = _OFFSCREEN_MAX_DISPLAY + 10
        for i in range(5, 5 + total_offscreen):
            ref_map[f"@e{i}"] = {"role": "link", "name": f"o{i}", "tag": "a", "text": f"o{i}", "inViewport": False}

        lines = format_refs_for_llm(ref_map)

        # 应有省略计数提示
        omitted_found = any("more offscreen elements below viewport" in line for line in lines)
        assert omitted_found, f"应提示省略的offscreen元素数(超出{_OFFSCREEN_MAX_DISPLAY}个)"
        # offscreen展示的元素数不超过_OFFSCREEN_MAX_DISPLAY
        offscreen_ref_lines = [l for l in lines if "[offscreen]" in l and "---" not in l and "..." not in l]
        assert len(offscreen_ref_lines) <= _OFFSCREEN_MAX_DISPLAY

    def test_all_visible_no_offscreen_section(self):
        """全部可见元素时不显示offscreen分隔符"""
        ref_map = {
            "@e0": {"role": "button", "name": "b1", "tag": "button", "text": "b1", "inViewport": True},
            "@e1": {"role": "button", "name": "b2", "tag": "button", "text": "b2", "inViewport": True},
        }

        lines = format_refs_for_llm(ref_map)

        assert len(lines) == 2
        separator_found = any("--- offscreen" in line for line in lines)
        assert not separator_found, "无offscreen元素时不应显示分隔符"

    def test_all_offscreen_only_offscreen_section(self):
        """全部offscreen元素时仍正确分区展示"""
        ref_map = {
            "@e0": {"role": "link", "name": "o1", "tag": "a", "text": "o1", "inViewport": False},
        }

        lines = format_refs_for_llm(ref_map)

        # 无可见元素,直接是offscreen区
        separator_found = any("--- offscreen elements" in line for line in lines)
        assert separator_found, "应有offscreen分隔符"
        assert any("[offscreen]" in line and "@e0" in line for line in lines)


class TestMatchA11yNode:
    """_match_a11y_node: accessibility节点模糊匹配"""

    def test_exact_match(self):
        """文本精确匹配"""
        nodes = [{"role": "button", "name": "登录", "value": ""}]

        result = _match_a11y_node("button", "登录", nodes)

        assert result is not None
        assert result["role"] == "button"

    def test_partial_match_contains(self):
        """name包含text时匹配"""
        nodes = [{"role": "button", "name": "点击登录按钮", "value": ""}]

        result = _match_a11y_node("button", "登录", nodes)

        assert result is not None

    def test_partial_match_within(self):
        """text包含name时匹配"""
        nodes = [{"role": "link", "name": "首", "value": ""}]

        result = _match_a11y_node("a", "首页", nodes)

        assert result is not None

    def test_case_insensitive_match(self):
        """大小写不敏感匹配"""
        nodes = [{"role": "button", "name": "Submit", "value": ""}]

        result = _match_a11y_node("button", "SUBMIT", nodes)

        assert result is not None

    def test_no_match_returns_none(self):
        """无匹配返回None"""
        nodes = [{"role": "button", "name": "other", "value": ""}]

        result = _match_a11y_node("button", "登录", nodes)

        assert result is None

    def test_empty_text_returns_none(self):
        """空text返回None"""
        nodes = [{"role": "button", "name": "登录", "value": ""}]

        result = _match_a11y_node("button", "", nodes)

        assert result is None


# ==================== PlaywrightBrowser ref集成测试 ====================


class TestPlaywrightBrowserRefLifecycle:
    """PlaywrightBrowser的ref_map生命周期管理"""

    def _create_browser(self) -> PlaywrightBrowser:
        """创建PlaywrightBrowser实例(仅用于ref逻辑测试,不连接真实浏览器)"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        # mock page/browser避免触发真实CDP连接
        browser.page = MagicMock()
        browser.page.url = "https://example.com/page"
        browser.page.query_selector = AsyncMock(return_value=None)
        browser.page.evaluate = AsyncMock(return_value=[])
        browser.page.accessibility = MagicMock()
        browser.page.accessibility.snapshot = AsyncMock(return_value=None)
        browser.page.keyboard = MagicMock()
        browser.page.keyboard.press = AsyncMock()
        browser.page.keyboard.type = AsyncMock()
        browser.page.mouse = MagicMock()
        browser.page.mouse.click = AsyncMock()
        browser.browser = MagicMock()
        return browser

    @pytest.mark.asyncio
    async def test_build_ref_map_success(self):
        """_build_ref_map成功构建并存储到self._ref_map"""
        browser = self._create_browser()
        elements = [
            {
                "index": 0, "tag": "button", "text": "OK",
                "selector": '[data-manus-id="manus-element-0"]',
                "semanticAttrs": {"role": "button"}, "inViewport": True,
            },
        ]
        browser.page.accessibility.snapshot = AsyncMock(return_value=None)

        await browser._build_ref_map(elements)

        assert "@e0" in browser._ref_map
        assert browser._ref_map["@e0"]["selector"] == '[data-manus-id="manus-element-0"]'

    @pytest.mark.asyncio
    async def test_build_ref_map_failure_degrades_to_empty(self):
        """_build_ref_map异常时降级为空dict,不影响主流程"""
        browser = self._create_browser()
        browser.page.accessibility.snapshot = AsyncMock(
            side_effect=RuntimeError("page detached")
        )
        # 预填充旧数据,验证异常时是否清空
        browser._ref_map = {"@e0": {"selector": "old"}}

        await browser._build_ref_map([])

        assert browser._ref_map == {}

    def test_format_ref_map_for_llm_delegates(self):
        """_format_ref_map_for_llm正确代理到format_refs_for_llm"""
        browser = self._create_browser()
        browser._ref_map = {
            "@e0": {
                "role": "button", "name": "提交",
                "tag": "button", "text": "提交",
                "inViewport": True,
            },
        }

        lines = browser._format_ref_map_for_llm()

        assert len(lines) == 1
        assert "[@e0]" in lines[0]

    @pytest.mark.asyncio
    async def test_navigate_clears_ref_map_before_load(self):
        """_navigate_impl在导航前清空ref_map,避免旧页面引用残留"""
        browser = self._create_browser()
        browser._ref_map = {"@e0": {"selector": "old-page-sel"}}
        # mock导航流程
        browser._ensure_page = AsyncMock()
        browser.page.goto = AsyncMock()
        browser._wait_dom_stable = AsyncMock()
        browser._wait_for_loading_disappear = AsyncMock()
        browser._wait_for_content_ready = AsyncMock()
        browser._auto_dismiss_blocking_elements = AsyncMock(return_value=[])
        browser._extract_interactive_elements = AsyncMock(return_value=[])
        browser.page.evaluate = AsyncMock(return_value={"url": "https://new.com", "title": "New"})
        browser.page.interactive_elements_cache = []

        await browser._navigate_impl("https://new.com")

        # 导航后ref_map应被重建(先清空再由_extract_interactive_elements重建)
        # 由于_extract_interactive_elements是mock,不会重建,因此应为空dict
        assert browser._ref_map == {}


class TestFormatElementsVisibilityPriority:
    """PlaywrightBrowser._format_elements可见性优先排序测试(会话1146286e优化)"""

    def _create_browser(self) -> PlaywrightBrowser:
        """创建PlaywrightBrowser实例(仅用于格式化逻辑测试)"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser.page = MagicMock()
        return browser

    @pytest.mark.asyncio
    async def test_visible_elements_before_offscreen(self):
        """可见元素排在offscreen元素之前"""
        browser = self._create_browser()
        elements = [
            {"index": 0, "tag": "a", "text": "offscreen-link", "inViewport": False},
            {"index": 1, "tag": "button", "text": "visible-btn", "inViewport": True},
            {"index": 2, "tag": "a", "text": "offscreen-link2", "inViewport": False},
            {"index": 3, "tag": "button", "text": "visible-btn2", "inViewport": True},
        ]

        lines = await browser._format_elements(elements)

        # 可见元素(index=1,3)应在offscreen元素(index=0,2)之前
        idx1_pos = next(i for i, l in enumerate(lines) if ": <button>visible-btn" in l)
        idx3_pos = next(i for i, l in enumerate(lines) if ": <button>visible-btn2" in l)
        idx0_pos = next(i for i, l in enumerate(lines) if ": <a>offscreen-link</a>" in l)
        assert idx1_pos < idx0_pos, "可见元素应在offscreen元素之前"
        assert idx3_pos < idx0_pos, "可见元素应在offscreen元素之前"

    @pytest.mark.asyncio
    async def test_offscreen_section_separator(self):
        """offscreen元素区有分区分隔符"""
        browser = self._create_browser()
        elements = [
            {"index": 0, "tag": "button", "text": "v1", "inViewport": True},
            {"index": 1, "tag": "a", "text": "o1", "inViewport": False},
        ]

        lines = await browser._format_elements(elements)

        separator_found = any("--- offscreen elements" in line for line in lines)
        assert separator_found, "offscreen区应有分隔符"

    @pytest.mark.asyncio
    async def test_offscreen_truncation_with_count(self):
        """offscreen元素超过15个时截断并显示计数"""
        browser = self._create_browser()
        elements = [
            {"index": 0, "tag": "button", "text": "visible", "inViewport": True},
        ]
        # 添加20个offscreen元素(超过_OFFSCREEN_MAX=15)
        for i in range(1, 21):
            elements.append({"index": i, "tag": "a", "text": f"off-{i}", "inViewport": False})

        lines = await browser._format_elements(elements)

        omitted_found = any("more offscreen elements below viewport" in line for line in lines)
        assert omitted_found, "应提示省略的offscreen元素数"

    @pytest.mark.asyncio
    async def test_all_visible_no_separator(self):
        """全部可见元素时不显示offscreen分隔符"""
        browser = self._create_browser()
        elements = [
            {"index": 0, "tag": "button", "text": "b1", "inViewport": True},
            {"index": 1, "tag": "button", "text": "b2", "inViewport": True},
        ]

        lines = await browser._format_elements(elements)

        assert len(lines) == 2
        assert not any("--- offscreen" in line for line in lines)

    @pytest.mark.asyncio
    async def test_format_elements_state_marked_priority(self):
        """_format_elements状态标记元素置顶,确保截断时[checked]/[selected]不丢失

        会话b143f0be根因: @e372-374的[checked]标记因高索引排在visible列表尾部,
        被_truncate_field_to_fit尾丢弃→LLM无法判断radio选中态。
        修复: visible元素按状态标记优先级排序(checked/selected置顶),仅改变展示
        顺序不影响@eN ref编号。
        """
        browser = self._create_browser()
        # 构造元素: 低index无状态, 高index有状态(模拟radio button场景)
        elements = [
            {"index": 100, "tag": "a", "text": "link0", "inViewport": True},
            {"index": 101, "tag": "button", "text": "btn1", "inViewport": True},
            {"index": 372, "tag": "span", "text": "Left", "inViewport": True},
            {"index": 373, "tag": "span", "text": "Right", "inViewport": True,
             "state": {"checked": True}},
            {"index": 374, "tag": "span", "text": "Top", "inViewport": True},
            {"index": 375, "tag": "option", "text": "Opt", "inViewport": True,
             "state": {"selected": True}},
        ]

        lines = await browser._format_elements(elements)

        # 状态标记元素(373 Right [checked], 375 Opt [selected])应在非状态元素之前
        right_idx = next(i for i, l in enumerate(lines) if "373" in l)
        opt_idx = next(i for i, l in enumerate(lines) if "375" in l)
        link_idx = next(i for i, l in enumerate(lines) if "100" in l)
        btn_idx = next(i for i, l in enumerate(lines) if "101" in l)
        # 状态标记元素排在非状态元素前面
        assert right_idx < link_idx, "checked元素应排在普通元素之前"
        assert opt_idx < link_idx, "selected元素应排在普通元素之前"
        assert right_idx < btn_idx
        assert opt_idx < btn_idx
        # 状态标记内容仍正确显示
        assert "[checked]" in lines[right_idx]
        assert "[selected]" in lines[opt_idx]

    @pytest.mark.asyncio
    async def test_visible_elements_safety_ceiling(self):
        """_format_elements可见元素安全上限兜底(_VISIBLE_ELEMENTS_SAFETY_CEILING=2000)

        架构原则(会话9b0bf463根因修复): 源头不再硬限流,由memory.py动态截断统一控制。
        安全上限2000仅防极端页面(10000+元素)JSON爆炸,正常页面完整展示。
        超出安全上限时添加计数提示引导text_locator定位。
        """
        browser = self._create_browser()
        # 构造安全上限+30个可见元素(共2030个,超出安全上限)
        elements = [
            {"index": i, "tag": "a", "text": f"link{i}", "inViewport": True}
            for i in range(_VISIBLE_ELEMENTS_SAFETY_CEILING + 30)
        ]

        lines = await browser._format_elements(elements)

        # 应有限流提示(仅安全上限超出时)
        omitted_hint = next((l for l in lines if "more visible elements below viewport" in l), None)
        assert omitted_hint is not None, "超出安全上限应显示省略计数提示"
        # 提示应引导LLM使用text_locator
        assert "text_locator" in omitted_hint

        # 可见元素行数 = 安全上限 + 1条提示(无offscreen)
        element_lines = [l for l in lines if not l.startswith("---") and not l.startswith("...")]
        assert len(element_lines) == _VISIBLE_ELEMENTS_SAFETY_CEILING, "应恰好展示安全上限个元素"

    @pytest.mark.asyncio
    async def test_normal_page_no_truncation(self):
        """正常页面(295元素,模拟Element Plus文档页)不触发限流提示

        核心回归测试(会话9b0bf463): 旧版_MAX_VISIBLE_ELEMENTS=80截断295元素文档页,
        导致"215 more visible elements omitted",LLM看不全关键表格内容。
        移除源头硬限流后,295元素页面应完整展示,无任何限流提示。
        """
        browser = self._create_browser()
        # 模拟Element Plus Form表单文档页: 295个可见元素
        elements = [
            {"index": i, "tag": "a", "text": f"elem{i}", "inViewport": True}
            for i in range(295)
        ]

        lines = await browser._format_elements(elements)

        # 不应有任何限流提示
        omitted_hint = next((l for l in lines if "more visible elements below viewport" in l), None)
        assert omitted_hint is None, "295元素页面(低于安全上限)不应触发限流提示"
        # 应完整展示全部295个元素
        element_lines = [l for l in lines if not l.startswith("---") and not l.startswith("...")]
        assert len(element_lines) == 295, "应完整展示全部295个元素"

    @pytest.mark.asyncio
    async def test_safety_ceiling_preserves_state_marked_elements(self):
        """安全上限限流时状态标记元素(checked/selected)始终保留(优先级P0置顶)

        根因: 限流若不排序,高索引状态标记元素会被丢弃。
        修复: 三级优先级排序(状态标记>对话框>普通),限流仅丢弃P2普通元素。
        """
        browser = self._create_browser()
        # 构造场景: 安全上限个普通元素(填满上限) + 高索引状态标记元素
        elements = [
            {"index": i, "tag": "a", "text": f"link{i}", "inViewport": True}
            for i in range(_VISIBLE_ELEMENTS_SAFETY_CEILING)
        ]
        # 高索引状态标记元素(超出安全上限,但优先级P0应保留)
        elements.append({"index": 9999, "tag": "span", "text": "Right", "inViewport": True,
                         "state": {"checked": True}})
        elements.append({"index": 9998, "tag": "option", "text": "Opt", "inViewport": True,
                         "state": {"selected": True}})

        lines = await browser._format_elements(elements)

        # 状态标记元素(9999, 9998)应保留在结果中(P0优先级置顶)
        right_line = next((l for l in lines if "9999" in l and "Right" in l), None)
        opt_line = next((l for l in lines if "9998" in l and "Opt" in l), None)
        assert right_line is not None, "[checked]元素应因P0优先级保留"
        assert opt_line is not None, "[selected]元素应因P0优先级保留"
        assert "[checked]" in right_line
        assert "[selected]" in opt_line

    @pytest.mark.asyncio
    async def test_safety_ceiling_dialog_priority(self):
        """安全上限限流时对话框内元素(P1)优先于普通元素(P2)

        弹窗是当前交互焦点,背景元素可丢弃。三级优先级: 状态标记(0)>对话框(1)>普通(2)。
        """
        browser = self._create_browser()
        # 构造场景: 安全上限个普通元素(填满上限) + 对话框内元素(超出上限,但P1应保留)
        elements = [
            {"index": i, "tag": "a", "text": f"bg{i}", "inViewport": True}
            for i in range(_VISIBLE_ELEMENTS_SAFETY_CEILING)
        ]
        elements.append({"index": 5000, "tag": "button", "text": "dialog-confirm",
                         "inViewport": True, "inDialog": True})

        lines = await browser._format_elements(elements)

        # 对话框元素(5000)应保留
        dialog_line = next((l for l in lines if "5000" in l and "dialog-confirm" in l), None)
        assert dialog_line is not None, "对话框元素应因P1优先级保留"
        assert "[dialog]" in dialog_line

    @pytest.mark.asyncio
    async def test_format_single_element_marks(self):
        """_format_single_element正确标记offscreen/shadow/dialog"""
        # offscreen标记
        el_off = {"index": 0, "tag": "a", "text": "link", "inViewport": False}
        assert "[offscreen]" in PlaywrightBrowser._format_single_element(el_off)
        # shadow标记
        el_shadow = {"index": 1, "tag": "button", "text": "btn", "inViewport": True, "inShadowDOM": True}
        assert "[shadow]" in PlaywrightBrowser._format_single_element(el_shadow)
        # dialog标记
        el_dialog = {"index": 2, "tag": "input", "text": "inp", "inViewport": True, "inDialog": True}
        assert "[dialog]" in PlaywrightBrowser._format_single_element(el_dialog)
        # 无标记
        el_plain = {"index": 3, "tag": "button", "text": "plain", "inViewport": True}
        line = PlaywrightBrowser._format_single_element(el_plain)
        assert "[offscreen]" not in line
        assert "[shadow]" not in line
        assert "[dialog]" not in line

    def test_format_single_element_state_marks(self):
        """_format_single_element正确标记表单元素状态checked/selected/disabled"""
        # checked状态(radio/checkbox选中)
        el_checked = {"index": 0, "tag": "span", "text": "Left", "inViewport": True,
                      "state": {"checked": True}}
        assert "[checked]" in PlaywrightBrowser._format_single_element(el_checked)
        # selected状态(option选中)
        el_selected = {"index": 1, "tag": "option", "text": "A", "inViewport": True,
                       "state": {"selected": True}}
        assert "[selected]" in PlaywrightBrowser._format_single_element(el_selected)
        # disabled状态
        el_disabled = {"index": 2, "tag": "button", "text": "btn", "inViewport": True,
                       "state": {"disabled": True}}
        assert "[disabled]" in PlaywrightBrowser._format_single_element(el_disabled)
        # 多状态叠加
        el_multi = {"index": 3, "tag": "span", "text": "X", "inViewport": True,
                    "state": {"checked": True, "disabled": True}}
        line = PlaywrightBrowser._format_single_element(el_multi)
        assert "[checked]" in line
        assert "[disabled]" in line
        # 无state字段时不报错且无状态标记
        el_no_state = {"index": 4, "tag": "a", "text": "link", "inViewport": True}
        line = PlaywrightBrowser._format_single_element(el_no_state)
        assert "[checked]" not in line
        assert "[selected]" not in line
        assert "[disabled]" not in line

    def test_interactive_elements_func_radio_label_state_climbing(self):
        """GET_INTERACTIVE_ELEMENTS_FUNC包含radio/checkbox label状态攀升逻辑

        会话ab17bf13根因: el-radio-button的input视觉隐藏被过滤,捕获的是内部span,
        但span不携带[checked]状态→LLM无法判断radio选中态。修复: 向上攀升到label
        查询关联input的checked态。本测试验证JS函数包含该逻辑。
        """
        from app.infrastructure.external.browser.playwright_browser_fun import (
            GET_INTERACTIVE_ELEMENTS_FUNC,
        )
        # 必须覆盖element-plus与ant-design的radio/checkbox label容器
        assert "el-radio-button" in GET_INTERACTIVE_ELEMENTS_FUNC
        assert "el-radio" in GET_INTERACTIVE_ELEMENTS_FUNC
        assert "el-checkbox" in GET_INTERACTIVE_ELEMENTS_FUNC
        assert "ant-radio-wrapper" in GET_INTERACTIVE_ELEMENTS_FUNC
        # 必须通过closest攀升到label容器查询关联input
        assert ".closest(" in GET_INTERACTIVE_ELEMENTS_FUNC
        assert 'input[type="radio"]' in GET_INTERACTIVE_ELEMENTS_FUNC

    def test_extract_label_from_ref_text(self):
        """_extract_label_from_ref_text从格式化文本提取标签用于input ref回退定位"""
        # [Label:xxx]格式(input元素最常见)
        assert PlaywrightBrowser._extract_label_from_ref_text("[Label:Name]") == "Name"
        assert PlaywrightBrowser._extract_label_from_ref_text("[Label:Activity zone]") == "Activity zone"
        # [Label:xxx] [Value:yyy]组合(带当前值的input)
        assert PlaywrightBrowser._extract_label_from_ref_text("[Label:Name] [Value:Tom]") == "Name"
        # [Placeholder:xxx]格式(无label的input)
        assert PlaywrightBrowser._extract_label_from_ref_text("[Placeholder:Pick a date]") == "Pick a date"
        # 原始文本(span/a等非input元素,直接返回)
        assert PlaywrightBrowser._extract_label_from_ref_text("Left") == "Left"
        assert PlaywrightBrowser._extract_label_from_ref_text("Form 表单组件") == "Form 表单组件"
        # 占位符文本过滤(不以[]包裹的不过滤,以[]包裹的占位符返回空)
        assert PlaywrightBrowser._extract_label_from_ref_text("[No text]") == ""
        assert PlaywrightBrowser._extract_label_from_ref_text("[text]") == ""
        # 空字符串
        assert PlaywrightBrowser._extract_label_from_ref_text("") == ""


class TestDomTreeViewportPriority:
    """_dom_tree_to_text视口优先输出测试(会话437cbc75根因修复)

    视口内(non-offscreen)子节点先输出,视口外(offscreen)子节点后输出。
    截断(从头保留)时视口内容优先保留,视口外内容优先丢弃。
    契合"快照临时存在、滚动后内容交由LLM理解"原则。
    """

    def test_viewport_children_before_offscreen(self):
        """视口内子节点在视口外子节点之前输出"""
        # 模拟DOM树: 根节点有两个子节点,offscreen在前(DOM顺序),viewport在后
        dom_tree = {
            "tag": "body",
            "children": [
                {"tag": "nav", "text": "页脚导航", "offscreen": True},
                {"tag": "table", "text": "表格数据", "offscreen": False},
            ],
        }
        text = PlaywrightBrowser._dom_tree_to_text(dom_tree)
        # 视口内(table)应在视口外(nav)之前
        table_pos = text.find("表格数据")
        nav_pos = text.find("页脚导航")
        assert table_pos < nav_pos, \
            f"视口内容(表格)应排在视口外内容(导航)之前, got table@{table_pos} nav@{nav_pos}"

    def test_offscreen_content_truncated_first(self):
        """截断时视口外内容优先丢弃,视口内容保留

        模拟滚动后场景: 视口内有表格数据(LLM需要),视口外有大量导航文本。
        content截断从头保留→视口内容(前部)保留,视口外内容(后部)丢弃。
        """
        # 视口内: 关键表格数据
        viewport_text = "商品名称: 测试商品 条码: 6903431104863"
        # 视口外: 大量导航文本(模拟侧边栏菜单)
        offscreen_text = "菜单项" * 500  # 2000字符,足够撑满截断阈值

        dom_tree = {
            "tag": "body",
            "children": [
                {"tag": "aside", "text": offscreen_text, "offscreen": True},
                {"tag": "main", "text": viewport_text, "offscreen": False},
            ],
        }
        full_text = PlaywrightBrowser._dom_tree_to_text(dom_tree)
        # 模拟截断: 只保留前500字符(模拟content预算不足)
        truncated = full_text[:500]
        # 视口内容(表格数据)应在前500字符内(被保留)
        assert viewport_text in truncated, \
            "截断后视口内容(表格数据)应被保留"
        # 视口外内容(菜单项)不应在前500字符的起始位置(被推到后部)
        assert truncated.find(viewport_text) < truncated.find("菜单项"), \
            "视口内容应在视口外内容之前"

    def test_mixed_children_viewport_first(self):
        """混合子节点: 视口内先输出,视口外后输出,各自保持相对顺序"""
        dom_tree = {
            "tag": "div",
            "children": [
                {"tag": "a", "text": "offscreen-1", "offscreen": True},
                {"tag": "b", "text": "viewport-1", "offscreen": False},
                {"tag": "c", "text": "offscreen-2", "offscreen": True},
                {"tag": "d", "text": "viewport-2", "offscreen": False},
            ],
        }
        text = PlaywrightBrowser._dom_tree_to_text(dom_tree)
        # 视口内(viewport-1, viewport-2)应在视口外(offscreen-1, offscreen-2)之前
        vp1 = text.find("viewport-1")
        vp2 = text.find("viewport-2")
        os1 = text.find("offscreen-1")
        os2 = text.find("offscreen-2")
        assert vp1 < os1 and vp1 < os2, "视口内容应全部在视口外内容之前"
        assert vp2 < os1 and vp2 < os2, "视口内容应全部在视口外内容之前"
        # 视口内保持相对顺序
        assert vp1 < vp2, "视口内子节点应保持相对顺序"
        # 视口外保持相对顺序
        assert os1 < os2, "视口外子节点应保持相对顺序"

    def test_no_offscreen_children_unchanged(self):
        """无offscreen子节点时,输出顺序与DOM顺序一致(向后兼容)"""
        dom_tree = {
            "tag": "div",
            "children": [
                {"tag": "a", "text": "first"},
                {"tag": "b", "text": "second"},
                {"tag": "c", "text": "third"},
            ],
        }
        text = PlaywrightBrowser._dom_tree_to_text(dom_tree)
        # 无offscreen时,顺序应与DOM顺序一致
        assert text.find("first") < text.find("second") < text.find("third"), \
            "无offscreen子节点时应保持DOM顺序"


class TestPlaywrightBrowserClickRef:
    """PlaywrightBrowser._click_impl的ref分支测试"""

    def _create_browser(self) -> PlaywrightBrowser:
        """创建mock PlaywrightBrowser实例"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser.page = MagicMock()
        browser.page.url = "https://example.com/page"
        browser.page.query_selector = AsyncMock(return_value=None)
        browser.page.evaluate = AsyncMock(return_value={"interactable": True})
        browser.page.accessibility = MagicMock()
        browser.page.accessibility.snapshot = AsyncMock(return_value=None)
        browser.page.keyboard = MagicMock()
        browser.page.keyboard.press = AsyncMock()
        browser.page.mouse = MagicMock()
        browser.page.mouse.click = AsyncMock()
        browser.browser = MagicMock()
        # mock _ensure_page和_post_action_sync避免触发真实浏览器初始化
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        return browser

    @pytest.mark.asyncio
    async def test_click_with_ref_success(self):
        """通过ref成功点击元素"""
        browser = self._create_browser()
        element = MagicMock()
        element.click = AsyncMock()
        browser.page.query_selector = AsyncMock(return_value=element)
        browser._ref_map = {"@e3": {"selector": '[data-manus-id="manus-element-3"]'}}
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=True)

        result = await browser._click_impl(ref="@e3")

        assert result.success is True
        assert result.data["snapshot_version"] == browser._snapshot_version
        browser._click_with_retry.assert_called_once_with(element, target_description="@e3")

    @pytest.mark.asyncio
    async def test_click_with_ref_refresh_retry_success(self):
        """ref首次解析失败,刷新交互元素缓存后重试成功"""
        browser = self._create_browser()
        element = MagicMock()
        # 第一次query_selector返回None(元素未加载),第二次返回element(刷新后找到)
        browser.page.query_selector = AsyncMock(
            side_effect=[None, element]
        )
        browser._ref_map = {"@e1": {"selector": '[data-manus-id="manus-element-1"]'}}
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=True)
        # mock _extract_interactive_elements重建ref_map
        async def mock_extract_impl():
            browser._snapshot_version += 1
            browser._ref_map = {"@e1": {"selector": '[data-manus-id="manus-element-1"]'}}
            return []
        browser._extract_interactive_elements = AsyncMock(side_effect=mock_extract_impl)

        result = await browser._click_impl(ref="@e1")

        assert result.success is True
        assert browser._extract_interactive_elements.call_count >= 1

    @pytest.mark.asyncio
    async def test_click_with_ref_completely_invalid(self):
        """ref完全无效(刷新后仍找不到),返回失败"""
        browser = self._create_browser()
        browser.page.query_selector = AsyncMock(return_value=None)
        browser._ref_map = {"@e99": {"selector": '[data-manus-id="manus-element-99"]'}}

        async def mock_extract_impl():
            browser._snapshot_version += 1
            return []
        browser._extract_interactive_elements = AsyncMock(side_effect=mock_extract_impl)

        result = await browser._click_impl(ref="@e99")

        assert result.success is False
        assert "ref[@e99]" in result.message
        assert "不存在或已失效" in result.message

    @pytest.mark.asyncio
    async def test_click_ref_priority_over_text_and_index(self):
        """ref优先级高于text和index(同时传参时使用ref)"""
        browser = self._create_browser()
        element = MagicMock()
        browser.page.query_selector = AsyncMock(return_value=element)
        browser._ref_map = {"@e0": {"selector": '[data-manus-id="manus-element-0"]'}}
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=True)
        # 如果走了text分支,会调用_locate_element_by_text
        browser._locate_element_by_text = AsyncMock(return_value=None)

        await browser._click_impl(ref="@e0", text="按钮", index=5)

        # 应走ref分支,不调用text定位
        browser._locate_element_by_text.assert_not_called()
        browser._click_with_retry.assert_called_once_with(element, target_description="@e0")

    @pytest.mark.asyncio
    async def test_click_with_ref_disabled_element(self):
        """ref指向的元素被禁用时返回友好错误"""
        browser = self._create_browser()
        element = MagicMock()
        browser.page.query_selector = AsyncMock(return_value=element)
        browser._ref_map = {"@e2": {"selector": '[data-manus-id="manus-element-2"]'}}
        browser._check_element_interactable = AsyncMock(
            return_value={"interactable": False, "reason": "disabled"}
        )

        result = await browser._click_impl(ref="@e2")

        assert result.success is False
        assert "禁用" in result.message

    @pytest.mark.asyncio
    async def test_click_with_ref_whitespace_stripped(self):
        """ref参数前后空白被strip处理"""
        browser = self._create_browser()
        element = MagicMock()
        browser.page.query_selector = AsyncMock(return_value=element)
        browser._ref_map = {"@e0": {"selector": '[data-manus-id="manus-element-0"]'}}
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        browser._click_with_retry = AsyncMock(return_value=True)

        result = await browser._click_impl(ref="  @e0  ")

        assert result.success is True


class TestPlaywrightBrowserInputRef:
    """PlaywrightBrowser._input_impl的ref/text_locator分支测试"""

    def _create_browser(self) -> PlaywrightBrowser:
        """创建mock PlaywrightBrowser实例"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser.page = MagicMock()
        browser.page.url = "https://example.com/page"
        browser.page.query_selector = AsyncMock(return_value=None)
        browser.page.evaluate = AsyncMock(return_value={"interactable": True})
        browser.page.accessibility = MagicMock()
        browser.page.accessibility.snapshot = AsyncMock(return_value=None)
        browser.page.keyboard = MagicMock()
        browser.page.keyboard.press = AsyncMock()
        browser.page.keyboard.type = AsyncMock()
        browser.page.mouse = MagicMock()
        browser.page.mouse.click = AsyncMock()
        browser.browser = MagicMock()
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        return browser

    @pytest.mark.asyncio
    async def test_input_with_ref_success(self):
        """通过ref成功定位输入框并输入文本"""
        browser = self._create_browser()
        element = MagicMock()
        element.click = AsyncMock()
        browser.page.query_selector = AsyncMock(return_value=element)
        browser._ref_map = {"@e5": {"selector": '[data-manus-id="manus-element-5"]'}}
        # mock _input_text_to_element避免触发三级输入策略细节
        browser._input_text_to_element = AsyncMock()

        result = await browser._input_impl(text="hello", press_enter=True, ref="@e5")

        assert result.success is True
        browser._input_text_to_element.assert_called_once_with(element, "hello")
        browser.page.keyboard.press.assert_called_once_with("Enter")

    @pytest.mark.asyncio
    async def test_input_with_text_locator_success(self):
        """通过text_locator定位输入框(无ref时)"""
        browser = self._create_browser()
        element = MagicMock()
        browser._locate_element_by_text = AsyncMock(return_value=element)
        browser._input_text_to_element = AsyncMock()

        result = await browser._input_impl(
            text="world", press_enter=False, text_locator="用户名",
        )

        assert result.success is True
        browser._locate_element_by_text.assert_called_once_with("用户名")
        browser._input_text_to_element.assert_called_once_with(element, "world")

    @pytest.mark.asyncio
    async def test_input_ref_priority_over_text_locator(self):
        """ref优先级高于text_locator(同时传参时使用ref)"""
        browser = self._create_browser()
        element = MagicMock()
        browser.page.query_selector = AsyncMock(return_value=element)
        browser._ref_map = {"@e0": {"selector": '[data-manus-id="manus-element-0"]'}}
        browser._input_text_to_element = AsyncMock()
        browser._locate_element_by_text = AsyncMock(return_value=None)

        await browser._input_impl(
            text="data", press_enter=False, ref="@e0", text_locator="label",
        )

        browser._locate_element_by_text.assert_not_called()
        browser._input_text_to_element.assert_called_once_with(element, "data")

    @pytest.mark.asyncio
    async def test_input_ref_refresh_retry_success(self):
        """input ref首次解析失败,刷新后重试成功"""
        browser = self._create_browser()
        element = MagicMock()
        browser.page.query_selector = AsyncMock(side_effect=[None, element])
        browser._ref_map = {"@e1": {"selector": '[data-manus-id="manus-element-1"]'}}
        browser._input_text_to_element = AsyncMock()

        async def mock_extract_impl():
            browser._snapshot_version += 1
            browser._ref_map = {"@e1": {"selector": '[data-manus-id="manus-element-1"]'}}
            return []
        browser._extract_interactive_elements = AsyncMock(side_effect=mock_extract_impl)

        result = await browser._input_impl(text="retry", press_enter=False, ref="@e1")

        assert result.success is True

    @pytest.mark.asyncio
    async def test_input_ref_completely_invalid(self):
        """input ref完全无效,返回失败"""
        browser = self._create_browser()
        browser.page.query_selector = AsyncMock(return_value=None)
        browser._ref_map = {"@e88": {"selector": '[data-manus-id="manus-element-88"]'}}

        async def mock_extract_impl():
            browser._snapshot_version += 1
            return []
        browser._extract_interactive_elements = AsyncMock(side_effect=mock_extract_impl)

        result = await browser._input_impl(text="x", press_enter=False, ref="@e88")

        assert result.success is False
        assert "ref[@e88]" in result.message

    @pytest.mark.asyncio
    async def test_input_no_locator_params_returns_error(self):
        """无任何定位参数(ref/text_locator/index/coordinate)时返回错误"""
        browser = self._create_browser()

        result = await browser._input_impl(text="x", press_enter=False)

        assert result.success is False
        assert "请提供" in result.message


# ==================== BrowserTool工具层ref参数测试 ====================


class TestBrowserToolRefParameter:
    """BrowserTool工具层ref/text_locator参数透传测试"""

    def _create_tool(self):
        """创建带mock browser的BrowserTool"""
        mock_browser = MagicMock()
        return BrowserTool(browser=mock_browser), mock_browser

    @pytest.mark.asyncio
    async def test_browser_click_passes_ref(self):
        """browser_click正确传递ref参数给browser.click"""
        tool, mock_browser = self._create_tool()
        mock_browser.click = AsyncMock(
            return_value=ToolResult(success=True, message="OK")
        )

        await tool.browser_click(ref="@e1", text="按钮", index=0)

        mock_browser.click.assert_called_once_with("@e1", "按钮", 0, None, None)

    @pytest.mark.asyncio
    async def test_browser_click_passes_only_ref(self):
        """browser_click仅传ref时其他参数为None"""
        tool, mock_browser = self._create_tool()
        mock_browser.click = AsyncMock(
            return_value=ToolResult(success=True, message="OK")
        )

        await tool.browser_click(ref="@e5")

        mock_browser.click.assert_called_once_with("@e5", None, None, None, None)

    @pytest.mark.asyncio
    async def test_browser_input_passes_ref_and_text_locator(self):
        """browser_input正确传递ref和text_locator参数"""
        tool, mock_browser = self._create_tool()
        mock_browser.input = AsyncMock(
            return_value=ToolResult(success=True, message="OK")
        )

        await tool.browser_input(
            text="hello", press_enter=True, ref="@e2", text_locator="搜索",
        )

        mock_browser.input.assert_called_once_with(
            "hello", True, "@e2", "搜索", None, None, None,
        )

    @pytest.mark.asyncio
    async def test_browser_input_backward_compatible_without_ref(self):
        """browser_input向后兼容:不传ref/text_locator时仍能正常调用"""
        tool, mock_browser = self._create_tool()
        mock_browser.input = AsyncMock(
            return_value=ToolResult(success=True, message="OK")
        )

        await tool.browser_input(text="data", press_enter=False, index=3)

        mock_browser.input.assert_called_once_with(
            "data", False, None, None, 3, None, None,
        )

    @pytest.mark.asyncio
    async def test_browser_click_ref_overrides_text_on_timeout(self):
        """browser_click传递ref后,超时由browser层处理(不丢失ref)"""
        tool, mock_browser = self._create_tool()
        mock_browser.click = AsyncMock(side_effect=asyncio.TimeoutError())

        result = await tool.browser_click(ref="@e1")

        assert result.success is False
        assert "超时" in result.message
        mock_browser.click.assert_called_once_with("@e1", None, None, None, None)
