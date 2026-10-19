import { useCallback, useEffect, useMemo, useState } from "react";
import { request, ApiRequestError } from "../services/api";
import { errorText } from "../services/errorText";
import type {
  Connector,
  ConnectorDiscoveredToolSummary,
  ConnectorOpenapiImportResult,
  ConnectorTestResult,
  ConnectorToolLink,
} from "../types";
import { Loading } from "../components/Loading";
import { Modal } from "../components/Modal";
import { SecretRefInput } from "../components/SecretRefInput";
import { GuideCallout } from "../components/GuideCallout";
import { useToast } from "../components/Toast";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useAuth } from "../stores/auth";

const API = "/api/z-torque/connectors";

type Tab = "overview" | "config" | "auth" | "ops" | "test";
type Filter = "all" | "REST" | "DB" | "MCP";

// 连接类型展示信息（对齐 connector.html 的 tag 配色体系）
function typeInfo(type: string): { label: string; cls: string } {
  if (type === "REST") return { label: "REST", cls: "connector-tag-rest" };
  if (type === "DB") return { label: "数据库（只读）", cls: "connector-tag-db" };
  if (type === "MCP") return { label: "MCP", cls: "connector-tag-mcp" };
  return { label: type || "—", cls: "connector-tag-off" };
}

// 状态展示（后端仅 ENABLED / DISABLED）
function statusInfo(status: string): { label: string; cls: string } {
  const s = String(status || "").toUpperCase();
  if (s === "ENABLED") return { label: "启用", cls: "connector-tag-ok" };
  if (s === "DISABLED") return { label: "已停用", cls: "connector-tag-off" };
  if (s === "ARCHIVED") return { label: "已归档", cls: "connector-tag-off" };
  return { label: status || "—", cls: "connector-tag-warn" };
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return "—";
  }
}

// 从 config JSON 中安全读取字符串值
function cfgStr(config: Record<string, unknown>, key: string): string {
  const v = config[key];
  if (v === undefined || v === null) return "";
  if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return v.map((x) => String(x)).join(", ");
  return JSON.stringify(v);
}

// TLS 校验值与文案互转（布尔存储）
const TLS_ON = "强制校验";
const TLS_OFF = "仅开发环境可关闭";
function tlsText(v: unknown): string {
  if (v === undefined || v === null) return TLS_ON;
  return v === false || v === "false" ? TLS_OFF : TLS_ON;
}

interface CfgField {
  key: string;
  label: string;
  kind: "text" | "number" | "select";
  options?: string[];
  full?: boolean;
  disabled?: boolean;
  hint?: string;
}

// 按类型返回连接配置字段（键名与后端 config JSON 约定一致）
function configFields(type: string): CfgField[] {
  if (type === "DB") {
    return [
      { key: "driver", label: "驱动", kind: "select", options: ["postgresql", "mysql", "sqlite"], hint: "sqlite 时「数据库名」填本地文件路径" },
      { key: "host", label: "主机", kind: "text", hint: "如：localhost（sqlite 免填）" },
      { key: "port", label: "端口", kind: "number", hint: "PostgreSQL 5432 · MySQL 3306（sqlite 免填）" },
      { key: "database", label: "数据库名", kind: "text" },
      { key: "username", label: "用户名", kind: "text", hint: "建议使用只读账号（sqlite 免填）" },
      { key: "allowed_tables", label: "允许的表", kind: "text", full: true, hint: "逗号分隔；建议在健康检查中勾选保存" },
      { key: "query_limits", label: "查询限制", kind: "text", full: true, hint: "只读 SELECT 支持 JOIN/UNION/WITH；写入语句需审批 · 默认最大 100 行（后端强制）" },
    ];
  }
  if (type === "MCP") {
    return [
      { key: "transport", label: "传输方式", kind: "select", options: ["HTTP", "stdio"] },
      { key: "endpoint", label: "端点 / 命令", kind: "text", full: true, hint: "如 https://mcp.example.com/mcp；本地联调可用 http://127.0.0.1:8765/mcp" },
      { key: "timeout_seconds", label: "超时（秒）", kind: "number" },
    ];
  }
  return [
    { key: "base_url", label: "Base URL", kind: "text", full: true },
    { key: "timeout_seconds", label: "超时（秒）", kind: "number" },
    { key: "verify_tls", label: "TLS 校验", kind: "select", options: [TLS_ON, TLS_OFF] },
  ];
}

const AUTH_MODES = ["Bearer Token", "API Key Header", "Basic Auth", "匿名（需管理员允许）"];

// 认证模式展示映射（存储值保持原始字符串，仅渲染点转中文）
function authModeLabel(m: string): string {
  if (m === "API Key Header") return "API Key 请求头";
  if (m === "Basic Auth") return "Basic 认证";
  return m;
}

// 工具类型展示映射（存储值保持原始枚举，仅渲染点转中文）
function toolTypeLabel(t: string | null | undefined): string {
  if (!t) return "—";
  if (t === "HTTP_API") return "HTTP API";
  if (t === "DB_SQL") return "数据库 SQL";
  return t;
}

// REST 测试结果标题：把"真的连上了 / 被鉴权拒了 / 压根没发请求"说清楚，
// 避免原来那种"只要 ENABLED 就显示连接就绪"的误导
function restTestHeadline(r: ConnectorTestResult): string {
  if (r.http_status === undefined || r.http_status === null) {
    return "配置就绪（未发起网络请求）";
  }
  if (r.status === "auth_required") return "远端可达，鉴权被拒";
  if (r.status === "ready") return "连接就绪（已真实请求）";
  return `响应 ${r.http_status}`;
}

export function ConnectorsPage() {
  const { user } = useAuth();
  const { push: toast } = useToast();
  // 危险操作三连：停用 / 归档 / 删除统一走 ConfirmDialog
  const [confirmDisable, setConfirmDisable] = useState<{ id: string; name: string } | null>(null);
  const [confirmArchive, setConfirmArchive] = useState<{ id: string; name: string } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{ id: string; name: string } | null>(null);
  // 权限：connector.manage 才能新建/编辑/启停/注册（对齐 connector.md 安全要求）
  const canManage = user?.permissions?.includes("connector.manage") ?? false;

  const [items, setItems] = useState<Connector[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Connector | null>(null);
  const [tools, setTools] = useState<ConnectorToolLink[]>([]);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const [filter, setFilter] = useState<Filter>("all");
  const [keyword, setKeyword] = useState("");
  const [tab, setTab] = useState<Tab>("overview");

  // 编辑态（名称/描述 + 当前类型 config 字段 + 认证字段）
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [editConfig, setEditConfig] = useState<Record<string, string>>({});

  // 健康检查结果（本会话内保留，按连接 id 记忆）
  const [testResults, setTestResults] = useState<Record<string, ConnectorTestResult>>({});
  const [testFailure, setTestFailure] = useState<{ message: string; traceId?: string } | null>(null);
  const [testing, setTesting] = useState(false);

  // MCP 工具发现（/discover 结果，切换连接时重置）
  const [discovering, setDiscovering] = useState(false);
  const [discovered, setDiscovered] = useState<ConnectorDiscoveredToolSummary[] | null>(null);

  // OpenAPI 批量导入（REST 专用）：REST 没有自动发现协议，靠 spec 一次性录入 N 个接口
  const [oaOpen, setOaOpen] = useState(false);
  const [oaSource, setOaSource] = useState<"url" | "json">("url");
  const [oaUrl, setOaUrl] = useState("");
  const [oaSpec, setOaSpec] = useState("");
  const [oaPrefix, setOaPrefix] = useState("");
  const [oaImporting, setOaImporting] = useState(false);
  const [oaResult, setOaResult] = useState<ConnectorOpenapiImportResult | null>(null);

  // 新建连接
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createType, setCreateType] = useState("REST");
  const [createDesc, setCreateDesc] = useState("");
  const [createTarget, setCreateTarget] = useState("");
  const [createSecret, setCreateSecret] = useState("");
  // 数据库连接向导字段（类型为 DB 时显示）
  const [createDriver, setCreateDriver] = useState("postgresql");
  const [createHost, setCreateHost] = useState("");
  const [createPort, setCreatePort] = useState("5432");
  const [createDbName, setCreateDbName] = useState("");
  const [createDbUser, setCreateDbUser] = useState("");
  // 切换驱动时同步默认端口（sqlite 无端口概念）
  function changeCreateDriver(driver: string) {
    setCreateDriver(driver);
    if (driver === "postgresql") setCreatePort("5432");
    if (driver === "mysql") setCreatePort("3306");
  }

  // 更换凭证
  const [secretOpen, setSecretOpen] = useState(false);
  const [secretValue, setSecretValue] = useState("");

  // 注册为 Tool
  const [regOpen, setRegOpen] = useState(false);
  const [regName, setRegName] = useState("");
  const [regDesc, setRegDesc] = useState("");
  const [regType, setRegType] = useState("HTTP");
  const [regExternal, setRegExternal] = useState("");
  const [regUrl, setRegUrl] = useState("");
  const [regHeaders, setRegHeaders] = useState("");
  const [regQuery, setRegQuery] = useState("");
  const [regSchema, setRegSchema] = useState("");
  const [regSql, setRegSql] = useState("");

  // 数据库表清单勾选（健康检查页签中维护 allowed_tables）
  const [tableSelection, setTableSelection] = useState<string[]>([]);
  const [savingTables, setSavingTables] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await request<Connector[]>(API);
      setItems(data);
    } catch (err) {
      setError(errorText(err, "加载失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 默认展示列表第一项，避免主区空白（仅当前无选中时生效；删除当前选中后自动补选）
  useEffect(() => {
    if (loading || selectedId) return;
    if (items.length > 0) void openDetail(items[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, items, selectedId]);

  const loadTools = useCallback(async (id: string) => {
    try {
      const list = await request<ConnectorToolLink[]>(`${API}/${id}/tools`);
      setTools(list);
    } catch {
      setTools([]);
    }
  }, []);

  async function openDetail(c: Connector) {
    setSelectedId(c.id);
    setDetail(c);
    setTab("overview");
    setEditing(false);
    setTestFailure(null);
    setDiscovered(null);
    setLoadingDetail(true);
    setError(null);
    try {
      const d = await request<Connector>(`${API}/${c.id}`);
      setDetail(d);
      setEditName(d.name);
      setEditDesc(d.description ?? "");
      setEditConfig(initEditConfig(d));
      setTableSelection(initTableSelection(d));
      await loadTools(d.id);
    } catch (err) {
      setError(errorText(err, "加载详情失败"));
    } finally {
      setLoadingDetail(false);
    }
  }

  // 由后端 config JSON 初始化可编辑字段
  function initEditConfig(d: Connector): Record<string, string> {
    const map: Record<string, string> = {
      auth_mode: cfgStr(d.config, "auth_mode") || AUTH_MODES[0],
      auth_header: cfgStr(d.config, "auth_header") || "Authorization",
    };
    for (const f of configFields(d.connector_type)) {
      if (f.key === "verify_tls") {
        map[f.key] = tlsText(d.config[f.key]);
      } else {
        map[f.key] = cfgStr(d.config, f.key);
      }
    }
    return map;
  }

  // 由 config.allowed_tables 初始化表勾选
  function initTableSelection(d: Connector): string[] {
    const v = d.config["allowed_tables"];
    return Array.isArray(v) ? v.map((x) => String(x)) : [];
  }

  // 将编辑字段写回 config JSON（保留未在前端编辑的原有键）
  function buildConfig(): Record<string, unknown> {
    const base = { ...(detail?.config ?? {}) };
    if (!detail) return base;
    for (const f of configFields(detail.connector_type)) {
      const raw = (editConfig[f.key] ?? "").trim();
      if (f.key === "verify_tls") {
        base[f.key] = raw !== TLS_OFF;
      } else if (detail.connector_type === "DB" && f.key === "allowed_tables") {
        // 数据库允许表以数组存储（后端执行器按列表校验）
        const tables = raw ? raw.split(",").map((t) => t.trim()).filter(Boolean) : [];
        if (tables.length) base[f.key] = tables;
        else delete base[f.key];
      } else if (f.kind === "number" && raw !== "") {
        const n = Number(raw);
        if (!Number.isNaN(n)) base[f.key] = n;
      } else if (raw !== "") {
        base[f.key] = raw;
      } else {
        delete base[f.key];
      }
    }
    if (detail.connector_type !== "DB") {
      base["auth_mode"] = (editConfig["auth_mode"] ?? "").trim() || AUTH_MODES[0];
      base["auth_header"] = (editConfig["auth_header"] ?? "").trim() || "Authorization";
    }
    return base;
  }

  const selected = useMemo(
    () => items.find((c) => c.id === selectedId) ?? detail,
    [items, selectedId, detail],
  );

  // 列表筛选 + 搜索（名称 / 描述 / 目标）
  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    const dbTarget = (c: Connector) => {
      const host = cfgStr(c.config, "host");
      const port = cfgStr(c.config, "port");
      return host ? (port ? `${host}:${port}` : host) : "";
    };
    return items.filter((c) => {
      if (filter !== "all" && c.connector_type !== filter) return false;
      if (!kw) return true;
      const target = cfgStr(c.config, "base_url") || dbTarget(c) || cfgStr(c.config, "endpoint");
      return (c.name + " " + (c.description ?? "") + " " + target).toLowerCase().includes(kw);
    });
  }, [items, filter, keyword]);

  async function saveEdit() {
    if (!detail || !editName.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const d = await request<Connector>(`${API}/${detail.id}`, {
        method: "PATCH",
        body: {
          name: editName.trim(),
          description: editDesc.trim() || null,
          config: buildConfig(),
        },
      });
      setDetail(d);
      setEditing(false);
      await load();
      toast("success", "连接配置已保存");
    } catch (err) {
      setError(errorText(err, "保存失败"));
    } finally {
      setBusy(false);
    }
  }

  async function toggleStatus(c: Connector) {
    if (!confirmDisable || confirmDisable.id !== c.id) return;
    setBusy(true);
    setError(null);
    try {
      const d = await request<Connector>(`${API}/${c.id}/disable`, { method: "POST" });
      setDetail(d);
      await load();
      toast("success", "连接已停用");
    } catch (err) {
      setError(errorText(err, "操作失败"));
    } finally {
      setBusy(false);
      setConfirmDisable(null);
    }
  }

  // 启用是低风险操作（恢复可见/可调用），不弹确认
  async function enableConnector(c: Connector) {
    setBusy(true);
    setError(null);
    try {
      const d = await request<Connector>(`${API}/${c.id}/enable`, { method: "POST" });
      setDetail(d);
      await load();
      toast("success", "连接已启用");
    } catch (err) {
      setError(errorText(err, "操作失败"));
    } finally {
      setBusy(false);
    }
  }

  async function archiveConnector(c: Connector) {
    if (!confirmArchive || confirmArchive.id !== c.id) return;
    setBusy(true);
    setError(null);
    try {
      const d = await request<Connector>(`${API}/${c.id}/archive`, { method: "POST" });
      setDetail(d);
      await load();
      toast("success", "连接已归档");
    } catch (err) {
      setError(errorText(err, "归档失败"));
    } finally {
      setBusy(false);
      setConfirmArchive(null);
    }
  }

  async function deleteConnector(c: Connector) {
    if (!confirmDelete || confirmDelete.id !== c.id) return;
    setBusy(true);
    setError(null);
    try {
      await request(`${API}/${c.id}`, { method: "DELETE" });
      setDetail(null);
      setSelectedId(null);
      await load();
      toast("success", "连接已删除");
    } catch (err) {
      setError(errorText(err, "删除失败"));
    } finally {
      setBusy(false);
      setConfirmDelete(null);
    }
  }

  async function runTest(target?: Connector) {
    const c = target ?? detail;
    if (!c) return;
    setTesting(true);
    setTestFailure(null);
    try {
      const r = await request<ConnectorTestResult>(`${API}/${c.id}/test`, { method: "POST" });
      setTestResults((prev) => ({ ...prev, [c.id]: r }));
    } catch (err) {
      setTestResults((prev) => {
        const next = { ...prev };
        delete next[c.id];
        return next;
      });
      setTestFailure({
        message: errorText(err, "检查失败"),
        traceId: err instanceof ApiRequestError ? err.traceId : undefined,
      });
    } finally {
      setTesting(false);
    }
  }

  // MCP 工具发现：真实握手（initialize + tools/list），结果仅用于展示与快捷注册
  async function runDiscover() {
    if (!detail) return;
    setDiscovering(true);
    setError(null);
    try {
      const r = await request<{ connector_id: string; tools: ConnectorDiscoveredToolSummary[]; requires_approval: boolean }>(`${API}/${detail.id}/discover`, { method: "POST" });
      setDiscovered(r.tools);
    } catch (err) {
      setDiscovered(null);
      setError(errorText(err, "发现工具失败"));
    } finally {
      setDiscovering(false);
    }
  }

  // OpenAPI 批量导入：把 spec 里每个 path+method 转成一个 HTTP 工具
  async function importOpenapi() {
    if (!detail) return;
    setError(null);
    let spec: Record<string, unknown> | null = null;
    if (oaSource === "json") {
      if (!oaSpec.trim()) {
        setError("请粘贴 OpenAPI 文档内容");
        return;
      }
      try {
        const parsed = JSON.parse(oaSpec);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not an object");
        spec = parsed as Record<string, unknown>;
      } catch {
        setError("OpenAPI 文档必须是合法的 JSON 对象");
        return;
      }
    }
    setOaImporting(true);
    try {
      const body: Record<string, unknown> = {
        source: oaSource,
        name_prefix: oaPrefix.trim(),
      };
      if (oaSource === "url") body["url"] = oaUrl.trim();
      else body["spec"] = spec;
      const r = await request<ConnectorOpenapiImportResult>(`${API}/${detail.id}/tools/import-openapi`, { method: "POST", body });
      setOaResult(r);
      await loadTools(detail.id);
      toast("success", `已导入 ${r.registered.length} 个工具`);
    } catch (err) {
      setError(errorText(err, "导入失败"));
    } finally {
      setOaImporting(false);
    }
  }

  async function createConnector() {
    if (!createName.trim()) return;
    if (createSecret.trim() && !createSecret.trim().startsWith("secret://")) {
      setError("Secret URI 必须以 secret:// 开头");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const config: Record<string, unknown> = {};
      if (createType === "REST" && createTarget.trim()) config["base_url"] = createTarget.trim();
      if (createType === "DB") {
        // 数据库连接向导：驱动可选；主机/端口/库名/用户名入 config，密码走 Secret URI（sqlite 本地文件免凭证）
        if (createDriver === "sqlite") {
          if (!createDbName.trim()) {
            setError("请填写 SQLite 数据库文件路径");
            setBusy(false);
            return;
          }
          config["driver"] = "sqlite";
          config["database"] = createDbName.trim();
        } else {
          if (!createHost.trim()) {
            setError("请填写数据库主机");
            setBusy(false);
            return;
          }
          config["driver"] = createDriver;
          config["host"] = createHost.trim();
          if (createPort.trim() && !Number.isNaN(Number(createPort))) config["port"] = Number(createPort);
          if (createDbName.trim()) config["database"] = createDbName.trim();
          if (createDbUser.trim()) config["username"] = createDbUser.trim();
        }
      }
      if (createType === "MCP") {
        if (createTarget.trim()) config["endpoint"] = createTarget.trim();
        config["transport"] = "HTTP";
      }
      const d = await request<Connector>(API, {
        method: "POST",
        body: {
          name: createName.trim(),
          connector_type: createType,
          description: createDesc.trim() || null,
          config,
          secret_ref: createSecret.trim() || null,
        },
      });
      setCreateOpen(false);
      setCreateName("");
      setCreateType("REST");
      setCreateDesc("");
      setCreateTarget("");
      setCreateSecret("");
      setCreateHost("");
      setCreatePort("5432");
      setCreateDbName("");
      setCreateDbUser("");
      await load();
      await openDetail(d);
      toast("success", "连接已创建");
      if (d.connector_type === "DB") {
        // 数据库连接创建后直接进入健康检查，探测表清单供勾选
        setTab("test");
        await runTest(d);
      }
      if (d.connector_type === "MCP") {
        // MCP 连接创建后直接进入健康检查，真实握手（initialize + tools/list）
        setTab("test");
        await runTest(d);
      }
    } catch (err) {
      setError(errorText(err, "创建失败"));
    } finally {
      setBusy(false);
    }
  }

  function toggleTable(name: string) {
    setTableSelection((prev) => (prev.includes(name) ? prev.filter((t) => t !== name) : [...prev, name]));
  }

  async function saveAllowedTables() {
    if (!detail || tableSelection.length === 0) return;
    setSavingTables(true);
    setError(null);
    try {
      const d = await request<Connector>(`${API}/${detail.id}`, {
        method: "PATCH",
        body: { config: { ...detail.config, allowed_tables: tableSelection } },
      });
      setDetail(d);
      setEditConfig(initEditConfig(d));
      await load();
      await runTest(d);
    } catch (err) {
      setError(errorText(err, "保存失败"));
    } finally {
      setSavingTables(false);
    }
  }

  async function changeSecret() {
    if (!detail) return;
    const v = secretValue.trim();
    if (!v.startsWith("secret://")) {
      setError("Secret URI 必须以 secret:// 开头");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const d = await request<Connector>(`${API}/${detail.id}`, { method: "PATCH", body: { secret_ref: v } });
      setDetail(d);
      setSecretOpen(false);
      setSecretValue("");
      await load();
    } catch (err) {
      setError(errorText(err, "更换凭证失败"));
    } finally {
      setBusy(false);
    }
  }

  async function registerTool() {
    if (!detail || !regName.trim()) return;
    const parseJson = (text: string, label: string): Record<string, unknown> | null => {
      if (!text.trim()) return {};
      try {
        const parsed = JSON.parse(text);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not an object");
        return parsed as Record<string, unknown>;
      } catch {
        setError(`${label} 必须是 JSON 对象`);
        return null;
      }
    };
    const body: Record<string, unknown> = {
      name: regName.trim(),
      description: regDesc.trim() || null,
    };
    if (detail.connector_type === "DB") {
      // DB 连接：注册只读 SQL 工具（DB_SQL），SQL 模板 + 输入参数 Schema
      if (!regSql.trim()) {
        setError("SQL 模板不能为空");
        return;
      }
      const inputSchema = parseJson(regSchema || "{}", "输入参数 Schema");
      if (inputSchema === null) return;
      body["tool_type"] = "DB_SQL";
      body["sql_template"] = regSql.trim();
      body["input_schema"] = inputSchema;
    } else {
      let config: Record<string, unknown> = {};
      let inputSchema: Record<string, unknown> | undefined;
      if (regType === "HTTP") {
        if (!regUrl.trim().startsWith("https://")) {
          setError("工具 URL 必须以 https:// 开头");
          return;
        }
        const headers = parseJson(regHeaders, "请求头");
        if (headers === null) return;
        const queryParams = parseJson(regQuery, "查询参数");
        if (queryParams === null) return;
        config = { method: "GET", url: regUrl.trim(), headers, queryParams };
      }
      if (regSchema.trim()) {
        const parsed = parseJson(regSchema, "输入参数 Schema");
        if (parsed === null) return;
        inputSchema = parsed;
      }
      body["tool_type"] = regType;
      body["external_name"] = regExternal.trim() || null;
      if (Object.keys(config).length) body["config"] = config;
      if (inputSchema) body["input_schema"] = inputSchema;
    }
    setBusy(true);
    setError(null);
    try {
      await request(`${API}/${detail.id}/tools/register`, { method: "POST", body });
      setRegOpen(false);
      setRegName("");
      setRegDesc("");
      setRegType("HTTP");
      setRegExternal("");
      setRegUrl("");
      setRegHeaders("");
      setRegQuery("");
      setRegSchema("");
      setRegSql("");
      await loadTools(detail.id);
      toast("success", "工具已注册");
    } catch (err) {
      setError(errorText(err, "注册失败"));
    } finally {
      setBusy(false);
    }
  }

  // MCP 批量注册：把发现结果中尚未登记的工具按默认名称一次性注册，免去逐个弹窗
  async function registerAllDiscovered() {
    if (!detail || !discovered) return;
    const pending = discovered.filter((t) => !registeredNames.has(t.name.trim()));
    if (pending.length === 0) {
      toast("info", "发现结果均已登记");
      return;
    }
    setBusy(true);
    setError(null);
    let ok = 0;
    const failed: string[] = [];
    for (const t of pending) {
      try {
        await request(`${API}/${detail.id}/tools/register`, {
          method: "POST",
          body: { name: t.name.trim(), description: t.description || null, tool_type: "MCP", external_name: t.name.trim() },
        });
        ok += 1;
      } catch {
        failed.push(t.name);
      }
    }
    await loadTools(detail.id);
    setBusy(false);
    if (failed.length === 0) toast("success", `已批量登记 ${ok} 个工具`);
    else toast("info", `已登记 ${ok} 个，失败 ${failed.length} 个：${failed.slice(0, 3).join("、")}${failed.length > 3 ? "…" : ""}`);
  }

  const lastTest = selected ? testResults[selected.id] ?? null : null;
  // 已登记工具名集合：用于在发现结果里区分「已注册 / 未注册」，并支撑批量注册
  const registeredNames = new Set(
    tools.map((t) => String(t.external_name ?? t.name ?? "").trim()).filter(Boolean),
  );
  const enabled = selected?.status === "ENABLED";
  const typeLabel = selected ? typeInfo(selected.connector_type).label : "—";
  const fields = selected ? configFields(selected.connector_type) : [];
  const editingFields = editing && canManage;

  // 编辑 / 保存操作条（编辑态出现在概览与配置 Tab）
  function editBar() {
    return (
      <div className="connector-section connector-edit-bar">
        <button className="btn btn-secondary" disabled={busy} onClick={() => {
          setEditing(false);
          if (detail) {
            setEditName(detail.name);
            setEditDesc(detail.description ?? "");
            setEditConfig(initEditConfig(detail));
          }
        }}>取消</button>
        <button className="btn btn-primary" disabled={busy || !editName.trim()} onClick={() => void saveEdit()}>保存修改</button>
      </div>
    );
  }

  return (
    <div className="connector-page">
      {error && (
        <div className="skill-alert skill-alert-error">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      <div className="connector-shell">
        {/* 左侧列表 */}
        <aside className="connector-list-pane">
          <div className="connector-list-header">
            <h1>连接器</h1>
            <div className="connector-list-actions">
              {canManage && (
                <button className="btn btn-primary" type="button" onClick={() => setCreateOpen(true)}>+ 新建连接</button>
              )}
              <button className="btn btn-secondary btn-sm" type="button" onClick={() => void load()} title="刷新">刷新</button>
            </div>
            <input
              className="connector-search-box"
              type="search"
              placeholder="搜索名称或目标…"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
            />
            <div className="connector-filter-tabs">
              {(["all", "REST", "DB", "MCP"] as Filter[]).map((f) => (
                <button
                  key={f}
                  className={`connector-filter-tab${filter === f ? " active" : ""}`}
                  type="button"
                  onClick={() => setFilter(f)}
                >
                  {f === "all" ? "全部" : typeInfo(f).label}
                </button>
              ))}
            </div>
          </div>

          <div className="connector-list">
            {loading ? (
              <Loading />
            ) : filtered.length === 0 ? (
              <div className="connector-detail-empty">
                <GuideCallout
                  title={loading ? "加载中" : "还没有连接"}
                  why={loading ? undefined : canManage ? "接入外部系统与 MCP 工具，需要先创建连接" : "连接由管理员统一创建与维护"}
                  actionLabel={canManage ? "新建连接" : undefined}
                  onAction={canManage ? () => setCreateOpen(true) : undefined}
                  hint={canManage ? "创建后可在「操作」tab 登记工具，供技能绑定调用" : undefined}
                />
              </div>
            ) : (
              filtered.map((c) => {
                const ti = typeInfo(c.connector_type);
                const si = statusInfo(c.status);
                return (
                  <div
                    key={c.id}
                    className={`connector-card${c.id === selectedId ? " active" : ""}`}
                    onClick={() => void openDetail(c)}
                  >
                    <div className="row1">
                      <span className="name">{c.name}</span>
                      <span className={`connector-tag ${si.cls}`}>{si.label}</span>
                    </div>
                    <div className="desc">{c.description ?? "—"}</div>
                    <div className="meta">
                      <span className={`connector-tag ${ti.cls}`}>{ti.label}</span>
                      <span className={`connector-tag ${c.secret_configured ? "connector-tag-secret" : "connector-tag-off"}`}>
                        {c.secret_configured ? "凭证已配置" : "无凭证 / 匿名"}
                      </span>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </aside>

        {/* 右侧详情 */}
        <section className="connector-detail-pane">
          {!selected ? (
            <div className="connector-detail-empty">
              <h2>选择左侧连接查看详情</h2>
              <span>连接由管理员配置，前端不直连目标系统；执行一律经 SkillGateway。</span>
            </div>
          ) : (
            <>
              <div className="connector-detail-header">
                <div className="breadcrumb">连接器 / {typeLabel}</div>
                <div className="title-row">
                  <div>
                    <h2>{detail?.name ?? selected.name}</h2>
                    <p className="subtitle">{detail?.description ?? selected.description ?? "（暂无描述）"}</p>
                    <div className="connector-detail-tags">
                      <span className={`connector-tag connector-tag-lg ${typeInfo(selected.connector_type).cls}`}>
                        {typeInfo(selected.connector_type).label}
                      </span>
                      <span className={`connector-tag connector-tag-lg ${statusInfo(selected.status).cls}`}>
                        {statusInfo(selected.status).label}
                      </span>
                      <span className={`connector-tag connector-tag-lg ${selected.secret_configured ? "connector-tag-secret" : "connector-tag-off"}`}>
                        {selected.secret_configured ? "凭证已配置" : "无凭证 / 匿名"}
                      </span>
                    </div>
                  </div>
                  <div className="connector-detail-actions">
                    {enabled && (
                      <button className="btn btn-primary" disabled={busy} onClick={() => { setTab("test"); void runTest(); }}>测试连接</button>
                    )}
                    {canManage && !editing && selected.status !== "ARCHIVED" && (
                      <button className="btn btn-secondary" disabled={busy} onClick={() => setEditing(true)}>编辑</button>
                    )}
                    {canManage && !enabled && selected.status !== "ARCHIVED" && (
                      <button
                        className="btn btn-secondary"
                        disabled={busy}
                        onClick={() => setConfirmArchive({ id: selected.id, name: selected.name })}
                      >
                        归档
                      </button>
                    )}
                    {canManage && selected.status === "ARCHIVED" && (
                      <button
                        className="skill-btn-danger-outline"
                        disabled={busy}
                        onClick={() => setConfirmDelete({ id: selected.id, name: selected.name })}
                      >
                        删除
                      </button>
                    )}
                    {canManage && selected.status !== "ARCHIVED" && (
                      <button
                        className={enabled ? "skill-btn-danger-outline" : "btn btn-primary"}
                        disabled={busy}
                        onClick={() => {
                          if (enabled) {
                            setConfirmDisable({ id: selected.id, name: selected.name });
                          } else {
                            void enableConnector(selected);
                          }
                        }}
                      >
                        {enabled ? "停用" : "启用"}
                      </button>
                    )}
                  </div>
                </div>
              </div>

              <div className="connector-tabs">
                {(["overview", "config", "auth", "ops", "test"] as Tab[]).map((t) => (
                  <button
                    key={t}
                    className={`connector-tab${tab === t ? " active" : ""}`}
                    type="button"
                    onClick={() => setTab(t)}
                  >
                    {t === "overview" ? "概览" : t === "config" ? "连接配置" : t === "auth" ? "认证与密钥" : t === "ops" ? "操作 / 能力" : "健康检查"}
                  </button>
                ))}
              </div>

              <div className="connector-detail-body">
                {loadingDetail ? (
                  <Loading />
                ) : tab === "overview" ? (
                  <div className="tab-panel">
                    <div className="connector-alert connector-alert-info">
                      <span>ℹ</span>
                      <span>
                        连接由管理员配置。前端不得绕过连接器与 SkillGateway 直连目标系统。数据库连接必须为<strong>只读</strong>并限制表与查询。敏感项只引用 Secret URI。
                      </span>
                    </div>

                    <div className="connector-section">
                      <h3>基本信息</h3>
                      <div className="connector-form-grid">
                        <div className="connector-field">
                          <label>名称</label>
                          <input type="text" value={editingFields ? editName : selected.name} disabled={!editingFields} onChange={(e) => setEditName(e.target.value)} />
                        </div>
                        <div className="connector-field">
                          <label>类型</label>
                          <input type="text" value={typeLabel} disabled />
                        </div>
                        <div className="connector-field full">
                          <label>描述</label>
                          <textarea rows={2} value={editingFields ? editDesc : selected.description ?? ""} disabled={!editingFields} onChange={(e) => setEditDesc(e.target.value)} />
                        </div>
                        <div className="connector-field">
                          <label>状态</label>
                          <select value={selected.status === "ENABLED" ? "启用" : selected.status === "ARCHIVED" ? "已归档" : "已停用"} disabled>
                            <option>启用</option>
                            <option>已停用</option>
                          </select>
                          <div className="help">启停请使用右上角操作按钮</div>
                        </div>
                        <div className="connector-field">
                          <label>最近检查</label>
                          <input
                            type="text"
                            value={lastTest ? `${fmtTime(lastTest.checked_at)} · 检查正常` : testFailure ? "最近一次检查异常" : "未检查"}
                            disabled
                          />
                        </div>
                      </div>
                    </div>

                    <div className="connector-section">
                      <h3>健康摘要</h3>
                      <div className="connector-health-grid">
                        <div className="connector-health-card">
                          <div className="label">连通性</div>
                          <div className={`value${lastTest ? " ok" : testFailure ? " err" : ""}`}>
                            {lastTest ? "正常" : testFailure ? "异常" : "未检查"}
                          </div>
                        </div>
                        <div className="connector-health-card">
                          <div className="label">延迟</div>
                          <div className="value">{lastTest ? `${lastTest.latency_ms} ms` : "—"}</div>
                        </div>
                        <div className="connector-health-card">
                          <div className="label">可用操作</div>
                          <div className="value">{tools.length}</div>
                        </div>
                      </div>
                    </div>

                    {editingFields && editBar()}
                  </div>
                ) : tab === "config" ? (
                  <div className="tab-panel">
                    {selected.connector_type === "REST" && (
                      <div className="connector-section">
                        <h3>REST 目标 <span className="hint">须通过后端策略校验</span></h3>
                        <div className="connector-form-grid">
                          {fields.map((f) => (
                            <div key={f.key} className={`connector-field${f.full ? " full" : ""}`}>
                              <label>{f.label}</label>
                              {f.kind === "select" ? (
                                <select
                                  value={editConfig[f.key] ?? ""}
                                  disabled={!editingFields}
                                  onChange={(e) => setEditConfig((p) => ({ ...p, [f.key]: e.target.value }))}
                                >
                                  {(f.options ?? []).map((o) => <option key={o}>{o}</option>)}
                                </select>
                              ) : (
                                <input
                                  type={f.kind === "number" ? "number" : "text"}
                                  value={editConfig[f.key] ?? ""}
                                  disabled={!editingFields}
                                  placeholder={f.hint ?? ""}
                                  onChange={(e) => setEditConfig((p) => ({ ...p, [f.key]: e.target.value }))}
                                />
                              )}
                              {f.hint && <div className="help">{f.hint}</div>}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {selected.connector_type === "DB" && (
                      <div className="connector-section">
                        <h3>只读数据库 <span className="hint">必须只读 · 限制表与查询</span></h3>
                        <div className="connector-form-grid">
                          {fields.map((f) => (
                            <div key={f.key} className={`connector-field${f.full ? " full" : ""}`}>
                              <label>{f.label}</label>
                              <input
                                type="text"
                                value={editConfig[f.key] ?? ""}
                                disabled={!editingFields || f.disabled}
                                placeholder={f.hint ?? ""}
                                onChange={(e) => setEditConfig((p) => ({ ...p, [f.key]: e.target.value }))}
                              />
                              {f.hint && <div className="help">{f.hint}</div>}
                            </div>
                          ))}
                        </div>
                        <div className="connector-alert connector-alert-warn" style={{ marginTop: 12, marginBottom: 0 }}>
                          <span>⚠</span>
                          <span>生产默认拒绝写入账号与无表限制配置。凭证走 Secret URI。</span>
                        </div>
                      </div>
                    )}
                    {selected.connector_type === "MCP" && (
                      <div className="connector-section">
                        <h3>MCP 传输 <span className="hint">JSON-RPC 2.0 · tools/list · tools/call</span></h3>
                        <div className="connector-form-grid">
                          {fields.map((f) => (
                            <div key={f.key} className={`connector-field${f.full ? " full" : ""}`}>
                              <label>{f.label}</label>
                              {f.kind === "select" ? (
                                <select
                                  value={editConfig[f.key] ?? ""}
                                  disabled={!editingFields}
                                  onChange={(e) => setEditConfig((p) => ({ ...p, [f.key]: e.target.value }))}
                                >
                                  {(f.options ?? []).map((o) => <option key={o}>{o}</option>)}
                                </select>
                              ) : (
                                <input
                                  type={f.kind === "number" ? "number" : "text"}
                                  value={editConfig[f.key] ?? ""}
                                  disabled={!editingFields}
                                  placeholder={f.hint ?? ""}
                                  onChange={(e) => setEditConfig((p) => ({ ...p, [f.key]: e.target.value }))}
                                />
                              )}
                              {f.hint && <div className="help">{f.hint}</div>}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {editingFields && editBar()}
                  </div>
                ) : tab === "auth" ? (
                  <div className="tab-panel">
                    <div className="connector-alert connector-alert-warn">
                      <span>⚠</span>
                      <span>支持 API Key / Bearer、Basic，以及管理员明确允许的匿名连接。凭证只存 Secret 引用，页面不展示明文。</span>
                    </div>
                    <div className="connector-section">
                      <h3>认证模式</h3>
                      <div className="connector-form-grid">
                        <div className="connector-field">
                          <label>模式</label>
                          <select
                            value={editConfig["auth_mode"] ?? AUTH_MODES[0]}
                            disabled={!editingFields}
                            onChange={(e) => setEditConfig((p) => ({ ...p, auth_mode: e.target.value }))}
                          >
                            {AUTH_MODES.map((m) => <option key={m} value={m}>{authModeLabel(m)}</option>)}
                          </select>
                        </div>
                        <div className="connector-field">
                          <label>请求头名（如适用）</label>
                          <input
                            type="text"
                            value={editConfig["auth_header"] ?? "Authorization"}
                            disabled={!editingFields}
                            onChange={(e) => setEditConfig((p) => ({ ...p, auth_header: e.target.value }))}
                          />
                        </div>
                      </div>
                      <div style={{ marginTop: 16 }}>
                        <label style={{ fontSize: 12, fontWeight: 500, color: "var(--text-muted)", display: "block", marginBottom: 8 }}>凭证</label>
                        <div className="connector-secret-row">
                          <div className="secret-status">
                            <span className={`connector-tag ${selected.secret_configured ? "connector-tag-secret" : "connector-tag-off"}`}>
                              {selected.secret_configured ? "已配置" : "未配置"}
                            </span>
                            <code>{selected.secret_configured ? selected.secret_ref_masked ?? "已配置（仅引用，不展示明文）" : "—"}</code>
                          </div>
                          {canManage && (
                            <button className="btn btn-secondary" type="button" onClick={() => { setSecretValue(""); setSecretOpen(true); }}>更换凭证</button>
                          )}
                        </div>
                      </div>
                    </div>
                    {editingFields && editBar()}
                  </div>
                ) : tab === "ops" ? (
                  <div className="tab-panel">
                    <div className="connector-section">
                      <h3>
                        已注册操作 / 能力
                        <span className="hint">可登记为工具供 Skill 使用</span>
                      </h3>
                      {tools.length === 0 ? (
                        <div className="skill-readonly-banner">当前连接尚未注册任何操作。</div>
                      ) : (
                        <table className="connector-op-table">
                          <thead>
                            <tr>
                              <th>操作</th>
                              <th>类型</th>
                              <th>说明</th>
                              <th>授权</th>
                            </tr>
                          </thead>
                          <tbody>
                            {tools.map((t) => (
                              <tr key={t.id}>
                                <td className="op-name">{t.name ?? t.external_name ?? t.id}</td>
                                <td>{toolTypeLabel(t.tool_type)}</td>
                                <td>{t.description ?? "—"}</td>
                                <td>
                                  <span className={`connector-tag ${t.approved ? "connector-tag-ok" : "connector-tag-warn"}`}>
                                    {t.approved ? "已注册" : "待授权"}
                                  </span>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                      {canManage && enabled && (
                        <div style={{ marginTop: 12, display: "flex", gap: 8, flexWrap: "wrap" }}>
                          <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => { setRegType(selected.connector_type === "DB" ? "DB_SQL" : "HTTP"); setRegOpen(true); }}>登记新操作为工具</button>
                          {selected.connector_type === "REST" && (
                            <button
                              className="btn btn-secondary btn-sm"
                              disabled={busy}
                              onClick={() => { setOaResult(null); setOaOpen(true); }}
                              title="上传或拉取 OpenAPI 3.x 文档，一次性把 N 个接口登记为工具"
                            >
                              从 OpenAPI 导入
                            </button>
                          )}
                          {selected.connector_type === "MCP" && (
                            <button className="btn btn-secondary btn-sm" disabled={busy || discovering} onClick={() => void runDiscover()}>
                              {discovering ? "发现中…" : "从 MCP 服务发现工具"}
                            </button>
                          )}
                          {selected.connector_type === "MCP" && discovered !== null && (
                            <button
                              className="btn btn-primary btn-sm"
                              disabled={busy || discovering || discovered.every((t) => registeredNames.has(t.name.trim()))}
                              onClick={() => void registerAllDiscovered()}
                              title="把发现结果中尚未登记的工具按默认名称一次性注册"
                            >
                              批量登记未注册工具（{discovered.filter((t) => !registeredNames.has(t.name.trim())).length}）
                            </button>
                          )}
                        </div>
                      )}
                      {/* MCP 发现结果：发现 ≠ 已授权，注册后才能被 Skill 绑定调用 */}
                      {selected.connector_type === "MCP" && discovered !== null && (
                        <div className="connector-section" style={{ marginTop: 12, marginBottom: 0 }}>
                          <h3>发现结果 <span className="hint">来自 MCP 服务 tools/list · 需注册后才可用</span></h3>
                          {discovered.length === 0 ? (
                            <div className="skill-readonly-banner">MCP 服务未返回任何工具。</div>
                          ) : (
                            <table className="connector-op-table">
                              <thead>
                                <tr>
                                  <th>工具</th>
                                  <th>说明</th>
                                  {canManage && enabled && <th>操作</th>}
                                </tr>
                              </thead>
                              <tbody>
                                {discovered.map((t) => {
                                  const already = registeredNames.has(t.name.trim());
                                  return (
                                    <tr key={t.name}>
                                      <td className="op-name">{t.name}</td>
                                      <td>{t.description || "—"}</td>
                                      {canManage && enabled && (
                                        <td>
                                          {already ? (
                                            <span className="connector-tag connector-tag-ok">已登记</span>
                                          ) : (
                                            <button
                                              className="btn btn-secondary btn-sm"
                                              disabled={busy}
                                              onClick={() => { setRegType("MCP"); setRegName(t.name); setRegExternal(t.name); setRegDesc(t.description); setRegOpen(true); }}
                                            >
                                              登记为工具
                                            </button>
                                          )}
                                        </td>
                                      )}
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          )}
                        </div>
                      )}
                      <p className="connector-foot-hint">
                        发现能力 ≠ 已授权。注册与启停由后端与权限控制；前端不能直接调用目标系统。
                      </p>
                    </div>
                  </div>
                ) : (
                  /* 健康检查 */
                  <div className="tab-panel">
                    <div className="connector-section">
                      <h3>健康检查 / 测试连接</h3>
                      <p style={{ fontSize: 13, color: "var(--text-muted)", margin: "0 0 14px" }}>
                        由后端按连接类型探测，结果写入状态与审计。停用的连接无法执行检查。
                        {selected?.connector_type === "REST" && " REST 连接会真实发起一次 HTTP 请求并回报状态码。"}
                      </p>
                      <button className="btn btn-primary" disabled={busy || testing || !enabled} onClick={() => void runTest()}>
                        {testing ? "检查中…" : "运行检查"}
                      </button>
                      {!enabled && (
                        <p className="connector-foot-hint">连接已停用，启用后才能运行检查。</p>
                      )}
                      {lastTest && (
                        <div className={`connector-test-result ${lastTest.partial ? "warn" : "ok"}`}>
                          {/* REST：区分「真的连上了」「被鉴权拒了」「压根没发请求」三种情况 */}
                          {selected?.connector_type === "REST" ? (
                            <>
                              <strong>{restTestHeadline(lastTest)}</strong>
                              <div className="meta">
                                {typeLabel} · HTTP {lastTest.http_status ?? "—"} · {lastTest.response_method ?? "—"} · 耗时 {lastTest.latency_ms} ms · {fmtTime(lastTest.checked_at)}
                              </div>
                              {lastTest.endpoint && <div className="meta">目标 {lastTest.endpoint}{lastTest.server ? ` · server=${lastTest.server}` : ""}</div>}
                              <div className="meta">
                                凭证 {lastTest.auth_used === false ? "未携带（未配置 Secret URI 或选择了匿名）" : "已按认证方式携带"}
                              </div>
                              {/* 内网地址直连探测，避免本机 http_proxy 返回误导性的 502 */}
                              {lastTest.proxy_bypassed && (
                                <div className="meta">已绕过本机代理直连（内网地址，不经 http_proxy）</div>
                              )}
                              {lastTest.warning && <div className="meta">{lastTest.warning}</div>}
                            </>
                          ) : (
                            <>
                              <strong>连接就绪</strong>
                              <div className="meta">
                                {typeLabel} · 状态 {lastTest.status} · 检查耗时 {lastTest.latency_ms} ms · {fmtTime(lastTest.checked_at)}
                              </div>
                              {selected?.connector_type === "DB" && (
                                <div className="meta">数据库 {lastTest.database || "—"} · 发现 {lastTest.tables?.length ?? 0} 张表</div>
                              )}
                              {selected?.connector_type === "MCP" && (
                                <div className="meta">端点 {lastTest.endpoint || "—"} · 握手成功 · 发现 {lastTest.tools?.length ?? 0} 个工具</div>
                              )}
                            </>
                          )}
                          {lastTest.trace_id && <div className="trace">trace_id: {lastTest.trace_id}</div>}
                        </div>
                      )}
                      {testFailure && !lastTest && (
                        <div className="connector-test-result fail">
                          <strong>检查失败</strong>
                          <div className="meta">{testFailure.message}</div>
                          {testFailure.traceId && <div className="trace">trace_id: {testFailure.traceId}</div>}
                        </div>
                      )}
                      {/* DB 连接：表清单勾选 → 保存 allowed_tables，供 SQL 工具注册引用 */}
                      {lastTest && selected.connector_type === "DB" && (
                        <div className="connector-section" style={{ marginTop: 16, marginBottom: 0 }}>
                          <h3>允许的表 <span className="hint">勾选后保存；注册 SQL 工具仅可引用这些表</span></h3>
                          {(lastTest.tables ?? []).length === 0 ? (
                            <div className="skill-readonly-banner">未在目标库中发现任何表。</div>
                          ) : (
                            <div className="connector-table-grid">
                              {(lastTest.tables ?? []).map((t) => (
                                <label key={t} className={`connector-table-item${tableSelection.includes(t) ? " checked" : ""}`}>
                                  <input
                                    type="checkbox"
                                    checked={tableSelection.includes(t)}
                                    onChange={() => toggleTable(t)}
                                    disabled={!canManage}
                                  />
                                  <code>{t}</code>
                                </label>
                              ))}
                            </div>
                          )}
                          {canManage && (
                            <div style={{ marginTop: 12 }}>
                              <button
                                className="btn btn-primary btn-sm"
                                disabled={busy || savingTables || tableSelection.length === 0}
                                onClick={() => void saveAllowedTables()}
                              >
                                {savingTables ? "保存中…" : `保存允许的表（${tableSelection.length}）`}
                              </button>
                            </div>
                          )}
                        </div>
                      )}
                      {/* MCP 连接：展示真实握手发现的工具清单 */}
                      {lastTest && selected.connector_type === "MCP" && (
                        <div className="connector-section" style={{ marginTop: 16, marginBottom: 0 }}>
                          <h3>发现的工具 <span className="hint">在「操作 / 能力」页签可注册为工具</span></h3>
                          {(lastTest.tools ?? []).length === 0 ? (
                            <div className="skill-readonly-banner">MCP 服务未返回任何工具。</div>
                          ) : (
                            <div className="connector-table-grid">
                              {(lastTest.tools ?? []).map((t) => (
                                <div key={t.name} className="connector-table-item checked" title={t.description || t.name}>
                                  <code>{t.name}</code>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </section>
      </div>

      {/* 新建连接弹窗 */}
      <Modal
        open={createOpen}
        title="新建连接"
        onClose={() => setCreateOpen(false)}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setCreateOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={busy || !createName.trim()} onClick={() => void createConnector()}>创建</button>
          </>
        }
      >
        <div className="form-field">
          <label>名称</label>
          <input className="input" value={createName} onChange={(e) => setCreateName(e.target.value)} placeholder="例如：CRM 开放 API" />
        </div>
        <div className="form-field">
          <label>类型</label>
          <select className="select" value={createType} onChange={(e) => { setCreateType(e.target.value); setCreateTarget(""); }}>
            <option value="REST">REST API</option>
            <option value="DB">只读数据库</option>
            <option value="MCP">MCP 服务</option>
          </select>
        </div>
        {createType !== "DB" && (
          <div className="form-field">
            <label>{createType === "REST" ? "Base URL" : "MCP 端点"}</label>
            <input className="input" value={createTarget} onChange={(e) => setCreateTarget(e.target.value)} placeholder={createType === "REST" ? "https://crm.example.com/api/v2" : "https://mcp.example.com/rpc"} />
          </div>
        )}
        {createType === "DB" && (
          <>
            {/* 数据库连接向导：驱动可选，连接信息 + 密码走 Secret URI，创建后自动探测表清单 */}
            <div className="form-field">
              <label>驱动</label>
              <select className="select" value={createDriver} onChange={(e) => changeCreateDriver(e.target.value)}>
                <option value="postgresql">PostgreSQL</option>
                <option value="mysql">MySQL</option>
                <option value="sqlite">SQLite（本地文件）</option>
              </select>
            </div>
            {createDriver !== "sqlite" && (
              <>
                <div className="form-field">
                  <label>主机</label>
                  <input className="input" value={createHost} onChange={(e) => setCreateHost(e.target.value)} placeholder="例如：localhost" />
                </div>
                <div className="form-field">
                  <label>端口</label>
                  <input className="input" type="number" value={createPort} onChange={(e) => setCreatePort(e.target.value)} placeholder={createDriver === "mysql" ? "3306" : "5432"} />
                </div>
                <div className="form-field">
                  <label>用户名</label>
                  <input className="input" value={createDbUser} onChange={(e) => setCreateDbUser(e.target.value)} placeholder="建议使用只读账号" />
                </div>
              </>
            )}
            <div className="form-field">
              <label>{createDriver === "sqlite" ? "数据库文件路径" : "数据库名"}</label>
              <input className="input" value={createDbName} onChange={(e) => setCreateDbName(e.target.value)} placeholder={createDriver === "sqlite" ? "例如：D:/data/app.db" : "例如：ajlsy_db"} />
            </div>
          </>
        )}
        {(createType !== "DB" || createDriver !== "sqlite") && (
          <div className="form-field">
            <label>{createType === "DB" ? "数据库密码（Secret URI，可粘贴明文一键转换）" : "Secret URI（可选）"}</label>
            <SecretRefInput value={createSecret} onChange={setCreateSecret} placeholder="secret://vault/xxx" />
          </div>
        )}
        <div className="form-field">
          <label>描述</label>
          <textarea className="textarea" value={createDesc} onChange={(e) => setCreateDesc(e.target.value)} placeholder="描述该连接的用途（可选）" />
        </div>
      </Modal>

      {/* 更换凭证弹窗 */}
      <Modal
        open={secretOpen}
        title="更换凭证"
        onClose={() => setSecretOpen(false)}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setSecretOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={busy || !secretValue.trim()} onClick={() => void changeSecret()}>保存</button>
          </>
        }
      >
        <div className="form-field">
          <label>Secret URI</label>
          <SecretRefInput value={secretValue} onChange={setSecretValue} placeholder="secret://vault/xxx" />
        </div>
      </Modal>

      {/* 登记操作为 Tool 弹窗 */}
      <Modal
        open={regOpen}
        title="登记操作为工具"
        onClose={() => setRegOpen(false)}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setRegOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={busy || !regName.trim()} onClick={() => void registerTool()}>注册</button>
          </>
        }
      >
        <div className="form-field">
          <label>操作名</label>
          <input className="input" value={regName} onChange={(e) => setRegName(e.target.value)} placeholder="例如：list_customers" />
        </div>
        <div className="form-field">
          <label>类型</label>
          {selected?.connector_type === "DB" ? (
            <input className="input" value="数据库 SQL（只读查询 / 写入需审批）" disabled />
          ) : (
            <select className="select" value={regType} onChange={(e) => setRegType(e.target.value)}>
              <option value="HTTP">HTTP</option>
              <option value="MCP">MCP</option>
            </select>
          )}
        </div>
        {selected?.connector_type !== "DB" && (
          <div className="form-field">
            <label>外部名（可选）</label>
            <input className="input" value={regExternal} onChange={(e) => setRegExternal(e.target.value)} placeholder="目标系统侧的操作名" />
          </div>
        )}
        {selected?.connector_type === "DB" && (
          <>
            <div className="form-field">
              <label>SQL 模板</label>
              <textarea className="textarea" rows={4} value={regSql} onChange={(e) => setRegSql(e.target.value)} placeholder={"SELECT name, price FROM tombs WHERE status = {{status}} LIMIT 20"} />
              <span className="hint">{"{{参数名}}"} 为绑定变量占位（防注入）；SELECT 支持 JOIN/UNION/子查询/WITH；INSERT/UPDATE/DELETE 为写入工具，执行前强制人工审批（UPDATE/DELETE 必须带 WHERE）；引用表必须在「允许的表」内</span>
            </div>
            <div className="form-field">
              <label>输入参数 Schema（JSON）</label>
              <textarea className="textarea" rows={3} value={regSchema} onChange={(e) => setRegSchema(e.target.value)} placeholder='{"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}' />
              <span className="hint">SQL 模板中的每个 {"{{参数}}"} 必须在此声明</span>
            </div>
          </>
        )}
        {regType === "HTTP" && (
          <>
            <div className="form-field">
              <label>请求 URL</label>
              <input className="input" value={regUrl} onChange={(e) => setRegUrl(e.target.value)} placeholder="https://api.example.com/v1/resource" />
              <div className="field-help">当前仅支持 GET 请求；POST/PUT 等写接口暂不支持登记。</div>
            </div>
            <div className="form-field">
              <label>请求头（JSON，可选）</label>
              <textarea className="textarea" rows={2} value={regHeaders} onChange={(e) => setRegHeaders(e.target.value)} placeholder='{"Authorization": "Bearer {{secret}}"}' />
              <span className="hint">模板：{"{{secret}}"} 引用连接器密钥（需先在凭证中配置）</span>
            </div>
            <div className="form-field">
              <label>查询参数（JSON，可选）</label>
              <textarea className="textarea" rows={2} value={regQuery} onChange={(e) => setRegQuery(e.target.value)} placeholder='{"city": "{{city}}"}' />
              <span className="hint">模板：{"{{参数名}}"} 取运行时入参，额外入参自动并到查询串</span>
            </div>
            <div className="form-field">
              <label>输入参数 Schema（JSON，可选）</label>
              <textarea className="textarea" rows={3} value={regSchema} onChange={(e) => setRegSchema(e.target.value)} placeholder='{"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}' />
            </div>
          </>
        )}
        <div className="form-field">
          <label>说明</label>
          <textarea className="textarea" value={regDesc} onChange={(e) => setRegDesc(e.target.value)} placeholder="该操作的用途（可选）" />
        </div>
      </Modal>

      {/* 危险操作三连：停用 / 归档 / 删除 */}
      <ConfirmDialog
        open={confirmDisable !== null}
        title="停用该连接？"
        confirmText="停用"
        busy={busy}
        onCancel={() => setConfirmDisable(null)}
        onConfirm={() => { const c = detail?.id === confirmDisable?.id ? detail : null; if (c) void toggleStatus(c); }}
      >
        <p>连接「{confirmDisable?.name ?? ""}」将进入「停用」状态，Skill 不再可调用。已注册的工具也会暂停使用。</p>
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmArchive !== null}
        title="归档该连接？"
        confirmText="归档"
        busy={busy}
        onCancel={() => setConfirmArchive(null)}
        onConfirm={() => { const c = detail?.id === confirmArchive?.id ? detail : null; if (c) void archiveConnector(c); }}
      >
        <p>连接「{confirmArchive?.name ?? ""}」将进入「已归档」状态，之后不可再启用或调用。配置信息保留。</p>
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmDelete !== null}
        title="删除该连接？"
        confirmText="删除"
        destructive
        requireText={confirmDelete?.name ?? ""}
        requireLabel="请输入连接名称以确认删除"
        busy={busy}
        onCancel={() => setConfirmDelete(null)}
        onConfirm={() => { const c = detail?.id === confirmDelete?.id ? detail : null; if (c) void deleteConnector(c); }}
      >
        <p>连接「{confirmDelete?.name ?? ""}」将被永久删除。若仍注册有工具，后端会拒绝删除，请先删除对应工具。</p>
      </ConfirmDialog>

      {/* OpenAPI 批量导入：REST 没有自动发现协议，只能靠 spec 一次性录入 */}
      <Modal
        open={oaOpen}
        title="从 OpenAPI 导入工具"
        onClose={() => setOaOpen(false)}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setOaOpen(false)}>关闭</button>
            <button
              className="btn btn-primary"
              disabled={oaImporting || busy || (oaSource === "url" ? !oaUrl.trim() : !oaSpec.trim())}
              onClick={() => void importOpenapi()}
            >
              {oaImporting ? "导入中…" : "解析并导入"}
            </button>
          </>
        }
      >
        <div className="form-field">
          <label>来源</label>
          <select className="select" value={oaSource} onChange={(e) => { setOaSource(e.target.value as "url" | "json"); setOaResult(null); }}>
            <option value="url">从 URL 拉取（https）</option>
            <option value="json">粘贴 JSON</option>
          </select>
        </div>
        {oaSource === "url" ? (
          <div className="form-field">
            <label>OpenAPI 文档地址</label>
            <input
              className="input"
              value={oaUrl}
              onChange={(e) => setOaUrl(e.target.value)}
              placeholder="https://petstore.example.com/openapi.json"
            />
          </div>
        ) : (
          <div className="form-field">
            <label>OpenAPI 文档（JSON）</label>
            <textarea
              className="textarea"
              rows={10}
              value={oaSpec}
              onChange={(e) => setOaSpec(e.target.value)}
              placeholder='{"openapi":"3.0.3","paths":{"/users":{"get":{"operationId":"listUsers"}}}}'
            />
          </div>
        )}
        <div className="form-field">
          <label>工具名前缀（可选）</label>
          <input className="input" value={oaPrefix} onChange={(e) => setOaPrefix(e.target.value)} placeholder="例如：crm_（避免与其它连接重名）" />
        </div>
        <p style={{ fontSize: 12, color: "var(--text-muted)", margin: "6px 0 0" }}>
          每个 <code>path × method</code> 会生成一个 HTTP 工具；重复名称自动加后缀。单次上限 200 个。
        </p>
        {oaResult && (
          <div className="connector-test-result ok" style={{ marginTop: 12 }}>
            <strong>导入完成：已注册 {oaResult.registered.length} 个</strong>
            {oaResult.skipped.length > 0 && (
              <div className="meta">跳过 {oaResult.skipped.length} 个</div>
            )}
            <div className="connector-table-grid" style={{ marginTop: 8 }}>
              {oaResult.registered.slice(0, 24).map((t) => (
                <div key={t.tool_id} className="connector-table-item checked" title={`${t.method} ${t.path}`}>
                  <code>{t.method} {t.path}</code>
                </div>
              ))}
            </div>
            {oaResult.registered.length > 24 && (
              <div className="meta">…等共 {oaResult.registered.length} 个</div>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
