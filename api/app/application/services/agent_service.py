#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/04 17:12

@File    : agent_service.py
"""
import asyncio
import logging
from datetime import datetime
from typing import AsyncGenerator, Optional, List, Type, Callable, Dict, TYPE_CHECKING

from pydantic import TypeAdapter

from app.domain.external.file_storage import FileStorage
from app.domain.external.json_parser import JSONParser
from app.domain.external.llm import LLM
from app.domain.external.sandbox import Sandbox
from app.domain.external.search import ContentFetcher, SearchEngine
from app.domain.external.task import Task
from app.domain.models.app_config import AgentConfig, MCPConfig, A2AConfig, DeepResearchConfig, ToolExecutionConfig, FilePresentationConfig
from app.domain.models.event import BaseEvent, ErrorEvent, MessageEvent, Event, DoneEvent, WaitEvent
from app.domain.models.session import Session, SessionStatus
from app.domain.repositories.uow import IUnitOfWork
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.skill_service import SkillService
from app.application.services.file_presentation_service import FilePresentationService
from app.infrastructure.external.llm.token_counter import TokenCounter
from app.infrastructure.external.task_callback import RedisStreamTaskCallbackManager
from core.config import get_settings

if TYPE_CHECKING:
    from app.infrastructure.storage.search_cache import SearchCache
    from app.infrastructure.storage.tool_cache import ToolResultCache
    from app.infrastructure.storage.idempotent_tool_registry import IdempotentToolRegistry
    from app.infrastructure.storage.session_prompt_cache import SessionPromptCache

logger = logging.getLogger(__name__)

# 沙箱空闲销毁TTL默认值(秒): 会话结束后沙箱保留时长,超时自动销毁释放资源。
# 实际运行时从Settings.sandbox_idle_ttl_seconds读取(支持.env/环境变量覆盖)。
# TTL内续接会话可复用沙箱(_cancel_sandbox_ttl取消延迟销毁任务)。
# 默认2小时(7200秒);此常量仅作为文档性默认值,实例属性self._sandbox_idle_ttl_seconds为实际值。
SANDBOX_IDLE_TTL_SECONDS = 7200  # 2小时

# SSE断连恢复配置
_MAX_REPLAY_COUNT = 50  # 单次补发断连事件上限，避免大量事件阻塞SSE
_FALLBACK_REPLAY_COUNT = 10  # last_event_id未找到时的回退补发数
_BROWSER_CLEANUP_TIMEOUT = 5.0  # 停止会话时浏览器清理超时(秒)

# 输出流读取阻塞时长(毫秒)：有限阻塞确保及时检测后台任务失败，
# 避免block_ms=0(无限阻塞)导致Redis XREAD挂起至socket_timeout(30s)
OUTPUT_STREAM_BLOCK_MS = 5000


class AgentService:
    """I-DOAgent服务,管理会话生命周期与任务调度"""

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],
            llm: LLM,
            agent_config: AgentConfig,
            mcp_config: MCPConfig,
            a2a_config: A2AConfig,
            sandbox_cls: Type[Sandbox],
            task_cls: Type[Task],
            json_parser: JSONParser,
            search_engine: SearchEngine,
            content_fetcher: ContentFetcher,
            file_storage: FileStorage,
            skill_service: SkillService,
            search_cache: Optional["SearchCache"] = None,
            deep_research_config: Optional[DeepResearchConfig] = None,
            token_counter: Optional[TokenCounter] = None,
            context_window: int = 64000,
            planner_llm: Optional[LLM] = None,  # PlanAgent轻量化: 规划Agent专用LLM(可选)
            multimodal_llm: Optional[LLM] = None,  # 多模态LLM(可选): 浏览器visual_click视觉点击兜底,None时降级不可用
            tool_cache: Optional["ToolResultCache"] = None,  # 工具结果缓存(可选)
            tool_execution_config: Optional[ToolExecutionConfig] = None,  # 工具并行执行配置(可选)
            idempotent_registry: Optional["IdempotentToolRegistry"] = None,  # 幂等工具调用去重注册表(P10-1,可选)
            file_presentation_config: Optional[FilePresentationConfig] = None,  # F10-8文件展示策略配置(可选,None时默认配置)
            prompt_cache: Optional["SessionPromptCache"] = None,  # 会话级提示词缓存(可选,L1内存+L2 Redis)
    ) -> None:
        """构造函数，完成Agent服务初始化"""
        self._uow_factory = uow_factory
        self._uow = uow_factory()
        self._llm = llm
        self._planner_llm = planner_llm  # PlanAgent轻量化: 规划Agent专用LLM,None时PlannerReActFlow降级到llm
        self._multimodal_llm = multimodal_llm  # 多模态LLM: 浏览器visual_click视觉兜底,None时降级
        self._tool_cache = tool_cache  # 工具结果缓存,None时BaseAgent不使用缓存
        self._tool_execution_config = tool_execution_config  # 工具并行执行配置,None时BaseAgent走串行路径
        self._idempotent_registry = idempotent_registry  # 幂等工具调用去重注册表,None时BaseAgent不使用去重
        self._prompt_cache = prompt_cache  # 会话级提示词缓存,None时MCP/A2A/Skills降级纯内存
        # F10-8文件展示策略服务(集中化交付物过滤规则+交付前校验清单)
        # 共享实例: AgentService与SessionService使用同一份配置,保证过滤规则一致
        self._file_presentation = FilePresentationService(config=file_presentation_config)
        self._agent_config = agent_config
        self._mcp_config = mcp_config
        self._a2a_config = a2a_config
        self._sandbox_cls = sandbox_cls
        self._task_cls = task_cls
        self._json_parser = json_parser
        self._search_engine = search_engine
        self._content_fetcher = content_fetcher
        self._search_cache = search_cache
        self._deep_research_config = deep_research_config
        self._file_storage = file_storage
        self._skill_service = skill_service
        self._token_counter = token_counter
        self._context_window = context_window
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._sandbox_ttl_tasks: Dict[str, asyncio.Task] = {}
        # 字典并发保护锁: _session_locks与_sandbox_ttl_tasks的读-改-写操作
        # 在多协程并发下需要原子性保护，避免竞态导致锁实例被覆盖或TTL任务丢失
        self._locks_guard = asyncio.Lock()
        # 沙箱空闲销毁TTL(秒): 从配置读取,支持.env/环境变量覆盖,默认2小时(7200秒)。
        # 会话结束后延迟销毁沙箱,TTL内续接会话可复用(取消延迟销毁任务)。
        self._sandbox_idle_ttl_seconds: int = get_settings().sandbox_idle_ttl_seconds
        logger.info("AgentService初始化成功")

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """获取会话级别的异步锁，防止同一会话并发chat请求

        双重检查锁优化:
        - 快路径: 无锁检查字典，命中则直接返回(避免高并发下频繁争用_locks_guard)
        - 慢路径: 未命中时进入_locks_guard临界区，再次检查后创建
        确保并发场景下同一session_id拿到同一个Lock实例，互斥生效
        """
        # 1.快路径: 无锁检查(命中即可返回，避免锁争用开销)
        lock = self._session_locks.get(session_id)
        if lock is not None:
            return lock

        # 2.慢路径: 进入临界区再次检查(防止竞态: A和B同时通过快路径检查)
        async with self._locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
                logger.debug(f"会话[{session_id}]创建新会话锁实例")
            return lock

    def remove_session_lock(self, session_id: str) -> None:
        """移除会话锁（删除会话时调用，防止锁字典无限增长）"""
        self._remove_session_lock(session_id)

    def _remove_session_lock(self, session_id: str) -> None:
        """移除会话锁，防止_session_locks字典无限增长

        同步pop操作在CPython下受GIL保护为原子操作，无需额外加锁
        """
        self._session_locks.pop(session_id, None)

    def cancel_sandbox_ttl(self, session_id: str) -> None:
        """取消会话的沙箱延迟销毁任务（删除会话时调用，防止对已删除会话执行延迟销毁）"""
        self._cancel_sandbox_ttl(session_id)

    def _cancel_sandbox_ttl(self, session_id: str) -> None:
        """取消会话的沙箱延迟销毁任务（续接会话复用沙箱时调用）

        同步pop+cancel操作在CPython下受GIL保护为原子操作，无需额外加锁
        """
        ttl_task = self._sandbox_ttl_tasks.pop(session_id, None)
        if ttl_task and not ttl_task.done():
            ttl_task.cancel()
            logger.info(f"会话[{session_id}]沙箱TTL延迟销毁已取消，沙箱将被复用")

    async def _schedule_sandbox_ttl(self, session_id: str) -> None:
        """注册沙箱延迟销毁任务，在TTL超时后自动销毁沙箱释放资源

        每次任务完成或会话停止时调用，确保沙箱在空闲TTL后自动清理。
        如果用户在TTL内发起新消息，_cancel_sandbox_ttl()会取消此任务。
        """
        self._cancel_sandbox_ttl(session_id)

        async def _destroy_after_ttl():
            try:
                await asyncio.sleep(self._sandbox_idle_ttl_seconds)
                logger.info(f"会话[{session_id}]沙箱空闲超过{self._sandbox_idle_ttl_seconds}秒，开始销毁")
                async with self._uow:
                    session = await self._uow.session.get_by_id(session_id)
                if session and session.sandbox_id:
                    try:
                        sandbox = await self._sandbox_cls.get(session.sandbox_id)
                        if sandbox:
                            await sandbox.destroy()
                            logger.info(f"会话[{session_id}]沙箱[{session.sandbox_id}]已销毁")
                    except Exception as e:
                        logger.warning(f"会话[{session_id}]沙箱[{session.sandbox_id}]销毁失败: {e}")
                    async with self._uow:
                        session.sandbox_id = None
                        await self._uow.session.save(session)
                # 字典清理: 在锁保护下移除已完成的TTL任务,避免与_schedule并发写入竞态
                async with self._locks_guard:
                    self._sandbox_ttl_tasks.pop(session_id, None)
            except asyncio.CancelledError:
                logger.debug(f"会话[{session_id}]沙箱TTL延迟销毁任务已取消")
            except Exception as e:
                logger.warning(f"会话[{session_id}]沙箱TTL延迟销毁失败: {e}")
                async with self._locks_guard:
                    self._sandbox_ttl_tasks.pop(session_id, None)

        # 字典写入: 在锁保护下创建TTL任务,避免与_cancel并发pop竞态
        async with self._locks_guard:
            ttl_task = asyncio.create_task(_destroy_after_ttl())
            self._sandbox_ttl_tasks[session_id] = ttl_task
        logger.info(f"会话[{session_id}]沙箱TTL延迟销毁已注册，{self._sandbox_idle_ttl_seconds}秒后执行")

    async def _get_task(self, session: Session) -> Optional[Task]:
        """根据传递的任务会话获取任务实例"""
        # 1.从会话中取出任务id
        task_id = session.task_id
        if not task_id:
            return None

        # 2.调用任务类的get方法获取对应的任务实例
        return self._task_cls.get(task_id)

    async def _create_task(self, session: Session) -> Task:
        """根据传递的会话创建一个新任务"""
        # 1.获取沙箱实例
        sandbox = None
        sandbox_id = session.sandbox_id
        if sandbox_id:
            sandbox = await self._sandbox_cls.get(sandbox_id)

        # 2.判断是否能获取到沙箱(如果没有则创建)
        if not sandbox:
            # 3.沙箱不存在则创建一个新的(有可能被释放了)
            sandbox = await self._sandbox_cls.create()
            session.sandbox_id = sandbox.id
            async with self._uow:
                await self._uow.session.save(session)
        else:
            # 沙箱复用：取消已有的延迟销毁任务
            self._cancel_sandbox_ttl(session.id)

        # 4.从沙箱中获取浏览器实例(注入LLM: 文本LLM用于摘要,多模态LLM用于visual_click视觉兜底)
        browser = await sandbox.get_browser(llm=self._llm, multimodal_llm=self._multimodal_llm)
        if not browser:
            logger.error(f"获取沙箱[{sandbox.id}]中的浏览器实例失败")
            raise RuntimeError(f"获取沙箱[{sandbox.id}]中的浏览器实例失败")

        # 5.创建AgentTaskRunner
        # F10-7: 创建会话级 TaskCallbackManager(基于 Redis Stream),
        # 用于 shell_execute(async_mode=true) 异步任务回调通知
        callback_manager = RedisStreamTaskCallbackManager()
        task_runner = AgentTaskRunner(
            uow_factory=self._uow_factory,
            llm=self._llm,
            planner_llm=self._planner_llm,  # PlanAgent轻量化: 规划Agent专用LLM(可选)
            agent_config=self._agent_config,
            mcp_config=self._mcp_config,
            a2a_config=self._a2a_config,
            session_id=session.id,
            file_storage=self._file_storage,
            json_parser=self._json_parser,
            browser=browser,
            search_engine=self._search_engine,
            content_fetcher=self._content_fetcher,
            search_cache=self._search_cache,
            deep_research_config=self._deep_research_config,
            sandbox=sandbox,
            skill_service=self._skill_service,
            token_counter=self._token_counter,
            context_window=self._context_window,
            tool_cache=self._tool_cache,  # 工具结果缓存(可选)
            tool_execution_config=self._tool_execution_config,  # 工具并行执行配置(可选)
            idempotent_registry=self._idempotent_registry,  # 幂等工具调用去重注册表(P10-1,可选)
            callback_manager=callback_manager,  # F10-7异步任务回调管理器
            file_presentation=self._file_presentation,  # F10-8文件展示策略服务(集中化交付物过滤+校验)
            prompt_cache=self._prompt_cache,  # 会话级提示词缓存(MCP/A2A/Skills持久化)
        )

        # 6.创建任务Task并更新会话中的信息
        task = self._task_cls.create(task_runner=task_runner)
        session.task_id = task.id
        async with self._uow:
            await self._uow.session.save(session)

        return task

    async def _safe_update_unread_count(self, session_id: str) -> None:
        """在独立的后台任务中安全地更新未读消息计数

        该方法通过asyncio.create_task()调用，运行在一个全新的asyncio Task中，
        因此不受sse_starlette的anyio cancel scope影响，数据库操作可以正常完成。
        使用uow_factory创建全新的UoW实例，避免与被取消的上下文共享数据库连接。
        """
        try:
            uow = self._uow_factory()
            async with uow:
                await uow.session.update_unread_message_count(session_id, 0)
        except Exception as e:
            logger.warning(f"会话[{session_id}]后台更新未读消息计数失败: {e}")

    async def _safe_complete_session(self, session_id: str) -> None:
        """在独立的后台任务中安全地将会话状态更新为COMPLETED

        异常发生时(如LLM调用失败、工具异常)，原任务上下文可能已被anyio cancel
        scope影响，直接在except块中执行数据库操作可能被取消。通过
        asyncio.create_task()在全新Task中执行，确保会话状态一定能更新为
        COMPLETED，避免僵尸会话永久停留在RUNNING状态。
        """
        try:
            uow = self._uow_factory()
            async with uow:
                await uow.session.update_status(session_id, SessionStatus.COMPLETED)
            logger.info(f"会话[{session_id}]异常后已安全更新为COMPLETED状态")
        except Exception as e:
            logger.warning(f"会话[{session_id}]后台更新COMPLETED状态失败: {e}")

    async def replay_missed_events(
            self, session_id: str, last_event_id: str,
    ) -> AsyncGenerator[BaseEvent, None]:
        """补发断连期间产生的事件，用于SSE重连后的断点续传

        F3-3流式读取优化: 改用repository.get_events_after仅查询events列并按需切片,
        避免原get_by_id加载完整Session(含memories/files大字段)造成的内存与反序列化开销。
        单轮SSE重连: Session领域对象反序列化 → 仅events JSONB列+limit条事件反序列化。

        最多补发_MAX_REPLAY_COUNT条，避免大量事件阻塞SSE。
        last_event_id未找到时回退补发最近_FALLBACK_REPLAY_COUNT条。
        """
        try:
            async with self._uow:
                result = await self._uow.session.get_events_after(
                    session_id, last_event_id,
                    limit=_MAX_REPLAY_COUNT,
                    fallback_limit=_FALLBACK_REPLAY_COUNT,
                )
            if result is None:
                return  # 会话不存在

            missed, found = result
            if not found and last_event_id:
                # last_event_id未命中(可能已过期或来自旧会话): 记录回退日志便于排查
                logger.warning(
                    f"会话[{session_id}]last_event_id[{last_event_id}]未在历史事件中找到, "
                    f"回退补发最近{_FALLBACK_REPLAY_COUNT}条事件"
                )

            for event in missed:
                yield event

            if missed:
                logger.info(f"会话[{session_id}]补发{len(missed)}条断连事件")
        except Exception as e:
            logger.warning(f"会话[{session_id}]补发断连事件失败: {e}")

    # F10-3 重构: chat()方法拆分,提取输出流消费为独立方法
    # 原chat()约170行,输出流消费循环占30行且逻辑独立,提取后chat()聚焦于
    # 会话状态管理与异常兜底,可读性/可测性提升

    async def _consume_output_stream(
            self,
            session_id: str,
            task: Task,
            latest_event_id: Optional[str],
    ) -> AsyncGenerator[BaseEvent, None]:
        """从任务输出流消费事件并转发(F10-3 重构提取)

        循环条件: task存在且(任务未完成 或 输出流还有未读事件)
        修复竞态条件: task.done可能在最终事件(DoneEvent/is_final消息)入队后才变为True,
        此时输出流中可能还有事件未读取,必须继续drain

        检测到结束事件(DoneEvent/ErrorEvent/WaitEvent)时:
        - 注册沙箱延迟销毁(TTL后自动清理,续接时取消)
        - 退出循环

        Args:
            session_id: 会话ID
            task: 任务实例
            latest_event_id: 上次消费的事件ID(用于断连恢复)

        Yields:
            BaseEvent: 从输出流读取的事件
        """
        while task and (not task.done or not await task.output_stream.is_empty()):
            # 从输出消息队列中获取数据
            event_id, event_str = await task.output_stream.get(
                start_id=latest_event_id, block_ms=OUTPUT_STREAM_BLOCK_MS
            )
            # 防御: get()返回(None,None)时(超时无数据)不覆盖latest_event_id,
            # 否则下次start_id=None→'$'会跳过未消费事件导致前端漏显
            if event_id is not None:
                latest_event_id = event_id
            if event_str is None:
                if task.done:
                    break
                logger.debug(f"在会话[{session_id}]输出队列中未发现事件内容")
                continue

            # 反序列化事件并设置ID
            event = TypeAdapter(Event).validate_json(event_str)
            event.id = event_id
            logger.debug(f"从会话[{session_id}]中获取事件: {type(event).__name__}")

            yield event

            # 检测结束事件: 注册沙箱TTL后退出循环
            if isinstance(event, (DoneEvent, ErrorEvent, WaitEvent)):
                await self._schedule_sandbox_ttl(session_id)
                break

    async def chat(
            self,
            session_id: str,
            message: Optional[str] = None,
            attachments: Optional[List[str]] = None,
            latest_event_id: Optional[str] = None,
            timestamp: Optional[datetime] = None,
    ) -> AsyncGenerator[BaseEvent, None]:
        """根据传递的信息调用Agent服务发起对话请求"""
        lock = await self._get_session_lock(session_id)
        if lock.locked():
            logger.warning(f"会话[{session_id}]正在处理中，拒绝并发请求")
            yield ErrorEvent(error="该会话正在处理中，请等待当前任务完成后再试")
            return

        async with lock:
            # 预初始化task，防止异常发生在task赋值前时except块引用未定义变量
            task = None
            # F3-1批量化: 跟踪本轮是否产出事件,仅在有事件时才在finally调度未读清零后台Task,
            # 保留F1-4原始语义(无事件→不UPDATE),避免空轮次产生无意义DB写入
            events_produced = False
            try:
                # 1.检查会话是否存在
                async with self._uow:
                    session = await self._uow.session.get_by_id(session_id)
                if not session:
                    logger.error(f"尝试与不存在的任务会话[{session_id}]对话")
                    raise RuntimeError("任务会话不存在, 请核实后重试")

                # 2.获取对应会话任务
                task = await self._get_task(session)

                # 3.判断是否传递了message
                if message:
                    # 4.判断是否需要投递消息：
                    #    会话RUNNING且任务存活(task.done=False)时，任务仍在运行并产出输出，
                    #    仅需恢复输出流读取，跳过消息投递，避免SSE重连导致重复LLM处理。
                    #    （AgentTaskRunner.invoke()在WaitEvent后返回，session→WAITING，
                    #     task.done→True，所以WAITING状态一定走重建分支，不会误跳过）
                    is_task_alive = task is not None and not task.done
                    if session.status == SessionStatus.RUNNING and is_task_alive:
                        logger.info(f"会话[{session_id}]任务仍在运行，恢复输出流读取（跳过消息投递）")
                    else:
                        # 5.需要创建新任务或重建死任务：
                        #    a) 会话非RUNNING状态（已完成或空闲）
                        #    b) 任务不存在
                        #    c) 任务已死(done=True)——死任务复用会导致输出流永不产数据，会话卡死
                        is_dead_task = task is not None and task.done
                        if session.status != SessionStatus.RUNNING or task is None or is_dead_task:
                            if is_dead_task:
                                logger.warning(f"会话[{session_id}]检测到死任务(task.done=True)，重建任务以恢复输出流")
                            if task is not None:
                                task.cancel()
                            task = await self._create_task(session)
                            if not task:
                                logger.error(f"会话[{session_id}]创建任务失败")
                                raise RuntimeError(f"会话[{session_id}]创建任务失败")

                        # 6.传递了消息则更新会话中的最后一条消息
                        async with self._uow:
                            await self._uow.session.update_latest_message(
                                session_id=session_id,
                                message=message,
                                timestamp=timestamp or datetime.now(),
                            )

                        # bugfix:从文件数据库中查询数据并更新attachments实际内容
                        # attachments None保护，防止Optional[List[str]]=None时崩溃
                        async with self._uow:
                            db_attachments = [await self._uow.file.get_by_id(fid) for fid in (attachments or [])]

                        # 7.创建一个人类消息事件
                        message_event = MessageEvent(
                            role="user",
                            message=message,
                            attachments=[attachment for attachment in db_attachments if attachment is not None],
                        )

                        # 8.将事件添加到任务的输入流中，好让Agent获取到数据
                        event_id = await task.input_stream.put(message_event.model_dump_json())
                        message_event.id = event_id
                        yield message_event
                        async with self._uow:
                            await self._uow.session.add_event(session_id, message_event)

                        # 9.执行任务
                        await task.invoke()
                        logger.info(f"往会话[{session_id}]输入消息队列写入消息: {message[:50]}...")

                # 10.记录日志展示会话已启动
                logger.info(f"会话[{session_id}]已启动")
                logger.info(f"会话[{session_id}]任务实例: {task}")

                # 防御性修复: 会话状态为RUNNING但task为None(任务已过期/被Redis清理)时,
                # 会话已无法继续产出事件。若直接关闭SSE流而不更新状态,
                # 前端将因SSE_STREAM_END+会话仍RUNNING而500ms无限重连(高频空转循环)。
                # 根因场景: 恢复卡住的WAITING会话时手动改状态为RUNNING,但旧task_id
                # 对应的Redis Stream已过期;或API重启后内存task实例丢失但DB状态未更新。
                # 修复: 更新状态为COMPLETED + 发送DoneEvent通知前端正常结束。
                if task is None:
                    if session.status == SessionStatus.RUNNING:
                        logger.warning(
                            f"会话[{session_id}]状态为RUNNING但task为None(任务已过期),"
                            f"更新为COMPLETED防止前端无限重连空转"
                        )
                        async with self._uow:
                            await self._uow.session.update_status(
                                session_id, SessionStatus.COMPLETED
                            )
                        done_event = DoneEvent()
                        async with self._uow:
                            await self._uow.session.add_event(session_id, done_event)
                        yield done_event
                        events_produced = True
                    # task为None且会话非RUNNING(如COMPLETED/WAITING): 无事件可产出,直接结束
                    logger.info(f"会话[{session_id}]无活跃任务,本轮运行结束")
                    return

                # 11.从任务的输出流中读取数据(F10-3 重构: 提取到 _consume_output_stream)
                # 循环条件/竞态修复/结束事件检测/沙箱TTL注册 均封装在 _consume_output_stream 内
                # F3-1批量化: 未读计数清零统一由finally块的后台Task执行,不在循环内UPDATE
                async for event in self._consume_output_stream(session_id, task, latest_event_id):
                    yield event
                    events_produced = True  # F3-1: 标记本轮已产出事件,finally据此决定是否UPDATE

                # 16.循环外面表示这次任务AI端的已结束
                logger.info(f"会话[{session_id}]本轮运行结束")
            except Exception as e:
                # 17.记录日志并返回错误事件
                logger.error(f"任务会话[{session_id}]对话出错: {str(e)}")

                # 取消可能仍在运行的后台任务，防止资源泄漏
                if task is not None and not task.done:
                    try:
                        task.cancel()
                        logger.info(f"会话[{session_id}]异常后已取消后台任务")
                    except Exception as cancel_err:
                        logger.warning(f"会话[{session_id}]取消后台任务失败: {cancel_err}")

                # 在独立Task中安全更新会话状态为COMPLETED，避免僵尸会话
                # （独立Task不受当前anyio cancel scope影响，数据库操作可正常完成）
                try:
                    asyncio.create_task(self._safe_complete_session(session_id))
                except RuntimeError:
                    logger.warning(f"会话[{session_id}]无法创建后台任务完结会话")

                event = ErrorEvent(error=str(e))
                try:
                    async with self._uow:
                        await self._uow.session.add_event(session_id, event)
                except (asyncio.CancelledError, Exception) as add_err:
                    logger.warning(f"会话[{session_id}]添加错误事件失败(可能是客户端断开连接): {add_err}")
                yield event
                events_produced = True  # F3-1: 错误事件也计入产出,finally据此决定是否UPDATE
            finally:
                # 18.会话完整传递给前端后，表示至少用户肯定收到了这些消息，所以不应该有未读消息数
                # 注意：当SSE客户端断开连接时，sse_starlette使用anyio cancel scope取消当前Task中
                # 所有的await操作（asyncio.shield也无法对抗anyio的cancel scope）。
                # 如果在finally块中直接执行数据库操作，该操作会被立即取消，并且SQLAlchemy在尝试
                # 终止被中断的连接时也会被取消，从而产生ERROR日志并可能污染连接池。
                # 解决方案：将数据库更新操作放到独立的asyncio Task中执行，新Task不受当前
                # cancel scope的影响，可以正常完成数据库操作。
                #
                # F3-1批量化: 仅在本轮产出过事件时才调度未读清零后台Task,
                # 保留F1-4语义(无事件→不UPDATE),避免空轮次产生无意义DB写入
                if events_produced:
                    try:
                        asyncio.create_task(self._safe_update_unread_count(session_id))
                    except RuntimeError:
                        # 事件循环已关闭（如应用正在关闭），无法创建后台任务
                        logger.warning(f"会话[{session_id}]无法创建后台任务更新未读消息计数")

    async def stop_session(self, session_id: str) -> None:
        """根据传递的会话id停止指定会话,清理浏览器状态防止续接时残留页面

        获取会话锁确保与活跃chat请求互斥: 防止stop取消任务时chat正在投递消息,
        避免stop与chat竞态导致状态不一致。
        """
        # 0.获取会话锁,与活跃chat请求互斥
        lock = await self._get_session_lock(session_id)
        async with lock:
            # 1.查找会话是否存在
            async with self._uow:
                session = await self._uow.session.get_by_id(session_id)
            if not session:
                logger.error(f"尝试停止不存在的会话[{session_id}]")
                raise RuntimeError("任务会话不存在, 请核实后重试")

            # 2.根据会话获取任务信息
            task = await self._get_task(session)
            if task:
                # 在取消前尝试清理浏览器状态(导航到空白页,释放当前页面资源)
                # 防止续接会话时残留上个会话的页面状态
                # 使用Task公开接口cleanup_browser,避免反射访问私有属性
                try:
                    await asyncio.wait_for(
                        task.cleanup_browser(),
                        timeout=_BROWSER_CLEANUP_TIMEOUT,
                    )
                    logger.info(f"会话[{session_id}]停止前清理浏览器状态成功")
                except asyncio.TimeoutError:
                    logger.warning(f"会话[{session_id}]停止前清理浏览器超时({_BROWSER_CLEANUP_TIMEOUT}s), 不影响主流程")
                except Exception as e:
                    logger.warning(f"会话[{session_id}]停止前清理浏览器失败: {e}, 不影响主流程")

                # F10-7: 取消 shell_execute(async_mode=true) 启动的后台异步任务
                # 防止任务运行器实例销毁后仍有孤儿任务在运行
                try:
                    await task.cancel_background_tasks()
                    logger.info(f"会话[{session_id}]停止前取消后台异步任务成功")
                except Exception as e:
                    logger.warning(f"会话[{session_id}]停止前取消后台异步任务失败: {e}, 不影响主流程")

            # Batch 35: COMPLETED 先于 cancel(遵循 project_memory 硬约束,防止竞态)
            # 任务取消前先将状态置为 COMPLETED,使被取消任务在 CancelledError 处理器中
            # 看到的状态已是终态,避免二次写入竞态;无任务时同样设置 COMPLETED
            async with self._uow:
                await self._uow.session.update_status(session_id, SessionStatus.COMPLETED)

            # 3.取消任务(无任务时跳过)
            if task:
                task.cancel()

            # 4.注册沙箱延迟销毁（保留TTL时间供续接复用）
            await self._schedule_sandbox_ttl(session_id)

        # 5.清理会话锁(在锁释放后移除,避免影响锁内逻辑)
        self._remove_session_lock(session_id)

    async def shutdown(self) -> None:
        """关闭Agent服务"""
        logger.info("正在清除所有会话任务资源并释放")

        # 1.取消所有沙箱TTL延迟销毁任务
        for session_id, ttl_task in list(self._sandbox_ttl_tasks.items()):
            if not ttl_task.done():
                ttl_task.cancel()
        self._sandbox_ttl_tasks.clear()

        # 2.销毁所有任务（含沙箱）
        await self._task_cls.destroy()
        logger.info("所有会话任务资源清除成功")
