#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : filename_utils.py
文件名处理工具 - 统一处理文件名的编码、解码和格式化，支持中文文件名
"""
import re
import urllib.parse


class FilenameUtils:

    @staticmethod
    def fix_encoding(filename: str) -> str:
        if not filename:
            return filename
        try:
            raw = filename.encode("latin-1")
            try:
                decoded = raw.decode("utf-8")
                return decoded
            except UnicodeDecodeError:
                pass
            try:
                decoded = raw.decode("gbk")
                return decoded
            except UnicodeDecodeError:
                pass
            try:
                decoded = raw.decode("gb18030")
                return decoded
            except UnicodeDecodeError:
                pass
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return filename

    @staticmethod
    def decode_filename(filename: str) -> str:
        if not filename:
            return filename
        try:
            return urllib.parse.unquote(filename)
        except Exception:
            return filename

    @staticmethod
    def encode_filename_for_download(filename: str) -> str:
        if not filename:
            return filename
        return urllib.parse.quote(filename)

    @staticmethod
    def sanitize_filename(filename: str, replacement: str = "_") -> str:
        if not filename:
            return filename
        illegal_chars = r'[<>:"/\\|?*\x00-\x1f]'
        sanitized = re.sub(illegal_chars, replacement, filename)
        sanitized = re.sub(r"_+", "_", sanitized)
        sanitized = sanitized.strip("._")
        return sanitized

    @staticmethod
    def normalize_filename(filename: str) -> str:
        if not filename:
            return filename
        fixed = FilenameUtils.fix_encoding(filename)
        decoded = FilenameUtils.decode_filename(fixed)
        return FilenameUtils.sanitize_filename(decoded)

    @staticmethod
    def split_extension(filename: str) -> tuple:
        if not filename:
            return ("", "")
        if "." in filename:
            parts = filename.rsplit(".", 1)
            return (parts[0], "." + parts[1])
        return (filename, "")

    @staticmethod
    def get_display_filename(filename: str, max_length: int = 40) -> str:
        if not filename:
            return filename
        decoded = FilenameUtils.decode_filename(filename)
        if len(decoded) <= max_length:
            return decoded
        name, ext = FilenameUtils.split_extension(decoded)
        available_length = max_length - len(ext) - 3
        if available_length > 0:
            return name[:available_length] + "..." + ext
        return decoded[: max_length - 3] + "..."


class OSSExtensionMapper:

    ALLOWED_EXTENSIONS = frozenset({
        ".bmp", ".gif", ".jpg", ".jpeg", ".png", ".doc", ".docx",
        ".xls", ".xlsx", ".ppt", ".pptx", ".html", ".htm", ".txt",
        ".rar", ".zip", ".gz", ".bz2", ".apk", ".xml", ".edi",
        ".pms", ".pdf", ".swf", ".flv", ".mp3", ".wav", ".wma",
        ".wmv", ".mid", ".avi", ".mpg", ".asf", ".rm", ".rmvb",
        ".mp4", ".webm", ".ogg", ".json", ".conf", ".blob",
    })

    EXTENSION_MAPPING = {
        ".md": ".txt",
        ".markdown": ".txt",
        ".py": ".txt",
        ".js": ".txt",
        ".ts": ".txt",
        ".css": ".txt",
        ".yaml": ".txt",
        ".yml": ".txt",
        ".toml": ".txt",
        ".ini": ".txt",
        ".cfg": ".txt",
        ".log": ".txt",
        ".sh": ".txt",
        ".bat": ".txt",
        ".sql": ".txt",
        ".r": ".txt",
        ".c": ".txt",
        ".cpp": ".txt",
        ".h": ".txt",
        ".java": ".txt",
        ".rb": ".txt",
        ".go": ".txt",
        ".rs": ".txt",
        ".swift": ".txt",
        ".kt": ".txt",
        ".scala": ".txt",
        ".tsv": ".txt",
        ".rtf": ".txt",
        ".svg": ".xml",
        ".graphql": ".txt",
        ".proto": ".txt",
        ".dockerfile": ".txt",
        ".env": ".txt",
        ".gitignore": ".txt",
        ".properties": ".txt",
        ".ipynb": ".json",
        ".tex": ".txt",
        ".ps1": ".txt",
    }

    @classmethod
    def map_extension(cls, ext: str) -> str:
        ext_lower = ext.lower()
        if ext_lower in cls.ALLOWED_EXTENSIONS:
            return ext_lower
        mapped = cls.EXTENSION_MAPPING.get(ext_lower)
        if mapped:
            return mapped
        return ".txt"

    @classmethod
    def is_extension_allowed(cls, ext: str) -> bool:
        return ext.lower() in cls.ALLOWED_EXTENSIONS
