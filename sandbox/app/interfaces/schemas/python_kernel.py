#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : python_kernel.py
Python 内核预热服务请求/响应 schema
"""
from typing import Optional

from pydantic import BaseModel, Field


class PythonExecRequest(BaseModel):
    """在预加载内核中执行 Python 脚本请求"""
    script_path: str = Field(..., description="Python 脚本绝对路径")
    timeout: Optional[int] = Field(
        default=None,
        description="执行超时(秒),默认300秒,超时自动终止进程",
    )


class PythonExecResult(BaseModel):
    """Python 脚本执行结果"""
    returncode: int = Field(..., description="进程退出码,0 表示成功")
    stdout: str = Field(default="", description="标准输出内容")
    stderr: str = Field(default="", description="标准错误内容")
