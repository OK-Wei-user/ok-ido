#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : speech2text.py
语音转文本工具 - 基于BigModel ASR服务将音频转换为文字
"""
import json
import logging

from ..client import BigModelClient, BigModelAPIError
from ..utils.file_utils import load_file_bytes, SandboxFileError, FileLoadError

logger = logging.getLogger(__name__)


def register_speech2text(mcp, client: BigModelClient):
    @mcp.tool()
    async def asr_speech2text(
        audio_source: str,
    ) -> str:
        """语音转文本工具(ASR)，将音频文件转换为文字。

            适用场景：
            - 用户上传音频文件并要求转写为文字
            - 需要识别录音、语音消息中的内容

        Args:
            audio_source: 音频来源。
                        - 优先使用URL地址（如附件的OSS地址/key字段）
                        - 支持upload://引用（通过上传端点获取）
                        - 禁止使用沙箱路径（如/home/ubuntu/...），MCP服务无法访问沙箱文件
                        - 若文件仅在沙箱中，须先用Shell工具上传：curl -F file=@<沙箱路径> http://mcp-multimodal:9100/upload，再使用返回的upload://引用
                        - 支持MP3/WAV/M4A/FLAC/OGG/AAC格式

        Returns:
            音频中识别出的文字内容
        """
        try:
            file_bytes, filename = await load_file_bytes(audio_source)
            result = await client.asr_transcribe(
                audio_bytes=file_bytes,
                filename=filename,
            )
            return result
        except SandboxFileError as e:
            logger.warning(f"沙箱路径访问: {e.sandbox_path}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        except FileLoadError as e:
            logger.error(f"音频文件加载失败: {e}")
            return json.dumps({"error": f"音频文件加载失败: {str(e)}"}, ensure_ascii=False)
        except BigModelAPIError as e:
            logger.error(f"ASR API失败: {e}")
            return json.dumps({"error": f"语音转文本失败: {str(e)}"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"语音转文本异常: {e}")
            return json.dumps({"error": f"语音转文本失败: {str(e)}"}, ensure_ascii=False)
