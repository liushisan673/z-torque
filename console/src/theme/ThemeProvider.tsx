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
import enUS from "antd/locale/en_US";
import zhCN from "antd/locale/zh_CN";
import { translate } from "../i18n/messages";
import type { Locale } from "../i18n/messages";
import { buildThemeConfig } from "./antdTheme";
import type { ThemeMode } from "./antdTheme";

const THEME_KEY = "z_torque_theme";
const LOCALE_KEY = "z_torque_locale";
const BACKGROUND_KEY = "z_torque_background";
const FONT_KEY = "z_torque_font_scale";

export type BackgroundChoice = "default" | "soft" | "warm";
export type FontScale = "compact" | "normal" | "comfortable";

interface ThemeContextValue {
  mode: ThemeMode;
  setMode: (mode: ThemeMode) => void;
  toggle: () => ThemeMode;
  locale: Locale;
  setLocale: (locale: Locale) => void;
  background: BackgroundChoice;
  setBackground: (background: BackgroundChoice) => void;
  fontScale: FontScale;
  setFontScale: (scale: FontScale) => void;
  t: (key: string) => string;
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

/** 读取有白名单的本地偏好；值非法时回退默认，不让脏 localStorage 破坏界面 */
function storedChoice<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const value = localStorage.getItem(key);
    return value && allowed.includes(value as T) ? (value as T) : fallback;
  } catch {
    return fallback;
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(initialMode);
  const [locale, setLocaleState] = useState<Locale>(() => storedChoice(LOCALE_KEY, ["zh", "en"] as const, "zh"));
  const [background, setBackgroundState] = useState<BackgroundChoice>(() => storedChoice(BACKGROUND_KEY, ["default", "soft", "warm"] as const, "default"));
  const [fontScale, setFontScaleState] = useState<FontScale>(() => storedChoice(FONT_KEY, ["compact", "normal", "comfortable"] as const, "normal"));

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

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    try {
      localStorage.setItem(LOCALE_KEY, next);
    } catch {
      /* localStorage 不可用时仅当前页生效 */
    }
  }, []);

  const setBackground = useCallback((next: BackgroundChoice) => {
    setBackgroundState(next);
    try {
      localStorage.setItem(BACKGROUND_KEY, next);
    } catch {
      /* 同上 */
    }
  }, []);

  const setFontScale = useCallback((next: FontScale) => {
    setFontScaleState(next);
    try {
      localStorage.setItem(FONT_KEY, next);
    } catch {
      /* 同上 */
    }
  }, []);

  // 背景/字号通过 data-* 落到根元素，由 index.css 的 CSS 变量统一派生
  useEffect(() => {
    document.documentElement.dataset.bg = background;
    document.documentElement.dataset.font = fontScale;
  }, [background, fontScale]);

  // 用户未显式选择时跟随系统偏好变化
  useEffect(() => {
    if (hasExplicitChoice()) return;
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const onChange = (e: MediaQueryListEvent) => apply(e.matches ? "dark" : "light", false);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [apply]);

  const t = useCallback((key: string) => translate(locale, key), [locale]);
  const ctx = useMemo<ThemeContextValue>(
    () => ({ mode, setMode, toggle, locale, setLocale, background, setBackground, fontScale, setFontScale, t }),
    [mode, setMode, toggle, locale, setLocale, background, setBackground, fontScale, setFontScale, t],
  );
  const themeConfig = useMemo(() => buildThemeConfig(mode), [mode]);

  return (
    <ThemeContext.Provider value={ctx}>
      <ConfigProvider theme={themeConfig} locale={locale === "en" ? enUS : zhCN}>
        {/* component={false}：不渲染包裹 div，避免破坏依赖 100vh 的现有布局 */}
        <AntdApp component={false}>{children}</AntdApp>
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}

/** 取主题/偏好上下文；必须在 ThemeProvider 内使用 */
export function useThemeMode(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useThemeMode 必须在 ThemeProvider 内使用");
  return ctx;
}

/** 语义别名：新代码用 usePreferences 表达语言/背景/字号偏好 */
export const usePreferences = useThemeMode;
