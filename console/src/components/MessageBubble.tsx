// 会话消息气泡：主会话页与多会话分栏共用同一套 Bubble 渲染，
// 保证用户/助手气泡、头像、时间、Markdown 与附件格式完全一致。
import { useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Avatar } from "antd";
import { Bubble } from "@ant-design/x";
import { downloadFile, fetchBlob } from "../services/api";
import { HistoricalRun } from "../pages/chatRun";
import type { Message, MessageContentPart } from "../types";

const MEDIA_PLACEHOLDER_RE = /^\[(图片|语音|视频|文件)\]$/;
const MEDIA_KINDS = new Set(["image", "audio", "video"]);

// 外部来源消息的徽标文案；未登记的渠道统一显示「外部」
const EXTERNAL_SOURCE_LABELS: Record<string, string> = {
  webhook: "Webhook",
  api: "API",
};
function externalSourceLabel(m: Message): string {
  const label = EXTERNAL_SOURCE_LABELS[String(m.source ?? "")] ?? "外部";
  // external_sender 存储格式为「接入名称:会话标识」，徽标只展示接入名称（冒号前的部分）
  const sender = m.external_sender ? m.external_sender.split(":")[0] : "";
  return sender ? `${label} · ${sender}` : label;
}

// 外部来源消息头像：按渠道展示对应图标（Webhook=链接、API=代码），替代固定"外"字
function ExternalSourceAvatar({ source }: { source: string }) {
  const stroke = { fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
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
      <path d="M14 11a5 5 0 0 0 7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" {...stroke} />
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

export interface MessageBubbleProps {
  m: Message;
  lastAssistantId?: string | null;
  busy?: boolean;
  copiedId?: string | null;
  onCopy?: (text: string, key: string) => void;
  onRegenerate?: (m: Message) => void;
  onEdit?: (m: Message) => void;
  onRetry?: (m: Message) => void;
  onDownload?: (fileId: string, name: string) => void;
}

/** 一条消息气泡：Bubble 承担头像/气泡/头脚槽位，Markdown 与附件仍在内容槽内 */
export function MessageBubble({
  m,
  lastAssistantId,
  busy = false,
  copiedId,
  onCopy,
  onRegenerate,
  onEdit,
  onRetry,
  onDownload,
}: MessageBubbleProps) {
  const isUser = m.sender_type === "USER";
  // 外部来源（通用 Webhook 等）消息：用户侧内容 + 独立虚线气泡与来源徽标
  const isExternal = m.sender_type === "EXTERNAL";
  const externalSource = isExternal ? String(m.source ?? "") : "";
  const failed = !isUser && m.status === "FAILED";
  // 防御性清理历史回复/旧模型输出中的内部检索标识，避免 UUID 暴露在用户界面。
  const displayContent = String(m.content ?? "").replace(/\\[(?:chunk|source):[^\\]\\n]+\\]/gi, "").replace(/[ \\t]{2,}/g, " ").trim();
  const allParts = Array.isArray(m.content_parts) ? m.content_parts : [];
  const parts = allParts.filter((p) => p?.file_id);
  const proposalParts = allParts.filter((p) => String(p.type ?? "") === "file_change" && p.change_id);
  const hasAttachment = parts.length > 0 || proposalParts.length > 0;
  // 占位文本只在没有附件时兜底显示
  const hidePlaceholder = hasAttachment && MEDIA_PLACEHOLDER_RE.test(m.content.trim());
  const showRegenerate = Boolean(onRegenerate) && !isUser && !isExternal && !failed && m.id === lastAssistantId && !busy;
  const showEdit = Boolean(onEdit) && isUser && !busy;
  const showActions = (failed && Boolean(onRetry)) || (!failed && Boolean(m.content)) || showRegenerate || showEdit;

  const body = (
    <>
      {isExternal && <div className="chat-msg-source-badge">{externalSourceLabel(m)}</div>}
      {isUser || isExternal ? (
        <div className="chat-plain">{hidePlaceholder ? "" : m.content}</div>
      ) : (
        <div className="chat-md">
          <Markdown remarkPlugins={[remarkGfm]}>{displayContent}</Markdown>
        </div>
      )}
      {hasAttachment && (
        <div className="chat-msg-files">
          {proposalParts.map((p, i) => (
            <span key={`proposal-${i}`} className="chat-msg-file-proposal" title="文件变更提案，待确认">
              变更提案 · {String(p.file_name ?? "文件")}
            </span>
          ))}
          {parts.map((p, i) => {
            if (partKind(p) !== "file") return <MessageMedia key={i} part={p} />;
            const name = String(p.file_name ?? `附件 ${String(p.file_id).slice(0, 8)}`);
            return (
              <button
                key={i}
                className="chat-msg-file"
                type="button"
                onClick={() => (onDownload ?? ((id, n) => void downloadFile(`/api/z-torque/files/${id}/raw`, n)))(String(p.file_id), name)}
                title="下载附件"
              >
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
          {failed && onRetry && (
            <button className="chat-msg-action" type="button" disabled={busy} onClick={() => onRetry(m)}>
              重试
            </button>
          )}
          {!failed && m.content && (
            <button
              className="chat-msg-action"
              type="button"
              onClick={() => {
                if (onCopy) onCopy(m.content, m.id);
                else void navigator.clipboard?.writeText(m.content);
              }}
            >
              {copiedId === m.id ? "已复制" : "复制"}
            </button>
          )}
          {/* 重新生成：仅最新一条正常助手回复提供（外部来源消息不参与） */}
          {showRegenerate && onRegenerate && (
            <button className="chat-msg-action" type="button" onClick={() => onRegenerate(m)} title="基于你上一条消息重新生成一份回答（原回答保留）">
              重新生成
            </button>
          )}
          {/* 编辑重发：用户消息可回填输入框修改后重发（后端无消息删除接口，原对话保留） */}
          {showEdit && onEdit && (
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
      // 历史任务的执行链路内联在结论之上（与发生顺序一致）
      header={!isUser && !isExternal && m.task_id ? <HistoricalRun taskId={String(m.task_id)} /> : undefined}
      content={body}
      footer={footer}
      footerPlacement={isUser ? "outer-end" : "outer-start"}
    />
  );
}
