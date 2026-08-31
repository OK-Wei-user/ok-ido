/**
 * API 统一响应格式
 */
export type ApiResponse<T = unknown> = {
  code: number;
  msg: string;
  data: T | null;
};

/**
 * 会话状态
 */
export type SessionStatus = "pending" | "running" | "waiting" | "completed";

/**
 * 执行状态
 */
export type ExecutionStatus = "pending" | "running" | "completed" | "failed";

/**
 * 工具事件状态
 */
export type ToolEventStatus = "calling" | "called";

/**
 * MCP 传输类型
 */
export type MCPTransport = "stdio" | "sse" | "streamable_http";

// ==================== 配置模块类型 ====================

/**
 * LLM 配置
 */
export type LLMConfig = {
  base_url?: string;
  api_key?: string;
  model_name?: string;
  temperature?: number;
  max_tokens?: number;
  [key: string]: unknown;
};

/**
 * Agent 通用配置
 */
export type AgentConfig = {
  max_iterations?: number;
  max_retries?: number;
  max_search_results?: number;
  [key: string]: unknown;
};

/**
 * MCP 服务器列表项（GET 响应）
 */
export type ListMCPServerItem = {
  server_name: string;
  enabled: boolean;
  transport: MCPTransport;
  tools: string[];
};

/**
 * MCP 服务器列表响应
 */
export type MCPServersData = {
  mcp_servers: ListMCPServerItem[];
};

/**
 * MCP 服务器配置（POST 请求体中单个服务器的配置）
 */
export type MCPServerConfig = {
  transport?: MCPTransport;
  enabled?: boolean;
  description?: string | null;
  env?: Record<string, unknown> | null;
  command?: string | null;
  args?: string[] | null;
  url?: string | null;
  headers?: Record<string, unknown> | null;
  [key: string]: unknown;
};

/**
 * MCP 配置（POST 新增 MCP 服务的请求体）
 */
export type MCPConfig = {
  mcpServers: Record<string, MCPServerConfig>;
  [key: string]: unknown;
};

/**
 * A2A 服务器列表项（GET 响应）
 */
export type ListA2AServerItem = {
  id: string;
  name: string;
  description: string;
  input_modes: string[];
  output_modes: string[];
  streaming: boolean;
  push_notifications: boolean;
  enabled: boolean;
};

/**
 * A2A 服务器列表响应
 */
export type A2AServersData = {
  a2a_servers: ListA2AServerItem[];
};

/**
 * 新增 A2A 服务器请求参数
 */
export type CreateA2AServerParams = {
  base_url: string;
};

// ==================== 文件模块类型 ====================

/**
 * 文件信息
 */
export type FileInfo = {
  id: string;
  filename: string;
  filepath: string;
  key: string;
  extension: string;
  content_type: string;
  size: number;
  [key: string]: unknown;
};

/**
 * 文件上传请求参数
 */
export type FileUploadParams = {
  file: File;
  session_id?: string;
};

// ==================== 会话模块类型 ====================

/**
 * 会话信息
 */
export type Session = {
  session_id: string;
  title: string;
  latest_message: string;
  latest_message_at: string;
  status: SessionStatus;
  unread_message_count: number;
  [key: string]: unknown;
};

/**
 * 会话列表响应
 */
export type SessionsData = {
  sessions: Session[];
};

/**
 * 创建会话请求参数
 */
export type CreateSessionParams = {
  title?: string;
  [key: string]: unknown;
};

/**
 * 聊天消息
 *
 * is_streaming: 流式delta标记，true 表示该消息为流式输出的增量chunk，
 *               仅前端累积显示，不持久化DB。仅 summarize() 流式输出会产生此标记。
 * is_final: 最终答案标记，true 表示该消息为会话最终答案（summarize 输出）。
 *           is_final=true 时前端用完整内容替换之前的流式delta累积。
 * is_thinking: 思考过程标记，true 表示该消息为 LLM 思考内容(reasoning_content)的流式增量。
 *              配合 is_streaming=true 仅前端累积显示（不持久化DB），路由到「思考中」区域展示。
 *              思考增量与最终答案增量按 is_thinking 分组聚合，互不合并。
 */
export type ChatMessage = {
  role: "user" | "assistant" | "system";
  message: string;
  attachments?: Array<{
    file_id: string;
    filename: string;
    [key: string]: unknown;
  }>;
  is_streaming?: boolean;
  is_final?: boolean;
  is_thinking?: boolean;
  [key: string]: unknown;
};

/**
 * 聊天请求参数
 * message 为空时用于流式拉取未完成任务的事件列表
 */
export type ChatParams = {
  message?: string;
  attachments?: string[];
  [key: string]: unknown;
};

/**
 * 会话详情（含事件列表，与 chat 流式响应格式一致）
 */
export type SessionDetail = Session & {
  events?: SSEEventData[];
  /** 沙箱ID: 非空表示沙箱仍存在(TTL内),前端据此显示VNC远程桌面入口 */
  sandbox_id?: string;
};

/**
 * 计划步骤
 */
export type PlanStep = {
  id: string;
  description: string;
  status: ExecutionStatus;
  [key: string]: unknown;
};

/**
 * 计划事件
 */
export type PlanEvent = {
  steps: PlanStep[];
  [key: string]: unknown;
};

/**
 * 步骤事件
 */
export type StepEvent = {
  id: string;
  status: ExecutionStatus;
  description: string;
  [key: string]: unknown;
};

/**
 * 工具调用事件
 *
 * is_streaming: Shell 输出流式增量标记，true 表示该事件为命令执行期间的中间轮询输出，
 *               仅前端累积显示（不持久化DB）。相同 tool_call_id 的 streaming 事件 console 累积 append，
 *               CALLED 事件 console replace 为完整输出。
 */
export type ToolEvent = {
  name: string;
  function: string;
  args: Record<string, unknown>;
  content?: unknown;
  status?: ToolEventStatus;
  is_streaming?: boolean;
  [key: string]: unknown;
};

/**
 * SSE 事件类型
 */
export type SSEEventType =
  | "message"
  | "title"
  | "plan"
  | "step"
  | "tool"
  | "wait"
  | "done"
  | "error";

/**
 * SSE 事件数据
 */
export type SSEEventData =
  | { type: "message"; data: ChatMessage }
  | { type: "title"; data: { title: string } }
  | { type: "plan"; data: PlanEvent }
  | { type: "step"; data: StepEvent }
  | { type: "tool"; data: ToolEvent }
  | { type: "wait"; data: Record<string, unknown> }
  | { type: "done"; data: Record<string, unknown> }
  | { type: "error"; data: { error: string } };

/**
 * SSE 事件处理器
 */
export type SSEEventHandler = (event: SSEEventData) => void;

/**
 * 会话文件信息
 */
export type SessionFile = {
  id: string;
  filename: string;
  filepath: string;
  key: string;
  extension: string;
  content_type: string;
  size: number;
  [key: string]: unknown;
};

/**
 * 查看文件内容请求参数
 */
export type ViewFileParams = {
  filepath: string;
  [key: string]: unknown;
};

/**
 * 查看 Shell 输出请求参数
 */
export type ViewShellParams = {
  shell_session_id: string;
  [key: string]: unknown;
};

// ==================== 深度研究模块类型 ====================

/**
 * 单条研究洞察（与后端 ResearchInsight 对齐）
 */
export type ResearchInsight = {
  content: string;
  source_url: string;
  source_title: string;
  relevance_score: number;
  [key: string]: unknown;
};

/**
 * 研究总结（与后端 ResearchSummary 对齐）
 * - key_findings: relevance_score >= 0.8 的核心发现
 * - additional_findings: 0.5 <= score < 0.8 的补充发现
 * - supplementary: score < 0.5 的参考信息
 */
export type ResearchSummary = {
  query: string;
  key_findings: ResearchInsight[];
  additional_findings: ResearchInsight[];
  supplementary: ResearchInsight[];
  follow_up_queries: string[];
  total_sources: number;
  [key: string]: unknown;
};

/**
 * 深度研究工具事件 content（与后端 DeepResearchToolContent 对齐）
 */
export type DeepResearchToolContent = {
  summary: ResearchSummary;
  [key: string]: unknown;
};

// ==================== 认证模块类型 ====================

export type UserRole = "admin" | "user";

export type User = {
  user_id: string;
  username: string;
  phone: string;
  role: UserRole;
  is_active: boolean;
  created_at?: string;
};

export type LoginParams = {
  username: string;
  password: string;
};

export type LoginResponse = {
  access_token: string;
  refresh_token: string;
  token_type: string;
  user_id: string;
  username: string;
  role: string;
};

export type RegisterParams = {
  username: string;
  phone: string;
  password: string;
};

export type RefreshTokenParams = {
  refresh_token: string;
};

export type RefreshTokenResponse = {
  access_token: string;
  refresh_token: string;
  token_type: string;
};

export type ChangePasswordParams = {
  old_password: string;
  new_password: string;
};

