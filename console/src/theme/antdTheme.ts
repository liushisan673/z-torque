/**
 * antd 主题：把 tokens.ts 的语义令牌翻译成 antd ThemeConfig。
 *
 * 只在这里出现 antd token 名称，业务组件不直接写颜色/圆角字面量。
 * 暗色通过 `theme.darkAlgorithm` + 显式覆盖背景色实现，
 * 不再依赖各页面手写 CSS 变量覆写（原先暗色靠逐页 20+ 条变量映射）。
 */
import { theme } from "antd";
import type { ThemeConfig } from "antd";
import { darkTokens, fontStack, geometry, lightTokens, monoStack } from "./tokens";
import type { SemanticTokens } from "./tokens";

export type ThemeMode = "light" | "dark";

/** 由语义令牌生成 antd 配置（浅色/暗色共用同一份结构） */
function buildToken(t: SemanticTokens): ThemeConfig["token"] {
  return {
    /* 主色：只用于动作（主按钮/链接/选中态） */
    colorPrimary: t.brand,
    colorPrimaryHover: t.brandHover,
    colorPrimaryActive: t.brandActive,
    colorPrimaryBg: t.brandSoft,
    colorTextLightSolid: t.textOnPrimary,
    colorLink: t.brand,
    colorLinkHover: t.brandHover,

    /* 状态色 */
    colorSuccess: t.success,
    colorWarning: t.warning,
    colorError: t.danger,
    colorInfo: t.info,

    /* 文字：近黑为主，层级靠三级灰 */
    colorText: t.textPrimary,
    colorTextHeading: t.textPrimary,
    colorTextSecondary: t.textSecondary,
    colorTextTertiary: t.textTertiary,
    colorTextQuaternary: t.textQuaternary,

    /* 背景 */
    colorBgBase: t.bgBase,
    colorBgLayout: t.bgLayout,
    colorBgContainer: t.bgContainer,
    colorBgElevated: t.bgElevated,
    colorFillQuaternary: t.bgLayer2,
    colorFillTertiary: t.bgLayer3,

    /* 描边：发丝线（4% / 10%），不画灰色实线 */
    colorBorder: t.borderL2,
    colorBorderSecondary: t.borderL1,
    colorSplit: t.borderL1,

    /* 几何：四档圆角 + 两档控件高度 */
    borderRadius: geometry.radius,
    borderRadiusXS: geometry.radiusXS,
    borderRadiusSM: geometry.radiusSM,
    borderRadiusLG: geometry.radiusLG,
    controlHeight: geometry.controlHeight,
    controlHeightSM: geometry.controlHeightSM,
    controlHeightLG: geometry.controlHeightLG,

    /* 排版：正文 14 / 辅助 12 / 标题 16 */
    fontSize: geometry.fontSize,
    fontSizeSM: geometry.fontSizeSM,
    fontSizeLG: geometry.fontSizeLG,
    lineHeight: geometry.lineHeight,
    fontFamily: fontStack,
    fontFamilyCode: monoStack,

    /* 阴影：0.5px 描边 + 两层 3% */
    boxShadow: geometry.shadowSoft,
    boxShadowSecondary: geometry.shadowPanel,

    motionDurationFast: geometry.motionFast,
    motionDurationMid: geometry.motionMid,
  };
}

function buildComponents(t: SemanticTokens): ThemeConfig["components"] {
  return {
    Button: {
      fontWeight: 500,
      // 去掉 antd 默认的实心投影，保持 DSH 的"扁平 + 发丝线"观感
      primaryShadow: "none",
      defaultShadow: "none",
      dangerShadow: "none",
      paddingInline: 14,
      paddingInlineSM: 10,
    },
    Card: {
      paddingLG: 20,
      headerFontSize: geometry.fontSize,
      headerBg: "transparent",
    },
    Layout: {
      headerBg: t.bgContainer,
      bodyBg: t.bgLayout,
      siderBg: t.bgLayer2,
      headerHeight: 56,
      headerPadding: "0 24px",
    },
    Table: {
      headerBg: t.bgLayout,
      headerColor: t.textSecondary,
      headerSplitColor: "transparent",
      borderColor: t.borderL1,
      rowHoverBg: t.bgLayer2,
      cellPaddingBlock: 10,
      cellPaddingInline: 12,
      cellFontSize: 13,
    },
    Modal: {
      contentBg: t.bgElevated,
      headerBg: t.bgElevated,
      titleFontSize: geometry.fontSizeLG,
    },
    Menu: {
      itemBg: "transparent",
      subMenuItemBg: "transparent",
      itemColor: t.textSecondary,
      itemHoverColor: t.textPrimary,
      itemHoverBg: t.bgHover,
      // 导航选中态用中性叠加，不用主色填充
      itemSelectedBg: t.bgHover,
      itemSelectedColor: t.textPrimary,
      itemBorderRadius: geometry.radiusSM,
      itemHeight: 34,
    },
    Tabs: {
      itemColor: t.textSecondary,
      itemSelectedColor: t.textPrimary,
      itemHoverColor: t.textPrimary,
      inkBarColor: t.textPrimary,
      horizontalItemGutter: 24,
    },
    Segmented: {
      trackBg: t.bgLayer2,
      itemSelectedBg: t.bgContainer,
      itemSelectedColor: t.textPrimary,
    },
    Input: {
      paddingBlock: 7,
      paddingInline: 11,
    },
    Select: {
      optionSelectedBg: t.brandSoft,
    },
    Tooltip: {
      colorBgSpotlight: t.textPrimary,
    },
  };
}

/** 生成指定模式下的 antd ThemeConfig */
export function buildThemeConfig(mode: ThemeMode): ThemeConfig {
  const t = mode === "dark" ? darkTokens : lightTokens;
  return {
    algorithm: mode === "dark" ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: buildToken(t),
    components: buildComponents(t),
  };
}
