import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Avatar, Button, Select } from "antd";
import { Bubble } from "@ant-design/x";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { downloadFile, request, uploadFile } from "../services/api";
import { openGetStream, openPostStream, type SseHandle } from "../services/sse";
import { ActiveRun, eventLabel, localStep, payloadOf, RunBlock, TERMINAL, type TaskStep } from "../pages/chatRun";
import type { Conversation, FileMetadata, Message, Task } from "../types";
import { MessageBubble } from "./MessageBubble";

interface Props {
  conversations: Conversation[];
  currentId: string | null;
  onClose: () => void;
  generationPreset: string;
}

function parseAgentMentions(text: string): { text: string; mentions: Array<{ type: "agent"; id: string }> } {
  const found = [...text.matchAll(/@\[agent:([0-9a-f-]{36})\]/gi)].map((match) => ({ type: "agent" as const, id: match[1] }));
  const cleaned = text.replace(/@\[[^\]]*?\]/g, "").replace(/\s+/g, " ").trim();
  return { text: cleaned || text, mentions: found.slice(0, 1) };
}

export function ConversationPane({ conversations, currentId, onClose, generationPreset }: Props) {
  const options = useMemo(
    () => conversations.filter((item) => item.id !== currentId).map((item) => ({ label: item.title || "未命名会话", value: item.id })),
    [conversations, currentId],
  );
  const [convId, setConvId] = useState<string | null>(() => options[0]?.value ?? null);
  const convIdRef = useRef<string | null>(convId);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [streamingText, setStreamingText] = useState<string | null>(null);
  const [typeText, setTypeText] = useState<string | null>(null);
  const [activeRun, setActiveRun] = useState<ActiveRun | null>(null);
  const [attachments, setAttachments] = useState<FileMetadata[]>([]);
  const [uploading, setUploading] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const streamRef = useRef<SseHandle | null>(null);
  const typeTimerRef = useRef<number | null>(null);
  const pollRef = useRef<number | null>(null);

  const loadMessages = useCallback(async (id: string, quiet = false) => {
    try {
      const value = await request<Message[]>(`/api/z-torque/conversations/${id}/messages`);
      if (convIdRef.current !== id) return;
      if (Array.isArray(value)) setMessages(value);
    } catch {
      if (!quiet && convIdRef.current === id) setError("消息加载失败，请重新选择会话");
    }
  }, []);

  useEffect(() => {
    convIdRef.current = convId;
    setStreamingText(null);
    setTypeText(null);
    setActiveRun(null);
    streamRef.current?.abort();
    if (pollRef.current) window.clearInterval(pollRef.current);
    if (!convId) { setMessages([]); return; }
    void loadMessages(convId);
    return () => { streamRef.current?.abort(); if (pollRef.current) window.clearInterval(pollRef.current); };
  }, [convId, loadMessages]);

  useEffect(() => {
    const element = scrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [messages.length, streamingText, typeText, activeRun?.steps.length]);

  const clearTypeTimer = useCallback(() => {
    if (typeTimerRef.current !== null) { window.clearInterval(typeTimerRef.current); typeTimerRef.current = null; }
  }, []);

  const startTypewriter = useCallback((text: string, onDone?: () => void) => {
    clearTypeTimer();
    setTypeText("");
    let index = 0;
    typeTimerRef.current = window.setInterval(() => {
      index = Math.min(text.length, index + 3);
      setTypeText(text.slice(0, index));
      if (index >= text.length) { clearTypeTimer(); onDone?.(); }
    }, 12);
  }, [clearTypeTimer]);

  const subscribeTask = useCallback((taskId: string, target: string) => {
    streamRef.current?.abort();
    const seen = new Set<string>();
    const applyRun = (updater: (prev: ActiveRun) => ActiveRun) => setActiveRun((prev) => (prev?.taskId === taskId ? updater(prev) : prev));
    const finish = () => {
      setActiveRun((prev) => (prev?.taskId === taskId ? null : prev));
      setSending(false);
      void loadMessages(target, true);
    };
    // 兜底轮询：任务事件流偶发丢终态时，靠任务状态主动发现结束
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = window.setInterval(() => {
      void request<Task>(`/api/z-torque/tasks/${taskId}`).then((task) => {
        if (convIdRef.current !== target) return;
        if (TERMINAL.has(task.status)) {
          if (pollRef.current) window.clearInterval(pollRef.current);
          finish();
        }
      }).catch(() => undefined);
    }, 2500);

    streamRef.current = openGetStream(`/api/z-torque/tasks/${taskId}/stream`, {
      onEvent: (msg) => {
        const d = msg.data as Record<string, unknown>;
        if (msg.event === "task.snapshot") {
          const status = String(d.status ?? "");
          applyRun((prev) => ({ ...prev, status }));
          return;
        }
        const key = String(msg.id ?? "");
        if (key && seen.has(key)) return;
        if (key) seen.add(key);
        if (msg.event === "task.done") {
          if (pollRef.current) window.clearInterval(pollRef.current);
          const status = String(d.status ?? "");
          const content = (d.result as Record<string, unknown> | null)?.content;
          setSending(false);
          if (status === "SUCCESS" && typeof content === "string" && content) {
            startTypewriter(content, () => { finish(); });
          } else { finish(); }
          return;
        }
        const step: TaskStep = {
          seq: Number(d.seq ?? seen.size),
          eventType: msg.event,
          summary: String(d.summary ?? eventLabel(msg.event)),
          time: new Date().toLocaleTimeString(),
          payload: (d.payload_summary as Record<string, unknown> | undefined) ?? payloadOf(d),
        };
        applyRun((prev) => ({ ...prev, steps: prev.steps.some((s) => s.seq === step.seq) ? prev.steps : [...prev.steps, step] }));
        const state = String(d.to_state ?? d.status ?? "");
        if (state) applyRun((prev) => ({ ...prev, status: state }));
        if (msg.event === "TASK_WAITING_HUMAN") applyRun((prev) => ({ ...prev, waiting: true }));
        if (msg.event === "TASK_APPROVAL_GRANTED" || msg.event === "TASK_APPROVAL_REJECTED") applyRun((prev) => ({ ...prev, waiting: false }));
        if (msg.event === "TASK_CANCELLED" || msg.event === "TASK_APPROVAL_REJECTED") {
          if (pollRef.current) window.clearInterval(pollRef.current);
          finish();
        }
      },
      onDone: () => { void loadMessages(target, true); },
      onError: () => setError("执行过程连接中断，正在重新获取消息"),
    });
  }, [loadMessages, startTypewriter]);

  async function send() {
    const raw = input.trim();
    const target = convId;
    if (!raw || !target || sending) return;
    const { text, mentions } = parseAgentMentions(raw);
    const parts = attachments.map((file) => ({ file_id: file.id }));
    const optimistic: Message = {
      id: `local-${Date.now()}`, conversation_id: target, sender_type: "USER", content: text,
      content_parts: parts.length ? parts : null, status: "PROCESSING", task_id: null, error_code: null,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, optimistic]);
    setInput("");
    setAttachments([]);
    setSending(true);
    setError(null);
    setStreamingText("");
    clearTypeTimer();
    setTypeText(null);
    setActiveRun(null);

    try {
      // 与主页面一致：先走流式入口，工具调用会由后端分流为任务
      const body = { agent_id: conversations.find((item) => item.id === target)?.agent_id ?? null, conversation_id: target, message: text, content_parts: parts.length ? parts : null, stream: true, mentions, generation_preset: generationPreset };
      let taskId: string | null = null;
      let settled = false;
      await new Promise<void>((resolve, reject) => {
        streamRef.current = openPostStream("/api/z-torque/chat/stream", body, {
          onEvent: (msg) => {
            if (convIdRef.current !== target) return;
            const d = msg.data as Record<string, unknown>;
            if (msg.event === "message.delta") {
              setStreamingText((prev) => (prev ?? "") + String(d.content ?? ""));
            } else if (msg.event === "message.result") {
              const content = String(d.content ?? "");
              if (!content) return;
              settled = true;
              setStreamingText(null);
              startTypewriter(content, () => { setSending(false); void loadMessages(target, true); });
            } else if (msg.event === "message.task_created") {
              taskId = String(d.task_id ?? "") || null;
              if (!taskId) return;
              settled = true;
              setStreamingText(null);
              setActiveRun({ taskId, steps: [localStep("LOCAL_UNDERSTAND", "识别到需要执行，准备创建任务")], status: "QUEUED", waiting: false, startedAt: Date.now() });
              subscribeTask(taskId, target);
            } else if (msg.event === "message.error") {
              reject(new Error(String(d.message ?? "生成失败")));
            }
          },
          onError: reject,
          onDone: () => { if (!settled) void loadMessages(target, true); resolve(); },
        }, { "Idempotency-Key": crypto.randomUUID() });
      });
      if (!taskId) setSending(false);
    } catch (err) {
      if (convIdRef.current !== target) return;
      setMessages((prev) => prev.filter((item) => item.id !== optimistic.id));
      setInput(raw);
      setAttachments(attachments);
      setError(err instanceof Error ? err.message : "发送失败，请重试");
      setSending(false);
    }
  }

  async function handleUpload(file: File) {
    if (!convId || uploading) return;
    setUploading(true);
    setError(null);
    try {
      const meta = await uploadFile<FileMetadata>("/api/z-torque/files", file, { conversationId: convId });
      setAttachments((prev) => [...prev, meta]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "上传失败");
    } finally {
      setUploading(false);
    }
  }

  const busy = sending || streamingText !== null || activeRun !== null;
  const liveText = typeText ?? streamingText;

  return (
    <div className="chat-split-pane">
      <div className="chat-split-pane-head">
        <Select size="small" value={convId ?? undefined} options={options} placeholder="选择会话"
          onChange={(value) => { setConvId(value ?? null); setInput(""); setError(null); }} style={{ flex: 1, minWidth: 0 }} />
        <Button size="small" type="text" title="关闭分栏" onClick={onClose}>关闭</Button>
      </div>
      <div className="chat-split-pane-messages" ref={scrollRef}>
        {messages.length === 0 && <div className="chat-split-pane-empty">选择会话后，可在这里独立发送和查看消息。</div>}
        {messages.map((message) => (
          <MessageBubble
            key={message.id}
            m={message}
            onDownload={(fileId, name) => void downloadFile(`/api/z-torque/files/${fileId}/raw`, name).catch((err) => setError(err instanceof Error ? err.message : "下载失败"))}
          />
        ))}
        {(activeRun || liveText !== null) && (
          <Bubble className="chat-bubble chat-bubble-live" placement="start" variant="borderless" shape="corner"
            classNames={{ body: "chat-bubble-body", content: "chat-bubble-content", header: "chat-bubble-header", footer: "chat-bubble-footer", avatar: "chat-bubble-avatar" }}
            avatar={<Avatar size={30} className="chat-avatar-ai">Z</Avatar>}
            header={activeRun ? <RunBlock activeRun={activeRun} taskError={null} onResolve={() => undefined} /> : undefined}
            content={
              <div className="chat-md">
                <Markdown remarkPlugins={[remarkGfm]}>{liveText ?? ""}</Markdown>
              </div>
            } />
        )}
      </div>
      {error && <div className="chat-split-pane-error">{error}</div>}
      {attachments.length > 0 && (
        <div className="chat-split-pane-files">
          {attachments.map((file) => <span key={file.id} className="chat-split-pane-file">{file.name || file.id}
            <button type="button" onClick={() => setAttachments((prev) => prev.filter((item) => item.id !== file.id))}>×</button></span>)}
        </div>
      )}
      <div className="chat-split-pane-composer">
        <label className="chat-split-pane-upload">
          {uploading ? "上传中…" : "＋附件"}
          <input type="file" hidden disabled={!convId || uploading} onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void handleUpload(file);
            event.target.value = "";
          }} />
        </label>
        <textarea value={input} rows={2} placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} />
        <div className="chat-split-pane-actions">
          {busy && <Button size="small" type="text" onClick={() => { streamRef.current?.abort(); if (pollRef.current) window.clearInterval(pollRef.current); setSending(false); setActiveRun(null); setStreamingText(null); }}>停止</Button>}
          <Button type="primary" size="small" loading={sending} disabled={!convId || !input.trim()} onClick={() => void send()}>发送</Button>
        </div>
      </div>
    </div>
  );
}
