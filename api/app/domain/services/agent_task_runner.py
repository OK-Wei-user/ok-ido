#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/22 0:52

@File    : agent_task_runner.py
"""
import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, AsyncGenerator, Callable, BinaryIO, Optional, TYPE_CHECKING

from fastapi import UploadFile
from pydantic import TypeAdapter

from app.domain.external.browser import Browser
from app.domain.external.file_storage import FileStorage
from app.domain.external.json_parser import JSONParser
from app.domain.external.llm import LLM
from app.domain.external.sandbox import Sandbox
from app.domain.external.search import ContentFetcher, SearchEngine
from app.domain.external.task import TaskRunner, Task
from app.domain.external.task_callback import TaskCallbackManager
from app.domain.models.app_config import AgentConfig, MCPConfig, A2AConfig, DeepResearchConfig, ToolExecutionConfig
from app.domain.models.event import ErrorEvent, Event, MessageEvent, BaseEvent, ToolEvent, ToolEventStatus, \
    BrowserToolContent, SearchToolContent, ShellToolContent, FileToolContent, MCPToolContent, A2AToolContent, \
    SkillToolContent, DeepResearchToolContent, TitleEvent, WaitEvent, DoneEvent, StepEvent, StepEventStatus, \
    SandboxScanEvent
from app.domain.models.file import File
from app.domain.models.message import Message
from app.domain.models.plan import Step
from app.domain.models.research import ResearchSummary
from app.domain.models.search import SearchResults
from app.domain.models.session import SessionStatus
from app.domain.models.tool_result import ToolResult
from app.domain.repositories.uow import IUnitOfWork
from app.domain.services.flows.planner_react import PlannerReActFlow
from app.domain.services.observability import MetricsCollector
from app.domain.services.skill_service import SkillService
from app.domain.services.tools.a2a import A2ATool
from app.domain.services.tools.mcp import MCPTool
from app.domain.services.tools.skill import SkillTool
from app.application.services.file_presentation_service import FilePresentationService
from app.infrastructure.external.llm.token_counter import TokenCounter

if TYPE_CHECKING:
    from app.infrastructure.storage.search_cache import SearchCache
    from app.infrastructure.storage.tool_cache import ToolResultCache
    from app.infrastructure.storage.idempotent_tool_registry import IdempotentToolRegistry
    from app.infrastructure.storage.session_prompt_cache import SessionPromptCache

from app.infrastructure.storage.vnc_status_tracker import VNCStatusTracker

logger = logging.getLogger(__name__)

# 截图策略 — 视觉变化优先: 所有改变页面可见内容的操作必须截图
# 会话be7718f5暴露: scroll_down/scroll_to_top等操作因节流被跳过截图,
# 用户看到"浏览器操作已完成"但无画面,无法确认操作结果。
# 优化方向: 按视觉影响分类,视觉变化操作无条件截图,非视觉操作跳过。
_SCREENSHOT_THROTTLE_SECONDS = 1.0  # 未知操作的兜底节流间隔(秒)
# 混合方案: VNC连接时截图降级节流间隔(秒)
# VNC实时画面已覆盖操作过程,截图仅用于历史回放,延长间隔减少OSS上传开销
_SCREENSHOT_VNC_THROTTLE_SECONDS = 3.0
# 关键视觉操作: VNC连接时也必须截图(不受节流控制)
# 这些操作的结果对用户理解任务进展至关重要,且调用频率低,不会显著增加OSS开销
# 会话34af4e8d暴露: browser_click 4次调用仅2次截图(50%缺失),VNC节流导致关键操作结果丢失
_SCREENSHOT_CRITICAL_OPS = frozenset({
    "browser_navigate", "browser_restart",   # 页面切换(用户需确认导航结果)
    "browser_click", "browser_input",        # 交互操作(用户需确认操作效果)
    "browser_select_option",                 # 下拉选择(用户需确认选中结果)
})
# 视觉变化操作: 必截图(页面可见内容发生改变,用户需确认操作结果)
_SCREENSHOT_REQUIRED_OPS = frozenset({
    "browser_navigate", "browser_restart", "browser_view",  # 页面切换/查看
    "browser_click", "browser_input", "browser_press_key",   # 交互操作
    "browser_select_option", "browser_respond_dialog",       # 选择/对话框
    "browser_scroll_down", "browser_scroll_up",              # 滚动(改变视口)
    "browser_scroll_to_top", "browser_scroll_to_text",       # 滚动定位
    "browser_console_exec", "browser_wait_for",              # JS执行/等待元素出现
    "browser_move_mouse",                                    # 鼠标移动(可能触发hover效果)
})
# 非视觉操作: 不截图(纯文本数据,无页面可见内容变化)
_SCREENSHOT_SKIP_OPS = frozenset({
    "browser_console_view",      # 控制台文本输出
    "browser_wait",              # 定时等待(无视觉变化)
    "browser_network_requests",  # 网络请求数据(纯文本)
})

# 技能脚本增量同步配置
_SKILL_MANIFEST_PATH = "/home/ubuntu/.skill_manifest.json"  # 沙箱中manifest存储路径
_SKILL_SYNC_CONCURRENCY = 5  # 并行上传文件并发数

# 文件系统一致性保障配置
_FILE_SIZE_BLOCK_THRESHOLD = 500 * 1024 * 1024  # 500MB 阻塞阈值(跳过同步)
_FILE_CONTENT_SSE_MAX = 8 * 1024  # 8KB SSE回传上限
_ATTACHMENT_SYNC_CONCURRENCY = 3  # 附件并发同步度(避免沙箱httpx连接池耗尽)
_FILE_SYNC_MAX_RETRIES = 2  # 文件同步OSS最大重试次数

# Shell 输出流式轮询配置(改进B)
# 工具执行期间周期性读取已产生的 console_records 增量推送,平衡实时性与沙箱压力
_SHELL_POLL_INTERVAL_SECONDS = 1.0  # 轮询间隔(秒)

# 批次 38: 步骤结果文件路径自动提取正则(与 _plan_update_policy._FILE_PATH_PATTERN 对齐)
# 匹配 /home/ubuntu/xxx.csv 或 /tmp/xxx.txt 等绝对路径,用于 step.attachments 为空时兜底提取
# 批次45 P0-1: \w → [\w\u4e00-\u9fff] 支持中日韩文字文件名(如"2026年1-5月出入库深度经营分析报告.docx")
# 根因: 原 \w 不匹配中文,导致中文文件名无法被正则提取 → 文件同步断裂 → session.files=0
_STEP_RESULT_FILE_PATH_PATTERN = re.compile(r'/[\w\u4e00-\u9fff./\-]+\.\w{1,10}')

# 批次 38: 交付物文件扩展名白名单(仅同步最终交付物,过滤中间产物/脚本/临时文件)
_DELIVERABLE_EXTENSIONS = frozenset({
    ".xlsx", ".xls", ".csv",  # 数据表
    ".docx", ".doc", ".pdf",  # 文档
    ".pptx",  # 演示
    ".md", ".txt",  # 文本(仅 /home/ubuntu/ 根目录下的)
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg",  # 图表
    ".json", ".xml", ".yaml", ".html",  # 结构化数据
})

# 批次 38: 中间产物路径前缀(不同步到交付物列表)
_INTERMEDIATE_PATH_PREFIXES = ("/tmp/", "/home/ubuntu/workspace/", "/home/ubuntu/.skill")


class AgentTaskRunner(TaskRunner):
    """基于Agent智能体的任务运行器"""

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],  # uow模块
            llm: LLM,  # 大语言模型
            agent_config: AgentConfig,  # 智能体配置
            mcp_config: MCPConfig,  # mcp配置
            a2a_config: A2AConfig,  # a2a配置
            session_id: str,  # 会话id
            file_storage: FileStorage,  # 文件存储桶
            json_parser: JSONParser,  # json解析器
            browser: Browser,  # 浏览器
            search_engine: SearchEngine,  # 搜索引擎
            content_fetcher: ContentFetcher,  # 网页正文抓取器
            sandbox: Sandbox,  # 沙箱
            skill_service: SkillService,  # 技能服务
            search_cache: Optional["SearchCache"] = None,  # 搜索结果缓存(可选)
            deep_research_config: Optional[DeepResearchConfig] = None,  # 深度研究配置(可选)
            token_counter: Optional[TokenCounter] = None,  # token计数器(可选)
            context_window: int = 64000,  # 上下文窗口大小(token)
            planner_llm: Optional[LLM] = None,  # PlanAgent轻量化: 规划Agent专用LLM(可选)
            tool_cache: Optional["ToolResultCache"] = None,  # 工具结果缓存(可选)
            tool_execution_config: Optional[ToolExecutionConfig] = None,  # 工具并行执行配置(可选)
            idempotent_registry: Optional["IdempotentToolRegistry"] = None,  # 幂等工具调用去重注册表(P10-1,可选)
            metrics_collector: Optional[MetricsCollector] = None,  # 可观测性指标收集器(F10-9,可选),None时内部创建
            callback_manager: Optional[TaskCallbackManager] = None,  # F10-7异步任务回调管理器(可选)
            file_presentation: Optional[FilePresentationService] = None,  # F10-8文件展示策略服务(可选,默认FilePresentationService())
            prompt_cache: Optional["SessionPromptCache"] = None,  # 会话级提示词缓存(可选,L1内存+L2 Redis)
    ) -> None:
        """构造函数，完成Agent任务运行器的创建"""
        self._uow_factory = uow_factory
        self._session_id = session_id
        self._sandbox = sandbox
        self._agent_config = agent_config  # 保存引用供 _run_flow/_handle_tool_event 读取流式开关
        self._mcp_config = mcp_config
        # 批次 26: 传递 session_id 启用 MCP_POLL_STATS 统计收集
        # MCP工具直接加载,无需prompt_cache(原桥接工具search/describe缓存已移除)
        self._mcp_tool = MCPTool(
            sandbox=sandbox,
            callback_manager=callback_manager,
            session_id=session_id,
        )
        self._a2a_config = a2a_config
        # 传递 prompt_cache 启用 A2A agent cards Redis 持久化(避免每次初始化网络请求)
        self._a2a_tool = A2ATool(prompt_cache=prompt_cache)
        self._skill_tool = SkillTool(skill_service)
        self._file_storage = file_storage
        self._browser = browser
        self._shell_console_sent_count: Dict[str, int] = {}
        self._last_browser_screenshot_time: float = 0.0  # 上次浏览器截图时间戳(用于节流)
        # F10-9可观测性: 默认创建独立实例(确保即使未显式注入也有指标收集)
        # 传入时复用调用方实例(便于跨组件聚合),None时创建独立实例
        self._metrics = metrics_collector if metrics_collector is not None else MetricsCollector(session_id=session_id)
        self._flow = PlannerReActFlow(
            uow_factory=uow_factory,
            llm=llm,
            agent_config=agent_config,
            session_id=session_id,
            json_parser=json_parser,
            browser=browser,
            sandbox=sandbox,
            search_engine=search_engine,
            content_fetcher=content_fetcher,
            search_cache=search_cache,
            deep_research_config=deep_research_config,
            mcp_tool=self._mcp_tool,
            a2a_tool=self._a2a_tool,
            skill_tool=self._skill_tool,
            skill_service=skill_service,
            token_counter=token_counter,
            context_window=context_window,
            planner_llm=planner_llm,  # PlanAgent轻量化: 透传规划Agent专用LLM
            tool_cache=tool_cache,  # 工具结果缓存: 透传
            tool_execution_config=tool_execution_config,  # 工具并行执行: 透传配置
            idempotent_registry=idempotent_registry,  # 幂等工具调用去重注册表(P10-1): 透传
            metrics_collector=self._metrics,  # 可观测性指标收集器(F10-9): 透传给Flow与Agents
            callback_manager=callback_manager,  # F10-7异步任务回调管理器: 透传
            file_presentation=file_presentation,  # F10-8文件展示策略服务: 透传(集中化交付物过滤+校验)
            prompt_cache=prompt_cache,  # 会话级提示词缓存: 透传(延迟注入A2A能力摘要)
        )

    async def _put_and_add_event(self, task: Task, event: Event) -> None:
        """往指定任务的消息队列中添加事件"""
        # 1.往任务的输出消息队列中新增事件
        event_id = await task.output_stream.put(event.model_dump_json())
        event.id = event_id

        # 2.流式delta事件仅推送SSE，不持久化DB（历史回放只看完整事件）
        # 改进A: 思考增量(MessageEvent.is_streaming) + 改进B: shell轮询增量(ToolEvent.is_streaming)
        if isinstance(event, (MessageEvent, ToolEvent)) and getattr(event, "is_streaming", False):
            return

        # 3.将事件添加到对应的会话中
        # UoW并发安全: 每次创建独立UoW实例,避免asyncio.gather并发时db_session相互覆盖
        async with self._uow_factory() as uow:
            await uow.session.add_event(self._session_id, event)

    @classmethod
    async def _pop_event(cls, task: Task) -> Event:
        """从任务的输入流中获取事件信息"""
        # 1.从任务task中读取数据
        event_id, event_str = await task.input_stream.pop()
        if event_str is None:
            logger.warning("AgentTaskRunner接收到空消息")
            return

        # 2.使用pydantic+type类型将字符串转换成事件
        event = TypeAdapter(Event).validate_json(event_str)
        event.id = event_id

        return event

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        """清理文件名中的特殊字符，防止沙箱环境读取失败"""
        sanitized = filename
        for char, replacement in [("\u201c", ""), ("\u201d", ""), ("\u2018", ""), ("\u2019", "")]:
            sanitized = sanitized.replace(char, replacement)
        sanitized = re.sub(r'[<>:"|?*]', '_', sanitized)
        if sanitized != filename:
            logger.info(f"文件名已清理: {filename} -> {sanitized}")
        return sanitized

    async def _sync_skill_scripts_to_sandbox(self) -> None:
        """增量同步技能脚本到沙箱环境

        基于manifest(文件路径→md5映射)的增量同步策略:
        1. 计算本地技能脚本的manifest
        2. 读取沙箱中已有的manifest
        3. 仅上传有变更(新增/修改)的文件
        4. 更新沙箱manifest
        """
        try:
            # 1.获取本地技能脚本manifest
            local_manifest = await self._build_local_skill_manifest()
            if not local_manifest:
                logger.debug("无技能脚本需要同步")
                return

            # 2.读取沙箱中已有的manifest
            remote_manifest = await self._read_remote_skill_manifest()

            # 3.计算差异: 仅上传新增或md5不一致的文件
            changed_files = [
                rel_path for rel_path, md5 in local_manifest.items()
                if rel_path not in remote_manifest or remote_manifest[rel_path] != md5
            ]

            if not changed_files:
                logger.info(f"技能脚本无需同步(全部{len(local_manifest)}个文件已最新)")
                return

            # 4.并行上传变更文件(限制并发数防止沙箱过载)
            semaphore = asyncio.Semaphore(_SKILL_SYNC_CONCURRENCY)
            upload_tasks = [
                self._upload_skill_file(rel_path, semaphore)
                for rel_path in changed_files
            ]
            results = await asyncio.gather(*upload_tasks, return_exceptions=True)
            success_count = sum(1 for r in results if not isinstance(r, Exception))

            logger.info(f"技能脚本增量同步完成: {success_count}/{len(changed_files)}个文件已上传")

            # 5.更新沙箱manifest
            await self._write_remote_skill_manifest(local_manifest)

        except Exception as e:
            logger.warning(f"技能脚本增量同步失败(不影响主流程): {e}")

    async def _build_local_skill_manifest(self) -> Dict[str, str]:
        """构建本地技能脚本manifest: {相对路径: md5}"""
        manifest: Dict[str, str] = {}
        skills = await self._skill_tool._skill_service.list_skills()

        for skill in skills:
            scripts_dir = os.path.join(skill.path, "scripts")
            if not os.path.isdir(scripts_dir):
                continue

            for root, dirs, files in os.walk(scripts_dir):
                for filename in files:
                    local_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(local_path, skill.path).replace("\\", "/")
                    try:
                        with open(local_path, "rb") as f:
                            md5 = hashlib.md5(f.read()).hexdigest()
                        manifest[rel_path] = md5
                    except Exception as e:
                        logger.warning(f"计算技能脚本md5失败: {rel_path}, {e}")

        return manifest

    async def _read_remote_skill_manifest(self) -> Dict[str, str]:
        """读取沙箱中已有的技能脚本manifest,读取失败时返回空字典(降级为全量同步)"""
        try:
            result = await self._sandbox.read_file(_SKILL_MANIFEST_PATH)
            if result.success and result.data:
                content = (result.data or {}).get("content", "")
                if content:
                    return json.loads(content)
        except Exception as e:
            # F4-2: manifest读取失败降级为全量同步(不阻断技能同步)
            logger.debug(f"读取技能manifest失败,降级全量同步: {e}")
        return {}

    async def _write_remote_skill_manifest(self, manifest: Dict[str, str]) -> None:
        """写入技能脚本manifest到沙箱"""
        try:
            await self._sandbox.write_file(
                filepath=_SKILL_MANIFEST_PATH,
                content=json.dumps(manifest, ensure_ascii=False),
            )
        except Exception as e:
            logger.warning(f"写入技能manifest失败: {e}")

    async def _upload_skill_file(self, rel_path: str, semaphore: asyncio.Semaphore) -> None:
        """上传单个技能脚本到沙箱"""
        async with semaphore:
            skills = await self._skill_tool._skill_service.list_skills()
            for skill in skills:
                local_path = os.path.join(skill.path, rel_path.replace("/", os.sep))
                if os.path.isfile(local_path):
                    sandbox_path = f"/home/ubuntu/{rel_path}"
                    try:
                        with open(local_path, "rb") as f:
                            file_data = io.BytesIO(f.read())
                        result = await self._sandbox.upload_file(
                            file_data=file_data,
                            filepath=sandbox_path,
                            filename=os.path.basename(local_path),
                        )
                        if not result.success:
                            logger.warning(f"技能脚本上传失败: {sandbox_path}, {result.message}")
                    except Exception as e:
                        logger.warning(f"技能脚本上传异常: {sandbox_path}, {e}")
                    return

    async def _sync_file_to_sandbox(self, file_id: str) -> Optional[File]:
        """根据文件id将文件从OSS下载并同步到沙箱

        Returns:
            File对象(filepath已更新为沙箱路径)，失败时返回None
        """
        try:
            file_data, file = await self._file_storage.download_file(file_id)
            filepath = f"/home/ubuntu/upload/{self._sanitize_filename(file.filename)}"
            tool_result = await self._sandbox.upload_file(file_data=file_data, filepath=filepath, filename=file.filename)
            if tool_result.success:
                file.filepath = filepath
                async with self._uow_factory() as uow:
                    await uow.file.save(file)
                return file
            logger.warning(f"文件上传沙箱失败: file_id={file_id}, filename={file.filename}")
        except Exception as e:
            logger.exception(f"AgentTaskRunner同步文件[{file_id}]失败: {str(e)}")
        return None

    async def _sync_message_attachments_to_sandbox(self, event: MessageEvent) -> None:
        """并发将用户上传的附件从OSS同步到沙箱

        使用Semaphore控制并发度,避免沙箱httpx连接池耗尽(默认连接池上限10)。
        单个附件失败不影响其他,失败附件保留原始信息供调用方回退。
        """
        if not event.attachments:
            return

        semaphore = asyncio.Semaphore(_ATTACHMENT_SYNC_CONCURRENCY)

        async def _sync_one(attachment: File) -> Optional[File]:
            async with semaphore:
                return await self._sync_file_to_sandbox(attachment.id)

        try:
            results = await asyncio.gather(
                *[_sync_one(att) for att in event.attachments],
                return_exceptions=True,
            )
            synced: List[File] = []
            for i, result in enumerate(results):
                original = event.attachments[i]
                if isinstance(result, Exception):
                    logger.warning(f"附件同步到沙箱失败, 保留原始附件信息: id={original.id}, error={str(result)}")
                    synced.append(original)
                elif result:
                    synced.append(result)
                    async with self._uow_factory() as uow:
                        await uow.session.add_file(self._session_id, result)
                else:
                    logger.warning(f"附件同步到沙箱失败, 保留原始附件信息: id={original.id}, filename={original.filename}")
                    synced.append(original)
            event.attachments = synced
        except Exception as e:
            logger.exception(f"AgentTaskRunner并发同步消息附件到沙箱失败: {str(e)}")

    @classmethod
    def _get_stream_size(cls, f: BinaryIO) -> int:
        """根据传递的文件流，获取计算文件的大小"""
        # 1.记录当前文件指针位置
        current_pos = f.tell()

        # 2.将指针移动到文件末尾, seek，0: 偏移量、2: 相对文件末尾
        f.seek(0, 2)

        # 3.获取当前位置，也就是文件大小
        size = f.tell()

        # 4.恢复指针到原始位置
        f.seek(current_pos)

        return size

    @staticmethod
    def _extract_deliverable_paths(step: Step) -> List[str]:
        """从步骤产出中提取交付物文件路径(批次 38 attachments 兜底提取)

        提取策略:
        1. step.attachments 非空时直接返回(LLM 已显式声明)
        2. step.attachments 为空时,从 step.result 文本中正则提取文件路径
        3. 过滤中间产物(/tmp/ /workspace/ .skill 等),仅保留交付物扩展名

        Args:
            step: 已完成的步骤对象

        Returns:
            去重后的交付物文件路径列表(可能为空)
        """
        # 1.LLM 已显式声明 attachments → 直接返回
        if step.attachments:
            return [fp for fp in step.attachments if fp]

        # 2.step.result 为空 → 无法提取
        if not step.result:
            return []

        # 3.从 result 文本中正则提取文件路径
        raw_paths = _STEP_RESULT_FILE_PATH_PATTERN.findall(step.result)
        if not raw_paths:
            return []

        # 4.过滤中间产物 + 扩展名白名单校验
        deliverable_paths: List[str] = []
        seen: set = set()
        for path in raw_paths:
            # 去重
            if path in seen:
                continue
            # 跳过中间产物路径
            if any(path.startswith(prefix) for prefix in _INTERMEDIATE_PATH_PREFIXES):
                continue
            # 扩展名白名单校验(仅保留交付物类型)
            ext = os.path.splitext(path)[1].lower()
            if ext not in _DELIVERABLE_EXTENSIONS:
                continue
            # .txt/.md/.json 仅同步 /home/ubuntu/ 根目录下的(避免误同步日志/配置文件)
            if ext in (".txt", ".md", ".json") and not path.startswith("/home/ubuntu/"):
                continue
            seen.add(path)
            deliverable_paths.append(path)

        return deliverable_paths

    async def _sync_file_to_storage(self, filepath: str, max_retries: int = _FILE_SYNC_MAX_RETRIES) -> Optional[File]:
        """将沙箱中指定的文件路径数据同步到存储桶中(写入顺序+大小预检查+重试)

        写入顺序: 大小预检查 → 下载沙箱文件 → 上传OSS(持久层优先) → 更新DB
        失败处理: OSS上传失败时保留沙箱文件,标记sync_status=PENDING,不删除沙箱文件

        Args:
            filepath: 沙箱中的文件路径
            max_retries: 同步失败后的最大重试次数

        Returns:
            同步成功的File对象,失败时返回None
        """
        # 1.大小预检查(避免大文件阻塞沙箱httpx连接)
        file_size = 0
        try:
            file_size = await self._sandbox.get_file_size(filepath)
            if file_size > _FILE_SIZE_BLOCK_THRESHOLD:
                logger.warning(f"文件过大跳过同步: filepath={filepath}, size={file_size}")
                async with self._uow_factory() as uow:
                    existing = await uow.session.get_file_by_path(self._session_id, filepath)
                    if not existing:
                        pending_file = File(
                            filepath=filepath,
                            filename=filepath.split("/")[-1],
                            size=file_size,
                            sync_status="PENDING",
                        )
                        await uow.session.add_file(self._session_id, pending_file)
                return None
            if file_size == 0:
                logger.warning(f"文件大小为0，跳过同步: filepath={filepath}")
                return None
        except Exception as e:
            logger.debug(f"文件大小预检查失败(降级无预检查): filepath={filepath}, error={str(e)}")

        # 2.重试循环: 下载沙箱→上传OSS→更新DB
        for attempt in range(max_retries + 1):
            try:
                # 下载沙箱文件
                file_data = await self._sandbox.download_file(filepath)

                # 上传OSS(持久层优先,成功后才更新业务层)
                filename = filepath.split("/")[-1]
                upload_file = UploadFile(
                    file=file_data,
                    filename=filename,
                    size=self._get_stream_size(file_data),
                )
                file = await self._file_storage.upload_file(upload_file)
                file.filepath = filepath
                file.sync_status = "SYNCED"

                # OSS成功后更新DB: 移除同路径旧文件 + 添加新文件(同一UoW,原子操作)
                async with self._uow_factory() as uow:
                    await uow.session.remove_files_by_path(self._session_id, filepath)
                    await uow.session.add_file(self._session_id, file)
                return file
            except Exception as e:
                if attempt < max_retries:
                    wait = 0.5 * (2 ** attempt)  # 指数退避: 0.5s, 1s, 2s
                    logger.warning(
                        f"文件同步OSS失败(第{attempt + 1}次),{wait}s后重试: "
                        f"filepath={filepath}, error={str(e)}"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.exception(
                        f"文件同步OSS失败(已重试{max_retries}次),标记PENDING: "
                        f"filepath={filepath}, error={str(e)}"
                    )
                    # 标记为PENDING,供后续补偿恢复
                    try:
                        async with self._uow_factory() as uow:
                            existing = await uow.session.get_file_by_path(self._session_id, filepath)
                            if not existing:
                                pending_file = File(
                                    filepath=filepath,
                                    filename=filepath.split("/")[-1],
                                    size=file_size,
                                    sync_status="PENDING",
                                )
                                await uow.session.add_file(self._session_id, pending_file)
                    except Exception as mark_e:
                        logger.error(f"标记PENDING失败: filepath={filepath}, error={str(mark_e)}")
        return None

    async def _sync_message_attachments_to_storage(self, event: MessageEvent) -> None:
        """并发将消息附件从沙箱同步到OSS,过滤同步失败的占位文件

        使用Semaphore控制并发度,避免沙箱httpx连接池耗尽。
        同步失败的附件(0B文件/沙箱中不存在)被过滤丢弃,而非保留占位File,
        防止前端渲染出"· 0 B"的空文件条目(根因:占位File默认size=0,filename="")。
        """
        if not event.attachments:
            return

        semaphore = asyncio.Semaphore(_ATTACHMENT_SYNC_CONCURRENCY)

        async def _sync_one(attachment: File) -> Optional[File]:
            async with semaphore:
                return await self._sync_file_to_storage(attachment.filepath)

        try:
            results = await asyncio.gather(
                *[_sync_one(att) for att in event.attachments],
                return_exceptions=True,
            )
            synced: List[File] = []
            failed_paths: List[str] = []
            for i, result in enumerate(results):
                original = event.attachments[i]
                if isinstance(result, Exception):
                    failed_paths.append(original.filepath)
                elif result:
                    synced.append(result)
                else:
                    # 同步失败(0B文件/沙箱中不存在): 丢弃占位File,不展示给用户
                    failed_paths.append(original.filepath)
            if failed_paths:
                logger.warning(f"以下附件同步到存储桶失败,已从交付列表移除: {failed_paths}")
            event.attachments = synced
        except Exception as e:
            logger.exception(f"AgentTaskRunner并发同步消息附件到存储桶失败: {str(e)}")

    async def _get_browser_screenshot(self) -> str:
        """获取浏览器截图并返回截图文件对应的在线URL"""
        # 1.调用浏览器完成截图
        screenshot = await self._browser.screenshot()

        # 2.将浏览器截图上传到文件存储中
        file = await self._file_storage.upload_file(UploadFile(
            file=io.BytesIO(screenshot),
            filename=f"{str(uuid.uuid4())}.png",
            # bugfix:添加size尺寸
            size=self._get_stream_size(io.BytesIO(screenshot)),
        ))

        # 3.获取setting并组装完整URL
        return file.key

    async def _upload_image_bytes_to_storage(self, data: bytes, ext: str = "png") -> str:
        """上传图片字节数据到文件存储,返回在线URL

        供MCP多模态工具结果图片上传复用,与浏览器截图共用同一存储通道。
        单张上传失败时抛出异常,由调用方降级处理(不影响主流程)。
        """
        file = await self._file_storage.upload_file(UploadFile(
            file=io.BytesIO(data),
            filename=f"{str(uuid.uuid4())}.{ext}",
            size=self._get_stream_size(io.BytesIO(data)),
        ))
        return file.key

    async def _extract_mcp_images(self, data: Any) -> tuple[Any, List[str]]:
        """从MCP工具结果中剥离图片base64并上传OSS,返回(剥离图片后的结果, 图片URL列表)

        MCP多模态工具返回结构: {"text": "...", "images": [{"data": base64, "mime_type": str}]}
        - 剥离images字段(避免base64膨胀SSE载荷),result仅保留text等文本结构
        - 图片base64解码后上传OSS,返回在线URL列表供前端展示
        - 单张上传失败降级跳过(不影响其他图片与文本结果)

        Args:
            data: MCP工具结果(function_result.data)

        Returns:
            (剥离images后的result_data, 图片URL列表);无图片时返回(原data, [])
        """
        if not isinstance(data, dict) or not isinstance(data.get("images"), list):
            return data, []
        images_raw = data["images"]
        if not images_raw:
            return data, []
        # 剥离images后的result副本(保留text等文本字段)
        result_copy = {k: v for k, v in data.items() if k != "images"}
        image_urls: List[str] = []
        for img in images_raw:
            if not isinstance(img, dict):
                continue
            b64_data = img.get("data")
            if not isinstance(b64_data, str) or not b64_data:
                continue
            mime = img.get("mime_type") or "image/png"
            ext = mime.split("/")[-1].split(";")[0] or "png"
            try:
                img_bytes = base64.b64decode(b64_data)
                url = await self._upload_image_bytes_to_storage(img_bytes, ext)
                image_urls.append(url)
            except Exception as e:
                logger.warning(f"MCP图片上传OSS失败,跳过该图片: {e}")
        return result_copy, image_urls

    def _should_take_screenshot(self, function_name: str) -> bool:
        """判断浏览器工具调用后是否需要截图

        视觉变化优先策略(会话be7718f5优化 + 混合方案VNC降级 + 关键操作豁免):
        1. 关键视觉操作(navigate/click/input/select): VNC连接时也必须截图
           这些操作结果对用户理解任务进展至关重要,且调用频率低,不受节流控制
        2. 视觉变化操作(view/scroll/console_exec等): 必截图,用户需确认操作结果
           - VNC连接时: 降级为节流模式(3秒间隔),实时画面已覆盖,截图仅用于历史回放
        3. 非视觉操作(console_view/wait/network_requests): 不截图,前端展示文本摘要
        4. 未知操作: 兜底节流(1秒间隔),平衡截图覆盖与上传开销
           - VNC连接时: 跳过(实时画面已覆盖,无需额外截图)
        """
        # 不截图操作: 无视觉变化(无论VNC是否连接)
        if function_name in _SCREENSHOT_SKIP_OPS:
            return False

        # 关键视觉操作: VNC连接时也必须截图(不受节流控制)
        # 会话34af4e8d: browser_click 50%截图缺失,关键操作结果丢失影响用户理解
        if function_name in _SCREENSHOT_CRITICAL_OPS:
            self._last_browser_screenshot_time = time.monotonic()
            return True

        # VNC连接状态检查(混合方案: VNC实时画面已覆盖时降低截图频率)
        vnc_connected = VNCStatusTracker.is_connected(self._session_id)

        # 必截图操作: 页面发生重大变化
        if function_name in _SCREENSHOT_REQUIRED_OPS:
            if vnc_connected:
                # VNC降级: 节流模式(3秒间隔),截图仅用于历史回放
                now = time.monotonic()
                if now - self._last_browser_screenshot_time >= _SCREENSHOT_VNC_THROTTLE_SECONDS:
                    self._last_browser_screenshot_time = now
                    return True
                return False
            # 完整模式: 无条件截图
            self._last_browser_screenshot_time = time.monotonic()
            return True

        # 未知操作: 兜底节流
        if vnc_connected:
            # VNC降级: 跳过未知操作截图(实时画面已覆盖)
            return False

        now = time.monotonic()
        if now - self._last_browser_screenshot_time >= _SCREENSHOT_THROTTLE_SECONDS:
            self._last_browser_screenshot_time = now
            return True

        return False

    @staticmethod
    def _extract_browser_op_summary(event: ToolEvent) -> str:
        """从浏览器工具事件提取操作结果摘要(无截图时供前端展示)

        优先取 function_result.message(操作结果文本),截断防止SSE载荷膨胀;
        无结果时返回按操作类型生成的默认文案,避免前端显示"等待页面截图"。

        Args:
            event: 工具事件(CALLED 状态)

        Returns:
            供前端展示的操作摘要文本
        """
        # 优先取工具结果的消息文本
        result = getattr(event, "function_result", None)
        if result is not None:
            msg = getattr(result, "message", None)
            if isinstance(msg, str) and msg.strip():
                # 截断过长的操作结果,前端预览仅展示摘要
                return msg[:200] + ("..." if len(msg) > 200 else "")
        # 无结果消息时按操作类型生成默认文案
        op_defaults = {
            "browser_console_view": "控制台输出已获取",
            "browser_wait": "等待完成",
            "browser_console_exec": "脚本执行完成",
            "browser_view": "页面状态已获取(截图捕获失败,请通过远程桌面查看)",
            "browser_navigate": "页面导航完成(截图捕获失败,请通过远程桌面查看)",
            "browser_click": "点击操作完成(截图捕获失败,请通过远程桌面查看)",
            "browser_input": "输入操作完成(截图捕获失败,请通过远程桌面查看)",
        }
        return op_defaults.get(event.function_name, "浏览器操作已完成(截图捕获失败,请通过远程桌面查看)")

    async def _read_file_content_with_protection(self, filepath: str) -> str:
        """读取沙箱文件内容并应用SSE载荷保护

        分级策略:
        - 超大文件(>500MB): 仅回传元信息,不读取content
        - 中等文件(>8KB): 截断回传前N字符
        - 小文件/预检查失败: 完整回传(降级到原流程)
        """
        try:
            file_size = await self._sandbox.get_file_size(filepath)
        except Exception as e:
            logger.debug(f"获取文件大小失败,降级完整读取: filepath={filepath}, error={str(e)}")
            file_size = -1

        # 超大文件: 仅回传元信息,避免下载阻塞SSE
        if file_size > _FILE_SIZE_BLOCK_THRESHOLD:
            logger.warning(f"文件过大,SSE仅回传元信息: filepath={filepath}, size={file_size}")
            return f"(文件过大: {file_size}字节,请使用shell工具分块读取)"

        # 读取文件内容
        try:
            file_read_result = await self._sandbox.read_file(filepath)
        except Exception as e:
            logger.warning(f"读取沙箱文件失败: filepath={filepath}, error={str(e)}")
            return "(读取文件失败)"

        content: str = (file_read_result.data or {}).get("content", "") if file_read_result else ""

        # 中等文件: 截断保护
        if len(content) > _FILE_CONTENT_SSE_MAX:
            logger.info(f"文件内容超过SSE上限,已截断: filepath={filepath}, total={len(content)}")
            return content[:_FILE_CONTENT_SSE_MAX] + f"\n...(truncated, total {len(content)} chars)"

        return content

    async def _handle_sandbox_scan_event(self, event: SandboxScanEvent) -> None:
        """处理沙箱扫描事件: 并发同步扫描发现的交付物到OSS+DB(批次45 P0-1)

        Flow在SUMMARIZING阶段session.files为空时主动扫描沙箱,通过SandboxScanEvent
        回传路径。本方法并发调用_sync_file_to_storage将文件同步到OSS+DB,
        使session.files在重新查询时能返回已同步的交付物。

        异常容错: 单个文件同步失败不影响其他文件(gather return_exceptions=True)。
        """
        if not event.file_paths:
            return
        logger.info(
            f"批次45 P0-1: 并发同步{len(event.file_paths)}个沙箱扫描交付物: "
            f"{event.file_paths}"
        )
        semaphore = asyncio.Semaphore(_ATTACHMENT_SYNC_CONCURRENCY)

        async def _sync_one(fp: str):
            async with semaphore:
                return await self._sync_file_to_storage(fp)

        sync_results = await asyncio.gather(
            *[_sync_one(fp) for fp in event.file_paths if fp],
            return_exceptions=True,
        )
        success = sum(
            1 for r in sync_results
            if r is not None and not isinstance(r, Exception)
        )
        logger.info(
            f"批次45 P0-1: 沙箱扫描交付物同步完成 {success}/{len(event.file_paths)}"
        )

    async def _handle_tool_event(self, event: ToolEvent) -> None:
        """额外处理工具消息，使其前端交互更友好"""
        try:
            # 0.CALLING 状态: 浏览器工具设置"执行中"标记,避免前端显示"等待页面截图"
            #    截图在 CALLED 时补充(操作完成页面稳定),CALLING 期间不截图避免与正在执行的操作竞争锁
            if (event.status == ToolEventStatus.CALLING
                    and event.tool_name == "browser"
                    and event.tool_content is None):
                event.tool_content = BrowserToolContent(
                    screenshot=None, message="正在执行浏览器操作...",
                )
            # 1.如果事件状态为已调用则执行以下代码
            if event.status == ToolEventStatus.CALLED:
                # 2.工具为浏览器则补全工具浏览器工具内容(截图节流)
                if event.tool_name == "browser":
                    if self._should_take_screenshot(event.function_name):
                        # 截图捕获独立容错: click/console_exec等操作可能触发页面过渡态,
                        # 截图失败时降级展示操作摘要,避免前端"浏览器操作已完成"无画面
                        # 会话a890692f: 最后一次browser_view截图失败显示"浏览器操作已完成"无画面,
                        # 添加1次重试覆盖页面过渡态导致的瞬时失败
                        screenshot_url = None
                        for attempt in range(2):
                            try:
                                screenshot_url = await self._get_browser_screenshot()
                                break
                            except Exception as screenshot_err:
                                if attempt == 0:
                                    logger.debug(f"浏览器截图首次失败,重试中: {screenshot_err}")
                                    await asyncio.sleep(0.5)
                                else:
                                    logger.warning(f"浏览器截图重试仍失败,降级展示操作摘要: {screenshot_err}")
                        if screenshot_url:
                            event.tool_content = BrowserToolContent(screenshot=screenshot_url)
                        else:
                            event.tool_content = BrowserToolContent(
                                screenshot=None,
                                message=self._extract_browser_op_summary(event),
                            )
                    else:
                        # 不截图操作(console_view/wait等): 传递操作结果摘要,
                        # 避免前端 screenshot=null 时显示"等待页面截图"误导用户
                        event.tool_content = BrowserToolContent(
                            screenshot=None,
                            message=self._extract_browser_op_summary(event),
                        )
                elif event.tool_name == "search":
                    # 3.工具为搜索则添加搜索工具内容，剥离content字段避免前端SSE载荷膨胀
                    search_results: ToolResult[SearchResults] = event.function_result
                    if search_results and getattr(search_results, "data", None):
                        display_results = [
                            item.model_copy(update={"content": None})
                            for item in search_results.data.results
                        ]
                        event.tool_content = SearchToolContent(results=display_results)
                    else:
                        event.tool_content = SearchToolContent(results=[])
                elif event.tool_name == "shell":
                    # 批次42修复: 优先从 function_args 读取 session_id;
                    # LLM 省略 session_id 时(批次41可选化)从 function_result.data 回读,
                    # 避免 _handle_tool_event 走 else 分支设置字符串 console 导致前端
                    # 永远显示"等待命令输出..."
                    shell_session_id = event.function_args.get("session_id")
                    if not shell_session_id and event.function_result is not None:
                        result_data = getattr(event.function_result, "data", None)
                        if isinstance(result_data, dict):
                            shell_session_id = result_data.get("session_id")
                    if shell_session_id:
                        shell_result = await self._sandbox.read_shell_output(
                            shell_session_id,
                            console=True,
                        )
                        all_records = (shell_result.data or {}).get("console_records", [])
                        if self._agent_config.stream_shell_output:
                            # 改进B 流式模式: CALLED 携带完整 console,前端 replace 累积结果
                            # 中间轮询事件(is_streaming)未持久化,replay 只见 CALLED,故 CALLED 必须含全量
                            event.tool_content = ShellToolContent(console=all_records)
                        else:
                            # 非流式模式(向后兼容): 仅推送本轮新增记录
                            sent_count = self._shell_console_sent_count.get(shell_session_id, 0)
                            new_records = all_records[sent_count:]
                            event.tool_content = ShellToolContent(console=new_records)
                        self._shell_console_sent_count[shell_session_id] = len(all_records)
                    else:
                        # 兜底: 回读失败时展示命令执行结果(message),避免前端空白
                        fallback_msg = ""
                        if event.function_result is not None:
                            fallback_msg = getattr(event.function_result, "message", "") or ""
                        event.tool_content = ShellToolContent(console=fallback_msg)
                elif event.tool_name == "file":
                    # 4.工具为file则将文件同步到对象存储(大小预检查+SSE截断保护)
                    if "filepath" in event.function_args:
                        filepath = event.function_args["filepath"]
                        file_content = await self._read_file_content_with_protection(filepath)
                        event.tool_content = FileToolContent(content=file_content)
                        await self._sync_file_to_storage(filepath)
                    else:
                        event.tool_content = FileToolContent(content="(No Content)")
                elif event.tool_name in ["mcp", "a2a"]:
                    # 5.工具为mcp/a2a则处理调用结果
                    #    MCP多模态: result.data含images(base64)时,剥离上传OSS获取URL,
                    #    填充MCPToolContent.images供前端展示,避免base64膨胀SSE载荷
                    logger.info(f"处理MCP/A2A工具事件, function_result: {event.function_result}")
                    if event.function_result:
                        if hasattr(event.function_result, "data") and event.function_result.data:
                            logger.info(f"MCP/A2A工具调用结果: {event.function_result.data}")
                            if event.tool_name == "mcp":
                                # MCP工具: 检测并剥离images base64,上传OSS后填充images URL列表
                                result_data, image_urls = await self._extract_mcp_images(
                                    event.function_result.data
                                )
                                event.tool_content = MCPToolContent(
                                    result=result_data, images=image_urls or None,
                                )
                            else:
                                event.tool_content = A2AToolContent(a2a_result=event.function_result.data)
                        elif hasattr(event.function_result, "success") and event.function_result.success:
                            # 6.mcp/a2a工具调用正常，但无结果产生
                            logger.info(f"MCP/A2A工具调用成功返回，但无结果: {event.function_result}")
                            result_data = event.function_result.model_dump() \
                                if hasattr(event.function_result, "model_dump") \
                                else str(event.function_result)
                            event.tool_content = MCPToolContent(result=result_data) \
                                if event.tool_name == "mcp" \
                                else A2AToolContent(a2a_result=result_data)
                        else:
                            # 7.其他情况将结果转换成字符串进行传递
                            logger.info(f"MCP/A2A工具调用结果: {event.function_result}")
                            event.tool_content = MCPToolContent(result=str(event.function_result)) \
                                if event.tool_name == "mcp" \
                                else A2AToolContent(a2a_result=str(event.function_result))
                    else:
                        logger.warning("MCP/A2A工具调用结果未发现")
                        event.tool_content = MCPToolContent(result="(MCP工具无可用结果)") \
                            if event.tool_name == "mcp" \
                            else A2AToolContent(a2a_result="(A2A智能体无可用结果)")
                elif event.tool_name == "skill":
                    if event.function_result and hasattr(event.function_result, "data"):
                        self._skill_tool.record_skill_usage(
                            self._session_id,
                            event.function_args.get("skill_name", "unknown"),
                            task_context=event.function_args.get("query", ""),
                            success=event.function_result.success,
                        )
                        event.tool_content = SkillToolContent(result=event.function_result.data)
                    else:
                        event.tool_content = SkillToolContent(result="(技能工具无可用结果)")
                elif event.tool_name == "deep_research":
                    # 6.深度研究工具则将研究摘要传递给前端展示
                    if event.function_result and getattr(event.function_result, "data", None):
                        event.tool_content = DeepResearchToolContent(summary=event.function_result.data)
                    else:
                        event.tool_content = DeepResearchToolContent(
                            summary=ResearchSummary(query=event.function_args.get("query", ""))
                        )
        except Exception as e:
            logger.exception(f"AgentTaskRunner生成工具内容失败: {str(e)}")

    async def _run_flow(self, message: Message) -> AsyncGenerator[BaseEvent, None]:
        """根据消息对象运行PlannerReActFlow

        改进B: 采用「生产者任务 → asyncio.Queue → 主循环消费」合并模式,
        以便在 shell 工具执行期间并发插入轮询增量事件(ToolEvent.is_streaming=True)。
        工具执行时 flow 生成器挂起在 _invoke_tool,事件循环空闲可并发跑轮询任务,
        轮询事件经同一队列按到达顺序 yield 给外层,确保 SSE 顺序连贯。
        """
        # 1.判断传递的消息是否为空
        if not message.message:
            logger.warning("AgentTaskRunner接收了一条空消息")
            yield ErrorEvent(error="空消息错误")
            return

        # 2.队列合并: 生产者消费 flow 事件,主循环从队列消费并并发处理 shell 轮询
        queue: asyncio.Queue = asyncio.Queue()
        # tool_call_id -> 后台轮询任务(CALLING 启动, CALLED 取消)
        active_shell_polls: Dict[str, asyncio.Task] = {}

        async def _consume_flow():
            """生产者: 消费 flow 事件并入队,异常/结束以哨兵标记"""
            try:
                async for flow_event in self._flow.invoke(message):
                    await queue.put(flow_event)
            except Exception as flow_err:  # noqa: BLE001 - 异常经队列传播由主循环重新抛出
                await queue.put(flow_err)
            finally:
                await queue.put(None)  # 结束哨兵

        consumer = asyncio.create_task(_consume_flow())
        try:
            while True:
                event = await queue.get()
                # 3.哨兵: flow 消费完毕
                if event is None:
                    break
                # 4.flow 异常: 重新抛出(由上层 invoke 的 except 捕获并降级总结)
                if isinstance(event, Exception):
                    raise event

                # 改进B: shell CALLING 时启动后台轮询(配置开启且携带 session_id)
                if (isinstance(event, ToolEvent)
                        and event.status == ToolEventStatus.CALLING
                        and event.tool_name == "shell"
                        and self._agent_config.stream_shell_output):
                    shell_session_id = event.function_args.get("session_id")
                    if shell_session_id:
                        poll_task = asyncio.create_task(
                            self._poll_shell_console(event.tool_call_id, shell_session_id, queue)
                        )
                        active_shell_polls[event.tool_call_id] = poll_task
                    # CALLING 补充空 console,前端立即渲染命令行骨架(避免空白)
                    event.tool_content = ShellToolContent(console=[])

                # 改进B: shell CALLED 时取消对应轮询任务(命令已结束,无需继续轮询)
                if (isinstance(event, ToolEvent)
                        and event.status == ToolEventStatus.CALLED
                        and event.tool_call_id in active_shell_polls):
                    active_shell_polls.pop(event.tool_call_id).cancel()

                # 5.事件额外处理(保留既有语义)
                if isinstance(event, ToolEvent):
                    await self._handle_tool_event(event)
                elif isinstance(event, StepEvent):
                    # 步骤完成时自动同步 attachments 到存储（支持 shell_execute 生成的二进制文件）
                    if event.status == StepEventStatus.COMPLETED:
                        # 批次 38: attachments 兜底提取 — LLM 未声明 attachments 时从 result 自动提取
                        # 根因: LLM 不在 JSON 响应 attachments 字段声明文件路径 → step.attachments 为空
                        # → _sync_file_to_storage 未触发 → session.files 始终为 0
                        # 修复: 从 step.result 中提取文件路径,过滤中间产物后补填到 step.attachments
                        # 注意: 通过类名调用静态方法(而非 self.),确保 MagicMock 测试时也能正确执行
                        attachments_to_sync = AgentTaskRunner._extract_deliverable_paths(event.step)
                        if attachments_to_sync:
                            # 补填到 step.attachments,供后续 summarize 和交付物校验使用
                            if not event.step.attachments:
                                event.step.attachments = attachments_to_sync
                                logger.info(
                                    f"批次38兜底提取: 从步骤结果中提取{len(attachments_to_sync)}个交付物路径: "
                                    f"{attachments_to_sync}"
                                )
                            for filepath in attachments_to_sync:
                                if not filepath:
                                    continue
                                synced = await self._sync_file_to_storage(filepath)
                                if synced is None:
                                    logger.warning(f"步骤附件同步失败: filepath={filepath}")
                elif isinstance(event, MessageEvent):
                    # 流式delta事件跳过附件同步（无附件且高频，最终答案事件才同步）
                    if not getattr(event, "is_streaming", False):
                        await self._sync_message_attachments_to_storage(event)
                elif isinstance(event, SandboxScanEvent):
                    # 批次45 P0-1: 沙箱扫描发现的文件并发同步到OSS+DB(内部事件,不yield给外层)
                    # Flow在SUMMARIZING阶段扫描沙箱发现未同步交付物时回传此事件
                    await self._handle_sandbox_scan_event(event)
                    # 内部事件不yield给外层(不持久化/不推送前端)
                    continue

                # 6.将事件直接返回
                yield event
        finally:
            # 7.清理: 取消生产者与所有未完成的轮询任务,防止孤儿任务
            consumer.cancel()
            for poll_task in active_shell_polls.values():
                poll_task.cancel()

    async def _poll_shell_console(
            self,
            tool_call_id: str,
            shell_session_id: str,
            queue: asyncio.Queue,
    ) -> None:
        """后台轮询 Shell 输出,增量推送 is_streaming=True 的 ToolEvent(改进B)

        工具执行期间周期性读取已产生的 console_records,仅推送新增记录。
        中间事件不持久化(ToolEvent.is_streaming=True, _put_and_add_event 跳过 DB),
        CALLED 到达时由 _run_flow 取消本任务,_handle_tool_event 读取完整 console。
        复用 _shell_console_sent_count 增量跟踪,与 CALLED 读取共享同一计数器。
        """
        try:
            while True:
                await asyncio.sleep(_SHELL_POLL_INTERVAL_SECONDS)
                shell_result = await self._sandbox.read_shell_output(
                    shell_session_id, console=True,
                )
                all_records = (shell_result.data or {}).get("console_records", [])
                sent_count = self._shell_console_sent_count.get(shell_session_id, 0)
                new_records = all_records[sent_count:]
                self._shell_console_sent_count[shell_session_id] = len(all_records)
                if new_records:
                    await queue.put(ToolEvent(
                        tool_call_id=tool_call_id,
                        tool_name="shell",
                        function_name="shell_execute",
                        function_args={"session_id": shell_session_id},
                        tool_content=ShellToolContent(console=new_records),
                        status=ToolEventStatus.CALLING,
                        is_streaming=True,
                    ))
        except asyncio.CancelledError:
            # CALLED 到达时正常取消,静默退出
            raise
        except Exception as e:
            # 轮询异常不阻断主流程(降级为只在 CALLED 时一次性返回)
            logger.debug(f"shell输出轮询异常(降级忽略): {e}")

    async def _emit_degraded_summary(self, task: Task, error: str) -> None:
        """LLM调用失败时，基于已有工具结果生成降级总结"""
        try:
            async with self._uow_factory() as uow:
                session = await uow.session.get_by_id(self._session_id)
            if not session or not session.events:
                summary = f"任务执行中断: {error}\n\n未获取到有效工具结果。"
            else:
                tool_events = [
                    e for e in session.events
                    if isinstance(e, ToolEvent) and e.status == ToolEventStatus.CALLED
                ]
                if not tool_events:
                    summary = f"任务执行中断: {error}\n\n未获取到有效工具结果。"
                else:
                    summary = f"任务执行中断: {error}\n\n已获取的工具结果:\n"
                    for i, event in enumerate(tool_events, 1):
                        result_text = ""
                        if event.function_result:
                            result_text = event.function_result.model_dump_json()[:200]
                        summary += f"{i}. {event.function_name}({event.function_args}): {result_text}...\n"
            await self._put_and_add_event(task, MessageEvent(
                role="assistant",
                message=summary,
                attachments=[],
                is_final=True,
            ))
        except Exception as e:
            logger.warning(f"生成降级总结失败: {str(e)}")

    async def _cleanup_tools(self) -> None:
        """清理MCP和A2A工具资源，确保在同一任务上下文中释放

        注意：该方法必须在初始化MCP/A2A的同一个asyncio Task中调用，
        否则anyio的cancel scope会检测到任务上下文切换并抛出RuntimeError。
        """
        try:
            if self._mcp_tool:
                await self._mcp_tool.cleanup()
        except Exception as e:
            logger.warning(f"清理MCP工具资源时出错: {e}")
        try:
            if self._a2a_tool:
                await self._a2a_tool.manager.cleanup()
        except Exception as e:
            logger.warning(f"清理A2A工具资源时出错: {e}")

    async def invoke(self, task: Task) -> None:
        """根据传递的任务处理agent消息队列并运行agent流"""
        try:
            # 1.重置Shell输出计数器，防止续接会话时旧状态导致输出截断
            self._shell_console_sent_count.clear()

            # 2.确保沙箱、技能脚本、mcp、a2a均初始化完成
            # Skills同步与A2A初始化互不依赖,并行执行降低启动延迟
            # MCP初始化必须顺序执行: streamablehttp_client使用anyio cancel scope,
            # 要求initialize和cleanup在同一asyncio Task中(并行会创建子Task导致cancel scope冲突)
            logger.info("AgentTaskRunner任务处理开始")
            await self._sandbox.ensure_sandbox()
            await asyncio.gather(
                self._sync_skill_scripts_to_sandbox(),
                self._a2a_tool.initialize(self._a2a_config),
            )
            await self._mcp_tool.initialize(self._mcp_config)

            # 3.循环读取任务中的输入消息队列
            while not await task.input_stream.is_empty():
                event = await self._pop_event(task)
                message = ""

                # 4.判断事件类型是否为消息事件，如果是则处理消息并将附件同步到沙箱中
                if isinstance(event, MessageEvent):
                    message = event.message or ""
                    await self._sync_message_attachments_to_sandbox(event)
                    logger.info(f"AgentTaskRunner接收到新消息: {message[:50]}...")

                # 5.将消息事件转换为消息对象
                message_obj = Message(
                    message=message,
                    attachments=[
                        attachment.filepath or f"/home/ubuntu/upload/{attachment.filename}"
                        for attachment in event.attachments
                    ]
                )

                # 6.传递消息对象并运行PlannerReActFlow
                async for event in self._run_flow(message_obj):
                    # 7.将得到的事件添加到消息队列中
                    await self._put_and_add_event(task, event)

                    # 8.如果事件类型为标题事件则更新会话标题
                    if isinstance(event, TitleEvent):
                        async with self._uow_factory() as uow:
                            await uow.session.update_title(self._session_id, event.title)
                    elif isinstance(event, MessageEvent):
                        # 9.消息事件：仅 is_final=True 时写库（聚合后的完整内容），
                        # delta 事件仅通过 SSE 推送到前端，避免高频 DB 写入与未读计数错乱
                        if getattr(event, "is_final", False):
                            async with self._uow_factory() as uow:
                                await uow.session.update_latest_message(
                                    self._session_id,
                                    event.message,
                                    event.created_at,
                                )
                                await uow.session.increment_unread_message_count(self._session_id)
                    elif isinstance(event, WaitEvent):
                        # 10.如果事件为等待，则更新会话状态并终止程序
                        async with self._uow_factory() as uow:
                            await uow.session.update_status(self._session_id, SessionStatus.WAITING)
                        return

                    # 11.若输入队列有新消息等待处理，中断当前flow事件流以回到外层循环处理下一条消息
                    if not await task.input_stream.is_empty():
                        break

            # 12.更新会话状态为已完成
            async with self._uow_factory() as uow:
                await uow.session.update_status(self._session_id, SessionStatus.COMPLETED)
        except asyncio.CancelledError:
            # 13.异步任务被取消，推送结束事件并更新状态
            logger.info("AgentTaskRunner任务运行取消")
            await self._put_and_add_event(task, DoneEvent())
            # Batch 35: DB写入兜底,stop_session已先于cancel设置COMPLETED,
            # 此处失败仅记录debug(状态已是终态,避免二次写入竞态)
            try:
                async with self._uow_factory() as uow:
                    await uow.session.update_status(self._session_id, SessionStatus.COMPLETED)
            except Exception as e:
                logger.debug(f"会话[{self._session_id}]取消时兜底状态更新失败(已被stop_session设置): {e}")
            raise
        except Exception as e:
            logger.exception(f"AgentTaskRunner运行出错: {str(e)}")
            await self._emit_degraded_summary(task, str(e))
            await self._put_and_add_event(task, ErrorEvent(error=f"AgentTaskRunner出错: {str(e)}"))
            async with self._uow_factory() as uow:
                await uow.session.update_status(self._session_id, SessionStatus.COMPLETED)
        finally:
            # 14.在同一个asyncio Task上下文中清理MCP/A2A工具资源
            # 这是关键：streamablehttp_client内部使用anyio.create_task_group()，
            # 要求在同一个Task中进入和退出cancel scope，
            # 所以必须在invoke()的finally块（即初始化MCP的同一个Task）中清理
            await self._cleanup_tools()
            # F10-9可观测性: 会话结束时输出指标快照(结构化日志,便于ELK采集)
            # 仅标记会话结束并输出,不阻断主流程;异常时静默降级
            try:
                self._metrics.mark_session_end()
                # Batch 39 / 方向3: 合并工具预算使用报告到指标快照
                # budget_tracker 由 Flow 持有,此处提取使用报告作为 gauge 指标
                if self._flow and hasattr(self._flow, "_budget_tracker") and self._flow._budget_tracker:
                    try:
                        budget_report = self._flow._budget_tracker.get_usage_report()
                        self._metrics.set_gauge("tool_budget_report", budget_report)
                    except Exception as budget_err:
                        logger.debug(f"会话[{self._session_id}]预算报告合并失败(降级忽略): {budget_err}")
                # Batch 40 / 方向2: 合并实验组标识到指标快照(供 A/B 分析)
                if self._flow and hasattr(self._flow, "_experiment_group"):
                    self._metrics.set_gauge("experiment_group", self._flow._experiment_group)
                # Batch 40 / 方向3: 合并 shell 调用画像到指标快照
                if self._flow and hasattr(self._flow, "_shell_profiler"):
                    try:
                        shell_profile = self._flow._shell_profiler.get_profile_summary()
                        self._metrics.set_gauge("shell_call_profile", shell_profile)
                    except Exception as shell_err:
                        logger.debug(f"会话[{self._session_id}]shell画像合并失败(降级忽略): {shell_err}")
                self._metrics.log_snapshot()
                # Batch 40 / 方向2+3: 持久化指标到 Redis(供离线 A/B 分析 + 合并引导效果量化)
                try:
                    from app.domain.services.observability.metrics_persister import MetricsPersister
                    persister = MetricsPersister()
                    experiment_group = getattr(self._flow, "_experiment_group", "default") if self._flow else "default"
                    await persister.persist(self._session_id, self._metrics.snapshot(), experiment_group)
                except Exception as persist_err:
                    logger.debug(f"会话[{self._session_id}]指标持久化失败(降级忽略): {persist_err}")
            except Exception as metrics_err:
                logger.debug(f"会话[{self._session_id}]指标快照输出失败(降级忽略): {metrics_err}")

    async def destroy(self) -> None:
        """销毁任务运行器并释放资源"""
        # 1.清除沙箱
        logger.info("开始清除销毁AgentTaskRunner资源")
        if self._sandbox:
            logger.info("销毁AgentTaskRunner中的沙箱环境")
            await self._sandbox.destroy()

        # 2.取消所有后台异步任务(F10-7),防止服务关闭后仍有孤儿任务在运行
        # 放在 _cleanup_tools 之前,避免MCP/A2A连接提前关闭导致回调通知失败
        await self.cancel_background_tasks()

        # 3.清除mcp和a2a工具（幂等操作，如果invoke()中已清理则不会重复执行）
        await self._cleanup_tools()

    async def cleanup_browser(self) -> None:
        """清理浏览器状态(导航到空白页,释放当前页面资源)

        停止会话时调用，防止续接会话时残留上个会话的页面状态。
        异常不抛出(仅记录警告)，确保不影响主流程的会话停止。
        """
        if not self._browser:
            return
        try:
            await self._browser.navigate("about:blank")
            logger.info(f"会话[{self._session_id}]浏览器状态清理成功(已导航至about:blank)")
        except Exception as e:
            logger.warning(f"会话[{self._session_id}]浏览器状态清理失败(不影响主流程): {e}")

    async def cancel_background_tasks(self) -> None:
        """取消所有后台异步任务(F10-7,会话停止时调用)

        委托给 PlannerReActFlow.cancel_background_tasks 取消 shell_execute(async_mode=true)
        启动的后台命令,防止任务运行器实例销毁后仍有孤儿任务在运行。
        异常不抛出(仅记录警告),确保不影响主流程的会话停止。
        """
        if not self._flow:
            return
        try:
            await self._flow.cancel_background_tasks()
        except Exception as e:
            logger.warning(f"会话[{self._session_id}]取消后台异步任务失败(不影响主流程): {e}")

    async def on_done(self, task: Task) -> None:
        """任务结束时执行的回调函数"""
        logger.info("AgentTaskRunner任务执行结束")
