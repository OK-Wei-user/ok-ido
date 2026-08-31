#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_skill_incremental_sync.py
04-Skills增量同步单元测试
- manifest构建: 本地技能脚本md5计算
- 增量同步: 无变更跳过/有变更仅上传变更文件
- manifest持久化: 读写沙箱manifest
- 并行初始化: Skills+A2A并行, MCP顺序
"""
import hashlib
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.services.agent_task_runner import AgentTaskRunner


class TestSkillManifestBuild:
    """_build_local_skill_manifest 本地manifest构建测试"""

    def _create_runner(self, skills=None) -> AgentTaskRunner:
        """创建带mock技能服务的AgentTaskRunner"""
        with patch.object(AgentTaskRunner, '__init__', lambda self: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._skill_tool = MagicMock()
            runner._skill_tool._skill_service = MagicMock()
            runner._skill_tool._skill_service.list_skills = AsyncMock(return_value=skills or [])
            return runner

    def _create_mock_skill(self, path: str):
        """创建mock skill对象"""
        skill = MagicMock()
        skill.path = path
        return skill

    @pytest.mark.asyncio
    async def test_manifest_empty_when_no_skills(self):
        """无技能时manifest为空"""
        runner = self._create_runner(skills=[])
        manifest = await runner._build_local_skill_manifest()
        assert manifest == {}

    @pytest.mark.asyncio
    async def test_manifest_empty_when_no_scripts_dir(self):
        """技能无scripts目录时manifest为空"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill = self._create_mock_skill(tmpdir)
            runner = self._create_runner(skills=[skill])
            manifest = await runner._build_local_skill_manifest()
            assert manifest == {}

    @pytest.mark.asyncio
    async def test_manifest_contains_script_md5(self):
        """manifest包含脚本文件的md5"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建scripts目录和测试脚本
            scripts_dir = os.path.join(tmpdir, "scripts")
            os.makedirs(scripts_dir)
            script_content = b"print('hello')"
            script_path = os.path.join(scripts_dir, "test.py")
            with open(script_path, "wb") as f:
                f.write(script_content)

            skill = self._create_mock_skill(tmpdir)
            runner = self._create_runner(skills=[skill])
            manifest = await runner._build_local_skill_manifest()

            expected_md5 = hashlib.md5(script_content).hexdigest()
            assert "scripts/test.py" in manifest
            assert manifest["scripts/test.py"] == expected_md5

    @pytest.mark.asyncio
    async def test_manifest_multiple_files(self):
        """manifest包含多个脚本文件"""
        with tempfile.TemporaryDirectory() as tmpdir:
            scripts_dir = os.path.join(tmpdir, "scripts")
            os.makedirs(scripts_dir)
            for i in range(3):
                with open(os.path.join(scripts_dir, f"script{i}.py"), "wb") as f:
                    f.write(f"# script {i}".encode())

            skill = self._create_mock_skill(tmpdir)
            runner = self._create_runner(skills=[skill])
            manifest = await runner._build_local_skill_manifest()

            assert len(manifest) == 3


class TestIncrementalSync:
    """_sync_skill_scripts_to_sandbox 增量同步测试"""

    def _create_runner(self, sandbox=None, skills=None) -> AgentTaskRunner:
        """创建带mock sandbox和技能服务的AgentTaskRunner"""
        with patch.object(AgentTaskRunner, '__init__', lambda self: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._sandbox = sandbox or MagicMock()
            runner._skill_tool = MagicMock()
            runner._skill_tool._skill_service = MagicMock()
            runner._skill_tool._skill_service.list_skills = AsyncMock(return_value=skills or [])
            return runner

    @pytest.mark.asyncio
    async def test_no_upload_when_manifests_match(self):
        """本地与远程manifest一致时不上传任何文件"""
        with tempfile.TemporaryDirectory() as tmpdir:
            scripts_dir = os.path.join(tmpdir, "scripts")
            os.makedirs(scripts_dir)
            script_content = b"print('test')"
            with open(os.path.join(scripts_dir, "test.py"), "wb") as f:
                f.write(script_content)

            expected_md5 = hashlib.md5(script_content).hexdigest()
            remote_manifest = {"scripts/test.py": expected_md5}

            sandbox = MagicMock()
            sandbox.read_file = AsyncMock(return_value=MagicMock(
                success=True, data={"content": json.dumps(remote_manifest)}
            ))
            sandbox.upload_file = AsyncMock(return_value=MagicMock(success=True))
            sandbox.write_file = AsyncMock()

            skill = MagicMock()
            skill.path = tmpdir
            runner = self._create_runner(sandbox=sandbox, skills=[skill])

            await runner._sync_skill_scripts_to_sandbox()

            sandbox.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_only_changed_files(self):
        """仅上传新增或md5不一致的文件"""
        with tempfile.TemporaryDirectory() as tmpdir:
            scripts_dir = os.path.join(tmpdir, "scripts")
            os.makedirs(scripts_dir)

            # 创建3个文件,其中1个已存在且md5一致,1个已存在但md5变化,1个新增
            file1_content = b"file1"
            file2_content = b"file2_changed"
            file3_content = b"file3_new"
            with open(os.path.join(scripts_dir, "file1.py"), "wb") as f:
                f.write(file1_content)
            with open(os.path.join(scripts_dir, "file2.py"), "wb") as f:
                f.write(file2_content)
            with open(os.path.join(scripts_dir, "file3.py"), "wb") as f:
                f.write(file3_content)

            remote_manifest = {
                "scripts/file1.py": hashlib.md5(file1_content).hexdigest(),
                "scripts/file2.py": hashlib.md5(b"file2_original").hexdigest(),  # md5不一致
            }

            sandbox = MagicMock()
            sandbox.read_file = AsyncMock(return_value=MagicMock(
                success=True, data={"content": json.dumps(remote_manifest)}
            ))
            sandbox.upload_file = AsyncMock(return_value=MagicMock(success=True))
            sandbox.write_file = AsyncMock()

            skill = MagicMock()
            skill.path = tmpdir
            runner = self._create_runner(sandbox=sandbox, skills=[skill])

            await runner._sync_skill_scripts_to_sandbox()

            # 应上传file2(变更)和file3(新增),file1跳过
            assert sandbox.upload_file.call_count == 2
            uploaded_paths = [call.kwargs.get("filepath", "") for call in sandbox.upload_file.call_args_list]
            assert any("file2.py" in p for p in uploaded_paths)
            assert any("file3.py" in p for p in uploaded_paths)
            assert not any("file1.py" in p for p in uploaded_paths)

    @pytest.mark.asyncio
    async def test_fallback_to_full_sync_when_no_manifest(self):
        """沙箱无manifest时全量上传(降级)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            scripts_dir = os.path.join(tmpdir, "scripts")
            os.makedirs(scripts_dir)
            for i in range(3):
                with open(os.path.join(scripts_dir, f"script{i}.py"), "wb") as f:
                    f.write(f"# {i}".encode())

            sandbox = MagicMock()
            sandbox.read_file = AsyncMock(return_value=MagicMock(success=False, data=None))
            sandbox.upload_file = AsyncMock(return_value=MagicMock(success=True))
            sandbox.write_file = AsyncMock()

            skill = MagicMock()
            skill.path = tmpdir
            runner = self._create_runner(sandbox=sandbox, skills=[skill])

            await runner._sync_skill_scripts_to_sandbox()

            # 无manifest时全量上传3个文件
            assert sandbox.upload_file.call_count == 3

    @pytest.mark.asyncio
    async def test_manifest_updated_after_sync(self):
        """同步完成后更新沙箱manifest"""
        with tempfile.TemporaryDirectory() as tmpdir:
            scripts_dir = os.path.join(tmpdir, "scripts")
            os.makedirs(scripts_dir)
            with open(os.path.join(scripts_dir, "test.py"), "wb") as f:
                f.write(b"test")

            sandbox = MagicMock()
            sandbox.read_file = AsyncMock(return_value=MagicMock(success=False, data=None))
            sandbox.upload_file = AsyncMock(return_value=MagicMock(success=True))
            sandbox.write_file = AsyncMock()

            skill = MagicMock()
            skill.path = tmpdir
            runner = self._create_runner(sandbox=sandbox, skills=[skill])

            await runner._sync_skill_scripts_to_sandbox()

            sandbox.write_file.assert_called_once()
            call_kwargs = sandbox.write_file.call_args.kwargs
            assert "content" in call_kwargs
            written_manifest = json.loads(call_kwargs["content"])
            assert "scripts/test.py" in written_manifest


class TestManifestPersistence:
    """manifest读写持久化测试"""

    def _create_runner(self, sandbox=None) -> AgentTaskRunner:
        with patch.object(AgentTaskRunner, '__init__', lambda self: None):
            runner = AgentTaskRunner.__new__(AgentTaskRunner)
            runner._sandbox = sandbox or MagicMock()
            return runner

    @pytest.mark.asyncio
    async def test_read_manifest_parses_json(self):
        """读取manifest正确解析JSON"""
        manifest_data = {"scripts/test.py": "abc123", "scripts/util.py": "def456"}
        sandbox = MagicMock()
        sandbox.read_file = AsyncMock(return_value=MagicMock(
            success=True, data={"content": json.dumps(manifest_data)}
        ))
        runner = self._create_runner(sandbox=sandbox)

        result = await runner._read_remote_skill_manifest()

        assert result == manifest_data

    @pytest.mark.asyncio
    async def test_read_manifest_returns_empty_on_failure(self):
        """读取失败时返回空字典(降级为全量同步)"""
        sandbox = MagicMock()
        sandbox.read_file = AsyncMock(side_effect=Exception("read error"))
        runner = self._create_runner(sandbox=sandbox)

        result = await runner._read_remote_skill_manifest()

        assert result == {}

    @pytest.mark.asyncio
    async def test_read_manifest_returns_empty_when_no_content(self):
        """manifest文件不存在时返回空字典"""
        sandbox = MagicMock()
        sandbox.read_file = AsyncMock(return_value=MagicMock(success=False, data=None))
        runner = self._create_runner(sandbox=sandbox)

        result = await runner._read_remote_skill_manifest()

        assert result == {}

    @pytest.mark.asyncio
    async def test_write_manifest_serializes_json(self):
        """写入manifest正确序列化为JSON"""
        sandbox = MagicMock()
        sandbox.write_file = AsyncMock()
        runner = self._create_runner(sandbox=sandbox)

        manifest = {"scripts/test.py": "abc123"}
        await runner._write_remote_skill_manifest(manifest)

        sandbox.write_file.assert_called_once()
        call_kwargs = sandbox.write_file.call_args.kwargs
        written_data = json.loads(call_kwargs["content"])
        assert written_data == manifest
