#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_browser_llm_injection.py
缺口1-LLM注入链单元测试
- DockerSandbox.get_browser: 透传llm/multimodal_llm到PlaywrightBrowser
- PlaywrightBrowser构造: 分离存储llm(文本摘要)与multimodal_llm(视觉兜底)
- _click_with_retry: 视觉兜底使用multimodal_llm而非llm(防文本模型误点)
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser


# ==================== PlaywrightBrowser构造分离LLM测试 ====================


class TestPlaywrightBrowserLLMSeparation:
    """PlaywrightBrowser构造: 分离文本LLM与多模态LLM"""

    def test_constructor_stores_both_llms(self):
        """构造时分别存储llm与multimodal_llm"""
        text_llm = MagicMock(name="text_llm")
        vision_llm = MagicMock(name="vision_llm")

        browser = PlaywrightBrowser(
            cdp_url="http://localhost:9222",
            llm=text_llm,
            multimodal_llm=vision_llm,
        )

        assert browser.llm is text_llm
        assert browser.multimodal_llm is vision_llm

    def test_constructor_defaults_none(self):
        """未传LLM时llm/multimodal_llm均为None(五级DOM容错完整可用)"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")

        assert browser.llm is None
        assert browser.multimodal_llm is None

    def test_text_llm_only_does_not_reuse_for_visual(self):
        """仅注入文本llm时,multimodal_llm仍为None(visual_click降级不可用)"""
        text_llm = MagicMock(name="text_llm")

        browser = PlaywrightBrowser(cdp_url="http://localhost:9222", llm=text_llm)

        # 文本LLM绝不复用为视觉兜底,避免文本模型返回垃圾坐标误点
        assert browser.llm is text_llm
        assert browser.multimodal_llm is None


# ==================== _click_with_retry视觉兜底LLM选择测试 ====================


class TestClickWithRetryLLMSelection:
    """_click_with_retry视觉兜底使用multimodal_llm而非llm"""

    def _create_browser(self, multimodal_llm=None) -> PlaywrightBrowser:
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser.multimodal_llm = multimodal_llm
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
    async def test_visual_fallback_uses_multimodal_llm_not_text_llm(self):
        """视觉兜底调用visual_click时传入multimodal_llm,而非self.llm"""
        vision_llm = MagicMock(name="vision_llm")
        text_llm = MagicMock(name="text_llm")
        browser = self._create_browser(multimodal_llm=vision_llm)
        browser.llm = text_llm  # 注入文本LLM,验证不被误用
        # 五级DOM策略全失败
        element = MagicMock()
        element.click = AsyncMock(side_effect=RuntimeError("not clickable"))
        element.bounding_box = AsyncMock(return_value=None)
        browser.page.evaluate = AsyncMock(side_effect=RuntimeError("js error"))

        with patch(
            "app.infrastructure.external.browser.playwright_browser.visual_click",
            new_callable=AsyncMock, return_value=True,
        ) as mock_vc:
            await browser._click_with_retry(element, target_description="提交按钮")

        mock_vc.assert_called_once()
        # 第二个参数必须是multimodal_llm(vision_llm),不能是text_llm
        assert mock_vc.call_args[0][1] is vision_llm
        assert mock_vc.call_args[0][1] is not text_llm

    @pytest.mark.asyncio
    async def test_visual_fallback_skipped_when_multimodal_none(self):
        """multimodal_llm为None时不触发visual_click(即使llm已注入)"""
        text_llm = MagicMock(name="text_llm")
        browser = self._create_browser(multimodal_llm=None)
        browser.llm = text_llm
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


# ==================== DockerSandbox.get_browser透传测试 ====================


class TestDockerSandboxGetBrowserPassthrough:
    """DockerSandbox.get_browser: 透传llm/multimodal_llm到PlaywrightBrowser"""

    @pytest.mark.asyncio
    async def test_get_browser_passes_both_llms(self):
        """get_browser透传llm与multimodal_llm到PlaywrightBrowser构造器"""
        from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox

        text_llm = MagicMock(name="text_llm")
        vision_llm = MagicMock(name="vision_llm")
        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox._cdp_url = "http://localhost:9222"  # cdp_url属性由_cdp_url支持

        with patch(
            "app.infrastructure.external.sandbox.docker_sandbox.PlaywrightBrowser"
        ) as mock_ctor:
            mock_ctor.return_value = MagicMock()
            await sandbox.get_browser(llm=text_llm, multimodal_llm=vision_llm)

        mock_ctor.assert_called_once_with(
            "http://localhost:9222", llm=text_llm, multimodal_llm=vision_llm,
        )

    @pytest.mark.asyncio
    async def test_get_browser_defaults_none(self):
        """get_browser未传LLM时透传None(浏览器仅依赖DOM五级容错)"""
        from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox._cdp_url = "http://localhost:9222"  # cdp_url属性由_cdp_url支持

        with patch(
            "app.infrastructure.external.sandbox.docker_sandbox.PlaywrightBrowser"
        ) as mock_ctor:
            mock_ctor.return_value = MagicMock()
            await sandbox.get_browser()

        mock_ctor.assert_called_once_with(
            "http://localhost:9222", llm=None, multimodal_llm=None,
        )
