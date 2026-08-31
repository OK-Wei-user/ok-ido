#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_scenario3_warehouse.py
场景3 单独 E2E 测试 — 仓库方案设计(mermaid + 长文本理解 + 复杂业务推理)
"""
import json
import time
import sys
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime

import httpx

API_BASE = "http://localhost:8000"
USERNAME = "admin"
PASSWORD = "admin123"
LOGIN_TIMEOUT = 10
CHAT_TIMEOUT = 600  # 10分钟,仓库设计场景可能较长
DETAIL_TIMEOUT = 10


SCENARIO_MESSAGE = (
    "我有一个仓库，分成了两个储区，周转箱储区和托盘储区，"
    "周转箱储区主要作为小件货的分拣，托盘储区作为周转箱储区的仓储区和大件货的存储区，"
    "同时也会进行大件货的分拣。这两个储区都是高位立体货架，"
    "周转箱储区来了订单以后，通过CTU和线体把周转箱运输到工位，人工分拣，"
    "分拣后进过闪电分播墙完成分拣，然后进行打包贴快递面单。"
    "托盘储区由自动化叉车把要分拣的商品从立体货架取出，放到出库点，"
    "然后人工移动托盘到分拣区进行分拣打包。"
    "当前订单进入系统后，会自动的根据周转箱储区和托盘储区的商品属性进行拆分成两个订单，"
    "分别进行分拣发货。 目前对接了一个新的订单系统，要求订单不能进行拆分，"
    "我现场作业流程应该怎么规划，给我提出一个合理的方案。并设计详细作业流程图。"
)


def log(step: str, status: str = "INFO", detail: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    msg = f"[{ts}] [{status}] {step}"
    if detail:
        msg += f" | {detail}"
    print(msg, flush=True)


def login() -> Optional[str]:
    log("登录", "RUN", f"POST /api/auth/login 用户={USERNAME}")
    try:
        resp = httpx.post(
            f"{API_BASE}/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=LOGIN_TIMEOUT,
        )
        if resp.status_code != 200:
            log("登录", "FAIL", f"HTTP {resp.status_code}")
            return None
        token = resp.json()["data"]["access_token"]
        log("登录", "PASS", f"token={token[:24]}...")
        return token
    except Exception as e:
        log("登录", "FAIL", f"异常: {e}")
        return None


def create_session(token: str) -> Optional[str]:
    log("创建会话", "RUN", "POST /api/sessions")
    try:
        resp = httpx.post(
            f"{API_BASE}/api/sessions",
            headers={"Authorization": f"Bearer {token}"},
            timeout=DETAIL_TIMEOUT,
        )
        session_id = resp.json()["data"]["session_id"]
        log("创建会话", "PASS", f"session_id={session_id}")
        return session_id
    except Exception as e:
        log("创建会话", "FAIL", f"异常: {e}")
        return None


def chat_sse(token: str, session_id: str) -> Tuple[List[Dict[str, Any]], float]:
    log("SSE聊天", "RUN", f"消息长度={len(SCENARIO_MESSAGE)}字")
    events: List[Dict[str, Any]] = []
    start_perf = time.perf_counter()

    try:
        with httpx.stream(
                "POST",
                f"{API_BASE}/api/sessions/{session_id}/chat",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json={"message": SCENARIO_MESSAGE},
                timeout=CHAT_TIMEOUT,
        ) as resp:
            log("SSE响应", "INFO", f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                return events, time.perf_counter() - start_perf

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
                    event_time = time.perf_counter() - start_perf
                    event_info = {
                        "seq": len(events) + 1,
                        "type": event_type,
                        "elapsed_ms": round(event_time * 1000, 1),
                        "data_preview": data_str[:200] if data_str else "",
                    }
                    events.append(event_info)
                    # 仅打印关键事件(plan/step/done/error/title)
                    if event_type in ("plan", "step", "done", "error", "title", "message"):
                        log(f"SSE事件#{event_info['seq']}", "EVENT",
                            f"type={event_type} t={event_info['elapsed_ms']:.0f}ms "
                            f"preview={data_str[:120]}")
                    if event_type == "done":
                        break
                    event_type = None
                    data_lines = []
    except httpx.ReadTimeout:
        elapsed = time.perf_counter() - start_perf
        log("SSE聊天", "WARN", f"读取超时({CHAT_TIMEOUT}s),已收到{len(events)}个事件")
        return events, elapsed
    except Exception as e:
        elapsed = time.perf_counter() - start_perf
        log("SSE聊天", "FAIL", f"异常: {e}")
        return events, elapsed

    elapsed = time.perf_counter() - start_perf
    log("SSE聊天", "PASS", f"共{len(events)}个事件,耗时{elapsed:.2f}s")
    return events, elapsed


def get_detail(token: str, session_id: str) -> Optional[Dict[str, Any]]:
    log("获取详情", "RUN", f"GET /api/sessions/{session_id}")
    try:
        resp = httpx.get(
            f"{API_BASE}/api/sessions/{session_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=DETAIL_TIMEOUT,
        )
        data = resp.json()["data"]
        log("获取详情", "PASS",
            f"status={data.get('status')} events={len(data.get('events', []))}")
        return data
    except Exception as e:
        log("获取详情", "FAIL", f"异常: {e}")
        return None


def delete_session(token: str, session_id: str) -> bool:
    log("删除会话", "RUN", f"POST /api/sessions/{session_id}/delete")
    try:
        resp = httpx.post(
            f"{API_BASE}/api/sessions/{session_id}/delete",
            headers={"Authorization": f"Bearer {token}"},
            timeout=DETAIL_TIMEOUT,
        )
        if resp.status_code == 200 and resp.json().get("code") == 200:
            log("删除会话", "PASS", "finally资源清理已执行")
            return True
        return False
    except Exception as e:
        log("删除会话", "FAIL", f"异常: {e}")
        return False


def analyze(events: List[Dict[str, Any]], elapsed: float, detail: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    type_counts: Dict[str, int] = {}
    for ev in events:
        t = ev["type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    tool_calls: List[str] = []
    for ev in events:
        if ev["type"] == "tool":
            try:
                data = json.loads(ev["data_preview"]) if ev["data_preview"] else {}
                tool_name = data.get("tool_name", data.get("name", ""))
                if tool_name:
                    tool_calls.append(tool_name)
            except Exception:
                pass

    return {
        "total_events": len(events),
        "elapsed_sec": round(elapsed, 2),
        "type_distribution": type_counts,
        "tool_calls": tool_calls,
        "tool_call_count": len(tool_calls),
        "has_done": any(ev["type"] == "done" for ev in events),
        "has_error": any(ev["type"] == "error" for ev in events),
        "has_plan": any(ev["type"] == "plan" for ev in events),
        "has_title": any(ev["type"] == "title" for ev in events),
        "has_message": any(ev["type"] == "message" for ev in events),
        "session_status": detail.get("status") if detail else "unknown",
        "persisted_events": len(detail.get("events", [])) if detail else 0,
    }


def main():
    print("=" * 80)
    print(">>> 场景3: 仓库方案设计(mermaid + 长文本理解 + 复杂业务推理)")
    print("=" * 80)

    token = login()
    if not token:
        return 1

    session_id = create_session(token)
    if not session_id:
        return 1

    events, elapsed = chat_sse(token, session_id)
    detail = get_detail(token, session_id)
    result = analyze(events, elapsed, detail)

    print(f"\n{'=' * 80}")
    print(">>> 场景3 测试结果汇总")
    print(f"{'=' * 80}")
    print(f"  总事件数: {result['total_events']}")
    print(f"  总耗时: {result['elapsed_sec']}s")
    print(f"  事件类型分布: {result['type_distribution']}")
    print(f"  工具调用数: {result['tool_call_count']}")
    if result['tool_calls']:
        print(f"  工具调用序列: {result['tool_calls']}")
    print(f"  正常结束(done): {result['has_done']}")
    print(f"  有错误(error): {result['has_error']}")
    print(f"  有规划(plan): {result['has_plan']}")
    print(f"  有标题(title): {result['has_title']}")
    print(f"  有消息(message): {result['has_message']}")
    print(f"  会话最终状态: {result['session_status']}")
    print(f"  持久化事件数: {result['persisted_events']}")

    delete_session(token, session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
