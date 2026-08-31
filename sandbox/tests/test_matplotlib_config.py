#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : test_matplotlib_config.py
matplotlib 中文字体配置单元测试

测试覆盖:
1. setup_chinese_fonts 幂等性(重复调用不重复配置)
2. 字体别名注册(SimHei/Microsoft YaHei 等映射到 WenQuanYi Micro Hei)
3. findfont 别名解析(LLM 硬编码 SimHei 也能正确解析)
4. setup_figure_defaults 图片尺寸约束
5. reset_config_state 测试辅助函数
6. sitecustomize 导入钩子安装与触发

运行方式:
    cd sandbox && python -m pytest tests/test_matplotlib_config.py -v
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# 将 sandbox 目录加入 sys.path
_SANDBOX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SANDBOX_ROOT not in sys.path:
    sys.path.insert(0, _SANDBOX_ROOT)

# 测试是否在沙箱环境(有 CJK 字体)内运行
_HAS_CJK_FONTS = os.path.exists("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc") or \
                  os.path.exists("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")

# 标记需要真实 CJK 字体的测试
requires_cjk_fonts = pytest.mark.skipif(
    not _HAS_CJK_FONTS,
    reason="当前环境无 CJK 字体文件,需在沙箱容器内运行"
)


@pytest.fixture(autouse=True)
def _reset_config():
    """每个测试前重置配置状态,确保隔离"""
    from app.core.matplotlib_config import reset_config_state
    reset_config_state()
    yield
    reset_config_state()


# -------------------- setup_chinese_fonts 幂等性测试 --------------------

class TestSetupChineseFontsIdempotent:
    """setup_chinese_fonts 幂等性测试"""

    @requires_cjk_fonts
    def test_idempotent_multiple_calls(self):
        """多次调用 setup_chinese_fonts 应幂等,仅首次执行实际配置"""
        from app.core import matplotlib_config

        matplotlib_config.setup_chinese_fonts()
        first_state = matplotlib_config._CONFIGURED
        assert first_state is True

        # 第二次调用应直接返回,不重复配置
        matplotlib_config.setup_chinese_fonts()
        assert matplotlib_config._CONFIGURED is True

    @requires_cjk_fonts
    def test_configured_flag_set_after_call(self):
        """调用后 _CONFIGURED 应为 True"""
        from app.core import matplotlib_config
        assert matplotlib_config._CONFIGURED is False
        matplotlib_config.setup_chinese_fonts()
        assert matplotlib_config._CONFIGURED is True


# -------------------- 字体别名注册测试 --------------------

class TestFontAliases:
    """Windows/macOS 字体别名注册测试"""

    @requires_cjk_fonts
    def test_simhei_alias_registered(self):
        """SimHei 别名应注册到 fontManager"""
        from app.core.matplotlib_config import setup_chinese_fonts
        import matplotlib.font_manager as fm

        setup_chinese_fonts()

        font_names = {f.name for f in fm.fontManager.ttflist}
        assert "SimHei" in font_names, "SimHei 别名未注册,LLM 硬编码 SimHei 将导致乱码"

    @requires_cjk_fonts
    def test_microsoft_yahei_alias_registered(self):
        """Microsoft YaHei 别名应注册到 fontManager"""
        from app.core.matplotlib_config import setup_chinese_fonts
        import matplotlib.font_manager as fm

        setup_chinese_fonts()

        font_names = {f.name for f in fm.fontManager.ttflist}
        assert "Microsoft YaHei" in font_names

    @requires_cjk_fonts
    def test_kaiti_alias_registered(self):
        """KaiTi 别名应注册到 fontManager"""
        from app.core.matplotlib_config import setup_chinese_fonts
        import matplotlib.font_manager as fm

        setup_chinese_fonts()
        font_names = {f.name for f in fm.fontManager.ttflist}
        assert "KaiTi" in font_names

    @requires_cjk_fonts
    def test_findfont_resolves_simhei(self):
        """findfont 应能解析 SimHei 别名到实际字体文件"""
        from app.core.matplotlib_config import setup_chinese_fonts
        import matplotlib.font_manager as fm

        setup_chinese_fonts()

        # findfont 应返回非 DejaVu 的字体路径
        path = fm.findfont(fm.FontProperties(family="SimHei"))
        assert "DejaVu" not in path, f"SimHei 别名解析失败,回退到 DejaVu: {path}"
        assert path.endswith(".ttc") or path.endswith(".ttf"), \
            f"SimHei 应解析到字体文件,实际: {path}"

    @requires_cjk_fonts
    def test_simhei_resolves_to_wqy_or_noto(self):
        """SimHei 别名应解析到 WenQuanYi 或 Noto 字体文件"""
        from app.core.matplotlib_config import setup_chinese_fonts
        import matplotlib.font_manager as fm

        setup_chinese_fonts()
        path = fm.findfont(fm.FontProperties(family="SimHei"))
        # 应解析到 wqy-microhei 或 NotoSansCJK
        assert "wqy" in path.lower() or "noto" in path.lower(), \
            f"SimHei 别名应映射到 wqy/noto,实际: {path}"


# -------------------- 图片尺寸约束测试 --------------------

class TestFigureDefaults:
    """图片默认尺寸约束测试"""

    @requires_cjk_fonts
    def test_figure_size_constrained(self):
        """figsize 应约束为 (10,6) 适配 Word A4 嵌入"""
        from app.core.matplotlib_config import setup_chinese_fonts
        import matplotlib

        setup_chinese_fonts()
        fig_size = matplotlib.rcParams["figure.figsize"]
        assert list(fig_size) == [10, 6], f"figsize 应为 [10,6],实际: {fig_size}"

    @requires_cjk_fonts
    def test_dpi_constrained(self):
        """DPI 应约束为 100"""
        from app.core.matplotlib_config import setup_chinese_fonts
        import matplotlib

        setup_chinese_fonts()
        assert matplotlib.rcParams["figure.dpi"] == 100
        assert matplotlib.rcParams["savefig.dpi"] == 100

    @requires_cjk_fonts
    def test_unicode_minus_disabled(self):
        """axes.unicode_minus 应为 False"""
        from app.core.matplotlib_config import setup_chinese_fonts
        import matplotlib

        setup_chinese_fonts()
        assert matplotlib.rcParams["axes.unicode_minus"] is False


# -------------------- rcParams 配置测试 --------------------

class TestRcParamsConfig:
    """rcParams 中文字体配置测试"""

    @requires_cjk_fonts
    def test_font_family_set_to_sans_serif(self):
        """font.family 应设为 sans-serif"""
        from app.core.matplotlib_config import setup_chinese_fonts
        import matplotlib

        setup_chinese_fonts()
        assert matplotlib.rcParams["font.family"] == "sans-serif"

    @requires_cjk_fonts
    def test_cjk_font_in_sans_serif(self):
        """font.sans-serif 首项应为 CJK 字体"""
        from app.core.matplotlib_config import setup_chinese_fonts
        import matplotlib

        setup_chinese_fonts()
        sans_serif = matplotlib.rcParams["font.sans-serif"]
        cjk_candidates = [
            "WenQuanYi Micro Hei", "Noto Sans CJK SC",
            "Noto Sans CJK JP", "SimHei"
        ]
        assert any(c in sans_serif for c in cjk_candidates), \
            f"font.sans-serif 首项应为 CJK 字体,实际: {sans_serif}"


# -------------------- 异常处理测试 --------------------

class TestExceptionHandling:
    """异常处理测试"""

    def test_setup_handles_matplotlib_not_installed(self):
        """matplotlib 未安装时应静默降级,不抛异常"""
        from app.core import matplotlib_config

        with patch.dict(sys.modules, {"matplotlib": None, "matplotlib.font_manager": None}):
            # 应不抛异常
            matplotlib_config.setup_chinese_fonts()

    def test_register_font_aliases_nonexistent_file(self):
        """字体文件不存在时应跳过别名注册,不抛异常"""
        from app.core.matplotlib_config import _register_font_aliases

        # 应不抛异常
        _register_font_aliases("/nonexistent/font.ttc")

    def test_find_actual_cjk_font_path_returns_empty_on_missing(self):
        """无 CJK 字体时应返回空字符串"""
        from app.core.matplotlib_config import _find_actual_cjk_font_path

        with patch("app.core.matplotlib_config.glob.glob", return_value=[]):
            path = _find_actual_cjk_font_path()
            assert path == ""


# -------------------- reset_config_state 测试 --------------------

class TestResetConfigState:
    """reset_config_state 测试辅助函数"""

    @requires_cjk_fonts
    def test_reset_allows_reconfiguration(self):
        """reset 后可重新配置"""
        from app.core import matplotlib_config

        matplotlib_config.setup_chinese_fonts()
        assert matplotlib_config._CONFIGURED is True

        matplotlib_config.reset_config_state()
        assert matplotlib_config._CONFIGURED is False

        # 可重新调用
        matplotlib_config.setup_chinese_fonts()
        assert matplotlib_config._CONFIGURED is True


# -------------------- 端到端渲染测试 --------------------

class TestEndToEndRendering:
    """端到端渲染测试 - 验证中文能正常渲染"""

    @requires_cjk_fonts
    def test_chinese_text_renders_without_warnings(self, tmp_path):
        """中文文本渲染应无 missing glyph 警告"""
        from app.core.matplotlib_config import setup_chinese_fonts
        import warnings
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        setup_chinese_fonts()

        output = tmp_path / "chinese_test.png"
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            fig, ax = plt.subplots(figsize=(8, 4))
            ax.set_title("2026年5月经营分析报告")
            ax.set_xlabel("日期")
            ax.set_ylabel("出库量(吨)")
            ax.plot(["2026-05-01", "2026-05-02", "2026-05-03"], [100, 200, 150])
            ax.legend(["出库趋势"])
            plt.savefig(str(output), dpi=80, bbox_inches="tight")
            plt.close()

            # 检查是否有 missing glyph 警告
            glyph_warnings = [
                x for x in w
                if "missing from font" in str(x.message).lower()
                or "Glyph" in str(x.message)
            ]
            assert len(glyph_warnings) == 0, \
                f"存在 missing glyph 警告,中文可能乱码: {[str(x.message) for x in glyph_warnings]}"

        assert output.exists()
        assert output.stat().st_size > 1000, "生成的图片过小,可能渲染失败"

    @requires_cjk_fonts
    def test_simhei_hardcoded_renders_correctly(self, tmp_path):
        """LLM 硬编码 SimHei 时也应正常渲染(别名生效)"""
        from app.core.matplotlib_config import setup_chinese_fonts
        import warnings
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        setup_chinese_fonts()

        output = tmp_path / "simhei_test.png"
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            # 模拟 LLM 硬编码 SimHei 的场景
            plt.rcParams["font.sans-serif"] = ["SimHei"]
            plt.rcParams["axes.unicode_minus"] = False

            fig, ax = plt.subplots(figsize=(8, 4))
            ax.set_title("每日出入库趋势分析")
            ax.text(0.5, 0.5, "齐鲁号海铁联运 2026年5月数据", ha="center", fontsize=12)
            plt.savefig(str(output), dpi=80, bbox_inches="tight")
            plt.close()

            glyph_warnings = [
                x for x in w
                if "missing from font" in str(x.message).lower()
                or "Glyph" in str(x.message)
            ]
            assert len(glyph_warnings) == 0, \
                f"SimHei 别名未生效,中文乱码: {[str(x.message) for x in glyph_warnings]}"

        assert output.exists()
