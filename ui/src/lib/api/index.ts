/**
 * API 模块统一导出
 */

// 核心 fetch 封装
export {
  request,
  get,
  post,
  put,
  del,
  createSSEConnection,
  createSSEStream,
  parseSSEStream,
  ApiError,
  API_CONFIG,
  getToken,
  setToken,
  removeToken,
  getRefreshToken,
  setRefreshToken,
  onAuthExpired,
} from "./fetch";

// 类型定义
export type {
  ApiResponse,
  SessionStatus,
  ExecutionStatus,
  ToolEventStatus,
  MCPTransport,
  LLMConfig,
  AgentConfig,
  ListMCPServerItem,
  MCPServerConfig,
  MCPConfig,
  MCPServersData,
  ListA2AServerItem,
  A2AServersData,
  CreateA2AServerParams,
  FileInfo,
  FileUploadParams,
  Session,
  SessionDetail,
  SessionsData,
  CreateSessionParams,
  ChatMessage,
  ChatParams,
  PlanStep,
  PlanEvent,
  StepEvent,
  ToolEvent,
  SSEEventType,
  SSEEventData,
  SSEEventHandler,
  SessionFile,
  ViewFileParams,
  ViewShellParams,
  UserRole,
  User,
  LoginParams,
  LoginResponse,
  RegisterParams,
  RefreshTokenParams,
  RefreshTokenResponse,
  ChangePasswordParams,
} from "./types";

export { configApi } from "./config";
export { fileApi } from "./file";
export { sessionApi } from "./session";
export { authApi } from "./auth";

