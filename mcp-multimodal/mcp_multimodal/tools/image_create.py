#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : image_create.py
图像生成工具 - 基于BigModel CogView图像生成模型
"""
import json
import logging

from ..client import BigModelClient

logger = logging.getLogger(__name__)


def register_image_create(mcp, client: BigModelClient):
    @mcp.tool()
    async def image_create(
        prompt: str,
        size: str = "1024x1024",
    ) -> str:
        """AI文本生图工具，根据文字描述生成具有艺术性或真实感的图像。

            适用场景（AI生图）：
            - 生成风景、人物、动物、物品等具象画面
            - 创建艺术插画、海报设计、创意图片
            - 用户明确要求"AI画图"、"生成图片"、"创作插图"

            不适用场景（必须用Python代码绘制）：
            - 数据可视化图表（柱状图/折线图/饼图/散点图等）→ 用Shell执行Python matplotlib
            - 精确数学/几何图形 → 用Shell执行Python matplotlib/Pillow
            - 含具体数值的统计图 → 用Shell执行Python matplotlib
            原因：AI生图无法保证数值精确性和标签准确性，数据可视化必须用代码绘制。

        Args:
            prompt: 图像生成的文字描述/提示词，越详细生成效果越好。建议包含：主体内容、风格、色调、构图等细节
            size: 生成图像的尺寸，默认1024x1024，可选: 768x1344/864x1152/1344x768/1152x864/1440x720/720x1440

        Returns:
            生成图像的URL地址
        """
        try:
            result = await client.image_generate(prompt=prompt, size=size)
            return result
        except Exception as e:
            logger.error(f"图像生成失败: {e}")
            return json.dumps({"error": f"图像生成失败: {str(e)}"}, ensure_ascii=False)
