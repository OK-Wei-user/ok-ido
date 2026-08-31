#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_deep_research.py
DeepResearchTool单元测试 - 递归研究图、洞察抽取、后续查询生成、超时熔断
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.models.research import ResearchContext, ResearchInsight, ResearchSummary
from app.domain.models.search import SearchResultItem, SearchResults
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.deep_research import DeepResearchTool


def _make_search_result(content="正文内容", url="https://example.com", title="Example"):
    item = SearchResultItem(url=url, title=title, content=content)
    return ToolResult(success=True, data=SearchResults(query="test", results=[item]))


def _make_llm_response(content):
    return {"role": "assistant", "content": content}


class TestDeepResearchBasic:
    """基础功能测试"""

    @pytest.mark.asyncio
    async def test_returns_research_summary(self):
        search_tool = MagicMock()
        search_tool._search_internal = AsyncMock(return_value=_make_search_result())
        llm = MagicMock()
        llm.invoke = AsyncMock(return_value=_make_llm_response('{"insights": [{"content": "test", "relevance_score": 0.9}]}'))
        json_parser = MagicMock()
        json_parser.invoke = AsyncMock(return_value={"insights": [{"content": "test", "relevance_score": 0.9}]})
        tool = DeepResearchTool(search_tool=search_tool, llm=llm, json_parser=json_parser, max_depth=1)

        result = await tool.deep_research("test query")

        assert result.success
        assert isinstance(result.data, ResearchSummary)
        assert result.data.query == "test query"

    @pytest.mark.asyncio
    async def test_first_round_uses_original_query(self):
        search_tool = MagicMock()
        search_tool._search_internal = AsyncMock(return_value=ToolResult(success=True, data=SearchResults(query="q", results=[])))
        llm = MagicMock()
        llm.invoke = AsyncMock()
        json_parser = MagicMock()
        json_parser.invoke = AsyncMock()
        tool = DeepResearchTool(search_tool=search_tool, llm=llm, json_parser=json_parser, max_depth=1)

        await tool.deep_research("original query")

        # 第一轮应直接用原query，不调用_generate_optimized_query的LLM
        args, kwargs = search_tool._search_internal.call_args
        assert kwargs.get("query") == "original query"

    @pytest.mark.asyncio
    async def test_max_depth_zero_returns_empty_summary(self):
        search_tool = MagicMock()
        search_tool._search_internal = AsyncMock()
        llm = MagicMock()
        json_parser = MagicMock()
        tool = DeepResearchTool(search_tool=search_tool, llm=llm, json_parser=json_parser, max_depth=0)

        result = await tool.deep_research("test")

        assert result.success
        assert len(result.data.key_findings) == 0
        search_tool._search_internal.assert_not_called()


class TestDeepResearchInsightExtraction:
    """洞察抽取测试"""

    @pytest.mark.asyncio
    async def test_extracts_insights_from_content(self):
        search_tool = MagicMock()
        search_tool._search_internal = AsyncMock(return_value=_make_search_result(content="重要内容"))
        llm = MagicMock()
        llm.invoke = AsyncMock(return_value=_make_llm_response('{"insights": [{"content": "洞察1", "relevance_score": 0.9}]}'))
        json_parser = MagicMock()
        json_parser.invoke = AsyncMock(return_value={"insights": [{"content": "洞察1", "relevance_score": 0.9}]})
        tool = DeepResearchTool(search_tool=search_tool, llm=llm, json_parser=json_parser, max_depth=1, max_insights=20)

        result = await tool.deep_research("test")

        assert len(result.data.key_findings) == 1
        assert result.data.key_findings[0].content == "洞察1"
        assert result.data.key_findings[0].relevance_score == 0.9

    @pytest.mark.asyncio
    async def test_fallback_score_on_parse_failure(self):
        search_tool = MagicMock()
        search_tool._search_internal = AsyncMock(return_value=_make_search_result(content="内容"))
        llm = MagicMock()
        llm.invoke = AsyncMock(return_value=_make_llm_response("invalid json"))
        json_parser = MagicMock()
        # 解析器抛异常触发兜底逻辑
        json_parser.invoke = AsyncMock(side_effect=Exception("parse failed"))
        tool = DeepResearchTool(search_tool=search_tool, llm=llm, json_parser=json_parser, max_depth=1)

        result = await tool.deep_research("test")

        # 解析失败时使用兜底评分0.7，归入additional_findings
        assert len(result.data.additional_findings) >= 1
        assert result.data.additional_findings[0].relevance_score == 0.7

    @pytest.mark.asyncio
    async def test_relevance_score_tiers(self):
        search_tool = MagicMock()
        search_tool._search_internal = AsyncMock(return_value=_make_search_result())
        llm = MagicMock()
        llm.invoke = AsyncMock(return_value=_make_llm_response(
            '{"insights": [{"content": "high", "relevance_score": 0.9}, {"content": "mid", "relevance_score": 0.6}, {"content": "low", "relevance_score": 0.3}]}'
        ))
        json_parser = MagicMock()
        json_parser.invoke = AsyncMock(return_value={
            "insights": [
                {"content": "high", "relevance_score": 0.9},
                {"content": "mid", "relevance_score": 0.6},
                {"content": "low", "relevance_score": 0.3},
            ]
        })
        tool = DeepResearchTool(search_tool=search_tool, llm=llm, json_parser=json_parser, max_depth=1)

        result = await tool.deep_research("test")

        assert len(result.data.key_findings) == 1  # >=0.8
        assert len(result.data.additional_findings) == 1  # 0.5-0.8
        assert len(result.data.supplementary) == 1  # <0.5


class TestDeepResearchRecursion:
    """递归研究测试"""

    @pytest.mark.asyncio
    async def test_visited_urls_dedup_across_recursion(self):
        # 第一轮和第二轮返回相同URL，第二轮应跳过
        same_result = _make_search_result(url="https://same.com")
        search_tool = MagicMock()
        search_tool._search_internal = AsyncMock(return_value=same_result)
        llm = MagicMock()
        llm.invoke = AsyncMock(side_effect=[
            _make_llm_response('{"insights": [{"content": "i1", "relevance_score": 0.9}]}'),
            _make_llm_response('{"follow_ups": ["follow up 1"]}'),
            _make_llm_response('{"insights": [{"content": "i2", "relevance_score": 0.9}]}'),
            _make_llm_response('{"follow_ups": []}'),
        ])
        json_parser = MagicMock()
        json_parser.invoke = AsyncMock(side_effect=[
            {"insights": [{"content": "i1", "relevance_score": 0.9}]},
            {"follow_ups": ["follow up 1"]},
            {"insights": [{"content": "i2", "relevance_score": 0.9}]},
            {"follow_ups": []},
        ])
        tool = DeepResearchTool(search_tool=search_tool, llm=llm, json_parser=json_parser, max_depth=2)

        result = await tool.deep_research("test")

        # 第二轮的相同URL应被跳过，不会重复抽取
        # visited_urls应只包含1个URL
        assert result.data.total_sources == 1

    @pytest.mark.asyncio
    async def test_max_insights_truncates(self):
        search_tool = MagicMock()
        search_tool._search_internal = AsyncMock(return_value=_make_search_result())
        llm = MagicMock()
        llm.invoke = AsyncMock(return_value=_make_llm_response(
            '{"insights": [{"content": "i1", "relevance_score": 0.9}, {"content": "i2", "relevance_score": 0.9}]}'
        ))
        json_parser = MagicMock()
        json_parser.invoke = AsyncMock(return_value={
            "insights": [
                {"content": "i1", "relevance_score": 0.9},
                {"content": "i2", "relevance_score": 0.9},
            ]
        })
        tool = DeepResearchTool(search_tool=search_tool, llm=llm, json_parser=json_parser, max_depth=1, max_insights=1)

        result = await tool.deep_research("test")

        # max_insights=1，应只保留1条
        total = len(result.data.key_findings) + len(result.data.additional_findings) + len(result.data.supplementary)
        assert total == 1


class TestDeepResearchTimeout:
    """超时熔断测试"""

    @pytest.mark.asyncio
    async def test_timeout_returns_partial_results(self):
        async def slow_search(*args, **kwargs):
            await asyncio.sleep(10)
            return _make_search_result()

        search_tool = MagicMock()
        search_tool._search_internal = slow_search
        llm = MagicMock()
        json_parser = MagicMock()
        tool = DeepResearchTool(search_tool=search_tool, llm=llm, json_parser=json_parser, max_depth=2, time_limit=1)

        result = await tool.deep_research("test")

        # 超时后仍返回ResearchSummary（部分结果）
        assert result.success
        assert isinstance(result.data, ResearchSummary)
        assert result.data.query == "test"

    @pytest.mark.asyncio
    async def test_exception_returns_partial_results(self):
        search_tool = MagicMock()
        search_tool._search_internal = AsyncMock(side_effect=Exception("search failed"))
        llm = MagicMock()
        json_parser = MagicMock()
        tool = DeepResearchTool(search_tool=search_tool, llm=llm, json_parser=json_parser, max_depth=2)

        result = await tool.deep_research("test")

        # 异常后仍返回ResearchSummary
        assert result.success
        assert isinstance(result.data, ResearchSummary)


class TestResearchSummary:
    """ResearchSummary分档测试"""

    def test_from_context_empty(self):
        ctx = ResearchContext(query="test")
        summary = ResearchSummary.from_context(ctx)
        assert len(summary.key_findings) == 0
        assert len(summary.additional_findings) == 0
        assert len(summary.supplementary) == 0
        assert summary.total_sources == 0

    def test_from_context_buckets_by_score(self):
        ctx = ResearchContext(query="test")
        ctx.insights = [
            ResearchInsight(content="high", source_url="u1", source_title="t1", relevance_score=0.9),
            ResearchInsight(content="mid", source_url="u2", source_title="t2", relevance_score=0.6),
            ResearchInsight(content="low", source_url="u3", source_title="t3", relevance_score=0.3),
        ]
        ctx.visited_urls = {"u1", "u2", "u3"}

        summary = ResearchSummary.from_context(ctx)

        assert len(summary.key_findings) == 1
        assert summary.key_findings[0].content == "high"
        assert len(summary.additional_findings) == 1
        assert summary.additional_findings[0].content == "mid"
        assert len(summary.supplementary) == 1
        assert summary.supplementary[0].content == "low"
        assert summary.total_sources == 3
