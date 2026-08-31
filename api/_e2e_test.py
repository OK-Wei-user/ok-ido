#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""浏览器操作会话E2E测试 - 验证Fix1(自适应content预算)+Fix2(视口优先)效果"""
import json
import sys
import time
import requests

API_BASE = "http://localhost:8000/api"
TIMEOUT = 600


def main():
    task_message = (
        "请使用浏览器完成以下操作：\n"
        "1. 打开 https://element-plus.org/zh-CN/component/overview\n"
        "2. 点击【Form 表单】菜单进入Form表单页面\n"
        "3. 下滑到【对齐方式】区域\n"
        "4. 将 Form Align 调整为 Left\n"
        "5. 在name输入框输入杰瑞"
    )

    # 登录
    resp = requests.post(f"{API_BASE}/auth/login", json={"username": "admin", "password": "admin123"}, timeout=30)
    token = resp.json()["data"]["access_token"]
    print(f"[登录成功] user=admin")

    # 创建会话
    resp = requests.post(f"{API_BASE}/sessions", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    session_id = resp.json()["data"]["session_id"]
    print(f"[会话创建] session_id={session_id}")

    # 发送任务并消费SSE流
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream", "Content-Type": "application/json; charset=utf-8"}
    print(f"\n[发送任务] {task_message}")
    print("=" * 80)

    start_time = time.time()
    event_count = 0
    has_omitted = False
    has_compressed = False

    resp = requests.post(f"{API_BASE}/sessions/{session_id}/chat", headers=headers, json={"message": task_message}, stream=True, timeout=TIMEOUT)
    resp.raise_for_status()

    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        raw = line.split(":", 1)[1].strip()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        event_count += 1
        content_str = json.dumps(data, ensure_ascii=False)
        if "more visible elements omitted" in content_str:
            has_omitted = True
            print(f"  [⚠发现visible elements omitted]")
        if "被压缩" in content_str:
            has_compressed = True
            print(f"  [⚠发现'被压缩'反馈]")
        evt_type = data.get("event_type") or data.get("type") or ""
        if evt_type in ("completed", "session_completed") or "session_end" in content_str:
            break

    elapsed = time.time() - start_time
    print("=" * 80)
    print(f"[耗时] {elapsed:.1f}s | [事件数] {event_count}")
    print(f"[omitted检测] {'⚠发现(优化未生效!)' if has_omitted else '✓未发现(优化生效)'}")
    print(f"[被压缩检测] {'⚠发现(content仍被压缩)' if has_compressed else '✓未发现(content预算充足)'}")
    print(f"\n[会话URL] http://10.235.127.227:3000/sessions/{session_id}")

    # 分析会话详情
    print("\n" + "=" * 80)
    print("[分析会话详情]")
    resp = requests.get(f"{API_BASE}/sessions/{session_id}", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    sdata = resp.json()["data"]
    print(f"状态: {sdata.get('status', '')}")
    events = sdata.get("events", [])
    print(f"事件数: {len(events)}")

    thinking_count = 0
    tool_count = 0
    console_exec_count = 0
    text_locator_count = 0
    compressed_count = 0
    final_msg = ""

    for e in events:
        edata = e.get("data", {})
        evt = e.get("event", "")
        if edata.get("is_thinking"):
            thinking_count += 1
            msg = edata.get("message", "")
            if "被压缩" in msg:
                compressed_count += 1
        if evt == "tool":
            tool_count += 1
            func = edata.get("function", "")
            args_str = json.dumps(edata.get("args", {}), ensure_ascii=False)
            if "console_exec" in func:
                console_exec_count += 1
            if "text_locator" in args_str:
                text_locator_count += 1
        if edata.get("is_final") and edata.get("role") == "assistant":
            final_msg = edata.get("message", "")

    print(f"深度思考段数: {thinking_count}")
    print(f"工具调用数: {tool_count}")
    print(f"console_exec调用: {console_exec_count} (旧会话437cbc75为69次)")
    print(f"text_locator使用: {text_locator_count} (旧会话437cbc75为19次)")
    print(f"'被压缩'反馈: {compressed_count} (旧会话437cbc75为14次)")
    print(f"\n[最终回复]\n{final_msg[:600] if final_msg else '(无)'}")


if __name__ == "__main__":
    main()
