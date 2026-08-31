#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : main.py
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.security import get_password_hash
from app.domain.models.user import User, UserRole
from app.domain.services.skills_prompt_cache import SkillsPromptCache
from app.infrastructure.logging import setup_logging
from app.infrastructure.storage.cos import get_cos
from app.infrastructure.storage.postgres import get_postgres, get_uow
from app.infrastructure.storage.redis import get_redis
from app.interfaces.endpoints.routes import router
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.interfaces.service_dependencies import get_agent_service, get_skill_service
from core.config import get_settings

# 1.加载配置信息
settings = get_settings()

# 2.初始化日志系统
setup_logging()
logger = logging.getLogger()

# 3.定义FastAPI路由tags标签
openapi_tags = [
    {
        "name": "状态模块",
        "description": "包含 **状态监测** 等API 接口，用于监测系统的运行状态。"
    }
]


async def _seed_default_admin() -> None:
    """幂等播种默认管理员账户(admin/admin123)。

    Fresh DB启动时若 admin 不存在则创建,已存在则跳过。
    异常兜底: 播种失败仅记录日志,不阻塞应用启动。
    """
    DEFAULT_ADMIN_USERNAME = "admin"
    DEFAULT_ADMIN_PASSWORD = "admin123"
    try:
        async with get_uow() as uow:
            existing = await uow.user.get_by_username(DEFAULT_ADMIN_USERNAME)
            if existing is not None:
                logger.debug(f"默认管理员账户[{DEFAULT_ADMIN_USERNAME}]已存在,跳过播种")
                return
            admin = User(
                username=DEFAULT_ADMIN_USERNAME,
                phone="",
                hashed_password=get_password_hash(DEFAULT_ADMIN_PASSWORD),
                role=UserRole.ADMIN,
            )
            await uow.user.save(admin)
            logger.info(f"默认管理员账户[{DEFAULT_ADMIN_USERNAME}]播种完成")
    except Exception as e:
        logger.error(f"默认管理员账户播种失败(不影响主流程): {str(e)}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """创建FastAPI应用生命周期上下文管理器"""
    # 0.重新初始化日志系统(uvicorn启动时dictConfig会影响根日志处理器，需要在此重新配置)
    setup_logging()

    # 1.日志打印代码已经开始执行了
    logger.info("I-DO正在初始化")

    # 2.运行数据库迁移(将数据同步到生产环境)
    # F10-9可观测性: alembic.env会调用fileConfig(alembic.ini)重新配置根logger,
    # 其中[logger_root] level=WARNING 会覆盖 setup_logging() 的 DEBUG/INFO 级别,
    # 导致后续应用子logger(如agent_task_runner/metrics_collector)的INFO日志被过滤,
    # 指标快照等可观测性输出无法被ELK采集。迁移完成后必须重新初始化日志系统。
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")
    setup_logging()  # 重新初始化日志系统,恢复 INFO/DEBUG 级别,保障可观测性

    # 3.初始化Redis/Postgres/Cos客户端
    await get_redis().init()
    await get_postgres().init()
    await get_cos().init()

    # 幂等播种默认管理员账户(确保fresh DB可登录)
    await _seed_default_admin()

    # 4.初始化Skills提示词缓存
    try:
        skill_service = get_skill_service()
        await SkillsPromptCache.initialize(skill_service)
    except Exception as e:
        logger.warning(f"Skills提示词缓存初始化失败(不影响主流程): {str(e)}")

    try:
        # 4.lifespan分界点
        yield
    finally:
        try:
            # 5.等待agent服务关闭
            logger.info("I-DO正在关闭")
            await asyncio.wait_for(get_agent_service().shutdown(), timeout=30.0)
            logger.info("Agent服务成功关闭")
        except asyncio.TimeoutError:
            logger.warning("Agent服务关闭超时, 强制关闭, 部分任务将被释放")
        except Exception as e:
            logger.error(f"Agent服务关闭期间出现错误: {str(e)}")

        # 6.关闭其他应用
        await get_redis().shutdown()
        await get_postgres().shutdown()
        await get_cos().shutdown()

        logger.info("I-DO应用关闭成功")


# 4.创建I-DO应用实例
app = FastAPI(
    title="I-DO通用智能体",
    description="I-DO是一个通用的AI Agent系统，可以完全私有部署，使用A2A+MCP连接Agent/Tool，同时支持在沙箱中运行各种内置工具和操作",
    lifespan=lifespan,
    openapi_tags=openapi_tags,
    version="1.0.0",
)

# 5.配置CORS中间件，解决跨域问题
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 6.注册错误处理器
register_exception_handlers(app)

# 7.集成路由
app.include_router(router, prefix="/api")
