#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : research.py
深度研究数据模型 - 洞察/上下文/总结，供DeepResearchTool使用
"""
from typing import List, Set

from pydantic import BaseModel, Field

# 相关性分档阈值
_KEY_FINDING_THRESHOLD = 0.8  # >=0.8 归入 key_findings
_ADDITIONAL_FINDING_THRESHOLD = 0.5  # 0.5-0.8 归入 additional_findings；<0.5 归入 supplementary


class ResearchInsight(BaseModel):
    """单条研究洞察（从网页正文抽取的关键信息）"""
    content: str  # 洞察文本
    source_url: str  # 来源URL
    source_title: str  # 来源标题
    relevance_score: float = Field(ge=0, le=1)  # 与研究主题的相关性评分(0-1)


class ResearchContext(BaseModel):
    """研究过程上下文，递归传递携带已积累的洞察与已访问URL"""
    query: str  # 原始研究主题
    insights: List[ResearchInsight] = Field(default_factory=list)  # 累积的洞察列表
    follow_up_queries: List[str] = Field(default_factory=list)  # 后续查询候选
    visited_urls: Set[str] = Field(default_factory=set)  # 已抓取过的URL，跨递归层去重
    current_depth: int = 0  # 当前递归深度
    max_depth: int = 2  # 最大递归深度


class ResearchSummary(BaseModel):
    """研究总结，DeepResearchTool的ToolResult.data载荷

    按relevance_score分档：
    - key_findings: >=0.8，核心发现
    - additional_findings: 0.5-0.8，补充发现
    - supplementary: <0.5，参考信息
    """
    query: str  # 原始研究主题
    key_findings: List[ResearchInsight] = Field(default_factory=list)
    additional_findings: List[ResearchInsight] = Field(default_factory=list)
    supplementary: List[ResearchInsight] = Field(default_factory=list)
    follow_up_queries: List[str] = Field(default_factory=list)  # 未探索的后续查询
    total_sources: int = 0  # 已查阅的独立来源数

    @classmethod
    def from_context(cls, ctx: ResearchContext) -> "ResearchSummary":
        """从ResearchContext构建分档总结，按relevance_score分类populate"""
        key_findings: List[ResearchInsight] = []
        additional_findings: List[ResearchInsight] = []
        supplementary: List[ResearchInsight] = []

        for insight in ctx.insights:
            score = insight.relevance_score
            if score >= _KEY_FINDING_THRESHOLD:
                key_findings.append(insight)
            elif score >= _ADDITIONAL_FINDING_THRESHOLD:
                additional_findings.append(insight)
            else:
                supplementary.append(insight)

        return cls(
            query=ctx.query,
            key_findings=key_findings,
            additional_findings=additional_findings,
            supplementary=supplementary,
            follow_up_queries=ctx.follow_up_queries,
            total_sources=len(ctx.visited_urls),
        )
