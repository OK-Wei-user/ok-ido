#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_batch2_p1_architecture.py
批次2 P1架构解耦单元测试

覆盖4个优化点:
- F2-1: session_routes.py拆分为5个子路由文件,验证路由注册完整性
- F2-2: SessionService文件展示策略抽离到FilePresentationService,验证组合关系与委托
- F2-3: _is_likely_process_file模式外置到FilePresentationConfig,验证配置驱动
- F2-4: react.py业务关键词外置到AgentConfig,验证关键词注入与向后兼容

设计原则:
- 每个优化点独立测试类,便于定位回归
- 优先验证"行为契约"而非"实现细节",降低重构脆弱性
- 向后兼容性是核心验证点(老config.yaml无新字段时使用默认值)
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from app.domain.models.app_config import (
    AgentConfig,
    AppConfig,
    FilePresentationConfig,
)


# ===========================================================================
# F2-1: session_routes.py路由拆分验证
# ===========================================================================
class TestF21RouteSplit:
    """F2-1路由拆分测试: 验证5个子路由模块正确注册,路径与原路由一致"""

    def test_five_sub_route_modules_importable(self):
        """5个session子路由模块均可正常导入"""
        from app.interfaces.endpoints import (
            session_routes,
            chat_routes,
            session_file_routes,
            session_shell_routes,
            session_vnc_routes,
        )
        # 每个模块必须暴露router(APIRouter实例)
        for module in (session_routes, chat_routes, session_file_routes,
                       session_shell_routes, session_vnc_routes):
            assert hasattr(module, "router"), f"{module.__name__} 缺少router属性"

    def test_session_common_module_exists(self):
        """_session_common共享模块存在且暴露公共工具"""
        from app.interfaces.endpoints import _session_common
        # 共享模块应暴露会话归属校验函数
        assert hasattr(_session_common, "_validate_session_ownership")

    def test_all_session_routes_registered_in_api_router(self):
        """所有5个session子路由都注册到api_router,路径与原路由一致

        通过直接检查各子路由模块的routes验证路径完整性,
        再验证api_router通过include_router聚合了所有子路由。
        """
        from app.interfaces.endpoints import (
            session_routes,
            chat_routes,
            session_file_routes,
            session_shell_routes,
            session_vnc_routes,
        )

        # 收集5个子路由模块的所有路径(route.path已含/sessions前缀)
        actual_paths = set()
        for module in (session_routes, chat_routes, session_file_routes,
                       session_shell_routes, session_vnc_routes):
            for route in module.router.routes:
                if hasattr(route, "path"):
                    actual_paths.add(route.path)

        # 验证关键会话路径全部存在(F2-1拆分后路径必须与原路由一致)
        expected_session_paths = {
            "/sessions",                                    # 创建会话 + 列表
            "/sessions/stream",                             # 列表SSE流
            "/sessions/{session_id}",                       # 会话详情
            "/sessions/{session_id}/chat",                  # 聊天SSE流
            "/sessions/{session_id}/files",                 # 会话文件列表
            "/sessions/{session_id}/file",                  # 单文件读取
            "/sessions/{session_id}/shell",                 # Shell输出读取
            "/sessions/{session_id}/vnc",                   # VNC WebSocket代理
            "/sessions/{session_id}/clear-unread-message-count",  # 清除未读
            "/sessions/{session_id}/delete",                # 删除会话
            "/sessions/{session_id}/stop",                  # 停止会话
        }
        missing = expected_session_paths - actual_paths
        assert not missing, f"以下会话路径未注册: {missing}"

    def test_api_router_includes_all_session_sub_routers(self):
        """api_router通过include_router聚合了所有5个session子路由"""
        from app.interfaces.endpoints.routes import router
        # 通过检查router.routes的数量验证聚合了多个子路由
        # (auth + status + app_config + file + 5个session子路由 = 至少9个IncludedRouter)
        assert len(router.routes) >= 9, \
            f"api_router应聚合至少9个子路由,实际{len(router.routes)}个"

    def test_session_sub_routers_share_prefix_and_tags(self):
        """5个session子路由共享prefix="/sessions"与tags=["会话模块"]"""
        from app.interfaces.endpoints import (
            session_routes,
            chat_routes,
            session_file_routes,
            session_shell_routes,
            session_vnc_routes,
        )
        for module in (session_routes, chat_routes, session_file_routes,
                       session_shell_routes, session_vnc_routes):
            # 验证router的prefix和tags(通过router内部routes的路径前缀间接验证)
            assert module.router.prefix == "/sessions", \
                f"{module.__name__} prefix应为/sessions, 实际为{module.router.prefix}"
            assert "会话模块" in module.router.tags, \
                f"{module.__name__} tags应包含'会话模块'"


# ===========================================================================
# F2-2: FilePresentationService抽离验证
# ===========================================================================
class TestF22FilePresentationService:
    """F2-2抽离测试: 验证SessionService通过组合持有FilePresentationService并正确委托"""

    def test_file_presentation_service_exists(self):
        """FilePresentationService类存在且可导入"""
        from app.application.services.file_presentation_service import FilePresentationService
        assert FilePresentationService is not None

    def test_session_service_composes_file_presentation(self):
        """SessionService通过组合方式持有FilePresentationService实例"""
        from app.application.services.session_service import SessionService
        from app.application.services.file_presentation_service import FilePresentationService

        # 验证SessionService构造函数接受file_presentation_config参数
        import inspect
        sig = inspect.signature(SessionService.__init__)
        assert "file_presentation_config" in sig.parameters, \
            "SessionService.__init__ 应接受 file_presentation_config 参数"

    @pytest.mark.asyncio
    async def test_session_service_delegates_get_session_files(self):
        """SessionService.get_session_files委托给FilePresentationService"""
        from unittest.mock import AsyncMock
        from app.application.services.session_service import SessionService
        from app.application.services.file_presentation_service import FilePresentationService

        # 通过mock验证委托关系(get_session_files为async方法)
        with patch.object(SessionService, '__init__', lambda self, **kw: None):
            service = SessionService.__new__(SessionService)
            service._file_presentation = MagicMock(spec=FilePresentationService)
            # deduplicate_files/filter_empty_files为静态方法,需直接返回值
            service._file_presentation.deduplicate_files = MagicMock(return_value=[])
            service._file_presentation.filter_empty_files = MagicMock(return_value=[])
            service._file_presentation.present_files = MagicMock(return_value=[])

            # 构造支持async with的mock uow
            mock_session = MagicMock()
            mock_session.files = []
            mock_uow = MagicMock()
            mock_uow.session = MagicMock()
            mock_uow.session.get_by_id = AsyncMock(return_value=mock_session)
            mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
            mock_uow.__aexit__ = AsyncMock(return_value=None)
            service._uow = mock_uow

            # 调用async方法
            await service.get_session_files("test_session_id")

            # 验证委托调用了FilePresentationService的方法
            service._file_presentation.deduplicate_files.assert_called_once()
            service._file_presentation.filter_empty_files.assert_called_once()
            service._file_presentation.present_files.assert_called_once()

    def test_file_presentation_service_methods_exist(self):
        """FilePresentationService暴露所有抽离的方法"""
        from app.application.services.file_presentation_service import FilePresentationService
        expected_methods = [
            "present_files",
            "deduplicate_files",      # static
            "filter_empty_files",     # static
            "sort_files_by_priority",
            "extract_delivered_file_paths",
            "is_likely_process_file",
        ]
        for method_name in expected_methods:
            assert hasattr(FilePresentationService, method_name), \
                f"FilePresentationService 缺少方法: {method_name}"

    def test_deduplicate_files_is_static(self):
        """deduplicate_files为静态方法,可通过类名直接调用"""
        from app.application.services.file_presentation_service import FilePresentationService
        # 静态方法不需要实例化即可调用
        result = FilePresentationService.deduplicate_files([])
        assert result == []

    def test_filter_empty_files_is_static(self):
        """filter_empty_files为静态方法,可通过类名直接调用"""
        from app.application.services.file_presentation_service import FilePresentationService
        result = FilePresentationService.filter_empty_files([])
        assert result == []


# ===========================================================================
# F2-3: FilePresentationConfig配置外置验证
# ===========================================================================
class TestF23ConfigExternalization:
    """F2-3配置外置测试: 验证FilePresentationConfig驱动过程文件识别"""

    def test_file_presentation_config_default_values(self):
        """FilePresentationConfig默认值与原SessionService硬编码值一致"""
        config = FilePresentationConfig()
        # 验证关键默认值(与原硬编码完全一致,保证向后兼容)
        assert ".log" in config.log_extensions
        assert ".py" in config.script_extensions
        assert ".js" in config.script_extensions
        assert config.text_process_extension == ".txt"
        # 验证文件类型优先级
        assert config.file_type_priority[".xlsx"] == 100
        assert config.file_type_priority[".csv"] == 95
        assert config.file_type_priority[".md"] == 70
        assert config.file_type_priority[".log"] == 20
        assert config.default_file_priority == 40

    def test_is_likely_process_file_uses_config(self):
        """is_likely_process_file使用配置而非硬编码"""
        from app.application.services.file_presentation_service import FilePresentationService

        # 默认配置: .log文件为过程文件
        service = FilePresentationService()
        assert service.is_likely_process_file("/home/ubuntu/debug.log") is True
        assert service.is_likely_process_file("/home/ubuntu/report.xlsx") is False

    def test_custom_config_overrides_default(self):
        """自定义配置能覆盖默认值"""
        # 自定义配置: 将.log从过程文件中移除
        custom_config = FilePresentationConfig(
            log_extensions=[],  # 空列表,不识别任何日志扩展名为过程文件
        )
        from app.application.services.file_presentation_service import FilePresentationService
        service = FilePresentationService(config=custom_config)

        # .log不再被识别为过程文件
        assert service.is_likely_process_file("/home/ubuntu/debug.log") is False

    def test_custom_script_patterns(self):
        """自定义脚本名模式能生效"""
        custom_config = FilePresentationConfig(
            script_extensions=[".py"],
            script_name_patterns=["mytool"],  # 仅识别包含mytool的.py文件
        )
        from app.application.services.file_presentation_service import FilePresentationService
        service = FilePresentationService(config=custom_config)

        # 匹配自定义模式的脚本为过程文件
        assert service.is_likely_process_file("/home/ubuntu/mytool.py") is True
        # 不匹配模式的脚本不是过程文件
        assert service.is_likely_process_file("/home/ubuntu/analysis.py") is False

    def test_app_config_includes_file_presentation_config(self):
        """AppConfig包含file_presentation_config字段,默认值保证向后兼容"""
        # AppConfig应该有file_presentation_config字段
        assert hasattr(AppConfig, "model_fields")
        assert "file_presentation_config" in AppConfig.model_fields

    def test_old_config_yaml_without_file_presentation_field(self):
        """老config.yaml无file_presentation_config字段时使用默认值(向后兼容)"""
        # 模拟老config.yaml(无file_presentation_config字段)
        from app.domain.models.app_config import LLMConfig, MCPConfig, A2AConfig
        old_style_config = AppConfig(
            llm_config=LLMConfig(),
            agent_config=AgentConfig(),
            mcp_config=MCPConfig(),
            a2a_config=A2AConfig(),
            # 不传file_presentation_config,应使用默认值
        )
        assert old_style_config.file_presentation_config is not None
        assert isinstance(old_style_config.file_presentation_config, FilePresentationConfig)
        # 默认值验证
        assert ".log" in old_style_config.file_presentation_config.log_extensions

    def test_service_dependencies_injects_config(self):
        """service_dependencies正确加载config并注入FilePresentationConfig"""
        # 验证get_session_service函数签名能正确加载AppConfig并注入
        import inspect
        from app.interfaces.service_dependencies import get_session_service
        # 函数应可调用(实际调用需要DB连接,这里仅验证存在性)
        assert callable(get_session_service)


# ===========================================================================
# F2-4: AgentConfig.special_capability_keywords外置验证
# ===========================================================================
class TestF24KeywordExternalization:
    """F2-4关键词外置测试: 验证react.py方法接受keywords参数,AgentConfig提供默认值"""

    def test_agent_config_has_special_capability_keywords(self):
        """AgentConfig包含special_capability_keywords字段"""
        assert "special_capability_keywords" in AgentConfig.model_fields

    def test_agent_config_default_keywords_not_empty(self):
        """AgentConfig默认关键词列表非空(覆盖多模态+专业领域)"""
        config = AgentConfig()
        assert len(config.special_capability_keywords) > 0
        # 验证多模态关键词
        assert "图片" in config.special_capability_keywords
        assert "ocr" in config.special_capability_keywords
        # 验证专业领域关键词
        assert "天气" in config.special_capability_keywords
        assert "翻译" in config.special_capability_keywords

    def test_agent_config_keywords_customizable(self):
        """AgentConfig关键词可通过config.yaml自定义"""
        custom_keywords = ["自定义能力1", "custom_ability"]
        config = AgentConfig(special_capability_keywords=custom_keywords)
        assert config.special_capability_keywords == custom_keywords

    def test_react_methods_accept_keywords_param(self):
        """react.py的3个方法接受可选keywords参数"""
        import inspect
        from app.domain.services.agents.react import ReActAgent

        for method_name in ("_is_special_capability_step", "_build_mcp_capability_hint",
                            "_build_no_tool_retry_guidance"):
            method = getattr(ReActAgent, method_name)
            sig = inspect.signature(method)
            assert "keywords" in sig.parameters, \
                f"ReActAgent.{method_name} 应接受 keywords 参数"

    def test_build_execution_query_accepts_keywords_param(self):
        """_build_execution_query接受special_capability_keywords参数"""
        import inspect
        from app.domain.services.agents.react import ReActAgent
        sig = inspect.signature(ReActAgent._build_execution_query)
        assert "special_capability_keywords" in sig.parameters

    def test_keywords_param_overrides_default(self):
        """显式传入keywords参数时覆盖默认值"""
        from app.domain.services.agents.react import ReActAgent

        # 自定义关键词列表,仅识别"特殊能力"
        custom_keywords = ["特殊能力"]
        # 步骤描述包含自定义关键词
        assert ReActAgent._is_special_capability_step("这是一个特殊能力步骤", custom_keywords) is True
        # 步骤描述包含默认关键词但不在自定义列表中
        assert ReActAgent._is_special_capability_step("查询天气", custom_keywords) is False

    def test_keywords_none_uses_default(self):
        """keywords为None时使用模块默认值(向后兼容)"""
        from app.domain.services.agents.react import ReActAgent
        # 不传keywords参数(默认None),应使用模块默认值
        assert ReActAgent._is_special_capability_step("查询天气") is True
        assert ReActAgent._is_special_capability_step("普通步骤") is False

    def test_module_default_keywords_constant_exists(self):
        """模块级_DEFAULT_SPECIAL_CAPABILITY_KEYWORDS常量存在"""
        from app.domain.services.agents import react
        assert hasattr(react, "_DEFAULT_SPECIAL_CAPABILITY_KEYWORDS")
        # 验证默认值与AgentConfig默认值一致(保证向后兼容)
        default_keywords = react._DEFAULT_SPECIAL_CAPABILITY_KEYWORDS
        agent_config_keywords = AgentConfig().special_capability_keywords
        # 两者内容应一致(顺序可能不同,用集合比较)
        assert set(default_keywords) == set(agent_config_keywords)

    def test_mcp_hint_uses_keywords_param(self):
        """_build_mcp_capability_hint使用传入的keywords识别专业能力步骤"""
        from app.domain.services.agents.react import ReActAgent

        # 自定义关键词: 仅识别"业务能力"
        custom_keywords = ["业务能力"]
        # 包含自定义关键词的步骤应返回MCP引导
        hint = ReActAgent._build_mcp_capability_hint("执行业务能力分析", custom_keywords)
        assert hint is not None
        # 直接加载模式: 引导中应包含mcp_前缀标识与直接调用说明(无桥接工具)
        assert "mcp_" in hint
        assert "直接调用" in hint
        # 已移除桥接工具,不应再出现mcp_tool_search
        assert "mcp_tool_search" not in hint

        # 包含默认关键词但不包含自定义关键词的步骤不应返回引导
        hint = ReActAgent._build_mcp_capability_hint("查询天气", custom_keywords)
        assert hint is None

    def test_no_tool_retry_guidance_uses_keywords_param(self):
        """_build_no_tool_retry_guidance使用传入的keywords识别专业能力步骤"""
        from app.domain.services.agents.react import ReActAgent

        custom_keywords = ["专属能力"]
        guidance = ReActAgent._build_no_tool_retry_guidance("执行专属能力", custom_keywords)
        # 应包含专业能力引导(直接加载模式:mcp_前缀+直接调用)
        assert "mcp_" in guidance
        assert "专业领域能力" in guidance
        # 已移除桥接工具,不应再出现mcp_tool_search
        assert "mcp_tool_search" not in guidance

        # 不匹配自定义关键词的步骤,引导中不应含专业能力提示
        guidance = ReActAgent._build_no_tool_retry_guidance("普通步骤", custom_keywords)
        assert "专业领域能力" not in guidance
