#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_logging_setup.py

F10-9 可观测性单元测试 - setup_logging() 兜底修复验证。

覆盖场景:
1. setup_logging() 应清除 root logger 现有 handlers,并添加唯一的 stderr StreamHandler
2. setup_logging() 应将 root logger 等级设置为 settings.log_level
3. F10-9 关键修复: setup_logging() 应重新启用被 fileConfig(disable_existing_loggers=True)
   静默禁用的子 logger,确保 app.domain.* 等业务日志可被采集
4. setup_logging() 应可重复调用,结果幂等
"""
import logging
import sys
from logging.config import fileConfig

import pytest

from app.infrastructure.logging import setup_logging


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """每个测试结束前恢复 root logger 状态,避免测试间相互污染。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    # 清理 setup_logging 添加的 handler,恢复原始状态
    root.handlers.clear()
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def _make_logger(name: str) -> logging.Logger:
    """创建一个独立的 logger,用于测试。"""
    return logging.getLogger(name)


class TestSetupLoggingRootConfig:
    """验证 root logger 的基础配置。"""

    def test_clears_existing_handlers_and_adds_single_stderr_handler(self):
        # Arrange: 预置一个干扰 handler
        root = logging.getLogger()
        noise = logging.NullHandler()
        root.addHandler(noise)
        assert noise in root.handlers

        # Act
        setup_logging()

        # Assert: 噪声 handler 被清除,仅保留一个 StreamHandler 指向 stderr
        assert noise not in root.handlers
        assert len(root.handlers) == 1
        handler = root.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stderr

    def test_root_level_matches_settings_log_level(self, monkeypatch):
        # Arrange: 注入 DEBUG 等级 (get_settings 定义在 app.infrastructure.logging.logging 模块)
        class _StubSettings:
            log_level = "DEBUG"

        monkeypatch.setattr(
            "app.infrastructure.logging.logging.get_settings",
            lambda: _StubSettings(),
        )

        # Act
        setup_logging()

        # Assert
        assert logging.getLogger().level == logging.DEBUG

    def test_idempotent_multiple_calls_keep_single_handler(self):
        # Act: 连续调用 3 次
        setup_logging()
        setup_logging()
        setup_logging()

        # Assert: 仍然只有一个 handler,不会累积
        assert len(logging.getLogger().handlers) == 1


class TestSetupLoggingReenablesDisabledLoggers:
    """
    F10-9 关键修复验证:

    alembic/env.py 调用 fileConfig(alembic.ini) 时,若未传
    disable_existing_loggers=False,默认会把所有未在 [loggers] 节声明的
    已存在子 logger 标记为 disabled=True。setup_logging() 应兜底恢复。
    """

    def test_setup_logging_reenables_loggers_disabled_by_fileconfig_default(self, tmp_path):
        # Arrange: 模拟应用启动时创建子 logger (类似 main.py import 阶段)
        biz_logger = _make_logger("app.domain.services.agent_task_runner")
        biz_logger.info("pre-disable")  # 确保 logger 已实例化进入 loggerDict

        # 模拟 alembic/env.py 调用 fileConfig(disable_existing_loggers=True 默认)
        # 会禁用所有不在 [loggers] 节的已存在 logger
        ini = tmp_path / "alembic.ini"
        ini.write_text(
            "[loggers]\nkeys=root\n\n"
            "[handlers]\nkeys=console\n\n"
            "[formatters]\nkeys=generic\n\n"
            "[logger_root]\nlevel=WARNING\nhandlers=console\n\n"
            "[handler_console]\nclass=StreamHandler\nargs=(sys.stderr,)\n"
            "level=NOTSET\nformatter=generic\n\n"
            "[formatter_generic]\nformat=%(message)s\n",
            encoding="utf-8",
        )
        fileConfig(str(ini))  # disable_existing_loggers 默认 True
        assert biz_logger.disabled is True, "前置条件: fileConfig 应已禁用 biz_logger"

        # Act
        setup_logging()

        # Assert: 子 logger 被重新启用
        assert biz_logger.disabled is False, (
            "F10-9 修复: setup_logging() 必须重新启用被 fileConfig 禁用的子 logger,"
            "否则 app.domain.* 的 INFO 日志(含指标快照)将被静默过滤"
        )

    def test_setup_logging_reenables_multiple_disabled_loggers(self):
        # Arrange: 创建多个子 logger
        names = [
            "app.domain.services.agent_task_runner",
            "app.application.services.agent_service",
            "app.domain.services.observability.metrics_collector",
            "app.domain.services.flows.planner_react",
        ]
        loggers = [_make_logger(n) for n in names]
        for lg in loggers:
            lg.disabled = True

        # Act
        setup_logging()

        # Assert
        for lg in loggers:
            assert lg.disabled is False, f"logger[{lg.name}] 应被重新启用"

    def test_setup_logging_does_not_disable_normally_enabled_loggers(self):
        # Arrange
        normal_logger = _make_logger("app.normal.logger")
        normal_logger.disabled = False

        # Act
        setup_logging()

        # Assert
        assert normal_logger.disabled is False

    def test_setup_logging_handles_placeholders_in_logger_dict(self):
        """
        Logger.manager.loggerDict 中除了 Logger 实例外,
        还可能存在 PlaceHolder 对象(子 logger 父级自动创建)。
        setup_logging 不应对 PlaceHolder 调用 .disabled,应跳过。
        """
        # Arrange: 创建多层命名 logger 触发 PlaceHolder 生成
        _make_logger("app.deeply.nested.tier1.tier2")
        # 此时 'app.deeply' 和 'app.deeply.nested' 可能是 PlaceHolder

        # Act & Assert: 不应抛出 AttributeError
        setup_logging()


class TestSetupLoggingPreservesHandlerSemantics:
    """验证 handler 的格式与等级设置。"""

    def test_handler_level_matches_settings(self, monkeypatch):
        # Arrange
        class _StubSettings:
            log_level = "INFO"

        monkeypatch.setattr(
            "app.infrastructure.logging.logging.get_settings",
            lambda: _StubSettings(),
        )

        # Act
        setup_logging()

        # Assert
        handler = logging.getLogger().handlers[0]
        assert handler.level == logging.INFO

    def test_handler_has_formatter_with_expected_format(self):
        # Act
        setup_logging()

        # Assert: formatter 包含时间、logger 名、等级、消息
        handler = logging.getLogger().handlers[0]
        assert handler.formatter is not None
        # 直接通过格式化一条记录验证格式包含必要字段
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        formatted = handler.formatter.format(record)
        assert "INFO" in formatted
        assert "hello" in formatted


class TestSetupLoggingIntegrationWithMetricsSnapshot:
    """
    F10-9 端到端验证: 模拟 fileConfig 禁用 metrics_collector logger 后,
    setup_logging 应恢复其 log_snapshot() 的日志可输出能力。
    """

    def test_metrics_collector_log_snapshot_visible_after_setup_logging(self, tmp_path, monkeypatch):
        # Arrange: 引入并创建 MetricsCollector,使其 logger 进入 loggerDict
        from app.domain.services.observability.metrics_collector import MetricsCollector

        metrics = MetricsCollector(session_id="test-session")
        metrics_logger = logging.getLogger(
            "app.domain.services.observability.metrics_collector"
        )

        # 模拟 alembic fileConfig 默认行为禁用所有非声明 logger
        ini = tmp_path / "alembic.ini"
        ini.write_text(
            "[loggers]\nkeys=root\n\n"
            "[handlers]\nkeys=console\n\n"
            "[formatters]\nkeys=generic\n\n"
            "[logger_root]\nlevel=WARNING\nhandlers=console\n\n"
            "[handler_console]\nclass=StreamHandler\nargs=(sys.stderr,)\n"
            "level=NOTSET\nformatter=generic\n\n"
            "[formatter_generic]\nformat=%(message)s\n",
            encoding="utf-8",
        )
        fileConfig(str(ini))
        assert metrics_logger.disabled is True

        # Act: 调用 setup_logging 恢复
        # 注入 INFO 等级使快照日志可通过 root handler 输出
        class _StubSettings:
            log_level = "INFO"

        monkeypatch.setattr(
            "app.infrastructure.logging.logging.get_settings",
            lambda: _StubSettings(),
        )
        setup_logging()

        # Assert: log_snapshot 应能正常调用且不抛异常,logger 已恢复
        assert metrics_logger.disabled is False
        metrics.log_snapshot()  # 不应抛出异常
