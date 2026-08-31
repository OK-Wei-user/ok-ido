#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2025/05/15 12:20

@File    : file.py
"""
import logging
import posixpath
from typing import Any, Dict, List, Optional

from app.domain.external.sandbox import Sandbox
from app.domain.models.tool_result import ToolResult
from .base import BaseTool, tool

logger = logging.getLogger(__name__)


# === find_files 路径验证常量 ===
# 系统目录黑名单：禁止从这些目录发起扫描
# 根因：会话 f2611353 卡死 — LLM 调用 find_files(dir_path="/", glob_pattern="**/pptxgenjs.md")
# 导致 sandbox glob.glob('/**/...') 进入 /proc /sys 虚拟文件系统挂起
_FORBIDDEN_SCAN_ROOTS = frozenset({
    "/", "/proc", "/sys", "/dev", "/boot", "/etc", "/usr", "/var",
    "/run", "/snap", "/lib", "/lib32", "/lib64", "/libx32", "/sbin", "/bin",
})

# 允许扫描的工作区目录提示（用于错误消息引导 LLM 修正路径）
_ALLOWED_WORKSPACE_HINTS = "/home /workspace /sandbox /tmp /root /opt"


class FileTool(BaseTool):
    """文件工具箱"""
    name: str = "file"

    def __init__(self, sandbox: Sandbox) -> None:
        """构造函数，完成文件工具箱初始化"""
        super().__init__()
        self.sandbox = sandbox

    @staticmethod
    def _normalize_write_file_items(
            items: Optional[List[Dict[str, Any]]],
            filepath: Optional[str],
            content: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """规范化 write_file 调用参数 - 兼容 LLM 误传 items 数组格式

        LLM 偶尔会将 write_file 参数误传为:
            {"items": [{"filepath": "...", "content": "..."}]}
        而非标准的扁平格式:
            {"filepath": "...", "content": "..."}

        本方法从 items 数组中提取首个 filepath 并合并所有 content,
        与已传入的 filepath/content 取优先级(扁平参数优先)。

        Args:
            items: LLM 误传的 items 数组(可为 None)
            filepath: 已传入的 filepath(可为 None)
            content: 已传入的 content(可为 None)

        Returns:
            (filepath, content) 二元组,缺失时对应位置为 None
        """
        # 扁平参数优先,无需规范化
        if filepath and content is not None:
            return filepath, content

        # 无 items 数组,返回原值
        if not items or not isinstance(items, list):
            return filepath, content

        logger.warning(
            f"write_file收到items数组格式(非标准扁平参数),"
            f"自动规范化提取(filepath+content), items长度={len(items)}"
        )

        # 从 items 数组提取首个 filepath
        normalized_filepath = filepath
        if not normalized_filepath:
            for item in items:
                if isinstance(item, dict) and item.get("filepath"):
                    normalized_filepath = str(item["filepath"])
                    break

        # 合并所有 content(若扁平 content 已存在则优先,否则合并 items 中的 content)
        normalized_content = content
        if normalized_content is None:
            content_parts: List[str] = []
            for item in items:
                if isinstance(item, dict) and item.get("content") is not None:
                    content_parts.append(str(item["content"]))
            if content_parts:
                normalized_content = "\n".join(content_parts)

        return normalized_filepath, normalized_content

    @tool(
        name="read_file",
        description="读取文件内容。用于检查文件内容、分析日志或读取配置文件。",
        parameters={
            "filepath": {
                "type": "string",
                "description": "要读取文件的绝对路径"
            },
            "start_line": {
                "type": "integer",
                "description": "(可选)读取的起始行, 索引从 0 开始",
            },
            "end_line": {
                "type": "integer",
                "description": "(可选)结束行号, 不包含该行",
            },
            "sudo": {
                "type": "boolean",
                "description": "(可选)是否使用 sudo 权限",
            },
            "max_length": {
                "type": "integer",
                "description": "(可选)读取文件内容的最大长度, 默认为10000"
            }
        },
        required=["filepath"],
    )
    async def read_file(
            self,
            filepath: str,
            start_line: Optional[int] = None,
            end_line: Optional[int] = None,
            sudo: Optional[bool] = False,
            max_length: int = 10000,
    ) -> ToolResult:
        """传递文件路径读取沙箱中的文件内容"""
        return await self.sandbox.read_file(
            filepath=filepath,
            start_line=start_line,
            end_line=end_line,
            sudo=sudo,
            max_length=max_length,
        )

    @tool(
        name="write_file",
        description="对文件进行覆盖或追加写入。用于创建新文件、追加内容或修改现有文件。",
        parameters={
            "filepath": {
                "type": "string",
                "description": "要写入文件的绝对路径"
            },
            "content": {
                "type": "string",
                "description": "要写入的文本内容"
            },
            "append": {
                "type": "boolean",
                "description": "(可选)是否使用追加模式"
            },
            "leading_newline": {
                "type": "boolean",
                "description": "(可选)是否添加前置换行符, 在内容开头"
            },
            "trailing_newline": {
                "type": "boolean",
                "description": "(可选)是否添加后置换行符, 在内容结尾"
            },
            "sudo": {
                "type": "boolean",
                "description": "(可选)是否使用 sudo 权限"
            }
        },
        required=["filepath", "content"]
    )
    async def write_file(
            self,
            filepath: Optional[str] = None,
            content: Optional[str] = None,
            append: Optional[bool] = False,
            leading_newline: Optional[bool] = False,
            trailing_newline: Optional[bool] = False,
            sudo: Optional[bool] = False,
            # items 参数兼容 LLM 误传 {"items":[{"filepath":...,"content":...}]} 数组格式
            # 故意不加入 @tool parameters 字典,避免在 schema 中暴露诱导 LLM
            items: Optional[List[Dict[str, Any]]] = None,
    ) -> ToolResult:
        """写入文件 - 兼容扁平参数和 items 数组两种格式

        标准调用: write_file(filepath="/path/to/file", content="文本内容")
        兼容调用: write_file(items=[{"filepath": "/path/to/file", "content": "文本内容"}])

        缺失 filepath 或 content 时返回 ToolResult(success=False),
        避免抛出 "missing required positional arguments" 导致会话中断。
        """
        # 规范化参数 - 兼容 items 数组格式
        normalized_filepath, normalized_content = self._normalize_write_file_items(
            items=items, filepath=filepath, content=content
        )

        # 缺失必填参数时返回失败结果而非抛异常,避免会话中断
        if not normalized_filepath:
            error_msg = (
                "write_file 缺少必填参数 filepath。"
                "请使用扁平参数结构调用: write_file(filepath=\"/path/to/file\", content=\"内容\"),"
                "严禁使用 {\"items\":[...]} 数组格式包裹参数。"
            )
            logger.warning(f"write_file调用失败: 缺少filepath (items={'有' if items else '无'})")
            return ToolResult(success=False, message=error_msg, data=None)

        if normalized_content is None:
            error_msg = (
                "write_file 缺少必填参数 content。"
                "请使用扁平参数结构调用: write_file(filepath=\"/path/to/file\", content=\"内容\"),"
                "严禁使用 {\"items\":[...]} 数组格式包裹参数。"
            )
            logger.warning(f"write_file调用失败: 缺少content (filepath={normalized_filepath})")
            return ToolResult(success=False, message=error_msg, data=None)

        return await self.sandbox.write_file(
            filepath=normalized_filepath,
            content=normalized_content,
            append=append,
            leading_newline=leading_newline,
            trailing_newline=trailing_newline,
            sudo=sudo,
        )

    @tool(
        name="replace_in_file",
        description="在文件中替换指定的字符串。用于更新文件中的特定内容或修复代码中的错误。",
        parameters={
            "filepath": {
                "type": "string",
                "description": "要执行替换操作的文件的绝对路径"
            },
            "old_str": {
                "type": "string",
                "description": "要被替换的原始字符串"
            },
            "new_str": {
                "type": "string",
                "description": "用于替换的新字符串"
            },
            "sudo": {
                "type": "boolean",
                "description": "(可选)是否使用 sudo 权限"
            }
        },
        required=["filepath", "old_str", "new_str"]
    )
    async def replace_in_file(
            self,
            filepath: str,
            old_str: str,
            new_str: str,
            sudo: Optional[bool] = False
    ) -> ToolResult:
        return await self.sandbox.replace_in_file(
            filepath=filepath,
            old_str=old_str,
            new_str=new_str,
            sudo=sudo,
        )

    @tool(
        name="search_in_file",
        description="在文件内容中搜索匹配的文本。用于查找文件中的特定内容或模式。",
        parameters={
            "filepath": {
                "type": "string",
                "description": "要进行搜索的文件的绝对路径"
            },
            "regex": {
                "type": "string",
                "description": "用于匹配的正则表达式模式"
            },
            "sudo": {
                "type": "boolean",
                "description": "(可选)是否使用 sudo 权限"
            }
        },
        required=["filepath", "regex"]
    )
    async def search_in_file(
            self,
            filepath: str,
            regex: str,
            sudo: Optional[bool] = False
    ) -> ToolResult:
        return await self.sandbox.search_in_file(
            filepath=filepath,
            regex=regex,
            sudo=sudo,
        )

    @tool(
        name="find_files",
        description=(
            "在指定目录中根据名称模式查找文件。用于定位具有特定命名模式的文件。"
            "禁止扫描系统目录(/ /proc /sys /dev /etc /usr /var 等)，请在工作区目录内搜索。"
        ),
        parameters={
            "dir_path": {
                "type": "string",
                "description": "要搜索的目录的绝对路径(禁止使用 / /proc /sys /dev /etc 等系统目录)"
            },
            "glob_pattern": {
                "type": "string",
                "description": "使用 glob 语法通配符的文件名模式"
            }
        },
        required=["dir_path", "glob_pattern"]
    )
    async def find_files(
            self,
            dir_path: str,
            glob_pattern: str
    ) -> ToolResult:
        """查找文件 — 三层防护之工具层

        1. 路径验证：拒绝系统目录扫描，避免 sandbox glob 进入 /proc /sys 挂起
        2. 转发至 sandbox(自带 15s/30s glob 超时 + HTTP 30s 超时)
        3. 空结果引导：返回引导消息，避免 LLM 错误升级路径
        """
        # 1.路径验证：规范化后与黑名单比对，兼容尾部斜杠和相对路径
        #    使用 posixpath 而非 os.path，因为 dir_path 始终是 Linux 沙箱内的绝对路径
        #    (在 Windows 主机运行单元测试时，os.path.normpath('/') 会返回 '\' 导致比对失败)
        normalized_path = posixpath.normpath(dir_path) if dir_path else "/"
        if normalized_path in _FORBIDDEN_SCAN_ROOTS:
            logger.warning(
                f"find_files拒绝扫描系统目录: dir_path={dir_path}, pattern={glob_pattern}"
            )
            return ToolResult(
                success=False,
                message=(
                    f"禁止扫描系统目录[{dir_path}]，系统目录包含虚拟文件系统(/proc /sys)和"
                    f"操作系统文件，扫描会导致挂起。请使用工作区目录({_ALLOWED_WORKSPACE_HINTS})。"
                    f"若需查找特定文件，请提供更具体的工作区路径。"
                ),
            )

        # 2.转发至 sandbox 执行
        result = await self.sandbox.find_files(
            dir_path=dir_path,
            glob_pattern=glob_pattern,
        )

        # 3.空结果引导：成功但未找到文件时，附加引导消息帮助 LLM 修正策略
        if result.success:
            files = result.data.get("files") if isinstance(result.data, dict) else None
            if not files:
                result.message = (
                    f"在[{dir_path}]中未找到匹配[{glob_pattern}]的文件。"
                    f"建议：1)确认路径是否正确 2)扩大搜索目录至父目录 3)使用 shell_execute "
                    f"执行 'find <dir> -name <pattern>' 获取更详细的搜索结果"
                )

        return result
