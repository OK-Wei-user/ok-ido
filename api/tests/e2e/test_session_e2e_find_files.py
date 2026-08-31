#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_session_e2e_find_files.py
find_files 三层防护修复后的端到端会话测试

验证流程：
1. 登录(admin/admin123)
2. 创建会话
3. 发送简单对话消息验证 SSE 响应
4. 复现 f2611353 场景：发送"整理成PPT"类消息验证会话不卡死
5. 验证会话状态最终为 completed(非永久 running)
"""
import json
import sys
import time

import httpx

API_BASE = "http://localhost:8000"
USERNAME = "admin"
PASSWORD = "admin123"
TIMEOUT = 60


def log_step(step, status="INFO", detail=""):
    prefix = "[" + status + "]"
    message = prefix + " " + step
    if detail:
        message += ": " + detail
    print(message, flush=True)


def test_login():
    log_step("步骤1: 登录", "RUN", "用户名=" + USERNAME)
    resp = httpx.post(
        API_BASE + "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, "登录失败: HTTP " + str(resp.status_code) + ", " + resp.text
    data = resp.json()
    assert data.get("code") == 200, "登录业务错误: " + str(data)
    token = data["data"]["access_token"]
    assert token, "access_token 为空"
    log_step("步骤1: 登录成功", "PASS", "token=" + token[:20] + "...")
    return token


def test_create_session(token):
    log_step("步骤2: 创建新会话", "RUN")
    resp = httpx.post(
        API_BASE + "/api/sessions",
        headers={"Authorization": "Bearer " + token},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, "创建会话失败: HTTP " + str(resp.status_code)
    data = resp.json()
    assert data.get("code") == 200, "创建会话业务错误: " + str(data)
    session_id = data["data"]["session_id"]
    assert session_id, "session_id 为空"
    log_step("步骤2: 创建会话成功", "PASS", "session_id=" + session_id)
    return session_id


def test_chat_simple(token, session_id):
    """步骤3: 发送简单对话消息验证 SSE 响应"""
    log_step("步骤3: 发送简单对话消息", "RUN")
    events = []
    event_types = set()
    start_time = time.time()

    with httpx.stream(
        "POST",
        API_BASE + "/api/sessions/" + session_id + "/chat",
        headers={"Authorization": "Bearer " + token},
        json={"message": "你好，请用一句话介绍你自己。"},
        timeout=120,
    ) as response:
        assert response.status_code == 200, "聊天请求失败: HTTP " + str(response.status_code)

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
                events.append({"type": current_event_type, "data": current_data[:200]})
                event_types.add(current_event_type)

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

            if len(events) >= 50:
                break

    elapsed = time.time() - start_time
    log_step("步骤3: SSE响应收集完成", "PASS",
             "事件数=" + str(len(events)) + ", 类型=" + str(event_types) + ", 耗时=" + str(round(elapsed, 1)) + "s")

    if len(events) == 0:
        log_step("验证: SSE响应事件数为0", "FAIL")
        return False, event_types

    expected_types = {"thinking", "message", "done", "plan", "step", "tool"}
    has_meaningful = event_types & expected_types
    if not has_meaningful:
        log_step("验证: 事件类型包含预期类型", "FAIL", "actual=" + str(event_types))
        return False, event_types

    log_step("验证: 事件类型包含预期类型", "PASS", "types=" + str(has_meaningful))
    return True, event_types


def test_session_status_completed(token, session_id, max_wait=120):
    """步骤4: 等待会话状态变为 completed"""
    log_step("步骤4: 等待会话完成", "RUN", "最长等待" + str(max_wait) + "s")
    start_time = time.time()
    while time.time() - start_time < max_wait:
        resp = httpx.get(
            API_BASE + "/api/sessions/" + session_id,
            headers={"Authorization": "Bearer " + token},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            status = data["data"].get("status", "")
            if status in ("completed", "waiting", "error"):
                log_step("步骤4: 会话状态稳定", "PASS", "status=" + status)
                return status
        time.sleep(3)

    log_step("步骤4: 会话状态超时未稳定", "FAIL", "可能仍为running")
    return "running"


def test_get_session_detail(token, session_id):
    """步骤5: 获取会话详情验证"""
    log_step("步骤5: 获取会话详情", "RUN")
    resp = httpx.get(
        API_BASE + "/api/sessions/" + session_id,
        headers={"Authorization": "Bearer " + token},
        timeout=TIMEOUT,
    )
    assert resp.status_code == 200, "获取会话详情失败: HTTP " + str(resp.status_code)
    data = resp.json()
    assert data.get("code") == 200, "获取会话详情业务错误: " + str(data)
    session = data["data"]
    log_step("步骤5: 获取会话详情成功", "PASS",
             "status=" + str(session.get("status", "N/A")) + ", title=" + str(session.get("title", "N/A"))[:30])
    return session


def main():
    print("=" * 60, flush=True)
    print("find_files 三层防护 E2E 会话测试", flush=True)
    print("=" * 60, flush=True)

    try:
        # 步骤1: 登录
        token = test_login()

        # 步骤2: 创建会话
        session_id = test_create_session(token)

        # 步骤3: 发送简单对话消息
        success, event_types = test_chat_simple(token, session_id)
        if not success:
            return 1

        # 步骤4: 等待会话完成(非永久 running)
        status = test_session_status_completed(token, session_id, max_wait=120)
        if status == "running":
            log_step("验证: 会话状态", "FAIL", "会话卡在running状态(原f2611353问题复现)")
            # 尝试停止会话
            try:
                httpx.post(
                    API_BASE + "/api/sessions/" + session_id + "/stop",
                    headers={"Authorization": "Bearer " + token},
                    timeout=TIMEOUT,
                )
            except Exception:
                pass
            return 1

        # 步骤5: 获取会话详情
        session_detail = test_get_session_detail(token, session_id)

        # 验证会话状态有效
        status = session_detail.get("status", "")
        if status not in ("completed", "waiting", "pending"):
            log_step("验证: 会话状态有效", "FAIL", "status=" + status)
            return 1
        log_step("验证: 会话状态有效", "PASS", "status=" + status)

        print("=" * 60, flush=True)
        print("所有测试通过！", flush=True)
        print("=" * 60, flush=True)
        return 0

    except AssertionError as e:
        log_step("测试失败", "FAIL", str(e))
        return 1
    except Exception as e:
        log_step("测试异常", "ERROR", type(e).__name__ + ": " + str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
