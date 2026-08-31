#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : stream_chunk.py
LLM流式输出的单个块数据结构
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class LLMStreamChunk:
    """LLM流式输出的单个块

    Attributes:
        delta_content: 文本内容的增量片段，前端逐块累积显示
        delta_reasoning: 推理过程的增量片段（DeepSeek V4思考模式），不展示给前端
        delta_tool_calls: 工具调用的增量片段（流式tool_calls分片），供调用方累积构建完整tool_calls
        finish_reason: 结束原因（stop/length/tool_calls），仅在最后一个块出现
    """
    delta_content: str = ""
    delta_reasoning: str = ""
    delta_tool_calls: Optional[List[Dict[str, Any]]] = None
    finish_reason: Optional[str] = None
