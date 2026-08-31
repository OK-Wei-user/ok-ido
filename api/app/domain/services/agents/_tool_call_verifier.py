#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""工具调用强制校验模块

防止 LLM 在 json_object 模式下未实际执行工具却声称完成任务(工具调用幻觉)。

设计原则:
- 动作类步骤(生成/创建/导出/查询/分析等)必须调用工具
- 认知类步骤(纯思考/回顾)可不调用工具
- 召回优先: 动作类关键词尽量宽泛,漏判代价(幻觉被接受,文件未生成却标记完成)
  远大于误判代价(认知类步骤多一次重试)

背景:
- 会话 f7ef16db 中,LLM 在 json_object 模式下未产出 tool_calls,
  直接在 content 中返回 {"success": true, "attachments": [...]} 的幻觉 JSON,
  导致 step 被错误标记为 COMPLETED,文件未实际创建(sync_status=PENDING)。
- 现有 no_tool_retry_done 机制仅重试一次,重试后仍无工具调用时会接受 LLM 的幻觉结果。
- 本模块在重试后仍无工具调用时,对动作类步骤强制标记 FAILED,拒绝幻觉结果。
"""
# 动作类关键词: 命中则步骤必须调用工具
# 覆盖执行阶段常见操作动词,中英文兼顾
# 组织顺序: 按操作语义分组,便于维护扩展
_ACTION_KEYWORDS: tuple = (
    # 生成/创建/制作类
    "生成", "创建", "制作", "编写", "撰写", "导出", "输出", "保存", "写入",
    "构建", "建立", "搭建", "实现", "开发", "部署", "设计",
    # 读取/获取类
    "读取", "查看", "获取", "下载", "上传", "导入", "加载", "采集",
    # 查询/搜索类
    "查询", "搜索", "检索", "查找", "调研", "研究", "爬取",
    # 分析/计算类
    "分析", "计算", "统计", "汇总", "对比", "比较", "评估", "测算", "核算",
    # 转换/处理类
    "转换", "格式化", "处理", "清洗", "合并", "拆分", "分割", "整理",
    # 识别/提取类
    "识别", "提取", "翻译", "解析", "校验", "验证",
    # 浏览器/页面交互类(会话5f5ae2ab暴露: 打开/点击/输入等动词缺失导致幻觉防护失效)
    "打开", "点击", "进入", "跳转", "导航", "访问", "浏览", "滚动", "下滑", "上滑",
    "输入", "填写", "选择", "勾选", "切换", "调整", "修改", "设置", "定位", "截图",
    # 英文动作类(大小写不敏感,通过 desc_lower 匹配)
    "generate", "create", "export", "download", "read", "write", "query",
    "search", "analyze", "convert", "extract", "translate", "process",
    "calculate", "build", "make", "save", "load", "fetch", "parse",
    # 英文浏览器/交互类(与上方中文组对齐)
    "navigate", "browse", "open", "visit", "click", "login", "scroll",
    "type", "input", "select", "check", "toggle", "adjust", "modify",
    "set", "locate", "screenshot",
)

# 工具名前缀/全名: 步骤描述显式引用工具名 → 必须调用工具
# 信号最强: Planner 生成的步骤常以"使用browser_navigate打开..."形式描述,
# 直接点明要调用的工具,幻觉防护应无条件触发。
_TOOL_NAME_MARKERS: tuple = (
    "browser_navigate", "browser_view", "browser_click", "browser_type",
    "browser_scroll", "browser_restart", "browser_wait", "browser_console",
    "browser_action", "browser_",  # 兜底前缀: 覆盖未来新增的browser_*工具
    "shell_execute", "write_file", "read_file", "search_web", "content_fetch",
    "mcp_", "skill_", "a2a_",
)


def step_requires_tool_call(step_description: str) -> bool:
    """判断步骤是否必须调用工具

    动作类步骤(生成/创建/导出/查询/分析/浏览器交互等)必须调用工具,
    防止 LLM 幻觉声明完成。认知类步骤(纯思考/回顾)可不调用工具。

    判断策略(任一命中即必须调用工具):
    - 步骤描述命中任一动作类关键词 → 必须调用工具
    - 步骤描述显式引用工具名(browser_navigate/shell_execute等) → 必须调用工具

    召回优先设计: 动作类关键词与工具名尽量宽泛,确保执行阶段实际操作类步骤
    都被识别。漏判会导致 LLM 幻觉被接受(工具未调用却标记完成,如会话5f5ae2ab
    中"打开URL"步骤因"打开"不在关键词表而被误判为认知类,4步全部幻觉完成),
    误判仅导致认知类步骤多一次重试(可接受)。

    Args:
        step_description: 步骤描述文本

    Returns:
        True 表示步骤必须调用工具, False 表示可不调用
    """
    if not step_description:
        return False
    desc_lower = step_description.lower()
    if any(kw in desc_lower for kw in _ACTION_KEYWORDS):
        return True
    return any(marker in desc_lower for marker in _TOOL_NAME_MARKERS)


def build_missing_tool_error(step_description: str) -> str:
    """构建无工具调用失败时的错误信息

    用于步骤重试后仍无工具调用时,标记 FAILED 并返回错误信息。
    错误信息会通过 ErrorEvent 传递给前端展示,并记录到 step.error。

    Args:
        step_description: 步骤描述文本(用于错误定位)

    Returns:
        错误信息字符串,包含步骤描述和操作建议
    """
    return (
        f"步骤未调用任何工具即声明完成,疑似工具调用幻觉。"
        f"步骤描述: {step_description[:150]}。"
        f"动作类步骤必须通过工具调用(shell_execute/write_file/"
        f"browser_navigate/browser_click等)实际执行操作后再提交结果,"
        f"禁止凭空声明完成。"
    )
