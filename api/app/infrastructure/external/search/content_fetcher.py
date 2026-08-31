#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : content_fetcher.py
网页正文抓取器 - httpx异步抓取 + BeautifulSoup去脚本样式 + 截断 + 重试 + 并发限流
用于深度研究场景，将搜索结果URL的全文内容提取为纯文本供LLM分析。
"""
import asyncio
import logging
from typing import List

import httpx
from bs4 import BeautifulSoup

from app.domain.external.search import ContentFetcher
from app.domain.models.tool_result import ToolResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15  # 单页抓取超时(秒)
_DEFAULT_MAX_RETRIES = 2  # 抓取失败重试次数
_DEFAULT_MAX_CHARS = 10000  # 正文截断阈值(字符)
_DEFAULT_MAX_CONCURRENCY = 5  # 最大并发抓取数(防雪崩)

# 需要剥离的非正文标签：脚本/样式/导航/页眉页脚/表单/装饰元素
_STRIP_TAGS = ("script", "style", "header", "footer", "nav", "aside", "form", "svg", "noscript")

# 模拟主流浏览器UA，避免被部分站点拦截
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class WebContentFetcher(ContentFetcher):
    """网页正文抓取器 - 异步httpx + bs4去噪 + 重试 + 并发限流"""

    def __init__(
            self,
            timeout: float = _DEFAULT_TIMEOUT,
            max_retries: int = _DEFAULT_MAX_RETRIES,
            max_chars: int = _DEFAULT_MAX_CHARS,
            max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        """构造函数，完成抓取器参数初始化"""
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_chars = max_chars
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def fetch(self, url: str) -> ToolResult[str]:
        """抓取单个URL的网页正文，返回去噪后的纯文本。
        失败时返回ToolResult(success=False)，不影响调用方主流程。"""
        async with self._semaphore:
            return await self._fetch_with_retry(url)

    async def fetch_many(self, urls: List[str]) -> List[ToolResult[str]]:
        """批量并发抓取多个URL，单失败不影响其他，返回与urls等长的结果列表"""
        if not urls:
            return []
        return await asyncio.gather(*[self.fetch(u) for u in urls])

    async def _fetch_with_retry(self, url: str) -> ToolResult[str]:
        """带重试的抓取逻辑：超时或HTTP错误时重试，最多max_retries次"""
        last_error = ""
        for attempt in range(1, self._max_retries + 2):
            try:
                async with httpx.AsyncClient(
                        timeout=self._timeout,
                        headers={"User-Agent": _USER_AGENT},
                        follow_redirects=True,
                ) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    text = self._extract_text(response.text)
                    return ToolResult(success=True, data=text)
            except httpx.TimeoutException as e:
                last_error = f"请求超时: {str(e)}"
                logger.debug(f"抓取[{url}]超时(第{attempt}次)")
            except httpx.HTTPStatusError as e:
                last_error = f"HTTP {e.response.status_code}"
                # 4xx客户端错误不重试(除429外)
                if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                    return ToolResult(success=False, message=f"抓取失败: {last_error}")
                logger.debug(f"抓取[{url}]HTTP错误(第{attempt}次): {last_error}")
            except Exception as e:
                last_error = str(e)
                logger.debug(f"抓取[{url}]异常(第{attempt}次): {last_error}")
        return ToolResult(success=False, message=f"抓取失败(重试{self._max_retries}次): {last_error}")

    def _extract_text(self, html: str) -> str:
        """从HTML提取纯文本：BeautifulSoup解析后剥离非正文标签，压缩空白并截断"""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(_STRIP_TAGS):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # 压缩连续空行
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        text = "\n".join(lines)
        return text[:self._max_chars]
