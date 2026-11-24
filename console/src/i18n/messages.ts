/**
 * 轻量中英字典（Phase 2 §四 i18n）：零 npm 依赖、零动态导入。
 *
 * 覆盖全站导航、顶栏与偏好设置页；业务页文案保持中文硬编码（渐进迁移，
 * 不为了"全量 i18n"引入 react-i18next 或大字典拖慢 exe 前端）。
 * 缺失 key 回退中文，再回退 key 本身，绝不显示空白。
 */

export type Locale = "zh" | "en";

export const MESSAGES: Record<Locale, Record<string, string>> = {
  zh: {
    "nav.chat": "会话管理",
    "nav.agents": "智能体管理",
    "nav.skills": "技能管理",
    "nav.models": "模型管理",
    "nav.connectors": "连接器管理",
    "nav.webhooks": "Webhook 接入",
    "nav.audit": "审计管理",
    "nav.users": "用户管理",
    "nav.settings": "偏好设置",
    "top.theme.light": "切换为浅色模式",
    "top.theme.dark": "切换为深色模式",
    "top.language": "切换语言",
    "top.logout": "退出",
    "top.notLoggedIn": "未登录",
    "settings.title": "偏好设置",
    "settings.subtitle": "语言、外观与阅读舒适度只保存在本机浏览器，不影响其他用户。",
    "settings.section.language": "语言",
    "settings.language.hint": "切换后导航与设置页立即更新；业务页文案将逐步覆盖。",
    "settings.language.zh": "简体中文",
    "settings.language.en": "English",
    "settings.section.appearance": "外观",
    "settings.appearance.mode": "主题模式",
    "settings.appearance.light": "浅色",
    "settings.appearance.dark": "深色",
    "settings.appearance.background": "背景",
    "settings.background.default": "默认",
    "settings.background.soft": "柔和",
    "settings.background.warm": "暖色",
    "settings.section.reading": "阅读",
    "settings.reading.fontScale": "字号",
    "settings.fontScale.compact": "紧凑",
    "settings.fontScale.normal": "标准",
    "settings.fontScale.comfortable": "舒适",
    "settings.preview": "预览",
    "settings.preview.text": "这是一段用于预览字号与背景的示例文字。",
    "settings.saved": "偏好已保存到本机浏览器",
  },
  en: {
    "nav.chat": "Conversations",
    "nav.agents": "Agents",
    "nav.skills": "Skills",
    "nav.models": "Models",
    "nav.connectors": "Connectors",
    "nav.webhooks": "Webhooks",
    "nav.audit": "Audit",
    "nav.users": "Users",
    "nav.settings": "Preferences",
    "top.theme.light": "Switch to light mode",
    "top.theme.dark": "Switch to dark mode",
    "top.language": "Switch language",
    "top.logout": "Sign out",
    "top.notLoggedIn": "Not signed in",
    "settings.title": "Preferences",
    "settings.subtitle": "Language, appearance and reading comfort are stored only in this browser.",
    "settings.section.language": "Language",
    "settings.language.hint": "Navigation and this page update immediately; business pages are being translated gradually.",
    "settings.language.zh": "简体中文",
    "settings.language.en": "English",
    "settings.section.appearance": "Appearance",
    "settings.appearance.mode": "Theme",
    "settings.appearance.light": "Light",
    "settings.appearance.dark": "Dark",
    "settings.appearance.background": "Background",
    "settings.background.default": "Default",
    "settings.background.soft": "Soft",
    "settings.background.warm": "Warm",
    "settings.section.reading": "Reading",
    "settings.reading.fontScale": "Font size",
    "settings.fontScale.compact": "Compact",
    "settings.fontScale.normal": "Normal",
    "settings.fontScale.comfortable": "Comfortable",
    "settings.preview": "Preview",
    "settings.preview.text": "This is a sample sentence for previewing font size and background.",
    "settings.saved": "Preferences saved in this browser",
  },
};

export function translate(locale: Locale, key: string): string {
  return MESSAGES[locale]?.[key] ?? MESSAGES.zh[key] ?? key;
}
