#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time  : 2026/07/21 16:00

@File  : _fragments.py
共享提示词片段(DRY 优化)

将多处重复的提示词内容提取为共享常量,被 system.py / planner.py / react.py
共同引用。修改时只需改一处,避免同步遗漏导致多 prompt 间漂移。

设计原则:
- 片段是无状态字符串,不含 .format 占位符(避免与各 prompt 的占位符冲突)
- 片段内容应聚焦"数据型规则"(如扩展名→工具映射、自检清单条目),
  而非"行为型约束"(行为约束允许在各 prompt 中以不同上下文强化)

EN片段说明:
- 当前系统仅加载中文版提示词(prompts/system.py, planner.py, react.py)
- EN片段作为"未来英文支持的预留翻译"保留,通过 test_prompts.py 参数化测试
  保证 CN/EN 数量一致,确保未来接入英文版时翻译已就绪
- 如需启用英文支持,需实现语言切换机制(如配置驱动或 importlib 动态导入)
  并创建 prompts/en/ 目录引用对应 EN 片段
"""

# ============================================================
# 附件处理决策树(8 种扩展名映射 + 附件技能优先原则)
# 被 PLANNER_SYSTEM_PROMPT / REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用
# 修改时只需改此处,CN/EN 同步更新
# ============================================================

ATTACHMENT_EXTENSION_MAP_CN = """- **扩展名识别策略**：根据附件扩展名选择读取方式：
  - 文本类（.txt/.md/.csv/.json/.log/.py/.js/.xml/.yaml/.html 等）：用 read_file 直接读取
  - Excel（.xlsx/.xls）：用 openpyxl 或 pandas 读取
  - Word（.docx）：用 python-docx 读取
  - PDF（.pdf）：用 pdfplumber 或 pypdf 读取
  - PowerPoint（.pptx）：用 python-pptx 读取
  - 图片（.png/.jpg/.jpeg/.gif/.bmp）：优先用 MCP `mcp_mcp-multimodal_vl_image_understand` 视觉理解（识别内容/OCR/物体/位置推断），或用 Pillow 读取元数据（EXIF/GPS）；**严禁 browser_navigate 访问本地图片**（浏览器禁止访问本地文件路径，详见 <browser_rules>）
  - 压缩包（.zip/.tar/.tar.gz/.gz）：用 unzip 或 tar 命令解压后读取内部文件
  - 未知格式：先用 shell_execute 执行 `file` 命令检测文件类型，再选择对应工具"""

ATTACHMENT_EXTENSION_MAP_EN = """- **Extension recognition strategy**: Select the reading method based on the attachment extension:
  - Text (.txt/.md/.csv/.json/.log/.py/.js/.xml/.yaml/.html, etc.): Use read_file directly
  - Excel (.xlsx/.xls): Use openpyxl or pandas
  - Word (.docx): Use python-docx
  - PDF (.pdf): Use pdfplumber or pypdf
  - PowerPoint (.pptx): Use python-pptx
  - Image (.png/.jpg/.jpeg/.gif/.bmp): Prefer MCP `mcp_mcp-multimodal_vl_image_understand` for visual understanding (content recognition/OCR/object/location inference), or use Pillow to read metadata (EXIF/GPS); **strictly forbidden to use browser_navigate to access local images** (browser tools cannot access local file paths, see <browser_rules>)
  - Archive (.zip/.tar/.tar.gz/.gz): Use unzip or tar command to extract, then read internal files
  - Unknown format: First use shell_execute to run the `file` command to detect the file type, then select the corresponding tool"""

# ============================================================
# 图片附件识别决策树(根治 LLM 误用 browser_navigate 访问本地图片)
# 被 REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用
# 修改时只需改此处,CN/EN 同步更新
# ============================================================

IMAGE_ATTACHMENT_DECISION_TREE_CN = """- **图片附件识别决策树（重要,按优先级从高到低选择,严禁跳过低级工具后再回退）**：
  1. **首选 MCP `mcp_mcp-multimodal_vl_image_understand`**：用于视觉理解/OCR/物体识别/位置推断/场景描述;调用时查看工具schema中 image_source 参数格式（通常为 `upload://xxx` 引用沙箱内文件）
  2. **次选 shell_execute + Pillow**：仅用于读取图片元数据（EXIF/GPS/尺寸/拍摄时间），不用于视觉理解
  3. **严禁 browser_navigate 访问本地图片**：浏览器工具禁止访问 `file:///` 协议，违反会触发工具错误并浪费 token"""

IMAGE_ATTACHMENT_DECISION_TREE_EN = """- **Image attachment recognition decision tree (important, select by priority from high to low, strictly forbidden to skip lower-level tools and then fall back)**:
  1. **Prefer MCP `mcp_mcp-multimodal_vl_image_understand`**: For visual understanding/OCR/object recognition/location inference/scene description; when calling, check the tool schema for the image_source parameter format (usually `upload://xxx` referencing sandbox files)
  2. **Fallback shell_execute + Pillow**: Only for reading image metadata (EXIF/GPS/size/shooting time), not for visual understanding
  3. **Strictly forbidden browser_navigate to access local images**: Browser tools cannot access `file:///` protocol ; violation triggers tool errors and wastes tokens"""

# ============================================================
# 并行工具调用语义去重约束(防止同一目标并行调用语义重复工具)
# 被 REACT_SYSTEM_PROMPT 引用
# 修改时只需改此处,CN/EN 同步更新
# ============================================================

PARALLEL_TOOL_DEDUP_CN = """- **并行工具调用语义去重（重要,避免同一目标多工具冗余）**：并行 tool_calls 应针对独立目标,严禁同一目标并行调用语义重复工具
  - 反例：同一图片同时调用 `browser_navigate(file:///...)` + `shell_execute(PIL Image.open)` + `mcp_mcp-multimodal_vl_image_understand(...)` —— 三者目标均为"识别图片内容",应择优选用 MCP 工具,失败再降级
  - 正例 1：并行调用 `mcp_mcp-multimodal_vl_image_understand(...)` + `shell_execute(ls -la /home/ubuntu/upload/)` —— 一个识别图片,一个确认文件存在,目标独立
  - 正例 2：并行调用 `mcp_amap_weather(city="广州")` + `mcp_amap_weather(city="深圳")` —— 同一工具不同参数,查询不同城市,目标独立"""

PARALLEL_TOOL_DEDUP_EN = """- **Parallel tool call semantic deduplication (important, avoid multi-tool redundancy for the same target)**: Parallel tool_calls should target independent goals; it is strictly forbidden to call semantically duplicate tools in parallel for the same target
  - Negative example: For the same image, calling `browser_navigate(file:///...)` + `shell_execute(PIL Image.open)` + `mcp_mcp-multimodal_vl_image_understand(...)` in parallel — all three aim to "recognize image content"; you should prefer the MCP tool and fall back only on failure
  - Positive example 1: Parallel call `mcp_mcp-multimodal_vl_image_understand(...)` + `shell_execute(ls -la /home/ubuntu/upload/)` — one recognizes the image, one confirms file existence, independent goals
  - Positive example 2: Parallel call `mcp_amap_weather(city="Guangzhou")` + `mcp_amap_weather(city="Shenzhen")` — same tool with different params, querying different cities, independent goals"""

# 附件技能优先原则(单独提取,因 PLANNER 与 REACT/EXECUTION 上下文措辞略有差异,
# 但核心约束完全相同,提取为常量保证一致性)
ATTACHMENT_SKILL_PRIORITY_CN = """- **附件技能优先**：若系统已注入相关附件技能提示（如 pdf技能/excel技能/docx技能，可通过上下文中的 `[附件技能提示: xxx]` 标记识别），必须优先按指南操作，禁止绕过技能自行实现"""

ATTACHMENT_SKILL_PRIORITY_EN = """- **Attachment skill priority**: If the system has injected relevant attachment skill hints (e.g., pdf skill/excel skill/docx skill, identifiable by the `[附件技能提示: xxx]` marker in the context), you must prioritize following the guide and must not bypass the skill to implement on your own"""

# ============================================================
# 交付物完整性自检清单(4 项检查)
# 被 <delivery_rules> / REACT_SYSTEM_PROMPT / EXECUTION_PROMPT / SUMMARIZE_PROMPT 引用
# 修改时只需改此处,CN/EN 同步更新
# ============================================================

DELIVERY_SELF_CHECK_CN = """- 文件非空且大小合理(>1KB;异常小可能写入失败)
  - 格式正确(如 docx 能被 python-docx 读取,xlsx 能被 openpyxl 读取)
  - 内容完整性(关键章节齐全、数据行数与原始数据一致、无截断)
  - 命名符合语义化规范"""

DELIVERY_SELF_CHECK_EN = """- File is non-empty and size is reasonable (>1KB; abnormally small may indicate write failure)
  - Format is correct (e.g., docx can be read by python-docx, xlsx can be read by openpyxl)
  - Content is complete (key chapters present, data row count matches raw data, no truncation)
  - Naming follows semantic conventions"""

# 用户预期对齐原则(在 REACT_SYSTEM_PROMPT 与 EXECUTION_PROMPT 中重复,
# 提取为常量保证一致性)
USER_EXPECTATION_ALIGNMENT_CN = """- **用户预期对齐**：执行过程中如发现交付物可能不符合用户预期(如数据量不足、格式不匹配、内容偏离用户需求),必须通过 `message_ask_user` 主动向用户确认,而非交付半成品或偏离需求的产物。"""

USER_EXPECTATION_ALIGNMENT_EN = """- **User expectation alignment**: If during execution you find that the deliverable may not meet user expectations (e.g., insufficient data volume, format mismatch, content deviating from user needs), you must proactively confirm with the user via `message_ask_user` rather than delivering a semi-finished or off-demand product."""

# ============================================================
# 数据持久化与批量完整性约束(交付质量执行增强)
# 被 REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用
# 修改时只需改此处,CN/EN 同步更新
#
# 设计动机: 长会话中 LLM 常见交付质量问题:
# 1. 通过工具提取的数据仅在上下文中持有,未 write_file 落盘,
#  后续被上下文窗口截断丢失 → 数据即时持久化
# 2. 批量任务(导出N条/处理N个文件)部分完成(M<N)时误报完成 → 批量完整性验证
# 3. 将"已通过 browser/search_web/read_file 查看"误认为"已拥有" → viewed≠saved 区分
# 4. 迷路后返回正确页面的导航被误判为重复操作而拒绝执行 → 恢复导航豁免
# ============================================================

DATA_PERSISTENCE_CN = """- **数据即时持久化（重要,避免数据丢失）**：通过工具（MCP/search_web/browser/read_file）提取的数据必须立即用 `write_file` 持久化到文件,严禁仅在上下文中持有而未落盘。上下文窗口有限,未持久化的数据可能在长会话中被截断或丢失。
- **批量任务完整性验证（重要,避免部分交付）**：批量任务（如逐条导出 N 条数据、逐个处理 N 个文件）完成时,必须验证已持久化的数量等于目标总数(N)。仅 M<N 时严禁声明完成,必须继续处理剩余项或明确告知用户部分完成的数量与原因。
- **中断前必须交付已有成果（重要,避免 0 附件）**：会话因超时、预算耗尽、异常等原因即将中断时,**必须将已生成的所有文件(图表、数据表、部分报告、中间产物)作为附件交付给用户**,并在最终消息中说明:已完成的内容、未完成的内容、中断原因、后续建议。**严禁 0 附件结束会话**——即使主报告未生成,也必须交付已生成的图表、数据表等部分成果,让用户能基于已有成果继续工作。
- **"已查看" ≠ "已保存"**：通过 browser/search_web/read_file 查看到的数据不算"已拥有",必须 write_file 落盘后才算"已保存"。重复执行检查时区分:已保存的数据直接复用,未保存的数据必须重新提取并持久化。
- **恢复导航不算重复操作**：在浏览器中迷路后返回正确页面的导航操作不算"重复操作",不受"避免重复操作"约束限制。重复操作指对同一目标重复执行相同动作而无新结果。"""

DATA_PERSISTENCE_EN = """- **Data immediate persistence (important, avoid data loss)**: Data extracted via tools (MCP/search_web/browser/read_file) must be immediately persisted to a file via `write_file`; never hold data only in context without saving to disk. The context window is limited; unpersisted data may be truncated or lost in long sessions.
- **Batch task completeness verification (important, avoid partial delivery)**: When completing batch tasks (e.g., exporting N records one by one, processing N files), you must verify that the persisted count equals the target total (N). When only M<N, it is strictly forbidden to declare completion; you must continue processing remaining items or explicitly inform the user of the partial completion count and reason.
- **Must deliver existing results before interruption (important, avoid 0 attachments)**: When the session is about to be interrupted due to timeout, budget exhaustion, exception, etc., **you MUST deliver all generated files (charts, data tables, partial reports, intermediate products) as attachments to the user**, and explain in the final message: completed content, incomplete content, interruption reason, follow-up suggestions. **Strictly forbidden to end a session with 0 attachments** — even if the main report is not generated, you must deliver generated charts, data tables, and other partial results so the user can continue working based on existing results.
- **"Viewed" is not "Saved"**: Data viewed via browser/search_web/read_file is not "owned"; it is only "saved" after write_file persists it to disk. When checking for duplicate operations, distinguish: saved data can be directly reused; unsaved data must be re-extracted and persisted.
- **Recovery navigation is not a repeated operation**: Navigation to return to the correct page after getting lost in the browser is not a "repeated operation" and is not subject to the "avoid repeated operations" constraint. Repeated operations refer to executing the same action on the same target without new results."""

# ============================================================
# 部分完成判断原则(UPDATE_PLAN_PROMPT 专用)
# 防止 LLM 将部分完成误判为完整完成,导致后续步骤被删除
# ============================================================

PARTIAL_COMPLETION_PRINCIPLE_CN = """- **部分完成 ≠ 完整完成（重要,防止误删后续步骤）**：当步骤目标是产出 N 项结果(如 N 条数据、N 个文件、N 个章节)但仅完成 M 项(M<N)时,该步骤不算完成,不得删除或跳过后续依赖该步骤的步骤。必须在步骤描述中明确剩余项的处理方式(继续处理/分批交付/向用户说明部分完成原因)。"""

PARTIAL_COMPLETION_PRINCIPLE_EN = """- **Partial completion is not full completion (important, prevent premature step deletion)**: When a step's goal is to produce N items (e.g., N records, N files, N chapters) but only M items are completed (M<N), the step is not considered complete; do not delete or skip subsequent steps that depend on it. You must explicitly state in the step description how remaining items will be handled (continue processing / partial delivery / inform user of the partial completion reason)."""


# ============================================================
# 异步任务处理决策树(消除 react.py/system.py/mcp.py 三处 sleep 矛盾)
# 被 REACT_SYSTEM_PROMPT(CN/EN)引用;system.py mcp_rules 简化引用本决策树场景B
# 修改时只需改此处,CN/EN 同步更新
# 与 mcp.py 运行时机制对齐: 后台轮询上限 10 次,退避序列 60/120/180s
# ============================================================

ASYNC_TASK_DECISION_TREE_CN = """- **异步任务处理决策树（重要,避免会话超时,按场景选择策略）**：
  - **场景A:长耗时Shell命令(>30s,如编译/大文件下载/LibreOffice转换/数据处理脚本)**：
  - **必须使用** `shell_execute(async_mode=true)` 启动,立即返回 task_id 不阻塞
  - **必须使用** `task_wait(task_id, timeout=300)` 阻塞等待完成,**期间不消耗LLM token**
  - 示例: shell_execute(command="python long_task.py", async_mode=true) → 返回 task_id;task_wait(task_id="shell_xxx", timeout=300) → 阻塞等待,完成后返回执行结果
  - **严禁** 对 Shell 长耗时命令使用 `sleep N` 同步等待(浪费LLM token且阻塞会话)
  - **task_wait 超时处理约束(重要,避免回退sleep轮询)**: task_wait 返回"等待超时"时,表示任务仍在执行中(非失败)。**必须继续调用 `task_wait(task_id, timeout=300)` 等待**,不得回退到 `sleep N` 轮询或 `shell_read_output` 反复检查。大文件下载等场景可能需要多次 task_wait(每次300秒),累计等待10-20分钟属正常。仅当连续3次 task_wait 超时(累计15分钟)仍无结果时,才考虑用 `shell_read_output` 检查一次执行状态,然后继续 task_wait。
  - **场景B:MCP异步任务(MCP工具返回"任务已提交/异步处理中/pending"状态,或同步调用超时/失败),按场景分两类**：
  - **B1:同步超时自动转异步**(MCP工具同步调用超时后,系统自动启动后台轮询任务):
  - 同步调用MCP工具超时(>120s)时,系统自动转异步,返回task_id
  - **必须使用** `task_wait(task_id, timeout=300)` 阻塞等待完成,**期间不消耗LLM token**
  - 后台自动按递增退避(60s/120s/180s)轮询查询任务状态,最多10次
  - 超过10次仍未完成,基于已有数据推进任务或向用户报告进度与等待原因
  - **严禁** 对超时的MCP工具使用 `shell_execute(sleep N)` 轮询(浪费LLM token且阻塞会话)
  - **task_wait 完成后行为约束(重要,避免回退轮询)**: task_wait 返回成功表示异步任务已完成,直接使用返回结果(含下载链接/文件路径/任务状态)。**严禁** 在 task_wait 完成后回退到 `getDownloadTaskList` + `sleep N` 轮询确认下载状态。如需确认下载文件就绪状态,最多调用查询工具1次;状态为"已完成/成功"直接用 `shell_execute(wget/curl)` 下载,状态为"处理中"使用 `task_wait` 继续等待(而非 sleep 轮询)
  - **B2:业务系统异步导出**(如 mcp_xxx_export,提交后用 getDownloadTaskList 轮询查询状态):
  - ⚠️ **退避序列强制: 60→120→180→180→180递增,严禁固定sleep 60**
  - 调用导出工具提交任务(返回"任务已提交/异步处理中")
  - 用对应的查询工具(如 `mcp_xxx_getDownloadTaskList`)查询任务状态(**推荐不传status查询所有状态**,或按fileName精确查询目标任务,严禁仅传status=0)
  - 状态为"处理中"时,用 `shell_execute(sleep N)` 等待后重新查询,N按上述序列递增,最多轮询5次
  - 超过5次仍未完成,基于已有数据推进任务或向用户报告进度与等待原因
  - 状态为"已完成/成功"直接用 `shell_execute(curl -L -o <本地路径> "<下载URL>")` 下载
  - **同步调用超时/失败时(尤其导出/生成/下载类工具)**: 超时后系统自动转异步(B1),用task_wait等待;若未超时但返回失败,检查参数后重试或改用替代方案
  - **严禁** 用 `browser_wait` 等待MCP异步任务(browser_wait仅用于浏览器DOM渲染,详见 <browser_rules>)
  - **场景C:[系统提示]标记触发**: 当MCP工具返回结果末尾出现 `[系统提示]` 标记时,必须按提示中的等待时间执行sleep或停止轮询
  - **通用约束**: 场景 A 必须使用 async_mode=true + task_wait;B1 同步超时自动转异步后用 task_wait;B2 业务导出按指数退避(60→120→180→180→180) sleep 轮询,严禁固定重复同一时长(沙箱主动推送通知为长期规划,当前由API层轮询模拟)"""

ASYNC_TASK_DECISION_TREE_EN = """- **Async task processing decision tree (important, avoid session timeout, select strategy by scenario)**:
  - **Scenario A: Long-running Shell command (>30s, e.g., compilation/large file download/LibreOffice conversion/data processing script)**:
  - **Must use** `shell_execute(async_mode=true)` to start, immediately returns task_id without blocking
  - **Must use** `task_wait(task_id, timeout=300)` to block and wait for completion, **without consuming LLM token**
  - Example: shell_execute(command="python long_task.py", async_mode=true) → returns task_id; task_wait(task_id="shell_xxx", timeout=300) → blocks and waits, returns execution result after completion
  - **Strictly forbidden** to use `sleep N` synchronous wait for long-running Shell commands (wastes LLM token and blocks session)
  - **task_wait timeout handling constraint (important, avoid fallback to sleep polling)**: When task_wait returns "timeout", the task is still running (not failed). **Must continue calling `task_wait(task_id, timeout=300)` to wait**; do not fall back to `sleep N` polling or repeated `shell_read_output` checks. Large file downloads may require multiple task_wait calls (300s each), with cumulative waits of 10-20 minutes being normal. Only after 3 consecutive task_wait timeouts (15 minutes total) with no result, consider using `shell_read_output` to check status once, then resume task_wait.
  - **Scenario B: MCP async task (MCP tool returns "task submitted/processing asynchronously/pending" status, or sync call times out/fails), split into two types**:
  - **B1: Sync timeout auto-async** (after MCP tool sync call times out, system auto-starts background polling task):
  - When sync call to MCP tool times out (>120s), system auto-converts to async, returns task_id
  - **Must use** `task_wait(task_id, timeout=300)` to block and wait for completion, **without consuming LLM token**
  - Backend automatically polls task status with incremental backoff (60s/120s/180s), max 10 attempts
  - If still incomplete after 10 attempts, advance task based on existing data or report progress and waiting reason to user
  - **Strictly forbidden** to use `shell_execute(sleep N)` to poll timed-out MCP tools (wastes LLM token and blocks session)
  - **task_wait completion behavior constraint (important, avoid fallback polling)**: When task_wait returns success, the async task is complete; directly use the returned result (including download link/file path/task status). **Strictly forbidden** to fall back to `getDownloadTaskList` + `sleep N` polling to confirm download status after task_wait completes. If you need to confirm download file readiness, call the query tool at most once; if status is "completed/success", download directly with `shell_execute(wget/curl)`; if "processing", use `task_wait` to continue waiting (not sleep polling)
  - **B2: Business system async export** (e.g., mcp_xxx_export, submit then poll via getDownloadTaskList):
  - ⚠️ **Backoff sequence mandatory: 60→120→180→180→180 increment, strictly forbidden fixed sleep 60**
  - Call the export tool to submit the task (returns "task submitted/processing asynchronously")
  - Query task status via the corresponding query tool (e.g., `mcp_xxx_getDownloadTaskList`) (**recommend not passing status to query all states**, or query by fileName, strictly forbidden to only pass status=0)
  - When status is "processing", use `shell_execute(sleep N)` to wait then re-query, N increments per the above sequence, max 5 polling attempts
  - If still incomplete after 5 attempts, advance task based on existing data or report progress and waiting reason to user
  - When status is "completed/success", download directly with `shell_execute(curl -L -o <local_path> "<download_URL>")`
  - **When sync call times out/fails (especially export/generation/download tools)**: After timeout, system auto-converts to async (B1), use task_wait to wait; if not timed out but returns failure, check parameters and retry or use alternative strategy
  - **Strictly forbidden** to use `browser_wait` for MCP async tasks (browser_wait is only for browser DOM rendering, see <browser_rules>)
  - **Scenario C: [系统提示] marker trigger**: When `[系统提示]` marker appears at the end of MCP tool result, must execute sleep for the duration specified in the hint or stop polling
  - **General constraint**: Scenario A must use async_mode=true + task_wait; B1 uses task_wait after sync timeout auto-async; B2 business export uses exponential backoff (60→120→180→180→180) sleep polling, strictly forbidden to repeat the same duration (sandbox proactive push notification is a long-term plan, currently simulated by API-layer polling)"""

# ============================================================
# 预算超限响应策略(引导 LLM 在预算超限后立即切换策略)
# 被 REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用(CN/EN)
# 修改时只需改此处,CN/EN 同步更新
#
# 设计动机: budget_tracker 机制已正确拦截超限调用(75%告警+100%硬拦截),
# 但 LLM 在收到"调用次数已达上限"错误后仍继续尝试调用同一工具 4-5 次,
# 每次重试浪费一轮 LLM token 且无法获得新结果。本片段在 prompt 层引导
# LLM 收到预算超限错误后立即停止调用并切换策略。
# ============================================================

BUDGET_EXCEEDED_STRATEGY_CN = """- **预算超限响应策略（重要,避免无效重试浪费token）**：当工具返回"调用次数已达上限"错误时,必须立即停止调用该工具,严禁继续重试。应基于已有结果综合分析,或切换到错误消息中建议的替代策略(如 search_web 超限后切换到 deep_research/browser_navigate,deep_research 超限后基于已有研究摘要综合分析,browser_navigate 超限后切换到 search_web)。每次对已超限工具的重试都浪费一轮 LLM token 且无法获得新结果。"""

BUDGET_EXCEEDED_STRATEGY_EN = """- **Budget exceeded response strategy (important, avoid wasted retries consuming tokens)**: When a tool returns a "call limit reached" error, you must immediately stop calling that tool; strictly forbidden to retry. You should synthesize analysis based on existing results, or switch to the alternative strategy suggested in the error message (e.g., after search_web limit reached, switch to deep_research/browser_navigate; after deep_research limit reached, synthesize analysis based on existing research summary; after browser_navigate limit reached, switch to search_web). Each retry of an exhausted tool wastes a round of LLM token and cannot produce new results."""


# ============================================================
# 工具选择与文件下载引导(根治 LLM 工具选择困惑)
# 被 REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用(CN/EN)
# 修改时只需改此处,CN/EN 同步更新
#
# 设计动机: E2E 发现 LLM 存在 3 类困惑:
# 1. 对内置工具(shell_execute/write_file 等)不知直接可用,试图寻找替代
# 2. 使用 browser_navigate 访问 OSS 文件下载 URL(.xlsx),7 次无效访问,
#  应使用 shell_execute + curl -L -o 下载
# 3. download_data.py 执行失败后,不检查错误日志,陷入困惑循环导致会话连续超时
# ============================================================

TOOL_SELECTION_GUIDE_CN = """- **内置工具优先原则（重要）**：shell_execute、write_file、read_file、search_in_file、browser_navigate、search_web、deep_research 是系统内置工具,直接可用。MCP 工具(以`mcp_`前缀标识)仅用于外部系统接口调用（如 mcp_xxx_export 业务系统导出）或专业领域能力（如多模态视觉理解）。当需要执行命令、下载文件、读写文件时,直接使用对应的内置工具。
- **文件下载约束（重要,避免误用浏览器）**：下载网络文件（OSS/HTTP/HTTPS URL 指向的 .xlsx/.docx/.pdf/.zip 等二进制文件）**必须使用 `shell_execute` + `curl -L -o <本地路径> "<URL>"` 或 `wget -O <本地路径> "<URL>"`**。**严禁用 `browser_navigate` 访问文件下载 URL** — 浏览器无法下载二进制文件,只会渲染乱码或触发下载对话框,浪费 token 且无法获取文件内容。
  - 正确示例：`shell_execute(command='curl -L -o /home/ubuntu/data.xlsx "https://oss.example.com/xxx.xlsx"')`
  - 错误示例：`browser_navigate(url="https://oss.example.com/xxx.xlsx")` — 严禁这样操作
- **工具失败恢复策略（重要,避免困惑循环）**：内置工具执行失败时,**必须检查错误输出并修复命令/脚本**,而非寻找替代工具。具体策略：
  - shell_execute 失败 → 读取 stderr 错误信息,修复命令参数或脚本逻辑后重试
  - Python 脚本失败 → 检查 traceback,修复代码错误后重新执行
  - 文件下载失败 → 检查 URL 有效性/网络连通性,更换 curl/wget 重试
  - **严禁** 因内置工具失败而反复重试相同调用（会导致循环）"""

TOOL_SELECTION_GUIDE_EN = """- **Built-in tool priority principle (important, avoid redundant MCP searches)**: shell_execute, write_file, read_file, search_in_file, browser_navigate, search_web, deep_research are built-in tools, directly available. MCP tools (identified by `mcp_` prefix) are only for external system API calls (e.g., mcp_xxx_export business system export) or professional domain capabilities (e.g., multimodal visual understanding). When you need to execute commands, download files, or read/write files, directly use the corresponding built-in tools.
- **File download constraint (important, avoid browser misuse)**: Downloading network files (binary files like .xlsx/.docx/.pdf/.zip pointed to by OSS/HTTP/HTTPS URLs) **must use `shell_execute` + `curl -L -o <local_path> "<URL>"` or `wget -O <local_path> "<URL>"`**. **Strictly forbidden to use `browser_navigate` to access file download URLs** — browsers cannot download binary files; they only render garbled content or trigger download dialogs, wasting tokens without obtaining file content.
  - Correct example: `shell_execute(command='curl -L -o /home/ubuntu/data.xlsx "https://oss.example.com/xxx.xlsx"')`
  - Wrong example: `browser_navigate(url="https://oss.example.com/xxx.xlsx")` — strictly forbidden
- **Tool failure recovery strategy (important, avoid confusion loops)**: When built-in tools fail, **you must check error output and fix the command/script**, rather than searching MCP tools for alternatives. Specific strategies:
  - shell_execute failure → read stderr error message, fix command parameters or script logic, then retry
  - Python script failure → check traceback, fix code errors, then re-execute
  - File download failure → check URL validity/network connectivity, switch between curl/wget and retry
  - **Strictly forbidden** to search for MCP alternatives when built-in tools fail (built-in tools cover all basic capabilities)"""

# ============================================================
# 内置编程能力白名单(根治文档生成被误判为MCP专业能力)
# 被 REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用
# 修改时只需改此处,CN/EN 同步更新
#
# 设计动机: 步骤"使用docx技能生成专业的深度研究报告"
# 被AI误判为"专业领域能力"需MCP搜索,实际python-docx是沙箱预装库。
# 根因: "专业领域能力"定义模糊,未排除编程/库能力。
# 修复: 显式列出内置编程能力白名单,明确这些能力走shell_execute,严禁MCP搜索。
# ============================================================

BUILT_IN_CAPABILITY_CN = """- **内置编程能力白名单（重要,以下能力非MCP专业能力）**：以下能力均通过沙箱预装Python库 + shell_execute 实现,属于内置工具能力,**不是MCP专业领域能力**,无需使用MCP工具:
  - 文档生成: docx(python-docx)、xlsx(openpyxl)、pptx(python-pptx)、pdf(reportlab/pdfplumber)
  - 数据处理: pandas、numpy、scipy、scikit-learn
  - 可视化: matplotlib、seaborn
  - 图像处理: Pillow
  - 网络请求: requests、beautifulsoup4
  - 上述能力的"专业性"来自Python库,不依赖外部系统接口,直接用shell_execute编写Python脚本完成"""

BUILT_IN_CAPABILITY_EN = """- **Built-in programming capability whitelist (important, no MCP tools needed for the following capabilities)**: The following capabilities are implemented via sandbox pre-installed Python libraries + shell_execute, and are built-in tool capabilities, **NOT MCP professional domain capabilities**; no need to use MCP tools:
  - Document generation: docx (python-docx), xlsx (openpyxl), pptx (python-pptx), pdf (reportlab/pdfplumber)
  - Data processing: pandas, numpy, scipy, scikit-learn
  - Visualization: matplotlib, seaborn
  - Image processing: Pillow
  - Network requests: requests, beautifulsoup4
  - The "professionalism" of these capabilities comes from Python libraries, does not depend on external system APIs, and is completed directly via shell_execute writing Python scripts"""

# ============================================================
# 步骤描述措辞约束(根治规划层"使用XX技能"误导性表述)
# 被 PLANNER_SYSTEM_PROMPT 引用(CN/EN)
#
# 设计动机: planner生成步骤"使用docx技能生成专业的深度研究报告",
# 执行者误判"技能"为MCP专业能力,发起冗余的MCP工具调用。
# 根因: 规划层未约束步骤描述措辞,允许"使用XX技能"这种模糊表述。
# 修复: 强制步骤描述对内置能力使用具体库名(python-docx/openpyxl等),
# 严禁"使用XX技能"表述,从源头消除执行者的MCP调用困惑。
# ============================================================

STEP_WORDING_GUIDE_CN = """- **步骤描述措辞约束（重要,避免执行者MCP搜索困惑）**：步骤描述涉及内置编程能力（文档生成/数据处理/可视化/图像处理等）时,必须使用具体库名 + 实现方式,严禁使用"使用XX技能"这种模糊表述。
  - 正例: "用 python-docx 生成经营分析报告.docx"、"用 openpyxl 将统计数据写入 xlsx"、"用 matplotlib 绘制趋势图"、"用 pandas 清洗并分析业务数据"
  - 反例: "使用 docx 技能生成报告"、"使用 excel 技能处理数据"、"使用 pdf 技能提取文本" — "技能"表述会让执行者误判为MCP专业能力,发起冗余的MCP工具调用
  - 内置能力清单: docx(python-docx)、xlsx(openpyxl)、pptx(python-pptx)、pdf(reportlab/pdfplumber)、数据处理(pandas/numpy)、可视化(matplotlib/seaborn)、图像处理(Pillow)
  - MCP能力表述: 仅外部系统接口(如"使用 mcp_xxx_export 导出数据")或专业领域能力(如"使用 mcp_mcp-multimodal_vl_image_understand 识别图片")才在步骤中引用MCP工具名"""

STEP_WORDING_GUIDE_EN = """- **Step description wording constraint (important, avoid executor MCP confusion)**: When step descriptions involve built-in programming capabilities (document generation/data processing/visualization/image processing, etc.), you must use specific library names + implementation method; strictly forbidden to use vague expressions like "use XX skill".
  - Correct: "use python-docx to generate Business Analysis Report.docx", "use openpyxl to write statistics to xlsx", "use matplotlib to draw trend charts", "use pandas to clean and analyze business data"
  - Wrong: "use the docx skill to generate a report", "use the excel skill to process data", "use the pdf skill to extract text" — "skill" expressions cause the executor to misjudge them as MCP professional capabilities and initiate redundant MCP tool calls
  - Built-in capability list: docx (python-docx), xlsx (openpyxl), pptx (python-pptx), pdf (reportlab/pdfplumber), data processing (pandas/numpy), visualization (matplotlib/seaborn), image processing (Pillow)
  - MCP capability expression: Only external system APIs (e.g., "use mcp_xxx_export to export data") or professional domain capabilities (e.g., "use mcp_mcp-multimodal_vl_image_understand to recognize images") should reference MCP tool names in steps"""

# ============================================================
# 脚本合并原则(引导 LLM 合并批量同类操作,降低 shell_execute 调用频次)
# 被 REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用(CN/EN)
# ============================================================

SCRIPT_CONSOLIDATION_CN = """- **脚本合并原则（重要,减少 shell_execute 调用次数,节省 LLM token）**：批量同类操作**必须合并为单次 shell_execute 调用**,在 Python 脚本内用循环完成,而非每次操作单独调用 shell_execute。
  - 正例: 单个 Python 脚本用 for 循环将 N 个工作表写入同一个 xlsx 文件(`shell_execute` 调用 1 次)
  - 反例: 对每个工作表分别调用 shell_execute 写入(`shell_execute` 调用 N 次,浪费 N 轮 LLM token)
  - 适用场景: 多工作表 Excel 生成、多文件批量下载、多图表批量生成、多数据集批量导出
  - **数据分析全流程脚本化（关键,根治 shell_execute 过多）**：数据分析任务的清洗+分析+可视化+文件生成环节**必须合并为单个 Python 脚本**,通过 `shell_execute(command="python3 analysis.py")` 一次调用完成,而非逐步用 shell_execute 执行清洗脚本、分析脚本、绘图脚本、写文件脚本。
  - 正例: 单个 analysis.py 完成数据读取→清洗→统计分析→matplotlib 绘图→openpyxl 写 xlsx→python-docx 写 docx(`shell_execute` 调用 1 次)
  - 反例: 分别调用 shell_execute 执行 clean.py、analyze.py、plot.py、write_xlsx.py、write_docx.py(`shell_execute` 调用 5+ 次,浪费 5+ 轮 LLM token,且易超时)
  - 判断标准: 当一个步骤涉及 2 个及以上文件生成操作时,必须合并为单脚本
  - 边界约束: 异类操作(如下载+解析+写入)应分离为独立步骤;单脚本超过 200 行时应拆分为多个脚本以提高可维护性;合并后必须用 py_compile 校验语法"""

SCRIPT_CONSOLIDATION_EN = """- **Script consolidation principle (important, reduce shell_execute call count, save LLM tokens)**: Batch homogeneous operations **must be consolidated into a single shell_execute call**, completed via loops within a Python script, rather than calling shell_execute separately for each operation.
  - Correct: A single Python script uses a for loop to write N worksheets into one xlsx file (`shell_execute` called 1 time)
  - Wrong: Calling shell_execute separately for each worksheet (`shell_execute` called N times, wasting N rounds of LLM tokens)
  - Applicable scenarios: Multi-worksheet Excel generation, multi-file batch download, multi-chart batch generation, multi-dataset batch export
  - **Data analysis full-pipeline scripting (critical, fix excessive shell_execute calls)**: The cleaning + analysis + visualization + file generation phases of data analysis tasks **must be consolidated into a single Python script**, completed via `shell_execute(command="python3 analysis.py")` in one call, rather than incrementally calling shell_execute for cleaning scripts, analysis scripts, plotting scripts, and file-writing scripts.
  - Correct: A single analysis.py completes data reading -> cleaning -> statistical analysis -> matplotlib plotting -> openpyxl writing xlsx -> python-docx writing docx (`shell_execute` called 1 time)
  - Wrong: Separately calling shell_execute to run clean.py, analyze.py, plot.py, write_xlsx.py, write_docx.py (`shell_execute` called 5+ times, wasting 5+ rounds of LLM tokens, prone to timeout)
  - Judgment criteria: When a step involves 2 or more file generation operations, it must be consolidated into a single script
  - Boundary constraints: Heterogeneous operations (e.g., download+parse+write) should be separated into independent steps; a single script exceeding 200 lines should be split into multiple scripts for maintainability; after consolidation, py_compile must be used to validate syntax"""

# ============================================================
# 意图明确自主执行原则(根治 LLM 过度询问用户)
# 被 REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用(CN/EN)
# 修改时只需改此处,CN/EN 同步更新
#
# 设计动机:LLM 识别出"广州"后,计划步骤已明确指示
# "识别到具体城市则使用 amap_weather 查询天气",但执行时 LLM 用
# message_ask_user 询问"是否与图片有关",未自主调用天气工具,导致体验割裂。
# 本片段在 prompt 层引导 LLM 在意图明确且信息充足时直接调用工具,
# 仅在关键信息缺失或存在歧义时才询问用户。
# ============================================================

AUTONOMOUS_TOOL_INVOCATION_CN = """- **意图明确自主执行（重要,避免过度询问用户）**：当用户意图明确且所需信息（工具参数/上下文/前序步骤结果）已充足时,**必须直接调用对应工具自主执行**,严禁用 `message_ask_user` 询问用户是否执行。仅在以下情况使用 `message_ask_user`：关键参数缺失且无法从上下文推断、存在多种合理解释需要用户抉择、需要用户提供凭据/授权/确认高风险操作。
  - 正例: 用户上传图片并问"这是哪里的天气" → 识别出城市后**直接调用** `amap_weather` 查询该城市天气,无需问"是否要查询天气"(计划步骤已写明"识别到具体城市则使用 amap_weather 查询"时同理,识别出城市后直接执行,无需向用户二次确认)
  - 反例: 用户上传图片并问"这是哪里的天气" → 识别出"广州"后,询问用户"是否需要查询广州天气?" — 过度询问,浪费用户时间
  - 反例: 数据分析任务执行中,询问用户"是否需要生成 docx 报告?" / "是否需要包含可视化图表?" — 任务计划已明确交付物,执行中严禁询问用户是否执行计划内的操作(会导致会话卡死,用户不回复则会话无法继续)
  - **交付阶段红线（重要,根治交付确认询问）**：当任务计划明确的交付物已全部生成,必须直接进入交付阶段(summarize),将文件路径填入 attachments 交付给用户。**严禁在交付前询问用户"是否需要交付"/"是否需要这些文件"/"是否需要调整"** — 这类询问会导致会话卡死,文件已生成但用户无法获取。交付物生成后直接交付,用户收到后可自行反馈调整需求。
  - **执行中询问用户红线**：任务计划已明确的操作,执行阶段严禁用 message_ask_user 询问用户"是否执行";仅在以下情况询问: 关键参数缺失(如日期范围、数据源)且无法从上下文推断、存在多种合理解释需要用户抉择、需要用户提供凭据/授权
  - 判断标准: 工具所需参数可从用户消息/附件/上下文/前序步骤结果中获取时即视为"信息充足",直接执行"""

AUTONOMOUS_TOOL_INVOCATION_EN = """- **Autonomous execution on clear intent (important, avoid over-asking the user)**: When the user's intent is clear and the required information (tool parameters/context/previous step results) is sufficient, **you MUST directly call the corresponding tool to execute autonomously**; it is strictly forbidden to use `message_ask_user` to ask the user whether to execute. Use `message_ask_user` only in these cases: key parameters are missing and cannot be inferred from context, multiple reasonable interpretations exist requiring user choice, or credentials/authorization/high-risk operation confirmation is needed from the user.
  - Correct: User uploads an image and asks "what's the weather here" → after recognizing the city, **directly call** `amap_weather` to query that city's weather, no need to ask "do you want to check the weather?" (the same applies when the plan step already states "if a specific city is recognized, use amap_weather to query" — after recognizing the city, execute directly without confirming with the user again)
  - Wrong: User uploads an image and asks "what's the weather here" → after recognizing "Guangzhou", asking the user "do you want to check Guangzhou weather?" — over-asking, wasting user time
  - Wrong: During data analysis task execution, asking the user "do you need a docx report?" / "do you need visualization charts?" — the task plan has already specified deliverables; strictly forbidden to ask the user whether to execute planned operations during execution (causes session deadlock; if the user does not reply, the session cannot continue)
  - **Delivery phase red line (important, eliminate delivery confirmation asking)**: When all deliverables specified in the task plan have been generated, you MUST directly enter the delivery phase (summarize), filling file paths into attachments to deliver to the user. **Strictly forbidden to ask the user "do you need delivery" / "do you need these files" / "do you need adjustments" before delivery** — such asking causes session deadlock; files are generated but the user cannot access them. Deliver directly after generation; the user can provide feedback for adjustments after receiving them.
  - **In-execution user inquiry red line**: For operations already specified in the task plan, strictly forbidden to use message_ask_user during execution to ask "whether to execute"; only inquire in these cases: key parameters missing (e.g., date range, data source) and cannot be inferred from context, multiple reasonable interpretations requiring user choice, credentials/authorization needed
  - Judgment criteria: When the tool's required parameters can be obtained from the user message/attachments/context/previous step results, it is considered "sufficient information"; execute directly"""

# ============================================================
# 文件读取效率约束(根治 LLM 低效读取大文件)
# 被 REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用(CN/EN)
# 修改时只需改此处,CN/EN 同步更新
#
# 设计动机: 二次会话中,LLM 用 cat/head/sed/awk/python3
# 多种方式读取 top20_output.txt,甚至拆成 top20_line_01.txt~05.txt 单行
# 文件再逐个 read_file,极度浪费 LLM token。本片段在 prompt 层引导 LLM
# 使用 read_file 的行范围参数分批读取,而非用 shell 命令或拆分文件。
# ============================================================

FILE_READ_EFFICIENCY_CN = """- **文件读取效率约束（重要,避免低效读取浪费token）**：读取文件内容时必须遵守以下原则：
  - **内置工具优先**：必须用 `read_file(filepath, start_line, end_line)` 分批读取文件,严禁用 `shell_execute` + `cat`/`head`/`sed`/`awk`/`grep` 组合读取文件内容(每次 shell_execute 都消耗一轮 LLM token,远高于 read_file 单次调用)
  - **禁止拆分文件**：严禁将大文件用 `split`/`awk` 命令拆分成多个小文件(如 `top20_line_01.txt` 等)再逐个 `read_file` —— 拆分本身消耗 token,逐个读取再消耗 N 轮 token,极度浪费
  - **禁止管道组合读取**：严禁用 `cat xxx | head -N`、`cat xxx | tail -N`、`sed -n '1,10p' xxx`、`awk 'NR<=10' xxx` 等管道/行范围组合读取文件;读取前 N 行用 `read_file(filepath, start_line=1, end_line=N)`,读取末尾用 `read_file(filepath, start_line=<总行数-N+1>, end_line=<总行数>)`
  - **行范围参数分批**：大文件用 `start_line`/`end_line` 分批读取,单批建议 200-500 行;已读取的部分不重复读取,继续读取下一批即可
  - **搜索定位**：需查找特定内容时先用 `search_in_file(filepath, regex)` 定位行号,再 `read_file` 精确读取该行附近的上下文,而非从头顺序读取整个文件
  - **正例**: `read_file(filepath="/home/ubuntu/output.txt", start_line=1, end_line=200)` → 一次读取前 200 行
  - **反例**: `shell_execute(command="cat /home/ubuntu/output.txt")` → 浪费一轮 LLM token,且大文件输出会撑爆上下文
  - **反例**: `shell_execute(command="cat /tmp/info.txt | head -10")` → 管道组合读取前 10 行,应用 `read_file(filepath="/tmp/info.txt", start_line=1, end_line=10)`
  - **反例**: `shell_execute(command="split -l 1 output.txt top20_line_")` 后逐个 `read_file` —— 拆分+逐个读取,浪费多轮 token"""

FILE_READ_EFFICIENCY_EN = """- **File reading efficiency constraint (important, avoid inefficient reading that wastes tokens)**: When reading file content, you must follow these principles:
  - **Built-in tool priority**: You MUST use `read_file(filepath, start_line, end_line)` to read files in batches; strictly forbidden to use `shell_execute` + `cat`/`head`/`sed`/`awk`/`grep` combinations to read file content (each shell_execute consumes a round of LLM token, far higher than a single read_file call)
  - **No file splitting**: Strictly forbidden to split large files into multiple small files (e.g., `top20_line_01.txt`) using `split`/`awk` commands and then `read_file` them one by one — splitting itself consumes tokens, and reading one by one consumes N rounds of tokens, extremely wasteful
  - **No pipe combination reading**: Strictly forbidden to use `cat xxx | head -N`, `cat xxx | tail -N`, `sed -n '1,10p' xxx`, `awk 'NR<=10' xxx` and other pipe/line-range combinations to read files; to read the first N lines use `read_file(filepath, start_line=1, end_line=N)`, to read the tail use `read_file(filepath, start_line=<total_lines-N+1>, end_line=<total_lines>)`
  - **Line range parameter batching**: For large files, use `start_line`/`end_line` to read in batches, recommended 200-500 lines per batch; do not re-read already read parts, just continue to the next batch
  - **Search positioning**: When you need to find specific content, first use `search_in_file(filepath, regex)` to locate the line number, then `read_file` to precisely read the context around that line, rather than sequentially reading the entire file from the beginning
  - **Correct**: `read_file(filepath="/home/ubuntu/output.txt", start_line=1, end_line=200)` → reads the first 200 lines in one call
  - **Wrong**: `shell_execute(command="cat /home/ubuntu/output.txt")` → wastes a round of LLM token, and large file output will blow up the context
  - **Wrong**: `shell_execute(command="cat /tmp/info.txt | head -10")` → pipe combination reading the first 10 lines; should use `read_file(filepath="/tmp/info.txt", start_line=1, end_line=10)`
  - **Wrong**: `shell_execute(command="split -l 1 output.txt top20_line_")` then `read_file` one by one — splitting + reading one by one, wasting multiple rounds of tokens"""

# ============================================================
# matplotlib 中文图表规范(根治 LLM 硬编码 SimHei 导致中文乱码)
# 被 EXECUTION_PROMPT 引用(CN/EN)
#
# 设计动机: LLM 训练数据中大量中文 matplotlib 教程使用
# `plt.rcParams['font.sans-serif'] = ['SimHei']`,但 SimHei 是
# Windows 字体,Linux 沙箱中不存在,导致图表中文显示为方块。
# 沙箱已通过 sitecustomize.py 自动配置 WenQuanYi Micro Hei 并
# 注册 SimHei 等为别名,本片段在 prompt 层引导 LLM 不要手动
# 覆盖字体设置,直接使用 matplotlib 即可。
# ============================================================

MATPLOTLIB_CHINESE_FONT_CN = """- **matplotlib 中文图表规范（重要,避免中文乱码）**：沙箱已预配置中文字体(WenQuanYi Micro Hei),Python 启动时通过 sitecustomize.py 自动加载,直接使用 matplotlib 即可正常显示中文标题/标签/图例。
  - **严禁手动覆盖字体设置**: 不要写 `plt.rcParams['font.sans-serif'] = ['SimHei']` 或 `plt.rcParams['font.family'] = 'SimHei'` —— SimHei 是 Windows 字体,沙箱无此字体,会导致中文显示为方块
  - **直接使用即可**: 无需任何字体配置代码,`plt.title("每日出入库趋势")`、`plt.xlabel("日期")`、`plt.ylabel("数量(吨)")` 均可正常显示
  - **如必须指定字体**: 仅使用 `WenQuanYi Micro Hei` 或 `Noto Sans CJK JP`(沙箱已安装);严禁使用 SimHei/SimSun/Microsoft YaHei/KaiTi 等 Windows/macOS 字体名
  - **负号显示**: `axes.unicode_minus` 已默认设为 False,无需手动设置
  - **图片保存**: 使用 `plt.savefig(path, dpi=100, bbox_inches='tight')`,默认尺寸已约束为(10,6)适配 Word A4 嵌入
  - **正例**:
    ```python
    import matplotlib.pyplot as plt
    plt.figure()
    plt.plot(dates, values)
    plt.title("2026年5月每日出库趋势")
    plt.xlabel("日期")
    plt.ylabel("出库量(吨)")
    plt.savefig("/home/ubuntu/trend.png", dpi=100, bbox_inches='tight')
    ```
  - **反例**:
    ```python
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif'] = ['SimHei']  # ✗ 乱码!SimHei 不存在
    plt.rcParams['axes.unicode_minus'] = False     # ✗ 多余,已默认配置
    ```"""

MATPLOTLIB_CHINESE_FONT_EN = """- **matplotlib Chinese chart specification (important, avoid garbled Chinese)**: The sandbox has pre-configured Chinese fonts (WenQuanYi Micro Hei), auto-loaded via sitecustomize.py at Python startup; use matplotlib directly to display Chinese titles/labels/legends normally.
  - **Strictly forbidden to manually override font settings**: Do not write `plt.rcParams['font.sans-serif'] = ['SimHei']` or `plt.rcParams['font.family'] = 'SimHei'` — SimHei is a Windows font not present in the sandbox, causing Chinese to display as boxes
  - **Just use it directly**: No font configuration code needed; `plt.title("Daily Trend")`, `plt.xlabel("Date")`, `plt.ylabel("Quantity")` all display normally
  - **If you must specify a font**: Only use `WenQuanYi Micro Hei` or `Noto Sans CJK JP` (installed in sandbox); strictly forbidden to use SimHei/SimSun/Microsoft YaHei/KaiTi etc. Windows/macOS font names
  - **Negative sign display**: `axes.unicode_minus` is already set to False by default, no need to set manually
  - **Image saving**: Use `plt.savefig(path, dpi=100, bbox_inches='tight')`; default size is constrained to (10,6) to fit Word A4 embedding
  - **Correct**:
    ```python
    import matplotlib.pyplot as plt
    plt.figure()
    plt.plot(dates, values)
    plt.title("May 2026 Daily Outbound Trend")
    plt.xlabel("Date")
    plt.ylabel("Quantity (tons)")
    plt.savefig("/home/ubuntu/trend.png", dpi=100, bbox_inches='tight')
    ```
  - **Wrong**:
    ```python
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif'] = ['SimHei']  # ✗ garbled! SimHei does not exist
    plt.rcParams['axes.unicode_minus'] = False     # ✗ redundant, already configured
    ```"""

# ============================================================
# 数据源完整性校验与告知(根治业务数据源不完整时未告知用户+冗余重新导出)
# 被 REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用(CN/EN)
# 修改时只需改此处,CN/EN 同步更新
#
# 设计动机: 会话实测发现,用户要求"某月全部业务数据",智能体导出参数
# 正确(请求了完整月份范围),但业务系统数据实际只到月中某日。智能体在
# 统计分析后才偶然发现数据范围不全,随后重新导出验证(参数本就正确,冗余),
# 且未在交付物中显式标注数据覆盖范围,导致用户基于"全部数据"的错误前提
# 做生产决策。根因: 提示词缺乏"数据源完整性"这一前置校验与告知约束:
#   1. USER_EXPECTATION_ALIGNMENT 侧重"交付物不符合预期",未覆盖"数据源不完整"
#   2. DATA_PERSISTENCE 侧重"批量完整性(M<N)",未覆盖"数据源范围不完整"
#   3. 缺少:导出后立即校验范围、区分"业务源不完整vs参数错误"、避免冗余重新导出、
#      交付物显式标注局限性、识别幂等旧结果
# 修复: 新增数据源完整性校验约束,引导LLM前置校验+显式标注+主动告知
# ============================================================

DATA_SOURCE_INTEGRITY_CN = """- **数据源完整性校验与告知（重要,避免基于不完整数据误导决策）**：导出批量数据（如查询结果/报表/导出文件等）后,必须立即校验数据完整性,并在交付物中显式标注数据覆盖范围:
  - **导出后立即校验（前置化,避免分析后才发现）**：批量数据导出/下载完成后,在进入分析前必须用 `shell_execute` 校验数据覆盖范围: 检查时间/序号字段的范围是否达到请求范围、记录数是否合理、关键字段是否缺失,并将校验结果写入中间文件供后续步骤引用
  - **区分根因（重要,避免冗余重新导出）**：发现数据不完整时,必须区分两种根因:
    - **数据源本身不完整**: 导出参数(范围/筛选)正确,但数据源本身只到某节点(如请求完整范围但数据只到部分)。此时**严禁重新导出**(参数已正确,重新导出得相同结果,属冗余)。应识别为数据源限制,直接进入告知流程
    - **导出参数错误**: 导出参数(范围/筛选条件)错误导致数据不全。此时修正参数后重新导出
  - **识别幂等旧结果**：当工具返回的文件时间戳早于本次调用时间、或数据范围明显未达本次请求范围时,识别为"幂等旧结果"(工具返回了缓存的旧结果而非新结果)。通过 fileName/时间戳/数据范围判断,确认后告知用户数据可能为旧结果,而非盲目基于旧数据分析
  - **交付物显式标注局限性（强制,根治误导决策）**：确认数据源不完整后,必须在交付物中显式标注数据局限性:
    - 报告执行摘要首段: 明确"本报告基于 XX-XX 范围的数据,数据源截至XX,未覆盖请求的完整范围"
    - 图表标题: 标注实际数据范围,如"XX趋势(数据截至XX)"
    - 数据表: 在表头或说明区标注数据覆盖范围
    - 关键结论: 基于"部分数据"的结论必须标注数据前提,不得表述为"整体/全部趋势"
  - **交付消息主动告知（强制,保障用户知情权）**：最终交付消息必须在执行摘要中主动告知数据覆盖范围与局限性,让用户基于准确前提做决策,而非在用户基于"全部数据"前提使用报告后才暴露问题
  - **正例**: 导出某范围数据→校验发现数据只到部分→识别为数据源不完整(参数正确)→不重新导出→报告标注"基于XX-XX范围数据"→交付消息告知"数据源截至XX,报告基于部分范围数据分析"
  - **反例**: 导出某范围数据→分析后发现数据范围不全→重新导出(冗余,参数正确)→报告未标注局限性→用户误以为"全部数据"做决策"""

DATA_SOURCE_INTEGRITY_EN = """- **Data source integrity verification and disclosure (important, avoid misleading decisions based on incomplete data)**: After exporting batch data (e.g., query results/reports/exported files), you MUST immediately verify data integrity and explicitly mark the data coverage in deliverables:
  - **Immediate verification post-export (front-load, avoid discovering after analysis)**: After batch data export/download is complete, before entering analysis, you MUST verify data coverage via `shell_execute`: check whether the range of time/sequence fields reaches the requested range, whether record count is reasonable, and whether key fields are missing; write the verification result to an intermediate file for subsequent steps to reference
  - **Distinguish root cause (important, avoid redundant re-export)**: When data incompleteness is found, distinguish two root causes:
    - **Data source itself incomplete**: Export parameters (range/filter) are correct, but the data source itself only goes up to a certain point (e.g., requested full range but data only covers part). In this case **strictly forbidden to re-export** (parameters are already correct; re-export yields the same result, which is redundant). Recognize this as a data source limitation and proceed directly to the disclosure flow
    - **Export parameter error**: Export parameters (range/filter conditions) are wrong, causing incomplete data. In this case, fix parameters and re-export
  - **Identify idempotent stale results**: When the file timestamp returned by the tool is earlier than the current call time, or the data range clearly does not reach the requested range, recognize it as an "idempotent stale result" (the tool returned a cached old result rather than a new one). Judge by fileName/timestamp/data range; after confirmation, inform the user that the data may be stale, rather than blindly analyzing based on old data
  - **Explicitly mark limitations in deliverables (mandatory, root-cause fix for misleading decisions)**: After confirming the data source is incomplete, you MUST explicitly mark data limitations in deliverables:
    - Report executive summary first paragraph: explicitly state "This report is based on data in the XX-XX range; data source goes up to XX and does not cover the full requested range"
    - Chart titles: mark actual data range, e.g., "XX Trend (data as of XX)"
    - Data tables: mark data coverage in headers or notes
    - Key conclusions: conclusions based on "partial data" must be marked with the data premise, not expressed as "overall/whole trend"
  - **Proactively inform in delivery message (mandatory, ensure user's right to know)**: The final delivery message must proactively inform the data coverage and limitations in the executive summary, so users make decisions based on accurate premises, rather than exposing the issue only after users use the report based on the full-data premise
  - **Correct example**: Export data of a range → verify and find data only covers part → recognize as data source incomplete (parameters correct) → do not re-export → report marks "based on XX-XX range data" → delivery message informs "data source as of XX, report based on partial range data analysis"
  - **Wrong example**: Export data of a range → discover incomplete range after analysis → re-export (redundant, parameters correct) → report does not mark limitations → user mistakenly makes decisions based on the full-data premise"""

# ============================================================
# 输出截断应对策略(根治 LLM 识别到截断后陷入低效读取循环)
# 被 REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用(CN/EN)
# 修改时只需改此处,CN/EN 同步更新
#
# 设计动机: 会话实测发现,智能体在分析阶段读取中间结果文件时,
# read_file 返回"(content truncated)",shell_execute 输出也被截断。
# 智能体反复尝试 read_file 分批、search_in_file、拆分文件、base64 编码
# 等方法绕过截断,浪费 30+ 次工具调用。根因: 提示词缺乏"截断是上下文
# 压缩而非工具错误"的认知,以及"中间结果仅供脚本消费,不需 LLM 完整读取"
# 的约束。修复: 新增截断识别与应对策略片段。
# ============================================================

OUTPUT_TRUNCATION_STRATEGY_CN = """- **输出截断应对策略（重要,避免低效读取循环浪费工具调用）**：当工具返回内容出现截断标记时,必须识别为"上下文压缩"而非工具错误,并停止反复读取:
  - **识别截断信号**：以下标记表示输出被上下文压缩截断(非工具故障):
    - `...(content truncated)` / `(file: ..., content truncated, original N chars, ...)` — read_file 返回内容过长被截断
    - `...(file content truncated, original N chars, this is context compression not tool error, ...)` — 文件内容被压缩截断
    - `...(shell output truncated, original N chars, this is context compression not tool error, ...)` — shell_execute 输出被压缩截断
    - `...(truncated)` / `...(truncated, original N chars)` — 通用截断标记
  - **严禁反复读取（重要,根治低效循环）**：识别到截断后,严禁通过以下方式反复尝试获取完整内容:
    - ✗ 多次 read_file 使用不同 start_line/end_line 分批读取中间结果
    - ✗ search_in_file 定位关键词后再 read_file 精确读取上下文
    - ✗ shell_execute + cat/head/tail/sed/awk 重新读取文件
    - ✗ 将文件拆分为多个小文件(split 命令)后逐个读取
    - ✗ base64 编码后再解码查看完整内容
  - **中间结果仅供脚本消费（核心原则）**：中间结果文件(如 analysis_summary.json、exported_data.csv)是**脚本的输入**,不是 LLM 需要完整读取的内容。LLM 只需知道文件路径和用途,由 shell_execute + Python 脚本直接读取并生成交付物
  - **正确应对流程**：识别截断 → 用 shell_execute 运行 Python 脚本,在脚本内用 open()/pandas 读取完整中间结果 → 脚本直接生成交付物(xlsx/docx/png) → 验证交付物完整性
  - **正例**: read_file("analysis.json") 返回截断 → 不再读取 → `shell_execute("python gen_report.py")` 脚本内 `json.load` 读取完整数据 → 生成"经营分析报告.docx"
  - **反例**: read_file("analysis.json") 返回截断 → read_file(start_line=1,end_line=50) → search_in_file("keyword") → read_file 上下文 → split 拆分 → 逐个读取(浪费 8+ 次调用)"""

OUTPUT_TRUNCATION_STRATEGY_EN = """- **Output truncation response strategy (important, avoid inefficient read loops wasting tool calls)**: When tool return content shows truncation markers, you MUST recognize it as "context compression" rather than a tool error, and stop repeatedly reading:
  - **Recognize truncation signals**: The following markers indicate output was truncated by context compression (not a tool failure):
    - `...(content truncated)` / `(file: ..., content truncated)` — read_file returned content too long and was truncated
    - `...(shell output truncated, original N chars, this is context compression not tool error, ...)` — shell_execute output compressed and truncated
    - `...(truncated)` — generic truncation marker
  - **Strictly forbidden to repeatedly read (important, root-cause fix for inefficient loops)**: After recognizing truncation, strictly forbidden to repeatedly attempt to obtain full content via:
    - ✗ Multiple read_file calls with different start_line/end_line to batch-read intermediate results
    - ✗ search_in_file to locate keywords then read_file for precise context
    - ✗ shell_execute + cat/head/tail/sed/awk to re-read the file
    - ✗ Splitting files into multiple small files (split command) then reading one by one
    - ✗ base64 encoding then decoding to view full content
  - **Intermediate results are for script consumption only (core principle)**: Intermediate result files (e.g., analysis_summary.json, exported_data.csv) are **inputs for scripts**, not content the LLM needs to fully read. The LLM only needs to know the file path and purpose; let shell_execute + Python script directly read and generate deliverables
  - **Correct response flow**: Recognize truncation → use shell_execute to run a Python script that reads full intermediate results via open()/pandas inside the script → script directly generates deliverables (xlsx/docx/png) → verify deliverable integrity
  - **Correct example**: read_file("analysis.json") returns truncated → stop reading → `shell_execute("python gen_report.py")` script uses `json.load` to read full data → generates "Business Analysis Report.docx"
  - **Wrong example**: read_file("analysis.json") returns truncated → read_file(start_line=1,end_line=50) → search_in_file("keyword") → read_file context → split into chunks → read one by one (wastes 8+ calls)"""

# ============================================================
# 上下文压缩恢复引导(根治 emergency_compact 后丢步重执)
# 被 REACT_SYSTEM_PROMPT / EXECUTION_PROMPT 引用(CN/EN)
# 修改时只需改此处,CN/EN 同步更新
#
# 设计动机: 会话实测发现,上下文压缩(尤其 emergency_compact)后,
# 智能体丢失"哪些步骤已完成"的记录,重新执行已完成的
# describe/getDownloadTaskList/验证步骤,导致冗余工具调用。
# 根因: 压缩后仅保留 key_facts + 最近 N 条消息,前序步骤的
# 用户消息(含 prior_steps_context)被压缩。修复:
#   1. 代码层: extract_key_facts 新增 step_completed 分类(已实现)
#   2. 提示词层: 本片段引导 LLM 压缩后优先检查已有交付物和步骤完成状态
# ============================================================

CONTEXT_COMPRESSION_RECOVERY_CN = """- **上下文压缩恢复引导（重要,避免压缩后重复执行已完成步骤）**：当系统提示中出现"[会话进展摘要]"或"[step_completed]"关键事实时,表明上下文已被压缩,你必须先恢复对已完成工作的认知再继续:
  - **优先检查已有交付物（压缩后第一步）**：上下文压缩后,首先用 `shell_execute("ls -la /home/ubuntu/")` 检查根目录已生成的交付物文件,并核对"[会话进展摘要]"中记录的步骤转移(→步骤: xxx)。已生成的文件可直接复用,严禁重新生成
  - **核对步骤完成状态（重要,防丢步重执）**：系统注入的"[之前操作的关键记录]"中若含 `[step_completed]` 条目,表示对应步骤已完成。**严禁重新执行这些步骤**:
    - 已完成的MCP工具调用不再重复(导出/查询/下载等,文件已生成,用 `ls` 确认后直接复用)
    - 已完成的数据校验/验证步骤不重复执行(结论已记录在摘要中)
  - **区分"已完成"与"需重试"**：[step_completed] 条目表示步骤成功完成;若某步骤在摘要中标记为失败或未完成,才允许重新执行
  - **基于已有成果继续推进**：压缩后的行动基线是"已完成的步骤产出已存在",应基于已有文件和数据继续后续步骤,而非从头开始。若不确定某步骤是否完成,优先 `ls` 检查文件是否存在,而非重新执行整个步骤
  - **正例**: 压缩后 → 看到 [step_completed] 步骤1已完成: 已导出5月数据 → `ls` 确认 data.xlsx 存在 → 直接基于 data.xlsx 继续分析步骤
  - **反例**: 压缩后 → 忽略关键记录 → 重新调用MCP导出工具 → 重新下载(浪费 3+ 次调用,且导出参数可能与上次不同)"""

CONTEXT_COMPRESSION_RECOVERY_EN = """- **Context compression recovery guide (important, avoid re-executing completed steps after compression)**: When the system prompt contains "[Session Progress Summary]" or "[step_completed]" key facts, it indicates context has been compressed; you MUST first recover awareness of completed work before continuing:
  - **Check existing deliverables first (first step after compression)**: After context compression, first use `shell_execute("ls -la /home/ubuntu/")` to check deliverable files already generated in the root directory, and cross-check step transitions (→step: xxx) recorded in the "[Session Progress Summary]". Generated files can be directly reused; strictly forbidden to regenerate
  - **Verify step completion status (important, prevent step-loss re-execution)**: If "[Previous operation key records]" injected by the system contains `[step_completed]` entries, those steps are completed. **Strictly forbidden to re-execute these steps**:
    - Completed MCP tool calls should not be repeated (export/query/download, file already generated, confirm via `ls` then reuse directly)
    - Completed data verification steps should not be repeated (conclusions already recorded in summary)
  - **Distinguish "completed" from "needs retry"**: [step_completed] entries indicate steps completed successfully; only if a step is marked failed or incomplete in the summary may it be re-executed
  - **Continue from existing results**: The action baseline after compression is "completed step outputs already exist"; continue subsequent steps based on existing files and data, not from scratch. If unsure whether a step was completed, prefer `ls` to check file existence rather than re-executing the entire step
  - **Correct example**: After compression → see [step_completed] step 1 completed: May data exported → `ls` confirms data.xlsx exists → continue analysis step directly based on data.xlsx
  - **Wrong example**: After compression → ignore key records → re-run MCP export tool → re-download (wastes 5+ calls, and export parameters may differ from last time)"""
