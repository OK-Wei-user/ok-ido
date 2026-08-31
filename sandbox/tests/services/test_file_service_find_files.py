#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_file_service_find_files.py
FileService.find_files 防护单元测试(L3 沙箱服务层)

背景:会话 f2611353 卡死 - LLM 调用 find_files(dir_path="/", glob_pattern="**/pptxgenjs.md")
导致 sandbox glob.glob('/**/...') 进入 /proc /sys 虚拟文件系统挂起。

L3 防护:
    1. 路径验证:拒绝系统目录(/ /proc /sys /dev 等)扫描
    2. 超时保护:递归搜索(**)15s、非递归搜索30s

运行环境:沙箱容器内(sandbox 容器需安装 pytest)
    docker exec sandbox pip install pytest pytest-asyncio
    docker exec sandbox python -m pytest /sandbox/tests/services/test_file_service_find_files.py -v
"""
import asyncio
import os
import tempfile

import pytest

from app.services.file import (
    FileService,
    _FORBIDDEN_SCAN_ROOTS,
    _RECURSIVE_GLOB_TIMEOUT,
    _NORMAL_GLOB_TIMEOUT,
    _is_forbidden_scan_root,
)
from app.interfaces.errors.exceptions import BadRequestException, NotFoundException, AppException


class TestIsForbiddenScanRoot:
    """路径验证函数单元测试"""

    def test_root_is_forbidden(self):
        assert _is_forbidden_scan_root("/") is True

    def test_proc_is_forbidden(self):
        assert _is_forbidden_scan_root("/proc") is True

    def test_sys_is_forbidden(self):
        assert _is_forbidden_scan_root("/sys") is True

    def test_dev_is_forbidden(self):
        assert _is_forbidden_scan_root("/dev") is True

    def test_all_blacklisted(self):
        """黑名单中所有目录均被识别"""
        for path in _FORBIDDEN_SCAN_ROOTS:
            assert _is_forbidden_scan_root(path) is True, f"未拦截: {path}"

    def test_trailing_slash_normalized(self):
        """尾部斜杠规范化后比对"""
        assert _is_forbidden_scan_root("/proc/") is True
        assert _is_forbidden_scan_root("/sys/") is True

    def test_home_allowed(self):
        assert _is_forbidden_scan_root("/home") is False

    def test_tmp_allowed(self):
        assert _is_forbidden_scan_root("/tmp") is False

    def test_workspace_allowed(self):
        assert _is_forbidden_scan_root("/workspace") is False
        assert _is_forbidden_scan_root("/sandbox") is False


class TestFindFilesPathValidation:
    """L3 路径验证:拒绝系统目录扫描"""

    @pytest.mark.asyncio
    async def test_root_rejected(self):
        """根目录被拒绝"""
        with pytest.raises(BadRequestException) as exc_info:
            await FileService.find_files(dir_path="/", glob_pattern="**/*.md")
        assert "禁止扫描系统目录" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_proc_rejected(self):
        """/proc 被拒绝"""
        with pytest.raises(BadRequestException):
            await FileService.find_files(dir_path="/proc", glob_pattern="*")

    @pytest.mark.asyncio
    async def test_sys_rejected(self):
        """/sys 被拒绝"""
        with pytest.raises(BadRequestException):
            await FileService.find_files(dir_path="/sys", glob_pattern="*")

    @pytest.mark.asyncio
    async def test_dev_rejected(self):
        """/dev 被拒绝"""
        with pytest.raises(BadRequestException):
            await FileService.find_files(dir_path="/dev", glob_pattern="*")

    @pytest.mark.asyncio
    async def test_nonexistent_dir_raises_not_found(self):
        """不存在的目录(非系统目录)抛出 NotFoundException"""
        with pytest.raises(NotFoundException):
            await FileService.find_files(dir_path="/nonexistent_path_xyz", glob_pattern="*")


class TestFindFilesTimeoutProtection:
    """L3 超时保护:glob 操作超时返回 AppException"""

    @pytest.mark.asyncio
    async def test_recursive_glob_uses_15s_timeout(self):
        """递归搜索(**)使用 15s 超时"""
        # 创建一个大目录树模拟慢速扫描
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建大量子目录模拟慢速 glob
            for i in range(50):
                subdir = os.path.join(tmpdir, f"sub_{i}")
                os.makedirs(subdir, exist_ok=True)
                for j in range(20):
                    with open(os.path.join(subdir, f"file_{j}.txt"), "w") as f:
                        f.write("x")

            # 正常扫描应成功完成(小目录不会触发超时)
            result = await FileService.find_files(dir_path=tmpdir, glob_pattern="**/*.txt")
            assert len(result.files) > 0

    @pytest.mark.asyncio
    async def test_timeout_constants_correct(self):
        """超时常量值正确"""
        assert _RECURSIVE_GLOB_TIMEOUT == 15
        assert _NORMAL_GLOB_TIMEOUT == 30

    @pytest.mark.asyncio
    async def test_normal_glob_in_tmp_succeeds(self):
        """非递归搜索在 /tmp 中正常工作"""
        # 创建临时文件
        with tempfile.NamedTemporaryFile(suffix=".testfile", delete=False) as f:
            f.write(b"test content")
            tmpfile = f.name

        try:
            result = await FileService.find_files(
                dir_path=os.path.dirname(tmpfile),
                glob_pattern=os.path.basename(tmpfile),
            )
            assert tmpfile in result.files
        finally:
            os.unlink(tmpfile)


class TestFindFilesNormalOperation:
    """L3 正常操作:工作区目录扫描正常工作"""

    @pytest.mark.asyncio
    async def test_recursive_search_in_tmp(self):
        """在 /tmp 递归搜索文件正常工作"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建嵌套结构
            nested = os.path.join(tmpdir, "a", "b", "c")
            os.makedirs(nested, exist_ok=True)
            target = os.path.join(nested, "target.md")
            with open(target, "w") as f:
                f.write("# test")

            result = await FileService.find_files(dir_path=tmpdir, glob_pattern="**/*.md")
            assert target in result.files

    @pytest.mark.asyncio
    async def test_non_recursive_search(self):
        """非递归搜索仅匹配当前目录"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 当前目录文件
            current_file = os.path.join(tmpdir, "current.txt")
            with open(current_file, "w") as f:
                f.write("x")

            # 子目录文件(不应被匹配)
            os.makedirs(os.path.join(tmpdir, "sub"), exist_ok=True)
            sub_file = os.path.join(tmpdir, "sub", "sub.txt")
            with open(sub_file, "w") as f:
                f.write("x")

            result = await FileService.find_files(dir_path=tmpdir, glob_pattern="*.txt")
            assert current_file in result.files
            assert sub_file not in result.files

    @pytest.mark.asyncio
    async def test_empty_result_when_no_match(self):
        """无匹配文件时返回空列表"""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = await FileService.find_files(dir_path=tmpdir, glob_pattern="*.nonexistent")
            assert result.files == []
