#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_prompts.py
提示词模板单元测试(CN版):验证占位符格式化安全、交付指令完整性、
通用性约束(无特化业务名)与 Bug 修复约束保留,防止提示词优化引入回退。

EN测试说明:当前系统仅加载中文版提示词(prompts/system.py, planner.py, react.py),
EN片段作为"未来英文支持的预留翻译"保留在_fragments.py中。
当prompts/en/目录创建后,可恢复CN/EN同步测试。
"""
import re

import pytest

from app.domain.services.prompts import planner as cn_planner
from app.domain.services.prompts import react as cn_react
from app.domain.services.prompts import system as cn_system

# 各模板合法占位符集合（.format() 应传入的字段名）
PLACEHOLDERS = {
    "CREATE_PLAN_PROMPT": {"message", "tools_summary", "context_block", "attachments"},
    "UPDATE_PLAN_PROMPT": {"step", "plan", "prior_steps_context"},
    "EXECUTION_PROMPT": {"message", "attachments", "language", "step"},
    "SUMMARIZE_PROMPT": {"files"},
}

# 模板名 → 所属模块映射
PROMPT_MODULES = {
    "CREATE_PLAN_PROMPT": cn_planner,
    "UPDATE_PLAN_PROMPT": cn_planner,
    "EXECUTION_PROMPT": cn_react,
    "SUMMARIZE_PROMPT": cn_react,
}

# 5b54ddc 旧示例中的特化业务名/特化数字（新通用示例不得出现，保证通用性）
SPECIALIZED_TOKENS = ["商城", "进销存", "入库明细", "535条", "46个功能模块"]

# REACT_SYSTEM_PROMPT 必须保留的 Bug 修复约束关键词（防回退）
REACT_BUGFIX_KEYWORDS = [
    "避免重复操作",          # 5b54ddc 基线
    "浏览器操作降级策略",    # 5b54ddc 基线
    "Shell命令超时注意",     # 5b54ddc 基线
    "MCP工具使用约束",       # 直接加载模式保留(原"MCP工具发现约束"已演进)
]

# EXECUTION_PROMPT 必须保留的 Bug 修复约束关键词
EXECUTION_BUGFIX_KEYWORDS = [
    "是你来执行",            # 5b54ddc 基线
    "执行质量要求",          # 5b54ddc 基线
    "message_notify_user",   # 5b54ddc 基线
]

# SUMMARIZE_PROMPT 四层交付结构关键词
SUMMARIZE_LAYERS = ["执行摘要", "关键发现", "详细结果", "文件交付"]

# 生产提示词中不得出现的开发噪声(代码常量名/批次标记/会话ID)
DEV_NOISE_TOKENS = ["F10-7", "WaitEvent", "max_iterations=", "P11", "_MAX_POLL_THRESHOLD"]


def _extract_placeholders(template: str) -> set:
    """提取模板中未被转义的单大括号占位符名（忽略 {{ }} 转义）"""
    cleaned = template.replace("{{", "").replace("}}", "")
    return set(re.findall(r"\{(\w+)\}", cleaned))


class TestPromptPlaceholders:
    """占位符格式化安全性"""

    @pytest.mark.parametrize("prompt_name", list(PLACEHOLDERS.keys()))
    def test_placeholders_match_expected(self, prompt_name):
        """模板声明的占位符应与预期集合一致，防止误删/误增占位符"""
        template = getattr(PROMPT_MODULES[prompt_name], prompt_name)
        assert _extract_placeholders(template) == PLACEHOLDERS[prompt_name]

    @pytest.mark.parametrize("prompt_name", list(PLACEHOLDERS.keys()))
    def test_format_succeeds_with_expected_kwargs(self, prompt_name):
        """用预期 kwargs 调用 .format() 不应抛 KeyError/IndexError，且转义花括号正确还原"""
        kwargs = {name: f"<{name}>" for name in PLACEHOLDERS[prompt_name]}
        template = getattr(PROMPT_MODULES[prompt_name], prompt_name)
        result = template.format(**kwargs)
        assert "{{" not in result and "}}" not in result
        assert not re.search(r"\{\w+\}", result)


class TestSummarizeDeliveryDirectives:
    """SUMMARIZE_PROMPT 交付指令完整性（核心质量保障）"""

    def test_four_layer_structure_present(self):
        """SUMMARIZE 必须包含执行摘要/关键发现/详细结果/文件交付四层结构指令"""
        prompt = cn_react.SUMMARIZE_PROMPT
        for layer in SUMMARIZE_LAYERS:
            assert layer in prompt, f"中文 SUMMARIZE 缺失交付层次: {layer}"

    def test_few_shot_example_present(self):
        """SUMMARIZE 必须包含 few-shot 示例锚点（恢复 5b54ddc 质量关键）"""
        assert "示例" in cn_react.SUMMARIZE_PROMPT

    def test_quantitative_metric_directive_present(self):
        """SUMMARIZE 必须强制量化指标指导"""
        assert "量化指标" in cn_react.SUMMARIZE_PROMPT


class TestBugfixConstraintsRetention:
    """Bug 修复约束保留性（防回退）"""

    def test_react_system_keeps_all_bugfix_constraints(self):
        """REACT_SYSTEM_PROMPT 必须保留全部 Bug 修复约束关键词"""
        prompt = cn_react.REACT_SYSTEM_PROMPT
        for keyword in REACT_BUGFIX_KEYWORDS:
            assert keyword in prompt, f"REACT_SYSTEM_PROMPT 丢失 Bug 修复约束: {keyword}"

    def test_execution_keeps_all_bugfix_constraints(self):
        """EXECUTION_PROMPT 必须保留全部 Bug 修复约束关键词"""
        prompt = cn_react.EXECUTION_PROMPT
        for keyword in EXECUTION_BUGFIX_KEYWORDS:
            assert keyword in prompt, f"EXECUTION_PROMPT 丢失 Bug 修复约束: {keyword}"


class TestDevNoiseAbsence:
    """开发噪声清除验证(批次优化: F10-7/WaitEvent/max_iterations 等代码常量名不得出现在提示词正文)"""

    @pytest.mark.parametrize("token", DEV_NOISE_TOKENS)
    def test_system_prompt_no_dev_noise(self, token):
        """SYSTEM_PROMPT 不得包含开发噪声标记"""
        assert token not in cn_system.SYSTEM_PROMPT, f"SYSTEM_PROMPT 残留开发噪声: {token}"

    @pytest.mark.parametrize("token", DEV_NOISE_TOKENS)
    def test_react_system_no_dev_noise(self, token):
        """REACT_SYSTEM_PROMPT 不得包含开发噪声标记"""
        assert token not in cn_react.REACT_SYSTEM_PROMPT, f"REACT_SYSTEM_PROMPT 残留开发噪声: {token}"

    @pytest.mark.parametrize("token", DEV_NOISE_TOKENS)
    def test_execution_no_dev_noise(self, token):
        """EXECUTION_PROMPT 不得包含开发噪声标记"""
        assert token not in cn_react.EXECUTION_PROMPT, f"EXECUTION_PROMPT 残留开发噪声: {token}"

    @pytest.mark.parametrize("token", DEV_NOISE_TOKENS)
    def test_planner_system_no_dev_noise(self, token):
        """PLANNER_SYSTEM_PROMPT 不得包含开发噪声标记"""
        assert token not in cn_planner.PLANNER_SYSTEM_PROMPT, f"PLANNER_SYSTEM_PROMPT 残留开发噪声: {token}"


# 通用型智能体提示词不应硬编码具体业务系统名或业务数据示例
# 应使用通用示例: mcp_xxx_export、业务数据(如订单/库存/报表)、经营分析报告.docx
GENERIC_VIOLATION_TOKENS = [
    "system",
    "getOutboundDetailExport",
    "getWarehousingDetailExport",
    "出入库分析报告",
    "出入库数据",
    "库存数据",
    "销售订单",
    "财务报表",
    "会员数据",
    "入库明细",
]


class TestGenericPromptAbsence:
    """通用型提示词验证: 不得硬编码具体业务系统名或业务数据示例

    通用型智能体提示词应使用通用示例(如 mcp_xxx_export、订单/库存/报表),
    而非具体业务系统名(如 system)或具体业务数据(如出入库数据)。
    """

    @pytest.mark.parametrize("token", GENERIC_VIOLATION_TOKENS)
    def test_system_prompt_no_business_hardcode(self, token):
        """SYSTEM_PROMPT 不得包含具体业务系统名或业务数据示例"""
        assert token not in cn_system.SYSTEM_PROMPT, (
            f"SYSTEM_PROMPT 残留业务硬编码: {token}"
        )

    @pytest.mark.parametrize("token", GENERIC_VIOLATION_TOKENS)
    def test_react_system_no_business_hardcode(self, token):
        """REACT_SYSTEM_PROMPT 不得包含具体业务系统名或业务数据示例"""
        assert token not in cn_react.REACT_SYSTEM_PROMPT, (
            f"REACT_SYSTEM_PROMPT 残留业务硬编码: {token}"
        )

    @pytest.mark.parametrize("token", GENERIC_VIOLATION_TOKENS)
    def test_execution_no_business_hardcode(self, token):
        """EXECUTION_PROMPT 不得包含具体业务系统名或业务数据示例"""
        assert token not in cn_react.EXECUTION_PROMPT, (
            f"EXECUTION_PROMPT 残留业务硬编码: {token}"
        )

    @pytest.mark.parametrize("token", GENERIC_VIOLATION_TOKENS)
    def test_planner_system_no_business_hardcode(self, token):
        """PLANNER_SYSTEM_PROMPT 不得包含具体业务系统名或业务数据示例"""
        assert token not in cn_planner.PLANNER_SYSTEM_PROMPT, (
            f"PLANNER_SYSTEM_PROMPT 残留业务硬编码: {token}"
        )

    @pytest.mark.parametrize("token", GENERIC_VIOLATION_TOKENS)
    def test_fragments_no_business_hardcode(self, token):
        """_fragments.py 不得包含具体业务系统名或业务数据示例"""
        from app.domain.services.prompts import _fragments
        # 检查所有以 _CN 结尾的片段常量
        for attr_name in dir(_fragments):
            if attr_name.endswith("_CN") and attr_name.isupper():
                fragment = getattr(_fragments, attr_name)
                if isinstance(fragment, str):
                    assert token not in fragment, (
                        f"_fragments.{attr_name} 残留业务硬编码: {token}"
                    )


class TestMCPGuidanceRetention:
    """MCP专业工具引导完整性（直接加载模式提示词引导）

    直接加载模式: MCP工具全量加载到工具列表,以mcp_前缀标识,
    LLM直接调用,无需桥接工具(search/describe/call)中间步骤。
    """

    def test_planner_system_has_mcp_awareness(self):
        """PLANNER_SYSTEM_PROMPT 必须包含MCP专业工具感知段"""
        prompt = cn_planner.PLANNER_SYSTEM_PROMPT
        assert "MCP专业工具感知" in prompt
        assert "直接加载" in prompt
        # 直接加载模式: 规划阶段不出现桥接工具名
        assert "mcp_tool_search" not in prompt
        assert "mcp_tool_describe" not in prompt
        assert "mcp_tool_call" not in prompt

    def test_planner_create_has_step_granularity(self):
        """CREATE_PLAN_PROMPT 必须包含步骤粒度原则"""
        prompt = cn_planner.CREATE_PLAN_PROMPT
        assert "步骤粒度原则" in prompt
        assert "业务目标" in prompt
        assert "mcp_xxx" in prompt
        assert "执行阶段细节" in prompt

    def test_react_system_has_mcp_usage_guide(self):
        """REACT_SYSTEM_PROMPT 必须包含MCP直接调用使用指南"""
        prompt = cn_react.REACT_SYSTEM_PROMPT
        assert "MCP专业工具使用指南" in prompt
        # 直接加载模式关键词
        assert "直接调用" in prompt
        assert "mcp_" in prompt
        assert "全量加载" in prompt
        # 桥接工具名不得出现(已移除)
        assert "mcp_tool_search" not in prompt
        assert "mcp_tool_describe" not in prompt
        assert "mcp_tool_call" not in prompt

    def test_mcp_guidance_uses_generic_examples(self):
        """MCP引导示例必须使用通用名称(mcp_xxx)，不得含特化业务名"""
        for token in SPECIALIZED_TOKENS:
            assert token not in cn_react.REACT_SYSTEM_PROMPT
            assert token not in cn_planner.PLANNER_SYSTEM_PROMPT
            assert token not in cn_planner.CREATE_PLAN_PROMPT


class TestBrowserRefMapGuidance:
    """浏览器 ref_map 交互引导完整性"""

    def test_browser_rules_has_ref_map_format(self):
        """browser_rules 必须包含 ref_map 格式说明"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "[@eN]" in prompt
        assert "ref_map" in prompt

    def test_cn_browser_rules_has_ref_priority(self):
        """CN browser_rules 必须包含 ref 参数优先于 index 的引导"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "优先使用ref参数" in prompt
        assert "@e1" in prompt

    def test_cn_browser_rules_has_text_fallback(self):
        """CN browser_rules 必须包含 ref 失效时回退到 text 的兜底引导"""
        assert "回退到text" in cn_system.SYSTEM_PROMPT

    def test_browser_rules_has_dialog_handling(self):
        """browser_rules 必须包含 pending_dialogs 对话框处理引导"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "pending_dialogs" in prompt
        assert "browser_respond_dialog" in prompt

    def test_cn_browser_rules_has_wait_scope_constraint(self):
        """CN browser_rules 必须包含 browser_wait 使用边界约束"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "browser_wait" in prompt
        assert "shell_execute(sleep N)" in prompt
        assert "非浏览器" in prompt or "MCP异步" in prompt

    def test_cn_browser_rules_has_observe_act_reobserve_loop(self):
        """CN browser_rules 必须包含 观察→行动→重观察闭环 引导"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "观察→行动→重观察" in prompt
        assert "重新调用browser_view" in prompt or "重新browser_view" in prompt

    def test_cn_browser_rules_has_ref_no_reuse(self):
        """CN browser_rules 必须包含 ref绝不复用 引导"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "ref绝不复用" in prompt
        assert "重渲染" in prompt

    def test_cn_browser_rules_has_priority_order(self):
        """CN browser_rules 必须包含 ref>text>index>coordinate 优先级"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "ref > text > index > coordinate" in prompt

    def test_cn_browser_rules_has_wait_for_priority(self):
        """CN browser_rules 必须包含 browser_wait_for 优先于 browser_wait"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "browser_wait_for" in prompt
        assert "browser_wait_for优先于browser_wait" in prompt

    def test_cn_browser_rules_has_network_requests(self):
        """CN browser_rules 必须包含 browser_network_requests 异步排查引导"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "browser_network_requests" in prompt
        assert "异步" in prompt

    def test_cn_browser_rules_has_include_diff(self):
        """CN browser_rules 必须包含 include_diff 重渲染检测引导"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "include_diff" in prompt
        assert "重渲染" in prompt


class TestBrowserWaitToolDescription:
    """browser_wait 工具描述使用边界约束测试"""

    def _get_browser_wait_description(self):
        """从BrowserTool类中提取browser_wait工具的description"""
        from app.domain.services.tools.browser import BrowserTool

        for attr_name in dir(BrowserTool):
            attr = getattr(BrowserTool, attr_name, None)
            if hasattr(attr, "_tool_name") and getattr(attr, "_tool_name", "") == "browser_wait":
                return getattr(attr, "_tool_description", "")
        return ""

    def test_description_contains_browser_scope(self):
        """browser_wait description 必须明确仅用于浏览器场景"""
        desc = self._get_browser_wait_description()
        assert desc, "未找到browser_wait工具的description"
        assert "浏览器" in desc or "DOM" in desc

    def test_description_prohibits_non_browser_usage(self):
        """browser_wait description 必须明确禁止非浏览器任务使用"""
        desc = self._get_browser_wait_description()
        assert desc, "未找到browser_wait工具的description"
        assert "禁止" in desc or "不得" in desc

    def test_description_redirects_to_shell_execute_sleep(self):
        """browser_wait description 必须引导非浏览器场景使用shell_execute(sleep N)"""
        desc = self._get_browser_wait_description()
        assert desc, "未找到browser_wait工具的description"
        assert "shell_execute(sleep N)" in desc

    def test_description_mentions_mcp_async_scenario(self):
        """browser_wait description 必须提及MCP异步任务场景作为禁用示例"""
        desc = self._get_browser_wait_description()
        assert desc, "未找到browser_wait工具的description"
        assert "MCP异步" in desc or "MCP" in desc


class TestMcpRulesGuidance:
    """SYSTEM_PROMPT mcp_rules 区块完整性测试（直接加载模式）

    直接加载模式: mcp_rules 引导LLM直接调用mcp_前缀工具,
    无需桥接工具(search/describe/call)中间步骤。
    """

    def test_cn_system_has_mcp_rules_block(self):
        """CN SYSTEM_PROMPT 必须包含 <mcp_rules> 区块"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "<mcp_rules>" in prompt
        assert "</mcp_rules>" in prompt

    def test_cn_mcp_rules_has_direct_call_principle(self):
        """CN mcp_rules 必须包含MCP工具直接调用原则"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "MCP工具直接调用" in prompt
        assert "mcp_" in prompt
        assert "直接" in prompt

    def test_cn_mcp_rules_has_tool_selection_guide(self):
        """CN mcp_rules 必须包含工具选择引导"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "工具选择" in prompt

    def test_cn_mcp_rules_no_bridge_tools(self):
        """CN mcp_rules 不得包含桥接工具名(直接加载模式硬约束)"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "mcp_tool_search" not in prompt
        assert "mcp_tool_describe" not in prompt
        assert "mcp_tool_call" not in prompt

    def test_cn_mcp_rules_has_async_wait_guidance(self):
        """CN mcp_rules 必须包含异步任务通知引导(task_wait)"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "task_wait(task_id" in prompt
        assert "P11" not in prompt
        assert "sleep等待总次数上限为5次" not in prompt

    def test_cn_mcp_rules_has_polling_param_fixation(self):
        """CN mcp_rules 必须包含轮询查询参数策略"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "轮询查询参数策略" in prompt
        assert "严禁仅传status=0" in prompt
        assert "推荐不传status查询所有状态" in prompt

    def test_cn_mcp_rules_has_system_hint_marker(self):
        """CN mcp_rules 必须包含[系统提示]标记识别引导"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "[系统提示]" in prompt
        assert "停止轮询" in prompt

    def test_mcp_rules_no_debug_markers(self):
        """mcp_rules 不得包含调试标记和代码常量名(项目硬约束)"""
        prompt = cn_system.SYSTEM_PROMPT
        for marker in ["P11", "_MAX_POLL_THRESHOLD"]:
            assert marker not in prompt, f"CN mcp_rules 仍含调试标记: {marker}"


class TestMcpRulesLayering:
    """mcp_rules 分层设计验证(优化E: 退避细节从CORE移到EXTRA)

    验证 Planner 仅加载 CORE(简化版 mcp_rules,不含退避序列细节),
    ReAct 加载完整 SYSTEM_PROMPT(含退避策略)。
    """

    def test_core_mcp_rules_is_concise(self):
        """SYSTEM_PROMPT_CORE 的 mcp_rules 应精简(不含退避序列细节)"""
        core = cn_system.SYSTEM_PROMPT_CORE
        # CORE 应包含基本 mcp_rules(直接加载模式)
        assert "<mcp_rules>" in core
        assert "MCP工具直接调用" in core
        # CORE 不应包含退避序列细节(已迁移到 EXTRA)
        assert "60→120→180→180→180" not in core

    def test_full_system_has_execution_rules(self):
        """完整 SYSTEM_PROMPT 应包含 mcp_execution_rules(退避细节)"""
        full = cn_system.SYSTEM_PROMPT
        assert "<mcp_execution_rules>" in full
        assert "60→120→180→180→180" in full

    def test_planner_does_not_load_execution_rules(self):
        """PlannerAgent 系统提示(CORE+PLANNER)不应包含 mcp_execution_rules"""
        from app.domain.services.agents.planner import PlannerAgent
        planner_prompt = PlannerAgent._system_prompt
        assert "<mcp_execution_rules>" not in planner_prompt
        # 但应包含基本 mcp_rules
        assert "<mcp_rules>" in planner_prompt


class TestDeliveryQualityOptimization:
    """结果交付质量优化测试"""

    DELIVERY_RULES_KEYWORDS_CN = [
        "结构化交付",
        "格式智能选择",
        "综合提炼能力",
        "量化指标呈现",
        "交付物自验证",
    ]

    REACT_QUALITY_AWARENESS_CN = [
        "信息收集意识",
        "结构化思维",
        "交付物质量预判",
        "综合提炼准备",
    ]

    SUMMARIZE_FILE_CONSTRAINTS_CN = [
        "结构化交付",
        "综合提炼",
        "量化指标",
        "attachments 字段",
    ]

    @pytest.mark.parametrize("keyword", DELIVERY_RULES_KEYWORDS_CN)
    def test_cn_delivery_rules_has_keyword(self, keyword):
        """CN system.py delivery_rules 必须包含交付质量关键词"""
        assert keyword in cn_system.SYSTEM_PROMPT, f"CN delivery_rules 缺失: {keyword}"

    @pytest.mark.parametrize("keyword", REACT_QUALITY_AWARENESS_CN)
    def test_cn_react_has_quality_awareness(self, keyword):
        """CN REACT_SYSTEM_PROMPT 必须包含执行质量意识关键词"""
        assert keyword in cn_react.REACT_SYSTEM_PROMPT, f"CN REACT 缺失质量意识: {keyword}"

    @pytest.mark.parametrize("keyword", SUMMARIZE_FILE_CONSTRAINTS_CN)
    def test_cn_summarize_has_file_constraints(self, keyword):
        """CN SUMMARIZE_PROMPT 必须包含文件交付约束关键词"""
        assert keyword in cn_react.SUMMARIZE_PROMPT, f"CN SUMMARIZE 缺失: {keyword}"


class TestHighQualityDeliveryOptimization:
    """高质量交付优化测试"""

    DELIVERY_RULES_ADVANCED_CN = [
        "用户预期超越原则",
        "多交付物互补原则",
        "交付物命名规范",
        "交付物完整性自检清单",
    ]

    CREATE_PLAN_DELIVERY_CN = [
        "交付质量规划",
        "交付物清单明确",
        "用户预期超越",
    ]

    REACT_DELIVERY_CONSTRAINTS_CN = [
        "交付物完整性自检",
        "用户预期对齐",
        "文件命名语义化",
    ]

    EXECUTION_SELFCHECK_CN = [
        "交付物自验证清单",
        "用户预期对齐",
    ]

    SUMMARIZE_ADVANCED_CN = [
        "交付物质量自检",
        "交付物清单完整性",
        "后续建议",
    ]

    @pytest.mark.parametrize("keyword", DELIVERY_RULES_ADVANCED_CN)
    def test_cn_delivery_rules_has_advanced_keyword(self, keyword):
        """CN system.py delivery_rules 必须包含新增交付质量关键词"""
        assert keyword in cn_system.SYSTEM_PROMPT, f"CN delivery_rules 缺失新增关键词: {keyword}"

    @pytest.mark.parametrize("keyword", CREATE_PLAN_DELIVERY_CN)
    def test_cn_create_plan_has_delivery_planning(self, keyword):
        """CN CREATE_PLAN_PROMPT 必须包含交付质量规划关键词"""
        assert keyword in cn_planner.CREATE_PLAN_PROMPT, f"CN CREATE_PLAN 缺失交付规划: {keyword}"

    @pytest.mark.parametrize("keyword", REACT_DELIVERY_CONSTRAINTS_CN)
    def test_cn_react_has_delivery_constraints(self, keyword):
        """CN REACT_SYSTEM_PROMPT 必须包含交付物质量约束关键词"""
        assert keyword in cn_react.REACT_SYSTEM_PROMPT, f"CN REACT 缺失交付约束: {keyword}"

    @pytest.mark.parametrize("keyword", EXECUTION_SELFCHECK_CN)
    def test_cn_execution_has_self_check(self, keyword):
        """CN EXECUTION_PROMPT 必须包含交付物自验证清单关键词"""
        assert keyword in cn_react.EXECUTION_PROMPT, f"CN EXECUTION 缺失自检清单: {keyword}"

    @pytest.mark.parametrize("keyword", SUMMARIZE_ADVANCED_CN)
    def test_cn_summarize_has_advanced_directives(self, keyword):
        """CN SUMMARIZE_PROMPT 必须包含新增交付质量自检+清单完整性+后续建议关键词"""
        assert keyword in cn_react.SUMMARIZE_PROMPT, f"CN SUMMARIZE 缺失新增指令: {keyword}"


class TestAttachmentHandlingOptimization:
    """附件处理优化测试(根治LLM误用browser_navigate访问本地附件)"""

    BROWSER_RULES_NEW_CN = [
        "网络 URL",
        "严禁用浏览器工具访问本地文件路径",
    ]

    FILE_RULES_NEW_CN = [
        "read_file 适用范围",
        "严禁用 read_file 直接读取二进制文件",
        "openpyxl/python-docx/pdfplumber/python-pptx/Pillow",
    ]

    PLANNER_ATTACHMENT_CN = [
        "附件识别与工具选择",
        "附件即本地文件",
        "扩展名识别策略",
        "附件技能优先",
        "规划步骤明确工具",
    ]

    CREATE_PLAN_ATTACHMENT_CN = [
        "附件处理规划",
        "严禁步骤中出现",
        "用浏览器打开附件",
    ]

    REACT_ATTACHMENT_CN = [
        "附件处理约束",
        "不是网络 URL,严禁用 browser_navigate 访问本地文件路径",
    ]

    EXECUTION_ATTACHMENT_CN = [
        "附件处理指引",
        "沙箱本地文件路径,不是网络 URL,严禁用 browser_navigate 访问",
    ]

    EXTENSION_TOOLS = [
        "openpyxl", "python-docx", "pdfplumber", "python-pptx", "Pillow", "read_file",
    ]

    @pytest.mark.parametrize("keyword", BROWSER_RULES_NEW_CN)
    def test_cn_browser_rules_has_attachment_constraint(self, keyword):
        """CN <browser_rules> 必须包含附件区分约束"""
        assert keyword in cn_system.SYSTEM_PROMPT, f"CN browser_rules 缺失新增约束: {keyword}"

    @pytest.mark.parametrize("keyword", FILE_RULES_NEW_CN)
    def test_cn_file_rules_has_binary_constraint(self, keyword):
        """CN <file_rules> 必须包含二进制文件处理约束"""
        assert keyword in cn_system.SYSTEM_PROMPT, f"CN file_rules 缺失新增约束: {keyword}"

    @pytest.mark.parametrize("keyword", PLANNER_ATTACHMENT_CN)
    def test_cn_planner_system_has_attachment_recognition(self, keyword):
        """CN PLANNER_SYSTEM_PROMPT 必须包含附件识别与工具选择原则"""
        assert keyword in cn_planner.PLANNER_SYSTEM_PROMPT, f"CN PLANNER_SYSTEM_PROMPT 缺失: {keyword}"

    @pytest.mark.parametrize("keyword", CREATE_PLAN_ATTACHMENT_CN)
    def test_cn_create_plan_has_attachment_planning(self, keyword):
        """CN CREATE_PLAN_PROMPT 必须包含附件处理规划段落"""
        assert keyword in cn_planner.CREATE_PLAN_PROMPT, f"CN CREATE_PLAN_PROMPT 缺失: {keyword}"

    @pytest.mark.parametrize("keyword", REACT_ATTACHMENT_CN)
    def test_cn_react_system_has_attachment_constraint(self, keyword):
        """CN REACT_SYSTEM_PROMPT 必须包含附件处理约束"""
        assert keyword in cn_react.REACT_SYSTEM_PROMPT, f"CN REACT_SYSTEM_PROMPT 缺失: {keyword}"

    @pytest.mark.parametrize("keyword", EXECUTION_ATTACHMENT_CN)
    def test_cn_execution_has_attachment_guidance(self, keyword):
        """CN EXECUTION_PROMPT 必须包含附件处理指引"""
        assert keyword in cn_react.EXECUTION_PROMPT, f"CN EXECUTION_PROMPT 缺失: {keyword}"

    @pytest.mark.parametrize("tool", EXTENSION_TOOLS)
    def test_cn_planner_has_all_extension_tools(self, tool):
        """CN PLANNER_SYSTEM_PROMPT 必须包含所有扩展名对应工具"""
        assert tool in cn_planner.PLANNER_SYSTEM_PROMPT, f"CN PLANNER_SYSTEM_PROMPT 缺失工具: {tool}"

    @pytest.mark.parametrize("tool", EXTENSION_TOOLS)
    def test_cn_react_system_has_all_extension_tools(self, tool):
        """CN REACT_SYSTEM_PROMPT 必须包含所有扩展名对应工具"""
        assert tool in cn_react.REACT_SYSTEM_PROMPT, f"CN REACT_SYSTEM_PROMPT 缺失工具: {tool}"

    @pytest.mark.parametrize("tool", EXTENSION_TOOLS)
    def test_cn_execution_has_all_extension_tools(self, tool):
        """CN EXECUTION_PROMPT 必须包含所有扩展名对应工具"""
        assert tool in cn_react.EXECUTION_PROMPT, f"CN EXECUTION_PROMPT 缺失工具: {tool}"

    def test_cn_browser_rules_explicitly_forbids_local_file(self):
        """CN <browser_rules> 必须显式禁止访问本地文件"""
        prompt = cn_system.SYSTEM_PROMPT
        assert "严禁用浏览器工具访问本地文件路径" in prompt
        assert "/home/ubuntu" in prompt

    def test_cn_create_plan_forbids_browser_for_attachment(self):
        """CN CREATE_PLAN_PROMPT 必须显式禁止用浏览器打开附件"""
        assert "用浏览器打开附件" in cn_planner.CREATE_PLAN_PROMPT


class TestDataSourceIdentificationOptimization:
    """数据来源识别优化测试(根治LLM在无附件场景误让用户上传附件)"""

    DATA_SOURCE_CN = [
        "数据来源识别",
        "业务数据需求识别",
        "订单/库存/报表",
        "MCP 工具",
        "严禁假设用户会提供附件",
        "严禁让用户上传附件作为前置条件",
        "兜底 询问用户",
        "附件场景判定",
    ]

    NO_ATTACHMENT_DECISION_CN = [
        "无附件场景决策",
        "仅当 attachments 字段非空",
        "严禁生成",
        "读取附件",
        "解析附件",
        "等待用户上传附件",
    ]

    @pytest.mark.parametrize("keyword", DATA_SOURCE_CN)
    def test_cn_planner_has_data_source_identification(self, keyword):
        """CN PLANNER_SYSTEM_PROMPT 必须包含"数据来源识别"段关键词"""
        assert keyword in cn_planner.PLANNER_SYSTEM_PROMPT, (
            f"CN PLANNER_SYSTEM_PROMPT 缺失数据来源识别关键词: {keyword}"
        )

    @pytest.mark.parametrize("keyword", NO_ATTACHMENT_DECISION_CN)
    def test_cn_create_plan_has_no_attachment_decision(self, keyword):
        """CN CREATE_PLAN_PROMPT 必须包含"无附件场景决策"段关键词"""
        assert keyword in cn_planner.CREATE_PLAN_PROMPT, (
            f"CN CREATE_PLAN_PROMPT 缺失无附件场景决策关键词: {keyword}"
        )

    def test_data_source_identification_no_specialized_tokens(self):
        """数据来源识别段必须保持通用性,不得含特化业务名"""
        for token in SPECIALIZED_TOKENS:
            assert token not in cn_planner.PLANNER_SYSTEM_PROMPT, (
                f"CN PLANNER_SYSTEM_PROMPT 数据来源识别段含特化业务名: {token}"
            )
            assert token not in cn_planner.CREATE_PLAN_PROMPT, (
                f"CN CREATE_PLAN_PROMPT 数据来源识别段含特化业务名: {token}"
            )

    def test_cn_attachment_planning_has_nonempty_check(self):
        """CN CREATE_PLAN_PROMPT "附件处理规划"段必须有"仅当 attachments 字段非空"前置判断"""
        assert "仅当 attachments 字段非空" in cn_planner.CREATE_PLAN_PROMPT


class TestImageAttachmentOptimization:
    """图片附件识别决策树 + 并行工具语义去重优化测试"""

    IMAGE_TREE_CN = [
        "图片附件识别决策树",
        "首选 MCP",
        "mcp_mcp-multimodal_vl_image_understand",
        "次选 shell_execute + Pillow",
        "严禁 browser_navigate 访问本地图片",
        "file:///",
    ]

    PARALLEL_DEDUP_CN = [
        "并行工具调用语义去重",
        "严禁同一目标并行调用语义重复工具",
        "反例",
        "正例",
        "browser_navigate(file:///...)",
        "shell_execute(PIL Image.open)",
        "mcp_mcp-multimodal_vl_image_understand",
    ]

    OLD_IMAGE_EXCEPTION_CN = ["图片除外", "可用 browser_navigate 查看本地图片", "图片是 browser_navigate 的唯一合法本地用途"]

    @pytest.mark.parametrize("keyword", IMAGE_TREE_CN)
    def test_cn_react_has_image_decision_tree(self, keyword):
        """CN REACT_SYSTEM_PROMPT 必须包含图片附件识别决策树所有关键词"""
        assert keyword in cn_react.REACT_SYSTEM_PROMPT, f"CN REACT_SYSTEM_PROMPT 缺失图片决策树关键词: {keyword}"

    @pytest.mark.parametrize("keyword", IMAGE_TREE_CN)
    def test_cn_execution_has_image_decision_tree(self, keyword):
        """CN EXECUTION_PROMPT 必须包含图片附件识别决策树所有关键词"""
        assert keyword in cn_react.EXECUTION_PROMPT, f"CN EXECUTION_PROMPT 缺失图片决策树关键词: {keyword}"

    @pytest.mark.parametrize("keyword", PARALLEL_DEDUP_CN)
    def test_cn_react_has_parallel_dedup(self, keyword):
        """CN REACT_SYSTEM_PROMPT 必须包含并行工具语义去重约束所有关键词"""
        assert keyword in cn_react.REACT_SYSTEM_PROMPT, f"CN REACT_SYSTEM_PROMPT 缺失并行去重关键词: {keyword}"

    @pytest.mark.parametrize("keyword", OLD_IMAGE_EXCEPTION_CN)
    def test_cn_react_no_image_exception_misleading(self, keyword):
        """CN REACT_SYSTEM_PROMPT 必须移除旧版'图片除外'误导词(防回退)"""
        assert keyword not in cn_react.REACT_SYSTEM_PROMPT, f"CN REACT_SYSTEM_PROMPT 残留误导词: {keyword}"

    @pytest.mark.parametrize("keyword", OLD_IMAGE_EXCEPTION_CN)
    def test_cn_execution_no_image_exception_misleading(self, keyword):
        """CN EXECUTION_PROMPT 必须移除旧版'图片除外'误导词(防回退)"""
        assert keyword not in cn_react.EXECUTION_PROMPT, f"CN EXECUTION_PROMPT 残留误导词: {keyword}"

    @pytest.mark.parametrize("keyword", OLD_IMAGE_EXCEPTION_CN)
    def test_cn_attachment_extension_map_no_misleading(self, keyword):
        """CN ATTACHMENT_EXTENSION_MAP_CN 必须移除旧版'图片除外'误导词"""
        from app.domain.services.prompts._fragments import ATTACHMENT_EXTENSION_MAP_CN
        assert keyword not in ATTACHMENT_EXTENSION_MAP_CN, f"CN ATTACHMENT_EXTENSION_MAP 残留误导词: {keyword}"

    def test_cn_react_has_correct_image_guidance(self):
        """CN REACT_SYSTEM_PROMPT 必须包含正确引导词'包括图片附件'"""
        assert "包括图片附件" in cn_react.REACT_SYSTEM_PROMPT
        assert "mcp_mcp-multimodal_vl_image_understand" in cn_react.REACT_SYSTEM_PROMPT

    def test_cn_planner_create_has_correct_image_guidance(self):
        """CN CREATE_PLAN_PROMPT 必须包含正确引导词'包括图片附件'"""
        assert "包括图片附件" in cn_planner.CREATE_PLAN_PROMPT

    def test_image_tree_uses_generic_tokens(self):
        """图片决策树与并行去重不得含特化业务名(通用性)"""
        from app.domain.services.prompts._fragments import (
            IMAGE_ATTACHMENT_DECISION_TREE_CN,
            PARALLEL_TOOL_DEDUP_CN,
        )
        for token in SPECIALIZED_TOKENS:
            assert token not in IMAGE_ATTACHMENT_DECISION_TREE_CN, f"IMAGE_TREE_CN 含特化词: {token}"
            assert token not in PARALLEL_TOOL_DEDUP_CN, f"PARALLEL_DEDUP_CN 含特化词: {token}"


class TestAsyncTaskOptimization:
    """Sleep 异步任务提示词优化测试"""

    ASYNC_TREE_CN = [
        "异步任务处理决策树",
        "场景A:长耗时Shell命令",
        "场景B:MCP异步任务",
        "场景C:[系统提示]标记触发",
        "async_mode=true",
        "task_wait(task_id",
        "期间不消耗LLM token",
        "递增退避(60s/120s/180s)",
        "60s", "120s", "180s",
        "最多10次",
        "停止轮询",
        "长期规划",
        "严禁",
    ]

    OLD_SLEEP_PROHIBITION_CN = [
        "禁止使用sleep等待异步任务",
        "严禁使用 `sleep N` 命令等待异步任务完成",
    ]

    def test_cn_react_has_async_decision_tree(self):
        """CN REACT_SYSTEM_PROMPT 必须包含异步任务决策树所有关键词"""
        prompt = cn_react.REACT_SYSTEM_PROMPT
        for kw in self.ASYNC_TREE_CN:
            assert kw in prompt, f"CN REACT 缺失异步决策树关键词: {kw}"

    def test_cn_react_no_absolute_sleep_prohibition(self):
        """CN REACT 必须移除"禁止使用sleep"绝对禁止词"""
        prompt = cn_react.REACT_SYSTEM_PROMPT
        for kw in self.OLD_SLEEP_PROHIBITION_CN:
            assert kw not in prompt, f"CN REACT 仍含旧版绝对禁止词: {kw}"

    def test_cn_react_keeps_idempotent_and_file_reuse(self):
        """CN REACT 必须保留幂等禁止/文件复用约束"""
        prompt = cn_react.REACT_SYSTEM_PROMPT
        assert "重复发起幂等写操作禁止" in prompt
        assert "生成文件复用" in prompt

    def test_cn_system_mcp_rules_aligned_with_mcp_py(self):
        """CN system.py mcp_rules 与 mcp.py 异步任务机制对齐(直接加载模式)"""
        prompt = cn_system.SYSTEM_PROMPT
        # 直接加载模式: 异步任务通过 task_wait 等待,无需桥接工具
        assert "task_wait(task_id" in prompt
        assert "mcp_tool_call" not in prompt  # 桥接工具已移除
        assert "P11" not in prompt
        assert "_MAX_POLL_THRESHOLD" not in prompt

    def test_async_tree_uses_generic_tokens(self):
        """异步决策树示例必须使用通用名称,不得含特化业务名"""
        from app.domain.services.prompts._fragments import ASYNC_TASK_DECISION_TREE_CN
        for token in SPECIALIZED_TOKENS:
            assert token not in ASYNC_TASK_DECISION_TREE_CN, (
                f"ASYNC_TREE_CN 含特化词: {token}"
            )

    def test_react_bugfix_keywords_retained(self):
        """不得破坏 5b54ddc 基线 + MCP系统方法优化约束"""
        prompt = cn_react.REACT_SYSTEM_PROMPT
        for keyword in REACT_BUGFIX_KEYWORDS:
            assert keyword in prompt, f"REACT_SYSTEM_PROMPT 丢失 Bug 修复约束: {keyword}"


class TestDeepResearchGuidance:
    """deep_research 工具使用引导提示词测试"""

    DEEP_RESEARCH_REACT_CN = [
        "深度研究工具使用指南",
        "深度研究优先",
        "deep_research 工具",
        "深度搜索",
        "deep_research 优势",
        "使用时机判断",
        "避免冗余",
        "预算意识",
    ]

    DEEP_RESEARCH_PLANNER_CN = [
        "搜索规划",
        "使用 deep_research 工具研究",
        "复杂研究任务",
        "简单事实查询",
        "deep_research 会话级上限 2 次",
    ]

    def test_cn_react_has_deep_research_guide(self):
        """CN REACT_SYSTEM_PROMPT 必须包含深度研究工具使用指南"""
        prompt = cn_react.REACT_SYSTEM_PROMPT
        for kw in self.DEEP_RESEARCH_REACT_CN:
            assert kw in prompt, f"CN REACT 缺失 deep_research 引导关键词: {kw}"

    def test_cn_create_plan_has_deep_research_planning(self):
        """CN CREATE_PLAN_PROMPT 必须包含 deep_research 搜索规划引导"""
        prompt = cn_planner.CREATE_PLAN_PROMPT
        for kw in self.DEEP_RESEARCH_PLANNER_CN:
            assert kw in prompt, f"CN CREATE_PLAN 缺失 deep_research 规划关键词: {kw}"

    def test_planner_infra_tools_includes_deep_research(self):
        """PlannerAgent _INFRA_TOOL_NAMES 必须包含 deep_research(最高优先级类别)"""
        from app.domain.services.agents.planner import PlannerAgent
        assert "deep_research" in PlannerAgent._INFRA_TOOL_NAMES


class TestDataPersistenceOptimization:
    """数据持久化与批量完整性优化测试"""

    DATA_PERSISTENCE_KEYWORDS_CN = [
        "数据即时持久化",
        "批量任务完整性验证",
        "已查看",
        "已保存",
        "恢复导航不算重复操作",
    ]

    PARTIAL_COMPLETION_KEYWORDS_CN = [
        "部分完成",
        "完整完成",
        "防止误删后续步骤",
    ]

    def test_fragments_has_data_persistence_cn(self):
        """_fragments.py 必须定义 DATA_PERSISTENCE_CN"""
        from app.domain.services.prompts._fragments import DATA_PERSISTENCE_CN
        assert DATA_PERSISTENCE_CN, "DATA_PERSISTENCE_CN 未定义或为空"

    def test_fragments_has_partial_completion_cn(self):
        """_fragments.py 必须定义 PARTIAL_COMPLETION_PRINCIPLE_CN"""
        from app.domain.services.prompts._fragments import PARTIAL_COMPLETION_PRINCIPLE_CN
        assert PARTIAL_COMPLETION_PRINCIPLE_CN, "PARTIAL_COMPLETION_PRINCIPLE_CN 未定义或为空"

    @pytest.mark.parametrize("keyword", DATA_PERSISTENCE_KEYWORDS_CN)
    def test_data_persistence_cn_has_keywords(self, keyword):
        """DATA_PERSISTENCE_CN 必须包含所有关键内容"""
        from app.domain.services.prompts._fragments import DATA_PERSISTENCE_CN
        assert keyword in DATA_PERSISTENCE_CN, f"DATA_PERSISTENCE_CN 缺失: {keyword}"

    @pytest.mark.parametrize("keyword", PARTIAL_COMPLETION_KEYWORDS_CN)
    def test_partial_completion_cn_has_keywords(self, keyword):
        """PARTIAL_COMPLETION_PRINCIPLE_CN 必须包含所有关键内容"""
        from app.domain.services.prompts._fragments import PARTIAL_COMPLETION_PRINCIPLE_CN
        assert keyword in PARTIAL_COMPLETION_PRINCIPLE_CN, f"PARTIAL_COMPLETION_PRINCIPLE_CN 缺失: {keyword}"

    @pytest.mark.parametrize("keyword", DATA_PERSISTENCE_KEYWORDS_CN)
    def test_cn_react_system_has_data_persistence(self, keyword):
        """CN REACT_SYSTEM_PROMPT 必须包含数据持久化约束"""
        assert keyword in cn_react.REACT_SYSTEM_PROMPT, \
            f"CN REACT_SYSTEM_PROMPT 缺失数据持久化约束: {keyword}"

    @pytest.mark.parametrize("keyword", DATA_PERSISTENCE_KEYWORDS_CN)
    def test_cn_execution_has_data_persistence(self, keyword):
        """CN EXECUTION_PROMPT 必须包含数据持久化约束"""
        assert keyword in cn_react.EXECUTION_PROMPT, \
            f"CN EXECUTION_PROMPT 缺失数据持久化约束: {keyword}"

    @pytest.mark.parametrize("keyword", PARTIAL_COMPLETION_KEYWORDS_CN)
    def test_cn_update_plan_has_partial_completion(self, keyword):
        """CN UPDATE_PLAN_PROMPT 必须包含部分完成原则"""
        assert keyword in cn_planner.UPDATE_PLAN_PROMPT, \
            f"CN UPDATE_PLAN_PROMPT 缺失部分完成原则: {keyword}"

    def test_data_persistence_cn_mentions_write_file(self):
        """DATA_PERSISTENCE_CN 必须引导使用 write_file 持久化"""
        from app.domain.services.prompts._fragments import DATA_PERSISTENCE_CN
        assert "write_file" in DATA_PERSISTENCE_CN, "DATA_PERSISTENCE_CN 必须提到 write_file"

    def test_execution_prompt_placeholders_unchanged(self):
        """EXECUTION_PROMPT 占位符集合不变(防误删占位符)"""
        cn_placeholders = _extract_placeholders(cn_react.EXECUTION_PROMPT)
        assert cn_placeholders == PLACEHOLDERS["EXECUTION_PROMPT"], \
            f"EXECUTION_PROMPT 占位符变化: 期望 {PLACEHOLDERS['EXECUTION_PROMPT']}, 实际 {cn_placeholders}"

    def test_update_plan_prompt_placeholders_unchanged(self):
        """UPDATE_PLAN_PROMPT 占位符集合不变(防误删占位符)"""
        cn_placeholders = _extract_placeholders(cn_planner.UPDATE_PLAN_PROMPT)
        assert cn_placeholders == PLACEHOLDERS["UPDATE_PLAN_PROMPT"], \
            f"UPDATE_PLAN_PROMPT 占位符变化: 期望 {PLACEHOLDERS['UPDATE_PLAN_PROMPT']}, 实际 {cn_placeholders}"


class TestBudgetExceededStrategy:
    """预算超限响应策略 + task_wait 完成后行为约束测试"""

    def test_async_tree_cn_has_task_wait_completion_constraint(self):
        """ASYNC_TASK_DECISION_TREE_CN 必须包含 task_wait 完成后行为约束"""
        from app.domain.services.prompts._fragments import ASYNC_TASK_DECISION_TREE_CN
        assert "task_wait 完成后行为约束" in ASYNC_TASK_DECISION_TREE_CN
        assert "getDownloadTaskList" in ASYNC_TASK_DECISION_TREE_CN

    def test_budget_exceeded_strategy_cn_exists_and_nonempty(self):
        """BUDGET_EXCEEDED_STRATEGY_CN 存在且非空"""
        from app.domain.services.prompts._fragments import BUDGET_EXCEEDED_STRATEGY_CN
        assert BUDGET_EXCEEDED_STRATEGY_CN, "BUDGET_EXCEEDED_STRATEGY_CN 不应为空"
        assert len(BUDGET_EXCEEDED_STRATEGY_CN) > 50

    def test_budget_exceeded_strategy_cn_has_key_directives(self):
        """BUDGET_EXCEEDED_STRATEGY_CN 包含关键指令: 立即停止+切换策略"""
        from app.domain.services.prompts._fragments import BUDGET_EXCEEDED_STRATEGY_CN
        assert "立即停止" in BUDGET_EXCEEDED_STRATEGY_CN
        assert "切换" in BUDGET_EXCEEDED_STRATEGY_CN
        assert "严禁继续重试" in BUDGET_EXCEEDED_STRATEGY_CN

    def test_react_system_prompt_has_budget_strategy(self):
        """REACT_SYSTEM_PROMPT 必须包含预算超限响应策略"""
        assert "预算超限响应策略" in cn_react.REACT_SYSTEM_PROMPT

    def test_execution_prompt_has_budget_strategy(self):
        """EXECUTION_PROMPT 必须包含预算超限响应策略"""
        assert "预算超限响应策略" in cn_react.EXECUTION_PROMPT

    def test_react_system_prompt_has_task_wait_constraint(self):
        """REACT_SYSTEM_PROMPT 必须包含 task_wait 完成后行为约束"""
        assert "task_wait 完成后行为约束" in cn_react.REACT_SYSTEM_PROMPT


class TestMatplotlibChineseFontGuidance:
    """matplotlib 中文字体引导提示词测试

    防回退: 确保 LLM 不会硬编码 SimHei 导致中文乱码。
    沙箱通过 sitecustomize.py 自动配置字体 + 注册 SimHei 别名,
    提示词层引导 LLM 不要手动覆盖字体设置。
    """

    MATPLOTLIB_GUIDE_KEYWORDS_CN = [
        "matplotlib 中文图表规范",
        "严禁手动覆盖字体设置",
        "SimHei",
        "WenQuanYi Micro Hei",
        "sitecustomize.py",
        "直接使用即可",
    ]

    FORBIDDEN_FONT_SUGGESTIONS = [
        "plt.rcParams['font.sans-serif'] = ['SimHei']",
    ]

    def test_fragments_has_matplotlib_chinese_font_cn(self):
        """_fragments.py 必须定义 MATPLOTLIB_CHINESE_FONT_CN"""
        from app.domain.services.prompts._fragments import MATPLOTLIB_CHINESE_FONT_CN
        assert MATPLOTLIB_CHINESE_FONT_CN, "MATPLOTLIB_CHINESE_FONT_CN 未定义或为空"
        assert len(MATPLOTLIB_CHINESE_FONT_CN) > 100

    @pytest.mark.parametrize("keyword", MATPLOTLIB_GUIDE_KEYWORDS_CN)
    def test_matplotlib_guide_cn_has_keywords(self, keyword):
        """MATPLOTLIB_CHINESE_FONT_CN 必须包含所有关键引导内容"""
        from app.domain.services.prompts._fragments import MATPLOTLIB_CHINESE_FONT_CN
        assert keyword in MATPLOTLIB_CHINESE_FONT_CN, \
            f"MATPLOTLIB_CHINESE_FONT_CN 缺失: {keyword}"

    @pytest.mark.parametrize("keyword", MATPLOTLIB_GUIDE_KEYWORDS_CN)
    def test_cn_execution_has_matplotlib_guide(self, keyword):
        """CN EXECUTION_PROMPT 必须包含 matplotlib 中文字体引导"""
        assert keyword in cn_react.EXECUTION_PROMPT, \
            f"CN EXECUTION_PROMPT 缺失 matplotlib 字体引导: {keyword}"

    def test_matplotlib_guide_prohibits_simhei_hardcode(self):
        """MATPLOTLIB_CHINESE_FONT_CN 必须明确禁止硬编码 SimHei"""
        from app.domain.services.prompts._fragments import MATPLOTLIB_CHINESE_FONT_CN
        # 必须包含禁止性引导(而非示例性引导)
        assert "严禁" in MATPLOTLIB_CHINESE_FONT_CN or "禁止" in MATPLOTLIB_CHINESE_FONT_CN
        assert "SimHei" in MATPLOTLIB_CHINESE_FONT_CN

    def test_matplotlib_guide_mentions_wqy_as_safe_font(self):
        """MATPLOTLIB_CHINESE_FONT_CN 必须引导使用 WenQuanYi Micro Hei 作为安全字体"""
        from app.domain.services.prompts._fragments import MATPLOTLIB_CHINESE_FONT_CN
        assert "WenQuanYi Micro Hei" in MATPLOTLIB_CHINESE_FONT_CN

    def test_matplotlib_guide_mentions_savefig_dpi(self):
        """MATPLOTLIB_CHINESE_FONT_CN 必须引导 savefig 使用 dpi=100"""
        from app.domain.services.prompts._fragments import MATPLOTLIB_CHINESE_FONT_CN
        assert "dpi=100" in MATPLOTLIB_CHINESE_FONT_CN
        assert "bbox_inches='tight'" in MATPLOTLIB_CHINESE_FONT_CN

    def test_system_prompt_mentions_font_auto_config(self):
        """system.py 必须说明 matplotlib 中文字体已自动配置"""
        assert "sitecustomize.py" in cn_system.SYSTEM_PROMPT or \
               "自动加载" in cn_system.SYSTEM_PROMPT

    def test_execution_prompt_placeholders_unchanged_with_matplotlib(self):
        """新增 matplotlib 引导后,EXECUTION_PROMPT 占位符集合不变"""
        cn_placeholders = _extract_placeholders(cn_react.EXECUTION_PROMPT)
        assert cn_placeholders == PLACEHOLDERS["EXECUTION_PROMPT"], \
            f"EXECUTION_PROMPT 占位符变化: 期望 {PLACEHOLDERS['EXECUTION_PROMPT']}, 实际 {cn_placeholders}"

    def test_matplotlib_guide_uses_generic_examples(self):
        """matplotlib 引导示例必须使用通用名称,不得含特化业务名"""
        from app.domain.services.prompts._fragments import MATPLOTLIB_CHINESE_FONT_CN
        for token in SPECIALIZED_TOKENS:
            assert token not in MATPLOTLIB_CHINESE_FONT_CN, \
                f"MATPLOTLIB_CHINESE_FONT_CN 含特化词: {token}"

    def test_matplotlib_guide_no_dev_noise(self):
        """matplotlib 引导不得包含开发噪声(代码常量名/批次标记)"""
        from app.domain.services.prompts._fragments import MATPLOTLIB_CHINESE_FONT_CN
        for noise in DEV_NOISE_TOKENS:
            assert noise not in MATPLOTLIB_CHINESE_FONT_CN, \
                f"MATPLOTLIB_CHINESE_FONT_CN 含开发噪声: {noise}"


class TestDataSourceIntegrityOptimization:
    """数据源完整性校验与告知优化测试

    根治问题: 用户请求"某月全部业务数据",智能体导出参数正确但业务系统数据
    只到月中。智能体在统计分析后才偶然发现数据范围不全,随后重新导出验证
    (参数本就正确,冗余),且未在交付物中显式标注数据覆盖范围,导致用户基于
    "全部数据"的错误前提做生产决策。

    防回退: 确保 DATA_SOURCE_INTEGRITY_CN 片段存在且包含关键约束,
    并被 REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用,
    SUMMARIZE_PROMPT 包含数据覆盖范围告知指令。
    """

    DATA_SOURCE_INTEGRITY_KEYWORDS_CN = [
        "数据源完整性校验与告知",
        "导出后立即校验",
        "区分根因",
        "数据源本身不完整",  # 泛化: 原"业务数据源不完整"→"数据源本身不完整"(通用化)
        "导出参数错误",
        "识别幂等旧结果",
        "交付物显式标注局限性",
        "交付消息主动告知",
    ]

    def test_fragments_has_data_source_integrity_cn(self):
        """_fragments.py 必须定义 DATA_SOURCE_INTEGRITY_CN"""
        from app.domain.services.prompts._fragments import DATA_SOURCE_INTEGRITY_CN
        assert DATA_SOURCE_INTEGRITY_CN, "DATA_SOURCE_INTEGRITY_CN 未定义或为空"
        assert len(DATA_SOURCE_INTEGRITY_CN) > 100

    @pytest.mark.parametrize("keyword", DATA_SOURCE_INTEGRITY_KEYWORDS_CN)
    def test_data_source_integrity_cn_has_keywords(self, keyword):
        """DATA_SOURCE_INTEGRITY_CN 必须包含所有关键约束"""
        from app.domain.services.prompts._fragments import DATA_SOURCE_INTEGRITY_CN
        assert keyword in DATA_SOURCE_INTEGRITY_CN, \
            f"DATA_SOURCE_INTEGRITY_CN 缺失关键约束: {keyword}"

    def test_data_source_integrity_cn_has_correct_negative_example(self):
        """DATA_SOURCE_INTEGRITY_CN 必须包含反例(冗余重新导出)"""
        from app.domain.services.prompts._fragments import DATA_SOURCE_INTEGRITY_CN
        # 反例应明确标注"重新导出(冗余,参数正确)"
        assert "冗余" in DATA_SOURCE_INTEGRITY_CN
        assert "重新导出" in DATA_SOURCE_INTEGRITY_CN

    def test_data_source_integrity_cn_mentions_shell_verification(self):
        """DATA_SOURCE_INTEGRITY_CN 必须引导用 shell_execute 校验数据覆盖范围(通用,不绑定pandas)"""
        from app.domain.services.prompts._fragments import DATA_SOURCE_INTEGRITY_CN
        # 泛化后: 用 shell_execute 校验,不绑定具体技术栈(pandas/df),保持通用型定位
        assert "shell_execute" in DATA_SOURCE_INTEGRITY_CN
        assert "校验数据覆盖范围" in DATA_SOURCE_INTEGRITY_CN

    def test_data_source_integrity_cn_no_business_specific_terms(self):
        """DATA_SOURCE_INTEGRITY_CN 不得包含业务特化术语(保持通用型智能体定位)"""
        from app.domain.services.prompts._fragments import DATA_SOURCE_INTEGRITY_CN
        # 防回退: 业务特化术语(订单/库存/出库/生产把控/经营)违反通用型定位
        business_terms = ["订单", "库存", "出库", "生产把控", "经营", "pandas", "df["]
        for term in business_terms:
            assert term not in DATA_SOURCE_INTEGRITY_CN, \
                f"DATA_SOURCE_INTEGRITY_CN 包含业务特化术语 '{term}',违反通用型智能体定位"

    @pytest.mark.parametrize("keyword", DATA_SOURCE_INTEGRITY_KEYWORDS_CN)
    def test_cn_react_system_has_data_source_integrity(self, keyword):
        """CN REACT_SYSTEM_PROMPT 必须包含数据源完整性校验约束"""
        assert keyword in cn_react.REACT_SYSTEM_PROMPT, \
            f"CN REACT_SYSTEM_PROMPT 缺失数据源完整性约束: {keyword}"

    @pytest.mark.parametrize("keyword", DATA_SOURCE_INTEGRITY_KEYWORDS_CN)
    def test_cn_execution_has_data_source_integrity(self, keyword):
        """CN EXECUTION_PROMPT 必须包含数据源完整性校验约束"""
        assert keyword in cn_react.EXECUTION_PROMPT, \
            f"CN EXECUTION_PROMPT 缺失数据源完整性约束: {keyword}"

    def test_cn_summarize_has_data_coverage_disclosure(self):
        """CN SUMMARIZE_PROMPT 必须包含数据覆盖范围告知指令"""
        assert "数据覆盖范围告知" in cn_react.SUMMARIZE_PROMPT
        assert "执行摘要首段" in cn_react.SUMMARIZE_PROMPT
        assert "未覆盖完整月份" in cn_react.SUMMARIZE_PROMPT

    def test_data_source_integrity_uses_generic_examples(self):
        """数据源完整性校验片段必须使用通用示例,不得含特化业务名"""
        from app.domain.services.prompts._fragments import DATA_SOURCE_INTEGRITY_CN
        for token in SPECIALIZED_TOKENS:
            assert token not in DATA_SOURCE_INTEGRITY_CN, \
                f"DATA_SOURCE_INTEGRITY_CN 含特化业务名: {token}"

    def test_data_source_integrity_no_dev_noise(self):
        """数据源完整性校验片段不得包含开发噪声(代码常量名/批次标记)"""
        from app.domain.services.prompts._fragments import DATA_SOURCE_INTEGRITY_CN
        for noise in DEV_NOISE_TOKENS:
            assert noise not in DATA_SOURCE_INTEGRITY_CN, \
                f"DATA_SOURCE_INTEGRITY_CN 含开发噪声: {noise}"

    def test_execution_prompt_placeholders_unchanged_with_data_source(self):
        """新增数据源完整性约束后,EXECUTION_PROMPT 占位符集合不变"""
        cn_placeholders = _extract_placeholders(cn_react.EXECUTION_PROMPT)
        assert cn_placeholders == PLACEHOLDERS["EXECUTION_PROMPT"], \
            f"EXECUTION_PROMPT 占位符变化: 期望 {PLACEHOLDERS['EXECUTION_PROMPT']}, 实际 {cn_placeholders}"

    def test_summarize_prompt_placeholders_unchanged_with_data_source(self):
        """新增数据覆盖范围告知后,SUMMARIZE_PROMPT 占位符集合不变"""
        cn_placeholders = _extract_placeholders(cn_react.SUMMARIZE_PROMPT)
        assert cn_placeholders == PLACEHOLDERS["SUMMARIZE_PROMPT"], \
            f"SUMMARIZE_PROMPT 占位符变化: 期望 {PLACEHOLDERS['SUMMARIZE_PROMPT']}, 实际 {cn_placeholders}"


class TestSummarizingFallbackDelivery:
    """SUMMARIZING 阶段终极兜底交付测试(根治'未生成最终交付'问题)

    根因: planner_react.py 中 Step 未导入, P0-2 附件门禁触发时抛 NameError,
    异常传播到 AgentTaskRunner._emit_degraded_summary(attachments=[]),
    导致用户'没有最终交付、没有返回交付内容及文件'。

    防回退: 确保 Step 已导入 + SUMMARIZING 块有 try/except 终极兜底。
    采用源码级检查(避免触发 planner_react 的重依赖链)。
    """

    def _read_planner_react_source(self) -> str:
        """读取 planner_react.py 源码(避免重依赖导入)"""
        import os
        filepath = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..",
            "app", "domain", "services", "flows", "planner_react.py"
        )
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()

    def test_planner_react_imports_step(self):
        """planner_react.py 必须导入 Step 类(防 NameError 回退)"""
        source = self._read_planner_react_source()
        # 必须存在从 plan 模块导入 Step 的语句
        assert re.search(r"from\s+app\.domain\.models\.plan\s+import\s+[^\n]*\bStep\b", source), \
            "planner_react.py 未导入 Step 类,P0-2 附件门禁会抛 NameError 导致无交付"

    def test_plan_module_exports_step(self):
        """plan 模块必须导出 Step 类"""
        from app.domain.models.plan import Step
        assert Step is not None

    def test_planner_react_has_summarizing_fallback(self):
        """planner_react.py SUMMARIZING 块必须有终极兜底 try/except"""
        source = self._read_planner_react_source()
        # 必须存在 except 块构造兜底 MessageEvent(is_final=True)
        assert "终极兜底" in source, \
            "planner_react.py 缺少 SUMMARIZING 终极兜底(try/except 保障异常时仍交付文件)"
        assert "is_final=True" in source


class TestBatch46SandboxScanAlways:
    """批次46: 沙箱始终扫描并合并测试(根治 shell_execute 生成文件未被发现问题)

    根因: LLM 通过 shell_execute 运行脚本生成 PPT/MD 等交付物,
    这些文件不经过 write_file,因此不被 session.files 追踪。
    原逻辑仅在 session.files 为空时扫描沙箱,当 session.files 有过程文件
    (如 .py 脚本)时不会触发扫描,导致真实交付物未被发现。

    防回退: 确保 _SANDBOX_SCAN_GLOB_PATTERNS 包含 .md +
    SUMMARIZING 阶段始终扫描沙箱并合并 + summarize 后附件兜底。
    """

    def _read_planner_react_source(self) -> str:
        """读取 planner_react.py 源码(避免重依赖导入)"""
        import os
        filepath = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..",
            "app", "domain", "services", "flows", "planner_react.py"
        )
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()

    def test_glob_patterns_includes_md(self):
        """_SANDBOX_SCAN_GLOB_PATTERNS 必须包含 *.md(Markdown交付物)"""
        source = self._read_planner_react_source()
        assert '"*.md"' in source, \
            "_SANDBOX_SCAN_GLOB_PATTERNS 缺少 *.md,Markdown交付物不会被沙箱扫描发现"

    def test_sandbox_scan_not_gated_by_empty_session_files(self):
        """沙箱扫描不能仅在 session.files 为空时触发(批次46核心修复)"""
        source = self._read_planner_react_source()
        # 原逻辑: if not all_files: scanned = await self._scan_sandbox_deliverables()
        # 新逻辑: 始终扫描,不再有 if not all_files 门禁
        assert "if not all_files:" not in source.split("_scan_sandbox_deliverables")[0].split("# 批次46")[-1], \
            "沙箱扫描仍被 if not all_files 门禁控制,shell_execute生成的文件无法被发现"

    def test_sandbox_scan_always_merges(self):
        """沙箱扫描结果必须与 session.files 合并(去重保序)"""
        source = self._read_planner_react_source()
        assert "批次46" in source, "缺少批次46注释标记"
        assert "dict.fromkeys(scanned + all_files)" in source, \
            "缺少沙箱扫描结果与session.files的合并逻辑(dict.fromkeys去重保序)"

    def test_post_summarize_attachment_safety_net(self):
        """summarize后必须有附件兜底检查(无带附件事件时补发交付)"""
        source = self._read_planner_react_source()
        assert "delivered_with_attachments" in source, \
            "缺少 delivered_with_attachments 跟踪变量,无法检测summarize是否产出带附件事件"
        assert "not delivered_with_attachments" in source, \
            "缺少 summarize 后附件兜底检查(not delivered_with_attachments)"

    def test_sandbox_scan_finds_new_files(self):
        """沙箱扫描必须识别session.files中未追踪的新文件并回传同步"""
        source = self._read_planner_react_source()
        assert "new_files" in source, \
            "缺少 new_files 识别逻辑(扫描发现的新文件需要回传Runner同步到OSS+DB)"
        assert "SandboxScanEvent" in source, \
            "缺少 SandboxScanEvent 回传(新文件需通过事件回传给Runner同步)"


class TestOutputTruncationStrategyOptimization:
    """输出截断应对策略优化测试(根治 LLM 识别到截断后陷入低效读取循环)

    根因: 智能体在分析阶段读取中间结果文件时,read_file/shell_execute 输出被截断,
    智能体反复尝试 read_file 分批、search_in_file、拆分文件、base64 编码等方法
    绕过截断,浪费 30+ 次工具调用(会话实测根因)。
    修复: 新增 OUTPUT_TRUNCATION_STRATEGY_CN/EN 片段,引导识别截断信号、
    停止反复读取、用脚本直接生成交付物。
    """

    TRUNCATION_STRATEGY_KEYWORDS_CN = [
        "输出截断应对策略",
        "识别截断信号",
        "content truncated",
        "shell output truncated",
        "context compression not tool error",
        "严禁反复读取",
        "中间结果仅供脚本消费",
        "正确应对流程",
    ]

    def test_fragments_has_output_truncation_cn(self):
        """_fragments.py 必须定义 OUTPUT_TRUNCATION_STRATEGY_CN"""
        from app.domain.services.prompts._fragments import OUTPUT_TRUNCATION_STRATEGY_CN
        assert OUTPUT_TRUNCATION_STRATEGY_CN, "OUTPUT_TRUNCATION_STRATEGY_CN 未定义或为空"
        assert len(OUTPUT_TRUNCATION_STRATEGY_CN) > 100

    def test_fragments_has_output_truncation_en(self):
        """_fragments.py 必须定义 OUTPUT_TRUNCATION_STRATEGY_EN(CN/EN 同步)"""
        from app.domain.services.prompts._fragments import OUTPUT_TRUNCATION_STRATEGY_EN
        assert OUTPUT_TRUNCATION_STRATEGY_EN, "OUTPUT_TRUNCATION_STRATEGY_EN 未定义或为空"

    @pytest.mark.parametrize("keyword", TRUNCATION_STRATEGY_KEYWORDS_CN)
    def test_truncation_strategy_cn_has_keywords(self, keyword):
        """OUTPUT_TRUNCATION_STRATEGY_CN 必须包含所有关键约束"""
        from app.domain.services.prompts._fragments import OUTPUT_TRUNCATION_STRATEGY_CN
        assert keyword in OUTPUT_TRUNCATION_STRATEGY_CN, \
            f"OUTPUT_TRUNCATION_STRATEGY_CN 缺失: {keyword}"

    @pytest.mark.parametrize("keyword", TRUNCATION_STRATEGY_KEYWORDS_CN)
    def test_cn_react_system_has_truncation_strategy(self, keyword):
        """CN REACT_SYSTEM_PROMPT 必须包含输出截断应对策略"""
        assert keyword in cn_react.REACT_SYSTEM_PROMPT, \
            f"CN REACT_SYSTEM_PROMPT 缺失截断策略: {keyword}"

    @pytest.mark.parametrize("keyword", TRUNCATION_STRATEGY_KEYWORDS_CN)
    def test_cn_execution_has_truncation_strategy(self, keyword):
        """CN EXECUTION_PROMPT 必须包含输出截断应对策略"""
        assert keyword in cn_react.EXECUTION_PROMPT, \
            f"CN EXECUTION_PROMPT 缺失截断策略: {keyword}"

    def test_truncation_strategy_no_dev_noise(self):
        """输出截断策略片段不得包含开发噪声"""
        from app.domain.services.prompts._fragments import OUTPUT_TRUNCATION_STRATEGY_CN
        for noise in DEV_NOISE_TOKENS:
            assert noise not in OUTPUT_TRUNCATION_STRATEGY_CN, \
                f"OUTPUT_TRUNCATION_STRATEGY_CN 含开发噪声: {noise}"

    def test_truncation_strategy_uses_generic_examples(self):
        """输出截断策略不得含特化业务名(通用性)"""
        from app.domain.services.prompts._fragments import OUTPUT_TRUNCATION_STRATEGY_CN
        for token in SPECIALIZED_TOKENS:
            assert token not in OUTPUT_TRUNCATION_STRATEGY_CN, \
                f"OUTPUT_TRUNCATION_STRATEGY_CN 含特化词: {token}"

    def test_execution_prompt_placeholders_unchanged_with_truncation(self):
        """新增截断策略后,EXECUTION_PROMPT 占位符集合不变"""
        cn_placeholders = _extract_placeholders(cn_react.EXECUTION_PROMPT)
        assert cn_placeholders == PLACEHOLDERS["EXECUTION_PROMPT"], \
            f"EXECUTION_PROMPT 占位符变化: 期望 {PLACEHOLDERS['EXECUTION_PROMPT']}, 实际 {cn_placeholders}"


class TestContextCompressionRecoveryOptimization:
    """上下文压缩恢复引导优化测试(根治 emergency_compact 后丢步重执)

    根因: 上下文压缩后,智能体丢失"哪些步骤已完成"的记录,重新执行已完成的
    describe/getDownloadTaskList/验证步骤,导致冗余工具调用(会话6d4f313b根因)。
    修复: 1.代码层 extract_key_facts 新增 step_completed 分类(已实现)
    2.提示词层新增 CONTEXT_COMPRESSION_RECOVERY_CN/EN 片段,引导压缩后
    优先检查已有交付物和步骤完成状态。
    """

    COMPRESSION_RECOVERY_KEYWORDS_CN = [
        "上下文压缩恢复引导",
        "优先检查已有交付物",
        "核对步骤完成状态",
        "step_completed",
        "严禁重新执行这些步骤",
        "区分",
        "已完成",
        "需重试",
        "基于已有成果继续推进",
    ]

    def test_fragments_has_compression_recovery_cn(self):
        """_fragments.py 必须定义 CONTEXT_COMPRESSION_RECOVERY_CN"""
        from app.domain.services.prompts._fragments import CONTEXT_COMPRESSION_RECOVERY_CN
        assert CONTEXT_COMPRESSION_RECOVERY_CN, "CONTEXT_COMPRESSION_RECOVERY_CN 未定义或为空"
        assert len(CONTEXT_COMPRESSION_RECOVERY_CN) > 100

    def test_fragments_has_compression_recovery_en(self):
        """_fragments.py 必须定义 CONTEXT_COMPRESSION_RECOVERY_EN(CN/EN 同步)"""
        from app.domain.services.prompts._fragments import CONTEXT_COMPRESSION_RECOVERY_EN
        assert CONTEXT_COMPRESSION_RECOVERY_EN, "CONTEXT_COMPRESSION_RECOVERY_EN 未定义或为空"

    @pytest.mark.parametrize("keyword", COMPRESSION_RECOVERY_KEYWORDS_CN)
    def test_compression_recovery_cn_has_keywords(self, keyword):
        """CONTEXT_COMPRESSION_RECOVERY_CN 必须包含所有关键引导"""
        from app.domain.services.prompts._fragments import CONTEXT_COMPRESSION_RECOVERY_CN
        assert keyword in CONTEXT_COMPRESSION_RECOVERY_CN, \
            f"CONTEXT_COMPRESSION_RECOVERY_CN 缺失: {keyword}"

    @pytest.mark.parametrize("keyword", COMPRESSION_RECOVERY_KEYWORDS_CN)
    def test_cn_react_system_has_compression_recovery(self, keyword):
        """CN REACT_SYSTEM_PROMPT 必须包含上下文压缩恢复引导"""
        assert keyword in cn_react.REACT_SYSTEM_PROMPT, \
            f"CN REACT_SYSTEM_PROMPT 缺失压缩恢复引导: {keyword}"

    @pytest.mark.parametrize("keyword", COMPRESSION_RECOVERY_KEYWORDS_CN)
    def test_cn_execution_has_compression_recovery(self, keyword):
        """CN EXECUTION_PROMPT 必须包含上下文压缩恢复引导"""
        assert keyword in cn_react.EXECUTION_PROMPT, \
            f"CN EXECUTION_PROMPT 缺失压缩恢复引导: {keyword}"

    def test_compression_recovery_no_dev_noise(self):
        """上下文压缩恢复片段不得包含开发噪声"""
        from app.domain.services.prompts._fragments import CONTEXT_COMPRESSION_RECOVERY_CN
        for noise in DEV_NOISE_TOKENS:
            assert noise not in CONTEXT_COMPRESSION_RECOVERY_CN, \
                f"CONTEXT_COMPRESSION_RECOVERY_CN 含开发噪声: {noise}"

    def test_compression_recovery_uses_generic_examples(self):
        """上下文压缩恢复片段不得含特化业务名(通用性)"""
        from app.domain.services.prompts._fragments import CONTEXT_COMPRESSION_RECOVERY_CN
        for token in SPECIALIZED_TOKENS:
            assert token not in CONTEXT_COMPRESSION_RECOVERY_CN, \
                f"CONTEXT_COMPRESSION_RECOVERY_CN 含特化词: {token}"

    def test_execution_prompt_placeholders_unchanged_with_recovery(self):
        """新增压缩恢复引导后,EXECUTION_PROMPT 占位符集合不变"""
        cn_placeholders = _extract_placeholders(cn_react.EXECUTION_PROMPT)
        assert cn_placeholders == PLACEHOLDERS["EXECUTION_PROMPT"], \
            f"EXECUTION_PROMPT 占位符变化: 期望 {PLACEHOLDERS['EXECUTION_PROMPT']}, 实际 {cn_placeholders}"
