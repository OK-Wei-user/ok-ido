#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : query_refiner.py
搜索查询精简器 - 确定性约束关键词数量，提升搜索引擎召回质量
"""
import logging
import re

logger = logging.getLogger(__name__)

_CN_STOPWORDS = frozenset({
    "的", "了", "和", "与", "在", "是", "有", "为", "中", "等",
    "个", "上", "下", "不", "也", "都", "而", "及", "或", "被",
    "从", "到", "对", "把", "让", "用", "以", "这", "那", "之",
    "可以", "能够", "需要", "应该", "如何", "什么", "怎么", "哪些",
    "一些", "一个", "那种", "这种", "那样", "这样", "关于", "对于",
    "通过", "进行", "以及", "还是", "但是", "然而", "虽然", "因为",
    "所以", "如果", "就是", "只是", "不是", "没有", "已经", "正在",
})

_EN_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to",
    "for", "of", "with", "by", "from", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can",
    "how", "what", "which", "who", "when", "where", "why",
    "this", "that", "these", "those", "it", "its",
})

_MAX_KEYWORDS = 4
_SITE_PREFIX_PATTERN = re.compile(r"^(site:[^\s]+\s*)(.*)", re.IGNORECASE)


class QueryRefiner:
    """搜索查询精简器 - 确保查询关键词在2-4个以内"""

    def __init__(self, max_keywords: int = _MAX_KEYWORDS):
        self._max_keywords = max_keywords

    def refine(self, query: str) -> str:
        if not query or not query.strip():
            return query

        site_prefix, body = self._extract_site_prefix(query)
        tokens = self._tokenize(body)
        tokens = self._remove_stopwords(tokens)
        tokens = self._deduplicate(tokens)
        tokens = self._truncate(tokens, self._max_keywords)

        if not tokens:
            tokens = self._tokenize(body)[:self._max_keywords]

        refined = " ".join(tokens)
        if site_prefix:
            refined = f"{site_prefix}{refined}"

        if refined != query.strip():
            logger.info(f"查询精简: [{query}] -> [{refined}]")

        return refined

    def _extract_site_prefix(self, query: str) -> tuple:
        match = _SITE_PREFIX_PATTERN.match(query.strip())
        if match:
            return match.group(1), match.group(2)
        return "", query.strip()

    def _tokenize(self, text: str) -> list:
        return [t for t in re.split(r"[\s]+", text.strip()) if t]

    def _remove_stopwords(self, tokens: list) -> list:
        return [t for t in tokens if t.lower() not in _CN_STOPWORDS and t.lower() not in _EN_STOPWORDS]

    def _deduplicate(self, tokens: list) -> list:
        seen = set()
        result = []
        for t in tokens:
            key = t.lower()
            if key not in seen:
                seen.add(key)
                result.append(t)
        return result

    def _truncate(self, tokens: list, max_count: int) -> list:
        if len(tokens) <= max_count:
            return tokens

        scored = [(i, self._score_token(t), t) for i, t in enumerate(tokens)]
        scored.sort(key=lambda x: x[1], reverse=True)
        selected = sorted(scored[:max_count], key=lambda x: x[0])
        return [t for _, _, t in selected]

    def _score_token(self, token: str) -> float:
        score = 0.0
        has_chinese = bool(re.search(r"[\u4e00-\u9fff]", token))
        has_digit = bool(re.search(r"\d", token))
        is_year = bool(re.fullmatch(r"\d{4}", token))
        is_acronym = bool(re.fullmatch(r"[A-Z]{2,}", token))
        is_short = len(token) <= 2

        if is_acronym:
            score += 3.0
        if is_year:
            score += 2.5
        if has_chinese:
            score += 2.0
        if has_digit and not is_year:
            score += 1.5
        if is_short and has_chinese:
            score += 1.0
        if not has_chinese and not has_digit and len(token) > 6:
            score -= 0.5

        return score
