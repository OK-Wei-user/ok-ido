import type { ApiResponse } from "./types";

export const API_CONFIG = {
  baseURL: process.env.NEXT_PUBLIC_API_BASE_URL || "http://10.235.127.227:8000/api",
  timeout: 30000,
} as const;

const TOKEN_KEY = "auth_token";
const REFRESH_TOKEN_KEY = "auth_refresh_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(TOKEN_KEY, token);
}

export function removeToken(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
}

export function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

export function setRefreshToken(token: string): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(REFRESH_TOKEN_KEY, token);
}

// ==================== 认证过期回调 ====================

/** 认证过期时的回调类型 */
type AuthExpiredCallback = () => void;

let _onAuthExpired: AuthExpiredCallback | null = null;

/**
 * 注册认证过期回调（由 AuthProvider 调用）
 * 当 token 刷新失败时触发，用于跳转登录页
 */
export function onAuthExpired(callback: AuthExpiredCallback): void {
  _onAuthExpired = callback;
}

// ==================== Token 自动刷新 ====================

/** 刷新锁：确保并发401只触发一次刷新 */
let _refreshPromise: Promise<boolean> | null = null;

/**
 * 尝试使用 refresh_token 刷新 access_token
 * 并发调用会共享同一个刷新 Promise
 * @returns true=刷新成功, false=刷新失败
 */
async function tryRefreshToken(): Promise<boolean> {
  // 已有刷新在进行中，复用同一个 Promise
  if (_refreshPromise) return _refreshPromise;

  _refreshPromise = (async () => {
    const refreshToken = getRefreshToken();
    if (!refreshToken) return false;

    try {
      const url = `${API_CONFIG.baseURL}/auth/refresh`;
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });

      if (!response.ok) return false;

      const result: ApiResponse<{ access_token: string; refresh_token: string }> = await response.json();
      if (result.code !== 0 && result.code !== 200) return false;
      if (!result.data) return false;

      // 更新本地存储的 token
      setToken(result.data.access_token);
      setRefreshToken(result.data.refresh_token);
      return true;
    } catch {
      return false;
    } finally {
      _refreshPromise = null;
    }
  })();

  return _refreshPromise;
}

/**
 * 处理认证失败：尝试刷新 token，失败则触发过期回调
 * @returns true=刷新成功可重试, false=需跳转登录
 */
async function handleAuthExpired(): Promise<boolean> {
  const refreshed = await tryRefreshToken();
  if (!refreshed) {
    removeToken();
    _onAuthExpired?.();
    return false;
  }
  return true;
}

/**
 * 自定义错误类
 */
export class ApiError extends Error {
  constructor(
    public code: number,
    public msg: string,
    public data: unknown = null
  ) {
    super(msg);
    this.name = "ApiError";
  }
}

/**
 * 请求选项
 */
type RequestOptions = RequestInit & {
  timeout?: number;
  skipErrorHandler?: boolean;
};

/**
 * 解析响应
 */
async function parseResponse<T>(response: Response): Promise<ApiResponse<T>> {
  const contentType = response.headers.get("content-type");
  
  if (contentType?.includes("application/json")) {
    return await response.json();
  }
  
  // 处理非 JSON 响应（如文件下载）
  const text = await response.text();
  return {
    code: response.ok ? 0 : response.status,
    msg: response.ok ? "success" : text || response.statusText,
    data: text as unknown as T,
  };
}

/**
 * 处理错误响应
 */
async function handleErrorResponse(response: Response): Promise<never> {
  let errorData: ApiResponse;
  
  try {
    errorData = await parseResponse(response);
  } catch {
    errorData = {
      code: response.status,
      msg: response.statusText || "请求失败",
      data: null,
    };
  }
  
  throw new ApiError(errorData.code, errorData.msg, errorData.data);
}

/**
 * 带超时的 fetch
 */
function fetchWithTimeout(
  url: string,
  options: RequestOptions = {},
  timeout: number = API_CONFIG.timeout
): Promise<Response> {
  return new Promise((resolve, reject) => {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => {
      controller.abort();
      reject(new ApiError(408, "请求超时"));
    }, timeout);

    fetch(url, {
      ...options,
      signal: controller.signal,
    })
      .then((response) => {
        clearTimeout(timeoutId);
        resolve(response);
      })
      .catch((error) => {
        clearTimeout(timeoutId);
        if (error.name === "AbortError") {
          reject(new ApiError(408, "请求超时"));
        } else {
          reject(error);
        }
      });
  });
}

/**
 * 核心请求函数（含401自动刷新重试）
 */
export async function request<T = unknown>(
  endpoint: string,
  options: RequestOptions = {}
): Promise<T> {
  const url = endpoint.startsWith("http")
    ? endpoint
    : `${API_CONFIG.baseURL}${endpoint}`;

  const {
    timeout = API_CONFIG.timeout,
    skipErrorHandler = false,
    headers = {},
    ...fetchOptions
  } = options;

  const mergedHeaders: HeadersInit = {
    "Content-Type": "application/json",
    ...headers,
  };

  const token = getToken();
  if (token) {
    (mergedHeaders as Record<string, string>)["Authorization"] = `Bearer ${token}`;
  }

  if (fetchOptions.body instanceof FormData) {
    delete (mergedHeaders as Record<string, string>)["Content-Type"];
  }

  try {
    const response = await fetchWithTimeout(
      url,
      {
        ...fetchOptions,
        headers: mergedHeaders,
      },
      timeout
    );

    // 401 认证失败：尝试刷新 token 后重试一次
    if (response.status === 401) {
      const refreshed = await handleAuthExpired();
      if (!refreshed) {
        throw new ApiError(401, "登录已过期，请重新登录");
      }
      // 用新 token 重试原请求
      const newToken = getToken();
      const retryHeaders: HeadersInit = {
        ...mergedHeaders,
      };
      if (newToken) {
        (retryHeaders as Record<string, string>)["Authorization"] = `Bearer ${newToken}`;
      }
      const retryResponse = await fetchWithTimeout(url, { ...fetchOptions, headers: retryHeaders }, timeout);
      if (!retryResponse.ok) {
        if (skipErrorHandler) return parseResponse<T>(retryResponse) as Promise<T>;
        await handleErrorResponse(retryResponse);
      }
      const retryResult = await parseResponse<T>(retryResponse);
      if (retryResult.code !== 0 && retryResult.code !== 200) {
        if (skipErrorHandler) return retryResult.data as T;
        throw new ApiError(retryResult.code, retryResult.msg, retryResult.data);
      }
      return retryResult.data as T;
    }

    // 处理 HTTP 错误状态码
    if (!response.ok) {
      if (skipErrorHandler) {
        return parseResponse<T>(response) as Promise<T>;
      }
      await handleErrorResponse(response);
    }

    const result = await parseResponse<T>(response);

    // 处理业务错误（code 不在成功范围内）
    if (result.code !== 0 && result.code !== 200) {
      if (skipErrorHandler) {
        return result.data as T;
      }
      throw new ApiError(result.code, result.msg, result.data);
    }

    return result.data as T;
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }

    // 处理网络错误
    if (error instanceof TypeError && error.message === "Failed to fetch") {
      throw new ApiError(500, "网络连接失败，请检查网络设置");
    }

    throw new ApiError(500, error instanceof Error ? error.message : "未知错误");
  }
}

/**
 * GET 请求
 */
export function get<T = unknown>(
  endpoint: string,
  params?: Record<string, string | number | boolean>,
  options?: RequestOptions
): Promise<T> {
  let url = endpoint;
  
  if (params) {
    const searchParams = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null) {
        searchParams.append(key, String(value));
      }
    });
    const queryString = searchParams.toString();
    if (queryString) {
      url += `?${queryString}`;
    }
  }

  return request<T>(url, {
    ...options,
    method: "GET",
  });
}

/**
 * POST 请求
 */
export function post<T = unknown>(
  endpoint: string,
  data?: unknown,
  options?: RequestOptions
): Promise<T> {
  return request<T>(endpoint, {
    ...options,
    method: "POST",
    body: data instanceof FormData ? data : JSON.stringify(data),
  });
}

/**
 * PUT 请求
 */
export function put<T = unknown>(
  endpoint: string,
  data?: unknown,
  options?: RequestOptions
): Promise<T> {
  return request<T>(endpoint, {
    ...options,
    method: "PUT",
    body: JSON.stringify(data),
  });
}

/**
 * DELETE 请求
 */
export function del<T = unknown>(
  endpoint: string,
  options?: RequestOptions
): Promise<T> {
  return request<T>(endpoint, {
    ...options,
    method: "DELETE",
  });
}

/**
 * 创建 SSE 连接
 */
export function createSSEConnection(
  endpoint: string,
  data?: unknown,
  _options?: RequestOptions
): EventSource {
  const url = endpoint.startsWith("http")
    ? endpoint
    : `${API_CONFIG.baseURL}${endpoint}`;

  // 对于 POST 请求，使用 fetch + ReadableStream 方式
  if (data !== undefined) {
    throw new Error(
      "带数据的 SSE 连接请使用 createSSEStream 函数"
    );
  }

  return new EventSource(url);
}

/**
 * 创建流式 SSE 连接（支持 POST 请求，含401自动刷新）
 *
 * @param endpoint  接口路径
 * @param data      请求体
 * @param options   请求选项，可通过 `signal` 传入外部 AbortSignal 以便调用方随时中止连接
 */
export async function createSSEStream(
  endpoint: string,
  data?: unknown,
  options?: RequestOptions
): Promise<ReadableStream<Uint8Array>> {
  const url = endpoint.startsWith("http")
    ? endpoint
    : `${API_CONFIG.baseURL}${endpoint}`;

  const {
    timeout = API_CONFIG.timeout,
    headers = {},
    signal: externalSignal,
    ...fetchOptions
  } = options || {};

  const mergedHeaders: HeadersInit = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
    ...headers,
  };

  const token = getToken();
  if (token) {
    (mergedHeaders as Record<string, string>)["Authorization"] = `Bearer ${token}`;
  }

  const controller = new AbortController();
  // 只对初始连接设置超时，连接建立后会清除
  const timeoutId = setTimeout(() => {
    controller.abort();
  }, timeout);

  // 将外部 AbortSignal 关联到内部 controller，
  // 这样调用方 abort 时会同时中止底层 fetch 连接
  if (externalSignal) {
    if (externalSignal.aborted) {
      clearTimeout(timeoutId);
      controller.abort();
    } else {
      externalSignal.addEventListener(
        "abort",
        () => {
          clearTimeout(timeoutId);
          controller.abort();
        },
        { once: true },
      );
    }
  }

  const doFetch = async (useToken?: string) => {
    const fetchHeaders: HeadersInit = { ...mergedHeaders };
    if (useToken) {
      (fetchHeaders as Record<string, string>)["Authorization"] = `Bearer ${useToken}`;
    }
    return fetch(url, {
      ...fetchOptions,
      method: "POST",
      headers: fetchHeaders,
      body: JSON.stringify(data),
      signal: controller.signal,
    });
  };

  try {
    let response = await doFetch(token || undefined);

    // 连接已建立，清除初始连接的超时
    clearTimeout(timeoutId);

    // 401 认证失败：尝试刷新 token 后重试一次
    if (response.status === 401) {
      const refreshed = await handleAuthExpired();
      if (!refreshed) {
        throw new ApiError(401, "登录已过期，请重新登录");
      }
      // 用新 token 重试
      const newToken = getToken();
      response = await doFetch(newToken || undefined);
      if (!response.ok) {
        await handleErrorResponse(response);
      }
    } else if (!response.ok) {
      await handleErrorResponse(response);
    }

    if (!response.body) {
      throw new ApiError(500, "响应体为空");
    }

    return response.body;
  } catch (error) {
    clearTimeout(timeoutId);
    // 忽略 AbortError，这是正常的连接中止
    if (error instanceof Error && error.name === 'AbortError') {
      throw error; // 重新抛出，让调用方处理
    }
    if (error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(500, error instanceof Error ? error.message : "未知错误");
  }
}

/**
 * 解析 SSE 事件流
 */
export async function parseSSEStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (event: MessageEvent) => void,
  onError?: (error: Error) => void
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();

      if (done) {
        // 处理缓冲区中剩余的数据
        if (buffer.trim()) {
          processSSEBuffer(buffer, onEvent, onError);
        }
        break;
      }

      buffer += decoder.decode(value, { stream: true });

      // 标准化行尾：服务端可能使用 \r\n (CRLF)，统一转为 \n (LF)
      buffer = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");

      const parts = buffer.split("\n\n");
      
      // 保留最后一个不完整的事件（可能没有以 \n\n 结尾）
      buffer = parts.pop() || "";

      // 处理完整的事件
      for (const part of parts) {
        if (part.trim()) {
          processSSEEvent(part, onEvent, onError);
        }
      }
    }
  } catch (error) {
    // 忽略 AbortError，这是正常的连接中止
    if (error instanceof Error && error.name === 'AbortError') {
      return;
    }
    if (onError) {
      onError(error instanceof Error ? error : new Error("读取流失败"));
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // 忽略 releaseLock 错误，可能已经被释放
    }
  }
}

/**
 * 处理单个 SSE 事件
 */
function processSSEEvent(
  eventText: string,
  onEvent: (event: MessageEvent) => void,
  onError?: (error: Error) => void
): void {
  let eventType = "message";
  let eventData = "";
  let eventId = "";

  const lines = eventText.split("\n");
  
  for (const line of lines) {
    if (line.startsWith("event:")) {
      eventType = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      // 支持多行数据
      const dataLine = line.slice(5);
      if (eventData) {
        eventData += "\n" + dataLine;
      } else {
        eventData = dataLine;
      }
    } else if (line.startsWith("id:")) {
      eventId = line.slice(3).trim();
    }
    // 忽略其他行（如 retry:、comment 等）
  }

  if (eventData) {
    try {
      const data = JSON.parse(eventData.trim());
      onEvent(
        new MessageEvent(eventType, {
          data,
          lastEventId: eventId,
        })
      );
    } catch (error) {
      if (onError) {
        onError(
          error instanceof Error
            ? error
            : new Error(`解析 SSE 数据失败: ${eventData}`)
        );
      }
    }
  }
}

/**
 * 处理 SSE 缓冲区（用于处理流结束时的剩余数据）
 */
function processSSEBuffer(
  buffer: string,
  onEvent: (event: MessageEvent) => void,
  onError?: (error: Error) => void
): void {
  const events = buffer.split("\n\n").filter((e) => e.trim());
  for (const event of events) {
    processSSEEvent(event, onEvent, onError);
  }
}

