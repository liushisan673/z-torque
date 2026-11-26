/** 管理页次级列表的抽屉入口，与会话页的会话列表开关保持一致的交互。 */
export function ListDrawerToggle({ onClick, label = "打开列表" }: { onClick: () => void; label?: string }) {
  return (
    <button type="button" className="list-drawer-toggle" onClick={onClick} aria-label={label} title={label}>
      <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M3 6h18M3 12h18M3 18h18" />
      </svg>
      <span>列表</span>
    </button>
  );
}
