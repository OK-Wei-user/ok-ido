#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/25 10:14

@File    : search.py
"""
import logging
import re
from typing import Any, Coroutine, List, Optional
from urllib.parse import urlparse

from app.domain.external.search import ContentFetcher, SearchEngine
from app.domain.models.search import SearchResultItem, SearchResults
from app.domain.models.tool_result import ToolResult
from app.infrastructure.external.search.query_refiner import QueryRefiner
from app.infrastructure.storage.search_cache import SearchCache
from .base import BaseTool, tool
from .budget_tracker import ToolBudgetTracker

logger = logging.getLogger(__name__)

_DEFAULT_MAX_FETCH = 5  # 默认单次搜索抓取正文URL数上限，防雪崩

# 查询相似度阈值(project_memory: 相似度 ≥ 0.6 视为重复查询)
_QUERY_SIMILARITY_THRESHOLD = 0.6

# 低质量结果过滤配置(project_memory: 空内容域名 + 百科单分词释义)
# 空内容域名: 这些域名的内容往往为导航页/工具集合,与具体查询无关
_LOW_QUALITY_DOMAINS = frozenset({
    "ai-bot.cn",  # AI 工具集合页,与具体查询无关
})

# 百科域名(单分词释义过滤): 标题仅为查询单一分词时视为百科单分词释义
_BAIKE_DOMAINS = frozenset({
    "baike.baidu.com",
    "zh.wikipedia.org",
    "en.wikipedia.org",
})

# 中文单字/单分词模式(匹配百科单分词释义: 标题仅为查询的单一分词)
_SINGLE_TOKEN_PATTERN = re.compile(r"^[\u4e00-\u9fa5]{1,2}$")


class SearchTool(BaseTool):
    """搜索工具包，提供与搜索引擎交互的能力

    支持能力：
    - 关键词精简（QueryRefiner）
    - URL规范化去重（基于dedup_key保留首条）
    - Redis缓存（命中跳过搜索引擎调用）
    - 按需抓取网页正文（fetch_content=True时启用，深度研究场景使用）
    - 工具调用预算会话级追踪(search_web=8,project_memory硬约束)
      * 预占式预算: check+increment原子完成(无await间隙),解决并发竞态
      * 失败回退: 重复查询/搜索引擎失败时decrement,让LLM可重试
      * _internal参数: deep_research内部调用跳过预算(由其自身预算控制)
    - 查询去重(相似度 ≥ 0.6 视为重复,跳过搜索引擎调用)
    - 低质量结果过滤(空内容域名 + 百科单分词释义)
    """
    name: str = "search"

    def __init__(
            self,
            search_engine: SearchEngine,
            content_fetcher: Optional[ContentFetcher] = None,
            cache: Optional[SearchCache] = None,
            max_fetch: int = _DEFAULT_MAX_FETCH,
            budget_tracker: Optional[ToolBudgetTracker] = None,
    ) -> None:
        """构造函数，完成搜索工具包初始化

        Args:
            search_engine: 搜索引擎实例
            content_fetcher: 网页正文抓取器（可选，None时跳过正文抓取）
            cache: 搜索结果缓存（可选，None时跳过缓存）
            max_fetch: 单次搜索抓取正文URL数上限
            budget_tracker: 工具调用预算追踪器(可选,None时跳过预算检查)
        """
        super().__init__()
        self.search_engine = search_engine
        self._content_fetcher = content_fetcher
        self._cache = cache
        self._max_fetch = max_fetch
        self._query_refiner = QueryRefiner()
        self._budget_tracker = budget_tracker
        # 会话级查询历史(用于查询相似度去重)
        self._query_history: List[str] = []

    @tool(
        name="search_web",
        description="全网搜索引擎工具。当需要获取实时信息（如突发新闻、天气）、补充内部知识库未涵盖的内容或进行事实核查时使用。该工具会返回相关的网页摘要和链接。",
        parameters={
            "query": {
                "type": "string",
                "description": "搜索引擎查询字符串。必须遵守以下规则：1.必须使用与用户消息相同的语言（用户用中文则用中文搜索，用户用英文则用英文搜索）；2.仅提取2-4个核心关键词，用空格分隔，严禁超过4个词；3.禁止使用完整自然语言问句。示例：'今天北京的天气怎么样'→'北京 天气'，'2025年中国GDP增长率'→'中国 GDP 增长率 2025'，'best AI tools 2026'→'best AI tools 2026'。"
            },
            "date_range": {
                "type": "string",
                "enum": ["all", "past_hour", "past_day", "past_week", "past_month", "past_year"],
                "description": "（可选）搜索结果的时间范围过滤。当用户询问特定时效性的新闻或事件时（如'昨天'、'上周'），必须指定此参数。默认为 'all'。"
            },
            "fetch_content": {
                "type": "boolean",
                "description": "（可选）是否抓取搜索结果URL的网页正文，深度研究场景开启。默认为 false，仅返回摘要。"
            }
        },
        required=["query"]
    )
    async def search_web(
            self,
            query: str,
            date_range: Optional[str] = None,
            fetch_content: bool = False,
    ) -> ToolResult[SearchResults]:
        """LLM 对外入口: 调用搜索引擎获取搜索结果(受 search_web 预算约束)

        采用预占式预算(check+increment原子完成,无await间隙),
        解决 LLM 并行工具调用 + deep_research 并发递归导致的并发竞态问题。
        失败回退(重复查询/搜索引擎失败时decrement,让LLM可重试)。
        """
        refined_query = self._query_refiner.refine(query)

        # 0.工具调用预算预占(project_memory: search_web=8 会话级上限)
        # 预占式: 检查+计数原子完成(无await间隙),解决并发竞态导致的 count 超限问题。
        budget_occupied = False
        if self._budget_tracker:
            if self._budget_tracker.is_exceeded("search_web"):
                count = self._budget_tracker.get_count("search_web")
                budget = self._budget_tracker.get_budget("search_web")
                logger.info(f"search_web 调用次数已达上限: {count}/{budget}, 拒绝调用")
                # Batch 39 / 方向3: 标记超限事件,供 BaseAgent 消费联动 metrics
                self._budget_tracker.mark_exceeded("search_web")
                return ToolResult(
                    success=False,
                    message=(
                        f"search_web 调用次数已达会话上限({count}/{budget})。"
                        f"请基于已有搜索结果综合分析,或切换策略(如使用 deep_research、"
                        f"browser_navigate 访问具体URL)。"
                    ),
                )
            # 立即预占名额(同步操作,无await间隙,避免并发竞态)
            self._budget_tracker.increment("search_web")
            # Batch 39: check_and_warn 统一由 BaseAgent._invoke_tool 调用(联动 metrics)
            budget_occupied = True

        # 委托核心搜索逻辑(预占失败时由 _search_web_core 回退)
        return await self._search_web_core(
            refined_query=refined_query,
            date_range=date_range,
            fetch_content=fetch_content,
            budget_occupied=budget_occupied,
        )

    async def _search_web_core(
            self,
            refined_query: str,
            date_range: Optional[str] = None,
            fetch_content: bool = False,
            budget_occupied: bool = False,
    ) -> ToolResult[SearchResults]:
        """核心搜索逻辑(供 search_web 和 deep_research 复用)

        分离设计目的:
        - search_web: LLM 对外入口,受 search_web 预算约束(预占式)
        - _search_web_core: deep_research 内部调用入口,跳过 search_web 预算
          (deep_research 自有预算=2 已控制总体调用频率,内部递归不应消耗 search_web 预算)
        - 通过方法分离而非参数标记,避免 LLM 幻觉传参绕过预算(@tool 装饰器只暴露 search_web)

        Args:
            refined_query: 已精简的查询字符串(由调用方负责精简)
            date_range: 时间范围过滤
            fetch_content: 是否抓取正文
            budget_occupied: 是否已预占 search_web 预算(True=search_web调用,需失败回退;
                False=deep_research内部调用,无预算可回退)

        Returns:
            搜索结果 ToolResult
        """
        # 1.查询相似度去重(project_memory: 相似度 ≥ 0.6 视为重复)
        similar = self._find_similar_query(refined_query)
        if similar is not None:
            logger.info(f"查询相似度去重: '{refined_query}' 与历史 '{similar}' 相似度 ≥ {_QUERY_SIMILARITY_THRESHOLD}, 跳过")
            # 重复查询回退预占(未实际调用搜索引擎,不消耗预算)
            if budget_occupied and self._budget_tracker:
                self._budget_tracker.decrement("search_web")
            return ToolResult(
                success=False,
                message=(
                    f"查询与历史查询过于相似(历史: '{similar}'),"
                    f"请基于已有结果综合分析,或换用更具体的关键词。"
                ),
            )
        self._query_history.append(refined_query)

        # 2.缓存命中检查
        if self._cache:
            cached = await self._cache.get(refined_query, date_range)
            if cached:
                logger.debug(f"搜索缓存命中: query={refined_query}")
                # 缓存命中保留预占(缓存命中也计入预算,避免LLM反复查缓存绕过预算)
                return ToolResult(success=True, data=cached)

        # 3.调用搜索引擎
        result = await self.search_engine.invoke(refined_query, date_range)
        if not result.success:
            # 调用失败回退预占(让LLM可重试,不消耗预算)
            if budget_occupied and self._budget_tracker:
                self._budget_tracker.decrement("search_web")
            return result

        # 4.预算计数已在预占阶段完成(此处不再重复increment)

        # 5.URL规范化去重：基于dedup_key保留首次出现
        result.data.results = self._dedup_results(result.data.results)

        # 6.低质量结果过滤(project_memory: 空内容域名 + 百科单分词释义)
        before_filter = len(result.data.results)
        result.data.results = self._filter_low_quality_results(
            result.data.results, refined_query
        )
        filtered_count = before_filter - len(result.data.results)
        if filtered_count > 0:
            logger.debug(
                f"低质量结果过滤: 查询='{refined_query}', 过滤{filtered_count}条"
            )

        # 7.按需抓取正文（仅前max_fetch个URL）
        if fetch_content and self._content_fetcher:
            await self._fetch_content_for_results(result.data.results)

        # 8.写回缓存
        if self._cache:
            await self._cache.set(result.data)

        return result

    def _search_internal(
            self,
            query: str,
            date_range: Optional[str] = None,
            fetch_content: bool = False,
    ) -> "Coroutine[Any, Any, ToolResult[SearchResults]]":
        """deep_research 内部调用入口(跳过 search_web 预算)

        设计目的: deep_research 自有预算(=2)已控制总体调用频率,
        内部递归调用不应消耗 search_web 预算,否则 2 次 deep_research
        至少消耗 6 次 search_web 预算,挤压 LLM 直接调用额度。

        通过独立方法(而非 _internal 参数)实现,避免 LLM 幻觉传参绕过预算。
        由 deep_research._research_graph 调用。

        Args:
            query: 原始查询字符串(内部自行精简)
            date_range: 时间范围过滤
            fetch_content: 是否抓取正文
        """
        refined_query = self._query_refiner.refine(query)
        return self._search_web_core(
            refined_query=refined_query,
            date_range=date_range,
            fetch_content=fetch_content,
            budget_occupied=False,  # 内部调用: 不预占 search_web 预算
        )

    def _find_similar_query(self, query: str) -> Optional[str]:
        """查找与历史查询相似度 ≥ 阈值的查询(基于 Jaccard 相似度)

        project_memory: 相似度 ≥ 0.6 视为重复查询,跳过搜索引擎调用。
        使用 Jaccard 相似度(分词集合交集/并集),计算简单且对短查询效果好。

        Args:
            query: 待检查的查询(refined 后)

        Returns:
            相似的历史查询字符串,无相似查询返回 None
        """
        if not self._query_history or not query:
            return None
        query_tokens = self._tokenize_query(query)
        if not query_tokens:
            return None
        for hist_query in self._query_history:
            hist_tokens = self._tokenize_query(hist_query)
            if not hist_tokens:
                continue
            # Jaccard 相似度 = 交集大小 / 并集大小
            intersection = len(query_tokens & hist_tokens)
            union = len(query_tokens | hist_tokens)
            if union == 0:
                continue
            similarity = intersection / union
            if similarity >= _QUERY_SIMILARITY_THRESHOLD:
                return hist_query
        return None

    @staticmethod
    def _tokenize_query(query: str) -> set:
        """查询分词(支持空格分隔 + 中文单字)

        简单分词策略,避免引入 jieba 等重依赖:
        - 按空格/标点分隔
        - 中文连续字符按单字切分(粗粒度,足够检测相似查询)
        """
        if not query:
            return set()
        # 按非字母数字汉字字符分隔
        parts = re.split(r"[^\w\u4e00-\u9fa5]+", query.lower())
        tokens: set = set()
        for part in parts:
            if not part:
                continue
            # 中文连续字符按单字切分
            if re.fullmatch(r"[\u4e00-\u9fa5]+", part):
                tokens.update(ch for ch in part)
            else:
                tokens.add(part)
        # 过滤过短的分词(单字英文无意义)
        return {t for t in tokens if len(t) >= 2 or re.match(r"[\u4e00-\u9fa5]", t)}

    def _filter_low_quality_results(
            self,
            items: List[SearchResultItem],
            query: str,
    ) -> List[SearchResultItem]:
        """低质量结果过滤(project_memory: 空内容域名 + 百科单分词释义)

        过滤规则:
        1. 空内容域名: 域名命中 _LOW_QUALITY_DOMAINS 时剔除(导航页/工具集合页)
        2. 百科单分词释义: 百科域名 + 标题仅为查询的单一分词时剔除
           (如查"2026 AI"返回"年(汉语文字)_百度百科"视为百科单分词释义)

        Args:
            items: 搜索结果列表
            query: 原始查询(refined 后,用于百科单分词判断)

        Returns:
            过滤后的结果列表
        """
        if not items:
            return items
        query_tokens = self._tokenize_query(query)
        filtered: List[SearchResultItem] = []
        for item in items:
            domain = self._extract_domain(item.url)
            if not domain:
                filtered.append(item)
                continue
            # 1.空内容域名过滤
            if domain in _LOW_QUALITY_DOMAINS:
                continue
            # 2.百科单分词释义过滤: 百科域名 + 标题仅 1-2 个中文字符且不在查询 token 中
            if domain in _BAIKE_DOMAINS:
                title = (item.title or "").strip()
                # 标题仅为 1-2 个中文字符(如"年"、"AI"等单分词)
                if _SINGLE_TOKEN_PATTERN.match(title):
                    # 检查标题是否与查询主题相关(标题字符是否在查询 token 中)
                    title_in_query = any(ch in query_tokens for ch in title)
                    if not title_in_query:
                        continue
            filtered.append(item)
        return filtered

    @staticmethod
    def _extract_domain(url: str) -> str:
        """从 URL 提取主域名(用于低质量过滤)"""
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            return parsed.netloc.lower()
        except Exception:
            return ""

    def _dedup_results(self, items: List[SearchResultItem]) -> List[SearchResultItem]:
        """基于dedup_key去重，保留首次出现；dedup_key为空时保留条目"""
        seen: set = set()
        unique: List[SearchResultItem] = []
        for item in items:
            key = item.dedup_key
            if not key or key not in seen:
                if key:
                    seen.add(key)
                unique.append(item)
        return unique

    async def _fetch_content_for_results(self, items: List[SearchResultItem]) -> None:
        """对前max_fetch条结果抓取正文，失败时保持item.content=None"""
        targets = items[: self._max_fetch]
        urls = [item.url for item in targets]
        if not urls:
            return
        try:
            text_results = await self._content_fetcher.fetch_many(urls)
            for item, text_result in zip(targets, text_results):
                if text_result.success and text_result.data:
                    item.content = text_result.data
        except Exception as e:
            logger.warning(f"批量抓取网页正文失败: {str(e)}")
