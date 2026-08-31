#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沙箱回调代理(Batch 40 / 方向1: P11 沙箱异步任务通知)

设计目标:
- 监控沙箱内后台任务完成状态,主动 HTTP POST 回调 API
- 替代 API 层轮询,实现"沙箱完成即通知",延迟 < 1s
- 轻量: 纯标准库实现,无外部依赖

工作流程:
1. API 通过 shell_execute(async_mode=true) 提交命令时,在沙箱创建状态文件
   /tmp/task_status/{task_id}.json (含 api_callback_url)
2. 沙箱 exec_command 完成后,向状态文件写入结果
3. callback_agent 轮询 /tmp/task_status/ 目录,发现已完成任务后 HTTP POST 到 API
4. POST 成功后删除状态文件(幂等: 已删除的文件不会重复回调)

部署方式:
- supervisord 配置中新增 callback_agent 进程,随沙箱启动
- 与 supervisord 其他进程(xvfb, chromium, vnc)平级运行

降级策略:
- callback_agent 进程异常退出时,supervisord 自动重启
- API 侧保留 asyncio.Task 降级兜底(回调失败时仍由 API 轮询)
"""
import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

# 任务状态目录
_TASK_STATUS_DIR = Path("/tmp/task_status")
# 轮询间隔(秒)
_POLL_INTERVAL = 1.0
# HTTP 回调超时(秒)
_HTTP_TIMEOUT = 5
# 最大重试次数
_MAX_RETRIES = 3
# 重试间隔(秒)
_RETRY_INTERVAL = 2.0


def ensure_status_dir() -> None:
    """确保任务状态目录存在"""
    _TASK_STATUS_DIR.mkdir(parents=True, exist_ok=True)


def send_callback(task_id: str, callback_url: str, payload: dict) -> bool:
    """发送 HTTP 回调到 API

    Args:
        task_id: 任务 ID
        callback_url: API 回调端点 URL
        payload: 回调载荷(含 success/message/data)

    Returns:
        True 表示回调成功
    """
    for attempt in range(_MAX_RETRIES):
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                callback_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                if resp.status == 200:
                    print(f"[callback_agent] 回调成功: task_id={task_id}")
                    return True
                else:
                    print(f"[callback_agent] 回调非200: task_id={task_id}, status={resp.status}")
        except urllib.error.URLError as e:
            print(f"[callback_agent] 回调失败(第{attempt+1}次): task_id={task_id}, error={e}")
        except Exception as e:
            print(f"[callback_agent] 回调异常(第{attempt+1}次): task_id={task_id}, error={e}")

        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_INTERVAL)

    print(f"[callback_agent] 回调最终失败: task_id={task_id}, 已重试{_MAX_RETRIES}次")
    return False


def process_completed_tasks() -> int:
    """扫描状态目录,处理已完成但未回调的任务

    Returns:
        本次处理的任务数
    """
    if not _TASK_STATUS_DIR.exists():
        return 0

    processed = 0
    for status_file in _TASK_STATUS_DIR.glob("*.json"):
        try:
            raw = status_file.read_text(encoding="utf-8")
            task_data = json.loads(raw)

            # 检查任务是否已完成(result 字段存在表示已完成)
            if "result" not in task_data:
                continue

            task_id = task_data.get("task_id", status_file.stem)
            callback_url = task_data.get("api_callback_url", "")
            if not callback_url:
                print(f"[callback_agent] 任务[{task_id}]无回调URL,跳过")
                status_file.unlink(missing_ok=True)
                continue

            payload = {
                "task_id": task_id,
                "success": task_data["result"].get("success", False),
                "message": task_data["result"].get("message", ""),
                "data": task_data["result"].get("data"),
                "exit_code": task_data["result"].get("exit_code", -1),
            }

            if send_callback(task_id, callback_url, payload):
                # 回调成功,删除状态文件(幂等)
                status_file.unlink(missing_ok=True)
                processed += 1
            else:
                # 回调失败,保留文件等待下次重试(但标记重试次数)
                retries = task_data.get("_callback_retries", 0) + 1
                if retries >= _MAX_RETRIES:
                    print(f"[callback_agent] 任务[{task_id}]回调重试耗尽,删除状态文件")
                    status_file.unlink(missing_ok=True)
                else:
                    task_data["_callback_retries"] = retries
                    status_file.write_text(
                        json.dumps(task_data, ensure_ascii=False), encoding="utf-8"
                    )

        except json.JSONDecodeError as e:
            print(f"[callback_agent] 状态文件解析失败: {status_file}, error={e}")
            status_file.unlink(missing_ok=True)
        except Exception as e:
            print(f"[callback_agent] 处理状态文件异常: {status_file}, error={e}")

    return processed


def main() -> None:
    """主循环: 轮询状态目录,处理已完成任务"""
    print("[callback_agent] 启动沙箱回调代理")
    ensure_status_dir()

    while True:
        try:
            count = process_completed_tasks()
            if count > 0:
                print(f"[callback_agent] 本次处理 {count} 个已完成任务")
        except Exception as e:
            print(f"[callback_agent] 主循环异常(继续运行): {e}")

        time.sleep(_POLL_INTERVAL)


if __name__ == "__main__":
    main()
