#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : screenshot.py
网页截图工具 - 基于Playwright的异步网页截图捕获
"""
import logging
from typing import Optional

from ..config import ScreenshotConfig

logger = logging.getLogger(__name__)


class ScreenshotCapture:
    """Playwright网页截图捕获器，单例浏览器+每请求新建页面"""

    def __init__(self, config: ScreenshotConfig) -> None:
        self._config = config
        self._playwright = None
        self._browser = None

    async def start(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            logger.info("Playwright Chromium浏览器已启动")
        except Exception as e:
            logger.error(f"Playwright浏览器启动失败: {e}")
            self._playwright = None
            self._browser = None
            raise

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Playwright浏览器已关闭")

    @property
    def available(self) -> bool:
        return self._browser is not None

    async def capture(self, url: str) -> bytes:
        """对指定URL进行网页截图，返回PNG格式的截图字节"""
        if not self._browser:
            raise RuntimeError("ScreenshotCapture未启动或启动失败")

        page = await self._browser.new_page(
            viewport={"width": self._config.width, "height": self._config.height}
        )
        try:
            await page.goto(
                url,
                wait_until="networkidle",
                timeout=self._config.timeout_ms,
            )
            screenshot = await page.screenshot(
                full_page=self._config.full_page,
                type="png",
            )
            logger.info(f"网页截图完成: {url}, 大小={len(screenshot)}字节")
            return screenshot
        except Exception as e:
            logger.error(f"网页截图失败: url={url}, error={e}")
            raise
        finally:
            await page.close()
