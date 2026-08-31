#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_python_kernel.py
PythonKernelService 单元测试 - 预加载/执行/超时/降级/异常路径覆盖
"""
import asyncio
import os
import sys
import tempfile
import textwrap
from unittest.mock import patch, MagicMock

import pytest

# 把 sandbox 目录加入 sys.path 以便导入 app.services.python_kernel
_SANDBOX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SANDBOX_ROOT not in sys.path:
    sys.path.insert(0, _SANDBOX_ROOT)

from app.services.python_kernel import (  # noqa: E402
    PythonKernelService,
    _preload_modules,
    _exec_script_worker,
    get_kernel_service,
    reset_kernel_service,
)


# -------------------- 辅助函数 --------------------

def _write_script(content: str) -> str:
    """写入临时 Python 脚本,返回绝对路径"""
    fd, path = tempfile.mkstemp(suffix=".py", prefix="kernel_test_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(content))
    return path


# -------------------- preload 测试 --------------------

class TestPreload:
    """预加载模块测试"""

    def test_preload_idempotent(self):
        """preload 多次调用应幂等,不重复导入"""
        service = PythonKernelService()
        assert service._preloaded is False

        service.preload()
        assert service._preloaded is True

        # 第二次调用应直接返回,不报错
        service.preload()
        assert service._preloaded is True

    def test_preload_modules_function_does_not_raise(self):
        """_preload_modules 函数应能正常执行,不抛异常"""
        # 即使某些模块不存在,也不应抛异常
        _preload_modules()


# -------------------- exec_script 正常路径测试 --------------------

class TestExecScriptNormal:
    """exec_script 正常执行路径测试"""

    @pytest.mark.asyncio
    async def test_exec_simple_script(self):
        """执行简单脚本应返回 stdout 和退出码 0"""
        script = """
            print("hello kernel")
            x = 1 + 2
            print(f"result={x}")
        """
        path = _write_script(script)
        try:
            service = PythonKernelService()
            returncode, stdout, stderr = await service.exec_script(path, timeout=30)
            assert returncode == 0
            assert "hello kernel" in stdout
            assert "result=3" in stdout
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_exec_imports_pandas_fast(self):
        """执行 import pandas 的脚本应成功(复用预加载)"""
        # 跳过条件: 当前环境无 pandas(如 Windows 本地),仅在沙箱容器内执行
        try:
            import pandas  # noqa: F401
        except ImportError:
            pytest.skip("pandas 未安装,仅在沙箱容器内执行此测试")

        script = """
            import pandas as pd
            df = pd.DataFrame({"a": [1, 2, 3]})
            print(f"rows={len(df)}")
            print(df.sum().to_dict())
        """
        path = _write_script(script)
        try:
            service = PythonKernelService()
            returncode, stdout, stderr = await service.exec_script(path, timeout=60)
            assert returncode == 0
            assert "rows=3" in stdout
            assert "6" in stdout  # sum of [1,2,3]
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_exec_script_with_args(self):
        """脚本应能读取 sys.argv[1:] 作为参数"""
        script = """
            import sys
            print(f"argv={sys.argv[1:]}")
        """
        path = _write_script(script)
        try:
            service = PythonKernelService()
            # 注意: 当前实现 sys.argv = [script_path],不传递额外参数
            returncode, stdout, stderr = await service.exec_script(path, timeout=10)
            assert returncode == 0
            assert "argv=[]" in stdout
        finally:
            os.unlink(path)


# -------------------- exec_script 异常路径测试 --------------------

class TestExecScriptError:
    """exec_script 异常路径测试"""

    @pytest.mark.asyncio
    async def test_script_with_sys_exit_nonzero(self):
        """sys.exit(1) 应返回退出码 1"""
        script = """
            import sys
            print("before exit")
            sys.exit(1)
        """
        path = _write_script(script)
        try:
            service = PythonKernelService()
            returncode, stdout, stderr = await service.exec_script(path, timeout=10)
            assert returncode == 1
            assert "before exit" in stdout
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_script_with_runtime_exception(self):
        """运行时异常应返回退出码 1 和 traceback"""
        script = """
            print("before error")
            raise ValueError("test error")
        """
        path = _write_script(script)
        try:
            service = PythonKernelService()
            returncode, stdout, stderr = await service.exec_script(path, timeout=10)
            assert returncode == 1
            assert "ValueError" in stderr
            assert "test error" in stderr
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_nonexistent_script(self):
        """不存在的脚本应返回错误,不抛异常"""
        service = PythonKernelService()
        returncode, stdout, stderr = await service.exec_script(
            "/nonexistent/path/script.py", timeout=10
        )
        assert returncode == 1
        assert "不存在" in stderr or "not exist" in stderr.lower()

    @pytest.mark.asyncio
    async def test_relative_path_rejected(self):
        """相对路径应被拒绝(安全考虑)"""
        service = PythonKernelService()
        returncode, stdout, stderr = await service.exec_script(
            "relative/script.py", timeout=10
        )
        assert returncode == 1
        assert "绝对路径" in stderr


# -------------------- exec_script 超时测试 --------------------

class TestExecScriptTimeout:
    """exec_script 超时保护测试"""

    @pytest.mark.asyncio
    async def test_timeout_terminates_process(self):
        """超时应终止进程并返回 124"""
        script = """
            import time
            print("starting long sleep")
            time.sleep(30)
            print("should not reach here")
        """
        path = _write_script(script)
        try:
            service = PythonKernelService()
            returncode, stdout, stderr = await service.exec_script(path, timeout=2)
            assert returncode == 124
            assert "超时" in stderr or "timeout" in stderr.lower()
        finally:
            os.unlink(path)


# -------------------- 状态隔离测试 --------------------

class TestStateIsolation:
    """多次执行间的状态隔离测试"""

    @pytest.mark.asyncio
    async def test_no_variable_leak_between_executions(self):
        """两次执行间不应有变量泄漏"""
        script1 = """
            shared_var = "from_script1"
            print(f"script1 sees shared_var={shared_var}")
        """
        script2 = """
            try:
                print(f"script2 sees shared_var={shared_var}")
            except NameError:
                print("script2: shared_var not defined (isolated)")
        """
        path1 = _write_script(script1)
        path2 = _write_script(script2)
        try:
            service = PythonKernelService()
            code1, out1, _ = await service.exec_script(path1, timeout=10)
            code2, out2, _ = await service.exec_script(path2, timeout=10)

            assert code1 == 0
            assert code2 == 0
            assert "from_script1" in out1
            assert "isolated" in out2  # 第二次执行不应看到第一次的变量
        finally:
            os.unlink(path1)
            os.unlink(path2)


# -------------------- 单例管理测试 --------------------

class TestSingleton:
    """全局单例管理测试"""

    def test_get_kernel_service_returns_singleton(self):
        """get_kernel_service 应返回同一实例"""
        reset_kernel_service()
        s1 = get_kernel_service()
        s2 = get_kernel_service()
        assert s1 is s2

    def test_reset_kernel_service_clears_singleton(self):
        """reset_kernel_service 应清除单例"""
        s1 = get_kernel_service()
        reset_kernel_service()
        s2 = get_kernel_service()
        assert s1 is not s2
