#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : server.py
多模态MCP服务入口 - FastMCP streamable-http + 文件上传端点
"""
import logging
import os

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import AppConfig
from .client import BigModelClient
from .file_store import FileStore, set_instance as set_file_store_instance
from .utils.screenshot import ScreenshotCapture
from .tools import ALL_TOOL_REGISTRARS

logger = logging.getLogger(__name__)

_app_state: dict = {}


def create_mcp_server(config: AppConfig) -> FastMCP:
    if not config.multimodal.api_key:
        raise ValueError("MULTIMODAL_API_KEY未配置，请设置环境变量或在config.yaml中配置")

    mcp = FastMCP(
        name="mcp-multimodal",
        host=config.server.host,
        port=config.server.port,
        streamable_http_path="/mcp",
        stateless_http=True,
    )

    client = BigModelClient(config.multimodal)
    screenshot = ScreenshotCapture(config.screenshot)
    file_store = FileStore(config.file_store)

    _app_state["client"] = client
    _app_state["screenshot"] = screenshot
    _app_state["file_store"] = file_store
    set_file_store_instance(file_store)

    for registrar in ALL_TOOL_REGISTRARS:
        if registrar.__name__ == "register_browser_image":
            registrar(mcp, client, screenshot)
        else:
            registrar(mcp, client)
        logger.info(f"注册MCP工具: {registrar.__name__}")

    logger.info(f"多模态MCP服务初始化完成，共注册{len(ALL_TOOL_REGISTRARS)}个工具")
    return mcp


async def upload_endpoint(request: Request) -> JSONResponse:
    """文件上传端点 - Agent通过此端点上传沙箱文件，获取upload://引用"""
    file_store: FileStore = _app_state.get("file_store")
    if not file_store:
        return JSONResponse({"error": "文件存储服务未就绪"}, status_code=503)

    try:
        form = await request.form()
        upload_file = form.get("file")
        if not upload_file:
            return JSONResponse({"error": "缺少file字段"}, status_code=400)

        content = await upload_file.read()
        filename = upload_file.filename or "upload"
        mime_type = upload_file.content_type or "application/octet-stream"

        file_id = file_store.put(filename=filename, content=content, mime_type=mime_type)
        return JSONResponse({
            "file_id": file_id,
            "upload_ref": f"upload://{file_id}",
            "filename": filename,
            "size": len(content),
        })
    except Exception as e:
        logger.error(f"文件上传失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def download_endpoint(request: Request) -> Response:
    """文件下载端点 - 通过file_id下载已上传的文件"""
    file_store: FileStore = _app_state.get("file_store")
    if not file_store:
        return JSONResponse({"error": "文件存储服务未就绪"}, status_code=503)

    file_id = request.path_params["file_id"]
    stored = file_store.get(file_id)
    if not stored:
        return JSONResponse({"error": "文件不存在或已过期"}, status_code=404)

    return Response(
        content=stored.content,
        media_type=stored.mime_type,
        headers={"Content-Disposition": f'inline; filename="{stored.filename}"'},
    )


async def health_endpoint(request: Request) -> JSONResponse:
    """健康检查端点"""
    screenshot: ScreenshotCapture = _app_state.get("screenshot")
    return JSONResponse({
        "status": "ok",
        "screenshot": screenshot.available if screenshot else False,
    })


def create_app(config: AppConfig = None):
    """创建MCP应用，将自定义路由注入MCP streamable_http_app"""
    if config is None:
        config_path = os.environ.get("MCP_CONFIG_PATH", "config.yaml")
        config = AppConfig.load(config_path)

    mcp = create_mcp_server(config)
    app = mcp.streamable_http_app()

    app.routes.insert(0, Route("/health", health_endpoint, methods=["GET"]))
    app.routes.insert(0, Route("/upload", upload_endpoint, methods=["POST"]))
    app.routes.insert(0, Route("/files/{file_id:path}", download_endpoint, methods=["GET"]))

    original_lifespan = app.router.lifespan_context if hasattr(app.router, "lifespan_context") else None

    from contextlib import asynccontextmanager
    from starlette.applications import Starlette

    @asynccontextmanager
    async def combined_lifespan(app):
        file_store: FileStore = _app_state.get("file_store")
        screenshot: ScreenshotCapture = _app_state.get("screenshot")

        if file_store:
            await file_store.start()
        if screenshot:
            try:
                await screenshot.start()
            except Exception as e:
                logger.warning(f"Playwright截图服务启动失败(网页视觉分析工具不可用): {e}")

        if original_lifespan:
            async with original_lifespan(app):
                yield
        else:
            yield

        if screenshot:
            await screenshot.stop()
        if file_store:
            await file_store.stop()

    app.router.lifespan_context = combined_lifespan
    return app


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config_path = os.environ.get("MCP_CONFIG_PATH", "config.yaml")
    config = AppConfig.load(config_path)
    import uvicorn

    app = create_app(config)
    uvicorn.run(app, host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
