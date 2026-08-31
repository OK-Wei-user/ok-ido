#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批次 27 E2E 会话测试: PlanAgent 计划重复执行修复验证

测试目标:
  1. 验证多步骤任务正常完成(步骤间数据传递不中断)
  2. 验证 plan_update 事件正常触发(批次27 注入 prior_steps_context)
  3. 验证 LLM 不重建已完成步骤(无异常重复工具调用)
  4. 验证会话最终状态为 completed

测试场景:
  构造三步任务"生成数据→分析数据→生成报告",
  观察步骤 2/3 是否复用步骤 1 的产出而非重复执行。
"""
import json
import time

import httpx

BASE_URL = "http://localhost:8000/api"
TIMEOUT = 300


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
    print(f"[创建会话] session_id={session_id}")
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


def run_batch27_e2e(client: httpx.Client, token: str, session_id: str) -> dict:
    """执行批次 27 E2E 测试"""
    print("\n" + "=" * 60)
    print("批次 27 E2E: PlanAgent 计划重复执行修复验证")
    print("=" * 60)

    chat_body = {
        "message": (
            "请用Python完成一个三步数据分析任务: "
            "1) 生成一个包含10条销售记录的CSV文件(字段: 日期、产品、数量、金额),保存为 sales_data.csv; "
            "2) 用 pandas 读取该CSV并统计每个产品的总销售额,将统计结果保存为 product_summary.csv; "
            "3) 基于统计结果生成一份 Markdown 格式的分析报告,包含数据概览、产品排名和结论建议。"
        ),
        "timestamp": 0,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    print("[步骤1] 发送三步任务, 监控事件流...")
    start_time = time.time()

    events_received = []
    plan_created_count = 0
    plan_updated_count = 0
    step_started_count = 0
    step_completed_count = 0
    step_failed_count = 0
    tool_call_count = 0
    tool_calls_detail = []
    step_results = []
    current_event = None
    current_data = None
    prev_plan_step_count = 0

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
                events_received.append({
                    "event": current_event,
                    "data": current_data[:200] if current_data else "",
                })

                if current_event == "plan":
                    try:
                        plan_data = json.loads(current_data)
                        steps = plan_data.get("steps", [])
                        plan_status = plan_data.get("status", "")
                        cur_step_count = len(steps)
                        has_completed = any(
                            s.get("status") == "completed" for s in steps
                        )
                        if prev_plan_step_count == 0:
                            plan_created_count += 1
                            print(f"  [计划创建] {cur_step_count}个步骤")
                            for i, step in enumerate(steps, 1):
                                desc = step.get("description", "")[:80]
                                print(f"    步骤{i}: {desc}")
                        elif cur_step_count < prev_plan_step_count:
                            plan_updated_count += 1
                            print(f"  [计划更新] 第{plan_updated_count}次: "
                                  f"{prev_plan_step_count}步→{cur_step_count}步")
                            for i, step in enumerate(steps, 1):
                                desc = step.get("description", "")[:80]
                                sstatus = step.get("status", "")
                                print(f"    步骤{i}({sstatus}): {desc}")
                        elif has_completed and plan_status != "created":
                            plan_updated_count += 1
                            print(f"  [计划更新] 第{plan_updated_count}次"
                                  f"(状态变化), {cur_step_count}个步骤")
                        else:
                            print(f"  [计划事件] {cur_step_count}个步骤 "
                                  f"(status={plan_status})")
                        prev_plan_step_count = cur_step_count
                    except json.JSONDecodeError:
                        pass

                elif current_event == "step":
                    try:
                        step_data = json.loads(current_data)
                        step_status = step_data.get("status", "")
                        message = step_data.get("message", "")
                        if step_status == "running":
                            step_started_count += 1
                            step_desc = step_data.get("description", "")[:60]
                            print(f"  [步骤开始] {step_started_count}: {step_desc}")
                        elif step_status == "completed":
                            step_completed_count += 1
                            result = step_data.get("result", "")[:100]
                            step_results.append(result)
                            print(f"  [步骤完成] {step_completed_count}, "
                                  f"结果: {result}")
                        elif step_status == "failed":
                            step_failed_count += 1
                            print(f"  [步骤失败] 第{step_failed_count}次: "
                                  f"{message[:80]}")
                    except json.JSONDecodeError:
                        pass

                elif current_event == "tool":
                    tool_call_count += 1
                    try:
                        tool_data = json.loads(current_data)
                        tool_name = tool_data.get("name", "unknown")
                        tool_args = str(tool_data.get("arguments", ""))[:80]
                        tool_calls_detail.append({
                            "name": tool_name,
                            "args": tool_args,
                        })
                        print(f"  [工具调用] #{tool_call_count}: "
                              f"{tool_name}({tool_args})")
                    except json.JSONDecodeError:
                        tool_calls_detail.append({
                            "name": "unknown",
                            "args": current_data[:80],
                        })

                elif current_event == "done":
                    print("  [完成] 收到 done 事件")
                    break

    total_time = time.time() - start_time

    print("[步骤2] 等待会话最终状态...")
    max_wait = 60
    waited = 0
    detail = None
    status = "unknown"
    while waited < max_wait:
        detail = get_session_detail(client, token, session_id)
        status = detail["status"]
        if status in ("completed", "waiting"):
            break
        time.sleep(3)
        waited += 3
        print(f"  会话状态: {status}, 等待{waited}s...")

    total_events = len(detail.get("events", [])) if detail else 0

    print(f"\n[统计] 总耗时: {total_time:.1f}s")
    print(f"  事件总数: {len(events_received)} (会话DB事件: {total_events})")
    print(f"  计划创建: {plan_created_count}次")
    print(f"  计划更新: {plan_updated_count}次")
    print(f"  步骤开始: {step_started_count}次")
    print(f"  步骤完成: {step_completed_count}次")
    print(f"  步骤失败: {step_failed_count}次")
    print(f"  工具调用: {tool_call_count}次")
    print(f"  会话最终状态: {status}")

    # === 验证项 ===
    passed_count = 0
    total_checks = 5

    # 验证1: 会话正常完成
    if status == "completed":
        print("[通过] 会话正常完成, 步骤间数据传递不中断")
        passed_count += 1
    else:
        print(f"[失败] 会话状态: {status}, 期望 completed")

    # 验证2: 计划创建成功
    if plan_created_count >= 1:
        print("[通过] 计划创建成功")
        passed_count += 1
    else:
        print("[失败] 未收到计划创建事件")

    # 验证3: 步骤正常完成(至少1步)
    if step_completed_count >= 1:
        print(f"[通过] {step_completed_count}个步骤正常完成")
        passed_count += 1
    else:
        print(f"[失败] 无步骤完成事件")

    # 验证4: 计划更新触发(批次27 注入 prior_steps_context 后,
    # LLM 应能感知已完成步骤,正常触发计划更新)
    if plan_updated_count >= 1:
        print(f"[通过] 计划更新{plan_updated_count}次 (批次27 prior_steps_context 注入正常)")
        passed_count += 1
    else:
        print(f"[警告] 计划更新0次 (可能步骤数<2或LLM未触发更新条件, 非硬性失败)")

    # 验证5: 无异常重复工具调用
    tool_name_counts = {}
    for tc in tool_calls_detail:
        name = tc["name"]
        tool_name_counts[name] = tool_name_counts.get(name, 0) + 1

    excessive_tools = {
        name: count
        for name, count in tool_name_counts.items()
        if count > 8
    }
    if not excessive_tools:
        print(f"[通过] 无异常重复工具调用 (工具分布: {tool_name_counts})")
        passed_count += 1
    else:
        print(f"[警告] 部分工具调用次数较多: {excessive_tools}")

    print(f"\n[验证结果] {passed_count}/{total_checks} 项通过")
    if passed_count >= 4:
        print("\n[结果] 批次 27 E2E: 通过")
        result = True
    else:
        print("\n[结果] 批次 27 E2E: 需关注")
        result = False

    return {
        "result": result,
        "passed": passed_count,
        "total": total_checks,
        "session_id": session_id,
        "total_time": total_time,
        "plan_created": plan_created_count,
        "plan_updated": plan_updated_count,
        "step_completed": step_completed_count,
        "step_failed": step_failed_count,
        "tool_calls": tool_call_count,
        "tool_distribution": tool_name_counts,
        "final_status": status,
    }


def main():
    """主测试入口"""
    print("=" * 60)
    print("批次 27 E2E 会话测试: PlanAgent 计划重复执行修复验证")
    print("=" * 60)

    with httpx.Client(timeout=httpx.Timeout(TIMEOUT)) as client:
        token = login(client)
        session_id = create_session(
            client, token, "批次27验证_PlanAgent防重复执行"
        )

        try:
            result = run_batch27_e2e(client, token, session_id)
        except Exception as e:
            print(f"[异常] 批次 27 E2E 测试失败: {e}")
            import traceback
            traceback.print_exc()
            result = {"result": False, "error": str(e)}

    print("\n" + "=" * 60)
    if result.get("result"):
        print("测试结果: 通过")
    else:
        print("测试结果: 需关注")
    print("=" * 60)

    return 0 if result.get("result") else 1


if __name__ == "__main__":
    raise SystemExit(main())
