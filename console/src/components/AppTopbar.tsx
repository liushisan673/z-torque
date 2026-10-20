import { useState } from "react";
import { NavLink } from "react-router-dom";
import { useAuth } from "../stores/auth";
import type { UserInfo } from "../types";

// 主题持久化 key：与 index.html 的防闪烁脚本保持一致
const THEME_KEY = "z_torque_theme";
export type Theme = "light" | "dark";

/** 当前生效主题（index.html 内联脚本已在首屏前写入 html data-theme） */
export function currentTheme(): Theme {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

/** 切换主题：写入 html data-theme 并持久化，返回切换后的主题 */
export function toggleTheme(): Theme {
  const next: Theme = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch {
    /* localStorage 不可用时仅当前页生效 */
  }
  return next;
}

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

/** 登录后的默认主页：管理员为模型配置（部署后第一件事），普通用户为会话 */
export function homePath(user: UserInfo | null): string {
  return isAdminUser(user) ? "/models" : "/chat";
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
  // 主题切换：local state 仅驱动图标重绘，实际主题落在 html data-theme 上（CSS 变量全局联动）
  const [theme, setTheme] = useState<Theme>(currentTheme);
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
        <button
          type="button"
          className="chat-theme-toggle"
          onClick={() => setTheme(toggleTheme())}
          title={theme === "dark" ? "切换为浅色模式" : "切换为深色模式"}
          aria-label={theme === "dark" ? "切换为浅色模式" : "切换为深色模式"}
        >
          {theme === "dark" ? (
            // 太阳：当前深色，点击切浅色
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" /></svg>
          ) : (
            // 月亮：当前浅色，点击切深色
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" /></svg>
          )}
        </button>
        <span className="chat-user-avatar">{user?.username.slice(0, 1).toUpperCase() ?? "U"}</span>
        <span className="chat-user-name">{user?.username ?? "未登录"}</span>
        <button type="button" className="chat-btn-logout" onClick={() => void logout()}>退出</button>
      </div>
    </header>
  );
}
