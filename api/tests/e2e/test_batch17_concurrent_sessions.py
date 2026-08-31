#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
批次17并发会话测试脚本 — 验证F10-7/F10-8/文件下载/结果交付优化

会话1: 根据26年1-5月份的全部出入库、库存数据深度分析(数据型任务,验证F10-7异步回调+交付物质量)
会话2: 深度搜索2026年人工智能发展趋势(搜索型任务,验证search_web预算+deep_research)

并发执行后报告:
- 事件数/工具调用数/SSE事件类型
- F10-7 sleep调用次数(应=0)/task_wait调用次数(应>=1)
- F10-8过滤日志(临时文件/过程文件/未同步)
- 交付物文件路径+命名质量
- 文件下载是否仍500(应已修复为422)
"""
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

import httpx

API_BASE = "http://localhost:8000/api"
USERNAME = "admin"
PASSWORD = "admin123"
TIMEOUT = 600  # 单会话最长10分钟


def log(session_name: str, status: str, msg: str = "") -> None:
    """线程安全日志输出"""
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}][{session_name}][{status}] {msg}"
    print(line, flush=True)


def login() -> str:
    """登录获取access_token"""
    resp = httpx.post(
        f"{API_BASE}/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=60,
    )
    assert resp.status_code == 200, f"登录失败: HTTP {resp.status_code}, {resp.text}"
    data = resp.json()
    assert data.get("code") == 200, f"登录业务错误: {data}"
    return data["data"]["access_token"]


def create_session(token: str, title: str) -> str:
    """创建会话"""
    resp = httpx.post(
        f"{API_BASE}/sessions",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": title},
        timeout=60,
    )
    assert resp.status_code == 200, f"创建会话失败: HTTP {resp.status_code}"
    return resp.json()["data"]["session_id"]


def run_session(session_name: str, token: str, session_id: str, message: str) -> Dict[str, Any]:
    """执行单会话: 发送消息 + 收集SSE事件"""
    log(session_name, "RUN", f"发送消息: {message[:80]}...")
    events: List[Dict[str, Any]] = []
    event_types: set = set()
    tool_calls: List[str] = []
    sleep_calls: int = 0
    task_wait_calls: int = 0
    f10_8_filtered: int = 0
    deliverables: List[str] = []
    final_message_snippet: str = ""
    start_time = time.time()

    try:
        with httpx.stream(
            "POST",
            f"{API_BASE}/sessions/{session_id}/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": message},
            timeout=TIMEOUT,
        ) as response:
            if response.status_code != 200:
                log(session_name, "ERROR", f"聊天请求失败: HTTP {response.status_code}")
                return {"error": f"HTTP {response.status_code}"}

            current_event_type = None
            current_data_parts: List[str] = []

            for line in response.iter_lines():
                if time.time() - start_time > TIMEOUT:
                    log(session_name, "WARN", f"超时({TIMEOUT}s),停止读取")
                    break

                if line.startswith("event:"):
                    current_event_type = line[6:].strip()
                elif line.startswith("data:"):
                    current_data_parts.append(line[5:].strip())
                elif line == "" and current_event_type:
                    data_str = "".join(current_data_parts)
                    events.append({"type": current_event_type, "data": data_str[:500]})
                    event_types.add(current_event_type)

                    # 解析事件数据
                    try:
                        parsed = json.loads(data_str) if data_str else {}
                    except (json.JSONDecodeError, TypeError):
                        parsed = {}

                    # 工具调用统计
                    if current_event_type == "tool_call":
                        tool_name = parsed.get("data", {}).get("tool_name", "")
                        if tool_name:
                            tool_calls.append(tool_name)
                            if tool_name == "shell_execute":
                                cmd = parsed.get("data", {}).get("input", {}).get("command", "")
                                if cmd and "sleep" in cmd.lower():
                                    sleep_calls += 1
                            elif tool_name == "task_wait":
                                task_wait_calls += 1

                    # F10-8过滤日志(从日志中提取)
                    if "filtered" in data_str.lower() or "过滤" in data_str:
                        f10_8_filtered += 1

                    # 交付物路径
                    if current_event_type in ("step", "step_completed"):
                        step_data = parsed.get("data", {})
                        attachments = step_data.get("attachments") or []
                        for att in attachments:
                            if isinstance(att, str):
                                deliverables.append(att)
                            elif isinstance(att, dict):
                                fp = att.get("filepath") or att.get("path")
                                if fp:
                                    deliverables.append(fp)

                    # 最终消息
                    if current_event_type in ("message", "final_message", "done"):
                        if parsed.get("event_type") == "done" or parsed.get("is_final"):
                            final_message_snippet = str(parsed.get("data", {}).get("content", ""))[:200]

                    # 终止信号
                    stop = False
                    if current_event_type and "done" in str(current_event_type).lower():
                        stop = True
                    if "event_type" in data_str:
                        if parsed.get("event_type") in ("done", "error"):
                            stop = True

                    current_event_type = None
                    current_data_parts = []

                    if stop:
                        break
    except Exception as e:
        log(session_name, "ERROR", f"会话异常: {type(e).__name__}: {str(e)[:200]}")
        return {
            "session_name": session_name,
            "session_id": session_id,
            "error": str(e),
            "events_count": len(events),
        }

    elapsed = time.time() - start_time
    result = {
        "session_name": session_name,
        "session_id": session_id,
        "events_count": len(events),
        "event_types": sorted(event_types),
        "tool_calls": tool_calls,
        "tool_calls_count": len(tool_calls),
        "sleep_calls": sleep_calls,
        "task_wait_calls": task_wait_calls,
        "f10_8_filtered": f10_8_filtered,
        "deliverables": deliverables,
        "final_message_snippet": final_message_snippet,
        "elapsed_seconds": round(elapsed, 1),
    }
    log(
        session_name,
        "DONE",
        f"事件={len(events)} 工具={len(tool_calls)} sleep={sleep_calls} task_wait={task_wait_calls} "
        f"交付物={len(deliverables)} 耗时={elapsed:.1f}s",
    )
    return result


def verify_file_download(token: str, session_id: str) -> Dict[str, Any]:
    """验证文件下载是否正常(测试500错误修复)"""
    log(session_id[:8], "VERIFY", "获取会话文件列表...")
    resp = httpx.get(
        f"{API_BASE}/sessions/{session_id}/files",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        return {"files_api_status": resp.status_code}
    # 响应结构: {"data": {"files": [...]}}
    files_data = resp.json().get("data", {}).get("files", [])
    download_results = []
    for f in files_data[:3]:  # 只测试前3个文件
        file_id = f.get("id") or f.get("file_id")
        if not file_id:
            continue
        # HEAD 请求验证下载可用性,不实际下载文件内容
        try:
            dl_resp = httpx.get(
                f"{API_BASE}/files/{file_id}/download",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                follow_redirects=False,
            )
            download_results.append({
                "file_id": file_id,
                "filename": f.get("filename"),
                "sync_status": f.get("sync_status"),
                "download_status": dl_resp.status_code,
                "is_500": dl_resp.status_code == 500,
                "is_422": dl_resp.status_code == 422,
            })
        except Exception as e:
            download_results.append({
                "file_id": file_id,
                "error": str(e)[:100],
            })
    return {"files_count": len(files_data), "download_results": download_results}


def main() -> int:
    """主入口: 并发执行2个会话"""
    print("=" * 80)
    print("批次17并发会话测试 - 验证F10-7/F10-8/文件下载/结果交付优化")
    print("=" * 80)

    # 1.登录
    print("\n[1] 登录...")
    token = login()
    print(f"    token={token[:30]}...")

    # 2.并发创建2个会话
    print("\n[2] 并发创建2个会话...")
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_s1 = pool.submit(create_session, token, "S1-出入库分析")
        future_s2 = pool.submit(create_session, token, "S2-AI趋势搜索")
        session_s1 = future_s1.result()
        session_s2 = future_s2.result()
    print(f"    S1 session_id={session_s1}")
    print(f"    S2 session_id={session_s2}")

    # 3.并发发送消息
    print("\n[3] 并发发送消息(SSE)...")
    message_s1 = "根据26年1-5月份的全部出入库、库存数据,为我深度分析,用于生产把控和经营参考"
    message_s2 = "深度搜索请为我搜索 2026 年人工智能发展趋势"

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_s1_chat = pool.submit(run_session, "S1-出入库", token, session_s1, message_s1)
        future_s2_chat = pool.submit(run_session, "S2-AI搜索", token, session_s2, message_s2)

        results = []
        for future in as_completed([future_s1_chat, future_s2_chat]):
            try:
                results.append(future.result())
            except Exception as e:
                results.append({"error": f"{type(e).__name__}: {str(e)[:200]}"})

    # 4.验证文件下载(会话完成后)
    print("\n[4] 验证文件下载(测试500错误修复)...")
    download_verifications = []
    for r in results:
        if r.get("session_id") and not r.get("error"):
            ver = verify_file_download(token, r["session_id"])
            ver["session_name"] = r.get("session_name")
            download_verifications.append(ver)

    # 5.输出最终报告
    print("\n" + "=" * 80)
    print("最终报告")
    print("=" * 80)

    for r in results:
        if r.get("error"):
            print(f"\n[{r.get('session_name', '?')}] ERROR: {r['error']}")
            continue
        print(f"\n[{r['session_name']}]")
        print(f"  会话ID: {r['session_id']}")
        print(f"  耗时: {r['elapsed_seconds']}s")
        print(f"  事件数: {r['events_count']}")
        print(f"  事件类型: {', '.join(r['event_types'])}")
        print(f"  工具调用数: {r['tool_calls_count']}")
        print(f"  工具列表: {r['tool_calls']}")
        print(f"  F10-7 sleep调用: {r['sleep_calls']} (期望=0)")
        print(f"  F10-7 task_wait调用: {r['task_wait_calls']} (期望>=1,若涉及长任务)")
        print(f"  F10-8 过滤日志条目: {r['f10_8_filtered']}")
        print(f"  交付物文件: ")
        for fp in r["deliverables"]:
            print(f"    - {fp}")
        if r["final_message_snippet"]:
            print(f"  最终消息片段: {r['final_message_snippet']}")

    print("\n[文件下载验证]")
    if not download_verifications:
        print("  无文件可下载(可能会话未生成交付物)")
    for ver in download_verifications:
        print(f"\n  [{ver.get('session_name', '?')}]")
        print(f"    会话文件数: {ver.get('files_count', 0)}")
        for dr in ver.get("download_results", []):
            status_flag = ""
            if dr.get("is_500"):
                status_flag = " [BUG:仍500!]"
            elif dr.get("is_422"):
                status_flag = " [预期:未同步422]"
            elif dr.get("download_status") == 200:
                status_flag = " [OK:可下载]"
            print(
                f"    - {dr.get('filename', '?')} (id={dr.get('file_id', '?')[:8]}..., "
                f"sync={dr.get('sync_status')}, status={dr.get('download_status')}){status_flag}"
            )

    # 6.写JSON结果到文件
    output = {
        "results": results,
        "download_verifications": download_verifications,
        "test_completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open("batch17_test_result.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n[结果已保存] batch17_test_result.json")

    # 7.总结
    print("\n" + "=" * 80)
    print("优化预期验证")
    print("=" * 80)
    all_sleep = sum(r.get("sleep_calls", 0) for r in results)
    all_task_wait = sum(r.get("task_wait_calls", 0) for r in results)
    all_deliverables = sum(len(r.get("deliverables", [])) for r in results)
    all_500 = sum(1 for v in download_verifications for dr in v.get("download_results", []) if dr.get("is_500"))
    print(f"  F10-7 sleep调用总数: {all_sleep} (期望=0,根因修复)")
    print(f"  F10-7 task_wait调用总数: {all_task_wait} (期望>=1,异步回调生效)")
    print(f"  交付物总数: {all_deliverables}")
    print(f"  文件下载500错误数: {all_500} (期望=0,500错误已修复)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
