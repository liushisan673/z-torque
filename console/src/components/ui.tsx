import type { ReactNode } from "react";

/** 将服务端状态字符串映射为标签样式，并返回可读中文。 */
export function statusMeta(status: string): { label: string; variant: string; running?: boolean } {
  const s = String(status || "").toUpperCase();
  switch (s) {
    case "ACTIVE":
    case "ENABLED":
    case "PUBLISHED":
    case "SUCCESS":
    case "COMPLETED":
    case "PARSED":
    case "UPLOADED":
    case "APPROVED":
      return { label: statusLabel(status), variant: "tag-success" };
    case "RUNNING":
    case "QUEUED":
    case "PENDING":
    case "PROCESSING":
      return { label: statusLabel(status), variant: "tag-running", running: true };
    case "WAITING_HUMAN":
    case "CANCELING":
    case "CANCEL_REQUESTED":
    case "DRAFT":
      return { label: statusLabel(status), variant: "tag-warning" };
    case "FAILED":
    case "TIMEOUT":
    case "CANCELLED":
    case "DISABLED":
    case "ARCHIVED":
    case "PARSE_FAILED":
    case "REJECTED":
      return { label: statusLabel(status), variant: "tag-failed" };
    default:
      return { label: status, variant: "tag-muted" };
  }
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    ACTIVE: "启用",
    ENABLED: "启用",
    DISABLED: "停用",
    PUBLISHED: "已发布",
    DRAFT: "草稿",
    ARCHIVED: "已归档",
    SUCCESS: "成功",
    COMPLETED: "完成",
    FAILED: "失败",
    TIMEOUT: "超时",
    CANCELLED: "已取消",
    CANCELING: "取消中",
    CANCEL_REQUESTED: "取消中",
    RUNNING: "运行中",
    QUEUED: "排队中",
    PENDING: "待处理",
    WAITING_HUMAN: "待确认",
    PROCESSING: "处理中",
    PARSED: "已解析",
    UPLOADED: "已上传",
    PARSE_FAILED: "解析失败",
    APPROVED: "已批准",
    REJECTED: "已拒绝",
  };
  return map[status] ?? status;
}

export function StatusTag({ status }: { status: string }) {
  const meta = statusMeta(status);
  return (
    <span className={`tag ${meta.variant}`}>
      {meta.running && <span className="pulse" />}
      {meta.label}
    </span>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="empty-state">
      <div style={{ fontWeight: 600, color: "#4a5262" }}>{title}</div>
      {hint && <div className="text-sm" style={{ marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

export function Alert({ kind = "info", children }: { kind?: "error" | "success" | "info"; children: ReactNode }) {
  return <div className={`alert alert-${kind}`}>{children}</div>;
}
