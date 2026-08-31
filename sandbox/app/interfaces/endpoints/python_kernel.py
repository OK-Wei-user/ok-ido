#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : python_kernel.py
Python 内核预热服务 API - 通过 fork 预加载进程执行 Python 脚本
"""
from fastapi import APIRouter, Depends

from app.interfaces.schemas.base import Response
from app.interfaces.schemas.python_kernel import PythonExecRequest, PythonExecResult
from app.interfaces.service_dependencies import get_python_kernel_service
from app.services.python_kernel import PythonKernelService

router = APIRouter(prefix="/python", tags=["Python内核模块"])


@router.post(
    path="/exec-script",
    response_model=Response[PythonExecResult],
)
async def exec_script(
    request: PythonExecRequest,
    kernel_service: PythonKernelService = Depends(get_python_kernel_service),
) -> Response[PythonExecResult]:
    """在预加载内核中执行 Python 脚本

    通过 fork 预加载了 pandas/numpy/openpyxl 的主进程,
    子进程继承 sys.modules 缓存,避免重复 import 开销。

    适用场景:
    - 数据分析脚本(pandas/numpy 密集)
    - 需要多次执行 Python 脚本的批量任务
    - 对启动时间敏感的短脚本

    降级策略: 内核不可用时自动回退到直接 subprocess
    """
    returncode, stdout, stderr = await kernel_service.exec_script(
        script_path=request.script_path,
        timeout=request.timeout,
    )

    return Response.success(
        msg=f"脚本执行完成,退出码: {returncode}",
        data=PythonExecResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )
