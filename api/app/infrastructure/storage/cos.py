#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/5/14 10:53
@File    : cos.py
"""
import asyncio
import json
import logging
import re
from functools import lru_cache
from io import BytesIO
from typing import Optional, Dict, Any

import aiohttp

from core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# OSS 上传瞬态错误重试配置(指数退避)
# 设计: 仅重试可恢复错误(5xx服务端错误/网络异常),非瞬态错误(4xx业务失败)立即抛出,
# 避免无效重试。与 AgentTaskRunner._sync_file_to_storage 外层重试+PENDING降级互补:
# HTTP层处理瞬态抖动(无需重新下载沙箱文件),外层处理持续失败。
_OSS_UPLOAD_MAX_RETRIES = 3  # 最大重试次数(不含首次)
_OSS_RETRY_BASE_DELAY = 0.5  # 指数退避基数(秒): 0.5, 1.0, 2.0


def _is_transient_oss_error(exc: Optional[Exception] = None, status: Optional[int] = None) -> bool:
    """判断 OSS 错误是否为可重试的瞬态错误

    可重试: 5xx 服务端错误 / aiohttp 网络连接异常 / 超时
    不重试: 4xx 客户端错误(业务失败,重试无意义)
    """
    if status is not None and 500 <= status < 600:
        return True
    if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError)):
        return True
    return False


class Cos:
    """公司OSS对象存储（兼容原COS接口命名）"""

    def __init__(self):
        self._settings: Settings = get_settings()
        self._client: Optional[aiohttp.ClientSession] = None

    async def init(self) -> None:
        if self._client is not None:
            logger.warning("OSS对象存储已初始化，无需重复操作")
            return

        try:
            self._client = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300)
            )
            logger.info("OSS对象存储初始化成功")
        except Exception as e:
            logger.error(f"OSS对象存储初始化失败: {str(e)}")
            raise

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("关闭OSS对象存储成功")

        get_cos.cache_clear()

    async def upload_file(
            self,
            file_data: bytes,
            filename: str,
            bucket_name: Optional[str] = None,
            dir_path: Optional[str] = None,
            is_covered: bool = False,
            is_random: bool = True,
            is_frame: bool = False,
    ) -> Dict[str, Any]:
        if self._client is None:
            raise RuntimeError("OSS对象存储未初始化，请调用init()完成初始化")

        data = aiohttp.FormData()
        data.add_field(
            "file", file_data, filename=filename,
            content_type="application/octet-stream",
        )
        data.add_field("bucketName", bucket_name or self._settings.oss_bucket or "")
        if dir_path:
            data.add_field("dir", dir_path)
        data.add_field("isCovered", str(is_covered).lower())
        data.add_field("isRandom", str(is_random).lower())
        data.add_field("isFrame", str(is_frame).lower())

        # 瞬态错误重试循环(指数退避): 仅重试 5xx / 网络异常,4xx 业务失败立即抛出
        last_exc: Optional[Exception] = None
        for attempt in range(_OSS_UPLOAD_MAX_RETRIES + 1):
            try:
                async with self._client.post(
                    url=self._settings.oss_base_url, data=data
                ) as response:
                    response_text = await response.text()

                    if response.status != 200:
                        # 瞬态 5xx 错误: 重试(未耗尽时)
                        if (_is_transient_oss_error(status=response.status)
                                and attempt < _OSS_UPLOAD_MAX_RETRIES):
                            wait = _OSS_RETRY_BASE_DELAY * (2 ** attempt)
                            logger.warning(
                                f"OSS上传瞬态失败(status={response.status}),"
                                f"{wait}s后重试(第{attempt + 1}次): {filename}"
                            )
                            last_exc = RuntimeError(
                                f"OSS上传失败: {response.status}, 响应: {response_text}"
                            )
                            await asyncio.sleep(wait)
                            continue
                        raise RuntimeError(
                            f"OSS上传失败: {response.status}, 响应: {response_text}"
                        )

                    result = json.loads(response_text)
                    if result.get("code") != 200:
                        # 业务失败(code!=200): 非瞬态,立即抛出不重试
                        raise RuntimeError(
                            f"OSS上传业务失败: {result.get('msg', '未知错误')}"
                        )

                    logger.info(f"文件上传成功: {filename}")

                    upload_data = result["data"]
                    url = upload_data.get("url", "")
                    if url:
                        url = self._fix_url(url)
                        upload_data["url"] = url

                    return upload_data
            except aiohttp.ClientError as e:
                # 网络异常: 重试(未耗尽时)
                if attempt < _OSS_UPLOAD_MAX_RETRIES:
                    wait = _OSS_RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"OSS上传网络异常,{wait}s后重试(第{attempt + 1}次): "
                        f"{filename}, error: {e}"
                    )
                    last_exc = e
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"上传文件[{filename}]失败(网络异常,已重试耗尽): {str(e)}")
                raise
            except RuntimeError:
                # 业务失败/非瞬态HTTP错误: 已在上方 raise,直接向上传播不重试
                raise
            except Exception as e:
                # 其他未预期异常: 不重试,记录并抛出
                logger.error(f"上传文件[{filename}]失败: {str(e)}")
                raise

        # 重试耗尽: 抛出最后一次瞬态错误
        logger.error(f"上传文件[{filename}]失败(瞬态错误已重试{_OSS_UPLOAD_MAX_RETRIES}次耗尽)")
        raise last_exc if last_exc is not None else RuntimeError("OSS上传失败: 重试耗尽")

    async def download_file(self, file_url: str) -> BytesIO:
        if self._client is None:
            raise RuntimeError("OSS对象存储未初始化，请调用init()完成初始化")

        async with self._client.get(file_url) as response:
            if response.status != 200:
                raise RuntimeError(f"OSS下载失败: {response.status}, URL: {file_url}")
            file_bytes = await response.read()
            return BytesIO(file_bytes)

    @staticmethod
    def _fix_url(url: str) -> str:
        if "https://" in url and url.count("https://") > 1:
            last_pos = url.rfind("https://")
            if last_pos > 0:
                url = url[last_pos:]

        if "http://" in url and url.count("http://") > 1:
            last_pos = url.rfind("http://")
            if last_pos > 0:
                url = url[last_pos:]

        url = re.sub(r"(?<!:)/+", "/", url)
        url = re.sub(r"https:/([^/])", r"https://\1", url)
        url = re.sub(r"http:/([^/])", r"http://\1", url)
        return url


@lru_cache()
def get_cos() -> Cos:
    return Cos()
