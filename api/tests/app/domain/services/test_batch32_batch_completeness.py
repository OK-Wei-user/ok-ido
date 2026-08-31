#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Batch 32: 批量任务完整性校验单元测试

验证 _batch_verifier 纯函数的量化目标提取与完成数比对逻辑。
覆盖: 完整完成/部分完成无原因(拒绝)/部分完成有原因(附警告)/无量化目标(不校验)四种场景。
"""
from types import SimpleNamespace

import pytest

from app.domain.services.agents._batch_verifier import (
    count_completed_items,
    extract_quantified_target,
    verify_batch_completeness,
)


def _make_step(description: str, result: str = "", attachments=None):
    """构造测试用 step 对象(仅含校验所需属性)"""
    return SimpleNamespace(
        description=description,
        result=result,
        attachments=attachments or [],
    )


class TestExtractQuantifiedTarget:
    """量化目标提取测试"""

    def test_extract_numeric_target(self):
        """'导出50条' → 50"""
        assert extract_quantified_target("导出50条数据") == 50

    def test_extract_no_target(self):
        """'分析数据' → None(无量化目标)"""
        assert extract_quantified_target("分析数据趋势") is None

    def test_extract_various_units(self):
        """不同量词均可提取"""
        assert extract_quantified_target("生成10个文件") == 10
        assert extract_quantified_target("处理100项记录") == 100


class TestVerifyBatchCompleteness:
    """批量任务完整性校验主逻辑测试"""

    def test_complete_batch_passes(self):
        """用例1: 目标50 + 完成50 → is_complete=True,无警告"""
        step = _make_step("导出50条数据", result="成功导出50条", attachments=["f1"] * 50)
        is_complete, guidance = verify_batch_completeness(step)
        assert is_complete is True
        assert guidance == ""

    def test_partial_without_reason_rejected(self):
        """用例2: 目标50 + 仅完成30 + 无原因 → is_complete=False,注入引导"""
        step = _make_step("导出50条数据", result="导出完成", attachments=["f1"] * 30)
        is_complete, guidance = verify_batch_completeness(step)
        assert is_complete is False
        assert "未通过" in guidance
        assert "50" in guidance
        assert "30" in guidance

    def test_partial_with_reason_allowed_with_warning(self):
        """用例3: 目标50 + 完成30 + 含原因 → is_complete=True,附警告"""
        step = _make_step(
            "导出50条数据",
            result="仅完成30条,剩余20条因权限失败",
            attachments=["f1"] * 30,
        )
        is_complete, guidance = verify_batch_completeness(step)
        assert is_complete is True
        assert "部分完成" in guidance
        assert "50" in guidance

    def test_no_quantified_target_not_checked(self):
        """用例4: 无量化目标 → is_complete=True,不校验"""
        step = _make_step("分析数据趋势", result="分析完成", attachments=["report.xlsx"])
        is_complete, guidance = verify_batch_completeness(step)
        assert is_complete is True
        assert guidance == ""

    def test_result_declared_count_can_satisfy_target(self):
        """result中声明的完成数满足目标时通过(attachments不足但result声明够)"""
        step = _make_step("导出50条数据", result="成功完成50条记录", attachments=["f1"] * 10)
        is_complete, guidance = verify_batch_completeness(step)
        assert is_complete is True
        assert guidance == ""

    # ------------------------------------------------------------------
    # 批量校验时机修复验证(会话6a9c2c12根因)
    # 修复: verify_batch_completeness 原在 step.result 设置前调用,
    #   导致 count_completed_items 返回 0,无罪推定(completed==0 and step.result)
    #   因 step.result 为空而失效,误报"目标N项完成0项"强制LLM重试浪费token。
    #   现移到 step.result 解析之后,以下测试验证修复后的正确行为。
    # ------------------------------------------------------------------

    def test_result_set_but_no_attachments_passes_via_presumption(self):
        """step.result已设置但attachments为空时,无罪推定通过(修复核心用例)

        场景: LLM返回result="导出完成"但未在JSON中声明attachments列表,
        count_completed_items返回0(无attachments无数字),但result非空。
        无罪推定逻辑(completed==0 and step.result)应信任LLM已完成。
        修复前: step.result为空时此逻辑失效,误报"目标6项完成0项"。
        """
        step = _make_step("导出6条出入库数据", result="导出完成,数据已保存", attachments=[])
        is_complete, guidance = verify_batch_completeness(step)
        assert is_complete is True
        assert guidance == ""

    def test_empty_result_with_target_fails(self):
        """step.result为空且有量化目标时,校验不通过(旧bug复现场景)

        场景: verify_batch_completeness在step.result设置前调用时,
        result为空,attachments为空,count_completed_items返回0,
        无罪推定因result为空而失效,返回(is_complete=False)。
        此测试验证: result确实为空时,校验正确拒绝(这是预期行为,
        修复点是确保此函数在result已设置后才被调用)。
        """
        step = _make_step("导出6条出入库数据", result="", attachments=[])
        is_complete, guidance = verify_batch_completeness(step)
        assert is_complete is False
        assert "未通过" in guidance
        assert "6" in guidance

    def test_result_with_partial_count_below_target_without_reason_fails(self):
        """result声明部分完成数(低于目标)且无原因时,校验不通过

        场景: 目标10项,result声明"已完成3项"但未说明剩余原因,
        count_completed_items提取到3,3<10且无部分完成原因关键词。
        """
        step = _make_step("导出10条数据", result="已完成3项", attachments=[])
        is_complete, guidance = verify_batch_completeness(step)
        assert is_complete is False
        assert "10" in guidance
        assert "3" in guidance

    def test_result_with_partial_count_below_target_with_reason_passes(self):
        """result声明部分完成数(低于目标)但有原因时,允许完成(附警告)

        场景: 目标10项,result声明"已完成3项,剩余7项因权限失败",
        含"剩余"关键词,允许COMPLETED但附警告。
        """
        step = _make_step(
            "导出10条数据",
            result="已完成3项,剩余7项因权限失败",
            attachments=[],
        )
        is_complete, guidance = verify_batch_completeness(step)
        assert is_complete is True
        assert "部分完成" in guidance

    def test_result_set_with_checkmarks_passes_via_count(self):
        """result含完成标记(✓)时,通过通道3计数通过校验

        场景: 上下文压缩后LLM用列表+勾号声明完成,
        count_completed_items通道3(✓计数)提取完成数。
        """
        step = _make_step(
            "导出5条数据",
            result="1.✓ 数据A\n2.✓ 数据B\n3.✓ 数据C\n4.✓ 数据D\n5.✓ 数据E",
            attachments=[],
        )
        is_complete, guidance = verify_batch_completeness(step)
        assert is_complete is True
        assert guidance == ""
