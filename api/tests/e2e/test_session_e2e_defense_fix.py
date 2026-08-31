#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_session_e2e_defense_fix.py
防御缺口修复后的端到端会话测试

验证流程：登录 → 创建会话 → 发送消息 → 验证SSE响应
测试用户：admin / admin123
"""
import json
import sys
import time

import httpx

API_BASE = "http://localhost:8000"
USERNAME = "admin"
PASSWORD = "admin123"
TIMEOUT = 60


def log_step(step: str, status: str = "INFO", detail: str = "") -> None:
    """打印步骤日志"""
    prefix = f"[{status}]"
    message = f"{prefix} {step}"
    if detail:
        message += f": {detail}"
    print(message, flush=True)


def test_login() -> str:
    """步骤1: 登录获取 access_token"""
    log_step("步骤1: 登录", "RUN", f"用户名={USERNAME}")
    resp = httpx.post(
        f"{API_BASE}/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, f"登录失败: HTTP {resp.status_code}, {resp.text}"
    data = resp.json()
    assert data.get("code") == 200, f"登录业务错误: {data}"
    token = data["data"]["access_token"]
    assert token, "access_token 为空"
    log_step("步骤1: 登录成功", "PASS", f"token={token[:20]}...")
    return token


def test_create_session(token: str) -> str:
    """步骤2: 创建新会话"""
    log_step("步骤2: 创建新会话", "RUN")
    resp = httpx.post(
        f"{API_BASE}/sessions",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, f"创建会话失败: HTTP {resp.status_code}, {resp.text}"
    data = resp.json()
    assert data.get("code") == 200, f"创建会话业务错误: {data}"
    session_id = data["data"]["session_id"]
    assert session_id, "session_id 为空"
    log_step("步骤2: 创建会话成功", "PASS", f"session_id={session_id}")
    return session_id


def test_chat(token: str, session_id: str, message: str) -> dict:
    """步骤3: 发送消息并收集SSE响应事件

    返回收集到的事件统计。
    """
    log_step("步骤3: 发送消息", "RUN", f"消息={message[:50]}...")
    events = []
    event_types = set()
    start_time = time.time()

    with httpx.stream(
        "POST",
        f"{API_BASE}/sessions/{session_id}/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": message},
        timeout=120,
    ) as response:
        assert response.status_code == 200, f"聊天请求失败: HTTP {response.status_code}"

        current_event_type = None
        current_data = ""

        for line in response.iter_lines():
            if time.time() - start_time > 100:
                log_step("步骤3: 超时(100s)，停止读取", "WARN")
                break

            if line.startswith("event:"):
                current_event_type = line[6:].strip()
            elif line.startswith("data:"):
                current_data = line[5:].strip()
            elif line == "" and current_event_type:
                # 事件边界
                events.append({
                    "type": current_event_type,
                    "data": current_data[:200],  # 截断长数据
                })
                event_types.add(current_event_type)

                # 检查是否收到终止信号
                stop = False
                if current_event_type and "done" in str(current_event_type).lower():
                    stop = True
                if current_data and '"event_type"' in current_data:
                    try:
                        parsed = json.loads(current_data)
                        if isinstance(parsed, dict) and parsed.get("event_type") in ("done", "error", "wait"):
                            stop = True
                    except (json.JSONDecodeError, TypeError):
                        pass

                current_event_type = None
                current_data = ""

                if stop:
                    break

            # 收到足够事件后停止（避免无限等待）
            if len(events) >= 50:
                log_step("步骤3: 收集到50个事件，停止读取", "WARN")
                break

    elapsed = time.time() - start_time
    log_step(
        "步骤3: SSE响应收集完成",
        "PASS",
        f"事件数={len(events)}, 类型={event_types}, 耗时={elapsed:.1f}s",
    )
    return {
        "events": events,
        "event_types": event_types,
        "count": len(events),
        "elapsed": elapsed,
    }


def test_list_sessions(token: str) -> int:
    """步骤4: 获取会话列表验证"""
    log_step("步骤4: 获取会话列表", "RUN")
    resp = httpx.get(
        f"{API_BASE}/sessions",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, f"获取会话列表失败: HTTP {resp.status_code}"
    data = resp.json()
    assert data.get("code") == 200, f"获取会话列表业务错误: {data}"
    sessions = data["data"]["sessions"]
    log_step("步骤4: 获取会话列表成功", "PASS", f"会话数={len(sessions)}")
    return len(sessions)


def test_get_session_detail(token: str, session_id: str) -> dict:
    """步骤5: 获取会话详情验证"""
    log_step("步骤5: 获取会话详情", "RUN", f"session_id={session_id}")
    resp = httpx.get(
        f"{API_BASE}/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, f"获取会话详情失败: HTTP {resp.status_code}"
    data = resp.json()
    assert data.get("code") == 200, f"获取会话详情业务错误: {data}"
    session = data["data"]
    log_step(
        "步骤5: 获取会话详情成功",
        "PASS",
        f"status={session.get('status', 'N/A')}, title={session.get('title', 'N/A')[:30]}",
    )
    return session


def main() -> int:
    """主测试流程"""
    print("=" * 60, flush=True)
    print("防御缺口修复后端到端会话测试", flush=True)
    print("=" * 60, flush=True)

    try:
        # 步骤1: 登录
        token = test_login()

        # 步骤2: 创建会话
        session_id = test_create_session(token)

        # 步骤4: 获取会话列表（在聊天前）
        test_list_sessions(token)

        # 步骤3: 发送简单对话消息
        chat_result = test_chat(
            token, session_id,
            "你好，请简单介绍一下你自己，一句话即可。",
        )

        # 验证SSE响应
        if chat_result["count"] == 0:
            log_step("验证: SSE响应事件数为0", "FAIL")
            return 1
        log_step("验证: SSE响应事件数>0", "PASS", f"count={chat_result['count']}")

        # 验证事件类型包含预期类型
        expected_types = {"thinking", "message", "done", "plan", "step", "tool"}
        actual_types = chat_result["event_types"]
        has_meaningful = actual_types & expected_types
        if not has_meaningful:
            log_step("验证: 事件类型包含预期类型", "FAIL", f"actual={actual_types}")
            return 1
        log_step("验证: 事件类型包含预期类型", "PASS", f"types={has_meaningful}")

        # 步骤5: 获取会话详情（在聊天后）
        session_detail = test_get_session_detail(token, session_id)

        # 验证会话状态
        status = session_detail.get("status", "")
        if status not in ("completed", "running", "waiting", "pending"):
            log_step("验证: 会话状态有效", "FAIL", f"status={status}")
            return 1
        log_step("验证: 会话状态有效", "PASS", f"status={status}")

        print("=" * 60, flush=True)
        print("所有测试通过！", flush=True)
        print("=" * 60, flush=True)
        return 0

    except AssertionError as e:
        log_step("测试失败", "FAIL", str(e))
        return 1
    except Exception as e:
        log_step("测试异常", "ERROR", f"{type(e).__name__}: {str(e)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
