#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : matplotlib_config.py
matplotlib 中文字体运行时配置。

核心能力:
1. 显式注册 TTC 字体文件,解决 matplotlib 缓存不识别问题
2. 注册 Windows 常用中文字体别名(SimHei/Microsoft YaHei 等),
   使 LLM 生成的脚本中 `plt.rcParams['font.sans-serif'] = ['SimHei']`
   也能正确解析到 WenQuanYi Micro Hei,避免中文乱码
3. 约束默认图片尺寸,避免 LLM 生成过大图片导致 Word 文档显示异常

设计要点:
- 幂等: 全局 _CONFIGURED 标记,重复调用直接返回
- 健壮: 字体注册失败不影响后续流程,降级到 matplotlibrc 默认配置
- 通用: 通过 sitecustomize.py 自动加载,覆盖 shell/python_kernel 所有执行路径
"""
import glob
import logging
import os
from typing import Set

logger = logging.getLogger(__name__)

# CJK 字体候选名(按优先级排序,第一个可用的将被设为默认)
_CJK_FONT_CANDIDATES = [
    "WenQuanYi Micro Hei",  # 独立 TTC 文件,matplotlib 稳定识别
    "Noto Sans CJK SC",     # 需 fontconfig 回退(SC 子字体不直接注册)
    "Noto Sans CJK JP",     # TTC 集合首个子字体,matplotlib 可直接识别
    "Noto Sans CJK TC",
    "SimHei",               # Windows 字体别名(通过 _register_font_aliases 注册)
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

# Windows/Office 常用中文字体名 → LLM 训练数据中高频出现的字体名
# 将这些别名映射到实际安装的 CJK 字体文件,避免 LLM 硬编码 SimHei 时乱码
_FONT_ALIASES = [
    "SimHei",           # 黑体(最常被 LLM 引用)
    "SimSun",           # 宋体
    "Microsoft YaHei",  # 微软雅黑
    "KaiTi",            # 楷体
    "FangSong",         # 仿宋
    "STKaiti",          # 华文楷体
    "STSong",           # 华文宋体
    "STHeiti",          # 华文黑体
    "Arial Unicode MS", # macOS 常用
    "PingFang SC",      # macOS 苹方
]

# 图片默认尺寸约束 - 适配 Word A4 页面嵌入(避免图片过大导致显示异常)
# A4 页面可用宽度约 16cm(约 6.3inch),figsize=(10,6) 在 DPI=100 下生成 1000x600px,
# 嵌入 Word 后缩放至页面宽度,显示清晰且不溢出
_DEFAULT_FIG_SIZE = (10, 6)
_DEFAULT_DPI = 100
_DEFAULT_SAVEFIG_DPI = 100

_CONFIGURED = False


def _register_ttc_fonts() -> Set[str]:
    """扫描并注册系统 TTC/OTF/TTF 中文字体文件,返回注册后的可用字体名集合

    Returns:
        已注册字体的名称集合
    """
    import matplotlib.font_manager as fm

    font_files: Set[str] = set()
    for pattern in _CJK_FONT_PATTERNS:
        font_files.update(glob.glob(pattern, recursive=True))

    for font_file in sorted(font_files):
        try:
            fm.fontManager.addfont(font_file)
            logger.debug(f"已注册字体文件: {font_file}")
        except Exception as e:
            logger.warning(f"注册字体文件失败 {font_file}: {e}")

    return {f.name for f in fm.fontManager.ttflist}


def _register_font_aliases(actual_font_path: str) -> None:
    """将 Windows/macOS 常用中文字体名注册为实际字体文件的别名

    LLM 训练数据中大量中文 matplotlib 教程使用 `plt.rcParams['font.sans-serif'] = ['SimHei']`,
    但 SimHei 是 Windows 字体,Linux 容器中不存在。通过注册别名,
    使这些字体名能解析到实际安装的 WenQuanYi Micro Hei / Noto Sans CJK。

    Args:
        actual_font_path: 实际字体文件路径(如 wqy-microhei.ttc)
    """
    import matplotlib.font_manager as fm

    if not os.path.exists(actual_font_path):
        logger.warning(f"字体别名注册跳过: 文件不存在 {actual_font_path}")
        return

    registered_count = 0
    existing_names = {f.name for f in fm.fontManager.ttflist}
    for alias in _FONT_ALIASES:
        # 跳过已存在的字体名(避免重复注册覆盖真实字体)
        if alias in existing_names:
            continue
        try:
            # FontEntry 直接追加到 ttflist,绕过 addfont 的字体名解析
            entry = fm.FontEntry(fname=actual_font_path, name=alias)
            fm.fontManager.ttflist.append(entry)
            registered_count += 1
            logger.debug(f"已注册字体别名: {alias} -> {actual_font_path}")
        except Exception as e:
            logger.warning(f"注册字体别名失败 {alias}: {e}")

    if registered_count:
        logger.info(f"已注册 {registered_count} 个 Windows/macOS 中文字体别名")


def _find_actual_cjk_font_path() -> str:
    """查找实际可用的 CJK 字体文件路径(优先 WenQuanYi Micro Hei)

    Returns:
        字体文件路径,未找到时返回空字符串
    """
    # 优先 WenQuanYi Micro Hei(独立 TTC,matplotlib 稳定识别)
    wqy_patterns = [
        "/usr/share/fonts/**/wqy-microhei.ttc",
        "/usr/share/fonts/**/wqy*.ttf",
    ]
    for pattern in wqy_patterns:
        files = glob.glob(pattern, recursive=True)
        if files:
            return files[0]

    # 次选 Noto Sans CJK
    noto_patterns = [
        "/usr/share/fonts/**/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/**/NotoSansCJK*.ttc",
    ]
    for pattern in noto_patterns:
        files = glob.glob(pattern, recursive=True)
        if files:
            return files[0]

    return ""


def setup_figure_defaults() -> None:
    """配置 matplotlib 默认图片尺寸与 DPI,避免 LLM 生成过大图片导致 Word 显示异常

    约束项:
    - figure.figsize: 默认 (10,6),适配 Word A4 页面宽度
    - figure.dpi: 默认 100,平衡清晰度与文件大小
    - savefig.dpi: 默认 100,确保保存的图片尺寸可控
    - figure.autolayout: True,自动调整子图边距避免标签溢出
    """
    import matplotlib

    matplotlib.rcParams["figure.figsize"] = list(_DEFAULT_FIG_SIZE)
    matplotlib.rcParams["figure.dpi"] = _DEFAULT_DPI
    matplotlib.rcParams["savefig.dpi"] = _DEFAULT_SAVEFIG_DPI
    matplotlib.rcParams["figure.autolayout"] = True
    logger.debug(
        f"matplotlib 图片尺寸已约束: figsize={_DEFAULT_FIG_SIZE}, dpi={_DEFAULT_DPI}"
    )


def setup_chinese_fonts() -> None:
    """运行时配置 matplotlib 中文字体,确保图表中文正常显示

    执行流程:
    1. 设置 axes.unicode_minus=False(避免负号显示为方块)
    2. 检查 fontManager 是否有 CJK 字体,没有则扫描注册 TTC 文件
    3. 注册 Windows/macOS 字体别名(SimHei 等),防御 LLM 硬编码非 Linux 字体
    4. 设置 font.sans-serif 优先级列表
    5. 约束默认图片尺寸

    幂等性: 全局 _CONFIGURED 标记,重复调用直接返回
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    try:
        import matplotlib
        import matplotlib.font_manager as fm

        matplotlib.rcParams["axes.unicode_minus"] = False

        available_fonts = {f.name for f in fm.fontManager.ttflist}

        # 若 fontManager 未识别 CJK 字体,扫描注册 TTC 文件
        if not any(f in available_fonts for f in _CJK_FONT_CANDIDATES):
            available_fonts = _register_ttc_fonts()

        # 注册 Windows/macOS 字体别名(防御 LLM 硬编码 SimHei 等)
        actual_font_path = _find_actual_cjk_font_path()
        if actual_font_path:
            _register_font_aliases(actual_font_path)
            # 别名注册后刷新可用字体集合
            available_fonts = {f.name for f in fm.fontManager.ttflist}

        # 选择首个可用的 CJK 字体作为默认
        cjk_font = next(
            (f for f in _CJK_FONT_CANDIDATES if f in available_fonts), None
        )

        if cjk_font:
            matplotlib.rcParams["font.sans-serif"] = [cjk_font] + list(
                matplotlib.rcParams.get("font.sans-serif", [])
            )
            matplotlib.rcParams["font.family"] = "sans-serif"
            logger.info(f"matplotlib 中文字体已配置: {cjk_font}")
        else:
            logger.warning("未找到 CJK 中文字体,图表中文可能无法正常显示")

        # 约束默认图片尺寸,避免 LLM 生成过大图片导致 Word 文档显示异常
        setup_figure_defaults()

        _CONFIGURED = True
    except ImportError:
        logger.debug("matplotlib 未安装,跳过中文字体配置")
    except Exception as e:
        logger.warning(f"matplotlib 中文字体配置失败: {e}")


def reset_config_state() -> None:
    """重置配置状态标记(仅测试用)

    允许测试用例重复调用 setup_chinese_fonts() 验证不同场景
    """
    global _CONFIGURED
    _CONFIGURED = False
