#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : url_utils.py
URL处理工具 - 网页文本提取
"""
import logging

import httpx

logger = logging.getLogger(__name__)


async def fetch_webpage_text(url: str, max_length: int = 8000) -> str:
    """获取网页HTML文本内容"""
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            return resp.text[:max_length]
    except httpx.HTTPStatusError as e:
        logger.error(f"获取网页文本失败 HTTP {e.response.status_code}: {url}")
        raise
    except httpx.RequestError as e:
        logger.error(f"获取网页文本请求异常: {url}, {e}")
        raise
