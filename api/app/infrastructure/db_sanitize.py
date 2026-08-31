#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : db_sanitize.py
PostgreSQL JSONB 数据清洗工具

PostgreSQL 的 JSONB/TEXT 列不支持 Unicode NULL 字符 (\u0000)，
当外部数据（如网页搜索结果、LLM 输出）含 \u0000 时会导致
asyncpg.UntranslatableCharacterError。

本模块提供递归清洗函数，在数据持久化前移除 \u0000 字符。
"""
from typing import Any

# PostgreSQL JSONB 不支持的字符：仅 \u0000 (NULL)
_NULL_CHAR = "\u0000"


def sanitize_for_postgres(data: Any) -> Any:
    """
    递归清洗数据，移除 PostgreSQL JSONB 不支持的 \u0000 字符。

    :param data: 任意可序列化数据（dict/list/str/int/float/bool/None）
    :return: 清洗后的数据（不修改原始对象）
    """
    if isinstance(data, str):
        return data.replace(_NULL_CHAR, "")
    if isinstance(data, dict):
        return {key: sanitize_for_postgres(value) for key, value in data.items()}
    if isinstance(data, list):
        return [sanitize_for_postgres(item) for item in data]
    return data
