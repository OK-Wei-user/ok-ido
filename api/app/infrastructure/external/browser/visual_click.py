#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : visual_click.py
视觉路径点击兜底 - 当常规选择器/ref/text定位全部失败时，调用多模态LLM分析截图，
返回目标元素坐标并执行mouse.click。作为五级容错后的第六级兜底策略。

设计要点:
1. 复用PlaywrightBrowser注入的LLM实例，不引入额外依赖；
2. LLM不可用或返回无效坐标时优雅降级(返回False，不阻塞主流程)；
3. 截图缩放比例校正: LLM看到的是缩放后图片，需反算实际页面坐标；
4. 目标描述由调用方传入(元素text或语义描述)，避免硬编码。
"""
import base64
import io
import json
import logging
import re
from typing import Optional, Tuple, Any

logger = logging.getLogger(__name__)

# 视觉定位请求的截图最大宽度(像素)，超过则缩放以控制token消耗
_VISUAL_SCREENSHOT_MAX_WIDTH = 1280
# 视觉定位失败时的默认返回
_INVALID_COORDINATES: Tuple[Optional[float], Optional[float]] = (None, None)


async def visual_click(page: Any, llm: Any, target_description: str) -> bool:
    """视觉路径点击: 截图→LLM分析坐标→mouse.click。

    Args:
        page: Playwright Page实例
        llm: LLM实例(需支持多模态invoke)，为None时直接返回False
        target_description: 目标元素描述(如"提交按钮"或元素文本)

    Returns:
        True=点击成功，False=定位失败或LLM不可用
    """
    if llm is None:
        logger.debug("visual_click跳过: LLM未注入")
        return False
    if not target_description or not target_description.strip():
        logger.debug("visual_click跳过: 目标描述为空")
        return False

    try:
        screenshot_b64, scale = await _capture_scaled_screenshot(page)
        if not screenshot_b64:
            return False

        x, y = await _visual_locate(llm, screenshot_b64, target_description)
        if x is None or y is None:
            logger.info(f"视觉定位[{target_description}]未返回有效坐标")
            return False

        # 校正缩放比例: LLM基于缩放图返回坐标，需反算实际页面坐标
        actual_x = x / scale
        actual_y = y / scale
        logger.info(f"视觉点击[{target_description}]: 坐标({actual_x:.0f},{actual_y:.0f})")
        await page.mouse.click(actual_x, actual_y)
        return True
    except Exception as e:
        logger.warning(f"visual_click失败(不影响主流程): {str(e)}")
        return False


async def _capture_scaled_screenshot(page: Any) -> Tuple[Optional[str], float]:
    """截取视口截图并缩放，返回(base64编码, 缩放比例)"""
    try:
        png_bytes = await page.screenshot(type="png", full_page=False)
        if not png_bytes:
            return None, 1.0
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        if img.width <= _VISUAL_SCREENSHOT_MAX_WIDTH:
            return base64.b64encode(png_bytes).decode("ascii"), 1.0
        scale = _VISUAL_SCREENSHOT_MAX_WIDTH / img.width
        new_height = int(img.height * scale)
        img = img.resize((_VISUAL_SCREENSHOT_MAX_WIDTH, new_height), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=60)
        return base64.b64encode(buf.getvalue()).decode("ascii"), scale
    except Exception as e:
        logger.debug(f"视觉截图失败: {str(e)}")
        return None, 1.0


async def _visual_locate(
        llm: Any, screenshot_b64: str, target_description: str,
) -> Tuple[Optional[float], Optional[float]]:
    """调用多模态LLM分析截图，返回目标元素坐标(x, y)。

    LLM需返回JSON格式: {"x": <number>, "y": <number>}
    """
    prompt = (
        f"请在截图中找到目标元素并返回其中心点坐标。目标描述: {target_description}\n"
        f"只返回JSON格式: {{\"x\": <横坐标>, \"y\": <纵坐标>}}，不要返回其他内容。"
        f"如果找不到目标元素，返回: {{\"x\": null, \"y\": null}}。"
    )
    try:
        response = await llm.invoke([
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                    },
                ],
            }
        ])
        content = response.get("content", "") if isinstance(response, dict) else str(response)
        return _parse_coordinates(content)
    except Exception as e:
        logger.debug(f"LLM视觉定位失败: {str(e)}")
        return _INVALID_COORDINATES


def _parse_coordinates(content: str) -> Tuple[Optional[float], Optional[float]]:
    """从LLM响应中解析坐标JSON"""
    try:
        # 兼容LLM可能包裹```json```的情况
        match = re.search(r'\{[^{}]*"x"[^{}]*"y"[^{}]*\}', content, re.DOTALL)
        if not match:
            return _INVALID_COORDINATES
        data = json.loads(match.group(0))
        x = data.get("x")
        y = data.get("y")
        if x is None or y is None:
            return _INVALID_COORDINATES
        return float(x), float(y)
    except Exception:
        return _INVALID_COORDINATES
