import { useCallback, useEffect, useMemo, useState } from "react";
import { request } from "../services/api";
import { errorText } from "../services/errorText";
import { useToast } from "../components/Toast";
import type { IamRole, IamUser } from "../types";
import { Loading } from "../components/Loading";
import { Modal } from "../components/Modal";
import { PaneResizer, usePaneWidth } from "../components/PaneResizer";

const API = "/api/z-torque/admin/iam";

type UserState = "ok" | "locked" | "off";

// 状态判定（与后端 authenticate 锁定逻辑一致）：禁用优先，其次未过期的 locked_until
function stateOf(u: IamUser): UserState {
  if (!u.is_active) return "off";
  if (u.locked_until && new Date(u.locked_until).getTime() > Date.now()) return "locked";
  return "ok";
}

const STATE_META: Record<UserState, { label: string; cls: string }> = {
  ok: { label: "正常", cls: "users-state-ok" },
  locked: { label: "已锁定", cls: "users-state-locked" },
  off: { label: "已禁用", cls: "users-state-off" },
};

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return "—";
  }
}

function initialOf(u: IamUser): string {
  return (u.display_name || u.username).charAt(0).toUpperCase();
}

// 角色差异说明（与系统实际权限种子一致：admin 全部权限；user Chat + 个人资源）
// 角色展示名（仅渲染层映射，存储值不变）
const ROLE_LABEL: Record<string, string> = {
  admin: "管理员",
  user: "普通用户",
};

function roleLabel(name: string): string {
  return ROLE_LABEL[name] ?? name;
}

function roleDesc(name: string): string {
  if (name === "admin") return "全部权限：可管理智能体、技能、模型、连接器、用户与审计";
  return "基础权限：对话与个人资源管理";
}

type DialogKind = "create" | "edit" | "reset" | "delete" | null;

export function UsersPage() {
  const { push: toast } = useToast();
  // 左右分栏宽度：拖拽把手调整右侧详情宽度，会话内保持，退出登录恢复默认
  const listPane = usePaneWidth("users", "--users-side-w");
  const [users, setUsers] = useState<IamUser[]>([]);
  const [roles, setRoles] = useState<IamRole[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 工具栏筛选（后端返回全量列表，筛选在客户端进行，与文档工具栏一致）
  const [keyword, setKeyword] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [stateFilter, setStateFilter] = useState("");

  // 侧栏详情
  const [selected, setSelected] = useState<IamUser | null>(null);

  // 弹窗
  const [dialog, setDialog] = useState<DialogKind>(null);
  const [target, setTarget] = useState<IamUser | null>(null);
  const [mUsername, setMUsername] = useState("");
  const [mDisplayName, setMDisplayName] = useState("");
  const [mPassword, setMPassword] = useState("");
  const [mPassword2, setMPassword2] = useState("");
  const [mRoleIds, setMRoleIds] = useState<string[]>([]);
  const [mActive, setMActive] = useState(true);
  const [mConfirmName, setMConfirmName] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [u, r] = await Promise.all([
        request<{ items: IamUser[] }>(`${API}/users`),
        request<{ items: IamRole[] }>(`${API}/roles`),
      ]);
      setUsers(u.items);
      setRoles(r.items);
      setSelected((prev) => (prev ? u.items.find((x) => x.id === prev.id) ?? null : null));
    } catch (err) {
      setError(errorText(err, "加载失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return users.filter((u) => {
      if (kw && !u.username.toLowerCase().includes(kw) && !(u.display_name ?? "").toLowerCase().includes(kw)) return false;
      if (roleFilter && !u.roles.some((r) => r.name === roleFilter)) return false;
      if (stateFilter && stateOf(u) !== stateFilter) return false;
      return true;
    });
  }, [users, keyword, roleFilter, stateFilter]);

  function openCreate() {
    setTarget(null);
    setMUsername(""); setMDisplayName(""); setMPassword(""); setMPassword2("");
    setMRoleIds(roles.find((r) => r.name === "user") ? [roles.find((r) => r.name === "user")!.id] : []);
    setMActive(true);
    setDialog("create");
  }

  function openEdit(u: IamUser) {
    setTarget(u);
    setMDisplayName(u.display_name ?? "");
    setMRoleIds(u.roles.map((r) => r.id));
    setMActive(u.is_active);
    setDialog("edit");
  }

  function openReset(u: IamUser) {
    setTarget(u);
    setMPassword(""); setMPassword2("");
    setDialog("reset");
  }

  function openDelete(u: IamUser) {
    setTarget(u);
    setMConfirmName("");
    setDialog("delete");
  }

  async function submitCreate() {
    if (!mUsername.trim()) { setError("用户名必填"); return; }
    if (!mPassword) { setError("初始密码必填"); return; }
    setBusy(true); setError(null);
    try {
      await request(`${API}/users`, {
        method: "POST",
        body: { username: mUsername.trim(), password: mPassword, display_name: mDisplayName.trim() || null, is_active: true, role_ids: mRoleIds },
      });
      setDialog(null);
      toast("success", "用户已创建");
      await reload();
    } catch (err) {
      setError(errorText(err, "创建失败"));
    } finally {
      setBusy(false);
    }
  }

  async function submitEdit() {
    if (!target) return;
    setBusy(true); setError(null);
    try {
      await request(`${API}/users/${target.id}`, {
        method: "PATCH",
        body: { display_name: mDisplayName.trim() || null, is_active: mActive, role_ids: mRoleIds },
      });
      setDialog(null);
      toast("success", "用户已更新");
      await reload();
    } catch (err) {
      setError(errorText(err, "更新失败"));
    } finally {
      setBusy(false);
    }
  }

  async function submitReset() {
    if (!target) return;
    if (!mPassword) { setError("新密码必填"); return; }
    if (mPassword !== mPassword2) { setError("两次输入的密码不一致"); return; }
    setBusy(true); setError(null);
    try {
      await request(`${API}/users/${target.id}/reset-password`, {
        method: "POST",
        body: { password: mPassword },
      });
      setDialog(null);
      toast("success", "密码已重置（Argon2id 存储）");
      await reload();
    } catch (err) {
      setError(errorText(err, "重置失败"));
    } finally {
      setBusy(false);
    }
  }

  async function submitDelete() {
    if (!target) return;
    if (mConfirmName.trim() !== target.username) { setError("输入的用户名不匹配"); return; }
    setBusy(true); setError(null);
    try {
      await request(`${API}/users/${target.id}`, { method: "DELETE" });
      setDialog(null);
      toast("success", "用户已删除");
      await reload();
    } catch (err) {
      setError(errorText(err, "删除失败"));
    } finally {
      setBusy(false);
    }
  }

  async function toggleActive(u: IamUser) {
    setBusy(true); setError(null);
    try {
      await request(`${API}/users/${u.id}`, { method: "PATCH", body: { is_active: !u.is_active } });
      toast("success", u.is_active ? "已禁用" : "已启用");
      await reload();
    } catch (err) {
      setError(errorText(err, "操作失败"));
    } finally {
      setBusy(false);
    }
  }

  async function unlock(u: IamUser) {
    setBusy(true); setError(null);
    try {
      await request(`${API}/users/${u.id}/unlock`, { method: "POST" });
      toast("success", "已解锁（失败计数清零）");
      await reload();
    } catch (err) {
      setError(errorText(err, "解锁失败"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="users-page" style={listPane.style}>
      {/* 工具栏 */}
      <div className="users-toolbar">
        <div className="users-field">
          <label>搜索</label>
          <input type="search" placeholder="搜索用户名…" value={keyword} onChange={(e) => setKeyword(e.target.value)} style={{ width: 200 }} />
        </div>
        <div className="users-field">
          <label>角色</label>
          <select value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)}>
            <option value="">全部角色</option>
            {roles.map((r) => <option key={r.id} value={r.name}>{roleLabel(r.name)}</option>)}
          </select>
        </div>
        <div className="users-field">
          <label>状态</label>
          <select value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>
            <option value="">全部状态</option>
            <option value="ok">正常</option>
            <option value="off">已禁用</option>
            <option value="locked">已锁定</option>
          </select>
        </div>
        <div className="spacer" />
        <button className="btn btn-primary" onClick={openCreate}>创建用户</button>
      </div>

      {error && (
        <div className="skill-alert skill-alert-error" style={{ marginBottom: 12 }}>
          <span>⚠</span><span>{error}</span>
        </div>
      )}
      <div className="users-shell">
        {/* 列表 */}
        <div className="users-list-pane">
          <div className="users-table-wrap">
            {loading ? (
              <Loading />
            ) : filtered.length === 0 ? (
              <div className="users-empty">暂无符合条件的用户</div>
            ) : (
              <table className="users-table">
                <thead>
                  <tr>
                    <th>用户</th>
                    <th>角色</th>
                    <th>状态</th>
                    <th>最近登录</th>
                    <th>创建时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((u) => {
                    const st = stateOf(u);
                    return (
                      <tr key={u.id} className={selected?.id === u.id ? "active" : ""} onClick={() => setSelected(u)}>
                        <td>
                          <div className="users-idcell">
                            <span className="users-avatar">{initialOf(u)}</span>
                            <div className="meta">
                              <div className="name">{u.username}</div>
                              <div className="disp">{u.display_name ?? "—"}</div>
                            </div>
                          </div>
                        </td>
                        <td>
                          {u.roles.length === 0 ? <span className="disp">—</span> : u.roles.map((r) => (
                            <span key={r.id} className={`users-role-tag ${r.name === "admin" ? "users-role-tag-admin" : "users-role-tag-user"}`} style={{ marginRight: 4 }}>
                              {roleLabel(r.name)}
                            </span>
                          ))}
                        </td>
                        <td><span className={`users-state ${STATE_META[st].cls}`}>{STATE_META[st].label}</span></td>
                        <td className="time">{fmtTime(u.last_login_at)}</td>
                        <td className="time">{fmtTime(u.created_at)}</td>
                        <td>
                          <div className="users-actions" onClick={(e) => e.stopPropagation()}>
                            <button className="btn btn-secondary btn-sm" onClick={() => openEdit(u)}>编辑</button>
                            <button className="btn btn-secondary btn-sm" onClick={() => openReset(u)}>重置密码</button>
                            {st === "locked" && (
                              <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void unlock(u)}>解锁</button>
                            )}
                            <button
                              className="btn btn-secondary btn-sm"
                              disabled={busy}
                              onClick={() => void toggleActive(u)}
                            >
                              {u.is_active ? "禁用" : "启用"}
                            </button>
                            <button className="btn btn-danger btn-sm" disabled={busy} onClick={() => openDelete(u)}>删除</button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>

        {/* 左右分栏拖拽把手（反向：调整右侧详情宽度） */}
        <PaneResizer invert onPreview={listPane.previewWidth} onCommit={listPane.commitWidth} />

        {/* 侧栏详情 */}
        {selected && (
          <aside className="users-side">
            <div className="users-side-header">
              <h2>用户详情</h2>
              <button className="btn btn-secondary btn-sm" onClick={() => setSelected(null)}>关闭</button>
            </div>
            <div className="users-protect-note">
              系统至少保留一名可用管理员：最后一个管理员的管理员角色不可移除，且不可被禁用。
            </div>
            <div className="users-side-section">
              <h3>基本信息</h3>
              <dl className="users-kv">
                <dt>用户名</dt><dd>{selected.username}</dd>
                <dt>显示名</dt><dd>{selected.display_name ?? "—"}</dd>
                <dt>角色</dt>
                <dd>
                  {selected.roles.length === 0 ? "—" : selected.roles.map((r) => (
                    <span key={r.id} className={`users-role-tag ${r.name === "admin" ? "users-role-tag-admin" : "users-role-tag-user"}`} style={{ marginRight: 4 }}>{roleLabel(r.name)}</span>
                  ))}
                </dd>
                <dt>状态</dt><dd><span className={`users-state ${STATE_META[stateOf(selected)].cls}`}>{STATE_META[stateOf(selected)].label}</span></dd>
                <dt>ID</dt><dd style={{ fontFamily: "var(--mono)", fontSize: 11 }}>{selected.id}</dd>
                <dt>创建时间</dt><dd>{fmtTime(selected.created_at)}</dd>
                <dt>最近登录</dt><dd>{fmtTime(selected.last_login_at)}</dd>
                <dt>连续失败</dt><dd>{selected.failed_login_count} 次{selected.locked_until ? `（锁定至 ${fmtTime(selected.locked_until)}）` : ""}</dd>
              </dl>
            </div>
            <div className="users-side-section">
              <h3>有效权限（{selected.permissions.length}）</h3>
              {selected.permissions.length === 0 ? (
                <span style={{ fontSize: 12, color: "var(--text-muted)" }}>无（由角色推导，前端不直接编辑）</span>
              ) : (
                <div className="users-perm-list">
                  {selected.permissions.map((p) => <span key={p.id} className="users-perm">{p.name}</span>)}
                </div>
              )}
            </div>
          </aside>
        )}
      </div>

      {/* 创建 / 编辑弹窗 */}
      {(dialog === "create" || dialog === "edit") && (
        <Modal
          open
          title={dialog === "create" ? "创建用户" : "编辑用户"}
          onClose={() => setDialog(null)}
          footer={
            <>
              <button className="btn btn-secondary" disabled={busy} onClick={() => setDialog(null)}>取消</button>
              <button className="btn btn-primary" disabled={busy} onClick={() => void (dialog === "create" ? submitCreate() : submitEdit())}>
                {busy ? "提交中…" : "保存"}
              </button>
            </>
          }
        >
          <div className="users-form-grid">
            {dialog === "create" && (
              <div className="users-form-item">
                <label>用户名 <span className="req">*</span></label>
                <input type="text" placeholder="登录用户名，唯一" value={mUsername} onChange={(e) => setMUsername(e.target.value)} autoComplete="off" />
              </div>
            )}
            <div className="users-form-item">
              <label>显示名</label>
              <input type="text" placeholder="可选，用于界面展示" value={mDisplayName} onChange={(e) => setMDisplayName(e.target.value)} />
            </div>
            {dialog === "create" && (
              <div className="users-form-item">
                <label>初始密码 <span className="req">*</span></label>
                <input type="password" placeholder="不少于策略要求的长度" value={mPassword} onChange={(e) => setMPassword(e.target.value)} autoComplete="new-password" />
              </div>
            )}
            <div className="users-form-item">
              <label>角色</label>
              <div className="users-role-choice">
                {roles.map((r) => {
                  const isSel = mRoleIds.includes(r.id);
                  return (
                    <div
                      key={r.id}
                      className={`users-role-option ${isSel ? "selected" : ""}`}
                      onClick={() => setMRoleIds(isSel ? mRoleIds.filter((x) => x !== r.id) : [...mRoleIds, r.id])}
                    >
                      <div className="t">{roleLabel(r.name)}</div>
                      <div className="d">{roleDesc(r.name)}</div>
                    </div>
                  );
                })}
              </div>
              <span className="help">权限点由角色推导，前端不直接编辑权限明细。</span>
            </div>
            {dialog === "edit" && (
              <div className="users-form-item">
                <label>账号状态</label>
                <label style={{ display: "flex", alignItems: "center", gap: 6, fontWeight: 400 }}>
                  <input type="checkbox" checked={mActive} onChange={(e) => setMActive(e.target.checked)} style={{ height: "auto" }} />
                  <span style={{ fontSize: 12 }}>启用该账号（禁用后无法登录）</span>
                </label>
              </div>
            )}
          </div>
        </Modal>
      )}

      {/* 重置密码弹窗 */}
      {dialog === "reset" && target && (
        <Modal
          open
          title={`重置密码 — ${target.username}`}
          onClose={() => setDialog(null)}
          footer={
            <>
              <button className="btn btn-secondary" disabled={busy} onClick={() => setDialog(null)}>取消</button>
              <button className="btn btn-primary" disabled={busy} onClick={() => void submitReset()}>
                {busy ? "提交中…" : "重置"}
              </button>
            </>
          }
        >
          <div className="users-form-grid">
            <div className="users-form-item">
              <label>新密码 <span className="req">*</span></label>
              <input type="password" placeholder="新密码" value={mPassword} onChange={(e) => setMPassword(e.target.value)} autoComplete="new-password" />
            </div>
            <div className="users-form-item">
              <label>确认新密码 <span className="req">*</span></label>
              <input type="password" placeholder="再次输入" value={mPassword2} onChange={(e) => setMPassword2(e.target.value)} autoComplete="new-password" />
              <span className="help">后端以 Argon2id 哈希存储；重置后失败计数清零并解除锁定。</span>
            </div>
          </div>
        </Modal>
      )}
      {/* 删除确认弹窗 */}
      {dialog === "delete" && target && (
        <Modal
          open
          title={`删除用户 — ${target.username}`}
          onClose={() => setDialog(null)}
          footer={
            <>
              <button className="btn btn-secondary" disabled={busy} onClick={() => setDialog(null)}>取消</button>
              <button className="btn btn-danger" disabled={busy || mConfirmName.trim() !== target.username} onClick={() => void submitDelete()}>
                {busy ? "删除中…" : "永久删除"}
              </button>
            </>
          }
        >
          <div className="users-form-grid">
            <div className="users-protect-note">
              删除为永久操作：该用户的会话、消息、文件、任务、确认与记忆将一并清除；
              其创建的连接器与模型提供方将转交当前管理员；智能体/工具/技能保留但归属清空；
              审计记录保留（操作者标记置空）。登录会话与角色绑定同步清除，不可恢复。
            </div>
            <div className="users-form-item">
              <label>输入用户名以确认删除 <span className="req">*</span></label>
              <input
                type="text"
                placeholder={target.username}
                value={mConfirmName}
                onChange={(e) => setMConfirmName(e.target.value)}
                autoComplete="off"
              />
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
