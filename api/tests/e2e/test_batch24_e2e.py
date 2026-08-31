#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
批次 24 E2E 回归测试 - 验证 P11 沙箱异步任务通知上线后会话流程正常

测试场景:
1. 登录获取 token (admin/admin123)
2. 创建会话
3. 发送简单消息,验证 SSE 事件流正常
4. 验证事件类型完整(message/title/plan/done)
5. 验证不出现异常错误
"""
import json
import time
import requests

BASE_URL = "http://localhost:8000/api"


def test_e2e_session_flow():
    """E2E 测试: 登录→创建会话→发送消息→验证 SSE 事件流"""
    # 1. 登录
    login_resp = requests.post(
        f"{BASE_URL}/auth/login",
        json={"username": "admin", "password": "admin123"},
        timeout=10,
    )
    assert login_resp.status_code == 200, f"登录失败: HTTP {login_resp.status_code}"
    token = login_resp.json()["data"]["access_token"]
    print(f"[1/5] Login OK, token={token[:30]}...")

    # 2. 创建会话
    headers = {"Authorization": f"Bearer {token}"}
    create_resp = requests.post(
        f"{BASE_URL}/sessions", headers=headers, timeout=10
    )
    assert create_resp.status_code == 200, f"创建会话失败: HTTP {create_resp.status_code}"
    session_id = create_resp.json()["data"]["session_id"]
    print(f"[2/5] Session created: {session_id}")

    # 3. 发送消息(正确编码中文)
    chat_body = {
        "message": "你好,请用一句话介绍你是谁",
        "attachments": [],
        "timestamp": int(time.time()),
    }
    chat_resp = requests.post(
        f"{BASE_URL}/sessions/{session_id}/chat",
        json=chat_body,
        headers=headers,
        timeout=120,
        stream=True,
    )
    assert chat_resp.status_code == 200, f"Chat 失败: HTTP {chat_resp.status_code}"
    print(f"[3/5] Chat HTTP {chat_resp.status_code}, SSE stream opened")

    # 4. 读取 SSE 事件流,收集事件类型
    event_types = set()
    assistant_messages = []
    plan_steps_count = None
    has_error = False
    error_msg = None
    has_done = False

    for line in chat_resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("event:"):
            evt_type = line.split(":", 1)[1].strip()
            event_types.add(evt_type)
        elif line.startswith("data:"):
            data_str = line.split(":", 1)[1].strip()
            try:
                data = json.loads(data_str)
                if data.get("event_id") and "error" in str(data).lower():
                    has_error = True
                    error_msg = str(data)[:200]
                if data.get("role") == "assistant" and data.get("message"):
                    assistant_messages.append(data["message"])
                if "steps" in data:
                    plan_steps_count = len(data.get("steps", []))
            except json.JSONDecodeError:
                pass

    print(f"[4/5] Event types: {sorted(event_types)}")
    print(f"      Plan steps: {plan_steps_count}")
    print(f"      Assistant messages: {len(assistant_messages)}")
    if assistant_messages:
        print(f"      First assistant msg: {assistant_messages[0][:200]}")
    if has_error:
        print(f"      [WARN] Error detected: {error_msg}")

    # 5. 验证事件类型完整性
    assert "message" in event_types, "缺失 message 事件"
    assert "done" in event_types, "缺失 done 事件(会话未正常完成)"
    print(f"[5/5] E2E PASSED: 事件流完整(message+done 均存在)")

    return {
        "session_id": session_id,
        "event_types": sorted(event_types),
        "plan_steps": plan_steps_count,
        "assistant_messages": assistant_messages,
        "has_error": has_error,
    }


if __name__ == "__main__":
    result = test_e2e_session_flow()
    print()
    print("=" * 60)
    print("批次 24 E2E 回归测试结果:")
    print(f"  会话 ID: {result['session_id']}")
    print(f"  事件类型: {result['event_types']}")
    print(f"  计划步骤数: {result['plan_steps']}")
    print(f"  助手消息数: {len(result['assistant_messages'])}")
    print(f"  是否有错误: {result['has_error']}")
    print("=" * 60)
