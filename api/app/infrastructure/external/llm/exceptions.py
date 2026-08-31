#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2026/07/06

@File    : exceptions.py
LLM 调用相关异常类型，供 tenacity 区分可重试与不可重试错误
"""


class RetryableLLMError(Exception):
    """可重试的LLM错误

    触发场景：429(限流)、500/502/503/504(服务端错误)、网络瞬时故障。
    tenacity 会按指数退避策略重试此类异常。
    """

    def __init__(self, message: str, status_code: int = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NonRetryableLLMError(Exception):
    """不可重试的LLM错误

    触发场景：4xx(非429，请求格式错误、鉴权失败等)、参数非法。
    tenacity 不会重试此类异常，直接抛出。
    """

    def __init__(self, message: str, status_code: int = None) -> None:
        super().__init__(message)
        self.status_code = status_code
