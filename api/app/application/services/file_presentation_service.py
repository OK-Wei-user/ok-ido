#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : file_presentation_service.py
文件展示策略服务(F2-2抽离 + F2-3外置 + F10-8集中化校验)

职责:
- 文件去重: 按filepath去重,保留首次出现的文件记录
- 空文件过滤: 仅过滤已同步的空文件,PENDING文件保留避免误杀
- 优先级排序: 按文件类型重要性排序,核心交付文件优先
- 交付文件提取: 从会话事件中提取最终答案消息attachments的文件路径
- 过程文件识别: 识别明显的过程文件(脚本/日志/检查类),从交付列表中剔除
- 临时文件过滤: 基于扩展名排除.tmp/.log等临时文件(F10-8,集中化原MemoryConfig规则)
- 交付物智能选择: 集中化的类型过滤+截断(F10-8,替代PlannerReActFlow._get_relevant_files)
- 交付前校验清单: 完整性/空文件/重复/过程文件/临时文件五项校验+量化指标(F10-8)

设计原则:
- 配置外置: 所有模式与优先级通过FilePresentationConfig注入,运维可调整
- 单一职责: 仅关注文件展示策略,不涉及DB/沙箱操作
- 无状态: 所有方法为纯函数(静态方法),便于测试与复用
- 向后兼容: 默认配置与原SessionService硬编码值完全一致
- 规则集中化(F10-8): 交付物过滤规则单一数据源(FilePresentationConfig),避免双套规则不一致
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Set

from app.domain.models.app_config import FilePresentationConfig
from app.domain.models.event import MessageEvent
from app.domain.models.file import File
from app.domain.models.session import Session

logger = logging.getLogger(__name__)


@dataclass
class DeliveryValidationResult:
    """交付物校验结果(F10-8交付前校验清单)

    记录交付前校验清单的执行结果,包含有效附件列表与各类过滤统计,
    用于日志输出量化指标(命中率/过滤率),支持运维监控交付质量趋势。

    设计原则:
    - 不可变统计: total_declared/total_valid在校验完成后设置,运行中只追加filtered_*
    - 量化可观测: hit_rate属性提供命中率,log_report()输出单行报告便于日志聚合
    - 保留剔除明细: filtered_*保留被剔除的文件路径,便于排查LLM声明异常
    """
    valid_attachments: List[str]  # 校验通过的附件路径(已去重,保持原顺序)
    filtered_missing: List[str] = field(default_factory=list)  # 不在session.files中的附件(LLM声明但未生成)
    filtered_empty: List[str] = field(default_factory=list)  # size=0的SYNCED空文件
    filtered_duplicates: List[str] = field(default_factory=list)  # 重复声明的附件
    filtered_process: List[str] = field(default_factory=list)  # 识别为过程文件
    filtered_temp: List[str] = field(default_factory=list)  # 临时文件扩展名命中
    filtered_unsynced: List[str] = field(default_factory=list)  # 未同步到OSS(key为空或sync_status!=SYNCED,下载会失败)
    total_declared: int = 0  # LLM声明的附件总数
    total_valid: int = 0  # 校验通过的附件数

    @property
    def hit_rate(self) -> float:
        """命中率 = 有效附件数 / 声明总数

        0声明时返回1.0(无声明视为完全命中,避免除零告警)。
        """
        if self.total_declared == 0:
            return 1.0
        return self.total_valid / self.total_declared

    @property
    def total_filtered(self) -> int:
        """被过滤的附件总数"""
        return self.total_declared - self.total_valid

    def log_report(self) -> str:
        """生成校验报告日志(单行,便于日志聚合分析)

        输出格式: 声明=N, 有效=M, 命中率=XX.X%, 过滤: 缺失=X, 空文件=X, 重复=X, 过程文件=X, 临时文件=X, 未同步=X
        """
        return (
            f"声明={self.total_declared}, 有效={self.total_valid}, "
            f"命中率={self.hit_rate:.1%}, "
            f"过滤: 缺失={len(self.filtered_missing)}, "
            f"空文件={len(self.filtered_empty)}, "
            f"重复={len(self.filtered_duplicates)}, "
            f"过程文件={len(self.filtered_process)}, "
            f"临时文件={len(self.filtered_temp)}, "
            f"未同步={len(self.filtered_unsynced)}"
        )


class FilePresentationService:
    """文件展示策略服务

    从SessionService抽离(F2-2),所有模式与优先级通过FilePresentationConfig注入(F2-3)。
    F10-8集中化: 将原MemoryConfig的excluded_extensions/max_deliverable_files迁移至
    FilePresentationConfig,实现交付物过滤规则单一数据源。
    SessionService与PlannerReActFlow通过组合方式持有本服务实例,委托文件展示策略执行。
    """

    def __init__(self, config: FilePresentationConfig = None) -> None:
        """构造函数,完成文件展示策略服务初始化

        Args:
            config: 文件展示策略配置,None时使用默认配置(兼容老config.yaml)
        """
        # 未传config时使用默认配置,保证向后兼容
        self._config = config if config is not None else FilePresentationConfig()
        # F10-8: excluded_extensions转frozenset加速in查询(扩展名匹配高频调用)
        self._excluded_set: Set[str] = set(self._config.excluded_extensions)

    def present_files(self, session: Session, all_files: List[File]) -> List[File]:
        """根据交付策略对会话文件进行去重、过滤、分组与排序

        文件展示策略(解决交付文件不全问题):
        - 交付文件优先: 最终答案消息attachments中的文件排在前面
        - 中间产物过滤: 非交付文件中的明显过程文件(.py脚本/.txt切片
          及workspace/tmp目录下文件)不返回,避免过程文件污染交付列表
        - 辅助文件保留: 非过程类的非交付文件(如根目录.png图表/.html交付物)仍展示(排在后面)
        - 空文件过滤: SYNCED状态且size为0的文件不返回(写入失败或空文件)
        - PENDING文件保留: 尚未同步完成的文件保留(可能size暂为0)
        - 优先级排序: 每组内按文件类型重要性排序(xlsx>png>txt>py>log)

        HTML过滤策略(会话b30b3e14根因修复):
        原策略将.html/.htm一刀切过滤(批次51),误杀用户明确要求的HTML交付物。
        新策略: intermediate_extensions默认空列表,HTML由路径前缀独占过滤——
        /tmp/和/workspace/下的HTML分片仍被拦截,根目录HTML为合法交付物/辅助文件。

        Args:
            session: 会话对象(用于提取交付文件路径)
            all_files: 已经过滤空文件并去重后的文件列表

        Returns:
            排序后的文件列表(交付文件优先,辅助文件补全,过程文件已剔除)
        """
        delivered_paths = self.extract_delivered_file_paths(session)

        if not delivered_paths:
            # 无交付文件时,仍需过滤中间产物(避免纯过程文件场景污染列表)
            filtered = self._filter_process_files(all_files)
            return self.sort_files_by_priority(filtered)

        # 交付文件优先 + 辅助文件补全(过程文件已剔除)
        delivered = [f for f in all_files if f.filepath in delivered_paths]
        non_delivered = [f for f in all_files if f.filepath not in delivered_paths]
        # 批次51: 对非交付文件应用过程文件过滤,剔除明显中间产物
        non_delivered = self._filter_process_files(non_delivered)
        if non_delivered:
            logger.info(
                f"文件列表: {len(delivered)}个交付 + {len(non_delivered)}个辅助文件"
            )
        return self.sort_files_by_priority(delivered) + self.sort_files_by_priority(non_delivered)

    def _filter_process_files(self, files: List[File]) -> List[File]:
        """过滤明显的过程文件

        使用is_likely_process_file识别并剔除中间产物(.py脚本/
        .txt切片文件/workspace/tmp目录下文件),保留辅助文件(如根目录.png图表/.html交付物)。

        与filter_intermediate_files区别:
        - filter_intermediate_files: 仅按扩展名过滤(intermediate_extensions),用于select_deliverable_files
        - _filter_process_files: 综合路径/扩展名/文件名模式过滤,用于present_files

        Args:
            files: 待过滤的文件列表

        Returns:
            过滤后的文件列表(剔除明显过程文件,保留辅助文件)
        """
        result = []
        filtered_count = 0
        for f in files:
            if self.is_likely_process_file(f.filepath):
                filtered_count += 1
            else:
                result.append(f)
        if filtered_count > 0:
            logger.info(f"过程文件过滤: 剔除{filtered_count}个中间产物,保留{len(result)}个辅助文件")
        return result

    @staticmethod
    def deduplicate_files(files: List[File]) -> List[File]:
        """按filepath去重,保留首次出现的文件记录

        session.files可能因多次write_file或shell_execute生成同路径文件,
        产生多条File记录。去重确保文件列表API返回无冗余条目。
        """
        seen: Set[str] = set()
        result: List[File] = []
        for f in files:
            fp = f.filepath
            if fp not in seen:
                seen.add(fp)
                result.append(f)
        return result

    @staticmethod
    def filter_empty_files(files: List[File]) -> List[File]:
        """过滤大小为0的空文件(仅过滤已同步的空文件)

        SYNCED状态且size为0: 文件已同步但内容为空(写入失败或空文件),过滤
        PENDING状态且size为0: 文件尚未同步完成,size可能暂为0,保留避免误杀
        FAILED状态且size为0: 同步失败的空文件,过滤
        size>0的文件: 无论同步状态都保留
        """
        return [
            f for f in files
            if f.size > 0 or getattr(f, "sync_status", "SYNCED") == "PENDING"
        ]

    def sort_files_by_priority(self, files: List[File]) -> List[File]:
        """按文件类型重要性排序,核心交付文件优先

        排序规则(优先级降序):
        - xlsx/xls/csv(100/95): 数据分析核心交付物
        - docx/doc(90): 文档类交付物
        - pdf/pptx/ppt(85): 报告/演示类交付物
        - png/jpg/jpeg/gif(80/75): 图表类交付物
        - md(70): 文档类中间产物
        - txt(60): 文本类中间产物
        - 未知类型(40): 默认优先级
        - py/js/sh(30): 脚本类过程文件
        - log(20): 日志类过程文件
        同优先级按filename保持稳定排序(stable sort)。
        """
        priority_map = self._config.file_type_priority
        default_priority = self._config.default_file_priority

        def _priority(f: File) -> int:
            # 优先使用filename提取扩展名,兜底使用filepath
            name = f.filename or f.filepath or ""
            ext = os.path.splitext(name)[1].lower()
            return priority_map.get(ext, default_priority)

        return sorted(files, key=_priority, reverse=True)

    def extract_delivered_file_paths(self, session: Session) -> Set[str]:
        """从会话事件中提取最终答案消息attachments的文件路径集合

        最终答案消息(is_final=True, is_streaming=False)的attachments
        包含智能筛选出的最终交付物文件列表。

        过程文件智能过滤: 即使LLM误将过程文件填入attachments,也通过文件名模式
        识别并剔除,确保交付列表仅包含真正的交付物。被剔除的文件仍在文件列表中
        展示(作为非交付文件排在后面),不会丢失。
        """
        paths: Set[str] = set()
        for event in reversed(session.events):
            if isinstance(event, MessageEvent) and event.is_final and not event.is_streaming:
                for att in event.attachments:
                    if att.filepath:
                        paths.add(att.filepath)
                if paths:
                    break
        # 过程文件智能过滤: 剔除明显的过程文件,保留真正的交付物
        return {p for p in paths if not self.is_likely_process_file(p)}

    def is_likely_process_file(self, filepath: str) -> bool:
        """识别明显的过程文件(非交付物)

        判定规则(满足任一即视为过程文件,按优先级顺序):
        0. 路径前缀匹配intermediate_path_prefixes: workspace/tmp目录下的文件必定为过程文件
        1. 扩展名为log_extensions中的项(默认.log,日志文件必定为过程文件)
        2. 扩展名为intermediate_extensions(默认空列表,运维可按需配置)
        3. 扩展名为script_extensions(.py/.js)且文件名匹配script_name_patterns
        4. 扩展名为text_process_extension(.txt)且文件名匹配text_process_name_patterns

        HTML过滤策略变更(会话b30b3e14根因修复):
        - 原策略: .html/.htm在intermediate_extensions中一刀切过滤,导致用户明确要求的
          HTML交付物("输出html给我")被误杀,4层过滤闭环使HTML文件完全不可达
        - 新策略: intermediate_extensions默认空列表,HTML过滤改由intermediate_path_prefixes
          (路径前缀)独占——/tmp/和/workspace/下的HTML分片仍被路径过滤,根目录HTML为合法交付物
        - 安全网: planner_react.py自动填充逻辑中,HTML文件仅在LLM显式声明时交付,不自动填充

        注意: 保守过滤,仅剔除模式明确的过程文件,避免误杀用户真正需要的txt交付物。
        被识别为过程文件的仍会在文件列表中展示(排在后面),不会丢失。

        Args:
            filepath: 文件路径

        Returns:
            True表示该文件为过程文件,应从交付列表中剔除
        """
        name = os.path.basename(filepath or "")
        ext = os.path.splitext(name)[1].lower()
        name_lower = name.lower()
        filepath_lower = (filepath or "").lower()
        cfg = self._config

        # 0.路径过滤(批次51): workspace/tmp目录下的文件视为过程文件
        # 最可靠信号: 这些目录设计用于存放中间产物,根目录存放最终交付物
        if any(filepath_lower.startswith(p) for p in cfg.intermediate_path_prefixes):
            return True

        # 1.日志类文件必定为过程文件
        if ext in cfg.log_extensions:
            return True

        # 2.中间产物扩展名(默认空列表): 运维可按需配置需一刀切过滤的扩展名
        if ext in cfg.intermediate_extensions:
            return True

        # 3.脚本类过程文件: 扩展名匹配且文件名匹配分析/报告生成脚本模式
        if ext in cfg.script_extensions:
            if any(p in name_lower for p in cfg.script_name_patterns):
                return True

        # 4.文本类过程文件: 扩展名匹配且文件名匹配检查/预览/调试等模式
        if ext == cfg.text_process_extension:
            if any(p in name_lower for p in cfg.text_process_name_patterns):
                return True

        return False

    # ============================================================
    # F10-8 交付物过滤规则集中化(替代PlannerReActFlow._is_temp_file/_get_relevant_files)
    # ============================================================

    def is_excluded_file(self, filepath: str) -> bool:
        """判断是否为临时文件(按扩展名过滤,F10-8集中化)

        替代原PlannerReActFlow._is_temp_file,实现规则集中化。
        扩展名命中excluded_extensions即视为临时文件,从交付列表中剔除。
        与is_likely_process_file区别: 后者基于文件名模式(保守识别过程文件),
        本方法基于扩展名(明确剔除临时文件类型,无歧义)。

        Args:
            filepath: 文件路径

        Returns:
            True表示该文件为临时文件,应从交付列表中剔除
        """
        # 提取扩展名(与原_is_temp_file保持一致: 无扩展名时返回空串,不命中任何排除项)
        ext = "." + filepath.lower().rsplit(".", 1)[-1] if "." in filepath else ""
        return ext in self._excluded_set

    def filter_excluded_files(self, filepaths: List[str]) -> List[str]:
        """过滤临时文件(基于扩展名,F10-8集中化)

        Args:
            filepaths: 文件路径列表

        Returns:
            过滤后的文件路径列表(保持原顺序)
        """
        return [fp for fp in filepaths if not self.is_excluded_file(fp)]

    def filter_intermediate_files(self, filepaths: List[str]) -> List[str]:
        """过滤中间产物文件(基于扩展名)

        与filter_excluded_files区别:
        - filter_excluded_files: 过滤系统临时文件(.tmp/.log/.pyc)
        - filter_intermediate_files: 过滤AI生成的中间产物(默认空,运维可按需配置)

        HTML过滤策略变更(会话b30b3e14根因修复):
        intermediate_extensions默认改为空列表,不再一刀切过滤.html/.htm。
        原策略(批次51)将会话b6505eb7的网页抓取产物(gartner_ai.html)误交付问题
        通过扩展名过滤修复,但误杀了用户明确要求的HTML交付物。
        新策略: 路径前缀(intermediate_path_prefixes)独占中间产物拦截,
        /tmp/和/workspace/下的HTML分片由路径过滤,根目录HTML为合法交付物。

        Args:
            filepaths: 文件路径列表

        Returns:
            过滤后的文件路径列表(保持原顺序)
        """
        intermediate_set = set(self._config.intermediate_extensions)
        result = []
        for fp in filepaths:
            ext = "." + fp.lower().rsplit(".", 1)[-1] if "." in fp else ""
            if ext not in intermediate_set:
                result.append(fp)
        return result

    def select_deliverable_files(self, all_files: List[str]) -> List[str]:
        """交付物智能选择(F10-8集中化,替代PlannerReActFlow._get_relevant_files)

        设计理念(参考5b54ddc): 信任LLM,代码层仅做类型过滤+截断,
        交付质量完全由提示词优化驱动,不引入评分或分组等复杂逻辑。

        策略:
        1. 类型过滤: 排除excluded_extensions中的临时文件(.tmp/.log等)
        2. 中间产物过滤: 排除intermediate_extensions中的扩展名(默认空,不再一刀切过滤HTML)
        3. 截断: 超过max_deliverable_files时取最近N个(按列表末尾)

        兜底: 全部被过滤时返回原始列表,避免交付物为空导致用户无文件可下载。

        Args:
            all_files: 全量文件路径列表(通常来自session.files)

        Returns:
            筛选后的交付物文件路径列表
        """
        if not all_files:
            return []

        # 1.类型过滤: 排除临时文件
        filtered = self.filter_excluded_files(all_files)
        # 2.中间产物过滤: 排除intermediate_extensions中的扩展名(默认空,HTML由路径前缀过滤)
        filtered = self.filter_intermediate_files(filtered)
        if not filtered:
            # 兜底: 全部被过滤时返回原始列表(避免交付物为空)
            return all_files

        # 3.截断: 超过上限时取最近N个(按列表末尾,保留最近生成的文件)
        max_files = self._config.max_deliverable_files
        if len(filtered) > max_files:
            logger.info(f"交付物截断: {len(filtered)}个 → 最近{max_files}个")
            return filtered[-max_files:]

        return filtered

    def validate_deliverables(
        self,
        declared_attachments: List[str],
        session_files: List[File],
    ) -> DeliveryValidationResult:
        """交付物交付前校验清单(F10-8)

        在summarize阶段对LLM声明的attachments进行校验,确保交付物完整可用,
        并产出量化指标(命中率/过滤率)用于运维监控交付质量趋势。

        校验项(按顺序执行,任一不通过即从valid_attachments中剔除):
        1. 完整性: 文件必须真实存在于session.files(LLM声明但未生成的剔除)
        2. 空文件: SYNCED状态且size=0的文件剔除(PENDING保留,size可能暂为0)
        3. 重复文件: 按filepath去重(保留首次出现,LLM可能重复声明)
        4. 过程文件: 剔除识别为过程文件的条目(is_likely_process_file)
        5. 临时文件: 剔除扩展名命中excluded_extensions的条目
        6. 未同步: file.key为空或sync_status!=SYNCED的文件剔除(下载会失败)

        Args:
            declared_attachments: LLM在summarize中声明的attachments路径列表
            session_files: session.files全量文件列表(含filepath/size/sync_status)

        Returns:
            DeliveryValidationResult: 包含有效附件列表 + 过滤统计 + 命中率
        """
        # 构建session.files的filepath→File索引(用于完整性校验与空文件校验)
        file_index: Dict[str, File] = {
            f.filepath: f for f in session_files if f.filepath
        }

        valid: List[str] = []
        filtered_missing: List[str] = []
        filtered_empty: List[str] = []
        filtered_duplicates: List[str] = []
        filtered_process: List[str] = []
        filtered_temp: List[str] = []
        filtered_unsynced: List[str] = []
        seen: Set[str] = set()

        for filepath in declared_attachments:
            # 1.完整性校验: 文件必须存在于session.files
            if filepath not in file_index:
                filtered_missing.append(filepath)
                continue

            # 2.空文件校验: SYNCED状态且size=0的文件剔除(写入失败或空文件)
            f = file_index[filepath]
            sync_status = getattr(f, "sync_status", "SYNCED")
            if getattr(f, "size", 0) == 0 and sync_status == "SYNCED":
                filtered_empty.append(filepath)
                continue

            # 3.重复文件校验: 按filepath去重(保留首次出现)
            if filepath in seen:
                filtered_duplicates.append(filepath)
                continue
            seen.add(filepath)

            # 4.过程文件校验: 剔除识别为过程文件的条目
            if self.is_likely_process_file(filepath):
                filtered_process.append(filepath)
                continue

            # 5.临时文件校验: 剔除扩展名命中excluded_extensions的条目
            if self.is_excluded_file(filepath):
                filtered_temp.append(filepath)
                continue

            # 6.未同步校验: file.key 为空或 sync_status != SYNCED 时剔除
            # 设计动机: 沙箱生成的文件 OSS 上传失败时标记 PENDING/key="",
            # 前端展示该文件但点击下载会 500,污染交付列表。
            # 此处前置剔除,保证交付物列表中的文件均可下载。
            if sync_status != "SYNCED" or not getattr(f, "key", ""):
                filtered_unsynced.append(filepath)
                logger.warning(
                    f"交付物未同步到OSS,从交付列表剔除: filepath={filepath}, "
                    f"sync_status={sync_status}, key={'空' if not getattr(f, 'key', '') else '已设置'}"
                )
                continue

            valid.append(filepath)

        return DeliveryValidationResult(
            valid_attachments=valid,
            filtered_missing=filtered_missing,
            filtered_empty=filtered_empty,
            filtered_duplicates=filtered_duplicates,
            filtered_process=filtered_process,
            filtered_temp=filtered_temp,
            filtered_unsynced=filtered_unsynced,
            total_declared=len(declared_attachments),
            total_valid=len(valid),
        )
