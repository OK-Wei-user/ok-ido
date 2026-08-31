#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : browser_image.py
网页视觉分析工具 - 截取网页截图并基于视觉模型进行画面理解

仅用于必须视觉理解的场景：内嵌视频、实时监控画面、数据可视化图表、
视觉识别等。禁止用于普通纯文字网页或图文资讯的纯文本阅读场景。
"""
import base64
import json
import logging

from ..client import BigModelClient
from ..utils.screenshot import ScreenshotCapture

logger = logging.getLogger(__name__)


def register_browser_image(mcp, client: BigModelClient, screenshot: ScreenshotCapture):
    @mcp.tool()
    async def webpage_visual_analyse(
        url: str,
        prompt: str = "请详细描述这个网页画面的视觉内容",
    ) -> str:
        """【仅限视觉理解】截取网页截图并进行视觉分析。
            - 仅当网页包含**必须通过视觉才能理解的内容时使用**，如：内嵌视频画面、实时监控画面、数据可视化图表、地图、设计稿、图片画廊等视觉元素。
            - 禁止用于：普通纯文字网页、新闻资讯提取、文档内容获取、图文混排网页内容解析等常规网页阅读类场景。以上所有场景，请统一使用浏览器工具直接读取页面内容，不得使用本工具。

        Args:
            url: 需要视觉分析的网页URL地址
            prompt: 对网页画面的视觉分析要求，如"描述监控画面中的人员活动"、"分析图表数据趋势"

        Returns:
            基于网页截图的视觉分析结果
        """
        if not screenshot.available:
            return json.dumps(
                {"error": "截图服务不可用(Playwright未启动)，无法进行网页视觉分析"},
                ensure_ascii=False,
            )

        try:
            screenshot_bytes = await screenshot.capture(url)
            img_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            result = await client.vl_chat(
                prompt=prompt,
                image_base64_list=[img_b64],
            )
            return result
        except RuntimeError as e:
            logger.error(f"网页截图服务异常: {e}")
            return json.dumps(
                {"error": f"网页截图服务异常: {str(e)}"},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"网页视觉分析失败: {e}")
            return json.dumps(
                {"error": f"网页视觉分析失败: {str(e)}"},
                ensure_ascii=False,
            )
