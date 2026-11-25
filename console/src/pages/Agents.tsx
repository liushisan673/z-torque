import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { request } from "../services/api";
import { errorText } from "../services/errorText";
import { readSelectedId, writeSelectedId } from "../services/selectionMemory";
import type { Agent, AgentVersion, AgentVersionArchiveCheck, BoundSkill } from "../types";
import type { ModelResource } from "../types";
import type { Task } from "../types";
import type { SelectableSkillGroup, SelectableSkillResponse, SelectableSkillVersion } from "../types";
import { Loading } from "../components/Loading";
import { Modal } from "../components/Modal";
import { useToast } from "../components/Toast";
import { GuideCallout } from "../components/GuideCallout";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PaneResizer, usePaneWidth } from "../components/PaneResizer";
import { Pagination, usePager } from "../components/Pagination";
import { useAuth } from "../stores/auth";

// Agent 绑定技能：单个 Agent 版本最多引用的去重工具数（与后端发布闸门一致）
const MAX_AGENT_TOOLS = 30;

// 选中记忆：跳转页面后返回时恢复上次选中的智能体（sessionStorage 按标签页隔离）
const SELECTED_KEY = "zt.agents.selectedId";

type Filter = "all" | "mine" | "official" | "draft";

// 详情区锚点配置：无页签，详情一次纵向展示全部，顶部锚点仅做快捷跳转
const DETAIL_ANCHORS = [
  { id: "overview", label: "概览" },
  { id: "config", label: "配置" },
  { id: "skills", label: "技能绑定" },
  { id: "versions", label: "版本" },
];

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

// 版本归档阻断原因：按后端错误码映射中文文案（避免英文 message 直接外露）
const ARCHIVE_BLOCK_TEXT: Record<string, string> = {
  AI_AGENT_VERSION_IS_CURRENT: "当前发布版本不可归档，请先发布其他版本",
  AI_AGENT_VERSION_IN_USE: "该版本仍有任务在执行，请等待结束或取消后再归档",
  };

function archiveBlockText(reason: { code: string; message: string }): string {
  return ARCHIVE_BLOCK_TEXT[reason.code] ?? reason.message;
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
  // M12-3 B-5：跨页引导——已发布智能体跳会话页使用（携带 agentId 预选），
  // 并消费 /agents?bindSkill= 技能页引导（新建弹窗预选该技能）
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
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
  // 发布预检：缺项时先展示检查结果，允许「仍要发布」强制跳过
  const [publishCheck, setPublishCheck] = useState<{ agentId: string; version: AgentVersion; missing: string[] } | null>(null);

  const [filter, setFilter] = useState<Filter>("all");
  const [keyword, setKeyword] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  // 左右分栏宽度：拖拽把手调整，会话内保持，退出登录恢复默认
  const listPane = usePaneWidth("agent", "--agent-list-w");
  // 顶部锚点导航：当前高亮区块；详情内容纵向滚动一次展示全部（无页签）
  const [activeAnchor, setActiveAnchor] = useState<string | null>("overview");
  const detailBodyRef = useRef<HTMLDivElement | null>(null);

  // 创建 / 编辑
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  // 新建弹窗一步创建并发布：系统规则 + 模型 + 已发布技能版本勾选（M12-2）
  const [createPrompt, setCreatePrompt] = useState("");
  const [createModel, setCreateModel] = useState("");
  const [createLoadingSkills, setCreateLoadingSkills] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");

  // 版本草稿编辑
  const [editing, setEditing] = useState<{ agentId: string; version: AgentVersion } | null>(null);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [modelId, setModelId] = useState("");
  const [skillGroups, setSkillGroups] = useState<SelectableSkillGroup[]>([]);
  const [skillQuery, setSkillQuery] = useState("");
  // 技能绑定选择器：对齐技能页「绑定工具」的弹窗式双栏多选（一次可勾选多个技能版本）
  const [bindSkillOpen, setBindSkillOpen] = useState(false);
  const [skillSource, setSkillSource] = useState<"all" | "official" | "mine">("all");
  const [skillPicks, setSkillPicks] = useState<{ group: SelectableSkillGroup; version: SelectableSkillVersion }[]>([]);
  const [bindingSkills, setBindingSkills] = useState(false);
  const [unbindingSkillId, setUnbindingSkillId] = useState<string | null>(null);
  const { push: toast } = useToast();
  // 危险操作（归档 / 级联删除 / 版本归档）统一走 ConfirmDialog，避免 window.confirm
  const [confirmArchive, setConfirmArchive] = useState<{ id: string; name: string } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{ id: string; name: string } | null>(null);
  const [confirmVersionArchive, setConfirmVersionArchive] = useState<{ agentId: string; versionId: string; versionLabel: string; warnings: string[] } | null>(null);
  const [confirmRollback, setConfirmRollback] = useState<{ agentId: string; versionId: string; label: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await request<Agent[]>(`/api/z-torque/agents${showArchived ? "?include_archived=true" : ""}`);
      setAgents(data);
    } catch (err) {
      setError(errorText(err, "加载失败"));
    } finally {
      setLoading(false);
    }
  }, [showArchived]);

  useEffect(() => {
    void load();
  }, [load]);

  // 默认展示列表第一项，避免主区空白；跳转回来时优先恢复上次选中（记忆 id 已不存在则回退第一项）
  useEffect(() => {
    if (loading || selectedId) return;
    if (agents.length === 0) return;
    const remembered = readSelectedId(SELECTED_KEY);
    const target = (remembered && agents.find((a) => a.id === remembered)) || agents[0];
    void openDetail(target);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, agents, selectedId]);

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

  // 技能选择器：关键字（技能名/说明/版本号）+ 来源（全部/平台/我的）过滤
  const filteredSkillGroups = useMemo(() => {
    const kw = skillQuery.trim().toLowerCase();
    return skillGroups
      .filter((group) => {
        if (skillSource === "official") return group.official;
        if (skillSource === "mine") return !group.official;
        return true;
      })
      .map((group) => {
        if (!kw) return group;
        const groupHit =
          group.skill_name.toLowerCase().includes(kw) || (group.description ?? "").toLowerCase().includes(kw);
        const versions = groupHit
          ? group.versions
          : group.versions.filter((version) => `v${version.version_number}`.includes(kw));
        return { ...group, versions };
      })
      .filter((group) => group.versions.length > 0);
  }, [skillGroups, skillQuery, skillSource]);

  // M12 后列表统一分页：前端切片 + 公共分页条（新建弹窗与绑定技能弹窗各自独立分页，均对分组数组每页 10 组）
  const createSkillPager = usePager(filteredSkillGroups, 10);
  const bindSkillPager = usePager(filteredSkillGroups, 10);


  // 已绑定技能的工具数合计（按技能计；去重后的最终校验在发布时执行）
  const boundToolTotal = useMemo(() => {
    const toolCountByVersion = new Map<string, number>();
    skillGroups.forEach((group) => group.versions.forEach((version) => toolCountByVersion.set(version.skill_version_id, version.tool_count)));
    return boundSkills.reduce((sum, skill) => sum + (toolCountByVersion.get(skill.skill_version_id) ?? 0), 0);
  }, [skillGroups, boundSkills]);

  const boundSkillVersionIds = useMemo(() => new Set(boundSkills.map((skill) => skill.skill_version_id)), [boundSkills]);

  // 本次已选技能的工具数合计与绑定后预估（对照单版本工具上限，发布时后端最终校验）
  const pickedToolTotal = useMemo(
    () => skillPicks.reduce((sum, pick) => sum + pick.version.tool_count, 0),
    [skillPicks],
  );
  const projectedToolTotal = boundToolTotal + pickedToolTotal;

  // 锚点跳转：平滑滚动到对应区块（CSS scroll-margin-top 预留吸顶导航高度）
  function scrollToSection(id: string) {
    setActiveAnchor(id);
    document.getElementById(`agent-sec-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // 滚动跟随：按区块相对滚动容器顶部的位置高亮当前锚点，触底时强制高亮最后一项
  function handleDetailScroll() {
    const container = detailBodyRef.current;
    if (!container) return;
    const ids = DETAIL_ANCHORS.map((a) => a.id);
    const containerTop = container.getBoundingClientRect().top;
    let current = ids[0] ?? null;
    for (const id of ids) {
      const el = document.getElementById(`agent-sec-${id}`);
      if (!el) continue;
      if (el.getBoundingClientRect().top - containerTop <= 96) current = id;
    }
    if (container.scrollTop + container.clientHeight >= container.scrollHeight - 4) {
      current = ids[ids.length - 1] ?? current;
    }
    setActiveAnchor(current);
  }

  async function openDetail(a: Agent) {
    setSelectedId(a.id);
    writeSelectedId(SELECTED_KEY, a.id);
    setVersions([]);
    setBoundSkills([]);
    setLatestTask(null);
    setActiveAnchor("overview");
    if (detailBodyRef.current) detailBodyRef.current.scrollTop = 0;
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
      setError(errorText(err, "加载版本失败"));
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
      setError(errorText(err, "加载技能失败"));
      setBoundSkills([]);
    } finally {
      setLoadingSkills(false);
    }
  }

  // 打开新建弹窗：重置表单并加载已发布技能版本（勾选后随创建一并绑定并发布）；
  // M12-3 B-5：携带 ?bindSkill=<skill_id> 时（技能页引导跳转）自动预选该技能的最新发布版本
  async function openCreate() {
    setCreateOpen(true);
    setName("");
    setDescription("");
    setCreatePrompt("");
    setCreateModel("");
    setSkillPicks([]);
    setSkillQuery("");
    setSkillSource("all");
    setCreateLoadingSkills(true);
    try {
      const groups = await loadSelectableSkills();
      const bindSkillParam = searchParams.get("bindSkill");
      if (bindSkillParam) {
        setSearchParams({}, { replace: true });
        const picks: { group: SelectableSkillGroup; version: SelectableSkillVersion }[] = [];
        for (const g of groups) {
          if (g.skill_id !== bindSkillParam) continue;
          // versions 按 version_number 倒序返回，首项即最新发布版本
          const ver = g.versions[0];
          if (ver) picks.push({ group: g, version: ver });
        }
        if (picks.length > 0) setSkillPicks(picks);
        else toast("info", "引导指定的技能暂无已发布版本，可到技能页完成发布后再绑定");
      }
    } finally {
      setCreateLoadingSkills(false);
    }
  }

  // 一步创建并发布：创建智能体 + 首版本 + 绑定技能 + 发布一次提交（后端同事务，失败整体回滚）
  async function createAgent() {
    if (!name.trim() || !createModel) return;
    setBusy(true);
    setError(null);
    try {
      const snapshot: Record<string, unknown> = { system_prompt: createPrompt.trim(), model: createModel };
      await request("/api/z-torque/agents", {
        method: "POST",
        body: {
          name: name.trim(),
          description: description.trim() || null,
          publish: true,
          config_snapshot: snapshot,
          skill_version_ids: skillPicks.map((pick) => pick.version.skill_version_id),
        },
      });
      setCreateOpen(false);
      setName("");
      setDescription("");
      setCreatePrompt("");
      setCreateModel("");
      setSkillPicks([]);
      toast("success", "智能体已创建并发布，可到「会话」选择使用");
      await load();
    } catch (err) {
      setError(errorText(err, "创建失败"));
    } finally {
      setBusy(false);
    }
  }

  async function copyAgent(id: string) {
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${id}/copy`, { method: "POST" });
      toast("success", "已复制智能体");
      await load();
    } catch (err) {
      setError(errorText(err, "复制失败"));
    } finally {
      setBusy(false);
    }
  }

  async function archiveAgent(id: string) {
    if (!confirmArchive || confirmArchive.id !== id) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${id}/archive`, { method: "POST" });
      toast("success", "已归档");
      await load();
      if (selectedId === id) setSelectedId(null);
    } catch (err) {
      setError(errorText(err, "归档失败"));
    } finally {
      setBusy(false);
      setConfirmArchive(null);
    }
  }

  // M12-4：设为默认智能体（同租户互斥，后端自动清掉旧默认）；外部接入未指定目标时优先使用
  async function setDefaultAgent(agent: Agent) {
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${agent.id}/set-default`, { method: "POST" });
      await load();
      if (selectedId === agent.id) await openDetail(agent);
      toast("success", "已设为默认智能体，会话页将优先选用");
    } catch (err) {
      setError(errorText(err, "设置默认失败"));
    } finally {
      setBusy(false);
    }
  }

  async function deleteAgent(id: string) {
    if (!confirmDelete || confirmDelete.id !== id) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${id}`, { method: "DELETE" });
      if (selectedId === id) setSelectedId(null);
      await load();
      toast("success", "已删除");
    } catch (err) {
      setError(errorText(err, "删除失败"));
    } finally {
      setBusy(false);
      setConfirmDelete(null);
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
      setError(errorText(err, "保存失败"));
    } finally {
      setBusy(false);
    }
  }

  // 版本归档：先预检（运行中任务 / 当前发布版本）→ 阻断则顶部提示，通过则确认后归档
  async function archiveVersion(agentId: string, versionId: string) {
    setBusy(true);
    setError(null);
    try {
      const check = await request<AgentVersionArchiveCheck>(`/api/z-torque/agents/${agentId}/versions/${versionId}/archive-check`);
      if (!check.can_archive) {
        setError(check.reasons.map(archiveBlockText).join("；"));
        return;
      }
      const warnings = check.warnings.map((w) => w.message);
      const versionLabel = "历史版本";
      setConfirmVersionArchive({ agentId, versionId, versionLabel, warnings });
    } catch (err) {
      setError(errorText(err, "归档版本预检失败"));
    } finally {
      setBusy(false);
    }
  }

  // 版本归档确认后的实际归档（与 archiveVersion 解耦，避免嵌套确认窗）
  async function confirmArchiveVersion() {
    if (!confirmVersionArchive) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${confirmVersionArchive.agentId}/versions/${confirmVersionArchive.versionId}/archive`, { method: "POST" });
      toast("success", "版本已归档");
      if (selected) await openDetail(selected);
      await load();
    } catch (err) {
      setError(errorText(err, "归档版本失败"));
    } finally {
      setBusy(false);
      setConfirmVersionArchive(null);
    }
  }

  async function confirmRollbackVersion() {
    if (!confirmRollback) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${confirmRollback.agentId}/versions/${confirmRollback.versionId}/rollback`, { method: "POST" });
      toast("success", `已切换到 ${confirmRollback.label}`);
      if (selected) await openDetail(selected);
      await load();
    } catch (err) {
      setError(errorText(err, "版本回滚失败"));
    } finally {
      setBusy(false);
      setConfirmRollback(null);
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
      toast("success", "已创建草稿版本，请编辑后发布");
    } catch (err) {
      setError(errorText(err, "创建版本失败"));
    } finally {
      setBusy(false);
    }
  }

  // 可选技能版本：后端按技能分组一次性返回，替代逐技能详情请求（避免 N+1）；
  // 失败时置空列表（选择器显示空态），不阻塞绑定主流程；返回分组供引导预填使用
  async function loadSelectableSkills(): Promise<SelectableSkillGroup[]> {
    try {
      const data = await request<SelectableSkillResponse>("/api/z-torque/skills-tools/skill-versions/selectable?size=200");
      setSkillGroups(data.groups);
      return data.groups;
    } catch {
      setSkillGroups([]);
      return [];
    }
  }

  async function openEditor(agentId: string, version: AgentVersion) {
    setError(null);
    setEditing({ agentId, version });
    scrollToSection("config");
    const cfg = version.config_snapshot ?? {};
    setSystemPrompt(typeof cfg.system_prompt === "string" ? cfg.system_prompt : "");
    setModelId(typeof cfg.model === "string" ? cfg.model : "");
    setBindSkillOpen(false);
    setSkillPicks([]);
    setSkillQuery("");
    setSkillSource("all");
    try {
      await loadSelectableSkills();
      await loadBoundSkills(agentId, version.id);
    } catch (err) {
      setError(errorText(err, "加载技能失败"));
      setSkillGroups([]);
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
        },
      });
      setEditing({ agentId: editing.agentId, version: v });
      if (selected) await openDetail(selected);
      setError(null);
      toast("success", "配置已保存");
    } catch (err) {
      setError(errorText(err, "保存失败"));
    } finally {
      setBusy(false);
    }
  }

  function openSkillBindModal() {
    setBindSkillOpen(true);
    setSkillPicks([]);
    setSkillQuery("");
    setSkillSource("all");
    void loadSelectableSkills();
  }

  // 勾选/取消一个技能版本；工具合计超上限时拦截（与技能页选择器同款守卫）
  function toggleSkillPick(group: SelectableSkillGroup, version: SelectableSkillVersion) {
    if (boundSkillVersionIds.has(version.skill_version_id)) return;
    setSkillPicks((prev) => {
      if (prev.some((pick) => pick.version.skill_version_id === version.skill_version_id)) {
        return prev.filter((pick) => pick.version.skill_version_id !== version.skill_version_id);
      }
      if (boundToolTotal + prev.reduce((sum, pick) => sum + pick.version.tool_count, 0) >= MAX_AGENT_TOOLS) {
        toast("info", `绑定技能的工具合计不能超过 ${MAX_AGENT_TOOLS} 个`);
        return prev;
      }
      return [...prev, { group, version }];
    });
  }

  async function bindSkills() {
    if (!editing || skillPicks.length === 0) return;
    setBindingSkills(true);
    setError(null);
    try {
      // 顺序提交：与技能页绑定工具一致，保持已选区顺序
      for (const pick of skillPicks) {
        await request(`/api/z-torque/agents/${editing.agentId}/versions/${editing.version.id}/skills`, {
          method: "POST",
          body: { skill_version_id: pick.version.skill_version_id },
        });
      }
      const count = skillPicks.length;
      setBindSkillOpen(false);
      await loadBoundSkills(editing.agentId, editing.version.id);
      await loadSelectableSkills();
      toast("success", `已绑定 ${count} 个技能`);
    } catch (err) {
      setError(errorText(err, "绑定技能失败"));
      // 顺序提交可能部分成功：刷新已绑定列表，避免界面与实际状态不一致
      await loadBoundSkills(editing.agentId, editing.version.id);
      await loadSelectableSkills();
    } finally {
      setBindingSkills(false);
    }
  }

  async function detachSkill(skillVersionId: string) {
    if (!editing) return;
    setUnbindingSkillId(skillVersionId);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${editing.agentId}/versions/${editing.version.id}/skills/${skillVersionId}`, {
        method: "DELETE",
      });
      await loadBoundSkills(editing.agentId, editing.version.id);
      await loadSelectableSkills();
      toast("success", "已解绑");
    } catch (err) {
      setError(errorText(err, "解绑失败"));
    } finally {
      setUnbindingSkillId(null);
    }
  }

  // 发布前预检：把"对话跑不起来"的问题拦在发布前（可强制跳过）
  function publishVersion(agentId: string, version: AgentVersion) {
    const snapshot = version.config_snapshot ?? {};
    const missing: string[] = [];
    const model = typeof snapshot.model === "string" ? snapshot.model.trim() : "";
    if (!model) missing.push("未绑定模型——对话将无法调用大模型");
    const sp = typeof snapshot.system_prompt === "string" ? snapshot.system_prompt.trim() : "";
    if (!sp) missing.push("系统规则为空——智能体将缺少人设与行为约束");
    if (missing.length > 0) {
      setPublishCheck({ agentId, version, missing });
      return;
    }
    void doPublish(agentId, version.id);
  }

  async function doPublish(agentId: string, versionId: string) {
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/agents/${agentId}/versions/${versionId}/publish`, { method: "POST" });
      setEditing(null);
      if (selected) await openDetail(selected);
      await load();
      toast("success", "版本已发布，可到「会话」开始对话");
    } catch (err) {
      setError(errorText(err, "发布失败"));
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
          <button className="agent-btn-danger-outline" disabled={busy} onClick={() => setConfirmDelete({ id: selected.id, name: selected.name })}>删除</button>
        </>
      );
    }
    if (status === "DRAFT") {
      return (
        <>
          <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => openEdit(selected)}>继续编辑</button>
          <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void createVersion(selected.id)}>新建版本</button>
          <button className="agent-btn-danger-outline" disabled={busy} onClick={() => setConfirmArchive({ id: selected.id, name: selected.name })}>归档</button>
        </>
      );
    }
    // 已发布
    return (
      <>
        <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => openEdit(selected)}>编辑信息</button>
        <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void createVersion(selected.id)}>新建草稿版本</button>
        {selected.is_default ? (
          <button className="btn btn-secondary btn-sm" disabled title="当前已是默认智能体">当前默认</button>
        ) : (
          <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void setDefaultAgent(selected)} title="设为默认后，外部接入未指定目标时优先使用">设为默认</button>
        )}
        <button
          className="btn btn-primary btn-sm"
          disabled={!selected.current_published_version_id}
          onClick={() => navigate(`/chat?agentId=${selected.id}`)}
          title="前往会话页，会自动选中该智能体"
        >
          前往会话页
        </button>
        <button className="agent-btn-danger-outline" disabled={busy} onClick={() => setConfirmArchive({ id: selected.id, name: selected.name })}>归档</button>
      </>
    );
  }, [selected, busy]);

  // M12 后列表统一分页：前端切片 + 公共分页条（左侧智能体列表与详情版本列表，均每页 10 条）
  const agentPager = usePager(filteredAgents, 10);
  const versionPager = usePager(versions, 10);

  return (
    <div className="agent-page">
      {error && (
        <div className="agent-alert agent-alert-error">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      <div className="agent-shell" style={listPane.style}>
        {/* 左侧列表 */}
        <aside className="agent-list-pane">
          <div className="agent-list-header">
            <h1>智能体</h1>
            <div className="agent-list-actions">
              <button className="btn btn-primary" type="button" onClick={() => void openCreate()}>
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
                <GuideCallout
                  title="还没有智能体"
                  why="创建后需绑定模型并发布版本，才能在会话中使用"
                  actionLabel="新建智能体"
                  onAction={() => void openCreate()}
                  hint="推荐路径：新建智能体（可勾选已发布技能）→ 创建并发布 → 到会话使用"
                />
              </div>
            ) : (
              agentPager.paged.map((a) => {
                const isActive = a.id === selectedId;
                const isOfficial = sourceOf(a, meId) === "official";
                const si = statusInfo(a.status);
                const curVer = versions.find((v) => v.id === a.current_published_version_id);
                const verText = a.status === "DRAFT" ? "草稿" : curVer ? `当前 v${curVer.version_number}` : "未发布";
                return (
                  <div
                    key={a.id}
                    className={`agent-card${isActive ? " active" : ""}`}
                    role="button"
                    tabIndex={0}
                    aria-pressed={isActive}
                    onClick={() => void openDetail(a)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        void openDetail(a);
                      }
                    }}
                  >
                    <div className="row1">
                      <span className="name">{a.name}{a.is_default_parent && " · 默认父"}{!a.is_default_parent && a.is_default && " · 默认"}</span>
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
          {/* M12 后列表统一分页：前端切片 + 公共分页条（列表本身继续滚动，分页条固定在滚动容器外底部） */}
          <Pagination
            total={agentPager.total}
            page={agentPager.page}
            pageSize={agentPager.pageSize}
            onChange={agentPager.onChange}
          />
        </aside>

        {/* 左右分栏拖拽把手 */}
        <PaneResizer onPreview={listPane.previewWidth} onCommit={listPane.commitWidth} />

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

              <div className="agent-detail-body" ref={detailBodyRef} onScroll={handleDetailScroll}>
                {loadingVersions ? (
                  <Loading />
                ) : (
                  <>
                    {/* 顶部锚点导航：快捷跳转，非页签；详情内容纵向一次展示全部 */}
                    <nav className="skill-anchor-nav" aria-label="详情锚点导航">
                      {DETAIL_ANCHORS.map((a) => (
                        <button
                          key={a.id}
                          type="button"
                          className={`skill-anchor-link${activeAnchor === a.id ? " active" : ""}`}
                          onClick={() => scrollToSection(a.id)}
                        >
                          {a.label}
                        </button>
                      ))}
                    </nav>

                    {/* 概览 */}
                    <section id="agent-sec-overview" className="skill-anchor-section">
                  <div className="tab-panel">
                    <div className="agent-section">
                      <h3>基本信息</h3>
                      <div className="agent-form-grid">
                        <div className="agent-field">
                          <label>名称</label>
                          <input type="text" value={selected.name} disabled />
                        </div>
                      <div className="agent-field">
                          <label>创建时间</label>
                          <input type="text" value={fmtTime(selected.created_at)} disabled />
                        </div>
                        <div className="agent-field full">
                          <label>描述</label>
                          <textarea value={selected.description ?? ""} disabled />
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
                          <label>最近使用</label>
                          <input
                            type="text"
                            value={latestTask ? `${fmtTime(latestTask.created_at)} · ${latestTask.request_summary || "任务"}` : "—"}
                            disabled
                          />
                        </div>
                      </div>
                    </div>

                  </div>
                    </section>

                    {/* 配置 */}
                    <section id="agent-sec-config" className="skill-anchor-section">
                  <div className="tab-panel">
                    {editing ? (
                      <div className="agent-alert agent-alert-info">
                        <span>正在编辑草稿版本 v{editing.version.version_number}，保存后可发布为新版本。</span>
                      </div>
                    ) : sourceOf(selected, meId) === "official" ? (
                      <div className="agent-alert agent-alert-warn">
                        <span>当前查看的是平台智能体的只读快照。修改前须先「复制到我的」生成个人副本。</span>
                      </div>
                    ) : (
                      <div>
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
                            {models.map((m) => {
                              // 健康标注：供应商缺密钥的模型选了也跑不通，直接在选项里提示
                              const noSecret = m.provider ? m.provider.secret_configured === false : false;
                              return (
                                <option key={m.id} value={m.model_identifier || m.id}>
                                  {m.name}{m.model_identifier ? `（${m.model_identifier}）` : ""}{noSecret ? " · 密钥未配置" : ""}
                                </option>
                              );
                            })}
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
                      <h3>A2A 智能体协作</h3>
                      <div className="agent-alert agent-alert-info">
                        <span>
                          智能体通过统一 A2A 发现与委派机制动态协作，无需在创建或发布版本时预先配置父子关系或子智能体白名单。
                          每次调用都会重新校验当前用户对目标智能体及其资源、操作的访问权限。
                        </span>
                      </div>
                    </div>

                    {editing && (
                      <div className="agent-section" style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                        <button className="btn btn-secondary" disabled={busy} onClick={() => void saveVersion()}>保存草稿</button>
                        <button className="btn btn-primary" disabled={busy} onClick={() => publishVersion(editing.agentId, editing.version)}>发布新版本</button>
                        <button className="btn btn-secondary" onClick={() => setEditing(null)}>取消</button>
                      </div>
                    )}
                  </div>
                    </section>

                    {/* 技能绑定 */}
                    <section id="agent-sec-skills" className="skill-anchor-section">
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
                              {editing && <th>操作</th>}
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
                                  {editing && (
                                    <td>
                                      <button
                                        className="btn btn-ghost btn-sm"
                                        disabled={busy || unbindingSkillId === s.skill_version_id}
                                        onClick={() => void detachSkill(s.skill_version_id)}
                                      >
                                        {unbindingSkillId === s.skill_version_id ? "解绑中…" : "解绑"}
                                      </button>
                                    </td>
                                  )}
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      )}

                      {editing && (
                        <div className="agent-skill-actions" style={{ marginTop: 12 }}>
                          <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => openSkillBindModal()}>绑定技能</button>
                          <span className={projectedToolTotal > MAX_AGENT_TOOLS ? "skill-bind-limit danger" : "skill-bind-limit"}>
                            工具合计 {boundToolTotal} / {MAX_AGENT_TOOLS}
                          </span>
                        </div>
                      )}

                     
                    </div>
                  </div>
                    </section>

                    {/* 版本 */}
                    <section id="agent-sec-versions" className="skill-anchor-section">
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
                        <>
                        {/* M12 后列表统一分页：前端切片 + 公共分页条（放在版本列表下方） */}
                        <div className="agent-version-list">
                          {versionPager.paged.map((v) => {
                            const isCurrent = currentPublishedVersion?.id === v.id;
                            const readOnly = sourceOf(selected, meId) === "official" || selected.status === "ARCHIVED";
                            const archived = v.status === "ARCHIVED";
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
                                  {v.status === "DRAFT" && (
                                    <>
                                      <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void openEditor(selected.id, v)}>编辑</button>
                                      <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => publishVersion(selected.id, v)}>发布</button>
                                    </>
                                  )}
                                  {v.status !== "DRAFT" && (
                                    <button className="btn btn-secondary btn-sm" onClick={() => void openEditor(selected.id, v)}>查看快照</button>
                                  )}
                                  {!readOnly && !archived && (
                                    isCurrent ? (
                                      <button className="btn btn-secondary btn-sm" disabled title="当前发布版本不可归档，请先发布其他版本">归档</button>
                                    ) : (
                                      <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void archiveVersion(selected.id, v.id)}>归档</button>
                                    )
                                  )}
                                  {!readOnly && !isCurrent && (v.status === "PUBLISHED" || archived) && (
                                    <button
                                      className="btn btn-secondary btn-sm"
                                      disabled={busy}
                                      onClick={() => setConfirmRollback({ agentId: selected.id, versionId: v.id, label: `v${v.version_number}` })}
                                    >
                                      回滚至此版本
                                    </button>
                                  )}
                                </div>
                              </div>
                            );
                          })}
                        </div>
                        {/* M12 后列表统一分页：前端切片 + 公共分页条（放在版本列表下方） */}
                        <Pagination
                          total={versionPager.total}
                          page={versionPager.page}
                          pageSize={versionPager.pageSize}
                          onChange={versionPager.onChange}
                        />
                        </>
                      )}
                    </div>
                    <div className="agent-alert agent-alert-info">
                      <span>ℹ</span>
                      <span>已发布版本不可原地修改。变更请基于最新版本创建草稿并重新发布。</span>
                    </div>
                  </div>
                    </section>
                  </>
                )}
              </div>
            </>
          )}
        </section>
      </div>

      {/* 新建 Agent 弹窗：一步创建并发布（创建 + 首版本 + 绑定技能 + 发布） */}
      <Modal
        open={createOpen}
        title="新建智能体"
        onClose={() => setCreateOpen(false)}
        width={640}
        footer={
          <>
            <span className="skill-bind-count">
              {skillPicks.length > 0 ? `已选 ${skillPicks.length} 个技能 · 工具合计 ${skillPicks.reduce((sum, pick) => sum + pick.version.tool_count, 0)} / ${MAX_AGENT_TOOLS}` : "保存后立即可在「会话」使用"}
            </span>
            <button className="btn btn-secondary" onClick={() => setCreateOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={busy || !name.trim() || !createModel} onClick={() => void createAgent()}>
              {busy ? "发布中…" : "创建并发布"}
            </button>
          </>
        }
      >
        <div className="agent-alert agent-alert-info">
          <span>创建后会立即发布首个版本。先填写名称、模型和系统规则即可开始；技能绑定为可选项，委派与运行策略可在后续草稿版本中配置。</span>
        </div>
        <div className="form-field">
          <label>名称</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="例如：客服助手" />
        </div>
        <div className="form-field">
          <label>说明</label>
          <textarea className="textarea" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="描述该智能体的职责（可选）" />
        </div>
        <div className="form-field">
          <label>供应商 / 模型</label>
          <select className="input" value={createModel} onChange={(e) => setCreateModel(e.target.value)}>
            <option value="">请选择模型</option>
            {models.map((m) => {
              // 健康标注：供应商缺密钥的模型选了也跑不通，直接在选项里提示
              const noSecret = m.provider ? m.provider.secret_configured === false : false;
              return (
                <option key={m.id} value={m.model_identifier || m.id}>
                  {m.name}{m.model_identifier ? `（${m.model_identifier}）` : ""}{noSecret ? " · 密钥未配置" : ""}
                </option>
              );
            })}
          </select>
        </div>
        <div className="form-field">
          <label>系统规则</label>
          <textarea className="textarea" value={createPrompt} onChange={(e) => setCreatePrompt(e.target.value)} placeholder="定义智能体的人设与行为约束（可选，后续可在版本编辑中完善）" />
        </div>
        <div className="form-field">
          <label>绑定技能（可选，仅已发布版本）</label>
          {createLoadingSkills ? (
            <div className="skill-readonly-banner">技能加载中…</div>
          ) : filteredSkillGroups.length === 0 ? (
            <div className="skill-readonly-banner">当前没有可绑定的已发布技能版本，可稍后在版本编辑中绑定。</div>
          ) : (
            <>
            {/* M12 后列表统一分页：对分组数组前端切片（每页 10 组）+ 公共分页条 */}
            <div className="skill-bind-list" style={{ maxHeight: 220, overflowY: "auto" }}>
              {createSkillPager.paged.map((group) => (
                <div key={group.skill_id} className="skill-bind-group">
                  <div className="skill-bind-group-title">
                    <span>
                      {group.skill_name}
                      {group.official && <span className="skill-tag skill-tag-official">平台</span>}
                    </span>
                    <span className="hint">{group.versions.length} 个已发布版本</span>
                  </div>
                  {group.versions.map((version) => {
                    const picked = skillPicks.some((pick) => pick.version.skill_version_id === version.skill_version_id);
                    const projected = skillPicks.reduce((sum, pick) => sum + pick.version.tool_count, 0);
                    const disabled = !picked && projected + version.tool_count > MAX_AGENT_TOOLS;
                    return (
                      <label
                        key={version.skill_version_id}
                        className={`skill-tool-picker-item${picked ? " selected" : ""}${disabled ? " disabled" : ""}`}
                        title={version.orchestration_hint ?? undefined}
                      >
                        <input
                          type="checkbox"
                          disabled={disabled || busy}
                          checked={picked}
                          onChange={() => toggleSkillPick(group, version)}
                        />
                        <div className="t-main">
                          <div className="t-row">
                            <span className="t-name">v{version.version_number}</span>
                            <span className="skill-tag skill-tag-neutral">{version.mode === "script" ? "脚本" : "纯指令"}</span>
                            <span className="skill-tag skill-tag-neutral">{version.tool_count} 个工具</span>
                            {version.requires_confirmation && <span className="skill-tag skill-tag-confirm">需审批</span>}
                          </div>
                          <div className="t-desc">{group.description || version.orchestration_hint || "—"}</div>
                        </div>
                      </label>
                    );
                  })}
                </div>
              ))}
            </div>
            {/* M12 后列表统一分页：前端切片 + 公共分页条（放在勾选列表下方） */}
            <Pagination
              total={createSkillPager.total}
              page={createSkillPager.page}
              pageSize={createSkillPager.pageSize}
              onChange={createSkillPager.onChange}
            />
            </>
          )}
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

      {/* 绑定技能选择器：与技能页「绑定工具」同款的弹窗式双栏多选 */}
      <Modal
        open={bindSkillOpen}
        title="绑定技能（已发布版本）"
        onClose={() => setBindSkillOpen(false)}
        width={880}
        footer={
          <>
            <span className="skill-bind-count">
              已绑定 {boundSkills.length} · 本次已选 {skillPicks.length} · 工具合计 {projectedToolTotal} / {MAX_AGENT_TOOLS}
            </span>
            <button className="btn btn-secondary" onClick={() => setBindSkillOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={bindingSkills || skillPicks.length === 0} onClick={() => void bindSkills()}>
              {bindingSkills ? "绑定中…" : `绑定 ${skillPicks.length || ""}`.trim()}
            </button>
          </>
        }
      >
        <div className="skill-bind-toolbar">
          <input
            className="input"
            value={skillQuery}
            onChange={(e) => setSkillQuery(e.target.value)}
            placeholder="搜索技能名称、说明或版本"
            aria-label="搜索技能"
          />
          <select
            className="input"
            value={skillSource}
            onChange={(e) => setSkillSource(e.target.value as "all" | "official" | "mine")}
            aria-label="按来源过滤"
          >
            <option value="all">全部来源</option>
            <option value="official">平台</option>
            <option value="mine">我的</option>
          </select>
        </div>

        {filteredSkillGroups.length === 0 ? (
          <div className="skill-readonly-banner">当前没有可绑定的已发布技能版本（或调整搜索条件）。</div>
        ) : (
          <div className="skill-bind-layout">
            <div className="skill-bind-list">
              {/* M12 后列表统一分页：对分组数组前端切片（每页 10 组） */}
              {bindSkillPager.paged.map((group) => (
                <div key={group.skill_id} className="skill-bind-group">
                  <div className="skill-bind-group-title">
                    <span>
                      {group.skill_name}
                      {group.official && <span className="skill-tag skill-tag-official">平台</span>}
                    </span>
                    <span className="hint">{group.versions.length} 个已发布版本</span>
                  </div>
                  {group.versions.map((version) => {
                    const already = boundSkillVersionIds.has(version.skill_version_id);
                    const picked = skillPicks.some((pick) => pick.version.skill_version_id === version.skill_version_id);
                    const disabled = already || (!picked && projectedToolTotal >= MAX_AGENT_TOOLS);
                    return (
                      <label
                        key={version.skill_version_id}
                        className={`skill-tool-picker-item${picked ? " selected" : ""}${already ? " disabled" : ""}`}
                        title={already ? "已绑定当前版本" : (version.orchestration_hint ?? undefined)}
                      >
                        <input
                          type="checkbox"
                          disabled={disabled || bindingSkills}
                          checked={picked || already}
                          onChange={() => toggleSkillPick(group, version)}
                        />
                        <div className="t-main">
                          <div className="t-row">
                            <span className="t-name">v{version.version_number}</span>
                            <span className="skill-tag skill-tag-neutral">{version.mode === "script" ? "脚本" : "纯指令"}</span>
                            <span className="skill-tag skill-tag-neutral">{version.tool_count} 个工具</span>
                            {version.requires_confirmation && <span className="skill-tag skill-tag-confirm">需审批</span>}
                            {already && <span className="t-bound">已绑定</span>}
                          </div>
                          <div className="t-desc">{group.description || version.orchestration_hint || "—"}</div>
                        </div>
                      </label>
                    );
                  })}
                </div>
              ))}
            </div>

            <div className="skill-bind-picked">
              <div className="skill-bind-group-title">
                <span>本次已选</span>
                <span className={projectedToolTotal > MAX_AGENT_TOOLS ? "skill-bind-limit danger" : "skill-bind-limit"}>
                  工具合计 {projectedToolTotal} / {MAX_AGENT_TOOLS}
                </span>
              </div>
              {skillPicks.length === 0 ? (
                <div className="skill-readonly-banner">从左侧勾选技能版本；一次可绑定多个。</div>
              ) : (
                skillPicks.map((pick) => (
                  <div key={pick.version.skill_version_id} className="skill-bind-pick-card">
                    <div className="pick-head">
                      <span className="t-name">{pick.group.skill_name}</span>
                      <span className="skill-tag skill-tag-neutral">v{pick.version.version_number}</span>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => setSkillPicks((prev) => prev.filter((item) => item.version.skill_version_id !== pick.version.skill_version_id))}
                        aria-label={`移除 ${pick.group.skill_name}`}
                      >
                        ✕
                      </button>
                    </div>
                    <div className="t-desc">
                      {pick.version.tool_count} 个工具
                      {pick.version.orchestration_hint ? ` · ${pick.version.orchestration_hint}` : ""}
                    </div>
                  </div>
                ))
              )}
            </div>
            {/* M12 后列表统一分页：前端切片 + 公共分页条（放在弹窗列表区域底部） */}
            <Pagination
              total={bindSkillPager.total}
              page={bindSkillPager.page}
              pageSize={bindSkillPager.pageSize}
              onChange={bindSkillPager.onChange}
            />
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={confirmRollback !== null}
        title="回滚 Agent 版本？"
        confirmText="确认回滚"
        busy={busy}
        onCancel={() => setConfirmRollback(null)}
        onConfirm={() => void confirmRollbackVersion()}
      >
        <p>将当前发布指针切换到 {confirmRollback?.label ?? "所选版本"}。版本配置快照不会被修改，正在执行的任务也不会切换版本。</p>
      </ConfirmDialog>

      {/* 危险操作三连：归档 / 级联删除 / 版本归档（含告警） */}
      <ConfirmDialog
        open={confirmArchive !== null}
        title="归档该智能体？"
        confirmText="归档"
        busy={busy}
        onCancel={() => setConfirmArchive(null)}
        onConfirm={() => { if (confirmArchive) void archiveAgent(confirmArchive.id); }}
      >
        <p>智能体「{confirmArchive?.name ?? ""}」将进入「已归档」状态，不再被会话选用，但已发布版本与配置都会保留。</p>
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmDelete !== null}
        title="删除该智能体？"
        confirmText="删除"
        destructive
        requireText={confirmDelete?.name ?? ""}
        requireLabel="请输入智能体名称以确认删除"
        busy={busy}
        onCancel={() => setConfirmDelete(null)}
        onConfirm={() => { if (confirmDelete) void deleteAgent(confirmDelete.id); }}
      >
        <p>智能体「{confirmDelete?.name ?? ""}」的<strong>全部版本与技能绑定将被级联删除</strong>，且不可恢复。如不再使用，建议改用「归档」。</p>
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmVersionArchive !== null}
        title="归档该版本？"
        confirmText="归档"
        busy={busy}
        onCancel={() => setConfirmVersionArchive(null)}
        onConfirm={() => void confirmArchiveVersion()}
      >
        <p>归档后该版本不再被任何智能体选用，但代码与配置保留。</p>
        {confirmVersionArchive && confirmVersionArchive.warnings.length > 0 && (
          <ul style={{ margin: "8px 0 0 18px", padding: 0, color: "var(--warning, #b45309)" }}>
            {confirmVersionArchive.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        )}
      </ConfirmDialog>

      {/* 发布预检确认：缺项清单 + 强制发布出口 */}
      <ConfirmDialog
        open={publishCheck !== null}
        title="发布前检查未通过"
        confirmText="仍要发布"
        cancelText="返回修改"
        busy={busy}
        onCancel={() => setPublishCheck(null)}
        onConfirm={() => {
          if (publishCheck) {
            const { agentId, version } = publishCheck;
            setPublishCheck(null);
            void doPublish(agentId, version.id);
          }
        }}
      >
        <p>以下配置项缺失，发布后对话可能无法正常工作：</p>
        <ul style={{ margin: "8px 0", paddingLeft: 20 }}>
          {publishCheck?.missing.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
        <p style={{ color: "var(--text-muted)", fontSize: 13 }}>可先「返回修改」补全配置；确认无误也可仍按当前草稿发布。</p>
      </ConfirmDialog>
    </div>
  );
}
