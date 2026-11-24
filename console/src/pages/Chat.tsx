// 会话页（阶段13 第 4 批）：视觉层迁到 antd 6 + @ant-design/x。
//
// 组件映射（DSH 同源观感，不再手写气泡/输入框/会话列表）：
//   · 消息气泡      → `Bubble`（placement/variant/header/footer 槽位）
//   · 输入区        → `Sender`（Enter 发送、loading 时内置停止按钮、prefix 附件、footer 提示）
//   · 会话列表      → `Conversations`（creation 新建 + menu 更多操作）
//   · 首启示例提问  → `Prompts`
//   · 待发送附件    → `Attachments`（file-card 展示 + 移除）
//   · 执行链路折叠  → `Think`（见 pages/chatRun.tsx，与 /embed/chat 共用）
// 业务逻辑（流式、任务事件、归档、记忆摘要、任务面板）保持原样，只替换呈现层。
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ComponentRef } from "react";
import { useNavigate } from "react-router-dom";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Attachments, Bubble, Conversations, Prompts, Sender } from "@ant-design/x";
import { Avatar, Button, Checkbox, Input, Progress, Select, Tooltip } from "antd";
import type { MenuProps } from "antd";
import { HistoryOutlined, PaperClipOutlined, SearchOutlined } from "@ant-design/icons";
import { ApiRequestError, downloadFile, fetchBlob, request, uploadFile } from "../services/api";
import { errorText } from "../services/errorText";
import { readSelectedId, writeSelectedId } from "../services/selectionMemory";
import { openGetStream, openPostStream } from "../services/sse";
import type { Agent, ChatResponse, Confirmation, Conversation, FileContent, FileMetadata, FileStatus, Message, MessageContentPart, Task, TaskEvent } from "../types";
import { StatusTag, EmptyState, Alert } from "../components/ui";
import { Loading } from "../components/Loading";
import { Modal } from "../components/Modal";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PaneResizer, usePaneWidth } from "../components/PaneResizer";
import { GuideCallout } from "../components/GuideCallout";
import { useToast } from "../components/Toast";
import { AppTopbar, APP_NAV, isAdminUser } from "../components/AppTopbar";
import { useAuth } from "../stores/auth";
// 内联执行块共享模块：与嵌入对话页（/embed/chat）共用类型/工具函数/渲染组件，保证实时链路展示一致
import { HistoricalRun, RunBlock, eventLabel, localStep, payloadOf, TERMINAL, type ActiveRun, type TaskStep } from "./chatRun";

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

const MEDIA_PLACEHOLDER_RE = /^\[(图片|语音|视频|文件)\]$/;
const MEDIA_KINDS = new Set(["image", "audio", "video"]);

type SenderHandle = ComponentRef<typeof Sender>;

// 外部来源消息（Webhook/企微）的徽标文案
const EXTERNAL_SOURCE_LABELS: Record<string, string> = {
  webhook: "Webhook",
  wecom: "企微",
  api: "API",
};
function externalSourceLabel(m: Message): string {
  const label = EXTERNAL_SOURCE_LABELS[String(m.source ?? "")] ?? "外部";
  // external_sender 存储格式为「Webhook接入名称:会话标识」，徽标只展示接入名称（冒号前的部分）；
  // 企微来源的 sender 为 userId（无冒号），原样展示
  const sender = m.external_sender ? m.external_sender.split(":")[0] : "";
  return sender ? `${label} · ${sender}` : label;
}

// 外部来源消息头像：按渠道展示对应图标（Webhook=链接、企微=对话气泡、API=代码），替代固定"外"字
function ExternalSourceAvatar({ source }: { source: string }) {
  const stroke = { fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  if (source === "wecom") {
    return (
      <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
        <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" {...stroke} />
      </svg>
    );
  }
  if (source === "api") {
    return (
      <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
        <polyline points="16 18 22 12 16 6" {...stroke} />
        <polyline points="8 6 2 12 8 18" {...stroke} />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" {...stroke} />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" {...stroke} />
    </svg>
  );
}

function partKind(part: MessageContentPart): "image" | "file" | "audio" | "video" {
  const kind = String(part.type ?? "");
  return MEDIA_KINDS.has(kind) ? (kind as "image" | "audio" | "video") : "file";
}

// 媒体气泡：带鉴权取 blob 再转 object URL，卸载时 revoke，避免 <img> 401 与内存泄漏
function MessageMedia({ part }: { part: MessageContentPart }) {
  const kind = partKind(part);
  const fileId = String(part.file_id ?? "");
  const name = String(part.file_name ?? `附件 ${fileId.slice(0, 8)}`);
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    if (!fileId || !MEDIA_KINDS.has(kind)) return;
    let active = true;
    let objectUrl: string | null = null;
    void (async () => {
      try {
        const blob = await fetchBlob(`/api/z-torque/files/${fileId}/raw`);
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      } catch {
        if (active) setError(true);
      }
    })();
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [fileId, kind]);
  if (!url) {
    return <div className="chat-msg-media-state">{error ? "媒体加载失败" : "媒体加载中…"}</div>;
  }
  if (kind === "image") {
    return <img className="chat-msg-media-image" src={url} alt={name} onClick={() => window.open(url, "_blank", "noopener")} />;
  }
  if (kind === "audio") {
    return <audio className="chat-msg-media-audio" src={url} controls />;
  }
  return <video className="chat-msg-media-video" src={url} controls />;
}

/** 等待首个字/首步的计时：让用户看见「确实在跑」而不是一块空白（借鉴 DSH 的执行行） */
function useWaitElapsed(startedAt: number | null, active: boolean): string {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active || startedAt === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, startedAt]);
  if (!active || startedAt === null) return "";
  return `${Math.max(0, Math.round((now - startedAt) / 1000))}s`;
}

/** 一条消息气泡：Bubble 承担头像/气泡/头脚槽位，Markdown 与附件仍在内容槽内 */
function MessageBubble({
  m,
  lastAssistantId,
  busy,
  copiedId,
  onCopy,
  onRegenerate,
  onEdit,
  onRetry,
  onDownload,
}: {
  m: Message;
  lastAssistantId: string | null;
  busy: boolean;
  copiedId: string | null;
  onCopy: (text: string, key: string) => void;
  onRegenerate: (m: Message) => void;
  onEdit: (m: Message) => void;
  onRetry: (m: Message) => void;
  onDownload: (fileId: string, name: string) => void;
}) {
  const isUser = m.sender_type === "USER";
  // 外部来源（Webhook/企微）消息：用户侧内容 + 独立虚线气泡与来源徽标
  const isExternal = m.sender_type === "EXTERNAL";
  const externalSource = isExternal ? String(m.source ?? "") : "";
  const failed = !isUser && m.status === "FAILED";
  const parts = Array.isArray(m.content_parts) ? m.content_parts.filter((p) => p?.file_id) : [];
  const hasAttachment = parts.length > 0;
  // 占位文本只在没有附件时兜底显示
  const hidePlaceholder = hasAttachment && MEDIA_PLACEHOLDER_RE.test(m.content.trim());
  const showActions =
    failed ||
    ((!isUser || isExternal) && !failed && Boolean(m.content)) ||
    (isUser && !busy);

  const body = (
    <>
      {isExternal && <div className="chat-msg-source-badge">{externalSourceLabel(m)}</div>}
      {isUser || isExternal ? (
        <div className="chat-plain">{hidePlaceholder ? "" : m.content}</div>
      ) : (
        <div className="chat-md">
          <Markdown remarkPlugins={[remarkGfm]}>{m.content}</Markdown>
        </div>
      )}
      {hasAttachment && (
        <div className="chat-msg-files">
          {parts.map((p, i) => {
            if (partKind(p) !== "file") return <MessageMedia key={i} part={p} />;
            const name = String(p.file_name ?? `附件 ${String(p.file_id).slice(0, 8)}`);
            return (
              <button key={i} className="chat-msg-file" type="button" onClick={() => onDownload(String(p.file_id), name)} title="下载附件">
                {name}
              </button>
            );
          })}
        </div>
      )}
      {failed && <div className="chat-msg-failed">发送失败{m.error_code ? `（${m.error_code}）` : ""}</div>}
    </>
  );

  const footer = (
    <div className="chat-msg-foot">
      {showActions && (
        <div className="chat-msg-actions">
          {failed && (
            <button className="chat-msg-action" type="button" disabled={busy} onClick={() => onRetry(m)}>
              重试
            </button>
          )}
          {!failed && m.content && (
            <button className="chat-msg-action" type="button" onClick={() => onCopy(m.content, m.id)}>
              {copiedId === m.id ? "已复制" : "复制"}
            </button>
          )}
          {/* 重新生成：仅最新一条正常助手回复提供（外部来源消息不参与） */}
          {!isUser && !isExternal && !failed && m.id === lastAssistantId && !busy && (
            <button className="chat-msg-action" type="button" onClick={() => onRegenerate(m)} title="基于你上一条消息重新生成一份回答（原回答保留）">
              重新生成
            </button>
          )}
          {/* 编辑重发：用户消息可回填输入框修改后重发（后端无消息删除接口，原对话保留） */}
          {isUser && !busy && (
            <button className="chat-msg-action" type="button" onClick={() => onEdit(m)} title="将该消息内容填入输入框，修改后重新发送（原对话保留）">
              编辑重发
            </button>
          )}
        </div>
      )}
      <span className="chat-msg-meta">{new Date(m.created_at).toLocaleTimeString()}</span>
    </div>
  );

  return (
    <Bubble
      placement={isUser ? "end" : "start"}
      variant={isUser || isExternal ? "filled" : "borderless"}
      shape="corner"
      className={`chat-bubble${isUser ? " chat-bubble-user" : ""}${isExternal ? ` chat-bubble-external src-${externalSource}` : ""}`}
      classNames={{ body: "chat-bubble-body", content: "chat-bubble-content", header: "chat-bubble-header", footer: "chat-bubble-footer", avatar: "chat-bubble-avatar" }}
      avatar={
        isUser ? (
          <Avatar size={30} className="chat-avatar-user">我</Avatar>
        ) : isExternal ? (
          <span title={externalSourceLabel(m)}>
            <Avatar size={30} className="chat-avatar-external">
              <ExternalSourceAvatar source={externalSource} />
            </Avatar>
          </span>
        ) : (
          <Avatar size={30} className="chat-avatar-ai">Z</Avatar>
        )
      }
      // 历史任务的执行链路内联在结论之上（与发生顺序一致，替代原来的右侧滑窗）
      header={!isUser && !isExternal && m.task_id ? <HistoricalRun taskId={String(m.task_id)} /> : undefined}
      content={body}
      footer={footer}
      footerPlacement={isUser ? "outer-end" : "outer-start"}
    />
  );
}

export function ChatPage() {
  const { user } = useAuth();
  const admin = isAdminUser(user);
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
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingConv, setLoadingConv] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 模型未配置是「可修复的引导状态」而非普通错误：单独标记以渲染开通入口
  const [modelMissing, setModelMissing] = useState(false);

  const [attachments, setAttachments] = useState<FileMetadata[]>([]);
  const [uploading, setUploading] = useState(false);
  // 附件上传真实进度（XHR progress 事件，0-100；null=不在上传中）
  const [uploadPercent, setUploadPercent] = useState<number | null>(null);
  // 重命名：由 Conversations 的 ⋯ 菜单打开（弹窗替代原来的行内输入框）
  const [renameTarget, setRenameTarget] = useState<Conversation | null>(null);
  const [renameValue, setRenameValue] = useState("");
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

  function isModelMissingError(err: unknown): boolean {
    return err instanceof ApiRequestError && err.code === "AI_MODEL_NOT_CONFIGURED";
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
  // 本轮开始的时间戳（毫秒）：用于「正在理解… 3s」的实时计时
  const [turnStartedAt, setTurnStartedAt] = useState<number | null>(null);
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
  const senderRef = useRef<SenderHandle | null>(null);

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

  // 已读回执：本地先归零让角标即刻消失，再写服务端已读位（失败不阻塞阅读）
  const markConversationRead = useCallback(async (id: string) => {
    setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, unread_count: 0 } : c)));
    try {
      await request(`/api/z-torque/conversations/${id}/read`, { method: "POST" });
    } catch {
      /* 已读回执失败忽略；下次打开会话或收到消息会重试 */
    }
  }, []);

  // 连续到达的消息（外部回调常一次推两条）合并为一次已读请求
  const readTimersRef = useRef<Record<string, number>>({});
  const scheduleRead = useCallback(
    (id: string) => {
      const timer = readTimersRef.current[id];
      if (timer) window.clearTimeout(timer);
      readTimersRef.current[id] = window.setTimeout(() => {
        delete readTimersRef.current[id];
        void markConversationRead(id);
      }, 400);
    },
    [markConversationRead],
  );

  useEffect(
    () => () => {
      Object.values(readTimersRef.current).forEach((timer) => window.clearTimeout(timer));
    },
    [],
  );

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

  // 当前打开的会话视为已读：切换会话或消息列表增长（新消息到达/加载完成）都写一次已读位
  useEffect(() => {
    if (!conversationId) return;
    scheduleRead(conversationId);
  }, [conversationId, messages.length, scheduleRead]);

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
  //
  // 节奏按长度自适应（阶段13 性能优化）：整段控制在约 1.1s 内播完。
  // 原来固定「2 字/16ms」= 125 字/秒，一条 1500 字的回答要播 12 秒——
  // 任务本身已经跑完，用户却还在等动画，这是"最后内容出得太慢"的主要观感来源。
  const TYPEWRITER_TARGET_MS = 1100;
  const TYPEWRITER_TICK_MS = 16;
  // 超长回答逐帧重渲染 Markdown 的代价高于动画收益：直接整段上屏
  const TYPEWRITER_MAX_CHARS = 4000;

  function startTypewriter(text: string, onDone: () => void) {
    clearTypeTimer();
    if (!text) {
      onDone();
      return;
    }
    typeTargetRef.current = { text, onDone };
    // 结论气泡出现即刷新本轮时间戳（与 chat.html 的 finishUi 一致）
    setTurnTime(new Date().toLocaleTimeString());
    if (text.length > TYPEWRITER_MAX_CHARS) {
      typeTargetRef.current = null;
      setRunAnswer(text);
      onDone();
      return;
    }
    const step = Math.max(2, Math.ceil(text.length / (TYPEWRITER_TARGET_MS / TYPEWRITER_TICK_MS)));
    setRunAnswer("");
    let i = 0;
    const tick = () => {
      i += step;
      setRunAnswer(text.slice(0, i));
      if (i < text.length) {
        typeTimerRef.current = window.setTimeout(tick, TYPEWRITER_TICK_MS);
      } else {
        typeTimerRef.current = null;
        typeTargetRef.current = null;
        onDone();
      }
    };
    typeTimerRef.current = window.setTimeout(tick, TYPEWRITER_TICK_MS);
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
    setTurnStartedAt((prev) => prev ?? Date.now());
    setActiveRun({
      taskId,
      steps: [localStep("LOCAL_UNDERSTAND", "识别到需要执行，准备创建任务")],
      status,
      waiting: false,
      stopped: false,
      startedAt: Date.now(),
    });
  }

  // 编辑重发：把该用户消息内容回填输入框（后端无消息删除/截断接口，原对话保留，改完作为新消息发送）
  function editUserMessage(m: Message) {
    if (sending) return;
    setInput(m.content);
    senderRef.current?.focus();
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
          startedAt: Date.now(),
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

  // 重命名：⋯ 菜单打开弹窗，输入后提交（原先的行内输入框与气泡点击互相抢事件）
  function startRename(c: Conversation) {
    setRenameTarget(c);
    setRenameValue(c.title ?? "");
  }

  async function renameConversation() {
    const target = renameTarget;
    const title = renameValue.trim();
    if (!target || !title) return;
    setError(null);
    try {
      await request(`/api/z-torque/conversations/${target.id}`, { method: "PATCH", body: { title } });
      setConversations((prev) => prev.map((c) => (c.id === target.id ? { ...c, title } : c)));
      setRenameTarget(null);
      toast("success", "会话已重命名");
    } catch (err) {
      setError(errorText(err, "重命名失败"));
    }
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
  // opts.text：本次要发送的文本（Sender 提交时直接给值，避免读到未刷新的受控 state）
  // opts.parts：附件（失败重试沿用原消息附件）
  // opts.fromRetry：重试/重新生成，不清空输入框
  async function sendMessage(opts?: { text?: string; parts?: { file_id: string }[]; fromRetry?: boolean }) {
    const fromRetry = opts?.fromRetry === true;
    const text = (opts?.text ?? input).trim();
    if (!text || sending) return;
    setError(null);
    setModelMissing(false);
    setSending(true);
    let convId = conversationId;
    const contentParts = opts?.parts ?? attachments.map((f) => ({ file_id: f.id }));
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
    if (!fromRetry) {
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
      if (!fromRetry) setInput(text);
      setError(errorText(err, "发送失败"));
      setModelMissing(isModelMissingError(err));
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
        if (!fromRetry) setAttachments([]);
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
        if (!fromRetry) setInput(text);
        setError(errorText(err, "发送失败"));
        setModelMissing(isModelMissingError(err));
      } finally {
        setSending(false);
      }
      return;
    }
    // 统一流式链路：闲聊直答 + 工具分流（后端按模型返回分流，见阶段05 文档 2.6）
    clearTypeTimer();
    setRunAnswer(null);
    setTurnTime(new Date().toLocaleTimeString());
    setTurnStartedAt(Date.now());
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
            setModelMissing(d.code === "AI_MODEL_NOT_CONFIGURED");
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
      void sendMessage({ text: u.content, parts, fromRetry: true });
      return;
    }
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

  // 说明：原「从消息入口打开右侧面板查看执行链路」已改为消息内联展开
  // （见 chatRun.tsx 的 HistoricalRun），此处不再保留入口函数。

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
      if (m.sender_type === "ASSISTANT" && m.status !== "FAILED" && m.content) return m.id;
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
  // 是否忙碌：用于隐藏「重新生成 / 编辑重发」等会打断本轮的动作
  const busy = sending || streamingText !== null || runLive;
  // 本轮正文（打字机优先，其次流式增量）：为空表示还在等首字节，此时显示「正在理解…」而不是空白
  const turnText = runAnswer ?? streamingText ?? "";
  const turnWaiting = turnText.length === 0;
  const waitElapsed = useWaitElapsed(turnStartedAt, turnWaiting);

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

  // 后台入站消息（通用 Webhook / 企微）由后端 SSE 主动推送：收到后立刻刷新会话列表/当前会话，不再轮询。
  useEffect(() => {
    let disposed = false;
    let retryTimer: number | null = null;
    let handle: { abort: () => void } | null = null;

    function scheduleReconnect() {
      if (disposed || retryTimer !== null) return;
      retryTimer = window.setTimeout(() => {
        retryTimer = null;
        connect();
      }, 3000);
    }

    function connect() {
      if (disposed) return;
      handle = openGetStream("/api/z-torque/chat/events", {
        onEvent: (msg) => {
          if (msg.event !== "chat.message") return;
          const payload = msg.data as
            | {
                conversation_id?: string;
                message_id?: string;
                sender_type?: string;
                source?: string | null;
                external_sender?: string | null;
                content?: string;
                content_parts?: MessageContentPart[] | null;
                created_at?: string;
                task_id?: string | null;
                pending_task_id?: string | null;
              }
            | null;
          if (!payload?.conversation_id || !payload.message_id) return;
          // 当前会话直接插入气泡，不再额外请求消息列表
          if (conversationIdRef.current === payload.conversation_id) {
            setMessages((prev) =>
              prev.some((m) => m.id === payload.message_id)
                ? prev
                : [
                    ...prev,
                    {
                      id: payload.message_id as string,
                      conversation_id: payload.conversation_id as string,
                      sender_type: payload.sender_type ?? "USER",
                      source: payload.source ?? null,
                      external_sender: payload.external_sender ?? null,
                      content: payload.content ?? "",
                      content_parts: payload.content_parts ?? null,
                      status: "COMPLETED",
                      task_id: payload.task_id ?? null,
                      error_code: null,
                      created_at: payload.created_at ?? new Date().toISOString(),
                    },
                  ],
            );
            // 后台 Webhook/企微触发的任务：像手动发送一样展示内联执行块并订阅任务事件流
            if ((payload.sender_type === "USER" || payload.sender_type === "EXTERNAL") && payload.pending_task_id) {
              startRun(payload.pending_task_id);
              subscribeTask(payload.pending_task_id, { convId: payload.conversation_id });
            }
          } else {
            // 非当前会话：本地先 +1 让角标即刻可见（随后由列表接口校准未读数与排序）
            setConversations((prev) =>
              prev.map((c) =>
                c.id === payload.conversation_id
                  ? {
                      ...c,
                      unread_count: (c.unread_count ?? 0) + 1,
                      last_message_at: payload.created_at ?? c.last_message_at,
                    }
                  : c,
              ),
            );
          }
          // 会话列表只需要刷新标题/最新会话（不轮询）
          void loadConversations(showArchived);
        },
        onError: scheduleReconnect,
        onDone: scheduleReconnect,
      });
    }

    connect();
    return () => {
      disposed = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      handle?.abort();
    };
  }, [showArchived, loadConversations, loadMessages]);

  // Agent meta 标签：默认父 Agent / 官方 / 我的
  const agentMetaTag = currentAgent
    ? currentAgent.is_default_parent
      ? { cls: "default", label: "默认" }
      : currentAgent.source_type === "OFFICIAL"
        ? { cls: "official", label: "官方" }
        : { cls: "mine", label: "我的" }
    : null;

  // ---- Conversations 数据源：标题 + 时间 + 未读角标 + 运行态点 ----
  const convItems = useMemo(
    () =>
      filteredConvs.map((c) => {
        const archived = String(c.status || "").toUpperCase() === "ARCHIVED";
        const active = c.id === conversationId;
        const showRunning = active && taskRunning;
        const dotCls = waitingConfirm ? "waiting" : "running";
        return {
          key: c.id,
          label: (
            <span className="chat-conv">
              <span className="chat-conv-row">
                {showRunning && <span className={`chat-conv-dot ${dotCls}`} />}
                <span className="chat-conv-title">{c.title ?? "未命名会话"}</span>
                {archived && <span className="chat-conv-tag">已归档</span>}
                {!active && !archived && (c.unread_count ?? 0) > 0 && (
                  <span className="chat-conv-unread" title={`${c.unread_count} 条未读消息`}>
                    {c.unread_count > 99 ? "99+" : c.unread_count}
                  </span>
                )}
              </span>
              <span className="chat-conv-time">{c.last_message_at ? new Date(c.last_message_at).toLocaleString() : "—"}</span>
            </span>
          ),
        };
      }),
    [filteredConvs, conversationId, taskRunning, waitingConfirm],
  );

  // ---- Conversations ⋯ 菜单：记忆摘要 / 重命名 / 归档（已归档则只给恢复） ----
  function convMenu(c: Conversation): MenuProps {
    const archived = String(c.status || "").toUpperCase() === "ARCHIVED";
    if (archived) {
      return {
        items: [{ key: "restore", label: restoringId === c.id ? "恢复中…" : "恢复会话", disabled: restoringId === c.id }],
        onClick: ({ key, domEvent }) => {
          domEvent.stopPropagation();
          if (key === "restore") void restoreConversation(c.id);
        },
      };
    }
    return {
      items: [
        { key: "memory", label: "记忆摘要" },
        { key: "rename", label: "重命名" },
        { type: "divider" },
        { key: "archive", label: "归档", danger: true },
      ],
      onClick: ({ key, domEvent }) => {
        domEvent.stopPropagation();
        if (key === "memory") void openMemoryModal(c.id);
        else if (key === "rename") startRename(c);
        else if (key === "archive") setArchiveTarget(c);
      },
    };
  }

  return (
    <div className="chat-page">
      <AppTopbar nav={APP_NAV} />
      <div className="chat-shell" style={listPane.style}>
        {/* 窄屏遮罩：侧栏以抽屉形式覆盖（原实现直接隐藏，手机上无法切换会话/智能体） */}
        {sidebarOpen && <div className="chat-sidebar-mask" onClick={() => setSidebarOpen(false)} />}

        {/* 左侧栏：Agent 选择 + 会话列表 */}
        <aside className={`chat-sidebar${sidebarOpen ? " open" : ""}`}>
          <div className="chat-agent-picker">
            <div className="chat-agent-label">当前智能体</div>
            <Select
              className="chat-agent-select"
              value={agentId || undefined}
              disabled={agents.length === 0}
              placeholder={agents.length === 0 ? "加载中…" : "选择智能体"}
              popupMatchSelectWidth={300}
              onChange={(next) => {
                if (next !== agentId) handleAgentChange(next);
              }}
              options={agents.map((a) => ({
                value: a.id,
                label: `${a.name}${a.is_default_parent ? "（默认）" : ""}`,
                disabled: !a.current_published_version_id,
              }))}
              optionRender={(option) => {
                const a = agents.find((x) => x.id === String(option.value));
                const usable = Boolean(a?.current_published_version_id);
                return (
                  <div className="chat-agent-opt">
                    <div className="chat-agent-opt-name">
                      <span>{option.label}</span>
                      {!usable && <span className="chat-agent-opt-tag">未发布</span>}
                    </div>
                    {a?.description && <div className="chat-agent-opt-desc">{a.description}</div>}
                  </div>
                );
              }}
            />
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
                actionLabel={admin && agents.length === 0 ? "一键开通" : agents.length === 0 ? "去创建智能体" : "去发布版本"}
                onAction={() => navigate(admin && agents.length === 0 ? "/onboarding" : "/agents")}
                hint={admin && agents.length === 0 ? "约 1 分钟完成，之后可直接对话" : "你也可以自己创建一个，仅自己可见"}
              />
            )}
          </div>

          <div className="chat-session-toolbar">
            <h3>会话</h3>
            <Tooltip title="查看任务历史">
              <Button size="small" icon={<HistoryOutlined />} onClick={() => void openTaskHistory()}>
                任务
              </Button>
            </Tooltip>
          </div>

          <div className="chat-session-filters">
            {/* 归档可恢复：默认隐藏已归档会话，勾选后可见并支持恢复 */}
            <Checkbox checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)}>
              <span className="chat-archived-label">显示已归档</span>
            </Checkbox>
            <Input
              allowClear
              size="small"
              placeholder="搜索会话标题"
              prefix={<SearchOutlined />}
              value={convFilter}
              onChange={(e) => setConvFilter(e.target.value)}
            />
          </div>

          <Conversations
            className="chat-conv-list"
            items={convItems}
            activeKey={conversationId ?? undefined}
            onActiveChange={(key) => {
              const conv = conversations.find((x) => x.id === String(key));
              if (conv) {
                selectConversation(conv);
                setSidebarOpen(false);
              }
            }}
            menu={(item) => {
              const conv = conversations.find((x) => x.id === String(item.key));
              return conv ? convMenu(conv) : { items: [] };
            }}
            creation={{
              label: "新建会话",
              disabled: !agentId,
              onClick: () => void newConversation(),
            }}
          />
          {filteredConvs.length === 0 && (
            <div className="chat-session-empty">
              {convFilter.trim()
                ? "没有匹配的会话"
                : showArchived
                  ? "暂无会话（含已归档）"
                  : agentId
                    ? "暂无会话，点击「新建会话」开始"
                    : "请先发布一个智能体"}
            </div>
          )}
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
              <Button size="small" type="text" onClick={() => void loadConfirmations()} title="查看确认记录">确认记录</Button>
              {task && (
                <button
                  className={`chat-task-badge ${taskBadgeInfo.cls}`}
                  type="button"
                  onClick={() => setTaskPanelOpen((open) => !open)}
                  title={taskPanelOpen ? "收起执行过程面板" : "展开执行过程面板"}
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
                    actionLabel={admin && agents.length === 0 ? "一键开通" : agents.length === 0 ? "去创建智能体" : "去发布版本"}
                    onAction={() => navigate(admin && agents.length === 0 ? "/onboarding" : "/agents")}
                    hint={admin && agents.length === 0 ? "约 1 分钟完成，之后可直接对话" : "你也可以自己创建一个，仅自己可见"}
                  />
                ) : (
                  <Prompts
                    className="chat-starters"
                    title="试试这样问"
                    vertical
                    items={STARTER_PROMPTS.map((p) => ({ key: p, label: p }))}
                    onItemClick={({ data }) => {
                      const text = STARTER_PROMPTS.find((p) => p === data.key) ?? "";
                      setInput(text);
                      senderRef.current?.focus();
                    }}
                  />
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
                {visibleMessages.map((m) => (
                  <MessageBubble
                    key={m.id}
                    m={m}
                    lastAssistantId={lastAssistantId}
                    busy={busy}
                    copiedId={copiedId}
                    onCopy={(text, key) => void copyText(text, key)}
                    onRegenerate={regenerateAnswer}
                    onEdit={editUserMessage}
                    onRetry={retryFailedMessage}
                    onDownload={(fileId, name) => void downloadFile(`/api/z-torque/files/${fileId}/raw`, name)}
                  />
                ))}
                {/* 一轮助手回复：同一个气泡内，过程（header）在上实时追加、结论（content）在下流式输出 */}
                {(activeRun || runAnswer !== null || streamingText !== null) && (
                  <Bubble
                    className={`chat-bubble chat-bubble-live${runAnswer !== null || streamingText !== null ? " streaming" : ""}`}
                    classNames={{ body: "chat-bubble-body", content: "chat-bubble-content", header: "chat-bubble-header", footer: "chat-bubble-footer", avatar: "chat-bubble-avatar" }}
                    placement="start"
                    variant="borderless"
                    shape="corner"
                    avatar={<Avatar size={30} className="chat-avatar-ai">Z</Avatar>}
                    header={activeRun ? (
                      <RunBlock
                        activeRun={activeRun}
                        taskError={task?.id === activeRun.taskId && task.error_message ? { message: taskErrorText(task.error_message, task.error_code), code: task.error_code } : null}
                        onResolve={(approved) => void resolveConfirm(approved)}
                      />
                    ) : undefined}
                    // Bubble 在 loading 时不渲染 header/footer（源码：`if (loading) return loadingRender()`），
                    // 而执行链路正挂在 header 槽里 —— 若按「没有正文就 loading」处理，
                    // 整个执行中阶段执行块会被吞掉，直到任务结束正文到达才一次性出现。
                    // 因此只有「还没有执行块」的等待期（决策轮）才用 loading 占位。
                    loading={turnWaiting && !activeRun}
                    loadingRender={() => (
                      // 首字节等待：明确告诉用户「在做什么 + 已经多久」，避免空白气泡看起来像卡死
                      <div className="chat-wait">
                        <span className="spinner" />
                        <span>{activeRun ? "正在执行…" : "正在理解你的需求…"}</span>
                        {waitElapsed && <span className="chat-wait-elapsed">{waitElapsed}</span>}
                      </div>
                    )}
                    streaming={streamingText !== null}
                    content={
                      <div className={`chat-md${streamingText !== null || typeTimerRef.current !== null ? " chat-streaming-cursor" : ""}`}>
                        <Markdown remarkPlugins={[remarkGfm]}>{turnText}</Markdown>
                      </div>
                    }
                    footer={
                      <div className="chat-msg-foot">
                        {/* 跳过动画入口：仅在打字机定时器存活时显示（普通流式本就实时直出，无需跳过） */}
                        {runAnswer !== null && streamingText === null && typeTimerRef.current !== null && (
                          <button className="chat-msg-action" type="button" onClick={skipTypewriter} title="跳过打字动画，立即显示完整回复">
                            跳过动画
                          </button>
                        )}
                        {turnTime && <span className="chat-msg-meta">{turnTime}</span>}
                      </div>
                    }
                    footerPlacement="outer-start"
                  />
                )}
              </>
            )}
          </div>

          {/* 输入区：Sender（内置 Enter 发送 / loading 时停止按钮） */}
          <div className="chat-composer">
            <Sender
              ref={senderRef}
              className="chat-sender"
              value={input}
              onChange={(next) => setInput(next)}
              onSubmit={(text) => {
                if (!text.trim()) return;
                void sendMessage({ text });
              }}
              loading={busy}
              onCancel={() => void stopActive()}
              submitType="enter"
              autoSize={{ minRows: 1, maxRows: 6 }}
              disabled={agents.length === 0}
              placeholder="输入消息，Enter 发送，Shift+Enter 换行"
              header={
                attachments.length > 0 || (uploading && uploadPercent !== null) ? (
                  <div className="chat-pending">
                    {/* 附件上传真实进度：XHR progress 事件驱动，上传完成后服务端解析期间显示「处理中」 */}
                    {uploading && uploadPercent !== null && (
                      <div className="chat-upload-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={uploadPercent}>
                        <Progress percent={uploadPercent} size="small" showInfo={false} status={uploadPercent < 100 ? "active" : "normal"} />
                        <span className="chat-upload-progress-text">{uploadPercent < 100 ? `上传中 ${uploadPercent}%` : "处理中…"}</span>
                      </div>
                    )}
                    {attachments.length > 0 && (
                      <Attachments
                        items={attachments.map((f) => ({
                          uid: f.id,
                          name: f.name,
                          size: f.size_bytes,
                          status: "done" as const,
                          type: "file" as const,
                          onClick: () => void viewFile(f),
                        }))}
                        overflow="wrap"
                        disabled
                        onRemove={(item) => {
                          setAttachments((prev) => prev.filter((x) => x.id !== String(item.uid)));
                          return true;
                        }}
                      />
                    )}
                  </div>
                ) : undefined
              }
              prefix={
                <Tooltip title={uploading ? "上传中…" : "上传附件"}>
                  <label className="chat-upload-btn">
                    {uploading ? <span className="spinner" style={{ width: 15, height: 15 }} /> : <PaperClipOutlined />}
                    <input
                      type="file"
                      hidden
                      onChange={(e) => {
                        const f = e.target.files?.[0];
                        if (f) void handleUpload(f);
                        e.target.value = "";
                      }}
                    />
                  </label>
                </Tooltip>
              }
              footer={
                <div className="chat-sender-foot">
                  <span className="chat-sender-hint">支持 PDF/TXT/MD/CSV/XLSX，单文件 ≤ 50MB</span>
                </div>
              }
            />
            {error && (
              <div className="chat-composer-error">
                {modelMissing && admin ? (
                  <GuideCallout
                    compact
                    title="还没有配置模型"
                    why="对话需要一个已绑定模型的助手。完成一次开通即可继续。"
                    actionLabel="去开通向导"
                    onAction={() => navigate("/onboarding")}
                    hint="约 1 分钟完成，之后无需再次配置"
                  />
                ) : (
                  <Alert kind="error">{error}</Alert>
                )}
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

      {/* 重命名会话：⋯ 菜单打开（替代行内输入框） */}
      <Modal
        open={renameTarget !== null}
        title="重命名会话"
        onClose={() => setRenameTarget(null)}
        width={460}
        footer={
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <button className="btn btn-secondary btn-sm" onClick={() => setRenameTarget(null)}>取消</button>
            <button className="btn btn-primary btn-sm" disabled={!renameValue.trim()} onClick={() => void renameConversation()}>保存</button>
          </div>
        }
      >
        <Input
          value={renameValue}
          maxLength={120}
          autoFocus
          placeholder="会话标题"
          onChange={(e) => setRenameValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void renameConversation();
            }
          }}
        />
      </Modal>

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
