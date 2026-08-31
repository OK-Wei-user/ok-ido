#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_batch38_optimization.py
Batch 38 优化单元测试 - 验证硬超时强化、步骤去重、交付物路径提取

测试覆盖:
- B38-4: _inject_budget_warnings 硬超时后每次迭代注入提醒(不再仅注入一次)
- B38-5: PlannerAgent._deduplicate_steps 步骤去重(与已完成步骤重复 + 内部重复)
- B38-6: AgentTaskRunner._extract_deliverable_paths 交付物路径自动提取
"""
import asyncio
import re
from unittest.mock import MagicMock, patch

import pytest

from app.domain.models.app_config import AgentConfig
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.services.agents.planner import PlannerAgent
from app.domain.services.agent_task_runner import AgentTaskRunner


# ========== B38-4: 硬超时强化测试 ==========

class TestBatch38HardTimeoutReinforcement:
    """B38-4: 硬超时后每次迭代注入简短提醒(不再仅注入一次)"""

    def _build_agent(self, session_timeout: int = 1800, session_warning: int = 1500) -> MagicMock:
        """构建BaseAgent实例用于测试_inject_budget_warnings"""
        from app.domain.services.agents.base import BaseAgent
        agent = object.__new__(BaseAgent)
        agent.name = "test_agent"
        agent._session_id = "test_session"
        agent._agent_config = AgentConfig(
            max_iterations=10,
            session_timeout_seconds=session_timeout,
            session_warning_seconds=session_warning,
        )
        return agent

    def test_first_timeout_injects_full_directive(self):
        """首次触发硬超时应注入完整指令并返回True"""
        agent = self._build_agent(session_timeout=1800)
        tool_messages = []
        # 模拟已运行2000秒(超过1800秒硬超时)
        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 3000.0
            result = agent._inject_budget_warnings(
                iteration=1,
                tool_messages=tool_messages,
                session_start_ts=1000.0,
                session_timeout_injected=False,
            )
        assert result is True
        assert len(tool_messages) == 1
        assert "系统超时指令" in tool_messages[0]["content"]

    def test_subsequent_timeout_injects_reminder(self):
        """硬超时后后续迭代应注入简短提醒(批次38核心修复)"""
        agent = self._build_agent(session_timeout=1800)
        tool_messages = []
        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 3100.0
            result = agent._inject_budget_warnings(
                iteration=2,
                tool_messages=tool_messages,
                session_start_ts=1000.0,
                session_timeout_injected=True,  # 已注入过
            )
        # 应返回True(保持已注入状态)
        assert result is True
        # 应注入简短提醒
        assert len(tool_messages) == 1
        assert "超时提醒" in tool_messages[0]["content"]
        # 不应再注入完整指令
        assert "系统超时指令" not in tool_messages[0]["content"]

    def test_no_timeout_no_injection(self):
        """未超时时不应注入任何超时提醒"""
        agent = self._build_agent(session_timeout=1800)
        tool_messages = []
        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 1200.0
            result = agent._inject_budget_warnings(
                iteration=1,
                tool_messages=tool_messages,
                session_start_ts=1000.0,
                session_timeout_injected=False,
            )
        assert result is False
        # 不应有超时消息(可能有迭代预算消息,但不含超时)
        timeout_msgs = [m for m in tool_messages if "超时" in m["content"]]
        assert len(timeout_msgs) == 0

    def test_warning_before_timeout(self):
        """软警告阶段(未达硬超时)应注入收敛提示"""
        agent = self._build_agent(session_timeout=1800, session_warning=1500)
        tool_messages = []
        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 2600.0  # 运行1600秒(>1500,<1800)
            result = agent._inject_budget_warnings(
                iteration=1,
                tool_messages=tool_messages,
                session_start_ts=1000.0,
                session_timeout_injected=False,
            )
        # 批次45 P1-3: 软警告也设session_timeout_injected=True防重复注入(原仅硬超时设置)
        assert result is True
        # 应有软警告消息
        warning_msgs = [m for m in tool_messages if "系统时间警告" in m["content"]]
        assert len(warning_msgs) == 1

    def test_timeout_disabled(self):
        """session_timeout=0时不启用超时检测"""
        agent = self._build_agent(session_timeout=0, session_warning=0)
        tool_messages = []
        result = agent._inject_budget_warnings(
            iteration=1,
            tool_messages=tool_messages,
            session_start_ts=1000.0,
            session_timeout_injected=False,
        )
        assert result is False
        # 不应有超时消息
        timeout_msgs = [m for m in tool_messages if "超时" in m["content"]]
        assert len(timeout_msgs) == 0


# ========== B38-5: 步骤去重测试 ==========

class TestBatch38StepDeduplication:
    """B38-5: PlannerAgent._deduplicate_steps 步骤去重"""

    def _make_step(self, desc: str, done: bool = False) -> Step:
        """构建Step实例"""
        return Step(
            id="1",
            description=desc,
            status=ExecutionStatus.COMPLETED if done else ExecutionStatus.PENDING,
        )

    def test_no_duplicates_returns_unchanged(self):
        """无重复时应原样返回"""
        new_steps = [
            self._make_step("步骤A: 读取数据"),
            self._make_step("步骤B: 分析数据"),
        ]
        completed = [self._make_step("前置步骤: 准备环境", done=True)]

        result = PlannerAgent._deduplicate_steps(new_steps, completed)
        assert len(result) == 2

    def test_removes_duplicate_with_completed(self):
        """应移除与已完成步骤描述重复的新步骤"""
        new_steps = [
            self._make_step("分步生成三个交付物文件：第一步，执行数据检查"),  # 重复
            self._make_step("步骤B: 分析数据"),
        ]
        completed = [
            self._make_step("分步生成三个交付物文件：第一步，执行数据检查", done=True),
        ]

        result = PlannerAgent._deduplicate_steps(new_steps, completed)
        assert len(result) == 1
        assert "步骤B" in result[0].description

    def test_removes_internal_duplicates(self):
        """应移除新步骤列表内部的重复"""
        new_steps = [
            self._make_step("步骤A: 读取数据"),
            self._make_step("步骤A: 读取数据"),  # 内部重复
            self._make_step("步骤B: 分析数据"),
        ]
        completed = []

        result = PlannerAgent._deduplicate_steps(new_steps, completed)
        assert len(result) == 2

    def test_empty_new_steps(self):
        """空新步骤列表应返回空"""
        result = PlannerAgent._deduplicate_steps([], [self._make_step("已完成", done=True)])
        assert len(result) == 0

    def test_empty_completed_steps(self):
        """空已完成列表时仅做内部去重"""
        new_steps = [
            self._make_step("步骤A"),
            self._make_step("步骤A"),  # 内部重复
        ]
        result = PlannerAgent._deduplicate_steps(new_steps, [])
        assert len(result) == 1

    def test_empty_description_kept(self):
        """空描述步骤应保留(不去重)"""
        new_steps = [
            self._make_step(""),
            self._make_step(""),
        ]
        result = PlannerAgent._deduplicate_steps(new_steps, [])
        assert len(result) == 2

    def test_long_description_prefix_matching(self):
        """长描述应通过前50字符匹配去重(避免长文本噪声)"""
        long_desc = "分步生成三个交付物文件：第一步，执行数据检查命令验证7个原始Excel文件可正常读取" + "x" * 100
        new_steps = [
            self._make_step(long_desc),
            self._make_step("步骤B"),
        ]
        completed = [self._make_step(long_desc, done=True)]

        result = PlannerAgent._deduplicate_steps(new_steps, completed)
        assert len(result) == 1
        assert "步骤B" in result[0].description

    def test_jd_md_scenario(self):
        """模拟 jd.md 中的实际场景: 步骤6和步骤11重复"""
        step6_desc = "分步生成三个交付物文件：第一步，执行数据检查命令验证7个原始Excel文件可正常读取；第二步，编写并执行generate_charts.py脚本，使..."
        new_steps = [
            self._make_step(step6_desc),  # 与已完成步骤6重复
            self._make_step("验证交付物完整性：检查Word报告是否包含所有章节"),
        ]
        completed = [
            self._make_step("使用mcp_system_getWarehousingDetailExport工具导出", done=True),
            self._make_step("用pandas读取所有下载的数据文件", done=True),
            self._make_step(step6_desc, done=True),  # 已完成的步骤6
        ]

        result = PlannerAgent._deduplicate_steps(new_steps, completed)
        assert len(result) == 1
        assert "验证交付物完整性" in result[0].description


# ========== B38-6: 交付物路径提取测试 ==========

class TestBatch38DeliverablePathExtraction:
    """B38-6: AgentTaskRunner._extract_deliverable_paths 交付物路径自动提取"""

    def _make_step(self, attachments=None, result: str = "") -> Step:
        """构建Step实例"""
        return Step(
            id="1",
            description="test",
            attachments=attachments or [],
            result=result,
            status=ExecutionStatus.COMPLETED,
        )

    def test_existing_attachments_returned_directly(self):
        """step.attachments 非空时应直接返回(LLM已声明)"""
        step = self._make_step(
            attachments=["/home/ubuntu/report.docx"],
            result="some result",
        )
        result = AgentTaskRunner._extract_deliverable_paths(step)
        assert result == ["/home/ubuntu/report.docx"]

    def test_extract_from_result_xlsx(self):
        """应从 result 中提取 .xlsx 文件路径"""
        step = self._make_step(
            result="已生成文件 /home/ubuntu/出入库分析报告.xlsx 和 /home/ubuntu/数据明细.xlsx",
        )
        result = AgentTaskRunner._extract_deliverable_paths(step)
        assert "/home/ubuntu/出入库分析报告.xlsx" in result
        assert "/home/ubuntu/数据明细.xlsx" in result

    def test_extract_from_result_docx(self):
        """应从 result 中提取 .docx 文件路径"""
        step = self._make_step(
            result="Word报告已生成: /home/ubuntu/经营分析报告.docx",
        )
        result = AgentTaskRunner._extract_deliverable_paths(step)
        assert "/home/ubuntu/经营分析报告.docx" in result

    def test_extract_from_result_png(self):
        """应从 result 中提取 .png 图表文件路径"""
        step = self._make_step(
            result="图表已生成: /home/ubuntu/charts/trend.png, /home/ubuntu/charts/pie.png",
        )
        result = AgentTaskRunner._extract_deliverable_paths(step)
        assert "/home/ubuntu/charts/trend.png" in result
        assert "/home/ubuntu/charts/pie.png" in result

    def test_filter_intermediate_products(self):
        """应过滤中间产物路径(/tmp/ /workspace/)"""
        step = self._make_step(
            result="脚本: /tmp/script.py, 输出: /home/ubuntu/workspace/temp.csv, 交付物: /home/ubuntu/report.xlsx",
        )
        result = AgentTaskRunner._extract_deliverable_paths(step)
        assert "/home/ubuntu/report.xlsx" in result
        # /tmp/ 和 /workspace/ 路径应被过滤
        assert all(not p.startswith("/tmp/") for p in result)
        assert all(not p.startswith("/home/ubuntu/workspace/") for p in result)

    def test_filter_non_deliverable_extensions(self):
        """应过滤非交付物扩展名(.py .log .sh 等)"""
        step = self._make_step(
            result="脚本: /home/ubuntu/script.py, 日志: /home/ubuntu/run.log, 报告: /home/ubuntu/report.docx",
        )
        result = AgentTaskRunner._extract_deliverable_paths(step)
        assert "/home/ubuntu/report.docx" in result
        assert all(not p.endswith(".py") for p in result)
        assert all(not p.endswith(".log") for p in result)

    def test_txt_md_json_only_in_home_root(self):
        """ .txt/.md/.json 仅同步 /home/ubuntu/ 根目录下的"""
        step = self._make_step(
            result="配置: /tmp/config.json, 笔记: /home/ubuntu/notes.md, 脚本: /home/ubuntu/workspace/data.txt",
        )
        result = AgentTaskRunner._extract_deliverable_paths(step)
        assert "/home/ubuntu/notes.md" in result
        # /tmp/ 和 /workspace/ 下的 .txt/.md/.json 应被过滤
        assert "/tmp/config.json" not in result
        assert "/home/ubuntu/workspace/data.txt" not in result

    def test_empty_result_returns_empty(self):
        """step.result 为空时应返回空列表"""
        step = self._make_step(result="")
        result = AgentTaskRunner._extract_deliverable_paths(step)
        assert result == []

    def test_no_file_paths_in_result(self):
        """result 中无文件路径时应返回空列表"""
        step = self._make_step(result="任务已完成，但未生成文件")
        result = AgentTaskRunner._extract_deliverable_paths(step)
        assert result == []

    def test_deduplication(self):
        """重复路径应去重"""
        step = self._make_step(
            result="文件: /home/ubuntu/report.xlsx, 再次提及: /home/ubuntu/report.xlsx",
        )
        result = AgentTaskRunner._extract_deliverable_paths(step)
        assert result.count("/home/ubuntu/report.xlsx") == 1

    def test_real_e2e_scenario(self):
        """模拟 E2E 实际场景: 16个文件约40.4MB"""
        result_text = """
        任务完成。已生成以下交付物:
        原始Excel文件:
        - /home/ubuntu/入库明细.xlsx
        - /home/ubuntu/出库明细.xlsx
        - /home/ubuntu/库存总览.xlsx
        分析报告:
        - /home/ubuntu/经营分析报告.docx
        可视化图表:
        - /home/ubuntu/charts/月度趋势.png
        - /home/ubuntu/charts/品类分布.png
        中间产物(不应同步):
        - /tmp/process.py
        - /home/ubuntu/workspace/temp_data.csv
        """
        step = self._make_step(result=result_text)
        result = AgentTaskRunner._extract_deliverable_paths(step)

        # 应提取6个交付物文件(过滤中间产物 /tmp/process.py 和 /workspace/temp_data.csv)
        assert len(result) == 6
        assert "/home/ubuntu/入库明细.xlsx" in result
        assert "/home/ubuntu/出库明细.xlsx" in result
        assert "/home/ubuntu/库存总览.xlsx" in result
        assert "/home/ubuntu/经营分析报告.docx" in result
        assert "/home/ubuntu/charts/月度趋势.png" in result
        assert "/home/ubuntu/charts/品类分布.png" in result
        # 中间产物不应出现
        assert "/tmp/process.py" not in result
        assert "/home/ubuntu/workspace/temp_data.csv" not in result


# ========== Prompt 片段完整性测试 ==========

class TestBatch38PromptFragments:
    """验证 TOOL_SELECTION_GUIDE_CN/EN 片段已正确添加"""

    def test_cn_fragment_exists(self):
        """中文片段应存在且非空"""
        from app.domain.services.prompts._fragments import TOOL_SELECTION_GUIDE_CN
        assert TOOL_SELECTION_GUIDE_CN
        assert "内置工具优先" in TOOL_SELECTION_GUIDE_CN
        assert "文件下载约束" in TOOL_SELECTION_GUIDE_CN
        assert "工具失败恢复策略" in TOOL_SELECTION_GUIDE_CN
        assert "curl -L -o" in TOOL_SELECTION_GUIDE_CN

    def test_en_fragment_exists(self):
        """英文片段应存在且非空"""
        from app.domain.services.prompts._fragments import TOOL_SELECTION_GUIDE_EN
        assert TOOL_SELECTION_GUIDE_EN
        assert "Built-in tool priority" in TOOL_SELECTION_GUIDE_EN
        assert "File download constraint" in TOOL_SELECTION_GUIDE_EN
        assert "Tool failure recovery" in TOOL_SELECTION_GUIDE_EN
        assert "curl -L -o" in TOOL_SELECTION_GUIDE_EN

    def test_cn_react_prompt_references_fragment(self):
        """中文 REACT_SYSTEM_PROMPT 应引用 TOOL_SELECTION_GUIDE_CN"""
        from app.domain.services.prompts.react import REACT_SYSTEM_PROMPT
        assert "内置工具优先" in REACT_SYSTEM_PROMPT
        assert "文件下载约束" in REACT_SYSTEM_PROMPT

    def test_en_react_prompt_references_fragment(self):
        """英文 REACT_SYSTEM_PROMPT 应引用 TOOL_SELECTION_GUIDE_EN"""
        from app.domain.services.prompts.en.react import REACT_SYSTEM_PROMPT
        assert "Built-in tool priority" in REACT_SYSTEM_PROMPT
        assert "File download constraint" in REACT_SYSTEM_PROMPT

    def test_cn_execution_prompt_references_fragment(self):
        """中文 EXECUTION_PROMPT 应引用 TOOL_SELECTION_GUIDE_CN"""
        from app.domain.services.prompts.react import EXECUTION_PROMPT
        assert "内置工具优先" in EXECUTION_PROMPT
        assert "curl -L -o" in EXECUTION_PROMPT

    def test_en_execution_prompt_references_fragment(self):
        """英文 EXECUTION_PROMPT 应引用 TOOL_SELECTION_GUIDE_EN"""
        from app.domain.services.prompts.en.react import EXECUTION_PROMPT
        assert "Built-in tool priority" in EXECUTION_PROMPT
        assert "curl -L -o" in EXECUTION_PROMPT
