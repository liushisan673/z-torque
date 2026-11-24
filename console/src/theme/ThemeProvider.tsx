/**
 * 主题提供者：全站唯一的 antd 配置入口。
 *
 * 三件事：
 *   1. 维护浅色/暗色模式（初始值来自 index.html 首屏防闪烁脚本写入的 data-theme）
 *   2. 把模式同步到 <html data-theme>，让残留的手写 CSS 变量同步切换
 *   3. 用 ConfigProvider 注入 design token + 中文语言包，并用 antd <App> 提供
 *      message/notification/modal 的上下文实例（component={false} 不额外插 DOM，避免影响现有 100vh 布局）
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { buildThemeConfig } from "./antdTheme";
import type { ThemeMode } from "./antdTheme";

const THEME_KEY = "z_torque_theme";

interface ThemeContextValue {
  mode: ThemeMode;
  setMode: (mode: ThemeMode) => void;
  toggle: () => ThemeMode;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

/** 读取首屏脚本已确定的主题 */
function initialMode(): ThemeMode {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

/** 是否已有用户显式选择（无则跟随系统） */
function hasExplicitChoice(): boolean {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === "light" || v === "dark";
  } catch {
    return false;
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(initialMode);

  /** 应用模式：先改 data-theme（CSS 变量立即生效），再持久化 */
  const apply = useCallback((next: ThemeMode, persist: boolean) => {
    document.documentElement.dataset.theme = next;
    setModeState(next);
    if (!persist) return;
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      /* localStorage 不可用时仅当前页生效 */
    }
  }, []);

  const setMode = useCallback((next: ThemeMode) => apply(next, true), [apply]);

  const toggle = useCallback(() => {
    const next: ThemeMode = (document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    apply(next, true);
    return next;
  }, [apply]);

  // 用户未显式选择时跟随系统偏好变化
  useEffect(() => {
    if (hasExplicitChoice()) return;
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const onChange = (e: MediaQueryListEvent) => apply(e.matches ? "dark" : "light", false);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [apply]);

  const ctx = useMemo<ThemeContextValue>(() => ({ mode, setMode, toggle }), [mode, setMode, toggle]);
  const themeConfig = useMemo(() => buildThemeConfig(mode), [mode]);

  return (
    <ThemeContext.Provider value={ctx}>
      <ConfigProvider theme={themeConfig} locale={zhCN}>
        {/* component={false}：不渲染包裹 div，避免破坏依赖 100vh 的现有布局 */}
        <AntdApp component={false}>{children}</AntdApp>
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}

/** 取主题上下文；必须在 ThemeProvider 内使用 */
export function useThemeMode(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useThemeMode 必须在 ThemeProvider 内使用");
  return ctx;
}
