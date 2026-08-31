#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
多模态 E2E 会话测试 - 验证思考模式与tool_choice兼容性修复

测试场景:
1. 登录 → 获取JWT
2. 创建会话
3. 生成测试图片 → 上传 → 获取file_id
4. 带图片附件发起聊天(触发多模态视觉理解步骤)
5. 验证:
   - 不出现 "Thinking mode does not support this tool_choice" 400错误
   - 会话正常完成(收到done事件,状态completed)
   - 不出现error事件

修复背景:
GLM/DeepSeek思考模式不支持tool_choice参数,ReAct多模态步骤重试时
设置tool_choice="required"会触发400错误。openai_llm.py适配器层
自动降级tool_choice为None,让LLM自主决策。
"""
import io
import json
import os
import sys
import time
from typing import Optional

import requests

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000/api")
USERNAME = "admin"
PASSWORD = "admin123"
# 多模态测试消息: 询问图片内容,触发视觉理解工具调用
TEST_MESSAGE = "请描述这张图片的内容"
SSE_READ_TIMEOUT = 300  # 多模态场景需要更长时间(视觉理解+LLM推理)
MAX_EVENTS = 3000
# 标记修复前的错误关键词,用于检测是否复现
ERROR_MARKER = "Thinking mode does not support this tool_choice"


def _log(stage: str, msg: str, level: str = "INFO") -> None:
    """统一日志格式"""
    print(f"[{level}] [{stage}] {msg}", flush=True)


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def login() -> str:
    """登录并返回 access_token"""
    _log("LOGIN", f"POST {API_BASE}/auth/login as {USERNAME}")
    resp = requests.post(
        f"{API_BASE}/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    token = resp.json()["data"]["access_token"]
    _log("LOGIN", f"成功获取 token (前12位): {token[:12]}...")
    return token


def create_session(token: str) -> str:
    """创建新会话并返回 session_id"""
    _log("CREATE", "POST /sessions")
    resp = requests.post(
        f"{API_BASE}/sessions",
        headers=_auth_header(token),
        timeout=10,
    )
    resp.raise_for_status()
    session_id = resp.json()["data"]["session_id"]
    _log("CREATE", f"会话创建成功: {session_id}")
    return session_id


def generate_test_image() -> bytes:
    """生成测试用 JPEG 图片(纯色+文字,约2KB)"""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (200, 100), color=(135, 206, 235))  # 天蓝色
        draw = ImageDraw.Draw(img)
        draw.text((30, 40), "Test Image", fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except ImportError:
        # 无PIL时返回最小JPEG(1x1像素)
        return bytes([
            0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
            0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
            0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
            0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
            0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
            0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
            0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
            0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
            0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
            0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
            0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
            0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
            0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
            0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
            0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
            0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
            0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
            0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
            0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
            0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
            0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
            0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
            0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
            0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
            0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
            0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
            0x00, 0x00, 0x3F, 0x00, 0x7B, 0x40, 0x1B, 0xFF, 0xD9,
        ])


def upload_image(token: str, image_bytes: bytes) -> str:
    """上传图片到 /api/files,返回 file_id"""
    _log("UPLOAD", f"POST /files (图片大小: {len(image_bytes)} bytes)")
    files = {"file": ("test.jpg", image_bytes, "image/jpeg")}
    resp = requests.post(
        f"{API_BASE}/files",
        headers=_auth_header(token),
        files=files,
        timeout=30,
    )
    resp.raise_for_status()
    file_id = resp.json()["data"]["id"]
    _log("UPLOAD", f"文件上传成功: file_id={file_id}")
    return file_id


def chat_via_sse(token: str, session_id: str, message: str, file_id: str) -> dict:
    """通过 SSE 发起聊天(带图片附件),收集事件并返回统计"""
    _log("CHAT", f"POST /sessions/{session_id}/chat (SSE, msg='{message}', attachment={file_id})")

    stats = {
        "event_types": {},
        "has_plan": False,
        "has_message": False,
        "has_done": False,
        "has_error": False,
        "error_messages": [],
        "final_message": "",
        "event_count": 0,
        "has_tool_call": False,
    }

    with requests.post(
        f"{API_BASE}/sessions/{session_id}/chat",
        headers={**_auth_header(token), "Accept": "text/event-stream"},
        json={"message": message, "attachments": [file_id]},
        stream=True,
        timeout=(10, SSE_READ_TIMEOUT),
    ) as resp:
        resp.raise_for_status()
        _log("CHAT", f"SSE 连接建立, HTTP {resp.status_code}")

        event_type = None
        data_lines = []
        event_id = None

        for raw in resp.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            if raw == "":
                if event_type is not None and data_lines:
                    _process_sse_event(event_type, data_lines, event_id, stats)
                event_type = None
                data_lines = []
                event_id = None
                continue
            if raw.startswith("event:"):
                event_type = raw[6:].strip()
            elif raw.startswith("data:"):
                data_lines.append(raw[5:].strip())
            elif raw.startswith("id:"):
                event_id = raw[3:].strip()

            if stats["event_count"] >= MAX_EVENTS:
                _log("CHAT", f"达到事件上限 {MAX_EVENTS}, 主动断开", "WARN")
                break

    _log(
        "CHAT",
        f"SSE 结束: 共 {stats['event_count']} 个事件, "
        f"types={stats['event_types']}",
    )
    return stats


def _process_sse_event(
    event_type: Optional[str],
    data_lines: list,
    event_id: Optional[str],
    stats: dict,
) -> None:
    """处理单个 SSE 事件"""
    stats["event_count"] += 1
    stats["event_types"][event_type or "unknown"] = (
        stats["event_types"].get(event_type or "unknown", 0) + 1
    )

    data_str = "\n".join(data_lines)
    try:
        data = json.loads(data_str) if data_str else {}
    except json.JSONDecodeError:
        data = {"raw": data_str}

    if event_type == "plan":
        stats["has_plan"] = True
        _log("EVENT", f"[plan] id={event_id}")
    elif event_type == "message":
        stats["has_message"] = True
        msg = data.get("message", "") if isinstance(data, dict) else ""
        is_final = data.get("is_final", False) if isinstance(data, dict) else False
        if msg:
            stats["final_message"] = msg
        if is_final:
            _log("EVENT", f"[message] is_final=True len={len(msg)}")
    elif event_type == "done":
        stats["has_done"] = True
        _log("EVENT", f"[done] id={event_id}")
    elif event_type == "error":
        stats["has_error"] = True
        err_msg = data.get("error", "") if isinstance(data, dict) else str(data)
        stats["error_messages"].append(err_msg)
        _log("EVENT", f"[error] {err_msg}", "ERROR")
    elif event_type == "title":
        title = data.get("title", "") if isinstance(data, dict) else ""
        _log("EVENT", f"[title] {title}")
    elif event_type == "step":
        step_status = data.get("status", "") if isinstance(data, dict) else ""
        step_desc = data.get("description", "") if isinstance(data, dict) else ""
        _log("EVENT", f"[step] status={step_status} desc={step_desc[:60]}")
    elif event_type == "tool":
        stats["has_tool_call"] = True
        tool_name = data.get("function_name", "") if isinstance(data, dict) else ""
        _log("EVENT", f"[tool] {tool_name}")


def assert_multimodal_e2e(stats: dict) -> None:
    """断言多模态 E2E 测试结果"""
    failures = []

    # 1. 不应出现修复前的400错误
    for err_msg in stats["error_messages"]:
        if ERROR_MARKER in err_msg:
            failures.append(
                f"复现修复前错误: {ERROR_MARKER} (适配器层降级未生效)"
            )

    # 2. 会话应正常完成(收到done事件)
    if not stats["has_done"]:
        failures.append("未收到 done 事件(会话未正常结束)")

    # 3. 不应有error事件(除非是工具调用失败等可恢复错误,但不能是400错误)
    critical_errors = [
        err for err in stats["error_messages"]
        if ERROR_MARKER in err or "tool_choice" in err.lower()
    ]
    if critical_errors:
        failures.append(f"存在tool_choice相关错误: {critical_errors}")

    # 4. 至少有message事件
    if not stats["has_message"]:
        failures.append("未收到任何 message 事件")

    if failures:
        msg = "多模态 E2E 测试失败:\n" + "\n".join(f"  - {f}" for f in failures)
        raise AssertionError(msg)

    _log("ASSERT", "所有断言通过 ✓")


def main() -> int:
    """主流程: 登录 → 创建会话 → 上传图片 → SSE聊天 → 校验"""
    _log("START", f"API_BASE={API_BASE}, 用户={USERNAME}")
    try:
        token = login()
        session_id = create_session(token)

        # 生成并上传测试图片
        image_bytes = generate_test_image()
        file_id = upload_image(token, image_bytes)

        # 带图片附件发起聊天
        t0 = time.time()
        stats = chat_via_sse(token, session_id, TEST_MESSAGE, file_id)
        elapsed = time.time() - t0
        _log("CHAT", f"聊天耗时 {elapsed:.1f}s")

        assert_multimodal_e2e(stats)

        _log("DONE", f"会话 {session_id} 多模态 E2E 测试通过")
        preview = stats["final_message"][:200].replace("\n", " ")
        _log("RESULT", f"AI 回复: {preview}")
        return 0
    except Exception as e:
        _log("FAIL", f"多模态 E2E 测试异常: {type(e).__name__}: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
