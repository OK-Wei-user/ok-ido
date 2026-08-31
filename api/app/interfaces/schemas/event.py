#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/09 10:33

@File    : event.py
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any, Self, Type, Literal, List, Union, get_args

from pydantic import BaseModel, Field, ConfigDict

from app.domain.models.event import Event, PlanEvent, ToolEventStatus, ToolEvent, StepEvent, StepEventStatus
from app.domain.models.file import File
from app.domain.models.plan import ExecutionStatus


# StepEventStatus → ExecutionStatus 映射(步骤事件状态序列化专用)
# 根因修复: StepEvent.step 是共享可变引用,step.status 可能在事件持久化前被
# react.py 中的 step.status = COMPLETED 赋值污染,导致进度事件快照错误捕获 completed。
# event.status(StepEventStatus)在事件创建时设定且不可变,始终正确反映事件语义:
#   STARTED → RUNNING(执行中,含进度推进)
#   COMPLETED → COMPLETED(步骤完成)
#   FAILED → FAILED(步骤失败)
_STEP_EVENT_STATUS_MAP: dict[StepEventStatus, ExecutionStatus] = {
    StepEventStatus.STARTED: ExecutionStatus.RUNNING,
    StepEventStatus.COMPLETED: ExecutionStatus.COMPLETED,
    StepEventStatus.FAILED: ExecutionStatus.FAILED,
}


class BaseEventData(BaseModel):
    """基础事件数据"""
    event_id: Optional[str] = None  # 事件id
    created_at: datetime = Field(default_factory=datetime.now)  # 事件时间

    # pydantic v2写法，序列化时将datetime转换为时间戳
    model_config = ConfigDict(json_encoders={
        datetime: lambda v: int(v.timestamp())
    })

    @classmethod
    def base_event_data(cls, event: Event) -> Dict[str, Any]:
        """类方法，用于将事件Domain模型转换成基础事件数据字典"""
        return {
            "event_id": event.id,
            "created_at": int(event.created_at.timestamp()),
        }

    @classmethod
    def from_event(cls, event: Event) -> Self:
        """从事件Domain模型中构建基础事件数据"""
        return cls(
            **cls.base_event_data(event),
            **event.model_dump(mode="json", exclude={"id", "type", "created_at"}),
        )


class BaseSSEEvent(BaseModel):
    """基础流式事件数据类型"""
    event: str  # 事件类型
    data: BaseEventData  # 数据

    @classmethod
    def from_event(cls, event: Event) -> Self:
        """将事件Domain模型转换成基础流式事件"""
        # 1.获取事件数据的类型，如果没有则使用基础事件数据BaseEventData
        data_class: Type[BaseEventData] = cls.__annotations__.get("data", BaseEventData)

        # 2.调用构造函数完成初始化
        return cls(
            event=event.type,
            data=data_class.from_event(event),
        )


class CommonEventData(BaseEventData):
    """通用事件数据，让结构允许填充额外的数据"""
    model_config = ConfigDict(
        json_encoders={
            datetime: lambda v: int(v.timestamp()),
        },
        extra="allow",
    )


class CommonSSEEvent(BaseSSEEvent):
    """通用事件"""
    event: str
    data: CommonEventData


class MessageEventData(BaseEventData):
    """消息事件数据"""
    role: Literal["user", "assistant"] = "assistant"
    message: str = ""
    attachments: List[File] = Field(default_factory=list)
    is_streaming: bool = False  # 流式delta标记，True表示增量chunk
    is_final: bool = False  # 最终答案标记，True 表示会话最终答案（summarize 输出）
    is_thinking: bool = False  # 思考过程标记，True 表示思考内容增量(前端路由到「思考中」区域)


class MessageSSEEvent(BaseSSEEvent):
    """流式消息事件数据响应结构"""
    event: Literal["message"] = "message"
    data: MessageEventData

    @classmethod
    def from_event(cls, event: Event) -> Self:
        return cls(
            data=MessageEventData(
                **BaseEventData.base_event_data(event),
                role=event.role,
                message=event.message,
                attachments=event.attachments,
                is_streaming=getattr(event, "is_streaming", False),
                is_final=getattr(event, "is_final", False),
                is_thinking=getattr(event, "is_thinking", False),
            )
        )


class TitleEventData(BaseEventData):
    """标题事件数据"""
    title: str


class TitleSSEEvent(BaseSSEEvent):
    """标题流式事件"""
    event: Literal["title"] = "title"
    data: TitleEventData


class StepEventData(BaseEventData):
    """步骤事件数据"""
    id: str  # 步骤id
    status: ExecutionStatus  # 步骤执行状态
    description: str  # 步骤描述
    progress: int = 0  # 步骤执行进度(0-100),F10-5 新增,0 表示未上报


class StepSSEEvent(BaseSSEEvent):
    """步骤流式事件"""
    event: Literal["step"] = "step"
    data: StepEventData

    @classmethod
    def from_event(cls, event: StepEvent) -> Self:
        return cls(
            data=StepEventData(
                **BaseEventData.base_event_data(event),
                status=_STEP_EVENT_STATUS_MAP.get(event.status, ExecutionStatus.RUNNING),
                id=event.step.id,
                description=event.step.description,
                progress=getattr(event, "progress", 0),
            )
        )


class PlanEventData(BaseEventData):
    """计划事件数据"""
    steps: List[StepEventData]


class PlanSSEEvent(BaseSSEEvent):
    """计划流式事件"""
    event: Literal["plan"] = "plan"
    data: PlanEventData

    @classmethod
    def from_event(cls, event: PlanEvent) -> Self:
        return cls(
            data=PlanEventData(
                **BaseEventData.base_event_data(event),
                steps=[
                    StepEventData(
                        **BaseEventData.base_event_data(event),
                        id=step.id,
                        status=step.status,
                        description=step.description,
                    )
                    for step in event.plan.steps
                ]
            )
        )


class ToolEventData(BaseEventData):
    """工具事件数据"""
    tool_call_id: str  # 工具调用id
    name: str  # 工具箱名字
    status: ToolEventStatus  # 工具状态
    function: str  # 工具名字
    args: Dict[str, Any]  # 工具参数
    content: Optional[Any] = None  # 工具调用结果
    is_streaming: bool = False  # Shell 输出流式增量标记，True 表示中间轮询事件仅推 SSE 不写 DB


class ToolSSEEvent(BaseSSEEvent):
    """工具流式事件"""
    event: Literal["tool"] = "tool"
    data: ToolEventData

    @classmethod
    def from_event(cls, event: ToolEvent) -> Self:
        return cls(
            data=ToolEventData(
                **BaseEventData.base_event_data(event),
                tool_call_id=event.tool_call_id,
                name=event.tool_name,
                status=event.status,
                function=event.function_name,
                args=event.function_args,
                content=event.tool_content,
                is_streaming=getattr(event, "is_streaming", False),
            )
        )


class DoneSSEEvent(BaseSSEEvent):
    """停止流式事件"""
    event: Literal["done"] = "done"


class WaitSSEEvent(BaseSSEEvent):
    """等待人类输入流式事件"""
    event: Literal["wait"] = "wait"


class ErrorEventData(BaseEventData):
    """错误事件数据"""
    error: str


class ErrorSSEEvent(BaseSSEEvent):
    """错误流式事件"""
    event: Literal["error"] = "error"
    data: ErrorEventData


# 定义Agent流式事件类型集合
AgentSSEEvent = Union[
    CommonSSEEvent,
    MessageSSEEvent,
    TitleSSEEvent,
    StepSSEEvent,
    PlanSSEEvent,
    ToolSSEEvent,
    DoneSSEEvent,
    ErrorSSEEvent,
    WaitSSEEvent,
]


@dataclass
class EventMapping:
    """事件映射数据类，用于存储事件映射信息，涵盖流式事件类型、数据类、事件类型字符串"""
    sse_event_class: Type[BaseSSEEvent]
    data_class: Type[BaseEventData]
    event_type: str


class EventMapper:
    """事件映射类，利用Python自身提供的自省机制，将业务逻辑中的Event转换成适合流式传输的AgentSSEEvent"""
    # 缓存映射(type: EventMapping)
    _cache_mapping: Optional[Dict[str, EventMapping]] = None

    @staticmethod
    def _get_event_type_mapping() -> Dict[str, EventMapping]:
        """通过反射动态构建从事件类型字符串到AgentSSEEvent的映射"""
        # 1.判断缓存映射是否存在，如果存在则直接返回
        if EventMapper._cache_mapping is not None:
            return EventMapper._cache_mapping

        # 2.获取AgentSSEEvent的所有可能存在类
        sse_event_classes = get_args(AgentSSEEvent)
        mapping = {}

        # 3.循环遍历AgentSSEEvent可能的所有类逐个处理
        for sse_event_class in sse_event_classes:
            # 4.跳过基类
            if sse_event_class == BaseSSEEvent:
                continue

            # 5.检查类是否包含event属性
            if hasattr(sse_event_class, "__annotations__") and "event" in sse_event_class.__annotations__:
                # 6.提取事件字段
                event_field = sse_event_class.__annotations__["event"]

                # 7.提取事件的具体值(Literal的值)
                if hasattr(event_field, "__args__") and len(event_field.__args__) > 0:
                    event_type = event_field.__args__[0]

                    # 8.提取sse的载荷数据
                    data_class = None
                    if hasattr(sse_event_class, "__annotations__") and "data" in sse_event_class.__annotations__:
                        data_class = sse_event_class.__annotations__["data"]

                    # 9.构建并注册映射关系
                    mapping[event_type] = EventMapping(
                        sse_event_class=sse_event_class,
                        data_class=data_class,
                        event_type=event_type
                    )

        # 10.更新类级缓存
        EventMapper._cache_mapping = mapping
        return mapping

    @staticmethod
    def event_to_sse_event(event: Event) -> AgentSSEEvent:
        """将领域事件转换为Agent流式事件模型"""
        # 1.获取事件映射表
        event_type_mapping = EventMapper._get_event_type_mapping()

        # 2.根据传递进来的事件获取映射类
        event_mapping = event_type_mapping.get(event.type)

        # 3.如果找到了类型映射则进行转换
        if event_mapping:
            sse_event = event_mapping.sse_event_class.from_event(event)
            return sse_event

        # 4.如果没找到类型则使用通用类型
        return CommonSSEEvent.from_event(event)

    @staticmethod
    def events_to_sse_events(events: List[Event]) -> List[AgentSSEEvent]:
        """将领域事件模型列表转换为SSE流式事件列表"""
        return list(filter(lambda x: x is not None, [
            EventMapper.event_to_sse_event(event) for event in events
        ]))
