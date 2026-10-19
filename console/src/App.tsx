import { lazy, Suspense } from "react";
import type { ReactElement } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./stores/auth";
import { ToastProvider, useToast } from "./components/Toast";
import { LoginPage } from "./pages/Login";
import { AppTopbar, APP_NAV, visibleNav, homePath } from "./components/AppTopbar";
import type { NavItem } from "./components/AppTopbar";
import { LoadingScreen } from "./components/Loading";

// 路由级代码分割：首屏只加载登录页，其余页面按需加载（主 chunk 从 ~528KB 显著下降）
const ChatPage = lazy(() => import("./pages/Chat").then((m) => ({ default: m.ChatPage })));
const AgentsPage = lazy(() => import("./pages/Agents").then((m) => ({ default: m.AgentsPage })));
const SkillsPage = lazy(() => import("./pages/Skills").then((m) => ({ default: m.SkillsPage })));
const ModelsPage = lazy(() => import("./pages/Models").then((m) => ({ default: m.ModelsPage })));
const ConnectorsPage = lazy(() => import("./pages/Connectors").then((m) => ({ default: m.ConnectorsPage })));
const AuditPage = lazy(() => import("./pages/Audit").then((m) => ({ default: m.AuditPage })));
const UsersPage = lazy(() => import("./pages/Users").then((m) => ({ default: m.UsersPage })));

/**
 * 认证 + 访问守卫：未登录跳登录页；已登录但无对应导航访问权限时
 * （adminOnly 或缺少 permission），跳回其角色默认主页并提示原因。
 * 访问规则取自 APP_NAV 单一数据源，保证导航显隐与路由守卫一致。
 */
function RequireAccess({ navTo, children }: { navTo?: string; children: ReactElement }) {
  const { user, initializing } = useAuth();
  const location = useLocation();
  const { push: toast } = useToast();
  if (initializing) return <LoadingScreen />;
  // 登录态丢失时记住来源页，登录成功后回跳，避免用户上下文（如正在输入的对话）丢失
  if (!user) return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  if (navTo) {
    const item: NavItem | undefined = APP_NAV.find((n) => n.to === navTo);
    const allowed = visibleNav(item ? [item] : [], user).length > 0;
    if (!allowed) {
      // 显式告知用户被静默弹回的原因（不暴露英文错误码）
      toast("info", `没有访问该页面的权限，已返回主页`);
      return <Navigate to={homePath(user)} replace />;
    }
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
    <ToastProvider>
      <Suspense fallback={<LoadingScreen />}>
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
      </Suspense>
    </ToastProvider>
  );
}

export default App;
