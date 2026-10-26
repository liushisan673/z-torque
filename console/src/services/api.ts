// 统一 API 请求层（见 docs/阶段10 3.4 节）
// 集中处理：响应解包、错误解析、Trace ID、认证失效、文件上传、幂等头。
import type { ApiEnvelope } from "../types";

declare global {
  interface Window {
    __API_BASE__?: string;
  }
}

const BASE =
  (typeof window !== "undefined" && window.__API_BASE__) ||
  (typeof import.meta !== "undefined" && (import.meta as any).env?.VITE_API_BASE) ||
  "http://127.0.0.1:8000";

export class ApiRequestError extends Error {
  readonly code: string;
  readonly status: number;
  readonly traceId?: string;

  constructor(code: string, message: string, status: number, traceId?: string) {
    super(message);
    this.name = "ApiRequestError";
    this.code = code;
    this.status = status;
    this.traceId = traceId;
  }
}

const ACCESS_TOKEN_KEY = "z_torque_access_token";
const REFRESH_TOKEN_KEY = "z_torque_refresh_token";

export function setAccessToken(token: string | null): void {
  if (!token) {
    localStorage.removeItem(ACCESS_TOKEN_KEY);
  } else {
    localStorage.setItem(ACCESS_TOKEN_KEY, token);
  }
}

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function setRefreshToken(token: string | null): void {
  if (!token) {
    localStorage.removeItem(REFRESH_TOKEN_KEY);
  } else {
    localStorage.setItem(REFRESH_TOKEN_KEY, token);
  }
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY);
}

let authFailureHandler: (() => void) | null = null;

/** 注册认证失效回调（由认证 store 调用），401 时触发。 */
export function onAuthFailure(handler: () => void): void {
  authFailureHandler = handler;
}

// ---- 认证刷新：单飞（并发 401 共享同一次刷新），避免 refresh_token 轮换竞态 ----
let refreshInFlight: Promise<boolean> | null = null;

async function doRefresh(): Promise<boolean> {
  const refresh = getRefreshToken();
  if (!refresh) return false;
  try {
    // 原始 fetch，不走 request()：避免刷新失败再次触发 401 处理形成递归
    const resp = await fetch(buildUrl("/api/z-torque/auth/refresh"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
    if (!resp.ok) {
      setRefreshToken(null);
      return false;
    }
    const envelope = await parseEnvelope<{ access_token: string; refresh_token?: string }>(resp);
    setAccessToken(envelope.data.access_token);
    setRefreshToken(envelope.data.refresh_token ?? null);
    return true;
  } catch {
    return false;
  }
}

async function refreshAccessToken(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = doRefresh().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

/** 发起请求；遇 401 时尝试刷新凭证并重试一次，仍 401 则原样返回由调用方处理。 */
export async function fetchWithAuth(url: string, init: RequestInit): Promise<Response> {
  let resp: Response;
  try {
    resp = await fetch(url, init);
  } catch {
    throw new ApiRequestError("AI_SERVICE_UNAVAILABLE", "无法连接到后端服务", 503);
  }
  if (resp.status === 401 && (await refreshAccessToken())) {
    const retryInit: RequestInit = {
      ...init,
      headers: { ...init.headers, Authorization: `Bearer ${getAccessToken()}` },
    };
    try {
      resp = await fetch(url, retryInit);
    } catch {
      throw new ApiRequestError("AI_SERVICE_UNAVAILABLE", "无法连接到后端服务", 503);
    }
  }
  return resp;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | undefined | null>;
  headers?: Record<string, string>;
  idempotencyKey?: string;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = new URL(BASE + path, window.location.origin);
  if (query) {
    Object.entries(query).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") {
        url.searchParams.set(k, String(v));
      }
    });
  }
  return url.toString();
}

async function parseEnvelope<T>(resp: Response): Promise<ApiEnvelope<T>> {
  const contentType = resp.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    throw new ApiRequestError("AI_INTERNAL_ERROR", "服务返回了非预期内容", resp.status);
  }
  const traceId = resp.headers.get("X-Trace-ID") ?? undefined;
  const payload = (await resp.json()) as Partial<ApiEnvelope<T>>;
  return {
    code: payload.code ?? -1,
    message: payload.message ?? "未知错误",
    data: payload.data as T,
    trace_id: payload.trace_id ?? traceId ?? "",
  };
}

/**
 * 发起请求并返回已解包的 data 字段。业务错误抛出 ApiRequestError。
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, query, headers, idempotencyKey, signal } = options;
  const url = buildUrl(path, query);
  const token = getAccessToken();
  const init: RequestInit = {
    method,
    credentials: "same-origin",
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
    signal,
  };
  if (body !== undefined) {
    init.headers = { ...init.headers, "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  if (idempotencyKey) {
    init.headers = { ...init.headers, "Idempotency-Key": idempotencyKey };
  }

  const resp = await fetchWithAuth(url, init);

  const traceId = resp.headers.get("X-Trace-ID") ?? undefined;

  if (resp.status === 401) {
    authFailureHandler?.();
    setAccessToken(null);
    setRefreshToken(null);
    const envelope = await safeEnvelope(resp);
    throw new ApiRequestError(
      String(envelope?.code ?? "AI_AUTH_FAILED"),
      envelope?.message ?? "认证已失效",
      resp.status,
      envelope?.trace_id ?? traceId,
    );
  }

  if (!resp.ok) {
    const envelope = await safeEnvelope(resp);
    throw new ApiRequestError(
      String(envelope?.code ?? "AI_INTERNAL_ERROR"),
      envelope?.message ?? `请求失败（${resp.status}）`,
      resp.status,
      envelope?.trace_id ?? traceId,
    );
  }

  const envelope = await parseEnvelope<T>(resp);
  if (envelope.code !== 0) {
    throw new ApiRequestError(envelope.code ? String(envelope.code) : "AI_INTERNAL_ERROR", envelope.message, resp.status, envelope.trace_id || traceId);
  }
  return envelope.data;
}

/**
 * 受保护文件下载：先带 Bearer 头取回二进制，再触发浏览器保存。
 * 直接 `<a href>` 无法携带 Authorization（跨源 + 无 cookie 场景），会 401。
 */
export async function downloadFile(path: string, filename: string): Promise<void> {
  const url = buildUrl(path);
  const token = getAccessToken();
  const resp = await fetchWithAuth(url, {
    method: "GET",
    credentials: "same-origin",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!resp.ok) {
    const envelope = await safeEnvelope(resp);
    throw new ApiRequestError(
      String(envelope?.code ?? "AI_INTERNAL_ERROR"),
      envelope?.message ?? `下载失败（${resp.status}）`,
      resp.status,
    );
  }
  const blob = await resp.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
}

async function safeEnvelope(resp: Response): Promise<Partial<ApiEnvelope<unknown>> | null> {
  try {
    const contentType = resp.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) return null;
    return (await resp.json()) as Partial<ApiEnvelope<unknown>>;
  } catch {
    return null;
  }
}

/** 文件上传（multipart），返回已解包 data。
 * 用 XHR 而非 fetch：fetch 不支持上传方向的进度事件，无法实现真实上传进度条。
 * 认证/错误处理与 request() 对齐：401 先刷新凭证重试一次，仍失败走认证失效回调。 */
export async function uploadFile<T>(
  path: string,
  file: File,
  extra?: { conversationId?: string; signal?: AbortSignal; onProgress?: (percent: number) => void },
): Promise<T> {
  const form = new FormData();
  form.append("upload", file);
  const url = buildUrl(path, { conversation_id: extra?.conversationId });

  // 单次上传：完成后 resolve xhr（错误处理需要 status / 响应头 / 响应体）
  const sendOnce = () =>
    new Promise<XMLHttpRequest>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      // 与 request() 一致携带 Bearer 头：跨源场景（dev 直连后端端口）下 cookie 不可用，必须依赖 Authorization
      const token = getAccessToken();
      if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) extra?.onProgress?.(Math.round((e.loaded / e.total) * 100));
      };
      xhr.onload = () => resolve(xhr);
      xhr.onerror = () => reject(new ApiRequestError("AI_SERVICE_UNAVAILABLE", "无法连接到后端服务", 503));
      xhr.onabort = () => reject(new DOMException("上传已取消", "AbortError"));
      if (extra?.signal) {
        if (extra.signal.aborted) {
          xhr.abort();
        } else {
          extra.signal.addEventListener("abort", () => xhr.abort(), { once: true });
        }
      }
      xhr.send(form);
    });

  let xhr = await sendOnce();
  if (xhr.status === 401 && (await refreshAccessToken())) {
    xhr = await sendOnce();
  }

  const traceId = xhr.getResponseHeader("X-Trace-ID") ?? undefined;
  const ok = xhr.status >= 200 && xhr.status < 300;
  const contentType = xhr.getResponseHeader("Content-Type") ?? "";
  const envelope = contentType.includes("application/json") ? safeEnvelopeText(xhr.responseText) : null;

  if (xhr.status === 401) {
    authFailureHandler?.();
    setAccessToken(null);
    setRefreshToken(null);
    throw new ApiRequestError(
      String(envelope?.code ?? "AI_AUTH_FAILED"),
      envelope?.message ?? "上传失败",
      xhr.status,
      envelope?.trace_id ?? traceId,
    );
  }
  if (!ok) {
    throw new ApiRequestError(
      String(envelope?.code ?? "AI_INTERNAL_ERROR"),
      envelope?.message ?? "上传失败",
      xhr.status,
      envelope?.trace_id ?? traceId,
    );
  }
  if (!contentType.includes("application/json")) {
    throw new ApiRequestError("AI_INTERNAL_ERROR", "服务返回了非预期内容", xhr.status, traceId);
  }
  return envelope?.data as T;
}

// XHR 场景的响应体解包（对应 safeEnvelope 的 Response 版本）
function safeEnvelopeText(text: string): Partial<ApiEnvelope<unknown>> | null {
  try {
    return JSON.parse(text) as Partial<ApiEnvelope<unknown>>;
  } catch {
    return null;
  }
}
