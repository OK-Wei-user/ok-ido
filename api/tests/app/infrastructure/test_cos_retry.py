#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_cos_retry.py
OSS 上传瞬态错误重试单元测试 - 验证 Cos.upload_file 的指数退避重试与错误分类

测试覆盖:
- 502 瞬态错误 → 重试后成功
- 持续 502 → 重试耗尽抛出
- 4xx 非瞬态错误 → 立即抛出不重试
- 网络异常(aiohttp.ClientError) → 重试后成功
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from app.infrastructure.storage.cos import Cos, _is_transient_oss_error


# ========== 辅助函数 ==========

def _mk_response(status: int, text: str = "", json_data: dict | None = None):
    """构造 mock aiohttp 响应(async context manager)"""
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=text)
    if json_data is not None:
        import json as _json
        response.text = AsyncMock(return_value=_json.dumps(json_data))
    return response


def _mk_post_mock(responses):
    """构造 mock post 方法,按顺序返回响应列表

    responses: list of (response_or_exception)
    - 若为 MagicMock: 作为 async context manager 返回
    - 若为 Exception: 进入 async context 时抛出
    """
    call_count = {"n": 0}

    class _CM:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            if isinstance(self._resp, Exception):
                raise self._resp
            return self._resp

        async def __aexit__(self, *args):
            return False

    def _post(*args, **kwargs):
        idx = call_count["n"]
        call_count["n"] += 1
        if idx < len(responses):
            return _CM(responses[idx])
        # 超出列表则重复最后一个
        return _CM(responses[-1])

    return _post, call_count


def _build_cos_with_client(post_fn):
    """构造已初始化的 Cos 实例,mock _client.post"""
    cos = Cos.__new__(Cos)
    cos._settings = MagicMock()
    cos._settings.oss_bucket = "test-bucket"
    cos._settings.oss_base_url = "http://oss.test/upload"
    cos._client = MagicMock()
    cos._client.post = post_fn
    return cos


# ========== _is_transient_oss_error 分类测试 ==========

class TestIsTransientOssError:
    """错误分类逻辑测试"""

    def test_5xx_is_transient(self):
        assert _is_transient_oss_error(status=500) is True
        assert _is_transient_oss_error(status=502) is True
        assert _is_transient_oss_error(status=503) is True

    def test_4xx_is_not_transient(self):
        assert _is_transient_oss_error(status=400) is False
        assert _is_transient_oss_error(status=404) is False
        assert _is_transient_oss_error(status=422) is False

    def test_network_error_is_transient(self):
        assert _is_transient_oss_error(exc=aiohttp.ClientError("conn")) is True
        assert _is_transient_oss_error(exc=asyncio.TimeoutError()) is True

    def test_runtime_error_is_not_transient(self):
        assert _is_transient_oss_error(exc=RuntimeError("biz fail")) is False


# ========== upload_file 重试行为测试 ==========

class TestUploadFileRetry:
    """upload_file 重试循环测试(加速: 重试间隔设为 0)"""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        """禁用真实 sleep,加速测试"""
        monkeypatch.setattr(
            "app.infrastructure.storage.cos.asyncio.sleep", AsyncMock()
        )

    @pytest.mark.asyncio
    async def test_502_transient_then_success(self):
        """502 瞬态错误 → 重试后成功"""
        success_resp = _mk_response(200, json_data={
            "code": 200, "data": {"url": "https://oss.test/file.md"}
        })
        post_fn, call_count = _mk_post_mock([
            _mk_response(502, "Bad Gateway"),
            success_resp,
        ])
        cos = _build_cos_with_client(post_fn)

        result = await cos.upload_file(b"data", "test.md")
        assert result["url"] == "https://oss.test/file.md"
        assert call_count["n"] == 2  # 首次失败 + 重试成功

    @pytest.mark.asyncio
    async def test_persistent_502_exhausts_retries(self):
        """持续 502 → 重试耗尽抛出"""
        post_fn, call_count = _mk_post_mock([_mk_response(502, "Bad Gateway")])
        cos = _build_cos_with_client(post_fn)

        with pytest.raises(RuntimeError, match="502"):
            await cos.upload_file(b"data", "test.md")
        # 首次 + 3 次重试 = 4 次调用
        assert call_count["n"] == 4

    @pytest.mark.asyncio
    async def test_4xx_not_retried(self):
        """4xx 非瞬态错误 → 立即抛出不重试"""
        post_fn, call_count = _mk_post_mock([_mk_response(400, "Bad Request")])
        cos = _build_cos_with_client(post_fn)

        with pytest.raises(RuntimeError, match="400"):
            await cos.upload_file(b"data", "test.md")
        assert call_count["n"] == 1  # 仅 1 次,未重试

    @pytest.mark.asyncio
    async def test_business_failure_not_retried(self):
        """业务失败(code!=200) → 立即抛出不重试"""
        post_fn, call_count = _mk_post_mock([
            _mk_response(200, json_data={"code": 500, "msg": "业务错误"})
        ])
        cos = _build_cos_with_client(post_fn)

        with pytest.raises(RuntimeError, match="业务失败"):
            await cos.upload_file(b"data", "test.md")
        assert call_count["n"] == 1  # 仅 1 次,未重试

    @pytest.mark.asyncio
    async def test_network_error_then_success(self):
        """网络异常(aiohttp.ClientError) → 重试后成功"""
        success_resp = _mk_response(200, json_data={
            "code": 200, "data": {"url": "https://oss.test/file.md"}
        })
        post_fn, call_count = _mk_post_mock([
            aiohttp.ClientError("connection refused"),
            success_resp,
        ])
        cos = _build_cos_with_client(post_fn)

        result = await cos.upload_file(b"data", "test.md")
        assert result["url"] == "https://oss.test/file.md"
        assert call_count["n"] == 2  # 首次网络异常 + 重试成功
