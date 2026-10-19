import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { request, uploadFile } from "../services/api";
import { errorText } from "../services/errorText";
import type { SkillListItem, SkillDetail, SkillVersionDetail, SkillToolLink, SkillVersionRef, ToolListItem } from "../types";
import { Loading } from "../components/Loading";
import { Modal } from "../components/Modal";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useToast } from "../components/Toast";
import { GuideCallout } from "../components/GuideCallout";
import { isAdminUser } from "../components/AppTopbar";
import { useAuth } from "../stores/auth";

type Tab = "overview" | "content" | "scripts" | "load" | "tools" | "versions";
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

  // 绑定 Tool
  const [bindOpen, setBindOpen] = useState(false);
  const [availableTools, setAvailableTools] = useState<ToolListItem[]>([]);
  const [loadingAvail, setLoadingAvail] = useState(false);
  const [selectedToolVersionId, setSelectedToolVersionId] = useState<string | null>(null);
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

  // 默认展示列表第一项，避免主区空白（仅当前无选中时生效；删除当前选中后自动补选）
  useEffect(() => {
    if (loading || selectedId) return;
    if (skills.length > 0) void openDetail(skills[0]);
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
    setEditScript(v.script_source ?? "");
    setEditPerms(v.required_permissions.join("\n"));
    setEditConfirm(v.requires_confirmation);
    setTab("content");
  }

  async function saveVersion() {
    if (!editing) return;
    setBusy(true);
    setError(null);
    try {
      const perms = editPerms
        .split("\n")
        .map((p) => p.trim())
        .filter(Boolean);
      const v = await request<SkillVersionDetail>(
        `/api/z-torque/skills-tools/skill-versions/${editing.id}`,
        {
          method: "PATCH",
          body: {
            instructions: editInstructions.trim() || null,
            script_source: editScript.trim() || null,
            required_permissions: perms,
            requires_confirmation: editConfirm,
          },
        },
      );
      setEditing(v);
      setVersionDetail(v);
      setError(null);
    } catch (err) {
      setError(errorText(err, "保存失败"));
    } finally {
      setBusy(false);
    }
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

  async function openBindModal() {
    setBindOpen(true);
    setError(null);
    setLoadingAvail(true);
    setSelectedToolVersionId(null);
    try {
      const list = await request<ToolListItem[]>("/api/z-torque/skills-tools/tools");
      // 仅可绑定已发布版本
      const bindable = list.filter((t) => t.versions.some((v) => v.status === "PUBLISHED"));
      setAvailableTools(bindable);
      const firstPub = bindable[0]?.versions.find((v) => v.status === "PUBLISHED");
      setSelectedToolVersionId(firstPub ? firstPub.id : null);
    } catch (err) {
      setError(errorText(err, "加载可用工具失败"));
    } finally {
      setLoadingAvail(false);
    }
  }

  async function bindTool() {
    if (!viewVersion || !selectedToolVersionId) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/z-torque/skills-tools/skill-versions/${viewVersion.id}/tools`, {
        method: "POST",
        body: { tool_version_id: selectedToolVersionId },
      });
      setBindOpen(false);
      await refreshTools(viewVersion.id);
    } catch (err) {
      setError(errorText(err, "绑定失败"));
    } finally {
      setBusy(false);
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
          <button className="btn btn-secondary btn-sm" disabled={busy} onClick={openInfoEditor}>编辑信息</button>
          {versionDetail && (
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
        <button className="btn btn-secondary btn-sm" disabled={busy} onClick={openInfoEditor}>编辑信息</button>
        <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void createVersion(selected.id)}>新建草稿版本</button>
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
            <p className="skill-sub">
              MD-Skill 扫描入库后在此展示
            </p>
            <div className="skill-list-actions">
              <button
                className="btn btn-secondary btn-sm"
                type="button"
                disabled={scanning}
                onClick={() => void syncMdSkills()}
                title="触发后端重新扫描 md-skills/ 目录"
              >
                {scanning ? <span className="spinner" style={{ width: 14, height: 14 }} /> : "⟳"} 同步 md-skills
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
                        {modeOf(selected) === "script" ? "allow_scripts: true" : "纯指令"}
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
                {(["overview", "content", "scripts", "load", "tools", "versions"] as Tab[]).map((t) => (
                  <button
                    key={t}
                    className={`skill-tab${tab === t ? " active" : ""}`}
                    type="button"
                    onClick={() => setTab(t)}
                  >
                    {t === "overview"
                      ? "概览 · front-matter"
                      : t === "content"
                        ? "正文（操作手册）"
                        : t === "scripts"
                          ? "scripts / examples"
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
                    {detailIsMd && (
                      <div className="skill-alert skill-alert-violet">
                        <span>◈</span>
                        <span>
                          <strong>MD-Skill 规范（社区版）</strong>：目录 <code>md-skills/&lt;name&gt;/SKILL.md</code> → 解析 YAML front-matter + 正文 →
                          写入 <code>zr_skill(skill_type=MD_SKILL)</code> + <code>zr_skill_version</code>。
                          前端不读磁盘，只展示已入库记录。执行走 SkillGateway，无浏览器跑脚本。
                        </span>
                      </div>
                    )}
                    {!detailIsMd && (
                      <div className="skill-alert skill-alert-info">
                        <span>ℹ</span>
                        <span>该技能为控制台创建的非目录来源技能；平台资源只读，修改前请复制到个人空间。</span>
                      </div>
                    )}

                    <div className="skill-section">
                      <h3>SKILL.md front-matter 字段</h3>
                      <div className="skill-form-grid">
                        <div className="skill-field">
                          <label>name（唯一标识）</label>
                          <input type="text" value={detail?.name ?? selected.name} disabled />
                        </div>
                        <div className="skill-field">
                          <label>implementation（实现方式）</label>
                          <input type="text" value={implementationLabel(viewVersion?.implementation)} disabled />
                        </div>
                        <div className="skill-field full">
                          <label>description</label>
                          <textarea value={detail?.description ?? selected.description ?? ""} disabled rows={2} />
                        </div>
                        <div className="skill-field">
                          <label>allow_scripts</label>
                          <input type="text" value={viewVersion?.allow_scripts != null ? String(viewVersion.allow_scripts) : "—"} disabled />
                        </div>
                        <div className="skill-field">
                          <label>requires_confirmation</label>
                          <input type="text" value={viewVersion ? String(viewVersion.requires_confirmation) : "—"} disabled />
                        </div>
                        <div className="skill-field">
                          <label>declared_version（文件内 version）</label>
                          <input type="text" value={viewVersion?.declared_version ?? "—"} disabled />
                        </div>
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

                    <div className="skill-section">
                      <h3>params（JSON Schema · 工具入参）<span className="hint">input_schema</span></h3>
                      <div className="skill-schema-box">{prettyJson(viewVersion?.input_schema ?? { type: "object", properties: {} })}</div>
                      <p className="skill-field help" style={{ marginTop: "10px" }}>
                        执行前按 schema 校验入参；注册制路径下作为工具入参描述。
                      </p>
                    </div>

                    <div className="skill-section">
                      <h3>output_schema（输出约束）</h3>
                      <div className="skill-schema-box">{prettyJson(viewVersion?.output_schema ?? { type: "object", properties: {} })}</div>
                    </div>

                    <div className="skill-section">
                      <h3>运行标志</h3>
                      <div className="skill-flag-grid">
                        <div className="skill-flag-card">
                          <div className="label">skill_type</div>
                          <div className="value">{selected.skill_type}</div>
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
                      <h3>权限要求 <span className="hint">required_permissions</span></h3>
                      {viewVersion && viewVersion.required_permissions.length > 0 ? (
                        <div className="skill-perm-list">
                          {viewVersion.required_permissions.map((p) => (
                            <span key={p} className="skill-perm-chip">{p}</span>
                          ))}
                        </div>
                      ) : (
                        <div className="skill-readonly-banner">无额外权限要求。</div>
                      )}
                      <p className="skill-field help" style={{ marginTop: "10px" }}>执行时由后端 PEP 与 SkillGateway 校验，前端不绕过。</p>
                    </div>
                  </div>
                ) : tab === "content" ? (
                  <div className="tab-panel">
                    <div className="skill-section">
                      <h3>
                        正文 · 给模型看的操作手册
                        <span className="hint">不可信输入，不能覆盖系统策略 / PEP</span>
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
                    {editing ? (
                      <div className="skill-section" style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                        <button className="btn btn-secondary" disabled={busy} onClick={() => void saveVersion()}>保存草稿</button>
                        <button className="btn btn-primary" disabled={busy} onClick={() => void publishVersion(editing.id)}>发布</button>
                        <button className="btn btn-secondary" onClick={() => setEditing(null)}>取消</button>
                      </div>
                    ) : (
                      <div className="skill-alert skill-alert-info">
                        <span>ℹ</span>
                        <span>
                          <strong>注册制路径</strong>：正文作为该工具的「操作说明」注入工具描述（或按需一次性上下文）；模型据此调用；<code>params</code> 作入参 schema。
                          <strong>脚本路径</strong>（allow_scripts: true）：受控子进程执行，stdout/stderr 回传并截断。
                        </span>
                      </div>
                    )}
                  </div>
                ) : tab === "scripts" ? (
                  <div className="tab-panel">
                    <div className="skill-section">
                      <h3>
                        scripts/
                        <span className="hint">仅 allow_scripts: true · 扩展名白名单 .py</span>
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
                      <p className="skill-field help" style={{ marginTop: "12px" }}>
                        资源限制 CPU/内存；工作目录锁定在 skill 目录；默认无网络；超时强杀；拒绝 <code>..</code> 与目录外读写。
                        <strong>前端无「运行脚本」按钮。</strong>示例文件（examples/）不自动获得执行权限。
                      </p>
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
                              <th>工具</th>
                              <th>版本</th>
                              <th>说明</th>
                              <th>状态</th>
                              {canEditTools && <th className="skill-tool-op-col">操作</th>}
                            </tr>
                          </thead>
                          <tbody>
                            {tools.map((t) => (
                              <tr key={t.tool_version_id}>
                                <td className="tool-name">{t.tool_name}</td>
                                <td>v{t.version_number}</td>
                                <td>{t.description ?? "—"}</td>
                                <td><span className={`skill-tag ${statusInfo(t.status).cls}`}>{statusInfo(t.status).label}</span></td>
                                {canEditTools && (
                                  <td className="skill-tool-op-col">
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
                          : "所有工具执行必须经过 SkillGateway；前端不能直接调用外部能力。"}
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
                                  </div>
                                  <div className="v-meta">
                                    {fmtTime(v.published_at)}
                                    {v.snapshot_hash ? ` · ${v.snapshot_hash.slice(0, 12)}` : ""}
                                  </div>
                                </div>
                                <div className="v-actions">
                                  {v.status === "DRAFT" ? (
                                    <>
                                      <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void loadVersionDetail(v.id).then((sv) => { if (sv) openEditor(sv); })}>编辑</button>
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

      {/* 绑定 Tool 弹窗 */}
      <Modal
        open={bindOpen}
        title="绑定工具（已发布版本）"
        onClose={() => setBindOpen(false)}
        width={520}
        footer={
          <>
            <button className="btn btn-secondary" onClick={() => setBindOpen(false)}>取消</button>
            <button className="btn btn-primary" disabled={busy || !selectedToolVersionId} onClick={() => void bindTool()}>绑定</button>
          </>
        }
      >
        {loadingAvail ? (
          <Loading />
        ) : availableTools.length === 0 ? (
          <div className="skill-readonly-banner">当前没有可绑定的工具（需要有已发布版本的工具）。</div>
        ) : (
          <div className="skill-tool-picker">
            {availableTools.flatMap((tool) =>
              tool.versions
                .filter((v) => v.status === "PUBLISHED")
                .map((v) => {
                  const vid = v.id;
                  const already = boundVersionIds.has(vid);
                  const checked = selectedToolVersionId === vid;
                  return (
                    <label
                      key={vid}
                      className={`skill-tool-picker-item${checked ? " selected" : ""}${already ? " disabled" : ""}`}
                      title={already ? "已绑定当前草稿版本" : undefined}
                    >
                      <input
                        type="radio"
                        name="bind-tool-version"
                        disabled={already}
                        checked={checked}
                        onChange={() => setSelectedToolVersionId(vid)}
                      />
                      <span className="t-name">{tool.name}</span>
                      {tool.official && <span className="skill-tag skill-tag-official">平台</span>}
                      <span className="t-ver">v{v.version_number}</span>
                      <span className="t-desc">{tool.description ?? "—"}</span>
                      {already && <span className="t-bound">已绑定</span>}
                    </label>
                  );
                }),
            )}
          </div>
        )}
        <p className="skill-field help" style={{ marginTop: "10px" }}>
          仅可选择已发布状态的工具版本；绑定后随草稿保存，发布时固化进快照。
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

      {/* 编辑草稿的脚本源编辑弹窗 */}
      {editing && (
        <Modal
          open={true}
          title={`编辑草稿 v${editing.version_number}`}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button className="btn btn-secondary" onClick={() => setEditing(null)}>取消</button>
              <button className="btn btn-secondary" disabled={busy} onClick={() => void saveVersion()}>保存草稿</button>
              <button className="btn btn-primary" disabled={busy} onClick={() => void publishVersion(editing.id)}>发布</button>
            </>
          }
        >
          <div className="form-field">
            <label>脚本源 script_source</label>
            <textarea
              className="textarea"
              style={{ fontFamily: "var(--mono)", minHeight: "120px" }}
              value={editScript}
              onChange={(e) => setEditScript(e.target.value)}
              placeholder="scripts/summarize.py（留空则为纯指令模式）"
            />
            <span className="hint">填入脚本路径则为脚本模式，留空为纯指令模式</span>
          </div>
          <div className="form-field">
            <label>权限要求（每行一个）</label>
            <textarea
              className="textarea"
              style={{ fontFamily: "var(--mono)", minHeight: "80px" }}
              value={editPerms}
              onChange={(e) => setEditPerms(e.target.value)}
              placeholder="skill.execute&#10;file.read"
            />
          </div>
          <div className="form-field">
            <label>
              <input type="checkbox" checked={editConfirm} onChange={(e) => setEditConfirm(e.target.checked)} style={{ marginRight: 6 }} />
              需要用户确认 requires_confirmation
            </label>
          </div>
        </Modal>
      )}

      {/* 停用：可逆操作，轻确认 */}
      <ConfirmDialog
        open={confirmArchive !== null}
        title="停用该技能？"
        confirmText="停用"
        busy={busy}
        onCancel={() => setConfirmArchive(null)}
        onConfirm={() => { if (confirmArchive) void archiveSkill(confirmArchive.id); }}
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
        onConfirm={() => { if (confirmDelete) void deleteSkill(confirmDelete.id); }}
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
