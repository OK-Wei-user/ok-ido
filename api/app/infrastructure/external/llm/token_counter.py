#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2026/07/06

@File    : token_counter.py
基于 tiktoken 的 token 计数器，用于触发 memory 压缩阈值判定
"""
import logging
from typing import List, Dict, Any

import tiktoken

logger = logging.getLogger(__name__)


class TokenCounter:
    """基于 tiktoken 的 token 计数器

    用于在 BaseAgent._invoke_llm 调用前判定 memory 是否接近上下文窗口阈值，
    超阈值时触发 compact_memory（与 should_compress/is_context_overflow 兜底并存）。
    """

    # 未知模型时使用的兜底编码
    _FALLBACK_ENCODING = "cl100k_base"

    # F3-4缓存: 模型名→编码器实例的模块级缓存,避免重复初始化tiktoken编码器
    # tiktoken.encoding_for_model/get_encoding内部虽有缓存,但每次调用仍有函数调用开销,
    # 且三级降级逻辑重复执行。缓存后同一model_name只初始化一次。
    _ENCODER_CACHE: Dict[str, Any] = {}

    def __init__(self, model_name: str) -> None:
        """构造函数，三级降级加载 tiktoken 编码器

        Level 1: tiktoken.encoding_for_model(模型专用编码)
        Level 2: tiktoken.get_encoding(cl100k_base)  (通用编码，需在线下载tiktoken文件)
        Level 3: None (CJK字符估算，Docker容器SSL错误无法下载cl100k_base.ttiktoken时兜底)

        三级降级确保任何环境下 TokenCounter 都能正常构造，
        避免 chat 端点因 tiktoken 初始化失败返回 500。

        F3-4缓存: 编码器实例按model_name缓存,同model_name的TokenCounter实例共享编码器,
        避免重复执行三级降级逻辑(tiktoken.encoding_for_model可能触发在线下载)。
        """
        self._model_name = model_name
        # F3-4: 优先从缓存读取编码器,命中则直接复用
        if model_name in self._ENCODER_CACHE:
            self._encoder = self._ENCODER_CACHE[model_name]
            return

        self._encoder = None
        try:
            # Level 1: 模型专用编码
            self._encoder = tiktoken.encoding_for_model(model_name)
        except KeyError:
            logger.warning(f"tiktoken无法识别模型[{model_name}]，降级使用 {self._FALLBACK_ENCODING} 编码")
            try:
                # Level 2: 通用编码（可能因SSL错误失败）
                self._encoder = tiktoken.get_encoding(self._FALLBACK_ENCODING)
            except Exception as e:
                # Level 3: CJK字符估算兜底（Docker容器cl100k_base.tiktoken下载失败时）
                logger.warning(
                    f"tiktoken加载{self._FALLBACK_ENCODING}失败(可能SSL错误)，"
                    f"降级使用CJK字符估算: {str(e)}"
                )
                self._encoder = None

        # F3-4: 缓存编码器实例(包括None,避免重复执行降级逻辑)
        self._ENCODER_CACHE[model_name] = self._encoder

    def count_text(self, text: str) -> int:
        """统计纯文本的 token 数（三级降级）

        _encoder 可用时使用 tiktoken 精确计数，
        不可用时降级到 CJK 字符估算（足够触发压缩阈值判定使用）。
        """
        if not text:
            return 0
        if self._encoder is not None:
            return len(self._encoder.encode(text))
        # Level 3: CJK字符估算兜底
        return self._estimate_tokens_by_chars(text)

    @staticmethod
    def _estimate_tokens_by_chars(text: str) -> int:
        """CJK感知的字符级token估算（cl100k_base不可用时的兜底）

        估算规则（基于GPT tokenizer经验值，足够触发压缩阈值判定使用）：
        - CJK字符(中日韩统一表意文字、全角符号): 1.5 token/字
        - ASCII字母数字: 0.25 token/字符
        - 其他(标点、空格等): 0.5 token/字符
        """
        cjk_count = 0
        ascii_count = 0
        other_count = 0
        for ch in text:
            code = ord(ch)
            # CJK统一表意文字 + CJK扩展 + 全角标点符号
            if (0x4E00 <= code <= 0x9FFF or    # CJK统一表意文字
                    0x3000 <= code <= 0x30FF or  # CJK标点+假名
                    0xFF00 <= code <= 0xFFEF):   # 全角形式
                cjk_count += 1
            elif ch.isascii() and (ch.isalnum() or ch.isspace()):
                ascii_count += 1
            else:
                other_count += 1
        return int(cjk_count * 1.5 + ascii_count * 0.25 + other_count * 0.5)

    def count_message(self, message: Dict[str, Any]) -> int:
        """统计单条消息的 token 数

        按 OpenAI 官方规则：每条消息固定开销 + 各字段 token 累加 + 角色/内容分隔开销。
        估算精度足够触发阈值判定使用，非精确计费。
        """
        # 每条消息固定开销（role/content 等结构开销）
        tokens = 4
        for key, value in message.items():
            if value is None:
                continue
            if isinstance(value, str):
                tokens += self.count_text(value)
            elif isinstance(value, list):
                # 工具调用/多模态内容列表：序列化为 JSON 字符串后统计
                import json
                tokens += self.count_text(json.dumps(value, ensure_ascii=False))
            else:
                tokens += self.count_text(str(value))
            # 字段名开销
            tokens += 1
        return tokens

    def count_messages(self, messages: List[Dict[str, Any]]) -> int:
        """统计消息列表的总 token 数

        含消息间分隔符开销（每条 2 token）与整体结尾开销（3 token）。
        """
        if not messages:
            return 0
        total = 0
        for message in messages:
            total += self.count_message(message)
            total += 2  # 消息分隔符开销
        total += 3  # 结尾开销
        return total
