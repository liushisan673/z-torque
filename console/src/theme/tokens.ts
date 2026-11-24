/**
 * 设计令牌单一来源（DSH 三层结构的 Z-Torque 版）
 *
 * 结构对齐 DeepSeek Harness 桌面版（`@deepseek-ai/dsh-client-ui-theme`）：
 *   1. palette   调色板 —— 只放原始色阶，不含语义
 *   2. semantic  语义别名 —— 界面只用这一层（浅色/暗色各一份）
 *   3. geometry  几何与动效 —— 圆角/控件高度/字号/阴影/过渡
 *
 * 规则（来自 DSH 的「简洁」机制，改任何一页都必须遵守）：
 *   · 描边用发丝线：4% / 10% / 12% / 16% 黑（暗色为白），不画灰色实线
 *   · 阴影几乎不可见：0.5px 描边 + 两层 3% 透明，只用来托层次
 *   · 主色只用于「动作」：主按钮 / 链接 / 选中态；界面本体是近黑文字 + 白底
 *   · 字号、控件高度各两档，不新增第三档
 */

/** 原始色阶：中性色带轻微蓝调（neutral-bluish），与 DSH 一致 */
export const palette = {
  neutral: {
    0: "#ffffff",
    50: "#f9fafb",
    60: "#f5f6f7",
    75: "#f1f3f5",
    100: "#ebeef2",
    150: "#e9ecf2",
    200: "#e1e5ee",
    300: "#cfd3d6",
    400: "#adb2b8",
    500: "#979da6",
    600: "#81858c",
    700: "#61666b",
    750: "#43454a",
    800: "#353638",
    850: "#2c2c2e",
    875: "#232324",
    900: "#1b1b1c",
    950: "#151517",
    1000: "#0f1115",
  },
  blue: {
    50: "#eff6ff",
    75: "#e5f0ff",
    100: "#dbeafe",
    300: "#93c5fd",
    400: "#60a5fa",
    450: "#4d93f8",
    500: "#3b82f6",
    600: "#2563eb",
    800: "#1e40af",
  },
  /** DSH 的品牌强调蓝（浅色模式下的 new-color-primary） */
  brand: "#4176e6",
  /** 暗色模式下的品牌蓝（DSH deepseek-450 量级） */
  brandDark: "#4d6bfe",
  green: { 100: "#e6faed", 400: "#4ed17e", 500: "#22c55e", 600: "#1a8f4c" },
  red: { 50: "#fef2f2", 100: "#fee2e2", 400: "#f25a5a", 500: "#ef4444", 600: "#ec1313", 700: "#c00d0d" },
  amber: { 100: "#fef5e7", 400: "#f7ad31", 500: "#f59e0b", 600: "#b45309" },
} as const;

/** 语义令牌：业务代码与组件库只允许引用这一层 */
export interface SemanticTokens {
  /* 背景 */
  bgBase: string;
  bgLayout: string;
  bgContainer: string;
  bgElevated: string;
  bgLayer2: string;
  bgLayer3: string;
  /** 悬浮态叠加遮罩（hover 用，保持透明叠加而不是换底色） */
  bgHover: string;
  bgActive: string;

  /* 文字 */
  textPrimary: string;
  textSecondary: string;
  textTertiary: string;
  textQuaternary: string;
  /** 主色底上的文字（近白） */
  textOnPrimary: string;

  /* 描边：发丝线，四档透明度 */
  borderL1: string;
  borderL2: string;
  borderL3: string;
  borderL4: string;
  /** 阴影外描边（0.5px） */
  elevationStroke: string;

  /* 主色与状态色 */
  brand: string;
  brandHover: string;
  brandActive: string;
  brandSoft: string;

  success: string;
  successSoft: string;
  successText: string;

  warning: string;
  warningSoft: string;
  warningText: string;

  danger: string;
  dangerSoft: string;
  dangerText: string;

  info: string;
  infoSoft: string;
  infoText: string;
}

export const lightTokens: SemanticTokens = {
  bgBase: palette.neutral[0],
  bgLayout: palette.neutral[50],
  bgContainer: palette.neutral[0],
  bgElevated: palette.neutral[0],
  bgLayer2: palette.neutral[60],
  bgLayer3: palette.neutral[75],
  bgHover: "rgba(0,0,0,0.04)",
  bgActive: "rgba(0,0,0,0.07)",

  textPrimary: palette.neutral[1000],
  textSecondary: palette.neutral[700],
  textTertiary: palette.neutral[600],
  textQuaternary: palette.neutral[500],
  textOnPrimary: palette.neutral[0],

  borderL1: "rgba(0,0,0,0.04)",
  borderL2: "rgba(0,0,0,0.10)",
  borderL3: "rgba(0,0,0,0.12)",
  borderL4: "rgba(0,0,0,0.16)",
  elevationStroke: "rgba(0,0,0,0.06)",

  brand: palette.brand,
  brandHover: "#3663d0",
  brandActive: "#2a53b8",
  brandSoft: palette.blue[50],

  success: palette.green[500],
  successSoft: palette.green[100],
  successText: palette.green[600],

  warning: palette.amber[500],
  warningSoft: palette.amber[100],
  warningText: palette.amber[600],

  danger: palette.red[600],
  dangerSoft: palette.red[50],
  dangerText: palette.red[700],

  info: palette.blue[600],
  infoSoft: palette.blue[50],
  infoText: palette.blue[800],
};

export const darkTokens: SemanticTokens = {
  bgBase: palette.neutral[950],
  bgLayout: palette.neutral[900],
  bgContainer: palette.neutral[875],
  bgElevated: palette.neutral[850],
  bgLayer2: palette.neutral[850],
  bgLayer3: palette.neutral[800],
  bgHover: "rgba(255,255,255,0.06)",
  bgActive: "rgba(255,255,255,0.10)",

  textPrimary: palette.neutral[50],
  textSecondary: palette.neutral[400],
  textTertiary: palette.neutral[600],
  textQuaternary: palette.neutral[500],
  textOnPrimary: palette.neutral[0],

  borderL1: "rgba(255,255,255,0.06)",
  borderL2: "rgba(255,255,255,0.12)",
  borderL3: "rgba(255,255,255,0.16)",
  borderL4: "rgba(255,255,255,0.20)",
  elevationStroke: "rgba(255,255,255,0.08)",

  brand: palette.brandDark,
  brandHover: "#6b85ff",
  brandActive: "#3d5cf0",
  brandSoft: "#232e4d",

  success: palette.green[400],
  successSoft: "#1c3226",
  successText: palette.green[400],

  warning: palette.amber[400],
  warningSoft: "#382c1c",
  warningText: palette.amber[400],

  danger: palette.red[400],
  dangerSoft: "#3a2226",
  dangerText: palette.red[400],

  info: palette.blue[400],
  infoSoft: "#1e2c3d",
  infoText: palette.blue[300],
};

/**
 * 几何与动效。
 * 圆角四档（4/8/12/16）与控件高度两档（36/28）直接照搬 DSH，不再自造数值。
 */
export const geometry = {
  radiusXS: 4,
  radiusSM: 8,
  radius: 12,
  radiusLG: 16,
  /** 胶囊（标签/筛选器） */
  radiusPill: 999,

  controlHeight: 36,
  controlHeightSM: 28,
  controlHeightLG: 44,

  fontSize: 14,
  fontSizeSM: 12,
  fontSizeLG: 16,
  lineHeight: 1.5714,

  /** DSH elevation-soft：0.5px 描边 + 两层 3% 阴影 */
  shadowSoft: "0 0 0 0.5px rgba(0,0,0,0.06), 0 4px 16px rgba(0,0,0,0.03), 0 0 24px rgba(0,0,0,0.03)",
  /** DSH elevation-panel：更贴近面板的投影 */
  shadowPanel: "0 0 0 0.5px rgba(0,0,0,0.06), 0 3px 8px rgba(0,0,0,0.03), 0 0 16px rgba(0,0,0,0.02)",

  motionFast: "0.12s",
  motionMid: "0.2s",
} as const;

export const fontStack =
  '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif';
export const monoStack = 'ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", monospace';
