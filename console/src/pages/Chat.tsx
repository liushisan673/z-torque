import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { request, uploadFile, ApiRequestError } from "../services/api";
import { openGetStream, openPostStream } from "../services/sse";
import type { Agent, ChatPendingConfirmation, ChatResponse, Confirmation, Conversation, FileContent, FileMetadata, FileStatus, Message, Task, TaskEvent } from "../types";
import { StatusTag, EmptyState, Alert } from "../components/ui";
import { Loading } from "../components/Loading";
import { Modal } from "../components/Modal";
import { AppTopbar, APP_NAV } from "../components/AppTopbar";

interface TaskStep {
  seq: number;
  eventType: string;
  summary: string;
  time: string;
}

function eventLabel(eventType: string): string {
  const map: Record<string, string> = {
    // 状态事件
    TASK_CREATED: "任务已创建",
    TASK_QUEUED: "排队中",
    TASK_STARTED: "开始执行",
    TASK_MODEL_SUCCEEDED: "模型推理完成",
    TASK_SKILL_SUCCEEDED: "能力执行完成",
    TASK_SUCCEEDED: "任务完成",
    TASK_FAILED: "任务失败",
    TASK_TIMEOUT: "任务超时",
    TASK_CANCELLED: "任务已取消",
    TASK_CANCEL_REQUESTED: "请求取消",
    TASK_WAITING_HUMAN: "等待人工确认",
    TASK_APPROVAL_GRANTED: "已批准",
    TASK_APPROVAL_REJECTED: "已拒绝",
    TASK_SKILL_FAILED: "能力执行失败",
    TASK_MODEL_FAILED: "模型推理失败",
    // 流式阶段事件（与文档 5.3 节事件名对齐，不虚构未发生步骤）
    "task.snapshot": "任务状态快照",
    "task.status": "状态更新",
    "task.delta": "执行进展",
    "task.tool_call": "工具调用",
    "task.tool_result": "工具结果",
    "task.waiting_human": "等待人工确认",
    "task.human_decision": "人工决策",
    "task.result": "最终结果",
    "task.error": "执行出错",
    "task.done": "执行完成",
    // 普通聊天事件（备用，本面板主要展示 Task 事件）
    "message.delta": "生成中",
    "message.result": "生成完成",
    "message.done": "回复结束",
    "message.error": "回复出错",
  };
  return map[eventType] ?? eventType;
}

const TERMINAL = new Set(["SUCCESS", "FAILED", "TIMEOUT", "CANCELLED"]);

// 附件前端校验：类型白名单与大小上限（文档 4.3，最终以后端校验为准）
const ALLOWED_FILE_EXTS = ["pdf", "txt", "md", "csv", "xlsx"];
const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50MB

export function ChatPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [agentId, setAgentId] = useState<string>("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingConv, setLoadingConv] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [attachments, setAttachments] = useState<FileMetadata[]>([]);
  const [uploading, setUploading] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [memory, setMemory] = useState<{ summary: string; source_start: number; source_end: number; updated_at: string } | null>(null);
  const [loadingMemory, setLoadingMemory] = useState(false);
  const [taskHistoryOpen, setTaskHistoryOpen] = useState(false);
  const [taskHistory, setTaskHistory] = useState<Task[]>([]);

  const [task, setTask] = useState<Task | null>(null);
  const [steps, setSteps] = useState<TaskStep[]>([]);
  const [waitingConfirm, setWaitingConfirm] = useState(false);
  // 写入类工具触发的审批挂起信息（工具名 + 参数，带批准/拒绝按钮）
  const [pendingConf, setPendingConf] = useState<ChatPendingConfirmation | null>(null);
  const [childTasks, setChildTasks] = useState<Task[]>([]);
  // Task 面板折叠状态：有等待确认时自动展开并高亮（文档 5.1）
  const [taskPanelOpen, setTaskPanelOpen] = useState(true);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const streamRef = useRef<{ abort: () => void } | null>(null);
  const lastEventIdRef = useRef<string>("");
  // 消息滚动容器引用：新内容上屏时自动滚动到底部
  const messagesScrollRef = useRef<HTMLDivElement | null>(null);

  // 普通聊天流式输出的增量文本（会话页默认流式输出，无需开关）
  const [streamingText, setStreamingText] = useState<string | null>(null);

  // 文件详情弹窗
  const [fileDetail, setFileDetail] = useState<{ meta: FileMetadata; status: FileStatus | null; content: FileContent | null } | null>(null);
  const [fileBusy, setFileBusy] = useState(false);

  // 确认记录
  const [confListOpen, setConfListOpen] = useState(false);
  const [confList, setConfList] = useState<Confirmation[]>([]);
  const [confRecord, setConfRecord] = useState<Confirmation | null>(null);

  const loadAgentsAndConversations = useCallback(async () => {
    setError(null);
    try {
      const [a, c] = await Promise.all([
        request<Agent[]>("/api/z-torque/agents"),
        request<Conversation[]>("/api/z-torque/conversations"),
      ]);
      setAgents(a);
      setConversations(c);
      const usable = a.filter((x) => x.current_published_version_id);
      const def = usable.find((x) => x.is_default_parent) ?? usable[0];
      if (def && !agentId) setAgentId(def.id);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载失败");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void loadAgentsAndConversations();
  }, [loadAgentsAndConversations]);

  // 等待人工确认时自动展开 Task 面板并高亮（文档 5.1）
  useEffect(() => {
    if (waitingConfirm) setTaskPanelOpen(true);
  }, [waitingConfirm]);

  // 消息或流式内容变化时自动滚动到底部，保证最新内容可见
  useEffect(() => {
    const el = messagesScrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, streamingText, pendingConf]);

  // 复制文本到剪贴板，并在短时间内给出「已复制」反馈
  async function copyText(text: string, key: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedId(key);
      window.setTimeout(() => setCopiedId((k) => (k === key ? null : k)), 1500);
    } catch {
      /* 剪贴板不可用时静默失败，不影响主流程 */
    }
  }

  const loadMessages = useCallback(async (convId: string, options?: { quiet?: boolean }): Promise<Message[]> => {
    // quiet 模式：发送后静默刷新，避免消息区闪现加载占位
    if (!options?.quiet) setLoadingConv(true);
    try {
      const msgs = await request<Message[]>(`/api/z-torque/conversations/${convId}/messages`);
      setMessages(msgs);
      return msgs;
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载消息失败");
      return [];
    } finally {
      if (!options?.quiet) setLoadingConv(false);
    }
  }, []);

  // 拉取子任务列表：同 root_task_id 且非根任务本身
  const refreshChildTasks = useCallback(async (rootId: string) => {
    try {
      const all = await request<{ items: Task[] }>("/api/z-torque/tasks", { query: { limit: 100 } });
      setChildTasks(all.items.filter((x) => x.root_task_id === rootId && x.id !== rootId));
    } catch {
      /* 子任务加载失败不影响主流程 */
    }
  }, []);

  // 任务详情 + 事件回放（用于页面刷新/切换会话后恢复 Task 状态）
  const loadTaskDetail = useCallback(async (taskId: string): Promise<Task | null> => {
    try {
      const [t, ev] = await Promise.all([
        request<Task>(`/api/z-torque/tasks/${taskId}`),
        request<{ items: TaskEvent[] }>(`/api/z-torque/tasks/${taskId}/events`),
      ]);
      setTask(t);
      setSteps(ev.items.map((e) => ({ seq: e.seq, eventType: e.event_type, summary: e.summary, time: new Date(e.created_at).toLocaleTimeString() })));
      setWaitingConfirm(t.status === "WAITING_HUMAN");
      const last = ev.items[ev.items.length - 1];
      lastEventIdRef.current = last ? String(last.seq) : "";
      void refreshChildTasks(t.root_task_id || taskId);
      return t;
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载任务详情失败");
      return null;
    }
  }, [refreshChildTasks]);

  function selectConversation(c: Conversation) {
    setConversationId(c.id);
    setAgentId(c.agent_id);
    setTask(null);
    setSteps([]);
    setWaitingConfirm(false);
    setPendingConf(null);
    setChildTasks([]);
    setStreamingText(null);
    setConfRecord(null);
    setFileDetail(null);
    lastEventIdRef.current = "";
    setAttachments([]);
    void (async () => {
      const msgs = await loadMessages(c.id);
      const withTask = [...msgs].reverse().find((m) => m.task_id);
      if (withTask?.task_id) {
        const t = await loadTaskDetail(withTask.task_id);
        if (t && !TERMINAL.has(t.status)) subscribeTask(withTask.task_id, { resume: true });
      }
    })();
    void loadMemory(c.id);
  }

  async function loadMemory(convId: string) {
    setLoadingMemory(true);
    try {
      const m = await request<{ summary: string; source_start: number; source_end: number; updated_at: string } | null>(`/api/z-torque/conversations/${convId}/memory`);
      setMemory(m);
    } catch {
      setMemory(null);
    } finally {
      setLoadingMemory(false);
    }
  }

  async function summarizeMemory() {
    if (!conversationId) return;
    setError(null);
    setLoadingMemory(true);
    try {
      const m = await request<{ summary: string; source_start: number; source_end: number; updated_at: string }>(`/api/z-torque/conversations/${conversationId}/memory/summarize`, { method: "POST" });
      setMemory(m);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "生成摘要失败");
    } finally {
      setLoadingMemory(false);
    }
  }

  async function archiveConversation(id: string) {
    if (!window.confirm("确定归档该会话吗？")) return;
    setError(null);
    try {
      await request(`/api/z-torque/conversations/${id}/archive`, { method: "POST" });
      setConversations((prev) => prev.filter((c) => c.id !== id));
      if (conversationId === id) {
        setConversationId(null);
        setMessages([]);
        setMemory(null);
        setTask(null);
      }
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "归档失败");
    }
  }

  async function openTaskHistory() {
    setError(null);
    setTaskHistoryOpen(true);
    try {
      const r = await request<{ items: Task[] }>("/api/z-torque/tasks", { query: { limit: 20 } });
      setTaskHistory(r.items);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载任务失败");
    }
  }

  async function newConversation() {
    setError(null);
    if (!agentId) return;
    try {
      const c = await request<Conversation>("/api/z-torque/conversations", {
        method: "POST",
        body: { agent_id: agentId },
      });
      setConversations((prev) => [c, ...prev]);
      selectConversation(c);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "创建会话失败");
    }
  }

  async function renameConversation(id: string) {
    const title = renameValue.trim();
    if (!title) return;
    setError(null);
    try {
      await request(`/api/z-torque/conversations/${id}`, { method: "PATCH", body: { title } });
      setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, title } : c)));
      setRenamingId(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "重命名失败");
    }
  }

  function startRename(c: Conversation) {
    setRenamingId(c.id);
    setRenameValue(c.title ?? "");
  }

  async function handleUpload(file: File) {
    // 前端类型与大小校验：给出明确提示，最终仍以后端校验为准（文档 4.3）
    const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
    if (!ALLOWED_FILE_EXTS.includes(ext)) {
      setError(`不支持的文件类型「.${ext || "?"}」，支持 PDF/TXT/MD/CSV/XLSX`);
      return;
    }
    if (file.size > MAX_FILE_SIZE) {
      setError(`文件过大（${(file.size / 1024 / 1024).toFixed(1)}MB），上限 50MB`);
      return;
    }
    setError(null);
    setUploading(true);
    try {
      const meta = await uploadFile<FileMetadata>("/api/z-torque/files", file, {
        conversationId: conversationId ?? undefined,
      });
      setAttachments((prev) => [...prev, meta]);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "上传失败");
    } finally {
      setUploading(false);
    }
  }

  function stopStream() {
    streamRef.current?.abort();
    streamRef.current = null;
  }

  function subscribeTask(taskId: string, options?: { resume?: boolean }) {
    stopStream();
    if (!options?.resume) {
      setSteps([]);
      setTask(null);
      lastEventIdRef.current = "";
    }
    const seen = new Set<string>();
    streamRef.current = openGetStream(`/api/z-torque/tasks/${taskId}/stream`, {
      lastEventId: lastEventIdRef.current || undefined,
      onEvent: (msg) => {
        if (msg.event === "task.snapshot") {
          const d = msg.data as Record<string, unknown>;
          const status = String(d.status ?? "");
          setTask((prev) => ({
            ...(prev ?? ({} as Task)),
            id: taskId,
            status,
            root_task_id: String(d.root_task_id ?? prev?.root_task_id ?? taskId),
            delegation_depth: Number(d.delegation_depth ?? prev?.delegation_depth ?? 0),
            result: (d.result as Record<string, unknown>) ?? prev?.result ?? null,
          } as Task));
          if (status === "WAITING_HUMAN") setWaitingConfirm(true);
          return;
        }
        const key = String(msg.id || "");
        if (seen.has(key)) return;
        seen.add(key);
        if (msg.id) lastEventIdRef.current = String(msg.id);
        const d = msg.data as Record<string, unknown>;
        const eventType = msg.event;
        const seq = Number(d.seq ?? seen.size);
        setSteps((prev) => (prev.some((s) => s.seq === seq) ? prev : [...prev, { seq, eventType, summary: String(d.summary ?? eventLabel(eventType)), time: new Date().toLocaleTimeString() }]));
        if (eventType === "TASK_WAITING_HUMAN") setWaitingConfirm(true);
        if (eventType === "TASK_APPROVAL_GRANTED" || eventType === "TASK_APPROVAL_REJECTED") setWaitingConfirm(false);
        const toState = String(d.to_state ?? "");
        if (toState) {
          setTask((prev) => prev ? { ...prev, status: toState } : prev);
        }
        if (TERMINAL.has(toState)) {
          setWaitingConfirm(false);
        }
      },
      onDone: () => {
        // 流结束后刷新消息、任务详情与子任务
        void (async () => {
          await loadMessages(conversationId ?? "");
          await loadTaskDetail(taskId);
        })();
      },
    });
  }

  // 发送消息：默认流式输出（POST /chat/stream）；
  // 选择智能体（工具链路：任务/人工审批）或携带附件时回退 POST /chat（仅该链路支持）
  async function sendMessage() {
    const text = input.trim();
    if (!text || sending) return;
    setError(null);
    setSending(true);
    let convId = conversationId;
    // 乐观更新：用户消息立即上屏并清空输入框，无需等待后端返回
    const optimistic: Message = {
      id: `pending-${Date.now()}`,
      conversation_id: convId ?? "",
      sender_type: "USER",
      content: text,
      content_parts: null,
      status: "COMPLETED",
      task_id: null,
      error_code: null,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, optimistic]);
    setInput("");
    const contentParts = attachments.map((f) => ({ file_id: f.id }));
    setAttachments([]);
    try {
      if (!convId) {
        const c = await request<Conversation>("/api/z-torque/conversations", { method: "POST", body: { agent_id: agentId || null } });
        setConversations((prev) => [c, ...prev]);
        convId = c.id;
        setConversationId(c.id);
      }
    } catch (err) {
      // 会话创建失败：移除乐观消息并恢复输入内容，便于用户重试
      setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
      setInput(text);
      setError(err instanceof ApiRequestError ? err.message : "发送失败");
      setSending(false);
      return;
    }
    if (agentId || contentParts.length > 0) {
      try {
        const idempotencyKey = crypto.randomUUID();
        const resp = await request<ChatResponse>("/api/z-torque/chat", {
          method: "POST",
          body: {
            agent_id: agentId || null,
            conversation_id: convId,
            message: text,
            content_parts: contentParts.length ? contentParts : null,
          },
          idempotencyKey,
        });
        if (resp.status === "WAITING_HUMAN" && resp.confirmation) {
          // 写入类工具触发人工审批：消息区显示审批卡片，任务面板同步挂起状态
          setPendingConf(resp.confirmation);
          subscribeTask(resp.task_id!);
        } else if (resp.response_type === "TASK" && resp.task_id) {
          subscribeTask(resp.task_id);
        } else {
          setTask(null);
          setSteps([]);
          setWaitingConfirm(false);
          setPendingConf(null);
        }
        await loadMessages(resp.conversation_id, { quiet: true });
        setConversationId(resp.conversation_id);
      } catch (err) {
        // 发送失败：移除乐观消息并恢复输入内容，便于用户重试
        setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
        setInput(text);
        setError(err instanceof ApiRequestError ? err.message : "发送失败");
      } finally {
        setSending(false);
      }
      return;
    }
    // 默认流式输出（仅文本增量，不触发任务）
    setStreamingText("");
    const finish = () => {
      setSending(false);
      setStreamingText(null);
      void loadMessages(convId!);
    };
    streamRef.current = openPostStream(
      "/api/z-torque/chat/stream",
      { agent_id: null, conversation_id: convId, message: text, content_parts: null, stream: true },
      {
        onEvent: (msg) => {
          if (msg.event === "message.delta") {
            const d = msg.data as { content?: string };
            setStreamingText((prev) => (prev ?? "") + (d.content ?? ""));
          } else if (msg.event === "message.error") {
            const d = msg.data as { message?: string };
            setError(d.message ?? "流式响应失败");
            finish();
          }
        },
        onError: (err) => {
          setError(err.message);
          finish();
        },
        onDone: finish,
      },
    );
  }

  // 文件详情 / 状态 / 解析内容
  async function viewFile(meta: FileMetadata) {
    setError(null);
    setFileBusy(true);
    setFileDetail({ meta, status: null, content: null });
    try {
      const [fresh, st] = await Promise.all([
        request<FileMetadata>(`/api/z-torque/files/${meta.id}`),
        request<FileStatus>(`/api/z-torque/files/${meta.id}/status`),
      ]);
      const ct = st.status === "PARSED" ? await request<FileContent>(`/api/z-torque/files/${meta.id}/content`) : null;
      setFileDetail({ meta: fresh, status: st, content: ct });
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载文件详情失败");
    } finally {
      setFileBusy(false);
    }
  }

  async function deleteFile(id: string) {
    if (!window.confirm("确定删除该文件吗？删除后不可恢复。")) return;
    setError(null);
    try {
      await request(`/api/z-torque/files/${id}`, { method: "DELETE" });
      setAttachments((prev) => prev.filter((x) => x.id !== id));
      setFileDetail((prev) => (prev && prev.meta.id === id ? null : prev));
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "删除失败");
    }
  }

  // 从消息入口查看对应 Task 执行过程：展开右侧面板并加载该任务（文档 4.2）
  function viewTaskFromMessage(taskId: string) {
    setTaskPanelOpen(true);
    if (task?.id === taskId) return;
    void (async () => {
      const t = await loadTaskDetail(taskId);
      if (t && !TERMINAL.has(t.status)) subscribeTask(taskId, { resume: true });
    })();
  }

  async function cancelTask() {
    if (!task) return;
    setError(null);
    try {
      await request(`/api/z-torque/tasks/${task.id}/cancel`, { method: "POST" });
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "取消失败");
    }
  }

  async function resolveConfirm(approved: boolean) {
    setError(null);
    try {
      if (pendingConf?.confirmation_id) {
        // 会话内审批：直接使用挂起的 confirmation_id（含工具名与参数的精确审批）
        await request(`/api/z-torque/confirmations/${pendingConf.confirmation_id}/resolve`, { method: "POST", body: { approved } });
      } else {
        // 兜底（刷新后恢复的面板场景）：按任务查询 PENDING 确认项
        const list = await request<{ id: string; status: string; task_id: string | null }[]>("/api/z-torque/confirmations");
        const target = list.find((c) => c.task_id === task?.id && c.status === "PENDING");
        if (!target) {
          setError("未找到待确认项");
          return;
        }
        await request(`/api/z-torque/confirmations/${target.id}/resolve`, { method: "POST", body: { approved } });
      }
      setWaitingConfirm(false);
      setPendingConf(null);
      // 审批后任务流会推送重放执行事件，无需额外轮询
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "操作失败");
    }
  }

  async function loadConfirmations() {
    setError(null);
    try {
      const list = await request<Confirmation[]>("/api/z-torque/confirmations");
      setConfList(list);
      setConfListOpen(true);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载确认记录失败");
    }
  }

  async function openConfRecord(id: string) {
    setError(null);
    try {
      const c = await request<Confirmation>(`/api/z-torque/confirmations/${id}`);
      setConfRecord(c);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载确认详情失败");
    }
  }

  const usableAgents = useMemo(() => agents.filter((a) => a.current_published_version_id), [agents]);
  const currentAgent = useMemo(() => agents.find((a) => a.id === agentId), [agents, agentId]);
  const currentConversation = useMemo(() => conversations.find((c) => c.id === conversationId), [conversations, conversationId]);
  const taskRunning = task !== null && !TERMINAL.has(task.status);

  // Task 状态 → 胶囊样式与文案（用于头部 badge 与面板 pill，统一样式来源）
  const taskBadgeInfo = useMemo(() => {
    if (!task) return { cls: "muted", label: "—" } as const;
    if (task.status === "WAITING_HUMAN") return { cls: "waiting", label: "等待确认" } as const;
    if (task.status === "SUCCESS") return { cls: "success", label: "已完成" } as const;
    if (TERMINAL.has(task.status)) return { cls: "failed", label: "已结束" } as const;
    return { cls: "running", label: "执行中" } as const;
  }, [task]);

  // 时间线节点样式：根据事件类型与位置推断 done/active/waiting/error
  function timelineItemClass(s: TaskStep, idx: number, total: number): string {
    if (s.eventType.includes("FAILED") || s.eventType.includes("ERROR") || s.eventType === "task.error") return "error";
    if (s.eventType === "TASK_WAITING_HUMAN" || s.eventType === "task.waiting_human") return "waiting";
    if (idx === total - 1 && taskRunning) return "active";
    return "done";
  }

  // Agent meta 标签：默认父 Agent / 官方 / 我的
  const agentMetaTag = currentAgent
    ? currentAgent.is_default_parent
      ? { cls: "default", label: "默认" }
      : currentAgent.source_type === "OFFICIAL"
        ? { cls: "official", label: "官方" }
        : { cls: "mine", label: "我的" }
    : null;

  return (
    <div className="chat-page">
      <AppTopbar nav={APP_NAV} />
      <div className="chat-shell">
        {/* 左侧栏：Agent 选择 + 会话列表 */}
        <aside className="chat-sidebar">
          <div className="chat-agent-picker">
            <label className="chat-agent-label">当前智能体</label>
            <select
              className="chat-agent-select"
              value={agentId}
              onChange={(e) => {
                setAgentId(e.target.value);
                setConversationId(null);
                setMessages([]);
                setTask(null);
                setAttachments([]);
              }}
            >
              {usableAgents.length === 0 && <option value="" disabled>暂无可用智能体（需先发布版本）</option>}
              {usableAgents.map((a) => (
                <option key={a.id} value={a.id}>{a.name}{a.is_default_parent ? "（默认）" : ""}</option>
              ))}
            </select>
            {agentMetaTag && (
              <div className="chat-agent-meta">
                <span className={`chat-tag-mini ${agentMetaTag.cls}`}>{agentMetaTag.label}</span>
                <span>{currentAgent?.current_published_version_id ? "已发布" : "未发布"}</span>
              </div>
            )}
          </div>

          <div className="chat-session-toolbar">
            <h3>会话</h3>
            <div style={{ display: "flex", gap: 6 }}>
              <button className="chat-btn-new" type="button" onClick={() => void newConversation()} disabled={!agentId} title="新建会话">
                + 新建
              </button>
              <button className="btn btn-secondary btn-sm" type="button" onClick={() => void openTaskHistory()} title="查看任务历史">任务</button>
            </div>
          </div>

          <div className="chat-session-list">
            {conversations.length === 0 ? (
              <div className="chat-session-empty">暂无会话</div>
            ) : (
              conversations.map((c) => {
                const isActive = c.id === conversationId;
                const showRunning = isActive && taskRunning;
                const dotCls = waitingConfirm ? "waiting" : "running";
                return (
                  <div
                    key={c.id}
                    className={`chat-session-item${isActive ? " active" : ""}`}
                    onClick={() => { if (renamingId !== c.id) selectConversation(c); }}
                  >
                    {renamingId === c.id ? (
                      <div className="chat-session-rename-row" style={{ flex: 1 }}>
                        <input
                          className="input"
                          style={{ flex: 1 }}
                          value={renameValue}
                          onChange={(e) => setRenameValue(e.target.value)}
                          onKeyDown={(e) => { if (e.key === "Enter") void renameConversation(c.id); }}
                          autoFocus
                          onClick={(e) => e.stopPropagation()}
                        />
                        <button className="btn btn-primary btn-sm" onClick={(e) => { e.stopPropagation(); void renameConversation(c.id); }}>确定</button>
                      </div>
                    ) : (
                      <>
                        <span className={`chat-status-dot${showRunning ? " " + dotCls : ""}`} title={waitingConfirm ? "等待确认" : taskRunning ? "任务进行中" : undefined} />
                        <div className="chat-session-body">
                          <div className="chat-session-name">{c.title ?? "未命名会话"}</div>
                          <div className="chat-session-time">{c.last_message_at ? new Date(c.last_message_at).toLocaleString() : "—"}</div>
                        </div>
                        <div className="chat-session-actions">
                          <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); startRename(c); }} title="重命名">重命名</button>
                          <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); void archiveConversation(c.id); }} title="归档">归档</button>
                        </div>
                      </>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </aside>

        {/* 中间对话区 */}
        <section className="chat-main">
          <div className="chat-header">
            <div className="chat-header-title-area">
              <span className="chat-header-title">{currentConversation?.title ?? "新会话"}</span>
              {currentAgent && <span className="chat-agent-chip">{currentAgent.name}</span>}
            </div>
            <div className="chat-header-actions">
              <button className="btn btn-ghost btn-sm" onClick={() => void loadConfirmations()} title="查看确认记录">确认记录</button>
              {task && (
                <button
                  className={`chat-task-badge ${taskBadgeInfo.cls}`}
                  type="button"
                  onClick={() => setTaskPanelOpen(true)}
                  title="点击查看执行过程"
                >
                  {taskBadgeInfo.label}
                </button>
              )}
            </div>
          </div>

          {conversationId && (
            <div className="chat-memory-bar">
              <span className="chat-memory-label">记忆摘要</span>
              {loadingMemory ? (
                <span className="chat-memory-text">加载中…</span>
              ) : memory ? (
                <span className="chat-memory-text" title={memory.summary}>{memory.summary}</span>
              ) : (
                <span className="chat-memory-text">暂无摘要</span>
              )}
              <button className="btn btn-ghost btn-sm" disabled={loadingMemory} onClick={() => void summarizeMemory()}>生成摘要</button>
            </div>
          )}

          <div className="chat-messages" ref={messagesScrollRef}>
            {loadingConv ? (
              <Loading />
            ) : messages.length === 0 && streamingText === null ? (
              <div className="chat-welcome">
                <h2>开始对话</h2>
                <p>选择智能体后输入问题。需要执行技能时将自动创建任务，并在右侧展示执行过程。</p>
              </div>
            ) : (
              <>
                {messages.map((m) => {
                  const isUser = m.sender_type === "USER";
                  return (
                    <div key={m.id} className={`chat-msg ${isUser ? "user" : "assistant"}`}>
                      <div className="chat-msg-avatar">{isUser ? "我" : "Z"}</div>
                      <div className="chat-msg-bubble">
                        <div className="chat-msg-content">{m.content}</div>
                        {!isUser && m.task_id && (
                          <button className="chat-task-link" type="button" onClick={() => viewTaskFromMessage(m.task_id as string)} title="在右侧面板查看该任务执行过程">
                            查看执行过程
                          </button>
                        )}
                        <div className="chat-msg-meta">{new Date(m.created_at).toLocaleTimeString()}</div>
                        {m.status === "FAILED" && (
                          <div className="chat-msg-failed">发送失败{m.error_code ? `（${m.error_code}）` : ""}</div>
                        )}
                      </div>
                    </div>
                  );
                })}
                {streamingText !== null && (
                  <div className="chat-msg assistant streaming">
                    <div className="chat-msg-avatar">Z</div>
                    <div className="chat-msg-bubble">
                      <div className="chat-msg-content chat-streaming-cursor">{streamingText || "…"}</div>
                    </div>
                  </div>
                )}
                {pendingConf && (
                  <div className="chat-msg assistant">
                    <div className="chat-msg-avatar">Z</div>
                    <div className="chat-msg-bubble">
                      <div className="chat-confirm-box">
                        <p>工具「{pendingConf.tool_name ?? "未知工具"}」为写入操作，需人工审批后才会执行。</p>
                        {Object.keys(pendingConf.arguments).length > 0 && (
                          <pre className="chat-confirm-args">{JSON.stringify(pendingConf.arguments, null, 2)}</pre>
                        )}
                        <div className="chat-confirm-actions">
                          <button className="chat-btn-confirm" type="button" onClick={() => void resolveConfirm(true)}>批准执行</button>
                          <button className="chat-btn-reject" type="button" onClick={() => void resolveConfirm(false)}>拒绝</button>
                        </div>
                      </div>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>

          {/* 输入区：卡片式 composer */}
          <div className="chat-composer">
            <div className="chat-composer-inner">
              {attachments.length > 0 && (
                <div className="chat-pending-files">
                  {attachments.map((f) => (
                    <div key={f.id} className="chat-pending-file" onClick={() => void viewFile(f)} title="点击查看文件详情">
                      <span>{f.name}</span>
                      <button
                        className="remove"
                        type="button"
                        onClick={(e) => { e.stopPropagation(); void deleteFile(f.id); }}
                        title="移除附件"
                      >×</button>
                    </div>
                  ))}
                </div>
              )}
              <div className="chat-composer-row">
                <label className="chat-btn-icon" title={uploading ? "上传中…" : "上传附件"} style={{ cursor: uploading ? "wait" : "pointer" }}>
                  {uploading ? (
                    <span className="spinner" style={{ width: 16, height: 16 }} />
                  ) : (
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" /></svg>
                  )}
                  <input type="file" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) void handleUpload(f); e.target.value = ""; }} />
                </label>
                <textarea
                  placeholder="输入消息，Enter 发送，Shift+Enter 换行"
                  value={input}
                  rows={1}
                  onChange={(e) => {
                    setInput(e.target.value);
                    e.target.style.height = "auto";
                    e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px";
                  }}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void sendMessage(); } }}
                />
                <div className="chat-composer-actions">
                  {(streamingText !== null || taskRunning) && (
                    <button className="chat-btn-stop" type="button" onClick={stopStream} title="停止当前流式输出或任务流">
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="1" /></svg>
                    </button>
                  )}
                  <button className="chat-btn-send" type="button" disabled={sending || !input.trim()} onClick={() => void sendMessage()} title="发送">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" /></svg>
                  </button>
                </div>
              </div>
            </div>
            <div className="chat-composer-extra">
              <span style={{ marginLeft: "auto" }}>支持 PDF/TXT/MD/CSV,单文件 ≤ 50MB</span>
            </div>
            {error && (
              <div style={{ maxWidth: "var(--chat-max-w)", margin: "8px auto 0" }}>
                <Alert kind="error">{error}</Alert>
              </div>
            )}
          </div>
        </section>

        {/* 右侧 Task 执行过程面板：可折叠，等待确认时自动展开并高亮（文档 5.1） */}
        {task && (
          <aside className={`chat-task-panel${taskPanelOpen ? "" : " collapsed"}${waitingConfirm ? " alerting" : ""}`}>
            <div className="chat-panel-header">
              <h3>执行过程</h3>
              <button className="chat-panel-close" type="button" onClick={() => setTaskPanelOpen(false)} title="收起面板">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6L6 18M6 6l12 12" /></svg>
              </button>
            </div>

            <div className="chat-panel-status">
              <div className="status-row">
                <span className={`chat-status-pill ${taskBadgeInfo.cls}`}>{taskBadgeInfo.label}</span>
              </div>
              <div className="chat-panel-agent-info">
                {currentAgent?.name ?? "智能体"} · {task.delegation_depth > 0 ? `子任务（第 ${task.delegation_depth} 层）` : "根任务"}
              </div>
              <div className="chat-panel-task-id" onClick={() => void copyText(task.id, "task-id")} title="点击复制完整任务 ID">
                {task.id.slice(0, 10)}…
                {copiedId === "task-id" && <span className="copy-flag">已复制</span>}
              </div>
              {task.delegation_depth > 0 && task.root_task_id && (
                <div className="chat-panel-status-meta">
                  根任务
                  <span style={{ fontFamily: "var(--mono)" }} onClick={(e) => { e.stopPropagation(); void copyText(task.root_task_id, "root-id"); }} title="点击复制根任务 ID">
                    {task.root_task_id.slice(0, 10)}…
                  </span>
                  {copiedId === "root-id" && <span style={{ color: "var(--success)" }}>已复制</span>}
                </div>
              )}
              {task.request_summary && (
                <div className="chat-panel-status-meta">{task.request_summary}</div>
              )}
              {task.error_message && (
                <div className="chat-error-box">
                  {task.error_message}{task.error_code ? `（${task.error_code}）` : ""}
                </div>
              )}
            </div>

            <div className="chat-panel-body">
              {steps.length === 0 ? (
                <div className="loading"><span className="spinner" /> 等待执行…</div>
              ) : (
                <div className="chat-timeline">
                  {steps.map((s, i) => {
                    const cls = timelineItemClass(s, i, steps.length);
                    const label = eventLabel(s.eventType);
                    return (
                      <div key={s.seq} className={`chat-timeline-item ${cls}`}>
                        <div className="chat-timeline-dot" />
                        <div className="chat-timeline-title">{label}</div>
                        {s.summary && s.summary !== label && (
                          <div className="chat-timeline-desc">{s.summary}</div>
                        )}
                        <div className="chat-timeline-time">{s.time}</div>
                      </div>
                    );
                  })}
                </div>
              )}

              {waitingConfirm && (
                <div className="chat-confirm-box">
                  <p>任务需要人工确认，请批准或拒绝以继续执行。</p>
                  <div className="chat-confirm-actions">
                    <button className="chat-btn-confirm" type="button" onClick={() => void resolveConfirm(true)}>确认</button>
                    <button className="chat-btn-reject" type="button" onClick={() => void resolveConfirm(false)}>拒绝</button>
                  </div>
                </div>
              )}

              {task.status === "SUCCESS" && (
                <div className="chat-result-box">
                  {(() => {
                    const r = task.result as Record<string, unknown> | null;
                    if (r && typeof r.summary === "string") return r.summary;
                    if (r && typeof r.message === "string") return r.message;
                    return "任务已完成";
                  })()}
                </div>
              )}

              {childTasks.length > 0 && (
                <div className="chat-delegation-block">
                  <h4>子任务（{childTasks.length}）</h4>
                  {childTasks.map((c) => (
                    <div key={c.id} className="chat-child-task" onClick={() => void loadTaskDetail(c.id)} title="点击查看该子任务">
                      <div className="ct-head">
                        <span className="ct-name">{c.request_summary || c.id.slice(0, 8)}</span>
                        <StatusTag status={c.status} />
                      </div>
                      <div className="ct-meta">层级 {c.delegation_depth}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="chat-panel-actions">
              {taskRunning && (
                <button className="chat-btn-cancel-task" type="button" onClick={() => void cancelTask()}>取消任务</button>
              )}
            </div>
          </aside>
        )}

        {/* 面板收起时的展开按钮（等待确认时带高亮提示点） */}
        {task && !taskPanelOpen && (
          <button className="chat-panel-toggle" type="button" onClick={() => setTaskPanelOpen(true)} title="展开执行过程">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" /></svg>
            {waitingConfirm && <span className="alert-dot" />}
          </button>
        )}
      </div>

      {taskHistoryOpen && (
        <Modal open title="任务历史" onClose={() => setTaskHistoryOpen(false)} width={720}>
          {taskHistory.length === 0 ? (
            <EmptyState title="暂无任务" hint="发起需要执行的请求后会在此记录任务" />
          ) : (
            <table className="table">
              <thead>
                <tr><th>状态</th><th>摘要</th><th>层级</th><th>创建时间</th></tr>
              </thead>
              <tbody>
                {taskHistory.map((t) => (
                  <tr key={t.id}>
                    <td><StatusTag status={t.status} /></td>
                    <td className="text-sm">{t.request_summary}</td>
                    <td className="text-sm">{t.delegation_depth > 0 ? "子任务" : "根任务"}</td>
                    <td className="text-muted text-sm">{new Date(t.created_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Modal>
      )}

      {fileDetail && (
        <Modal open title={`文件详情 · ${fileDetail.meta.name}`} onClose={() => setFileDetail(null)} width={640}>
          {fileBusy ? (
            <Loading />
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div className="text-sm"><span className="text-muted">文件 ID：</span>{fileDetail.meta.id}</div>
              <div className="text-sm"><span className="text-muted">媒体类型：</span>{fileDetail.meta.media_type}</div>
              <div className="text-sm"><span className="text-muted">大小：</span>{fileDetail.meta.size_bytes} 字节</div>
              <div className="text-sm" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="text-muted">状态：</span>
                <StatusTag status={fileDetail.status?.status ?? fileDetail.meta.status} />
              </div>
              {fileDetail.status?.error && <Alert kind="error">{fileDetail.status.error}</Alert>}
              <div>
                <div className="text-sm" style={{ fontWeight: 600, marginBottom: 6 }}>解析内容</div>
                {fileDetail.content ? (
                  <pre style={{ maxHeight: 260, overflow: "auto", background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 8, padding: 10, fontSize: 12.5, whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0 }}>{fileDetail.content.content}</pre>
                ) : (
                  <div className="text-muted text-sm">暂无可用内容（文件未解析成功或正在处理中）。</div>
                )}
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
                <button className="btn btn-danger btn-sm" onClick={() => void deleteFile(fileDetail.meta.id)}>删除文件</button>
                <button className="btn btn-secondary btn-sm" onClick={() => setFileDetail(null)}>关闭</button>
              </div>
            </div>
          )}
        </Modal>
      )}

      {confListOpen && (
        <Modal open title="确认记录" onClose={() => setConfListOpen(false)} width={720}>
          {confList.length === 0 ? (
            <EmptyState title="暂无确认记录" hint="当能力执行需要人工确认时会在此记录" />
          ) : (
            <table className="table">
              <thead>
                <tr><th>状态</th><th>原因</th><th>关联任务</th><th>操作</th></tr>
              </thead>
              <tbody>
                {confList.map((c) => (
                  <tr key={c.id}>
                    <td><StatusTag status={c.status} /></td>
                    <td className="text-sm">{c.reason ?? "—"}</td>
                    <td className="text-sm">{c.task_id ? `${c.task_id.slice(0, 8)}…` : "—"}</td>
                    <td><button className="btn btn-ghost btn-sm" onClick={() => void openConfRecord(c.id)}>查看</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Modal>
      )}

      {confRecord && (
        <Modal open title="确认详情" onClose={() => setConfRecord(null)} width={560}>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div className="text-sm"><span className="text-muted">确认 ID：</span>{confRecord.id}</div>
            <div className="text-sm" style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span className="text-muted">状态：</span>
              <StatusTag status={confRecord.status} />
            </div>
            <div className="text-sm"><span className="text-muted">原因：</span>{confRecord.reason ?? "—"}</div>
            <div className="text-sm"><span className="text-muted">关联任务：</span>{confRecord.task_id ?? "—"}</div>
            <div className="text-sm"><span className="text-muted">发起人：</span>{confRecord.requested_by_user_id || "—"}</div>
            <div className="text-sm"><span className="text-muted">确认人：</span>{confRecord.confirmed_by_user_id ?? "—"}</div>
            {confRecord.skill_version_id && <div className="text-sm"><span className="text-muted">能力版本：</span>{confRecord.skill_version_id}</div>}
            {confRecord.tool_version_id && <div className="text-sm"><span className="text-muted">工具版本：</span>{confRecord.tool_version_id}</div>}
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button className="btn btn-secondary btn-sm" onClick={() => setConfRecord(null)}>关闭</button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
