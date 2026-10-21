/**
 * 页面选中项跨路由记忆：跳转到其他页面再返回时恢复上次选中，
 * 直到用户点击其他条目为止。sessionStorage 按标签页隔离，各标签页互不干扰。
 *
 * 约定：记忆 id 在列表中已不存在（删除/停用隐藏/无权限）时，由页面回退到默认第一项。
 */
export function readSelectedId(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null; // 存储不可用时退化为"无记忆"
  }
}

/**
 * 写入选中记忆。id 为 null 时移除记忆（回退默认）；
 * 传空串表示"记住当前无选中"（如会话页的新对话状态），返回时保持空白。
 */
export function writeSelectedId(key: string, id: string | null): void {
  try {
    if (id === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, id);
  } catch {
    /* 存储不可用时仅当前页生效 */
  }
}
