#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""工具调用强制校验模块单元测试

验证 _tool_call_verifier 纯函数的步骤类型识别与错误信息构建逻辑。
覆盖:
- 动作类步骤识别(生成/创建/导出/查询/分析等关键词命中)
- 认知类步骤识别(无动作类关键词,允许无工具调用)
- 中英文关键词覆盖
- 错误信息构建
"""
import pytest

from app.domain.services.agents._tool_call_verifier import (
    step_requires_tool_call,
    build_missing_tool_error,
)


class TestStepRequiresToolCall:
    """step_requires_tool_call 步骤类型识别测试"""

    # === 动作类步骤(必须调用工具) ===

    def test_generate_keyword(self):
        """'生成'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("生成Word文档介绍自己") is True

    def test_create_keyword(self):
        """'创建'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("创建Excel报表") is True

    def test_export_keyword(self):
        """'导出'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("导出50条数据到Excel") is True

    def test_analyze_keyword(self):
        """'分析'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("分析数据趋势") is True

    def test_download_keyword(self):
        """'下载'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("下载附件文件") is True

    def test_search_keyword(self):
        """'搜索'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("搜索最新行业报告") is True

    def test_read_keyword(self):
        """'读取'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("读取文件内容") is True

    def test_convert_keyword(self):
        """'转换'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("转换PDF为Word") is True

    def test_extract_keyword(self):
        """'提取'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("提取文档中的关键信息") is True

    def test_recognize_keyword(self):
        """'识别'关键词命中 → 必须调用工具(覆盖OCR场景)"""
        assert step_requires_tool_call("对文档执行OCR文字识别") is True

    def test_translate_keyword(self):
        """'翻译'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("将文档翻译为英文") is True

    def test_calculate_keyword(self):
        """'计算'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("计算数据汇总值") is True

    def test_write_keyword(self):
        """'写入'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("写入数据到文件") is True

    # === 英文动作类关键词(大小写不敏感) ===

    def test_english_generate_keyword(self):
        """英文'generate'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("generate a report") is True

    def test_english_create_keyword_uppercase(self):
        """英文'CREATE'大写关键词命中 → 必须调用工具(大小写不敏感)"""
        assert step_requires_tool_call("CREATE a new file") is True

    def test_english_analyze_keyword(self):
        """英文'analyze'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("analyze the data trend") is True

    def test_english_export_keyword(self):
        """英文'export'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("export data to Excel") is True

    def test_english_download_keyword(self):
        """英文'download'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("download the attachment") is True

    # === 认知类步骤(可不调用工具) ===

    def test_pure_thinking_step(self):
        """纯思考步骤(无动作类关键词) → 可不调用工具"""
        assert step_requires_tool_call("思考最佳的解决方案") is False

    def test_review_step(self):
        """回顾步骤(无动作类关键词) → 可不调用工具"""
        assert step_requires_tool_call("回顾前序步骤的执行情况") is False

    def test_empty_description(self):
        """空描述 → 可不调用工具"""
        assert step_requires_tool_call("") is False

    def test_none_like_description(self):
        """None类描述(空字符串) → 可不调用工具"""
        assert step_requires_tool_call("") is False

    def test_plain_text_step(self):
        """纯文本回复步骤(无动作类关键词) → 可不调用工具"""
        assert step_requires_tool_call("纯文本回复") is False

    def test_self_introduction_step(self):
        """自我介绍步骤(无动作类关键词) → 可不调用工具

        注意: "介绍"不在动作类关键词中,允许无工具调用。
        但如果步骤描述为"生成自我介绍文档",则"生成"命中 → 必须调用工具。
        """
        assert step_requires_tool_call("纯文本自我介绍回复") is False

    # === 边界场景 ===

    def test_description_with_path(self):
        """步骤描述含文件路径 + 动作关键词 → 必须调用工具"""
        assert step_requires_tool_call("读取/home/ubuntu/data.xlsx文件") is True

    def test_mixed_chinese_english(self):
        """中英文混合描述 + 动作关键词 → 必须调用工具"""
        assert step_requires_tool_call("使用shell_execute导出数据") is True

    # === 浏览器/页面交互类关键词(会话5f5ae2ab暴露的缺口) ===

    def test_open_keyword(self):
        """'打开'关键词命中 → 必须调用工具(浏览器导航场景)"""
        assert step_requires_tool_call("使用browser_navigate打开URL") is True

    def test_click_keyword(self):
        """'点击'关键词命中 → 必须调用工具(页面交互场景)"""
        assert step_requires_tool_call("点击【Form 表单】菜单项") is True

    def test_enter_keyword(self):
        """'进入'关键词命中 → 必须调用工具(页面跳转场景)"""
        assert step_requires_tool_call("进入Form表单页面") is True

    def test_scroll_keyword(self):
        """'下滑'关键词命中 → 必须调用工具(页面滚动场景)"""
        assert step_requires_tool_call("下滑到【对齐方式】区域") is True

    def test_input_keyword(self):
        """'输入'关键词命中 → 必须调用工具(表单填写场景)"""
        assert step_requires_tool_call("输入文本'杰瑞'") is True

    def test_adjust_keyword(self):
        """'调整'关键词命中 → 必须调用工具(选项切换场景)"""
        assert step_requires_tool_call("将Form Align调整为Left") is True

    def test_english_navigate_keyword(self):
        """英文'navigate'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("navigate to the page") is True

    def test_english_click_keyword(self):
        """英文'click'关键词命中 → 必须调用工具"""
        assert step_requires_tool_call("click the button") is True

    # === 工具名显式引用(最强信号) ===

    def test_browser_navigate_tool_name(self):
        """步骤描述显式引用browser_navigate → 必须调用工具"""
        assert step_requires_tool_call("使用browser_navigate打开URL") is True

    def test_shell_execute_tool_name(self):
        """步骤描述显式引用shell_execute → 必须调用工具"""
        assert step_requires_tool_call("通过shell_execute执行脚本") is True

    def test_browser_prefix_tool_name(self):
        """步骤描述引用browser_前缀工具 → 必须调用工具(兜底前缀匹配)"""
        assert step_requires_tool_call("调用browser_custom_tool操作") is True

    def test_real_session_step_descriptions(self):
        """会话5f5ae2ab真实步骤描述全部应必须调用工具(回归测试)

        覆盖4个真实失败步骤,确保幻觉防护不再失效。
        """
        steps = [
            "使用browser_navigate打开URL https://element-plus.org/zh-CN/component/radio",
            "在页面中找到并点击【Form 表单】菜单项，进入Form表单页面",
            "在Form表单页面中，下滑到【对齐方式】区域，找到Form Align选项并将其调整为Left",
            "在【对齐方式】区域找到name输入框，输入文本'杰瑞'",
        ]
        for desc in steps:
            assert step_requires_tool_call(desc) is True, f"步骤应必须调用工具: {desc}"


class TestBuildMissingToolError:
    """build_missing_tool_error 错误信息构建测试"""

    def test_error_contains_step_description(self):
        """错误信息应包含步骤描述"""
        desc = "生成Word文档介绍自己"
        error = build_missing_tool_error(desc)
        assert desc in error

    def test_error_contains_tool_call_hint(self):
        """错误信息应包含工具调用提示"""
        error = build_missing_tool_error("导出数据")
        assert "shell_execute" in error or "write_file" in error
        assert "工具" in error

    def test_error_contains_hallucination_keyword(self):
        """错误信息应包含'幻觉'关键词,便于日志检索"""
        error = build_missing_tool_error("生成文档")
        assert "幻觉" in error

    def test_long_description_truncated(self):
        """过长步骤描述应被截断(防止错误信息过长)"""
        long_desc = "生成" + "数据" * 200
        error = build_missing_tool_error(long_desc)
        # 截断到150字符(步骤描述部分)
        assert len(error) < len(long_desc) + 200

    def test_empty_description_does_not_crash(self):
        """空描述不应崩溃"""
        error = build_missing_tool_error("")
        assert isinstance(error, str)
        assert len(error) > 0
