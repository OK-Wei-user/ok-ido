#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/5/17 10:54

@File    : dependencies.py
"""
import logging
import os
from typing import Optional

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.agent_service import AgentService
from app.application.services.app_config_service import AppConfigService
from app.application.services.file_service import FileService
from app.application.services.session_service import SessionService
from app.application.services.status_service import StatusService
from app.application.services.user_service import UserService
from app.core.security import TokenBlacklist
from app.domain.repositories.skill_repository import ISkillRepository
from app.domain.services.skill_service import SkillService
from app.infrastructure.external.file_storage.cos_file_storage import CosFileStorage
from app.infrastructure.external.health_checker.postgres_health_checker import PostgresHealthChecker
from app.infrastructure.external.health_checker.redis_health_checker import RedisHealthChecker
from app.infrastructure.external.json_parser.repair_json_parser import RepairJSONParser
from app.infrastructure.external.llm.factory import create_llm
from app.infrastructure.external.llm.token_counter import TokenCounter
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.external.search.bing_search import BingSearchEngine
from app.infrastructure.external.search.content_fetcher import WebContentFetcher
from app.infrastructure.external.search.fallback_search import FallbackSearchEngine
from app.infrastructure.external.search.searxng_search import SearXNGSearchEngine
from app.infrastructure.external.task.redis_stream_task import RedisStreamTask
from app.infrastructure.repositories.file_app_config_repository import FileAppConfigRepository
from app.infrastructure.repositories.file_skill_repository import FileSkillRepository
from app.infrastructure.storage.cos import Cos, get_cos
from app.infrastructure.storage.postgres import get_db_session, get_uow
from app.infrastructure.storage.redis import RedisClient, get_redis
from app.infrastructure.storage.idempotent_tool_registry import IdempotentToolRegistry
from app.infrastructure.storage.search_cache import SearchCache
from app.infrastructure.storage.session_prompt_cache import SessionPromptCache
from app.infrastructure.storage.tool_cache import ToolResultCache
from core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_SKILLS_DIR = os.path.join(_PROJECT_ROOT, "skills")

# 模块级单例: 会话级提示词缓存(进程内共享,L1内存+L2 Redis)
# 避免重复读取配置与重复获取Redis连接;首次调用惰性初始化,后续直接复用
# 使用哨兵对象区分"未初始化"与"初始化为None(配置关闭/Redis不可用)",避免重复读配置
_PROMPT_CACHE_UNINITIALIZED = object()  # 哨兵: 尚未初始化
_prompt_cache_singleton: Optional[SessionPromptCache] = _PROMPT_CACHE_UNINITIALIZED  # type: ignore[assignment]


def get_skill_repository() -> ISkillRepository:
    return FileSkillRepository(skills_dir=_SKILLS_DIR)


def get_prompt_cache() -> Optional[SessionPromptCache]:
    """获取会话级提示词缓存单例(可选,L1内存+L2 Redis)

    持久化MCP搜索/描述结果、Skills技能指南、A2A Agent卡片到Redis,
    避免长会话中LLM上下文压缩遗忘后重复search/describe,降低token消耗。
    配置未启用或Redis不可用时返回None,调用方降级为纯内存缓存。

    单例语义: 进程内共享同一实例,L1内存缓存跨组件复用(如SkillService与MCPTool);
    None结果同样缓存(通过哨兵区分),避免配置未启用时每次调用都重复读取配置文件。
    """
    global _prompt_cache_singleton
    if _prompt_cache_singleton is not _PROMPT_CACHE_UNINITIALIZED:
        return _prompt_cache_singleton  # 已初始化(可能是None或有效实例)

    # 首次初始化: 读取配置并构建缓存实例
    # 注意: 直接使用FileAppConfigRepository.load()加载配置(与get_agent_service保持一致),
    # AppConfigService是配置服务层,不暴露load方法
    app_config = FileAppConfigRepository(settings.app_config_filepath).load()
    cache_config = app_config.session_prompt_cache_config
    if not cache_config.enabled:
        _prompt_cache_singleton = None  # 配置关闭,缓存None结果
        logger.info("会话级提示词缓存未启用(配置enabled=false),降级纯内存")
        return None
    try:
        redis_client = get_redis()
        _prompt_cache_singleton = SessionPromptCache(
            redis_client=redis_client,
            ttl=cache_config.ttl_seconds,
            key_prefix=cache_config.key_prefix,
            enabled=cache_config.enabled,
        )
        logger.info(
            f"会话级提示词缓存已启用: ttl={cache_config.ttl_seconds}s, "
            f"key_prefix={cache_config.key_prefix}"
        )
        return _prompt_cache_singleton
    except Exception as e:
        logger.warning(f"会话级提示词缓存初始化失败,降级纯内存: {e}")
        _prompt_cache_singleton = None
        return None


def get_skill_service() -> SkillService:
    return SkillService(
        repository=get_skill_repository(),
        prompt_cache=get_prompt_cache(),
    )


def get_app_config_service() -> AppConfigService:
    """获取应用配置服务"""
    # 1.获取数据仓库并打印日志
    logger.info("加载获取AppConfigService")
    file_app_config_repository = FileAppConfigRepository(settings.app_config_filepath)

    # 2.实例化AppConfigService
    return AppConfigService(app_config_repository=file_app_config_repository)


def get_status_service(
        db_session: AsyncSession = Depends(get_db_session),
        redis_client: RedisClient = Depends(get_redis),
) -> StatusService:
    """获取状态服务"""
    # 1.初始化postgres和redis健康检查
    postgres_checker = PostgresHealthChecker(db_session)
    redis_checker = RedisHealthChecker(redis_client)

    # 2.创建服务并返回
    logger.info("加载获取StatusService")
    return StatusService(checkers=[postgres_checker, redis_checker])


def get_file_service(
        cos: Cos = Depends(get_cos)
) -> FileService:
    # 1.初始化文件仓库和文件存储桶
    file_storage = CosFileStorage(
        bucket=settings.oss_bucket,
        cos=cos,
        uow_factory=get_uow,
    )

    # 2.构建服务并返回
    return FileService(
        uow_factory=get_uow,
        file_storage=file_storage,
    )


def get_session_service() -> SessionService:
    # 1.获取应用配置(读取配置需要实时获取,所以不配置缓存)
    # file_presentation_config为可选注入(F2-3外置),None时使用默认配置
    app_config_repository = FileAppConfigRepository(config_path=settings.app_config_filepath)
    app_config = app_config_repository.load()
    # 2.构建会话服务并返回
    return SessionService(
        uow_factory=get_uow,
        sandbox_cls=DockerSandbox,
        file_presentation_config=app_config.file_presentation_config,
    )


def get_user_service(
        redis_client: RedisClient = Depends(get_redis),
) -> UserService:
    token_blacklist = TokenBlacklist(redis_client=redis_client.client)
    return UserService(uow_factory=get_uow, token_blacklist=token_blacklist)


def get_agent_service(
        cos: Cos = Depends(get_cos),
) -> AgentService:
    # 1.获取应用配置信息(读取配置需要实时获取,所以不配置缓存)
    app_config_repository = FileAppConfigRepository(config_path=settings.app_config_filepath)
    app_config = app_config_repository.load()

    # 2.构建依赖实例
    llm = create_llm(app_config.llm_config)
    # PlanAgent轻量化: 规划Agent专用轻量化LLM(可选),未配置时为None,PlannerReActFlow会降级到llm
    planner_llm = create_llm(app_config.planner_llm_config) if app_config.planner_llm_config else None
    if planner_llm is not None:
        logger.info(
            f"PlannerAgent启用专用LLM: model={app_config.planner_llm_config.model_name}, "
            f"thinking={app_config.planner_llm_config.thinking_mode.value}"
        )
    # 多模态LLM(可选): 浏览器visual_click视觉点击兜底专用,需支持图像输入的视觉模型。
    # 未配置时为None,visual_click自动降级为不可用(五级DOM容错仍完整)。
    multimodal_llm = create_llm(app_config.multimodal_llm_config) if app_config.multimodal_llm_config else None
    if multimodal_llm is not None:
        logger.info(f"浏览器视觉兜底启用多模态LLM: model={app_config.multimodal_llm_config.model_name}")
    token_counter = TokenCounter(app_config.llm_config.model_name)
    context_window = app_config.llm_config.context_window
    file_storage = CosFileStorage(
        bucket=settings.oss_bucket,
        cos=cos,
        uow_factory=get_uow,
    )
    search_config = app_config.search_config
    content_fetcher = WebContentFetcher(
        timeout=search_config.fetch_timeout,
        max_retries=search_config.fetch_max_retries,
        max_chars=search_config.fetch_max_chars,
        max_concurrency=search_config.fetch_max_concurrency,
    )
    search_cache = SearchCache(
        redis_client=get_redis(),
        ttl=search_config.cache_ttl_seconds,
        key_prefix=search_config.cache_key_prefix,
    ) if search_config.cache_enabled else None

    # 工具结果缓存(可选),仅缓存白名单中的幂等工具结果
    tool_cache_config = app_config.tool_cache_config
    tool_cache = ToolResultCache(
        redis_client=get_redis(),
        ttl=tool_cache_config.ttl_seconds,
        key_prefix=tool_cache_config.key_prefix,
        cacheable_tools=tool_cache_config.cacheable_tools,
        cacheable_mcp_tools=tool_cache_config.cacheable_mcp_tools,
    ) if tool_cache_config.enabled else None
    if tool_cache is not None:
        logger.info(
            f"工具结果缓存已启用: ttl={tool_cache_config.ttl_seconds}s, "
            f"cacheable_tools={tool_cache_config.cacheable_tools}"
        )

    # 工具并行执行配置(默认关闭,启用后多工具调用并行化)
    tool_execution_config = app_config.tool_execution_config
    if tool_execution_config.enabled:
        logger.info(
            f"工具并行执行已启用: max_concurrency={tool_execution_config.max_concurrency}, "
            f"stateful_prefixes={tool_execution_config.stateful_tool_prefixes}"
        )

    # 幂等工具调用去重(P10-1,通用型框架能力): 防止LLM在长会话中重复发起相同参数的幂等写操作
    idempotent_dedup_config = app_config.idempotent_tool_dedup_config
    idempotent_registry = IdempotentToolRegistry(
        redis_client=get_redis(),
        ttl=idempotent_dedup_config.ttl_seconds,
        key_prefix=idempotent_dedup_config.key_prefix,
        dedup_tools=idempotent_dedup_config.idempotent_tools,
    ) if idempotent_dedup_config.enabled else None
    if idempotent_registry is not None:
        logger.info(
            f"幂等工具调用去重已启用: ttl={idempotent_dedup_config.ttl_seconds}s, "
            f"idempotent_tools={idempotent_dedup_config.idempotent_tools}"
        )

    # 会话级提示词缓存(可选,L1内存+L2 Redis): 持久化MCP/Skills/A2A提示词,降低token消耗
    # 单例复用: 与get_skill_service()共享同一实例,L1内存缓存跨组件复用
    prompt_cache = get_prompt_cache()

    # 3.实例Agent服务并返回
    return AgentService(
        uow_factory=get_uow,
        llm=llm,
        planner_llm=planner_llm,  # PlanAgent轻量化: 规划Agent专用LLM(可选)
        multimodal_llm=multimodal_llm,  # 多模态LLM(可选): 浏览器visual_click视觉兜底
        agent_config=app_config.agent_config,
        mcp_config=app_config.mcp_config,
        a2a_config=app_config.a2a_config,
        sandbox_cls=DockerSandbox,
        task_cls=RedisStreamTask,
        json_parser=RepairJSONParser(),
        search_engine=FallbackSearchEngine(
            primary=SearXNGSearchEngine(),
            fallback=BingSearchEngine(),
        ),
        content_fetcher=content_fetcher,
        search_cache=search_cache,
        deep_research_config=search_config.deep_research_config,
        file_storage=file_storage,
        skill_service=get_skill_service(),
        token_counter=token_counter,
        context_window=context_window,
        tool_cache=tool_cache,  # 工具结果缓存(可选)
        tool_execution_config=tool_execution_config,  # 工具并行执行配置
        idempotent_registry=idempotent_registry,  # 幂等工具调用去重注册表(P10-1,可选)
        file_presentation_config=app_config.file_presentation_config,  # F10-8文件展示策略配置(集中化交付物过滤)
        prompt_cache=prompt_cache,  # 会话级提示词缓存(MCP/Skills/A2A持久化)
    )
