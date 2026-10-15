import { NavLink } from "react-router-dom";
import { useAuth } from "../stores/auth";
import type { UserInfo } from "../types";

export interface NavItem {
  to: string;
  label: string;
  /** 展示所需的权限（与后端 require_permissions 对应）；管理员自动可见 */
  permission?: string;
  /** 仅管理员可见（对应后端 require_admin） */
  adminOnly?: boolean;
}

// 全站统一导航：仅包含已实现路由的入口；展示时按用户角色/权限过滤。
// 权限矩阵：模型/连接器不进普通用户管理页（仅通过 Skill/Agent 间接使用），审计对普通用户开放（仅本人记录）
export const APP_NAV: NavItem[] = [
  { to: "/chat", label: "会话管理" },
  { to: "/agents", label: "智能体管理" },
  { to: "/skills", label: "技能管理" },
  { to: "/models", label: "模型管理", adminOnly: true },
  { to: "/connectors", label: "连接器管理", adminOnly: true },
  // 审计为例外入口：普通用户可见，但后端自动限定为仅本人记录
  { to: "/audit", label: "审计管理" },
  { to: "/users", label: "用户管理", adminOnly: true },
];

/**
 * 管理员判定：与后端 require_admin 的最小判定保持一致——
 * 拥有 admin 角色，或 admin.manage 权限，或任一 user./role./system. 前缀权限。
 */
export function isAdminUser(user: UserInfo | null): boolean {
  if (!user) return false;
  if (user.roles?.includes("admin")) return true;
  const perms = user.permissions ?? [];
  if (perms.includes("admin.manage")) return true;
  return perms.some((p) => p.startsWith("user.") || p.startsWith("role.") || p.startsWith("system."));
}

/** 登录后的默认主页：管理员为用户管理，普通用户为会话 */
export function homePath(user: UserInfo | null): string {
  return isAdminUser(user) ? "/users" : "/chat";
}

/** 按导航元数据与用户权限过滤可见入口 */
export function visibleNav(nav: NavItem[], user: UserInfo | null): NavItem[] {
  const admin = isAdminUser(user);
  const perms = user?.permissions ?? [];
  return nav.filter((item) => {
    if (item.adminOnly && !admin) return false;
    if (item.permission && !admin && !perms.includes(item.permission)) return false;
    return true;
  });
}

/**
 * 应用顶栏：品牌 + 导航 + 用户区。
 * Chat 页与 ConsoleLayout 共用，保证全站导航视觉一致（对齐 docs/frontend/chat.html）。
 * 颜色/尺寸全部走 index.css 的 CSS 变量，不硬编码。
 */
export function AppTopbar({ nav }: { nav: NavItem[] }) {
  const { user, logout } = useAuth();
  const items = visibleNav(nav, user);
  return (
    <header className="chat-topbar">
      <div className="chat-topbar-brand">
        <span className="chat-logo">Z</span>
        <span className="chat-brand-name">Z-Torque</span>
        <span className="chat-brand-sub">极矩</span>
      </div>
      <nav className="chat-topbar-nav">
        {items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => "chat-nav-link" + (isActive ? " active" : "")}
          >
            {item.label}
          </NavLink>
        ))}
      </nav>
      <div className="chat-topbar-user">
        <span className="chat-user-avatar">{user?.username.slice(0, 1).toUpperCase() ?? "U"}</span>
        <span className="chat-user-name">{user?.username ?? "未登录"}</span>
        <button type="button" className="chat-btn-logout" onClick={() => void logout()}>退出</button>
      </div>
    </header>
  );
}
