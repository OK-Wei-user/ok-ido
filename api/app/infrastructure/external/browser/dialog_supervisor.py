#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : dialog_supervisor.py
JS原生对话框监督器 - 通过 page.on("dialog") 拦截 alert/confirm/prompt/beforeunload，
支持三种响应策略并用 Future 机制实现 LLM 延迟响应。

设计要点:
1. page.on("dialog") 注册全局处理器,拦截所有JS原生对话框;
2. auto_dismiss/auto_accept 策略立即响应,must_respond 通过 Future 等待 LLM 决策;
3. must_respond 策略有超时保护(30s),超时自动 dismiss 防止页面卡死;
4. 维护对话框历史记录,供 LLM 在 view_page 中了解已发生的对话框事件。
"""
import asyncio
import logging
import time
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

# 对话框响应策略
POLICY_AUTO_DISMISS = "auto_dismiss"
POLICY_AUTO_ACCEPT = "auto_accept"
POLICY_MUST_RESPOND = "must_respond"

# must_respond 策略下等待 LLM 响应的超时时间(秒),超时自动 dismiss
_MUST_RESPOND_TIMEOUT = 30.0

# 对话框历史最大记录数,防止内存膨胀
_HISTORY_MAX_SIZE = 20


class PendingDialog:
    """待处理的JS原生对话框"""

    def __init__(self, dialog_id: str, kind: str, message: str, default_prompt: str) -> None:
        self.id: str = dialog_id
        self.kind: str = kind  # alert | confirm | prompt | beforeunload
        self.message: str = message
        self.default_prompt: str = default_prompt
        self.opened_at: float = time.time()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "message": self.message,
            "default_prompt": self.default_prompt,
        }


class DialogRecord:
    """已处理的对话框记录(供LLM查看历史)"""

    def __init__(self, dialog_id: str, kind: str, message: str,
                 accept: bool, prompt_text: str, auto: bool) -> None:
        self.id: str = dialog_id
        self.kind: str = kind
        self.message: str = message
        self.accept: bool = accept
        self.prompt_text: str = prompt_text
        self.auto: bool = auto  # 是否自动响应(True)或LLM决策(False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "message": self.message,
            "accept": self.accept,
            "prompt_text": self.prompt_text,
            "auto": self.auto,
        }


class DialogSupervisor:
    """对话框监督器,绑定到单个Playwright Page实例。

    使用流程::
        supervisor = DialogSupervisor(policy=POLICY_AUTO_DISMISS)
        await supervisor.attach(page)
        # ... 用户操作触发对话框 ...
        dialogs = supervisor.get_pending_dialogs()   # 查看待处理
        await supervisor.respond("dialog_1", accept=True)  # LLM响应(must_respond策略)
    """

    def __init__(self, policy: str = POLICY_AUTO_DISMISS) -> None:
        self._policy: str = policy
        self._pending: Dict[str, PendingDialog] = {}
        self._futures: Dict[str, asyncio.Future] = {}
        self._history: List[DialogRecord] = []
        self._counter: int = 0
        self._attached: bool = False

    async def attach(self, page: Any) -> None:
        """绑定到Page: 注册 dialog 事件处理器"""
        if self._attached:
            return
        try:
            page.on("dialog", self._on_dialog)
            self._attached = True
            logger.info("DialogSupervisor已绑定到页面(policy=%s)", self._policy)
        except Exception as e:
            logger.warning(f"DialogSupervisor绑定失败(不影响主流程): {str(e)}")

    def _on_dialog(self, dialog: Any) -> None:
        """dialog事件回调 - Playwright自动调度,页面阻塞直到accept/dismiss完成"""
        self._counter += 1
        dialog_id = f"dialog_{self._counter}"
        kind = getattr(dialog, "type", "unknown")
        message = getattr(dialog, "message", "") or ""
        default_prompt = getattr(dialog, "default_prompt", "") or ""

        pending = PendingDialog(dialog_id, kind, message, default_prompt)
        self._pending[dialog_id] = pending
        logger.info(f"捕获对话框[{dialog_id}]: kind={kind}, message={message[:80]}")

        try:
            if self._policy == POLICY_AUTO_DISMISS:
                asyncio.ensure_future(self._handle_auto(dialog, dialog_id, accept=False))
            elif self._policy == POLICY_AUTO_ACCEPT:
                asyncio.ensure_future(
                    self._handle_auto(dialog, dialog_id, accept=True, prompt_text=default_prompt)
                )
            else:  # must_respond
                asyncio.ensure_future(self._handle_must_respond(dialog, dialog_id))
        except RuntimeError:
            # 无事件循环时回退到同步dismiss(防止页面卡死)
            logger.warning(f"调度对话框[{dialog_id}]处理失败,同步dismiss")
            try:
                dialog.dismiss()
            except Exception:
                pass

    async def _handle_auto(self, dialog: Any, dialog_id: str,
                           accept: bool, prompt_text: str = "") -> None:
        """自动响应策略: 立即 accept/dismiss"""
        try:
            if accept:
                await dialog.accept(prompt_text)
            else:
                await dialog.dismiss()
        except Exception as e:
            logger.warning(f"自动响应对话框[{dialog_id}]失败: {str(e)}")
        finally:
            pending = self._pending.pop(dialog_id, None)
            if pending:
                self._add_to_history(pending, accept, prompt_text, auto=True)

    async def _handle_must_respond(self, dialog: Any, dialog_id: str) -> None:
        """等待LLM响应策略: 通过Future等待respond()调用,超时自动dismiss"""
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._futures[dialog_id] = future

        try:
            accept, prompt_text = await asyncio.wait_for(
                future, timeout=_MUST_RESPOND_TIMEOUT,
            )
            if accept:
                await dialog.accept(prompt_text)
            else:
                await dialog.dismiss()
            pending = self._pending.pop(dialog_id, None)
            if pending:
                self._add_to_history(pending, accept, prompt_text, auto=False)
        except asyncio.TimeoutError:
            logger.warning(
                f"对话框[{dialog_id}]等待LLM响应超时({_MUST_RESPOND_TIMEOUT}s),自动dismiss"
            )
            try:
                await dialog.dismiss()
            except Exception:
                pass
            pending = self._pending.pop(dialog_id, None)
            if pending:
                self._add_to_history(pending, False, "", auto=True)
        except Exception as e:
            logger.error(f"处理对话框[{dialog_id}]异常: {str(e)}")
            try:
                await dialog.dismiss()
            except Exception:
                pass
            pending = self._pending.pop(dialog_id, None)
            if pending:
                self._add_to_history(pending, False, "", auto=True)
        finally:
            self._futures.pop(dialog_id, None)

    async def respond(self, dialog_id: str, accept: bool, prompt_text: str = "") -> bool:
        """响应指定对话框(must_respond策略下由LLM调用)

        Args:
            dialog_id: 对话框ID(来自get_pending_dialogs)
            accept: True=接受, False=取消
            prompt_text: prompt对话框的输入文本

        Returns:
            True=响应成功, False=对话框不存在或已响应
        """
        future = self._futures.get(dialog_id)
        if not future or future.done():
            return False
        future.set_result((accept, prompt_text))
        logger.info(f"LLM响应对话框[{dialog_id}]: accept={accept}")
        return True

    def get_pending_dialogs(self) -> List[dict]:
        """获取当前待处理对话框列表(供LLM在view_page中查看)"""
        return [d.to_dict() for d in self._pending.values()]

    def get_dialog_history(self) -> List[dict]:
        """获取最近处理的对话框历史(供LLM了解已发生的对话框事件)"""
        return [r.to_dict() for r in self._history]

    def _add_to_history(self, pending: PendingDialog, accept: bool,
                        prompt_text: str, auto: bool) -> None:
        """添加对话框记录到历史列表,超过上限时截断"""
        self._history.append(DialogRecord(
            pending.id, pending.kind, pending.message,
            accept, prompt_text, auto,
        ))
        if len(self._history) > _HISTORY_MAX_SIZE:
            self._history = self._history[-_HISTORY_MAX_SIZE:]

    def clear(self) -> None:
        """清空pending队列和历史记录(页面导航后调用)"""
        for future in self._futures.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._futures.clear()
        self._history.clear()
