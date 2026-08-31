#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : accessibility_snapshot.py
无障碍树驱动的元素引用(ref)机制 - 基于浏览器原生 accessibility tree 为可交互元素
分配稳定语义引用(@e1/@e2)，替代易漂移的纯DOM索引。

设计要点:
1. ref 与现有 index 共用底层 selector(data-manus-id)，保证向后兼容；
2. accessibility tree 仅作语义增强(role/name)，失败时退化到 interactive_elements 的 semanticAttrs；
3. resolve_ref 失败时返回 None，由调用方回退到 text/index 定位；
4. ref_map 在导航/刷新后重建，避免引用过期导致误操作。
"""
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# 可交互的 ARIA role 集合，用于 accessibility tree 节点过滤
_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "checkbox", "radio", "menuitem",
    "menuitemcheckbox", "menuitemradio", "tab", "option", "treeitem",
    "combobox", "searchbox", "spinbutton", "slider", "switch", "menu",
})

# accessibility tree 最大节点数(防止巨型页面膨胀)
_MAX_A11Y_NODES = 200

# 可见元素安全上限: 仅防极端页面(10000+元素)导致JSON爆炸,正常页面永不触及。
# 架构原则(会话9b0bf463根因修复): 源头不再硬限流可见元素数量,由memory.py的
# truncate_tool_result_dynamic基于剩余token预算动态截断统一控制。旧版_MAX_VISIBLE_ELEMENTS=80
# 在源头截断295元素的文档页,导致"215 more visible elements omitted",LLM看不全关键表格内容。
# 动态截断已实现上下文感知双向缩放(2x/1.5x/标准/0.25x),配合三级优先级排序(状态标记>
# 对话框>普通)确保截断时关键元素优先保留。安全上限2000覆盖所有真实页面(复杂文档页/表格页),
# 仅恶意页面才会触及,此时添加提示引导text_locator定位。
_VISIBLE_ELEMENTS_SAFETY_CEILING = 2000

# format_refs_for_llm 可见元素ref上限: 与interactive_elements保持一致,避免"能看到
# 元素描述却无ref可操作"的不一致(会话d03f7b01根因)。使用同一安全上限,保证ref覆盖完整。
_DEFAULT_MAX_REF_ELEMENTS = _VISIBLE_ELEMENTS_SAFETY_CEILING

# offscreen元素最大展示数: 超出部分仅汇总计数,避免上下文膨胀
# 会话1146286e根因: 30+offscreen菜单项充斥ref_map,LLM无法快速定位可见目标元素
_OFFSCREEN_MAX_DISPLAY = 15


async def build_ref_map(page: Any, interactive_elements: List[dict]) -> Dict[str, dict]:
    """为可交互元素构建 ref 映射表。

    Args:
        page: Playwright Page 实例
        interactive_elements: _extract_interactive_elements 返回的元素列表

    Returns:
        {ref: {selector, tag, text, role, name, inViewport}} 映射表；
        interactive_elements 为空时返回空 dict。
    """
    ref_map: Dict[str, dict] = {}
    if not interactive_elements:
        return ref_map

    # 尝试 accessibility tree 语义增强(失败不影响主流程)
    a11y_names = await _collect_accessibility_names(page)

    for el in interactive_elements:
        idx = el.get("index", 0)
        ref = f"@e{idx}"
        semantic_attrs = el.get("semanticAttrs") or {}
        tag = el.get("tag", "")
        text = el.get("text", "")

        # 语义来源优先级: semanticAttrs > accessibility tree 匹配 > 空值
        role = semantic_attrs.get("role", "")
        name = semantic_attrs.get("aria-label", "") or semantic_attrs.get("title", "")

        # 用 accessibility tree 补充缺失的 role/name(按 tag+text 模糊匹配)
        if (not role or not name) and a11y_names:
            a11y = _match_a11y_node(tag, text, a11y_names)
            if a11y:
                role = role or a11y.get("role", "")
                name = name or a11y.get("name", "")

        ref_map[ref] = {
            "selector": el.get("selector", ""),
            "tag": tag,
            "text": text,
            "role": role,
            "name": name,
            "inViewport": el.get("inViewport", True),
            "state": el.get("state"),
        }

    return ref_map


async def _collect_accessibility_names(page: Any) -> List[dict]:
    """提取 accessibility tree 中的可交互节点信息，用于语义增强。

    巨型页面可能产生大量节点，截断到 _MAX_A11Y_NODES 防止上下文膨胀。
    """
    try:
        snapshot = await page.accessibility.snapshot()
        if not snapshot:
            return []
        nodes: List[dict] = []
        _flatten_a11y(snapshot, nodes, _MAX_A11Y_NODES)
        return [n for n in nodes if n.get("role") in _INTERACTIVE_ROLES]
    except Exception as e:
        logger.debug(f"accessibility tree 提取失败(不影响主流程): {str(e)}")
        return []


def _flatten_a11y(node: dict, nodes: List[dict], max_nodes: int) -> None:
    """递归拍平 accessibility 快照为节点列表，保留 role/name/value 字段"""
    if not isinstance(node, dict) or len(nodes) >= max_nodes:
        return
    role = node.get("role", "")
    if role:
        nodes.append({
            "role": role,
            "name": node.get("name", ""),
            "value": node.get("value", ""),
        })
    for child in node.get("children", []) or []:
        _flatten_a11y(child, nodes, max_nodes)


def _match_a11y_node(tag: str, text: str, a11y_names: List[dict]) -> Optional[dict]:
    """根据 tag+text 在 accessibility 节点中模糊匹配，返回首个命中节点"""
    if not text:
        return None
    text_lower = text.lower().strip()
    for node in a11y_names:
        name = (node.get("name") or "").lower().strip()
        if name and (name == text_lower or name in text_lower or text_lower in name):
            return node
    return None


async def resolve_ref(page: Any, ref: str, ref_map: Dict[str, dict]) -> Optional[Any]:
    """通过 ref 解析为 Playwright ElementHandle。

    Args:
        page: Playwright Page 实例
        ref: 元素引用，如 "@e5"
        ref_map: build_ref_map 生成的映射表

    Returns:
        ElementHandle 或 None(ref 不存在、selector 缺失或元素已失效)
    """
    info = ref_map.get(ref)
    if not info:
        logger.debug(f"ref[{ref}] 不在映射表中，可能已过期")
        return None
    selector = info.get("selector", "")
    if not selector:
        logger.debug(f"ref[{ref}] 缺少 selector，无法定位")
        return None
    try:
        return await page.query_selector(selector)
    except Exception as e:
        logger.debug(f"ref[{ref}] 解析失败: {str(e)}")
        return None


def format_refs_for_llm(
    ref_map: Dict[str, dict], max_elements: int = _DEFAULT_MAX_REF_ELEMENTS,
) -> List[str]:
    """格式化 ref 映射为 LLM 可读的文本列表(可见性优先)。

    可见元素优先展示,offscreen元素截断到 _OFFSCREEN_MAX_DISPLAY 个,
    超出部分汇总为计数提示,避免上下文膨胀影响LLM定位精度。

    可见元素安全上限(_VISIBLE_ELEMENTS_SAFETY_CEILING=2000)仅防极端页面,
    正常页面完整展示。实际展示量由memory.py动态截断基于上下文预算统一控制。

    格式: ``[@e1] role "name" <tag>text</tag> [offscreen]``
    """
    # 按可见性分区: 可见元素优先,offscreen元素限流
    visible_refs: List[tuple] = []
    offscreen_refs: List[tuple] = []
    for ref, info in ref_map.items():
        if info.get("inViewport", True):
            visible_refs.append((ref, info))
        else:
            offscreen_refs.append((ref, info))

    lines: List[str] = []

    # 1.可见元素: 展示(上限max_elements,超出添加提示引导text_locator定位,保证信息流转闭环)
    for ref, info in visible_refs[:max_elements]:
        lines.append(_format_single_ref(ref, info))
    visible_hidden = len(visible_refs) - max_elements
    if visible_hidden > 0:
        lines.append(
            f"... ({visible_hidden} more visible elements without ref, "
            f"use text_locator to locate them)"
        )

    # 2.offscreen元素: 分区展示,超出限流部分汇总计数
    offscreen_shown = offscreen_refs[:_OFFSCREEN_MAX_DISPLAY]
    if offscreen_shown:
        lines.append(f"--- offscreen elements ({len(offscreen_refs)} total, showing {len(offscreen_shown)}) ---")
        for ref, info in offscreen_shown:
            lines.append(_format_single_ref(ref, info, force_offscreen=True))
        hidden_count = len(offscreen_refs) - len(offscreen_shown)
        if hidden_count > 0:
            lines.append(f"... ({hidden_count} more offscreen elements below viewport, scroll to reveal)")

    return lines


def _format_single_ref(ref: str, info: dict, force_offscreen: bool = False) -> str:
    """格式化单个ref为LLM可读文本行"""
    role = info.get("role", "")
    name = info.get("name", "")
    tag = info.get("tag", "")
    text = info.get("text", "")
    in_vp = info.get("inViewport", True)

    parts = [f"[{ref}]"]
    if role:
        parts.append(role)
    if name:
        parts.append(f'"{name}"')
    if tag or text:
        parts.append(f"<{tag}>{text}</{tag}>")
    # 表单元素状态标记(供LLM判断radio/checkbox选中态)
    state = info.get("state") or {}
    if state.get("checked"):
        parts.append("[checked]")
    if state.get("selected"):
        parts.append("[selected]")
    if state.get("disabled"):
        parts.append("[disabled]")
    if not in_vp or force_offscreen:
        parts.append("[offscreen]")
    return " ".join(parts)
