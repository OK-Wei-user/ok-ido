#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""条件化计划更新策略(Batch 30 / F11-1)

纯函数模块,封装"是否可跳过 update_plan"的判断逻辑,便于单元测试。

设计原则:
- 不侵入 BaseAgent.invoke 核心循环,仅在 PlannerReActFlow 状态转移处增加条件判断
- 保守策略: 宁可多更新一次计划,也不漏更新导致计划漂移
- 安全网: 连续跳过 MAX_CONSECUTIVE_SKIPS 次后强制更新,防止长期漂移
"""
import re
from typing import List

from app.domain.models.plan import Plan, Step, ExecutionStatus

# 连续跳过上限: 第3步强制更新(防漂移安全网)
MAX_CONSECUTIVE_SKIPS = 2

# 指代词: 后续步骤描述中出现时,表示引用了前序步骤的产出(保守判断)
_REFERENTIAL_KEYWORDS: tuple = (
    "上述", "该文件", "此数据", "该结果", "上述文件", "上一步",
    "该步骤", "前述", "上一步骤", "该产出", "上述数据", "该导出",
    "前一步", "前序", "已生成", "已导出", "已下载",
)

# 浏览器操作关键词: 涉及浏览器的步骤不跳过update_plan
# 根因(会话410949eb): 浏览器操作是状态依赖+顺序敏感的,单步执行可能完成多个计划步骤
# 的操作(navigate+scroll+click),跳过update_plan导致后续步骤描述与实际页面状态不符,
# LLM看到"滚动到X"但实际已滚动过,重复执行同一操作。
# 修复: 浏览器步骤强制更新计划,让planner根据当前页面状态调整后续步骤描述。
_BROWSER_KEYWORDS: tuple = (
    "browser", "浏览器", "navigate", "click", "scroll", "input",
    "点击", "滚动", "输入", "导航", "打开页面", "页面操作",
    "选择选项", "按下", "鼠标", "表单", "网页",
)

# 文件路径提取正则: 匹配 /home/ubuntu/xxx.csv 或 /tmp/xxx.txt 等绝对路径
_FILE_PATH_PATTERN = re.compile(r'/[\w./\-]+\.\w{1,10}')


def should_skip_update_plan(step: Step, plan: Plan, consecutive_skipped: int) -> bool:
    """判断步骤完成后是否可跳过 update_plan

    跳过条件(全部满足):
    1. step.status == COMPLETED 且 step.success == True
    2. 后续未完成步骤的描述中未引用当前步骤的产出
    3. 连续跳过次数未达安全网上限

    Args:
        step: 刚执行完成的步骤
        plan: 当前计划(含所有步骤)
        consecutive_skipped: 已连续跳过 update_plan 的次数

    Returns:
        True 表示可跳过 update_plan, False 表示必须更新
    """
    # 必须更新条件(任一满足即不跳过)
    if step.status == ExecutionStatus.FAILED:
        return False  # 失败需恢复决策
    if not step.success:
        return False  # 未成功需更新
    if consecutive_skipped >= MAX_CONSECUTIVE_SKIPS:
        return False  # 安全网: 连续跳过已达上限,强制更新
    if step_output_referenced_later(step, plan):
        return False  # 后续步骤引用了当前产出,需更新计划
    # 浏览器操作步骤: 强制更新计划(状态依赖,跳过会导致重复操作)
    if _is_browser_step(step):
        return False
    return True


def _is_browser_step(step: Step) -> bool:
    """检测步骤是否涉及浏览器操作

    浏览器操作是状态依赖+顺序敏感的: 单步执行可能完成多个计划步骤的操作
    (如navigate+scroll+click),跳过update_plan会导致后续步骤描述与实际
    页面状态不符,LLM重复执行已完成的操作。

    判断依据: 步骤描述中包含浏览器操作关键词(navigate/click/scroll/点击/滚动等)
    """
    if not step.description:
        return False
    desc_lower = step.description.lower()
    return any(kw.lower() in desc_lower for kw in _BROWSER_KEYWORDS)


def step_output_referenced_later(step: Step, plan: Plan) -> bool:
    """检测后续未完成步骤的描述是否引用了当前步骤的产出

    判断策略(保守,宁可误判为引用):
    1. 提取当前步骤产出中的文件路径(attachments + result中的路径)
    2. 若后续步骤描述中直接包含这些路径(或文件名) → 引用
    3. 若当前步骤有文件产出 且 后续步骤描述含指代词 → 引用

    Args:
        step: 刚执行完成的步骤
        plan: 当前计划

    Returns:
        True 表示后续步骤引用了当前产出(需更新计划), False 表示未引用
    """
    subsequent_descs = _get_subsequent_pending_descriptions(step, plan)
    if not subsequent_descs:
        return False  # 无后续步骤,无需更新

    # 1.提取当前步骤产出中的文件路径
    output_paths = _extract_output_paths(step)
    if output_paths:
        # 检查后续步骤描述是否直接引用了这些路径或文件名
        for desc in subsequent_descs:
            for path in output_paths:
                # 提取文件名(支持子路径匹配)
                basename = path.rsplit("/", 1)[-1] if "/" in path else path
                if path in desc or basename in desc:
                    return True

        # 2.当前步骤有文件产出 且 后续步骤含指代词 → 保守判断为引用
        for desc in subsequent_descs:
            if any(kw in desc for kw in _REFERENTIAL_KEYWORDS):
                return True

    return False


def _get_subsequent_pending_descriptions(step: Step, plan: Plan) -> List[str]:
    """获取当前步骤之后所有未完成(PENDING)步骤的描述列表

    步骤按 plan.steps 顺序执行,当前步骤之后的 PENDING 步骤即为"后续未完成步骤"。

    Args:
        step: 当前步骤
        plan: 当前计划

    Returns:
        后续 PENDING 步骤的描述列表(无后续步骤时返回空列表)
    """
    if not plan or not plan.steps:
        return []

    # 定位当前步骤在计划中的索引
    try:
        current_idx = next(
            i for i, s in enumerate(plan.steps) if s.id == step.id
        )
    except StopIteration:
        return []  # 当前步骤不在计划中(异常情况)

    # 收集当前步骤之后的所有 PENDING 步骤描述
    return [
        s.description
        for s in plan.steps[current_idx + 1:]
        if s.status == ExecutionStatus.PENDING and s.description
    ]


def _extract_output_paths(step: Step) -> List[str]:
    """从步骤产出中提取文件路径

    来源:
    1. step.attachments: 显式声明的附件路径列表
    2. step.result: 结果文本中匹配到的文件路径

    Args:
        step: 已完成的步骤

    Returns:
        去重后的文件路径列表
    """
    paths: List[str] = []
    # 1.显式附件路径
    if step.attachments:
        paths.extend(fp for fp in step.attachments if fp)
    # 2.result 文本中的路径
    if step.result:
        paths.extend(_FILE_PATH_PATTERN.findall(step.result))
    # 去重保序
    return list(dict.fromkeys(paths))
