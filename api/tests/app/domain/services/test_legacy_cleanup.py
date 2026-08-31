#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_legacy_cleanup.py
遗留建议修复验证测试 — Phase F注释清理 / Phase C字段移除 / Phase D ErrorEvent设计决策

验证三项遗留建议修复:
1. 生产代码(api/app/)无"XX优化"编号注释残留
2. KeyFact.importance字段已移除(旧数据兼容)
3. summarize失败时保留ErrorEvent(文档与代码一致性)
"""
import os
import re

import pytest

# 生产代码根目录
_PROD_CODE_ROOT = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "app"
)
_PROD_CODE_ROOT = os.path.normpath(_PROD_CODE_ROOT)

# "XX优化"编号注释模式(如 05优化、08优化、13优化等)
_OPTIMIZATION_NUM_PATTERN = re.compile(r"\d+优化")


class TestPhaseFNoOptimizationNumberComments:
    """Phase F: 验证生产代码无'XX优化'编号注释残留"""

    @staticmethod
    def _scan_python_files(root: str):
        """递归扫描目录下所有Python文件,返回(文件路径, 行号, 行内容)三元组"""
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                filepath = os.path.join(dirpath, filename)
                with open(filepath, encoding="utf-8") as f:
                    for lineno, line in enumerate(f, 1):
                        yield filepath, lineno, line

    def test_no_optimization_number_in_prod_code(self):
        """生产代码(api/app/)中不应存在'XX优化'编号注释

        Phase F已将所有编号注释清理为语义化描述,
        生产代码中残留编号注释会增加维护成本,干扰开发者理解。
        """
        violations = []
        for filepath, lineno, line in self._scan_python_files(_PROD_CODE_ROOT):
            if _OPTIMIZATION_NUM_PATTERN.search(line):
                violations.append(f"{filepath}:{lineno}: {line.strip()}")

        assert not violations, (
            f"生产代码中发现{len(violations)}处'XX优化'编号注释残留:\n"
            + "\n".join(violations)
        )


class TestPhaseCKeyFactImportanceRemoved:
    """Phase C: 验证KeyFact.importance字段已移除且旧数据兼容"""

    def test_importance_field_removed(self):
        """KeyFact.importance字段应已移除(不再存在于model_fields)"""
        from app.domain.models.memory import KeyFact

        # 验证字段已移除
        assert "importance" not in KeyFact.model_fields, (
            "KeyFact.importance字段应已移除,不再保留废弃字段"
        )

    def test_no_importance_field_in_source(self):
        """KeyFact源码不应包含importance字段定义"""
        memory_model_path = os.path.join(
            _PROD_CODE_ROOT, "domain", "models", "memory.py"
        )
        with open(memory_model_path, encoding="utf-8") as f:
            source = f.read()

        # 查找importance字段定义行(字段声明格式: importance: float = ...)
        importance_lines = [
            line for line in source.splitlines()
            if "importance" in line and "float" in line
        ]
        assert not importance_lines, (
            f"KeyFact源码不应包含importance字段定义,实际找到: {importance_lines}"
        )

    def test_extract_key_facts_not_using_importance(self):
        """extract_key_facts创建KeyFact时不应传importance参数"""
        memory_model_path = os.path.join(
            _PROD_CODE_ROOT, "domain", "models", "memory.py"
        )
        with open(memory_model_path, encoding="utf-8") as f:
            source = f.read()

        # 查找KeyFact构造调用,验证不含importance参数
        keyfact_constructions = re.findall(
            r"KeyFact\([^)]*\)", source, re.DOTALL
        )
        assert keyfact_constructions, "未找到KeyFact构造调用"

        for construction in keyfact_constructions:
            assert "importance" not in construction, (
                f"extract_key_facts创建KeyFact时不应传importance参数,实际: {construction[:100]}"
            )

    def test_old_data_with_importance_deserializes(self):
        """含importance字段的旧JSONB数据应能正常反序列化(pydantic extra=ignore)"""
        from app.domain.models.memory import KeyFact

        # 旧数据含importance字段,反序列化时应被pydantic自动忽略
        old_data = {
            "category": "url",
            "content": "https://example.com",
            "importance": 0.8,
        }
        fact = KeyFact(**old_data)
        assert fact.category == "url"
        assert fact.content == "https://example.com"
        # importance字段已移除,不应存在于实例上
        assert not hasattr(fact, "importance")


class TestPhaseDErrorEventRetention:
    """Phase D: 验证summarize失败时保留ErrorEvent与JSON解析降级"""

    def test_summarize_yields_error_event_on_failure(self):
        """summarize方法LLM调用失败时应yield ErrorEvent

        设计决策: 保留ErrorEvent比"不yield"更合理,
        让外层planner_react能感知summarize失败并触发_build_fallback_summary兜底交付。
        """
        react_path = os.path.join(
            _PROD_CODE_ROOT, "domain", "services", "agents", "react.py"
        )
        with open(react_path, encoding="utf-8") as f:
            source = f.read()

        # 验证summarize方法中存在ErrorEvent yield
        assert 'yield ErrorEvent' in source, (
            "summarize方法应保留ErrorEvent yield,让外层能感知失败并触发兜底"
        )

    def test_summarize_has_json_fallback(self):
        """summarize方法应有JSON解析降级: JSON解析失败时使用原始文本"""
        react_path = os.path.join(
            _PROD_CODE_ROOT, "domain", "services", "agents", "react.py"
        )
        with open(react_path, encoding="utf-8") as f:
            source = f.read()

        # 非流式_invoke_llm(tools_enabled=False)作为主调用
        assert "tools_enabled=False" in source, (
            "summarize应使用tools_enabled=False禁用工具,强制LLM生成文本内容"
        )
        # JSON解析降级: _json_parser解析失败时使用原始文本
        assert "_json_parser" in source, (
            "summarize应使用_json_parser解析JSON输出,解析失败时降级为原始文本"
        )

    def test_no_strip_dsml_artifacts_in_summarize(self):
        """summarize方法不应调用_strip_dsml_artifacts(Phase B已删除)"""
        react_path = os.path.join(
            _PROD_CODE_ROOT, "domain", "services", "agents", "react.py"
        )
        with open(react_path, encoding="utf-8") as f:
            source = f.read()

        assert "_strip_dsml_artifacts" not in source, (
            "Phase B已删除_strip_dsml_artifacts,react.py不应再调用"
        )

    def test_no_streaming_dsml_filter_in_react(self):
        """react.py不应再import StreamingDSMLFilter(改非流式JSON解析后不需要)"""
        react_path = os.path.join(
            _PROD_CODE_ROOT, "domain", "services", "agents", "react.py"
        )
        with open(react_path, encoding="utf-8") as f:
            source = f.read()

        assert "StreamingDSMLFilter" not in source, (
            "summarize改为非流式JSON解析后,react.py不应再引用StreamingDSMLFilter"
        )

    def test_no_normalize_summary_output_in_react(self):
        """react.py不应再包含_normalize_summary_output方法(改JSON解析后不需要)"""
        react_path = os.path.join(
            _PROD_CODE_ROOT, "domain", "services", "agents", "react.py"
        )
        with open(react_path, encoding="utf-8") as f:
            source = f.read()

        assert "_normalize_summary_output" not in source, (
            "summarize改为非流式JSON解析后,_normalize_summary_output方法应已移除"
        )

    def test_fallback_summary_exists_in_planner_react(self):
        """planner_react应存在_build_fallback_summary兜底交付方法"""
        planner_react_path = os.path.join(
            _PROD_CODE_ROOT, "domain", "services", "flows", "planner_react.py"
        )
        with open(planner_react_path, encoding="utf-8") as f:
            source = f.read()

        assert "_build_fallback_summary" in source, (
            "planner_react应存在_build_fallback_summary兜底交付方法"
        )
