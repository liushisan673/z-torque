// SSE 客户端：使用 Fetch ReadableStream 解析服务端事件流（见 docs/阶段10 3.6 节）。
// 后端事件格式：`id: <id>\nevent: <event>\ndata: <json>\n\n`
// 认证：统一携带 Authorization Bearer 头，401 时自动刷新凭证重试一次（与 api.ts 的 fetchWithAuth 行为一致）

import { fetchWithAuth, getAccessToken } from "./api";

export interface SseMessage {
  id: string;
  event: string;
  data: unknown;
}

export interface SseHandle {
  abort: () => void;
}

type EventHandler = (msg: SseMessage) => void;

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = getAccessToken();
  return { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...extra };
}

/**
 * 通过 POST 建立流式请求（用于 Chat Stream），逐个事件回调。
 * extraHeaders：附加请求头（如 Idempotency-Key），与认证头合并发送。
 */
export function openPostStream(
  path: string,
  body: unknown,
  handlers: { onEvent: EventHandler; onError?: (err: Error) => void; onDone?: () => void },
  extraHeaders?: Record<string, string>,
): SseHandle {
  const controller = new AbortController();
  void (async () => {
    try {
      const resp = await fetchWithAuth(path, {
        method: "POST",
        credentials: "same-origin",
        headers: authHeaders({ "Content-Type": "application/json", ...extraHeaders }),
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!resp.ok) {
        let detail = `流式请求失败（${resp.status}）`;
        try {
          const ct = resp.headers.get("content-type") || "";
          if (ct.includes("application/json")) {
            const j = (await resp.json()) as { message?: string; code?: string };
            if (j.message) detail = j.message;
          }
        } catch {
          /* ignore */
        }
        handlers.onError?.(new Error(detail));
        return;
      }
      await consumeStream(resp, handlers);
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      handlers.onError?.(err as Error);
    }
  })();
  return { abort: () => controller.abort() };
}

/**
 * 通过 GET 建立流式请求（用于 Task Stream），支持 Last-Event-ID 重连。
 */
export function openGetStream(
  path: string,
  options: { lastEventId?: string; onEvent: EventHandler; onError?: (err: Error) => void; onDone?: () => void },
): SseHandle {
  const controller = new AbortController();
  void (async () => {
    try {
      const headers: Record<string, string> = {};
      if (options.lastEventId) headers["Last-Event-ID"] = options.lastEventId;
      const resp = await fetchWithAuth(path, {
        method: "GET",
        credentials: "same-origin",
        headers: authHeaders(headers),
        signal: controller.signal,
      });
      if (!resp.ok) {
        let detail = `流式请求失败（${resp.status}）`;
        try {
          const ct = resp.headers.get("content-type") || "";
          if (ct.includes("application/json")) {
            const j = (await resp.json()) as { message?: string };
            if (j.message) detail = j.message;
          }
        } catch {
          /* ignore */
        }
        options.onError?.(new Error(detail));
        return;
      }
      await consumeStream(resp, {
        onEvent: options.onEvent,
        onError: options.onError,
        onDone: options.onDone,
      });
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      options.onError?.(err as Error);
    }
  })();
  return { abort: () => controller.abort() };
}

async function consumeStream(
  resp: Response,
  handlers: { onEvent: EventHandler; onError?: (err: Error) => void; onDone?: () => void },
): Promise<void> {
  const reader = resp.body?.getReader();
  if (!reader) {
    handlers.onError?.(new Error("浏览器不支持流式响应"));
    return;
  }
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      for (const raw of parts) {
        const msg = parseEvent(raw);
        if (msg) handlers.onEvent(msg);
      }
    }
    if (buffer.trim()) {
      const msg = parseEvent(buffer);
      if (msg) handlers.onEvent(msg);
    }
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    handlers.onError?.(err as Error);
  } finally {
    handlers.onDone?.();
  }
}

function parseEvent(raw: string): SseMessage | null {
  let id = "";
  let event = "message";
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("id:")) id = line.slice(3).trim();
    else if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (dataLines.length === 0) return null;
  let data: unknown = dataLines.join("\n");
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    /* 保持原始字符串 */
  }
  return { id, event, data };
}
