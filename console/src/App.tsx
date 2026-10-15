import type { ReactElement } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./stores/auth";
import { LoginPage } from "./pages/Login";
import { ChatPage } from "./pages/Chat";
import { AgentsPage } from "./pages/Agents";
import { SkillsPage } from "./pages/Skills";
import { ModelsPage } from "./pages/Models";
import { ConnectorsPage } from "./pages/Connectors";
import { AuditPage } from "./pages/Audit";
import { UsersPage } from "./pages/Users";
import { AppTopbar, APP_NAV, visibleNav, homePath } from "./components/AppTopbar";
import type { NavItem } from "./components/AppTopbar";
import { LoadingScreen } from "./components/Loading";

/**
 * 认证 + 访问守卫：未登录跳登录页；已登录但无对应导航访问权限时
 * （adminOnly 或缺少 permission），跳回其角色默认主页。
 * 访问规则取自 APP_NAV 单一数据源，保证导航显隐与路由守卫一致。
 */
function RequireAccess({ navTo, children }: { navTo?: string; children: ReactElement }) {
  const { user, initializing } = useAuth();
  if (initializing) return <LoadingScreen />;
  if (!user) return <Navigate to="/login" replace />;
  if (navTo) {
    const item: NavItem | undefined = APP_NAV.find((n) => n.to === navTo);
    const allowed = visibleNav(item ? [item] : [], user).length > 0;
    if (!allowed) return <Navigate to={homePath(user)} replace />;
  }
  return children;
}

// 按角色分流默认主页（管理员为用户管理，普通用户为会话）
function HomeRedirect() {
  const { user, initializing } = useAuth();
  if (initializing) return <LoadingScreen />;
  return <Navigate to={user ? homePath(user) : "/login"} replace />;
}

// 管理页外壳：顶栏 + 内容区（Chat 页自带顶栏，无需此壳）
function ConsoleShell({ children }: { children: ReactElement }) {
  return (
    <div className="app-shell">
      <AppTopbar nav={APP_NAV} />
      <main className="content">
        {children}
      </main>
    </div>
  );
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/chat" element={<RequireAccess><ChatPage /></RequireAccess>} />
      <Route path="/agents" element={<RequireAccess><ConsoleShell><AgentsPage /></ConsoleShell></RequireAccess>} />
      <Route path="/skills" element={<RequireAccess><ConsoleShell><SkillsPage /></ConsoleShell></RequireAccess>} />
      <Route path="/models" element={<RequireAccess navTo="/models"><ConsoleShell><ModelsPage /></ConsoleShell></RequireAccess>} />
      <Route path="/connectors" element={<RequireAccess navTo="/connectors"><ConsoleShell><ConnectorsPage /></ConsoleShell></RequireAccess>} />
      <Route path="/audit" element={<RequireAccess><ConsoleShell><AuditPage /></ConsoleShell></RequireAccess>} />
      <Route path="/users" element={<RequireAccess navTo="/users"><ConsoleShell><UsersPage /></ConsoleShell></RequireAccess>} />
      <Route path="*" element={<HomeRedirect />} />
    </Routes>
  );
}

export default App;
