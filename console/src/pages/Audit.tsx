import { Fragment, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { request } from "../services/api";
import { errorText } from "../services/errorText";
import type { AuditEvent, AuditListResponse } from "../types";
import { Loading } from "../components/Loading";
import { useAuth } from "../stores/auth";

const API = "/api/z-torque/audit";
const PAGE_SIZE_OPTIONS = [20, 50, 100];

// 行为类别（后端 audit_category 映射为唯一数据源）：筛选与"类型"列共用，value 需与后端一致
const CATEGORY_OPTIONS: { value: string; label: string }[] = [
  { value: "config", label: "配置变更" },
  { value: "model_call", label: "模型调用" },
  { value: "tool_call", label: "工具调用" },
  { value: "publish", label: "版本发布" },
  { value: "file", label: "文件操作" },
  { value: "confirmation", label: "确认审批" },
  { value: "task", label: "任务运行" },
  { value: "iam", label: "账号权限" },
  { value: "kernel", label: "系统/内核" },
];

// 类别 → 类型列标签与配色（复用现有 audit-tag-* 色板）
const CATEGORY_TAG: Record<string, { label: string; cls: string }> = {
  config: { label: "配置变更", cls: "audit-tag-other" },
  model_call: { label: "模型调用", cls: "audit-tag-model" },
  tool_call: { label: "工具调用", cls: "audit-tag-task" },
  publish: { label: "版本发布", cls: "audit-tag-agent" },
  file: { label: "文件操作", cls: "audit-tag-file" },
  confirmation: { label: "确认审批", cls: "audit-tag-confirm" },
  task: { label: "任务运行", cls: "audit-tag-conn" },
  iam: { label: "账号权限", cls: "audit-tag-skill" },
  kernel: { label: "系统/内核", cls: "audit-tag-other" },
};

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
  AGENT_VERSION: "智能体版本",
  SKILL: "技能",
  SKILL_VERSION: "技能版本",
  TOOL: "工具",
  TOOL_VERSION: "工具版本",
  MODEL: "模型",
  MODEL_PROVIDER: "模型供应商",
  MODEL_ENDPOINT: "模型接入点",
  MODEL_RESOURCE: "模型资源",
  CONNECTOR: "连接器",
  FILE: "文件",
  TASK: "任务",
  CONVERSATION: "会话",
  USER: "用户",
  CONFIRMATION: "确认",
  PORT: "内核能力",
};

// 展示用映射：审计动作（后缀动词，兜底未知 action）→ 中文
const ACTION_VERB_LABEL: Record<string, string> = {
  CREATED: "创建",
  UPDATED: "更新",
  DELETED: "删除",
  DISABLED: "停用",
  ENABLED: "启用",
  LOGIN: "登录",
  LOGOUT: "登出",
  TESTED: "测试",
  EXECUTED: "执行",
  PUBLISHED: "发布",
  ARCHIVED: "归档",
  READ: "读取",
  UPLOADED: "上传",
  CANCELLED: "取消",
};

// 展示用映射：审计动作全量枚举 → 中文（后端 record_audit 全部写入点已覆盖）
const ACTION_LABEL: Record<string, string> = {
  // 智能体
  AGENT_CREATED: "创建",
  AGENT_COPIED: "复制",
  AGENT_ARCHIVED: "归档",
  AGENT_DELETED: "删除",
  DEFAULT_PARENT_SET: "设置父级",
  DEFAULT_PARENT_BOOTSTRAPPED: "初始化父级",
  AGENT_VERSION_CREATED: "创建版本",
  AGENT_VERSION_PUBLISHED: "发布版本",
  AGENT_VERSION_ARCHIVED: "归档版本",
  AGENT_SKILL_ATTACHED: "挂载技能",
  AGENT_SKILL_DETACHED: "卸载技能",
  // 技能 / 工具
  SKILL_CREATED: "创建",
  SKILL_UPDATED: "更新",
  SKILL_DELETED: "删除",
  SKILL_ARCHIVED: "归档",
  TOOL_CREATED: "创建",
  TOOL_UPDATED: "更新",
  TOOL_DELETED: "删除",
  TOOL_ARCHIVED: "归档",
  TOOL_VERSION_PUBLISHED: "发布版本",
  SKILL_TOOL_BINDING_UPGRADED: "升级绑定",
  SKILL_EXECUTED: "执行",
  MD_SKILL_UPLOADED: "上传",
  MD_SKILL_DRAFT_CREATED: "发现草稿",
  MD_SKILL_REGISTERED: "注册",
  // 模型
  MODEL_PROVIDER_CREATED: "创建",
  MODEL_PROVIDER_UPDATED: "更新",
  MODEL_PROVIDER_DISABLED: "停用",
  MODEL_PROVIDER_ENABLED: "启用",
  MODEL_ENDPOINT_CREATED: "创建",
  MODEL_ENDPOINT_UPDATED: "更新",
  MODEL_ENDPOINT_DISABLED: "停用",
  MODEL_ENDPOINT_ENABLED: "启用",
  MODEL_RESOURCE_CREATED: "创建",
  MODEL_RESOURCE_UPDATED: "更新",
  MODEL_RESOURCE_DISABLED: "停用",
  MODEL_RESOURCE_ENABLED: "启用",
  MODEL_CALL_COMPLETED: "模型调用",
  MODEL_CALL_FAILED: "模型调用失败",
  // 连接器
  CONNECTOR_CREATED: "创建",
  CONNECTOR_UPDATED: "更新",
  CONNECTOR_DISABLED: "停用",
  CONNECTOR_ENABLED: "启用",
  CONNECTOR_ARCHIVED: "归档",
  CONNECTOR_DELETED: "删除",
  CONNECTOR_TESTED: "连接测试",
  CONNECTOR_DISCOVERED: "发现工具",
  CONNECTOR_TOOL_REGISTERED: "注册工具",
  CONNECTOR_TOOL_RECONFIGURED: "重配工具",
  CONNECTOR_COMPOSITE_TOOL_REGISTERED: "注册封装工具",
  CONNECTOR_COMPOSITE_TOOL_RECONFIGURED: "重配封装工具",
  CONNECTOR_OPENAPI_IMPORTED: "导入工具",
  // 文件
  FILE_UPLOADED: "上传",
  FILE_READ: "读取",
  FILE_DELETED: "删除",
  // 任务 / 会话
  TASK_CANCELLED: "取消任务",
  TASK_CANCEL: "取消任务请求",
  // 安全
  AUTHORIZATION_DENIED: "权限拒绝",
  // 用户
  USER_CREATED: "创建",
  USER_UPDATED: "更新",
  USER_DELETED: "删除",
  USER_PASSWORD_RESET: "重置密码",
  USER_UNLOCKED: "解锁",
  // 确认
  CONFIRMATION_REQUESTED: "发起确认",
  CONFIRMATION_RESOLVED: "处理确认",
  // 内核能力
  CAPABILITY_UNAVAILABLE: "能力不可用",
};

function resourceTypeLabel(t: string): string {
  return RESOURCE_TYPE_LABEL[t] ?? t;
}

function actionLabel(action: string): string {
  return ACTION_LABEL[action] ?? (() => {
    const i = action.lastIndexOf("_");
    const verb = i > 0 ? ACTION_VERB_LABEL[action.slice(i + 1)] : undefined;
    return verb ?? action;
  })();
}

// 展示用映射：审计摘要（后端 summary 原文）→ 中文
const SUMMARY_LABEL: Record<string, string> = {
  // 智能体
  "agent created": "创建智能体",
  "agent copied": "复制智能体",
  "agent archived": "归档智能体",
  "agent deleted": "删除智能体",
  "default parent set": "设置默认父智能体",
  "default parent bootstrapped": "初始化默认父智能体",
  "agent version draft created": "创建智能体草稿版本",
  "agent version published": "发布智能体版本",
  "agent version archived": "归档智能体版本",
  "required permissions are missing": "缺少所需权限",
  "task cancel requested": "请求取消任务",
  "skill version attached to agent version": "将技能版本挂载至智能体版本",
  "skill version detached from agent version": "将技能版本从智能体版本卸载",
  // 技能 / 工具
  "skill created": "创建技能",
  "skill updated": "更新技能",
  "skill deleted": "删除技能",
  "resource archived": "归档资源",
  "tool created": "创建工具",
  "tool metadata updated": "更新工具元数据",
  "tool deleted": "删除工具",
  "tool version published": "发布工具版本",
  "tool binding upgraded": "升级工具绑定",
  "skill execution completed": "技能执行完成",
  "MD-Skill uploaded": "上传 MD-Skill",
  "MD-Skill draft discovered": "发现 MD-Skill 草稿",
  "MD-Skill registered": "注册 MD-Skill",
  // 模型
  "model provider created": "创建模型供应商",
  "model provider updated": "更新模型供应商",
  "model provider disabled": "停用模型供应商",
  "model provider enabled": "启用模型供应商",
  "model endpoint created": "创建模型接入点",
  "model endpoint updated": "更新模型接入点",
  "model endpoint disabled": "停用模型接入点",
  "model endpoint enabled": "启用模型接入点",
  "model resource created": "创建模型资源",
  "model resource updated": "更新模型资源",
  "model resource disabled": "停用模型资源",
  "model resource enabled": "启用模型资源",
  "model call completed": "模型调用完成",
  "model call completed (tool calls)": "模型调用完成（触发工具调用）",
  "model task failed": "模型任务执行失败",
  "model stream failed": "模型流式调用失败",
  "model stream returned empty response": "模型流式返回空响应",
  // 连接器
  "connector created": "创建连接器",
  "connector updated": "更新连接器",
  "connector disabled": "停用连接器",
  "connector enabled": "启用连接器",
  "connector archived": "归档连接器",
  "connector deleted": "删除连接器",
  "connector test completed": "连接器测试完成",
  "connector test failed": "连接器测试失败",
  "connector tools discovered": "发现连接器工具",
  "connector tool registered": "注册连接器工具",
  "connector tool reconfigured": "重新配置连接器工具",
  "composite tool registered": "注册业务封装工具",
  "composite tool reconfigured": "更新业务封装工具",
  "openapi tools imported": "导入 OpenAPI 工具",
  // 文件
  "file uploaded": "上传文件",
  "file content read": "读取文件内容",
  "file deleted": "删除文件",
  // 任务 / 会话
  "task cancelled by user": "用户取消任务",
  // 用户
  "user created": "创建用户",
  "user updated": "更新用户",
  "user password reset": "重置用户密码",
  "user unlocked": "解锁用户",
  "user deleted": "删除用户",
  "user roles set": "设置用户角色",
  // 确认
  "execution confirmation requested": "发起执行确认",
  "execution confirmation resolved": "处理执行确认",
};

// 动态摘要：能力不可用（summary 内嵌端口名，无法静态枚举）
const CAPABILITY_SUMMARY_RE = /^capability (\S+) is unavailable$/;

function summaryLabel(summary: string | null | undefined): string {
  if (!summary) return "—";
  const direct = SUMMARY_LABEL[summary];
  if (direct) return direct;
  const m = CAPABILITY_SUMMARY_RE.exec(summary);
  if (m) return `能力 ${m[1]} 不可用`;
  return summary;
}

// 类型列标签：直接使用后端 category 字段（行为类别，与筛选同源），未识别时兜底原样展示
function categoryTag(e: AuditEvent): { label: string; cls: string } {
  return CATEGORY_TAG[e.category] ?? { label: e.category, cls: "audit-tag-other" };
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
  AI_CAPABILITY_UNAVAILABLE: "所需工具不可用",
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

  // 筛选状态（变更后自动防抖查询，无需手动「查询」按钮）
  const [range, setRange] = useState<RangeKey>("7d");
  const [startCustom, setStartCustom] = useState("");
  const [endCustom, setEndCustom] = useState("");
  const [category, setCategory] = useState("");
  const [username, setUsername] = useState("");
  const [agentId, setAgentId] = useState("");
  // 智能体筛选下拉数据源（替代手输 UUID）
  const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
  const [traceInput, setTraceInput] = useState("");
  const [keyword, setKeyword] = useState("");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [pageJump, setPageJump] = useState("");
  // 用户名下拉数据源（管理员专用；普通用户该项禁用）
  const [users, setUsers] = useState<string[]>([]);

  // 详情抽屉
  const [detail, setDetail] = useState<AuditEvent | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [traceChain, setTraceChain] = useState<AuditEvent[] | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);

  const buildQuery = useCallback((offset: number) => {
    const qs = new URLSearchParams();
    qs.set("offset", String(offset));
    qs.set("limit", String(pageSize));
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
    if (category) qs.set("category", category);
    if (username.trim()) qs.set("actor_username", username.trim());
    if (agentId.trim()) {
      qs.set("resource_type", "AGENT");
      qs.set("resource_id", agentId.trim());
    }
    if (traceInput.trim()) qs.set("trace_id", traceInput.trim());
    if (keyword.trim()) qs.set("keyword", keyword.trim());
    return qs.toString();
  }, [range, startCustom, endCustom, category, username, agentId, traceInput, keyword, pageSize]);

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

  // 用户下拉选项：管理员加载（供"执行用户"筛选），普通用户不请求
  useEffect(() => {
    if (!canReadAll) return;
    request<{ items: { username: string }[] }>("/api/z-torque/users")
      .then((r) => setUsers(r.items.map((u) => u.username)))
      .catch(() => setUsers([]));
  }, [canReadAll]);

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
    const changed = range !== "7d" || startCustom !== "" || endCustom !== "" || category !== ""
      || username !== "" || agentId !== "" || traceInput !== "" || keyword !== "";
    setRange("7d"); setStartCustom(""); setEndCustom(""); setCategory("");
    setUsername(""); setAgentId(""); setTraceInput(""); setKeyword("");
    setPage(0); setPageJump("");
    // 有筛选变更：防抖 effect 会自动重查（固定 offset 0），无需手动请求，避免双请求；
    // 无变更：effect 不会触发，仅当停留在非首页时手动拉回第 1 页数据
    if (!changed && page !== 0) void load(0);
  }

  // 跳页：夹在有效页码范围内
  function jumpToPage() {
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const target = Math.min(Math.max(1, Number(pageJump) || 1), totalPages);
    setPage(target - 1);
    setPageJump(String(target));
    void load((target - 1) * pageSize);
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
            <select value={category} onChange={(e) => setCategory(e.target.value)}>
              <option value="">全部类型</option>
              {CATEGORY_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
          <div className="audit-field">
            <label>执行用户{scope === "self" ? "（仅自己）" : ""}</label>
            {scope === "all" && users.length > 0 ? (
              <select value={username} onChange={(e) => setUsername(e.target.value)} style={{ width: 130 }}>
                <option value="">全部用户</option>
                {users.map((u) => (
                  <option key={u} value={u}>{u}</option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                placeholder="用户名"
                value={username}
                disabled={scope === "self"}
                onChange={(e) => setUsername(e.target.value)}
                style={{ width: 130 }}
              />
            )}
          </div>
          <div className="audit-field">
            <label>关键字</label>
            <input
              type="text"
              placeholder="摘要全文检索"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              style={{ width: 150 }}
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
          {/* 筛选变更已自动防抖查询，此处仅保留「重置」入口 */}
          <div className="audit-filter-actions">
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
                    const tg = categoryTag(e);
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
                        <td className="summary-cell" title={summaryLabel(e.summary)}>{resourceTypeLabel(e.resource_type)} · {summaryLabel(e.summary)}</td>
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
            <span>共 {total} 条 · 第 {Math.floor(offset / pageSize) + 1} / {Math.max(1, Math.ceil(total / pageSize))} 页</span>
            <div className="spacer" />
            <select value={pageSize} onChange={(e) => setPageSize(Number(e.target.value))} title="每页条数">
              {PAGE_SIZE_OPTIONS.map((s) => (
                <option key={s} value={s}>{s} 条/页</option>
              ))}
            </select>
            <button className="btn btn-secondary btn-sm" disabled={loading || offset <= 0} onClick={() => { const p = Math.max(0, page - 1); setPage(p); setPageJump(String(p + 1)); void load(p * pageSize); }}>上一页</button>
            <input
              className="input"
              style={{ width: 64 }}
              type="number"
              min={1}
              max={Math.max(1, Math.ceil(total / pageSize))}
              value={pageJump}
              placeholder="页码"
              onChange={(e) => setPageJump(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") jumpToPage(); }}
            />
            <button className="btn btn-secondary btn-sm" disabled={loading} onClick={() => jumpToPage()}>跳转</button>
            <button className="btn btn-secondary btn-sm" disabled={loading || offset + pageSize >= total} onClick={() => { const p = page + 1; setPage(p); setPageJump(String(p + 1)); void load(p * pageSize); }}>下一页</button>
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
                    <dt>类型</dt><dd><span className={`audit-tag ${categoryTag(detail).cls}`}>{categoryTag(detail).label}</span> <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-muted)" }}>{actionLabel(detail.action)}</span></dd>
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
                  <p style={{ fontSize: 12, color: "var(--text)", margin: "0 0 8px" }}>{summaryLabel(detail.summary)}</p>
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
                          <span className={`audit-tag ${categoryTag(s).cls}`}>{categoryTag(s).label}</span>
                          <span className="s">{summaryLabel(s.summary)}</span>
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
