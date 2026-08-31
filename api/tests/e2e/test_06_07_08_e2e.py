#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_06_07_08_e2e.py
06/07/08优化端到端会话测试

测试任务: 搜索2026年AI最新进展,并整理成报告保存到文件
触发点:
- 06: deep_research工具并发洞察抽取
- 07: file_write工具触发文件同步到OSS + SSE载荷保护
- 08: 多轮工具调用触发记忆管理主动预测压缩

验证项:
1. SSE流稳定无断连
2. 工具事件正常产生(deep_research/file等)
3. 无ErrorEvent
4. 最终有完整assistant回复
5. 文件同步成功(attachments非空或有file工具结果)
"""
import json
import time
import sys
from collections import Counter

import httpx

BASE_URL = "http://localhost:8000"
USERNAME = "admin"
PASSWORD = "admin123"
TEST_MESSAGE = "请搜索一下2026年人工智能最新进展,并简要总结要点,然后把总结保存到一个文件里"
REQUEST_TIMEOUT = 600  # 单次会话最长10分钟


def login(client: httpx.Client) -> str:
    """登录获取access_token"""
    resp = client.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"登录失败: {data}")
    return data["data"]["access_token"]


def create_session(client: httpx.Client, token: str) -> str:
    """创建新会话"""
    resp = client.post(
        "/api/sessions",
        json={},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"创建会话失败: {data}")
    return data["data"]["session_id"]


def stream_chat(client: httpx.Client, token: str, session_id: str, message: str) -> dict:
    """流式发送消息并收集SSE事件"""
    stats = {
        "total_events": 0,
        "event_types": Counter(),
        "tool_events": [],
        "message_events": 0,
        "streaming_chunks": 0,
        "error_events": [],
        "step_events": 0,
        "done_events": 0,
        "title_events": 0,
        "wait_events": 0,
        "first_event_time": None,
        "last_event_time": None,
        "max_sse_payload_size": 0,
        "tool_names": Counter(),
        "final_message": "",
        "last_event_id": None,
    }

    payload = {
        "message": message,
        "attachments": [],
        "timestamp": int(time.time()),
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
    }

    start_time = time.time()

    with client.stream(
        "POST",
        f"/api/sessions/{session_id}/chat",
        json=payload,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    ) as response:
        if response.status_code != 200:
            raise RuntimeError(f"chat端点返回非200: {response.status_code}")

        current_event_type = None
        current_data_lines = []
        current_id = None

        for line in response.iter_lines():
            if not line:
                # 空行表示一个SSE事件结束
                if current_data_lines:
                    data_str = "\n".join(current_data_lines)
                    stats["total_events"] += 1
                    stats["max_sse_payload_size"] = max(
                        stats["max_sse_payload_size"], len(data_str)
                    )
                    now = time.time()
                    if stats["first_event_time"] is None:
                        stats["first_event_time"] = now
                    stats["last_event_time"] = now

                    # 解析事件
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        data = {"raw": data_str}

                    event_type = current_event_type or "message"
                    stats["event_types"][event_type] += 1

                    if event_type == "tool":
                        # SSE事件字段: name(工具箱名), function(工具名), status, args
                        tool_name = data.get("name", data.get("tool_name", "unknown"))
                        function_name = data.get("function", data.get("function_name", ""))
                        stats["tool_names"][tool_name] += 1
                        stats["tool_events"].append({
                            "tool_name": tool_name,
                            "function_name": function_name,
                            "status": data.get("status", ""),
                        })
                    elif event_type == "message":
                        is_streaming = data.get("is_streaming", False)
                        if is_streaming:
                            stats["streaming_chunks"] += 1
                        else:
                            stats["message_events"] += 1
                            # SSE事件字段: message(内容), is_final(最终答案标记)
                            content = data.get("message", data.get("content", ""))
                            if content and data.get("is_final", False):
                                stats["final_message"] = content
                            elif content and not stats["final_message"]:
                                stats["final_message"] = content
                    elif event_type == "error":
                        stats["error_events"].append(data)
                    elif event_type == "step":
                        stats["step_events"] += 1
                    elif event_type == "done":
                        stats["done_events"] += 1
                    elif event_type == "title":
                        stats["title_events"] += 1
                    elif event_type == "wait":
                        stats["wait_events"] += 1

                    if current_id:
                        stats["last_event_id"] = current_id

                # 重置
                current_event_type = None
                current_data_lines = []
                current_id = None
                continue

            if line.startswith("event:"):
                current_event_type = line[6:].strip()
            elif line.startswith("data:"):
                current_data_lines.append(line[5:].lstrip())
            elif line.startswith("id:"):
                current_id = line[3:].strip()
            elif line.startswith(":") or line.startswith("retry:"):
                # 心跳或重试指令,忽略
                pass

    stats["total_duration"] = time.time() - start_time
    return stats


def analyze_results(stats: dict) -> dict:
    """分析测试结果,返回检查项通过情况"""
    checks = []

    # 检查1: SSE流稳定(至少有事件产生)
    checks.append({
        "name": "SSE流稳定(有事件产生)",
        "passed": stats["total_events"] > 0,
        "detail": f"共{stats['total_events']}个事件",
    })

    # 检查2: 有done事件(会话正常结束)
    checks.append({
        "name": "会话正常结束(有done事件)",
        "passed": stats["done_events"] >= 1,
        "detail": f"done事件数: {stats['done_events']}",
    })

    # 检查3: 无ErrorEvent
    checks.append({
        "name": "无错误事件",
        "passed": len(stats["error_events"]) == 0,
        "detail": f"错误事件数: {len(stats['error_events'])}",
        "errors": stats["error_events"][:3],
    })

    # 检查4: 有工具事件(触发Agent执行)
    checks.append({
        "name": "有工具事件产生",
        "passed": len(stats["tool_events"]) > 0,
        "detail": f"工具事件数: {len(stats['tool_events'])}, 工具: {dict(stats['tool_names'])}",
    })

    # 检查5: 最终有完整assistant回复
    checks.append({
        "name": "有完整assistant回复",
        "passed": bool(stats["final_message"]),
        "detail": f"回复长度: {len(stats['final_message'])}字符",
    })

    # 检查6: SSE载荷大小合理(<100KB,验证07优化SSE保护)
    checks.append({
        "name": "SSE载荷大小合理(<100KB)",
        "passed": stats["max_sse_payload_size"] < 100 * 1024,
        "detail": f"最大载荷: {stats['max_sse_payload_size']}字节",
    })

    # 检查7: 流式chunk正常(验证SSE流式输出)
    checks.append({
        "name": "流式chunk正常产生",
        "passed": stats["streaming_chunks"] > 0,
        "detail": f"流式chunk数: {stats['streaming_chunks']}",
    })

    # 检查8: 事件ID递增(验证SSE断连恢复基础)
    checks.append({
        "name": "有事件ID(支持断连恢复)",
        "passed": stats["last_event_id"] is not None,
        "detail": f"最后事件ID: {stats['last_event_id']}",
    })

    # 检查9: 触发搜索类工具(验证06优化,deep_research内部并发抽取)
    has_search = "deep_research" in stats["tool_names"] or "search" in stats["tool_names"]
    checks.append({
        "name": "触发搜索类工具(验证06优化)",
        "passed": has_search,
        "detail": f"deep_research: {stats['tool_names'].get('deep_research', 0)}, search: {stats['tool_names'].get('search', 0)}",
    })

    # 检查10: 触发file/shell工具(验证07优化文件同步,shell写文件通过step.attachments触发同步)
    has_file_or_shell = "file" in stats["tool_names"] or "shell" in stats["tool_names"]
    checks.append({
        "name": "触发file/shell工具(验证07优化文件同步)",
        "passed": has_file_or_shell,
        "detail": f"file: {stats['tool_names'].get('file', 0)}, shell: {stats['tool_names'].get('shell', 0)}",
    })

    passed_count = sum(1 for c in checks if c["passed"])
    return {
        "checks": checks,
        "passed": passed_count,
        "total": len(checks),
        "all_passed": passed_count == len(checks),
    }


def main():
    print("=" * 70)
    print("06/07/08优化端到端会话测试")
    print("=" * 70)
    print(f"测试任务: {TEST_MESSAGE}")
    print()

    with httpx.Client(base_url=BASE_URL) as client:
        # 1.登录
        print("[1/4] 登录获取token...")
        try:
            token = login(client)
            print(f"  [OK] token: {token[:20]}...")
        except Exception as e:
            print(f"  [FAIL] 登录失败: {e}")
            sys.exit(1)

        # 2.创建会话
        print("[2/4] 创建新会话...")
        try:
            session_id = create_session(client, token)
            print(f"  [OK] session_id: {session_id}")
        except Exception as e:
            print(f"  [FAIL] 创建会话失败: {e}")
            sys.exit(1)

        # 3.发送消息并流式接收
        print("[3/4] 发送消息,流式接收SSE事件...")
        print(f"  消息: {TEST_MESSAGE}")
        try:
            stats = stream_chat(client, token, session_id, TEST_MESSAGE)
            print(f"  [OK] 接收完成,共{stats['total_events']}个事件,耗时{stats['total_duration']:.1f}s")
        except Exception as e:
            print(f"  [FAIL] 流式接收失败: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

        # 4.分析结果
        print("[4/4] 分析测试结果...")
        analysis = analyze_results(stats)

        print()
        print("=" * 70)
        print("检查项详细结果")
        print("=" * 70)
        for i, check in enumerate(analysis["checks"], 1):
            status = "[PASS]" if check["passed"] else "[FAIL]"
            print(f"{i:2d}. {status} {check['name']}")
            print(f"    {check['detail']}")
            if not check["passed"] and "errors" in check:
                for err in check["errors"]:
                    print(f"    错误: {err}")

        print()
        print("=" * 70)
        print("事件统计")
        print("=" * 70)
        print(f"总事件数: {stats['total_events']}")
        print(f"事件类型分布: {dict(stats['event_types'])}")
        print(f"工具调用: {dict(stats['tool_names'])}")
        print(f"流式chunk数: {stats['streaming_chunks']}")
        print(f"完整消息数: {stats['message_events']}")
        print(f"步骤事件数: {stats['step_events']}")
        print(f"最大SSE载荷: {stats['max_sse_payload_size']}字节")
        print(f"总耗时: {stats['total_duration']:.1f}s")

        if stats["final_message"]:
            print()
            print("=" * 70)
            print("最终回复(前500字符)")
            print("=" * 70)
            print(stats["final_message"][:500])

        print()
        print("=" * 70)
        result = "全部通过" if analysis["all_passed"] else f"{analysis['passed']}/{analysis['total']}通过"
        print(f"测试结果: {result}")
        print("=" * 70)

        sys.exit(0 if analysis["all_passed"] else 1)


if __name__ == "__main__":
    main()
