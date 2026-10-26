import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties, MouseEvent as ReactMouseEvent } from "react";
import { readPanelWidth, writePanelWidth } from "../services/uiPreference";

/** 分栏宽度可调范围（px）：保证列表与详情两侧都保持可读 */
const MIN_PANE_W = 220;
const MAX_PANE_W = 560;

function clampWidth(w: number): number {
  return Math.min(MAX_PANE_W, Math.max(MIN_PANE_W, w));
}

/**
 * 左右分栏宽度状态：初始值读会话记忆；未设置时 width 为 null，
 * 沿用页面 CSS 变量的默认宽度（宽度默认值不在 JS 里硬编码）。
 * previewWidth 仅更新视图（拖拽中）；commitWidth 更新视图并写入会话记忆（松手时）。
 */
export function usePaneWidth(page: string, cssVar: string) {
  const [width, setWidth] = useState<number | null>(() => readPanelWidth(page));
  const previewWidth = useCallback((w: number) => setWidth(clampWidth(w)), []);
  const commitWidth = useCallback(
    (w: number) => {
      const clamped = clampWidth(w);
      setWidth(clamped);
      writePanelWidth(page, clamped);
    },
    [page],
  );
  // 已记忆宽度通过页面级 CSS 变量覆盖默认值；未记忆时不注入内联样式
  const style = (width != null ? { [cssVar]: `${width}px` } : undefined) as CSSProperties | undefined;
  return { width, style, previewWidth, commitWidth };
}

interface PaneResizerProps {
  onPreview: (width: number) => void;
  onCommit: (width: number) => void;
  /**
   * 右侧固定宽度面板模式（审计/用户页：右侧详情定宽、左侧列表自适应）：
   * 拖动方向取反，拖拽起点取把手右侧面板的宽度。
   */
  invert?: boolean;
}

export function PaneResizer({ onPreview, onCommit, invert = false }: PaneResizerProps) {
  const dragRef = useRef<{ startX: number; startW: number } | null>(null);

  useEffect(() => {
    const handleMove = (e: MouseEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const delta = e.clientX - drag.startX;
      onPreview(invert ? drag.startW - delta : drag.startW + delta);
    };
    const handleUp = (e: MouseEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      dragRef.current = null;
      document.body.classList.remove("pane-resizing");
      const delta = e.clientX - drag.startX;
      onCommit(invert ? drag.startW - delta : drag.startW + delta);
    };
    document.addEventListener("mousemove", handleMove);
    document.addEventListener("mouseup", handleUp);
    return () => {
      document.removeEventListener("mousemove", handleMove);
      document.removeEventListener("mouseup", handleUp);
      document.body.classList.remove("pane-resizing");
    };
  }, [onPreview, onCommit, invert]);

  const handleMouseDown = (e: ReactMouseEvent<HTMLDivElement>) => {
    const pane = (invert ? e.currentTarget.nextElementSibling : e.currentTarget.previousElementSibling) as HTMLElement | null;
    if (!pane) return;
    e.preventDefault();
    dragRef.current = { startX: e.clientX, startW: pane.getBoundingClientRect().width };
    document.body.classList.add("pane-resizing");
  };

  return (
    <div
      className="pane-resizer"
      role="separator"
      aria-orientation="vertical"
      title="拖动调整左右分栏宽度"
      onMouseDown={handleMouseDown}
    />
  );
}
