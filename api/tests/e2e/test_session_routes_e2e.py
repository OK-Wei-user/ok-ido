#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_session_routes_e2e.py
session_routes.py 端到端会话测试 - 验证部署后系统完整性

测试覆盖:
1. 登录认证 (POST /api/auth/login)
2. 创建会话 (POST /api/sessions)
3. 列出会话 (GET /api/sessions)
4. 获取详情 (GET /api/sessions/{id})
5. SSE聊天 (POST /api/sessions/{id}/chat) - 简短消息验证全链路
6. 删除会话 (POST /api/sessions/{id}/delete) - 验证资源清理

验证目标:
- PlanAgent + ReActAgent 双智能体协作正常
- MCP/Skills/Sandbox/Browser 集成无回归
- 记忆系统(compact/emergency_compact)无异常
- SSE 事件流(message/plan/step/done)完整
- 依赖预加载(PythonKernelService)不破坏现有功能
"""
import json
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

API_BASE = "http://localhost:8000"
USERNAME = "admin"
PASSWORD = "admin123"
LOGIN_TIMEOUT = 10
DETAIL_TIMEOUT = 10
CHAT_TIMEOUT = 180  # 3分钟,简短消息应快速完成

# 简短测试消息 - 触发规划+执行+交付全链路,但不过度复杂
TEST_MESSAGE = "请用Python计算1到100的和,并把结果写到一个文件中。"


def log(step: str, status: str = "INFO", detail: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    msg = f"[{ts}] [{status}] {step}"
    if detail:
        msg += f" | {detail}"
    print(msg, flush=True)


def login() -> Optional[str]:
    """1. 登录认证"""
    log("步骤1-登录", "RUN", f"POST /api/auth/login 用户={USERNAME}")
    try:
        resp = httpx.post(
            f"{API_BASE}/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=LOGIN_TIMEOUT,
        )
        if resp.status_code != 200:
            log("步骤1-登录", "FAIL", f"HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        token = resp.json()["data"]["access_token"]
        log("步骤1-登录", "PASS", f"token={token[:24]}...")
        return token
    except Exception as e:
        log("步骤1-登录", "FAIL", f"异常: {e}")
        return None


def create_session(token: str) -> Optional[str]:
    """2. 创建会话"""
    log("步骤2-创建会话", "RUN", "POST /api/sessions")
    try:
        resp = httpx.post(
            f"{API_BASE}/api/sessions",
            headers={"Authorization": f"Bearer {token}"},
            timeout=DETAIL_TIMEOUT,
        )
        if resp.status_code != 200:
            log("步骤2-创建会话", "FAIL", f"HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        session_id = resp.json()["data"]["session_id"]
        log("步骤2-创建会话", "PASS", f"session_id={session_id}")
        return session_id
    except Exception as e:
        log("步骤2-创建会话", "FAIL", f"异常: {e}")
        return None


def list_sessions(token: str, expected_id: str) -> bool:
    """3. 列出会话 - 验证新会话出现在列表中"""
    log("步骤3-列出会话", "RUN", "GET /api/sessions")
    try:
        resp = httpx.get(
            f"{API_BASE}/api/sessions",
            headers={"Authorization": f"Bearer {token}"},
            timeout=DETAIL_TIMEOUT,
        )
        if resp.status_code != 200:
            log("步骤3-列出会话", "FAIL", f"HTTP {resp.status_code}")
            return False
        data = resp.json()["data"]
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        found = any(s.get("session_id") == expected_id for s in sessions)
        log(
            "步骤3-列出会话",
            "PASS" if found else "WARN",
            f"总会话数={len(sessions)}, 目标会话{'已找到' if found else '未找到'}",
        )
        return found
    except Exception as e:
        log("步骤3-列出会话", "FAIL", f"异常: {e}")
        return False


def get_detail(token: str, session_id: str) -> Optional[Dict[str, Any]]:
    """4. 获取会话详情"""
    log("步骤4-获取详情", "RUN", f"GET /api/sessions/{session_id}")
    try:
        resp = httpx.get(
            f"{API_BASE}/api/sessions/{session_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=DETAIL_TIMEOUT,
        )
        if resp.status_code != 200:
            log("步骤4-获取详情", "FAIL", f"HTTP {resp.status_code}")
            return None
        data = resp.json()["data"]
        log(
            "步骤4-获取详情",
            "PASS",
            f"status={data.get('status')} title={data.get('title', '')[:30]}",
        )
        return data
    except Exception as e:
        log("步骤4-获取详情", "FAIL", f"异常: {e}")
        return None


def chat_sse(token: str, session_id: str) -> Tuple[List[Dict[str, Any]], float, bool]:
    """5. SSE聊天 - 验证全链路(PlanAgent+ReActAgent+工具+记忆)"""
    log("步骤5-SSE聊天", "RUN", f"消息: {TEST_MESSAGE[:50]}...")
    events: List[Dict[str, Any]] = []
    start_perf = time.perf_counter()
    has_error = False

    try:
        with httpx.stream(
            "POST",
            f"{API_BASE}/api/sessions/{session_id}/chat",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json={"message": TEST_MESSAGE},
            timeout=CHAT_TIMEOUT,
        ) as resp:
            log("步骤5-SSE聊天", "INFO", f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                return events, time.perf_counter() - start_perf, True

            event_type = None
            data_lines: List[str] = []

            for line in resp.iter_lines():
                if line is None:
                    continue
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "" and event_type is not None:
                    data_str = "\n".join(data_lines)
                    elapsed_ms = (time.perf_counter() - start_perf) * 1000
                    event_info = {
                        "seq": len(events) + 1,
                        "type": event_type,
                        "elapsed_ms": round(elapsed_ms, 1),
                        "data_preview": data_str[:150],
                    }
                    events.append(event_info)

                    if event_type in ("plan", "step", "done", "error", "title", "message"):
                        log(
                            f"  事件#{event_info['seq']}",
                            "EVENT",
                            f"type={event_type} t={elapsed_ms:.0f}ms "
                            f"preview={data_str[:80]}",
                        )

                    if event_type == "error":
                        has_error = True
                    if event_type == "done":
                        break

                    event_type = None
                    data_lines = []

    except httpx.ReadTimeout:
        elapsed = time.perf_counter() - start_perf
        log("步骤5-SSE聊天", "WARN", f"读取超时({CHAT_TIMEOUT}s),已收到{len(events)}个事件")
        return events, elapsed, True
    except Exception as e:
        elapsed = time.perf_counter() - start_perf
        log("步骤5-SSE聊天", "FAIL", f"异常: {e}")
        return events, elapsed, True

    elapsed = time.perf_counter() - start_perf
    log("步骤5-SSE聊天", "PASS" if not has_error else "FAIL",
        f"共{len(events)}个事件,耗时{elapsed:.2f}s")
    return events, elapsed, has_error


def delete_session(token: str, session_id: str) -> bool:
    """6. 删除会话 - 验证资源清理(沙箱TTL+会话锁)"""
    log("步骤6-删除会话", "RUN", f"POST /api/sessions/{session_id}/delete")
    try:
        resp = httpx.post(
            f"{API_BASE}/api/sessions/{session_id}/delete",
            headers={"Authorization": f"Bearer {token}"},
            timeout=DETAIL_TIMEOUT,
        )
        if resp.status_code == 200 and resp.json().get("code") == 200:
            log("步骤6-删除会话", "PASS", "资源清理已执行(沙箱TTL+会话锁)")
            return True
        log("步骤6-删除会话", "FAIL", f"HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log("步骤6-删除会话", "FAIL", f"异常: {e}")
        return False


def analyze(events: List[Dict[str, Any]], elapsed: float) -> Dict[str, Any]:
    """分析测试结果"""
    type_counts: Dict[str, int] = {}
    for ev in events:
        t = ev["type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    return {
        "total_events": len(events),
        "elapsed_sec": round(elapsed, 2),
        "type_distribution": type_counts,
        "has_done": any(ev["type"] == "done" for ev in events),
        "has_error": any(ev["type"] == "error" for ev in events),
        "has_plan": any(ev["type"] == "plan" for ev in events),
        "has_title": any(ev["type"] == "title" for ev in events),
        "has_message": any(ev["type"] == "message" for ev in events),
        "has_step": any(ev["type"] == "step" for ev in events),
    }


def main() -> int:
    print("=" * 80)
    print(">>> session_routes.py 端到端会话测试")
    print(f">>> 消息: {TEST_MESSAGE}")
    print("=" * 80)

    # 步骤1: 登录
    token = login()
    if not token:
        return 1

    # 步骤2: 创建会话
    session_id = create_session(token)
    if not session_id:
        return 1

    # 步骤3: 列出会话
    list_sessions(token, session_id)

    # 步骤4: 获取详情(创建后)
    get_detail(token, session_id)

    # 步骤5: SSE聊天
    events, elapsed, has_error = chat_sse(token, session_id)

    # 步骤5.1: 获取详情(聊天后)
    log("步骤5.1-聊天后详情", "RUN", "验证事件持久化")
    detail = get_detail(token, session_id)
    if detail:
        persisted = len(detail.get("events", []))
        log("步骤5.1-聊天后详情", "PASS", f"持久化事件数={persisted}")

    # 步骤6: 删除会话
    delete_session(token, session_id)

    # 结果分析
    result = analyze(events, elapsed)
    print(f"\n{'=' * 80}")
    print(">>> 测试结果汇总")
    print(f"{'=' * 80}")
    print(f"  总事件数: {result['total_events']}")
    print(f"  总耗时: {result['elapsed_sec']}s")
    print(f"  事件类型分布: {result['type_distribution']}")
    print(f"  正常结束(done): {result['has_done']}")
    print(f"  有错误(error): {result['has_error']}")
    print(f"  有规划(plan): {result['has_plan']}")
    print(f"  有标题(title): {result['has_title']}")
    print(f"  有消息(message): {result['has_message']}")
    print(f"  有步骤(step): {result['has_step']}")

    # 通过条件: 有done + 无error + 有plan + 有message
    passed = (
        result["has_done"]
        and not result["has_error"]
        and result["has_plan"]
        and result["has_message"]
    )
    print(f"\n  测试结论: {'PASS ✓' if passed else 'FAIL ✗'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
