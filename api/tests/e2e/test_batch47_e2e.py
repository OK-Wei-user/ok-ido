#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Batch 47 E2E 测试: MCP工具调用效率优化验证

验证点:
1. 工具名已知时跳过search(零冗余search)
2. 同一工具最多describe一次(零重复describe)
3. 轮询参数固定(不交替status=0/1)
4. 交付物完整
"""
import asyncio
import httpx
import json
import time
import sys
from collections import Counter

BASE_URL = "http://localhost:8000/api"
MAX_WAIT = 1800  # 30分钟


async def login(client):
    resp = await client.post(f"{BASE_URL}/auth/login", json={
        "username": "admin",
        "password": "admin123",
    })
    return resp.json()["data"]["access_token"]


async def create_session(client, token, title):
    resp = await client.post(
        f"{BASE_URL}/sessions",
        json={"title": title},
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.json()["data"]["session_id"]


async def send_message(client, token, session_id, message):
    """发送消息并等待完成,收集所有事件"""
    events = []
    start_time = time.time()
    print(f"\n[{session_id[:8]}] 发送: {message[:50]}...")

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
                        events.append({"event": event_type, "data": data})

                        # 打印关键事件
                        if event_type == "plan":
                            steps = data.get("steps", [])
                            if steps:
                                print(f"  [计划] {len(steps)}个步骤")
                                for s in steps:
                                    desc = s.get("description", "")
                                    print(f"    步骤{s.get('id','?')}: {desc[:80]}")
                        elif event_type == "tool":
                            func = data.get("function", "")
                            status = data.get("status", "")
                            if status == "called":
                                args = data.get("args", {})
                                if func == "mcp_tool_search":
                                    # 向后兼容:桥接模式
                                    print(f"  [SEARCH] {args.get('query','')}")
                                elif func == "mcp_tool_describe":
                                    print(f"  [DESCRIBE] {args.get('name','')}")
                                elif func == "mcp_tool_call":
                                    name = args.get("name", "")
                                    arguments = args.get("arguments", {})
                                    print(f"  [CALL] {name}({json.dumps(arguments, ensure_ascii=False)[:60]})")
                                elif func.startswith("mcp_"):
                                    # 直接加载模式: func本身就是mcp_*工具名
                                    arguments = args or {}
                                    print(f"  [MCP] {func}({json.dumps(arguments, ensure_ascii=False)[:60]})")
                        elif event_type == "step":
                            status = data.get("status", "")
                            if status == "started":
                                print(f"  [步骤开始] {data.get('description','')[:60]}")
                            elif status == "completed":
                                print(f"  [步骤完成] {data.get('description','')[:60]}")
                        elif event_type == "done":
                            print(f"  [完成]")

                    except json.JSONDecodeError:
                        pass
                    event_data = ""

            if time.time() - start_time > MAX_WAIT:
                print(f"  [超时] 已等待{time.time()-start_time:.0f}s")
                break

    elapsed = time.time() - start_time
    print(f"[{session_id[:8]}] 结束,耗时{elapsed:.0f}s,事件{len(events)}个")
    return events, elapsed


async def get_session_detail(client, token, session_id):
    resp = await client.get(
        f"{BASE_URL}/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.json().get("data", {})


async def get_files(client, token, session_id):
    resp = await client.get(
        f"{BASE_URL}/sessions/{session_id}/files",
        headers={"Authorization": f"Bearer {token}"},
    )
    data = resp.json().get("data", {})
    return data.get("files", []) if isinstance(data, dict) else []


def analyze_mcp_efficiency(events):
    """分析MCP工具调用效率(兼容直接加载模式+桥接模式)"""
    mcp_search = []
    mcp_describe = []
    mcp_call = []
    # 直接加载模式: func本身是mcp_*工具名
    mcp_direct_calls = []

    for evt in events:
        if evt.get("event") != "tool":
            continue
        data = evt.get("data", {})
        func = data.get("function", "")
        status = data.get("status", "")
        if status != "called":
            continue

        if func == "mcp_tool_search":
            query = data.get("args", {}).get("query", "")
            mcp_search.append(query)
        elif func == "mcp_tool_describe":
            name = data.get("args", {}).get("name", "")
            mcp_describe.append(name)
        elif func == "mcp_tool_call":
            name = data.get("args", {}).get("name", "")
            arguments = data.get("args", {}).get("arguments", {})
            mcp_call.append({"name": name, "arguments": arguments})
        elif func.startswith("mcp_"):
            # 直接加载模式: 工具调用记录到 direct_calls
            arguments = data.get("args", {}) or {}
            mcp_direct_calls.append({"name": func, "arguments": arguments})
            # 同步到 mcp_call,保证现有统计逻辑(如getDownloadTaskList轮询)可用
            mcp_call.append({"name": func, "arguments": arguments})

    # 检查冗余search: 工具名已知(mcp_开头)却搜索
    redundant_search = [q for q in mcp_search if q.startswith("mcp_")]

    # 检查重复describe
    describe_counter = Counter(mcp_describe)
    duplicate_describes = {name: count for name, count in describe_counter.items() if count > 1}

    # 检查轮询状态切换
    status_values = []
    for call in mcp_call:
        if "getDownloadTaskList" in call["name"] or "getDownloadTask" in call["name"]:
            status = call["arguments"].get("status", "")
            if status:
                status_values.append(status)

    status_switches = 0
    for i in range(1, len(status_values)):
        if status_values[i] != status_values[i-1]:
            status_switches += 1

    return {
        "search_count": len(mcp_search),
        "describe_count": len(mcp_describe),
        "call_count": len(mcp_call),
        "direct_call_count": len(mcp_direct_calls),
        "redundant_search": redundant_search,
        "duplicate_describes": duplicate_describes,
        "status_values": status_values,
        "status_switches": status_switches,
    }


async def run_session_test(client, token, title, message):
    """运行单个会话测试"""
    session_id = await create_session(client, token, title)
    events, elapsed = await send_message(client, token, session_id, message)

    detail = await get_session_detail(client, token, session_id)
    files = await get_files(client, token, session_id)

    analysis = analyze_mcp_efficiency(events)

    return {
        "session_id": session_id,
        "status": detail.get("status", "unknown"),
        "elapsed": elapsed,
        "files_count": len(files),
        "files": [f.get("filename", "?") if isinstance(f, dict) else str(f) for f in files],
        "analysis": analysis,
        "events_count": len(events),
    }


async def main():
    print("=" * 70)
    print("Batch 47 E2E 测试: MCP工具调用效率优化验证")
    print("=" * 70)

    async with httpx.AsyncClient() as client:
        token = await login(client)
        print(f"登录成功")

        # 顺序发起两个会话(避免资源竞争)
        print("\n" + "=" * 70)
        print("会话1: 26年1-5月出入库深度分析")
        print("=" * 70)
        result1 = await run_session_test(
            client, token,
            "Batch47出入库分析测试",
            "根据26年1-5月份的全部出入库、为我深度分析，用于生产把控和经营参考"
        )

        print("\n" + "=" * 70)
        print("会话2: 26年AI发展趋势分析")
        print("=" * 70)
        result2 = await run_session_test(
            client, token,
            "Batch47 AI趋势分析测试",
            "为我分析26年ai发展趋势"
        )

    # 综合评估
    print("\n" + "=" * 70)
    print("综合评估")
    print("=" * 70)

    for i, result in enumerate([result1, result2], 1):
        print(f"\n会话{i}: {result['session_id'][:8]}")
        print(f"  状态: {result['status']}")
        print(f"  耗时: {result['elapsed']:.0f}s")
        print(f"  事件: {result['events_count']}个")
        print(f"  文件: {result['files_count']}个")

        a = result["analysis"]
        print(f"  MCP调用: search={a['search_count']}, describe={a['describe_count']}, call={a['call_count']}")

        # 检查冗余search
        if a["redundant_search"]:
            print(f"  ❌ 冗余SEARCH({len(a['redundant_search'])}次): {a['redundant_search']}")
        else:
            print(f"  ✅ 无冗余SEARCH")

        # 检查重复describe
        if a["duplicate_describes"]:
            print(f"  ❌ 重复DESCRIBE: {a['duplicate_describes']}")
        else:
            print(f"  ✅ 无重复DESCRIBE")

        # 检查轮询状态切换
        if a["status_switches"] > 0:
            print(f"  ❌ 轮询状态切换({a['status_switches']}次): {a['status_values']}")
        else:
            print(f"  ✅ 无轮询状态切换")

    # 总结
    print("\n" + "=" * 70)
    all_pass = True
    for i, result in enumerate([result1, result2], 1):
        a = result["analysis"]
        checks = [
            ("无冗余SEARCH", len(a["redundant_search"]) == 0),
            ("无重复DESCRIBE", len(a["duplicate_describes"]) == 0),
            ("无轮询状态切换", a["status_switches"] == 0),
            ("会话完成", result["status"] == "completed"),
            ("有交付物", result["files_count"] > 0),
        ]
        for check_name, passed in checks:
            if not passed:
                all_pass = False
                print(f"  会话{i} ❌ {check_name}")

    if all_pass:
        print("\n🎉 Batch 47 优化验证全部通过!")
        return 0
    else:
        print("\n⚠️ 部分验证项未通过")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
