#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/5/17 17:21

@File    : openai_llm.py
基于OpenAI SDK的LLM调用实现，适配DeepSeek V4思考模式

DeepSeek V4思考模式参数说明：
- extra_body.thinking.type: "enabled"开启思考 / "disabled"关闭思考
- reasoning_effort: 思考强度，仅thinking=enabled时生效，取值low/medium/high/max/xhigh
- 思考模式下assistant响应携带reasoning_content字段，工具调用场景必须回传该字段

tenacity 包装网络层重试（429/5xx），语义错误仍由 BaseAgent 处理。
批次50增强: max_retries可配置 + 503/502专属退避策略(更长等待给服务端恢复时间)。
"""
import logging
from typing import List, Dict, Any, Optional, AsyncGenerator

from openai import AsyncOpenAI
from openai import APIError, APIStatusError, APITimeoutError, APIConnectionError, RateLimitError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    RetryCallState,
)

from app.application.errors.exceptions import ServerRequestsError
from app.domain.external.llm import LLM
from app.domain.models.app_config import LLMConfig, ThinkingMode
from app.infrastructure.external.llm.dsml_parser import parse_dsml_to_tool_calls, strip_dsml_artifacts
from app.infrastructure.external.llm.exceptions import RetryableLLMError, NonRetryableLLMError
from app.infrastructure.external.llm.stream_chunk import LLMStreamChunk

logger = logging.getLogger(__name__)

# 可重试的 HTTP 状态码：限流与服务端瞬时错误
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# --- 批次50: 503/502服务不可用专属退避策略 ---
# 502/503(服务不可用)通常需要较长恢复时间,使用更长退避(max=30s)
_SERVICE_UNAVAILABLE_CODES = {502, 503}
_SERVICE_UNAVAILABLE_BACKOFF = dict(multiplier=2, min=2, max=30)
# 通用可重试错误(429限流/500/504/超时)使用较短退避(max=10s)
_DEFAULT_BACKOFF = dict(multiplier=1, min=1, max=10)


def _select_wait_strategy(retry_state: RetryCallState) -> float:
    """根据异常状态码选择退避等待时间

    502/503(服务不可用): 指数退避 2s→4s→8s→16s→30s,给服务端充分恢复时间。
    其他可重试错误(429/500/504/超时): 指数退避 1s→2s→4s→8s→10s,快速重试。

    批次50根因: 会话f5c52cb2因503服务过载,原3次重试(max=8s)总等待~7s不足以等待服务恢复。
    """
    outcome = retry_state.outcome
    if outcome and outcome.failed:
        exc = outcome.exception()
        status_code = getattr(exc, "status_code", None)
        if status_code in _SERVICE_UNAVAILABLE_CODES:
            return wait_exponential(**_SERVICE_UNAVAILABLE_BACKOFF)(retry_state)
    return wait_exponential(**_DEFAULT_BACKOFF)(retry_state)


class OpenAILLM(LLM):
    """基于OpenAI SDK的LLM调用实现

    支持DeepSeek V4系列模型(deepseek-v4-pro/deepseek-v4-flash)与GLM-5.2等OpenAI兼容服务，
    通过LLMConfig.thinking_mode控制思考模式开关，
    思考开启时自动传递reasoning_effort参数。
    """

    def __init__(self, llm_config: LLMConfig, **kwargs) -> None:
        self._client = AsyncOpenAI(
            base_url=str(llm_config.base_url),
            api_key=llm_config.api_key,
            **kwargs,
        )
        self._model_name = llm_config.model_name
        self._temperature = llm_config.temperature
        self._max_tokens = llm_config.max_tokens
        self._timeout = 3600
        # 由配置显式控制思考模式，不再从模型名推断
        self._thinking_enabled = llm_config.thinking_mode == ThinkingMode.ENABLED
        self._reasoning_effort = llm_config.reasoning_effort
        # 批次50: max_retries可配置(从LLMConfig读取,默认5次)
        self._max_retries = llm_config.max_retries
        # 是否支持图像输入(多模态): 控制工具结果截图是否以image_url块发送给LLM
        self._supports_image_input = llm_config.supports_image_input

    def _build_extra_body(self) -> Optional[Dict[str, Any]]:
        """构建DeepSeek V4思考模式extra_body参数"""
        if not self._thinking_enabled:
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "enabled"}}

    def _build_create_kwargs(
            self,
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]] = None,
            response_format: Dict[str, Any] = None,
            tool_choice: str = None,
    ) -> Dict[str, Any]:
        """构造 chat.completions.create 调用参数

        思考模式与tool_choice兼容性处理:
        GLM/DeepSeek等模型思考模式不支持tool_choice参数(返回400错误),
        适配器层自动降级tool_choice为None,让LLM自主决定工具调用。
        这样领域层(BaseAgent/ReActAgent)无感知LLM提供商差异。
        """
        create_kwargs: Dict[str, Any] = dict(
            model=self._model_name,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            messages=messages,
            response_format=response_format,
            timeout=self._timeout,
        )

        # 思考模式开启时传递reasoning_effort控制思考深度
        if self._thinking_enabled:
            create_kwargs["reasoning_effort"] = self._reasoning_effort

        if tools:
            # 思考模式与tool_choice兼容性降级:
            # GLM/DeepSeek思考模式不支持tool_choice="required"等显式强制,
            # 自动降级为None让LLM自主决策,避免API返回400错误。
            if self._thinking_enabled and tool_choice is not None:
                logger.warning(
                    f"思考模式开启时tool_choice={tool_choice!r}不被支持,"
                    f"自动降级为None让LLM自主决策(模型: {self._model_name})"
                )
                tool_choice = None
            logger.info(f"调用OpenAI客户端向LLM发起请求并携带工具信息: {self._model_name}")
            create_kwargs.update(tools=tools, tool_choice=tool_choice, parallel_tool_calls=False)
        else:
            logger.info(f"调用OpenAI客户端向LLM发起请求: {self._model_name}")

        create_kwargs["extra_body"] = self._build_extra_body()
        return create_kwargs

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    @property
    def supports_images(self) -> bool:
        """是否支持图像输入(多模态): false时工具结果截图不构建image_url块"""
        return self._supports_image_input

    async def _invoke_once(
            self,
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]] = None,
            response_format: Dict[str, Any] = None,
            tool_choice: str = None,
    ) -> Dict[str, Any]:
        """执行单次LLM调用(不含重试逻辑)

        将SDK异常转换为RetryableLLMError/NonRetryableLLMError,
        供 invoke() 的 AsyncRetrying 按异常类型决定是否重试。

        - RetryableLLMError: 429/500/502/503/504/超时/网络故障 → tenacity重试
        - NonRetryableLLMError: 4xx(非429) → 不重试,直接抛出
        - ServerRequestsError: 未知异常 → 不重试,直接抛出
        """
        try:
            create_kwargs = self._build_create_kwargs(
                messages, tools, response_format, tool_choice
            )
            response = await self._client.chat.completions.create(**create_kwargs)
            logger.debug(f"OpenAI客户端返回内容: {response.model_dump()}")
            message = response.choices[0].message.model_dump()
            return self._normalize_dsml_response(message, tools)
        except RetryableLLMError:
            raise
        except NonRetryableLLMError:
            raise
        except RateLimitError as e:
            logger.warning(f"LLM触发限流(429)，tenacity将自动重试: {str(e)}")
            raise RetryableLLMError(str(e), status_code=429) from e
        except APITimeoutError as e:
            logger.warning(f"LLM请求超时，tenacity将自动重试: {str(e)}")
            raise RetryableLLMError(str(e)) from e
        except APIConnectionError as e:
            logger.warning(f"LLM网络连接失败，tenacity将自动重试: {str(e)}")
            raise RetryableLLMError(str(e)) from e
        except APIStatusError as e:
            status_code = getattr(e, "status_code", None) or (
                getattr(e.response, "status_code", None) if hasattr(e, "response") else None
            )
            if status_code in _RETRYABLE_STATUS_CODES:
                logger.warning(f"LLM返回{status_code}错误，tenacity将自动重试: {str(e)}")
                raise RetryableLLMError(str(e), status_code=status_code) from e
            logger.error(f"LLM返回不可重试的{status_code}错误: {str(e)}")
            raise NonRetryableLLMError(str(e), status_code=status_code) from e
        except APIError as e:
            # 通用API错误，按状态码区分（部分SDK不抛APIStatusError）
            status_code = getattr(e, "status_code", None)
            if status_code in _RETRYABLE_STATUS_CODES:
                logger.warning(f"LLM返回{status_code}错误(APIError)，tenacity将自动重试: {str(e)}")
                raise RetryableLLMError(str(e), status_code=status_code) from e
            logger.error(f"LLM返回不可重试的API错误: {str(e)}")
            raise NonRetryableLLMError(str(e), status_code=status_code) from e
        except Exception as e:
            logger.error(f"调用OpenAI客户端发生未知错误: {str(e)}")
            raise ServerRequestsError("调用OpenAI客户端向LLM发起请求出错") from e

    async def invoke(
            self,
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]] = None,
            response_format: Dict[str, Any] = None,
            tool_choice: str = None,
    ) -> Dict[str, Any]:
        """调用LLM并返回assistant消息字典

        自动注入思考模式参数(extra_body/reasoning_effort)，
        工具调用场景下parallel_tool_calls强制为False防止多工具并发。
        网络层错误（429/5xx）由 tenacity 自动重试，4xx 不重试。

        批次50增强:
        - max_retries 从 LLMConfig 读取(默认5次,原硬编码3次)
        - 502/503 服务不可用使用更长退避(max=30s),给服务端恢复时间
        - 其他可重试错误使用较短退避(max=10s),快速重试
        """
        result: Optional[Dict[str, Any]] = None
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(RetryableLLMError),
            stop=stop_after_attempt(self._max_retries),
            wait=_select_wait_strategy,
            reraise=True,
        ):
            with attempt:
                result = await self._invoke_once(
                    messages, tools, response_format, tool_choice
                )
        return result

    @staticmethod
    def _normalize_dsml_response(
            message: Dict[str, Any], tools: List[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """检测并转换DeepSeek DSML标记为标准tool_calls,并清理残余DSML标记

        两层处理确保DSML标记不会泄漏到用户输出:
        1. 工具调用场景(tools非空且无标准tool_calls): 解析DSML为标准tool_calls
        2. 所有场景(含summarize等无tools场景): 清理content中残余DSML标记

        根因: summarize调用LLM时tools_enabled=False,导致tools=None。
        若LLM异常输出DSML工具调用标记,旧逻辑因`not tools`提前返回,
        DSML标记直接泄漏到最终MessageEvent(is_final=True)。
        """
        content = message.get("content")
        if not content:
            return message

        # 1.工具调用场景: 尝试将DSML解析为标准tool_calls
        if tools and not message.get("tool_calls"):
            cleaned_content, dsml_tool_calls = parse_dsml_to_tool_calls(content)
            if dsml_tool_calls:
                message["tool_calls"] = dsml_tool_calls
                message["content"] = cleaned_content if cleaned_content else None
                return message
            # DSML解析未提取到工具调用,继续执行清理逻辑

        # 2.所有场景: 清理content中残余的DSML标记,防止泄漏到用户输出
        # 覆盖summarize等无tools场景,以及DSML解析失败但有残余标记的场景
        if "DSML" in content:
            cleaned = strip_dsml_artifacts(content)
            if cleaned != content:
                message["content"] = cleaned

        return message

    async def astream(
            self,
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]] = None,
            response_format: Dict[str, Any] = None,
            tool_choice: str = None,
            keep_response_format: bool = False,
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        """流式调用LLM，yield LLMStreamChunk

        自动注入思考模式参数。
        keep_response_format=False(默认): 移除response_format,纯文本输出,防止JSON片段(Summarize场景)
        keep_response_format=True: 保留response_format,支持JSON输出(ReAct流式调用场景,
            delta_content仅累积不直推前端,JSON片段不会到达前端)
        不加 @retry：tenacity 与 async generator 不兼容，重试由调用方处理。
        网络层错误抛出 RetryableLLMError/NonRetryableLLMError，由调用方处理降级。
        """
        try:
            create_kwargs = self._build_create_kwargs(
                messages, tools, response_format, tool_choice
            )
            create_kwargs["stream"] = True
            # keep_response_format=False时移除response_format(默认行为,Summarize场景防JSON片段)
            # keep_response_format=True时保留(ReAct流式场景,delta_content仅累积不直推前端)
            if not keep_response_format:
                create_kwargs.pop("response_format", None)

            logger.info(f"调用OpenAI客户端向LLM发起流式请求: {self._model_name}")
            response = await self._client.chat.completions.create(**create_kwargs)

            async for chunk in response:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                delta_content = ""
                delta_reasoning = ""
                delta_tool_calls = None

                if delta:
                    if hasattr(delta, "content") and delta.content:
                        delta_content = delta.content
                    # DeepSeek V4思考模式的推理内容增量（SDK扩展字段，用hasattr安全检查）
                    if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                        delta_reasoning = delta.reasoning_content
                    # 流式tool_calls增量(OpenAI SDK: delta.tool_calls按index分片返回)
                    # 每个分片包含index/id/type/function.name/function.arguments的部分内容
                    # 调用方按index累积合并,构建完整tool_calls
                    if hasattr(delta, "tool_calls") and delta.tool_calls:
                        delta_tool_calls = [
                            tc.model_dump(exclude_none=True) if hasattr(tc, "model_dump") else tc
                            for tc in delta.tool_calls
                        ]

                finish_reason = choice.finish_reason if choice.finish_reason else None

                # 跳过完全空的块（keep-alive 心跳）
                if not delta_content and not delta_reasoning and not delta_tool_calls and not finish_reason:
                    continue

                yield LLMStreamChunk(
                    delta_content=delta_content,
                    delta_reasoning=delta_reasoning,
                    delta_tool_calls=delta_tool_calls,
                    finish_reason=finish_reason,
                )
        except RateLimitError as e:
            logger.warning(f"LLM流式触发限流(429): {str(e)}")
            raise RetryableLLMError(str(e), status_code=429) from e
        except APITimeoutError as e:
            logger.warning(f"LLM流式请求超时: {str(e)}")
            raise RetryableLLMError(str(e)) from e
        except APIConnectionError as e:
            logger.warning(f"LLM流式网络连接失败: {str(e)}")
            raise RetryableLLMError(str(e)) from e
        except APIStatusError as e:
            status_code = getattr(e, "status_code", None) or (
                getattr(e.response, "status_code", None) if hasattr(e, "response") else None
            )
            if status_code in _RETRYABLE_STATUS_CODES:
                logger.warning(f"LLM流式返回{status_code}错误: {str(e)}")
                raise RetryableLLMError(str(e), status_code=status_code) from e
            logger.error(f"LLM流式返回不可重试的{status_code}错误: {str(e)}")
            raise NonRetryableLLMError(str(e), status_code=status_code) from e
        except APIError as e:
            status_code = getattr(e, "status_code", None)
            if status_code in _RETRYABLE_STATUS_CODES:
                raise RetryableLLMError(str(e), status_code=status_code) from e
            raise NonRetryableLLMError(str(e), status_code=status_code) from e
        except Exception as e:
            logger.error(f"流式调用OpenAI客户端发生未知错误: {str(e)}")
            raise ServerRequestsError("流式调用OpenAI客户端出错") from e
