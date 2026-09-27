/**
 * 同源 API 客户端。
 *
 * - 只请求相对路径 `/api/v1/...`，Cookie 走 `credentials: "same-origin"`。
 * - CSRF 令牌只保存在内存里，由 `setCsrfToken` 注入，写操作自动带 `X-CSRF-Token`。
 * - 不保存密码、会话 Cookie 或令牌到 localStorage/sessionStorage。
 */

const API_BASE = "/api/v1";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;

  constructor(status: number, code: string, message: string, requestId = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

let csrfToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

/** 401 统一回调：由状态层清空会话并回到登录界面。 */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 0) return error.message;
    return error.requestId !== "" ? `${error.message}（请求 ${error.requestId}）` : error.message;
  }
  return "发生未知错误";
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  const isWrite = method !== "GET" && method !== "HEAD";
  if (isWrite && csrfToken !== null) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  if (init.body !== undefined && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers,
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "网络请求失败，请确认本地服务已启动");
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  let body: unknown = null;
  if (text.length > 0) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }

  if (!response.ok) {
    const parsed = (body ?? {}) as { code?: unknown; message?: unknown; requestId?: unknown };
    const code = typeof parsed.code === "string" ? parsed.code : "HTTP_ERROR";
    const message =
      typeof parsed.message === "string" ? parsed.message : `请求失败（HTTP ${response.status}）`;
    const requestId = typeof parsed.requestId === "string" ? parsed.requestId : "";
    if (response.status === 401) {
      unauthorizedHandler?.();
    }
    throw new ApiError(response.status, code, message, requestId);
  }

  if (body === null) {
    throw new ApiError(response.status, "INVALID_RESPONSE", "服务端返回了无法解析的响应体");
  }
  return body as T;
}
