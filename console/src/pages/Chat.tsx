import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApiRequestError, request, uploadFile } from "../services/api";
import { errorText } from "../services/errorText";
import { readSelectedId, writeSelectedId } from "../services/selectionMemory";
import { openGetStream, openPostStream } from "../services/sse";
import type { Agent, ChatResponse, Confirmation, Conversation, FileContent, FileMetadata, FileStatus, Message, Task, TaskEvent } from "../types";
import { StatusTag, EmptyState, Alert } from "../components/ui";
import { Loading } from "../components/Loading";
import { Modal } from "../components/Modal";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PaneResizer, usePaneWidth } from "../components/PaneResizer";
import { GuideCallout } from "../components/GuideCallout";
import { useToast } from "../components/Toast";
import { AppTopbar, APP_NAV } from "../components/AppTopbar";
// 内联执行块共享模块：与嵌入对话页（/embed/chat）共用类型/工具函数/渲染组件，保证实时链路展示一致
import { RunBlock, eventLabel, localStep, payloadOf, TERMINAL, type ActiveRun, type TaskStep } from "./chatRun";

// 会话记忆摘要（GET /memory 与 POST /memory/summarize 共用返回结构）
interface MemorySummary {
  summary: string;
  source_start: number;
  source_end: number;
  updated_at: string;
}

// 附件前端校验：类型白名单与大小上限（文档 4.3，最终以后端校验为准）
const ALLOWED_FILE_EXTS = ["pdf", "txt", "md", "csv", "xlsx"];
const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50MB

// 首启示例提问：降低"不知道该问什么"的门槛；点击仅填入输入框，不自动发送
const STARTER_PROMPTS = [
  "帮我总结一段文字的关键结论",
  "把下面的内容整理成表格",
  "查一下数据库里的用户表结构",
];

// 长会话渐进渲染：一次只上屏最近 RENDER_STEP 条（Markdown 渲染开销大），更早的消息按需向前加载
const RENDER_STEP = 50;

// 选中记忆：跳转页面后返回时恢复上次会话与智能体（sessionStorage 按标签页隔离）。
// 会话记忆为空串表示"上次停留在新对话"，返回时保持空白而非自动选中第一个会话。
const CONV_KEY = "zt.chat.conversationId";
const AGENT_KEY = "zt.chat.agentId";

export function ChatPage() {
  // 左右分栏宽度：拖拽把手调整左侧会话列表宽度，会话内保持，退出登录恢复默认
  const listPane = usePaneWidth("chat", "--chat-sidebar-w");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  // 会话标题客户端过滤（服务端分页/摘要能力待后端支持后再升级）
  const [convFilter, setConvFilter] = useState("");
  const filteredConvs = useMemo(
    () =>
      convFilter.trim()
        ? conversations.filter((c) => (c.title ?? "").toLowerCase().includes(convFilter.trim().toLowerCase()))
        : conversations,
    [conversations, convFilter],
  );
  const [agentId, setAgentId] = useState<string>("");
  // 智能体选择器自定义下拉：可承载名称 + 描述（原生 select 的 option 不支持描述文案）
  const [agentMenuOpen, setAgentMenuOpen] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingConv, setLoadingConv] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [attachments, setAttachments] = useState<FileMetadata[]>([]);
  const [uploading, setUploading] = useState(false);
  // 附件上传真实进度（XHR progress 事件，0-100；null=不在上传中）
  const [uploadPercent, setUploadPercent] = useState<number | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  // 会话项 ⋯ 下拉菜单：记录当前展开菜单的会话 ID
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null);
  // 记忆摘要弹窗：从 ⋯ 菜单打开，按需加载对应会话的摘要
  const [memoryModal, setMemoryModal] = useState<{ convId: string } | null>(null);
  const [memoryData, setMemoryData] = useState<MemorySummary | null>(null);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const [taskHistoryOpen, setTaskHistoryOpen] = useState(false);
  const [taskHistory, setTaskHistory] = useState<Task[]>([]);

  // 任务级错误文案中文化（历史数据 error_message 为英文，前端统一映射展示）
  function taskErrorText(message: string, code: string | null): string {
    if (code === "AI_TASK_CANCELLED") return "任务已按用户要求取消";
    if (code === "AI_CONFIRMATION_REJECTED") return "用户拒绝工具执行，任务终止";
    return message || code || "任务执行失败";
  }

  const [task, setTask] = useState<Task | null>(null);
  const [steps, setSteps] = useState<TaskStep[]>([]);
  const [waitingConfirm, setWaitingConfirm] = useState(false);
  const [childTasks, setChildTasks] = useState<Task[]>([]);
  // Task 面板：降级为详情入口（默认收起，通过 badge / 消息「查看执行过程」打开）
  const [taskPanelOpen, setTaskPanelOpen] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const streamRef = useRef<{ abort: () => void } | null>(null);
  const lastEventIdRef = useRef<string>("");
  // 消息滚动容器引用：新内容上屏时自动滚动到底部
  const messagesScrollRef = useRef<HTMLDivElement | null>(null);

  // 普通聊天流式输出的增量文本（会话页默认流式输出，无需开关）
  const [streamingText, setStreamingText] = useState<string | null>(null);
  // 长会话渐进渲染窗口：当前渲染的消息条数上限（切换会话时重置）
  const [renderLimit, setRenderLimit] = useState(RENDER_STEP);

  // 任务链路内联执行块（run-block）：任务事件实时驱动步骤逐条增加
  const [activeRun, setActiveRun] = useState<ActiveRun | null>(null);
  // 任务完成后最终结论的打字机文本（来源 task.done 的 result.content）
  const [runAnswer, setRunAnswer] = useState<string | null>(null);
  // 当前这一轮助手回复的开始时间（过程块与结论共用同一时间戳）
  const [turnTime, setTurnTime] = useState<string>("");
  const typeTimerRef = useRef<number | null>(null);
  // 打字机目标（全文 + 收尾回调）：跳过动画时据此一次性上屏完整内容
  const typeTargetRef = useRef<{ text: string; onDone: () => void } | null>(null);
  // 首次进入页面已自动选中过会话的标记（仅生效一次，避免覆盖切智能体的新对话）
  const autoSelectedRef = useRef(false);
  // 当前会话引用：流式回调使用（闭包中的 state 会过期，切换会话后旧回调不得回写视图）
  const conversationIdRef = useRef<string | null>(null);

  // 文件详情弹窗
  const [fileDetail, setFileDetail] = useState<{ meta: FileMetadata; status: FileStatus | null; content: FileContent | null } | null>(null);
  const [fileBusy, setFileBusy] = useState(false);

  // 确认记录
  const [confListOpen, setConfListOpen] = useState(false);
  const [confList, setConfList] = useState<Confirmation[]>([]);
  const [confRecord, setConfRecord] = useState<Confirmation | null>(null);

  // 会话归档可恢复：显示已归档开关 + 恢复入口，避免误归档后永久丢失
  const [showArchived, setShowArchived] = useState(false);
  const [archiveTarget, setArchiveTarget] = useState<Conversation | null>(null);
  const [restoringId, setRestoringId] = useState<string | null>(null);
  // 切换智能体时若存在正在推进的执行链路，先确认再切换（原本会静默丢弃）
  const [pendingAgentId, setPendingAgentId] = useState<string | null>(null);
  // 高危操作：删除附件（不可恢复）需要显式确认
  const [deletingFileId, setDeletingFileId] = useState<string | null>(null);
  // 窄屏抽屉：原实现在 <768px 直接隐藏侧栏，手机上无法切换会话与智能体
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const navigate = useNavigate();
  const { push: toast } = useToast();
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const loadAgentsAndConversations = useCallback(async () => {
    setError(null);
    try {
      const [a, c] = await Promise.all([
        request<Agent[]>("/api/z-torque/agents"),
        request<Conversation[]>("/api/z-torque/conversations", {
          query: { include_archived: showArchived ? "true" : undefined },
        }),
      ]);
      setAgents(a);
      setConversations(c);
      const usable = a.filter((x) => x.current_published_version_id);
      // 智能体恢复：上次选中的仍在可用列表则优先，否则回退默认父智能体 / 第一个
      const rememberedAgentId = readSelectedId(AGENT_KEY);
      const rememberedAgent = rememberedAgentId ? usable.find((x) => x.id === rememberedAgentId) : null;
      const def = rememberedAgent ?? usable.find((x) => x.is_default_parent) ?? usable[0];
      if (def && !agentId) setAgentId(def.id);
      // 会话恢复：跳转回来时优先回到上次会话；记忆为空串表示上次停留在新对话（保持空白）；
      // 从未有记录或记忆会话已不存在（删除/归档）则默认展示第一个，避免主区空白
      if (!autoSelectedRef.current && c.length > 0 && !conversationId) {
        autoSelectedRef.current = true;
        const remembered = readSelectedId(CONV_KEY);
        if (remembered !== "") {
          const target = (remembered && c.find((x) => x.id === remembered)) || c[0];
          selectConversation(target);
        }
      }
    } catch (err) {
      setError(errorText(err, "加载失败"));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 仅刷新会话列表：归档 / 恢复 / 切换「显示已归档」后使用
  const loadConversations = useCallback(async (includeArchived: boolean) => {
    try {
      const c = await request<Conversation[]>("/api/z-torque/conversations", {
        query: { include_archived: includeArchived ? "true" : undefined },
      });
      setConversations(c);
    } catch (err) {
      setError(errorText(err, "加载会话失败"));
    }
  }, []);

  useEffect(() => {
    void loadAgentsAndConversations();
  }, [loadAgentsAndConversations]);

  // 「显示已归档」开关变化后重新拉取（首次加载由 loadAgentsAndConversations 完成）
  const archivedTouchedRef = useRef(false);
  useEffect(() => {
    if (!archivedTouchedRef.current) {
      archivedTouchedRef.current = true;
      return;
    }
    void loadConversations(showArchived);
  }, [showArchived, loadConversations]);

  // ⋯ 菜单展开时监听全局点击：点击菜单外任意位置自动收起
  useEffect(() => {
    if (!menuOpenId) return;
    const close = () => setMenuOpenId(null);
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, [menuOpenId]);

  // 智能体下拉展开时监听全局点击：点击下拉外任意位置自动收起（Esc 同样关闭）
  useEffect(() => {
    if (!agentMenuOpen) return;
    const close = () => setAgentMenuOpen(false);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [agentMenuOpen]);

  // 会话列表 ref：发送完成后判断是否需要同步自动标题（避免回调闭包读到过期列表）
  const conversationsRef = useRef<Conversation[]>([]);
  useEffect(() => {
    conversationsRef.current = conversations;
  }, [conversations]);

  // 首条消息落库后后端自动生成会话标题（取消息内容摘要）；列表中该会话仍无标题时静默刷新
  async function syncAutoTitle(convId: string | null) {
    if (!convId) return;
    if (conversationsRef.current.some((c) => c.id === convId && c.title)) return;
    try {
      const list = await request<Conversation[]>("/api/z-torque/conversations");
      setConversations(list);
    } catch {
      /* 标题同步失败不影响主流程，下次进入页面会显示正确标题 */
    }
  }

  // 消息或流式内容变化时自动滚动到底部，保证最新内容可见
  useEffect(() => {
    const el = messagesScrollRef.current;
    if (!el) return;
    // 「查看更早消息」触发的这次更新不强制滚底：由锚定补偿保持用户当前阅读位置
    if (loadMoreAnchorRef.current !== null) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, streamingText, runAnswer, activeRun]);

  // 长会话渐进渲染：当前实际渲染的消息窗口（取最近的 renderLimit 条）
  const visibleMessages = useMemo(
    () => (renderLimit >= messages.length ? messages : messages.slice(messages.length - renderLimit)),
    [messages, renderLimit],
  );
  // 向前加载更早消息：记录旧视口底距，DOM 更新后补偿差值，阅读位置不跳动
  const loadMoreAnchorRef = useRef<number | null>(null);
  useLayoutEffect(() => {
    const anchor = loadMoreAnchorRef.current;
    if (anchor === null) return;
    loadMoreAnchorRef.current = null;
    const el = messagesScrollRef.current;
    if (el) el.scrollTop = Math.max(0, el.scrollHeight - anchor);
  }, [renderLimit, messages]);

  // 「查看更早的消息」：多渲染一批，并锚定当前阅读位置
  function loadEarlier() {
    const el = messagesScrollRef.current;
    if (el) loadMoreAnchorRef.current = el.scrollHeight - el.scrollTop;
    setRenderLimit((v) => v + RENDER_STEP);
  }

  // 组件卸载时清理打字机定时器，避免内存泄漏
  useEffect(() => () => {
    if (typeTimerRef.current !== null) window.clearTimeout(typeTimerRef.current);
  }, []);

  // 最终结论打字机：逐字上屏（内容来自 task.done 的 result.content 或 CHAT 响应）
  function startTypewriter(text: string, onDone: () => void) {
    clearTypeTimer();
    if (!text) {
      onDone();
      return;
    }
    typeTargetRef.current = { text, onDone };
    // 结论气泡出现即刷新本轮时间戳（与 chat.html 的 finishUi 一致）
    setTurnTime(new Date().toLocaleTimeString());
    setRunAnswer("");
    let i = 0;
    const tick = () => {
      i += 2;
      setRunAnswer(text.slice(0, i));
      if (i < text.length) {
        typeTimerRef.current = window.setTimeout(tick, 16);
      } else {
        typeTimerRef.current = null;
        typeTargetRef.current = null;
        onDone();
      }
    };
    typeTimerRef.current = window.setTimeout(tick, 16);
  }

  function clearTypeTimer() {
    if (typeTimerRef.current !== null) {
      window.clearTimeout(typeTimerRef.current);
      typeTimerRef.current = null;
    }
  }

  // 跳过打字动画：立即完整显示当前结论，收尾流程（刷新真实消息）与自然播完一致
  function skipTypewriter() {
    const target = typeTargetRef.current;
    if (!target || typeTimerRef.current === null) return;
    clearTypeTimer();
    typeTargetRef.current = null;
    setRunAnswer(target.text);
    target.onDone();
  }

  // 开启一轮内联执行块：立刻出现「理解需求」首步（chat.html 阶段 A 的本地步骤）
  function startRun(taskId: string, status = "QUEUED") {
    clearTypeTimer();
    setRunAnswer(null);
    setTurnTime(new Date().toLocaleTimeString());
    setActiveRun({
      taskId,
      steps: [localStep("LOCAL_UNDERSTAND", "识别到需要执行，准备创建任务")],
      status,
      waiting: false,
      stopped: false,
    });
  }

  // 编辑重发：把该用户消息内容回填输入框（后端无消息删除/截断接口，原对话保留，改完作为新消息发送）
  function editUserMessage(m: Message) {
    if (sending) return;
    setInput(m.content);
    inputRef.current?.focus();
  }

  // 重新生成：对最新回答不满意时，取其前最近一条用户消息（含附件）原样重发，追加一份新回答
  function regenerateAnswer(anchor: Message) {
    retryFailedMessage(anchor);
  }

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
      setError(errorText(err, "加载消息失败"));
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
      const mapped: TaskStep[] = ev.items.map((e) => ({
        seq: e.seq,
        eventType: e.event_type,
        summary: e.summary,
        time: new Date(e.created_at).toLocaleTimeString(),
        payload: e.payload_summary,
      }));
      setSteps(mapped);
      // 内联执行块恢复：当前展示的正是该任务时，同步步骤与状态（刷新/切会话场景）
      // 保留本地步骤（seq < 0 的「理解需求」等），真实事件按 seq 追加在后
      setActiveRun((prev) =>
        prev?.taskId === taskId
          ? { ...prev, steps: [...prev.steps.filter((s) => s.seq < 0), ...mapped], status: t.status, waiting: t.status === "WAITING_HUMAN" }
          : prev,
      );
      setWaitingConfirm(t.status === "WAITING_HUMAN");
      const last = ev.items[ev.items.length - 1];
      lastEventIdRef.current = last ? String(last.seq) : "";
      void refreshChildTasks(t.root_task_id || taskId);
      return t;
    } catch (err) {
      setError(errorText(err, "加载任务详情失败"));
      return null;
    }
  }, [refreshChildTasks]);

  function selectConversation(c: Conversation) {
    // 切换会话先中断旧流：旧会话的流式回调不得把消息刷进新会话视图
    stopStream();
    setConversationId(c.id);
    setAgentId(c.agent_id);
    // 选中记忆：跳转后返回恢复上次会话与对应智能体
    writeSelectedId(CONV_KEY, c.id);
    writeSelectedId(AGENT_KEY, c.agent_id);
    conversationIdRef.current = c.id;
    setTask(null);
    setSteps([]);
    setWaitingConfirm(false);
    setChildTasks([]);
    setStreamingText(null);
    setConfRecord(null);
    setFileDetail(null);
    lastEventIdRef.current = "";
    setAttachments([]);
    // 渐进渲染窗口随会话切换重置：新会话从最近一批消息开始
    setRenderLimit(RENDER_STEP);
    // 切换会话：清理打字机与内联执行块（内联块仅在任务仍在执行时于下方恢复）
    clearTypeTimer();
    setActiveRun(null);
    setRunAnswer(null);
    setTurnTime("");
    void (async () => {
      await loadMessages(c.id);
      // 执行中任务恢复：按会话过滤任务列表（消息仅在回复完成后携带 task_id）
      try {
        const r = await request<{ items: Task[] }>("/api/z-torque/tasks", { query: { limit: 20 } });
        const active = r.items.find((t) => t.conversation_id === c.id && !TERMINAL.has(t.status));
        if (!active) return;
        // 先恢复内联执行块骨架，loadTaskDetail 会回填步骤与状态
        setActiveRun({
          taskId: active.id,
          steps: [localStep("LOCAL_UNDERSTAND", "识别到需要执行，准备创建任务")],
          status: active.status,
          waiting: active.status === "WAITING_HUMAN",
          stopped: false,
        });
        setTurnTime(new Date().toLocaleTimeString());
        const t = await loadTaskDetail(active.id);
        if (t && !TERMINAL.has(t.status)) subscribeTask(active.id, { resume: true, convId: c.id });
        else setActiveRun(null); // 历史终态任务仅保留「查看执行过程」入口
      } catch {
        /* 任务列表加载失败不影响主流程 */
      }
    })();
  }

  // 打开记忆摘要弹窗：按会话加载（GET 空闲超时会触发后端自动补摘要，加载中状态兜底）
  async function openMemoryModal(convId: string) {
    setMemoryModal({ convId });
    setMemoryData(null);
    setMemoryError(null);
    setMemoryLoading(true);
    try {
      const m = await request<MemorySummary | null>(`/api/z-torque/conversations/${convId}/memory`);
      setMemoryData(m);
    } catch (err) {
      setMemoryError(errorText(err, "加载记忆摘要失败"));
    } finally {
      setMemoryLoading(false);
    }
  }

  // 生成/重新生成当前弹窗会话的记忆摘要
  async function summarizeMemory() {
    if (!memoryModal) return;
    setMemoryError(null);
    setMemoryLoading(true);
    try {
      const m = await request<MemorySummary>(`/api/z-torque/conversations/${memoryModal.convId}/memory/summarize`, { method: "POST" });
      setMemoryData(m);
    } catch (err) {
      setMemoryError(errorText(err, "生成摘要失败"));
    } finally {
      setMemoryLoading(false);
    }
  }

  // 归档：确认由统一 ConfirmDialog 承载；归档后可随时恢复（列表「显示已归档」开关）
  async function archiveConversation(id: string) {
    setError(null);
    try {
      await request(`/api/z-torque/conversations/${id}/archive`, { method: "POST" });
      setArchiveTarget(null);
      toast("success", "会话已归档，可在「显示已归档」中恢复");
      await loadConversations(showArchived);
      if (conversationId === id) {
        setConversationId(null);
        setMessages([]);
        setTask(null);
      }
    } catch (err) {
      setError(errorText(err, "归档失败"));
    }
  }

  // 恢复归档会话：误归档的唯一补救入口
  async function restoreConversation(id: string) {
    setError(null);
    setRestoringId(id);
    try {
      await request(`/api/z-torque/conversations/${id}/restore`, { method: "POST" });
      toast("success", "会话已恢复");
      await loadConversations(showArchived);
    } catch (err) {
      setError(errorText(err, "恢复失败"));
    } finally {
      setRestoringId(null);
    }
  }

  async function openTaskHistory() {
    setError(null);
    setTaskHistoryOpen(true);
    try {
      const r = await request<{ items: Task[] }>("/api/z-torque/tasks", { query: { limit: 20 } });
      setTaskHistory(r.items);
    } catch (err) {
      setError(errorText(err, "加载任务失败"));
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
      setSidebarOpen(false);
    } catch (err) {
      setError(errorText(err, "创建会话失败"));
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
      toast("success", "会话已重命名");
    } catch (err) {
      setError(errorText(err, "重命名失败"));
    }
  }

  function startRename(c: Conversation) {
    setRenamingId(c.id);
    setRenameValue(c.title ?? "");
  }

  // 切换智能体：若当前有正在推进的执行链路（任务运行中 / 流式输出中），先确认再切换，
  // 否则用户一次误选就会丢失正在进行的回答。
  function handleAgentChange(nextId: string) {
    const busy = (activeRun !== null && !TERMINAL.has(activeRun.status)) || streamingText !== null;
    if (busy) {
      setPendingAgentId(nextId);
      return;
    }
    applyAgentSwitch(nextId);
  }

  function applyAgentSwitch(nextId: string) {
    setAgentId(nextId);
    setConversationId(null);
    // 选中记忆：记住切到的智能体；会话记忆记为空串（新对话态），返回时保持空白
    writeSelectedId(AGENT_KEY, nextId);
    writeSelectedId(CONV_KEY, "");
    setMessages([]);
    setTask(null);
    setAttachments([]);
    setPendingAgentId(null);
    setRenderLimit(RENDER_STEP);
    setSidebarOpen(false);
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
    setUploadPercent(0);
    try {
      const meta = await uploadFile<FileMetadata>("/api/z-torque/files", file, {
        conversationId: conversationId ?? undefined,
        onProgress: (p) => setUploadPercent(p),
      });
      setAttachments((prev) => [...prev, meta]);
    } catch (err) {
      setError(errorText(err, "上传失败"));
    } finally {
      setUploading(false);
      setUploadPercent(null);
    }
  }

  function stopStream() {
    streamRef.current?.abort();
    streamRef.current = null;
  }

  // 停止当前输出：中断 SSE 流，任务链路同时请求后端取消任务
  async function stopActive() {
    clearTypeTimer();
    streamRef.current?.abort();
    streamRef.current = null;
    setStreamingText(null);
    setSending(false);
    const runningId = activeRun && !TERMINAL.has(activeRun.status) ? activeRun.taskId : task && !TERMINAL.has(task.status) ? task.id : null;
    // 执行块标记「已停止」并追加末步（对齐 chat.html 的 finishUi(stopped)）
    setActiveRun((prev) =>
      prev && !TERMINAL.has(prev.status)
        ? { ...prev, status: "CANCELLED", waiting: false, stopped: true, steps: [...prev.steps, localStep("LOCAL_STOPPED", "用户取消或连接中断")] }
        : prev,
    );
    if (runningId) {
      try {
        await request(`/api/z-torque/tasks/${runningId}/cancel`, { method: "POST" });
        // 取消请求已被后端受理（同步收敛）：立即收起执行块并延迟拉取消息——
        // 后端已写入"任务已取消"提示气泡；不依赖 SSE 事件时序，实时反馈有保障
        setActiveRun((prev) => (prev?.taskId === runningId ? null : prev));
        window.setTimeout(() => {
          void loadMessages(conversationIdRef.current ?? conversationId ?? "", { quiet: true });
        }, 600);
      } catch (err) {
        setError(errorText(err, "取消失败"));
      }
    }
  }

  function subscribeTask(taskId: string, options?: { resume?: boolean; convId?: string | null }) {
    stopStream();
    if (!options?.resume) {
      setSteps([]);
      setTask(null);
      lastEventIdRef.current = "";
    }
    const seen = new Set<string>();
    let doneSeen = false; // task.done 已正常收尾标记（onDone 兜底刷新据此跳过，防打字机期间重复拉取）
    // applyRun 仅当当前展示的正是该任务时生效（prev 已保证非空）
    const applyRun = (updater: (prev: ActiveRun) => ActiveRun) => {
      setActiveRun((prev) => (prev?.taskId === taskId ? updater(prev) : prev));
    };
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
          if (status === "WAITING_HUMAN") {
            setWaitingConfirm(true);
            applyRun((prev) => ({ ...prev, status, waiting: true }));
          }
          return;
        }
        const key = String(msg.id || "");
        if (seen.has(key)) return;
        seen.add(key);
        if (msg.id) lastEventIdRef.current = String(msg.id);
        const d = msg.data as Record<string, unknown>;
        const eventType = msg.event;
        const seq = Number(d.seq ?? seen.size);

        // task.done：终态收尾（结论打字机 → 清理执行块）
        if (eventType === "task.done") {
          doneSeen = true;
          const status = String(d.status ?? "");
          setTask((prev) => (prev ? { ...prev, status, result: (d.result as Record<string, unknown>) ?? prev.result } : prev));
          setWaitingConfirm(false);
          applyRun((prev) => ({ ...prev, status, waiting: false }));
          const content = (d.result as Record<string, unknown> | null)?.content;
          const finishUp = () => {
            setActiveRun((prev) => (prev?.taskId === taskId ? null : prev));
            setRunAnswer(null);
            void loadMessages(options?.convId ?? conversationId ?? "", { quiet: true });
          };
          if (status === "SUCCESS" && typeof content === "string" && content) {
            // 阶段 B 前的最后一步：过程收尾（结论气泡随后出现在执行块下方）
            applyRun((prev) => (prev.steps.some((s) => s.eventType === "LOCAL_SUMMARY") ? prev : { ...prev, steps: [...prev.steps, localStep("LOCAL_SUMMARY", "根据工具结果整理对用户的答复")] }));
            startTypewriter(content, finishUp);
          } else {
            finishUp();
          }
          return;
        }

        // 其余事件：步骤逐条追加（右栏与内联执行块共用数据源）
        const step: TaskStep = {
          seq,
          eventType,
          summary: String(d.summary ?? eventLabel(eventType)),
          time: new Date().toLocaleTimeString(),
          // 后端把 payload_summary 打平进 data（tool_name/arguments/result 等），兼容嵌套写法
          payload: (d.payload_summary as Record<string, unknown> | undefined) ?? payloadOf(d),
        };
        setSteps((prev) => (prev.some((s) => s.seq === seq) ? prev : [...prev, step]));
        applyRun((prev) => ({ ...prev, steps: prev.steps.some((s) => s.seq === seq) ? prev.steps : [...prev.steps, step] }));
        if (eventType === "TASK_WAITING_HUMAN") {
          setWaitingConfirm(true);
          applyRun((prev) => ({ ...prev, waiting: true }));
        }
        if (eventType === "TASK_APPROVAL_GRANTED" || eventType === "TASK_APPROVAL_REJECTED") {
          setWaitingConfirm(false);
          applyRun((prev) => ({ ...prev, waiting: false }));
        }
        // 后端未在事件中下发 to_state，但每帧都带当前 status，据此同步任务状态
        const toState = String(d.to_state ?? d.status ?? "");
        if (toState) {
          setTask((prev) => (prev ? { ...prev, status: toState } : prev));
          applyRun((prev) => ({ ...prev, status: toState }));
        }
        if (TERMINAL.has(toState)) {
          setWaitingConfirm(false);
          applyRun((prev) => ({ ...prev, waiting: false }));
        }
        // 取消/拒绝收尾：按事件类型触发（后端事件流不带 to_state，不能用终态状态判断）。
        // 与完整回复一致——执行块收起，刷新消息展示后端写入的提示气泡（带「查看执行过程」入口）
        if (eventType === "TASK_CANCELLED" || eventType === "TASK_APPROVAL_REJECTED") {
          setWaitingConfirm(false);
          applyRun((prev) => ({ ...prev, waiting: false, status: "CANCELLED" }));
          setActiveRun((prev) => (prev?.taskId === taskId ? null : prev));
          void loadMessages(options?.convId ?? conversationId ?? "", { quiet: true });
        }
      },
      onDone: () => {
        // 流结束后刷新消息与任务详情（兜底：正常收尾已在 task.done 中处理）
        void (async () => {
          // 用户已切到其他会话：旧流的收尾刷新不得覆盖当前视图
          const targetConv = options?.convId ?? conversationId ?? "";
          if (conversationIdRef.current !== targetConv) return;
          // task.done 已正常收尾（结论打字机可能进行中）：消息由 finishUp 统一刷新，
          // 此处提前拉取会让真实结论消息与打字机气泡同屏重复
          if (doneSeen) {
            await loadTaskDetail(taskId);
            return;
          }
          await loadMessages(targetConv, { quiet: true });
          await loadTaskDetail(taskId);
        })();
      },
    });
  }

  // 发送消息路由（阶段05 分流定稿）：
  // 1) 带附件 → POST /chat 异步任务链路（附件解析依赖任务执行）
  // 2) 其余一律 → POST /chat/stream 统一流式：纯文本=闲聊直答落库（不创建任务）；
  //    模型返回 tool_calls 时后端分流创建任务（message.task_created），前端切换任务事件流
  async function sendMessage(retryText?: string, retryParts?: { file_id: string }[]) {
    // retryText 传入时为失败重试：不读输入框、不动已暂存附件
    const isRetry = retryText !== undefined;
    const text = (retryText ?? input).trim();
    if (!text || sending) return;
    setError(null);
    setSending(true);
    let convId = conversationId;
    const contentParts = isRetry ? retryParts ?? [] : attachments.map((f) => ({ file_id: f.id }));
    // 乐观更新：用户消息立即上屏并清空输入框，无需等待后端返回
    const optimistic: Message = {
      id: `pending-${Date.now()}`,
      conversation_id: convId ?? "",
      sender_type: "USER",
      content: text,
      content_parts: contentParts.length ? contentParts : null,
      status: "COMPLETED",
      task_id: null,
      error_code: null,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, optimistic]);
    // 输入框立即清空；附件暂存先保留（发送失败时恢复文字的同时附件不丢，可直接重试）
    if (!isRetry) {
      setInput("");
    }
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
      if (!isRetry) setInput(text);
      setError(errorText(err, "发送失败"));
      setSending(false);
      return;
    }
    // 任务链路：携带附件（附件解析依赖任务执行）
    if (contentParts.length > 0) {
      clearTypeTimer();
      setRunAnswer(null);
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
        setConversationId(resp.conversation_id);
        // 发送已被后端接受，此刻才清空附件暂存
        if (!isRetry) setAttachments([]);
        void syncAutoTitle(resp.conversation_id); // 后端已按首条消息生成标题，静默刷新左侧列表
        if (resp.response_type === "TASK" && resp.task_id) {
          // 任务链路：内联执行块占位 + 订阅事件流（步骤逐条增加）
          startRun(resp.task_id);
          subscribeTask(resp.task_id, { convId });
        } else if (resp.response_type === "CHAT" && resp.assistant_content) {
          // 网关直答降级：随响应返回全文，打字机上屏后由真实消息替换
          startTypewriter(resp.assistant_content, () => {
            setRunAnswer(null);
            void loadMessages(resp.conversation_id, { quiet: true });
          });
        } else {
          await loadMessages(resp.conversation_id, { quiet: true });
        }
      } catch (err) {
        // 发送失败：移除乐观消息并恢复输入内容，便于用户重试
        setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
        if (!isRetry) setInput(text);
        setError(errorText(err, "发送失败"));
      } finally {
        setSending(false);
      }
      return;
    }
    // 统一流式链路：闲聊直答 + 工具分流（后端按模型返回分流，见阶段05 文档 2.6）
    clearTypeTimer();
    setRunAnswer(null);
    setTurnTime(new Date().toLocaleTimeString());
    setStreamingText("");
    const finish = () => {
      setSending(false);
      setStreamingText(null);
      // 切换会话后旧流的收尾刷新不得覆盖当前视图（selectConversation 已 abort，此处兜底）
      if (conversationIdRef.current === convId) void loadMessages(convId!);
    };
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
            // 决策轮闲聊直答：文本已完整生成（非流式），由前端打字机接管上屏（与任务链路结论一致）
            const d = msg.data as { content?: string };
            if (!d.content) return;
            resultSeen = true;
            setStreamingText(null);
            startTypewriter(d.content, () => {
              setRunAnswer(null);
              setSending(false);
              void loadMessages(convId!, { quiet: true });
            });
          } else if (msg.event === "message.task_created") {
            // 分流：模型要求调用工具 → 后端已创建任务，切换到任务事件流（内联执行块承接后续过程）
            const d = msg.data as { task_id?: string; conversation_id?: string };
            if (!d.task_id) return;
            setStreamingText(null);
            startRun(d.task_id);
            subscribeTask(d.task_id, { convId: d.conversation_id ?? convId });
            // 流由服务端关闭，onDone 里 finish 统一收尾
          } else if (msg.event === "message.error") {
            const d = msg.data as { code?: string; message?: string };
            // 错误码走统一中文映射（message 可能是后端英文原文，不直接外露）
            setError(errorText(new ApiRequestError(String(d.code ?? "AI_INTERNAL_ERROR"), d.message ?? "", 0), "流式响应失败"));
            finish();
          }
        },
        onError: (err) => {
          setError(err.message);
          finish();
        },
        onDone: () => {
          void syncAutoTitle(convId); // 流式首条消息结束后同步自动标题
          // resultSeen：打字机进行中，其回调负责 setSending(false) 与 loadMessages 收尾
          if (!resultSeen) finish();
        },
      },
      { "Idempotency-Key": crypto.randomUUID() },
    );
  }

  // 失败重试：取失败助手消息之前最近一条用户消息（含附件）原样重发
  function retryFailedMessage(failed: Message) {
    const idx = messages.findIndex((x) => x.id === failed.id);
    if (idx < 0 || sending) return;
    for (let i = idx - 1; i >= 0; i--) {
      const u = messages[i];
      if (u.sender_type !== "USER" || !u.content.trim()) continue;
      const parts = Array.isArray(u.content_parts)
        ? (u.content_parts as { file_id?: string }[]).filter((p) => p?.file_id).map((p) => ({ file_id: p.file_id as string }))
        : [];
      void sendMessage(u.content, parts);
      return;
    }
  }

  // 消息气泡中的附件 chip：占位元数据即可，viewFile 内部会拉取真实详情
  function openMessageFile(fileId: string) {
    void viewFile({ id: fileId, name: `附件 ${fileId.slice(0, 8)}`, media_type: "", size_bytes: 0, status: "UPLOADED", created_at: "" });
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
      setError(errorText(err, "加载文件详情失败"));
    } finally {
      setFileBusy(false);
    }
  }

  async function deleteFile(id: string) {
    setError(null);
    setDeletingFileId(null);
    try {
      await request(`/api/z-torque/files/${id}`, { method: "DELETE" });
      setAttachments((prev) => prev.filter((x) => x.id !== id));
      setFileDetail((prev) => (prev && prev.meta.id === id ? null : prev));
      toast("success", "文件已删除");
    } catch (err) {
      setError(errorText(err, "删除失败"));
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
      setError(errorText(err, "取消失败"));
    }
  }

  async function resolveConfirm(approved: boolean) {
    setError(null);
    // 目标任务：优先内联执行块，其次右栏当前任务
    const targetTaskId = activeRun?.taskId ?? task?.id;
    try {
      // 异步任务链路：按任务查询 PENDING 确认项（approval 发生在 worker 内，无同步挂起信息）
      let confId: string | null = null;
      if (targetTaskId) {
        const list = await request<{ id: string; status: string; task_id: string | null }[]>("/api/z-torque/confirmations");
        confId = list.find((c) => c.task_id === targetTaskId && c.status === "PENDING")?.id ?? null;
      }
      if (!confId) {
        setError("未找到待确认项");
        return;
      }
      await request(`/api/z-torque/confirmations/${confId}/resolve`, { method: "POST", body: { approved } });
      setWaitingConfirm(false);
      // 内联执行块同步解除挂起（事件流随后推送批准/重放事件）
      setActiveRun((prev) => (prev ? { ...prev, waiting: false } : prev));
    } catch (err) {
      setError(errorText(err, "操作失败"));
    }
  }

  async function loadConfirmations() {
    setError(null);
    try {
      const list = await request<Confirmation[]>("/api/z-torque/confirmations");
      setConfList(list);
      setConfListOpen(true);
    } catch (err) {
      setError(errorText(err, "加载确认记录失败"));
    }
  }

  async function openConfRecord(id: string) {
    setError(null);
    try {
      const c = await request<Confirmation>(`/api/z-torque/confirmations/${id}`);
      setConfRecord(c);
    } catch (err) {
      setError(errorText(err, "加载确认详情失败"));
    }
  }

  const usableAgents = useMemo(() => agents.filter((a) => a.current_published_version_id), [agents]);
  const currentAgent = useMemo(() => agents.find((a) => a.id === agentId), [agents, agentId]);
  const currentConversation = useMemo(() => conversations.find((c) => c.id === conversationId), [conversations, conversationId]);
  // 最后一条正常助手回复：仅在其上提供「重新生成」入口（对最新回答不满意时一键重问）
  const lastAssistantId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.sender_type !== "USER" && m.status !== "FAILED" && m.content) return m.id;
    }
    return null;
  }, [messages]);
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

  // 是否有正在推进的执行链路（内联执行块运行中且未被停止）
  const runLive = activeRun !== null && !TERMINAL.has(activeRun.status) && !activeRun.stopped;

  // SSE 终端事件兜底：执行中状态下每 10s 核对任务真实状态。
  // 后端重启 / 网络中断 / 快速取消竞态可能让终端事件（TASK_CANCELLED / FAILED）丢失，
  // 不兜底会导致界面"执行中"永久悬挂（库内任务实际已是终态）。
  useEffect(() => {
    const taskId = activeRun?.taskId ?? (taskRunning && task ? task.id : null);
    if (!taskId) return;
    const timer = window.setInterval(async () => {
      try {
        const t = await request<Task>(`/api/z-torque/tasks/${taskId}`);
        if (!TERMINAL.has(t.status)) return;
        setTask((prev) => (prev && prev.id === taskId ? { ...prev, status: t.status, error_code: t.error_code, error_message: t.error_message } : prev));
        setActiveRun((prev) => (prev && prev.taskId === taskId ? { ...prev, status: t.status, waiting: false } : prev));
        const convId = conversationIdRef.current;
        if (convId) void loadMessages(convId); // 拉取取消/失败通知气泡
      } catch {
        /* 轮询失败忽略，下一周期重试 */
      }
    }, 10_000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRun?.taskId, task?.id, taskRunning, runLive]);

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
      <div className="chat-shell" style={listPane.style}>
        {/* 窄屏遮罩：侧栏以抽屉形式覆盖（原实现直接隐藏，手机上无法切换会话/智能体） */}
        {sidebarOpen && <div className="chat-sidebar-mask" onClick={() => setSidebarOpen(false)} />}

        {/* 左侧栏：Agent 选择 + 会话列表 */}
        <aside className={`chat-sidebar${sidebarOpen ? " open" : ""}`}>
          <div className="chat-agent-picker">
            <label className="chat-agent-label">当前智能体</label>
            <div className="chat-agent-dropdown">
              <button
                type="button"
                className="chat-agent-trigger"
                aria-haspopup="listbox"
                aria-expanded={agentMenuOpen}
                disabled={agents.length === 0}
                title={currentAgent?.description ?? undefined}
                onClick={(e) => { e.stopPropagation(); setAgentMenuOpen((v) => !v); }}
              >
                <span className="chat-agent-trigger-name">
                  {currentAgent
                    ? `${currentAgent.name}${currentAgent.is_default_parent ? "（默认）" : ""}`
                    : agents.length === 0 ? "加载中…" : "选择智能体"}
                </span>
                <svg className={"chat-agent-chevron" + (agentMenuOpen ? " open" : "")} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9l6 6 6-6" /></svg>
              </button>
              {agentMenuOpen && (
                <div className="chat-agent-menu" role="listbox" aria-label="选择智能体" onClick={(e) => e.stopPropagation()}>
                  {agents.map((a) => {
                    const usable = Boolean(a.current_published_version_id);
                    const active = a.id === agentId;
                    return (
                      <button
                        key={a.id}
                        type="button"
                        role="option"
                        aria-selected={active}
                        disabled={!usable}
                        title={usable ? undefined : "该智能体还没有已发布版本，无法用于对话"}
                        className={"chat-agent-option" + (active ? " active" : "")}
                        onClick={() => {
                          setAgentMenuOpen(false);
                          if (!active) handleAgentChange(a.id);
                        }}
                      >
                        <span className="chat-agent-option-name">
                          {a.name}
                          {a.is_default_parent && <span className="chat-agent-badge">默认</span>}
                          {!usable && <span className="chat-agent-badge muted">未发布</span>}
                        </span>
                        {a.description && <span className="chat-agent-option-desc">{a.description}</span>}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
            {agentMetaTag && (
              <div className="chat-agent-meta">
                <span className={`chat-tag-mini ${agentMetaTag.cls}`}>{agentMetaTag.label}</span>
                <span>{currentAgent?.current_published_version_id ? "已发布" : "未发布"}</span>
              </div>
            )}
            {/* 死胡同兜底：没有任何可用智能体时，明确告知原因并给出去哪修 */}
            {!loadingConv && usableAgents.length === 0 && (
              <GuideCallout
                compact
                title={agents.length === 0 ? "还没有可用智能体" : `有 ${agents.length} 个智能体尚未发布`}
                why={
                  agents.length === 0
                    ? "会话需要基于一个智能体才能开始。请先创建智能体，完成配置后发布版本。"
                    : "只有已发布版本的智能体能用于对话。请到智能体管理发布一个版本。"
                }
                actionLabel={agents.length === 0 ? "去创建智能体" : "去发布版本"}
                onAction={() => navigate("/agents")}
                hint="也可以联系管理员为你发布一个智能体"
              />
            )}
          </div>

          <div className="chat-session-toolbar">
            <h3>会话</h3>
            <div style={{ display: "flex", gap: 6 }}>
              <button
                className="chat-btn-new"
                type="button"
                onClick={() => void newConversation()}
                disabled={!agentId}
                title={agentId ? "新建会话" : "需要先有一个已发布的智能体"}
              >
                + 新建
              </button>
              <button className="btn btn-secondary btn-sm" type="button" onClick={() => void openTaskHistory()} title="查看任务历史">任务</button>
            </div>
          </div>

          {/* 归档可恢复：默认隐藏已归档会话，勾选后可见并支持恢复 */}
          <label className="chat-archived-toggle">
            <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
            <span>显示已归档</span>
          </label>

          <input
            className="input"
            placeholder="搜索会话标题"
            value={convFilter}
            onChange={(e) => setConvFilter(e.target.value)}
            style={{ marginBottom: 8 }}
          />

          <div className="chat-session-list">
            {filteredConvs.length === 0 ? (
              <div className="chat-session-empty">
                {convFilter.trim()
                  ? "没有匹配的会话"
                  : showArchived
                    ? "暂无会话（含已归档）"
                    : agentId
                      ? "暂无会话，点击「+ 新建」开始"
                      : "请先发布一个智能体"}
              </div>
            ) : (
              filteredConvs.map((c) => {
                const isActive = c.id === conversationId;
                const showRunning = isActive && taskRunning;
                const dotCls = waitingConfirm ? "waiting" : "running";
                const archived = String(c.status || "").toUpperCase() === "ARCHIVED";
                return (
                  <div
                    key={c.id}
                    className={`chat-session-item${isActive ? " active" : ""}${archived ? " archived" : ""}`}
                    onClick={() => { if (renamingId !== c.id) { selectConversation(c); setSidebarOpen(false); } }}
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
                          <div className="chat-session-name">
                            {c.title ?? "未命名会话"}
                            {archived && <span className="chat-session-archived-tag">已归档</span>}
                          </div>
                          <div className="chat-session-time">{c.last_message_at ? new Date(c.last_message_at).toLocaleString() : "—"}</div>
                        </div>
                        <div className="chat-session-actions">
                          <button
                            className="chat-session-more"
                            onClick={(e) => { e.stopPropagation(); setMenuOpenId((prev) => (prev === c.id ? null : c.id)); }}
                            title="更多操作"
                          >
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.8" /><circle cx="12" cy="12" r="1.8" /><circle cx="19" cy="12" r="1.8" /></svg>
                          </button>
                          {menuOpenId === c.id && (
                            <div className="chat-session-menu" onClick={(e) => e.stopPropagation()}>
                              {archived ? (
                                <button
                                  type="button"
                                  disabled={restoringId === c.id}
                                  onClick={() => { setMenuOpenId(null); void restoreConversation(c.id); }}
                                >
                                  {restoringId === c.id ? "恢复中…" : "恢复会话"}
                                </button>
                              ) : (
                                <>
                                  <button type="button" onClick={() => { setMenuOpenId(null); void openMemoryModal(c.id); }}>记忆摘要</button>
                                  <button type="button" onClick={() => { setMenuOpenId(null); startRename(c); }}>重命名</button>
                                  <button type="button" className="danger" onClick={() => { setMenuOpenId(null); setArchiveTarget(c); }}>归档</button>
                                </>
                              )}
                            </div>
                          )}
                        </div>
                      </>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </aside>

        {/* 左右分栏拖拽把手 */}
        <PaneResizer onPreview={listPane.previewWidth} onCommit={listPane.commitWidth} />

        {/* 中间对话区 */}
        <section className="chat-main">
          <div className="chat-header">
            <div className="chat-header-title-area">
              {/* 窄屏入口：侧栏抽屉（>768px 由 CSS 隐藏此按钮） */}
              <button
                className="chat-sidebar-toggle"
                type="button"
                onClick={() => setSidebarOpen(true)}
                aria-label="打开会话与智能体列表"
                title="会话与智能体"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 6h18M3 12h18M3 18h18" /></svg>
              </button>
              <span className="chat-header-title">{currentConversation?.title ?? "新会话"}</span>
              {currentAgent && <span className="chat-agent-chip">{currentAgent.name}</span>}
              {/* 执行中实时指示（对齐 chat.html 的 live-pill） */}
              {runLive && <span className="chat-live-pill"><span className="dot" />执行中</span>}
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

          <div className="chat-messages" ref={messagesScrollRef}>
            {loadingConv ? (
              <Loading />
            ) : messages.length === 0 && streamingText === null && !activeRun && runAnswer === null ? (
              <div className="chat-welcome">
                <h2>开始对话</h2>
                <p>选择智能体后输入问题。需要执行技能时将自动创建任务，并在对话中实时展示执行过程。</p>
                {/* 无可用智能体：这是产品的第一道门，必须给出原因与出路 */}
                {!agentId ? (
                  <GuideCallout
                    title={agents.length === 0 ? "还没有可用智能体" : "所选智能体尚未发布"}
                    why={
                      agents.length === 0
                        ? "会话需要基于一个已发布版本的智能体。请先创建智能体并完成配置、发布版本，再回来对话。"
                        : "当前没有可对话的已发布版本。请在智能体管理中发布一个版本。"
                    }
                    actionLabel={agents.length === 0 ? "去创建智能体" : "去发布版本"}
                    onAction={() => navigate("/agents")}
                    hint="也可以联系管理员为你发布一个智能体"
                  />
                ) : (
                  <div className="chat-starter-list">
                    <div className="chat-starter-title">试试这样问</div>
                    {STARTER_PROMPTS.map((p) => (
                      <button
                        key={p}
                        type="button"
                        className="chat-starter-chip"
                        onClick={() => { setInput(p); inputRef.current?.focus(); }}
                      >
                        {p}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <>
                {/* 长会话渐进渲染：还有更早未渲染的消息时提供向前加载入口 */}
                {messages.length > visibleMessages.length && (
                  <button className="chat-load-earlier" type="button" onClick={loadEarlier}>
                    查看更早的消息（还有 {messages.length - visibleMessages.length} 条）
                  </button>
                )}
                {visibleMessages.map((m) => {
                  const isUser = m.sender_type === "USER";
                  const failed = !isUser && m.status === "FAILED";
                  const files = Array.isArray(m.content_parts)
                    ? (m.content_parts as { file_id?: string }[]).filter((p) => p?.file_id)
                    : [];
                  return (
                    <div key={m.id} className={`chat-msg ${isUser ? "user" : "assistant"}`}>
                      <div className="chat-msg-avatar">{isUser ? "我" : "Z"}</div>
                      <div className="chat-msg-main">
                        <div className="chat-msg-bubble">
                          {isUser ? (
                            <div className="chat-msg-content">{m.content}</div>
                          ) : (
                            <div className="chat-msg-content chat-md">
                              <Markdown remarkPlugins={[remarkGfm]}>{m.content}</Markdown>
                            </div>
                          )}
                          {files.length > 0 && (
                            <div className="chat-msg-files">
                              {files.map((p, i) => (
                                <button
                                  key={i}
                                  className="chat-msg-file"
                                  type="button"
                                  onClick={() => openMessageFile(p.file_id as string)}
                                  title="查看文件详情"
                                >
                                  附件 {String(p.file_id).slice(0, 8)}
                                </button>
                              ))}
                            </div>
                          )}
                          {!isUser && m.task_id && (
                            <button className="chat-task-link" type="button" onClick={() => viewTaskFromMessage(m.task_id as string)} title="查看该任务执行过程详情">
                              查看执行过程
                            </button>
                          )}
                          <div className="chat-msg-meta">
                            {new Date(m.created_at).toLocaleTimeString()}
                          </div>
                          {failed && (
                            <div className="chat-msg-failed">发送失败{m.error_code ? `（${m.error_code}）` : ""}</div>
                          )}
                        </div>
                        {/* 操作按钮独立在气泡下方：比融在气泡内更醒目（复制 / 重新生成 / 编辑重发 / 重试） */}
                        {(failed
                          || (!isUser && !failed && m.content)
                          || (isUser && !sending && streamingText === null && !activeRun)) && (
                          <div className="chat-msg-actions">
                            {failed && (
                              <button className="chat-msg-action" type="button" disabled={sending} onClick={() => retryFailedMessage(m)}>
                                重试
                              </button>
                            )}
                            {!failed && m.content && (
                              <button className="chat-msg-action" type="button" onClick={() => void copyText(m.content, m.id)}>
                                {copiedId === m.id ? "已复制" : "复制"}
                              </button>
                            )}
                            {/* 重新生成：仅最新一条正常助手回复提供，且当前没有进行中的流/执行块 */}
                            {!isUser && !failed && m.id === lastAssistantId && !sending
                              && streamingText === null && !activeRun && runAnswer === null && (
                              <button className="chat-msg-action" type="button" onClick={() => regenerateAnswer(m)} title="基于你上一条消息重新生成一份回答（原回答保留）">
                                重新生成
                              </button>
                            )}
                            {/* 编辑重发：用户消息可回填输入框修改后重发（后端无消息删除接口，原对话保留） */}
                            {isUser && !sending && streamingText === null && !activeRun && (
                              <button className="chat-msg-action" type="button" onClick={() => editUserMessage(m)} title="将该消息内容填入输入框，修改后重新发送（原对话保留）">
                                编辑重发
                              </button>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
                {/* 一轮助手回复（chat.md 定稿）：同一个头像下，过程在上实时追加、结论在下流式输出 */}
                {(activeRun || runAnswer !== null || streamingText !== null) && (
                  <div className={`chat-msg assistant${runAnswer !== null || streamingText !== null ? " streaming" : ""}`}>
                    <div className="chat-msg-avatar">Z</div>
                    <div className="chat-turn">
                      {activeRun && (
                        <RunBlock
                          activeRun={activeRun}
                          taskError={task?.id === activeRun.taskId && task.error_message ? { message: taskErrorText(task.error_message, task.error_code), code: task.error_code } : null}
                          onResolve={(approved) => void resolveConfirm(approved)}
                        />
                      )}
                      {/* 最终结论：执行过程结束后才出现并逐字上屏 */}
                      {(runAnswer !== null || streamingText !== null) && (
                        <div className="chat-msg-bubble">
                          {/* 跳过动画入口：仅在打字机定时器存活时显示（普通流式本就实时直出，无需跳过） */}
                          {runAnswer !== null && streamingText === null && typeTimerRef.current !== null && (
                            <button
                              className="chat-typewriter-skip"
                              type="button"
                              onClick={skipTypewriter}
                              title="跳过打字动画，立即显示完整回复"
                            >跳过动画</button>
                          )}
                          {/* 光标仅在真正生成中显示（打字机定时器存活 / 普通流式进行中） */}
                          <div className={`chat-msg-content chat-md${streamingText !== null || typeTimerRef.current !== null ? " chat-streaming-cursor" : ""}`}>
                            <Markdown remarkPlugins={[remarkGfm]}>{runAnswer ?? streamingText ?? "…"}</Markdown>
                          </div>
                        </div>
                      )}
                      {turnTime && <div className="chat-msg-meta">{turnTime}</div>}
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
                        onClick={(e) => { e.stopPropagation(); setAttachments((prev) => prev.filter((x) => x.id !== f.id)); }}
                        title="从本次发送中移除（文件不会被删除）"
                      >×</button>
                    </div>
                  ))}
                </div>
              )}
              {/* 附件上传真实进度：XHR progress 事件驱动，上传完成后服务端解析期间显示「处理中」 */}
              {uploading && uploadPercent !== null && (
                <div className="chat-upload-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={uploadPercent}>
                  <div className="chat-upload-progress-track">
                    <div className="chat-upload-progress-bar" style={{ width: `${uploadPercent}%` }} />
                  </div>
                  <span className="chat-upload-progress-text">{uploadPercent < 100 ? `上传中 ${uploadPercent}%` : "处理中…"}</span>
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
                  ref={inputRef}
                  placeholder="输入消息，Enter 发送，Shift+Enter 换行"
                  value={input}
                  rows={1}
                  onChange={(e) => {
                    setInput(e.target.value);
                    e.target.style.height = "auto";
                    e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px";
                  }}
                  onKeyDown={(e) => {
                    // IME 组合输入中的回车用于确认候选词，不应触发发送
                    if (e.nativeEvent.isComposing) return;
                    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void sendMessage(); }
                  }}
                />
                <div className="chat-composer-actions">
                  {(streamingText !== null || taskRunning || (activeRun !== null && !TERMINAL.has(activeRun.status))) && (
                    <button className="chat-btn-stop" type="button" onClick={() => void stopActive()} title="停止当前流式输出或任务">
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
              <span style={{ marginLeft: "auto" }}>支持 PDF/TXT/MD/CSV/XLSX,单文件 ≤ 50MB</span>
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
                  {taskErrorText(task.error_message, task.error_code)}{task.error_code ? `（${task.error_code}）` : ""}
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
                <button className="btn btn-danger btn-sm" onClick={() => setDeletingFileId(fileDetail.meta.id)}>删除文件</button>
                <button className="btn btn-secondary btn-sm" onClick={() => setFileDetail(null)}>关闭</button>
              </div>
            </div>
          )}
        </Modal>
      )}

      {confListOpen && (
        <Modal open title="确认记录" onClose={() => setConfListOpen(false)} width={720}>
          {confList.length === 0 ? (
            <EmptyState title="暂无确认记录" hint="当工具执行需要人工确认时会在此记录" />
          ) : (
            <table className="table">
              <thead>
                <tr><th>状态</th><th>原因</th><th>发起人</th><th>关联任务</th><th>操作</th></tr>
              </thead>
              <tbody>
                {confList.map((c) => (
                  <tr key={c.id}>
                    <td><StatusTag status={c.status} /></td>
                    <td className="text-sm">{c.reason ?? "—"}</td>
                    {/* 管理员可见同租户他人确认单：优先展示用户名，缺失时回退短 ID */}
                    <td className="text-sm">{c.requested_by_username || `${c.requested_by_user_id.slice(0, 8)}…`}</td>
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
            <div className="text-sm">
              <span className="text-muted">发起人：</span>
              {confRecord.requested_by_username
                ? `${confRecord.requested_by_username}（${confRecord.requested_by_user_id}）`
                : confRecord.requested_by_user_id || "—"}
            </div>
            <div className="text-sm"><span className="text-muted">确认人：</span>{confRecord.confirmed_by_user_id ?? "—"}</div>
            {confRecord.skill_version_id && <div className="text-sm"><span className="text-muted">技能版本：</span>{confRecord.skill_version_id}</div>}
            {confRecord.tool_version_id && <div className="text-sm"><span className="text-muted">工具版本：</span>{confRecord.tool_version_id}</div>}
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button className="btn btn-secondary btn-sm" onClick={() => setConfRecord(null)}>关闭</button>
            </div>
          </div>
        </Modal>
      )}

      {memoryModal && (
        <Modal open title="记忆摘要" onClose={() => setMemoryModal(null)} width={560}>
          {memoryLoading ? (
            <Loading />
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div className="text-sm text-muted">
                {conversations.find((c) => c.id === memoryModal.convId)?.title ?? "会话"}
              </div>
              {memoryError && <Alert kind="error">{memoryError}</Alert>}
              {memoryData ? (
                <>
                  <div className="text-sm" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", lineHeight: 1.7, background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 8, padding: 12 }}>
                    {memoryData.summary}
                  </div>
                  <div className="text-muted text-sm">
                    覆盖第 {memoryData.source_start}–{memoryData.source_end} 条消息 · 更新于 {new Date(memoryData.updated_at).toLocaleString()}
                  </div>
                </>
              ) : !memoryError ? (
                <div className="text-muted text-sm">暂无摘要。发送消息后可点击下方按钮，为本会话生成记忆摘要。</div>
              ) : null}
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
                <button className="btn btn-primary btn-sm" disabled={memoryLoading} onClick={() => void summarizeMemory()}>
                  {memoryData ? "重新生成" : "生成摘要"}
                </button>
                <button className="btn btn-secondary btn-sm" onClick={() => setMemoryModal(null)}>关闭</button>
              </div>
            </div>
          )}
        </Modal>
      )}

      {/* 归档确认：明确告知"可恢复"，避免用户把归档当成删除而不敢点 */}
      <ConfirmDialog
        open={archiveTarget !== null}
        title="归档该会话？"
        confirmText="归档"
        onCancel={() => setArchiveTarget(null)}
        onConfirm={() => { if (archiveTarget) void archiveConversation(archiveTarget.id); }}
      >
        <p>
          「{archiveTarget?.title ?? "未命名会话"}」将从默认列表中隐藏。
          归档不是删除，消息与执行记录都会保留，你可以随时勾选「显示已归档」把它恢复回来。
        </p>
      </ConfirmDialog>

      {/* 删除文件：不可恢复，走高危确认 */}
      <ConfirmDialog
        open={deletingFileId !== null}
        title="删除该文件？"
        confirmText="删除"
        destructive
        onCancel={() => setDeletingFileId(null)}
        onConfirm={() => { if (deletingFileId) void deleteFile(deletingFileId); }}
      >
        <p>文件及其解析内容将被永久删除，且不可恢复。已发送给模型的消息内容不受影响。</p>
      </ConfirmDialog>

      {/* 切换智能体前确认：原本会静默清空正在进行的执行链路 */}
      <ConfirmDialog
        open={pendingAgentId !== null}
        title="当前对话仍在执行中"
        confirmText="放弃并切换"
        destructive
        onCancel={() => setPendingAgentId(null)}
        onConfirm={() => { if (pendingAgentId) applyAgentSwitch(pendingAgentId); }}
      >
        <p>
          切换智能体会清空当前会话视图，正在进行的任务与流式回答将不再展示（后端任务本身不会被取消）。
          建议等本轮执行结束后再切换。
        </p>
      </ConfirmDialog>
    </div>
  );
}
