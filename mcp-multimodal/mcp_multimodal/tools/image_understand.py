#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : image_understand.py
图像理解工具 - 基于视觉模型对图片内容进行分析理解
"""
import base64
import json
import logging

from ..client import BigModelClient, BigModelAPIError
from ..utils.file_utils import load_file_bytes, is_url, SandboxFileError, FileLoadError

logger = logging.getLogger(__name__)


def register_image_understand(mcp, client: BigModelClient):
    @mcp.tool()
    async def vl_image_understand(
        image_source: str,
        prompt: str = "请详细描述这张图片的内容",
    ) -> str:
        """图片视觉理解工具，对图片内容进行分析、描述和问答。

            适用场景：
            - 用户上传图片并提问（看图回答问题）
            - 需要识别图片中的文字、物体、人物、场景
            - 需要对图片进行内容描述或问答

            不适用场景：
            - 验证自己生成的图表是否正确 → 无需调用，信任代码执行结果即可
            - 纯文本提取 → 使用ocr_extract工具

        Args:
            image_source: 图片来源。
                        - 优先使用URL地址（如附件的OSS地址/key字段）
                        - 支持upload://引用（通过上传端点获取）
                        - 禁止使用沙箱路径（如/home/ubuntu/...），MCP服务无法访问沙箱文件
                        - 若文件仅在沙箱中，须先用Shell工具上传：curl -F file=@<沙箱路径> http://mcp-multimodal:9100/upload，再使用返回的upload://引用
            prompt: 对图片的提问或分析要求，如"描述图片内容"、"图中人员是否佩戴安全帽"

        Returns:
            模型对图片的视觉分析结果文本
        """
        try:
            if is_url(image_source):
                result = await client.vl_chat(
                    prompt=prompt,
                    image_urls=[image_source],
                )
            else:
                file_bytes, _ = await load_file_bytes(image_source)
                b64 = base64.b64encode(file_bytes).decode("utf-8")
                result = await client.vl_chat(
                    prompt=prompt,
                    image_base64_list=[b64],
                )
            return result
        except SandboxFileError as e:
            logger.warning(f"沙箱路径访问: {e.sandbox_path}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        except FileLoadError as e:
            logger.error(f"图片加载失败: {e}")
            return json.dumps({"error": f"图片加载失败: {str(e)}"}, ensure_ascii=False)
        except BigModelAPIError as e:
            logger.error(f"图像理解API失败: {e}")
            return json.dumps({"error": f"图像理解失败: {str(e)}"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"图像理解异常: {e}")
            return json.dumps({"error": f"图像理解失败: {str(e)}"}, ensure_ascii=False)
