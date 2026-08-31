#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
E2E 会话测试 - 验证遗留建议修复后系统功能完整性

测试链路:
1. 登录(admin/admin123) → 获取JWT
2. 创建会话 → 获取session_id
3. SSE 聊天 → 验证事件流(PlanEvent/MessageEvent/DoneEvent)
4. 获取会话详情 → 验证事件持久化

验证目标:
- Phase F 注释清理不影响代码执行
- Phase C KeyFact.importance 保留向后兼容
- Phase D ErrorEvent 保留设计不影响 summarize 流程
"""
import json
import os
import sys
import time
from typing import Optional

import requests

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000/api")
USERNAME = "admin"
PASSWORD = "admin123"
# 简单问候任务,触发 Planner → ReAct → summarize 全链路
TEST_MESSAGE = "你好,请用一句话介绍你自己"
# SSE 读取超时(秒): 留足时间让 LLM 完成规划+执行+汇总
SSE_READ_TIMEOUT = 180
# SSE 总事件数上限(防止异常无限流) — 流式 chunk 可能很多,放宽到 2000
MAX_EVENTS = 2000


def _log(stage: str, msg: str, level: str = "INFO") -> None:
    """统一日志格式"""
    print(f"[{level}] [{stage}] {msg}", flush=True)


def login() -> str:
    """登录并返回 access_token"""
    _log("LOGIN", f"POST {API_BASE}/auth/login as {USERNAME}")
    resp = requests.post(
        f"{API_BASE}/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload["data"]["access_token"]
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


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def chat_via_sse(token: str, session_id: str, message: str) -> dict:
    """通过 SSE 发起聊天,收集事件并返回统计

    Returns:
        {
            "event_types": dict[str,int],  # 事件类型计数
            "has_plan": bool,
            "has_message": bool,
            "has_done": bool,
            "has_error": bool,
            "final_message": str,  # 最后一条 assistant 消息
            "event_count": int,
        }
    """
    _log("CHAT", f"POST /sessions/{session_id}/chat (SSE, msg='{message[:30]}...')")

    stats = {
        "event_types": {},
        "has_plan": False,
        "has_message": False,
        "has_done": False,
        "has_error": False,
        "final_message": "",
        "event_count": 0,
    }

    # 流式读取 SSE
    with requests.post(
        f"{API_BASE}/sessions/{session_id}/chat",
        headers={**_auth_header(token), "Accept": "text/event-stream"},
        json={"message": message, "attachments": []},
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
                # 空行表示一个事件结束
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

    # 关键事件类型检测
    if event_type == "plan":
        stats["has_plan"] = True
        _log("EVENT", f"[plan] id={event_id}")
    elif event_type == "message":
        stats["has_message"] = True
        # 提取最终 assistant 消息(优先 is_final=True)
        msg = data.get("message", "") if isinstance(data, dict) else ""
        is_final = data.get("is_final", False) if isinstance(data, dict) else False
        if msg:
            stats["final_message"] = msg
        # 仅记录 is_final=True 的 message,减少流式 chunk 日志噪音
        if is_final:
            _log("EVENT", f"[message] is_final=True len={len(msg)}")
    elif event_type == "done":
        stats["has_done"] = True
        _log("EVENT", f"[done] id={event_id}")
    elif event_type == "error":
        stats["has_error"] = True
        err_msg = data.get("error", "") if isinstance(data, dict) else str(data)
        _log("EVENT", f"[error] {err_msg}", "ERROR")
    elif event_type == "title":
        title = data.get("title", "") if isinstance(data, dict) else ""
        _log("EVENT", f"[title] {title}")
    elif event_type == "step":
        step_status = data.get("status", "") if isinstance(data, dict) else ""
        _log("EVENT", f"[step] status={step_status}")
    elif event_type == "tool":
        tool_name = data.get("function_name", "") if isinstance(data, dict) else ""
        _log("EVENT", f"[tool] {tool_name}")
    elif event_type == "ping":
        # SSE 心跳, 忽略
        pass
    # 其他事件类型(message 流式 delta 等)不记录日志,仅累计计数


def get_session_detail(token: str, session_id: str) -> dict:
    """获取会话详情,验证事件持久化"""
    _log("DETAIL", f"GET /sessions/{session_id}")
    resp = requests.get(
        f"{API_BASE}/sessions/{session_id}",
        headers=_auth_header(token),
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()["data"]
    _log(
        "DETAIL",
        f"会话状态={payload.get('status')}, 事件数={len(payload.get('events', []))}",
    )
    return payload


def assert_e2e(stats: dict, detail: dict) -> None:
    """断言 E2E 测试结果,失败则抛出 AssertionError"""
    failures = []

    # 1. 至少有 message 事件(Planner+summarize 链路必有)
    if not stats["has_message"]:
        failures.append("未收到任何 message 事件(summarize 链路异常)")
    # 2. 应该有 done 事件(正常结束)
    if not stats["has_done"]:
        failures.append("未收到 done 事件(会话未正常结束)")
    # 3. 不应有 error 事件
    if stats["has_error"]:
        failures.append("收到 error 事件(流中存在错误)")
    # 4. 最终消息不能为空
    if not stats["final_message"].strip():
        failures.append("最终 assistant 消息为空")
    # 5. 会话详情事件数应 > 0
    if len(detail.get("events", [])) == 0:
        failures.append("会话详情事件列表为空(持久化失败)")
    # 6. 会话状态应为 completed(API 返回小写枚举值)
    if detail.get("status") != "completed":
        failures.append(f"会话状态非 completed: {detail.get('status')}")
    # 7. 最终消息不应是结构化JSON(summarize输出规范化验证)
    final_msg = stats["final_message"].strip()
    if final_msg.startswith("{") and final_msg.endswith("}"):
        import json as _json
        try:
            data = _json.loads(final_msg)
            if isinstance(data, dict) and "result" in data:
                failures.append(
                    f"最终消息是结构化JSON而非自然语言(summarize JSON解析异常): "
                    f"{final_msg[:100]}"
                )
        except _json.JSONDecodeError:
            pass  # 非合法JSON,不判定为结构化输出

    if failures:
        msg = "E2E 测试失败:\n" + "\n".join(f"  - {f}" for f in failures)
        raise AssertionError(msg)

    _log("ASSERT", "所有断言通过 ✓")


def main() -> int:
    """主流程: 登录 → 创建会话 → SSE 聊天 → 校验"""
    _log("START", f"API_BASE={API_BASE}, 用户={USERNAME}")
    try:
        token = login()
        session_id = create_session(token)

        t0 = time.time()
        stats = chat_via_sse(token, session_id, TEST_MESSAGE)
        elapsed = time.time() - t0
        _log("CHAT", f"聊天耗时 {elapsed:.1f}s")

        detail = get_session_detail(token, session_id)
        assert_e2e(stats, detail)

        _log("DONE", f"会话 {session_id} E2E 测试通过")
        # 输出最终消息预览
        preview = stats["final_message"][:200].replace("\n", " ")
        _log("RESULT", f"AI 回复: {preview}")
        return 0
    except Exception as e:
        _log("FAIL", f"E2E 测试异常: {type(e).__name__}: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
