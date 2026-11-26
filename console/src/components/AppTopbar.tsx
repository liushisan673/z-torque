import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import {
  AppstoreOutlined, AuditOutlined, CloudServerOutlined, DatabaseOutlined,
  MenuFoldOutlined, MenuUnfoldOutlined, MoonOutlined, RobotOutlined,
  SettingOutlined, SunOutlined, TeamOutlined, ToolOutlined, ApiOutlined, LogoutOutlined,
} from "@ant-design/icons";
import { useAuth } from "../stores/auth";
import { useThemeMode } from "../theme/ThemeProvider";
import type { UserInfo } from "../types";

export interface NavItem {
  to: string;
  label: string;
  labelKey?: string;
  permission?: string;
  adminOnly?: boolean;
  section?: "primary" | "more";
  group?: "operations" | "configuration" | "account";
}

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

export function isAdminUser(user: UserInfo | null): boolean {
  if (!user) return false;
  if (user.roles?.includes("admin")) return true;
  const perms = user.permissions ?? [];
  if (perms.includes("admin.manage")) return true;
  return perms.some((p) => p.startsWith("user.") || p.startsWith("role.") || p.startsWith("system."));
}

export function homePath(_user: UserInfo | null): string {
  return "/chat";
}

export function visibleNav(nav: NavItem[], user: UserInfo | null): NavItem[] {
  const admin = isAdminUser(user);
  const perms = user?.permissions ?? [];
  return nav.filter((item) => {
    if (item.adminOnly && !admin) return false;
    if (item.permission && !admin && !perms.includes(item.permission)) return false;
    return true;
  });
}

const NAV_ICONS: Record<string, ReactNode> = {
  "/chat": <AppstoreOutlined />,
  "/knowledge": <DatabaseOutlined />,
  "/agents": <RobotOutlined />,
  "/skills": <ToolOutlined />,
  "/audit": <AuditOutlined />,
  "/models": <CloudServerOutlined />,
  "/connectors": <ApiOutlined />,
  "/webhooks": <ApiOutlined />,
  "/users": <TeamOutlined />,
  "/settings": <SettingOutlined />,
};

/** 导航展开态：默认展开，用户选择持久化，刷新后保持 */
const NAV_EXPANDED_KEY = "z_torque_nav_expanded";

function readNavExpanded(): boolean {
  try {
    return localStorage.getItem(NAV_EXPANDED_KEY) !== "0";
  } catch {
    return true;
  }
}

function writeNavExpanded(expanded: boolean): void {
  try {
    localStorage.setItem(NAV_EXPANDED_KEY, expanded ? "1" : "0");
  } catch {
    /* 隐私模式下仅当前页生效 */
  }
}

/** 全站统一的左侧主导航；Chat 与管理页面共用同一权限过滤和导航数据源。 */
export function AppTopbar({ nav }: { nav: NavItem[] }) {
  const { user, logout } = useAuth();
  const items = visibleNav(nav, user);
  const primaryItems = items.filter((item) => item.section !== "more");
  const moreItems = items.filter((item) => item.section === "more");
  const { mode: theme, toggle, locale, setLocale, t } = useThemeMode();
  const location = useLocation();
  const [expanded, setExpanded] = useState(readNavExpanded);
  const collapsed = !expanded;
  const label = (item: NavItem) => (item.labelKey ? t(item.labelKey) : item.label);
  const groups = [
    { key: "operations", title: t("nav.group.operations"), items: moreItems.filter((item) => item.group === "operations") },
    { key: "configuration", title: t("nav.group.configuration"), items: moreItems.filter((item) => item.group === "configuration") },
    { key: "account", title: t("nav.group.account"), items: moreItems.filter((item) => item.group === "account") },
  ];

  useEffect(() => {
    writeNavExpanded(expanded);
  }, [expanded]);

  return (
    <aside
      className={`app-nav-sidebar${collapsed ? " is-collapsed" : ""}`}
      aria-label="主导航"
    >
      <div className="app-nav-brand">
        <button
          type="button"
          className="app-nav-logo"
          onClick={() => setExpanded((value) => !value)}
          title={collapsed ? "展开导航" : "收起导航"}
          aria-label={collapsed ? "展开导航" : "收起导航"}
          aria-expanded={!collapsed}
        >
          <span className="chat-logo">苍擎</span>
          <span className="app-nav-logo-toggle" aria-hidden>
            {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
          </span>
        </button>
        {!collapsed && (
          <span className="app-nav-brand-copy">
            <strong>苍擎 AI</strong>
            <small>企业数字员工平台</small>
          </span>
        )}
      </div>
      <nav className="app-nav-list">
        <div className="app-nav-group">
          {!collapsed && <div className="app-nav-group-title">工作空间</div>}
          {primaryItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => "app-nav-link" + (isActive ? " active" : "")}
              title={collapsed ? label(item) : undefined}
              aria-label={label(item)}
            >
              <span className="app-nav-icon">{NAV_ICONS[item.to]}</span>
              {!collapsed && <span className="app-nav-label">{label(item)}</span>}
            </NavLink>
          ))}
        </div>
        {groups.map((group) => group.items.length > 0 && (
          <div className="app-nav-group" key={group.key}>
            {!collapsed && <div className="app-nav-group-title">{group.title}</div>}
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  "app-nav-link" + (isActive || location.pathname.startsWith(item.to + "/") ? " active" : "")
                }
                title={collapsed ? label(item) : undefined}
                aria-label={label(item)}
              >
                <span className="app-nav-icon">{NAV_ICONS[item.to]}</span>
                {!collapsed && <span className="app-nav-label">{label(item)}</span>}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>
      <div className="app-nav-footer">
        <div className="app-nav-user">
          <span className="chat-user-avatar">{user?.username.slice(0, 1).toUpperCase() ?? "U"}</span>
          {!collapsed && (
            <span className="app-nav-username" title={user?.username}>
              {user?.username ?? t("top.notLoggedIn")}
            </span>
          )}
        </div>
        <div className="app-nav-tools">
          <button
            type="button"
            className="app-nav-tool"
            onClick={() => setLocale(locale === "zh" ? "en" : "zh")}
            title={t("top.language")}
            aria-label={t("top.language")}
          >
            {locale === "zh" ? "中" : "EN"}
          </button>
          <button
            type="button"
            className="app-nav-tool"
            onClick={toggle}
            title={theme === "dark" ? t("top.theme.light") : t("top.theme.dark")}
            aria-label={theme === "dark" ? t("top.theme.light") : t("top.theme.dark")}
          >
            {theme === "dark" ? <SunOutlined /> : <MoonOutlined />}
          </button>
          <button
            type="button"
            className="app-nav-tool app-nav-logout"
            onClick={() => void logout()}
            title={t("top.logout")}
            aria-label={t("top.logout")}
          >
            <LogoutOutlined />
            <span className="app-nav-logout-label">{t("top.logout")}</span>
          </button>
        </div>
      </div>
    </aside>
  );
}
