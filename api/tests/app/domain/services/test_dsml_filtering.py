#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_dsml_filtering.py
DSML工具调用标记过滤与解析单元测试
- parse_dsml_to_tool_calls: LLM适配器层DSML→tool_calls转换(主路径)
- strip_dsml_artifacts: LLM适配器层DSML标记清理(统一防线)
- StreamingDSMLFilter: 流式跨chunk DSML标记缓冲过滤
"""
import json

from app.infrastructure.external.llm.dsml_parser import (
    parse_dsml_to_tool_calls,
    strip_dsml_artifacts,
    StreamingDSMLFilter,
)

# 全角竖线U+FF5C,DeepSeek DSML标记的分隔符
_DSML = "\uff5c"
_TAG_OPEN = f"<{_DSML * 2}DSML{_DSML * 2}tool_calls>"
_TAG_CLOSE = f"</{_DSML * 2}DSML{_DSML * 2}tool_calls>"


def _make_dsml_block(inner: str) -> str:
    """构造完整的DSML tool_calls块"""
    return f"{_TAG_OPEN}{inner}{_TAG_CLOSE}"


def _make_invoke(name: str, params: dict) -> str:
    """构造DSML invoke块"""
    param_parts = []
    for pname, pval in params.items():
        param_parts.append(
            f"<{_DSML * 2}DSML{_DSML * 2}parameter name=\"{pname}\" string=\"true\">"
            f"{pval}</{_DSML * 2}DSML{_DSML * 2}parameter>"
        )
    return f"<{_DSML * 2}DSML{_DSML * 2}invoke name=\"{name}\">{''.join(param_parts)}</{_DSML * 2}DSML{_DSML * 2}invoke>"


class TestParseDsmlToToolCalls:
    """parse_dsml_to_tool_calls函数测试 — LLM适配器层DSML解析为主路径"""

    def test_parses_single_tool_call(self):
        """单个DSML工具调用应解析为标准tool_calls格式"""
        invoke = _make_invoke("read_file", {"filepath": "/home/ubuntu/data/stats.txt"})
        content = _make_dsml_block(invoke)
        cleaned, tool_calls = parse_dsml_to_tool_calls(content)
        assert len(tool_calls) == 1
        assert tool_calls[0]["type"] == "function"
        assert tool_calls[0]["function"]["name"] == "read_file"
        args = json.loads(tool_calls[0]["function"]["arguments"])
        assert args["filepath"] == "/home/ubuntu/data/stats.txt"
        assert "DSML" not in cleaned

    def test_parses_multiple_tool_calls_in_one_block(self):
        """单个DSML块内多个invoke应解析为多个tool_calls"""
        invoke1 = _make_invoke("read_file", {"filepath": "/a.txt"})
        invoke2 = _make_invoke("write_file", {"filepath": "/b.txt", "content": "hello"})
        content = _make_dsml_block(invoke1 + invoke2)
        cleaned, tool_calls = parse_dsml_to_tool_calls(content)
        assert len(tool_calls) == 2
        assert tool_calls[0]["function"]["name"] == "read_file"
        assert tool_calls[1]["function"]["name"] == "write_file"

    def test_parses_multiple_dsml_blocks(self):
        """多个独立DSML块应全部解析"""
        block1 = _make_dsml_block(_make_invoke("read_file", {"filepath": "/a.txt"}))
        block2 = _make_dsml_block(_make_invoke("shell_exec", {"command": "ls"}))
        content = f"前文 {block1} 中间 {block2} 后文"
        cleaned, tool_calls = parse_dsml_to_tool_calls(content)
        assert len(tool_calls) == 2
        assert "前文" in cleaned
        assert "后文" in cleaned
        assert "DSML" not in cleaned

    def test_preserves_normal_text_around_dsml(self):
        """DSML块前后的正常文本应保留"""
        invoke = _make_invoke("read_file", {"filepath": "/stats.txt"})
        content = f"分析完成。{_make_dsml_block(invoke)} 报告已生成。"
        cleaned, tool_calls = parse_dsml_to_tool_calls(content)
        assert len(tool_calls) == 1
        assert "分析完成" in cleaned
        assert "报告已生成" in cleaned

    def test_returns_empty_tool_calls_for_normal_content(self):
        """无DSML标记的正常内容应返回空tool_calls"""
        content = "这是正常的AI回复内容"
        cleaned, tool_calls = parse_dsml_to_tool_calls(content)
        assert tool_calls == []
        assert cleaned == content

    def test_returns_empty_for_none_content(self):
        """None应返回(None, [])"""
        cleaned, tool_calls = parse_dsml_to_tool_calls(None)
        assert cleaned is None
        assert tool_calls == []

    def test_returns_empty_for_empty_string(self):
        """空字符串应返回('', [])"""
        cleaned, tool_calls = parse_dsml_to_tool_calls("")
        assert cleaned == ""
        assert tool_calls == []

    def test_tool_call_has_valid_id(self):
        """解析出的tool_call应有有效的id字段"""
        invoke = _make_invoke("read_file", {"filepath": "/a.txt"})
        content = _make_dsml_block(invoke)
        _, tool_calls = parse_dsml_to_tool_calls(content)
        assert tool_calls[0]["id"].startswith("call_")

    def test_parses_multiline_dsml_block(self):
        """跨行DSML块应完整解析(DOTALL模式)"""
        D = _DSML * 2
        invoke = (
            f"<{D}DSML{D}invoke name=\"write_file\">\n"
            f"  <{D}DSML{D}parameter name=\"filepath\" string=\"true\">"
            f"/home/ubuntu/report.txt</{D}DSML{D}parameter>\n"
            f"  <{D}DSML{D}parameter name=\"content\" string=\"true\">"
            f"hello</{D}DSML{D}parameter>\n"
            f"</{D}DSML{D}invoke>"
        )
        content = _make_dsml_block(invoke)
        cleaned, tool_calls = parse_dsml_to_tool_calls(content)
        assert len(tool_calls) == 1
        args = json.loads(tool_calls[0]["function"]["arguments"])
        assert args["filepath"] == "/home/ubuntu/report.txt"
        assert args["content"] == "hello"

    def test_dsml_only_content_returns_empty_cleaned(self):
        """仅含DSML标记的content清理后应为空字符串"""
        invoke = _make_invoke("read_file", {"filepath": "/a.txt"})
        content = _make_dsml_block(invoke)
        cleaned, tool_calls = parse_dsml_to_tool_calls(content)
        assert cleaned == ""
        assert len(tool_calls) == 1


class TestStripDsmlArtifacts:
    """strip_dsml_artifacts 测试 — LLM适配器层DSML标记清理(统一防线)"""

    def test_strips_complete_dsml_block(self):
        """完整DSML块(含invoke/parameter)应被移除"""
        inner = (
            f"<{_DSML * 2}DSML{_DSML * 2}invoke name=\"read_file\">"
            f"<{_DSML * 2}DSML{_DSML * 2}parameter name=\"filepath\" string=\"true\">"
            "/home/ubuntu/data/stats.txt"
            f"</{_DSML * 2}DSML{_DSML * 2}parameter>"
            f"</{_DSML * 2}DSML{_DSML * 2}invoke>"
        )
        content = f"I-DO {_make_dsml_block(inner)}"
        result = strip_dsml_artifacts(content)
        assert "DSML" not in result
        assert "/home/ubuntu/data/stats.txt" not in result
        assert result == "I-DO"

    def test_strips_scattered_dsml_tags(self):
        """散落的DSML开/闭标记应被移除"""
        content = f"前文 {_TAG_OPEN} 中间内容 {_TAG_CLOSE} 后文"
        result = strip_dsml_artifacts(content)
        assert "DSML" not in result
        assert "前文" in result
        assert "后文" in result

    def test_normal_content_unchanged(self):
        """无DSML标记的正常内容应原样返回"""
        content = "这是正常的AI回复内容,不包含任何特殊标记。"
        result = strip_dsml_artifacts(content)
        assert result == content

    def test_empty_content_returns_empty(self):
        """空字符串应原样返回"""
        assert strip_dsml_artifacts("") == ""

    def test_none_content_returns_none(self):
        """None应原样返回(不抛异常)"""
        assert strip_dsml_artifacts(None) is None

    def test_strips_multiple_dsml_blocks(self):
        """多个DSML块应全部移除"""
        block1 = _make_dsml_block("invoke1")
        block2 = _make_dsml_block("invoke2")
        content = f"{block1} 中间文本 {block2}"
        result = strip_dsml_artifacts(content)
        assert "DSML" not in result
        assert "invoke1" not in result
        assert "invoke2" not in result
        assert "中间文本" in result

    def test_preserves_content_around_dsml(self):
        """DSML块前后的正常文本应保留"""
        content = f"分析完成。{_make_dsml_block('tool_call')} 报告已生成。"
        result = strip_dsml_artifacts(content)
        assert "分析完成" in result
        assert "报告已生成" in result
        assert "DSML" not in result

    def test_strips_standalone_dsml_tag_without_block(self):
        """单独的DSML标记(非成对块)应被移除"""
        standalone_tag = f"<{_DSML * 2}DSML{_DSML * 2}tool_calls>"
        content = f"文本 {standalone_tag} 更多文本"
        result = strip_dsml_artifacts(content)
        assert "DSML" not in result
        assert "文本" in result
        assert "更多文本" in result

    def test_strips_closing_only_dsml_tag(self):
        """单独的DSML闭标记应被移除"""
        content = f"文本 {_TAG_CLOSE} 后文"
        result = strip_dsml_artifacts(content)
        assert "DSML" not in result

    def test_strips_dsml_with_multiline_content(self):
        """跨行DSML块应被完整移除(DOTALL模式)"""
        inner = "line1\nline2\nline3"
        content = f"start\n{_make_dsml_block(inner)}\nend"
        result = strip_dsml_artifacts(content)
        assert "DSML" not in result
        assert "line1" not in result
        assert "line3" not in result
        assert "start" in result
        assert "end" in result

    def test_content_without_dsml_keyword_fast_path(self):
        """不含'DSML'关键字的内容直接返回(快速路径)"""
        content = "这是一段完全普通的中英文混合 text content 12345"
        result = strip_dsml_artifacts(content)
        assert result == content

    def test_strips_dsml_leaving_only_whitespace_returns_empty(self):
        """DSML块清除后仅剩空白时应返回空字符串"""
        content = _make_dsml_block("only_tool_call")
        result = strip_dsml_artifacts(content)
        assert result == ""


class TestStreamingDSMLFilter:
    """StreamingDSMLFilter测试 — 流式跨chunk DSML标记缓冲过滤"""

    def test_normal_text_passes_through(self):
        """正常文本(无DSML)应直接通过"""
        f = StreamingDSMLFilter()
        assert f.feed("你好，世界") == "你好，世界"
        assert f.flush() == ""

    def test_complete_dsml_block_in_single_chunk_filtered(self):
        """完整DSML块在单个chunk中应被完全过滤"""
        f = StreamingDSMLFilter()
        invoke = _make_invoke("read_file", {"filepath": "/data/a.txt"})
        content = _make_dsml_block(invoke)
        assert f.feed(content) == ""
        assert f.flush() == ""

    def test_dsml_block_across_multiple_chunks(self):
        """DSML块跨多个chunk应被完整过滤(核心场景)"""
        f = StreamingDSMLFilter()
        D = _DSML * 2
        # 模拟LLM流式输出: DSML标记被分割到多个chunk
        chunks = [
            f"分析完成。<{D}DSML{D}tool_calls>",
            f"<{D}DSML{D}invoke name=\"read_file\">",
            f"<{D}DSML{D}parameter name=\"filepath\" string=\"true\">/data/a.txt</{D}DSML{D}parameter>",
            f"</{D}DSML{D}invoke></{D}DSML{D}tool_calls>",
            " 报告已生成。",
        ]
        outputs = [f.feed(c) for c in chunks]
        outputs.append(f.flush())

        # DSML标记不应出现在任何输出中
        combined = "".join(outputs)
        assert "DSML" not in combined
        assert "/data/a.txt" not in combined
        # 正常文本应保留
        assert "分析完成" in combined
        assert "报告已生成" in combined

    def test_text_before_dsml_block_output_immediately(self):
        """DSML块之前的正常文本应立即输出"""
        f = StreamingDSMLFilter()
        invoke = _make_invoke("read_file", {"filepath": "/a.txt"})
        content = f"前文 {_make_dsml_block(invoke)}"
        output = f.feed(content)
        assert "前文" in output
        assert "DSML" not in output
        assert f.flush() == ""

    def test_text_after_dsml_block_output_on_flush(self):
        """DSML块之后的正常文本应在后续chunk或flush时输出"""
        f = StreamingDSMLFilter()
        invoke = _make_invoke("read_file", {"filepath": "/a.txt"})
        block = _make_dsml_block(invoke)
        # 先feed DSML块
        out1 = f.feed(block)
        assert out1 == ""
        # 再feed正常文本
        out2 = f.feed("后文")
        assert "后文" in out2
        assert f.flush() == ""

    def test_multiple_dsml_blocks(self):
        """多个DSML块应全部过滤"""
        f = StreamingDSMLFilter()
        block1 = _make_dsml_block(_make_invoke("read_file", {"filepath": "/a.txt"}))
        block2 = _make_dsml_block(_make_invoke("shell_exec", {"command": "ls"}))
        content = f"开始 {block1} 中间 {block2} 结束"
        output = f.feed(content)
        output += f.flush()
        assert "DSML" not in output
        assert "开始" in output
        assert "中间" in output
        assert "结束" in output

    def test_incomplete_prefix_buffered(self):
        """不完整的DSML前缀(如<)应被缓冲,不立即输出"""
        f = StreamingDSMLFilter()
        D = _DSML * 2
        # 只发送<,可能是DSML标记的开头
        out1 = f.feed("正常文本<")
        assert "正常文本" in out1
        assert "<" not in out1  # <被缓冲
        # 后续不是DSML,flush应返回<
        out2 = f.flush()
        assert out2 == "<"

    def test_incomplete_prefix_followed_by_non_dsml(self):
        """不完整前缀<后跟非DSML内容应全部输出"""
        f = StreamingDSMLFilter()
        out1 = f.feed("文本<")
        out2 = f.feed("div>HTML标签</div>")
        out3 = f.flush()
        combined = out1 + out2 + out3
        assert combined == "文本<div>HTML标签</div>"

    def test_unclosed_dsml_block_dropped_on_flush(self):
        """flush时未闭合的DSML块应被丢弃"""
        f = StreamingDSMLFilter()
        D = _DSML * 2
        # 只有开始标记,没有结束标记
        out1 = f.feed(f"前文<{D}DSML{D}tool_calls><{D}DSML{D}invoke name=\"read_file\">")
        assert "前文" in out1
        assert "DSML" not in out1
        # flush时未闭合块被丢弃
        out2 = f.flush()
        assert out2 == ""

    def test_empty_input_returns_empty(self):
        """空字符串输入应返回空"""
        f = StreamingDSMLFilter()
        assert f.feed("") == ""
        assert f.flush() == ""

    def test_none_like_input_returns_empty(self):
        """None输入应返回空(不抛异常)"""
        f = StreamingDSMLFilter()
        assert f.feed(None) == ""

    def test_user_scenario_4_invokes_across_chunks(self):
        """用户实际场景: 4个invoke的DSML块跨chunk到达"""
        f = StreamingDSMLFilter()
        D = _DSML * 2
        # 构造用户提供的完整DSML块
        full_block = (
            f"<{D}DSML{D}tool_calls> "
            f"<{D}DSML{D}invoke name=\"read_file\"> "
            f"<{D}DSML{D}parameter name=\"filepath\" string=\"true\">/home/ubuntu/data/data_overview.txt</{D}DSML{D}parameter> "
            f"</{D}DSML{D}invoke> "
            f"<{D}DSML{D}invoke name=\"shell_execute\"> "
            f"<{D}DSML{D}parameter name=\"command\" string=\"true\">ls -la 2>&1</{D}DSML{D}parameter> "
            f"</{D}DSML{D}invoke> "
            f"</{D}DSML{D}tool_calls>"
        )
        # 模拟LLM流式输出: 将DSML块分成多个不均匀的chunk
        mid = len(full_block) // 3
        chunk1 = full_block[:mid]
        chunk2 = full_block[mid:mid * 2]
        chunk3 = full_block[mid * 2:]

        out1 = f.feed(chunk1)
        out2 = f.feed(chunk2)
        out3 = f.feed(chunk3)
        out4 = f.flush()

        combined = out1 + out2 + out3 + out4
        # 任何DSML标记都不应泄漏
        assert "DSML" not in combined
        assert "｜｜" not in combined
        assert "read_file" not in combined
        assert "shell_execute" not in combined
        assert "/home/ubuntu" not in combined

    def test_mixed_normal_and_dsml_across_chunks(self):
        """混合场景: 正常文本+DSML块+正常文本跨chunk交错"""
        f = StreamingDSMLFilter()
        D = _DSML * 2
        invoke = _make_invoke("read_file", {"filepath": "/data/a.txt"})
        block = _make_dsml_block(invoke)

        # 分成多个chunk: 正常文本片段 + DSML片段 + 正常文本片段
        chunks = [
            "根据分析结果，",
            f"我们发现{block}",
            "数据包含以下关键指标。",
        ]
        outputs = [f.feed(c) for c in chunks]
        outputs.append(f.flush())
        combined = "".join(outputs)

        assert "DSML" not in combined
        assert "根据分析结果" in combined
        assert "我们发现" in combined
        assert "数据包含以下关键指标" in combined

    def test_flush_resets_state(self):
        """flush后过滤器可复用"""
        f = StreamingDSMLFilter()
        f.feed("正常文本")
        f.flush()
        # 复用
        assert f.feed("新文本") == "新文本"
        assert f.flush() == ""
