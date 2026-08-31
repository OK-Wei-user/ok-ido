#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : client.py
BigModel API客户端 - 封装视觉理解/OCR/ASR/图像生成等API调用
"""
import asyncio
import base64
import logging
from typing import Any, Dict, List, Optional

import httpx

from .config import MultimodalConfig

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_DELAY_SECONDS = 1.0


class BigModelAPIError(Exception):
    """BigModel API调用异常"""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class BigModelClient:
    """智谱BigModel API客户端"""

    def __init__(self, config: MultimodalConfig) -> None:
        self._config = config
        self._headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }

    @property
    def config(self) -> MultimodalConfig:
        return self._config

    @property
    def base_url(self) -> str:
        return self._config.base_url.rstrip("/")

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def images_url(self) -> str:
        return f"{self.base_url}/images/generations"

    @property
    def ocr_url(self) -> str:
        return f"{self.base_url}/files/ocr"

    async def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        last_error = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(url, headers=self._headers, json=payload)
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as e:
                body = e.response.text[:500] if e.response.text else "(empty)"
                raise BigModelAPIError(
                    f"API调用失败 HTTP {e.response.status_code}: {body}",
                    status_code=e.response.status_code,
                ) from e
            except httpx.RequestError as e:
                last_error = e
                if attempt < _MAX_RETRIES:
                    logger.warning(f"API请求异常(第{attempt + 1}次重试): {type(e).__name__}: {e}")
                    await asyncio.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
        raise BigModelAPIError(
            f"API请求异常(已重试{_MAX_RETRIES}次): {type(last_error).__name__}: {last_error}"
        ) from last_error

    async def _post_bytes(
        self, url: str, content: bytes, filename: str, form_fields: Dict[str, str]
    ) -> Dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        last_error = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(
                        url,
                        headers=headers,
                        data=form_fields,
                        files={"file": (filename, content, "application/octet-stream")},
                    )
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as e:
                body = e.response.text[:500] if e.response.text else "(empty)"
                raise BigModelAPIError(
                    f"API调用失败 HTTP {e.response.status_code}: {body}",
                    status_code=e.response.status_code,
                ) from e
            except httpx.RequestError as e:
                last_error = e
                if attempt < _MAX_RETRIES:
                    logger.warning(f"API请求异常(第{attempt + 1}次重试): {type(e).__name__}: {e}")
                    await asyncio.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
        raise BigModelAPIError(
            f"API请求异常(已重试{_MAX_RETRIES}次): {type(last_error).__name__}: {last_error}"
        ) from last_error

    @staticmethod
    def build_image_content(
        image_url: Optional[str] = None, image_base64: Optional[str] = None
    ) -> Dict[str, Any]:
        if image_url:
            return {"type": "image_url", "image_url": {"url": image_url}}
        if image_base64:
            return {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_base64}"},
            }
        raise ValueError("image_url和image_base64至少提供一个")

    async def vl_chat(
        self,
        prompt: str,
        image_urls: Optional[List[str]] = None,
        image_base64_list: Optional[List[str]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """视觉理解对话"""
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        if image_urls:
            for url in image_urls:
                content.append(self.build_image_content(image_url=url))
        if image_base64_list:
            for b64 in image_base64_list:
                content.append(self.build_image_content(image_base64=b64))

        payload = {
            "model": model or self._config.vl_model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens or self._config.max_tokens,
        }
        result = await self._post_json(self.chat_url, payload)

        choices = result.get("choices", [])
        if not choices:
            raise BigModelAPIError("视觉理解API返回空结果，模型未生成内容")

        message_content = choices[0].get("message", {}).get("content", "")
        if not message_content or not message_content.strip():
            raise BigModelAPIError("视觉理解API返回空内容，模型可能无法处理该图片")

        return message_content

    async def ocr_extract(
        self,
        file_bytes: bytes,
        filename: str,
        language_type: str = "CHN_ENG",
        probability: bool = False,
    ) -> str:
        """OCR文字提取"""
        form_fields = {
            "tool_type": "hand_write",
            "language_type": language_type,
            "probability": str(probability).lower(),
        }
        result = await self._post_bytes(self.ocr_url, file_bytes, filename, form_fields)
        words_result = result.get("words_result", [])
        if not words_result:
            return "OCR未识别到文字内容"
        lines = [item.get("words", "") for item in words_result]
        return "\n".join(lines)

    async def asr_transcribe(
        self,
        audio_bytes: bytes,
        filename: str,
        model: Optional[str] = None,
    ) -> str:
        """语音转文本"""
        model_name = model or self._config.asr_model
        payload = {
            "model": model_name,
            "file": base64.b64encode(audio_bytes).decode("utf-8"),
        }
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/asr"
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            return data.get("text", "")
        except httpx.HTTPStatusError as e:
            raise BigModelAPIError(
                f"ASR API调用失败 HTTP {e.response.status_code}",
                status_code=e.response.status_code,
            ) from e
        except httpx.RequestError as e:
            raise BigModelAPIError(
                f"ASR API请求异常: {type(e).__name__}: {e}"
            ) from e

    async def image_generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        size: str = "1024x1024",
    ) -> str:
        """图像生成"""
        payload = {
            "model": model or self._config.image_model,
            "prompt": prompt,
            "size": size,
        }
        result = await self._post_json(self.images_url, payload)
        data_list = result.get("data", [])
        if not data_list:
            raise BigModelAPIError("图像生成失败，API未返回结果")
        return data_list[0].get("url", "")
