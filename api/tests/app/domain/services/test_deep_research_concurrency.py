#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_deep_research_concurrency.py
06优化单元测试 - DeepResearch并发洞察抽取

验证点:
- 多条搜索结果并发调用_extract_insights(Semaphore控制并发度=3)
- 并发场景下insights上限二次检查生效,避免无效LLM调用
- 单条抽取异常不影响其他( return_exceptions=True )
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.research import ResearchContext
from app.domain.models.search import SearchResultItem
from app.domain.services.tools.deep_research import (
    DeepResearchTool,
    _INSIGHT_EXTRACTION_CONCURRENCY,
)


def _build_tool(max_insights: int = 20) -> DeepResearchTool:
    """构造DeepResearchTool实例(LLM/SearchTool均为mock)"""
    search_tool = MagicMock()
    search_tool.has_tool = MagicMock(return_value=True)
    llm = AsyncMock()
    json_parser = MagicMock()
    return DeepResearchTool(
        search_tool=search_tool,
        llm=llm,
        json_parser=json_parser,
        max_depth=1,
        results_per_search=5,
        max_insights=max_insights,
        time_limit=30,
    )


def _build_item(url: str, content: str = "正文内容") -> SearchResultItem:
    """构造单条搜索结果"""
    return SearchResultItem(
        title=f"标题-{url}",
        url=url,
        snippet="片段",
        content=content,
    )


class TestInsightExtractionConcurrency:
    """并发洞察抽取"""

    @pytest.mark.asyncio
    async def test_concurrent_extract_invokes_all_items(self):
        """5条结果并发抽取,每条都触发_extract_insights"""
        tool = _build_tool(max_insights=20)
        items = [_build_item(f"http://example.com/{i}") for i in range(5)]

        with patch.object(
            tool, "_extract_insights", new=AsyncMock()
        ) as mock_extract:
            semaphore = asyncio.Semaphore(_INSIGHT_EXTRACTION_CONCURRENCY)
            ctx = ResearchContext(query="测试主题")
            tasks = [
                tool._extract_insights_concurrent(item, ctx, semaphore)
                for item in items
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

            assert mock_extract.await_count == 5

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """Semaphore(3)限制并发,同一时刻最多3个_extract_insights在执行"""
        tool = _build_tool(max_insights=20)
        current_concurrent = 0
        max_concurrent = 0
        lock = asyncio.Lock()

        async def mock_extract(item, ctx):
            nonlocal current_concurrent, max_concurrent
            async with lock:
                current_concurrent += 1
                max_concurrent = max(max_concurrent, current_concurrent)
            await asyncio.sleep(0.05)
            async with lock:
                current_concurrent -= 1

        with patch.object(tool, "_extract_insights", new=mock_extract):
            semaphore = asyncio.Semaphore(_INSIGHT_EXTRACTION_CONCURRENCY)
            ctx = ResearchContext(query="测试主题")
            items = [_build_item(f"http://example.com/{i}") for i in range(6)]
            tasks = [
                tool._extract_insights_concurrent(item, ctx, semaphore)
                for item in items
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        assert max_concurrent <= _INSIGHT_EXTRACTION_CONCURRENCY

    @pytest.mark.asyncio
    async def test_skip_when_insights_full(self):
        """insights已达上限时,二次检查跳过LLM调用"""
        tool = _build_tool(max_insights=2)
        ctx = ResearchContext(query="测试主题")
        ctx.insights.append(MagicMock())
        ctx.insights.append(MagicMock())

        with patch.object(
            tool, "_extract_insights", new=AsyncMock()
        ) as mock_extract:
            semaphore = asyncio.Semaphore(_INSIGHT_EXTRACTION_CONCURRENCY)
            item = _build_item("http://example.com/1")
            await tool._extract_insights_concurrent(item, ctx, semaphore)

            mock_extract.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_failure_does_not_block_others(self):
        """单条抽取异常不影响其他(gather return_exceptions=True)"""
        tool = _build_tool(max_insights=20)

        call_count = 0

        async def mock_extract(item, ctx):
            nonlocal call_count
            call_count += 1
            if "fail" in item.url:
                raise RuntimeError("模拟LLM异常")

        with patch.object(tool, "_extract_insights", new=mock_extract):
            semaphore = asyncio.Semaphore(_INSIGHT_EXTRACTION_CONCURRENCY)
            ctx = ResearchContext(query="测试主题")
            items = [
                _build_item("http://example.com/ok-1"),
                _build_item("http://example.com/fail"),
                _build_item("http://example.com/ok-2"),
            ]
            tasks = [
                tool._extract_insights_concurrent(item, ctx, semaphore)
                for item in items
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        assert call_count == 3
