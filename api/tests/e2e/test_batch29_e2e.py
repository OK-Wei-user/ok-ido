#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_batch29_e2e.py
批次 29 E2E 双会话测试 — 验证 deep_research 工具调用 + 沙箱异步任务通知修复

会话1: 根据26年1-5月份的全部出入库、库存数据,深度分析,用于生产把控和经营参考
  预期: MCP 导出工具调用,失败时注入 async_mode 引导,不陷入同步重试循环

会话2: 深度搜索 2026 年人工智能发展趋势
  预期: deep_research 工具被装配并调用(批次 29 F10-6 前缀匹配修复)
"""
import asyncio
import json
import time
import httpx

API_BASE = "http://localhost:8000/api"
USERNAME = "admin"
PASSWORD = "admin123"

# 会话超时(秒) — 复杂任务可能需要较长时间
SESSION_TIMEOUT = 600


async def login(client: httpx.AsyncClient) -> str:
    """登录获取 access_token"""
    resp = await client.post(
        f"{API_BASE}/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["data"]["access_token"]
    print(f"[LOGIN] 登录成功, token={token[:20]}...")
    return token


async def create_session(client: httpx.AsyncClient, token: str) -> str:
    """创建新会话,返回 session_id"""
    resp = await client.post(
        f"{API_BASE}/sessions",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    data = resp.json()
    session_id = data["data"]["session_id"]
    print(f"[SESSION] 创建会话成功: {session_id}")
    return session_id


async def send_message_and_collect(
    client: httpx.AsyncClient,
    token: str,
    session_id: str,
    message: str,
    label: str,
) -> dict:
    """发送消息并收集 SSE 事件,返回事件统计"""
    events = []
    tool_calls = []
    plan_steps = []
    errors = []
    final_message = ""
    started_at = time.time()

    print(f"\n[{label}] === 开始会话 ===")
    print(f"[{label}] 消息: {message[:80]}...")

    async with client.stream(
        "POST",
        f"{API_BASE}/sessions/{session_id}/chat",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
        json={"message": message, "attachments": []},
        timeout=httpx.Timeout(SESSION_TIMEOUT),
    ) as response:
        event_type = None
        event_data = ""

        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                event_data = line[5:].strip()
            elif line == "" and event_type:
                # 事件边界
                events.append({"event": event_type, "data": event_data[:200]})

                try:
                    data = json.loads(event_data) if event_data else {}
                except (json.JSONDecodeError, ValueError):
                    data = {}

                # 解析事件类型
                if event_type == "plan":
                    steps = data.get("steps", [])
                    if steps:
                        plan_steps = steps
                        print(f"[{label}] [PLAN] {len(steps)} 个步骤")
                        for i, step in enumerate(steps):
                            desc = step.get("description", "")[:60]
                            print(f"[{label}]   步骤{i+1}: {desc}")

                elif event_type == "step":
                    step_desc = data.get("description", "")[:60]
                    status = data.get("status", "")
                    if status == "completed":
                        print(f"[{label}] [STEP-DONE] {step_desc}")

                elif event_type == "tool_call":
                    tool_name = data.get("name", "")
                    tool_calls.append(tool_name)
                    print(f"[{label}] [TOOL] {tool_name}")

                elif event_type == "tool_result":
                    tool_name = data.get("name", "")
                    success = data.get("success", True)
                    msg_snippet = str(data.get("message", ""))[:100]
                    if not success:
                        errors.append(f"{tool_name}: {msg_snippet}")
                        print(f"[{label}] [TOOL-FAIL] {tool_name}: {msg_snippet}")
                    # 检查是否包含 async_mode 引导
                    if "async_mode" in str(data.get("message", "")):
                        print(f"[{label}] [ASYNC-HINT] {tool_name} 返回 async_mode 引导!")

                elif event_type == "message":
                    content = data.get("content", "")
                    is_final = data.get("is_final", False)
                    if is_final:
                        final_message = content[:200]
                        print(f"[{label}] [FINAL] {final_message}")

                elif event_type == "error":
                    error_msg = data.get("message", str(data))[:100]
                    errors.append(f"ERROR: {error_msg}")
                    print(f"[{label}] [ERROR] {error_msg}")

                elif event_type == "ask_user":
                    print(f"[{label}] [ASK-USER] {str(data)[:100]}")

                event_type = None
                event_data = ""

    elapsed = time.time() - started_at
    print(f"\n[{label}] === 会话结束, 耗时 {elapsed:.1f}s ===")
    print(f"[{label}] 事件数: {len(events)}, 工具调用: {len(tool_calls)}, 步骤: {len(plan_steps)}")
    if tool_calls:
        # 统计工具调用次数
        from collections import Counter
        tool_counter = Counter(tool_calls)
        print(f"[{label}] 工具调用统计:")
        for name, count in tool_counter.most_common():
            print(f"[{label}]   {name}: {count}次")

    return {
        "label": label,
        "session_id": session_id,
        "elapsed": elapsed,
        "events": events,
        "tool_calls": tool_calls,
        "plan_steps": plan_steps,
        "errors": errors,
        "final_message": final_message,
    }


async def get_session_detail(client: httpx.AsyncClient, token: str, session_id: str) -> dict:
    """获取会话详情"""
    resp = await client.get(
        f"{API_BASE}/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()["data"]


async def main():
    """主测试函数: 并行发起两个会话"""
    async with httpx.AsyncClient() as client:
        # 1. 登录
        token = await login(client)

        # 2. 创建两个会话
        session1_id = await create_session(client, token)
        session2_id = await create_session(client, token)

        # 3. 并行发送消息
        message1 = "根据26年1-5月份的全部出入库、库存数据，为我深度分析，用于生产把控和经营参考"
        message2 = "深度搜索请为我搜索 2026 年人工智能发展趋势"

        results = await asyncio.gather(
            send_message_and_collect(client, token, session1_id, message1, "会话1-出入库分析"),
            send_message_and_collect(client, token, session2_id, message2, "会话2-深度搜索"),
        )

        # 4. 获取会话详情
        for result in results:
            detail = await get_session_detail(client, token, result["session_id"])
            result["status"] = detail.get("status", "unknown")
            result["title"] = detail.get("title", "")
            print(f"\n[{result['label']}] 会话状态: {result['status']}, 标题: {result['title']}")

        # 5. 架构师分析报告
        print("\n" + "=" * 80)
        print("架构师 E2E 分析报告")
        print("=" * 80)

        for result in results:
            label = result["label"]
            print(f"\n--- {label} ---")
            print(f"  耗时: {result['elapsed']:.1f}s")
            print(f"  状态: {result['status']}")
            print(f"  事件数: {len(result['events'])}")
            print(f"  工具调用: {len(result['tool_calls'])} 次")
            print(f"  步骤数: {len(result['plan_steps'])}")
            print(f"  错误数: {len(result['errors'])}")

            # 关键验证点
            tool_calls = result["tool_calls"]

            if "出入库" in label:
                # 会话1验证: MCP 导出工具调用 + async_mode 引导
                mcp_calls = [t for t in tool_calls if "export" in t.lower() or "Export" in t]
                has_async_hint = any("async_mode" in e.get("data", "")
                                     for e in result["events"] if e["event"] == "tool_result")
                print(f"  [验证] MCP导出工具调用: {len(mcp_calls)} 次")
                print(f"  [验证] async_mode引导触发: {has_async_hint}")
                # 检查是否陷入循环(同一工具调用>10次)
                from collections import Counter
                tool_counter = Counter(tool_calls)
                loop_detected = any(count > 10 for count in tool_counter.values())
                print(f"  [验证] 工具调用循环检测: {'存在!' if loop_detected else '正常'}")

            if "深度搜索" in label:
                # 会话2验证: deep_research 工具被调用
                has_deep_research = "deep_research" in tool_calls
                search_count = sum(1 for t in tool_calls if "search" in t.lower())
                print(f"  [验证] deep_research工具调用: {'是 ✓' if has_deep_research else '否 ✗'}")
                print(f"  [验证] search_web调用次数: {search_count}")
                if has_deep_research:
                    print(f"  [结论] 批次29 F10-6 修复生效: deep_research 已被装配并调用")
                else:
                    print(f"  [结论] 批次29 F10-6 修复未生效: deep_research 未被调用")

            if result["errors"]:
                print(f"  错误列表:")
                for err in result["errors"][:5]:
                    print(f"    - {err[:120]}")

        # 保存结果
        output = {
            "session1": {k: v for k, v in results[0].items() if k != "events"},
            "session2": {k: v for k, v in results[1].items() if k != "events"},
        }
        with open("batch29_e2e_result.json", "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n结果已保存到 batch29_e2e_result.json")


if __name__ == "__main__":
    asyncio.run(main())
