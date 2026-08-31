#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/5/17 17:14

@File    : llm.py
"""
from typing import Protocol, List, Dict, Any, AsyncGenerator

from app.infrastructure.external.llm.stream_chunk import LLMStreamChunk


class LLM(Protocol):
    """用于Agent应用与LLM进行交互的接口协议"""

    async def invoke(
            self,
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]] = None,
            response_format: Dict[str, Any] = None,
            tool_choice: str = None,
    ) -> Dict[str, Any]:
        """传递消息列表、工具列表、响应格式、工具选择策略调用LLM接口（一次性返回完整 message）"""
        ...

    async def astream(
            self,
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]] = None,
            response_format: Dict[str, Any] = None,
            tool_choice: str = None,
            keep_response_format: bool = False,
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        """流式调用LLM，逐块 yield LLMStreamChunk

        keep_response_format=False(默认): 移除response_format,纯文本输出(Summarize场景)
        keep_response_format=True: 保留response_format,支持JSON输出(ReAct流式调用场景)
        调用方负责异常处理与降级回退到 invoke()。
        """
        ...

    @property
    def model_name(self) -> str:
        """只读属性，返回LLM的名字"""
        ...

    @property
    def temperature(self) -> float:
        """只读属性，返回LLM的温度"""
        ...

    @property
    def max_tokens(self) -> int:
        """只读属性，返回LLM的最大生成token数"""
        ...

    @property
    def supports_images(self) -> bool:
        """只读属性，返回LLM是否支持图像输入(多模态)。
        false时工具结果中的截图不构建image_url块，避免非多模态LLM返回400错误。
        """
        ...
