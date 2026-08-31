#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/18 1:46

@File    : repair_json_parser.py
"""
import logging
from typing import Optional, Any, Union, Dict, List

import json_repair

from app.domain.external.json_parser import JSONParser

logger = logging.getLogger(__name__)


class RepairJSONParser(JSONParser):
    """基于修复逻辑的json解析器"""

    async def invoke(self, text: str, default_value: Optional[Any] = None) -> Union[Dict, List, Any]:
        """传递文本，并使用json修复库进行修复

        空文本处理策略:
        - 若调用方提供 default_value, 返回 default_value
        - 若未提供 default_value, 返回空 dict {} 而非抛出异常
          (防御性默认:避免未捕获异常导致会话中断,
           调用方应自行处理空 dict 的边界情况)
        """
        # 1.记录日志并判断text是否传递
        logger.info(f"解析json文本: {text}")
        if not text or not text.strip():
            logger.warning("json文本为空,返回空dict(防御性默认)")
            return default_value if default_value is not None else {}

        # 2.存在数值则使用json_repair库修复并解析
        return json_repair.repair_json(text, ensure_ascii=False, return_objects=True)
