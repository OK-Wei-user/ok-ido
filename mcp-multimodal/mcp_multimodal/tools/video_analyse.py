#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : video_analyse.py
视频分析工具 - 基于BigModel视觉模型对视频内容进行摘要与关键帧解析
"""
import json
import logging

import httpx

from ..client import BigModelClient, BigModelAPIError
from ..utils.file_utils import is_url

logger = logging.getLogger(__name__)


def register_video_analyse(mcp, client: BigModelClient):
    @mcp.tool()
    async def video_analyse(
        video_source: str,
        prompt: str = "请对视频内容进行摘要，提取关键帧信息和主要内容",
    ) -> str:
        """视频分析工具，对视频内容进行摘要分析和关键帧解析。仅支持视频URL地址。

            适用场景：
            - 用户上传视频并要求分析内容
            - 需要理解视频画面、提取关键信息

        Args:
            video_source: 视频文件的URL地址（如附件的OSS地址/key字段），仅支持URL方式传入
            prompt: 对视频的分析要求，默认为摘要和关键帧提取

        Returns:
            视频分析结果，包含摘要和关键帧描述
        """
        if not is_url(video_source):
            return json.dumps(
                {"error": "视频分析仅支持URL方式传入，请提供视频的URL地址（如附件的OSS地址）"},
                ensure_ascii=False,
            )

        try:
            content = [
                {"type": "video_url", "video_url": {"url": video_source}},
                {"type": "text", "text": prompt},
            ]
            payload = {
                "model": client.config.vl_model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": client.config.max_tokens,
            }
            headers = {
                "Authorization": f"Bearer {client.config.api_key}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=180.0) as http_client:
                resp = await http_client.post(client.chat_url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

            choices = data.get("choices", [])
            if not choices:
                return json.dumps({"error": "视频分析API返回空结果"}, ensure_ascii=False)

            result_text = choices[0].get("message", {}).get("content", "")
            if not result_text or not result_text.strip():
                return json.dumps({"error": "视频分析返回空内容，模型可能无法处理该视频"}, ensure_ascii=False)

            return result_text
        except httpx.HTTPStatusError as e:
            logger.error(f"视频分析API调用失败 HTTP {e.response.status_code}")
            return json.dumps(
                {"error": f"视频分析API调用失败: HTTP {e.response.status_code}"},
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"视频分析失败: {e}")
            return json.dumps({"error": f"视频分析失败: {str(e)}"}, ensure_ascii=False)
