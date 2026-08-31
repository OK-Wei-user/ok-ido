#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""前序步骤上下文构建器(Batch 34 / F11-5, DRY重构; Batch 48 增强)

消除 planner.py 与 react.py 中 _build_prior_steps_context 的 ~40 行重复代码。
通过 Union[Step, str] + context_type 参数统一,既消除重复又保留语义差异。

Batch 48 增强: 提取已完成步骤的 attachments(产出文件清单),单独列出
"已生成文件清单"段,强制 planner 在更新计划时引用已有文件路径而非重新生成。
根因: 会话7346aad8 中 prior_steps_context 仅含 result 摘要(300字),
planner 无法精准知道已导出/已生成哪些文件,导致重新规划时重复执行导出步骤。
"""
import re
from typing import List, Union

from app.domain.models.plan import Plan, Step, ExecutionStatus

# 上下文类型 → (标题, 尾部警告)
_CONTEXT_TEMPLATES = {
    "execution": (
        "【前序步骤完成情况（严禁重复执行已完成操作）】",
        "注意：上述步骤已完成操作和产出的文件可直接复用,不得重复执行。",
    ),
    "planning": (
        "【前序步骤完成情况（严禁重建或重复规划已完成步骤）】",
        "注意：上述步骤已完成,已生成的文件、已导出的数据、已搜索的结果必须直接复用,"
        "不得在更新后的计划中重建或重复这些步骤。仅当执行结果显示失败且需重试时,"
        "才允许重新规划对应步骤。后续步骤描述中必须直接引用\"已生成文件清单\"中的"
        "文件路径,严禁重新导出或重新生成已有文件。",
    ),
}

# 文件路径提取正则: 匹配 /home/ubuntu/xxx.ext 或 /tmp/xxx.ext 或 /home/ubuntu/workspace/xxx.ext
# Batch 48: 从 step.result 中提取沙箱文件路径,补充 attachments 未覆盖的情况
_FILE_PATH_PATTERN = re.compile(
    r"(?:/home/ubuntu(?:/workspace|/uploads)?/|/tmp/)[^\s\'\"<>\)\];,]+"
    r"\.(?:xlsx|xls|docx|pdf|pptx|png|jpg|jpeg|csv|md|py|json|txt)",
    re.IGNORECASE,
)


def _extract_file_paths(step: Step) -> List[str]:
    """从步骤的 attachments 和 result 中提取产出文件路径(Batch 48)

    优先取 attachments(执行者声明的交付物),其次从 result 文本中正则提取
    路径(覆盖执行者未声明 attachments 但在 result 中提及文件路径的情况)。

    Args:
        step: 已完成的步骤

    Returns:
        去重后的文件路径列表,保留出现顺序
    """
    paths: List[str] = []
    seen = set()

    # 1. 优先取 attachments(执行者明确声明的产出文件)
    for att in step.attachments or []:
        if isinstance(att, str) and att not in seen:
            paths.append(att)
            seen.add(att)

    # 2. 从 result 文本中正则提取文件路径(补充 attachments 未覆盖的情况)
    if step.result:
        for match in _FILE_PATH_PATTERN.finditer(step.result):
            p = match.group(0).rstrip(".,;)")
            if p not in seen:
                paths.append(p)
                seen.add(p)

    return paths


def build_prior_steps_context(
    plan: Plan,
    current_step: Union[Step, str],
    context_type: str = "execution",
) -> str:
    """构建前序步骤完成情况摘要(planner.py/react.py共享,消除重复)

    Args:
        plan: 当前计划
        current_step: 当前步骤(Step对象或step_id字符串)
        context_type: "execution"(执行阶段) 或 "planning"(规划阶段)

    Returns:
        前序步骤完成情况摘要文本,无已完成步骤时返回空字符串。
        Batch 48: planning 上下文额外包含"已生成文件清单"段,
        强制 planner 引用已有文件路径而非重新生成。
    """
    if not plan or not plan.steps:
        return ""

    current_id = current_step.id if isinstance(current_step, Step) else current_step
    completed_steps = [
        s for s in plan.steps
        if s.id != current_id
        and s.status == ExecutionStatus.COMPLETED
        and s.result
    ]
    if not completed_steps:
        return ""

    header, tail = _CONTEXT_TEMPLATES.get(context_type, _CONTEXT_TEMPLATES["execution"])
    lines = [header]
    for s in completed_steps:
        result_brief = s.result.strip()[:300]
        lines.append(f"- 步骤{s.id}(已完成)：{result_brief}")
    lines.append("")

    # Batch 48: planning 上下文额外提取"已生成文件清单"
    # 根因: 会话7346aad8 中 planner 仅看到 result 摘要,无法精准知道已生成文件,
    # 导致重新规划时重复执行导出步骤。新增文件清单段让 planner 直接引用文件路径。
    if context_type == "planning":
        all_files: List[str] = []
        file_seen = set()
        for s in completed_steps:
            for p in _extract_file_paths(s):
                if p not in file_seen:
                    all_files.append(p)
                    file_seen.add(p)
        if all_files:
            lines.append("【已生成文件清单（后续步骤必须直接引用,严禁重新生成）】")
            for p in all_files:
                lines.append(f"- {p}")
            lines.append("")

    lines.append(tail)
    return "\n".join(lines)
