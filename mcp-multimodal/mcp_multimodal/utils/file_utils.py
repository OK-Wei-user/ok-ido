#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : file_utils.py
文件处理工具 - 统一支持URL下载、upload://引用、base64解码、本地读取
"""
import base64
import logging
import mimetypes
import os
from typing import Tuple

import httpx

from ..file_store import get_instance as _get_file_store

logger = logging.getLogger(__name__)

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
SUPPORTED_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}
SUPPORTED_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
SUPPORTED_PDF_EXTS = {".pdf"}
SUPPORTED_PPT_EXTS = {".pptx", ".ppt"}

SANDBOX_PATH_PREFIXES = ("/home/ubuntu/", "/tmp/", "/root/")
MCP_UPLOAD_ENDPOINT = "http://mcp-multimodal:9100/upload"


class FileLoadError(Exception):
    """文件加载异常"""


class SandboxFileError(FileLoadError):
    """沙箱文件访问异常 - MCP服务无法直接访问沙箱路径"""

    def __init__(self, sandbox_path: str) -> None:
        self.sandbox_path = sandbox_path
        msg = (
            f"无法访问沙箱文件: {sandbox_path}。"
            f"MCP服务运行在独立容器中，无法直接读取沙箱路径。"
            f"请先使用Shell工具执行以下命令上传文件：\n"
            f"curl -F file=@{sandbox_path} {MCP_UPLOAD_ENDPOINT}\n"
            f"然后将返回的upload://引用作为参数值重新调用此工具。"
        )
        super().__init__(msg)


def is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def is_upload_ref(source: str) -> bool:
    return source.startswith("upload://")


def is_base64_data(source: str) -> bool:
    return source.startswith("data:")


def is_sandbox_path(source: str) -> bool:
    """检测是否为沙箱环境路径"""
    return any(source.startswith(p) for p in SANDBOX_PATH_PREFIXES)


async def download_bytes(url: str) -> Tuple[bytes, str]:
    """从URL下载文件内容"""
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            cd = resp.headers.get("content-disposition", "")
            filename = ""
            if "filename=" in cd:
                filename = cd.split("filename=")[-1].strip('" ')
            if not filename:
                filename = os.path.basename(url.split("?")[0]) or "download"
            return resp.content, filename
    except httpx.HTTPStatusError as e:
        raise FileLoadError(f"下载失败 HTTP {e.response.status_code}: {url}") from e
    except httpx.RequestError as e:
        raise FileLoadError(f"下载请求异常: {url}, {type(e).__name__}: {e}") from e


def read_local_bytes(filepath: str) -> Tuple[bytes, str]:
    """读取本地文件内容"""
    if is_sandbox_path(filepath):
        raise SandboxFileError(filepath)
    if not os.path.isfile(filepath):
        raise FileLoadError(f"本地文件不存在: {filepath}")
    try:
        with open(filepath, "rb") as f:
            content = f.read()
        return content, os.path.basename(filepath)
    except OSError as e:
        raise FileLoadError(f"读取本地文件失败: {filepath}, {e}") from e


def decode_base64_data(data_uri: str) -> Tuple[bytes, str]:
    """解码base64数据URI"""
    try:
        if "," in data_uri:
            header, encoded = data_uri.split(",", 1)
            mime_part = header.split(";")[0].split(":")[1] if ":" in header else ""
            ext = mimetypes.guess_extension(mime_part) or ".bin"
            filename = f"base64_input{ext}"
        else:
            encoded = data_uri
            filename = "base64_input.bin"
        return base64.b64decode(encoded), filename
    except Exception as e:
        raise FileLoadError(f"base64解码失败: {e}") from e


def resolve_upload_ref(source: str) -> Tuple[bytes, str]:
    """解析upload://引用为文件内容"""
    store = _get_file_store()
    if store is None:
        raise FileLoadError("文件存储服务未就绪")
    stored = store.resolve(source)
    if stored is None:
        raise FileLoadError(f"上传文件引用无效或已过期: {source}")
    return stored.content, stored.filename


async def load_file_bytes(source: str) -> Tuple[bytes, str]:
    """统一文件加载入口，按协议自动选择加载方式

    支持的source格式:
    - upload://引用 → 从临时存储解析
    - data: URI → base64解码
    - http(s):// URL → 异步下载
    - 本地路径 → 直接读取（沙箱路径会抛出SandboxFileError）
    """
    if is_upload_ref(source):
        return resolve_upload_ref(source)
    if is_base64_data(source):
        return decode_base64_data(source)
    if is_url(source):
        return await download_bytes(source)
    return read_local_bytes(source)


def detect_file_type(filepath: str) -> str:
    """根据扩展名判断文件类型"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in SUPPORTED_IMAGE_EXTS:
        return "image"
    if ext in SUPPORTED_AUDIO_EXTS:
        return "audio"
    if ext in SUPPORTED_VIDEO_EXTS:
        return "video"
    if ext in SUPPORTED_PDF_EXTS:
        return "pdf"
    if ext in SUPPORTED_PPT_EXTS:
        return "ppt"
    return "unknown"


def guess_mime_type(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"
