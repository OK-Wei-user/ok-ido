#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : dsml_parser.py
DeepSeek DSML工具调用标记解析器 — 将泄漏到content的DSML标记转换为标准tool_calls

DeepSeek V4模型在异常情况下会将工具调用以DSML格式输出到content字段,
而非通过标准tool_calls字段返回。此解析器在LLM适配器层将DSML标记
解析为标准OpenAI tool_calls格式,使领域层无感知。

DSML标记格式(分隔符为全角竖线U+FF5C):
  <｜｜DSML｜｜tool_calls>
    <｜｜DSML｜｜invoke name="function_name">
      <｜｜DSML｜｜parameter name="param_name" string="true">value</｜｜DSML｜｜parameter>
    </｜｜DSML｜｜invoke>
  </｜｜DSML｜｜tool_calls>
"""
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# 全角竖线U+FF5C,DSML标记的分隔符
_DSML = "\uff5c"
_DSML_DELIM = _DSML * 2  # ｜｜

# DSML tool_calls外层块正则(DOTALL支持跨行匹配)
_DSML_BLOCK_RE = re.compile(
    rf"<{_DSML_DELIM}DSML{_DSML_DELIM}tool_calls>(.*?)</{_DSML_DELIM}DSML{_DSML_DELIM}tool_calls>",
    re.DOTALL,
)

# DSML invoke块正则: 提取function name和内部参数
_DSML_INVOKE_RE = re.compile(
    rf"<{_DSML_DELIM}DSML{_DSML_DELIM}invoke\s+name=\"([^\"]+)\">(.*?)</{_DSML_DELIM}DSML{_DSML_DELIM}invoke>",
    re.DOTALL,
)

# DSML parameter正则: 提取参数名、类型属性和值
_DSML_PARAM_RE = re.compile(
    rf"<{_DSML_DELIM}DSML{_DSML_DELIM}parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)</{_DSML_DELIM}DSML{_DSML_DELIM}parameter>",
    re.DOTALL,
)

# 散落的DSML标签(非成对块)清理正则
_DSML_TAG_RE = re.compile(
    rf"</?{_DSML_DELIM}DSML{_DSML_DELIM}[^>]+/?>"
)


def parse_dsml_to_tool_calls(content: str) -> Tuple[str, List[Dict[str, Any]]]:
    """解析content中的DSML标记,转换为标准tool_calls格式

    当DeepSeek模型异常将工具调用以DSML文本格式输出到content时,
    此函数将其解析为OpenAI标准tool_calls格式,使Agent能正确执行工具调用。

    Args:
        content: LLM返回的content字段(可能包含DSML标记)

    Returns:
        (cleaned_content, tool_calls):
        - cleaned_content: 移除DSML标记后的文本(可能为空字符串)
        - tool_calls: 标准tool_calls列表(空列表表示无DSML工具调用)
    """
    if not content or "DSML" not in content:
        return content, []

    tool_calls: List[Dict[str, Any]] = []
    cleaned = content

    # 1.提取并解析所有DSML tool_calls块
    for block_match in _DSML_BLOCK_RE.finditer(content):
        block_body = block_match.group(1)

        # 2.解析块内的每个invoke调用
        for invoke_match in _DSML_INVOKE_RE.finditer(block_body):
            function_name = invoke_match.group(1)
            invoke_body = invoke_match.group(2)

            # 3.提取参数
            arguments: Dict[str, Any] = {}
            for param_match in _DSML_PARAM_RE.finditer(invoke_body):
                param_name = param_match.group(1)
                param_value = _parse_param_value(param_match.group(2))
                arguments[param_name] = param_value

            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": function_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            })

    # 4.移除所有DSML标记(块级+散落标签),保留正常文本
    cleaned = _DSML_BLOCK_RE.sub("", cleaned)
    cleaned = _DSML_TAG_RE.sub("", cleaned)
    cleaned = cleaned.strip()

    if tool_calls:
        logger.warning(
            f"DSML标记解析: 从content中提取{len(tool_calls)}个工具调用"
            f"(functions={[tc['function']['name'] for tc in tool_calls]})"
        )

    return cleaned, tool_calls


def _parse_param_value(raw: str) -> Any:
    """解析DSML参数值,尝试类型推断

    DSML parameter的string="true"属性表示字符串类型,
    无类型属性时尝试JSON解析(支持数字/布尔/对象),失败则降级为字符串。
    """
    value = raw.strip()
    # 尝试JSON解析(处理数字、布尔、null、嵌套对象等)
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        pass
    return value


def strip_dsml_artifacts(content: str) -> str:
    """移除content中的DSML标记(仅清理,不解析为tool_calls)

    用于流式场景的完整内容清洗(如accumulated_content最终清洗),
    此函数仅做标记清理,防止内部格式泄漏到用户输出。
    非流式场景应使用parse_dsml_to_tool_calls进行完整解析。

    注意: 此函数对完整DSML块有效,但对跨chunk的不完整DSML片段无效。
    流式逐块场景请使用StreamingDSMLFilter。
    """
    if not content or "DSML" not in content:
        return content
    cleaned = _DSML_BLOCK_RE.sub("", content)
    cleaned = _DSML_TAG_RE.sub("", cleaned)
    return cleaned.strip()


class StreamingDSMLFilter:
    """流式DSML标记跨chunk缓冲过滤器

    解决流式输出中DSML标记跨chunk分割导致逐块清洗失效的问题。
    当LLM在流式场景(如summarize)异常输出DSML工具调用标记时,
    标记会被分割到多个delta chunk中,strip_dsml_artifacts无法清洗
    不完整标签(缺少>的<｜｜DSML｜｜invoke)和不含DSML关键字的属性片段。

    策略:
    - 检测DSML标记开始(<｜｜DSML)时进入缓冲模式,不输出后续内容
    - 缓冲后续chunk直到DSML块结束(</｜｜DSML｜｜tool_calls>)
    - 丢弃完整DSML块,仅输出非DSML内容
    - 对可能是不完整DSML前缀的内容(如<、<｜)保留在缓冲区等待确认
    - flush时丢弃未闭合的DSML块
    """

    _BLOCK_START = f"<{_DSML_DELIM}DSML"  # <｜｜DSML
    _BLOCK_END = f"</{_DSML_DELIM}DSML{_DSML_DELIM}tool_calls>"  # </｜｜DSML｜｜tool_calls>

    def __init__(self) -> None:
        """初始化流式DSML过滤器"""
        self._buffer: str = ""
        self._in_dsml: bool = False

    def feed(self, delta: str) -> str:
        """输入流式chunk,返回可安全输出的内容

        - 非DSML内容: 直接返回
        - DSML块: 缓冲直到完整块到达,然后丢弃
        - 不完整前缀: 保留在缓冲区,等待后续chunk确认

        Args:
            delta: 流式输出的增量内容

        Returns:
            可安全输出给用户的内容(已过滤DSML标记)
        """
        if not delta:
            return ""

        self._buffer += delta
        output_parts: List[str] = []

        while self._buffer:
            if not self._in_dsml:
                # 查找DSML标记开始位置
                idx = self._buffer.find(self._BLOCK_START)
                if idx == -1:
                    # 无DSML标记开始,但buffer末尾可能是不完整前缀
                    safe, risky = self._split_safe_and_risky(self._buffer)
                    if safe:
                        output_parts.append(safe)
                    self._buffer = risky
                    break
                # 输出DSML标记之前的安全内容
                if idx > 0:
                    output_parts.append(self._buffer[:idx])
                self._buffer = self._buffer[idx:]
                self._in_dsml = True
            else:
                # 在DSML块中,查找块结束标记
                end_idx = self._buffer.find(self._BLOCK_END)
                if end_idx == -1:
                    # 块未结束,继续缓冲
                    break
                # 块结束,跳过整个DSML块
                self._buffer = self._buffer[end_idx + len(self._BLOCK_END):]
                self._in_dsml = False
                # 继续循环处理剩余buffer(可能还有后续DSML块)

        return "".join(output_parts)

    def flush(self) -> str:
        """流结束时返回剩余安全内容

        - 非DSML缓冲内容: 直接返回
        - 未闭合DSML块: 丢弃(防止格式标记泄漏)

        Returns:
            缓冲区中剩余的可安全输出内容
        """
        if self._in_dsml:
            logger.warning(
                f"流式DSML过滤器: 未闭合DSML块被丢弃, 缓冲长度={len(self._buffer)}"
            )
            self._buffer = ""
            self._in_dsml = False
            return ""

        result = self._buffer
        self._buffer = ""
        return result

    @classmethod
    def _split_safe_and_risky(cls, text: str) -> Tuple[str, str]:
        """在潜在的DSML前缀处分割,返回(安全部分, 风险部分)

        检查text末尾是否可能是DSML标记开头的不完整前缀,
        如<、<｜、<｜｜、<｜｜D、<｜｜DS、<｜｜DSM、<｜｜DSML。
        将可能的前缀保留在缓冲区,避免不完整标记泄漏到用户输出。

        Args:
            text: 待分割的文本

        Returns:
            (安全部分, 风险部分) — 风险部分是可能为DSML前缀的末尾片段
        """
        prefix = cls._BLOCK_START  # <｜｜DSML
        max_check = min(len(prefix), len(text))

        for i in range(max_check, 0, -1):
            if text.endswith(prefix[:i]):
                return text[:-i], text[-i:]

        return text, ""
