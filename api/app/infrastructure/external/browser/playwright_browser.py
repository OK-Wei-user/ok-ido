#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : playwright_browser.py
工业级DOM浏览器实现 - 纯DOM结构驱动、CDP原生连接、SPA适配、上下文压缩、五级容错、并发安全、操作指标
"""
import asyncio
import base64
import json
import logging
import os
import re
import time
from collections import deque
from typing import Optional, List, Any, Dict
from urllib.parse import urlparse

from markdownify import markdownify
from playwright.async_api import async_playwright, Playwright, Browser, Page

from app.domain.external.browser import Browser as BrowserProtocol
from app.domain.external.llm import LLM
from app.domain.models.tool_result import ToolResult
from .accessibility_snapshot import (
    build_ref_map,
    resolve_ref,
    format_refs_for_llm,
    _OFFSCREEN_MAX_DISPLAY,
    _VISIBLE_ELEMENTS_SAFETY_CEILING,
)
from .dialog_supervisor import DialogSupervisor, POLICY_AUTO_DISMISS
from .visual_click import visual_click
from .playwright_browser_fun import (
    GET_VISIBLE_CONTENT_FUNC,
    GET_INTERACTIVE_ELEMENTS_FUNC,
    INJECT_CONSOLE_LOGS_FUNC,
    GET_PAGE_STATE_FUNC,
    WAIT_DOM_STABLE_FUNC,
    DETECT_BLOCKING_ELEMENTS_FUNC,
    DISMISS_BLOCKING_ELEMENT_FUNC,
    CHECK_ELEMENT_INTERACTABLE_FUNC,
    DISPATCH_CLICK_FUNC,
    SCROLL_TO_TEXT_FUNC,
    LOCATE_BY_SEMANTIC_FUNC,
    CHECK_SPA_CONTENT_READY_FUNC,
    EXTRACT_SPA_CONTENT_FUNC,
    EXTRACT_DOC_CONTENT_FUNC,
    DETECT_DOC_CONTAINER_FUNC,
)

logger = logging.getLogger(__name__)

_MAX_CONTENT_LENGTH = 50000
_CLICK_RETRIES = 5
_DOM_STABLE_TIMEOUT = 10
_LOADING_WAIT_TIMEOUT = 8
_SPA_CONTENT_WAIT_TIMEOUT = 8  # SPA内容渲染等待超时(秒):VitePress/React/Vue等异步渲染兜底
_NAVIGATE_TIMEOUT = 30  # 页面导航超时(秒)
_NAVIGATE_RETRIES = 2  # 页面导航重试次数(总尝试=_NAVIGATE_RETRIES+1),应对瞬时网络抖动
_SELECT_RETRIES = 3  # 下拉选项选择重试次数
_SCREENSHOT_RETRIES = 2  # 截图重试次数
_CONSOLE_EXEC_TIMEOUT = 30  # console_exec执行用户JS的超时(秒),防止死循环/巨大计算阻塞浏览器实例
_NEW_TAB_READY_TIMEOUT = 2.0  # 新标签页就绪等待超时(秒),防止操作白屏
_RETURN_SCREENSHOT = os.environ.get("BROWSER_RETURN_SCREENSHOT", "true").lower() == "true"  # view_page是否返回截图(多模态LLM视觉辅助)
# 长页面(>5000字符)是否调用文本LLM生成摘要。默认关闭:摘要会丢失导航菜单等元素信息,
# 导致LLM找不到目标元素后滥用console_exec(会话c66c5ff2根因);文档模式主张"返回结构而非摘要"。
# 需要降低超长页面上下文占用时可设为true开启。
_CONTENT_SUMMARY = os.environ.get("BROWSER_CONTENT_SUMMARY", "false").lower() == "true"
_SCREENSHOT_JPEG_QUALITY = 60  # 截图JPEG质量(降低base64体积与token消耗)
_SCREENSHOT_MAX_WIDTH = 1280  # 截图最大宽度(像素),超过则缩放
_ACCESSIBILITY_MAX_NODES = 200  # accessibility tree最大节点数(防止巨型页面膨胀)
# 空白/浏览器内置页面URL前缀集合,识别需关闭的旧标签页(智能体打开新页面场景)
_BLANK_PAGE_URLS = (
    "about:blank", "chrome://newtab/", "chrome://new-tab-page/",
    "chrome-search://", "javascript:", "edge://", "view-source:",
)
# 顶层return语句检测正则: 匹配return关键字后跟单词边界(覆盖return空格/return{/return(/return[等所有变体)
# \b确保不匹配returnValue等标识符; 仅匹配语句开头,不解析AST(启发式优先保障可用性)
_RETURN_STMT_RE = re.compile(r'^return\b')
# 语句声明关键字检测: const/let/var声明是语句而非表达式,Playwright evaluate无法直接执行
# 匹配语句开头的声明关键字(后跟空格),用于触发IIFE包装
_STMT_KEYWORD_RE = re.compile(r'^(?:const|let|var)\s+')
# 交互元素基础回退查询: 主选择器(GET_INTERACTIVE_ELEMENTS_FUNC)因Shadow DOM/CSP/复杂SPA
# 返回空时使用。仅查询原生交互元素(a/button/input/textarea/select)和ARIA角色,
# 不含UI框架类名(.el-*/.ant-*),确保在极端环境下仍能获取基本交互能力。
_FALLBACK_INTERACTIVE_FUNC = """() => {
    const SELECTOR = 'a,button,input,textarea,select,[role="button"],[role="link"],[role="menuitem"],[role="tab"],[role="option"],[tabindex]:not([tabindex="-1"])';
    const elements = [];
    const found = document.querySelectorAll(SELECTOR);
    let idx = 0;
    for (const el of found) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') continue;
        const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().substring(0, 100);
        elements.push({
            tag: el.tagName.toLowerCase(),
            text: text,
            index: idx++,
            inViewport: r.bottom > 0 && r.top < window.innerHeight,
            inShadowDOM: false,
            inDialog: false,
        });
    }
    return elements;
}"""


class PlaywrightBrowser(BrowserProtocol):
    """工业级Playwright浏览器实现 - 并发安全、操作指标监控、五级点击容错、SPA适配"""

    def __init__(
            self,
            cdp_url: str,
            llm: Optional[LLM] = None,
            multimodal_llm: Optional[LLM] = None,
    ) -> None:
        # 文本LLM: 长页面内容摘要(受_CONTENT_SUMMARY开关控制)
        self.llm: Optional[LLM] = llm
        # 多模态LLM: visual_click视觉兜底(五级DOM策略失败后分析截图定位坐标)。
        # 必须是支持图像输入的视觉模型,None时visual_click优雅降级返回False,绝不复用纯文本self.llm避免误点。
        self.multimodal_llm: Optional[LLM] = multimodal_llm
        self.cdp_url: str = cdp_url
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self._last_url: str = ""
        self._operation_lock: asyncio.Lock = asyncio.Lock()
        # 可交互元素快照版本号：每次刷新递增，操作结果携带该版本供LLM对齐
        self._snapshot_version: int = 0
        # ref 引用映射表: 基于accessibility tree为可交互元素分配的语义引用(@e1/@e2)
        # 每次刷新交互元素缓存后重建,导航后清空避免引用过期导致误操作
        self._ref_map: Dict[str, dict] = {}
        # 上一次完整快照的ref_map,用于include_diff检测SPA两次快照差异;
        # 导航后清空(新页面无前次可比),_extract_interactive_elements刷新前保存前次
        self._prev_ref_map: Dict[str, dict] = {}
        # JS原生对话框监督器: 拦截alert/confirm/prompt,支持LLM延迟响应(must_respond策略)
        # 默认auto_dismiss策略避免阻塞页面,由view_page/navigate暴露pending_dialogs
        self._dialog_supervisor: Optional[DialogSupervisor] = None
        # XHR/fetch请求日志: page.on监听捕获,供browser_network_requests工具查询SPA异步通信。
        # 仅记xhr/fetch类型(排除图片/脚本),deque上限防膨胀,导航/清理时清空防残留。
        self._network_log: deque = deque(maxlen=100)

    # ==================== 辅助方法 ====================

    def _log_action(self, action: str, success: bool, duration: float, **extra) -> None:
        """结构化记录浏览器操作指标，便于监控与排查"""
        status = "success" if success else "failed"
        extra_str = " ".join(f"{k}={v}" for k, v in extra.items() if v is not None)
        logger.info(f"browser_action|{action}|{status}|{duration:.3f}s|{extra_str}")

    @staticmethod
    def _validate_url(url: str) -> bool:
        """校验URL格式合法性，必须包含http/https协议前缀"""
        try:
            parsed = urlparse(url)
            return parsed.scheme in ("http", "https") and bool(parsed.netloc)
        except Exception:
            return False

    async def _take_view_screenshot(self) -> Optional[str]:
        """截取当前视口图片并转base64 jpeg，供多模态LLM视觉辅助。
        失败时返回None不影响主流程；体积通过JPEG质量与最大宽度控制。
        PIL不可用时降级返回原始PNG base64(体积较大但保证视觉信息可用)。"""
        if not _RETURN_SCREENSHOT:
            return None
        try:
            png_bytes = await self.page.screenshot(type="png", full_page=False)
            if not png_bytes:
                return None
            # 转jpeg降低体积(浏览器截图场景png通常远大于jpeg)
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(png_bytes))
                if img.width > _SCREENSHOT_MAX_WIDTH:
                    ratio = _SCREENSHOT_MAX_WIDTH / img.width
                    img = img.resize(
                        (_SCREENSHOT_MAX_WIDTH, int(img.height * ratio)),
                        Image.LANCZOS,
                    )
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=_SCREENSHOT_JPEG_QUALITY)
                return base64.b64encode(buf.getvalue()).decode("ascii")
            except ImportError:
                # PIL不可用: 降级返回原始PNG base64(体积较大但保证视觉信息可用)
                logger.debug("PIL不可用,view_page截图降级返回原始PNG")
                return base64.b64encode(png_bytes).decode("ascii")
        except Exception as e:
            logger.debug(f"view_page截图失败(不影响主流程): {str(e)}")
            return None

    async def _extract_accessibility_tree(self) -> str:
        """提取Playwright原生accessibility语义树并文本化。
        与DOM文本并行返回，弥补纯DOM丢失的ARIA语义层级。
        巨型页面截断到_ACCESSIBILITY_MAX_NODES节点防止上下文膨胀。"""
        try:
            snapshot = await self.page.accessibility.snapshot()
            if not snapshot:
                return ""
            lines: List[str] = []
            self._flatten_accessibility(snapshot, 0, lines)
            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"accessibility树提取失败(不影响主流程): {str(e)}")
            return ""

    def _flatten_accessibility(self, node: Dict[str, Any], depth: int, lines: List[str]) -> None:
        """递归拍平accessibility快照为缩进文本，保留role/name/value/checked等语义字段"""
        if not isinstance(node, dict) or len(lines) >= _ACCESSIBILITY_MAX_NODES:
            return
        role = node.get("role", "")
        name = node.get("name", "")
        value = node.get("value", "")
        checked = node.get("checked")
        selected = node.get("selected")
        parts = [f"{'  ' * depth}- {role}"]
        if name:
            parts.append(f'"{name}"')
        if value:
            parts.append(f"value={value}")
        if checked is not None:
            parts.append("checked" if checked else "unchecked")
        if selected is not None:
            parts.append("selected" if selected else "unselected")
        lines.append(" ".join(parts))
        for child in node.get("children", []) or []:
            self._flatten_accessibility(child, depth + 1, lines)

    async def _refresh_interactive_cache(self) -> None:
        """操作后刷新可交互元素缓存，避免元素索引过期"""
        try:
            await self._extract_interactive_elements()
        except Exception as e:
            logger.debug(f"刷新交互元素缓存失败(不影响主流程): {str(e)}")

    def _current_snapshot_version(self) -> int:
        """获取当前快照版本号(0表示尚未初始化)"""
        return self._snapshot_version

    # ==================== 浏览器内部管理 ====================

    async def _ensure_browser(self) -> None:
        if not self.browser or not self.page:
            if not await self.initialize():
                raise RuntimeError("初始化Playwright浏览器失败")

    async def _ensure_page(self) -> None:
        await self._ensure_browser()
        if not self.page:
            self.page = await self.browser.new_page()
            return
        contexts = self.browser.contexts
        if contexts:
            pages = contexts[0].pages
            if pages and self.page != pages[-1]:
                self.page = pages[-1]

    async def _wait_dom_stable(self, timeout: int = _DOM_STABLE_TIMEOUT) -> None:
        # JS函数接受timeout参数(ms),用位置参数传递(Playwright evaluate的arg参数)
        try:
            await self.page.evaluate(WAIT_DOM_STABLE_FUNC, timeout * 1000)
        except Exception:
            await asyncio.sleep(1)

    async def _wait_for_loading_disappear(self, timeout: int = _LOADING_WAIT_TIMEOUT) -> None:
        # 加载遮罩选择器:覆盖element-plus/ant全家桶+通用兜底(自研组件库)
        loading_selectors = [
            '[class*="loading-mask"]', '[class*="spinner-overlay"]',
            '.el-loading-mask', '.ant-spin', '[class*="skeleton"]',
            '[class*="loading-container"]', '[class*="page-loading"]',
        ]
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            any_loading = False
            for sel in loading_selectors:
                try:
                    el = await self.page.query_selector(sel)
                    if el:
                        visible = await el.evaluate(
                            """(el) => { const s=window.getComputedStyle(el); return s.display!=='none' && s.visibility!=='hidden'; }"""
                        )
                        if visible:
                            any_loading = True
                            break
                except Exception:
                    continue
            if not any_loading:
                return
            await asyncio.sleep(0.3)

    async def _wait_for_content_ready(self, timeout: int = _SPA_CONTENT_WAIT_TIMEOUT) -> None:
        """等待SPA页面内容渲染完成,支持VitePress/Vue/React等框架。

        VitePress/React/Vue等SPA框架在domcontentloaded后异步加载JS模块渲染内容,
        _wait_dom_stable可能因DOM短暂稳定(空app div)提前返回,导致内容提取为空。
        此方法轮询SPA框架内容容器,确保渲染完成后再提取,避免LLM误判页面未加载。

        检测策略(按优先级):
        1. VitePress专用: .vp-doc/.VPContent容器文本长度>20
        2. Vue3: [data-v-app]容器文本长度>20
        3. 通用SPA: #app容器文本长度>20
        4. 通用回退: body.innerText长度>50

        关键修复(会话e5cce96a根因):
        - evaluate异常try/except移入循环内部,SPA路由切换中execution context
          可能暂时不可用,旧版except在循环外导致单次异常即退出等待
        - 内容稳定性检查: 连续2次检测到内容才返回,防止路由切换中旧内容
          短暂出现后消失的误判
        - about:blank等无内容页面不会阻塞(超时后静默返回)
        """
        try:
            deadline = asyncio.get_event_loop().time() + timeout
            stable_count = 0  # 连续检测到内容的次数(稳定性检查)
            while asyncio.get_event_loop().time() < deadline:
                try:
                    result = await self.page.evaluate(CHECK_SPA_CONTENT_READY_FUNC)
                    if result and result.get("ready"):
                        stable_count += 1
                        if stable_count >= 2:  # 连续2次确认内容稳定
                            await self._wait_dom_stable(timeout=3)
                            return
                    else:
                        stable_count = 0
                except Exception:
                    # SPA路由切换中execution context可能暂时不可用,继续轮询
                    stable_count = 0
                await asyncio.sleep(0.5)
        except Exception:
            # 轮询失败不阻塞主流程(页面可能是about:blank等无内容页面)
            pass

    async def _auto_dismiss_blocking_elements(self) -> List[str]:
        dismissed = []
        try:
            blocking_list = await self.page.evaluate(DETECT_BLOCKING_ELEMENTS_FUNC)
            for item in blocking_list:
                category = item.get("category", "")
                close_selectors = item.get("closeSelectors", [])
                if category == "loading_overlay":
                    await self._wait_for_loading_disappear()
                    dismissed.append(category)
                    continue
                for close_sel in close_selectors:
                    try:
                        closed = await self.page.evaluate(DISMISS_BLOCKING_ELEMENT_FUNC, close_sel)
                        if closed:
                            await asyncio.sleep(0.5)
                            dismissed.append(category)
                            break
                    except Exception:
                        continue
            if blocking_list and not dismissed:
                try:
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(0.5)
                    dismissed.append("escape_key")
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"阻塞元素检测失败: {str(e)}")
        return dismissed

    async def _post_action_sync(self) -> None:
        await self._sync_new_tab()
        await self._wait_dom_stable()
        await self._wait_for_loading_disappear()
        # SPA内容渲染等待: click/input等操作可能触发SPA路由切换(VitePress/Vue/React),
        # _wait_dom_stable可能因DOM短暂稳定(空app div)提前返回,补充内容就绪检测
        # 确保SPA已渲染。(会话e5cce96a根因: click导航后console_exec/view返回空,
        # LLM被迫多次探测,增加操作步骤)
        await self._wait_for_content_ready()
        # 操作后立即刷新交互元素缓存，避免下次操作"取不到→刷新重试"的额外往返
        await self._refresh_interactive_cache()

    async def _sync_new_tab(self) -> None:
        """同步新标签页:关闭空白旧标签,self.page跟随最新标签页。
        切换前等待新page的readyState达到interactive以上(最多_NEW_TAB_READY_TIMEOUT秒),
        防止智能体点击target=_blank后立即操作白屏失败。"""
        try:
            contexts = self.browser.contexts
            if not contexts:
                return
            pages = contexts[0].pages
            if len(pages) > 1:
                for p in pages[:-1]:
                    if self._is_blank_page_url(p.url):
                        try:
                            await p.close()
                        except Exception:
                            pass
                new_page = pages[-1]
                # 等待新标签页就绪,避免操作白屏
                ready = await self._wait_page_interactive(new_page, _NEW_TAB_READY_TIMEOUT)
                if not ready:
                    logger.warning(f"新标签页就绪等待超时,仍切换(可能未完成加载): {new_page.url}")
                self.page = new_page
        except Exception as e:
            logger.warning(f"同步新标签页失败: {str(e)}")

    @staticmethod
    def _is_blank_page_url(url: str) -> bool:
        """判断URL是否属于空白/浏览器内置页面,需关闭以避免标签页堆积"""
        if not url:
            return True
        return any(url == prefix or url.startswith(prefix) for prefix in _BLANK_PAGE_URLS)

    async def _wait_page_interactive(self, page, timeout: float = _NEW_TAB_READY_TIMEOUT) -> bool:
        """等待page的readyState达到interactive以上,防止操作白屏。
        超时返回False,调用方决定是否仍切换。"""
        try:
            start = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start < timeout:
                ready = await page.evaluate(
                    "() => document.readyState === 'interactive' || document.readyState === 'complete'"
                )
                if ready:
                    return True
                await asyncio.sleep(0.2)
            return False
        except Exception:
            return False

    async def _extract_content(self) -> str:
        await self._ensure_page()
        content = ""

        # 文档容器优先路径: 文档型页面(.vp-doc/main/article等容器)使用视口感知提取。
        # 根因(会话4f441827/6794ac3c): DOM树遍历对文档型SPA返回全body文本(含导航/侧边栏/页脚~50KB),
        # 截断后35%保底预算(~4KB)只够导航文本,LLM看不到文档正文("对齐方式"章节),被迫51次操作绕过浏览器。
        # 文档容器检测命中时走EXTRACT_DOC_CONTENT_FUNC(容器范围+视口感知+8KB上限),
        # 跳过DOM树遍历,既保留文档正文又降低提取开销。
        # 容器优先级覆盖: VitePress标准(.vp-doc) + Element Plus自定义主题(main) + 通用语义(article)。
        if not content.strip():
            try:
                doc_source = await self.page.evaluate(DETECT_DOC_CONTAINER_FUNC)
                if doc_source:
                    doc_content = await self.page.evaluate(EXTRACT_DOC_CONTENT_FUNC)
                    if doc_content and doc_content.strip():
                        content = doc_content
                        logger.info(
                            f"文档容器视口感知提取成功(source={doc_source},长度={len(content)})"
                        )
            except Exception as doc_err:
                logger.debug(f"文档容器提取失败,回退DOM树遍历: {str(doc_err)}")

        # 通用路径: DOM树结构化遍历(保留交互元素语义,适用于Web App/传统页面)
        if not content.strip():
            try:
                dom_json = await self.page.evaluate(GET_VISIBLE_CONTENT_FUNC)
                dom_tree = json.loads(dom_json) if dom_json else {}
                content = self._dom_tree_to_text(dom_tree)
            except Exception as e:
                logger.warning(f"DOM结构提取失败，回退到HTML模式: {str(e)}")
                try:
                    html = await self.page.content()
                    content = markdownify(html) if html else ""
                except Exception:
                    content = ""

        # 空内容回退链(按优先级):
        # 1. SPA容器直取: VitePress(.vp-doc/.VPContent)等框架内容容器innerText
        # 2. body.innerText: 通用SPA回退
        # (会话e5cce96a根因: DOM树遍历对VitePress页面返回空,body.innerText也为空,
        #  但.vp-doc容器innerText可直接获取文档内容)
        if not content.strip():
            try:
                spa_content = await self.page.evaluate(EXTRACT_SPA_CONTENT_FUNC)
                if spa_content:
                    content = spa_content
                    logger.info(
                        f"DOM树遍历返回空,SPA容器回退成功(长度={len(content)})"
                    )
            except Exception as spa_err:
                logger.debug(f"SPA容器回退失败: {str(spa_err)}")
        # 最终回退: body.innerText(SPA容器也为空时的兜底)
        if not content.strip():
            try:
                inner_text = await self.page.evaluate(
                    "() => (document.body && document.body.innerText) "
                    "? document.body.innerText.trim() : ''"
                )
                if inner_text:
                    content = inner_text
                    logger.info(
                        f"DOM树遍历返回空,回退到body.innerText成功(长度={len(content)})"
                    )
            except Exception as fallback_err:
                logger.warning(f"body.innerText回退也失败: {str(fallback_err)}")

        if _CONTENT_SUMMARY and self.llm and len(content) > 5000:
            llm_start = time.monotonic()
            try:
                response = await self.llm.invoke([
                    {"role": "system", "content": "提取页面核心信息为简洁Markdown，保留关键数据和交互元素。"},
                    {"role": "user", "content": content[:_MAX_CONTENT_LENGTH]},
                ])
                llm_duration = time.monotonic() - llm_start
                self._log_action("extract_content_llm", True, llm_duration)
                return response.get("content", content[:_MAX_CONTENT_LENGTH])
            except Exception as e:
                llm_duration = time.monotonic() - llm_start
                logger.warning(f"LLM内容摘要失败,降级返回原始内容(耗时{llm_duration:.3f}s): {str(e)}")
                self._log_action("extract_content_llm", False, llm_duration, error=str(e))

        return content[:_MAX_CONTENT_LENGTH]

    @staticmethod
    def _dom_tree_to_text(node: dict, indent: int = 0) -> str:
        """DOM树转文本,视口优先输出(会话437cbc75根因修复)

        视口内(non-offscreen)子节点先输出,视口外(offscreen)子节点后输出。
        截断时从头保留→视口内容优先保留,视口外内容(可滚动恢复)优先丢弃。
        契合"快照临时存在、滚动后内容交由LLM理解当前会话"原则:
        LLM滚动到表格区域后,表格数据(视口内)在content前部,不被截断;
        导航/页脚(视口外)在content后部,截断后LLM仍可通过interactive_elements定位。
        """
        if not node or not isinstance(node, dict):
            return ""
        parts = []
        tag = node.get("tag", "")
        text = node.get("text", "")
        attrs = node.get("attrs", {})
        interactive = node.get("interactive", False)
        offscreen = node.get("offscreen", False)

        prefix = "  " * indent
        attr_str = ""
        if attrs:
            attr_str = " " + " ".join(f'{k}="{v}"' for k, v in attrs.items())

        marker = ""
        if interactive:
            marker = " [interactive]"
        if offscreen:
            marker += " [offscreen]"

        if text:
            parts.append(f"{prefix}<{tag}{attr_str}>{marker} {text}</{tag}>")
        elif marker:
            parts.append(f"{prefix}<{tag}{attr_str}>{marker}</{tag}>")

        # 视口优先: 视口内子节点先输出,视口外子节点后输出
        # 确保截断(从头保留)时视口内容优先保留,视口外内容优先丢弃
        children = node.get("children", [])
        viewport_children = [c for c in children if not c.get("offscreen", False)]
        offscreen_children = [c for c in children if c.get("offscreen", False)]
        for child in viewport_children:
            parts.append(PlaywrightBrowser._dom_tree_to_text(child, indent + 1))
        for child in offscreen_children:
            parts.append(PlaywrightBrowser._dom_tree_to_text(child, indent + 1))

        return "\n".join(p for p in parts if p)

    async def _extract_interactive_elements(self) -> List[dict]:
        await self._ensure_page()
        self.page.interactive_elements_cache = []
        try:
            elements = await self.page.evaluate(GET_INTERACTIVE_ELEMENTS_FUNC)
        except Exception as e:
            logger.warning(f"交互元素提取失败,尝试基础回退: {str(e)}")
            elements = []
        # 空结果回退: 主选择器可能因Shadow DOM/CSP/复杂SPA结构返回空,
        # 回退到基础选择器确保至少获取a/button/input等原生交互元素
        if not elements:
            try:
                elements = await self.page.evaluate(_FALLBACK_INTERACTIVE_FUNC)
                if elements:
                    logger.info(f"主选择器返回空,基础回退成功(获取{len(elements)}个元素)")
            except Exception as fallback_err:
                logger.warning(f"基础回退也失败: {str(fallback_err)}")
        self.page.interactive_elements_cache = elements
        # 刷新成功后递增快照版本号，供操作结果对齐与过期检测
        self._snapshot_version += 1
        # 保存前次ref_map快照(供include_diff比对SPA两次快照差异),再重建当前ref_map
        self._prev_ref_map = dict(self._ref_map)
        # 同步重建 ref_map,保证 ref 与 index 共用同一份交互元素快照
        await self._build_ref_map(elements)
        return elements

    async def _build_ref_map(self, elements: List[dict]) -> None:
        """基于当前交互元素快照构建 ref 引用映射表。
        accessibility tree 提取失败时退化到 semanticAttrs,不影响主流程。"""
        try:
            self._ref_map = await build_ref_map(self.page, elements)
        except Exception as e:
            logger.debug(f"构建 ref_map 失败(不影响主流程): {str(e)}")
            self._ref_map = {}

    def _compute_snapshot_diff(self) -> dict:
        """计算当前_ref_map与_prev_ref_map的差异,供LLM判断SPA重渲染范围。

        返回 {has_diff, added, removed, changed}:
        - added: 新快照新增的ref元素
        - removed: 新快照消失的ref元素
        - changed: 同ref但text/role/name变化的元素
        _prev_ref_map为空(导航后首次快照)时返回has_diff=False。
        """
        if not self._prev_ref_map:
            return {"has_diff": False, "added": [], "removed": [], "changed": []}
        old_keys = set(self._prev_ref_map)
        new_keys = set(self._ref_map)
        added = [self._ref_map[k] for k in sorted(new_keys - old_keys)]
        removed = [self._prev_ref_map[k] for k in sorted(old_keys - new_keys)]
        changed = []
        for k in sorted(old_keys & new_keys):
            old, new = self._prev_ref_map[k], self._ref_map[k]
            if (old.get("text") != new.get("text")
                    or old.get("role") != new.get("role")
                    or old.get("name") != new.get("name")):
                changed.append({"ref": k, "old": old, "new": new})
        return {
            "has_diff": bool(added or removed or changed),
            "added": added,
            "removed": removed,
            "changed": changed,
        }

    def _format_ref_map_for_llm(self) -> List[str]:
        """格式化 ref_map 为 LLM 可读文本列表,供 view_page/navigate 返回"""
        return format_refs_for_llm(self._ref_map)

    async def _format_elements(self, elements: List[dict]) -> List[str]:
        """格式化交互元素列表为LLM可读文本行,可见性优先+状态优先+安全上限兜底。

        架构原则(会话9b0bf463根因修复): 源头不再硬限流可见元素数量,由memory.py的
        truncate_tool_result_dynamic基于剩余token预算动态截断统一控制。旧版80硬限流
        截断295元素文档页导致"215 more visible elements omitted",LLM看不全关键内容。
        三级优先级排序确保动态截断(按尾部丢弃)时关键元素始终保留:
        - P0: 状态标记元素(checked/selected) — 表单选中态是LLM决策的关键信息
        - P1: 对话框内元素(inDialog) — 弹窗是当前交互焦点,优先于背景元素
        - P2: 普通可见元素 — DOM顺序
        可见元素安全上限_VISIBLE_ELEMENTS_SAFETY_CEILING(2000)仅防极端页面JSON爆炸,
        正常页面完整展示。offscreen元素限流到_OFFSCREEN_MAX_DISPLAY(15)。
        仅改变展示顺序与数量,不影响index/ref编号。
        """
        # 可见性分区: 可见元素安全上限兜底,offscreen元素限流展示
        visible_elements = []
        offscreen_elements = []
        for el in elements:
            if el.get("inViewport", True):
                visible_elements.append(el)
            else:
                offscreen_elements.append(el)

        # 三级优先级排序: 状态标记(0) > 对话框(1) > 普通(2),同优先级按index升序
        # sort稳定排序,仅改变展示顺序不影响@eN ref编号
        def _visible_sort_key(el: dict) -> tuple:
            state = el.get("state") or {}
            has_state = state.get("checked") or state.get("selected")
            in_dialog = el.get("inDialog", False)
            priority = 0 if has_state else (1 if in_dialog else 2)
            return (priority, el.get("index", 0))

        visible_elements.sort(key=_visible_sort_key)

        formatted = []

        # 1.可见元素: 安全上限兜底,超出部分(仅极端页面)汇总计数引导text_locator定位
        visible_shown = visible_elements[:_VISIBLE_ELEMENTS_SAFETY_CEILING]
        for el in visible_shown:
            formatted.append(self._format_single_element(el))
        visible_hidden = len(visible_elements) - len(visible_shown)
        if visible_hidden > 0:
            formatted.append(
                f"... ({visible_hidden} more visible elements below viewport, "
                f"use text_locator or scroll to reveal)"
            )

        # 2.offscreen元素: 分区限流展示,超出部分汇总计数
        # 复用accessibility_snapshot的统一阈值,避免magic number重复定义
        offscreen_shown = offscreen_elements[:_OFFSCREEN_MAX_DISPLAY]
        if offscreen_shown:
            formatted.append(
                f"--- offscreen elements ({len(offscreen_elements)} total, showing {len(offscreen_shown)}) ---"
            )
            for el in offscreen_shown:
                formatted.append(self._format_single_element(el))
            hidden_count = len(offscreen_elements) - len(offscreen_shown)
            if hidden_count > 0:
                formatted.append(
                    f"... ({hidden_count} more offscreen elements below viewport, scroll to reveal)"
                )
        return formatted

    @staticmethod
    def _format_single_element(el: dict) -> str:
        """格式化单个交互元素为LLM可读文本行"""
        tag = el.get("tag", "")
        text = el.get("text", "")
        idx = el.get("index", 0)
        in_vp = el.get("inViewport", True)
        in_shadow = el.get("inShadowDOM", False)
        in_dialog = el.get("inDialog", False)
        vp_mark = "" if in_vp else " [offscreen]"
        shadow_mark = " [shadow]" if in_shadow else ""
        dialog_mark = " [dialog]" if in_dialog else ""
        # 表单元素状态标记(供LLM直接判断选中态,减少console_exec)
        state = el.get("state") or {}
        state_mark = ""
        if state.get("checked"):
            state_mark += " [checked]"
        if state.get("selected"):
            state_mark += " [selected]"
        if state.get("disabled"):
            state_mark += " [disabled]"
        return f"{idx}: <{tag}>{text}</{tag}>{state_mark}{vp_mark}{shadow_mark}{dialog_mark}"

    async def _get_element_by_id(self, index: int) -> Optional[Any]:
        """根据索引定位元素。主策略为data-manus-id属性选择器；
        严格CSP页面属性注入失效时，回退到语义属性选择器与XPath文本匹配。
        回退定位的元素会校验tag一致性，防止SPA重渲染导致索引错位误操作。"""
        if (
            not hasattr(self.page, "interactive_elements_cache")
            or not self.page.interactive_elements_cache
            or index >= len(self.page.interactive_elements_cache)
            or index < 0
        ):
            return None
        # 主策略：data-manus-id属性选择器(权威，属性为本次刷新时注入，无需校验tag)
        selector = f'[data-manus-id="manus-element-{index}"]'
        try:
            element = await self.page.query_selector(selector)
            if element:
                return element
        except Exception:
            pass

        # 回退定位：需校验tag一致性，SPA重渲染后同索引元素tag可能变化
        meta = self.page.interactive_elements_cache[index]
        expected_tag = (meta.get("tag") or "").lower()
        # 回退1：用缓存中的语义属性构建CSS选择器(aria-label/role/name等)
        element = await self._locate_by_semantic_attrs(meta)
        if element and await self._is_tag_match(element, expected_tag):
            return element
        # 回退2：tag+文本内容的XPath匹配(处理CSP拦截setAttribute的场景)
        element = await self._locate_by_xpath_text(meta)
        if element and await self._is_tag_match(element, expected_tag):
            return element
        return None

    @staticmethod
    async def _is_tag_match(element: Any, expected_tag: str) -> bool:
        """校验元素tag与期望tag是否一致(大小写不敏感)。
        回退定位可能匹配到SPA重渲染后的错位元素，tag不一致则视为过期，
        返回False使上层走刷新缓存重试路径，避免误操作。"""
        if not expected_tag:
            return True
        try:
            actual = await element.evaluate("el => (el.tagName || '').toLowerCase()")
            if actual and actual != expected_tag:
                logger.warning(f"元素tag不匹配(期望{expected_tag}, 实际{actual})，索引可能已过期")
                return False
        except Exception:
            pass
        return True

    async def _locate_by_semantic_attrs(self, meta: dict) -> Optional[Any]:
        """根据缓存元素的语义属性(aria-label/role/name/title)构建CSS选择器定位。
        用于data-manus-id注入失败时的语义化回退。"""
        attrs = meta.get("semanticAttrs") or {}
        tag = meta.get("tag", "")
        if not attrs or not tag:
            return None
        # 优先用唯一性强的属性：aria-label > data-testid > name > title
        for attr_name in ("aria-label", "data-testid", "name", "title"):
            val = attrs.get(attr_name)
            if not val:
                continue
            try:
                esc_val = val.replace('"', '\\"')
                css = f'{tag}[{attr_name}="{esc_val}"]'
                element = await self.page.query_selector(css)
                if element:
                    return element
            except Exception:
                continue
        return None

    async def _locate_by_xpath_text(self, meta: dict) -> Optional[Any]:
        """根据缓存元素的tag与可见文本构建XPath定位。
        文本截断到60字符并转义双引号，避免XPath注入与超长匹配失败。"""
        tag = meta.get("tag", "")
        text = (meta.get("text") or "").strip()
        if not tag or not text or text == "[No text]":
            return None
        # 截断并清洗文本，去掉placeholder/label前缀等噪声
        clean_text = text
        for prefix in ("[Placeholder:", "[Label:", "[Value:", "["):
            idx = clean_text.find(prefix)
            if idx == 0:
                clean_text = clean_text.split("]")[-1].strip()
                break
        clean_text = clean_text[:60].strip()
        if not clean_text:
            return None
        # 转义XPath字符串字面量中的双引号(用concat处理)
        if '"' in clean_text:
            parts = clean_text.split('"')
            concat_parts = [f'"{p}"' for p in parts if p]
            expr_text = ", '\"', ".join(concat_parts)
            xpath = f"//{tag}[contains(., concat({expr_text}))]"
        else:
            xpath = f'//{tag}[contains(., "{clean_text}")]'
        try:
            element = await self.page.query_selector(f"xpath={xpath}")
            if element:
                return element
        except Exception:
            return None
        return None

    async def _check_element_interactable(self, element) -> dict:
        try:
            return await self.page.evaluate(CHECK_ELEMENT_INTERACTABLE_FUNC, element)
        except Exception:
            return {"interactable": True}

    async def _click_with_retry(
            self, element, timeout: int = 5000,
            target_description: Optional[str] = None,
    ) -> bool:
        """五级DOM容错策略+第六级视觉兜底。
        target_description非空且LLM可用时,五级DOM策略全部失败后调用visual_click兜底。"""
        strategies = [
            self._strategy_normal_click,
            self._strategy_scroll_then_click,
            self._strategy_force_click,
            self._strategy_coordinate_click,
            self._strategy_js_dispatch_click,
        ]
        for attempt, strategy in enumerate(strategies):
            try:
                result = await strategy(element, timeout)
                if result:
                    return True
            except Exception as e:
                if attempt == len(strategies) - 1:
                    logger.error(f"点击元素失败(已尝试{len(strategies)}种策略): {str(e)}")
                await asyncio.sleep(0.3 * (attempt + 1))

        # 第六级视觉兜底: 五级DOM策略全部失败后,调用多模态LLM分析截图定位坐标
        # 强制使用multimodal_llm(视觉模型),严禁复用纯文本self.llm,避免文本模型返回垃圾坐标被误解析为错误点击
        if target_description and self.multimodal_llm is not None:
            logger.info(f"五级DOM策略失败,尝试视觉兜底[{target_description}]")
            return await visual_click(self.page, self.multimodal_llm, target_description)
        return False

    async def _strategy_normal_click(self, element, timeout: int = 5000) -> bool:
        await element.click(timeout=timeout)
        return True

    async def _strategy_scroll_then_click(self, element, timeout: int = 5000) -> bool:
        await self._scroll_into_view(element)
        await asyncio.sleep(0.3)
        await element.click(timeout=timeout)
        return True

    async def _strategy_force_click(self, element, timeout: int = 5000) -> bool:
        await element.click(force=True, timeout=timeout)
        return True

    async def _strategy_coordinate_click(self, element, timeout: int = 5000) -> bool:
        box = await element.bounding_box()
        if not box:
            return False
        await self.page.mouse.click(
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
        )
        return True

    async def _strategy_js_dispatch_click(self, element, timeout: int = 5000) -> bool:
        await self.page.evaluate(DISPATCH_CLICK_FUNC, element)
        return True

    async def _scroll_into_view(self, element) -> bool:
        try:
            await self.page.evaluate(
                """(el) => { if(el) el.scrollIntoView({behavior:'smooth',block:'center'}) }""",
                element,
            )
            await asyncio.sleep(0.5)
            return True
        except Exception:
            return False

    async def _locate_element_by_text(self, text: str) -> Optional[Any]:
        """通过文本语义定位元素,四级策略链(优先级从高到低):
        1. get_by_role(name=exact) - 遍历常见交互角色,accessible name含aria-label,最语义化
        2. get_by_text(exact) - 精确文本匹配,覆盖非role标注的可交互元素
        3. get_by_text(contains) - 子串匹配,仅当唯一匹配(count==1)时使用,避免歧义
        4. evaluate_handle(LOCATE_BY_SEMANTIC_FUNC) - JS回退,处理title/placeholder等属性
        每级策略用locator.count()检查存在性,locator.first.element_handle()获取ElementHandle。"""
        target = (text or "").strip()
        if not target:
            return None

        # 策略1: get_by_role with name=exact (accessible name含aria-label,最语义化)
        for role in ("button", "link", "menuitem", "tab", "option", "treeitem"):
            try:
                locator = self.page.get_by_role(role, name=target, exact=True)
                if await locator.count() > 0:
                    return await locator.first.element_handle()
            except Exception:
                continue

        # 策略2: get_by_text exact (精确文本匹配,覆盖非role标注元素)
        try:
            locator = self.page.get_by_text(target, exact=True)
            if await locator.count() > 0:
                return await locator.first.element_handle()
        except Exception:
            pass

        # 策略3: get_by_text contains (子串匹配,仅当唯一匹配时使用避免歧义)
        try:
            locator = self.page.get_by_text(target)
            if await locator.count() == 1:
                return await locator.first.element_handle()
        except Exception:
            pass

        # 策略4: JS语义属性回退 (aria-label/title/placeholder,处理图标按钮等无可见文本场景)
        try:
            handle = await self.page.evaluate_handle(LOCATE_BY_SEMANTIC_FUNC, target)
            element = handle.as_element() if handle else None
            if element:
                return element
        except Exception:
            pass

        return None

    # ==================== 网络请求监听(SPA异步信号) ====================

    def _attach_network_listeners(self) -> None:
        """挂载XHR/fetch请求监听,供browser_network_requests工具查询。
        失败仅记debug日志,不影响浏览器主流程。"""
        try:
            self.page.on("request", self._on_network_request)
            self.page.on("response", self._on_network_response)
        except Exception as e:
            logger.debug(f"网络请求监听挂载失败(不影响主流程): {str(e)}")

    def _on_network_request(self, request) -> None:
        """请求回调: 仅记录xhr/fetch类型,排除图片/脚本/样式等静态资源。回调内全try/except绝不影响页面。"""
        try:
            if request.resource_type not in ("xhr", "fetch"):
                return
            self._network_log.append({
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "start_time": time.time(),
                "status": None,
                "duration_ms": None,
            })
        except Exception:
            pass

    def _on_network_response(self, response) -> None:
        """响应回调: 回填同URL最近一条未完成请求的状态与耗时。"""
        try:
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            for entry in reversed(self._network_log):
                if entry.get("url") == response.url and entry.get("status") is None:
                    entry["status"] = response.status
                    start = entry.get("start_time")
                    if start:
                        entry["duration_ms"] = int((time.time() - start) * 1000)
                    break
        except Exception:
            pass

    async def initialize(self) -> bool:
        max_retries = 5
        retry_interval = 1
        for attempt in range(max_retries):
            try:
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
                contexts = self.browser.contexts
                if contexts and len(contexts[0].pages) == 1:
                    page = contexts[0].pages[0]
                    if page.url in ("about:blank", "chrome://newtab/", "chrome://new-tab-page/", ""):
                        self.page = page
                    else:
                        self.page = await contexts[0].new_page()
                else:
                    context = contexts[0] if contexts else await self.browser.new_context()
                    self.page = await context.new_page()
                self._last_url = ""
                # 创建并绑定对话框监督器(默认auto_dismiss策略,避免页面阻塞)
                self._dialog_supervisor = DialogSupervisor(policy=POLICY_AUTO_DISMISS)
                await self._dialog_supervisor.attach(self.page)
                # 挂载XHR/fetch请求监听,供browser_network_requests工具查询SPA异步通信
                self._attach_network_listeners()
                return True
            except Exception as e:
                await self.cleanup()
                if attempt == max_retries - 1:
                    logger.error(f"初始化Playwright浏览器失败(已重试{max_retries}次): {str(e)}")
                    return False
                retry_interval = min(retry_interval * 2, 10)
                logger.warning(f"初始化Playwright浏览器失败, 第{attempt + 1}次重试: {str(e)}")
                await asyncio.sleep(retry_interval)
        return False

    async def cleanup(self) -> None:
        try:
            if self.browser:
                for ctx in self.browser.contexts:
                    for p in ctx.pages:
                        if not p.is_closed():
                            await p.close()
            if self.page and not self.page.is_closed():
                await self.page.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            logger.error(f"清理Playwright浏览器资源出错: {str(e)}")
        finally:
            self.page = None
            self.browser = None
            self.playwright = None
            # 清空对话框监督器状态,防止跨生命周期残留
            if self._dialog_supervisor:
                self._dialog_supervisor.clear()
                self._dialog_supervisor = None
            # 清空快照差异基准与网络日志,防止跨生命周期残留
            self._prev_ref_map = {}
            self._ref_map = {}
            self._network_log.clear()

    async def wait_for_page_load(self, timeout: int = 15) -> bool:
        await self._ensure_page()
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout:
            ready = await self.page.evaluate("() => document.readyState === 'complete'")
            if ready:
                return True
            await asyncio.sleep(0.5)
        return False

    # ==================== 浏览器操作实现(无锁内部方法) ====================

    async def _navigate_impl(self, url: str) -> ToolResult:
        """导航实现(无锁)，供navigate和restart复用，避免锁重入死锁。
        导航本身带重试(总尝试_NAVIGATE_RETRIES+1次),与initialize重试策略对称,
        应对瞬时网络抖动;重试仅覆盖page.goto,后续DOM稳定等步骤不重试。"""
        await self._ensure_page()
        try:
            self.page.interactive_elements_cache = []
            # 导航前清空 ref_map 与 diff 基准,避免旧页面引用残留导致误操作/误判diff
            self._ref_map = {}
            self._prev_ref_map = {}
            # 导航是全新页面,旧网络日志无意义,清空防残留
            self._network_log.clear()
            # 导航前清空对话框监督器状态,避免旧页面待处理对话框残留
            if self._dialog_supervisor:
                self._dialog_supervisor.clear()
            # 导航重试:仅覆盖page.goto,应对瞬时网络抖动
            last_nav_error: Optional[Exception] = None
            for attempt in range(_NAVIGATE_RETRIES + 1):
                try:
                    await self.page.goto(
                        url, wait_until="domcontentloaded", timeout=_NAVIGATE_TIMEOUT * 1000,
                    )
                    last_nav_error = None
                    break
                except Exception as nav_e:
                    last_nav_error = nav_e
                    if attempt < _NAVIGATE_RETRIES:
                        retry_delay = 2 ** attempt  # 1s, 2s
                        logger.warning(
                            f"浏览器导航到[{url}]失败(第{attempt + 1}次尝试),"
                            f"{retry_delay}s后重试: {str(nav_e)}"
                        )
                        await asyncio.sleep(retry_delay)
            if last_nav_error is not None:
                return ToolResult(
                    success=False,
                    message=(
                        f"浏览器导航到[{url}]失败(已重试{_NAVIGATE_RETRIES + 1}次): "
                        f"{str(last_nav_error)}"
                    ),
                )
            await self._wait_dom_stable()
            await self._wait_for_loading_disappear()
            # SPA内容渲染等待: VitePress/React/Vue等框架在domcontentloaded后异步渲染,
            # _wait_dom_stable可能因DOM短暂稳定(空app div)提前返回,导致内容提取为空
            await self._wait_for_content_ready()
            await self._auto_dismiss_blocking_elements()
            elements = await self._extract_interactive_elements()
            state = await self.page.evaluate(GET_PAGE_STATE_FUNC)
            self._last_url = state.get("url", url)
            return ToolResult(success=True, data={
                "url": state.get("url", url),
                "title": state.get("title", ""),
                "interactive_elements": await self._format_elements(elements),
                "ref_map": self._format_ref_map_for_llm(),
                "page_state": state,
                "pending_dialogs": self._get_pending_dialogs(),
                "dialog_history": self._get_dialog_history(),
            })
        except Exception as e:
            return ToolResult(success=False, message=f"浏览器导航到[{url}]失败: {str(e)}")

    async def _resolve_ref_validated(self, ref: str) -> Optional[Any]:
        """增强ref解析: data-manus-id定位 + tag校验 + 语义回退。

        解决SPA异步重渲染导致data-manus-id过期/错位问题(会话ab17bf13根因:
        VitePress重渲染移除data-manus-id,刷新重试后index对应不同元素,
        LLM点击了错误区域的元素)。tag校验检测错位,语义回退保持LLM意图。
        """
        info = self._ref_map.get(ref)
        if not info:
            return None
        expected_tag = (info.get("tag") or "").lower()

        # 1.主策略: data-manus-id选择器(唯一,最快)
        element = await resolve_ref(self.page, ref, self._ref_map)
        if element and await self._is_tag_match(element, expected_tag):
            return element
        if element:
            logger.warning(f"ref[{ref}] tag不匹配(data-manus-id可能因SPA重渲染错位),尝试语义回退")

        # 2.语义回退: 用缓存中的语义信息定位(不刷新缓存,保持LLM意图)
        # 复用_get_element_by_id的回退链(semantic attrs + XPath text),避免刷新导致ref号漂移
        try:
            idx = int(ref[2:]) if ref.startswith("@e") else -1
        except ValueError:
            idx = -1
        if idx >= 0:
            element = await self._get_element_by_id(idx)
            if element and await self._is_tag_match(element, expected_tag):
                return element

        return None

    @staticmethod
    def _extract_label_from_ref_text(ref_text: str) -> str:
        """从ref_map缓存的格式化文本中提取标签文本,用于input ref失效时的文本回退定位。

        input元素的ref_map text格式为"[Label:Name]"或"[Placeholder:xxx]"等,
        直接用格式化字符串搜索无法匹配页面实际文本,需提取括号内的标签内容。
        非input元素(如span/a)的text是原始页面文本,直接返回。
        """
        if not ref_text:
            return ""
        # 优先提取[Label:xxx](表单标签,最常见)
        m = re.search(r"\[Label:([^\]]+)\]", ref_text)
        if m:
            return m.group(1).strip()
        # 其次提取[Placeholder:xxx]
        m = re.search(r"\[Placeholder:([^\]]+)\]", ref_text)
        if m:
            return m.group(1).strip()
        # 非格式化文本(如span的"Left"): 直接返回,但过滤掉[No text]等占位符
        if ref_text.startswith("[") and ref_text.endswith("]"):
            return ""
        return ref_text.strip()

    async def _climb_to_clickable_ancestor(self, element) -> Any:
        """非语义交互元素(span/div/li等)向上攀升到最近的可点击祖先。

        会话ab17bf13根因: @e372是<span>Left</span>(el-radio-button__inner),
        直接点击span可能不触发Vue radio选中事件。攀升到label/button祖先
        确保点击触发框架的事件绑定。最多向上查找5层,避免越过语义边界。
        """
        try:
            tag = await element.evaluate("el => (el.tagName || '').toLowerCase()")
        except Exception:
            return element
        # 原生交互标签直接返回(自身即可触发事件)
        if tag in ("a", "button", "input", "textarea", "select"):
            return element
        # 向上查找最近的可点击祖先(覆盖原生标签+ARIA角色+主流UI框架组件类)
        try:
            ancestor = await element.evaluate("""el => {
                const sel = 'label,button,[role="button"],[role="radio"],[role="checkbox"],[role="menuitem"],[role="tab"],[role="option"],.el-radio-button,.el-radio,.el-checkbox-button,.el-checkbox,.ant-radio-button,.ant-radio,.ant-checkbox-wrapper';
                let node = el;
                for (let i = 0; i < 5 && node; i++) {
                    if (node.matches && node.matches(sel)) return node;
                    node = node.parentElement;
                }
                return null;
            }""")
            if ancestor:
                logger.debug(f"攀升到可点击祖先(tag={tag}→{await ancestor.evaluate('el => el.tagName.toLowerCase()')})")
                return ancestor
        except Exception:
            pass
        return element

    async def _click_impl(
            self,
            ref: Optional[str] = None,
            text: Optional[str] = None,
            index: Optional[int] = None,
            coordinate_x: Optional[float] = None,
            coordinate_y: Optional[float] = None,
    ) -> ToolResult:
        """点击实现(无锁)，五级容错策略+快照版本校验+导航检测。
        参数优先级: ref > text > coordinate > index。
        ref为accessibility tree语义引用(最稳定),text语义定位次之(LLM推理步骤少、不依赖易过期索引)。"""
        await self._ensure_page()
        url_before_click = self.page.url if self.page else None  # 记录点击前URL，用于检测意外导航
        try:
            if ref is not None and ref.strip():
                # ref分支: 增强解析(data-manus-id+tag校验+语义回退,防SPA重渲染错位)
                ref_str = ref.strip()
                element = await self._resolve_ref_validated(ref_str)
                if not element:
                    # ref可能因页面未加载完成或缓存过期,刷新后重试一次(最后手段)
                    version_before = self._snapshot_version
                    await self._extract_interactive_elements()
                    element = await self._resolve_ref_validated(ref_str)
                    if not element:
                        # 最终回退: 用ref_map中缓存的文本语义定位(会话d1eb3b5c根因:
                        # SPA重渲染后ref号漂移,@e372在新快照中指向不同元素。但ref_map
                        # 缓存了原始文本如"Left",可用文本定位保持LLM操作意图)。
                        ref_text = (self._ref_map.get(ref_str, {}) or {}).get("text", "")
                        if ref_text:
                            logger.info(f"ref[{ref_str}]失效,回退到文本定位[{ref_text}]")
                            element = await self._locate_element_by_text(ref_text)
                        if not element:
                            return ToolResult(
                                success=False,
                                message=f"ref[{ref_str}]对应的元素不存在或已失效"
                                        + (f",文本[{ref_text}]也未找到" if ref_text else ""),
                            )
                        logger.info(f"ref[{ref_str}]文本回退定位成功")
                    if self._snapshot_version != version_before and self._snapshot_version > 0:
                        logger.info(f"click ref[{ref_str}]触发快照刷新(v{version_before}->v{self._snapshot_version})")

                interactable = await self._check_element_interactable(element)
                if not interactable.get("interactable", True):
                    reason = interactable.get("reason", "unknown")
                    if reason == "occluded":
                        dismissed = await self._auto_dismiss_blocking_elements()
                        if dismissed:
                            await asyncio.sleep(0.5)
                            element = await self._resolve_ref_validated(ref_str)
                            if not element:
                                return ToolResult(success=False, message=f"消除阻塞元素后ref[{ref_str}]的元素不存在")
                    elif reason in ("hidden", "zero_size"):
                        await self._scroll_into_view(element)
                        await asyncio.sleep(0.3)
                    elif reason == "disabled":
                        return ToolResult(success=False, message=f"ref[{ref_str}]的元素已被禁用(disabled)")
                    elif reason == "pointer_events_none":
                        pass

                # 非语义交互元素(span/div)攀升到可点击祖先(确保触发框架事件)
                element = await self._climb_to_clickable_ancestor(element)
                if not await self._click_with_retry(element, target_description=ref_str):
                    return ToolResult(success=False, message=f"点击ref[{ref_str}]的元素失败(已尝试6种策略)")
            elif text is not None and text.strip():
                # text分支: 通过文本语义定位元素(Playwright locator API + JS回退)
                element = await self._locate_element_by_text(text)
                if not element:
                    # 文本定位可能因页面未加载完成,刷新缓存重试一次
                    version_before = self._snapshot_version
                    await self._extract_interactive_elements()
                    element = await self._locate_element_by_text(text)
                    if not element:
                        return ToolResult(success=False, message=f"未找到文本为[{text}]的元素")
                    if self._snapshot_version != version_before and self._snapshot_version > 0:
                        logger.info(f"click文本[{text}]触发快照刷新(v{version_before}->v{self._snapshot_version})")

                interactable = await self._check_element_interactable(element)
                if not interactable.get("interactable", True):
                    reason = interactable.get("reason", "unknown")
                    if reason == "occluded":
                        dismissed = await self._auto_dismiss_blocking_elements()
                        if dismissed:
                            await asyncio.sleep(0.5)
                            element = await self._locate_element_by_text(text)
                            if not element:
                                return ToolResult(success=False, message=f"消除阻塞元素后文本[{text}]的元素不存在")
                    elif reason in ("hidden", "zero_size"):
                        await self._scroll_into_view(element)
                        await asyncio.sleep(0.3)
                    elif reason == "disabled":
                        return ToolResult(success=False, message=f"文本[{text}]的元素已被禁用(disabled)")
                    elif reason == "pointer_events_none":
                        pass

                if not await self._click_with_retry(element, target_description=text):
                    return ToolResult(success=False, message=f"点击文本[{text}]的元素失败(已尝试6种策略)")
            elif coordinate_x is not None and coordinate_y is not None:
                await self.page.mouse.click(coordinate_x, coordinate_y)
            elif index is not None:
                element = await self._get_element_by_id(index)
                if not element:
                    # 索引可能过期，刷新后重试一次
                    version_before = self._snapshot_version
                    await self._extract_interactive_elements()
                    element = await self._get_element_by_id(index)
                    if not element:
                        return ToolResult(success=False, message=f"索引{index}对应的元素不存在")
                    # 刷新后版本变了，提示LLM页面已变化需重新确认索引
                    if self._snapshot_version != version_before and self._snapshot_version > 0:
                        logger.info(f"click索引{index}触发快照刷新(v{version_before}->v{self._snapshot_version})")

                interactable = await self._check_element_interactable(element)
                if not interactable.get("interactable", True):
                    reason = interactable.get("reason", "unknown")
                    if reason == "occluded":
                        dismissed = await self._auto_dismiss_blocking_elements()
                        if dismissed:
                            await asyncio.sleep(0.5)
                            element = await self._get_element_by_id(index)
                            if not element:
                                return ToolResult(success=False, message=f"消除阻塞元素后索引{index}的元素不存在")
                    elif reason in ("hidden", "zero_size"):
                        await self._scroll_into_view(element)
                        await asyncio.sleep(0.3)
                    elif reason == "disabled":
                        return ToolResult(success=False, message=f"索引{index}的元素已被禁用(disabled)")
                    elif reason == "pointer_events_none":
                        pass

                if not await self._click_with_retry(element):
                    return ToolResult(success=False, message=f"点击索引{index}的元素失败(已尝试5种策略,索引无语义描述未启用视觉兜底)")
            else:
                return ToolResult(success=False, message="请提供text、index或coordinate_x/coordinate_y参数")

            await self._post_action_sync()
            # 导航检测：点击后URL变化说明触发了页面跳转(SPA路由切换或全页导航)。
            # 旧版将所有URL变化视为"可能误触"并建议返回原页面,导致LLM点击侧边栏菜单链接后
            # 误以为操作出错而反复navigate回去再重试(会话0a288ffe根因: 33次操作中6次navigate)。
            # 新策略: 提供导航通知+新页面状态(URL/标题/滚动信息),让LLM直接感知当前位置,
            # 无需额外browser_view确认,消除导航循环。
            result_data: Dict[str, Any] = {"snapshot_version": self._snapshot_version}
            url_after_click = self.page.url if self.page else None
            if url_before_click is not None and url_after_click != url_before_click:
                try:
                    state = await self.page.evaluate(GET_PAGE_STATE_FUNC)
                except Exception:
                    state = {"url": url_after_click, "title": ""}
                result_data["navigation_info"] = {
                    "from_url": url_before_click,
                    "to_url": url_after_click,
                    "title": state.get("title", ""),
                }
                result_data["page_state"] = state
                logger.info(
                    f"click触发导航: {url_before_click} -> {url_after_click} "
                    f"(title={state.get('title', '')})"
                )
            return ToolResult(success=True, data=result_data)
        except Exception as e:
            return ToolResult(success=False, message=f"点击操作失败: {str(e)}")

    async def _input_text_to_element(self, element, text: str) -> None:
        """对指定元素执行三级输入策略(keyboard→fill→JS)。
        keyboard.type优先:模拟真实键盘输入触发完整事件链(keydown/input/keyup),Angular/Element/React均兼容;
        fill次选:Playwright原生fill快速但可能绕过框架监听器;
        JS兜底:补全keyup+blur事件,兼容NgZone变更检测。"""
        try:
            # 策略1: 模拟真实键盘输入(click→全选→删除→type),触发完整事件链,框架兼容性最好
            await element.click()
            await self.page.keyboard.press("Control+a")
            await self.page.keyboard.press("Backspace")
            await self.page.keyboard.type(text)
        except Exception:
            try:
                # 策略2: Playwright原生fill,快速但可能绕过框架监听器
                await element.fill(text)
            except Exception:
                # 策略3: JS赋值+完整事件序列(input+change+keyup+blur),兼容Angular/Element NgZone变更检测
                await self.page.evaluate(
                    """(el, txt) => {
                        el.focus();
                        el.value = txt;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                    }""",
                    element, text,
                )

    async def _input_impl(
            self,
            text: str,
            press_enter: bool,
            ref: Optional[str] = None,
            text_locator: Optional[str] = None,
            index: Optional[int] = None,
            coordinate_x: Optional[float] = None,
            coordinate_y: Optional[float] = None,
    ) -> ToolResult:
        """输入实现(无锁)，三级输入策略(keyboard→fill→JS)+快照版本校验。
        参数优先级: ref > text_locator > coordinate > index。
        ref为accessibility tree语义引用(最稳定),text_locator为文本定位兜底。"""
        await self._ensure_page()
        try:
            if ref is not None and ref.strip():
                # ref分支: 增强解析(data-manus-id+tag校验+语义回退,防SPA重渲染错位)
                ref_str = ref.strip()
                element = await self._resolve_ref_validated(ref_str)
                if not element:
                    version_before = self._snapshot_version
                    await self._extract_interactive_elements()
                    element = await self._resolve_ref_validated(ref_str)
                    if not element:
                        # 最终回退: 用ref_map中缓存的文本语义定位(与_click_impl同理,
                        # SPA重渲染后ref号漂移。input的text格式为[Label:xxx],需提取标签)
                        ref_text = (self._ref_map.get(ref_str, {}) or {}).get("text", "")
                        search_text = self._extract_label_from_ref_text(ref_text)
                        if search_text:
                            logger.info(f"input ref[{ref_str}]失效,回退到文本定位[{search_text}]")
                            element = await self._locate_element_by_text(search_text)
                        if not element:
                            return ToolResult(
                                success=False,
                                message=f"ref[{ref_str}]对应的输入元素不存在或已失效"
                                        + (f",文本[{search_text}]也未找到" if search_text else ""),
                            )
                        logger.info(f"input ref[{ref_str}]文本回退定位成功")
                    if self._snapshot_version != version_before and self._snapshot_version > 0:
                        logger.info(f"input ref[{ref_str}]触发快照刷新(v{version_before}->v{self._snapshot_version})")
                await self._input_text_to_element(element, text)
            elif text_locator is not None and text_locator.strip():
                # text_locator分支: 通过文本语义定位输入元素(适用于输入框带label/placeholder场景)
                element = await self._locate_element_by_text(text_locator)
                if not element:
                    version_before = self._snapshot_version
                    await self._extract_interactive_elements()
                    element = await self._locate_element_by_text(text_locator)
                    if not element:
                        return ToolResult(success=False, message=f"未找到文本为[{text_locator}]的输入元素")
                    if self._snapshot_version != version_before and self._snapshot_version > 0:
                        logger.info(f"input text_locator[{text_locator}]触发快照刷新(v{version_before}->v{self._snapshot_version})")
                await self._input_text_to_element(element, text)
            elif coordinate_x is not None and coordinate_y is not None:
                await self.page.mouse.click(coordinate_x, coordinate_y)
                await asyncio.sleep(0.2)
                await self.page.keyboard.type(text)
            elif index is not None:
                element = await self._get_element_by_id(index)
                if not element:
                    # 索引可能过期，刷新后重试一次
                    version_before = self._snapshot_version
                    await self._extract_interactive_elements()
                    element = await self._get_element_by_id(index)
                    if not element:
                        return ToolResult(success=False, message=f"索引{index}对应的输入元素不存在")
                    if self._snapshot_version != version_before and self._snapshot_version > 0:
                        logger.info(f"input索引{index}触发快照刷新(v{version_before}->v{self._snapshot_version})")
                await self._input_text_to_element(element, text)
            else:
                return ToolResult(success=False, message="请提供ref、text_locator、index或coordinate_x/coordinate_y参数")

            if press_enter:
                await self.page.keyboard.press("Enter")

            await self._post_action_sync()
            return ToolResult(success=True, data={"snapshot_version": self._snapshot_version})
        except Exception as e:
            return ToolResult(success=False, message=f"输入操作失败: {str(e)}")

    async def _select_option_impl(
            self, index: int, option: Optional[int] = None, text: Optional[str] = None,
    ) -> ToolResult:
        """下拉选项选择实现(无锁)，支持按文本与按序号两种模式。
        text优先(更直观、LLM推理步骤少)，回退到option序号。
        容错策略：按text→按index→按value→JS赋值+事件派发。"""
        await self._ensure_page()
        try:
            element = await self._get_element_by_id(index)
            if not element:
                return ToolResult(success=False, message=f"索引[{index}]对应的下拉菜单元素不存在")

            # 策略1: 按文本选择(优先，LLM直接读选项文本无需数序号)
            if text:
                try:
                    await element.select_option(label=text)
                    await self._post_action_sync()
                    return ToolResult(success=True, data={"method": "text", "text": text})
                except Exception:
                    pass

            # 策略2: 按序号选择(原生select index参数)
            if option is not None:
                try:
                    await element.select_option(index=option)
                    await self._post_action_sync()
                    return ToolResult(success=True, data={"method": "index", "option": option})
                except Exception:
                    pass

                # 策略3: 按值选择(部分自定义组件以value匹配)
                try:
                    await element.select_option(value=str(option))
                    await self._post_action_sync()
                    return ToolResult(success=True, data={"method": "value", "value": str(option)})
                except Exception:
                    pass

            # 策略4: JS赋值+事件派发(兼容自定义下拉组件)
            target_value = text if text else str(option) if option is not None else None
            if target_value:
                try:
                    await self.page.evaluate(
                        """(el, val) => {
                            el.value = val;
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                        }""",
                        element, target_value,
                    )
                    await self._post_action_sync()
                    return ToolResult(success=True, data={"method": "js", "value": target_value})
                except Exception as e:
                    return ToolResult(
                        success=False,
                        message=f"选择下拉菜单选项失败(已尝试4种策略): {str(e)}",
                    )

            return ToolResult(success=False, message="请提供option序号或text文本参数")
        except Exception as e:
            return ToolResult(success=False, message=f"选择下拉菜单选项失败: {str(e)}")

    async def _scroll_to_text_impl(self, text: str) -> ToolResult:
        """滚动至包含目标文本的元素(无锁实现)。
        通过TreeWalker遍历文本节点定位目标，相比固定像素滚动更精准：
        长页面中目标元素位置不确定，文本匹配滚动一次到位，避免反复试错。
        滚动后刷新交互元素缓存，使新视口内的可交互元素索引可用。

        增强返回(会话252d3f44优化): 携带target_visible和target_text字段,
        让LLM直接确认目标元素是否已进入视口及匹配是否正确,减少冗余browser_view调用。
        """
        target = (text or "").strip()
        if not target:
            return ToolResult(success=False, message="text参数不能为空")
        try:
            await self._ensure_page()
            matched = await self.page.evaluate(SCROLL_TO_TEXT_FUNC, target)
            if not matched:
                return ToolResult(success=False, message=f"未找到包含文本的元素: {target[:50]}")
            await asyncio.sleep(0.5)
            await self._refresh_interactive_cache()
            state = await self.page.evaluate(GET_PAGE_STATE_FUNC)
            # 增强返回: 目标元素视口可见性 + 匹配文本片段,减少LLM额外browser_view确认
            result_data: Dict[str, Any] = {"page_state": state, "matched_text": target}
            if isinstance(matched, dict):
                result_data["target_visible"] = matched.get("inViewport", False)
                result_data["target_text"] = matched.get("text", "")
            return ToolResult(success=True, data=result_data)
        except Exception as e:
            return ToolResult(success=False, message=f"滚动至文本失败: {str(e)}")

    # ==================== Browser协议公共方法(并发安全+操作指标) ====================

    async def navigate(self, url: str) -> ToolResult:
        if not self._validate_url(url):
            return ToolResult(success=False, message=f"URL格式不合法: {url}，必须包含http/https协议前缀")
        start = time.monotonic()
        async with self._operation_lock:
            result = await self._navigate_impl(url)
        self._log_action("navigate", result.success, time.monotonic() - start, url=url)
        return result

    async def view_page(self, include_diff: bool = False) -> ToolResult:
        start = time.monotonic()
        async with self._operation_lock:
            try:
                await self._ensure_page()
                await self.wait_for_page_load()
                # SPA内容渲染等待: readyState可能停留interactive(外部脚本未完成),
                # 补充内容就绪检测确保SPA已渲染,避免返回空内容
                await self._wait_for_content_ready()
                await self._auto_dismiss_blocking_elements()
                elements = await self._extract_interactive_elements()
                content = await self._extract_content()
                state = await self.page.evaluate(GET_PAGE_STATE_FUNC)
                # 截图与accessibility树作为辅助通道，失败不影响主流程
                screenshot = await self._take_view_screenshot()
                accessibility_tree = await self._extract_accessibility_tree()
                formatted_elements = await self._format_elements(elements)
                formatted_ref_map = self._format_ref_map_for_llm()
                # 空内容告警: content和interactive_elements同时为空时记录WARNING,
                # 便于运维定位页面渲染异常(会话4b2ad987: 11次view_page返回空,LLM被迫盲操作)
                if not content and not formatted_elements:
                    logger.warning(
                        f"view_page返回空内容(url={state.get('url', '?')}, "
                        f"readyState={state.get('readyState', '?')}, "
                        f"scrollHeight={state.get('scrollHeight', 0)})"
                    )
                result_data = {
                    "content": content,
                    "interactive_elements": formatted_elements,
                    "ref_map": formatted_ref_map,
                    "page_state": state,
                    "screenshot": screenshot,
                    "accessibility_tree": accessibility_tree,
                    "snapshot_version": self._snapshot_version,
                    "pending_dialogs": self._get_pending_dialogs(),
                    "dialog_history": self._get_dialog_history(),
                }
                # 可见性摘要: 帮助LLM快速判断页面元素分布,决定是否需要滚动
                # 会话1146286e根因: LLM不知道有多少元素在视口外,对offscreen元素盲操作导致失败
                visible_count = sum(1 for el in elements if el.get("inViewport", True))
                offscreen_count = len(elements) - visible_count
                result_data["element_summary"] = {
                    "visible": visible_count,
                    "offscreen": offscreen_count,
                    "total": len(elements),
                }
                # SPA内容文本为空时的防误判提示(会话e4d0f778暴露:
                # VitePress等SPA的DOM文本提取可能返回空,但interactive_elements
                # 已正常提取。LLM误判"页面未加载"而频繁browser_restart。
                # 注入明确提示避免LLM误用browser_restart重置会话状态。)
                if not content and (formatted_elements or formatted_ref_map):
                    result_data["content_hint"] = (
                        "页面已加载(DOM文本提取为空但已检测到交互元素),"
                        "浏览器状态正常,请勿使用browser_restart。"
                        "请基于interactive_elements和ref_map操作元素,"
                        "或使用browser_console_exec检查具体DOM结构。"
                    )
                # include_diff: 操作后重新查看时识别SPA重渲染范围(新增/消失/变化元素)
                # _prev_ref_map为空(导航后首次快照)时返回has_diff=False,无前次可比
                if include_diff:
                    result_data["diff"] = self._compute_snapshot_diff()
                result = ToolResult(success=True, data=result_data)
            except Exception as e:
                result = ToolResult(success=False, message=f"查看页面失败: {str(e)}")
        self._log_action("view_page", result.success, time.monotonic() - start, include_diff=include_diff)
        return result

    async def restart(self, url: str) -> ToolResult:
        if not self._validate_url(url):
            return ToolResult(success=False, message=f"URL格式不合法: {url}，必须包含http/https协议前缀")
        start = time.monotonic()
        async with self._operation_lock:
            await self.cleanup()
            result = await self._navigate_impl(url)
        self._log_action("restart", result.success, time.monotonic() - start, url=url)
        return result

    async def click(
            self,
            ref: Optional[str] = None,
            text: Optional[str] = None,
            index: Optional[int] = None,
            coordinate_x: Optional[float] = None,
            coordinate_y: Optional[float] = None,
    ) -> ToolResult:
        start = time.monotonic()
        async with self._operation_lock:
            result = await self._click_impl(ref, text, index, coordinate_x, coordinate_y)
        self._log_action(
            "click", result.success, time.monotonic() - start,
            ref=ref, text=text, index=index, x=coordinate_x, y=coordinate_y,
        )
        return result

    async def input(
        self,
        text: str,
        press_enter: bool,
        ref: Optional[str] = None,
        text_locator: Optional[str] = None,
        index: Optional[int] = None,
        coordinate_x: Optional[float] = None,
        coordinate_y: Optional[float] = None,
    ) -> ToolResult:
        start = time.monotonic()
        async with self._operation_lock:
            result = await self._input_impl(
                text, press_enter, ref, text_locator, index, coordinate_x, coordinate_y,
            )
        self._log_action(
            "input", result.success, time.monotonic() - start,
            ref=ref, index=index, press_enter=press_enter,
        )
        return result

    async def move_mouse(self, coordinate_x: float, coordinate_y: float) -> ToolResult:
        start = time.monotonic()
        async with self._operation_lock:
            try:
                await self._ensure_page()
                await self.page.mouse.move(coordinate_x, coordinate_y)
                result = ToolResult(success=True)
            except Exception as e:
                result = ToolResult(success=False, message=f"移动鼠标失败: {str(e)}")
        self._log_action("move_mouse", result.success, time.monotonic() - start, x=coordinate_x, y=coordinate_y)
        return result

    async def press_key(self, key: str) -> ToolResult:
        start = time.monotonic()
        async with self._operation_lock:
            try:
                await self._ensure_page()
                await self.page.keyboard.press(key)
                await self._post_action_sync()
                result = ToolResult(success=True)
            except Exception as e:
                result = ToolResult(success=False, message=f"按键操作失败: {str(e)}")
        self._log_action("press_key", result.success, time.monotonic() - start, key=key)
        return result

    async def select_option(
            self, index: int, option: Optional[int] = None, text: Optional[str] = None,
    ) -> ToolResult:
        start = time.monotonic()
        async with self._operation_lock:
            result = await self._select_option_impl(index, option, text)
        self._log_action(
            "select_option", result.success, time.monotonic() - start,
            index=index, option=option, text=text,
        )
        return result

    async def _scroll_dialog(self, direction: str, to_end: bool = False) -> bool:
        """滚动弹窗内容(Element UI/Ant Design弹窗体),返回是否实际滚动了弹窗。
        direction: 'down'|'up',to_end: True表示滚动到底/顶。"""
        try:
            scrolled = await self.page.evaluate(
                """(args) => {
                    const dialog = document.querySelector(
                        '.el-dialog__wrapper:not([style*="display: none"]) .el-dialog, ' +
                        '.el-drawer__container:not([style*="display: none"]) .el-drawer, ' +
                        '.ant-modal-wrap:not([style*="display: none"]) .ant-modal'
                    );
                    if (!dialog) return false;
                    const body = dialog.querySelector('.el-dialog__body, .el-drawer__body, .ant-modal-body') || dialog;
                    if (!body) return false;
                    const step = body.clientHeight * 0.8;
                    if (args.to_end) {
                        body.scrollTop = args.direction === 'down' ? body.scrollHeight : 0;
                    } else {
                        body.scrollTop += args.direction === 'down' ? step : -step;
                    }
                    return true;
                }""",
                {"direction": direction, "to_end": to_end},
            )
            return bool(scrolled)
        except Exception:
            return False

    async def scroll_up(self, to_top: Optional[bool] = None) -> ToolResult:
        start = time.monotonic()
        async with self._operation_lock:
            try:
                await self._ensure_page()
                # 弹窗打开时优先滚动弹窗内容,否则滚动主窗口
                dialog_scrolled = await self._scroll_dialog("up", to_top or False)
                if not dialog_scrolled:
                    if to_top:
                        await self.page.evaluate("window.scrollTo(0, 0)")
                    else:
                        await self.page.evaluate("window.scrollBy(0, -window.innerHeight)")
                await asyncio.sleep(0.5)
                await self._refresh_interactive_cache()
                state = await self.page.evaluate(GET_PAGE_STATE_FUNC)
                result = ToolResult(success=True, data={"page_state": state})
            except Exception as e:
                result = ToolResult(success=False, message=f"向上滚动失败: {str(e)}")
        self._log_action("scroll_up", result.success, time.monotonic() - start, to_top=to_top)
        return result

    async def scroll_down(self, to_down: Optional[bool] = None) -> ToolResult:
        start = time.monotonic()
        async with self._operation_lock:
            try:
                await self._ensure_page()
                # 弹窗打开时优先滚动弹窗内容,否则滚动主窗口
                dialog_scrolled = await self._scroll_dialog("down", to_down or False)
                if not dialog_scrolled:
                    if to_down:
                        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    else:
                        await self.page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(0.5)
                await self._refresh_interactive_cache()
                state = await self.page.evaluate(GET_PAGE_STATE_FUNC)
                result = ToolResult(success=True, data={"page_state": state})
            except Exception as e:
                result = ToolResult(success=False, message=f"向下滚动失败: {str(e)}")
        self._log_action("scroll_down", result.success, time.monotonic() - start, to_down=to_down)
        return result

    async def scroll_to_text(self, text: str) -> ToolResult:
        """滚动至包含指定文本的元素，长页面精准定位目标位置"""
        start = time.monotonic()
        async with self._operation_lock:
            result = await self._scroll_to_text_impl(text)
        self._log_action("scroll_to_text", result.success, time.monotonic() - start, text=text)
        return result

    async def screenshot(self, full_page: Optional[bool] = None) -> bytes:
        start = time.monotonic()
        async with self._operation_lock:
            await self._ensure_page()
            # 截图前等待页面加载完成: click等操作可能触发导航,此时readyState可能
            # 停留在loading/interactive。等待complete确保页面就绪,避免截图捕获到
            # 导航过渡态或因页面未就绪而失败(会话4b2ad987: click 50%截图缺失根因)。
            # 短超时(5s): 大多数页面已加载完成时会立即返回,仅导航场景额外等待。
            await self.wait_for_page_load(timeout=5)
            # 截图前等待DOM稳定: click/console_exec等操作可能触发异步DOM变化,
            # 立即截图可能捕获到过渡态或因页面未就绪而失败。
            # 短超时(3s)等待确保页面渲染完成,view_page等已稳定的方法几乎无额外开销。
            await self._wait_dom_stable(timeout=3)
            # 截图重试机制，防止偶发渲染异常导致任务中断
            for attempt in range(_SCREENSHOT_RETRIES + 1):
                try:
                    result = await self.page.screenshot(full_page=full_page, type="png")
                    self._log_action("screenshot", True, time.monotonic() - start, full_page=full_page)
                    return result
                except Exception as e:
                    if attempt < _SCREENSHOT_RETRIES:
                        logger.warning(f"截图失败(第{attempt + 1}次重试): {str(e)}")
                        await asyncio.sleep(0.5 * (attempt + 1))
                    else:
                        self._log_action("screenshot", False, time.monotonic() - start, error=str(e))
                        raise

    async def console_exec(self, javascript: str) -> ToolResult:
        start = time.monotonic()
        async with self._operation_lock:
            try:
                await self._ensure_page()
                # SPA内容就绪等待(替代单纯DOM稳定等待):
                # 路由切换中DOM可能短暂稳定(空app div)但execution context未就绪,
                # page.evaluate返回undefined(非异常),LLM误判浏览器故障触发restart。
                # 等待内容就绪确保JS在已渲染页面上执行(会话e5cce96a根因:
                # click导航后"return document.title"返回空→LLM滥用console_exec探测)
                await self._wait_for_content_ready(timeout=5)
                try:
                    await self.page.evaluate(INJECT_CONSOLE_LOGS_FUNC)
                except Exception:
                    pass
                # 智能包装: LLM常发送"return document.title"形式的代码,
                # 但Playwright page.evaluate()在顶层脚本中不支持return语句,
                # 会抛出"Illegal return statement"错误。
                # 检测顶层return并自动包装为箭头函数: () => { <code> }
                wrapped_js = self._wrap_js_if_needed(javascript)
                # 超时保护:防止恶意/超长JS(死循环、巨大计算)阻塞浏览器实例
                try:
                    result_data = await asyncio.wait_for(
                        self.page.evaluate(wrapped_js), timeout=_CONSOLE_EXEC_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    self._log_action(
                        "console_exec", False, time.monotonic() - start,
                        timeout=_CONSOLE_EXEC_TIMEOUT,
                    )
                    return ToolResult(
                        success=False,
                        message=f"JS执行超时(超过{_CONSOLE_EXEC_TIMEOUT}秒)",
                    )
                result = ToolResult(success=True, data={"result": result_data})
            except Exception as e:
                result = ToolResult(success=False, message=f"JS执行失败: {str(e)}")
        self._log_action("console_exec", result.success, time.monotonic() - start)
        return result

    @staticmethod
    def _wrap_js_if_needed(code: str) -> str:
        """智能包装JavaScript代码,处理顶层return语句与多语句代码

        Playwright的page.evaluate()在顶层脚本中不支持return语句,
        会抛出SyntaxError: Illegal return statement。
        多语句代码(含const/let/var声明)若不以函数形式包裹也会SyntaxError。

        策略:
        1. 代码已是函数形式(以()=>或function开头) → 不包装
        2. 代码包含顶层return语句 → 包装为 () => { <code> }
        3. 多语句代码(含;分隔或换行分隔的多条语句) → 包装为 () => { <code> }
        4. 纯表达式(单条无return) → 不包装,直接作为表达式求值

        检测顶层return: 使用正则匹配return关键字后跟非标识符字符,覆盖所有变体:
        - "return document.title" (空格)  - "return{...}" (花括号,无空格)
        - "return(...)" (括号)           - "return[...]" (方括号)
        会话4b2ad987暴露: 旧版仅检测"return "(带空格),漏掉"return{"等变体导致SyntaxError
        """
        stripped = code.strip()
        if not stripped:
            return code
        # 已是函数形式,无需包装(覆盖三种常见写法: ()=>{...}, () =>{...}, function(){...})
        if (stripped.startswith("()=>")
                or stripped.startswith("() =>")
                or stripped.startswith("function")):
            return code
        # 按换行和分号分割为语句列表(覆盖单行多语句: "const x=1; return x;")
        statements = [s.strip() for s in stripped.replace("\n", ";").split(";") if s.strip()]
        # 检测顶层return: 正则匹配return关键字后跟非标识符字符(覆盖return+/return(/return{/return[等)
        # returnValue等标识符不匹配(\b确保return后是单词边界)
        has_top_level_return = any(
            _RETURN_STMT_RE.match(s) is not None for s in statements
        )
        # 检测多语句: 多条语句,或单条语句以声明关键字开头(const/let/var声明是语句而非表达式)
        is_multi_statement = len(statements) > 1 or (
            len(statements) == 1
            and _STMT_KEYWORD_RE.match(statements[0]) is not None
        )
        if has_top_level_return or is_multi_statement:
            return f"() => {{ {stripped} }}"
        return code

    async def console_view(self, max_lines: Optional[int] = None) -> ToolResult:
        start = time.monotonic()
        async with self._operation_lock:
            try:
                await self._ensure_page()
                logs = await self.page.evaluate("() => window.__consoleLogs || []")
                if max_lines is not None:
                    logs = logs[-max_lines:]
                result = ToolResult(success=True, data={"logs": logs})
            except Exception as e:
                result = ToolResult(success=False, message=f"获取控制台日志失败: {str(e)}")
        self._log_action("console_view", result.success, time.monotonic() - start, max_lines=max_lines)
        return result

    async def wait(self, seconds: float = 2.0) -> ToolResult:
        start = time.monotonic()
        async with self._operation_lock:
            try:
                await self._ensure_page()
                await asyncio.sleep(seconds)
                await self._wait_dom_stable()
                await self._wait_for_loading_disappear()
                # 等待后刷新可交互元素缓存，适配SPA异步渲染产生的新元素
                await self._refresh_interactive_cache()
                state = await self.page.evaluate(GET_PAGE_STATE_FUNC)
                result = ToolResult(success=True, data={"page_state": state})
            except Exception as e:
                result = ToolResult(success=False, message=f"等待操作失败: {str(e)}")
        self._log_action("wait", result.success, time.monotonic() - start, seconds=seconds)
        return result

    async def wait_for(
            self,
            text: Optional[str] = None,
            disappear_text: Optional[str] = None,
            selector: Optional[str] = None,
            timeout: float = 10.0,
    ) -> ToolResult:
        """增量等待: 文本出现/文本消失/选择器可见。SPA异步渲染的精准信号。

        任一指定条件满足即返回,优于wait的固定延时;超时返回失败。
        - text: 等待文本出现(get_by_text,可见即返回)
        - disappear_text: 等待文本消失(元素隐藏/移除即返回,不存在视为已消失)
        - selector: 等待CSS选择器元素可见
        """
        start = time.monotonic()
        async with self._operation_lock:
            try:
                await self._ensure_page()
                timeout_ms = int(timeout * 1000)
                # 文本出现: get_by_text(exact=False).first.wait_for(state="visible")
                if text:
                    locator = self.page.get_by_text(text, exact=False)
                    await locator.first.wait_for(state="visible", timeout=timeout_ms)
                # 文本消失: 元素不存在/隐藏均视为已消失,异常静默吞掉(已消失)
                if disappear_text:
                    locator = self.page.get_by_text(disappear_text, exact=False)
                    try:
                        await locator.first.wait_for(state="hidden", timeout=timeout_ms)
                    except Exception:
                        pass
                # 选择器可见
                if selector:
                    await self.page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
                # 等待条件满足后刷新交互元素缓存,捕获异步渲染产生的新元素
                await self._refresh_interactive_cache()
                state = await self.page.evaluate(GET_PAGE_STATE_FUNC)
                result = ToolResult(success=True, data={
                    "page_state": state,
                    "waited_for": {
                        "text": text,
                        "disappear_text": disappear_text,
                        "selector": selector,
                    },
                })
            except asyncio.TimeoutError:
                result = ToolResult(
                    success=False,
                    message=(
                        f"等待条件未在{timeout}s内满足(text={text}, "
                        f"disappear_text={disappear_text}, selector={selector})"
                    ),
                )
            except Exception as e:
                result = ToolResult(success=False, message=f"增量等待失败: {str(e)}")
        self._log_action(
            "wait_for", result.success, time.monotonic() - start,
            text=text, disappear_text=disappear_text, selector=selector, timeout=timeout,
        )
        return result

    async def network_requests(
            self,
            max_entries: int = 20,
            url_filter: Optional[str] = None,
            clear: bool = False,
    ) -> ToolResult:
        """获取已捕获的XHR/fetch请求列表(SPA异步通信信号)。

        用于判断异步加载是否完成、排查接口报错。可选按URL子串过滤、获取后清空日志。
        仅记录xhr/fetch类型(排除图片/脚本等静态资源)。
        """
        start = time.monotonic()
        async with self._operation_lock:
            try:
                entries = list(self._network_log)
                # URL子串过滤,排查特定接口
                if url_filter:
                    entries = [e for e in entries if url_filter in e.get("url", "")]
                entries = entries[-max_entries:] if max_entries > 0 else entries
                result = ToolResult(success=True, data={
                    "requests": entries,
                    "total_captured": len(self._network_log),
                })
                # 获取后清空,避免历史日志累积干扰下次排查
                if clear:
                    self._network_log.clear()
            except Exception as e:
                result = ToolResult(success=False, message=f"获取网络请求列表失败: {str(e)}")
        self._log_action(
            "network_requests", result.success, time.monotonic() - start,
            max_entries=max_entries, url_filter=url_filter, clear=clear,
        )
        return result

    # ==================== JS原生对话框响应 ====================

    def _get_pending_dialogs(self) -> List[dict]:
        """获取当前待处理对话框列表(supervisor未绑定或无对话框时返回空列表)"""
        if not self._dialog_supervisor:
            return []
        return self._dialog_supervisor.get_pending_dialogs()

    def _get_dialog_history(self) -> List[dict]:
        """获取已处理对话框历史记录(supervisor未绑定时返回空列表)"""
        if not self._dialog_supervisor:
            return []
        return self._dialog_supervisor.get_dialog_history()

    async def respond_dialog(
            self, dialog_id: str, accept: bool, prompt_text: str = "",
    ) -> ToolResult:
        """响应浏览器原生对话框(alert/confirm/prompt)。
        must_respond策略下由LLM调用此方法对指定对话框作出接受/取消决策。
        auto_dismiss/auto_accept策略下对话框已自动响应,此方法返回提示信息。"""
        start = time.monotonic()
        async with self._operation_lock:
            if not self._dialog_supervisor:
                result = ToolResult(
                    success=False,
                    message="对话框监督器未初始化,无法响应对话框",
                )
            else:
                ok = await self._dialog_supervisor.respond(dialog_id, accept, prompt_text)
                if ok:
                    result = ToolResult(
                        success=True,
                        data={"dialog_id": dialog_id, "accept": accept, "prompt_text": prompt_text},
                    )
                else:
                    result = ToolResult(
                        success=False,
                        message=f"对话框[{dialog_id}]不存在或已被响应",
                    )
        self._log_action(
            "respond_dialog", result.success, time.monotonic() - start,
            dialog_id=dialog_id, accept=accept,
        )
        return result
