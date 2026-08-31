#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : pdf_parse.py
PDF图文解析工具 - 基于视觉模型对PDF文档进行逐页图文解析
"""
import base64
import json
import logging
from io import BytesIO

from ..client import BigModelClient, BigModelAPIError
from ..utils.file_utils import load_file_bytes, SandboxFileError, FileLoadError

logger = logging.getLogger(__name__)

_MAX_PAGES = 20


def register_pdf_parse(mcp, client: BigModelClient):
    @mcp.tool()
    async def pdf_multimodal_parse(
        pdf_source: str,
        prompt: str = "请解析这个PDF文档的内容，提取文字和图表信息",
    ) -> str:
        """PDF图文解析工具，对PDF文档进行逐页图文解析。

            适用场景：
            - 用户上传PDF文件并要求提取内容
            - 需要解析PDF中的文字、图表、表格

        Args:
            pdf_source: PDF文件来源。
                        - 优先使用URL地址（如附件的OSS地址/key字段）
                        - 支持upload://引用（通过上传端点获取）
                        - 禁止使用沙箱路径（如/home/ubuntu/...），MCP服务无法访问沙箱文件
                        - 若文件仅在沙箱中，须先用Shell工具上传：curl -F file=@<沙箱路径> http://mcp-multimodal:9100/upload，再使用返回的upload://引用
            prompt: 对PDF内容的解析要求，默认为提取文字和图表信息

        Returns:
            PDF文档的逐页解析结果文本
        """
        try:
            file_bytes, _ = await load_file_bytes(pdf_source)
        except SandboxFileError as e:
            logger.warning(f"沙箱路径访问: {e.sandbox_path}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        except FileLoadError as e:
            logger.error(f"PDF文件加载失败: {e}")
            return json.dumps({"error": f"PDF文件加载失败: {str(e)}"}, ensure_ascii=False)

        try:
            page_images_b64 = _pdf_to_images_base64(file_bytes)
            if not page_images_b64:
                return json.dumps({"error": "PDF转换为图片失败，可能文件损坏或为空"}, ensure_ascii=False)

            pages_to_process = page_images_b64[:_MAX_PAGES]
            all_results = []
            for i, img_b64 in enumerate(pages_to_process):
                page_prompt = f"这是PDF文档第{i + 1}页的内容。{prompt}"
                result = await client.vl_chat(
                    prompt=page_prompt,
                    image_base64_list=[img_b64],
                )
                all_results.append(f"=== 第{i + 1}页 ===\n{result}")

            if len(page_images_b64) > _MAX_PAGES:
                all_results.append(f"\n[注: 文档共{len(page_images_b64)}页，仅解析前{_MAX_PAGES}页]")

            return "\n\n".join(all_results)
        except BigModelAPIError as e:
            logger.error(f"PDF解析API失败: {e}")
            return json.dumps({"error": f"PDF解析失败: {str(e)}"}, ensure_ascii=False)
        except Exception as e:
            logger.error(f"PDF解析失败: {e}")
            return json.dumps({"error": f"PDF解析失败: {str(e)}"}, ensure_ascii=False)


def _pdf_to_images_base64(pdf_bytes: bytes) -> list:
    """将PDF每页渲染为PNG图片的base64编码列表"""
    try:
        from pdf2image import convert_from_bytes

        pil_images = convert_from_bytes(pdf_bytes, dpi=200)
        images_b64 = []
        for pil_img in pil_images:
            buf = BytesIO()
            pil_img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            images_b64.append(b64)
        return images_b64
    except ImportError:
        logger.warning("pdf2image未安装，尝试使用pypdf提取")
        return _pdf_fallback_extract(pdf_bytes)
    except Exception as e:
        logger.error(f"PDF转图片异常: {e}")
        return []


def _pdf_fallback_extract(pdf_bytes: bytes) -> list:
    """pdf2image不可用时的降级方案：使用pypdf提取文本并渲染为图片"""
    try:
        from pypdf import PdfReader
        from PIL import Image, ImageDraw, ImageFont

        reader = PdfReader(BytesIO(pdf_bytes))
        images_b64 = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                img = Image.new("RGB", (1200, 1600), "white")
                draw = ImageDraw.Draw(img)
                try:
                    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
                except Exception:
                    font = ImageFont.load_default()
                draw.text((20, 20), text[:3000], fill="black", font=font)
                buf = BytesIO()
                img.save(buf, format="PNG")
                images_b64.append(base64.b64encode(buf.getvalue()).decode("utf-8"))
        return images_b64
    except Exception as e:
        logger.error(f"PDF降级提取异常: {e}")
        return []
