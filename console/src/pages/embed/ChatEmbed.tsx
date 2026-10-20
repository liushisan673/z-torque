// 嵌入对话页（/embed/chat）：供第三方宿主系统以 iframe 集成的精简对话框。
// 样式布局对照 docs/frontend/embed-preview-demo.html；postMessage 协议见
// docs/Z-Torque_iframe嵌入落地方案_v1.md §5（信封 { source: "z-torque-embed", type, payload }）。
// 认证：平台登录态直通（同站 Cookie + localStorage token）；跨站场景支持宿主经
// postMessage 注入 auth.token 兜底。对话与执行链路复用主 Chat：/chat/stream 流式、
// RunBlock 逐事件完整时间线（pages/chatRun.tsx 共享模块，与轻量控制台会话页保持一致）。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApiRequestError, request, setAccessToken } from "../../services/api";
import { openGetStream, openPostStream, type SseHandle } from "../../services/sse";
import type { Agent, Conversation, Message } from "../../types";
import { RunBlock, eventLabel, localStep, payloadOf, TERMINAL, type ActiveRun } from "../chatRun";

type Phase = "booting" | "unauthorized" | "noagents" | "ready" | "error";

function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
}

// 会话列表项时间（抽屉窄，只显示 月-日 时:分）
function fmtDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${(d.getMonth() + 1).toString().padStart(2, "0")}-${d.getDate().toString().padStart(2, "0")} ${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
}

export function ChatEmbedPage() {
  // URL 参数一次性解析：agent_id 预选 / theme / parent_origin（postMessage 目标）/ starters 快捷提问
  const boot = useMemo(() => {
    const p = new URLSearchParams(window.location.search);
    let parentOrigin: string | null = null;
    const raw = p.get("parent_origin");
    if (raw) {
      try {
        const u = new URL(raw);
        if (u.protocol === "http:" || u.protocol === "https:") parentOrigin = u.origin;
      } catch {
        /* 非法 origin：忽略，不启用桥接 */
      }
    }
    return {
      agentParam: p.get("agent_id") ?? "",
      theme: p.get("theme"),
      parentOrigin,
      starters: (p.get("starters") ?? "").split("|").map((s) => s.trim()).filter(Boolean),
    };
  }, []);

  const inIframe = useMemo(() => window.parent !== window, []);
  const [phase, setPhase] = useState<Phase>("booting");
  const [bootSeq, setBootSeq] = useState(0);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentId, setAgentId] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [streamingText, setStreamingText] = useState<string | null>(null);
  // 内联执行块（与主 Chat 一致：逐事件完整时间线，过程在上、结论在下）
  const [run, setRun] = useState<ActiveRun | null>(null);
  // 任务结论打字机文本（task.done SUCCESS 后逐字上屏，随后由真实消息替换）
  const [resultText, setResultText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [confirmBusy, setConfirmBusy] = useState(false);
  // 历史会话抽屉：点击 ☰ 展开（覆盖式，适配窄 iframe）；currentConvId 为渲染用当前会话 id
  const [convList, setConvList] = useState<Conversation[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [currentConvId, setCurrentConvId] = useState<string | null>(null);

  const conversationIdRef = useRef<string | null>(null);
  const streamRef = useRef<SseHandle | null>(null);
  const taskStreamRef = useRef<SseHandle | null>(null);
  const lastEventIdRef = useRef("");
  const lastStatusRef = useRef("");
  const typeTimerRef = useRef<number | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const agentName = agents.find((a) => a.id === agentId)?.name ?? "";
  // 与主 Chat 一致：执行块非终态或流式进行中即视为忙碌（含等待人工确认）
  const busy = sending || streamingText !== null || (run !== null && !TERMINAL.has(run.status));

  // ---- postMessage 桥：仅与 parent_origin 指定的宿主通信 ----
  const emit = useCallback(
    (type: string, payload?: unknown) => {
      if (!boot.parentOrigin || window.parent === window) return;
      window.parent.postMessage({ source: "z-torque-embed", type, payload }, boot.parentOrigin);
    },
    [boot.parentOrigin],
  );

  // 主题：URL theme=dark → 根元素重指派嵌入变量（卸载时还原）
  useEffect(() => {
    if (boot.theme === "dark") document.documentElement.dataset.embedTheme = "dark";
    else delete document.documentElement.dataset.embedTheme;
    return () => {
      delete document.documentElement.dataset.embedTheme;
    };
  }, [boot.theme]);

  // 宿主 → iframe：跨站兜底注入 token（auth.token），收到后重新探测登录态
  useEffect(() => {
    if (!boot.parentOrigin) return;
    const onMessage = (e: MessageEvent) => {
      if (e.origin !== boot.parentOrigin) return;
      const d = e.data as { type?: string; payload?: { token?: string } } | null;
      if (!d || d.type !== "auth.token" || !d.payload?.token) return;
      setAccessToken(d.payload.token);
      setBootSeq((n) => n + 1);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [boot.parentOrigin]);

  // 拉取历史会话列表（只取活跃会话，与主 Chat 一致）；失败静默不影响对话
  const refreshConvList = useCallback(async (): Promise<void> => {
    try {
      setConvList(await request<Conversation[]>("/api/z-torque/conversations"));
    } catch {
      /* 静默 */
    }
  }, []);

  const loadMessages = useCallback(
    async (convId: string): Promise<void> => {
      try {
        const msgs = await request<Message[]>(`/api/z-torque/conversations/${convId}/messages`);
        setMessages(msgs);
        // 消息变化后会话排序/标题可能变化，静默刷新抽屉列表
        void refreshConvList();
      } catch {
        /* 嵌入页静默：失败时保留当前气泡 */
      }
    },
    [refreshConvList],
  );

  // ---- 登录态探测（可重入）：/auth/me → 已发布智能体列表 → 预选 ----
  // probingRef 防并发重入；silent=true 时保持当前视图（成功直接就绪、失败维持原状），
  // 避免自动重探测时视图闪回「正在连接…」。
  const probingRef = useRef(false);
  const readyEmittedRef = useRef(false);
  const probe = useCallback(
    async (silent: boolean) => {
      if (probingRef.current) return;
      probingRef.current = true;
      if (!silent) setPhase("booting");
      try {
        await request<{ id: string }>("/api/z-torque/auth/me");
        const list = await request<Agent[]>("/api/z-torque/agents");
        const usable = list.filter((a) => a.current_published_version_id);
        if (usable.length === 0) {
          setPhase("noagents");
          return;
        }
        const chosen =
          usable.find((a) => a.id === boot.agentParam) ??
          usable.find((a) => a.is_default_parent) ??
          usable[0];
        setAgents(usable);
        setAgentId(chosen.id);
        setPhase("ready");
        void refreshConvList();
        // ready 只通知宿主一次，自动重探测成功时不重复发送
        if (!readyEmittedRef.current) {
          readyEmittedRef.current = true;
          emit("ready", { version: "v1", agent_name: chosen.name });
        }
      } catch (err) {
        if (err instanceof ApiRequestError && err.status === 401) {
          setPhase("unauthorized");
          emit("auth.required", { reason: "not_logged_in" });
        } else if (!silent) {
          setPhase("error");
        }
      } finally {
        probingRef.current = false;
      }
    },
    [boot.agentParam, emit, refreshConvList],
  );

  // 初次启动 / 手动重试 / 宿主注入 token 后触发探测
  useEffect(() => {
    void probe(false);
  }, [bootSeq, probe]);

  // 登录态自动恢复：用户在「前往登录」打开的新标签页登录后，token 写入同源
  // localStorage 会触发本窗口 storage 事件；此外标签页切回（visibilitychange）
  // 与 iframe 获得焦点（focus）时也静默重探测，三层保障覆盖各返回路径。
  useEffect(() => {
    const reprobe = () => void probe(true);
    const onStorage = (e: StorageEvent) => {
      if (e.key === "z_torque_access_token" && e.newValue) reprobe();
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") reprobe();
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener("focus", reprobe);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener("focus", reprobe);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [probe]);

  const stopStreams = useCallback(() => {
    streamRef.current?.abort();
    taskStreamRef.current?.abort();
    streamRef.current = null;
    taskStreamRef.current = null;
  }, []);

  const clearTypeTimer = useCallback(() => {
    if (typeTimerRef.current !== null) {
      window.clearInterval(typeTimerRef.current);
      typeTimerRef.current = null;
    }
  }, []);

  // 结论打字机：逐字上屏（与主 Chat 结论体验一致），完成后执行收尾
  const startTypewriter = useCallback(
    (content: string, done: () => void) => {
      clearTypeTimer();
      setResultText("");
      let i = 0;
      typeTimerRef.current = window.setInterval(() => {
        i += 2;
        if (i >= content.length) {
          setResultText(content);
          clearTypeTimer();
          done();
          return;
        }
        setResultText(content.slice(0, i));
      }, 24);
    },
    [clearTypeTimer],
  );

  // 卸载时停止一切流与定时器
  useEffect(
    () => () => {
      stopStreams();
      clearTypeTimer();
    },
    [stopStreams, clearTypeTimer],
  );

  // 消息/流式/执行块变化时滚到底部
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, streamingText, run, resultText]);

  // ---- 任务事件流（与主 Chat subscribeTask 逻辑一致）：逐事件完整时间线 ----
  const subscribeTask = useCallback(
    (taskId: string) => {
      // 与主 Chat stopStream 一致：先停掉 POST 流——分流后残余 message.delta 不得
      // 再写 streamingText，否则流式气泡与执行块会同屏互相抢占
      streamRef.current?.abort();
      streamRef.current = null;
      taskStreamRef.current?.abort();
      lastEventIdRef.current = "";
      const seen = new Set<string>();
      // applyRun 仅当当前展示的正是该任务时生效
      const applyRun = (updater: (prev: ActiveRun) => ActiveRun) => {
        setRun((prev) => (prev?.taskId === taskId ? updater(prev) : prev));
      };
      taskStreamRef.current = openGetStream(`/api/z-torque/tasks/${taskId}/stream`, {
        lastEventId: undefined,
        onEvent: (msg) => {
          if (msg.event === "task.snapshot") {
            const d = msg.data as Record<string, unknown>;
            const status = String(d.status ?? "");
            setRun((prev) =>
              prev && prev.taskId === taskId
                ? {
                    ...prev,
                    status,
                    waiting: status === "WAITING_HUMAN" ? true : prev.waiting,
                  }
                : prev,
            );
            if (status && status !== lastStatusRef.current) {
              lastStatusRef.current = status;
              emit("task.updated", { task_id: taskId, status });
            }
            return;
          }
          const key = String(msg.id || "");
          if (seen.has(key)) return;
          seen.add(key);
          if (msg.id) lastEventIdRef.current = String(msg.id);
          const d = msg.data as Record<string, unknown>;
          const eventType = msg.event;

          // task.done：终态收尾（结论打字机 → 清理执行块），与主 Chat 一致
          if (eventType === "task.done") {
            const status = String(d.status ?? "");
            applyRun((prev) => ({ ...prev, status, waiting: false }));
            lastStatusRef.current = status;
            emit("task.completed", { task_id: taskId, status });
            const content = (d.result as Record<string, unknown> | null)?.content;
            const finishUp = () => {
              setRun((prev) => (prev?.taskId === taskId ? null : prev));
              setResultText(null);
              void (async () => {
                const convId = conversationIdRef.current;
                if (convId) await loadMessages(convId);
              })();
            };
            if (status === "SUCCESS" && typeof content === "string" && content) {
              // 结论前补「汇总结论」本地步骤（与主 Chat 一致），随后打字机逐字上屏
              applyRun((prev) =>
                prev.steps.some((s) => s.eventType === "LOCAL_SUMMARY")
                  ? prev
                  : { ...prev, steps: [...prev.steps, localStep("LOCAL_SUMMARY", "根据工具结果整理对用户的答复")] },
              );
              startTypewriter(content, finishUp);
            } else {
              // 非 SUCCESS：与主 Chat 一致直接收尾，失败提示由 loadMessages 拉到的失败消息气泡承载
              finishUp();
            }
            return;
          }

          // 其余事件：步骤逐条追加（逐事件完整时间线，不合并不删减）
          const step = {
            seq: Number(d.seq ?? seen.size),
            eventType,
            summary: String(d.summary ?? eventLabel(eventType)),
            time: new Date().toLocaleTimeString(),
            // 后端把 payload_summary 打平进 data（tool_name/arguments/result 等），兼容嵌套写法
            payload: (d.payload_summary as Record<string, unknown> | undefined) ?? payloadOf(d),
          };
          applyRun((prev) => (prev.steps.some((s) => s.seq === step.seq) ? prev : { ...prev, steps: [...prev.steps, step] }));
          if (eventType === "TASK_WAITING_HUMAN") applyRun((prev) => ({ ...prev, waiting: true }));
          if (eventType === "TASK_APPROVAL_GRANTED" || eventType === "TASK_APPROVAL_REJECTED") {
            applyRun((prev) => ({ ...prev, waiting: false }));
          }
          // 后端每帧都带当前 status，据此同步任务状态
          const toState = String(d.to_state ?? d.status ?? "");
          if (toState) applyRun((prev) => ({ ...prev, status: toState }));
          if (toState && toState !== lastStatusRef.current) {
            lastStatusRef.current = toState;
            emit("task.updated", { task_id: taskId, status: toState });
          }
          if (TERMINAL.has(toState)) applyRun((prev) => ({ ...prev, waiting: false }));
        },
        onDone: () => {
          // task.done 已正常收尾（结论打字机可能进行中）：消息由 finishUp 统一刷新，
          // 此处提前拉取会让真实结论消息与打字机气泡同屏重复（与主 Chat 保护一致）
          if (TERMINAL.has(lastStatusRef.current)) return;
          // 流异常结束的兜底刷新
          void (async () => {
            const convId = conversationIdRef.current;
            if (convId) await loadMessages(convId);
          })();
        },
      });
    },
    [emit, loadMessages, startTypewriter],
  );

  // ---- 发送：建会话 → /chat/stream；分流 message.task_created 切任务事件流 ----
  const sendMessage = useCallback(
    async (presetText?: string) => {
      const text = (presetText ?? input).trim();
      if (!text || busy) return;
      setError(null);
      setSending(true);
      // 与主 Chat/演示页一致：快捷提问直接发送，不回填输入框
      if (presetText === undefined) {
        setInput("");
        if (textareaRef.current) textareaRef.current.style.height = "auto";
      }
      const optimistic: Message = {
        id: `pending-${Date.now()}`,
        conversation_id: conversationIdRef.current ?? "",
        sender_type: "USER",
        content: text,
        content_parts: null,
        status: "COMPLETED",
        task_id: null,
        error_code: null,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, optimistic]);
      emit("message.sent");

      // 无会话先建（/chat/stream 要求 conversation 已存在）
      if (!conversationIdRef.current) {
        try {
          const c = await request<Conversation>("/api/z-torque/conversations", {
            method: "POST",
            body: { agent_id: agentId || null },
          });
          conversationIdRef.current = c.id;
          setCurrentConvId(c.id);
          emit("conversation.started", { conversation_id: c.id, agent_id: agentId });
        } catch (err) {
          setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
          if (presetText === undefined) setInput(text);
          setError(err instanceof Error ? err.message : "发送失败，请重试");
          setSending(false);
          return;
        }
      }
      const convId = conversationIdRef.current!;

      clearTypeTimer();
      setResultText(null);
      streamRef.current?.abort();
      setStreamingText("");
      // 决策轮闲聊直答标记：message.result 已由打字机接管收尾时，onDone 不再重复 loadMessages
      let resultSeen = false;
      streamRef.current = openPostStream(
        "/api/z-torque/chat/stream",
        { agent_id: agentId || null, conversation_id: convId, message: text, content_parts: null, stream: true },
        {
          onEvent: (msg) => {
            if (msg.event === "message.delta") {
              const d = msg.data as { content?: string };
              setStreamingText((prev) => (prev ?? "") + (d.content ?? ""));
            } else if (msg.event === "message.result") {
              // 决策轮闲聊直答：文本已完整生成（非流式），由前端打字机接管上屏
              const d = msg.data as { content?: string };
              if (!d.content) return;
              resultSeen = true;
              setStreamingText(null);
              startTypewriter(d.content, () => {
                setResultText(null);
                setSending(false);
                void loadMessages(convId);
              });
            } else if (msg.event === "message.task_created") {
              // 分流：模型要求调用工具 → 后端已创建任务，切换到任务事件流（执行块承接后续过程）
              const d = msg.data as { task_id?: string; conversation_id?: string };
              if (!d.task_id) return;
              setStreamingText(null);
              lastStatusRef.current = "";
              setRun({
                taskId: d.task_id,
                steps: [localStep("LOCAL_UNDERSTAND", "识别到需要执行，准备创建任务")],
                status: "QUEUED",
                waiting: false,
              });
              emit("task.created", { task_id: d.task_id, conversation_id: d.conversation_id ?? convId });
              subscribeTask(d.task_id);
              // 流由服务端关闭，onDone 统一收尾
            } else if (msg.event === "message.error") {
              // 与主 Chat finish 一致：错误上屏并清掉流式占位，避免 typing 气泡悬挂占用
              const d = msg.data as { message?: string };
              setError(d.message ?? "回复失败，请重试");
              setStreamingText(null);
              setSending(false);
              void loadMessages(convId);
            }
          },
          onError: (err) => {
            if (err instanceof ApiRequestError && err.status === 401) {
              setPhase("unauthorized");
              emit("auth.expired");
            } else {
              setError(err.message || "回复失败，请重试");
            }
            setStreamingText(null);
            setSending(false);
            void loadMessages(convId);
          },
          onDone: () => {
            // resultSeen：打字机进行中，其回调负责 setSending(false) 与 loadMessages 收尾
            if (!resultSeen) {
              setStreamingText(null);
              setSending(false);
              void loadMessages(convId);
            }
          },
        },
        { "Idempotency-Key": crypto.randomUUID() },
      );
    },
    [agentId, busy, clearTypeTimer, emit, input, loadMessages, subscribeTask],
  );

  // ---- 人工确认：反查 PENDING 确认项 → resolve（与主 Chat resolveConfirm 一致） ----
  const resolveConfirm = useCallback(
    async (approved: boolean) => {
      if (!run || confirmBusy) return;
      setConfirmBusy(true);
      try {
        const list = await request<{ id: string; status: string; task_id: string | null }[]>("/api/z-torque/confirmations");
        const confId = list.find((c) => c.task_id === run.taskId && c.status === "PENDING")?.id ?? null;
        if (!confId) {
          setError("未找到待确认项");
          return;
        }
        await request(`/api/z-torque/confirmations/${confId}/resolve`, { method: "POST", body: { approved } });
        // 执行块解除挂起（事件流随后推送已批准/已拒绝步骤）
        setRun((prev) => (prev ? { ...prev, waiting: false } : prev));
      } catch (err) {
        setError(err instanceof Error ? err.message : "操作失败，请重试");
      } finally {
        setConfirmBusy(false);
      }
    },
    [confirmBusy, run],
  );

  // 切换智能体 = 开启新对话（清会话/消息/执行块，与主 Chat 行为一致）
  const switchAgent = useCallback(
    (id: string) => {
      if (busy || id === agentId) return;
      stopStreams();
      clearTypeTimer();
      conversationIdRef.current = null;
      lastStatusRef.current = "";
      setAgentId(id);
      setMessages([]);
      setStreamingText(null);
      setResultText(null);
      setRun(null);
      setError(null);
      setCurrentConvId(null);
    },
    [agentId, busy, clearTypeTimer, stopStreams],
  );

  // 打开历史会话：停流清态 → 按会话切换智能体 → 加载消息（抽屉自动收起）
  const openConversation = useCallback(
    (c: Conversation) => {
      if (busy) return;
      stopStreams();
      clearTypeTimer();
      conversationIdRef.current = c.id;
      lastStatusRef.current = "";
      if (c.agent_id && c.agent_id !== agentId) setAgentId(c.agent_id);
      setMessages([]);
      setStreamingText(null);
      setResultText(null);
      setRun(null);
      setError(null);
      setCurrentConvId(c.id);
      setDrawerOpen(false);
      void loadMessages(c.id);
    },
    [agentId, busy, clearTypeTimer, loadMessages, stopStreams],
  );

  // 新建会话：清空当前轮次，下次发送时再实际创建（与主 Chat 新对话一致）
  const startNewConversation = useCallback(() => {
    if (busy) return;
    stopStreams();
    clearTypeTimer();
    conversationIdRef.current = null;
    lastStatusRef.current = "";
    setMessages([]);
    setStreamingText(null);
    setResultText(null);
    setRun(null);
    setError(null);
    setCurrentConvId(null);
    setDrawerOpen(false);
  }, [busy, clearTypeTimer, stopStreams]);

  const retryLastUser = useCallback(() => {
    if (busy) return;
    const lastUser = [...messages].reverse().find((m) => m.sender_type === "USER" && m.content.trim());
    if (lastUser) void sendMessage(lastUser.content);
  }, [busy, messages, sendMessage]);

  const badge =
    phase === "ready"
      ? { cls: "ok", text: "已连接" }
      : phase === "booting"
        ? { cls: "idle", text: "连接中…" }
        : phase === "unauthorized"
          ? { cls: "err", text: "未登录" }
          : { cls: "err", text: "未连接" };

  return (
    <div className="embed-shell">
      <header className="embed-header">
        {phase === "ready" && (
          <button
            className="embed-icon-btn"
            type="button"
            title="历史会话"
            onClick={() => setDrawerOpen(true)}
          >
            ☰
          </button>
        )}
        <div className="embed-avatar">{(agentName || "Z").charAt(0)}</div>
        <div className="embed-info">
          {phase === "ready" ? (
            <div className="embed-agent-wrap">
              <select
                className="embed-agent-select"
                value={agentId}
                disabled={busy}
                onChange={(e) => switchAgent(e.target.value)}
                title="切换智能体（将开启新对话）"
              >
                {agents.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
            </div>
          ) : (
            <strong style={{ fontSize: 14 }}>Z-Torque</strong>
          )}
          <span className="embed-sub">Z-Torque · 嵌入模式</span>
        </div>
        <span className={`embed-badge ${badge.cls}`}>{badge.text}</span>
        {inIframe && (
          <button
            className="embed-close"
            type="button"
            title="关闭"
            onClick={() => emit("ui.close")}
          >
            ×
          </button>
        )}
      </header>

      {phase === "booting" && <div className="embed-state">正在连接…</div>}
      {phase === "unauthorized" && (
        <div className="embed-state">
          <strong>尚未登录</strong>
          <span>请先登录 Z-Torque 平台；登录完成后返回本窗口<br />将自动检测登录状态。</span>
          <span className="embed-state-hint">
            识别环境：{window.location.origin}
            <br />登录页须使用同一地址（localhost 与 127.0.0.1<br />属不同站点，登录态不互通）
          </span>
          <div className="embed-state-actions">
            <a className="embed-state-btn" href="/login" target="_blank" rel="noreferrer">
              前往登录
            </a>
            <button className="embed-state-btn" type="button" onClick={() => void probe(false)}>
              重新检测
            </button>
          </div>
        </div>
      )}
      {phase === "noagents" && (
        <div className="embed-state">
          <strong>暂无可用智能体</strong>
          <span>请管理员在平台发布智能体后重试。</span>
        </div>
      )}
      {phase === "error" && (
        <div className="embed-state">
          <strong>加载失败</strong>
          <span>网络异常或服务暂不可用。</span>
          <button className="embed-state-btn" type="button" onClick={() => void probe(false)}>
            重试
          </button>
        </div>
      )}

      {phase === "ready" && (
        <>
          <div className="embed-messages" ref={scrollRef}>
            {messages.length === 0 && !run && streamingText === null && resultText === null && (
              <div className="embed-empty">
                <strong>你好，我是{agentName || "Z-Torque 助手"}</strong>
                <span>有什么可以帮你？可直接输入问题。</span>
              </div>
            )}
            {messages.map((m) => {
              if (m.sender_type === "USER") {
                return (
                  <div className="embed-msg user" key={m.id}>
                    <div className="embed-bubble">{m.content}</div>
                    <div className="embed-time">{fmtTime(m.created_at)}</div>
                  </div>
                );
              }
              const failed = !!m.error_code && !m.content.trim();
              return (
                <div className="embed-msg ai" key={m.id}>
                  {failed ? (
                    <div className="embed-bubble" style={{ color: "var(--embed-danger)" }}>
                      发送失败，请重试
                    </div>
                  ) : (
                    <div className="embed-bubble chat-md">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
                    </div>
                  )}
                  <div className="embed-time">{fmtTime(m.created_at)}</div>
                </div>
              );
            })}
            {/* 一轮助手回复（与主 Chat chat-turn 同构）：执行块在上、结论气泡在下，同一容器不互相抢占 */}
            {(run || resultText !== null || streamingText !== null) && (
              <div className="embed-msg ai">
                <div className="embed-turn">
                  {run && (
                    <RunBlock
                      activeRun={run}
                      taskError={null}
                      onResolve={(approved) => void resolveConfirm(approved)}
                    />
                  )}
                  {(resultText !== null || streamingText !== null) && (
                    <div className="embed-bubble chat-md">
                      {resultText !== null ? (
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{resultText || "…"}</ReactMarkdown>
                      ) : streamingText ? (
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{streamingText}</ReactMarkdown>
                      ) : (
                        <div className="embed-typing">
                          <span />
                          <span />
                          <span />
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}
            {error && (
              <div className="embed-error-line">
                <span>{error}</span>
                <button className="embed-retry-btn" type="button" onClick={retryLastUser}>
                  重试
                </button>
              </div>
            )}
          </div>

          <div className="embed-input">
            {boot.starters.length > 0 && (
              <div className="embed-chips">
                {boot.starters.map((s) => (
                  <button
                    key={s}
                    type="button"
                    className="embed-chip"
                    disabled={busy}
                    onClick={() => void sendMessage(s)}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
            <div className="embed-input-box">
              <textarea
                ref={textareaRef}
                className="embed-textarea"
                rows={1}
                placeholder="输入问题，Enter 发送…"
                value={input}
                disabled={busy}
                onChange={(e) => {
                  setInput(e.target.value);
                  e.target.style.height = "auto";
                  e.target.style.height = `${Math.min(e.target.scrollHeight, 100)}px`;
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    void sendMessage();
                  }
                }}
              />
              <button className="embed-send" type="button" disabled={busy || !input.trim()} onClick={() => void sendMessage()}>
                发送
              </button>
            </div>
          </div>
        </>
      )}
      {/* 历史会话抽屉：覆盖式左侧滑出，点遮罩收起；仅就绪后可用 */}
      {phase === "ready" && drawerOpen && (
        <>
          <div className="embed-drawer-mask" onClick={() => setDrawerOpen(false)} />
          <aside className="embed-drawer">
            <div className="embed-drawer-head">
              <strong>历史会话</strong>
              <button className="embed-drawer-new" type="button" onClick={startNewConversation}>
                ＋ 新会话
              </button>
            </div>
            <div className="embed-drawer-list">
              {convList.length === 0 ? (
                <div className="embed-drawer-empty">暂无历史会话</div>
              ) : (
                convList.map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    className={`embed-drawer-item${c.id === currentConvId ? " current" : ""}`}
                    disabled={busy && c.id !== currentConvId}
                    onClick={() => openConversation(c)}
                  >
                    <span className="embed-drawer-title">{c.title || "未命名会话"}</span>
                    <span className="embed-drawer-time">{fmtDate(c.last_message_at)}</span>
                  </button>
                ))
              )}
            </div>
          </aside>
        </>
      )}
    </div>
  );
}
