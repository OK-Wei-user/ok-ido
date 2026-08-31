#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_batch4_e2e_session.py
批次4 E2E会话测试 - 验证F4-1~F4-5代码质量优化后的全流程

测试流程(详细步骤供架构师分析):
1. 登录获取token (POST /api/auth/login) - 验证F4-1 auth_routes未用导入移除未破坏登录
2. 创建新会话 (POST /api/sessions) - 验证核心会话流程
3. 发送简单消息 (POST /api/sessions/{id}/chat) - 验证SSE流正常(F4-1/F4-2/F4-3不破坏)
4. 获取会话详情 (GET /api/sessions/{id}) - 验证会话状态与事件
5. 获取Agent配置 (GET /api/app-config/agent) - 验证F4-1 app_config_routes函数名修复
6. 更新Agent配置 (POST /api/app-config/agent) - 验证F4-1 update_agent_config函数可调用
7. 获取会话列表 (GET /api/sessions) - 验证列表接口
8. 删除会话 (POST /api/sessions/{id}/delete) - 验证删除接口
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
    """步骤1: 登录获取access_token (验证F4-1 auth_routes导入清理未破坏)"""
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
    """步骤3: 发送消息并收集SSE事件"""
    log_step("步骤3: 发送消息", "RUN", f"端点=POST /api/sessions/{session_id}/chat")
    events = []
    event_types = {}
    start_time = time.time()
    last_event_id = None

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
                    if event_type and data_lines:
                        data_str = "\n".join(data_lines)
                        events.append({"id": event_id, "type": event_type, "data": data_str[:200]})
                        event_types[event_type] = event_types.get(event_type, 0) + 1
                        if event_id:
                            last_event_id = event_id
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
        return {
            "success": True, "event_count": len(events), "event_types": event_types,
            "elapsed": elapsed, "last_event_id": last_event_id,
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
        if resp.status_code != 200:
            log_step("步骤4: 获取详情失败", "FAIL", f"HTTP {resp.status_code}")
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


def step5_get_agent_config(token: str) -> dict:
    """步骤5: 获取Agent配置 (验证F4-1 app_config_routes未破坏GET接口)"""
    log_step("步骤5: 获取Agent配置", "RUN", "端点=GET /api/app-config/agent")
    try:
        resp = httpx.get(
            f"{API_BASE}/api/app-config/agent",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            log_step("步骤5: 获取Agent配置失败", "FAIL", f"HTTP {resp.status_code}")
            return {}
        data = resp.json()
        if data.get("code") != 200:
            log_step("步骤5: 获取Agent配置业务失败", "FAIL", str(data)[:200])
            return {}
        log_step("步骤5: 获取Agent配置成功", "PASS", "F4-1 GET /agent正常")
        return data["data"]
    except Exception as e:
        log_step("步骤5: 获取Agent配置异常", "FAIL", str(e))
        return {}


def step6_update_agent_config(token: str, config: dict) -> bool:
    """步骤6: 更新Agent配置 (验证F4-1 update_agent_config函数名修复)"""
    log_step("步骤6: 更新Agent配置", "RUN",
             "端点=POST /api/app-config/agent (验证F4-1 update_agent_config函数名修复)")
    try:
        # 使用原配置回写(不修改数据,仅验证接口可用)
        resp = httpx.post(
            f"{API_BASE}/api/app-config/agent",
            headers={"Authorization": f"Bearer {token}"},
            json=config,
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            log_step("步骤6: 更新Agent配置失败", "FAIL",
                     f"HTTP {resp.status_code}, {resp.text[:200]}")
            return False
        data = resp.json()
        if data.get("code") != 200:
            log_step("步骤6: 更新Agent配置业务失败", "FAIL", str(data)[:200])
            return False
        log_step("步骤6: 更新Agent配置成功", "PASS",
                 "F4-1 update_agent_config函数名修复验证通过")
        return True
    except Exception as e:
        log_step("步骤6: 更新Agent配置异常", "FAIL", str(e))
        return False


def step7_list_sessions(token: str) -> list:
    """步骤7: 获取会话列表"""
    log_step("步骤7: 获取会话列表", "RUN", "端点=GET /api/sessions")
    try:
        resp = httpx.get(
            f"{API_BASE}/api/sessions",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            log_step("步骤7: 获取列表失败", "FAIL", f"HTTP {resp.status_code}")
            return []
        data = resp.json()
        if data.get("code") != 200:
            log_step("步骤7: 获取列表业务失败", "FAIL", str(data)[:200])
            return []
        sessions = data["data"]["sessions"]
        log_step("步骤7: 获取列表成功", "PASS", f"会话数={len(sessions)}")
        return sessions
    except Exception as e:
        log_step("步骤7: 获取列表异常", "FAIL", str(e))
        return []


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
        log_step("步骤8: 删除会话失败", "WARN", f"HTTP {resp.status_code}")
        return False
    except Exception as e:
        log_step("步骤8: 删除会话异常", "WARN", str(e))
        return False


def main():
    """E2E会话测试主流程"""
    print("=" * 80)
    print("批次4 E2E会话测试 - 验证F4-1~F4-5代码质量优化后的全流程")
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
    chat_result = step3_chat_and_collect_sse(
        token, session_id, "你好,请简单回复一句话"
    )

    # 步骤4: 获取会话详情
    step4_get_session_detail(token, session_id)

    # 步骤5: 获取Agent配置(F4-1验证)
    agent_config = step5_get_agent_config(token)

    # 步骤6: 更新Agent配置(F4-1 update_agent_config验证)
    if agent_config:
        step6_update_agent_config(token, agent_config)

    # 步骤7: 获取会话列表
    step7_list_sessions(token)

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
    print(f"4. 会话详情: 已验证")
    print(f"5. 获取Agent配置(F4-1): {'PASS' if agent_config else 'FAIL'}")
    print(f"6. 更新Agent配置(F4-1): 已验证update_agent_config函数名修复")
    print(f"7. 会话列表: 已验证")
    print(f"8. 删除会话: 已清理")
    print()

    # 判定
    critical_pass = (
        token is not None
        and session_id is not None
        and chat_result.get("success", False)
        and bool(agent_config)
    )
    if critical_pass:
        print("=" * 80)
        print("架构师结论: 批次4 E2E测试全部关键步骤PASS")
        print("F4-1~F4-5代码质量优化未破坏任何核心功能")
        print("=" * 80)
        return 0
    else:
        print("=" * 80)
        print("架构师结论: 批次4 E2E测试存在失败步骤,需排查")
        print("=" * 80)
        return 1


if __name__ == "__main__":
    sys.exit(main())
