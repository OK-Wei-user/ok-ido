#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量任务完整性校验(Batch 32 / F11-3 + Batch 39 英文量词/合并引导)

纯函数模块,封装量化目标提取与完成数比对,便于单元测试。
设计原则:
- 无量化目标时不校验(避免误伤非批量步骤)
- 部分完成但LLM已说明原因时允许COMPLETED(附警告)
- 部分完成且无原因时注入引导重试一次(不无限阻塞)

Batch 39 扩展:
- 英文量词支持(records/files/items/sheets 等),覆盖 EN 语言步骤校验
- get_consolidation_guidance() 事前合并引导: 检测到量化目标时返回脚本合并建议,
  供 ReActAgent 在步骤上下文中注入,与 _fragments.SCRIPT_CONSOLIDATION 互补
  (prompt 层通用引导 + 量化目标感知的具体引导)
"""
import re
from typing import Optional, Tuple

# 量化目标提取正则: "导出50条" / "生成10个文件" / "共100项" / "export 50 records"
# Batch 39: 增加英文量词,覆盖 EN 语言步骤的完整性校验
_QUANTIFIED_PATTERNS = [
    re.compile(r'(\d+)\s*(条|个|项|份|篇|行|张|页|份|组|类)'),
    re.compile(r'(\d+)\s*(records?|files?|items?|sheets?|pages?|entries?|rows?|columns?|charts?|tables?|sections?|chapters?)', re.IGNORECASE),
]
# 部分完成原因关键词(LLM明确说明时允许COMPLETED)
_PARTIAL_REASON_KEYWORDS = ("部分", "仅完成", "剩余", "其中", "成功", "失败", "未完成",
                            "partial", "remaining", "only", "successful", "failed")

# Batch 39: 批量合并引导触发阈值(量化目标 >= 此值时注入合并建议)
# 低于此值的批量任务合并收益不明显,避免过度引导
_CONSOLIDATION_MIN_TARGET = 5

# 批次45 P1-2: 数据分析关键词(通道2,无量化目标时触发合并引导)
# 根因: 批次44 shell=86,数据分析任务无量化目标不触发原get_consolidation_guidance
_DATA_ANALYSIS_KEYWORDS = (
    "数据分析", "数据统计", "出入库", "经营分析", "报表生成",
    "数据汇总", "数据导出", "统计分析", "数据清洗", "数据计算",
)


def extract_quantified_target(step_description: str) -> Optional[int]:
    """从步骤描述提取量化目标N,无量化目标返回None"""
    if not step_description:
        return None
    for pattern in _QUANTIFIED_PATTERNS:
        match = pattern.search(step_description)
        if match and match.group(1).isdigit():
            return int(match.group(1))
    return None


def count_completed_items(step) -> int:
    """统计步骤实际完成数: 多通道取max

    三通道设计(覆盖上下文压缩后的多种输出形态):
    - 通道1: attachments数量(LLM在result JSON中声明的附件列表)
    - 通道2: result中"数字+量词"格式(如"已完成5项""生成10个文件")
    - 通道3: result中完成标记计数(✓/✅),覆盖"1.✓ 2.✓ 3.✓"列表完成格式
      (上下文压缩后LLM常用列表+勾号声明完成,原正则无法匹配此格式,会话6a347540根因)
    """
    count = 0
    # 通道1: attachments数量
    if step.attachments:
        count = max(count, len(step.attachments))
    if step.result:
        # 通道2: "数字+量词"格式
        for pattern in _QUANTIFIED_PATTERNS:
            for match in pattern.finditer(step.result):
                num_str = match.group(1)
                if num_str.isdigit():
                    count = max(count, int(num_str))
        # 通道3: 完成标记计数(✓/✅),覆盖列表完成格式
        checkmark_count = step.result.count("✓") + step.result.count("✅")
        if checkmark_count > 0:
            count = max(count, checkmark_count)
    return count


def verify_batch_completeness(step) -> Tuple[bool, str]:
    """校验批量任务完整性

    Returns: (is_complete, guidance_message)
    - 无量化目标: (True, "") 不校验
    - M >= N: (True, "") 完整完成
    - M < N 且 result含部分完成原因: (True, warning) 允许但附警告
    - M=0 且 result非空: (True, "") 信任完成(无罪推定,防上下文压缩误判)
    - M < N 且无原因: (False, guidance) 不允许COMPLETED,注入引导重试一次

    无罪推定原则(会话6a347540根因修复):
    - 上下文压缩后attachments可能丢失,result中可能未声明具体数字
    - 此时completed=0只表示"未提取到完成数",不代表"实际完成0项"
    - LLM已输出非空result说明它认为已完成,应信任而非误判
    - 仅当有明确的"M<N"证据(提取到部分数字)时才阻断
    """
    target = extract_quantified_target(step.description)
    if target is None:
        return (True, "")
    completed = count_completed_items(step)
    if completed >= target:
        return (True, "")
    # 无罪推定: completed=0但result非空时,信任LLM已完成(防上下文压缩误判)
    if completed == 0 and step.result:
        return (True, "")
    has_reason = step.result and any(kw in step.result for kw in _PARTIAL_REASON_KEYWORDS)
    if has_reason:
        return (True, f"⚠️ 批量任务部分完成: 目标{target},实际{completed}")
    return (False, (
        f"⚠️ 批量任务完整性校验未通过: 目标{target}项,实际仅完成{completed}项。"
        f"请继续完成剩余{target - completed}项,或在结果中说明部分完成的原因。"
    ))


def get_consolidation_guidance(step_description: str) -> str:
    """事前合并引导: 检测到量化目标或数据分析关键词时返回脚本合并建议

    双通道触发(批次45 P1-2 增强):
    - 通道1(Batch 39 / 方向4): 量化目标 N >= _CONSOLIDATION_MIN_TARGET 时触发
    - 通道2(批次45 P1-2): 数据分析关键词命中时触发(覆盖无量化目标的数据分析任务)

    与 _fragments.SCRIPT_CONSOLIDATION(通用 prompt 层引导)互补:
    - SCRIPT_CONSOLIDATION: 在系统提示词中注入通用合并原则(所有步骤可见)
    - 本函数: 量化目标或数据分析关键词感知,引导 LLM 合并 shell_execute 调用

    使用场景: ReActAgent.execute_step() 在设置步骤上下文时调用,
    若返回非空字符串则追加到 step_description,实现"事前引导"(而非事后校验)

    Args:
        step_description: 步骤描述文本

    Returns:
        合并引导字符串,无量化目标/目标过小/非数据分析时返回空串
    """
    # 通道1(原有): 量化目标 N >= 阈值
    target = extract_quantified_target(step_description)
    if target is not None and target >= _CONSOLIDATION_MIN_TARGET:
        return (
            f"\n[合并引导] 本步骤量化目标为 {target} 项,建议将批量同类操作合并为单次 "
            f"shell_execute 调用(在 Python 脚本内用循环完成),减少调用次数节省 token。"
        )
    # 通道2(批次45 P1-2新增): 数据分析关键词命中
    if step_description and any(kw in step_description for kw in _DATA_ANALYSIS_KEYWORDS):
        return (
            "\n[合并引导] 本步骤为数据分析任务,建议将多次 shell_execute 数据查询/计算"
            "合并为单次 Python 脚本调用(在脚本内用 pandas 循环处理),减少调用次数节省 token。"
        )
    return ""
