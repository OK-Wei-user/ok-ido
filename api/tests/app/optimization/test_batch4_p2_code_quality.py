#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_batch4_p2_code_quality.py
批次4 P2代码质量统一单元测试 — 验证F4-1~F4-5五项优化

测试覆盖:
- F4-1: 移除未使用导入/无效变量/修复f-string/修复函数名重复bug
- F4-2: 统一异常处理(4处except pass改为带日志)
- F4-3: 统一日志格式(session_vnc_routes中英混杂)
- F4-4: 类型注解完善(user_service.__init__)
- F4-5: 文件头注释规范化(验证一致性,已知不一致项记录于优化方案)

测试策略:
- 静态分析(AST/源码字符串)为主,避免运行时依赖
- pyflakes零告警验证(F4-1核心指标)
- 关键bug回归: app_config_routes函数名重复bug修复验证
"""
import ast
import importlib
import inspect
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 项目根目录(用于跨目录导入与文件扫描)
# tests/app/optimization/test_batch4_p2_code_quality.py -> parents[3] = api/
_API_ROOT = Path(__file__).resolve().parents[3]
_APP_ROOT = _API_ROOT / "app"


# ============ F4-1: 移除未使用导入/无效变量/修复f-string/修复函数名重复bug ============

class TestF41UnusedImportsAndFString:
    """F4-1: 移除未使用导入/无效变量/修复f-string/修复函数名重复bug"""

    def _run_pyflakes_on_dirs(self) -> list:
        """运行pyflakes扫描指定目录,返回告警行列表"""
        try:
            import subprocess
            result = subprocess.run(
                [sys.executable, "-m", "pyflakes",
                 "app/application/services/", "app/domain/services/",
                 "app/domain/models/", "app/domain/repositories/",
                 "app/infrastructure/repositories/",
                 "app/infrastructure/external/llm/",
                 "app/interfaces/endpoints/"],
                capture_output=True, text=True, cwd=str(_API_ROOT),
            )
            # pyflakes返回0=无告警, 1=有告警
            return [line for line in result.stdout.splitlines() if line.strip()]
        except Exception:
            pytest.skip("pyflakes未安装,跳过静态分析验证")

    def test_pyflakes_clean_for_all_modified_dirs(self):
        """F4-1核心: 7个核心目录pyflakes零告警"""
        warnings = self._run_pyflakes_on_dirs()
        if warnings:
            # 输出告警详情便于排查
            print("\n".join(warnings))
        assert not warnings, f"pyflakes发现{len(warnings)}项告警,应已全部修复"

    def test_app_config_routes_no_duplicate_function_name(self):
        """F4-1关键bug: app_config_routes中update_agent_config不应重复update_llm_config"""
        routes_path = _APP_ROOT / "interfaces" / "endpoints" / "app_config_routes.py"
        source = routes_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # 收集所有顶层async函数名
        func_names = [
            node.name for node in tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        ]

        # update_agent_config应存在(原bug: 误命名为update_llm_config)
        assert "update_agent_config" in func_names, "update_agent_config函数应存在(原bug已修复)"
        # update_llm_config应只有1个(原bug: 有2个同名函数)
        assert func_names.count("update_llm_config") == 1, \
            f"update_llm_config应只有1个(POST /llm), 实际{func_names.count('update_llm_config')}"
        # update_agent_config应只有1个
        assert func_names.count("update_agent_config") == 1, \
            f"update_agent_config应只有1个(POST /agent), 实际{func_names.count('update_agent_config')}"

    def test_memory_no_unused_tool_call_id(self):
        """F4-1: memory.py合并tool消息方法中tool_call_id未使用变量已移除

        _merge_consecutive_tool_messages中原本有未使用的tool_call_id赋值
        (带空字符串默认值),已移除。注意_sanitize_messages中同名的tool_call_id
        赋值(无默认值)是实际使用的,不应被移除。
        """
        memory_path = _APP_ROOT / "domain" / "models" / "memory.py"
        source = memory_path.read_text(encoding="utf-8")
        # 被移除的行: tool_call_id = msg.get("tool_call_id", "") (带空字符串默认值,未使用)
        assert 'tool_call_id = msg.get("tool_call_id", "")' not in source, \
            "_merge_consecutive_tool_messages中未使用的tool_call_id赋值应已移除(F4-1清理)"
        # 保留的行: tool_call_id = msg.get("tool_call_id") (无默认值,在_sanitize_messages中实际使用)
        assert 'tool_call_id = msg.get("tool_call_id")' in source, \
            "_sanitize_messages中实际使用的tool_call_id赋值应保留"

    def test_no_fstring_without_placeholders_in_key_files(self):
        """F4-1: 关键文件中不应有f-string无占位符告警(由pyflakes权威验证)

        注: pyflakes检测是权威源,本测试作为补充仅检查无多行拼接的简单f-string。
        多行f-string拼接(如logger.warning(f"..." f"..."))的AST分析易产生误报,
        故仅在不涉及多行拼接的文件上做AST补充检查。
        """
        # 仅检查不涉及多行f-string拼接的简单文件
        files_to_check = [
            "application/services/agent_service.py",
            "interfaces/endpoints/session_vnc_routes.py",
        ]
        for rel_path in files_to_check:
            filepath = _APP_ROOT / rel_path
            source = filepath.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.JoinedStr):
                    has_placeholder = any(
                        isinstance(child, ast.FormattedValue)
                        for child in node.values
                    )
                    assert has_placeholder, \
                        f"{rel_path}行{node.lineno} f-string无占位符,应改为普通字符串"


# ============ F4-2: 统一异常处理 ============

class TestF42ExceptionHandling:
    """F4-2: 统一异常处理(4处except pass改为带日志)"""

    def test_user_service_logout_no_bare_except_pass(self):
        """F4-2: user_service.logout中except Exception改为带logger.debug"""
        service_path = _APP_ROOT / "application" / "services" / "user_service.py"
        source = service_path.read_text(encoding="utf-8")
        # 不应有裸 except Exception: pass
        assert "except Exception:\n                pass" not in source, \
            "user_service中不应有裸except Exception:pass(F4-2已改为带日志)"
        # 应有logger.debug降级日志
        assert "logger.debug" in source, \
            "user_service应有logger.debug降级日志(F4-2)"

    def test_memory_no_bare_except_pass_in_key_methods(self):
        """F4-2: memory.py中URL解析与动态截断的except pass已改为带日志"""
        memory_path = _APP_ROOT / "domain" / "models" / "memory.py"
        source = memory_path.read_text(encoding="utf-8")
        # 不应有裸 except Exception: pass
        assert "except Exception:\n                pass" not in source
        assert "except Exception:\n            pass" not in source
        # 应有F4-2标记的debug日志
        assert "F4-2" in source, "memory.py应有F4-2标记的降级日志注释"

    def test_agent_task_runner_manifest_read_no_bare_pass(self):
        """F4-2: agent_task_runner的manifest读取except pass已改为带日志"""
        runner_path = _APP_ROOT / "domain" / "services" / "agent_task_runner.py"
        source = runner_path.read_text(encoding="utf-8")
        # _read_remote_skill_manifest方法中不应有裸pass
        assert "except Exception:\n            pass\n        return {}" not in source, \
            "_read_remote_skill_manifest不应有裸except pass(F4-2已改为带日志)"

    def test_all_service_layer_no_bare_except_pass(self):
        """F4-2扩展: 应用服务层与领域服务层不应有裸except Exception: pass"""
        scan_dirs = [
            _APP_ROOT / "application" / "services",
            _APP_ROOT / "domain" / "services",
            _APP_ROOT / "domain" / "models",
        ]
        violations = []
        for scan_dir in scan_dirs:
            if not scan_dir.exists():
                continue
            for py_file in scan_dir.rglob("*.py"):
                source = py_file.read_text(encoding="utf-8")
                # 检查 except Exception: 紧跟 pass 模式(允许不同缩进)
                lines = source.splitlines()
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped == "except Exception:":
                        # 查找下一个非空行
                        for j in range(i + 1, min(i + 5, len(lines))):
                            next_stripped = lines[j].strip()
                            if next_stripped:
                                if next_stripped == "pass":
                                    violations.append(f"{py_file.name}:{i + 1}")
                                break
        # browser/external基础设施层保留防御性pass(不列入扫描范围)
        assert not violations, \
            f"服务/模型层仍有裸except Exception:pass: {violations}"


# ============ F4-3: 统一日志格式 ============

class TestF43LogFormatConsistency:
    """F4-3: 统一日志格式(session_vnc_routes中英混杂)"""

    def test_session_vnc_routes_logs_use_chinese_session_format(self):
        """F4-3: session_vnc_routes日志应统一使用'会话[{session_id}]'中文格式"""
        vnc_path = _APP_ROOT / "interfaces" / "endpoints" / "session_vnc_routes.py"
        source = vnc_path.read_text(encoding="utf-8")
        # 不应遗留英文函数名式日志
        assert "forward_to_sandbox出错" not in source, \
            "应改为中文'Web->VNC转发异常'(F4-3统一中文格式)"
        assert "forward_from_sandbox出错" not in source, \
            "应改为中文'VNC->Web转发异常'(F4-3统一中文格式)"
        # 不应遗留半中半英的"Web->VNC连接终端"
        assert "Web->VNC连接终端" not in source, \
            "应改为'Web->VNC前端连接关闭'(F4-3统一格式)"
        # 不应遗留全角冒号"连接WebSocket VNC："
        assert "连接WebSocket VNC：" not in source, \
            "应改为'为会话[{session_id}]建立VNC后端连接'(F4-3统一格式与半角冒号)"
        # 应使用统一的会话[{session_id}]前缀
        assert "会话[{session_id}]" in source, \
            "VNC路由日志应使用'会话[{session_id}]'前缀(F4-3统一格式)"

    def test_no_fstring_without_placeholders_in_vnc_routes(self):
        """F4-3: session_vnc_routes不应有f-string无占位符告警"""
        vnc_path = _APP_ROOT / "interfaces" / "endpoints" / "session_vnc_routes.py"
        source = vnc_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                has_placeholder = any(
                    isinstance(child, ast.FormattedValue)
                    for child in node.values
                )
                assert has_placeholder, \
                    f"行{node.lineno} f-string无占位符,应改为普通字符串"


# ============ F4-4: 类型注解完善 ============

class TestF44TypeAnnotation:
    """F4-4: 类型注解完善(user_service.__init__)"""

    def test_user_service_init_has_uow_factory_annotation(self):
        """F4-4: UserService.__init__的uow_factory参数应有类型注解"""
        # 动态导入验证(确保语法正确且可导入)
        sys.path.insert(0, str(_API_ROOT))
        try:
            from app.application.services.user_service import UserService
            sig = inspect.signature(UserService.__init__)
            uow_factory_param = sig.parameters.get("uow_factory")
            assert uow_factory_param is not None, "uow_factory参数应存在"
            annotation = uow_factory_param.annotation
            # Callable[[], IUnitOfWork]的字符串表示应包含Callable与IUnitOfWork
            annotation_str = str(annotation)
            assert "Callable" in annotation_str, \
                f"uow_factory应有Callable类型注解,实际: {annotation_str}"
            assert "IUnitOfWork" in annotation_str, \
                f"uow_factory应标注返回IUnitOfWork,实际: {annotation_str}"
        finally:
            sys.path.pop(0)

    def test_user_service_init_docstring_exists(self):
        """F4-4: UserService.__init__应有完整docstring说明参数用途"""
        sys.path.insert(0, str(_API_ROOT))
        try:
            from app.application.services.user_service import UserService
            docstring = UserService.__init__.__doc__
            assert docstring is not None, "__init__应有docstring"
            assert "uow_factory" in docstring, "docstring应说明uow_factory参数"
            assert "token_blacklist" in docstring, "docstring应说明token_blacklist参数"
        finally:
            sys.path.pop(0)


# ============ F4-5: 文件头注释规范化 ============

class TestF45FileHeaderConsistency:
    """F4-5: 文件头注释规范化(验证一致性)"""

    def test_all_modified_files_have_shebang_and_coding(self):
        """F4-5: 批次4修改的所有文件应有shebang与coding声明"""
        modified_files = [
            "application/services/agent_service.py",
            "application/services/user_service.py",
            "domain/models/memory.py",
            "domain/repositories/session_repository.py",
            "domain/repositories/user_repository.py",
            "domain/services/agent_task_runner.py",
            "domain/services/agents/base.py",
            "domain/services/flows/planner_react.py",
            "domain/services/tools/a2a.py",
            "domain/services/tools/mcp.py",
            "interfaces/endpoints/app_config_routes.py",
            "interfaces/endpoints/auth_routes.py",
            "interfaces/endpoints/session_file_routes.py",
            "interfaces/endpoints/session_vnc_routes.py",
        ]
        for rel_path in modified_files:
            filepath = _APP_ROOT / rel_path
            source = filepath.read_text(encoding="utf-8")
            # 应有shebang
            assert source.startswith("#!/usr/bin/env python"), \
                f"{rel_path}应以shebang开头"
            # 应有coding声明
            assert "# -*- coding: utf-8 -*-" in source[:200], \
                f"{rel_path}应有coding声明"
            # 应有模块docstring
            assert '"""' in source[:500], \
                f"{rel_path}应有模块docstring"

    def test_modified_files_have_file_annotation(self):
        """F4-5: 批次4修改的所有文件应有@File标注"""
        modified_files = [
            "application/services/agent_service.py",
            "application/services/user_service.py",
            "domain/models/memory.py",
            "domain/repositories/session_repository.py",
            "domain/repositories/user_repository.py",
            "domain/services/agent_task_runner.py",
            "domain/services/agents/base.py",
            "domain/services/flows/planner_react.py",
            "domain/services/tools/a2a.py",
            "domain/services/tools/mcp.py",
            "interfaces/endpoints/app_config_routes.py",
            "interfaces/endpoints/auth_routes.py",
            "interfaces/endpoints/session_file_routes.py",
            "interfaces/endpoints/session_vnc_routes.py",
        ]
        for rel_path in modified_files:
            filepath = _APP_ROOT / rel_path
            source = filepath.read_text(encoding="utf-8")
            # 前10行应有@File标注
            first_10_lines = "\n".join(source.splitlines()[:10])
            assert "@File" in first_10_lines, \
                f"{rel_path}前10行应有@File标注"


# ============ 回归验证: 关键功能不破坏 ============

class TestF4RegressionSafety:
    """F4批次回归验证: 确保代码质量优化不破坏现有功能"""

    def test_app_config_routes_importable(self):
        """回归: app_config_routes模块可正常导入(函数名修复未破坏导入)"""
        sys.path.insert(0, str(_API_ROOT))
        try:
            from app.interfaces.endpoints import app_config_routes
            # update_agent_config函数应存在
            assert hasattr(app_config_routes, "update_agent_config"), \
                "update_agent_config函数应存在(F4-1修复后)"
            # update_llm_config函数应存在(POST /llm路由)
            assert hasattr(app_config_routes, "update_llm_config"), \
                "update_llm_config函数应存在(POST /llm路由)"
        finally:
            sys.path.pop(0)

    def test_memory_merge_tool_messages_still_works(self):
        """回归: memory.py合并tool消息逻辑(Batch16后保守策略)仍正常

        Batch16修复: _merge_consecutive_tool_messages 现在跟踪 tool_call_id →
        assistant(tool_calls) 的归属关系,无法确定归属的孤儿tool消息(无owning
        assistant)保守不合并,防止丢失同一assistant的并行tool结果。

        本测试验证新保守策略:
        - 场景1: 孤儿tool消息(无assistant tool_calls)→ 不合并,保留全部消息
        - 场景2: 不同assistant轮次的连续同fn tool消息 → 合并为1条(保留最后内容)
        """
        sys.path.insert(0, str(_API_ROOT))
        try:
            from app.domain.models.memory import Memory

            # ---- 场景1: 孤儿tool消息(无assistant tool_calls归属)→ 保守不合并 ----
            # _PROTECT_HEAD_COUNT + _PROTECT_TAIL_COUNT = 2 + 4 = 6
            # 构造9条消息(2 head + 4连续tool + 2 tail + 1 assistant)超阈值触发合并检查
            orphan_messages = [
                {"role": "system", "content": "system"},  # head
                {"role": "user", "content": "hello"},  # head
                {"role": "tool", "function_name": "shell_exec", "tool_call_id": "tc-1", "content": "result-1"},
                {"role": "tool", "function_name": "shell_exec", "tool_call_id": "tc-2", "content": "result-2"},
                {"role": "tool", "function_name": "shell_exec", "tool_call_id": "tc-3", "content": "result-3"},
                {"role": "tool", "function_name": "browser_view", "tool_call_id": "tc-4", "content": "page"},
                {"role": "assistant", "content": "thinking"},  # tail
                {"role": "user", "content": "next"},  # tail
                {"role": "assistant", "content": "done"},  # tail
            ]
            mem = Memory.model_construct(messages=orphan_messages)
            mem._merge_consecutive_tool_messages()
            # 孤儿tool消息(无owning assistant): 保守不合并,9条全部保留
            assert len(mem.messages) == 9, f"孤儿tool消息应保守不合并,实际{len(mem.messages)}"
            tool_msgs = [m for m in mem.messages if m.get("role") == "tool"]
            assert len(tool_msgs) == 4, "孤儿tool消息应全部保留(防并行结果丢失)"

            # ---- 场景2: 不同assistant轮次的连续同fn tool消息 → 合并 ----
            # 构造合法合并场景: 两个不同assistant各自发起一个shell_exec调用,
            # 但两个tool结果连续出现(中间无其他消息),且归属不同assistant → 合并
            merge_messages = [
                {"role": "system", "content": "system"},  # idx=0, head
                {"role": "user", "content": "hello"},  # idx=1, head
                {"role": "assistant", "content": "thinking1", "tool_calls": [
                    {"id": "tc-A", "type": "function", "function": {"name": "shell_exec"}}
                ]},  # idx=2
                {"role": "assistant", "content": "thinking2", "tool_calls": [
                    {"id": "tc-B", "type": "function", "function": {"name": "shell_exec"}}
                ]},  # idx=3 (连续两个assistant,模拟历史压缩后的边缘场景)
                {"role": "tool", "function_name": "shell_exec", "tool_call_id": "tc-A", "content": "result-A"},  # 属于 idx=2
                {"role": "tool", "function_name": "shell_exec", "tool_call_id": "tc-B", "content": "result-B"},  # 属于 idx=3, 不同idx → 合并
                {"role": "assistant", "content": "thinking3", "tool_calls": [
                    {"id": "tc-C", "type": "function", "function": {"name": "browser_view"}}
                ]},  # idx=6
                {"role": "tool", "function_name": "browser_view", "tool_call_id": "tc-C", "content": "page"},  # 不同fn,不合并
                {"role": "assistant", "content": "tail1"},  # tail
                {"role": "user", "content": "tail2"},  # tail
                {"role": "assistant", "content": "tail3"},  # tail
                {"role": "assistant", "content": "tail4"},  # tail
            ]
            mem2 = Memory.model_construct(messages=merge_messages)
            mem2._merge_consecutive_tool_messages()
            # tc-A 与 tc-B 同fn且不同assistant → 合并为1条(保留最后内容 result-B)
            # tc-C 不同fn → 不合并,保留1条
            tool_msgs2 = [m for m in mem2.messages if m.get("role") == "tool"]
            shell_msgs2 = [m for m in tool_msgs2 if m.get("function_name") == "shell_exec"]
            assert len(shell_msgs2) == 1, f"shell_exec应合并为1条,实际{len(shell_msgs2)}"
            assert shell_msgs2[0]["content"] == "result-B", "应保留最后一条内容(result-B)"
            # browser_view保留1条
            browser_msgs = [m for m in tool_msgs2 if m.get("function_name") == "browser_view"]
            assert len(browser_msgs) == 1, "browser_view应保留1条"
        finally:
            sys.path.pop(0)
