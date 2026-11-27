import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Drawer, Popover, Select } from "antd";
import { DeleteOutlined, SwapOutlined, ThunderboltOutlined, UploadOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { request, uploadFile } from "../services/api";
import type { FileContent, FileMetadata } from "../types";
import { PatchReviewPanel } from "./PatchReview";

// P1b-UX-1：能力/用量头部横条。全部文案对用户可读，不暴露
// MCP / AVAILABLE / DEGRADED / UNAVAILABLE / token / unknown 等专业术语。
type CapabilityState = "AVAILABLE" | "DEGRADED" | "UNAVAILABLE";

interface CapabilityAction {
  label: string;
  route?: string;
  action_code?: string;
  method?: string;
}

interface CapabilityCard {
  state: CapabilityState;
  label: string;
  reason_code: string | null;
  reason: string;
  actions: CapabilityAction[];
}

interface CapabilitiesPayload {
  capability_cards?: {
    external_tools: CapabilityCard;
    knowledge: CapabilityCard;
    model: CapabilityCard;
  };
}

interface UsageRow {
  model: string;
  window: string;
  token_in: number | string;
  token_out: number | string;
}

interface CapabilityVm extends CapabilityCard {
  key: "tools" | "knowledge" | "model";
}

const STATE_LABEL: Record<CapabilityState, string> = {
  AVAILABLE: "可用",
  DEGRADED: "受限",
  UNAVAILABLE: "不可用",
};

const CARD_KEYS = ["external_tools", "knowledge", "model"] as const;

// 原因、动作全部来自后端 capability_cards；前端只做展示与动作分发，不编造状态。
function readCards(caps: CapabilitiesPayload): CapabilityVm[] | null {
  const cards = caps.capability_cards;
  if (!cards) return null;
  const result: CapabilityVm[] = [];
  const keys: CapabilityVm["key"][] = ["tools", "knowledge", "model"];
  for (let index = 0; index < CARD_KEYS.length; index += 1) {
    const card = cards[CARD_KEYS[index]];
    if (!card?.label || !STATE_LABEL[card.state] || !Array.isArray(card.actions)) return null;
    result.push({ ...card, key: keys[index] });
  }
  return result;
}

function compactNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}m`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function todayUsageText(rows: UsageRow[]): string {
  // 后端按 UTC 日期窗口聚合，前端用同一口径只统计今天，不用「token」字样。
  const today = new Date().toISOString().slice(0, 10);
  const todays = rows.filter((row) => row.window === today);
  const read = (field: "token_in" | "token_out") => {
    const values = todays.map((row) => row[field]);
    if (values.length === 0 || values.every((v) => typeof v !== "number")) return "未上报";
    return compactNumber(values.reduce<number>((sum, v) => sum + (typeof v === "number" ? v : 0), 0));
  };
  return `输入 ${read("token_in")} / 输出 ${read("token_out")}`;
}

export function CapabilityStrip({ onUpload }: { onUpload?: () => void }) {
  const navigate = useNavigate();
  const [caps, setCaps] = useState<CapabilitiesPayload | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [usage, setUsage] = useState<UsageRow[]>([]);
  const [reload, setReload] = useState(0);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [reindexing, setReindexing] = useState(false);

  useEffect(() => {
    let active = true;
    void request<CapabilitiesPayload>("/api/z-torque/capabilities")
      .then((value) => {
        if (!active) return;
        if (!readCards(value)) { setCaps(null); setLoadFailed(true); return; }
        setCaps(value);
        setLoadFailed(false);
      })
      .catch(() => { if (active) { setCaps(null); setLoadFailed(true); } });
    return () => { active = false; };
  }, [reload]);

  useEffect(() => {
    let active = true;
    void request<UsageRow[]>("/api/z-torque/usage/summary")
      .then((value) => { if (active) setUsage(Array.isArray(value) ? value : []); })
      .catch(() => { if (active) setUsage([]); });
    return () => { active = false; };
  }, [reload]);

  const items = useMemo(() => (caps ? readCards(caps) ?? [] : []), [caps]);

  async function runAction(action: CapabilityAction) {
    if (action.route) {
      navigate(action.route);
      return;
    }
    switch (action.action_code) {
      case "UPLOAD_KNOWLEDGE":
        onUpload?.();
        return;
      case "RETEST_MCP":
        setFeedback(null);
        setReload((n) => n + 1);
        return;
      case "REINDEX_KNOWLEDGE": {
        setFeedback(null);
        setReindexing(true);
        try {
          await request("/api/z-torque/knowledge/reindex", { method: action.method || "POST" });
          setFeedback("索引重建已提交，可重新测试连接查看最新状态");
          setReload((n) => n + 1);
        } catch {
          setFeedback("索引重建未完成，请稍后重试");
        } finally {
          setReindexing(false);
        }
        return;
      }
      default:
        setFeedback("该动作暂不支持，请稍后重试");
    }
  }

  if (!caps) {
    return (
      <div className="chat-cap-strip">
        <span className="chat-cap-loading">{loadFailed ? "能力状态暂时无法获取" : "能力状态加载中…"}</span>
        {loadFailed && <Button size="small" type="link" onClick={() => setReload((n) => n + 1)}>重试</Button>}
      </div>
    );
  }

  return (
    <div className="chat-cap-strip">
      {items.map((item) => {
        const tone = item.state === "AVAILABLE" ? "ok" : item.state === "DEGRADED" ? "limited" : "off";
        const content = (
          <div className="chat-cap-popover">
            <div className="chat-cap-pop-title">{item.label}：{STATE_LABEL[item.state]}</div>
            {item.reason && <div className="chat-cap-pop-reason">原因：{item.reason}</div>}
            {feedback && <div className="chat-cap-pop-feedback">{feedback}</div>}
            <div className="chat-cap-pop-actions">
              {item.actions.map((action) => (
                <Button
                  key={action.label}
                  size="small"
                  type={action.route ? "primary" : "default"}
                  loading={reindexing && action.action_code === "REINDEX_KNOWLEDGE"}
                  onClick={() => void runAction(action)}
                >
                  {action.label}
                </Button>
              ))}
            </div>
          </div>
        );
        return (
          <Popover key={item.key} content={content} trigger="click" placement="bottomLeft">
            <button type="button" className={`chat-cap-item ${tone}`} title={`查看${item.label}详情`}>
              <span className="chat-cap-label">{item.label}</span>
              <span className="chat-cap-state">{STATE_LABEL[item.state]}</span>
            </button>
          </Popover>
        );
      })}
      <span className="chat-cap-usage">今日用量：{todayUsageText(usage)}</span>
    </div>
  );
}

type TextChange = { kind: "same" | "added" | "removed"; text: string };

function splitText(text: string): string[] {
  return text.replace(/\r\n/g, "\n").split(/(?<=[。！？!?；;\n])/).map((part) => part.trim()).filter(Boolean);
}

function compareText(before: string[], after: string[]): TextChange[] {
  const table: number[][] = Array.from({ length: before.length + 1 }, () => Array(after.length + 1).fill(0));
  for (let i = before.length - 1; i >= 0; i -= 1) {
    for (let j = after.length - 1; j >= 0; j -= 1) {
      table[i][j] = before[i] === after[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const rows: TextChange[] = [];
  let i = 0;
  let j = 0;
  while (i < before.length && j < after.length) {
    if (before[i] === after[j]) { rows.push({ kind: "same", text: before[i] }); i += 1; j += 1; }
    else if (table[i + 1][j] >= table[i][j + 1]) { rows.push({ kind: "removed", text: before[i] }); i += 1; }
    else { rows.push({ kind: "added", text: after[j] }); j += 1; }
  }
  while (i < before.length) { rows.push({ kind: "removed", text: before[i] }); i += 1; }
  while (j < after.length) { rows.push({ kind: "added", text: after[j] }); j += 1; }
  return rows;
}

export function FileCompareDrawer({
  conversationId,
  open: controlledOpen,
  onOpenChange,
  focusChangeId,
  onChanged,
}: {
  conversationId: string | null;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  focusChangeId?: string | null;
  onChanged?: () => void;
}) {
  const [internalOpen, setInternalOpen] = useState(false);
  const open = controlledOpen ?? internalOpen;
  const setOpen = onOpenChange ?? setInternalOpen;
  const [activeTab, setActiveTab] = useState<"changes" | "custom">("changes");
  const [files, setFiles] = useState<FileMetadata[]>([]);
  const [baseId, setBaseId] = useState<string | null>(null);
  const [compareFile, setCompareFile] = useState<FileMetadata | null>(null);
  const [localBaseFile, setLocalBaseFile] = useState<FileMetadata | null>(null);
  const [mode, setMode] = useState<"conversation" | "local">("conversation");
  const [uploading, setUploading] = useState(false);
  const [uploadingBase, setUploadingBase] = useState(false);
  const [baseOrigin, setBaseOrigin] = useState<"session" | "local">("session");
  const [compareOrigin, setCompareOrigin] = useState<"local" | "session">("local");
  const [baseText, setBaseText] = useState<string | null>(null);
  const [compareTextValue, setCompareTextValue] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const uploadRef = useRef<HTMLInputElement>(null);
  const localBaseRef = useRef<HTMLInputElement>(null);

  function switchMode(next: "conversation" | "local") {
    setMode(next);
    setBaseText(null);
    setCompareTextValue(null);
    setError(null);
  }

  function removeLocalBaseFile() {
    setLocalBaseFile(null);
    setBaseText(null);
    setError(null);
    if (localBaseRef.current) localBaseRef.current.value = "";
  }

  function removeCompareFile() {
    setCompareFile(null);
    setCompareTextValue(null);
    setError(null);
    if (uploadRef.current) uploadRef.current.value = "";
  }

  function swapFiles() {
    setBaseText(null);
    setCompareTextValue(null);
    setError(null);
    if (mode === "local") {
      setLocalBaseFile(compareFile);
      setCompareFile(localBaseFile);
      return;
    }
    if (!baseId || !compareFile) return;
    const previousBase = files.find((file) => file.id === baseId) ?? null;
    setBaseId(compareFile.id);
    setBaseOrigin(compareOrigin);
    setCompareFile(previousBase);
    setCompareOrigin(baseOrigin);
  }

  useEffect(() => {
    if (!open) return;
    let active = true;
    void request<FileMetadata[]>("/api/z-torque/files", { query: conversationId ? { conversation_id: conversationId } : undefined })
      .then((value) => { if (active) setFiles(Array.isArray(value) ? value : []); })
      .catch(() => { if (active) setError("会话文件加载失败"); });
    return () => { active = false; };
  }, [open, conversationId]);

  async function readContent(id: string): Promise<string> {
    const item = await request<FileContent>(`/api/z-torque/files/${id}/content`);
    return item.content ?? "";
  }

  async function runCompare(overrideBaseId?: string, overrideCompareId?: string) {
    setError(null);
    setBaseText(null);
    setCompareTextValue(null);
    const baseFileId = overrideBaseId ?? (mode === "conversation" ? baseId : localBaseFile?.id);
    const compareId = overrideCompareId ?? compareFile?.id;
    if (!baseFileId || !compareId) {
      setError(mode === "local" ? "请上传两个本地文件" : "请先选择会话文件并上传要对比的文件");
      return;
    }
    try {
      const [a, b] = await Promise.all([readContent(baseFileId), readContent(compareId)]);
      setBaseText(a);
      setCompareTextValue(b);
    } catch (err) {
      setError(err instanceof Error ? err.message : "文件内容读取失败");
    }
  }

  const baseFileMeta = mode === "conversation" ? files.find((file) => file.id === baseId) ?? null : localBaseFile;
  const baseName = baseFileMeta?.name ?? (mode === "conversation" && files.length === 0 ? "当前会话暂无文件" : "未选择文件");
  const rows = useMemo(
    () => (baseText !== null && compareTextValue !== null ? compareText(splitText(baseText), splitText(compareTextValue)) : []),
    [baseText, compareTextValue],
  );
  const added = rows.filter((row) => row.kind === "added");
  const removed = rows.filter((row) => row.kind === "removed");
  const unchanged = rows.filter((row) => row.kind === "same");

  return (
    <>
      <Button className="file-compare-trigger" size="small" type="default" title="文件变更" icon={<SwapOutlined />} onClick={() => setOpen(true)}>文件变更</Button>
      <Drawer title="文件变更" open={open} onClose={() => setOpen(false)} width="min(1120px, 94vw)">
        <div className="file-compare-tabs">
          <button type="button" className={activeTab === "changes" ? "active" : ""} onClick={() => setActiveTab("changes")}>文件变更</button>
          <button type="button" className={activeTab === "custom" ? "active" : ""} onClick={() => setActiveTab("custom")}>自定义对比</button>
        </div>
        {activeTab === "changes" ? (
          <PatchReviewPanel
            conversationId={conversationId}
            active={open}
            focusChangeId={focusChangeId}
            onChanged={onChanged}
          />
        ) : (
          <>
        <div className="file-compare-hint">
          支持 PDF、Word(.docx)、TXT、Markdown、CSV、Excel。可对比会话产出与本地文件，也可以直接上传两个本地文件互相对比。
        </div>
        <div className="file-compare-modes">
          <button type="button" className={mode === "conversation" ? "active" : ""} onClick={() => switchMode("conversation")}>会话文件对比</button>
          <button type="button" className={mode === "local" ? "active" : ""} onClick={() => switchMode("local")}>本地文件对比</button>
        </div>
        <div className="file-compare-workbench">
          <section className="file-compare-panel">
            <header className="file-compare-panel-head">
              <span className={`file-compare-source ${baseOrigin}`}>{baseOrigin === "session" ? "会话文件" : "本地文件"}</span>
              <strong>基准文件</strong>
            </header>
            <div className="file-compare-panel-body">
              {mode === "conversation" ? (
                <Select
                  size="small"
                  value={baseId ?? undefined}
                  options={files.map((file) => ({ label: file.name, value: file.id }))}
                  placeholder={files.length === 0 ? "当前会话暂无文件" : "选择文件"}
                  onChange={(value) => { setBaseId(value ?? null); setBaseOrigin("session"); setBaseText(null); }}
                  style={{ width: "100%" }}
                />
              ) : (
                <>
                  <label className="file-compare-upload-btn">
                    <UploadOutlined />
                    {uploadingBase ? "上传中…" : localBaseFile ? "重新选择" : "选择文件"}
                    <input
                      ref={localBaseRef}
                      type="file"
                      accept=".pdf,.docx,.txt,.md,.csv,.xlsx"
                      disabled={uploadingBase}
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (!file) return;
                        setUploadingBase(true);
                        setError(null);
                        void uploadFile<FileMetadata>("/api/z-torque/files", file).then((meta) => {
                          setLocalBaseFile(meta);
                          setBaseText(null);
                        }).catch((err: unknown) => setError(err instanceof Error ? err.message : "上传失败"))
                          .finally(() => setUploadingBase(false));
                        event.target.value = "";
                      }}
                    />
                  </label>
                  {localBaseFile && (
                    <button type="button" className="file-compare-remove" title="删除并重新选择" onClick={removeLocalBaseFile}>
                      <DeleteOutlined />
                    </button>
                  )}
                </>
              )}
              <div className="file-compare-file-name" title={baseName}>{baseName}</div>
            </div>
          </section>

          <button
            type="button"
            className="file-compare-swap"
            title="转换：交换基准文件与对比文件"
            disabled={!baseFileMeta || !compareFile}
            onClick={swapFiles}
          >
            <SwapOutlined />
            <span>转换</span>
          </button>

          <section className="file-compare-panel">
            <header className="file-compare-panel-head">
              <span className={`file-compare-source ${compareOrigin}`}>{compareOrigin === "session" ? "会话文件" : "本地文件"}</span>
              <strong>对比文件</strong>
            </header>
            <div className="file-compare-panel-body">
              {compareOrigin === "session" && compareFile ? (
                <div className="file-compare-session-file">来自当前会话</div>
              ) : (
                <>
                  <label className="file-compare-upload-btn">
                    <UploadOutlined />
                    {uploading ? "上传中…" : compareFile ? "重新选择" : "选择文件"}
                    <input
                      ref={uploadRef}
                      type="file"
                      accept=".pdf,.docx,.txt,.md,.csv,.xlsx"
                      disabled={uploading}
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (!file) return;
                        setUploading(true);
                        setError(null);
                        void uploadFile<FileMetadata>("/api/z-torque/files", file).then((meta) => {
                          setFiles((prev) => [meta, ...prev.filter((item) => item.id !== meta.id)]);
                          setCompareFile(meta);
                          setCompareOrigin("local");
                          setCompareTextValue(null);
                        }).catch((err: unknown) => setError(err instanceof Error ? err.message : "上传失败"))
                          .finally(() => setUploading(false));
                        event.target.value = "";
                      }}
                    />
                  </label>
                  {compareFile && (
                    <button type="button" className="file-compare-remove" title="删除并重新选择" onClick={removeCompareFile}>
                      <DeleteOutlined />
                    </button>
                  )}
                </>
              )}
              <div className="file-compare-file-name" title={compareFile?.name}>{compareFile?.name ?? "未选择文件"}</div>
            </div>
          </section>
        </div>
        <div className="file-compare-actions">
          <Button
            type="primary"
            size="small"
            className="file-compare-run"
            icon={<SwapOutlined />}
            onClick={() => void runCompare()}
            disabled={!baseFileMeta || !compareFile || uploading || uploadingBase}
          >
            开始对比
          </Button>
        </div>
        {error && <div className="chat-split-pane-error">{error}</div>}
        {baseText !== null && compareTextValue !== null && (
          <div className="version-compare">
            <div className="version-compare-summary">本次新增 {added.length} 处 · 本次删减 {removed.length} 处 · 未变化 {unchanged.length} 处</div>
            <section className="version-compare-section">
              <h4 className="version-compare-added-title">本次新增</h4>
              {added.length === 0 ? <div className="version-compare-empty">无新增内容</div> : added.map((row, index) => <p key={`a-${index}`} className="version-compare-added">{row.text}</p>)}
            </section>
            <section className="version-compare-section">
              <h4 className="version-compare-removed-title">本次删减</h4>
              {removed.length === 0 ? <div className="version-compare-empty">无删减内容</div> : removed.map((row, index) => <p key={`r-${index}`} className="version-compare-removed">{row.text}</p>)}
            </section>
            <details className="version-compare-unchanged">
              <summary>未变化（{unchanged.length} 处）</summary>
              {unchanged.map((row, index) => <p key={`s-${index}`}>{row.text}</p>)}
            </details>
          </div>
        )}
          </>
        )}
      </Drawer>
    </>
  );
}

const GENERATION_PRESETS = [
  { value: "concise", label: "简洁", hint: "短问题，直接答" },
  { value: "balanced", label: "平衡", hint: "默认推荐" },
  { value: "deep", label: "深度", hint: "复杂问题，展开分析" },
  { value: "creative", label: "创意", hint: "文案、起名更发散" },
];

export function ComposerExtras({ preset, onPresetChange }: { preset: string; onPresetChange: (value: string) => void }) {
  const [showPresets, setShowPresets] = useState(false);
  const presetLabel = GENERATION_PRESETS.find((item) => item.value === preset)?.label ?? "平衡";

  return (
    <Popover
      open={showPresets}
      onOpenChange={setShowPresets}
      trigger="click"
      placement="topLeft"
      content={
        <div className="chat-preset-menu">
          {GENERATION_PRESETS.map((item) => (
            <button
              key={item.value}
              type="button"
              className={`chat-preset-item${item.value === preset ? " active" : ""}`}
              onClick={() => { onPresetChange(item.value); setShowPresets(false); }}
            >
              <span className="chat-preset-item-text">
                <strong>{item.label}</strong>
                <em>{item.hint}</em>
              </span>
              {item.value === preset && <span className="chat-preset-check">✓</span>}
            </button>
          ))}
        </div>
      }
    >
      <button type="button" className="composer-preset-trigger">
        <ThunderboltOutlined />
        <span className="composer-preset-name">回答方式</span>
        <strong>{presetLabel}</strong>
        <span className="composer-preset-caret" aria-hidden="true">⌄</span>
      </button>
    </Popover>
  );
}
