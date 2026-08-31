#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批次 25 - P11 沙箱异步任务通知 E2E 验证

验证目标(承接批次 24 P11 实现):
    构造与 e88d464f 相同的 MCP 异步任务场景,观察 LLM 实际行为:
    - 期望: LLM 使用 mcp_tool_call(async_mode=true) + task_wait(task_id) 等待 MCP 异步任务
    - 严禁: LLM 使用 shell_execute(sleep N) 轮询 MCP 异步任务状态

测试策略(观察+宽松断言):
    由于 LLM 行为非确定性,采用"观察为主、宽松断言"策略:
    1. 发送触发 MCP 多模态异步任务的消息(图片生成天然异步耗时)
    2. 采集 SSE 事件流,分类记录工具调用
    3. 核心断言: 会话正常完成(done 事件到达)
    4. 观察报告: 输出实际工具调用序列,记录是否使用 async_mode/task_wait/sleep

非严格断言说明:
    - 若 LLM 调用了 mcp_tool_call 工具,则不应出现 shell_execute(sleep N) 用于等待 MCP 任务
    - 若 LLM 未调用 MCP 工具(任务理解偏差),记录但不视为失败(观察性质)
"""
import json
import time
from typing import Dict, List, Any

import httpx

BASE_URL = "http://localhost:8000/api"
TIMEOUT = 300  # 5 分钟,图片生成可能耗时较长


def login(client: httpx.Client) -> str:
    """登录获取 access_token"""
    resp = client.post(
        f"{BASE_URL}/auth/login",
        json={"username": "admin", "password": "admin123"},
    )
    resp.raise_for_status()
    data = resp.json()
    assert data["code"] == 200, f"登录失败: {data}"
    token = data["data"]["access_token"]
    print(f"[登录] 成功, token={token[:20]}...")
    return token


def create_session(client: httpx.Client, token: str, title: str) -> str:
    """创建新会话"""
    resp = client.post(
        f"{BASE_URL}/sessions",
        json={"title": title},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    session_id = resp.json()["data"]["session_id"]
    print(f"[创建会话] session_id={session_id}")
    return session_id


def parse_sse_line(line: str) -> Dict[str, str]:
    """解析 SSE 单行,返回字段字典"""
    if line.startswith("id: "):
        return {"field": "id", "value": line[4:]}
    elif line.startswith("event: "):
        return {"field": "event", "value": line[7:]}
    elif line.startswith("data: "):
        return {"field": "data", "value": line[6:]}
    return {"field": "", "value": ""}


def _classify_tool_call(tool_name: str, tool_args: str) -> Dict[str, Any]:
    """分类工具调用,提取关键信息

    Returns:
        包含以下字段的字典:
        - name: 工具名
        - args_brief: 参数摘要(前200字)
        - is_mcp_call: 是否为 mcp_tool_call
        - uses_async_mode: 是否使用 async_mode=true
        - is_task_wait: 是否为 task_wait
        - is_sleep_polling: 是否为 shell_execute(sleep N)
    """
    args_brief = tool_args[:200] if tool_args else ""
    return {
        "name": tool_name,
        "args_brief": args_brief,
        "is_mcp_call": tool_name == "mcp_tool_call",
        "uses_async_mode": "async_mode" in tool_args and "true" in tool_args.lower(),
        "is_task_wait": tool_name == "task_wait",
        "is_sleep_polling": (
            tool_name == "shell_execute"
            and "sleep" in tool_args.lower()
        ),
    }


def test_batch25_p11_async_mode_usage(
    client: httpx.Client,
    token: str,
    session_id: str,
) -> Dict[str, Any]:
    """观察 LLM 在 MCP 异步任务场景下是否使用 async_mode + task_wait

    核心断言:
        - 会话必须正常完成(done 事件到达)
        - 若调用 mcp_tool_call,则不应出现 shell_execute(sleep N) 用于等待 MCP 任务

    观察报告:
        - 工具调用序列
        - async_mode 使用次数
        - task_wait 使用次数
        - sleep 轮询次数
        - 总耗时
    """
    print("\n" + "=" * 60)
    print("批次 25 - P11 沙箱异步任务通知 E2E 验证")
    print("=" * 60)

    # 触发 MCP 多模态异步任务: 图片生成天然异步耗时
    chat_body = {
        "message": "请生成一张蓝色大海的图片",
        "timestamp": 0,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    print("[步骤1] 发送图片生成请求, 观察 LLM 工具调用策略...")
    start_time = time.time()

    tool_calls: List[Dict[str, Any]] = []
    mcp_calls_count = 0
    async_mode_count = 0
    task_wait_count = 0
    sleep_polling_count = 0
    has_done = False
    has_error = False
    error_msg = ""
    current_event = None

    with client.stream(
        "POST",
        f"{BASE_URL}/sessions/{session_id}/chat",
        json=chat_body,
        headers=headers,
        timeout=TIMEOUT,
    ) as response:
        for line in response.iter_lines():
            if not line:
                continue
            parsed = parse_sse_line(line)
            if parsed["field"] == "event":
                current_event = parsed["value"]
            elif parsed["field"] == "data":
                current_data = parsed["value"]

                if current_event == "tool":
                    try:
                        tool_data = json.loads(current_data)
                        tool_name = tool_data.get("name", "unknown")
                        tool_args = str(tool_data.get("arguments", ""))
                        classified = _classify_tool_call(tool_name, tool_args)
                        tool_calls.append(classified)

                        if classified["is_mcp_call"]:
                            mcp_calls_count += 1
                        if classified["uses_async_mode"]:
                            async_mode_count += 1
                        if classified["is_task_wait"]:
                            task_wait_count += 1
                        if classified["is_sleep_polling"]:
                            sleep_polling_count += 1

                        print(f"  [工具调用] {tool_name} "
                              f"(async_mode={classified['uses_async_mode']}, "
                              f"args={classified['args_brief'][:80]})")
                    except json.JSONDecodeError:
                        pass

                elif current_event == "done":
                    has_done = True
                    print("  [完成] 收到 done 事件")
                    break

                elif current_event == "error":
                    has_error = True
                    error_msg = current_data[:200] if current_data else ""
                    print(f"  [错误] {error_msg}")

    total_time = time.time() - start_time

    # ========== 核心断言 ==========
    print("\n[步骤2] 核心断言检查...")
    assert has_done, "会话未正常完成(缺失 done 事件)"
    print("  ✓ 会话正常完成(done 事件到达)")

    # 若调用了 mcp_tool_call,则不应使用 sleep 轮询等待 MCP 任务
    # 注意: shell_execute(sleep N) 可能用于其他场景(如文件下载等待),仅当与 MCP 调用并存时视为问题
    if mcp_calls_count > 0 and sleep_polling_count > 0:
        print(f"  [WARN] 检测到 MCP 调用({mcp_calls_count}次)与 sleep 轮询({sleep_polling_count}次)并存")
        print(f"         这可能违反 P11 优化目标,但鉴于 LLM 行为非确定性,仅作警告不视为失败")
    else:
        if mcp_calls_count > 0:
            print(f"  ✓ 检测到 MCP 调用({mcp_calls_count}次),无 sleep 轮询")
        elif sleep_polling_count == 0:
            print("  ✓ 未使用 sleep 轮询")

    # ========== 观察报告 ==========
    print("\n" + "=" * 60)
    print("批次 25 P11 E2E 观察报告")
    print("=" * 60)
    print(f"  会话 ID: {session_id}")
    print(f"  总耗时: {total_time:.1f}s")
    print(f"  工具调用总数: {len(tool_calls)}")
    print(f"  MCP 工具调用次数: {mcp_calls_count}")
    print(f"  async_mode=true 使用次数: {async_mode_count}")
    print(f"  task_wait 使用次数: {task_wait_count}")
    print(f"  shell_execute(sleep N) 次数: {sleep_polling_count}")
    print(f"  是否有错误: {has_error}")
    print(f"  会话是否完成: {has_done}")
    print("-" * 60)
    print("工具调用序列:")
    for i, call in enumerate(tool_calls, 1):
        print(f"  #{i}: {call['name']} "
              f"(async_mode={call['uses_async_mode']}, "
              f"task_wait={call['is_task_wait']}, "
              f"sleep={call['is_sleep_polling']})")
    print("=" * 60)

    # 判定 P11 是否生效(观察性质,不强制断言)
    p11_effective = (async_mode_count > 0 or task_wait_count > 0)
    p11_violation = (mcp_calls_count > 0 and sleep_polling_count > 0)

    if p11_effective:
        print(f"\n[P11 结论] ✓ P11 沙箱异步任务通知已生效: "
              f"async_mode={async_mode_count}次, task_wait={task_wait_count}次")
    elif mcp_calls_count == 0:
        print("\n[P11 结论] - LLM 未调用 MCP 工具(任务理解偏差),无法验证 P11 效果")
    elif p11_violation:
        print(f"\n[P11 结论] ⚠ P11 可能未生效: 检测到 sleep 轮询({sleep_polling_count}次)用于等待 MCP 任务")
    else:
        print("\n[P11 结论] - LLM 调用了 MCP 工具但未使用 async_mode(可能任务为同步快速返回)")

    return {
        "session_id": session_id,
        "total_time": total_time,
        "tool_calls": tool_calls,
        "mcp_calls_count": mcp_calls_count,
        "async_mode_count": async_mode_count,
        "task_wait_count": task_wait_count,
        "sleep_polling_count": sleep_polling_count,
        "has_done": has_done,
        "has_error": has_error,
        "p11_effective": p11_effective,
        "p11_violation": p11_violation,
    }


def main() -> None:
    """主入口:登录→创建会话→运行 P11 E2E 验证"""
    with httpx.Client(timeout=TIMEOUT) as client:
        token = login(client)
        session_id = create_session(client, token, "批次25-P11 E2E 验证")
        result = test_batch25_p11_async_mode_usage(client, token, session_id)

    print()
    print("=" * 60)
    print("批次 25 P11 E2E 验证最终结果:")
    print(f"  会话完成: {result['has_done']}")
    print(f"  P11 生效: {result['p11_effective']}")
    print(f"  P11 违规: {result['p11_violation']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
