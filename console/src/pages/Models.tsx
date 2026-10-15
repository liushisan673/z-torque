import { useCallback, useEffect, useMemo, useState } from "react";
import { request, ApiRequestError } from "../services/api";
import type { ModelProvider, ModelResource, ModelEndpoint, ModelTestResult } from "../types";
import { Loading } from "../components/Loading";
import { Modal } from "../components/Modal";
import { SecretRefInput } from "../components/SecretRefInput";
import { useAuth } from "../stores/auth";

type Tab = "overview" | "endpoints" | "secret" | "test";

// Provider 状态点：停用→灰；启用但缺密钥或无启用模型→黄；其余→绿
function providerDot(p: ModelProvider, models: ModelResource[]): "ok" | "warn" | "off" {
  if (p.status !== "ENABLED") return "off";
  const mine = models.filter((m) => m.provider_id === p.id);
  const hasEnabled = mine.some((m) => m.status === "ENABLED");
  if (!hasEnabled || !p.secret_configured) return "warn";
  return "ok";
}

// Model 行小标签：从真实数据推导（停用 / 无 Endpoint 未连接 / 默认 Endpoint）
function modelTag(m: ModelResource): { text: string; off: boolean } | null {
  if (m.status !== "ENABLED") return { text: "停用", off: true };
  const eps = (m.endpoints ?? []).filter((e) => e.status === "ENABLED");
  if (eps.length === 0) return { text: "未连接", off: true };
  const first = [...eps].sort((a, b) => a.priority - b.priority)[0];
  if (first) return { text: "默认", off: false };
  return null;
}

// 接口类型展示文案（由 provider_type 推导，作为未显式设置时的回退）
function ifaceLabel(providerType: string): string {
  const t = providerType.toUpperCase();
  if (t.includes("ANTHROPIC")) return "Anthropic Messages";
  if (t.includes("OLLAMA") || t.includes("LOCAL")) return "本地推理服务";
  return "OpenAI 兼容 Chat Completions";
}

// 接口类型可选项（与后端 app/api/routes/models.py 的 INTERFACE_TYPES 保持一致，存于模型 config）
const INTERFACE_TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "openai_compatible", label: "OpenAI 兼容" },
  { value: "anthropic", label: "Anthropic" },
  { value: "custom", label: "自定义" },
];

function ifaceText(value: string | null | undefined): string | null {
  if (!value) return null;
  return INTERFACE_TYPE_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

// 连接测试探测方式（与后端 PROBE_MODES 保持一致）
const PROBE_MODE_OPTIONS: { value: string; label: string }[] = [
  { value: "readiness", label: "网关就绪检查" },
  { value: "models_list", label: "模型列表探测" },
  { value: "minimal_chat", label: "最小 Chat 请求" },
];

export function ModelsPage() {
  const { user } = useAuth();
  // 仅 model.manage 持有者可执行写操作与连接测试；其余账号进入只读模式
  const canManage = (user?.permissions ?? []).includes("model.manage");
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [models, setModels] = useState<ModelResource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ModelResource | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [tab, setTab] = useState<Tab>("overview");

  // 本会话内最近一次测试结果（无后端持久化字段，见页面说明）
  const [testResult, setTestResult] = useState<ModelTestResult | null>(null);

  // 概览编辑表单
  const [editName, setEditName] = useState("");
  const [editIdentifier, setEditIdentifier] = useState("");
  const [editIface, setEditIface] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [editFnCall, setEditFnCall] = useState(true);
  const [editCtx, setEditCtx] = useState("");
  const [editMaxOut, setEditMaxOut] = useState("");

  // 弹窗：添加 Provider / 添加 Model / Endpoint / 更换密钥
  const [providerOpen, setProviderOpen] = useState(false);
  const [pName, setPName] = useState("");
  const [pType, setPType] = useState("openai_compatible");
  const [pBaseUrl, setPBaseUrl] = useState("");
  const [pSecret, setPSecret] = useState("");
  const [pDesc, setPDesc] = useState("");

  const [modelOpen, setModelOpen] = useState<string | null>(null); // provider_id
  const [mName, setMName] = useState("");
  const [mIdentifier, setMIdentifier] = useState("");
  const [mDesc, setMDesc] = useState("");

  const [epOpen, setEpOpen] = useState<null | { mode: "create" } | { mode: "edit"; ep: ModelEndpoint }>(null);
  const [epName, setEpName] = useState("");
  const [epUrl, setEpUrl] = useState("");
  const [epPriority, setEpPriority] = useState("0");
  const [epDesc, setEpDesc] = useState("");

  const [secretOpen, setSecretOpen] = useState(false);
  const [secretValue, setSecretValue] = useState("");

  // 连接测试
  const [testEndpointId, setTestEndpointId] = useState("");
  const [probeMode, setProbeMode] = useState("readiness");
  const [testing, setTesting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [ps, ms] = await Promise.all([
        request<ModelProvider[]>("/api/z-torque/models/providers"),
        request<ModelResource[]>("/api/z-torque/models"),
      ]);
      setProviders(ps);
      setModels(ms);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 详情打开时同步编辑表单
  function fillForm(d: ModelResource) {
    setEditName(d.name);
    setEditIdentifier(d.model_identifier);
    setEditIface(d.interface_type ?? "");
    setEditDesc(d.description ?? "");
    setEditFnCall(d.supports_function_calling);
    setEditCtx(d.context_window != null ? String(d.context_window) : "");
    setEditMaxOut(d.max_output_tokens != null ? String(d.max_output_tokens) : "");
  }

  async function openModel(m: ModelResource) {
    setSelectedId(m.id);
    setDetail(null);
    setTestResult(null);
    setTab("overview");
    setLoadingDetail(true);
    setError(null);
    try {
      const d = await request<ModelResource>(`/api/z-torque/models/${m.id}`);
      setDetail(d);
      fillForm(d);
      const eps = (d.endpoints ?? []).filter((e) => e.status === "ENABLED");
      setTestEndpointId(eps.length > 0 ? [...eps].sort((a, b) => a.priority - b.priority)[0].id : "");
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载详情失败");
    } finally {
      setLoadingDetail(false);
    }
  }

  async function refreshDetail(modelId: string) {
    const d = await request<ModelResource>(`/api/z-torque/models/${modelId}`);
    setDetail(d);
    fillForm(d);
    const eps = (d.endpoints ?? []).filter((e) => e.status === "ENABLED");
    setTestEndpointId((prev) => (eps.some((e) => e.id === prev) ? prev : eps.length > 0 ? [...eps].sort((a, b) => a.priority - b.priority)[0].id : ""));
    return d;
  }

  async function saveOverview() {
    if (!detail) return;
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, unknown> = {
        name: editName.trim(),
        model_identifier: editIdentifier.trim(),
        interface_type: editIface || null,
        description: editDesc.trim() || null,
        supports_function_calling: editFnCall,
        context_window: editCtx.trim() ? Number(editCtx) : null,
        max_output_tokens: editMaxOut.trim() ? Number(editMaxOut) : null,
      };
      const d = await request<ModelResource>(`/api/z-torque/models/${detail.id}`, { method: "PATCH", body });
      setDetail(d);
      fillForm(d);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function toggleModel() {
    if (!detail) return;
    const action = detail.status === "ENABLED" ? "disable" : "enable";
    if (action === "disable" && !window.confirm("停用后 Agent 配置侧将不可选用该模型，确定吗？")) return;
    setBusy(true);
    setError(null);
    try {
      const d = await request<ModelResource>(`/api/z-torque/models/${detail.id}/${action}`, { method: "POST" });
      setDetail(d);
      fillForm(d);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function runTest() {
    if (!detail) return;
    setTesting(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (testEndpointId) params.set("endpoint_id", testEndpointId);
      params.set("probe_mode", probeMode);
      const r = await request<ModelTestResult>(`/api/z-torque/models/${detail.id}/test?${params.toString()}`, { method: "POST", body: {} });
      setTestResult(r);
    } catch (err) {
      // 测试失败也作为可展示结果（含 trace）
      if (err instanceof ApiRequestError) {
        setTestResult({
          model_id: detail.id,
          status: "failed",
          provider: { id: detail.provider_id, name: detail.provider?.name ?? "", status: detail.provider?.status ?? "" },
          endpoint: null,
          checks: [],
          duration_ms: 0,
          trace_id: err.traceId ?? null,
          probe_mode: "error",
          http_status: null,
          summary: null,
          error_summary: err.message,
          supports_streaming: false,
          supports_function_calling: false,
          secret_configured: false,
        });
      } else {
        setError("测试失败");
      }
    } finally {
      setTesting(false);
    }
  }

  async function createProvider() {
    if (!pName.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await request("/api/z-torque/models/providers", {
        method: "POST",
        body: {
          name: pName.trim(),
          provider_type: pType,
          base_url: pBaseUrl.trim() || null,
          secret_ref: pSecret.trim() || null,
          description: pDesc.trim() || null,
        },
      });
      setProviderOpen(false);
      setPName(""); setPType("openai_compatible"); setPBaseUrl(""); setPSecret(""); setPDesc("");
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "创建失败");
    } finally {
      setBusy(false);
    }
  }

  async function createModel() {
    if (!modelOpen || !mName.trim() || !mIdentifier.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await request("/api/z-torque/models", {
        method: "POST",
        body: { provider_id: modelOpen, name: mName.trim(), model_identifier: mIdentifier.trim(), description: mDesc.trim() || null },
      });
      setModelOpen(null);
      setMName(""); setMIdentifier(""); setMDesc("");
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "创建失败");
    } finally {
      setBusy(false);
    }
  }

  function openEpCreate() {
    setEpName(""); setEpUrl(""); setEpPriority("0"); setEpDesc("");
    setEpOpen({ mode: "create" });
  }
  function openEpEdit(ep: ModelEndpoint) {
    setEpName(ep.name); setEpUrl(ep.endpoint_url); setEpPriority(String(ep.priority)); setEpDesc(ep.description ?? "");
    setEpOpen({ mode: "edit", ep });
  }

  async function submitEndpoint() {
    if (!detail || !epName.trim() || !epUrl.trim()) return;
    setBusy(true);
    setError(null);
    try {
      if (epOpen?.mode === "create") {
        await request("/api/z-torque/models/endpoints", {
          method: "POST",
          body: {
            provider_id: detail.provider_id,
            model_id: detail.id,
            name: epName.trim(),
            endpoint_url: epUrl.trim(),
            priority: Number(epPriority) || 0,
            description: epDesc.trim() || null,
          },
        });
      } else if (epOpen?.mode === "edit") {
        await request(`/api/z-torque/models/endpoints/${epOpen.ep.id}`, {
          method: "PATCH",
          body: {
            name: epName.trim(),
            endpoint_url: epUrl.trim(),
            priority: Number(epPriority) || 0,
            description: epDesc.trim() || null,
          },
        });
      }
      setEpOpen(null);
      await refreshDetail(detail.id);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function toggleEndpoint(ep: ModelEndpoint) {
    if (!detail) return;
    const action = ep.status === "ENABLED" ? "disable" : "enable";
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/models/endpoints/${ep.id}/${action}`, { method: "POST" });
      await refreshDetail(detail.id);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function changeSecret() {
    if (!detail?.provider || !secretValue.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/models/providers/${detail.provider.id}`, {
        method: "PATCH",
        body: { secret_ref: secretValue.trim() },
      });
      setSecretOpen(false);
      setSecretValue("");
      await refreshDetail(detail.id);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "更换密钥失败");
    } finally {
      setBusy(false);
    }
  }

  const selected = useMemo(() => models.find((m) => m.id === selectedId) ?? null, [models, selectedId]);
  const detailEndpoints = detail?.endpoints ?? [];

  return (
    <div className="model-page">
      {error && (
        <div className="skill-alert skill-alert-error">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      <div className="model-shell">
        {/* 左侧 Provider 树 */}
        <aside className="model-list-pane">
          <div className="model-list-header">
            <h1>模型</h1>
            {canManage && <button className="btn btn-primary" type="button" onClick={() => setProviderOpen(true)}>+ 添加供应商</button>}
          </div>
          <div className="model-provider-tree">
            {loading ? (
              <Loading />
            ) : providers.length === 0 ? (
              <div className="model-detail-empty">
                <h2>暂无供应商</h2>
                <span>请先添加供应商</span>
              </div>
            ) : (
              providers.map((p) => {
                const dot = providerDot(p, models);
                const mine = models.filter((m) => m.provider_id === p.id);
                return (
                  <div key={p.id} className="model-provider-group">
                    <div className="model-provider-head">
                      <span className={`dot ${dot}`} />
                      <span>{p.name}</span>
                      {canManage && (
                        <button
                          className="add-model-btn"
                          type="button"
                          title="添加模型"
                          onClick={() => { setMName(""); setMIdentifier(""); setMDesc(""); setModelOpen(p.id); }}
                        >+</button>
                      )}
                    </div>
                    {mine.length === 0 ? (
                      <div className="model-item" style={{ cursor: "default" }}>
                        <span>暂无模型</span>
                      </div>
                    ) : (
                      mine.map((m) => {
                        const tag = modelTag(m);
                        return (
                          <div
                            key={m.id}
                            className={`model-item${m.id === selectedId ? " active" : ""}`}
                            onClick={() => void openModel(m)}
                          >
                            <span>{m.name}</span>
                            {tag && <span className="mini-tag">{tag.text}</span>}
                          </div>
                        );
                      })
                    )}
                  </div>
                );
              })
            )}
          </div>
        </aside>

        {/* 右侧详情 */}
        <section className="model-detail-pane">
          {!selected ? (
            <div className="model-detail-empty">
              <h2>选择左侧模型查看详情</h2>
              <span>模型调用一律经后端 ModelGateway，前端不直连上游、不存密钥。</span>
            </div>
          ) : (
            <>
              <div className="model-detail-header">
                <div className="breadcrumb">模型 / {detail?.provider?.name ?? selected.provider_id} / {selected.name}</div>
                <div className="title-row">
                  <div>
                    <h2>{detail?.name ?? selected.name}</h2>
                    <p className="subtitle">
                      供应商：{detail?.provider?.name ?? "—"} · {ifaceText(detail?.interface_type) ?? ifaceLabel(detail?.provider?.provider_type ?? "")}
                    </p>
                    <div className="model-detail-tags">
                      {detail?.status === "ENABLED" ? (
                        <span className="model-tag model-tag-lg model-tag-ok">可用</span>
                      ) : (
                        <span className="model-tag model-tag-lg model-tag-err">不可用</span>
                      )}
                      {detail?.provider?.secret_configured ? (
                        <span className="model-tag model-tag-lg model-tag-secret">密钥已配置</span>
                      ) : (
                        <span className="model-tag model-tag-lg model-tag-off">未配置密钥</span>
                      )}
                      {detailEndpoints.some((e) => e.status === "ENABLED") && (
                        <span className="model-tag model-tag-lg model-tag-off">默认接入点</span>
                      )}
                    </div>
                  </div>
                  <div className="model-detail-actions">
                    {canManage && (
                      <>
                        <button className="btn btn-primary" type="button" disabled={busy} onClick={() => { setTab("test"); void runTest(); }}>测试连接</button>
                        <button className="btn btn-secondary" type="button" disabled={busy} onClick={() => void toggleModel()}>
                          {detail?.status === "ENABLED" ? "停用" : "启用"}
                        </button>
                      </>
                    )}
                  </div>
                </div>
              </div>

              <div className="model-tabs">
                {(["overview", "endpoints", "secret", "test"] as Tab[]).map((t) => (
                  <button key={t} className={`model-tab${tab === t ? " active" : ""}`} type="button" onClick={() => setTab(t)}>
                    {t === "overview" ? "概览" : t === "endpoints" ? "接入点" : t === "secret" ? "密钥" : "连接测试"}
                  </button>
                ))}
              </div>

              <div className="model-detail-body">
                {loadingDetail ? (
                  <Loading />
                ) : tab === "overview" ? (
                  <div>
                    <div className="skill-alert skill-alert-info">
                      <span>ℹ</span>
                      <span>模型调用由后端 ModelGateway 统一执行。API <strong>不返回密钥明文</strong>；页面仅展示是否已配置与脱敏密钥引用。更换密钥须走专用安全配置流程。</span>
                    </div>

                    {!canManage && (
                      <div className="skill-alert skill-alert-warn">
                        <span>ℹ</span>
                        <span>当前账号仅具备模型查看权限（model.read），修改配置与连接测试需要 model.manage 权限。</span>
                      </div>
                    )}

                    <div className="model-section">
                      <h3>模型信息</h3>
                      <div className="model-form-grid">
                        <div className="model-field">
                          <label>显示名称</label>
                          <input type="text" value={editName} onChange={(e) => setEditName(e.target.value)} disabled={busy || !canManage} />
                        </div>
                        <div className="model-field">
                          <label>模型标识 model_id</label>
                          <input type="text" value={editIdentifier} onChange={(e) => setEditIdentifier(e.target.value)} disabled={busy || !canManage} />
                          <div className="help">请求上游时使用的 model 字段</div>
                        </div>
                        <div className="model-field">
                          <label>供应商</label>
                          <input type="text" value={detail?.provider?.name ?? ""} disabled />
                        </div>
                        <div className="model-field">
                          <label>接口类型</label>
                          <select value={editIface} onChange={(e) => setEditIface(e.target.value)} disabled={busy || !canManage}>
                            <option value="">未设置</option>
                            {INTERFACE_TYPE_OPTIONS.map((o) => (
                              <option key={o.value} value={o.value}>{o.label}</option>
                            ))}
                          </select>
                          <div className="help">保存于模型配置，供网关识别上游协议</div>
                        </div>
                        <div className="model-field full">
                          <label>说明</label>
                          <textarea rows={2} value={editDesc} onChange={(e) => setEditDesc(e.target.value)} disabled={busy || !canManage} />
                        </div>
                        <div className="model-field">
                          <label>状态</label>
                          <input type="text" value={detail?.status === "ENABLED" ? "启用" : "停用"} disabled />
                        </div>
                        <div className="model-field">
                          <label>上下文窗口 / 最大输出</label>
                          <div style={{ display: "flex", gap: 8 }}>
                            <input type="number" placeholder="context_window" value={editCtx} onChange={(e) => setEditCtx(e.target.value)} disabled={busy || !canManage} />
                            <input type="number" placeholder="max_output_tokens" value={editMaxOut} onChange={(e) => setEditMaxOut(e.target.value)} disabled={busy || !canManage} />
                          </div>
                        </div>
                        <div className="model-field">
                          <label>
                            <input type="checkbox" checked={editFnCall} onChange={(e) => setEditFnCall(e.target.checked)} disabled={busy || !canManage} style={{ marginRight: 6 }} />
                            支持函数调用
                          </label>
                        </div>
                      </div>
                      {canManage && (
                        <div className="form-actions" style={{ marginTop: 12 }}>
                          <button className="btn btn-primary" disabled={busy || !editName.trim() || !editIdentifier.trim()} onClick={() => void saveOverview()}>保存修改</button>
                        </div>
                      )}
                    </div>

                    <div className="model-section">
                      <h3>
                        最近状态
                        <span className="hint">当前无测试结果持久化接口，仅展示本会话内的测试结果</span>
                      </h3>
                      <div className="model-stat-row">
                        <div className="model-stat-card">
                          <div className="label">上次测试</div>
                          {testResult ? (
                            <div className={`value ${testResult.status === "ready" ? "ok" : "err"}`}>
                              {testResult.status === "ready" ? "成功" : "失败"}
                            </div>
                          ) : (
                            <div className="value dim">—</div>
                          )}
                        </div>
                        <div className="model-stat-card">
                          <div className="label">耗时</div>
                          {testResult ? (
                            <div className="value">{testResult.duration_ms} ms</div>
                          ) : (
                            <div className="value dim">—</div>
                          )}
                        </div>
                        <div className="model-stat-card">
                          <div className="label">最近错误</div>
                          {testResult && testResult.status !== "ready" ? (
                            <div className="value dim">{testResult.error_summary ?? "测试未通过，详见连接测试"}</div>
                          ) : (
                            <div className="value dim">无</div>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                ) : tab === "endpoints" ? (
                  <div>
                    <div className="model-section">
                      <h3>
                        接入点列表
                        {canManage && <button className="btn btn-secondary btn-sm" type="button" onClick={openEpCreate}>添加接入点</button>}
                      </h3>
                      {detailEndpoints.length === 0 ? (
                        <div className="skill-readonly-banner">暂无接入点，请先添加。</div>
                      ) : (
                        <table className="model-endpoint-table">
                          <thead>
                            <tr>
                              <th>名称</th>
                              <th>Base URL</th>
                              <th>优先级</th>
                              <th>状态</th>
                              <th>操作</th>
                            </tr>
                          </thead>
                          <tbody>
                            {detailEndpoints.map((ep) => (
                              <tr key={ep.id}>
                                <td className="ep-name">{ep.name}</td>
                                <td className="url" title={ep.endpoint_url}>{ep.endpoint_url}</td>
                                <td>{ep.priority}</td>
                                <td>
                                  {ep.status === "ENABLED" ? (
                                    <span className="model-tag model-tag-ok">启用</span>
                                  ) : (
                                    <span className="model-tag model-tag-off">停用</span>
                                  )}
                                </td>
                                <td>
                                  {canManage && (
                                    <div style={{ display: "flex", gap: 6 }}>
                                      <button className="btn btn-secondary btn-sm" type="button" onClick={() => openEpEdit(ep)}>编辑</button>
                                      <button className="btn btn-secondary btn-sm" type="button" disabled={busy} onClick={() => void toggleEndpoint(ep)}>
                                        {ep.status === "ENABLED" ? "停用" : "启用"}
                                      </button>
                                    </div>
                                  )}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                      <p className="model-field help" style={{ marginTop: 12 }}>超时、重试等高级参数由后端配置；调用仍走 ModelGateway。</p>
                    </div>
                  </div>
                ) : tab === "secret" ? (
                  <div>
                    <div className="skill-alert skill-alert-warn">
                      <span>⚠</span>
                      <span>密钥通过 Secret Store 保存。页面只显示「已配置」或脱敏密钥引用，<strong>永不展示明文</strong>。更换时提交新值由后端写入密钥库。</span>
                    </div>
                    <div className="model-section">
                      <h3>API Key</h3>
                      <div className="model-secret-row">
                        <div className="secret-status">
                          {detail?.provider?.secret_configured ? (
                            <>
                              <span className="model-tag model-tag-secret">已配置</span>
                              <code>{detail.provider.secret_ref_masked ?? "secret://••••"}</code>
                            </>
                          ) : (
                            <span className="model-tag model-tag-off">未配置</span>
                          )}
                        </div>
                        {canManage && <button className="btn btn-secondary" type="button" onClick={() => { setSecretValue(""); setSecretOpen(true); }}>更换密钥</button>}
                      </div>
                      <p className="model-field help" style={{ marginTop: 10 }}>引用仅作标识展示（脱敏），不可用于还原明文。</p>
                    </div>
                  </div>
                ) : (
                  <div>
                    <div className="model-section">
                      <h3>
                        连接测试
                        <span className="hint">由后端向上游发起探测，前端不直连上游</span>
                      </h3>
                      {!canManage && (
                        <div className="skill-alert skill-alert-warn">
                          <span>ℹ</span>
                          <span>仅具备 model.manage 权限的账号可执行连接测试。</span>
                        </div>
                      )}
                      <div className="model-form-grid">
                        <div className="model-field">
                          <label>使用接入点</label>
                          {detailEndpoints.length === 0 ? (
                            <input type="text" value="无可用接入点" disabled />
                          ) : (
                            <select value={testEndpointId} onChange={(e) => setTestEndpointId(e.target.value)}>
                              {detailEndpoints.map((ep) => (
                                <option key={ep.id} value={ep.id}>{ep.name}（{ep.status === "ENABLED" ? "启用" : "停用"}）</option>
                              ))}
                            </select>
                          )}
                        </div>
                        <div className="model-field">
                          <label>探测方式</label>
                          <select value={probeMode} onChange={(e) => setProbeMode(e.target.value)}>
                            {PROBE_MODE_OPTIONS.map((o) => (
                              <option key={o.value} value={o.value}>{o.label}</option>
                            ))}
                          </select>
                          <div className="help">后两种方式将真实请求上游服务</div>
                        </div>
                      </div>
                      {canManage && (
                        <div style={{ marginTop: 16 }}>
                          <button className="btn btn-primary" type="button" disabled={testing || busy} onClick={() => void runTest()}>
                            {testing ? "测试中…" : "运行测试"}
                          </button>
                        </div>
                      )}

                      {testResult && (
                        <div className={`model-test-result ${testResult.status === "ready" ? "ok" : "fail"}`}>
                          <strong>
                            {testResult.status === "ready" ? "连接就绪" : testResult.status === "unavailable" ? "配置未就绪" : "探测失败"}
                          </strong>
                          <div className="meta">
                            {testResult.summary ?? (testResult.status === "ready" ? "检查通过" : "检查未通过")}
                            {` · 耗时 ${testResult.duration_ms} ms`}
                            {testResult.http_status != null ? ` · HTTP ${testResult.http_status}` : ""}
                            {` · 方式：${PROBE_MODE_OPTIONS.find((o) => o.value === testResult.probe_mode)?.label ?? testResult.probe_mode}`}
                          </div>
                          {testResult.error_summary && <div className="meta">{testResult.error_summary}</div>}
                          {testResult.checks.length > 0 && (
                            <ul className="checks">
                              {testResult.checks.map((c) => (
                                <li key={c.name}>{c.ok ? "✓" : "✗"} {c.name}：{c.detail}</li>
                              ))}
                            </ul>
                          )}
                          {testResult.trace_id && <div className="trace">trace_id: {testResult.trace_id}</div>}
                        </div>
                      )}
                      <p className="model-field help" style={{ marginTop: 12 }}>
                        「网关就绪检查」仅做网关配置检查；「模型列表探测」「最小 Chat 请求」将真实请求上游（不落库、不进会话），可能产生少量上游调用。
                      </p>
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </section>
      </div>

      {/* 添加 Provider */}
      <Modal
        open={providerOpen}
        title="添加供应商"
        onClose={() => setProviderOpen(false)}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setProviderOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={busy || !pName.trim()} onClick={() => void createProvider()}>创建</button>
          </>
        }
      >
        <div className="form-field">
          <label>名称</label>
          <input className="input" value={pName} onChange={(e) => setPName(e.target.value)} placeholder="例如：DeepSeek" />
        </div>
        <div className="form-field">
          <label>类型</label>
          <select className="select" value={pType} onChange={(e) => setPType(e.target.value)}>
            <option value="openai_compatible">OpenAI 兼容</option>
            <option value="anthropic">Anthropic</option>
            <option value="ollama">本地 Ollama</option>
            <option value="custom">自定义</option>
          </select>
        </div>
        <div className="form-field">
          <label>Base URL</label>
          <input className="input" value={pBaseUrl} onChange={(e) => setPBaseUrl(e.target.value)} placeholder="https://api.example.com/v1" />
        </div>
        <div className="form-field">
          <label>Secret URI（可选）</label>
          <SecretRefInput value={pSecret} onChange={setPSecret} placeholder="secret://…" />
        </div>
        <div className="form-field">
          <label>说明</label>
          <textarea className="textarea" rows={2} value={pDesc} onChange={(e) => setPDesc(e.target.value)} />
        </div>
      </Modal>

      {/* 添加模型 */}
      <Modal
        open={modelOpen != null}
        title="添加模型"
        onClose={() => setModelOpen(null)}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setModelOpen(null)}>取消</button>
            <button className="btn btn-primary" disabled={busy || !mName.trim() || !mIdentifier.trim()} onClick={() => void createModel()}>创建</button>
          </>
        }
      >
        <div className="form-field">
          <label>显示名称</label>
          <input className="input" value={mName} onChange={(e) => setMName(e.target.value)} placeholder="例如：DeepSeek-V3" />
        </div>
        <div className="form-field">
          <label>模型标识 model_id</label>
          <input className="input" value={mIdentifier} onChange={(e) => setMIdentifier(e.target.value)} placeholder="例如：deepseek-chat" />
        </div>
        <div className="form-field">
          <label>说明</label>
          <textarea className="textarea" rows={2} value={mDesc} onChange={(e) => setMDesc(e.target.value)} />
        </div>
      </Modal>

      {/* Endpoint 创建/编辑 */}
      <Modal
        open={epOpen != null}
        title={epOpen?.mode === "edit" ? "编辑接入点" : "添加接入点"}
        onClose={() => setEpOpen(null)}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setEpOpen(null)}>取消</button>
            <button className="btn btn-primary" disabled={busy || !epName.trim() || !epUrl.trim()} onClick={() => void submitEndpoint()}>保存</button>
          </>
        }
      >
        <div className="form-field">
          <label>名称</label>
          <input className="input" value={epName} onChange={(e) => setEpName(e.target.value)} placeholder="例如：默认（生产）" />
        </div>
        <div className="form-field">
          <label>Base URL</label>
          <input className="input" value={epUrl} onChange={(e) => setEpUrl(e.target.value)} placeholder="https://api.example.com/v1" />
        </div>
        <div className="form-field">
          <label>优先级（越小越优先）</label>
          <input className="input" type="number" value={epPriority} onChange={(e) => setEpPriority(e.target.value)} />
        </div>
        <div className="form-field">
          <label>说明</label>
          <textarea className="textarea" rows={2} value={epDesc} onChange={(e) => setEpDesc(e.target.value)} />
        </div>
      </Modal>

      {/* 更换密钥 */}
      <Modal
        open={secretOpen}
        title="更换密钥"
        onClose={() => setSecretOpen(false)}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setSecretOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={busy || !secretValue.trim()} onClick={() => void changeSecret()}>提交</button>
          </>
        }
      >
        <div className="skill-alert skill-alert-warn" style={{ marginBottom: 14 }}>
          <span>⚠</span>
          <span>提交后由后端写入 Secret Store，旧引用立即失效。请使用 Secret URI 形式。</span>
        </div>
        <div className="form-field">
          <label>Secret URI</label>
          <SecretRefInput value={secretValue} onChange={setSecretValue} placeholder="secret://…" />
        </div>
      </Modal>
    </div>
  );
}
