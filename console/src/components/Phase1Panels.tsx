import { useEffect, useMemo, useState } from "react";
import { Button, Drawer, Popover, Select } from "antd";
import { useNavigate } from "react-router-dom";
import { request, uploadFile } from "../services/api";
import type { FileContent, FileMetadata } from "../types";

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

export function FileCompareDrawer({ conversationId }: { conversationId: string | null }) {
  const [open, setOpen] = useState(false);
  const [files, setFiles] = useState<FileMetadata[]>([]);
  const [baseId, setBaseId] = useState<string | null>(null);
  const [compareFile, setCompareFile] = useState<FileMetadata | null>(null);
  const [uploading, setUploading] = useState(false);
  const [baseText, setBaseText] = useState<string | null>(null);
  const [compareTextValue, setCompareTextValue] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

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

  async function runCompare() {
    setError(null);
    setBaseText(null);
    setCompareTextValue(null);
    if (!baseId || !compareFile) { setError("请先选择会话文件并上传要对比的文件"); return; }
    try {
      const [a, b] = await Promise.all([readContent(baseId), readContent(compareFile.id)]);
      setBaseText(a);
      setCompareTextValue(b);
    } catch (err) {
      setError(err instanceof Error ? err.message : "文件内容读取失败");
    }
  }

  const rows = useMemo(
    () => (baseText !== null && compareTextValue !== null ? compareText(splitText(baseText), splitText(compareTextValue)) : []),
    [baseText, compareTextValue],
  );
  const added = rows.filter((row) => row.kind === "added");
  const removed = rows.filter((row) => row.kind === "removed");
  const unchanged = rows.filter((row) => row.kind === "same");

  return (
    <>
      <Button size="small" type="text" title="文件对比" onClick={() => setOpen(true)}>文件对比</Button>
      <Drawer title="文件对比" open={open} onClose={() => setOpen(false)} width="min(860px, 92vw)">
        <div className="file-compare-hint">
          当前会话产出的文件可直接选择；要对比的另一份文件需要你自己上传。系统还没有「直接生成文件」能力时，这里仍可用上传的文件做对比。
        </div>
        <div className="file-compare-pickers">
          <label>
            当前会话文件
            <Select size="small" value={baseId ?? undefined} options={files.map((file) => ({ label: file.name, value: file.id }))}
              placeholder={files.length === 0 ? "当前会话暂无文件" : "选择文件"}
              onChange={(value) => { setBaseId(value ?? null); setBaseText(null); }} style={{ width: "100%" }} />
          </label>
          <label>
            对比文件（上传）
            <input type="file" disabled={uploading} onChange={(event) => {
              const file = event.target.files?.[0];
              if (!file) return;
              setUploading(true);
              setError(null);
              void uploadFile<FileMetadata>("/api/z-torque/files", file).then((meta) => {
                setCompareFile(meta);
                setCompareTextValue(null);
              }).catch((err: unknown) => setError(err instanceof Error ? err.message : "上传失败"))
                .finally(() => setUploading(false));
              event.target.value = "";
            }} />
            {compareFile && <span className="file-compare-uploaded">{compareFile.name}</span>}
          </label>
          <Button type="primary" size="small" onClick={() => void runCompare()} disabled={!baseId || !compareFile}>开始对比</Button>
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
      </Drawer>
    </>
  );
}

const GENERATION_PRESETS = [
  { value: "concise", label: "简洁准确", hint: "回答简短直接，适合明确的小问题。" },
  { value: "balanced", label: "平衡", hint: "速度与细节兼顾，适合大多数问题。" },
  { value: "deep", label: "深度分析", hint: "回答更详细，适合复杂问题，等待时间更长。" },
  { value: "creative", label: "创意发散", hint: "表达更自由，适合起名、文案和头脑风暴。" },
];

export function ComposerExtras({ agents, input, onInputChange, preset, onPresetChange }: { agents: Array<{ id: string; name: string }>; input: string; onInputChange: (value: string) => void; preset: string; onPresetChange: (value: string) => void }) {
  const [showMentions, setShowMentions] = useState(false);
  return (
    <div className="phase1-composer-extras">
      <Select
        size="small"
        value={preset}
        options={GENERATION_PRESETS.map((item) => ({ label: item.label, value: item.value }))}
        onChange={onPresetChange}
        optionRender={(option) => {
          const item = GENERATION_PRESETS.find((p) => p.value === option.value);
          return <div className="phase1-preset-option"><strong>{option.label}</strong><span>{item?.hint}</span></div>;
        }}
        title="选择回答风格"
      />
      <Button size="small" type="text" onClick={() => setShowMentions((v) => !v)}>@ 提及</Button>
      {showMentions && (
        <div className="phase1-mention-list">
          <div className="phase1-mention-hint">点击后将直接调用该智能体处理本条需求（一次仅可指定一个）。</div>
          {agents.slice(0, 8).map((agent) => (
            <button
              key={agent.id}
              type="button"
              title={`将调用「${agent.name}」执行本条需求`}
              onClick={() => {
                const cleaned = input.replace(/@\[[^\]]*?\]/g, "").trim();
                onInputChange(`${cleaned} @[agent:${agent.id}] `);
                setShowMentions(false);
              }}
            >
              @{agent.name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
