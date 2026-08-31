#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : skill_service.py
Skills领域服务 - 技能查询、匹配与提示词生成的核心业务逻辑

架构设计(借鉴OpenClaw AgentSkills规范):
- 声明式元数据: 技能通过SKILL.md frontmatter声明file_extensions/enabled/keywords
- 动态扩展: 新增技能只需创建目录+SKILL.md，无需修改服务代码
- 集中过滤: _get_visible_skills统一过滤enabled=False的技能
- 分层注入: 附件驱动时注入摘要+指南，通过truncate_guide_for_injection控制上下文膨胀
"""
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional, List, Dict, TYPE_CHECKING

from app.domain.models.skill import Skill
from app.domain.repositories.skill_repository import ISkillRepository

if TYPE_CHECKING:
    from app.infrastructure.storage.session_prompt_cache import SessionPromptCache

logger = logging.getLogger(__name__)

_MAX_GUIDE_LENGTH = 16000       # get_skill_guide返回内容的最大长度
_MAX_INJECTION_LENGTH = 32000   # 上下文注入指南的最大长度
_SKILL_GUIDE_GLOBAL_SESSION = "__global__"  # 技能指南为全局共享(非会话级),使用固定session_id


@dataclass
class SkillMatch:
    """技能匹配结果"""
    skill: Skill
    score: float
    matched_keywords: List[str] = field(default_factory=list)
    match_reason: str = ""


class SkillMatcher:
    """基于关键词/扩展名的技能匹配器，支持多维度加权评分"""

    def match(self, query: str, skills: List[Skill], top_n: int = 5, min_score: float = 0.5) -> List[SkillMatch]:
        """对查询文本进行多维度匹配，返回按评分降序排列的匹配结果"""
        matches = []
        query_lower = query.lower()
        for skill in skills:
            score, keywords = self._calculate_similarity(query_lower, skill)
            if score >= min_score:
                reason = f"匹配关键词: {', '.join(keywords)}" if keywords else "语义匹配"
                matches.append(SkillMatch(skill=skill, score=score, matched_keywords=keywords, match_reason=reason))
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:top_n]

    def match_by_extension(self, filename: str, ext_map: Dict[str, str], skills: List[Skill]) -> Optional[Skill]:
        """根据文件扩展名精确匹配技能，大小写不敏感"""
        ext = self._extract_extension(filename)
        if not ext:
            return None
        skill_name = ext_map.get(ext.lower())
        if not skill_name:
            return None
        for skill in skills:
            if skill.name == skill_name:
                return skill
        return None

    def _calculate_similarity(self, query: str, skill: Skill) -> tuple:
        """多维度加权评分: 名称(3.0) > 扩展名(2.5) > 关键词(2.0) > 描述词(1.0) > 分类(1.0)"""
        score = 0.0
        matched = []

        # 维度1: 技能名称匹配（权重最高）
        if skill.name.lower() in query:
            score += 3.0
            matched.append(skill.name)

        # 维度2: 声明关键词匹配
        if skill.keywords:
            for kw in skill.keywords:
                if kw.lower() in query:
                    score += 2.0
                    matched.append(kw)

        # 维度3: 文件扩展名匹配
        if skill.file_extensions:
            for ext in skill.file_extensions:
                if ext.lower() in query:
                    score += 2.5
                    matched.append(ext)

        # 维度4: 描述词分词匹配
        for kw in (skill.description.lower() + " " + " ".join(skill.keywords or [])).split():
            kw_lower = kw.strip(".,;:()[]{}\"'").lower()
            if len(kw_lower) >= 2 and kw_lower in query:
                score += 1.0
                if kw_lower not in matched:
                    matched.append(kw_lower)

        # 维度5: 分类匹配
        if skill.category and skill.category.lower() in query:
            score += 1.0
            matched.append(skill.category)

        return score, matched

    @staticmethod
    def _extract_extension(filename: str) -> Optional[str]:
        """从文件名中提取扩展名（含前导点号），无扩展名返回None"""
        if "." not in filename:
            return None
        return filename[filename.rfind("."):]


@dataclass
class SkillUsage:
    """单次技能使用记录"""
    skill_name: str
    task_context: str = ""
    success: bool = True


class SkillContextTracker:
    """会话级技能使用历史追踪器，用于提示词中追加最近使用信息

    F3-5 LRU上限: _history改用OrderedDict并设置MAX_SESSIONS上限,
    超出时淘汰最久未访问的会话,防止长生命周期应用累积会话导致内存无限增长。
    每个会话内部usage列表仍受MAX_HISTORY条数约束。
    """

    MAX_HISTORY = 20       # 单会话保留的最大usage条数
    MAX_SESSIONS = 100     # _history最多保留的会话数(LRU淘汰)

    def __init__(self):
        # OrderedDict按插入顺序维护会话,move_to_end将最近访问的会话移到末尾,
        # popitem(last=False)淘汰队首(最久未访问)会话,实现LRU语义
        self._history: "OrderedDict[str, List[SkillUsage]]" = OrderedDict()

    def record_usage(self, session_id: str, skill_name: str, task_context: str = "", success: bool = True) -> None:
        """记录技能使用，超过MAX_HISTORY时自动截断单会话条数;
        超过MAX_SESSIONS时按LRU淘汰最久未访问的会话"""
        # 1.命中已有会话: 移到末尾(标记为最近访问),然后追加usage
        if session_id in self._history:
            self._history.move_to_end(session_id)
        usages = self._history.setdefault(session_id, [])
        usages.append(SkillUsage(skill_name=skill_name, task_context=task_context, success=success))
        if len(usages) > self.MAX_HISTORY:
            # in-place截断保留最近MAX_HISTORY条,避免重建list
            del usages[:-self.MAX_HISTORY]

        # 2.F3-5: 会话总数超限,淘汰队首(最久未访问)会话
        if len(self._history) > self.MAX_SESSIONS:
            evicted_sid, _ = self._history.popitem(last=False)
            logger.debug(f"SkillContextTracker会话数超限,LRU淘汰会话[{evicted_sid}]")

    def get_recent_skills(self, session_id: str, limit: int = 3) -> List[str]:
        """获取最近使用的技能名（去重，按最近使用顺序）;
        访问时同步刷新LRU顺序,将会话移到末尾标记为最近访问"""
        usages = self._history.get(session_id, [])
        if not usages:
            return []
        # F3-5: 命中会话后移到末尾(仅当存在多会话时才有意义,避免无谓写操作)
        if session_id in self._history:
            self._history.move_to_end(session_id)

        seen, result = set(), []
        for usage in reversed(usages):
            if usage.skill_name not in seen:
                seen.add(usage.skill_name)
                result.append(usage.skill_name)
            if len(result) >= limit:
                break
        return result

    def clear_session(self, session_id: str) -> None:
        """清除指定会话的使用历史"""
        self._history.pop(session_id, None)


class SkillService:
    """Skills领域服务 - 技能查询、匹配、指南获取与提示词生成的统一入口"""

    def __init__(
            self,
            repository: ISkillRepository,
            prompt_cache: Optional["SessionPromptCache"] = None,
    ):
        """构造函数

        Args:
            repository: 技能仓库
            prompt_cache: 会话级提示词缓存(可选),用于持久化技能指南到Redis,
                避免会话内重复读取技能文件;None时降级为纯内存(向后兼容)
        """
        self._repository = repository
        self._matcher = SkillMatcher()
        self._tracker = SkillContextTracker()
        self._ext_map: Optional[Dict[str, str]] = None  # 懒加载的扩展名映射
        self._prompt_cache = prompt_cache  # 提示词缓存(可选)
        self._guide_cache: Dict[str, str] = {}  # L1内存缓存: 技能名 → 指南内容

    # ── 内部工具方法 ──────────────────────────────────────

    async def _get_visible_skills(self) -> List[Skill]:
        """获取所有可见技能(enabled=True)，集中过滤逻辑"""
        skills = await self._repository.get_all()
        return [s for s in skills if s.enabled]

    async def _get_extension_map(self) -> Dict[str, str]:
        """获取扩展名映射（仅含enabled技能），从技能的file_extensions字段动态构建"""
        if self._ext_map is not None:
            return self._ext_map
        self._ext_map = {}
        for skill in await self._get_visible_skills():
            for ext in skill.file_extensions:
                normalized = ext.lower()
                if not normalized.startswith("."):
                    normalized = f".{normalized}"
                self._ext_map[normalized] = skill.name
        return self._ext_map

    # ── 公开查询接口 ──────────────────────────────────────

    async def list_skills(self) -> List[Skill]:
        """列出所有可见技能"""
        return await self._get_visible_skills()

    async def get_skill(self, name: str) -> Optional[Skill]:
        """按名称获取技能（含enabled=False），用于内部指南查询"""
        return await self._repository.get_by_name(name)

    async def match_skills(self, query: str, top_n: int = 5) -> List[SkillMatch]:
        """根据查询文本智能匹配技能"""
        skills = await self._get_visible_skills()
        return self._matcher.match(query, skills, top_n)

    async def match_by_filename(self, filename: str) -> Optional[Skill]:
        """根据文件名扩展名精确匹配技能"""
        skills = await self._get_visible_skills()
        ext_map = await self._get_extension_map()
        return self._matcher.match_by_extension(filename, ext_map, skills)

    async def detect_skills_from_attachments(self, filepaths: List[str]) -> List[Skill]:
        """根据附件路径列表检测相关技能，自动去重"""
        skills = await self._get_visible_skills()
        ext_map = await self._get_extension_map()
        seen_names: set = set()
        result: List[Skill] = []
        for filepath in filepaths:
            matched = self._matcher.match_by_extension(filepath, ext_map, skills)
            if matched and matched.name not in seen_names:
                seen_names.add(matched.name)
                result.append(matched)
        return result

    async def get_skill_guide(self, name: str, max_length: int = _MAX_GUIDE_LENGTH) -> Optional[str]:
        """获取技能操作指南，超长时自动截断并附加提示

        两级缓存(L1内存+L2 Redis)避免会话内重复读取技能文件:
        - L1内存: 零延迟,覆盖单次会话内重复获取
        - L2 Redis: 持久化,覆盖实例重建场景(技能指南为全局共享,非会话级)
        - 无prompt_cache时降级为纯L1内存(向后兼容)
        """
        # 1.L1内存缓存命中
        cache_key = f"{name}:{max_length}"
        if cache_key in self._guide_cache:
            logger.debug(f"技能指南L1缓存命中: skill='{name}'")
            return self._guide_cache[cache_key]

        # 2.L2 Redis缓存命中(技能指南为全局共享,使用固定session_id)
        if self._prompt_cache:
            l2_cached = await self._prompt_cache.get(
                _SKILL_GUIDE_GLOBAL_SESSION, "skill_guide", cache_key
            )
            if l2_cached:
                logger.debug(f"技能指南L2缓存命中(Redis): skill='{name}'")
                # 回写L1加速后续读取
                self._guide_cache[cache_key] = l2_cached
                return l2_cached

        # 3.缓存未命中: 从仓库读取技能指南
        skill = await self._repository.get_by_name(name)
        if not skill:
            return None
        content = skill.content
        if len(content) > max_length:
            content = content[:max_length] + "\n\n...(指南内容过长已截断，请聚焦关键步骤执行)"

        # 4.回写L1内存缓存
        self._guide_cache[cache_key] = content
        # 5.回写L2 Redis缓存(全局共享,加速跨会话读取)
        if self._prompt_cache:
            await self._prompt_cache.set(
                _SKILL_GUIDE_GLOBAL_SESSION, "skill_guide", cache_key, content
            )
        return content

    # ── 使用历史 ──────────────────────────────────────────

    def record_usage(self, session_id: str, skill_name: str, task_context: str = "", success: bool = True) -> None:
        """记录技能使用历史"""
        self._tracker.record_usage(session_id, skill_name, task_context, success)

    def get_recent_skills(self, session_id: str, limit: int = 3) -> List[str]:
        """获取会话最近使用的技能名列表"""
        return self._tracker.get_recent_skills(session_id, limit)

    # ── 缓存管理 ──────────────────────────────────────────

    async def refresh(self) -> None:
        """刷新技能缓存（清除映射+L1/L2指南缓存+重新加载仓库）

        清除两级缓存避免技能文件变更后脏读:
        - L1内存缓存: _guide_cache.clear()
        - L2 Redis缓存: clear_type(__global__, "skill_guide")(TTL=4小时,不主动清除会持续返回旧指南)
        """
        self._ext_map = None
        self._guide_cache.clear()  # 清除L1指南缓存(技能文件可能已变更)
        # 清除L2 Redis缓存(全局共享,技能文件变更时主动失效,避免4小时内脏读)
        if self._prompt_cache:
            await self._prompt_cache.clear_type(_SKILL_GUIDE_GLOBAL_SESSION, "skill_guide")
        await self._repository.refresh()

    # ── 提示词生成 ────────────────────────────────────────

    async def generate_skills_prompt(self, session_id: Optional[str] = None) -> str:
        """生成skills_index提示词块，供Agent系统提示使用"""
        skills = await self._get_visible_skills()
        if not skills:
            return ""

        parts = ["<skills_index>\n## 可用专业技能\n"]
        for skill in skills:
            info = skill.to_dict_for_prompt(lightweight=True)
            parts.append(f"- `{info['name']}`: {info['description']}\n")

        parts.append("\n## Skills使用原则\n")
        parts.append("1. **附件驱动**: 用户上传文件时，系统自动检测相关技能并注入指南，**必须按指南操作**，无需再调用get_skill_guide\n")
        parts.append("2. **优先使用技能脚本**: 技能提供的脚本已在沙箱中可用，直接执行即可，**禁止自行安装替代工具**(如pip install/apt-get install)\n")
        parts.append("3. **直接使用**: 任务明确涉及某Skill时，按其指南直接操作\n")
        parts.append("4. **智能匹配**: 不确定用哪个Skill时，调用match_skills\n")
        parts.append("5. **获取指南**: 仅当系统未自动注入指南时，才调用get_skill_guide\n")
        parts.append("6. **避免冗余**: 不要每次任务开始都调用list_skills\n")

        if session_id:
            recent = self._tracker.get_recent_skills(session_id)
            if recent:
                parts.append(f"\n## 最近使用的Skills\n本对话最近使用: {', '.join(f'`{s}`' for s in recent)}\n")

        parts.append("</skills_index>")
        return "".join(parts)

    @staticmethod
    def truncate_guide_for_injection(guide: str, max_length: int = _MAX_INJECTION_LENGTH) -> str:
        """截断指南内容用于上下文注入，防止上下文膨胀"""
        if len(guide) <= max_length:
            return guide
        return guide[:max_length] + "\n\n...(指南内容过长已截断，完整指南可通过get_skill_guide获取)"
