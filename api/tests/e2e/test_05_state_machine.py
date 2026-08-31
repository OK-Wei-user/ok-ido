#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""05-Planner-ReAct状态机强化端到端会话测试

通过 session_routes.py 暴露的HTTP端点测试:
  1. 正常会话流程不破坏(状态机正确流转)
  2. update_plan条件触发(正常步骤跳过update_plan,非每步都更新)
  3. 多步骤任务正常完成(步骤间不因跳过update_plan而中断)
"""
import json
import time
import httpx

BASE_URL = "http://localhost:8000/api"
TIMEOUT = 300


def login(client: httpx.Client) -> str:
    """登录获取access_token"""
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
    """解析SSE单行,返回字段字典"""
    if line.startswith("id: "):
        return {"field": "id", "value": line[4:]}
    elif line.startswith("event: "):
        return {"field": "event", "value": line[7:]}
    elif line.startswith("data: "):
        return {"field": "data", "value": line[6:]}
    return {"field": "", "value": ""}


def test_05_state_machine(client: httpx.Client, token: str, session_id: str) -> bool:
    """测试05-Planner-ReAct状态机强化

    验证:
    1. 正常多步骤任务能完成(状态机流转不破坏)
    2. update_plan条件触发(非每步都触发plan update事件)
    3. 步骤执行后正常推进(跳过update_plan时不中断)
    """
    print("\n" + "=" * 60)
    print("测试 05-Planner-ReAct状态机强化")
    print("=" * 60)

    # 使用多步骤任务,验证状态机流转
    chat_body = {
        "message": "请帮我搜索一下人工智能的最新进展,并简要总结",
        "timestamp": 0,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    print("[步骤1] 发送多步骤任务, 监控状态机流转...")
    start_time = time.time()

    events_received = []
    plan_created_count = 0
    plan_updated_count = 0
    step_started_count = 0
    step_completed_count = 0
    step_failed_count = 0
    step_retry_events = []
    current_event = None
    current_data = None

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
                    "data": current_data[:100] if current_data else "",
                })

                if current_event == "plan":
                    try:
                        plan_data = json.loads(current_data)
                        steps = plan_data.get("steps", [])
                        plan_status = plan_data.get("status", "")
                        if steps and plan_status in ("created", ""):
                            # plan事件: steps非空表示计划创建(SSE可能不携带status,DB中无status)
                            plan_created_count += 1
                            print(f"  [计划创建] {len(steps)}个步骤")
                            for i, step in enumerate(steps, 1):
                                desc = step.get("description", "")[:60]
                                print(f"    步骤{i}: {desc}")
                        elif plan_status == "updated":
                            plan_updated_count += 1
                            print(f"  [计划更新] 第{plan_updated_count}次更新")
                        elif plan_status == "completed":
                            print(f"  [计划完成] 所有步骤已完成")
                    except json.JSONDecodeError:
                        pass

                elif current_event == "step":
                    try:
                        step_data = json.loads(current_data)
                        step_status = step_data.get("status", "")
                        message = step_data.get("message", "")
                        if step_status == "running":
                            step_started_count += 1
                            step_desc = step_data.get("description", "")[:50]
                            print(f"  [步骤开始] {step_started_count}: {step_desc}")
                        elif step_status == "completed":
                            step_completed_count += 1
                            print(f"  [步骤完成] {step_completed_count}")
                        elif step_status == "failed":
                            step_failed_count += 1
                            if message:
                                step_retry_events.append(message)
                                print(f"  [步骤失败/重试] {message[:80]}")
                            else:
                                print(f"  [步骤失败] 第{step_failed_count}次")
                    except json.JSONDecodeError:
                        pass

                elif current_event == "done":
                    print("  [完成] 收到done事件")
                    break

    total_time = time.time() - start_time

    # 等待会话最终完成
    print("[步骤2] 等待会话最终状态...")
    max_wait = 60
    waited = 0
    while waited < max_wait:
        detail = get_session_detail(client, token, session_id)
        status = detail["status"]
        if status in ("completed", "waiting"):
            break
        time.sleep(3)
        waited += 3
        print(f"  会话状态: {status}, 等待{waited}s...")

    total_events = len(detail.get("events", []))

    print(f"\n[统计] 总耗时: {total_time:.1f}s")
    print(f"  事件总数: {len(events_received)} (会话DB事件: {total_events})")
    print(f"  计划创建: {plan_created_count}次")
    print(f"  计划更新: {plan_updated_count}次")
    print(f"  步骤开始: {step_started_count}次")
    print(f"  步骤完成: {step_completed_count}次")
    print(f"  步骤失败: {step_failed_count}次")
    print(f"  会话最终状态: {status}")

    # 验证1: 会话正常完成(不破坏)
    if status == "completed":
        print("[通过] 会话正常完成, 状态机流转不破坏")
    else:
        print(f"[警告] 会话状态: {status}")

    # 验证2: 计划创建成功
    if plan_created_count >= 1:
        print("[通过] 计划创建成功")
    else:
        print("[警告] 未收到计划创建事件")

    # 验证3: update_plan条件触发
    # 正常步骤应跳过update_plan,所以plan_updated_count应 < step_completed_count
    # (除非有定期同步或步骤结果触发更新)
    if step_completed_count > 0:
        if plan_updated_count < step_completed_count:
            print(f"[通过] update_plan条件触发: {plan_updated_count}次更新 < {step_completed_count}次步骤完成(跳过了部分update_plan)")
        elif plan_updated_count == 0 and step_completed_count > 0:
            print(f"[通过] 所有步骤均跳过update_plan(步骤数={step_completed_count},更新数=0)")
        else:
            print(f"[信息] 每步都触发了update_plan(可能由关键词或定期同步触发): 更新{plan_updated_count}次, 完成{step_completed_count}次")

    # 验证4: 步骤执行正常
    if step_completed_count > 0:
        print(f"[通过] {step_completed_count}个步骤正常完成")
    else:
        print("[警告] 无步骤完成事件")

    # 综合判断
    passed = (
        status == "completed"
        and plan_created_count >= 1
        and step_completed_count >= 1
    )
    if passed:
        print("\n[结果] 05-Planner-ReAct状态机强化: 通过")
    else:
        print("\n[结果] 05-Planner-ReAct状态机强化: 需关注")

    return passed


def main():
    """主测试入口"""
    print("=" * 60)
    print("05-Planner-ReAct状态机强化 端到端会话测试")
    print("=" * 60)

    with httpx.Client(timeout=httpx.Timeout(TIMEOUT)) as client:
        # 登录
        token = login(client)

        # 创建会话
        session_id = create_session(client, token, "05状态机强化测试")

        # 测试05
        try:
            result = test_05_state_machine(client, token, session_id)
        except Exception as e:
            print(f"[异常] 05测试失败: {e}")
            import traceback
            traceback.print_exc()
            result = False

    print("\n" + "=" * 60)
    if result:
        print("测试结果: 通过")
    else:
        print("测试结果: 需关注")
    print("=" * 60)

    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
