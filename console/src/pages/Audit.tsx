import { Fragment, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { request } from "../services/api";
import { errorText } from "../services/errorText";
import type { AuditEvent, AuditListResponse } from "../types";
import { Loading } from "../components/Loading";
import { useAuth } from "../stores/auth";

const API = "/api/z-torque/audit";
const PAGE_SIZE = 20;

// 类型筛选：映射到项目实际写入审计的 action 前缀（真实事件族）
const TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "全部类型" },
  { value: "AGENT_", label: "智能体" },
  { value: "SKILL_", label: "技能" },
  { value: "MODEL_", label: "模型" },
  { value: "CONNECTOR_", label: "连接器" },
  { value: "FILE_", label: "文件" },
  { value: "CONFIRMATION_", label: "确认" },
];

type RangeKey = "24h" | "7d" | "30d" | "all" | "custom";

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return "—";
  }
}

// 展示用映射（仅渲染层，不改存储值）：资源类型 → 中文
const RESOURCE_TYPE_LABEL: Record<string, string> = {
  AGENT: "智能体",
  SKILL: "技能",
  TOOL: "工具",
  MODEL: "模型",
  CONNECTOR: "连接器",
  FILE: "文件",
  TASK: "任务",
  USER: "用户",
  CONFIRMATION: "确认",
};

// 展示用映射：审计动作（后缀动词）→ 中文
const ACTION_VERB_LABEL: Record<string, string> = {
  CREATED: "创建",
  UPDATED: "更新",
  DELETED: "删除",
  LOGIN: "登录",
  LOGOUT: "登出",
  TESTED: "测试",
  EXECUTED: "执行",
  PUBLISHED: "发布",
  ARCHIVED: "归档",
};

function resourceTypeLabel(t: string): string {
  return RESOURCE_TYPE_LABEL[t] ?? t;
}

function actionLabel(action: string): string {
  const i = action.lastIndexOf("_");
  const verb = i > 0 ? ACTION_VERB_LABEL[action.slice(i + 1)] : undefined;
  return verb ?? ACTION_VERB_LABEL[action] ?? action;
}

// action → 类型标签（展示用，前缀映射）
function tagOf(e: AuditEvent): { label: string; cls: string } {
  const a = e.action;
  if (a === "MODEL_CALL_COMPLETED") return { label: "模型调用", cls: "audit-tag-task" };
  if (a.startsWith("AGENT_")) return { label: "智能体", cls: "audit-tag-agent" };
  if (a.startsWith("SKILL_") || a.startsWith("TOOL_") || a.startsWith("MD_SKILL_")) return { label: "技能", cls: "audit-tag-skill" };
  if (a.startsWith("MODEL_")) return { label: "模型", cls: "audit-tag-model" };
  if (a.startsWith("CONNECTOR_")) return { label: "连接器", cls: "audit-tag-conn" };
  if (a.startsWith("FILE_")) return { label: "文件", cls: "audit-tag-file" };
  if (a.startsWith("CONFIRMATION_")) return { label: "确认", cls: "audit-tag-confirm" };
  return { label: "其他", cls: "audit-tag-other" };
}

function resultOf(e: AuditEvent): { label: string; cls: string } {
  if (e.result === "success") return { label: "成功", cls: "audit-result-ok" };
  if (e.result === "failed" || e.result === "denied") return { label: e.result === "denied" ? "拒绝" : "失败", cls: "audit-result-err" };
  if (e.result === "pending") return { label: "进行中", cls: "audit-result-warn" };
  return { label: "—", cls: "audit-result-none" };
}

function isoDaysAgo(days: number): string {
  return new Date(Date.now() - days * 86400_000).toISOString();
}

// 列表用相对时间：短而省宽，完整时间放 title 悬停可见
function fmtListTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diff = Date.now() - t;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function traceShort(tid: string | null | undefined): string {
  return tid ? `${tid.slice(0, 8)}…` : "—";
}

// 常见错误码 → 中文（详情"失败原因"与参数值共用）
const ERROR_CODE_LABEL: Record<string, string> = {
  AI_TASK_FAILED: "任务执行失败",
  AI_MODEL_CALL_FAILED: "模型调用失败",
  AI_MODEL_UNAVAILABLE: "模型不可用",
  AI_CAPABILITY_UNAVAILABLE: "所需能力不可用",
  AI_CONNECTOR_SECRET_INVALID: "连接器凭证无效或已失效",
  AI_CONNECTOR_UNREACHABLE: "连接器无法连通",
  AI_MCP_HANDSHAKE_FAILED: "MCP 握手失败",
  AI_AGENT_NOT_PUBLISHED: "智能体版本未发布",
  AI_SKILL_NOT_PUBLISHED: "技能版本未发布",
  PERMISSION_DENIED: "无权限执行该操作",
  VALIDATION_ERROR: "参数校验未通过",
  NOT_FOUND: "目标资源不存在",
  CONFLICT: "资源状态冲突",
};

// 事件参数键 → 中文标签（展示层映射，不改存储值）
const PARAM_KEY_LABEL: Record<string, string> = {
  error_code: "错误码",
  error_message: "错误信息",
  message: "消息",
  detail_message: "详情",
  tool_name: "工具名",
  tool: "工具",
  args: "参数",
  arguments: "参数",
  skill_name: "技能名",
  agent_name: "智能体名",
  connector_name: "连接名",
  file_name: "文件名",
  model: "模型",
  model_id: "模型",
  provider: "供应商",
  endpoint: "接入点",
  status: "状态",
  reason: "原因",
  duration_ms: "耗时",
  host: "主机",
  ip: "IP 地址",
  user_agent: "客户端标识",
  secret_ref: "密钥引用",
  count: "数量",
  title: "标题",
};

// 常见状态类枚举值 → 中文
const PARAM_VALUE_LABEL: Record<string, string> = {
  ready: "就绪", ok: "正常", failed: "失败", success: "成功", pending: "进行中",
  enabled: "已启用", disabled: "已停用", draft: "草稿", published: "已发布",
  archived: "已归档", processing: "处理中", completed: "已完成",
};

function paramValueText(key: string, v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "boolean") return v ? "是" : "否";
  if (typeof v !== "string") return String(v);
  if (key === "error_code") return ERROR_CODE_LABEL[v] ?? v;
  if (key === "duration_ms") return `${v} ms`;
  if (key === "tool_name" || key === "tool") return v.replace(/^(crm_|zr_|mcp_)/, "");
  return PARAM_VALUE_LABEL[v] ?? v;
}

// 从 detail 中提取失败原因：优先 error_message / message / reason
function failureMessage(e: AuditEvent): { text: string; code: string | null } | null {
  const d = e.detail;
  if (!d) return null;
  const msg = [d.error_message, d.message, d.reason, d.error].find(
    (v) => typeof v === "string" && v.trim(),
  ) as string | undefined;
  const code = typeof d.error_code === "string" ? d.error_code : null;
  if (!msg && !code) return null;
  const codeText = code ? (ERROR_CODE_LABEL[code] ?? code) : null;
  return { text: msg ?? codeText ?? "未知原因", code };
}

export function AuditPage() {
  const { user } = useAuth();
  // 权限范围：audit.read_all（admin）看全量；普通用户仅自己相关（后端同步强制收缩）
  const canReadAll = user?.permissions?.includes("audit.read_all") ?? false;

  const [data, setData] = useState<AuditListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 筛选状态（查询按钮触发刷新）
  const [range, setRange] = useState<RangeKey>("7d");
  const [startCustom, setStartCustom] = useState("");
  const [endCustom, setEndCustom] = useState("");
  const [typePrefix, setTypePrefix] = useState("");
  const [username, setUsername] = useState("");
  const [agentId, setAgentId] = useState("");
  // 智能体筛选下拉数据源（替代手输 UUID）
  const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
  const [traceInput, setTraceInput] = useState("");
  const [page, setPage] = useState(0);

  // 详情抽屉
  const [detail, setDetail] = useState<AuditEvent | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [traceChain, setTraceChain] = useState<AuditEvent[] | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);

  const buildQuery = useCallback((offset: number) => {
    const qs = new URLSearchParams();
    qs.set("offset", String(offset));
    qs.set("limit", String(PAGE_SIZE));
    if (range === "custom") {
      if (startCustom) qs.set("start_time", new Date(startCustom).toISOString());
      if (endCustom) qs.set("end_time", new Date(endCustom).toISOString());
    } else if (range === "24h") {
      qs.set("start_time", isoDaysAgo(1));
    } else if (range === "7d") {
      qs.set("start_time", isoDaysAgo(7));
    } else if (range === "30d") {
      qs.set("start_time", isoDaysAgo(30));
    }
    if (typePrefix) qs.set("action_prefix", typePrefix);
    if (username.trim()) qs.set("actor_username", username.trim());
    if (agentId.trim()) {
      qs.set("resource_type", "AGENT");
      qs.set("resource_id", agentId.trim());
    }
    if (traceInput.trim()) qs.set("trace_id", traceInput.trim());
    return qs.toString();
  }, [range, startCustom, endCustom, typePrefix, username, agentId, traceInput]);

  const load = useCallback(async (offset: number) => {
    setLoading(true);
    setError(null);
    try {
      const resp = await request<AuditListResponse>(`${API}?${buildQuery(offset)}`);
      setData(resp);
    } catch (err) {
      setError(errorText(err, "加载失败"));
    } finally {
      setLoading(false);
    }
  }, [buildQuery]);

  // 筛选变更即时生效：250ms 防抖；注意同步重置页码，避免"数据回第 1 页、页码显示第 N 页"的状态脱节
  useEffect(() => {
    const t = window.setTimeout(() => { setPage(0); void load(0); }, 250);
    return () => window.clearTimeout(t);
  }, [buildQuery, load]);

  // 智能体下拉选项：仅首次加载
  useEffect(() => {
    request<{ id: string; name: string }[]>("/api/z-torque/agents")
      .then(setAgents)
      .catch(() => setAgents([]));
  }, []);

  // 详情抽屉支持 Esc 关闭，与 Modal 行为一致
  useEffect(() => {
    if (!detail) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") {
        setDetail(null);
        setTraceChain(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detail]);

  function resetFilters() {
    setRange("7d"); setStartCustom(""); setEndCustom(""); setTypePrefix("");
    setUsername(""); setAgentId(""); setTraceInput(""); setPage(0);
    void load(0);
  }

  // 智能体资源 ID → 名称（复用筛选下拉已加载的缓存；无法解析时返回 null 不显示该行）
  function agentNameOf(id: string | null | undefined): string | null {
    if (!id) return null;
    return agents.find((a) => a.id === id)?.name ?? null;
  }

  // keepTrace：从 Trace 链路点击子事件时保留链路视图，仅替换详情内容
  async function openDetail(e: AuditEvent, keepTrace = false) {
    setDetail(e);
    if (!keepTrace) setTraceChain(null);
    setDetailLoading(true);
    try {
      const d = await request<AuditEvent>(`${API}/${e.id}`);
      setDetail(d);
    } catch (err) {
      setError(errorText(err, "加载详情失败"));
    } finally {
      setDetailLoading(false);
    }
  }

  async function loadTraceChain(traceId: string) {
    setTraceLoading(true);
    setError(null);
    try {
      const resp = await request<{ items: AuditEvent[]; trace_id: string }>(`${API}/by-trace/${encodeURIComponent(traceId)}`);
      setTraceChain(resp.items);
    } catch (err) {
      setError(errorText(err, "加载链路失败"));
    } finally {
      setTraceLoading(false);
    }
  }

  const total = data?.total ?? 0;
  const offset = data?.offset ?? 0;
  const scope = data?.scope ?? (canReadAll ? "all" : "self");

  return (
    <div className="audit-page">
      {/* 范围提示 */}
      <div className={`audit-scope-banner ${scope === "all" ? "audit-scope-all" : "audit-scope-self"}`}>
        <span>{scope === "all" ? "✓" : "ℹ"}</span>
        <span>
          {scope === "all" ? "管理员 · 可查看全量审计" : "普通用户 · 仅显示与你相关的记录"}
          {" · "}审计记录只读，不可删除或关闭。
        </span>
      </div>

      {/* 筛选栏 */}
      <div className="audit-filter-bar">
        <div className="audit-filter-row">
          <div className="audit-field">
            <label>时间范围</label>
            <select value={range} onChange={(e) => setRange(e.target.value as RangeKey)}>
              <option value="24h">近 24 小时</option>
              <option value="7d">近 7 天</option>
              <option value="30d">近 30 天</option>
              <option value="all">全部时间</option>
              <option value="custom">自定义</option>
            </select>
          </div>
          {range === "custom" && (
            <>
              <div className="audit-field">
                <label>开始时间</label>
                <input type="datetime-local" value={startCustom} onChange={(e) => setStartCustom(e.target.value)} />
              </div>
              <div className="audit-field">
                <label>结束时间</label>
                <input type="datetime-local" value={endCustom} onChange={(e) => setEndCustom(e.target.value)} />
              </div>
            </>
          )}
          <div className="audit-field">
            <label>事件类型</label>
            <select value={typePrefix} onChange={(e) => setTypePrefix(e.target.value)}>
              {TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
          <div className="audit-field">
            <label>执行用户{scope === "self" ? "（仅自己）" : ""}</label>
            <input
              type="text"
              placeholder="用户名"
              value={username}
              disabled={scope === "self"}
              onChange={(e) => setUsername(e.target.value)}
              style={{ width: 130 }}
            />
          </div>
          <div className="audit-field">
            <label>智能体</label>
            <select value={agentId} onChange={(e) => setAgentId(e.target.value)} style={{ width: 180 }}>
              <option value="">全部智能体</option>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>{a.name}</option>
              ))}
            </select>
          </div>
          <div className="audit-field">
            <label>Trace ID</label>
            <input type="text" placeholder="精确关联" value={traceInput} onChange={(e) => setTraceInput(e.target.value)} style={{ width: 220 }} />
          </div>
          <div className="audit-filter-actions">
            <button className="btn btn-primary" disabled={loading} onClick={() => { setPage(0); void load(0); }}>查询</button>
            <button
              className="btn btn-secondary"
              disabled={loading}
              onClick={resetFilters}
            >
              重置
            </button>
          </div>
        </div>
      </div>

      {error && (
        <div className="skill-alert skill-alert-error" style={{ marginBottom: 12 }}>
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      <div className="audit-shell">
        {/* 事件表格 + 分页 */}
        <div className="audit-list-pane">
          <div className="audit-table-wrap">
            {loading ? (
              <Loading />
            ) : !data || data.items.length === 0 ? (
              <div className="audit-empty">
                <span>暂无符合条件的审计事件</span>
                <button className="btn btn-secondary btn-sm" disabled={loading} onClick={resetFilters}>重置筛选</button>
              </div>
            ) : (
              <table className="audit-table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>类型</th>
                    <th>动作</th>
                    <th>执行用户</th>
                    <th>资源 / 摘要</th>
                    <th>结果</th>
                    {scope === "all" && (
                      <>
                        <th>耗时</th>
                        <th>Trace ID</th>
                      </>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((e) => {
                    const tg = tagOf(e);
                    const rs = resultOf(e);
                    return (
                      <tr
                        key={e.id}
                        className={detail?.id === e.id ? "active" : ""}
                        onClick={() => void openDetail(e)}
                      >
                        <td className="time" title={fmtTime(e.created_at)}>{fmtListTime(e.created_at)}</td>
                        <td><span className={`audit-tag ${tg.cls}`}>{tg.label}</span></td>
                        <td>{actionLabel(e.action)}</td>
                        <td>{e.actor_username ?? "系统"}</td>
                        <td className="summary-cell" title={e.summary}>{resourceTypeLabel(e.resource_type)} · {e.summary}</td>
                        <td><span className={`audit-result ${rs.cls}`}>{rs.label}</span></td>
                        {scope === "all" && (
                          <>
                            <td>{e.duration_ms != null ? `${e.duration_ms} ms` : "—"}</td>
                            <td
                              className="trace-cell"
                              title={e.trace_id ? `${e.trace_id}（点击复制）` : ""}
                              onClick={(ev) => {
                                if (!e.trace_id) return;
                                ev.stopPropagation();
                                void navigator.clipboard.writeText(e.trace_id);
                              }}
                            >
                              {traceShort(e.trace_id)}
                            </td>
                          </>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
          <div className="audit-pager">
            <span>共 {total} 条</span>
            <div className="spacer" />
            <button className="btn btn-secondary btn-sm" disabled={loading || offset <= 0} onClick={() => { const p = Math.max(0, page - 1); setPage(p); void load(p * PAGE_SIZE); }}>上一页</button>
            <span>第 {Math.floor(offset / PAGE_SIZE) + 1} 页</span>
            <button className="btn btn-secondary btn-sm" disabled={loading || offset + PAGE_SIZE >= total} onClick={() => { const p = page + 1; setPage(p); void load(p * PAGE_SIZE); }}>下一页</button>
          </div>
        </div>

        {/* 详情抽屉 */}
        {detail && (
          <aside className="audit-detail-pane">
            <div className="audit-detail-header">
              <h2>事件详情</h2>
              <button className="btn btn-secondary btn-sm" onClick={() => { setDetail(null); setTraceChain(null); }}>关闭</button>
            </div>
            {detailLoading ? (
              <Loading />
            ) : (
              <>
                <div className="audit-detail-section">
                  <h3>基本信息</h3>
                  <dl className="audit-kv">
                    <dt>时间</dt><dd>{fmtTime(detail.created_at)}</dd>
                    <dt>类型</dt><dd><span className={`audit-tag ${tagOf(detail).cls}`}>{tagOf(detail).label}</span> <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-muted)" }}>{actionLabel(detail.action)}</span></dd>
                    <dt>动作</dt>
                    <dd>
                      <span className="mono" style={{ fontSize: 12 }}>{detail.action}</span>
                      <button className="btn btn-secondary btn-sm" style={{ marginLeft: 6 }} onClick={() => { void navigator.clipboard.writeText(detail.action); }}>复制</button>
                    </dd>
                    <dt>结果</dt><dd><span className={`audit-result ${resultOf(detail).cls}`}>{resultOf(detail).label}</span></dd>
                    <dt>耗时</dt><dd>{detail.duration_ms != null ? `${detail.duration_ms} ms` : "—"}</dd>
                    <dt>执行用户</dt><dd>{detail.actor_username ?? "系统"}</dd>
                    <dt>Trace ID</dt>
                    <dd>
                      <span className="mono">{detail.trace_id ?? "—"}</span>
                      {detail.trace_id && (
                        <button
                          className="btn btn-secondary btn-sm"
                          style={{ marginLeft: 6 }}
                          onClick={() => { void navigator.clipboard.writeText(detail.trace_id!); }}
                        >
                          复制
                        </button>
                      )}
                    </dd>
                  </dl>
                </div>

                {/* 失败/拒绝事件：把失败原因从参数里提出来置顶展示，不再让用户在 JSON 里找 */}
                {resultOf(detail).cls === "audit-result-err" && failureMessage(detail) && (
                  <div className="audit-detail-section">
                    <h3>失败原因</h3>
                    <div className="skill-alert skill-alert-error">
                      <span>⚠</span>
                      <span>
                        {failureMessage(detail)!.text}
                        {failureMessage(detail)!.code && (
                          <span style={{ display: "block", fontSize: 11, opacity: 0.75, marginTop: 2 }}>
                            错误码：{failureMessage(detail)!.code}
                          </span>
                        )}
                      </span>
                    </div>
                  </div>
                )}

                <div className="audit-detail-section">
                  <h3>关联资源</h3>
                  <dl className="audit-kv">
                    <dt>资源类型</dt><dd>{resourceTypeLabel(detail.resource_type)}</dd>
                    {detail.resource_type === "AGENT" && agentNameOf(detail.resource_id) && (
                      <>
                        <dt>资源名称</dt>
                        <dd>{agentNameOf(detail.resource_id)}</dd>
                      </>
                    )}
                    <dt>资源 ID</dt>
                    <dd className="mono" style={{ fontSize: 12, wordBreak: "break-all" }}>
                      {detail.resource_id ?? "—"}
                      {detail.resource_id && (
                        <button className="btn btn-secondary btn-sm" style={{ marginLeft: 6 }} onClick={() => { void navigator.clipboard.writeText(detail.resource_id!); }}>复制</button>
                      )}
                    </dd>
                  </dl>
                  <div style={{ marginTop: 8, display: "flex", gap: 8, flexWrap: "wrap" }}>
                    {detail.resource_type === "AGENT" && detail.resource_id && (
                      <Link className="btn btn-secondary btn-sm" to="/agents">查看智能体</Link>
                    )}
                    {(detail.resource_type === "SKILL" || detail.resource_type === "TOOL") && detail.resource_id && (
                      <Link className="btn btn-secondary btn-sm" to="/skills">查看技能</Link>
                    )}
                    {detail.resource_type === "CONNECTOR" && detail.resource_id && (
                      <Link className="btn btn-secondary btn-sm" to="/connectors">查看连接器</Link>
                    )}
                    {detail.resource_type === "TASK" && detail.resource_id && (
                      <Link className="btn btn-secondary btn-sm" to="/chat">回对话页查看执行</Link>
                    )}
                    {detail.trace_id && (
                      <button className="btn btn-secondary btn-sm" disabled={traceLoading} onClick={() => void loadTraceChain(detail.trace_id!)}>
                        {traceLoading ? "加载中…" : "查看同 Trace 链路"}
                      </button>
                    )}
                  </div>
                </div>

                <div className="audit-detail-section">
                  <h3>摘要（已脱敏）</h3>
                  <p style={{ fontSize: 12, color: "var(--text)", margin: "0 0 8px" }}>{detail.summary}</p>
                  {detail.detail && Object.keys(detail.detail).length > 0 ? (
                    (() => {
                      // 参数键值对标签化展示：平铺基础类型字段（中文键名 + 值翻译）；
                      // 嵌套对象/数组不属于"一眼可读"范畴，折叠进完整 JSON 按需展开
                      const entries = Object.entries(detail.detail);
                      const primitiveEntries = entries.filter(([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v));
                      const complexEntries = entries.filter(([, v]) => v !== null && !["string", "number", "boolean"].includes(typeof v));
                      return (
                        <>
                          {primitiveEntries.length > 0 && (
                            <dl className="audit-kv">
                              {primitiveEntries.map(([k, v]) => (
                                <Fragment key={k}>
                                  <dt>{PARAM_KEY_LABEL[k] ?? k}</dt>
                                  <dd style={{ wordBreak: "break-all" }}>{paramValueText(k, v)}</dd>
                                </Fragment>
                              ))}
                            </dl>
                          )}
                          {complexEntries.length > 0 && (
                            <details className="skill-doc-details" style={{ marginTop: primitiveEntries.length > 0 ? 8 : 0 }}>
                              <summary>查看完整 JSON（含 {complexEntries.length} 项嵌套数据）</summary>
                              <pre className="audit-json">{JSON.stringify(detail.detail, null, 2)}</pre>
                            </details>
                          )}
                        </>
                      );
                    })()
                  ) : (
                    <span style={{ fontSize: 12, color: "var(--text-muted)" }}>无附加参数</span>
                  )}
                </div>

                {traceChain && (
                  <div className="audit-detail-section">
                    <h3>同 Trace 执行链路（{traceChain.length} 条）</h3>
                    {traceChain.length === 0 ? (
                      <span style={{ fontSize: 12, color: "var(--text-muted)" }}>该 Trace 下无其他相关事件。</span>
                    ) : (
                      traceChain.map((s) => (
                        <div
                          key={s.id}
                          className={`audit-trace-step${s.id === detail.id ? " current" : ""}`}
                          style={{ cursor: "pointer" }}
                          title="点击查看该事件详情"
                          onClick={() => void openDetail(s, true)}
                        >
                          <span className="t">{fmtTime(s.created_at)}</span>
                          <span className={`audit-tag ${tagOf(s).cls}`}>{tagOf(s).label}</span>
                          <span className="s">{s.summary}</span>
                        </div>
                      ))
                    )}
                  </div>
                )}
              </>
            )}
          </aside>
        )}
      </div>
    </div>
  );
}
