#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : ocr_extract.py
OCR文字提取工具 - 基于BigModel OCR服务提取图片中的文字
"""
import json
import logging

from ..client import BigModelClient, BigModelAPIError
from ..utils.file_utils import load_file_bytes, is_url, SandboxFileError, FileLoadError

logger = logging.getLogger(__name__)


def register_ocr_extract(mcp, client: BigModelClient):
    @mcp.tool()
    async def ocr_extract(
        image_source: str,
        language_type: str = "CHN_ENG",
    ) -> str:
        """OCR文字提取工具，从图片中识别和提取文字内容。

            适用场景：
            - 用户上传图片并要求提取其中的文字
            - 需要识别文档扫描件、截图中的文字内容

        Args:
            image_source: 图片来源。
                        - 优先使用URL地址（如附件的OSS地址/key字段）
                        - 支持upload://引用（通过上传端点获取）
                        - 禁止使用沙箱路径（如/home/ubuntu/...），MCP服务无法访问沙箱文件
                        - 若文件仅在沙箱中，须先用Shell工具上传：curl -F file=@<沙箱路径> http://mcp-multimodal:9100/upload，再使用返回的upload://引用
            language_type: 识别语言类型，默认中英混合(CHN_ENG)，可选: ENG/JAP/KOR/FRE/SPA/POR/GER/ITA/RUS等

        Returns:
            图片中识别出的文字内容
        """
        try:
            file_bytes, filename = await load_file_bytes(image_source)
            result = await client.ocr_extract(
                file_bytes=file_bytes,
                filename=filename,
                language_type=language_type,
            )
            return result
        except SandboxFileError as e:
            logger.warning(f"沙箱路径访问: {e.sandbox_path}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        except FileLoadError as e:
            logger.error(f"OCR图片加载失败: {e}")
            return json.dumps({"error": f"图片加载失败: {str(e)}"}, ensure_ascii=False)
        except BigModelAPIError as e:
            logger.error(f"OCR API失败: {e}")
            return json.dumps({"error": f"OCR文字提取失败: {str(e)}"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"OCR文字提取异常: {e}")
            return json.dumps({"error": f"OCR文字提取失败: {str(e)}"}, ensure_ascii=False)
