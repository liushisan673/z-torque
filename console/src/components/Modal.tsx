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
  // Esc 关闭 + Tab 焦点圈定：键盘用户的焦点始终留在弹窗内，不会穿透到背景页面
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key === "Tab" && bodyRef.current) {
        const focusables = Array.from(
          bodyRef.current.querySelectorAll<HTMLElement>(
            'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
          ),
        ).filter((el) => el.offsetParent !== null);
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey && (active === first || !bodyRef.current.contains(active))) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && (active === last || !bodyRef.current.contains(active))) {
          e.preventDefault();
          first.focus();
        }
      }
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
