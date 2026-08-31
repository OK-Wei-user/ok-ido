#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/18 0:54

@File    : event.py
"""
import uuid
from datetime import datetime
from enum import Enum
from typing import Literal, List, Union, Optional, Any, Dict, Annotated

from pydantic import BaseModel, Field

from .file import File
from .plan import Plan, Step
from .research import ResearchSummary
from .search import SearchResultItem
from .tool_result import ToolResult


class PlanEventStatus(str, Enum):
    """规划事件状态"""
    CREATED = "created"  # 已创建
    UPDATED = "updated"  # 已更新
    COMPLETED = "completed"  # 已完成


class StepEventStatus(str, Enum):
    """步骤事件状态"""
    STARTED = "started"  # 已开始
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"  # 失败


class ToolEventStatus(str, Enum):
    """工具事件状态类型枚举"""
    CALLING = "calling"  # 调用中
    CALLED = "called"  # 调用完毕


class BaseEvent(BaseModel):
    """基础事件类型"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))  # 事件id
    type: Literal[""] = ""  # 事件的类型
    created_at: datetime = Field(default_factory=datetime.now)  # 事件创建时间


class PlanEvent(BaseEvent):
    """规划事件类型"""
    type: Literal["plan"] = "plan"
    plan: Plan  # 规划
    status: PlanEventStatus = PlanEventStatus.CREATED  # 规划事件状态


class TitleEvent(BaseEvent):
    """标题事件类型"""
    type: Literal["title"] = "title"
    title: str = ""  # 标题


class StepEvent(BaseEvent):
    """子任务/步骤事件

    F10-5 进度状态扩展:
    - progress: 步骤执行进度(0-100),0 表示未上报/未启用,100 表示完成
      - STARTED 事件: progress=0(开始)或基于已执行工具数估算
      - STARTED 中间进度事件: progress=N(工具调用推进)
      - COMPLETED 事件: progress=100
      - FAILED 事件: progress 保持上次值,前端据此显示中断位置
    前端契约: progress=0 时等价于未上报,前端可不展示进度条
    """
    type: Literal["step"] = "step"
    step: Step  # 步骤信息
    status: StepEventStatus = StepEventStatus.STARTED
    message: str = ""  # 步骤事件附加说明(如重试原因),默认为空
    progress: int = 0  # 步骤执行进度(0-100),0 表示未上报


class MessageEvent(BaseEvent):
    """消息事件，包含人类消息和AI消息

    is_streaming: 流式delta标记，True表示该消息为流式输出的增量chunk，
                  仅推送到SSE不持久化DB，前端累积显示。
                  仅 summarize() 的最终答案流式输出会产生此标记。
    is_final: 最终答案标记，True 表示该消息为会话最终答案（summarize 输出），
              AgentTaskRunner 据此写库（update_latest_message + increment_unread_message_count）。
              step.result / ask_user 等中间消息不携带 is_final=True。
    is_thinking: 思考过程标记，True 表示该消息为 LLM 思考内容(reasoning_content)的流式增量。
                 配合 is_streaming=True 仅推 SSE 不写 DB(思考属过程性信息,历史回放不需要)。
                 前端据此路由到「思考中」区域展示;PlannerAgent/ReActAgent 据此守卫跳过 Plan/Step JSON 解析。
    """
    type: Literal["message"] = "message"
    role: Literal["user", "assistant"] = "assistant"  # 消息角色
    message: str = ""  # 消息本身
    attachments: List[File] = Field(default_factory=list)  # 附件列表信息
    is_streaming: bool = False  # 流式delta标记，True表示增量chunk仅推SSE不写DB
    is_final: bool = False  # 最终答案标记，True 表示会话最终答案（summarize 输出）
    is_thinking: bool = False  # 思考过程标记，True 表示思考内容增量(配合 is_streaming 仅推 SSE)


class BrowserToolContent(BaseModel):
    """浏览器工具扩展内容"""
    screenshot: Optional[str] = None  # 浏览器快照截图(节流时可能为空)
    message: Optional[str] = None  # 操作结果摘要(无截图时供前端展示,避免"等待页面截图"误导)


class SearchToolContent(BaseModel):
    """搜索工具内容"""
    results: List[SearchResultItem]  # 搜索结果列表


class ShellToolContent(BaseModel):
    """Shell工具内容"""
    console: Any  # 控制台内容


class FileToolContent(BaseModel):
    """文件工具内容"""
    content: str  # 文件内容


class MCPToolContent(BaseModel):
    """MCP工具内容"""
    result: Any  # MCP工具结果(图片base64已剥离,仅保留文本结构)
    images: Optional[List[str]] = None  # MCP工具返回的图片URL列表(已上传OSS,供前端展示)


class A2AToolContent(BaseModel):
    """A2A智能体工具内容"""
    a2a_result: Any  # A2A智能体调用结果


class SkillToolContent(BaseModel):
    """技能工具内容"""
    result: Any  # 技能工具结果


class DeepResearchToolContent(BaseModel):
    """深度研究工具内容，供前端展示研究摘要"""
    summary: ResearchSummary  # 研究总结（分档洞察+来源数+后续查询）


ToolContent = Union[
    BrowserToolContent,
    SearchToolContent,
    ShellToolContent,
    FileToolContent,
    MCPToolContent,
    A2AToolContent,
    SkillToolContent,
    DeepResearchToolContent,
]


class ToolEvent(BaseEvent):
    """工具事件

    is_streaming: Shell 输出流式增量标记，True 表示该事件为命令执行期间的中间轮询输出，
                  仅推 SSE 不持久化 DB(中间输出频繁,历史回放只见 CALLED 完整 console)。
                  仅 stream_shell_output 启用时由 _poll_shell_console 产出。
    """
    type: Literal["tool"] = "tool"
    tool_call_id: str  # 工具调用id
    tool_name: str  # 工具箱/工具集的名字
    tool_content: Optional[ToolContent] = None  # 工具扩展内容
    function_name: str  # LLM调用函数/工具名字
    function_args: Dict[str, Any]  # LLM生成的工具调用参数
    function_result: Optional[ToolResult] = None  # 工具调用结果
    status: ToolEventStatus = ToolEventStatus.CALLING  # 工具事件状态
    is_streaming: bool = False  # Shell 输出流式增量标记，True 表示中间轮询事件仅推 SSE 不写 DB


class WaitEvent(BaseEvent):
    """等待事件，等待用户输入确认"""
    type: Literal["wait"] = "wait"


class ErrorEvent(BaseEvent):
    """错误事件"""
    type: Literal["error"] = "error"
    error: str = ""  # 错误信息


class DoneEvent(BaseEvent):
    """结束事件类型"""
    type: Literal["done"] = "done"


class SandboxScanEvent(BaseEvent):
    """沙箱交付物主动扫描结果事件(批次45 P0-1)

    SUMMARIZING 阶段 session.files 为空时,Flow 主动扫描沙箱,
    通过此事件将扫描到的文件路径回传给 Runner 进行同步。

    内部事件: Runner 处理后不 yield 给外层(不持久化/不推送前端)。
    """
    type: Literal["sandbox_scan"] = "sandbox_scan"
    file_paths: List[str] = Field(default_factory=list)  # 扫描到的交付物沙箱路径


# 定义应用事件类型声明
Event = Annotated[
    Union[
        PlanEvent,
        TitleEvent,
        StepEvent,
        MessageEvent,
        ToolEvent,
        WaitEvent,
        ErrorEvent,
        DoneEvent,
        SandboxScanEvent,
    ],
    Field(discriminator="type"),
]
