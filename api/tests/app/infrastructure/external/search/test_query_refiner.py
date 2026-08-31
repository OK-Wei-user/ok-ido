#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_query_refiner.py
QueryRefiner 单元测试 — 验证 F4-代码质量修复: 移除未使用 Optional 导入后的行为一致性

测试覆盖:
- refine: 正常查询精简(关键词数量 ≤ max_keywords)
- refine: 空字符串/纯空白输入边界
- refine: site: 前缀保留
- refine: 中英文停用词过滤
- refine: 关键词去重
- refine: 关键词打分排序(年份/缩写/中文优先)
- refine: 退化路径(全部被过滤时回退原始分词)
"""
from app.infrastructure.external.search.query_refiner import (
    QueryRefiner,
    _CN_STOPWORDS,
    _EN_STOPWORDS,
    _MAX_KEYWORDS,
)


# ============ 边界输入 ============

class TestBoundaryInput:
    """边界输入处理"""

    def test_refine_empty_string_returns_empty(self):
        """空字符串应原样返回"""
        refiner = QueryRefiner()
        assert refiner.refine("") == ""

    def test_refine_none_returns_none(self):
        """None 应原样返回"""
        refiner = QueryRefiner()
        assert refiner.refine(None) is None

    def test_refine_whitespace_only_returns_original(self):
        """纯空白字符串应原样返回(strip 后为空)"""
        refiner = QueryRefiner()
        result = refiner.refine("   ")
        assert result == "   "


# ============ 关键词数量约束 ============

class TestKeywordCountConstraint:
    """关键词数量约束验证"""

    def test_refine_truncates_to_max_keywords(self):
        """超过 max_keywords 的查询应被截断"""
        refiner = QueryRefiner(max_keywords=3)
        result = refiner.refine("Python Java Go Rust C++ JavaScript")
        tokens = result.split()
        assert len(tokens) <= 3

    def test_refine_default_max_is_four(self):
        """默认 max_keywords 应为 4(_MAX_KEYWORDS 常量)"""
        refiner = QueryRefiner()
        assert refiner._max_keywords == _MAX_KEYWORDS == 4

    def test_refine_keeps_short_query_intact(self):
        """短查询(≤max_keywords)应原样保留(无停用词情况下)"""
        refiner = QueryRefiner(max_keywords=4)
        result = refiner.refine("Python Java Go")
        assert "Python" in result
        assert "Java" in result
        assert "Go" in result


# ============ site: 前缀保留 ============

class TestSitePrefixPreservation:
    """site: 前缀保留验证"""

    def test_refine_preserves_site_prefix(self):
        """site:xxx 前缀应被保留并附加到精简结果前"""
        refiner = QueryRefiner(max_keywords=4)
        result = refiner.refine("site:example.com Python 教程 入门")
        assert result.startswith("site:example.com ")
        assert "Python" in result

    def test_refine_site_prefix_case_insensitive(self):
        """SITE: 大写也应被识别为前缀"""
        refiner = QueryRefiner(max_keywords=4)
        result = refiner.refine("SITE:example.com Python 教程")
        assert "example.com" in result


# ============ 停用词过滤 ============

class TestStopwordFiltering:
    """中英文停用词过滤验证"""

    def test_refine_filters_chinese_stopwords(self):
        """中文停用词应被过滤"""
        refiner = QueryRefiner(max_keywords=10)
        result = refiner.refine("如何 学习 Python")
        # "如何" 是停用词,应被移除
        assert "如何" not in result
        assert "Python" in result

    def test_refine_filters_english_stopwords(self):
        """英文停用词应被过滤"""
        refiner = QueryRefiner(max_keywords=10)
        result = refiner.refine("how to learn Python")
        # "how", "to" 是停用词
        assert "how" not in result.lower()
        assert "to" not in result.lower().split()
        assert "Python" in result

    def test_cn_stopwords_includes_common_words(self):
        """_CN_STOPWORDS 应包含常见中文停用词"""
        assert "的" in _CN_STOPWORDS
        assert "如何" in _CN_STOPWORDS
        assert "可以" in _CN_STOPWORDS

    def test_en_stopwords_includes_common_words(self):
        """_EN_STOPWORDS 应包含常见英文停用词"""
        assert "the" in _EN_STOPWORDS
        assert "how" in _EN_STOPWORDS
        assert "what" in _EN_STOPWORDS


# ============ 去重 ============

class TestDeduplication:
    """关键词去重验证"""

    def test_refine_deduplicates_case_insensitive(self):
        """大小写不同的重复词应被去重(保留首次出现的大小写)"""
        refiner = QueryRefiner(max_keywords=10)
        result = refiner.refine("Python python PYTHON")
        tokens = result.split()
        assert len(tokens) == 1
        assert tokens[0] == "Python"


# ============ 关键词打分 ============

class TestTokenScoring:
    """关键词打分排序验证"""

    def test_refine_prefers_year_tokens(self):
        """年份 token 应被优先保留(高于长中文词 2.0 分)"""
        refiner = QueryRefiner(max_keywords=1)
        # "2026" 年份 2.5 分,"数据分析报告" 长中文 2.0 分,年份胜出
        result = refiner.refine("2026 数据分析报告")
        tokens = result.split()
        assert "2026" in tokens

    def test_refine_year_score_higher_than_long_english(self):
        """年份 token(2.5)应高于长英文词(-0.5)"""
        refiner = QueryRefiner(max_keywords=1)
        result = refiner.refine("2026 documentation")
        tokens = result.split()
        assert "2026" in tokens

    def test_refine_prefers_acronyms(self):
        """缩写 token(≥2 个大写字母)应被优先保留"""
        refiner = QueryRefiner(max_keywords=2)
        result = refiner.refine("API 接口 文档")
        tokens = result.split()
        # "API" 是缩写,应被保留
        assert "API" in tokens


# ============ 退化路径 ============

class TestFallbackPath:
    """全部被过滤时的回退路径"""

    def test_refine_falls_back_to_raw_tokens_when_all_filtered(self):
        """所有 token 都是停用词时,应回退到原始分词(截断到 max_keywords)"""
        refiner = QueryRefiner(max_keywords=2)
        # 全部是中文停用词
        result = refiner.refine("的 了 和")
        # 应回退到原始分词
        assert result != ""
        assert len(result.split()) <= 2


# ============ Optional 导入移除验证 ============

class TestOptionalImportRemoved:
    """验证移除 `from typing import Optional` 未破坏模块"""

    def test_module_imports_successfully(self):
        """模块应可正常导入(无 Optional 依赖)"""
        import app.infrastructure.external.search.query_refiner as module

        assert hasattr(module, "QueryRefiner")
        assert hasattr(module, "_CN_STOPWORDS")
        assert hasattr(module, "_EN_STOPWORDS")

    def test_module_has_no_typing_optional_attribute(self):
        """模块不应再引用 typing.Optional"""
        import app.infrastructure.external.search.query_refiner as module

        # 模块源码中不应存在 Optional 引用
        source = open(module.__file__, encoding="utf-8").read()
        assert "Optional" not in source
