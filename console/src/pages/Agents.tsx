import { useCallback, useEffect, useMemo, useState } from "react";
import { request, ApiRequestError } from "../services/api";
import type { Agent, AgentVersion, BoundSkill } from "../types";
import type { ModelResource } from "../types";
import type { Task } from "../types";
import { Loading } from "../components/Loading";
import { Modal } from "../components/Modal";
import { useAuth } from "../stores/auth";

// 已发布 SkillVersion（用于绑定下拉选项）
interface PublishedSkillVersion {
  skill_id: string;
  skill_name: string;
  version_id: string;
  version_number: number;
}

// /skills-tools/skills 列表项
interface SkillListItem {
  id: string;
  name: string;
  skill_type: string;
  official: boolean;
}

// /skills-tools/skills/{id} 详情
interface SkillDetail extends SkillListItem {
  description: string | null;
  versions: { id: string; version_number: number; status: string; snapshot_hash: string | null }[];
}

type Tab = "overview" | "config" | "skills" | "versions";
type Filter = "all" | "mine" | "official" | "draft";

// Agent 在 UI 中的来源标签：本人资源为「我的」；官方与管理员创建的平台资源为「平台」（只读，可复制）
function sourceOf(a: Agent, meId?: string): "official" | "mine" {
  if (a.source_type === "OFFICIAL") return "official";
  return !!meId && !!a.owner_user_id && a.owner_user_id !== meId ? "official" : "mine";
}

// 状态文案与 tag class 映射（与文档保持一致）
function statusInfo(status: string): { label: string; cls: string } {
  const s = String(status || "").toUpperCase();
  if (s === "PUBLISHED") return { label: "已发布", cls: "agent-tag-published" };
  if (s === "DRAFT") return { label: "草稿", cls: "agent-tag-draft" };
  if (s === "ARCHIVED") return { label: "已归档", cls: "agent-tag-archived" };
  return { label: status, cls: "agent-tag-neutral" };
}

// Skill 模式：script_source 非空 → 脚本；否则纯指令
function skillMode(scriptSource: string | null) {
  return scriptSource && scriptSource.trim().length > 0 ? "script" : "instruction";
}

// 时间格式化（保持与 Chat 页一致）
function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return "—";
  }
}

export function AgentsPage() {
  const { user } = useAuth();
  const meId = user?.id;
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [versions, setVersions] = useState<AgentVersion[]>([]);
  const [loadingVersions, setLoadingVersions] = useState(false);
  const [boundSkills, setBoundSkills] = useState<BoundSkill[]>([]);
  const [loadingSkills, setLoadingSkills] = useState(false);
  const [latestTask, setLatestTask] = useState<Task | null>(null);
  const [models, setModels] = useState<ModelResource[]>([]);

  const [filter, setFilter] = useState<Filter>("all");
  const [keyword, setKeyword] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [tab, setTab] = useState<Tab>("overview");

  // 创建 / 编辑
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [editOpen, setEditOpen] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");

  // 版本草稿编辑
  const [editing, setEditing] = useState<{ agentId: string; version: AgentVersion } | null>(null);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [modelId, setModelId] = useState("");
  const [publishedSkills, setPublishedSkills] = useState<PublishedSkillVersion[]>([]);
  const [pickSkillId, setPickSkillId] = useState("");
  // 委派设置编辑（存于 AgentVersion 独立列，随版本草稿一并保存）
  const [editDelegation, setEditDelegation] = useState(false);
  const [editMaxChild, setEditMaxChild] = useState("");
  const [editChildAgents, setEditChildAgents] = useState<string[]>([]);
  const [childOptions, setChildOptions] = useState<{ version_id: string; label: string }[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await request<Agent[]>(`/api/z-torque/agents${showArchived ? "?include_archived=true" : ""}`);
      setAgents(data);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [showArchived]);

  useEffect(() => {
    void load();
  }, [load]);

  // 加载模型列表（用于配置 Tab 模型绑定下拉）
  useEffect(() => {
    void (async () => {
      try {
        const list = await request<ModelResource[]>("/api/z-torque/models");
        setModels(list);
      } catch {
        // 模型加载失败不阻塞主流程
      }
    })();
  }, []);

  const selected = useMemo(() => agents.find((a) => a.id === selectedId) ?? null, [agents, selectedId]);

  // 当前发布版本（与 current_published_version_id 匹配；无则取最新 PUBLISHED）
  const currentPublishedVersion = useMemo<AgentVersion | null>(() => {
    if (versions.length === 0) return null;
    if (selected?.current_published_version_id) {
      const v = versions.find((x) => x.id === selected.current_published_version_id);
      if (v) return v;
    }
    return versions.find((x) => x.status === "PUBLISHED") ?? null;
  }, [versions, selected]);

  // 当前查看的版本（用于配置/技能展示）：编辑草稿时用草稿；否则用当前发布版本
  const viewVersion = useMemo<AgentVersion | null>(() => {
    if (editing) return editing.version;
    return currentPublishedVersion;
  }, [editing, currentPublishedVersion]);

  async function openDetail(a: Agent) {
    setSelectedId(a.id);
    setVersions([]);
    setBoundSkills([]);
    setLatestTask(null);
    setTab("overview");
    setLoadingVersions(true);
    setError(null);
    try {
      const vs = await request<AgentVersion[]>(`/api/z-torque/agents/${a.id}/versions`);
      setVersions(vs);
      // 取当前发布版本并加载其绑定的技能
      const cur = vs.find((x) => x.id === a.current_published_version_id) ?? vs.find((x) => x.status === "PUBLISHED") ?? null;
      if (cur) {
        void loadBoundSkills(a.id, cur.id);
      }
      // 拉取最近使用（最近一次该 Agent 关联的 Task）
      try {
        const r = await request<{ items: Task[] }>("/api/z-torque/tasks", { query: { agent_id: a.id, limit: 1 } });
        setLatestTask(r.items[0] ?? null);
      } catch {
        setLatestTask(null);
      }
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载版本失败");
    } finally {
      setLoadingVersions(false);
    }
  }

  async function loadBoundSkills(agentId: string, versionId: string) {
    setLoadingSkills(true);
    try {
      const list = await request<BoundSkill[]>(`/api/z-torque/agents/${agentId}/versions/${versionId}/skills`);
      setBoundSkills(list);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载技能失败");
      setBoundSkills([]);
    } finally {
      setLoadingSkills(false);
    }
  }

  async function createAgent() {
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await request("/api/z-torque/agents", {
        method: "POST",
        body: { name: name.trim(), description: description.trim() || null },
      });
      setCreateOpen(false);
      setName("");
      setDescription("");
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "创建失败");
    } finally {
      setBusy(false);
    }
  }

  async function copyAgent(id: string) {
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${id}/copy`, { method: "POST" });
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "复制失败");
    } finally {
      setBusy(false);
    }
  }

  async function archiveAgent(id: string) {
    if (!window.confirm("确定归档该智能体吗？")) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${id}/archive`, { method: "POST" });
      await load();
      if (selectedId === id) setSelectedId(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "归档失败");
    } finally {
      setBusy(false);
    }
  }

  async function deleteAgent(id: string) {
    if (!window.confirm("确定删除该智能体吗？其全部版本与技能绑定将被级联删除，且不可恢复。")) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${id}`, { method: "DELETE" });
      if (selectedId === id) setSelectedId(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "删除失败");
    } finally {
      setBusy(false);
    }
  }

  function openEdit(a: Agent) {
    setEditName(a.name);
    setEditDescription(a.description ?? "");
    setEditOpen(true);
  }

  async function saveEdit() {
    if (!editName.trim() || !selected) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${selected.id}`, {
        method: "PATCH",
        body: { name: editName.trim(), description: editDescription.trim() || null },
      });
      setEditOpen(false);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function archiveVersion(agentId: string, versionId: string) {
    if (!window.confirm("确定归档该版本吗？")) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${agentId}/versions/${versionId}/archive`, { method: "POST" });
      if (selected) await openDetail(selected);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "归档版本失败");
    } finally {
      setBusy(false);
    }
  }

  async function createVersion(agentId: string) {
    setBusy(true);
    setError(null);
    try {
      const v = await request<AgentVersion>(`/api/z-torque/agents/${agentId}/versions`, {
        method: "POST",
        body: { config_snapshot: { system_prompt: "" } },
      });
      if (selected) await openDetail(selected);
      void openEditor(agentId, v);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "创建版本失败");
    } finally {
      setBusy(false);
    }
  }

  async function loadPublishedSkills() {
    const skills = await request<SkillListItem[]>("/api/z-torque/skills-tools/skills");
    const result: PublishedSkillVersion[] = [];
    for (const s of skills) {
      try {
        const d = await request<SkillDetail>(`/api/z-torque/skills-tools/skills/${s.id}`);
        for (const v of d.versions) {
          if (v.status === "PUBLISHED") {
            result.push({ skill_id: s.id, skill_name: s.name, version_id: v.id, version_number: v.version_number });
          }
        }
      } catch {
        /* 单个技能加载失败则跳过 */
      }
    }
    return result;
  }

  // 委派白名单候选：其他 Agent 的已发布版本（排除自身）
  async function loadChildOptions(currentAgentId: string) {
    try {
      const list = await request<Agent[]>("/api/z-torque/agents");
      const result: { version_id: string; label: string }[] = [];
      for (const a of list) {
        if (a.id === currentAgentId) continue;
        try {
          const vs = await request<AgentVersion[]>(`/api/z-torque/agents/${a.id}/versions`);
          for (const v of vs) {
            if (v.status === "PUBLISHED") {
              result.push({ version_id: v.id, label: `${a.name} · v${v.version_number}` });
            }
          }
        } catch {
          /* 单个 Agent 加载失败则跳过 */
        }
      }
      setChildOptions(result);
    } catch {
      setChildOptions([]);
    }
  }

  async function openEditor(agentId: string, version: AgentVersion) {
    setError(null);
    setEditing({ agentId, version });
    setTab("config");
    const cfg = version.config_snapshot ?? {};
    setSystemPrompt(typeof cfg.system_prompt === "string" ? cfg.system_prompt : "");
    setModelId(typeof cfg.model === "string" ? cfg.model : "");
    setPickSkillId("");
    setEditDelegation(Boolean(version.delegation_enabled));
    setEditMaxChild(
      version.child_necessity_policy && typeof version.child_necessity_policy.max_child === "number"
        ? String(version.child_necessity_policy.max_child)
        : "",
    );
    setEditChildAgents(Array.isArray(version.allowed_child_agent_versions) ? version.allowed_child_agent_versions : []);
    try {
      const published = await loadPublishedSkills();
      setPublishedSkills(published);
      await loadBoundSkills(agentId, version.id);
      void loadChildOptions(agentId);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "加载技能失败");
      setPublishedSkills([]);
    }
  }

  async function saveVersion() {
    if (!editing) return;
    setBusy(true);
    setError(null);
    const snapshot: Record<string, unknown> = { ...(editing.version.config_snapshot ?? {}) };
    snapshot.system_prompt = systemPrompt.trim();
    if (modelId.trim()) snapshot.model = modelId.trim();
    else delete snapshot.model;
    try {
      const v = await request<AgentVersion>(`/api/z-torque/agents/${editing.agentId}/versions/${editing.version.id}`, {
        method: "PATCH",
        body: {
          config_snapshot: snapshot,
          delegation_enabled: editDelegation,
          allowed_child_agent_versions: editDelegation ? editChildAgents : [],
          child_necessity_policy: editDelegation && editMaxChild.trim() ? { max_child: Number(editMaxChild) } : null,
        },
      });
      setEditing({ agentId: editing.agentId, version: v });
      if (selected) await openDetail(selected);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function attachSkill() {
    if (!editing || !pickSkillId) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${editing.agentId}/versions/${editing.version.id}/skills`, {
        method: "POST",
        body: { skill_version_id: pickSkillId },
      });
      await loadBoundSkills(editing.agentId, editing.version.id);
      setPickSkillId("");
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "绑定技能失败");
    } finally {
      setBusy(false);
    }
  }

  async function publishVersion(agentId: string, versionId: string) {
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${agentId}/versions/${versionId}/publish`, { method: "POST" });
      setEditing(null);
      if (selected) await openDetail(selected);
      await load();
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "发布失败");
    } finally {
      setBusy(false);
    }
  }

  // 列表筛选 + 搜索
  const filteredAgents = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return agents.filter((a) => {
      const matchFilter =
        filter === "all" ||
        (filter === "mine" && sourceOf(a, meId) === "mine") ||
        (filter === "official" && sourceOf(a, meId) === "official") ||
        (filter === "draft" && a.status === "DRAFT");
      if (!matchFilter) return false;
      if (!kw) return true;
      return (a.name + " " + (a.description ?? "")).toLowerCase().includes(kw);
    });
  }, [agents, filter, keyword]);

  // 详情操作按钮随状态变化（对齐文档 4.1）
  const detailActions = useMemo(() => {
    if (!selected) return null;
    const isOfficial = sourceOf(selected, meId) === "official";
    const status = selected.status.toUpperCase();
    if (isOfficial) {
      return (
        <>
          <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void copyAgent(selected.id)}>复制到我的</button>
          <button className="btn btn-secondary btn-sm" onClick={() => void openDetail(selected)}>查看快照</button>
        </>
      );
    }
    if (status === "ARCHIVED") {
      return (
        <>
          <button className="btn btn-secondary btn-sm" onClick={() => void openDetail(selected)}>查看快照</button>
          <button className="agent-btn-danger-outline" disabled={busy} onClick={() => void deleteAgent(selected.id)}>删除</button>
        </>
      );
    }
    if (status === "DRAFT") {
      return (
        <>
          <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => openEdit(selected)}>继续编辑</button>
          <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void createVersion(selected.id)}>新建版本</button>
          <button className="agent-btn-danger-outline" disabled={busy} onClick={() => void archiveAgent(selected.id)}>归档</button>
        </>
      );
    }
    // 已发布
    return (
      <>
        <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => openEdit(selected)}>编辑信息</button>
        <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void createVersion(selected.id)}>发布新版本</button>
        <button className="agent-btn-danger-outline" disabled={busy} onClick={() => void archiveAgent(selected.id)}>归档</button>
      </>
    );
  }, [selected, busy]);

  return (
    <div className="agent-page">
      {error && (
        <div className="agent-alert agent-alert-error">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      <div className="agent-shell">
        {/* 左侧列表 */}
        <aside className="agent-list-pane">
          <div className="agent-list-header">
            <h1>智能体</h1>
            <div className="agent-list-actions">
              <button className="btn btn-primary" type="button" onClick={() => setCreateOpen(true)}>
                + 创建智能体
              </button>
              <button className="btn btn-secondary btn-sm" type="button" onClick={() => void load()} title="刷新">刷新</button>
            </div>
            <div className="agent-search-row">
              <input
                className="agent-search-box"
                type="search"
                placeholder="搜索名称或描述…"
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
              />
            </div>
            <div className="agent-filter-tabs">
              {(["all", "mine", "official", "draft"] as Filter[]).map((f) => (
                <button
                  key={f}
                  className={`agent-filter-tab${filter === f ? " active" : ""}`}
                  type="button"
                  onClick={() => setFilter(f)}
                >
                  {f === "all" ? "全部" : f === "mine" ? "我的" : f === "official" ? "平台" : "草稿"}
                </button>
              ))}
              <label className="agent-archived-toggle">
                <input
                  type="checkbox"
                  checked={showArchived}
                  onChange={(e) => setShowArchived(e.target.checked)}
                />
                <span>显示已归档</span>
              </label>
            </div>
          </div>

          <div className="agent-list">
            {loading ? (
              <Loading />
            ) : filteredAgents.length === 0 ? (
              <div className="agent-detail-empty">
                <h2>暂无智能体</h2>
                <span>请先创建或调整筛选条件</span>
              </div>
            ) : (
              filteredAgents.map((a) => {
                const isActive = a.id === selectedId;
                const isOfficial = sourceOf(a, meId) === "official";
                const si = statusInfo(a.status);
                const curVer = versions.find((v) => v.id === a.current_published_version_id);
                const verText = a.status === "DRAFT" ? "草稿" : curVer ? `当前 v${curVer.version_number}` : "未发布";
                return (
                  <div
                    key={a.id}
                    className={`agent-card${isActive ? " active" : ""}`}
                    onClick={() => void openDetail(a)}
                  >
                    <div className="row1">
                      <span className="name">{a.name}{a.is_default_parent && " · 默认父"}</span>
                      <span className={`agent-tag ${si.cls}`}>{si.label}</span>
                    </div>
                    <div className="desc">{a.description ?? "—"}</div>
                    <div className="meta">
                      <span className={`agent-tag ${isOfficial ? "agent-tag-official" : "agent-tag-mine"}`}>
                        {isOfficial ? "平台" : "我的"}
                      </span>
                      <span className="ver">{verText}</span>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </aside>

        {/* 右侧详情 */}
        <section className="agent-detail-pane">
          {!selected ? (
            <div className="agent-detail-empty">
              <h2>选择左侧智能体查看详情</h2>
              <span>智能体发布后会生成不可变版本快照，任务固定使用创建时选定的版本。</span>
            </div>
          ) : (
            <>
              <div className="agent-detail-header">
                <div className="breadcrumb">智能体 / {sourceOf(selected, meId) === "official" ? "平台" : "我的"}</div>
                <div className="title-row">
                  <div>
                    <h2>{selected.name}</h2>
                    <p className="subtitle">{selected.description ?? "（暂无描述）"}</p>
                    <div className="agent-detail-tags">
                      <span className={`agent-tag agent-tag-lg ${sourceOf(selected, meId) === "official" ? "agent-tag-official" : "agent-tag-mine"}`}>
                        {sourceOf(selected, meId) === "official" ? "平台" : "我的"}
                      </span>
                      <span className={`agent-tag agent-tag-lg ${statusInfo(selected.status).cls}`}>{statusInfo(selected.status).label}</span>
                      {currentPublishedVersion && (
                        <span className="agent-tag agent-tag-lg agent-tag-neutral">当前版本 v{currentPublishedVersion.version_number}</span>
                      )}
                    </div>
                  </div>
                  <div className="agent-detail-actions">{detailActions}</div>
                </div>
              </div>

              <div className="agent-tabs">
                {(["overview", "config", "skills", "versions"] as Tab[]).map((t) => (
                  <button
                    key={t}
                    className={`agent-tab${tab === t ? " active" : ""}`}
                    type="button"
                    onClick={() => setTab(t)}
                  >
                    {t === "overview" ? "概览" : t === "config" ? "配置" : t === "skills" ? "技能绑定" : "版本"}
                  </button>
                ))}
              </div>

              <div className="agent-detail-body">
                {loadingVersions ? (
                  <Loading />
                ) : tab === "overview" ? (
                  <div className="tab-panel">
                    <div className="agent-section">
                      <h3>基本信息</h3>
                      <div className="agent-form-grid">
                        <div className="agent-field">
                          <label>名称</label>
                          <input type="text" value={selected.name} disabled />
                        </div>
                        <div className="agent-field">
                          <label>标识</label>
                          <input type="text" value={selected.id} disabled />
                        </div>
                        <div className="agent-field full">
                          <label>描述</label>
                          <textarea value={selected.description ?? ""} disabled />
                        </div>
                        <div className="agent-field">
                          <label>所有者</label>
                          <input
                            type="text"
                            value={sourceOf(selected, meId) === "official" ? "平台" : (selected.owner_user_id ?? "—")}
                            disabled
                          />
                        </div>
                        <div className="agent-field">
                          <label>创建时间</label>
                          <input type="text" value={fmtTime(selected.created_at)} disabled />
                        </div>
                      </div>
                    </div>

                    <div className="agent-section">
                      <h3>
                        运行概要
                        <span className="hint">只读，来自当前已发布版本快照</span>
                      </h3>
                      <div className="agent-form-grid">
                        <div className="agent-field">
                          <label>绑定模型</label>
                          <input
                            type="text"
                            value={
                              currentPublishedVersion && typeof currentPublishedVersion.config_snapshot?.model === "string"
                                ? String(currentPublishedVersion.config_snapshot.model)
                                : "未指定"
                            }
                            disabled
                          />
                        </div>
                        <div className="agent-field">
                          <label>技能数量</label>
                          <input type="text" value={`${boundSkills.length} 个`} disabled />
                        </div>
                        <div className="agent-field">
                          <label>委派</label>
                          <input
                            type="text"
                            value={
                              currentPublishedVersion
                                ? currentPublishedVersion.delegation_enabled
                                  ? `已开启${currentPublishedVersion.allowed_child_agent_versions?.length ? " · 子智能体 " + currentPublishedVersion.allowed_child_agent_versions.length : ""}`
                                  : "未开启"
                                : "—"
                            }
                            disabled
                          />
                        </div>
                        <div className="agent-field">
                          <label>最近使用</label>
                          <input
                            type="text"
                            value={latestTask ? `${fmtTime(latestTask.created_at)} · ${latestTask.request_summary || "任务"}` : "—"}
                            disabled
                          />
                        </div>
                      </div>
                    </div>

                    <div className="agent-alert agent-alert-info">
                      <span>ℹ</span>
                      <span>发布后生成不可变的智能体版本快照。已创建的任务固定使用创建时选定的版本，不会随后续发布自动升级。</span>
                    </div>
                  </div>
                ) : tab === "config" ? (
                  <div className="tab-panel">
                    {editing ? (
                      <div className="agent-alert agent-alert-info">
                        <span>ℹ</span>
                        <span>正在编辑草稿版本 v{editing.version.version_number}，保存后可发布为新版本。</span>
                      </div>
                    ) : sourceOf(selected, meId) === "official" ? (
                      <div className="agent-alert agent-alert-warn">
                        <span>⚠</span>
                        <span>当前查看的是平台智能体的只读快照。修改前须先「复制到我的」生成个人副本。</span>
                      </div>
                    ) : (
                      <div className="agent-alert agent-alert-warn">
                        <span>⚠</span>
                        <span>当前查看的是已发布版本的只读快照。请先「发布新版本」创建草稿后再修改配置。</span>
                      </div>
                    )}

                    <div className="agent-section">
                      <h3>模型绑定</h3>
                      <div className="agent-form-grid">
                        <div className="agent-field">
                          <label>供应商 / 模型</label>
                          <select
                            value={modelId}
                            onChange={(e) => setModelId(e.target.value)}
                            disabled={!editing || busy}
                          >
                            <option value="">未指定</option>
                            {models.map((m) => (
                              <option key={m.id} value={m.model_identifier || m.id}>
                                {m.name}{m.model_identifier ? `（${m.model_identifier}）` : ""}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div className="agent-field">
                          <label>接入点</label>
                          <input
                            type="text"
                            value={
                              viewVersion && viewVersion.config_snapshot?.endpoint
                                ? String(viewVersion.config_snapshot.endpoint)
                                : "默认"
                            }
                            disabled
                          />
                        </div>
                      </div>
                    </div>

                    <div className="agent-section">
                      <h3>
                        系统规则
                        <span className="hint">写入智能体版本快照，约束模型行为</span>
                      </h3>
                      <div className="agent-rules-editor">
                        <textarea
                          value={systemPrompt}
                          onChange={(e) => setSystemPrompt(e.target.value)}
                          disabled={!editing || busy}
                          placeholder="描述该智能体的角色、规则与行为约束"
                        />
                      </div>
                    </div>

                    <div className="agent-section">
                      <h3>
                        委派设置
                        <span className="hint">{editing ? "仅草稿可改；子智能体须为已发布版本" : "单层父子，子任务不得再委派"}</span>
                      </h3>
                      <div className="agent-form-grid">
                        <div className="agent-field">
                          <label>启用委派</label>
                          <select
                            value={editing ? (editDelegation ? "yes" : "no") : viewVersion?.delegation_enabled ? "yes" : "no"}
                            onChange={(e) => setEditDelegation(e.target.value === "yes")}
                            disabled={!editing || busy}
                          >
                            <option value="yes">是</option>
                            <option value="no">否</option>
                          </select>
                        </div>
                        <div className="agent-field">
                          <label>单根最大子任务数</label>
                          <input
                            type="number"
                            value={
                              editing
                                ? editMaxChild
                                : viewVersion?.child_necessity_policy && typeof viewVersion.child_necessity_policy.max_child === "number"
                                  ? Number(viewVersion.child_necessity_policy.max_child)
                                  : ""
                            }
                            onChange={(e) => setEditMaxChild(e.target.value)}
                            disabled={!editing || busy || !editDelegation}
                            placeholder="例如：3"
                          />
                        </div>
                        <div className="agent-field full">
                          <label>允许的子智能体（已发布版本{editing ? "，可多选" : ""}）</label>
                          {editing ? (
                            <select
                              multiple
                              size={4}
                              value={editChildAgents}
                              onChange={(e) => setEditChildAgents(Array.from(e.target.selectedOptions, (o) => o.value))}
                              disabled={busy || !editDelegation}
                            >
                              {childOptions.length === 0 ? (
                                <option value="" disabled>暂无其他智能体的已发布版本</option>
                              ) : (
                                childOptions.map((o) => (
                                  <option key={o.version_id} value={o.version_id}>{o.label}</option>
                                ))
                              )}
                            </select>
                          ) : (
                            <input
                              type="text"
                              value={(viewVersion?.allowed_child_agent_versions ?? []).join("、") || "—"}
                              disabled
                            />
                          )}
                          <div className="help">仅可选择已发布的子智能体；子任务继承用户 SecurityContext，不得提权。发布时后端会校验引用有效。</div>
                        </div>
                      </div>
                    </div>

                    {editing && (
                      <div className="agent-section" style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                        <button className="btn btn-secondary" disabled={busy} onClick={() => void saveVersion()}>保存草稿</button>
                        <button className="btn btn-primary" disabled={busy} onClick={() => void publishVersion(editing.agentId, editing.version.id)}>发布新版本</button>
                        <button className="btn btn-secondary" onClick={() => setEditing(null)}>取消</button>
                      </div>
                    )}
                  </div>
                ) : tab === "skills" ? (
                  <div className="tab-panel">
                    <div className="agent-section">
                      <h3>
                        已绑定技能
                        <span className="hint">绑定的是技能版本，不是技能目录</span>
                      </h3>
                      {loadingSkills ? (
                        <Loading />
                      ) : boundSkills.length === 0 ? (
                        <div className="agent-readonly-banner">当前版本未绑定任何技能。</div>
                      ) : (
                        <table className="agent-skill-table">
                          <thead>
                            <tr>
                              <th>技能</th>
                              <th>版本</th>
                              <th>模式</th>
                              <th>来源</th>
                              <th>状态</th>
                            </tr>
                          </thead>
                          <tbody>
                            {boundSkills.map((s) => {
                              const mode = skillMode(s.script_source);
                              return (
                                <tr key={s.skill_version_id}>
                                  <td className="skill-name">{s.skill_name}</td>
                                  <td>v{s.version_number}</td>
                                  <td>
                                    <span className={`agent-mode-tag ${mode === "script" ? "agent-mode-script" : "agent-mode-instruction"}`}>
                                      {mode === "script" ? "脚本" : "纯指令"}
                                    </span>
                                  </td>
                                  <td>{s.official ? "平台" : "我的"}</td>
                                  <td><span className={`agent-tag ${statusInfo(s.status).cls}`}>{statusInfo(s.status).label}</span></td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      )}

                      {editing && (
                        <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
                          <select
                            className="agent-field"
                            style={{ flex: 1, padding: "8px 12px", borderRadius: "var(--radius)", border: "1px solid var(--border-strong)" }}
                            value={pickSkillId}
                            onChange={(e) => setPickSkillId(e.target.value)}
                            disabled={busy}
                          >
                            <option value="">选择要绑定的已发布技能版本</option>
                            {publishedSkills.map((s) => (
                              <option key={s.version_id} value={s.version_id}>
                                {s.skill_name} · v{s.version_number}
                              </option>
                            ))}
                          </select>
                          <button className="btn btn-secondary" disabled={busy || !pickSkillId} onClick={() => void attachSkill()}>绑定</button>
                        </div>
                      )}

                      <p className="agent-skill-foot">
                        智能体只能绑定技能版本，不直接绑定工具。工具由技能版本声明并经 SkillGateway 执行。
                      </p>
                    </div>
                  </div>
                ) : (
                  /* versions */
                  <div className="tab-panel">
                    <div className="agent-section">
                      <h3
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          gap: 8,
                        }}
                      >
                        <span>版本历史</span>
                        {sourceOf(selected, meId) !== "official" && selected.status !== "ARCHIVED" && (
                          <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void createVersion(selected.id)}>
                            新建草稿
                          </button>
                        )}
                      </h3>
                      {versions.length === 0 ? (
                        <div className="agent-readonly-banner">暂无版本。</div>
                      ) : (
                        <div className="agent-version-list">
                          {versions.map((v) => {
                            const isCurrent = currentPublishedVersion?.id === v.id;
                            return (
                              <div key={v.id} className={`agent-version-item${isCurrent ? " current" : ""}`}>
                                <div className="v-badge">v{v.version_number}</div>
                                <div className="v-body">
                                  <div className="v-title">
                                    v{v.version_number}
                                    {isCurrent && <span className="agent-tag agent-tag-published">当前发布</span>}
                                    <span className={`agent-tag ${statusInfo(v.status).cls}`}>{statusInfo(v.status).label}</span>
                                  </div>
                                  <div className="v-meta">
                                    {fmtTime(v.published_at)}
                                    {v.config_snapshot && typeof v.config_snapshot.model === "string"
                                      ? ` · 模型 ${v.config_snapshot.model}`
                                      : ""}
                                  </div>
                                </div>
                                <div className="v-actions">
                                  {v.status === "DRAFT" ? (
                                    <>
                                      <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void openEditor(selected.id, v)}>编辑</button>
                                      <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void publishVersion(selected.id, v.id)}>发布</button>
                                      <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void archiveVersion(selected.id, v.id)}>归档</button>
                                    </>
                                  ) : (
                                    <button className="btn btn-secondary btn-sm" onClick={() => void openEditor(selected.id, v)}>查看快照</button>
                                  )}
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                    <div className="agent-alert agent-alert-info">
                      <span>ℹ</span>
                      <span>已发布版本不可原地修改。变更请基于最新版本创建草稿并重新发布。</span>
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </section>
      </div>

      {/* 新建 Agent 弹窗 */}
      <Modal
        open={createOpen}
        title="新建智能体"
        onClose={() => setCreateOpen(false)}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setCreateOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={busy || !name.trim()} onClick={() => void createAgent()}>创建</button>
          </>
        }
      >
        <div className="form-field">
          <label>名称</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="例如：客服助手" />
        </div>
        <div className="form-field">
          <label>说明</label>
          <textarea className="textarea" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="描述该智能体的职责（可选）" />
        </div>
      </Modal>

      {/* 编辑信息弹窗（仅基础信息，不涉及版本） */}
      <Modal
        open={editOpen}
        title="编辑智能体信息"
        onClose={() => setEditOpen(false)}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setEditOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={busy || !editName.trim()} onClick={() => void saveEdit()}>保存</button>
          </>
        }
      >
        <div className="form-field">
          <label>名称</label>
          <input className="input" value={editName} onChange={(e) => setEditName(e.target.value)} />
        </div>
        <div className="form-field">
          <label>说明</label>
          <textarea className="textarea" value={editDescription} onChange={(e) => setEditDescription(e.target.value)} />
        </div>
      </Modal>
    </div>
  );
}
