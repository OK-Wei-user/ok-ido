#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : sitecustomize.py
Python 启动时自动加载的站点定制模块。

核心职责:
- 在任意 Python 进程启动时自动配置 matplotlib 中文字体
- 覆盖 shell_execute(python3 script.py) 与 python_kernel(fork) 两条执行路径
- 确保 LLM 生成的 Python 脚本无论通过何种方式执行,中文图表都能正常显示

安装位置:
- /usr/local/lib/python3.10/dist-packages/sitecustomize.py
  (Python 解释器启动时自动 import,无需脚本显式调用)

设计要点:
- 自包含: 不依赖项目模块(app.core),纯标准库 + matplotlib,适配 site-packages 部署
- 延迟配置: 仅当 matplotlib 被 import 时触发,避免无 matplotlib 场景的开销
- 异常隔离: 配置失败不影响 Python 进程正常启动
- 幂等: 全局 _CONFIGURED 标记,重复触发直接返回
- 字体别名: 注册 SimHei/Microsoft YaHei 等 Windows 字体名为 WenQuanYi Micro Hei 别名,
  防御 LLM 硬编码 `plt.rcParams['font.sans-serif'] = ['SimHei']` 导致中文乱码
"""
import glob
import importlib.abc
import importlib.machinery
import logging
import os
import sys
import threading

# 独立 logger(不依赖项目日志配置,避免循环导入)
_logger = logging.getLogger("sitecustomize")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[%(name)s] %(levelname)s: %(message)s"))
    _logger.addHandler(_handler)
_logger.setLevel(logging.WARNING)  # 仅 WARNING+ 输出,避免污染脚本输出

_LOCK = threading.Lock()
_CONFIGURED = False

# CJK 字体候选名(按优先级排序)
_CJK_FONT_CANDIDATES = [
    "WenQuanYi Micro Hei",
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",
    "SimHei",  # 别名,通过 _register_font_aliases 注册
]

# TTC/OTF/TTF 字体文件 glob 模式
_CJK_FONT_PATTERNS = [
    "/usr/share/fonts/**/NotoSansCJK*.ttc",
    "/usr/share/fonts/**/NotoSerifCJK*.ttc",
    "/usr/share/fonts/**/*CJK*.ttf",
    "/usr/share/fonts/**/*CJK*.otf",
    "/usr/share/fonts/**/wqy*.ttc",
    "/usr/share/fonts/**/wqy*.ttf",
]

# Windows/macOS 常用中文字体名 → LLM 训练数据中高频出现的字体名
_FONT_ALIASES = [
    "SimHei", "SimSun", "Microsoft YaHei", "KaiTi", "FangSong",
    "STKaiti", "STSong", "STHeiti", "Arial Unicode MS", "PingFang SC",
]


def _register_ttc_fonts() -> set:
    """扫描并注册系统 TTC/OTF/TTF 中文字体文件"""
    import matplotlib.font_manager as fm

    font_files = set()
    for pattern in _CJK_FONT_PATTERNS:
        font_files.update(glob.glob(pattern, recursive=True))

    for font_file in sorted(font_files):
        try:
            fm.fontManager.addfont(font_file)
        except Exception as e:
            _logger.debug(f"注册字体文件失败 {font_file}: {e}")

    return {f.name for f in fm.fontManager.ttflist}


def _find_actual_cjk_font_path() -> str:
    """查找实际可用的 CJK 字体文件路径(优先 WenQuanYi Micro Hei)"""
    for pattern in [
        "/usr/share/fonts/**/wqy-microhei.ttc",
        "/usr/share/fonts/**/wqy*.ttf",
        "/usr/share/fonts/**/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/**/NotoSansCJK*.ttc",
    ]:
        files = glob.glob(pattern, recursive=True)
        if files:
            return files[0]
    return ""


def _register_font_aliases(actual_font_path: str) -> None:
    """将 Windows/macOS 常用中文字体名注册为实际字体文件的别名

    防御 LLM 硬编码 `plt.rcParams['font.sans-serif'] = ['SimHei']` 导致乱码:
    SimHei 是 Windows 字体,Linux 容器中不存在。通过注册别名,
    使 SimHei 解析到实际安装的 WenQuanYi Micro Hei。
    """
    import matplotlib.font_manager as fm

    if not os.path.exists(actual_font_path):
        return

    existing_names = {f.name for f in fm.fontManager.ttflist}
    for alias in _FONT_ALIASES:
        if alias in existing_names:
            continue
        try:
            entry = fm.FontEntry(fname=actual_font_path, name=alias)
            fm.fontManager.ttflist.append(entry)
        except Exception:
            pass


def _configure_matplotlib_fonts() -> None:
    """配置 matplotlib 中文字体(幂等,线程安全)"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    with _LOCK:
        if _CONFIGURED:
            return
        try:
            import matplotlib
            import matplotlib.font_manager as fm

            matplotlib.rcParams["axes.unicode_minus"] = False

            available_fonts = {f.name for f in fm.fontManager.ttflist}
            if not any(f in available_fonts for f in _CJK_FONT_CANDIDATES):
                available_fonts = _register_ttc_fonts()

            # 注册 Windows/macOS 字体别名(防御 LLM 硬编码 SimHei)
            actual_font_path = _find_actual_cjk_font_path()
            if actual_font_path:
                _register_font_aliases(actual_font_path)
                available_fonts = {f.name for f in fm.fontManager.ttflist}

            cjk_font = next(
                (f for f in _CJK_FONT_CANDIDATES if f in available_fonts), None
            )
            if cjk_font:
                matplotlib.rcParams["font.sans-serif"] = [cjk_font] + list(
                    matplotlib.rcParams.get("font.sans-serif", [])
                )
                matplotlib.rcParams["font.family"] = "sans-serif"

            # 约束默认图片尺寸(适配 Word A4 嵌入)
            matplotlib.rcParams["figure.figsize"] = [10, 6]
            matplotlib.rcParams["figure.dpi"] = 100
            matplotlib.rcParams["savefig.dpi"] = 100
            matplotlib.rcParams["figure.autolayout"] = True

        except ImportError:
            pass  # matplotlib 未安装,跳过
        except Exception as e:
            _logger.debug(f"matplotlib 字体配置失败: {e}")
        finally:
            _CONFIGURED = True


class _MatplotlibImportHook(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """matplotlib 导入钩子

    当 Python 首次 import matplotlib 时,触发中文字体自动配置。
    使用 sys.meta_path 前置注册,确保在其他 finder 之前拦截。
    """

    def find_spec(self, fullname, path, target=None):
        """仅拦截 matplotlib 顶层模块的导入"""
        if fullname != "matplotlib":
            return None
        # 委托给默认 finder 加载,在加载完成后触发配置
        for finder in sys.meta_path:
            if finder is self:
                continue
            try:
                spec = finder.find_spec(fullname, path, target)
            except (AttributeError, ImportError, TypeError):
                continue
            if spec is not None:
                spec.loader = _WrappedLoader(spec.loader)
                return spec
        return None


class _WrappedLoader(importlib.abc.Loader):
    """包装原 loader,在 matplotlib 模块加载完成后触发字体配置"""

    def __init__(self, original_loader):
        self._original = original_loader

    def create_module(self, spec):
        if hasattr(self._original, "create_module"):
            return self._original.create_module(spec)
        return None

    def exec_module(self, module):
        if hasattr(self._original, "exec_module"):
            self._original.exec_module(module)
        # matplotlib 加载完成后触发字体配置
        _configure_matplotlib_fonts()

    def load_module(self, fullname):
        """兼容旧式 loader 接口"""
        if hasattr(self._original, "load_module"):
            module = self._original.load_module(fullname)
            _configure_matplotlib_fonts()
            return module
        raise ImportError(f"无法加载 {fullname}")


def _install_hook() -> None:
    """安装 matplotlib 导入钩子"""
    try:
        if any(isinstance(f, _MatplotlibImportHook) for f in sys.meta_path):
            return
        sys.meta_path.insert(0, _MatplotlibImportHook())
    except Exception:
        pass  # 安装失败静默降级


# 模块加载时立即安装钩子(Python 启动时自动执行)
_install_hook()

# 兜底: 若 matplotlib 已被导入(如 fork 子进程继承主进程 sys.modules),
# 导入钩子不会再次触发,需立即配置
if "matplotlib" in sys.modules:
    _configure_matplotlib_fonts()
