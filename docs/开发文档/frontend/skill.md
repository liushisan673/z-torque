import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { request, uploadFile } from "../services/api";
import { errorText } from "../services/errorText";
import { readSelectedId, writeSelectedId } from "../services/selectionMemory";
import type { SkillListItem, SkillDetail, SkillVersionDetail, SkillToolLink, SkillVersionRef, SelectableTool, SelectableToolGroup, SelectableToolResponse } from "../types";
import { Loading } from "../components/Loading";
import { Modal } from "../components/Modal";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useToast } from "../components/Toast";
import { GuideCallout } from "../components/GuideCallout";
import { isAdminUser } from "../components/AppTopbar";
import { useAuth } from "../stores/auth";

// 选中记忆：跳转页面后返回时恢复上次选中的技能（sessionStorage 按标签页隔离）
const SELECTED_KEY = "zt.skills.selectedId";

type Tab = "overview" | "content" | "scripts" | "load" | "tools" | "versions";

type BindPick = {
  tool: SelectableTool;
  tool_version_id: string;
  version_number: number;
  role: string;
  note: string;
};

const BIND_ROLE_LABELS: Record<string, string> = {
  prerequisite: "前置",
  normal: "常规",
  followup: "后续",
};
// 筛选维度对齐 skill.html 规范：全部 / MD-Skill / 纯指令 / 脚本 / 待确认启用
type Filter = "all" | "md" | "instruction" | "script" | "pending";

// md-skills 同步接口返回（POST /skills/scan）
interface ScanIssue {
  path: string;
  message: string;
  code: string;
}
interface ScanResult {
  discovered: number;
  drafts_created: number;
  unchanged: number;
  issues: ScanIssue[];
}
// 扫描状态（GET /skills/scan/status，watchdog 实时状态）
interface ScanStatus {
  running: boolean;
  last_started_at: string | null;
  last_finished_at: string | null;
  last_error: string | null;
  last_result: ScanResult | null;
}

// MD-Skill 上传解析创建（POST /skills/upload）
interface SkillUploadResult {
  skill_id: string;
  name: string;
  version_number: number | null;
  status: "draft_created" | "unchanged";
}

const MAX_UPLOAD_MB = 50; // 对齐后端 max_upload_size_mb

// 文件大小展示（KB/MB）
function fmtBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

// Skill 来源标签：本人资源为「我的」；官方与管理员创建的平台资源为「平台」（只读，可复制）
function sourceOf(s: SkillListItem | SkillDetail, meId?: string): "official" | "mine" {
  if (s.official) return "official";
  return !!meId && !!s.owner_user_id && s.owner_user_id !== meId ? "official" : "mine";
}

// 状态文案与 tag class
function statusInfo(status: string): { label: string; cls: string } {
  const s = String(status || "").toUpperCase();
  if (s === "PUBLISHED") return { label: "已发布", cls: "skill-tag-published" };
  if (s === "DRAFT") return { label: "草稿", cls: "skill-tag-draft" };
  if (s === "ARCHIVED") return { label: "已停用", cls: "skill-tag-disabled" };
  return { label: status || "—", cls: "skill-tag-disabled" };
}

// MD-Skill 待确认启用：扫描入库后从未发布过、最新版本为 DRAFT，等待管理员确认（对齐 skill.html「待确认启用」）
function isPendingEnable(s: SkillListItem): boolean {
  return s.skill_type === "MD_SKILL" && !s.current_published_version_id && String(s.latest_version_status || "").toUpperCase() === "DRAFT";
}

// Skill 模式：优先取实现方式（md_script/md_instruction），回退脚本源判断
function modeOf(s: SkillListItem): "script" | "instruction" {
  if (s.implementation === "md_script") return "script";
  if (s.implementation === "md_instruction") return "instruction";
  return s.has_script ? "script" : "instruction";
}

function implementationLabel(impl: string | null | undefined): string {
  if (impl === "md_script") return "md_script（脚本）";
  if (impl === "md_instruction") return "md_instruction（纯指令）";
  return impl || "—";
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return "—";
  }
}

// JSON 美化（schema 展示用）
function prettyJson(obj: unknown): string {
  try {
    return JSON.stringify(obj, null, 2);
  } catch {
    return String(obj ?? "");
  }
}

export function SkillsPage() {
  const { user } = useAuth();
  const { push: toast } = useToast();
  // 危险操作（停用 / 级联删除）统一走 ConfirmDialog，避免 window.confirm
  const [confirmArchive, setConfirmArchive] = useState<{ id: string; name: string } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{ id: string; name: string } | null>(null);
  const meId = user?.id;
  const [skills, setSkills] = useState<SkillListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [versionDetail, setVersionDetail] = useState<SkillVersionDetail | null>(null);
  const [loadingVersion, setLoadingVersion] = useState(false);
  const [tools, setTools] = useState<SkillToolLink[]>([]);
  const [loadingTools, setLoadingTools] = useState(false);

  // 绑定 Tool（分组选择器 + 多选 + 顺序/角色/备注）
  const [bindOpen, setBindOpen] = useState(false);
  const [selectableGroups, setSelectableGroups] = useState<SelectableToolGroup[]>([]);
  const [loadingAvail, setLoadingAvail] = useState(false);
  const [bindLimit, setBindLimit] = useState(30);
  const [bindQuery, setBindQuery] = useState("");
  const [bindConnector, setBindConnector] = useState("all");
  const [bindTag, setBindTag] = useState("all");
  const [binding, setBinding] = useState(false);
  const [selectedBindings, setSelectedBindings] = useState<BindPick[]>([]);
  const [unbindingId, setUnbindingId] = useState<string | null>(null);

  const [filter, setFilter] = useState<Filter>("all");
  const [keyword, setKeyword] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [tab, setTab] = useState<Tab>("overview");

  // 创建 Skill
  const [createOpen, setCreateOpen] = useState(false);
  // 发布预检：指令内容为空时提示（可强制跳过）
  const [publishCheck, setPublishCheck] = useState<{ versionId: string; missing: string[] } | null>(null);
  const [createName, setCreateName] = useState("");
  const [createDesc, setCreateDesc] = useState("");
  const [createType, setCreateType] = useState("STANDARD");
  // MD_SKILL 类型：上传技能包（zip / SKILL.md）解析创建
  const [createFile, setCreateFile] = useState<File | null>(null);
  const createFileInputRef = useRef<HTMLInputElement | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // 编辑 Skill 本体信息（名称/描述）
  const [editInfoOpen, setEditInfoOpen] = useState(false);
  const [editInfoName, setEditInfoName] = useState("");
  const [editInfoDesc, setEditInfoDesc] = useState("");

  // 编辑草稿
  const [editing, setEditing] = useState<SkillVersionDetail | null>(null);
  const [editInstructions, setEditInstructions] = useState("");
  const [editOrchestration, setEditOrchestration] = useState("");
  const [editScript, setEditScript] = useState("");
  const [editPerms, setEditPerms] = useState("");
  const [editConfirm, setEditConfirm] = useState(false);

  // 同步 md-skills（触发后端扫描）与扫描状态
  const [scanning, setScanning] = useState(false);
  const [scanResult, setScanResult] = useState<ScanResult | null>(null);
  const [scanStatus, setScanStatus] = useState<ScanStatus | null>(null);
  const [loadingScanStatus, setLoadingScanStatus] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await request<SkillListItem[]>(`/api/z-torque/skills-tools/skills${showArchived ? "?include_archived=true" : ""}`);
      setSkills(data);
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
    if (skills.length === 0) return;
    const remembered = readSelectedId(SELECTED_KEY);
    const target = (remembered && skills.find((s) => s.id === remembered)) || skills[0];
    void openDetail(target);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, skills, selectedId]);

  // 进入「加载与生效」tab 时拉取 watchdog 实时扫描状态
  useEffect(() => {
    if (tab === "load" && selectedId) void loadScanStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, selectedId]);

  const selected = useMemo(
    () => skills.find((s) => s.id === selectedId) ?? null,
    [skills, selectedId],
  );

  // 当前查看版本：编辑草稿时用草稿；否则用已发布版本或最新版本
  const currentVersionRef = useMemo<SkillVersionRef | null>(() => {
    if (!detail) return null;
    if (editing) {
      const v = detail.versions.find((x) => x.id === editing.id);
      if (v) return v;
    }
    if (detail.current_published_version_id) {
      const v = detail.versions.find((x) => x.id === detail.current_published_version_id);
      if (v) return v;
    }
    return detail.versions.find((x) => x.status === "PUBLISHED") ?? detail.versions[detail.versions.length - 1] ?? null;
  }, [detail, editing]);

  async function openDetail(s: SkillListItem) {
    setSelectedId(s.id);
    writeSelectedId(SELECTED_KEY, s.id);
    setDetail(null);
    setVersionDetail(null);
    setTools([]);
    setEditing(null);
    setTab("overview");
    setLoadingDetail(true);
    setError(null);
    try {
      const d = await request<SkillDetail>(`/api/z-torque/skills-tools/skills/${s.id}`);
      setDetail(d);
      // 加载当前版本详情
      const ref = d.current_published_version_id
        ? d.versions.find((x) => x.id === d.current_published_version_id)
        : d.versions.find((x) => x.status === "PUBLISHED") ?? d.versions[d.versions.length - 1];
      if (ref) {
        await loadVersionDetail(ref.id);
      }
    } catch (err) {
      setError(errorText(err, "加载详情失败"));
    } finally {
      setLoadingDetail(false);
    }
  }

  async function refreshTools(versionId: string) {
    setLoadingTools(true);
    try {
      const list = await request<SkillToolLink[]>(`/api/z-torque/skills-tools/skill-versions/${versionId}/tools`);
      setTools(list);
    } catch {
      setTools([]);
    } finally {
      setLoadingTools(false);
    }
  }

  async function loadVersionDetail(versionId: string): Promise<SkillVersionDetail | null> {
    setLoadingVersion(true);
    try {
      const v = await request<SkillVersionDetail>(`/api/z-torque/skills-tools/skill-versions/${versionId}`);
      setVersionDetail(v);
      await refreshTools(versionId);
      return v;
    } catch (err) {
      setError(errorText(err, "加载版本详情失败"));
      setVersionDetail(null);
      return null;
    } finally {
      setLoadingVersion(false);
    }
  }

  // 同步 md-skills：触发后端扫描 md-skills/ 目录（对齐 skill.html「同步入口」）
  async function syncMdSkills() {
    setScanning(true);
    setError(null);
    try {
      const r = await request<ScanResult>("/api/z-torque/skills/scan", { method: "POST" });
      setScanResult(r);
      await load();
    } catch (err) {
      setError(errorText(err, "同步失败"));
    } finally {
      setScanning(false);
    }
  }

  // 扫描状态（watchdog 实时状态，用于「加载与生效」tab）
  async function loadScanStatus() {
    setLoadingScanStatus(true);
    try {
      const s = await request<ScanStatus>("/api/z-torque/skills/scan/status");
      setScanStatus(s);
    } catch {
      setScanStatus(null);
    } finally {
      setLoadingScanStatus(false);
    }
  }

  // 选择待上传的 MD-Skill 技能包（.zip / .md），前端先做类型/大小预检
  function handleCreateFile(file: File | null) {
    if (!file) {
      setCreateFile(null);
      return;
    }
    if (!/\.(zip|md)$/i.test(file.name)) {
      setError("仅支持 .zip 压缩包或 .md 技能文件");
      setCreateFile(null);
      return;
    }
    if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
      setError(`文件大小超过 ${MAX_UPLOAD_MB}MB 上限`);
      setCreateFile(null);
      return;
    }
    setError(null);
    setCreateFile(file);
  }

  async function createSkill() {
    if (createType === "MD_SKILL" ? !createFile : !createName.trim()) return;
    setBusy(true);
    setError(null);
    try {
      let uploaded: SkillUploadResult | null = null;
      if (createType === "MD_SKILL") {
        if (!createFile) return;
        const file = createFile;
        // 上传技能包：后端解析 front matter，落盘 md-skills/ 并创建草稿版本
        uploaded = await uploadFile<SkillUploadResult>("/api/z-torque/skills/upload", file);
      } else {
        await request("/api/z-torque/skills-tools/skills", {
          method: "POST",
          body: { name: createName.trim(), description: createDesc.trim() || null, skill_type: createType },
        });
      }
      setCreateOpen(false);
      setCreateName("");
      setCreateDesc("");
      setCreateType("STANDARD");
      setCreateFile(null);
      toast("success", uploaded ? "MD-Skill 已上传并生成草稿" : "Skill 已创建");
      await load();
      if (uploaded) {
        setSelectedId(uploaded.skill_id);
        if (uploaded.status === "unchanged") {
          setNotice(`MD-Skill「${uploaded.name}」内容无变化，未创建新版本`);
        }
      }
    } catch (err) {
      setError(errorText(err, "创建失败"));
    } finally {
      setBusy(false);
    }
  }

  function openInfoEditor() {
    if (!detail && !selected) return;
    setEditInfoName(detail?.name ?? selected?.name ?? "");
    setEditInfoDesc(detail?.description ?? selected?.description ?? "");
    setEditInfoOpen(true);
  }

  async function saveInfo() {
    if (!selected || !editInfoName.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await request<{ id: string; name: string; description: string | null }>(
        `/api/z-torque/skills-tools/skills/${selected.id}`,
        {
          method: "PATCH",
          body: { name: editInfoName.trim(), description: editInfoDesc.trim() || null },
        },
      );
      setEditInfoOpen(false);
      // 本地同步详情与列表，避免整页重置
      if (detail) setDetail({ ...detail, name: editInfoName.trim(), description: editInfoDesc.trim() || null });
      await load();
    } catch (err) {
      setError(errorText(err, "保存失败"));
    } finally {
      setBusy(false);
    }
  }

  async function copySkill(id: string) {
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/skills-tools/skills/${id}/copy`, { method: "POST" });
      await load();
    } catch (err) {
      setError(errorText(err, "复制失败"));
    } finally {
      setBusy(false);
    }
  }

  async function archiveSkill(id: string) {
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/skills-tools/skills/${id}/archive`, { method: "POST" });
      if (selectedId === id) setSelectedId(null);
      await load();
      toast("success", "已停用");
    } catch (err) {
      setError(errorText(err, "停用失败"));
    } finally {
      setBusy(false);
    }
  }

  async function deleteSkill(id: string) {
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/skills-tools/skills/${id}`, { method: "DELETE" });
      if (selectedId === id) setSelectedId(null);
      await load();
      toast("success", "已删除");
    } catch (err) {
      setError(errorText(err, "删除失败"));
    } finally {
      setBusy(false);
    }
  }

  async function createVersion(skillId: string) {
    setBusy(true);
    setError(null);
    try {
      const v = await request<{ id: string; version_number: number; status: string }>(
        `/api/z-torque/skills-tools/skills/${skillId}/versions`,
        { method: "POST", body: {} },
      );
      if (selected) await openDetail(selected);
      await loadVersionDetail(v.id);
      void load();
      toast("success", "已创建草稿版本");
    } catch (err) {
      setError(errorText(err, "创建版本失败"));
    } finally {
      setBusy(false);
    }
  }

  function openEditor(v: SkillVersionDetail) {
    setEditing(v);
    setEditInstructions(v.instructions ?? "");
    setEditOrchestration(v.orchestration_hint ?? "");
    setEditScript(v.script_source ?? "");
    setEditPerms(v.required_permissions.join("\n"));
    setEditConfirm(v.requires_confirmation);
    setTab("content");
  }

  // 持久化草稿表单（不含 busy 管理，供"保存草稿"与"发布"共用）
  async function persistVersion(): Promise<SkillVersionDetail | null> {
    if (!editing) return null;
    const perms = editPerms
      .split("\n")
      .map((p) => p.trim())
      .filter(Boolean);
    try {
      const v = await request<SkillVersionDetail>(
        `/api/z-torque/skills-tools/skill-versions/${editing.id}`,
        {
          method: "PATCH",
          body: {
            instructions: editInstructions.trim() || null,
            orchestration_hint: editOrchestration.trim() || null,
            script_source: editScript.trim() || null,
            required_permissions: perms,
            requires_confirmation: editConfirm,
          },
        },
      );
      setEditing(v);
      setVersionDetail(v);
      setError(null);
      return v;
    } catch (err) {
      setError(errorText(err, "保存失败"));
      return null;
    }
  }

  async function saveVersion() {
    if (!editing) return;
    setBusy(true);
    await persistVersion();
    setBusy(false);
  }

  // 发布前预检：指令内容为空时给出提示（可强制跳过）
  function publishVersion(versionId: string) {
    const vd = versionDetail && versionDetail.id === versionId ? versionDetail : null;
    const missing: string[] = [];
    if (vd && !(vd.instructions ?? "").trim()) {
      missing.push("指令内容为空——技能被调用时将无法产生有效输出");
    }
    if (missing.length > 0) {
      setPublishCheck({ versionId, missing });
      return;
    }
    void doPublish(versionId);
  }

  // 编辑态发布：先持久化表单（确认开关、指令等未保存修改），再走发布预检——
  // 避免"勾选确认后直接点发布"导致开关只存在于内存、发布版本不带确认
  async function saveAndPublish() {
    if (!editing) return;
    setBusy(true);
    const saved = await persistVersion();
    setBusy(false);
    if (!saved) {
      toast("error", "草稿保存失败，已取消发布");
      return;
    }
    publishVersion(saved.id);
  }

  async function doPublish(versionId: string) {
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/skills-tools/skill-versions/${versionId}/publish`, { method: "POST" });
      setEditing(null);
      if (selected) await openDetail(selected);
      await load();
      toast("success", "技能版本已发布，可在「智能体管理」绑定使用");
    } catch (err) {
      setError(errorText(err, "发布失败"));
    } finally {
      setBusy(false);
    }
  }

  // 展示用版本（编辑时用 editing，否则用 versionDetail）
  const viewVersion = editing ?? versionDetail;

  // 可编辑判定：仅自己的 Skill 的 DRAFT 版本可绑定/解绑 Tool（与后端 attach/detach 校验一致）
  const canEditTools = !!(viewVersion && viewVersion.status === "DRAFT" && selected && sourceOf(selected, meId) === "mine");

  // 当前草稿版本已绑定的 tool_version_id，用于选择器禁用重复项
  const boundVersionIds = useMemo(() => new Set(tools.map((t) => t.tool_version_id)), [tools]);

  // 选择器标签候选：来自工具持久化 tags，用于前端快速过滤
  const bindTagOptions = useMemo(() => {
    const set = new Set<string>();
    selectableGroups.forEach((group) => group.tools.forEach((tool) => (tool.tags ?? []).forEach((tag) => set.add(tag))));
    return Array.from(set).sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
  }, [selectableGroups]);

  // 选择器连接器候选：按后端分组结果去重
  const bindConnectorOptions = useMemo(
    () =>
      selectableGroups.map((group) => ({
        value: group.connector_id ?? "none",
        label: group.connector_name,
      })),
    [selectableGroups],
  );

  // 前端过滤：关键字（名称/说明/标签/参数）+ 连接器 + 标签
  const filteredBindGroups = useMemo(() => {
    const kw = bindQuery.trim().toLowerCase();
    return selectableGroups
      .filter((group) => {
        if (bindConnector === "all") return true;
        const value = group.connector_id ?? "none";
        return value === bindConnector;
      })
      .map((group) => ({
        ...group,
        tools: group.tools.filter((tool) => {
          if (bindTag !== "all" && !(tool.tags ?? []).includes(bindTag)) return false;
          if (!kw) return true;
          return (
            tool.name.toLowerCase().includes(kw) ||
            (tool.description ?? "").toLowerCase().includes(kw) ||
            (tool.tags ?? []).some((tag) => tag.toLowerCase().includes(kw)) ||
            (tool.param_summary ?? "").toLowerCase().includes(kw)
          );
        }),
      }))
      .filter((group) => group.tools.length > 0);
  }, [selectableGroups, bindQuery, bindConnector, bindTag]);

  // 已选上限：已绑定 + 本次已选 不能超过单版本上限
  const bindTotalCount = tools.length + selectedBindings.length;

  async function openBindModal() {
    if (!viewVersion) return;
    setBindOpen(true);
    setError(null);
    setLoadingAvail(true);
    setSelectedBindings([]);
    setBindQuery("");
    setBindConnector("all");
    setBindTag("all");
    try {
      const data = await request<SelectableToolResponse>(
        `/api/z-torque/skills-tools/tools/selectable?size=1000&skill_version_id=${viewVersion.id}`,
      );
      setSelectableGroups(data.groups);
      setBindLimit(data.limit || 30);
    } catch (err) {
      setError(errorText(err, "加载可用工具失败"));
      setSelectableGroups([]);
    } finally {
      setLoadingAvail(false);
    }
  }

  function toggleBinding(tool: SelectableTool) {
    const alreadyBound = tool.bound_to_current_version || boundVersionIds.has(tool.default_tool_version_id);
    if (alreadyBound) return;
    if (selectedBindings.some((pick) => pick.tool.tool_id === tool.tool_id)) {
      setSelectedBindings((prev) => prev.filter((pick) => pick.tool.tool_id !== tool.tool_id));
      return;
    }
    if (tools.length + selectedBindings.length >= bindLimit) {
      toast("info", `单个技能版本最多绑定 ${bindLimit} 个工具`);
      return;
    }
    const version =
      tool.published_versions.find((v) => v.tool_version_id === tool.default_tool_version_id) ??
      tool.published_versions[tool.published_versions.length - 1];
    if (!version) return;
    setSelectedBindings((prev) => [
      ...prev,
      { tool, tool_version_id: version.tool_version_id, version_number: version.version_number, role: "normal", note: "" },
    ]);
  }

  function changePickVersion(toolId: string, toolVersionId: string) {
    setSelectedBindings((prev) =>
      prev.map((pick) => {
        if (pick.tool.tool_id !== toolId) return pick;
        const version = pick.tool.published_versions.find((v) => v.tool_version_id === toolVersionId);
        return version ? { ...pick, tool_version_id: version.tool_version_id, version_number: version.version_number } : pick;
      }),
    );
  }

  function updatePick(toolId: string, patch: Partial<Pick<BindPick, "role" | "note">>) {
    setSelectedBindings((prev) => prev.map((pick) => (pick.tool.tool_id === toolId ? { ...pick, ...patch } : pick)));
  }

  async function bindTools() {
    if (!viewVersion || selectedBindings.length === 0) return;
    setBinding(true);
    setError(null);
    try {
      // 顺序提交：sort_order 由后端按当前最大值 +1 递增，保持已选区顺序
      for (const pick of selectedBindings) {
        await request(`/api/z-torque/skills-tools/skill-versions/${viewVersion.id}/tools`, {
          method: "POST",
          body: { tool_version_id: pick.tool_version_id, role: pick.role, note: pick.note.trim() || undefined },
        });
      }
      const count = selectedBindings.length;
      setBindOpen(false);
      await refreshTools(viewVersion.id);
      toast("success", `已绑定 ${count} 个工具`);
    } catch (err) {
      setError(errorText(err, "绑定失败"));
      // 顺序提交可能部分成功：刷新已绑定列表，避免界面与实际状态不一致
      await refreshTools(viewVersion.id);
    } finally {
      setBinding(false);
    }
  }

  async function moveTool(index: number, delta: number) {
    if (!viewVersion) return;
    const target = index + delta;
    if (target < 0 || target >= tools.length) return;
    const next = [...tools];
    [next[index], next[target]] = [next[target], next[index]];
    setTools(next);
    try {
      await request(`/api/z-torque/skills-tools/skill-versions/${viewVersion.id}/tools/order`, {
        method: "PUT",
        body: { tool_version_ids: next.map((item) => item.tool_version_id) },
      });
    } catch (err) {
      setError(errorText(err, "调整执行顺序失败"));
    } finally {
      await refreshTools(viewVersion.id);
    }
  }

  async function unbindTool(toolVersionId: string) {
    if (!viewVersion) return;
    setBusy(true);
    setUnbindingId(toolVersionId);
    setError(null);
    try {
      await request(`/api/z-torque/skills-tools/skill-versions/${viewVersion.id}/tools/${toolVersionId}`, { method: "DELETE" });
      await refreshTools(viewVersion.id);
    } catch (err) {
      setError(errorText(err, "解绑失败"));
    } finally {
      setBusy(false);
      setUnbindingId(null);
    }
  }

  // 一键升级：绑定保留顺序/角色/备注，仅换指向同一工具的新发布版本（仅草稿可操作）
  async function upgradeToolBinding(t: SkillToolLink) {
    if (!viewVersion || !t.current_published_version_id) return;
    setBusy(true);
    setUnbindingId(t.tool_version_id);
    setError(null);
    try {
      await request(`/api/z-torque/skills-tools/skill-versions/${viewVersion.id}/tools/${t.tool_version_id}/upgrade`, {
        method: "POST",
        body: { new_tool_version_id: t.current_published_version_id },
      });
      await refreshTools(viewVersion.id);
      toast("success", `已升级到 v${t.current_version_number}`);
    } catch (err) {
      setError(errorText(err, "升级绑定失败"));
    } finally {
      setBusy(false);
      setUnbindingId(null);
    }
  }

  // 列表筛选 + 搜索（维度对齐 skill.html：全部 / MD-Skill / 纯指令 / 脚本 / 待确认启用）
  const filteredSkills = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return skills.filter((s) => {
      const matchFilter =
        filter === "all" ||
        (filter === "md" && s.skill_type === "MD_SKILL") ||
        (filter === "instruction" && modeOf(s) === "instruction") ||
        (filter === "script" && modeOf(s) === "script") ||
        (filter === "pending" && isPendingEnable(s));
      if (!matchFilter) return false;
      if (!kw) return true;
      return (s.name + " " + (s.description ?? "")).toLowerCase().includes(kw);
    });
  }, [skills, filter, keyword]);

  // 详情操作按钮（状态驱动，对齐 skill.html 按钮组）
  const detailActions = useMemo(() => {
    if (!selected) return null;
    // MD-Skill 内容源自目录文件，平台侧不可编辑（改 name 会导致下次扫描建出重复技能）
    const isMd = selected.skill_type === "MD_SKILL";
    const isOfficial = sourceOf(selected, meId) === "official";
    const verStatus = (selected.latest_version_status ?? "").toUpperCase();
    if (isPendingEnable(selected)) {
      return (
        <>
          {/* 首次启用确认仅管理员可见（后端有权限校验，前端不暴露无权限操作） */}
          {isAdminUser(user) && (
            <button
              className="skill-btn-success"
              disabled={busy || !selected.latest_version_id}
              onClick={() => void publishVersion(selected.latest_version_id!)}
              title="发布扫描草稿，完成首次启用确认"
            >
              确认启用（管理员）
            </button>
          )}
          <button className="btn btn-secondary btn-sm" onClick={() => setTab("load")}>查看扫描状态</button>
          <button className="skill-btn-danger-outline" disabled={busy} onClick={() => setConfirmArchive({ id: selected.id, name: selected.name })}>忽略（停用）</button>
        </>
      );
    }
    if (isOfficial) {
      return (
        <>
          <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void copySkill(selected.id)}>复制到我的</button>
          {versionDetail && (
            <button className="btn btn-secondary btn-sm" onClick={() => void loadVersionDetail(versionDetail.id)}>查看快照</button>
          )}
        </>
      );
    }
    if ((selected.status ?? "").toUpperCase() === "ARCHIVED") {
      return (
        <>
          {versionDetail && (
            <button className="btn btn-secondary btn-sm" onClick={() => void loadVersionDetail(versionDetail.id)}>查看快照</button>
          )}
          <button className="skill-btn-danger-outline" disabled={busy} onClick={() => setConfirmDelete({ id: selected.id, name: selected.name })}>删除</button>
        </>
      );
    }
    if (verStatus === "DRAFT") {
      return (
        <>
          {!isMd && <button className="btn btn-secondary btn-sm" disabled={busy} onClick={openInfoEditor}>基础信息</button>}
          {!isMd && versionDetail && (
            <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => openEditor(versionDetail)}>继续编辑</button>
          )}
          <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void publishVersion(selected.latest_version_id!)}>发布</button>
          <button className="skill-btn-danger-outline" disabled={busy} onClick={() => setConfirmArchive({ id: selected.id, name: selected.name })}>停用</button>
        </>
      );
    }
    // 已发布
    return (
      <>
        {!isMd && <button className="btn btn-secondary btn-sm" disabled={busy} onClick={openInfoEditor}>基础信息</button>}
        {!isMd && <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void createVersion(selected.id)}>新建草稿版本</button>}
        <button className="skill-btn-danger-outline" disabled={busy} onClick={() => void archiveSkill(selected.id)}>停用</button>
      </>
    );
  }, [selected, busy, versionDetail, user]);

  // 详情面包屑与展示路径
  const detailRelativePath = viewVersion?.relative_path ?? selected?.relative_path ?? null;
  const detailIsMd = selected?.skill_type === "MD_SKILL";
  const detailPending = selected ? isPendingEnable(selected) : false;
  const breadcrumb = selected
    ? detailIsMd && detailRelativePath
      ? `技能 / MD-Skill / md-skills/${detailRelativePath}/`
      : `技能 / ${sourceOf(selected, meId) === "official" ? "平台" : "我的"}${detailIsMd ? " · MD-Skill" : ""}`
    : "";

  return (
    <div className="skill-page">
      {error && (
        <div className="skill-alert skill-alert-error">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      <div className="skill-shell">
        {/* 左侧列表 */}
        <aside className="skill-list-pane">
          <div className="skill-list-header">
            <h1>技能</h1>
            <div className="skill-list-actions">
              <button
                className="btn btn-secondary btn-sm"
                type="button"
                disabled={scanning}
                onClick={() => void syncMdSkills()}
                title="触发后端重新扫描 MD-skill/ 目录"
              >
                {scanning ? <span className="spinner" style={{ width: 14, height: 14 }} /> : "⟳"} 同步 MD-Skill
              </button>
              <button className="btn btn-primary btn-sm" type="button" onClick={() => setCreateOpen(true)}>+ 新建技能</button>
            </div>
            {scanResult && (
              <div className="skill-alert skill-alert-info skill-scan-result">
                <span>◈</span>
                <span>
                  同步完成：发现 {scanResult.discovered} 个目录 · 新建草稿 {scanResult.drafts_created} 个 · 无变化 {scanResult.unchanged} 个
                  {scanResult.issues.length > 0 && (
                    <div className="skill-issues">
                      {scanResult.issues.map((i) => (
                        <div key={`${i.path}-${i.code}`}>{i.path}：{i.message}</div>
                      ))}
                    </div>
                  )}
                </span>
                <button className="skill-scan-close" type="button" onClick={() => setScanResult(null)} title="关闭">×</button>
              </div>
            )}
            {notice && (
              <div className="skill-alert skill-alert-info skill-scan-result">
                <span>◈</span>
                <span>{notice}</span>
                <button className="skill-scan-close" type="button" onClick={() => setNotice(null)} title="关闭">×</button>
              </div>
            )}
            <div className="skill-search-row">
              <input
                className="skill-search-box"
                type="search"
                placeholder="搜索名称 / 描述 / 标签…"
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
              />
            </div>
            <div className="skill-filter-tabs">
              {(["all", "md", "instruction", "script", "pending"] as Filter[]).map((f) => (
                <button
                  key={f}
                  className={`skill-filter-tab${filter === f ? " active" : ""}`}
                  type="button"
                  onClick={() => setFilter(f)}
                >
                  {f === "all" ? "全部" : f === "md" ? "MD-Skill" : f === "instruction" ? "纯指令" : f === "script" ? "脚本" : "待确认启用"}
                </button>
              ))}
              <label className="skill-archived-toggle">
                <input
                  type="checkbox"
                  checked={showArchived}
                  onChange={(e) => setShowArchived(e.target.checked)}
                />
                <span>显示已停用</span>
              </label>
            </div>
          </div>

          <div className="skill-list">
            {loading ? (
              <Loading />
            ) : filteredSkills.length === 0 ? (
              <div className="skill-detail-empty">
                <GuideCallout
                  title="还没有技能"
                  why="技能需创建并发布版本后，才能被智能体绑定使用"
                  actionLabel="新建技能"
                  onAction={() => { setCreateName(""); setCreateDesc(""); setCreateType("STANDARD"); setCreateFile(null); setCreateOpen(true); }}
                  hint="推荐路径：新建技能 → 新建草稿版本 → 填写指令 → 发布 → 到智能体绑定"
                />
              </div>
            ) : (
              filteredSkills.map((s) => {
                const isActive = s.id === selectedId;
                const isOfficial = sourceOf(s, meId) === "official";
                const mode = modeOf(s);
                const pending = isPendingEnable(s);
                const si = statusInfo(s.latest_version_status ?? "");
                const verText = s.latest_version_number ? `v${s.latest_version_number}` : "—";
                return (
                  <div
                    key={s.id}
                    className={`skill-card${isActive ? " active" : ""}`}
                    onClick={() => void openDetail(s)}
                  >
                    <div className="row1">
                      <span className="name">{s.name}</span>
                      {pending ? (
                        <span className="skill-tag skill-tag-pending">待确认启用</span>
                      ) : (
                        <span className={`skill-tag ${si.cls}`}>{si.label}</span>
                      )}
                    </div>
                    <div className="desc">{s.description ?? "—"}</div>
                    <div className="meta">
                      {s.skill_type === "MD_SKILL" && <span className="skill-tag skill-tag-md">MD-Skill</span>}
                      <span className={`skill-tag ${isOfficial ? "skill-tag-official" : "skill-tag-mine"}`}>
                        {isOfficial ? "平台" : "我的"}
                      </span>
                      <span className={`skill-mode-tag ${mode === "script" ? "skill-mode-script" : "skill-mode-instruction"}`}>
                        {mode === "script" ? "脚本" : "纯指令"}
                      </span>
                      {s.requires_confirmation && <span className="skill-tag skill-tag-confirm">可配确认</span>}
                      <span className="ver">{verText}</span>
                    </div>
                    {s.skill_type === "MD_SKILL" && s.relative_path && (
                      <div className="path">md-skills/{s.relative_path}/</div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </aside>

        {/* 右侧详情 */}
        <section className="skill-detail-pane">
          {!selected ? (
            <div className="skill-detail-empty">
              <h2>选择左侧技能查看详情</h2>
              <span>技能区分纯指令与脚本模式，已发布版本不可原地修改。</span>
            </div>
          ) : (
            <>
              <div className="skill-detail-header">
                <div className="breadcrumb">{breadcrumb}</div>
                <div className="title-row">
                  <div>
                    <h2>{detail?.name ?? selected.name}</h2>
                    <p className="subtitle">{detail?.description ?? selected.description ?? "（暂无描述）"}</p>
                    <div className="skill-detail-tags">
                      {detailIsMd && <span className="skill-tag skill-tag-lg skill-tag-md">MD-Skill</span>}
                      <span className={`skill-tag skill-tag-lg ${sourceOf(selected, meId) === "official" ? "skill-tag-official" : "skill-tag-mine"}`}>
                        {sourceOf(selected, meId) === "official" ? "平台" : "我的"}
                      </span>
                      <span className={`skill-mode-tag skill-mode-tag-lg ${modeOf(selected) === "script" ? "skill-mode-script" : "skill-mode-instruction"}`}>
                        {modeOf(selected) === "script" ? "允许脚本" : "纯指令"}
                      </span>
                      {detailPending ? (
                        <span className="skill-tag skill-tag-lg skill-tag-pending">待管理员确认启用</span>
                      ) : currentVersionRef ? (
                        <span className={`skill-tag skill-tag-lg ${statusInfo(currentVersionRef.status).cls}`}>
                          {currentVersionRef.status === "PUBLISHED" ? `已启用 · 已发布 v${currentVersionRef.version_number}` : "草稿"}
                        </span>
                      ) : null}
                      {selected.requires_confirmation && (
                        <span className="skill-tag skill-tag-lg skill-tag-confirm">需人工确认</span>
                      )}
                    </div>
                  </div>
                  <div className="skill-detail-actions">{detailActions}</div>
                </div>
              </div>

              <div className="skill-tabs">
                {/* 页签按技能类型裁剪：scripts/load 为 MD-Skill 目录机制专属，标准技能不展示 */}
                {(detailIsMd
                  ? (["overview", "content", "scripts", "load", "tools", "versions"] as Tab[])
                  : (["overview", "content", "tools", "versions"] as Tab[])
                ).map((t) => (
                  <button
                    key={t}
                    className={`skill-tab${tab === t ? " active" : ""}`}
                    type="button"
                    onClick={() => setTab(t)}
                  >
                    {t === "overview"
                      ? "概览"
                      : t === "content"
                        ? "操作手册"
                        : t === "scripts"
                          ? "脚本 / 示例"
                          : t === "load"
                            ? "加载与生效"
                            : t === "tools"
                              ? "关联工具"
                              : "版本"}
                  </button>
                ))}
              </div>

              <div className="skill-detail-body">
                {loadingDetail || loadingVersion ? (
                  <Loading />
                ) : tab === "overview" ? (
                  <div className="tab-panel">
                    {!detailIsMd && sourceOf(selected, meId) === "official" && (
                      <div className="skill-alert skill-alert-info">
                        <span>ℹ</span>
                        <span>该技能为平台官方技能，资源只读；修改前请先复制到个人空间。</span>
                      </div>
                    )}

                    <div className="skill-section">
                      <h3>{detailIsMd ? "详情" : "技能配置"}</h3>
                      <div className="skill-form-grid">
                        <div className="skill-field">
                          <label>名称</label>
                          <input type="text" value={detail?.name ?? selected.name} disabled />
                        </div>
                        {detailIsMd && (
                          <div className="skill-field">
                            <label>实现方式</label>
                            <input type="text" value={implementationLabel(viewVersion?.implementation)} disabled />
                          </div>
                        )}
                        <div className="skill-field full">
                          <label>描述</label>
                          <textarea value={detail?.description ?? selected.description ?? ""} disabled rows={2} />
                        </div>
                        {detailIsMd ? (
                          <>
                            <div className="skill-field">
                              <label>允许脚本</label>
                              <input type="text" value={viewVersion?.allow_scripts != null ? (viewVersion.allow_scripts ? "是" : "否") : "—"} disabled />
                            </div>
                            <div className="skill-field">
                              <label>文件内版本</label>
                              <input type="text" value={viewVersion?.declared_version ?? "—"} disabled />
                            </div>
                          </>
                        ) : (
                          <div className="skill-field">
                            <label>是否开启审批</label>
                            <input type="text" value={viewVersion ? (viewVersion.requires_confirmation ? "是" : "否") : "—"} disabled />
                          </div>
                        )}
                        <div className="skill-field">
                          <label>来源目录</label>
                          <input
                            type="text"
                            value={detailRelativePath ? `md-skills/${detailRelativePath}/` : "—（控制台创建，非目录来源）"}
                            disabled
                          />
                        </div>
                      </div>
                    </div>

                    {detailIsMd && (
                      <>
                        <div className="skill-section">
                          <h3>输入参数<span className="hint"></span></h3>
                          <div className="skill-schema-box">{prettyJson(viewVersion?.input_schema ?? { type: "object", properties: {} })}</div>
                        </div>

                        <div className="skill-section">
                          <h3>输出参数</h3>
                          <div className="skill-schema-box">{prettyJson(viewVersion?.output_schema ?? { type: "object", properties: {} })}</div>
                        </div>
                      </>
                    )}

                    <div className="skill-section">
                      <h3>运行标志</h3>
                      <div className="skill-flag-grid">
                        <div className="skill-flag-card">
                          <div className="label">技能类型</div>
                          <div className="value">{selected.skill_type === "MD_SKILL" ? "MD-Skill" : "标准技能"}</div>
                        </div>
                        <div className="skill-flag-card">
                          <div className="label">当前版本状态</div>
                          <div className={`value${detailPending ? " text-warning" : " text-success"}`}>
                            {detailPending ? "待确认启用" : currentVersionRef ? statusInfo(currentVersionRef.status).label : "未发布"}
                          </div>
                        </div>
                        <div className="skill-flag-card">
                          <div className="label">首次启用</div>
                          <div className="value" style={{ fontSize: 13 }}>
                            {detailPending ? "待管理员确认" : currentVersionRef?.status === "PUBLISHED" ? "管理员已确认" : "—"}
                          </div>
                        </div>
                        <div className="skill-flag-card">
                          <div className="label">执行路径</div>
                          <div className="value" style={{ fontSize: 13 }}>
                            {viewVersion?.implementation === "md_script" ? "脚本 · 受控子进程" : "纯指令 · 上下文注入"}
                          </div>
                        </div>
                      </div>
                    </div>

                    <div className="skill-section">
                      <h3>权限要求 <span className="hint"></span></h3>
                      {viewVersion && viewVersion.required_permissions.length > 0 ? (
                        <div className="skill-perm-list">
                          {viewVersion.required_permissions.map((p) => (
                            <span key={p} className="skill-perm-chip">{p}</span>
                          ))}
                        </div>
                      ) : (
                        <div className="skill-readonly-banner">无额外权限要求。</div>
                      )}
                    </div>
                  </div>
                ) : tab === "content" ? (
                  <div className="tab-panel">
                    <div className="skill-section">
                      <h3 style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                        正文 · 给模型看的操作手册
                        {!detailIsMd && !editing && viewVersion?.status === "DRAFT" && (
                          <button className="btn btn-secondary btn-sm" style={{ marginLeft: "auto" }} disabled={busy} onClick={() => openEditor(viewVersion)}>编辑</button>
                        )}
                      </h3>
                      {editing ? (
                        <div className="skill-rules-editor">
                          <textarea
                            value={editInstructions}
                            onChange={(e) => setEditInstructions(e.target.value)}
                            disabled={busy}
                            placeholder="描述该技能的适用场景、执行要求与输出约束"
                          />
                        </div>
                      ) : viewVersion?.instructions ? (
                        <div className="skill-md-preview">
                          <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{viewVersion.instructions}</pre>
                        </div>
                      ) : (
                        <div className="skill-readonly-banner">暂无指令内容。</div>
                      )}
                    </div>
                    <div className="skill-section">
                      <h3>
                        调用提示
                      </h3>
                      {editing ? (
                        <textarea
                          className="textarea"
                          value={editOrchestration}
                          onChange={(e) => setEditOrchestration(e.target.value)}
                          disabled={busy}
                          maxLength={2000}
                          placeholder="例如：先调用「查询账户」确认身份，再按需调用「创建工单」；不要重复查询。"
                        />
                      ) : viewVersion?.orchestration_hint ? (
                        <div className="skill-md-preview">
                          <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{viewVersion.orchestration_hint}</pre>
                        </div>
                      ) : (
                        <div className="skill-readonly-banner">例如：先调用「查询账户」确认身份，再按需调用「创建工单」；不要重复查询。</div>
                      )}
                    </div>
                    {editing && (
                      <div className="skill-section">
                        <h3>版本参数</h3>
                        <div className="skill-form-grid">
                          <div className="skill-field full">
                            <textarea
                              className="textarea"
                              style={{ fontFamily: "var(--mono)" }}
                              value={editScript}
                              onChange={(e) => setEditScript(e.target.value)}
                              disabled={busy}
                              placeholder="填入脚本路径则为脚本模式，留空为纯指令模式"
                            />
                          </div>
                          <div className="skill-field full">
                            <h3>权限要求</h3>
                            <textarea
                              className="textarea"
                              style={{ fontFamily: "var(--mono)", minHeight: 80 }}
                              value={editPerms}
                              onChange={(e) => setEditPerms(e.target.value)}
                              disabled={busy}
                              placeholder={"skill.execute\nfile.read"}
                            />
                          </div>
                          <div className="skill-field full">
                            <h3 style={{ cursor: "pointer", display: "inline-flex", alignItems: "center" }}>
                              <input type="checkbox" checked={editConfirm} onChange={(e) => setEditConfirm(e.target.checked)} disabled={busy} style={{ marginRight: 6, minWidth: 24 ,minHeight: 24 }} />
                              开启人工审批
                            </h3>
                          </div>
                        </div>
                      </div>
                    )}
                    {editing ? (
                      <div className="skill-section" style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                        <button className="btn btn-secondary" disabled={busy} onClick={() => void saveVersion()}>保存草稿</button>
                        <button className="btn btn-primary" disabled={busy} onClick={() => void saveAndPublish()}>发布</button>
                        <button className="btn btn-secondary" onClick={() => setEditing(null)}>取消</button>
                      </div>
                    ) : (
                      <div className="skill-alert skill-alert-warning">
                      </div>
                    )}
                  </div>
                ) : tab === "scripts" ? (
                  <div className="tab-panel">
                    <div className="skill-section">
                      <h3>
                        scripts/
                      </h3>
                      {viewVersion?.script_files && viewVersion.script_files.length > 0 ? (
                        <div className="skill-file-list">
                          {viewVersion.script_files.map((f) => (
                            <div key={f} className="skill-file-item">
                              <div className="icon">{f.split(".").pop()?.slice(0, 2).toUpperCase() ?? "F"}</div>
                              <div>
                                <div className="name">{f.split("/").pop() ?? f}</div>
                                <div className="path">{detailRelativePath ? `md-skills/${detailRelativePath}/${f}` : f} · 已登记于 v{viewVersion.version_number} 快照</div>
                              </div>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="skill-readonly-banner">
                          当前技能 allow_scripts 为 false，或无 scripts/。示例与脚本不会自动执行。
                        </div>
                      )}
                    </div>
                    <div className="skill-section">
                      <h3>运行约束（展示用）</h3>
                      <div className="skill-form-grid">
                        <div className="skill-field">
                          <label>网络</label>
                          <input type="text" value="默认禁止" disabled />
                        </div>
                        <div className="skill-field">
                          <label>工作目录</label>
                          <input type="text" value="锁定在 skill 目录" disabled />
                        </div>
                        <div className="skill-field">
                          <label>超时</label>
                          <input type="text" value="超时强杀（后端 Runtime 限制）" disabled />
                        </div>
                        <div className="skill-field">
                          <label>密钥</label>
                          <input type="text" value="不向脚本暴露 Secret 明文" disabled />
                        </div>
                      </div>
                    </div>
                  </div>
                ) : tab === "load" ? (
                  <div className="tab-panel">
                    <div className="skill-section">
                      <h3>加载与生效</h3>
                      {/* 机制说明默认折叠：使用方只需要关心状态与入口，规范细节留给需要的人展开 */}
                      <details className="skill-doc-details">
                        <summary>展开查看加载机制说明（面向管理员 / 开发者）</summary>
                        <div className="skill-load-steps">
                          <strong>后端</strong>
                          <ol>
                            <li>启动时扫描 <code>md-skills/</code>；watchdog 监听变更（新增/修改 ≤ 30 秒生效，不重启）</li>
                            <li>解析 front-matter → 校验 → 写 <code>zr_skill(skill_type=MD_SKILL)</code> + <code>zr_skill_version</code></li>
                            <li>默认状态为已启用；<strong>首次启用需管理员一次确认</strong></li>
                            <li>绑定到智能体版本后，下一轮对话即可被模型看到</li>
                          </ol>
                          <strong style={{ display: "block", marginTop: 10 }}>写操作</strong>
                          <ol>
                            <li>改数据、发邮件等：requires_confirmation: true 或全局策略 → 单人确认 + 审计</li>
                          </ol>
                        </div>
                      </details>
                    </div>
                    <div className="skill-section">
                      <h3>
                        当前扫描状态
                        <button className="btn btn-secondary btn-sm" disabled={loadingScanStatus} onClick={() => void loadScanStatus()}>刷新</button>
                      </h3>
                      {loadingScanStatus ? (
                        <Loading />
                      ) : !scanStatus ? (
                        <div className="skill-readonly-banner">暂无扫描记录，点击「同步 md-skills」或等待自动扫描。</div>
                      ) : (
                        <div className="skill-form-grid">
                          <div className="skill-field">
                            <label>watchdog 运行中</label>
                            <input type="text" value={scanStatus.running ? "是（自动监听变更）" : "否（仅手动触发扫描）"} disabled />
                          </div>
                          <div className="skill-field">
                            <label>最近一次扫描开始</label>
                            <input type="text" value={fmtTime(scanStatus.last_started_at)} disabled />
                          </div>
                          <div className="skill-field">
                            <label>最近一次扫描结束</label>
                            <input type="text" value={fmtTime(scanStatus.last_finished_at)} disabled />
                          </div>
                          <div className="skill-field">
                            <label>最近结果</label>
                            <input
                              type="text"
                              value={
                                scanStatus.last_result
                                  ? `发现 ${scanStatus.last_result.discovered} · 新建草稿 ${scanStatus.last_result.drafts_created} · 无变化 ${scanStatus.last_result.unchanged} · 问题 ${scanStatus.last_result.issues.length}`
                                  : "—"
                              }
                              disabled
                            />
                          </div>
                          {scanStatus.last_error && (
                            <div className="skill-field full">
                              <label>最近错误</label>
                              <div className="skill-alert skill-alert-error" style={{ marginBottom: 0 }}>
                                <span>⚠</span>
                                <span>{scanStatus.last_error}</span>
                              </div>
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                    <div className="skill-alert skill-alert-warn">
                      <span>⚠</span>
                      <span>控制台「同步 md-skills」仅触发扫描；不能代替管理员首次确认启用。已发布版本不因磁盘修改而原地覆盖。</span>
                    </div>
                  </div>
                ) : tab === "tools" ? (
                  <div className="tab-panel">
                    <div className="skill-section">
                      <h3 className="skill-tools-head">
                        <span>关联工具版本 <span className="hint">技能版本绑定工具版本</span></span>
                        {canEditTools && (
                          <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void openBindModal()}>绑定工具</button>
                        )}
                      </h3>
                      {loadingTools ? (
                        <Loading />
                      ) : tools.length === 0 ? (
                        <div className="skill-readonly-banner">
                          {canEditTools
                            ? "当前草稿版本尚未绑定任何工具，点击右上角「绑定工具」添加。"
                            : "当前技能版本未绑定任何工具。"}
                        </div>
                      ) : (
                        <table className="skill-tool-table">
                          <thead>
                            <tr>
                              <th className="skill-tool-order-col">顺序</th>
                              <th>工具</th>
                              <th>版本</th>
                              <th>说明</th>
                              <th>状态</th>
                              {canEditTools && <th className="skill-tool-op-col">操作</th>}
                            </tr>
                          </thead>
                          <tbody>
                            {tools.map((t, index) => (
                              <tr key={t.tool_version_id}>
                                <td className="skill-tool-order-col">{index + 1}</td>
                                <td className="tool-name">
                                  {t.tool_name}
                                  {t.is_business_tool && <span className="skill-tag skill-tag-mine">业务封装</span>}
                                  {t.tags && t.tags.length > 0 && (
                                    <span className="skill-tool-tags">
                                      {t.tags.map((tag) => (
                                        <span key={tag} className="skill-tag skill-tag-neutral">{tag}</span>
                                      ))}
                                    </span>
                                  )}
                                </td>
                                <td>
                                  v{t.version_number}
                                  {t.current_version_number != null && t.current_version_number > t.version_number && (
                                    <span className="skill-tool-upgrade">
                                      <span className="skill-tag skill-tag-pending">新版本 v{t.current_version_number}</span>
                                      {canEditTools && (
                                        <button
                                          className="btn btn-ghost btn-sm"
                                          disabled={busy}
                                          onClick={() => void upgradeToolBinding(t)}
                                          title="升级到最新发布版本（保留顺序/角色/备注）"
                                        >
                                          升级
                                        </button>
                                      )}
                                    </span>
                                  )}
                                </td>
                                <td>
                                  <div className="skill-tool-note">
                                    {t.role && <span className="skill-tag skill-tag-neutral">{BIND_ROLE_LABELS[t.role] ?? t.role}</span>}
                                    <span>{t.note || t.description || "—"}</span>
                                  </div>
                                </td>
                                <td><span className={`skill-tag ${statusInfo(t.status).cls}`}>{statusInfo(t.status).label}</span></td>
                                {canEditTools && (
                                  <td className="skill-tool-op-col">
                                    <button
                                      className="btn btn-ghost btn-sm"
                                      disabled={busy || index === 0}
                                      onClick={() => void moveTool(index, -1)}
                                      aria-label="上移"
                                      title="上移"
                                    >
                                      ↑
                                    </button>
                                    <button
                                      className="btn btn-ghost btn-sm"
                                      disabled={busy || index === tools.length - 1}
                                      onClick={() => void moveTool(index, 1)}
                                      aria-label="下移"
                                      title="下移"
                                    >
                                      ↓
                                    </button>
                                    <button
                                      className="skill-btn-danger-outline btn-sm"
                                      disabled={busy}
                                      onClick={() => void unbindTool(t.tool_version_id)}
                                    >
                                      {unbindingId === t.tool_version_id ? "移除中…" : "移除"}
                                    </button>
                                  </td>
                                )}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                      <p className="skill-tool-foot">
                        {canEditTools
                          ? "仅草稿版本可绑定/解绑；发布后随快照固化，不可原地修改。"
                          : "所有工具执行必须经过 SkillGateway；前端不能直接调用外部工具。"}
                      </p>
                    </div>
                  </div>
                ) : (
                  /* versions */
                  <div className="tab-panel">
                    <div className="skill-section">
                      <h3
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          gap: 8,
                        }}
                      >
                        <span>技能版本（系统递增，非文件内随意 version）</span>
                        {sourceOf(selected, meId) !== "official" && !detailPending && (
                          <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void createVersion(selected.id)}>
                            新建草稿
                          </button>
                        )}
                      </h3>
                      {!detail || detail.versions.length === 0 ? (
                        <div className="skill-readonly-banner">暂无版本。</div>
                      ) : (
                        <div className="skill-version-list">
                          {detail.versions.map((v) => {
                            const isCurrent = detail.current_published_version_id === v.id;
                            return (
                              <div key={v.id} className={`skill-version-item${isCurrent ? " current" : ""}`}>
                                <div className="v-badge">v{v.version_number}</div>
                                <div className="v-body">
                                  <div className="v-title">
                                    v{v.version_number}
                                    {isCurrent && <span className="skill-tag skill-tag-published">当前发布</span>}
                                    <span className={`skill-tag ${statusInfo(v.status).cls}`}>{statusInfo(v.status).label}</span>
                                    {v.requires_confirmation && <span className="skill-tag skill-tag-confirm" title="被调用时需人工确认后才会执行">需确认</span>}
                                  </div>
                                  <div className="v-meta">
                                    {fmtTime(v.published_at)}
                                    {v.snapshot_hash ? ` · ${v.snapshot_hash.slice(0, 12)}` : ""}
                                  </div>
                                </div>
                                <div className="v-actions">
                                  {v.status === "DRAFT" ? (
                                    <>
                                      {!detailIsMd && <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void loadVersionDetail(v.id).then((sv) => { if (sv) openEditor(sv); })}>编辑</button>}
                                      <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void publishVersion(v.id)}>发布</button>
                                    </>
                                  ) : (
                                    <button className="btn btn-secondary btn-sm" onClick={() => void loadVersionDetail(v.id)}>快照</button>
                                  )}
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                    <div className="skill-alert skill-alert-info">
                      <span>ℹ</span>
                      <span>已发布技能版本不可原地修改。磁盘文件变更应生成新草稿版本，不覆盖正在被智能体版本引用的版本。</span>
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </section>
      </div>

      {/* 创建 Skill 弹窗 */}
      <Modal
        open={createOpen}
        title="新建 Skill"
        onClose={() => {
          setCreateOpen(false);
          setCreateFile(null);
          setError(null);
        }}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setCreateOpen(false)}>取消</button>
            <button
              className="btn btn-primary"
              disabled={busy || (createType === "MD_SKILL" ? !createFile : !createName.trim())}
              onClick={() => void createSkill()}
            >
              {createType === "MD_SKILL" ? "上传并解析" : "创建"}
            </button>
          </>
        }
      >
        {error && (
          <div className="skill-alert skill-alert-error">
            <span>⚠</span>
            <span>{error}</span>
          </div>
        )}
        {/* 类型放首位：先决定创建方式，再填写对应字段 */}
        <div className="form-field">
          <label>类型</label>
          <select
            className="select"
            value={createType}
            onChange={(e) => {
              setCreateType(e.target.value);
              setCreateFile(null);
              setError(null);
            }}
          >
            <option value="STANDARD">标准 Skill</option>
            <option value="MD_SKILL">MD-Skill（上传 zip 或 SKILL.md 文件）</option>
          </select>
        </div>
        {createType === "MD_SKILL" ? (
          <>
            <div
              className="skill-upload-zone"
              role="button"
              tabIndex={0}
              onClick={() => createFileInputRef.current?.click()}
            >
              <input
                ref={createFileInputRef}
                className="skill-upload-input"
                type="file"
                accept=".zip,.md"
                onChange={(e) => {
                  handleCreateFile(e.target.files?.[0] ?? null);
                  e.target.value = "";
                }}
              />
              <span className="skill-upload-icon">⇪</span>
              <span>点击选择 .zip 压缩包或 SKILL.md 文件</span>
            </div>
            {createFile && <div className="skill-upload-file">{createFile.name}（{fmtBytes(createFile.size)}）</div>}
            <div className="skill-upload-hint">
              技能名称与说明取自文件 front matter；同名技能将替换 md-skills 目录中的文件并生成新草稿版本。
            </div>
          </>
        ) : (
          <>
            <div className="form-field">
              <label>名称</label>
              <input className="input" value={createName} onChange={(e) => setCreateName(e.target.value)} placeholder="例如：数据分析" />
            </div>
            <div className="form-field">
              <label>说明</label>
              <textarea className="textarea" value={createDesc} onChange={(e) => setCreateDesc(e.target.value)} placeholder="描述该 Skill 的用途（可选）" />
            </div>
          </>
        )}
      </Modal>

      {/* 绑定 Tool 弹窗：连接器分组 + 多选 + 版本/角色/备注 + 上限计数 */}
      <Modal
        open={bindOpen}
        title="绑定工具（已发布版本）"
        onClose={() => setBindOpen(false)}
        width={880}
        footer={
          <>
            <span className="skill-bind-count">
              已绑定 {tools.length} · 本次已选 {selectedBindings.length} · 上限 {bindLimit}
            </span>
            <button className="btn btn-secondary" onClick={() => setBindOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={binding || selectedBindings.length === 0} onClick={() => void bindTools()}>
              {binding ? "绑定中…" : `绑定 ${selectedBindings.length || ""}`.trim()}
            </button>
          </>
        }
      >
        <div className="skill-bind-toolbar">
          <input
            className="input"
            value={bindQuery}
            onChange={(e) => setBindQuery(e.target.value)}
            placeholder="搜索工具名称、说明、标签或参数"
            aria-label="搜索工具"
          />
          <select className="input" value={bindConnector} onChange={(e) => setBindConnector(e.target.value)} aria-label="按连接器过滤">
            <option value="all">全部连接器</option>
            {bindConnectorOptions.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
          <select className="input" value={bindTag} onChange={(e) => setBindTag(e.target.value)} aria-label="按标签过滤">
            <option value="all">全部标签</option>
            {bindTagOptions.map((tag) => (
              <option key={tag} value={tag}>{tag}</option>
            ))}
          </select>
        </div>

        {loadingAvail ? (
          <Loading />
        ) : filteredBindGroups.length === 0 ? (
          <div className="skill-readonly-banner">当前没有可绑定的工具（需要有已发布版本的工具，或调整筛选条件）。</div>
        ) : (
          <div className="skill-bind-layout">
            <div className="skill-bind-list">
              {filteredBindGroups.map((group) => (
                <div key={group.connector_id ?? "none"} className="skill-bind-group">
                  <div className="skill-bind-group-title">
                    <span>{group.connector_name}</span>
                    <span className="hint">{group.tools.length} 个</span>
                  </div>
                  {group.tools.map((tool) => {
                    const alreadyBound = tool.bound_to_current_version || boundVersionIds.has(tool.default_tool_version_id);
                    const picked = selectedBindings.some((pick) => pick.tool.tool_id === tool.tool_id);
                    const disabled = alreadyBound || (!picked && bindTotalCount >= bindLimit);
                    return (
                      <label
                        key={tool.tool_id}
                        className={`skill-tool-picker-item${picked ? " selected" : ""}${disabled ? " disabled" : ""}`}
                        title={alreadyBound ? "已绑定当前草稿版本" : undefined}
                      >
                        <input
                          type="checkbox"
                          disabled={disabled}
                          checked={picked || alreadyBound}
                          onChange={() => toggleBinding(tool)}
                        />
                        <div className="t-main">
                          <div className="t-row">
                            <span className="t-name">{tool.name}</span>
                            {tool.is_business_tool && <span className="skill-tag skill-tag-mine">业务封装</span>}
                            {tool.requires_confirmation && <span className="skill-tag skill-tag-confirm">需审批</span>}
                            {tool.tags.map((tag) => (
                              <span key={tag} className="skill-tag skill-tag-neutral">{tag}</span>
                            ))}
                            {alreadyBound && <span className="t-bound">已绑定</span>}
                          </div>
                          <div className="t-desc">{tool.description || tool.param_summary || "—"}</div>
                          {tool.params && tool.params.length > 0 && (
                            <div className="t-params-list">
                              {tool.params.slice(0, 6).map((p) => (
                                <span
                                  key={p.name}
                                  className={`skill-tag ${p.required ? "skill-tag-confirm" : "skill-tag-neutral"}`}
                                  title={`${p.type}${p.description ? " · " + p.description : ""}${p.required ? "（必填）" : "（可选）"}`}
                                >
                                  {p.name}
                                  {!p.required && "?"}
                                </span>
                              ))}
                              {tool.params.length > 6 && <span className="t-more">+{tool.params.length - 6}</span>}
                            </div>
                          )}
                        </div>
                        <span className="t-ver">v{tool.published_versions[tool.published_versions.length - 1]?.version_number ?? 1}</span>
                      </label>
                    );
                  })}
                </div>
              ))}
            </div>

            <div className="skill-bind-picked">
              <div className="skill-bind-group-title">
                <span>本次已选</span>
                <span className={bindTotalCount >= bindLimit ? "skill-bind-limit danger" : "skill-bind-limit"}>
                  {bindTotalCount} / {bindLimit}
                </span>
              </div>
              {selectedBindings.length === 0 ? (
                <div className="skill-readonly-banner">从左侧勾选工具；勾选后在此设置版本、执行角色与说明。</div>
              ) : (
                selectedBindings.map((pick, index) => (
                  <div key={pick.tool.tool_id} className="skill-bind-pick-card">
                    <div className="pick-head">
                      <span className="pick-index">{tools.length + index + 1}</span>
                      <span className="t-name">{pick.tool.name}</span>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => setSelectedBindings((prev) => prev.filter((item) => item.tool.tool_id !== pick.tool.tool_id))}
                        aria-label={`移除 ${pick.tool.name}`}
                      >
                        ✕
                      </button>
                    </div>
                    <div className="pick-fields">
                      <label>
                        <span>版本</span>
                        <select
                          className="input"
                          value={pick.tool_version_id}
                          onChange={(e) => changePickVersion(pick.tool.tool_id, e.target.value)}
                        >
                          {pick.tool.published_versions.map((version) => (
                            <option key={version.tool_version_id} value={version.tool_version_id}>v{version.version_number}</option>
                          ))}
                        </select>
                      </label>
                      <label>
                        <span>角色</span>
                        <select
                          className="input"
                          value={pick.role}
                          onChange={(e) => updatePick(pick.tool.tool_id, { role: e.target.value })}
                        >
                          {Object.entries(BIND_ROLE_LABELS).map(([value, label]) => (
                            <option key={value} value={value}>{label}</option>
                          ))}
                        </select>
                      </label>
                    </div>
                    <input
                      className="input"
                      value={pick.note}
                      onChange={(e) => updatePick(pick.tool.tool_id, { note: e.target.value })}
                      placeholder="调用说明（可选，注入模型提示）"
                      maxLength={200}
                    />
                  </div>
                ))
              )}
            </div>
          </div>
        )}
        <p className="skill-field help" style={{ marginTop: "10px" }}>
          仅可选择已发布状态的工具版本；按勾选顺序作为默认执行顺序，发布时固化进快照。
        </p>
      </Modal>

      {/* 编辑 Skill 本体信息弹窗 */}
      <Modal
        open={editInfoOpen}
        title="编辑技能信息"
        onClose={() => setEditInfoOpen(false)}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setEditInfoOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={busy || !editInfoName.trim()} onClick={() => void saveInfo()}>保存</button>
          </>
        }
      >
        <div className="form-field">
          <label>名称</label>
          <input className="input" value={editInfoName} onChange={(e) => setEditInfoName(e.target.value)} placeholder="技能名称" />
        </div>
        <div className="form-field">
          <label>说明</label>
          <textarea className="textarea" value={editInfoDesc} onChange={(e) => setEditInfoDesc(e.target.value)} placeholder="描述该技能的用途（可留空）" />
        </div>
        <p className="skill-field help">仅修改技能基本信息；版本内容（指令、脚本、工具绑定）在对应草稿版本中编辑。</p>
      </Modal>

      {/* 停用：可逆操作，轻确认 */}
      <ConfirmDialog
        open={confirmArchive !== null}
        title="停用该技能？"
        confirmText="停用"
        busy={busy}
        onCancel={() => setConfirmArchive(null)}
        onConfirm={() => { if (confirmArchive) { const { id } = confirmArchive; setConfirmArchive(null); void archiveSkill(id); } }}
      >
        <p>技能「{confirmArchive?.name ?? ""}」将进入「已停用」状态，不再被智能体调用。已发布版本与绑定关系保留，后续可在列表中重新启用。</p>
      </ConfirmDialog>

      {/* 删除（级联）：不可逆，高危确认 + 输名 */}
      <ConfirmDialog
        open={confirmDelete !== null}
        title="删除该技能？"
        confirmText="删除"
        destructive
        requireText={confirmDelete?.name ?? ""}
        requireLabel="请输入技能名称以确认删除"
        busy={busy}
        onCancel={() => setConfirmDelete(null)}
        onConfirm={() => { if (confirmDelete) { const { id } = confirmDelete; setConfirmDelete(null); void deleteSkill(id); } }}
      >
        <p>技能「{confirmDelete?.name ?? ""}」的<strong>全部版本与工具绑定将被级联删除</strong>，且不可恢复。如不再使用，建议改用「停用」。</p>
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
            const { versionId } = publishCheck;
            setPublishCheck(null);
            void doPublish(versionId);
          }
        }}
      >
        <p>以下配置项缺失，发布后技能可能无法正常工作：</p>
        <ul style={{ margin: "8px 0", paddingLeft: 20 }}>
          {publishCheck?.missing.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
        <p style={{ color: "var(--text-muted)", fontSize: 13 }}>可先「返回修改」补全指令内容；确认无误也可仍按当前草稿发布。</p>
      </ConfirmDialog>
    </div>
  );
}
