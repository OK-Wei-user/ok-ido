#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批次 28 架构师 E2E 验证: 双会话场景 + 续接会话回归

会话 1: 出入库/库存数据分析(无附件场景) — 验证 LLM 处理无附件 + 续接会话回归
    - 期望: LLM 通过 ask_user 工具请求用户上传数据文件,而非生成"读取附件"步骤
    - 续接会话回归: 当 LLM ask_user 后,用户发"继续"消息,验证不触发 AttributeError
    - 历史 Bug: 'PlannerAgent' object has no attribute '_uow' at planner_react.py:433
      (修复: 改用 agent._uow_factory() 创建临时 uow)

会话 2: 深度搜索 2026 年 AI 发展趋势(deep_research 场景) — 验证 deep_research 工具调用
    - 期望: LLM 调用 deep_research 工具,完成深度搜索后生成结构化报告
    - 同时验证: deep_research 工具预算控制(2 次/会话)、流式输出、文件交付

验证项:
1. SSE 事件流完整(plan/step/tool/done)
2. 工具调用记录可追溯
3. 会话最终状态合理(completed / waiting_for_user_input)
4. 续接会话不触发 'PlannerAgent' object has no attribute '_uow' 错误
5. 批次 24-27 优化点不被回归
"""
import json
import time
import sys
import os
import httpx

BASE_URL = "http://localhost:8000/api"
TIMEOUT = 600  # 单会话最大 10 分钟


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
    data = resp.json()
    session_id = data["data"]["session_id"]
    print(f"[创建会话] session_id={session_id}, title={title}")
    return session_id


def get_session_detail(client: httpx.Client, token: str, session_id: str) -> dict:
    """获取会话详情(含全部事件)"""
    resp = client.get(
        f"{BASE_URL}/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()["data"]


def parse_sse_line(line: str) -> dict:
    """解析 SSE 单行"""
    if line.startswith("id: "):
        return {"field": "id", "value": line[4:]}
    elif line.startswith("event: "):
        return {"field": "event", "value": line[7:]}
    elif line.startswith("data: "):
        return {"field": "data", "value": line[6:]}
    return {"field": "", "value": ""}


def run_session_chat(client: httpx.Client, token: str, session_id: str, message: str,
                     label: str, follow_up_message: str = None) -> dict:
    """发起会话聊天并收集事件统计

    Args:
        follow_up_message: 续接会话消息(如"继续"),当首轮 ask_user 触发且会话状态变为
            waiting 时自动发送,用于验证续接会话回归 (批次 28 修复)

    Returns:
        统计字典
    """
    print(f"\n{'=' * 60}")
    print(f"[{label}] 发送消息: {message[:80]}...")
    print(f"{'=' * 60}")

    stats = _run_chat_stream(client, token, session_id, message, label)

    # 续接会话回归测试: 如果首轮触发 ask_user 且会话状态为 waiting,自动发送 follow_up_message
    if follow_up_message:
        detail = get_session_detail(client, token, session_id)
        if detail["status"] in ("waiting", "waiting_for_user_input"):
            print(f"\n[{label}-续接] 检测到 ask_user,发送续接消息: {follow_up_message}")
            print(f"{'=' * 60}")
            follow_up_stats = _run_chat_stream(
                client, token, session_id, follow_up_message, f"{label}-续接"
            )
            # 合并续接会话统计
            stats["events_received"] += follow_up_stats["events_received"]
            stats["plan_updated_count"] += follow_up_stats["plan_updated_count"]
            stats["step_started_count"] += follow_up_stats["step_started_count"]
            stats["step_completed_count"] += follow_up_stats["step_completed_count"]
            stats["step_failed_count"] += follow_up_stats["step_failed_count"]
            stats["tool_call_count"] += follow_up_stats["tool_call_count"]
            stats["tool_calls_detail"].extend(follow_up_stats["tool_calls_detail"])
            stats["ask_user_triggered"] = (
                stats["ask_user_triggered"] or follow_up_stats["ask_user_triggered"]
            )
            stats["deep_research_triggered"] = (
                stats["deep_research_triggered"] or follow_up_stats["deep_research_triggered"]
            )
            stats["search_web_triggered"] = (
                stats["search_web_triggered"] or follow_up_stats["search_web_triggered"]
            )
            stats["deliverable_files"].extend(follow_up_stats["deliverable_files"])
            stats["messages"].extend(follow_up_stats["messages"])
            stats["follow_up_triggered"] = True
            stats["total_time"] += follow_up_stats["total_time"]
            # 检查续接会话是否有 _uow 错误
            stats["has_uow_error"] = follow_up_stats.get("has_uow_error", False)
            # 重新查询最终状态
            detail = get_session_detail(client, token, session_id)
            stats["final_status"] = detail["status"]
            print(f"  [续接完成] 最终会话状态: {stats['final_status']}")
        else:
            print(f"\n[{label}-续接] 跳过,首轮状态: {detail['status']} (未触发 ask_user)")
            stats["follow_up_triggered"] = False

    return stats


def _run_chat_stream(client: httpx.Client, token: str, session_id: str, message: str,
                     label: str) -> dict:
    """执行单轮 SSE 流式聊天"""
    chat_body = {"message": message, "timestamp": 0}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    stats = {
        "events_received": 0,
        "plan_created_count": 0,
        "plan_updated_count": 0,
        "step_started_count": 0,
        "step_completed_count": 0,
        "step_failed_count": 0,
        "tool_call_count": 0,
        "tool_calls_detail": [],
        "ask_user_triggered": False,
        "deep_research_triggered": False,
        "search_web_triggered": False,
        "deliverable_files": [],
        "final_status": None,
        "total_time": 0.0,
        "messages": [],
        "has_uow_error": False,
    }

    current_event = None
    current_data = None
    prev_plan_step_count = 0
    start_time = time.time()

    try:
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
                    stats["events_received"] += 1

                    # 关键: 检测 _uow AttributeError (批次 28 修复回归点)
                    if "_uow" in current_data and "no attribute" in current_data:
                        stats["has_uow_error"] = True
                        print(f"  [!!BUG!!] 检测到 _uow 错误: {current_data[:200]}")

                    if current_event == "plan":
                        try:
                            plan_data = json.loads(current_data)
                            steps = plan_data.get("steps", [])
                            cur_step_count = len(steps)
                            has_completed = any(
                                s.get("status") == "completed" for s in steps
                            )
                            plan_status = plan_data.get("status", "")

                            if prev_plan_step_count == 0:
                                stats["plan_created_count"] += 1
                                print(f"  [计划创建] {cur_step_count} 个步骤")
                                for i, step in enumerate(steps, 1):
                                    desc = step.get("description", "")[:80]
                                    print(f"    步骤{i}: {desc}")
                            elif cur_step_count < prev_plan_step_count:
                                stats["plan_updated_count"] += 1
                                print(f"  [计划更新] 第{stats['plan_updated_count']}次: "
                                      f"{prev_plan_step_count}步→{cur_step_count}步")
                            elif has_completed and plan_status != "created":
                                stats["plan_updated_count"] += 1
                                print(f"  [计划更新] 第{stats['plan_updated_count']}次(状态变化), "
                                      f"{cur_step_count} 个步骤")
                            prev_plan_step_count = cur_step_count
                        except json.JSONDecodeError:
                            pass

                    elif current_event == "step":
                        try:
                            step_data = json.loads(current_data)
                            step_status = step_data.get("status", "")
                            if step_status == "running":
                                stats["step_started_count"] += 1
                                step_desc = step_data.get("description", "")[:60]
                                print(f"  [步骤开始] {stats['step_started_count']}: {step_desc}")
                            elif step_status == "completed":
                                stats["step_completed_count"] += 1
                                result = step_data.get("result", "")[:100]
                                print(f"  [步骤完成] {stats['step_completed_count']}, 结果: {result}")
                            elif step_status == "failed":
                                stats["step_failed_count"] += 1
                                msg = step_data.get("message", "")[:80]
                                print(f"  [步骤失败] 第{stats['step_failed_count']}次: {msg}")
                        except json.JSONDecodeError:
                            pass

                    elif current_event == "tool":
                        stats["tool_call_count"] += 1
                        try:
                            tool_data = json.loads(current_data)
                            tool_name = tool_data.get("name", "unknown")
                            tool_args = str(tool_data.get("arguments", ""))[:80]
                            stats["tool_calls_detail"].append(
                                {"name": tool_name, "args": tool_args}
                            )
                            print(f"  [工具调用] #{stats['tool_call_count']}: "
                                  f"{tool_name}({tool_args})")

                            if tool_name == "ask_user":
                                stats["ask_user_triggered"] = True
                            elif tool_name == "deep_research":
                                stats["deep_research_triggered"] = True
                            elif tool_name == "search_web":
                                stats["search_web_triggered"] = True
                        except json.JSONDecodeError:
                            stats["tool_calls_detail"].append(
                                {"name": "unknown", "args": current_data[:80]}
                            )

                    elif current_event == "message":
                        try:
                            msg_data = json.loads(current_data)
                            msg_text = msg_data.get("message", "")[:200]
                            stats["messages"].append(msg_text)
                            print(f"  [消息] {msg_text}")
                        except json.JSONDecodeError:
                            pass

                    elif current_event == "file":
                        try:
                            file_data = json.loads(current_data)
                            file_path = file_data.get("path", "") or file_data.get("filename", "")
                            if file_path:
                                stats["deliverable_files"].append(file_path)
                                print(f"  [文件交付] {file_path}")
                        except json.JSONDecodeError:
                            pass

                    elif current_event == "error":
                        print(f"  [错误事件] {current_data[:200]}")
                        if "_uow" in current_data:
                            stats["has_uow_error"] = True

                    elif current_event == "done":
                        print("  [完成] 收到 done 事件")
                        break
    except Exception as e:
        print(f"  [异常] SSE 流中断: {e}")

    stats["total_time"] = time.time() - start_time

    # 等待会话最终状态
    print(f"  [等待] 会话最终状态...")
    max_wait = 60
    waited = 0
    status = "unknown"
    while waited < max_wait:
        try:
            detail = get_session_detail(client, token, session_id)
            status = detail["status"]
            if status in ("completed", "waiting", "waiting_for_user_input", "failed"):
                break
            time.sleep(3)
            waited += 3
        except Exception as e:
            print(f"  [警告] 获取会话状态失败: {e}")
            time.sleep(3)
            waited += 3

    stats["final_status"] = status
    print(f"  [会话状态] {stats['final_status']}, 本轮耗时: {stats['total_time']:.1f}s")
    return stats


def evaluate_session1(stats: dict) -> dict:
    """评估会话 1: 出入库/库存数据分析(无附件场景)"""
    print(f"\n{'=' * 60}")
    print("[评估] 会话 1: 出入库/库存数据分析(无附件场景)")
    print(f"{'=' * 60}")

    checks = []

    # 1. SSE 流稳定
    checks.append(("SSE 事件流稳定", stats["events_received"] > 0))
    # 2. 计划创建成功
    checks.append(("计划创建成功", stats["plan_created_count"] >= 1))
    # 3. 会话最终状态合理(completed 或 waiting_for_user_input)
    final_ok = stats["final_status"] in (
        "completed", "waiting", "waiting_for_user_input"
    )
    checks.append((f"会话最终状态合理({stats['final_status']})", final_ok))
    # 4. 无 _uow AttributeError (批次 28 修复回归点 - 关键)
    checks.append((
        "无 _uow AttributeError (批次 28 修复回归点)",
        not stats.get("has_uow_error", False)
    ))
    # 5. 步骤数 >= 1
    checks.append(("至少 1 个步骤开始", stats["step_started_count"] >= 1))
    # 6. 无异常重复工具调用(同名工具 <= 8 次)
    tool_counts = {}
    for tc in stats["tool_calls_detail"]:
        tool_counts[tc["name"]] = tool_counts.get(tc["name"], 0) + 1
    excessive = {n: c for n, c in tool_counts.items() if c > 8}
    checks.append((f"无异常重复工具调用(分布: {tool_counts})", not excessive))
    # 7. 续接会话场景被触发(如果首轮 ask_user)
    if stats.get("follow_up_triggered") is not None:
        checks.append((
            f"续接会话场景被触发 ({stats.get('follow_up_triggered')})",
            stats.get("follow_up_triggered") is True or stats["final_status"] == "completed"
        ))

    passed = sum(1 for _, ok in checks if ok)
    for desc, ok in checks:
        mark = "[通过]" if ok else "[失败]"
        print(f"  {mark} {desc}")

    print(f"\n[结果] 会话 1: {passed}/{len(checks)} 项通过")
    return {
        "passed": passed,
        "total": len(checks),
        "checks": checks,
        "stats": stats,
    }


def evaluate_session2(stats: dict) -> dict:
    """评估会话 2: 深度搜索 2026 AI 发展趋势(deep_research 场景)"""
    print(f"\n{'=' * 60}")
    print("[评估] 会话 2: 深度搜索 2026 年 AI 发展趋势(deep_research 场景)")
    print(f"{'=' * 60}")

    checks = []

    # 1. SSE 流稳定
    checks.append(("SSE 事件流稳定", stats["events_received"] > 0))
    # 2. 计划创建成功
    checks.append(("计划创建成功", stats["plan_created_count"] >= 1))
    # 3. 会话最终状态为 completed
    checks.append((f"会话最终状态为 completed ({stats['final_status']})",
                   stats["final_status"] == "completed"))
    # 4. 触发 deep_research 或 search_web 工具
    search_triggered = stats["deep_research_triggered"] or stats["search_web_triggered"]
    checks.append((
        f"触发搜索类工具(deep_research={stats['deep_research_triggered']}, "
        f"search_web={stats['search_web_triggered']})",
        search_triggered,
    ))
    # 5. 步骤完成数 >= 1
    checks.append(("至少 1 个步骤完成", stats["step_completed_count"] >= 1))
    # 6. deep_research 调用次数 <= 2 (预算限制)
    dr_count = sum(1 for tc in stats["tool_calls_detail"] if tc["name"] == "deep_research")
    checks.append((f"deep_research 调用次数={dr_count}(预算<=2)", dr_count <= 2))
    # 7. search_web 调用次数 <= 8 (预算限制)
    sw_count = sum(1 for tc in stats["tool_calls_detail"] if tc["name"] == "search_web")
    checks.append((f"search_web 调用次数={sw_count}(预算<=8)", sw_count <= 8))
    # 8. 有交付文件(Markdown 报告)
    checks.append((f"有交付文件({len(stats['deliverable_files'])} 个)",
                   len(stats["deliverable_files"]) >= 1))
    # 9. 无 _uow AttributeError
    checks.append((
        "无 _uow AttributeError (批次 28 修复回归点)",
        not stats.get("has_uow_error", False)
    ))

    passed = sum(1 for _, ok in checks if ok)
    for desc, ok in checks:
        mark = "[通过]" if ok else "[失败]"
        print(f"  {mark} {desc}")

    print(f"\n[结果] 会话 2: {passed}/{len(checks)} 项通过")
    return {
        "passed": passed,
        "total": len(checks),
        "checks": checks,
        "stats": stats,
    }


def main():
    """主入口: 双会话并行验证"""
    print("=" * 60)
    print("批次 28 架构师 E2E 验证: 双会话场景 + 续接会话回归")
    print("=" * 60)

    client = httpx.Client(timeout=TIMEOUT)
    try:
        token = login(client)

        # 会话 1: 出入库/库存数据分析(无附件)
        # 加入续接会话回归: 如果首轮 ask_user,自动发"继续"验证 _uow 修复
        session_id_1 = create_session(client, token, "批次28-出入库库存数据分析-续接回归")
        stats_1 = run_session_chat(
            client, token, session_id_1,
            "根据26年1-5月份的全部出入库、库存数据,为我深度分析,"
            "用于生产把控和经营参考",
            label="会话1-无附件场景",
            follow_up_message="请基于现有数据进行深度分析,如无数据请生成模拟数据分析",
        )
        result_1 = evaluate_session1(stats_1)

        # 会话 2: 深度搜索 2026 年 AI 发展趋势
        session_id_2 = create_session(client, token, "批次28-2026AI发展趋势深度搜索")
        stats_2 = run_session_chat(
            client, token, session_id_2,
            "深度搜索请为我搜索 2026 年人工智能发展趋势",
            label="会话2-deep_research场景",
        )
        result_2 = evaluate_session2(stats_2)

        # 总结
        print(f"\n{'=' * 60}")
        print("[总结] 批次 28 架构师 E2E 验证")
        print(f"{'=' * 60}")
        total_passed = result_1["passed"] + result_2["passed"]
        total_checks = result_1["total"] + result_2["total"]
        print(f"会话 1: {result_1['passed']}/{result_1['total']} 项通过")
        print(f"会话 2: {result_2['passed']}/{result_2['total']} 项通过")
        print(f"总计: {total_passed}/{total_checks} 项通过")

        # 失败项汇总
        failures = []
        for r, label in [(result_1, "会话1"), (result_2, "会话2")]:
            for desc, ok in r["checks"]:
                if not ok:
                    failures.append(f"{label}: {desc}")
        if failures:
            print("\n[失败项汇总]")
            for f in failures:
                print(f"  - {f}")
        else:
            print("\n[全部通过] 批次 24-28 优化在双会话场景中达到预期")

        return 0 if total_passed == total_checks else 1

    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
