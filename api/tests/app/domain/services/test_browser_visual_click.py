#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_browser_visual_click.py
阶段4-视觉点击兜底单元测试
- visual_click模块: 截图缩放、LLM视觉定位、坐标解析、缩放校正、异常降级
- PlaywrightBrowser集成: _click_with_retry第六级兜底触发、target_description透传
"""
import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.tool_result import ToolResult
from app.infrastructure.external.browser.visual_click import (
    visual_click,
    _capture_scaled_screenshot,
    _visual_locate,
    _parse_coordinates,
    _VISUAL_SCREENSHOT_MAX_WIDTH,
    _INVALID_COORDINATES,
)
from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser


# ==================== _parse_coordinates 坐标解析测试 ====================


class TestParseCoordinates:
    """_parse_coordinates: 从LLM响应文本中解析坐标JSON"""

    def test_parse_valid_json(self):
        """标准JSON格式正确解析"""
        content = '{"x": 100, "y": 200}'

        x, y = _parse_coordinates(content)

        assert x == 100.0
        assert y == 200.0

    def test_parse_json_with_markdown_wrapper(self):
        """LLM返回```json```包裹的内容也能解析"""
        content = '```json\n{"x": 50, "y": 75}\n```'

        x, y = _parse_coordinates(content)

        assert x == 50.0
        assert y == 75.0

    def test_parse_json_with_extra_text(self):
        """JSON前后有额外文本也能提取"""
        content = '目标元素坐标为: {"x": 320, "y": 480} 请点击'

        x, y = _parse_coordinates(content)

        assert x == 320.0
        assert y == 480.0

    def test_parse_null_coordinates(self):
        """LLM返回null坐标表示找不到"""
        content = '{"x": null, "y": null}'

        x, y = _parse_coordinates(content)

        assert x is None
        assert y is None

    def test_parse_invalid_format_returns_invalid(self):
        """无效格式返回_INVALID_COORDINATES"""
        content = '找不到目标元素'

        x, y = _parse_coordinates(content)

        assert x is None
        assert y is None

    def test_parse_missing_y_field(self):
        """缺少y字段返回无效坐标"""
        content = '{"x": 100}'

        x, y = _parse_coordinates(content)

        assert x is None
        assert y is None

    def test_parse_string_numbers(self):
        """字符串数字也能转换为float"""
        content = '{"x": "100", "y": "200"}'

        x, y = _parse_coordinates(content)

        assert x == 100.0
        assert y == 200.0


# ==================== _capture_scaled_screenshot 截图测试 ====================


class TestCaptureScaledScreenshot:
    """_capture_scaled_screenshot: 截图与缩放处理"""

    @pytest.mark.asyncio
    async def test_screenshot_no_scaling(self):
        """小图不缩放,返回原始base64"""
        # 创建10x10的PNG图片字节(模拟)
        mock_page = MagicMock()
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        mock_page.screenshot = AsyncMock(return_value=fake_png)

        with patch("PIL.Image.open") as mock_open:
            mock_img = MagicMock()
            mock_img.width = 800
            mock_open.return_value = mock_img

            result, scale = await _capture_scaled_screenshot(mock_page)

        assert result is not None
        assert scale == 1.0

    @pytest.mark.asyncio
    async def test_screenshot_with_scaling(self):
        """大图缩放到最大宽度,返回缩放比例"""
        mock_page = MagicMock()
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        mock_page.screenshot = AsyncMock(return_value=fake_png)

        with patch("PIL.Image.open") as mock_open:
            mock_img = MagicMock()
            mock_img.width = 2560  # 超过最大宽度
            mock_img.resize = MagicMock(return_value=mock_img)
            mock_img.convert = MagicMock(return_value=mock_img)
            mock_img.save = MagicMock()
            mock_open.return_value = mock_img

            with patch("io.BytesIO") as mock_buf:
                mock_buf.return_value = MagicMock()
                with patch("base64.b64encode", return_value=b"encoded"):
                    result, scale = await _capture_scaled_screenshot(mock_page)

        assert result is not None
        assert scale == _VISUAL_SCREENSHOT_MAX_WIDTH / 2560

    @pytest.mark.asyncio
    async def test_screenshot_empty_bytes_returns_none(self):
        """空截图字节返回None"""
        mock_page = MagicMock()
        mock_page.screenshot = AsyncMock(return_value=b"")

        result, scale = await _capture_scaled_screenshot(mock_page)

        assert result is None
        assert scale == 1.0

    @pytest.mark.asyncio
    async def test_screenshot_exception_returns_none(self):
        """截图异常返回None(不影响主流程)"""
        mock_page = MagicMock()
        mock_page.screenshot = AsyncMock(side_effect=RuntimeError("page closed"))

        result, scale = await _capture_scaled_screenshot(mock_page)

        assert result is None
        assert scale == 1.0


# ==================== _visual_locate LLM视觉定位测试 ====================


class TestVisualLocate:
    """_visual_locate: 调用多模态LLM分析截图返回坐标"""

    @pytest.mark.asyncio
    async def test_llm_returns_valid_coordinates(self):
        """LLM返回有效坐标"""
        mock_llm = MagicMock()
        mock_llm.invoke = AsyncMock(return_value={"content": '{"x": 150, "y": 250}'})

        x, y = await _visual_locate(mock_llm, "base64data", "提交按钮")

        assert x == 150.0
        assert y == 250.0

    @pytest.mark.asyncio
    async def test_llm_returns_null_coordinates(self):
        """LLM返回null坐标(找不到元素)"""
        mock_llm = MagicMock()
        mock_llm.invoke = AsyncMock(return_value={"content": '{"x": null, "y": null}'})

        x, y = await _visual_locate(mock_llm, "base64data", "不存在的元素")

        assert x is None
        assert y is None

    @pytest.mark.asyncio
    async def test_llm_returns_string_content(self):
        """LLM返回字符串格式response(非dict)"""
        mock_llm = MagicMock()
        mock_llm.invoke = AsyncMock(return_value='{"x": 80, "y": 120}')

        x, y = await _visual_locate(mock_llm, "base64data", "按钮")

        assert x == 80.0
        assert y == 120.0

    @pytest.mark.asyncio
    async def test_llm_invoke_exception_returns_invalid(self):
        """LLM调用异常返回无效坐标(不传播异常)"""
        mock_llm = MagicMock()
        mock_llm.invoke = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        x, y = await _visual_locate(mock_llm, "base64data", "按钮")

        assert x is None
        assert y is None


# ==================== visual_click 集成测试 ====================


class TestVisualClick:
    """visual_click: 完整视觉点击流程"""

    @pytest.mark.asyncio
    async def test_visual_click_success(self):
        """完整的视觉点击流程成功"""
        mock_page = MagicMock()
        mock_page.mouse = MagicMock()
        mock_page.mouse.click = AsyncMock()
        mock_llm = MagicMock()
        mock_llm.invoke = AsyncMock(return_value={"content": '{"x": 100, "y": 200}'})

        # mock截图返回小图(不缩放)
        with patch("app.infrastructure.external.browser.visual_click._capture_scaled_screenshot") as mock_cap:
            mock_cap.return_value = ("base64data", 1.0)
            result = await visual_click(mock_page, mock_llm, "登录按钮")

        assert result is True
        mock_page.mouse.click.assert_called_once_with(100.0, 200.0)

    @pytest.mark.asyncio
    async def test_visual_click_with_scaling_correction(self):
        """缩放图坐标校正: LLM返回缩放图坐标,实际点击反算后坐标"""
        mock_page = MagicMock()
        mock_page.mouse = MagicMock()
        mock_page.mouse.click = AsyncMock()
        mock_llm = MagicMock()
        # LLM在缩放图(scale=0.5)上返回坐标(100, 100),实际页面坐标应为(200, 200)
        mock_llm.invoke = AsyncMock(return_value={"content": '{"x": 100, "y": 100}'})

        with patch("app.infrastructure.external.browser.visual_click._capture_scaled_screenshot") as mock_cap:
            mock_cap.return_value = ("base64data", 0.5)
            result = await visual_click(mock_page, mock_llm, "按钮")

        assert result is True
        # 实际点击坐标 = LLM坐标 / scale = 100/0.5 = 200
        mock_page.mouse.click.assert_called_once_with(200.0, 200.0)

    @pytest.mark.asyncio
    async def test_visual_click_llm_none_returns_false(self):
        """LLM未注入时返回False"""
        mock_page = MagicMock()

        result = await visual_click(mock_page, None, "按钮")

        assert result is False

    @pytest.mark.asyncio
    async def test_visual_click_empty_description_returns_false(self):
        """空目标描述返回False"""
        mock_page = MagicMock()
        mock_llm = MagicMock()

        result = await visual_click(mock_page, mock_llm, "")

        assert result is False

    @pytest.mark.asyncio
    async def test_visual_click_whitespace_description_returns_false(self):
        """纯空白目标描述返回False"""
        mock_page = MagicMock()
        mock_llm = MagicMock()

        result = await visual_click(mock_page, mock_llm, "   ")

        assert result is False

    @pytest.mark.asyncio
    async def test_visual_click_screenshot_failure_returns_false(self):
        """截图失败返回False"""
        mock_page = MagicMock()
        mock_llm = MagicMock()

        with patch("app.infrastructure.external.browser.visual_click._capture_scaled_screenshot") as mock_cap:
            mock_cap.return_value = (None, 1.0)
            result = await visual_click(mock_page, mock_llm, "按钮")

        assert result is False

    @pytest.mark.asyncio
    async def test_visual_click_llm_returns_null_returns_false(self):
        """LLM返回null坐标(找不到元素)返回False"""
        mock_page = MagicMock()
        mock_page.mouse = MagicMock()
        mock_page.mouse.click = AsyncMock()
        mock_llm = MagicMock()
        mock_llm.invoke = AsyncMock(return_value={"content": '{"x": null, "y": null}'})

        with patch("app.infrastructure.external.browser.visual_click._capture_scaled_screenshot") as mock_cap:
            mock_cap.return_value = ("base64data", 1.0)
            result = await visual_click(mock_page, mock_llm, "不存在的按钮")

        assert result is False
        mock_page.mouse.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_visual_click_mouse_click_exception_returns_false(self):
        """mouse.click异常返回False(不传播异常)"""
        mock_page = MagicMock()
        mock_page.mouse = MagicMock()
        mock_page.mouse.click = AsyncMock(side_effect=RuntimeError("page crashed"))
        mock_llm = MagicMock()
        mock_llm.invoke = AsyncMock(return_value={"content": '{"x": 50, "y": 50}'})

        with patch("app.infrastructure.external.browser.visual_click._capture_scaled_screenshot") as mock_cap:
            mock_cap.return_value = ("base64data", 1.0)
            result = await visual_click(mock_page, mock_llm, "按钮")

        assert result is False


# ==================== PlaywrightBrowser _click_with_retry 集成测试 ====================


class TestClickWithRetryVisualFallback:
    """_click_with_retry第六级视觉兜底触发测试"""

    def _create_browser(self) -> PlaywrightBrowser:
        """创建带mock的PlaywrightBrowser实例(注入multimodal_llm以启用视觉兜底)"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        # 视觉兜底契约: 必须注入multimodal_llm(视觉模型)才会触发visual_click;
        # None时降级不调用visual_click,避免纯文本LLM返回垃圾坐标误点
        browser.multimodal_llm = MagicMock()
        browser.page = MagicMock()
        browser.page.url = "https://example.com/page"
        browser.page.mouse = MagicMock()
        browser.page.mouse.click = AsyncMock()
        browser.page.screenshot = AsyncMock(return_value=b"")
        browser.page.evaluate = AsyncMock(return_value=True)
        browser.browser = MagicMock()
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        browser._scroll_into_view = AsyncMock()
        return browser

    @pytest.mark.asyncio
    async def test_visual_fallback_triggered_when_dom_strategies_fail(self):
        """五级DOM策略全部失败后触发视觉兜底"""
        browser = self._create_browser()
        element = MagicMock()
        # 所有策略都抛异常
        element.click = AsyncMock(side_effect=RuntimeError("not clickable"))
        element.bounding_box = AsyncMock(return_value=None)
        browser.page.evaluate = AsyncMock(side_effect=RuntimeError("js error"))

        # mock visual_click返回True
        with patch(
            "app.infrastructure.external.browser.playwright_browser.visual_click",
            new_callable=AsyncMock, return_value=True,
        ) as mock_vc:
            result = await browser._click_with_retry(element, target_description="登录按钮")

        assert result is True
        mock_vc.assert_called_once()
        # 验证传入的target_description
        call_args = mock_vc.call_args
        assert call_args[0][2] == "登录按钮"

    @pytest.mark.asyncio
    async def test_visual_fallback_not_triggered_without_description(self):
        """无target_description时不触发视觉兜底(返回False)"""
        browser = self._create_browser()
        element = MagicMock()
        element.click = AsyncMock(side_effect=RuntimeError("not clickable"))
        element.bounding_box = AsyncMock(return_value=None)
        browser.page.evaluate = AsyncMock(side_effect=RuntimeError("js error"))

        with patch(
            "app.infrastructure.external.browser.playwright_browser.visual_click",
            new_callable=AsyncMock, return_value=True,
        ) as mock_vc:
            result = await browser._click_with_retry(element)

        assert result is False
        mock_vc.assert_not_called()

    @pytest.mark.asyncio
    async def test_visual_fallback_not_triggered_when_dom_succeeds(self):
        """DOM策略成功时不触发视觉兜底"""
        browser = self._create_browser()
        element = MagicMock()
        element.click = AsyncMock()  # 正常点击成功

        with patch(
            "app.infrastructure.external.browser.playwright_browser.visual_click",
            new_callable=AsyncMock, return_value=True,
        ) as mock_vc:
            result = await browser._click_with_retry(element, target_description="按钮")

        assert result is True
        mock_vc.assert_not_called()

    @pytest.mark.asyncio
    async def test_visual_fallback_failure_returns_false(self):
        """视觉兜底也失败时返回False"""
        browser = self._create_browser()
        element = MagicMock()
        element.click = AsyncMock(side_effect=RuntimeError("not clickable"))
        element.bounding_box = AsyncMock(return_value=None)
        browser.page.evaluate = AsyncMock(side_effect=RuntimeError("js error"))

        with patch(
            "app.infrastructure.external.browser.playwright_browser.visual_click",
            new_callable=AsyncMock, return_value=False,
        ) as mock_vc:
            result = await browser._click_with_retry(element, target_description="按钮")

        assert result is False
        mock_vc.assert_called_once()

    @pytest.mark.asyncio
    async def test_visual_fallback_degrades_when_multimodal_llm_none(self):
        """multimodal_llm为None时优雅降级: 不调用visual_click,直接返回False。
        避免纯文本LLM被误用于视觉定位返回垃圾坐标导致误点。"""
        browser = self._create_browser()
        browser.multimodal_llm = None  # 未配置多模态LLM
        element = MagicMock()
        element.click = AsyncMock(side_effect=RuntimeError("not clickable"))
        element.bounding_box = AsyncMock(return_value=None)
        browser.page.evaluate = AsyncMock(side_effect=RuntimeError("js error"))

        with patch(
            "app.infrastructure.external.browser.playwright_browser.visual_click",
            new_callable=AsyncMock, return_value=True,
        ) as mock_vc:
            result = await browser._click_with_retry(element, target_description="按钮")

        assert result is False
        mock_vc.assert_not_called()


class TestClickImplVisualIntegration:
    """_click_impl的ref/text分支视觉兜底集成"""

    def _create_browser(self) -> PlaywrightBrowser:
        """创建带mock的PlaywrightBrowser实例"""
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
        browser._ensure_page = AsyncMock()
        browser._post_action_sync = AsyncMock()
        browser._check_element_interactable = AsyncMock(return_value={"interactable": True})
        return browser

    @pytest.mark.asyncio
    async def test_click_text_branch_passes_description_to_retry(self):
        """click的text分支将text作为target_description传入_click_with_retry"""
        browser = self._create_browser()
        element = MagicMock()
        browser._locate_element_by_text = AsyncMock(return_value=element)
        browser._click_with_retry = AsyncMock(return_value=True)

        result = await browser._click_impl(text="提交")

        assert result.success is True
        browser._click_with_retry.assert_called_once_with(element, target_description="提交")

    @pytest.mark.asyncio
    async def test_click_ref_branch_passes_description_to_retry(self):
        """click的ref分支将ref_str作为target_description传入_click_with_retry"""
        browser = self._create_browser()
        element = MagicMock()
        browser.page.query_selector = AsyncMock(return_value=element)
        browser._ref_map = {"@e0": {"selector": '[data-manus-id="manus-element-0"]'}}
        browser._click_with_retry = AsyncMock(return_value=True)

        result = await browser._click_impl(ref="@e0")

        assert result.success is True
        browser._click_with_retry.assert_called_once_with(element, target_description="@e0")

    @pytest.mark.asyncio
    async def test_click_index_branch_no_visual_fallback(self):
        """click的index分支不启用视觉兜底(无target_description)"""
        browser = self._create_browser()
        element = MagicMock()
        browser._get_element_by_id = AsyncMock(return_value=element)
        browser._click_with_retry = AsyncMock(return_value=False)

        result = await browser._click_impl(index=0)

        assert result.success is False
        # 验证未传target_description
        call_kwargs = browser._click_with_retry.call_args
        assert "target_description" not in call_kwargs.kwargs or call_kwargs.kwargs.get("target_description") is None
