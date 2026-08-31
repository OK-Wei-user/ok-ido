#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Batch 39 E2E 测试: 2个并发会话验证4方向优化

会话1: 出入库深度分析(方向2 data_analysis 任务类型 + 方向4 shell合并引导)
会话2: 深度搜索AI趋势(方向2 research 任务类型 + 方向1 异步轮询)
"""
import asyncio
import json
import time
import sys
import httpx

BASE_URL = "http://localhost:8000/api"
USERNAME = "admin"
PASSWORD = "admin123"
MAX_WAIT = 300  # 单会话最大等待秒数


async def login(client: httpx.AsyncClient) -> str:
    """登录获取 token"""
    resp = await client.post(f"{BASE_URL}/auth/login", json={
        "username": USERNAME,
        "password": PASSWORD,
    })
    resp.raise_for_status()
    data = resp.json()
    return data["data"]["access_token"]


async def create_session(client: httpx.AsyncClient, token: str, title: str) -> str:
    """创建会话"""
    resp = await client.post(
        f"{BASE_URL}/sessions",
        json={"title": title},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()["data"]["session_id"]


async def chat_and_collect(
        client: httpx.AsyncClient,
        token: str,
        session_id: str,
        message: str,
        label: str,
) -> dict:
    """发送聊天消息并收集SSE事件"""
    events = []
    step_events = []
    tool_events = []
    message_events = []
    error_events = []
    start_time = time.time()

    print(f"\n[{label}] 会话[{session_id[:8]}] 开始: {message[:50]}...")

    async with client.stream(
        "POST",
        f"{BASE_URL}/sessions/{session_id}/chat",
        json={"message": message},
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(MAX_WAIT),
    ) as response:
        event_type = None
        event_data = ""

        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                event_data = line[5:].strip()
                if event_data:
                    try:
                        data = json.loads(event_data)
                        events.append({"type": event_type, "data": data})

                        if event_type == "step":
                            status = data.get("status", "")
                            if status == "STARTED":
                                step_events.append({
                                    "step_id": data.get("step", {}).get("id"),
                                    "desc": data.get("step", {}).get("description", "")[:80],
                                })
                                print(f"  [{label}] 步骤开始: {data.get('step', {}).get('description', '')[:60]}")
                            elif status == "COMPLETED":
                                progress = data.get("progress", 0)
                                print(f"  [{label}] 步骤完成 (progress={progress})")
                        elif event_type == "tool":
                            tool_name = data.get("tool_name", "")
                            fn_name = data.get("function_name", "")
                            status = data.get("status", "")
                            if status == "CALLING":
                                tool_events.append({"tool": tool_name, "fn": fn_name})
                                print(f"  [{label}] 工具调用: {fn_name}")
                        elif event_type == "message":
                            msg = data.get("message", "")
                            is_final = data.get("is_final", False)
                            if is_final:
                                message_events.append(msg)
                                print(f"  [{label}] 最终消息: {msg[:100]}...")
                        elif event_type == "error":
                            error_events.append(data.get("error", ""))
                            print(f"  [{label}] 错误: {data.get('error', '')[:100]}")
                        elif event_type == "done":
                            print(f"  [{label}] 会话完成")
                    except json.JSONDecodeError:
                        pass
                event_type = None
                event_data = ""

    elapsed = time.time() - start_time
    return {
        "label": label,
        "session_id": session_id,
        "elapsed": round(elapsed, 1),
        "total_events": len(events),
        "step_count": len(step_events),
        "tool_call_count": len(tool_events),
        "message_count": len(message_events),
        "error_count": len(error_events),
        "steps": step_events,
        "tools": tool_events,
        "messages": message_events,
        "errors": error_events,
    }


async def main():
    async with httpx.AsyncClient() as client:
        # 1.登录
        print("正在登录...")
        token = await login(client)
        print(f"登录成功, token: {token[:20]}...")

        # 2.创建2个会话
        session1 = await create_session(client, token, "出入库深度分析")
        session2 = await create_session(client, token, "AI趋势深度搜索")
        print(f"会话1[{session1[:8]}]: 出入库深度分析")
        print(f"会话2[{session2[:8]}]: AI趋势深度搜索")

        # 3.并发发送消息
        msg1 = "根据26年1-5月份的全部出入库、为我深度分析，用于生产把控和经营参考"
        msg2 = "深度搜索请为我搜索 2026 年人工智能发展趋势"

        results = await asyncio.gather(
            chat_and_collect(client, token, session1, msg1, "出入库"),
            chat_and_collect(client, token, session2, msg2, "深度搜索"),
        )

        # 4.输出分析报告
        print("\n" + "=" * 80)
        print("E2E 测试结果分析报告")
        print("=" * 80)

        for r in results:
            print(f"\n--- {r['label']} ---")
            print(f"  耗时: {r['elapsed']}s")
            print(f"  事件总数: {r['total_events']}")
            print(f"  步骤数: {r['step_count']}")
            print(f"  工具调用数: {r['tool_call_count']}")
            print(f"  最终消息数: {r['message_count']}")
            print(f"  错误数: {r['error_count']}")
            if r["tools"]:
                tool_names = [t["fn"] for t in r["tools"]]
                shell_count = sum(1 for t in tool_names if t == "shell_execute")
                search_count = sum(1 for t in tool_names if t == "search_web")
                research_count = sum(1 for t in tool_names if t == "deep_research")
                browser_count = sum(1 for t in tool_names if t.startswith("browser_"))
                print(f"  工具分布: shell_execute={shell_count}, search_web={search_count}, deep_research={research_count}, browser={browser_count}")
            if r["errors"]:
                print(f"  错误详情: {r['errors'][:3]}")

        # 5.架构师分析
        print("\n" + "=" * 80)
        print("架构师分析")
        print("=" * 80)

        for r in results:
            print(f"\n[{r['label']}]")
            if r["error_count"] > 0:
                print(f"  ⚠️ 存在 {r['error_count']} 个错误")
            if r["message_count"] == 0:
                print(f"  ⚠️ 无最终消息输出")
            else:
                print(f"  ✅ 有最终消息输出")

            # 方向4验证: shell_execute 调用次数
            tool_names = [t["fn"] for t in r["tools"]]
            shell_count = sum(1 for t in tool_names if t == "shell_execute")
            if shell_count > 0:
                print(f"  shell_execute 调用 {shell_count} 次(方向4: 观测合并引导效果)")

            # 方向2验证: 任务类型识别
            if "出入库" in r["label"]:
                print(f"  任务类型: data_analysis(方向2: 应上调 search_web 预算)")
            elif "深度搜索" in r["label"]:
                print(f"  任务类型: research(方向2: 应上调 deep_research 预算)")
                research_count = sum(1 for t in tool_names if t == "deep_research")
                if research_count > 0:
                    print(f"  ✅ deep_research 被调用 {research_count} 次(研究类任务优先 deep_research)")

        print("\n" + "=" * 80)
        print("E2E 测试完成")
        print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
