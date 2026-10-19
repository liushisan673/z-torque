import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";

interface ModalProps {
  open: boolean;
  title: string;
  onClose: () => void;
  footer?: ReactNode;
  children: ReactNode;
  width?: number;
  /** 高危弹窗（如级联删除）应设为 false：避免误点遮罩就关闭 */
  closeOnBackdrop?: boolean;
}

export function Modal({ open, title, onClose, footer, children, width = 560, closeOnBackdrop = true }: ModalProps) {
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const titleId = useId();
  // Esc 关闭：弹窗打开期间全局监听（open 变化时挂载/卸载）
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // 打开后把焦点移入弹窗内部（首个可聚焦控件），键盘用户不必再手动 Tab 进弹窗
  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => {
      const first = bodyRef.current?.querySelector<HTMLElement>(
        "input:not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled])",
      );
      (first ?? bodyRef.current)?.focus();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [open]);

  if (!open) return null;
  return (
    <div className="modal-backdrop" onClick={closeOnBackdrop ? onClose : undefined}>
      <div
        className="modal"
        style={{ width }}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        ref={bodyRef}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <span id={titleId}>{title}</span>
          <button className="btn btn-ghost btn-sm" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}
