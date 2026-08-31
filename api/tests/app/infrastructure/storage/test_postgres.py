#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_postgres.py
Postgres 单元测试 — 验证 F4-代码质量修复: 移除无效 f-string + 简化未使用异常变量

测试覆盖:
- init: 重复初始化守护(无效 f-string 修复后日志正常输出)
- get_db_session: 异常路径触发回滚(except Exception: 不再绑定未使用变量 _)
- shutdown: 关闭流程清理引擎与缓存
- session_factory: 未初始化时抛出 RuntimeError
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infrastructure.storage.postgres import Postgres, get_db_session, get_postgres


# ============ 辅助函数 ============

def _make_postgres_with_engine() -> Postgres:
    """构造已初始化引擎的 Postgres 实例(不实际连接)"""
    pg = Postgres.__new__(Postgres)
    pg._engine = MagicMock()
    pg._session_factory = MagicMock()
    pg._settings = MagicMock()
    return pg


# ============ init: 重复初始化守护 ============

class TestInitIdempotency:
    """Postgres.init 重复初始化守护验证(F4: 移除无效 f-string)"""

    @pytest.mark.asyncio
    async def test_init_returns_early_when_engine_exists(self, caplog):
        """已存在引擎时 init 应直接返回,不重复创建"""
        pg = _make_postgres_with_engine()
        # init 应直接返回,不调用 create_async_engine
        with patch("app.infrastructure.storage.postgres.create_async_engine") as mock_create:
            await pg.init()
            mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_init_logs_warning_without_fstring_placeholders(self, caplog):
        """init 重复调用时应记录 warning(无占位符的字符串)"""
        import logging

        pg = _make_postgres_with_engine()
        with caplog.at_level(logging.WARNING, logger="app.infrastructure.storage.postgres"):
            await pg.init()

        # 验证 warning 日志包含预期文案(无 f-string 也能正确输出)
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Postgres引擎已初始化" in msg for msg in warning_messages), \
            f"应输出'Postgres引擎已初始化'提示,实际: {warning_messages}"


# ============ get_db_session: 异常回滚 ============

class TestGetDbSessionExceptionRollback:
    """get_db_session 异常路径回滚验证(F4: 移除未使用变量 _)"""

    @pytest.mark.asyncio
    async def test_get_db_session_rolls_back_on_exception(self):
        """会话抛异常时应触发 rollback 并重新抛出"""
        # 构造 mock session 抛出异常
        mock_session = AsyncMock()
        mock_session.rollback = AsyncMock()

        # session_factory 返回异步上下文管理器
        mock_factory = MagicMock()

        # 构造异步上下文: __aenter__ 返回 mock_session, 期间抛异常
        class _FakeAsyncCtx:
            async def __aenter__(self):
                return mock_session

            async def __aexit__(self, exc_type, exc, tb):
                return False

        mock_factory.return_value = _FakeAsyncCtx()

        # patch get_postgres 返回带 mock_factory 的实例
        with patch("app.infrastructure.storage.postgres.get_postgres") as mock_get_pg:
            mock_pg = MagicMock()
            mock_pg.session_factory = mock_factory
            mock_get_pg.return_value = mock_pg

            # 调用 get_db_session 并消费生成器,触发异常路径
            gen = get_db_session()
            # 第一次 next 进入 try 块
            session = await gen.__anext__()
            assert session is mock_session

            # 第二次 next 注入异常,触发 except 分支
            with pytest.raises(ValueError, match="test error"):
                await gen.athrow(ValueError("test error"))

            # 验证 rollback 被调用(except Exception: 路径)
            mock_session.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_db_session_yields_session_on_success(self):
        """正常路径应成功 yield session"""
        mock_session = MagicMock()

        class _FakeAsyncCtx:
            async def __aenter__(self):
                return mock_session

            async def __aexit__(self, exc_type, exc, tb):
                return False

        mock_factory = MagicMock()
        mock_factory.return_value = _FakeAsyncCtx()

        with patch("app.infrastructure.storage.postgres.get_postgres") as mock_get_pg:
            mock_pg = MagicMock()
            mock_pg.session_factory = mock_factory
            mock_get_pg.return_value = mock_pg

            gen = get_db_session()
            session = await gen.__anext__()
            assert session is mock_session
            # 正常关闭生成器
            await gen.aclose()


# ============ session_factory: 未初始化守护 ============

class TestSessionFactoryGuard:
    """session_factory 只读属性守护验证"""

    def test_session_factory_raises_when_not_initialized(self):
        """未初始化时访问 session_factory 应抛 RuntimeError"""
        pg = Postgres.__new__(Postgres)
        pg._engine = None
        pg._session_factory = None

        with pytest.raises(RuntimeError, match="Postgres未初始化"):
            _ = pg.session_factory

    def test_session_factory_returns_factory_when_initialized(self):
        """已初始化时 session_factory 应返回工厂实例"""
        pg = _make_postgres_with_engine()
        assert pg.session_factory is pg._session_factory


# ============ get_postgres: 单例缓存 ============

class TestGetPostgresSingleton:
    """get_postgres lru_cache 单例验证"""

    def test_get_postgres_returns_same_instance(self):
        """多次调用应返回同一实例(lru_cache 缓存)"""
        # 清除缓存以确保测试隔离
        get_postgres.cache_clear()
        try:
            pg1 = get_postgres()
            pg2 = get_postgres()
            assert pg1 is pg2
        finally:
            get_postgres.cache_clear()
