#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_search_budget_integration.py
SearchTool 预算/去重/低质量过滤集成测试

验证 project_memory 硬约束在 SearchTool 中的集成:
- 工具调用预算: search_web=8 会话级上限,超限返回错误 ToolResult
- 预占式预算: check+increment原子完成,解决并发竞态(无await间隙)
- 失败回退: 重复查询/搜索引擎失败时decrement,让LLM可重试
- deep_research内部调用: _search_internal 跳过 search_web 预算
- 查询相似度去重: Jaccard 相似度 ≥ 0.6 视为重复
- 低质量结果过滤: 空内容域名 + 百科单分词释义
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.domain.models.search import SearchResultItem, SearchResults
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.budget_tracker import ToolBudgetTracker
from app.domain.services.tools.search import SearchTool


def _make_item(url, title, snippet=""):
    return SearchResultItem(url=url, title=title, snippet=snippet)


def _make_results(items, query="test", date_range=None):
    return SearchResults(query=query, date_range=date_range, total_results=len(items), results=items)


def _make_engine_returning(items):
    """构造返回指定 items 的搜索引擎 mock"""
    engine = MagicMock()
    engine.invoke = AsyncMock(return_value=ToolResult(
        success=True,
        data=_make_results(items),
    ))
    return engine


def _make_slow_engine(items, delay=0.05):
    """构造带延迟的搜索引擎 mock(模拟并发场景下的await间隙)"""
    async def _slow_invoke(query, date_range=None):
        await asyncio.sleep(delay)
        return ToolResult(success=True, data=_make_results(items))
    engine = MagicMock()
    engine.invoke = _slow_invoke
    return engine


class TestSearchBudgetIntegration:
    """SearchTool 与 ToolBudgetTracker 集成测试"""

    @pytest.mark.asyncio
    async def test_budget_exceeded_returns_error(self):
        """search_web 达8次后,第9次返回错误 ToolResult"""
        engine = _make_engine_returning([_make_item("https://a.com", "A")])
        tracker = ToolBudgetTracker()
        # 预算上限为8,预先消耗8次
        for _ in range(8):
            tracker.increment("search_web")

        tool = SearchTool(search_engine=engine, budget_tracker=tracker)
        result = await tool.search_web("test query")

        assert result.success is False
        assert "上限" in result.message
        # 搜索引擎不应被调用
        engine.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_budget_counted_on_success(self):
        """search_web 成功后计数+1"""
        engine = _make_engine_returning([_make_item("https://a.com", "A")])
        tracker = ToolBudgetTracker()

        tool = SearchTool(search_engine=engine, budget_tracker=tracker)
        await tool.search_web("test query")

        assert tracker.get_count("search_web") == 1

    @pytest.mark.asyncio
    async def test_budget_not_counted_on_engine_failure(self):
        """搜索引擎调用失败时不计入预算(允许 LLM 重试)"""
        engine = MagicMock()
        engine.invoke = AsyncMock(return_value=ToolResult(success=False, message="engine error"))
        tracker = ToolBudgetTracker()

        tool = SearchTool(search_engine=engine, budget_tracker=tracker)
        await tool.search_web("test query")

        assert tracker.get_count("search_web") == 0

    @pytest.mark.asyncio
    async def test_budget_counted_on_cache_hit(self):
        """缓存命中也计入预算(避免LLM反复查缓存绕过预算)"""
        cached_results = _make_results([_make_item("https://cached.com", "Cached")])
        cache = MagicMock()
        cache.get = AsyncMock(return_value=cached_results)
        engine = _make_engine_returning([_make_item("https://fresh.com", "Fresh")])
        tracker = ToolBudgetTracker()

        tool = SearchTool(search_engine=engine, cache=cache, budget_tracker=tracker)
        await tool.search_web("test query")

        assert tracker.get_count("search_web") == 1
        # 缓存命中,搜索引擎不应被调用
        engine.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_budget_rollback_on_duplicate_query(self):
        """重复查询时回退预占(不消耗预算)"""
        engine = _make_engine_returning([_make_item("https://a.com", "A")])
        tracker = ToolBudgetTracker()
        tool = SearchTool(search_engine=engine, budget_tracker=tracker)

        # 第一次调用: 成功,计数+1
        await tool.search_web("人工智能趋势")
        assert tracker.get_count("search_web") == 1

        # 第二次调用相似查询: 被去重拒绝,应回退预占(计数不变)
        result = await tool.search_web("人工智能趋势发展")
        assert result.success is False
        assert "相似" in result.message
        # 重复查询回退预占,计数仍为1
        assert tracker.get_count("search_web") == 1


class TestPreOccupyConcurrencySafety:
    """预占式预算并发竞态测试

    场景: LLM 并行调用多个 search_web(BaseAgent 支持并行工具调用),
    或 deep_research 内部并发递归调用 _search_internal。
    旧实现(check+increment之间有await间隙)会导致 count 超过 budget。
    新实现(预占式: check+increment原子完成)解决此问题。
    """

    @pytest.mark.asyncio
    async def test_parallel_calls_respect_budget(self):
        """并行调用不超预算上限(预占式解决竞态)"""
        # 使用慢引擎模拟await间隙,放大竞态窗口
        engine = _make_slow_engine([_make_item("https://a.com", "A")], delay=0.05)
        tracker = ToolBudgetTracker()
        tool = SearchTool(search_engine=engine, budget_tracker=tracker)

        # 并发发起10次调用(预算=8,应只有8次成功,2次被拒绝)
        tasks = [tool.search_web(f"query_{i}") for i in range(10)]
        results = await asyncio.gather(*tasks)

        success_count = sum(1 for r in results if r.success)
        failure_count = sum(1 for r in results if not r.success)

        # 成功次数不超过预算8(预占式保证)
        assert success_count <= 8, f"成功次数{success_count}超过预算8(并发竞态未修复)"
        # 失败次数 = 10 - 成功次数
        assert failure_count == 10 - success_count
        # 计数器不超过预算8
        assert tracker.get_count("search_web") <= 8

    @pytest.mark.asyncio
    async def test_parallel_calls_at_budget_boundary(self):
        """恰好8个并行调用全部成功(边界场景)"""
        engine = _make_slow_engine([_make_item("https://a.com", "A")], delay=0.02)
        tracker = ToolBudgetTracker()
        tool = SearchTool(search_engine=engine, budget_tracker=tracker)

        # 8个并行调用(恰好等于预算)
        tasks = [tool.search_web(f"query_{i}") for i in range(8)]
        results = await asyncio.gather(*tasks)

        success_count = sum(1 for r in results if r.success)
        assert success_count == 8, f"8个并行调用应全部成功,实际{success_count}"
        assert tracker.get_count("search_web") == 8

    @pytest.mark.asyncio
    async def test_sequential_calls_exceed_budget_returns_error(self):
        """串行调用超预算后返回错误(基础场景回归)"""
        engine = _make_engine_returning([_make_item("https://a.com", "A")])
        tracker = ToolBudgetTracker()
        tool = SearchTool(search_engine=engine, budget_tracker=tracker)

        # 8次串行调用全部成功
        for i in range(8):
            result = await tool.search_web(f"query_{i}")
            assert result.success, f"第{i+1}次调用应成功"

        # 第9次应被拒绝
        result = await tool.search_web("query_8")
        assert result.success is False
        assert "上限" in result.message


class TestSearchInternal:
    """_search_internal deep_research内部调用入口测试

    验证 deep_research 内部递归调用不消耗 search_web 预算:
    - deep_research 自有预算(=2)已控制总体频率
    - 内部递归调用不应消耗 search_web 预算(否则2次deep_research消耗6+次search_web预算)
    """

    @pytest.mark.asyncio
    async def test_search_internal_not_counted_in_search_web_budget(self):
        """_search_internal 不消耗 search_web 预算"""
        engine = _make_engine_returning([_make_item("https://a.com", "A")])
        tracker = ToolBudgetTracker()
        tool = SearchTool(search_engine=engine, budget_tracker=tracker)

        # 通过 _search_internal 调用5次(超过 search_web 预算8?不,_search_internal不计数)
        for i in range(5):
            result = await tool._search_internal(f"query_{i}")
            assert result.success

        # search_web 预算未消耗(_search_internal 跳过预算)
        assert tracker.get_count("search_web") == 0
        # 仍可继续通过 search_web 调用(预算未达上限)
        assert tracker.is_exceeded("search_web") is False

    @pytest.mark.asyncio
    async def test_search_internal_still_uses_dedup(self):
        """_search_internal 仍受查询去重约束(避免重复查询)"""
        engine = _make_engine_returning([_make_item("https://a.com", "A")])
        tool = SearchTool(search_engine=engine)

        # 第一次调用成功
        result1 = await tool._search_internal("人工智能趋势")
        assert result1.success

        # 第二次相似查询应被去重拒绝
        result2 = await tool._search_internal("人工智能趋势发展")
        assert result2.success is False
        assert "相似" in result2.message

    @pytest.mark.asyncio
    async def test_search_internal_still_uses_cache(self):
        """_search_internal 仍使用缓存(命中跳过搜索引擎)"""
        cached_results = _make_results([_make_item("https://cached.com", "Cached")])
        cache = MagicMock()
        cache.get = AsyncMock(return_value=cached_results)
        engine = _make_engine_returning([_make_item("https://fresh.com", "Fresh")])

        tool = SearchTool(search_engine=engine, cache=cache)
        result = await tool._search_internal("test query")

        assert result.success
        # 缓存命中,搜索引擎不应被调用
        engine.invoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_internal_not_exposed_to_llm(self):
        """_search_internal 不在 LLM 可见的工具schema中(LLM无法绕过预算)"""
        engine = MagicMock()
        tool = SearchTool(search_engine=engine)

        # 获取工具列表(get_tools 返回 OpenAI 工具 schema 列表)
        tools_schema = tool.get_tools()
        # 仅暴露 search_web,不暴露 _search_internal
        tool_names = [t["function"]["name"] for t in tools_schema]
        assert "search_web" in tool_names
        assert "_search_internal" not in tool_names

        # search_web 的参数不应包含 budget_occupied(防止LLM幻觉传参绕过预算)
        search_web_schema = next(
            t for t in tools_schema if t["function"]["name"] == "search_web"
        )
        search_web_params = search_web_schema["function"]["parameters"]["properties"]
        assert "budget_occupied" not in search_web_params
        # 仅暴露 query/date_range/fetch_content
        assert set(search_web_params.keys()) == {"query", "date_range", "fetch_content"}

    @pytest.mark.asyncio
    async def test_search_internal_engine_failure_no_rollback(self):
        """_search_internal 搜索引擎失败时不回退(未预占预算,无名额可退)"""
        engine = MagicMock()
        engine.invoke = AsyncMock(return_value=ToolResult(success=False, message="error"))
        tracker = ToolBudgetTracker()
        tool = SearchTool(search_engine=engine, budget_tracker=tracker)

        result = await tool._search_internal("test query")

        assert result.success is False
        # _search_internal 未预占预算,无需回退,计数仍为0
        assert tracker.get_count("search_web") == 0


class TestQueryDeduplication:
    """查询相似度去重测试(Jaccard 相似度 ≥ 0.6)"""

    def test_tokenize_chinese_single_chars(self):
        """中文按单字切分"""
        tool = SearchTool(search_engine=MagicMock())
        tokens = tool._tokenize_query("人工智能趋势")
        assert tokens == {"人", "工", "智", "能", "趋", "势"}

    def test_tokenize_english_words(self):
        """英文按空格分词"""
        tool = SearchTool(search_engine=MagicMock())
        tokens = tool._tokenize_query("AI trends 2026")
        assert tokens == {"ai", "trends", "2026"}

    def test_tokenize_mixed(self):
        """中英混合分词"""
        tool = SearchTool(search_engine=MagicMock())
        tokens = tool._tokenize_query("AI 趋势 2026")
        assert tokens == {"ai", "趋", "势", "2026"}

    def test_find_similar_query_high_similarity(self):
        """相似度 ≥ 0.6 的查询视为重复"""
        tool = SearchTool(search_engine=MagicMock())
        tool._query_history.append("人工智能趋势")

        # 6/8 = 0.75 相似度,超过阈值
        similar = tool._find_similar_query("人工智能趋势发展")
        assert similar == "人工智能趋势"

    def test_find_similar_query_low_similarity(self):
        """相似度 < 0.6 不视为重复"""
        tool = SearchTool(search_engine=MagicMock())
        tool._query_history.append("人工智能")

        # tokens: {"人","工","智","能"} vs {"天","气"} → 交集0/并集6=0.0
        similar = tool._find_similar_query("天气")
        assert similar is None

    def test_find_similar_query_empty_history(self):
        """空历史返回 None"""
        tool = SearchTool(search_engine=MagicMock())
        assert tool._find_similar_query("any query") is None

    @pytest.mark.asyncio
    async def test_duplicate_query_returns_error(self):
        """重复查询返回错误 ToolResult,不调用搜索引擎"""
        engine = _make_engine_returning([_make_item("https://a.com", "A")])
        tool = SearchTool(search_engine=engine)

        # 第一次调用: 应成功
        await tool.search_web("人工智能趋势")
        # 第二次调用相似查询: 应被去重拒绝
        result = await tool.search_web("人工智能趋势发展")

        assert result.success is False
        assert "相似" in result.message
        # 搜索引擎仅被调用1次
        assert engine.invoke.call_count == 1


class TestLowQualityFilter:
    """低质量结果过滤测试"""

    def test_filter_empty_content_domain(self):
        """空内容域名(ai-bot.cn)被过滤"""
        tool = SearchTool(search_engine=MagicMock())
        items = [
            _make_item("https://ai-bot.cn/tool/1", "AI Tool"),
            _make_item("https://example.com/page", "Useful Page"),
        ]
        filtered = tool._filter_low_quality_results(items, "AI tool")
        assert len(filtered) == 1
        assert filtered[0].url == "https://example.com/page"

    def test_filter_baike_single_token_title(self):
        """百科单分词释义被过滤(标题仅1-2中文字符且与查询无关)"""
        tool = SearchTool(search_engine=MagicMock())
        items = [
            _make_item("https://baike.baidu.com/item/年", "年"),
            _make_item("https://example.com/page", "Useful Page"),
        ]
        # 查询: "人工智能"(tokens含"人","工","智","能"),标题"年"不在其中
        filtered = tool._filter_low_quality_results(items, "人工智能")
        assert len(filtered) == 1
        assert filtered[0].url == "https://example.com/page"

    def test_keep_baike_when_title_in_query(self):
        """百科标题与查询主题相关时保留(如查"年"时百科"年"页面)"""
        tool = SearchTool(search_engine=MagicMock())
        items = [
            _make_item("https://baike.baidu.com/item/年", "年"),
        ]
        # 查询: "年的含义",tokens含"年"
        filtered = tool._filter_low_quality_results(items, "年的含义")
        assert len(filtered) == 1

    def test_keep_normal_baike_pages(self):
        """百科正常标题(非单分词)保留"""
        tool = SearchTool(search_engine=MagicMock())
        items = [
            _make_item("https://baike.baidu.com/item/人工智能", "人工智能(计算机科学分支)"),
        ]
        filtered = tool._filter_low_quality_results(items, "人工智能")
        assert len(filtered) == 1

    def test_filter_empty_items(self):
        """空列表 no-op"""
        tool = SearchTool(search_engine=MagicMock())
        assert tool._filter_low_quality_results([], "any") == []

    def test_extract_domain(self):
        """URL 主域名提取"""
        tool = SearchTool(search_engine=MagicMock())
        assert tool._extract_domain("https://www.example.com/path") == "www.example.com"
        assert tool._extract_domain("https://baike.baidu.com/item/x") == "baike.baidu.com"
        assert tool._extract_domain("") == ""
        assert tool._extract_domain("invalid_url") == ""


class TestParallelToolMessageMerge:
    """_merge_consecutive_tool_messages 并行工具结果保留测试

    修复: 同一 assistant(tool_calls)的并行 tool 消息不应被合并,
    否则会丢失并行调用结果,导致 LLM 反复重试。
    """

    def _build_messages_with_parallel_tools(self):
        """构建并行工具调用场景

        assistant(tool_calls=[search_web_A, search_web_B])
          → tool(search_web_A, content="result A")
          → tool(search_web_B, content="result B")
        """
        return [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user question"},
            # 保护头部消息(2条) + 保护尾部消息(2条)需要填充
            {"role": "assistant", "content": "thinking"},
            {"role": "assistant", "content": "thinking2"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_A", "type": "function", "function": {"name": "search_web", "arguments": "{}"}},
                    {"id": "call_B", "type": "function", "function": {"name": "search_web", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_A", "function_name": "search_web", "content": "result A"},
            {"role": "tool", "tool_call_id": "call_B", "function_name": "search_web", "content": "result B"},
            {"role": "assistant", "content": "tail1"},
            {"role": "assistant", "content": "tail2"},
        ]

    def test_parallel_tool_messages_not_merged(self):
        """同一 assistant 的并行 tool 消息保留(不被合并)"""
        from app.domain.models.memory import Memory
        memory = Memory(messages=self._build_messages_with_parallel_tools())
        memory._merge_consecutive_tool_messages()

        # 应保留2条 tool 消息(并行调用结果)
        tool_msgs = [m for m in memory.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        # 两条 tool 消息的内容均存在(未被覆盖)
        contents = {m["content"] for m in tool_msgs}
        assert "result A" in contents
        assert "result B" in contents

    def _build_messages_with_serial_tools_different_assistants(self):
        """构建串行工具调用场景(不同 assistant 的同 fn tool 消息)

        assistant(tool_calls=[shell_exec_1]) → tool(shell_exec_1)
        assistant(tool_calls=[shell_exec_2]) → tool(shell_exec_2)
        注: 中间隔着 assistant,实际不会紧邻,此场景仅用于验证保守不合并
        """
        return [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user question"},
            {"role": "assistant", "content": "thinking"},
            {"role": "assistant", "content": "thinking2"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "shell_exec", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "function_name": "shell_exec", "content": "result 1"},
            {"role": "assistant", "content": "intermediate"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_2", "type": "function", "function": {"name": "shell_exec", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_2", "function_name": "shell_exec", "content": "result 2"},
            {"role": "assistant", "content": "tail1"},
            {"role": "assistant", "content": "tail2"},
        ]

    def test_serial_tools_different_assistants_not_merged(self):
        """不同 assistant 的同 fn tool 消息中间有 assistant,实际不会触发合并"""
        from app.domain.models.memory import Memory
        memory = Memory(messages=self._build_messages_with_serial_tools_different_assistants())
        memory._merge_consecutive_tool_messages()

        # 两条 tool 消息均保留(中间有 assistant 隔开,不触发合并)
        tool_msgs = [m for m in memory.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 2

    def test_protect_head_tail_short_circuit(self):
        """消息数 ≤ head+tail 时不压缩(保护头部尾部)"""
        from app.domain.models.memory import Memory
        # 仅少量消息,不触发合并
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        memory = Memory(messages=messages)
        memory._merge_consecutive_tool_messages()
        # 消息数不变
        assert len(memory.messages) == 3
