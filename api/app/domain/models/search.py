#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/24 14:56

@File    : search.py
"""
from typing import Optional, List
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from pydantic import BaseModel, Field, model_validator


# URL规范化时需剔除的跟踪参数，避免同一页面因tracking参数不同被判为不同结果
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "ref", "source", "spm",
})


def _normalize_url(url: str) -> str:
    """URL规范化：去fragment、host小写、去trailing slash、剔除tracking参数、排序剩余query。
    用于计算dedup_key，使同一页面不同tracking参数的URL被识别为重复。"""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        # 去掉末尾的斜杠(根路径除外)
        path = parsed.path.rstrip("/") or "/"
        # 剔除tracking参数并排序剩余query，保证参数顺序不影响去重
        clean_pairs = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
        ]
        clean_pairs.sort()
        query = urlencode(clean_pairs)
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return url


class SearchResultItem(BaseModel):
    """搜索结果条目数据模型"""
    url: str  # 搜索条目URL地址
    title: str  # 搜索条目标题
    snippet: str = ""  # 搜索条目简介
    dedup_key: str = ""  # URL规范化后的去重键，由model_validator自动计算
    content: Optional[str] = None  # fetch_content开启时填充的网页正文，默认None

    @model_validator(mode="after")
    def _compute_dedup_key(self) -> "SearchResultItem":
        """dedup_key为空时自动由url规范化计算，向后兼容旧数据(无字段时自动补齐)"""
        if not self.dedup_key:
            self.dedup_key = _normalize_url(self.url)
        return self


class SearchResults(BaseModel):
    """搜索结果数据模型"""
    query: str  # 用户的搜索词
    date_range: Optional[str] = None  # 日期检索范围
    total_results: int = 0  # 搜索结果条数
    results: List[SearchResultItem] = Field(default_factory=list)  # 搜索结果列表
