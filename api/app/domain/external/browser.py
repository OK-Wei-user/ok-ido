#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/03 0:20

@File    : browser.py
"""
from typing import Protocol, Optional

from app.domain.models.tool_result import ToolResult


class Browser(Protocol):
    """浏览器服务扩展，涵盖：访问页面、URL跳转、输入、移动鼠标、滚动、截图、执行js代码等"""

    async def view_page(self, include_diff: bool = False) -> ToolResult:
        """浏览获取当前浏览器的页面内容。

        Args:
            include_diff: 是否返回与上一次快照的差异(SPA重渲染检测)。操作后重新查看时传true,
                可识别新增/消失/变化的元素;首次查看或导航后无前次快照,返回has_diff=false。
        """
        ...

    async def navigate(self, url: str) -> ToolResult:
        """传递对应的url使用浏览器导航到该页面"""
        ...

    async def restart(self, url: str) -> ToolResult:
        """重启浏览器并访问对应的URL"""
        ...

    async def click(
            self,
            ref: Optional[str] = None,
            text: Optional[str] = None,
            index: Optional[int] = None,
            coordinate_x: Optional[float] = None,
            coordinate_y: Optional[float] = None,
    ) -> ToolResult:
        """点击页面元素。优先按ref引用定位(accessibility tree语义引用,稳定不易漂移),
        其次按text文本定位(语义化、LLM推理步骤少),
        最后按index索引或coordinate_x/coordinate_y坐标定位。"""
        ...

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
        """传递文本+回车标识+定位参数(ref/text_locator/index/xy坐标)实现在网页输入框中输入对应内容。
        ref 优先级最高(语义引用),text_locator 为文本定位兜底。"""
        ...

    async def respond_dialog(
            self, dialog_id: str, accept: bool, prompt_text: str = "",
    ) -> ToolResult:
        """响应浏览器原生对话框(alert/confirm/prompt)。
        传递对话框ID+是否接受+prompt输入文本,完成对话框响应。"""
        ...

    async def move_mouse(self, coordinate_x: float, coordinate_y: float) -> ToolResult:
        """传递对应的xy坐标移动鼠标"""
        ...

    async def press_key(self, key: str) -> ToolResult:
        """传递按键标识实现浏览器模拟按键"""
        ...

    async def select_option(
            self, index: int, option: Optional[int] = None, text: Optional[str] = None,
    ) -> ToolResult:
        """传递索引+选项在下拉菜单中选择指定的选项，支持按文本(text)或序号(option)选择"""
        ...

    async def scroll_up(self, to_top: Optional[bool] = None) -> ToolResult:
        """向上滚动浏览器，如果没传递to_top=True则向上滚动一屏"""
        ...

    async def scroll_down(self, to_down: Optional[bool] = None) -> ToolResult:
        """向下滚动浏览器，如果没传递to_down=True则向下滚动一屏"""
        ...

    async def scroll_to_text(self, text: str) -> ToolResult:
        """滚动至包含指定文本的元素，用于长页面精准定位目标位置"""
        ...

    async def screenshot(self, full_page: Optional[bool] = None) -> bytes:
        """对当前浏览器的页面进行截图，传递full_page=True则意味着整页截图"""
        ...

    async def console_exec(self, javascript: str) -> ToolResult:
        """传递对应的js脚本在浏览器的控制台执行"""
        ...

    async def console_view(self, max_lines: Optional[int] = None) -> ToolResult:
        """传递最大输出行数，获取控制台的输出结果，如果不传递则获取所有结果"""
        ...

    async def wait(self, seconds: float = 2.0) -> ToolResult:
        """等待指定秒数，用于SPA页面异步渲染或动画完成"""
        ...

    async def wait_for(
            self,
            text: Optional[str] = None,
            disappear_text: Optional[str] = None,
            selector: Optional[str] = None,
            timeout: float = 10.0,
    ) -> ToolResult:
        """增量等待: 文本出现/文本消失/选择器可见。SPA异步渲染的精准信号。
        任一指定条件满足即返回,优于wait的固定延时。超时返回失败。"""
        ...

    async def network_requests(
            self,
            max_entries: int = 20,
            url_filter: Optional[str] = None,
            clear: bool = False,
    ) -> ToolResult:
        """获取已捕获的XHR/fetch请求列表(SPA异步通信信号)。
        用于判断异步加载是否完成、排查接口报错。可选按URL子串过滤、获取后清空日志。"""
        ...
