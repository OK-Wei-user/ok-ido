#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : browser.py
工业级DOM浏览器工具 - 纯DOM结构驱动、语义化元素定位、SPA适配、阻塞元素自动消除

工具调用预算(project_memory硬约束): browser_navigate=10 会话级上限,
由 budget_tracker 在调用前硬拦截,超限时返回错误引导 LLM 切换策略。
"""
import asyncio
import logging
from typing import Optional

from app.domain.external.browser import Browser
from app.domain.models.tool_result import ToolResult
from .base import BaseTool, tool
from .budget_tracker import ToolBudgetTracker

logger = logging.getLogger(__name__)


class BrowserTool(BaseTool):
    """浏览器工具包"""

    name: str = "browser"

    # 浏览器操作超时配置(秒) — 防止单个操作卡死阻塞整个Agent流程
    _OP_TIMEOUT_NAVIGATE = 25        # 页面导航/重启
    _OP_TIMEOUT_VIEW = 20            # 查看页面DOM
    _OP_TIMEOUT_INTERACTIVE = 15     # 点击/输入/下拉选择
    _OP_TIMEOUT_SCROLL = 10          # 滚动/移动鼠标/按键
    _OP_TIMEOUT_CONSOLE_EXEC = 30    # JS执行(已有内部超时,外层兜底)
    _OP_TIMEOUT_CONSOLE_VIEW = 5     # 查看控制台
    _OP_TIMEOUT_SCREENSHOT = 5       # 截图
    _OP_TIMEOUT_WAIT_MAX = 30        # 等待操作上限(用户指定值小于此值时取此值)
    _OP_TIMEOUT_DIALOG = 10          # 响应JS原生对话框
    _OP_TIMEOUT_WAIT_FOR = 30        # 增量等待上限(wait_for的timeout超过此值时被截断)
    _OP_TIMEOUT_NETWORK = 5          # 获取网络请求列表(纯内存读取,快速)

    # 方案B: 复杂页面自适应console_exec预算(会话437cbc75根因修复)
    # 交互元素>阈值的页面(企业App)content易被截断,LLM需更多console_exec
    # 提取被截断的表格/弹窗文本。browser_view检测到复杂页面后动态上调预算。
    _COMPLEX_PAGE_ELEMENT_THRESHOLD = 200   # 复杂页面元素数阈值
    _COMPLEX_PAGE_CONSOLE_EXEC_BUDGET = 20   # 复杂页面console_exec预算上限(10→20)

    def __init__(
            self,
            browser: Browser,
            budget_tracker: Optional[ToolBudgetTracker] = None,
    ) -> None:
        """构造函数,完成浏览器工具初始化

        Args:
            browser: 浏览器实例
            budget_tracker: 工具调用预算追踪器(可选,None时跳过预算检查)
        """
        super().__init__()
        self.browser = browser
        self._budget_tracker = budget_tracker

    async def _with_timeout(self, coro, timeout: float, op_name: str) -> ToolResult:
        """包装浏览器操作协程,添加超时保护与统一异常处理"""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            return ToolResult(success=False, message=f"浏览器{op_name}超时(>{timeout}s)")
        except Exception as e:
            return ToolResult(success=False, message=f"浏览器{op_name}失败: {str(e)}")

    def _maybe_raise_console_exec_budget(self, result: ToolResult) -> None:
        """复杂页面自适应放宽console_exec预算(方案B/会话437cbc75根因修复)

        交互元素超过阈值的页面(企业App表格/表单密集页)content易被截断,
        静态10次console_exec硬上限不够用。browser_view检测到复杂页面后,
        通过budget_tracker.raise_budget动态上调(仅增不减,幂等安全)。

        判定依据: result.data.element_summary.total(未截断的原始元素总数),
        而非interactive_elements列表长度(可能已被截断)。
        """
        if not self._budget_tracker or not result.success:
            return
        data = getattr(result, "data", None)
        if not isinstance(data, dict):
            return
        element_summary = data.get("element_summary")
        if not isinstance(element_summary, dict):
            return
        total = element_summary.get("total", 0)
        if total >= self._COMPLEX_PAGE_ELEMENT_THRESHOLD:
            self._budget_tracker.raise_budget(
                "browser_console_exec", self._COMPLEX_PAGE_CONSOLE_EXEC_BUDGET,
            )

    @tool(
        name="browser_view",
        description="查看当前浏览器页面的DOM结构和可交互元素。返回结构化DOM内容、元素索引列表(可见性优先:视口内可见元素全量展示,offscreen元素分区限流展示)、ref引用映射表(@e1/@e2等语义引用,推荐用于click/input定位,同样可见性优先排序)、页面状态(URL/标题/滚动位置/弹窗检测/阻塞元素检测)、可选的视口截图(供多模态LLM视觉辅助)、accessibility语义树、待处理JS原生对话框列表(pending_dialogs)与已处理对话框历史(dialog_history)。自动消除弹窗/Cookie横幅等阻塞元素。当pending_dialogs非空时,需调用browser_respond_dialog作出响应。重要:优先操作视口内可见元素;offscreen元素需先scroll_down/scroll_up/scroll_to_text滚动至可见区域后再操作,避免对不可见元素盲操作导致定位失败。优先使用此工具了解页面状态。include_diff=true时额外返回与上一次快照的差异(新增/消失/变化的元素),用于操作后识别SPA重渲染范围。",
        parameters={
            "include_diff": {
                "type": "boolean",
                "description": "(可选)是否返回与上一次快照的差异(SPA重渲染检测)。操作后重新查看时传true,可识别新增/消失/变化的元素;首次查看或导航后无前次快照,返回has_diff=false。默认false。"
            }
        },
        required=[]
    )
    async def browser_view(self, include_diff: bool = False) -> ToolResult:
        result = await self._with_timeout(
            self.browser.view_page(include_diff=include_diff),
            self._OP_TIMEOUT_VIEW, "查看页面",
        )
        # 方案B: 复杂页面自适应放宽console_exec预算
        self._maybe_raise_console_exec_budget(result)
        return result

    @tool(
        name="browser_navigate",
        description="将浏览器导航至指定网址。自动等待DOM稳定、Loading消失、消除弹窗。已内置3次重试(总尝试3次),瞬时网络抖动/超时会自动重试,无需因偶发失败改用browser_restart。当需要访问新页面时使用。",
        parameters={
            "url": {
                "type": "string",
                "description": "要访问的完整URL，必须包含协议前缀(如https://)"
            }
        },
        required=["url"]
    )
    async def browser_navigate(self, url: str) -> ToolResult:
        # 工具调用预算检查(project_memory: browser_navigate=10 会话级上限)
        # 防止 LLM 在浏览器导航死循环(反复访问相同/相似URL)
        if self._budget_tracker and self._budget_tracker.is_exceeded("browser_navigate"):
            count = self._budget_tracker.get_count("browser_navigate")
            budget = self._budget_tracker.get_budget("browser_navigate")
            logger.info(f"browser_navigate 调用次数已达上限: {count}/{budget}, 拒绝调用")
            # Batch 39 / 方向3: 标记超限事件,供 BaseAgent 消费联动 metrics
            self._budget_tracker.mark_exceeded("browser_navigate")
            return ToolResult(
                success=False,
                message=(
                    f"browser_navigate 调用次数已达会话上限({count}/{budget})。"
                    f"请基于已访问页面的 browser_view 结果综合分析,"
                    f"或切换策略(如使用 search_web 搜索相关信息、"
                    f"browser_scroll_down 滚动当前页面查看更多内容)。"
                ),
            )

        result = await self._with_timeout(self.browser.navigate(url), self._OP_TIMEOUT_NAVIGATE, "导航")

        # 预算计数(导航调用即计入,超时也计入,避免LLM反复重试超时URL)
        if self._budget_tracker:
            self._budget_tracker.increment("browser_navigate")
            self._budget_tracker.check_and_warn("browser_navigate")

        return result

    @tool(
        name="browser_restart",
        description="重启浏览器实例并导航至指定URL。仅当浏览器实例卡死、page对象不可用、或browser_navigate重试3次仍失败时使用。普通页面跳转请优先使用browser_navigate(已内置重试),频繁restart会重置会话状态增加开销。",
        parameters={
            "url": {
                "type": "string",
                "description": "要访问的完整URL，必须包含协议前缀(如https://)"
            }
        },
        required=["url"]
    )
    async def browser_restart(self, url: str) -> ToolResult:
        return await self._with_timeout(self.browser.restart(url), self._OP_TIMEOUT_NAVIGATE, "重启")

    @tool(
        name="browser_click",
        description="点击当前页面中的元素。支持四种定位方式(优先级:ref>text>index>coordinate):按ref引用定位(accessibility tree语义引用,最稳定推荐)、按元素文本定位(语义化)、按索引定位、按坐标定位。支持六级容错策略(正常点击→滚动后点击→强制点击→坐标点击→JS事件派发→视觉兜底)。ref/text分支五级DOM策略全部失败后,调用多模态LLM分析截图定位坐标作为第六级兜底。自动检测元素可交互性(遮挡/隐藏/禁用)并尝试消除阻塞元素。优先使用ref参数(来自browser_view返回的ref_map),其次使用text参数点击按钮/链接/菜单等带文字的元素。",
        parameters={
            "ref": {
                "type": "string",
                "description": "(可选,最推荐)元素引用(来自browser_view返回的ref_map,如'@e1')。基于accessibility tree语义引用,比index更稳定,SPA重渲染后不易漂移。"
            },
            "text": {
                "type": "string",
                "description": "(可选,推荐)要点击的元素文本(如按钮文字'提交'、菜单项'商品审核')。通过文本语义定位元素,无需依赖易过期的索引。"
            },
            "index": {
                "type": "integer",
                "description": "(可选)需要点击的元素索引(来自browser_view返回的interactive_elements)。当目标元素无明确文本且无ref时使用。"
            },
            "coordinate_x": {
                "type": "number",
                "description": "(可选)点击位置的x坐标。仅当ref、text和index都无法定位时使用。"
            },
            "coordinate_y": {
                "type": "number",
                "description": "(可选)点击位置的y坐标。需与coordinate_x同时提供。"
            }
        },
        required=[],
    )
    async def browser_click(
            self,
            ref: Optional[str] = None,
            text: Optional[str] = None,
            index: Optional[int] = None,
            coordinate_x: Optional[float] = None,
            coordinate_y: Optional[float] = None,
    ) -> ToolResult:
        return await self._with_timeout(
            self.browser.click(ref, text, index, coordinate_x, coordinate_y),
            self._OP_TIMEOUT_INTERACTIVE, "点击",
        )

    @tool(
        name="browser_input",
        description="覆盖浏览器当前页面可编辑区域的文本(input/textarea输入框)。支持三级输入策略(fill→键盘输入→JS赋值)，兼容各类前端框架拦截。定位优先级:ref>text_locator>index>coordinate。重要:优先使用browser_view返回的ref定位输入框(最精准);使用text_locator时应选择唯一label文字(如'姓名'/'邮箱'),避免使用通用placeholder(如'请输入')因匹配多个输入框导致输入到错误字段。",
        parameters={
            "text": {
                "type": "string",
                "description": "要填充到输入框的完整文本内容",
            },
            "press_enter": {
                "type": "boolean",
                "description": "输入后是否按下回车键",
            },
            "ref": {
                "type": "string",
                "description": "(可选,最推荐)输入框的元素引用(来自browser_view返回的ref_map,如'@e3')。基于accessibility tree语义引用,最稳定。"
            },
            "text_locator": {
                "type": "string",
                "description": "(可选)输入框的文本定位标签。优先使用唯一label文字(如'姓名'/'邮箱'),避免使用通用placeholder(如'请输入')因匹配多个输入框导致输入到错误字段。"
            },
            "index": {
                "type": "integer",
                "description": "(可选)需要填充文本的元素索引"
            },
            "coordinate_x": {
                "type": "number",
                "description": "(可选)需要填充文本元素的x坐标"
            },
            "coordinate_y": {
                "type": "number",
                "description": "(可选)需要填充文本元素的y坐标"
            },
        },
        required=["text", "press_enter"],
    )
    async def browser_input(
            self,
            text: str,
            press_enter: bool,
            ref: Optional[str] = None,
            text_locator: Optional[str] = None,
            index: Optional[int] = None,
            coordinate_x: Optional[float] = None,
            coordinate_y: Optional[float] = None,
    ) -> ToolResult:
        return await self._with_timeout(
            self.browser.input(text, press_enter, ref, text_locator, index, coordinate_x, coordinate_y),
            self._OP_TIMEOUT_INTERACTIVE, "输入",
        )

    @tool(
        name="browser_respond_dialog",
        description="响应浏览器原生JS对话框(alert/confirm/prompt)。当browser_view或browser_navigate返回的pending_dialogs非空时,使用此工具对指定对话框作出接受/取消决策。alert类型只需accept=True;confirm类型accept=True表示确认/False表示取消;prompt类型需同时提供prompt_text作为输入文本。注意:默认策略下对话框会自动dismiss,此工具仅在must_respond策略或需要主动覆盖响应时使用。",
        parameters={
            "dialog_id": {
                "type": "string",
                "description": "要响应的对话框ID(来自pending_dialogs列表中的id字段,如'dialog_1')",
            },
            "accept": {
                "type": "boolean",
                "description": "是否接受对话框。True=确认(对应confirm/prompt的OK按钮),False=取消(对应Cancel按钮)。alert类型建议传True。",
            },
            "prompt_text": {
                "type": "string",
                "description": "(可选)prompt对话框的输入文本。仅在对话框类型为prompt且accept=True时需要提供。",
            },
        },
        required=["dialog_id", "accept"],
    )
    async def browser_respond_dialog(
            self, dialog_id: str, accept: bool, prompt_text: str = "",
    ) -> ToolResult:
        return await self._with_timeout(
            self.browser.respond_dialog(dialog_id, accept, prompt_text),
            self._OP_TIMEOUT_DIALOG, "响应对话框",
        )

    @tool(
        name="browser_move_mouse",
        description="将鼠标光标移动至当前浏览器页面的指定位置，用于模拟用户的鼠标移动",
        parameters={
            "coordinate_x": {
                "type": "number",
                "description": "目标光标位置的x坐标"
            },
            "coordinate_y": {
                "type": "number",
                "description": "目标光标位置的y坐标"
            },
        },
        required=["coordinate_x", "coordinate_y"],
    )
    async def browser_move_mouse(self, coordinate_x: float, coordinate_y: float) -> ToolResult:
        return await self._with_timeout(
            self.browser.move_mouse(coordinate_x, coordinate_y),
            self._OP_TIMEOUT_SCROLL, "移动鼠标",
        )

    @tool(
        name="browser_press_key",
        description="在当前浏览器页面模拟按键，当需要执行特定的键盘操作时使用。支持组合键(例如: Control+Enter)。",
        parameters={
            "key": {
                "type": "string",
                "description": "要模拟的按键名称(例如: Enter、Tab、ArrowUp)，支持组合键(例如: Control+Enter)",
            },
        },
        required=["key"],
    )
    async def browser_press_key(self, key: str) -> ToolResult:
        return await self._with_timeout(
            self.browser.press_key(key), self._OP_TIMEOUT_SCROLL, "按键",
        )

    @tool(
        name="browser_select_option",
        description="从当前浏览器页面的下拉列表元素中选择指定选项。优先使用text参数按选项文本选择(更直观、LLM推理步骤少)，无text时使用option序号。",
        parameters={
            "index": {
                "type": "integer",
                "description": "需要操作的下拉列表元素的索引号(序号)"
            },
            "text": {
                "type": "string",
                "description": "(可选)要选择的选项文本内容，例如选项显示为'北京'时传'北京'。优先使用此参数。"
            },
            "option": {
                "type": "integer",
                "description": "(可选)要选择的选项序号，从0开始(注: 指下拉框里的第几项)。当无text时使用。"
            },
        },
        required=["index"]
    )
    async def browser_select_option(
            self, index: int, option: Optional[int] = None, text: Optional[str] = None,
    ) -> ToolResult:
        return await self._with_timeout(
            self.browser.select_option(index, option, text),
            self._OP_TIMEOUT_INTERACTIVE, "下拉选择",
        )

    @tool(
        name="browser_scroll_up",
        description="向上滚动浏览器页面，用于查看上方内容或返回页面顶部。",
        parameters={
            "to_top": {
                "type": "boolean",
                "description": "(可选)是否直接滚动到页面顶部，而非向上滚动一屏。"
            }
        },
        required=[]
    )
    async def browser_scroll_up(self, to_top: Optional[bool] = None) -> ToolResult:
        return await self._with_timeout(
            self.browser.scroll_up(to_top), self._OP_TIMEOUT_SCROLL, "向上滚动",
        )

    @tool(
        name="browser_scroll_down",
        description="向下滚动当前浏览器页面，用于查看下方内容或跳转到页面底部。",
        parameters={
            "to_bottom": {
                "type": "boolean",
                "description": "(可选)是否直接滚动到页面底部，而非向下滚动一屏"
            }
        },
        required=[],
    )
    async def browser_scroll_down(self, to_bottom: Optional[bool] = None) -> ToolResult:
        return await self._with_timeout(
            self.browser.scroll_down(to_bottom), self._OP_TIMEOUT_SCROLL, "向下滚动",
        )

    @tool(
        name="browser_scroll_to_text",
        description="滚动到包含指定文本的元素位置。相比固定像素滚动更精准：长页面中目标元素位置不确定，文本匹配滚动一次到位，避免反复试错。当已知目标操作区域的文本标签时优先使用此工具。",
        parameters={
            "text": {
                "type": "string",
                "description": "需要滚动到的目标元素文本内容(支持部分匹配，例如'提交'、'下一步'、'联系我们')"
            }
        },
        required=["text"]
    )
    async def browser_scroll_to_text(self, text: str) -> ToolResult:
        return await self._with_timeout(
            self.browser.scroll_to_text(text), self._OP_TIMEOUT_SCROLL, "滚动到文本",
        )

    @tool(
        name="browser_scroll_to_top",
        description="直接滚动到当前浏览器页面的顶部，用于快速返回页面最上方查看内容。",
        parameters={},
        required=[]
    )
    async def browser_scroll_to_top(self) -> ToolResult:
        return await self._with_timeout(
            self.browser.scroll_up(to_top=True), self._OP_TIMEOUT_SCROLL, "滚动到顶部",
        )

    @tool(
        name="browser_console_exec",
        description=(
            "在浏览器控制台中执行JavaScript代码,仅用于调试JS错误或执行browser_view无法完成的JS操作。"
            "【使用约束】以下场景请避免使用(超范围调用将被预算拦截): "
            "① 提取页面文本内容 — browser_view的content字段已包含DOM树文本;"
            "   例外: 当browser_view返回content_truncated=true时(content被截断),"
            "   允许使用console_exec提取被截断的表格/弹窗等关键文本; "
            "② 查找/定位元素 — browser_view的interactive_elements已包含元素索引列表; "
            "③ 提取弹窗内容 — browser_view的page_state已包含pending_dialogs; "
            "④ 截图 — browser_view的screenshot字段已包含页面截图。"
            "正确用途: 调试JS运行时错误、执行DOM操作(如修改样式触发渲染)、调用页面JS函数。"
            "返回值写法: 使用return语句(如'return document.title'或'const links=document.querySelectorAll(\"a\"); return links.length'),"
            "系统会自动包装为箭头函数执行。"
            "【返回值流转】return的值直接放在本工具返回结果的result字段中,可直接查看;"
            "切勿调用browser_console_view查找return值——console_view仅显示console.log/warn/error输出,"
            "不显示console_exec的return值(LLM易误以为return值在console_view,反复调用落空)。"
            "会话级调用上限10次(复杂页面自适应放宽至20次),超限请改用browser_view。"
        ),
        parameters={
            "javascript": {
                "type": "string",
                "description": "要执行的JavaScript代码。使用return语句返回结果(如'return document.title'),系统自动包装为函数执行。"
            },
        },
        required=["javascript"],
    )
    async def browser_console_exec(self, javascript: str) -> ToolResult:
        # 工具调用预算检查(project_memory: browser_console_exec=10 会话级上限,
        # 复杂页面自适应放宽至20,由_maybe_raise_console_exec_budget动态上调)
        # 防止 LLM 滥用 console_exec 提取页面内容(content/interactive_elements已有)
        if self._budget_tracker and self._budget_tracker.is_exceeded("browser_console_exec"):
            count = self._budget_tracker.get_count("browser_console_exec")
            budget = self._budget_tracker.get_budget("browser_console_exec")
            logger.info(f"browser_console_exec 调用次数已达上限: {count}/{budget}, 拒绝调用")
            self._budget_tracker.mark_exceeded("browser_console_exec")
            return ToolResult(
                success=False,
                message=(
                    f"browser_console_exec 调用次数已达会话上限({count}/{budget})。"
                    f"建议: 先browser_scroll_down/scroll_to_text滚动到目标区域,"
                    f"再browser_view查看(视口优先策略确保当前可见区域内容不被截断)。"
                    f"元素定位请使用browser_view的interactive_elements/ref_map字段。"
                ),
            )

        result = await self._with_timeout(
            self.browser.console_exec(javascript), self._OP_TIMEOUT_CONSOLE_EXEC, "JS执行",
        )

        # 预算计数(执行即计入,超时也计入,避免LLM反复重试)
        if self._budget_tracker:
            self._budget_tracker.increment("browser_console_exec")
            self._budget_tracker.check_and_warn("browser_console_exec")

        return result

    @tool(
        name="browser_console_view",
        description=(
            "查看浏览器控制台输出,仅用于调试JS错误或查看console.log日志。"
            "【使用约束】请勿用于获取页面内容 — 页面文本请使用 browser_view 的 content 字段,"
            "元素信息请使用 browser_view 的 interactive_elements 字段。"
            "正确用途: 检查JS运行时错误、查看console.log/console.error输出、排查接口调用异常。"
            "【范围说明】仅显示console.log/warn/error输出,不显示browser_console_exec的return值"
            "(return值在console_exec返回结果的result字段中,无需通过本工具查看)。"
        ),
        parameters={
            "max_lines": {
                "type": "integer",
                "description": "(可选)返回的最大日志行数。"
            }
        },
        required=[],
    )
    async def browser_console_view(self, max_lines: Optional[int] = None) -> ToolResult:
        return await self._with_timeout(
            self.browser.console_view(max_lines), self._OP_TIMEOUT_CONSOLE_VIEW, "查看控制台",
        )

    @tool(
        name="browser_wait",
        description="等待指定秒数,仅用于浏览器操作后等待DOM渲染/动画/网络请求完成。禁止用于等待非浏览器任务(如MCP异步任务、文件下载、后台进程),此类场景必须使用shell_execute(sleep N)。等待结束后自动检测DOM稳定和Loading状态。",
        parameters={
            "seconds": {
                "type": "number",
                "description": "(可选)等待的秒数，默认2秒"
            }
        },
        required=[],
    )
    async def browser_wait(self, seconds: float = 2.0) -> ToolResult:
        # 等待超时取用户指定值与上限的较大值,确保不截断用户预期等待
        timeout = max(seconds, self._OP_TIMEOUT_WAIT_MAX)
        return await self._with_timeout(
            self.browser.wait(seconds), timeout, "等待",
        )

    @tool(
        name="browser_wait_for",
        description="增量等待: 文本出现/文本消失/选择器可见。SPA异步渲染的精准信号,优于browser_wait的固定延时。任一指定条件满足即返回,超时返回失败。典型场景: 点击后等待目标文本出现确认页面切换、等待加载遮罩消失、等待特定元素可见后再操作。",
        parameters={
            "text": {
                "type": "string",
                "description": "(可选)等待出现的文本内容。文本可见时即满足条件。"
            },
            "disappear_text": {
                "type": "string",
                "description": "(可选)等待消失的文本内容。元素隐藏或移除时即满足条件,不存在视为已消失。常用于等待加载提示/遮罩消失。"
            },
            "selector": {
                "type": "string",
                "description": "(可选)等待可见的CSS选择器。元素可见时即满足条件。"
            },
            "timeout": {
                "type": "number",
                "description": "(可选)超时秒数,默认10秒。超时返回失败。"
            }
        },
        required=[],
    )
    async def browser_wait_for(
            self,
            text: Optional[str] = None,
            disappear_text: Optional[str] = None,
            selector: Optional[str] = None,
            timeout: float = 10.0,
    ) -> ToolResult:
        # wait_for内部已有Playwright原生超时,外层兜底取用户timeout与上限较大值,
        # 确保用户指定的较长等待不被截断;无任何参数时直接返回失败避免空等
        if text is None and disappear_text is None and selector is None:
            return ToolResult(
                success=False,
                message="browser_wait_for至少需要提供一个条件(text/disappear_text/selector)",
            )
        outer_timeout = max(timeout, self._OP_TIMEOUT_WAIT_FOR)
        return await self._with_timeout(
            self.browser.wait_for(text, disappear_text, selector, timeout),
            outer_timeout, "增量等待",
        )

    @tool(
        name="browser_network_requests",
        description="获取已捕获的XHR/fetch请求列表(SPA异步通信信号)。用于判断异步加载是否完成、排查接口报错。仅记录xhr/fetch类型(排除图片/脚本等静态资源)。可选按URL子串过滤特定接口,获取后可选清空日志避免历史累积干扰。",
        parameters={
            "max_entries": {
                "type": "integer",
                "description": "(可选)返回的最大条目数,默认20。按时间倒序取最近N条。"
            },
            "url_filter": {
                "type": "string",
                "description": "(可选)URL子串过滤,仅返回包含该子串的请求。用于排查特定接口。"
            },
            "clear": {
                "type": "boolean",
                "description": "(可选)获取后是否清空日志,默认false。排查完毕后传true清空,避免历史日志干扰下次排查。"
            }
        },
        required=[],
    )
    async def browser_network_requests(
            self,
            max_entries: int = 20,
            url_filter: Optional[str] = None,
            clear: bool = False,
    ) -> ToolResult:
        return await self._with_timeout(
            self.browser.network_requests(max_entries, url_filter, clear),
            self._OP_TIMEOUT_NETWORK, "获取网络请求",
        )
