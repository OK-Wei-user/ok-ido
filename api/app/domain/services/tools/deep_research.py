#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : deep_research.py
深度研究工具 - 迭代搜索+正文抓取+LLM洞察抽取+递归跟进

算法参考OpenManus DeepResearch：
1. LLM优化query → SearchTool._search_internal(fetch_content=True) (跳过search_web预算,由deep_research自有预算控制)
2. 遍历结果用LLM抽取insight+relevance_score，visited_urls去重
3. LLM生成后续查询，对前2个并发递归
4. 超时熔断返回部分结果，按relevance_score分档populate
"""
import asyncio
import logging
from typing import Any, List, Optional

from app.domain.external.json_parser import JSONParser
from app.domain.external.llm import LLM
from app.domain.models.research import ResearchContext, ResearchInsight, ResearchSummary
from app.domain.models.search import SearchResultItem
from app.domain.models.tool_result import ToolResult
from .base import BaseTool, tool
from .budget_tracker import ToolBudgetTracker
from .search import SearchTool

logger = logging.getLogger(__name__)

_DEFAULT_MAX_DEPTH = 2  # 默认最大递归深度
_DEFAULT_RESULTS_PER_SEARCH = 5  # 默认每轮搜索结果数
_DEFAULT_MAX_INSIGHTS = 20  # 默认最大洞察数
_DEFAULT_TIME_LIMIT = 120  # 默认超时(秒)
_MAX_FOLLOW_UPS_TO_RECURSE = 2  # 递归跟进的后续查询数上限
_MAX_FOLLOW_UPS_TO_GENERATE = 3  # 生成的后续查询数上限
_FALLBACK_RELEVANCE_SCORE = 0.7  # LLM解析失败时的兜底评分
_INSIGHT_EXTRACTION_CONCURRENCY = 3  # 洞察抽取并发数(避免LLM限流)


class DeepResearchTool(BaseTool):
    """深度研究工具 - 多轮搜索+正文抓取+LLM洞察抽取+递归跟进

    工具调用预算(project_memory硬约束): deep_research=2 会话级上限,
    由 budget_tracker 在调用前硬拦截,超限时返回错误引导 LLM 切换策略。
    """
    name: str = "deep_research"

    def __init__(
            self,
            search_tool: SearchTool,
            llm: LLM,
            json_parser: JSONParser,
            max_depth: int = _DEFAULT_MAX_DEPTH,
            results_per_search: int = _DEFAULT_RESULTS_PER_SEARCH,
            max_insights: int = _DEFAULT_MAX_INSIGHTS,
            time_limit: int = _DEFAULT_TIME_LIMIT,
            budget_tracker: Optional[ToolBudgetTracker] = None,
    ) -> None:
        """构造函数，完成深度研究工具初始化

        Args:
            search_tool: SearchTool实例（复用其缓存/去重/fetch_content能力）
            llm: 大语言模型实例
            json_parser: JSON解析器（容错修复）
            max_depth: 最大递归深度
            results_per_search: 每轮搜索结果数上限
            max_insights: 最大洞察数上限（达到后停止递归）
            time_limit: 总超时(秒)，超时熔断返回部分结果
            budget_tracker: 工具调用预算追踪器(可选,None时跳过预算检查)
        """
        super().__init__()
        self._search_tool = search_tool
        self._llm = llm
        self._json_parser = json_parser
        self._max_depth = max_depth
        self._results_per_search = results_per_search
        self._max_insights = max_insights
        self._time_limit = time_limit
        self._budget_tracker = budget_tracker

    @tool(
        name="deep_research",
        description="深度研究工具。对复杂问题进行多轮搜索+网页正文抓取+LLM洞察抽取，产出分档研究摘要。适用于需要综合多源信息的问题，简单事实查询请用 search_web。",
        parameters={
            "query": {
                "type": "string",
                "description": "研究主题或问题，使用与用户消息相同的语言。"
            },
            "max_depth": {
                "type": "integer",
                "description": "（可选）最大递归深度，默认2，范围1-5。深度越大研究越深入但耗时越长。"
            },
            "results_per_search": {
                "type": "integer",
                "description": "（可选）每轮搜索结果数，默认5，范围1-20。"
            }
        },
        required=["query"]
    )
    async def deep_research(
            self,
            query: str,
            max_depth: Optional[int] = None,
            results_per_search: Optional[int] = None,
    ) -> ToolResult[ResearchSummary]:
        """执行深度研究，超时熔断返回部分结果"""
        # 工具调用预算检查(project_memory: deep_research=2 会话级上限)
        # deep_research 单次即多轮递归搜索,2次足够覆盖主题,超限硬拦截
        if self._budget_tracker and self._budget_tracker.is_exceeded("deep_research"):
            count = self._budget_tracker.get_count("deep_research")
            budget = self._budget_tracker.get_budget("deep_research")
            logger.info(f"deep_research 调用次数已达上限: {count}/{budget}, 拒绝调用")
            # Batch 39 / 方向3: 标记超限事件,供 BaseAgent 消费联动 metrics
            self._budget_tracker.mark_exceeded("deep_research")
            return ToolResult(
                success=False,
                message=(
                    f"deep_research 调用次数已达会话上限({count}/{budget})。"
                    f"深度研究单次即多轮递归搜索,已积累足够信息。"
                    f"请基于已有研究结果综合分析,或切换策略(如使用 search_web "
                    f"补充特定关键词、browser_navigate 访问具体URL)。"
                ),
            )

        ctx = ResearchContext(
            query=query,
            max_depth=max_depth if max_depth is not None else self._max_depth,
        )
        effective_results = results_per_search if results_per_search is not None else self._results_per_search
        try:
            await asyncio.wait_for(
                self._research_graph(ctx, effective_results),
                timeout=self._time_limit,
            )
        except asyncio.TimeoutError:
            logger.warning(f"深度研究超时熔断(>{self._time_limit}s): query={query}")
        except Exception as e:
            logger.exception(f"深度研究异常: query={query}, error={str(e)}")

        # 预算计数(无论成功/超时/异常都计入,避免LLM反复重试超时任务)
        if self._budget_tracker:
            self._budget_tracker.increment("deep_research")
            self._budget_tracker.check_and_warn("deep_research")

        return ToolResult(success=True, data=ResearchSummary.from_context(ctx))

    async def _research_graph(self, ctx: ResearchContext, results_per_search: int) -> None:
        """递归研究图：终止检查→搜索→抽取洞察→生成后续查询→并发递归"""
        # 1.终止检查
        if ctx.current_depth >= ctx.max_depth:
            return
        if len(ctx.insights) >= self._max_insights:
            return

        # 2.生成查询（首轮用原query，后续轮用LLM优化）
        if ctx.current_depth == 0:
            search_query = ctx.query
        else:
            search_query = await self._generate_optimized_query(ctx)
            if not search_query:
                return

        # 3.调用SearchTool抓取正文（复用其缓存/去重/fetch能力）
        # 通过 _search_internal 入口调用: 跳过 search_web 预算检查与计数,
        # 由 deep_research 自身预算(=2)控制总体调用频率
        # (project_memory: deep_research=2 会话级上限,内部递归调用不应消耗 search_web 预算)
        result = await self._search_tool._search_internal(
            query=search_query,
            fetch_content=True,
        )
        if not result.success or not result.data or not result.data.results:
            return

        # 4.并发抽取洞察(Semaphore控制并发,避免LLM限流)
        items_to_process = []
        for item in result.data.results[:results_per_search]:
            if len(ctx.insights) >= self._max_insights:
                break
            if item.url in ctx.visited_urls:
                continue
            ctx.visited_urls.add(item.url)
            items_to_process.append(item)

        if items_to_process:
            semaphore = asyncio.Semaphore(_INSIGHT_EXTRACTION_CONCURRENCY)
            extract_tasks = [
                self._extract_insights_concurrent(item, ctx, semaphore)
                for item in items_to_process
            ]
            await asyncio.gather(*extract_tasks, return_exceptions=True)

        # 5.生成后续查询并并发递归
        follow_ups = await self._generate_follow_ups(ctx)
        if not follow_ups:
            return
        ctx.follow_up_queries = follow_ups

        # 6.对前2个follow_up并发递归
        top_follow_ups = follow_ups[:_MAX_FOLLOW_UPS_TO_RECURSE]
        if not top_follow_ups:
            return

        sub_contexts = []
        for fu in top_follow_ups:
            sub_ctx = ResearchContext(
                query=fu,
                insights=ctx.insights,
                follow_up_queries=[],
                visited_urls=ctx.visited_urls,
                current_depth=ctx.current_depth + 1,
                max_depth=ctx.max_depth,
            )
            sub_contexts.append(sub_ctx)

        await asyncio.gather(*[self._research_graph(s, results_per_search) for s in sub_contexts])

        # 7.合并子上下文洞察回主上下文（去重）
        for sub in sub_contexts:
            for insight in sub.insights:
                if insight not in ctx.insights:
                    ctx.insights.append(insight)

    async def _generate_optimized_query(self, ctx: ResearchContext) -> str:
        """LLM优化查询：基于已有洞察生成更精准的后续查询"""
        messages = [
            {
                "role": "system",
                "content": "你是研究助手。根据已有洞察优化搜索查询，使其更精准地补充缺失信息。只返回优化后的查询字符串，不要解释。",
            },
            {
                "role": "user",
                "content": f"研究主题：{ctx.query}\n已有洞察：{self._format_insights_summary(ctx.insights)}\n请生成一个优化查询：",
            },
        ]
        try:
            response = await self._llm.invoke(messages=messages, response_format=None)
            content = (response or {}).get("content", "").strip()
            return content[:200] if content else ""
        except Exception as e:
            logger.warning(f"生成优化查询失败，回退原query: {str(e)}")
            return ctx.query

    async def _extract_insights_concurrent(
            self, item: SearchResultItem, ctx: ResearchContext, semaphore: asyncio.Semaphore,
    ) -> None:
        """并发安全的洞察抽取(带信号量控制)

        二次检查insight上限(并发场景下可能已满),避免无效LLM调用。
        """
        async with semaphore:
            if len(ctx.insights) >= self._max_insights:
                return
            await self._extract_insights(item, ctx)

    async def _extract_insights(self, item: SearchResultItem, ctx: ResearchContext) -> None:
        """LLM从单条搜索结果正文抽取洞察，解析失败用兜底评分"""
        content = item.content or item.snippet
        if not content:
            return

        messages = [
            {
                "role": "system",
                "content": "你是研究助手。从网页正文中抽取与研究主题相关的关键洞察，并评估相关性。返回JSON格式：{\"insights\": [{\"content\": \"洞察内容\", \"relevance_score\": 0.0-1.0}]}",
            },
            {
                "role": "user",
                "content": f"研究主题：{ctx.query}\n来源标题：{item.title}\n来源URL：{item.url}\n网页正文：\n{content[:3000]}",
            },
        ]
        try:
            response = await self._llm.invoke(
                messages=messages,
                response_format={"type": "json_object"},
            )
            text = (response or {}).get("content", "")
            if not text:
                return
            parsed = await self._json_parser.invoke(text, default_value={})
            insights_data = (parsed or {}).get("insights", []) if isinstance(parsed, dict) else []
            for insight_data in insights_data:
                if len(ctx.insights) >= self._max_insights:
                    break
                insight = self._build_insight(insight_data, item)
                if insight:
                    ctx.insights.append(insight)
        except Exception as e:
            logger.debug(f"LLM抽取洞察失败，使用兜底评分: url={item.url}, error={str(e)}")
            ctx.insights.append(ResearchInsight(
                content=content[:500],
                source_url=item.url,
                source_title=item.title,
                relevance_score=_FALLBACK_RELEVANCE_SCORE,
            ))

    @staticmethod
    def _build_insight(data: Any, item: SearchResultItem) -> Optional[ResearchInsight]:
        """从LLM返回的dict构建ResearchInsight，字段缺失或越界返回None"""
        if not isinstance(data, dict):
            return None
        content = str(data.get("content", "")).strip()
        if not content:
            return None
        try:
            score = float(data.get("relevance_score", _FALLBACK_RELEVANCE_SCORE))
        except (TypeError, ValueError):
            score = _FALLBACK_RELEVANCE_SCORE
        score = max(0.0, min(1.0, score))
        return ResearchInsight(
            content=content,
            source_url=item.url,
            source_title=item.title,
            relevance_score=score,
        )

    async def _generate_follow_ups(self, ctx: ResearchContext) -> List[str]:
        """LLM生成后续研究查询，最多3个"""
        messages = [
            {
                "role": "system",
                "content": "你是研究助手。根据研究主题和已有洞察，生成后续研究查询以补充缺失信息。返回JSON格式：{\"follow_ups\": [\"查询1\", \"查询2\", \"查询3\"]}，最多3个。",
            },
            {
                "role": "user",
                "content": f"研究主题：{ctx.query}\n已有洞察：{self._format_insights_summary(ctx.insights)}\n请生成后续查询：",
            },
        ]
        try:
            response = await self._llm.invoke(
                messages=messages,
                response_format={"type": "json_object"},
            )
            text = (response or {}).get("content", "")
            if not text:
                return []
            parsed = await self._json_parser.invoke(text, default_value={})
            follow_ups = (parsed or {}).get("follow_ups", []) if isinstance(parsed, dict) else []
            return [str(q).strip() for q in follow_ups if str(q).strip()][:_MAX_FOLLOW_UPS_TO_GENERATE]
        except Exception as e:
            logger.warning(f"生成后续查询失败: {str(e)}")
            return []

    @staticmethod
    def _format_insights_summary(insights: List[ResearchInsight]) -> str:
        """将洞察列表格式化为简洁文本摘要，供LLM上下文使用"""
        if not insights:
            return "（暂无洞察）"
        return "\n".join(f"- {i.content[:100]}" for i in insights[:5])
