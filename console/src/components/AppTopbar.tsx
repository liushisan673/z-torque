import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { Dropdown } from "antd";
import type { MenuProps } from "antd";
import { DownOutlined } from "@ant-design/icons";
import { useAuth } from "../stores/auth";
import { useThemeMode } from "../theme/ThemeProvider";
import type { UserInfo } from "../types";

export interface NavItem {
  to: string;
  label: string;
  /** i18n key：有则用字典译文，缺失时回退 label（中文） */
  labelKey?: string;
  /** 展示所需的权限（与后端 require_permissions 对应）；管理员自动可见 */
  permission?: string;
  /** 仅管理员可见（对应后端 require_admin） */
  adminOnly?: boolean;
  section?: "primary" | "more";
  group?: "operations" | "configuration" | "account";
}

// 全站统一导航：仅包含已实现路由的入口；展示时按用户角色/权限过滤。
// 权限矩阵：模型/连接器不进普通用户管理页（仅通过 Skill/Agent 间接使用），审计对普通用户开放（仅本人记录）
export const APP_NAV: NavItem[] = [
  { to: "/chat", label: "工作台", labelKey: "nav.chat", section: "primary" },
  { to: "/knowledge", label: "知识库", labelKey: "nav.knowledge", section: "primary" },
  { to: "/agents", label: "智能体", labelKey: "nav.agents", section: "primary" },
  { to: "/skills", label: "技能", labelKey: "nav.skills", section: "primary" },
  { to: "/audit", label: "运行与审计", labelKey: "nav.audit", section: "more", group: "operations" },
  { to: "/models", label: "模型接入", labelKey: "nav.models", adminOnly: true, section: "more", group: "configuration" },
  { to: "/connectors", label: "连接器", labelKey: "nav.connectors", adminOnly: true, section: "more", group: "configuration" },
  { to: "/webhooks", label: "Webhook", labelKey: "nav.webhooks", adminOnly: true, section: "more", group: "configuration" },
  { to: "/users", label: "用户与权限", labelKey: "nav.users", adminOnly: true, section: "more", group: "account" },
  { to: "/settings", label: "偏好设置", labelKey: "nav.settings", section: "more", group: "account" },
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

/** 登录后的默认入口统一为工作台；系统配置按需进入，不把管理员直接送进配置页。 */
export function homePath(_user: UserInfo | null): string {
  return "/chat";
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
  const primaryItems = items.filter((item) => item.section !== "more");
  const moreItems = items.filter((item) => item.section === "more");
  const { mode: theme, toggle, locale, setLocale, t } = useThemeMode();
  const navigate = useNavigate();
  const location = useLocation();
  const activeMoreItem = moreItems.find((item) => location.pathname === item.to || location.pathname.startsWith(`${item.to}/`));
  const moreActive = Boolean(activeMoreItem);
  const moreMenuItems: MenuProps["items"] = [
    {
      type: "group" as const,
      label: t("nav.group.operations"),
      children: moreItems.filter((item) => item.group === "operations").map((item) => ({
        key: item.to,
        label: item.labelKey ? t(item.labelKey) : item.label,
      })),
    },
    {
      type: "group" as const,
      label: t("nav.group.configuration"),
      children: moreItems.filter((item) => item.group === "configuration").map((item) => ({
        key: item.to,
        label: item.labelKey ? t(item.labelKey) : item.label,
      })),
    },
    {
      type: "group" as const,
      label: t("nav.group.account"),
      children: moreItems.filter((item) => item.group === "account").map((item) => ({
        key: item.to,
        label: item.labelKey ? t(item.labelKey) : item.label,
      })),
    },
  ].filter((group) => (group.children?.length ?? 0) > 0);

  return (
    <header className="chat-topbar">
      <div className="chat-topbar-brand">
        <span className="chat-logo">Z</span>
        <span className="chat-brand-name">Z-Torque</span>
        <span className="chat-brand-sub">极矩</span>
      </div>
      <nav className="chat-topbar-nav" aria-label="主导航">
        {primaryItems.map((item) => (
          <NavLink key={item.to} to={item.to} className={({ isActive }) => "chat-nav-link" + (isActive ? " active" : "")}>
            {item.labelKey ? t(item.labelKey) : item.label}
          </NavLink>
        ))}
        {moreItems.length > 0 && (
          <Dropdown
            trigger={["click"]}
            menu={{
              items: moreMenuItems,
              selectedKeys: activeMoreItem ? [activeMoreItem.to] : [],
              onClick: ({ key }) => navigate(key),
            }}
          >
            <button type="button" className={"chat-nav-link chat-nav-more" + (moreActive ? " active" : "")}>
              {t("nav.system")} <DownOutlined />
            </button>
          </Dropdown>
        )}
      </nav>
      <div className="chat-topbar-user">
        <button type="button" className="chat-theme-toggle chat-locale-toggle" onClick={() => setLocale(locale === "zh" ? "en" : "zh")} title={t("top.language")} aria-label={t("top.language")}>
          <span className="chat-locale-label">{locale === "zh" ? "中" : "EN"}</span>
        </button>
        <button type="button" className="chat-theme-toggle" onClick={toggle} title={theme === "dark" ? t("top.theme.light") : t("top.theme.dark")} aria-label={theme === "dark" ? t("top.theme.light") : t("top.theme.dark")}>
          {theme === "dark" ? (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41-1.41M2 12h2M20 12h2M4.93 19.07l-1.41-1.41M17.66 6.34l-1.41-1.41" /></svg>
          ) : (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" /></svg>
          )}
        </button>
        <span className="chat-user-avatar">{user?.username.slice(0, 1).toUpperCase() ?? "U"}</span>
        <span className="chat-user-name">{user?.username ?? t("top.notLoggedIn")}</span>
        <button type="button" className="chat-btn-logout" onClick={() => void logout()}>{t("top.logout")}</button>
      </div>
    </header>
  );
}
