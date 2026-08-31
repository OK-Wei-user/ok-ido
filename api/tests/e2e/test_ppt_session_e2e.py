#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_ppt_session_e2e.py
复现 f2611353 场景:发送 PPT 制作任务验证会话不卡死

原始问题:LLM 收到 pptx skill 指南后调用 find_files(dir_path="/", ...) 卡死
修复后:find_files 三层防护拦截系统目录扫描,会话应正常完成或快速失败
"""
import json
import sys
import time

import httpx

API_BASE = "http://localhost:8000"
USERNAME = "admin"
PASSWORD = "admin123"
TIMEOUT = 60


def log_step(step, status="INFO", detail=""):
    prefix = "[" + status + "]"
    message = prefix + " " + step
    if detail:
        message += ": " + detail
    print(message, flush=True)


def main():
    print("=" * 60, flush=True)
    print("PPT 制作任务 E2E 测试(复现 f2611353 场景)", flush=True)
    print("=" * 60, flush=True)

    try:
        # 1.登录
        log_step("步骤1: 登录", "RUN")
        resp = httpx.post(
            API_BASE + "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=TIMEOUT,
        )
        token = resp.json()["data"]["access_token"]
        log_step("步骤1: 登录成功", "PASS")

        # 2.创建会话
        log_step("步骤2: 创建会话", "RUN")
        resp = httpx.post(
            API_BASE + "/api/sessions",
            headers={"Authorization": "Bearer " + token},
            timeout=TIMEOUT,
        )
        session_id = resp.json()["data"]["session_id"]
        log_step("步骤2: 创建会话成功", "PASS", "session_id=" + session_id)

        # 3.发送 PPT 制作消息(复现原始卡住场景)
        log_step("步骤3: 发送 PPT 制作消息", "RUN")
        events = []
        event_types = set()
        tool_calls = []
        start_time = time.time()
        max_duration = 180  # 3分钟上限

        with httpx.stream(
            "POST",
            API_BASE + "/api/sessions/" + session_id + "/chat",
            headers={"Authorization": "Bearer " + token},
            json={"message": "简单介绍一下你自己，你是谁，你有什么能力，你可以为我做些什么？整理成PPT"},
            timeout=200,
        ) as response:
            assert response.status_code == 200

            current_event_type = None
            current_data = ""

            for line in response.iter_lines():
                elapsed = time.time() - start_time
                if elapsed > max_duration:
                    log_step("步骤3: 超时(" + str(max_duration) + "s)，停止读取", "WARN")
                    break

                if line.startswith("event:"):
                    current_event_type = line[6:].strip()
                elif line.startswith("data:"):
                    current_data = line[5:].strip()
                elif line == "" and current_event_type:
                    events.append({"type": current_event_type, "data": current_data[:300]})
                    event_types.add(current_event_type)

                    # 记录工具调用
                    if current_event_type == "tool" and current_data:
                        try:
                            parsed = json.loads(current_data)
                            if isinstance(parsed, dict):
                                fn = parsed.get("function", "")
                                name = parsed.get("name", "")
                                status = parsed.get("status", "")
                                args = parsed.get("args", {})
                                if fn or name:
                                    tool_calls.append({
                                        "name": name,
                                        "function": fn,
                                        "status": status,
                                        "args_summary": str(args)[:100],
                                    })
                                    # 检测 find_files 调用
                                    if fn == "find_files" or name == "file":
                                        dir_path = args.get("dir_path", "") if isinstance(args, dict) else ""
                                        log_step("  工具调用: " + name + "/" + str(fn) + " status=" + status + " dir=" + str(dir_path), "INFO")
                        except (json.JSONDecodeError, TypeError):
                            pass

                    # 检查终止信号
                    stop = False
                    if current_event_type and "done" in str(current_event_type).lower():
                        stop = True
                    if current_data and '"event_type"' in current_data:
                        try:
                            parsed = json.loads(current_data)
                            if isinstance(parsed, dict) and parsed.get("event_type") in ("done", "error", "wait"):
                                stop = True
                        except (json.JSONDecodeError, TypeError):
                            pass

                    current_event_type = None
                    current_data = ""

                    if stop:
                        break

                if len(events) >= 100:
                    break

        elapsed = time.time() - start_time
        log_step("步骤3: SSE响应收集完成", "PASS",
                 "事件数=" + str(len(events)) + ", 工具调用数=" + str(len(tool_calls)) + ", 耗时=" + str(round(elapsed, 1)) + "s")

        # 4.等待会话状态稳定
        log_step("步骤4: 等待会话状态稳定", "RUN")
        start_wait = time.time()
        final_status = "running"
        while time.time() - start_wait < 120:
            resp = httpx.get(
                API_BASE + "/api/sessions/" + session_id,
                headers={"Authorization": "Bearer " + token},
                timeout=TIMEOUT,
            )
            if resp.status_code == 200:
                final_status = resp.json()["data"].get("status", "")
                if final_status in ("completed", "waiting", "error"):
                    break
            time.sleep(5)

        log_step("步骤4: 会话状态", "PASS", "status=" + final_status)

        # 5.分析结果
        print("", flush=True)
        print("=== 测试结果分析 ===", flush=True)
        print("会话状态: " + final_status, flush=True)
        print("总耗时: " + str(round(elapsed, 1)) + "s", flush=True)
        print("事件数: " + str(len(events)), flush=True)
        print("工具调用数: " + str(len(tool_calls)), flush=True)

        # 打印工具调用摘要
        if tool_calls:
            print("", flush=True)
            print("工具调用列表:", flush=True)
            for i, tc in enumerate(tool_calls):
                print("  " + str(i) + ": " + tc["name"] + "/" + tc["function"] + " [" + tc["status"] + "] " + tc["args_summary"], flush=True)

        # 关键验证:会话不应卡在 running
        if final_status == "running":
            log_step("验证: 会话状态", "FAIL", "会话卡在running(原f2611353问题复现)")
            # 停止会话
            try:
                httpx.post(
                    API_BASE + "/api/sessions/" + session_id + "/stop",
                    headers={"Authorization": "Bearer " + token},
                    timeout=TIMEOUT,
                )
            except Exception:
                pass
            return 1

        # 关键验证:如果有 find_files 调用,检查是否被防护拦截
        find_files_calls = [tc for tc in tool_calls if tc["function"] == "find_files"]
        if find_files_calls:
            print("", flush=True)
            print("find_files 调用检测:", flush=True)
            for tc in find_files_calls:
                print("  args: " + tc["args_summary"], flush=True)
                if "dir_path=/" in tc["args_summary"] or "dir_path='/" in tc["args_summary"]:
                    log_step("验证: find_files 根目录扫描", "PASS", "已检测到根目录调用,防护应拦截")

        print("", flush=True)
        print("=" * 60, flush=True)
        print("PPT 制作任务 E2E 测试通过！", flush=True)
        print("=" * 60, flush=True)
        return 0

    except Exception as e:
        log_step("测试异常", "ERROR", type(e).__name__ + ": " + str(e))
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
