#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_search.py
搜索数据模型单元测试 - dedup_key自动计算、URL规范化、content字段序列化
"""
import json

import pytest

from app.domain.models.search import SearchResultItem, SearchResults, _normalize_url


class TestNormalizeUrl:
    """URL规范化函数测试"""

    def test_strips_fragment(self):
        assert _normalize_url("https://example.com/path#section") == "https://example.com/path"

    def test_lowercases_host(self):
        assert _normalize_url("https://EXAMPLE.com/path").startswith("https://example.com/path")

    def test_removes_trailing_slash(self):
        assert _normalize_url("https://example.com/path/") == "https://example.com/path"

    def test_preserves_root_path(self):
        result = _normalize_url("https://example.com/")
        assert result == "https://example.com/"

    def test_strips_utm_params(self):
        result = _normalize_url("https://example.com/page?utm_source=google&utm_medium=cpc&id=123")
        assert "utm_source" not in result
        assert "utm_medium" not in result
        assert "id=123" in result

    def test_strips_gclid_fbclid(self):
        result = _normalize_url("https://example.com/page?gclid=abc&fbclid=xyz&keep=1")
        assert "gclid" not in result
        assert "fbclid" not in result
        assert "keep=1" in result

    def test_sorts_remaining_query_params(self):
        result = _normalize_url("https://example.com/page?b=2&a=1")
        assert "a=1&b=2" in result

    def test_empty_url_returns_empty(self):
        assert _normalize_url("") == ""

    def test_invalid_url_returns_original(self):
        invalid = "not a url at all"
        assert _normalize_url(invalid) == invalid


class TestSearchResultItemDedupKey:
    """SearchResultItem.dedup_key 自动计算测试"""

    def test_dedup_key_auto_computed_from_url(self):
        item = SearchResultItem(url="https://example.com/page", title="Test")
        assert item.dedup_key == _normalize_url("https://example.com/page")

    def test_dedup_key_strips_tracking_params(self):
        item1 = SearchResultItem(url="https://example.com/page?utm_source=x", title="A")
        item2 = SearchResultItem(url="https://example.com/page", title="B")
        assert item1.dedup_key == item2.dedup_key

    def test_dedup_key_strips_fragment(self):
        item1 = SearchResultItem(url="https://example.com/page#sec1", title="A")
        item2 = SearchResultItem(url="https://example.com/page#sec2", title="B")
        assert item1.dedup_key == item2.dedup_key

    def test_dedup_key_lowercases_host(self):
        item1 = SearchResultItem(url="https://EXAMPLE.com/path", title="A")
        item2 = SearchResultItem(url="https://example.com/path", title="B")
        assert item1.dedup_key == item2.dedup_key

    def test_dedup_key_preserves_explicit_value(self):
        item = SearchResultItem(url="https://example.com/page", title="Test", dedup_key="custom_key")
        assert item.dedup_key == "custom_key"

    def test_dedup_key_empty_when_url_empty(self):
        item = SearchResultItem(url="", title="Test")
        assert item.dedup_key == ""


class TestSearchResultItemContent:
    """SearchResultItem.content 字段测试"""

    def test_content_defaults_to_none(self):
        item = SearchResultItem(url="https://example.com", title="Test")
        assert item.content is None

    def test_content_serialized_when_set(self):
        item = SearchResultItem(url="https://example.com", title="Test", content="正文内容")
        data = json.loads(item.model_dump_json())
        assert data["content"] == "正文内容"

    def test_content_none_in_serialization(self):
        item = SearchResultItem(url="https://example.com", title="Test")
        data = json.loads(item.model_dump_json())
        assert data["content"] is None

    def test_content_does_not_break_search_results(self):
        item = SearchResultItem(url="https://example.com", title="Test", content="内容")
        results = SearchResults(query="q", results=[item])
        data = json.loads(results.model_dump_json())
        assert data["results"][0]["content"] == "内容"
