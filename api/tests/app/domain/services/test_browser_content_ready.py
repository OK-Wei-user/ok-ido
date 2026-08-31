#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""_wait_for_content_ready 单元测试: SPA异步渲染内容就绪检测。

验证VitePress/React/Vue等SPA框架在domcontentloaded后异步渲染场景下,
_wait_for_content_ready能正确轮询SPA框架内容容器(.vp-doc/.VPContent等),
确保渲染完成后再提取。

关键测试点(会话e5cce96a根因修复验证):
- VitePress容器(.vp-doc)内容检测
- evaluate异常在循环内被捕获,继续轮询(旧版except在循环外导致直接退出)
- 内容稳定性检查: 连续2次positive才返回(防止路由切换中旧内容短暂出现后消失)
- _extract_content的SPA容器回退
- console_exec使用_wait_for_content_ready
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser


class TestWaitForContentReady:
    """_wait_for_content_ready: SPA内容渲染就绪检测"""

    def _create_browser(self) -> PlaywrightBrowser:
        """创建不连接真实CDP的浏览器实例(mock _ensure_page避免真实连接)"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser._ensure_page = AsyncMock()
        browser.page = MagicMock()
        return browser

    @staticmethod
    def _ready_dict(source: str = ".vp-doc", length: int = 200) -> dict:
        """构造CHECK_SPA_CONTENT_READY_FUNC的positive返回值"""
        return {"ready": True, "source": source, "length": length}

    @staticmethod
    def _not_ready_dict(source: str = "body", length: int = 0) -> dict:
        """构造CHECK_SPA_CONTENT_READY_FUNC的negative返回值"""
        return {"ready": False, "source": source, "length": length}

    @pytest.mark.asyncio
    async def test_content_already_present_returns_after_stability_check(self):
        """页面已有内容时,需连续2次确认稳定后才返回(稳定性检查)"""
        browser = self._create_browser()
        # 连续2次返回ready=true
        browser.page.evaluate = AsyncMock(side_effect=[
            self._ready_dict(),
            self._ready_dict(),
        ])
        browser._wait_dom_stable = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await browser._wait_for_content_ready(timeout=8)

        # 稳定性检查: 连续2次evaluate确认内容稳定
        assert browser.page.evaluate.call_count == 2
        browser._wait_dom_stable.assert_called_once_with(timeout=3)

    @pytest.mark.asyncio
    async def test_content_appears_after_polling(self):
        """内容经轮询后出现: 前两次为空,第三次第四次检测到内容(稳定性检查)"""
        browser = self._create_browser()
        # 模拟SPA异步渲染: 前两次内容为空,后两次内容出现
        browser.page.evaluate = AsyncMock(side_effect=[
            self._not_ready_dict(),
            self._not_ready_dict(),
            self._ready_dict(),
            self._ready_dict(),
        ])
        browser._wait_dom_stable = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await browser._wait_for_content_ready(timeout=8)

        # 轮询4次后(2次空+2次有内容)才确认稳定
        assert browser.page.evaluate.call_count == 4
        browser._wait_dom_stable.assert_called_once_with(timeout=3)

    @pytest.mark.asyncio
    async def test_timeout_when_content_never_appears(self):
        """内容始终未出现(about:blank等)时超时返回,不抛异常"""
        browser = self._create_browser()
        browser.page.evaluate = AsyncMock(return_value=self._not_ready_dict())
        browser._wait_dom_stable = AsyncMock()

        loop = asyncio.get_event_loop()
        original_time = loop.time
        counter = [0]

        def mock_time():
            counter[0] += 1
            if counter[0] <= 6:
                return original_time()
            return original_time() + 100

        with patch.object(loop, "time", side_effect=mock_time):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await browser._wait_for_content_ready(timeout=2)

        # _wait_dom_stable不应被调用(内容未出现)
        browser._wait_dom_stable.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluate_exception_continues_polling(self):
        """evaluate异常(SPA路由切换中context不可用)不退出轮询,继续等待内容就绪。

        关键修复验证(会话e5cce96a根因): 旧版except在循环外,单次evaluate异常
        即退出_wait_for_content_ready,导致后续提取全部落空。
        """
        browser = self._create_browser()
        # 第一次抛异常(SPA路由切换中),第二次返回空,第三第四次返回有内容
        browser.page.evaluate = AsyncMock(side_effect=[
            RuntimeError("execution context destroyed"),
            self._not_ready_dict(),
            self._ready_dict(),
            self._ready_dict(),
        ])
        browser._wait_dom_stable = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # 不应抛异常,应继续轮询直到内容稳定
            await browser._wait_for_content_ready(timeout=8)

        # 4次evaluate: 1次异常+1次空+2次有内容
        assert browser.page.evaluate.call_count == 4
        browser._wait_dom_stable.assert_called_once_with(timeout=3)

    @pytest.mark.asyncio
    async def test_content_below_threshold_continues_polling(self):
        """内容低于阈值(<=20字符容器/<=50字符body)时继续轮询"""
        browser = self._create_browser()
        # 前两次内容为10字符(低于阈值),后两次为200字符
        browser.page.evaluate = AsyncMock(side_effect=[
            self._not_ready_dict(length=10),
            self._not_ready_dict(length=10),
            self._ready_dict(length=200),
            self._ready_dict(length=200),
        ])
        browser._wait_dom_stable = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await browser._wait_for_content_ready(timeout=8)

        assert browser.page.evaluate.call_count == 4
        browser._wait_dom_stable.assert_called_once_with(timeout=3)

    @pytest.mark.asyncio
    async def test_vitepress_container_detected(self):
        """VitePress .vp-doc容器有内容时检测为ready(会话e5cce96a核心修复)"""
        browser = self._create_browser()
        # VitePress容器检测: source为.vp-doc
        browser.page.evaluate = AsyncMock(side_effect=[
            self._ready_dict(source=".vp-doc", length=500),
            self._ready_dict(source=".vp-doc", length=500),
        ])
        browser._wait_dom_stable = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await browser._wait_for_content_ready(timeout=8)

        assert browser.page.evaluate.call_count == 2
        browser._wait_dom_stable.assert_called_once_with(timeout=3)

    @pytest.mark.asyncio
    async def test_content_instability_keeps_polling(self):
        """内容首次出现后消失(SPA路由切换中旧内容被移除)时继续轮询。

        稳定性检查验证: 第一次ready=true(旧内容),第二次ready=false(旧内容被移除),
        第三第四次ready=true(新内容渲染完成)。
        """
        browser = self._create_browser()
        browser.page.evaluate = AsyncMock(side_effect=[
            self._ready_dict(length=300),     # 旧内容短暂出现
            self._not_ready_dict(length=0),    # 旧内容被移除(路由切换中)
            self._ready_dict(length=200),      # 新内容渲染
            self._ready_dict(length=200),      # 新内容稳定
        ])
        browser._wait_dom_stable = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await browser._wait_for_content_ready(timeout=8)

        # 4次evaluate: 1次旧内容+1次空+2次新内容
        assert browser.page.evaluate.call_count == 4
        browser._wait_dom_stable.assert_called_once_with(timeout=3)


class TestExtractContentSpaFallback:
    """_extract_content: SPA容器回退提取"""

    def _create_browser(self) -> PlaywrightBrowser:
        """创建mock浏览器实例"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser._ensure_page = AsyncMock()
        browser.page = MagicMock()
        browser.llm = None
        return browser

    @pytest.mark.asyncio
    async def test_spa_container_fallback_when_dom_walk_empty(self):
        """非文档容器页面: DOM树遍历返回空时,SPA容器回退(.vp-doc)成功提取内容"""
        browser = self._create_browser()
        # DETECT_DOC_CONTAINER_FUNC返回None(非文档容器),GET_VISIBLE返回空JSON,EXTRACT_SPA返回内容
        browser.page.evaluate = AsyncMock(side_effect=[
            None,                              # DETECT_DOC_CONTAINER_FUNC → 非文档容器
            "{}",                              # GET_VISIBLE_CONTENT_FUNC → 空
            "VitePress文档内容" * 20,          # EXTRACT_SPA_CONTENT_FUNC → 有内容
        ])

        content = await browser._extract_content()

        assert "VitePress文档内容" in content
        assert browser.page.evaluate.call_count == 3

    @pytest.mark.asyncio
    async def test_body_inner_text_fallback_when_spa_container_empty(self):
        """非文档容器页面: SPA容器也为空时,最终回退到body.innerText"""
        browser = self._create_browser()
        browser.page.evaluate = AsyncMock(side_effect=[
            None,           # DETECT_DOC_CONTAINER_FUNC → 非文档容器
            "{}",           # GET_VISIBLE_CONTENT_FUNC → 空
            "",             # EXTRACT_SPA_CONTENT_FUNC → 空
            "body文本内容",  # body.innerText → 有内容
        ])

        content = await browser._extract_content()

        assert "body文本内容" in content
        assert browser.page.evaluate.call_count == 4

    @pytest.mark.asyncio
    async def test_dom_walk_success_no_fallback(self):
        """非文档容器页面: DOM树遍历成功时不触发SPA容器回退"""
        browser = self._create_browser()
        dom_tree = '{"tag":"div","text":"页面内容","children":[]}'
        browser.page.evaluate = AsyncMock(side_effect=[
            None,       # DETECT_DOC_CONTAINER_FUNC → 非文档容器
            dom_tree,   # GET_VISIBLE_CONTENT_FUNC → 有内容的DOM树
        ])

        content = await browser._extract_content()

        assert "页面内容" in content
        # 只调用2次evaluate(DETECT + GET_VISIBLE),不触发SPA回退
        assert browser.page.evaluate.call_count == 2


class TestExtractContentDocContainerPath:
    """_extract_content: 文档容器视口感知优先路径(会话4f441827/6794ac3c根因修复)

    文档型页面(.vp-doc/main/article等容器)使用EXTRACT_DOC_CONTENT_FUNC视口感知提取,
    跳过DOM树遍历(返回全body文本~50KB含导航/侧边栏),避免截断后文档正文被丢弃。
    覆盖VitePress标准(.vp-doc)与Element Plus自定义主题(main)两种容器结构。
    """

    def _create_browser(self) -> PlaywrightBrowser:
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser._ensure_page = AsyncMock()
        browser.page = MagicMock()
        browser.llm = None
        return browser

    @pytest.mark.asyncio
    async def test_vitepress_vp_doc_container_extraction(self):
        """VitePress标准页面(.vp-doc容器)走视口感知提取,跳过DOM树遍历"""
        browser = self._create_browser()
        doc_content = "## 对齐方式\nForm Align\nLeft Right\n## 表单验证"
        browser.page.evaluate = AsyncMock(side_effect=[
            ".vp-doc",      # DETECT_DOC_CONTAINER_FUNC → 命中.vp-doc容器
            doc_content,    # EXTRACT_DOC_CONTENT_FUNC → 视口感知内容
        ])

        content = await browser._extract_content()

        assert "对齐方式" in content
        assert "Form Align" in content
        # 仅2次evaluate(DETECT + EXTRACT),不调用DOM树遍历
        assert browser.page.evaluate.call_count == 2

    @pytest.mark.asyncio
    async def test_element_plus_main_container_extraction(self):
        """Element Plus自定义主题(main容器)走视口感知提取(会话6794ac3c根因)

        Element Plus使用自定义VitePress主题,无.vp-doc容器,文档正文位于<main>内。
        旧版仅检测.vp-doc导致漏判,content字段全空;新版检测main容器命中。
        """
        browser = self._create_browser()
        doc_content = "## 对齐方式\n根据你们的设计情况\nLeft\nRight\n## 表单验证"
        browser.page.evaluate = AsyncMock(side_effect=[
            "main",         # DETECT_DOC_CONTAINER_FUNC → 命中main容器(Element Plus)
            doc_content,    # EXTRACT_DOC_CONTENT_FUNC → 视口感知内容
        ])

        content = await browser._extract_content()

        assert "对齐方式" in content
        assert "Left" in content
        assert browser.page.evaluate.call_count == 2

    @pytest.mark.asyncio
    async def test_doc_container_empty_falls_back_to_dom_walk(self):
        """文档容器命中但提取返回空时,回退到DOM树遍历"""
        browser = self._create_browser()
        dom_tree = '{"tag":"div","text":"DOM树内容","children":[]}'
        browser.page.evaluate = AsyncMock(side_effect=[
            "main",         # DETECT_DOC_CONTAINER_FUNC → 命中
            "",             # EXTRACT_DOC_CONTENT_FUNC → 空(水合期未渲染完成)
            dom_tree,       # GET_VISIBLE_CONTENT_FUNC → 回退DOM树
        ])

        content = await browser._extract_content()

        assert "DOM树内容" in content
        assert browser.page.evaluate.call_count == 3

    @pytest.mark.asyncio
    async def test_doc_container_detection_failure_falls_back_to_dom_walk(self):
        """文档容器检测异常时不阻塞,回退到DOM树遍历"""
        browser = self._create_browser()
        dom_tree = '{"tag":"div","text":"DOM树内容","children":[]}'
        browser.page.evaluate = AsyncMock(side_effect=[
            RuntimeError("execution context destroyed"),  # DETECT异常(SPA路由切换中)
            dom_tree,                                       # GET_VISIBLE → 回退
        ])

        content = await browser._extract_content()

        assert "DOM树内容" in content
        assert browser.page.evaluate.call_count == 2

    @pytest.mark.asyncio
    async def test_doc_content_within_budget(self):
        """文档容器提取内容不超过_MAX_CONTENT_LENGTH上限"""
        browser = self._create_browser()
        # EXTRACT_DOC_CONTENT_FUNC内部已限制8000字符,这里验证上限裁剪仍生效
        doc_content = "x" * 60000
        browser.page.evaluate = AsyncMock(side_effect=[
            "main",
            doc_content,
        ])

        content = await browser._extract_content()

        # _MAX_CONTENT_LENGTH=50000,返回60000应被裁剪到50000
        assert len(content) <= 50000


class TestConsoleExecContentReady:
    """console_exec: 使用_wait_for_content_ready确保JS在内容就绪后执行"""

    def _create_browser(self) -> PlaywrightBrowser:
        """创建mock浏览器实例"""
        browser = PlaywrightBrowser(cdp_url="http://localhost:9222")
        browser._ensure_page = AsyncMock()
        browser.page = MagicMock()
        browser._wait_for_content_ready = AsyncMock()
        return browser

    @pytest.mark.asyncio
    async def test_console_exec_waits_for_content_ready(self):
        """console_exec执行前等待内容就绪(替代旧版_wait_dom_stable)"""
        browser = self._create_browser()
        # mock evaluate: INJECT_CONSOLE_LOGS_FUNC + 用户JS
        browser.page.evaluate = AsyncMock(side_effect=[
            None,                           # INJECT_CONSOLE_LOGS_FUNC
            "Element Plus",                 # 用户JS: return document.title
        ])

        result = await browser.console_exec("return document.title")

        assert result.success is True
        assert result.data["result"] == "Element Plus"
        # 验证_wait_for_content_ready被调用(关键修复: 旧版调用_wait_dom_stable)
        browser._wait_for_content_ready.assert_awaited_once_with(timeout=5)
