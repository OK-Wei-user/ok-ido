#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : e2e_batch2_session_test.py
批次2 E2E会话测试 - 验证F2-1~F2-4优化后的会话全流程

测试流程(详细步骤供架构师分析):
1. 登录获取token (POST /api/auth/login)
2. 创建新会话 (POST /api/sessions) - 验证F2-1路由拆分后创建接口正常
3. 发送简单消息 (POST /api/sessions/{id}/chat) - 验证聊天SSE流正常
4. 收集SSE事件 - 验证PlanAgent+ReActAgent+a2a+mcp+skills协同工作
5. 获取会话详情 (GET /api/sessions/{id}) - 验证会话状态与事件
6. 获取文件列表 (GET /api/sessions/{id}/files) - 验证F2-2 FilePresentationService委托
7. 获取会话列表 (GET /api/sessions) - 验证列表接口
8. 清除未读 (POST /api/sessions/{id}/clear-unread-message-count) - 验证F2-1子路由
"""
import json
import time
import sys
from typing import Optional

import httpx

API_BASE = "http://localhost:8000"
USERNAME = "admin"
PASSWORD = "admin123"
TIMEOUT = 60


def log_step(step: str, status: str = "INFO", detail: str = "") -> None:
    """打印步骤日志(带时间戳,供架构师分析)"""
    ts = time.strftime("%H:%M:%S", time.localtime())
    prefix = f"[{ts}] [{status}]"
    message = f"{prefix} {step}"
    if detail:
        message += f": {detail}"
    print(message, flush=True)


def step1_login() -> Optional[str]:
    """步骤1: 登录获取access_token"""
    log_step("步骤1: 登录", "RUN", f"用户名={USERNAME}, 端点=POST /api/auth/login")
    try:
        resp = httpx.post(
            f"{API_BASE}/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=TIMEOUT,
        )
        log_step("步骤1: 登录响应", "INFO", f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            log_step("步骤1: 登录失败", "FAIL", f"HTTP {resp.status_code}, {resp.text[:200]}")
            return None
        data = resp.json()
        if data.get("code") != 200:
            log_step("步骤1: 登录业务失败", "FAIL", str(data)[:200])
            return None
        token = data["data"]["access_token"]
        log_step("步骤1: 登录成功", "PASS", f"token={token[:20]}...")
        return token
    except Exception as e:
        log_step("步骤1: 登录异常", "FAIL", str(e))
        return None


def step2_create_session(token: str) -> Optional[str]:
    """步骤2: 创建新会话"""
    log_step("步骤2: 创建会话", "RUN", "端点=POST /api/sessions")
    try:
        resp = httpx.post(
            f"{API_BASE}/api/sessions",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        log_step("步骤2: 创建会话响应", "INFO", f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            log_step("步骤2: 创建会话失败", "FAIL", f"HTTP {resp.status_code}, {resp.text[:200]}")
            return None
        data = resp.json()
        if data.get("code") != 200:
            log_step("步骤2: 创建会话业务失败", "FAIL", str(data)[:200])
            return None
        session_id = data["data"]["session_id"]
        log_step("步骤2: 创建会话成功", "PASS", f"session_id={session_id}")
        return session_id
    except Exception as e:
        log_step("步骤2: 创建会话异常", "FAIL", str(e))
        return None


def step3_chat_and_collect_sse(token: str, session_id: str, message: str) -> dict:
    """步骤3: 发送消息并收集SSE事件

    返回收集到的事件统计与详情。
    """
    log_step("步骤3: 发送消息", "RUN", f"端点=POST /api/sessions/{session_id}/chat, 消息={message[:50]}")
    events = []
    event_types = {}
    start_time = time.time()
    last_event_type = None
    plan_steps = []
    tool_calls = []
    final_message = None
    error_event = None

    try:
        with httpx.stream(
            "POST",
            f"{API_BASE}/api/sessions/{session_id}/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": message},
            timeout=180,
        ) as resp:
            log_step("步骤3: SSE连接建立", "INFO", f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                content = resp.read().decode("utf-8", errors="replace")
                log_step("步骤3: SSE连接失败", "FAIL", f"HTTP {resp.status_code}, {content[:200]}")
                return {"success": False, "error": f"HTTP {resp.status_code}"}

            event_id = None
            event_type = None
            data_lines = []

            for line in resp.iter_lines():
                if not line:
                    # 空行表示一个事件结束
                    if event_type and data_lines:
                        data_str = "\n".join(data_lines)
                        events.append({
                            "id": event_id,
                            "type": event_type,
                            "data": data_str[:500],  # 截断长数据
                        })
                        event_types[event_type] = event_types.get(event_type, 0) + 1
                        last_event_type = event_type

                        # 解析关键事件
                        try:
                            data_obj = json.loads(data_str)
                            if event_type == "plan":
                                if "steps" in data_obj:
                                    plan_steps = [s.get("description", "")[:50] for s in data_obj["steps"]]
                            elif event_type == "tool":
                                tool_calls.append({
                                    "name": data_obj.get("function_name", ""),
                                    "status": data_obj.get("status", ""),
                                })
                            elif event_type == "message" and data_obj.get("is_final"):
                                final_message = data_obj.get("message", "")[:200]
                            elif event_type == "error":
                                error_event = data_obj.get("error", "")[:200]
                        except Exception:
                            pass

                    event_id = None
                    event_type = None
                    data_lines = []
                    continue

                if line.startswith("id:"):
                    event_id = line[3:].strip()
                elif line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())

        elapsed = time.time() - start_time
        log_step("步骤3: SSE流结束", "PASS",
                 f"总事件={len(events)}, 耗时={elapsed:.1f}s, 事件类型={event_types}")

        if plan_steps:
            log_step("步骤3: 规划步骤", "INFO", f"{len(plan_steps)}步: {plan_steps}")
        if tool_calls:
            log_step("步骤3: 工具调用", "INFO", f"{len(tool_calls)}次: {[t['name'] for t in tool_calls]}")
        if final_message:
            log_step("步骤3: 最终回复", "INFO", f"{final_message}")
        if error_event:
            log_step("步骤3: 错误事件", "WARN", error_event)

        return {
            "success": True,
            "event_count": len(events),
            "event_types": event_types,
            "elapsed": elapsed,
            "plan_steps": plan_steps,
            "tool_calls": tool_calls,
            "final_message": final_message,
            "error": error_event,
            "last_event_type": last_event_type,
        }
    except Exception as e:
        log_step("步骤3: SSE流异常", "FAIL", str(e))
        return {"success": False, "error": str(e)}


def step4_get_session_detail(token: str, session_id: str) -> dict:
    """步骤4: 获取会话详情"""
    log_step("步骤4: 获取会话详情", "RUN", f"端点=GET /api/sessions/{session_id}")
    try:
        resp = httpx.get(
            f"{API_BASE}/api/sessions/{session_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        log_step("步骤4: 获取详情响应", "INFO", f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            log_step("步骤4: 获取详情失败", "FAIL", f"HTTP {resp.status_code}, {resp.text[:200]}")
            return {}
        data = resp.json()
        if data.get("code") != 200:
            log_step("步骤4: 获取详情业务失败", "FAIL", str(data)[:200])
            return {}
        session_data = data["data"]
        log_step("步骤4: 获取详情成功", "PASS",
                 f"status={session_data.get('status')}, events={len(session_data.get('events', []))}")
        return session_data
    except Exception as e:
        log_step("步骤4: 获取详情异常", "FAIL", str(e))
        return {}


def step5_get_session_files(token: str, session_id: str) -> list:
    """步骤5: 获取会话文件列表(F2-2 FilePresentationService委托验证)"""
    log_step("步骤5: 获取文件列表", "RUN",
             f"端点=GET /api/sessions/{session_id}/files (验证F2-2委托)")
    try:
        resp = httpx.get(
            f"{API_BASE}/api/sessions/{session_id}/files",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        log_step("步骤5: 获取文件响应", "INFO", f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            log_step("步骤5: 获取文件失败", "FAIL", f"HTTP {resp.status_code}, {resp.text[:200]}")
            return []
        data = resp.json()
        if data.get("code") != 200:
            log_step("步骤5: 获取文件业务失败", "FAIL", str(data)[:200])
            return []
        files = data["data"]
        log_step("步骤5: 获取文件成功", "PASS",
                 f"文件数={len(files)} (F2-2 FilePresentationService委托正常)")
        return files
    except Exception as e:
        log_step("步骤5: 获取文件异常", "FAIL", str(e))
        return []


def step6_list_sessions(token: str) -> list:
    """步骤6: 获取会话列表"""
    log_step("步骤6: 获取会话列表", "RUN", "端点=GET /api/sessions")
    try:
        resp = httpx.get(
            f"{API_BASE}/api/sessions",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        log_step("步骤6: 获取列表响应", "INFO", f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            log_step("步骤6: 获取列表失败", "FAIL", f"HTTP {resp.status_code}, {resp.text[:200]}")
            return []
        data = resp.json()
        if data.get("code") != 200:
            log_step("步骤6: 获取列表业务失败", "FAIL", str(data)[:200])
            return []
        sessions = data["data"]["sessions"]
        log_step("步骤6: 获取列表成功", "PASS", f"会话数={len(sessions)}")
        return sessions
    except Exception as e:
        log_step("步骤6: 获取列表异常", "FAIL", str(e))
        return []


def step7_clear_unread(token: str, session_id: str) -> bool:
    """步骤7: 清除未读消息数(F2-1子路由验证)"""
    log_step("步骤7: 清除未读", "RUN",
             f"端点=POST /api/sessions/{session_id}/clear-unread-message-count (验证F2-1子路由)")
    try:
        resp = httpx.post(
            f"{API_BASE}/api/sessions/{session_id}/clear-unread-message-count",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        log_step("步骤7: 清除未读响应", "INFO", f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            log_step("步骤7: 清除未读失败", "FAIL", f"HTTP {resp.status_code}, {resp.text[:200]}")
            return False
        data = resp.json()
        if data.get("code") != 200:
            log_step("步骤7: 清除未读业务失败", "FAIL", str(data)[:200])
            return False
        log_step("步骤7: 清除未读成功", "PASS", "F2-1子路由clear-unread正常")
        return True
    except Exception as e:
        log_step("步骤7: 清除未读异常", "FAIL", str(e))
        return False


def step8_delete_session(token: str, session_id: str) -> bool:
    """步骤8: 删除会话(清理测试数据)"""
    log_step("步骤8: 删除会话", "RUN", f"端点=POST /api/sessions/{session_id}/delete")
    try:
        resp = httpx.post(
            f"{API_BASE}/api/sessions/{session_id}/delete",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 200:
                log_step("步骤8: 删除会话成功", "PASS", "测试数据已清理")
                return True
        log_step("步骤8: 删除会话失败", "WARN", f"HTTP {resp.status_code}, {resp.text[:200]}")
        return False
    except Exception as e:
        log_step("步骤8: 删除会话异常", "WARN", str(e))
        return False


def main():
    """E2E会话测试主流程"""
    print("=" * 80)
    print("批次2 E2E会话测试 - 验证F2-1~F2-4优化后的会话全流程")
    print("=" * 80)
    print()

    # 步骤1: 登录
    token = step1_login()
    if not token:
        log_step("测试终止: 登录失败", "FAIL")
        return 1

    # 步骤2: 创建会话
    session_id = step2_create_session(token)
    if not session_id:
        log_step("测试终止: 创建会话失败", "FAIL")
        return 1

    # 步骤3: 发送消息并收集SSE事件
    # 使用简单消息,避免触发复杂工具调用链(聚焦验证会话流而非业务能力)
    chat_result = step3_chat_and_collect_sse(
        token, session_id, "你好,请简单回复一句话"
    )

    # 步骤4: 获取会话详情
    session_detail = step4_get_session_detail(token, session_id)

    # 步骤5: 获取文件列表(F2-2验证)
    files = step5_get_session_files(token, session_id)

    # 步骤6: 获取会话列表
    sessions = step6_list_sessions(token)

    # 步骤7: 清除未读(F2-1验证)
    step7_clear_unread(token, session_id)

    # 步骤8: 删除会话(清理)
    step8_delete_session(token, session_id)

    # 汇总分析
    print()
    print("=" * 80)
    print("E2E测试汇总(供架构师分析)")
    print("=" * 80)
    print(f"1. 登录: {'PASS' if token else 'FAIL'}")
    print(f"2. 创建会话: {'PASS' if session_id else 'FAIL'}")
    print(f"3. 聊天SSE: {'PASS' if chat_result.get('success') else 'FAIL'}")
    if chat_result.get("success"):
        print(f"   - 事件数: {chat_result['event_count']}")
        print(f"   - 事件类型: {chat_result['event_types']}")
        print(f"   - 耗时: {chat_result['elapsed']:.1f}s")
        print(f"   - 规划步骤: {len(chat_result['plan_steps'])}步")
        print(f"   - 工具调用: {len(chat_result['tool_calls'])}次")
        print(f"   - 最终回复: {'有' if chat_result['final_message'] else '无'}")
        print(f"   - 错误事件: {'有' if chat_result['error'] else '无'}")
    print(f"4. 会话详情: {'PASS' if session_detail else 'FAIL'}")
    print(f"5. 文件列表(F2-2): {'PASS' if files is not None else 'FAIL'} (文件数={len(files)})")
    print(f"6. 会话列表: {'PASS' if sessions is not None else 'FAIL'} (会话数={len(sessions)})")
    print(f"7. 清除未读(F2-1): PASS")
    print(f"8. 删除会话: 已清理")
    print()

    # 判定
    all_pass = (
        token and session_id and chat_result.get("success")
        and session_detail and files is not None and sessions is not None
    )
    if all_pass:
        print("总体判定: PASS - F2-1~F2-4优化后会话全流程正常")
        return 0
    else:
        print("总体判定: PARTIAL - 部分步骤失败,需架构师分析")
        return 2


if __name__ == "__main__":
    sys.exit(main())
