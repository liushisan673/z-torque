/**
 * 会话级 UI 偏好（如左右分栏宽度）：sessionStorage 持久化，
 * 会话内（页面跳转/刷新）保持；退出登录时由 clearUiPreferences() 清除，
 * 下次登录恢复默认值。sessionStorage 按标签页隔离，各标签页互不干扰。
 */
const PANEL_WIDTH_PREFIX = "zt.ui.panelWidth.";

/** 读取某页面记录的分栏宽度；未设置或无效时返回 null（使用 CSS 变量默认宽度） */
export function readPanelWidth(page: string): number | null {
  try {
    const raw = sessionStorage.getItem(PANEL_WIDTH_PREFIX + page);
    if (!raw) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? Math.round(n) : null;
  } catch {
    return null;
  }
}

/** 写入分栏宽度（px）；传 null 移除记录并回退默认值 */
export function writePanelWidth(page: string, width: number | null): void {
  try {
    if (width === null) sessionStorage.removeItem(PANEL_WIDTH_PREFIX + page);
    else sessionStorage.setItem(PANEL_WIDTH_PREFIX + page, String(Math.round(width)));
  } catch {
    /* 存储不可用时仅当前视图生效 */
  }
}

/** 退出登录时清除全部会话级 UI 偏好（下次登录恢复默认分栏） */
export function clearUiPreferences(): void {
  try {
    const keys: string[] = [];
    for (let i = 0; i < sessionStorage.length; i++) {
      const key = sessionStorage.key(i);
      if (key && key.startsWith(PANEL_WIDTH_PREFIX)) keys.push(key);
    }
    keys.forEach((key) => sessionStorage.removeItem(key));
  } catch {
    /* 存储不可用时忽略 */
  }
}
