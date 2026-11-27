import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Empty, Spin, Tag } from "antd";
import { CheckOutlined, CloseOutlined, DownloadOutlined, ReloadOutlined, RollbackOutlined } from "@ant-design/icons";
import { downloadFile, request } from "../services/api";
import type { FileChangeSource, PatchReviewState, ReviewDetail, ReviewItem } from "../types";

type DiffKind = "same" | "add" | "del" | "hunk";

interface DiffRow {
  kind: DiffKind;
  oldNumber: number | null;
  newNumber: number | null;
  text: string;
}

interface SplitRow {
  key: string;
  left: DiffRow | null;
  right: DiffRow | null;
}

const STATE_META: Record<PatchReviewState, { label: string; color: string }> = {
  PENDING: { label: "待处理", color: "processing" },
  KEPT: { label: "已采纳", color: "success" },
  CANCELLED: { label: "已放弃", color: "default" },
  UNDONE: { label: "已撤销", color: "default" },
  EXPIRED: { label: "已过期", color: "warning" },
};

function splitLines(text: string): string[] {
  if (text === "") return [];
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  return lines;
}

function rawDiffRows(before: string, after: string): DiffRow[] {
  const oldLines = splitLines(before);
  const newLines = splitLines(after);
  const rows: DiffRow[] = [];
  if (oldLines.length * newLines.length > 1_000_000) {
    oldLines.forEach((text, index) => rows.push({ kind: "del", oldNumber: index + 1, newNumber: null, text }));
    newLines.forEach((text, index) => rows.push({ kind: "add", oldNumber: null, newNumber: index + 1, text }));
    return rows;
  }

  const table = Array.from({ length: oldLines.length + 1 }, () => new Uint32Array(newLines.length + 1));
  for (let i = oldLines.length - 1; i >= 0; i -= 1) {
    for (let j = newLines.length - 1; j >= 0; j -= 1) {
      table[i][j] = oldLines[i] === newLines[j]
        ? table[i + 1][j + 1] + 1
        : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }

  let i = 0;
  let j = 0;
  let oldNumber = 1;
  let newNumber = 1;
  while (i < oldLines.length && j < newLines.length) {
    if (oldLines[i] === newLines[j]) {
      rows.push({ kind: "same", oldNumber, newNumber, text: oldLines[i] });
      i += 1; j += 1; oldNumber += 1; newNumber += 1;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      rows.push({ kind: "del", oldNumber, newNumber: null, text: oldLines[i] });
      i += 1; oldNumber += 1;
    } else {
      rows.push({ kind: "add", oldNumber: null, newNumber, text: newLines[j] });
      j += 1; newNumber += 1;
    }
  }
  while (i < oldLines.length) {
    rows.push({ kind: "del", oldNumber, newNumber: null, text: oldLines[i] });
    i += 1; oldNumber += 1;
  }
  while (j < newLines.length) {
    rows.push({ kind: "add", oldNumber: null, newNumber, text: newLines[j] });
    j += 1; newNumber += 1;
  }
  return rows;
}

function buildDiffRows(before: string, after: string): DiffRow[] {
  const raw = rawDiffRows(before, after);
  const changed = raw
    .map((row, index) => (row.kind === "add" || row.kind === "del" ? index : -1))
    .filter((index) => index >= 0);
  if (changed.length === 0) return raw.slice(0, 3);

  const ranges: Array<[number, number]> = [];
  for (const index of changed) {
    const start = Math.max(0, index - 3);
    const end = Math.min(raw.length, index + 4);
    const last = ranges[ranges.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else ranges.push([start, end]);
  }

  const rows: DiffRow[] = [];
  let cursor = 0;
  for (const [start, end] of ranges) {
    if (start > cursor) {
      rows.push({ kind: "hunk", oldNumber: null, newNumber: null, text: `… 省略 ${start - cursor} 行未变化内容 …` });
    }
    const slice = raw.slice(start, end);
    const oldNumbers = slice.map((row) => row.oldNumber).filter((value): value is number => value !== null);
    const newNumbers = slice.map((row) => row.newNumber).filter((value): value is number => value !== null);
    rows.push({
      kind: "hunk",
      oldNumber: null,
      newNumber: null,
      text: `第 ${ranges.length} 处变更 · 原文 ${oldNumbers.length} 行 / 新版 ${newNumbers.length} 行`,
    });
    rows.push(...slice);
    cursor = end;
  }
  if (cursor < raw.length) {
    rows.push({ kind: "hunk", oldNumber: null, newNumber: null, text: `… 省略 ${raw.length - cursor} 行未变化内容 …` });
  }
  return rows;
}

function buildSplitRows(rows: DiffRow[]): SplitRow[] {
  const result: SplitRow[] = [];
  let removed: DiffRow[] = [];
  let added: DiffRow[] = [];
  const flush = () => {
    const size = Math.max(removed.length, added.length);
    for (let index = 0; index < size; index += 1) {
      result.push({ key: `split-${result.length}`, left: removed[index] ?? null, right: added[index] ?? null });
    }
    removed = [];
    added = [];
  };
  for (const row of rows) {
    if (row.kind === "del") { removed.push(row); continue; }
    if (row.kind === "add") { added.push(row); continue; }
    flush();
    result.push({ key: `split-${result.length}`, left: row, right: row.kind === "hunk" ? null : row });
  }
  flush();
  return result;
}

function fileName(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/");
  return parts[parts.length - 1] || path;
}

function formatTime(value: string | null): string {
  if (!value) return "长期有效";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function itemName(item: ReviewItem): string {
  return "name" in item ? item.name : fileName(item.path);
}

function sourceLabel(source: FileChangeSource): string {
  return source === "file" ? "文件版本" : "文本补丁";
}

function DiffLine({ row, index, showNumbers }: { row: DiffRow; index: number; showNumbers: boolean }) {
  if (row.kind === "hunk") {
    return <div key={`line-${index}`} className="patch-diff-line patch-diff-hunk">{row.text}</div>;
  }
  const sign = row.kind === "add" ? "+" : row.kind === "del" ? "-" : " ";
  return (
    <div key={`line-${index}`} className={`patch-diff-line patch-diff-${row.kind}`}>
      {showNumbers && <span className="patch-diff-num">{row.oldNumber ?? ""}</span>}
      {showNumbers && <span className="patch-diff-num">{row.newNumber ?? ""}</span>}
      <span className="patch-diff-sign">{sign}</span>
      <span className="patch-diff-text">{row.text || " "}</span>
    </div>
  );
}

function SplitCell({ row, showNumbers }: { row: DiffRow | null; showNumbers: boolean }) {
  if (!row) return <div className="patch-diff-split-cell empty" />;
  if (row.kind === "hunk") return <div className="patch-diff-split-cell patch-diff-hunk">{row.text}</div>;
  return (
    <div className={`patch-diff-split-cell patch-diff-${row.kind}`}>
      {showNumbers && <span className="patch-diff-num">{row.oldNumber ?? row.newNumber ?? ""}</span>}
      <span className="patch-diff-text">{row.text || " "}</span>
    </div>
  );
}

export function PatchReviewPanel({
  conversationId,
  active,
  focusChangeId,
  onChanged,
}: {
  conversationId: string | null;
  active: boolean;
  /** 从会话气泡跳转过来时要定位的提案 id */
  focusChangeId?: string | null;
  /** 采纳/放弃/回退成功后通知外层刷新消息（否则气泡停留在旧状态） */
  onChanged?: () => void;
}) {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"unified" | "split">("unified");
  const [showLineNumbers, setShowLineNumbers] = useState(false);

  const refreshDetail = useCallback(async (id: string, source: FileChangeSource) => {
    const base = source === "file" ? "file-changes" : "patch-artifacts";
    const next = await request<ReviewDetail>(`/api/z-torque/workspace/${base}/${id}`);
    setDetail(next);
  }, []);

  const loadList = useCallback(async () => {
    if (!conversationId) return;
    setLoading(true);
    setError(null);
    try {
      const [patches, files] = await Promise.all([
        request<ReviewItem[]>("/api/z-torque/workspace/patch-artifacts", { query: { conversation_id: conversationId } }),
        request<ReviewItem[]>("/api/z-torque/workspace/file-changes", { query: { conversation_id: conversationId } }),
      ]);
      const normalized = [
        ...(Array.isArray(patches) ? patches : []),
        ...(Array.isArray(files) ? files : []),
      ].sort((a, b) => {
        const left = new Date(a.created_at).getTime();
        const right = new Date(b.created_at).getTime();
        return (Number.isNaN(right) ? 0 : right) - (Number.isNaN(left) ? 0 : left);
      });
      setItems(normalized);
      setSelectedId((previous) => (
        focusChangeId && normalized.some((item) => item.id === focusChangeId)
          ? focusChangeId
          : previous && normalized.some((item) => item.id === previous)
            ? previous
            : normalized[0]?.id ?? null
      ));
    } catch (err) {
      setError(err instanceof Error ? err.message : "文件变更加载失败");
      setItems([]);
      setSelectedId(null);
    } finally {
      setLoading(false);
    }
  }, [conversationId, focusChangeId]);

  useEffect(() => {
    if (!active || !focusChangeId) return;
    setSelectedId(focusChangeId);
  }, [active, focusChangeId]);

  useEffect(() => {
    if (!active || !conversationId) {
      setItems([]);
      setSelectedId(null);
      setDetail(null);
      return;
    }
    void loadList();
  }, [active, conversationId, loadList]);

  useEffect(() => {
    if (!active || !selectedId) {
      setDetail(null);
      return;
    }
    const selected = items.find((item) => item.id === selectedId);
    if (!selected) {
      setDetail(null);
      return;
    }
    let alive = true;
    setDetailLoading(true);
    void refreshDetail(selectedId, selected.source ?? "patch")
      .catch((err: unknown) => { if (alive) setError(err instanceof Error ? err.message : "变更详情加载失败"); })
      .finally(() => { if (alive) setDetailLoading(false); });
    return () => { alive = false; };
  }, [active, selectedId, items, refreshDetail]);

  const diffRows = useMemo(
    () => (detail ? buildDiffRows(detail.before_text, detail.after_text) : []),
    [detail],
  );
  const splitRows = useMemo(() => buildSplitRows(diffRows), [diffRows]);

  async function downloadSelected() {
    if (!detail || busy) return;
    const url = detail.download_url
      ?? (detail.file_id ? `/api/z-torque/files/${detail.file_id}/raw` : null);
    if (!url) return;
    setError(null);
    try {
      await downloadFile(url, itemName(detail));
    } catch (err) {
      setError(err instanceof Error ? err.message : "下载失败");
    }
  }

  async function runAction(kind: "keep" | "cancel" | "undo" | "rollback") {
    if (!selectedId || busy) return;
    const selected = items.find((item) => item.id === selectedId);
    if (!selected) return;
    const source = selected.source ?? "patch";
    if (source === "patch" && (kind === "cancel" || kind === "rollback")) return;
    const base = source === "file" ? "file-changes" : "patch-artifacts";
    const endpoint = kind === "rollback" ? "undo" : kind;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/workspace/${base}/${selectedId}/${endpoint}`, { method: "POST" });
      await loadList();
      await refreshDetail(selectedId, source);
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  if (!conversationId) return <div className="patch-review-empty">请先选择一个会话</div>;

  const selected = items.find((item) => item.id === selectedId) ?? null;
  const state = selected?.review_state ?? detail?.review_state;
  const stateMeta = state ? STATE_META[state] : null;
  const isFileChange = (selected?.source ?? detail?.source) === "file";
  const canKeep = Boolean(detail && (isFileChange ? (detail.can_keep ?? detail.review_state === "PENDING") : detail.review_state === "PENDING"));
  const canCancel = Boolean(isFileChange && detail && (detail.can_cancel ?? detail.review_state === "PENDING"));
  const canRollback = Boolean(isFileChange && detail?.can_rollback);
  const canPatchUndo = Boolean(!isFileChange && detail?.can_undo);

  return (
    <div className="patch-review">
      {error && <div className="patch-review-error">{error}</div>}
      <div className="patch-review-layout">
        <aside className="patch-review-list">
          <div className="patch-review-list-head">
            <span>变更文件</span>
            <button type="button" className="patch-review-refresh" title="刷新" onClick={() => void loadList()} disabled={loading}>
              <ReloadOutlined />
            </button>
          </div>
          {loading && items.length === 0 ? (
            <div className="patch-review-list-loading"><Spin size="small" /></div>
          ) : items.length === 0 ? (
            <div className="patch-review-list-empty">本会话暂无文件变更</div>
          ) : (
            items.map((item) => {
              const meta = STATE_META[item.review_state];
              return (
                <button
                  type="button"
                  key={item.id}
                  className={`patch-review-item ${item.id === selectedId ? "active" : ""}`}
                  onClick={() => setSelectedId(item.id)}
                >
                  <span className="patch-review-item-name" title={item.path}>{itemName(item)}</span>
                  <span className="patch-review-item-meta">
                    <Tag color={meta.color}>{meta.label}</Tag>
                    <span className="patch-review-source">{sourceLabel(item.source ?? "patch")}</span>
                    <span className="patch-review-add">+{item.additions}</span>
                    <span className="patch-review-del">-{item.deletions}</span>
                  </span>
                  <span className="patch-review-item-time">{formatTime(item.created_at)}</span>
                </button>
              );
            })
          )}
        </aside>

        <section className="patch-review-main">
          {!selectedId ? (
            <Empty description="选择一个文件查看变更" />
          ) : detailLoading && !detail ? (
            <div className="patch-review-main-loading"><Spin /></div>
          ) : detail ? (
            <>
              <div className="patch-review-toolbar">
                <div className="patch-review-path" title={detail.path}>{itemName(detail)}</div>
                <div className="patch-review-toolbar-right">
                  {stateMeta && <Tag color={stateMeta.color}>{stateMeta.label}</Tag>}
                  {isFileChange && detail.kind && (
                    <Tag>{detail.kind === "ROLLBACK" ? "回退" : detail.kind === "CREATE" ? "新建" : "修改"}</Tag>
                  )}
                  <span className="patch-review-hash">{detail.content_hash.slice(0, 8)}</span>
                  <div className="patch-review-view-switch">
                    <button type="button" className={viewMode === "unified" ? "active" : ""} onClick={() => setViewMode("unified")}>统一视图</button>
                    <button type="button" className={viewMode === "split" ? "active" : ""} onClick={() => setViewMode("split")}>并排视图</button>
                    <button type="button" className={showLineNumbers ? "active" : ""} title="显示行号" onClick={() => setShowLineNumbers((value) => !value)}>行号</button>
                  </div>
                  <Button
                    size="small"
                    icon={<DownloadOutlined />}
                    disabled={busy || !(detail.download_url || detail.file_id)}
                    title="待评审下载草稿，已采纳下载正式文件"
                    onClick={() => void downloadSelected()}
                  >
                    下载
                  </Button>
                  <Button size="small" icon={<CheckOutlined />} disabled={busy || !canKeep} onClick={() => void runAction("keep")}>采纳</Button>
                  {isFileChange && (
                    <Button size="small" icon={<CloseOutlined />} disabled={busy || !canCancel} onClick={() => void runAction("cancel")}>放弃</Button>
                  )}
                  <Button
                    size="small"
                    danger
                    icon={<RollbackOutlined />}
                    disabled={busy || (isFileChange ? !canRollback : !canPatchUndo)}
                    onClick={() => void runAction(isFileChange ? "rollback" : "undo")}
                  >
                    {isFileChange ? "回退" : "撤销"}
                  </Button>
                </div>
              </div>
              {(detail.purpose || detail.risk) && (
                <div className="patch-review-summary">
                  {detail.purpose && <span>目的：{detail.purpose}</span>}
                  {detail.risk && <span>风险：{detail.risk}</span>}
                </div>
              )}
              <div className="patch-diff">
                {viewMode === "unified" ? (
                  <div className="patch-diff-code">
                    {diffRows.map((row, index) => (
                      <DiffLine key={`line-${index}`} row={row} index={index} showNumbers={showLineNumbers} />
                    ))}
                  </div>
                ) : (
                  <div className="patch-diff-split">
                    {splitRows.map((row) => (
                      <div key={row.key} className="patch-diff-split-row">
                        <SplitCell row={row.left} showNumbers={showLineNumbers} />
                        <SplitCell row={row.right} showNumbers={showLineNumbers} />
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </>
          ) : null}
        </section>
      </div>
    </div>
  );
}
