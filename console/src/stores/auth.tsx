import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { request, onAuthFailure, setAccessToken, setRefreshToken } from "../services/api";
import { clearUiPreferences } from "../services/uiPreference";
import type { UserInfo } from "../types";

interface AuthContextValue {
  user: UserInfo | null;
  initializing: boolean;
  /** 登录成功后返回最新的用户信息（含角色/权限），供按角色跳转 */
  login: (username: string, password: string) => Promise<UserInfo | null>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserInfo | null>(null);
  const [initializing, setInitializing] = useState(true);

  const loadUser = useCallback(async (): Promise<UserInfo | null> => {
    try {
      const me = await request<UserInfo>("/api/z-torque/auth/me");
      setUser(me);
      return me;
    } catch {
      setUser(null);
      return null;
    }
  }, []);

  useEffect(() => {
    onAuthFailure(() => setUser(null));
    void loadUser().finally(() => setInitializing(false));
  }, [loadUser]);

  const login = useCallback(async (username: string, password: string): Promise<UserInfo | null> => {
    const res = await request<{ access_token: string; refresh_token?: string; token_type?: string }>("/api/z-torque/auth/login", {
      method: "POST",
      body: { username, password },
    });
    setAccessToken(res.access_token);
    // 保存 refresh_token：access token 过期（30 分钟）时由请求层自动刷新，避免闪退登出
    setRefreshToken(res.refresh_token ?? null);
    return await loadUser();
  }, [loadUser]);

  const logout = useCallback(async () => {
    try {
      await request("/api/z-torque/auth/logout", { method: "POST" });
    } finally {
      setAccessToken(null);
      setRefreshToken(null);
      // 会话级 UI 偏好（如分栏宽度）随登出清除，下次登录恢复默认
      clearUiPreferences();
      setUser(null);
    }
  }, []);

  const value = useMemo(
    () => ({ user, initializing, login, logout }),
    [user, initializing, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
