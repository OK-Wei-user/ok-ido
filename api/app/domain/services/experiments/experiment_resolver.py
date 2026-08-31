#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实验分组解析器(Batch 40 / 方向2: 预算调整策略 A/B 测试)

设计目标:
- 基于 session_id 哈希分配实验组,确保同一会话始终落入同一组(确定性)
- 配置驱动实验: 通过 experiments.yaml 定义实验组与预算配置
- 无配置文件时降级到默认 _TASK_TYPE_BUDGET_ADJUSTMENTS(向后兼容)

实验配置示例(experiments.yaml):
    budget_adjustment_v1:
      enabled: true
      groups:
        control:
          research: {deep_research: 1}
          data_analysis: {search_web: 2}
          browser: {browser_navigate: 5}
        variant_a:
          research: {deep_research: 2}
          data_analysis: {search_web: 1}
          browser: {browser_navigate: 3}
      split: 50  # control 占比(%), 剩余分配给 variant_a

设计原则:
- 确定性分组: hash(session_id) % 100 < split → control, 否则 variant_a
- 降级安全: 配置文件缺失/解析失败时使用默认硬编码值
- 幂等: 同一 session_id 多次调用返回相同组名
"""
import hashlib
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 默认预算调整(与 budget_tracker._TASK_TYPE_BUDGET_ADJUSTMENTS 一致,降级时使用)
_DEFAULT_ADJUSTMENTS: Dict[str, Dict[str, int]] = {
    "research": {"deep_research": 1},
    "data_analysis": {"search_web": 2},
    "browser": {"browser_navigate": 5},
}

# 默认实验组名
_CONTROL_GROUP = "control"
_DEFAULT_GROUP = "default"


class ExperimentResolver:
    """实验分组解析器(Batch 40 / 方向2)

    基于 session_id 确定性哈希分配实验组,从配置文件读取实验定义。
    无配置时降级到默认硬编码预算调整值。

    使用方式:
        resolver = ExperimentResolver()
        group, adjustments = resolver.resolve(session_id)
        tracker.adjust_for_task_type(task_type, adjustments=adjustments)
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """构造函数

        Args:
            config: 可选的实验配置字典,None 时尝试从 experiments.yaml 加载,
                    加载失败则使用默认硬编码值
        """
        self._config = config
        if self._config is None:
            self._config = self._load_config()

    def _load_config(self) -> Optional[Dict[str, Any]]:
        """从 experiments.yaml 加载实验配置

        降级策略: 文件不存在/解析失败时返回 None,使用默认硬编码值
        """
        try:
            import yaml
            from pathlib import Path
            # 尝试多个可能的配置路径
            for config_path in [
                Path("config/experiments.yaml"),
                Path("api/config/experiments.yaml"),
                Path("/app/config/experiments.yaml"),
            ]:
                if config_path.exists():
                    with open(config_path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                        logger.info(f"实验配置已加载: {config_path}")
                        return data
        except ImportError:
            logger.debug("PyYAML 未安装,实验分组降级到默认配置")
        except Exception as e:
            logger.debug(f"实验配置加载失败(降级到默认): {e}")
        return None

    def resolve(self, session_id: str) -> Tuple[str, Dict[str, Dict[str, int]]]:
        """解析会话的实验组与对应预算调整配置

        Args:
            session_id: 会话 ID(用于确定性分组)

        Returns:
            (实验组名, 预算调整字典) 二元组
            - 实验组名: "control" / "variant_a" / "default"
            - 预算调整字典: {task_type: {tool_name: increment}}
        """
        # 无配置时降级到默认
        if not self._config:
            return _DEFAULT_GROUP, _DEFAULT_ADJUSTMENTS

        # 查找启用的实验
        experiment = self._find_active_experiment()
        if not experiment:
            return _DEFAULT_GROUP, _DEFAULT_ADJUSTMENTS

        groups = experiment.get("groups", {})
        split = experiment.get("split", 50)

        # 确定性分组: hash(session_id) % 100 < split → control
        group_name = self._assign_group(session_id, list(groups.keys()), split)
        group_config = groups.get(group_name, {})

        logger.info(
            f"会话[{session_id}]分配到实验组[{group_name}], "
            f"实验={experiment.get('name', 'unknown')}"
        )
        return group_name, group_config

    def _find_active_experiment(self) -> Optional[Dict[str, Any]]:
        """查找第一个启用的实验配置"""
        if not self._config:
            return None
        for name, exp in self._config.items():
            if isinstance(exp, dict) and exp.get("enabled", False):
                return {"name": name, **exp}
        return None

    @staticmethod
    def _assign_group(session_id: str, group_names: list, split: int) -> str:
        """基于 session_id 确定性分配实验组

        Args:
            session_id: 会话 ID
            group_names: 可用组名列表(第一个为 control 组)
            split: control 组占比(%)

        Returns:
            分配的组名
        """
        if not group_names:
            return _DEFAULT_GROUP
        if len(group_names) == 1:
            return group_names[0]

        # hash(session_id) % 100 < split → control(第一个组)
        hash_val = int(hashlib.md5(session_id.encode()).hexdigest(), 16) % 100
        if hash_val < split:
            return group_names[0]  # control
        return group_names[1] if len(group_names) > 1 else group_names[0]
