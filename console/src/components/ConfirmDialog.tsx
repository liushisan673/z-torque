import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Modal } from "./Modal";

/**
 * 统一确认对话框（替代各页面散落的 window.confirm）。
 *
 * 三级强度约定：
 * - 只读/可逆操作：不弹确认；
 * - 一般不可逆操作（归档、停用）：danger 未开启，destructive=false；
 * - 高危不可逆操作（级联删除）：destructive=true，禁用遮罩关闭，必要时要求输入确认文本。
 */
export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** 说明这次操作会发生什么、影响范围是什么 */
  children?: ReactNode;
  confirmText?: string;
  cancelText?: string;
  /** 高危操作：确认按钮变红、禁止点击遮罩关闭 */
  destructive?: boolean;
  /** 要求用户输入这段文字才能确认（如资源名 / 用户名） */
  requireText?: string;
  requireLabel?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  children,
  confirmText = "确定",
  cancelText = "取消",
  destructive = false,
  requireText,
  requireLabel,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const [typed, setTyped] = useState("");
  // 每次打开重置输入，避免上一次残留文本让"确认"误判为可用
  useEffect(() => {
    if (open) setTyped("");
  }, [open]);

  const needText = typeof requireText === "string" && requireText.length > 0;
  const textMatched = !needText || typed.trim() === requireText;

  return (
    <Modal
      open={open}
      title={title}
      onClose={onCancel}
      closeOnBackdrop={!destructive}
      width={480}
      footer={
        <div className="confirm-foot">
          <button className="btn btn-secondary btn-sm" type="button" onClick={onCancel} disabled={busy}>
            {cancelText}
          </button>
          <button
            className={`btn btn-sm ${destructive ? "btn-danger" : "btn-primary"}`}
            type="button"
            disabled={busy || !textMatched}
            onClick={onConfirm}
          >
            {busy ? "处理中…" : confirmText}
          </button>
        </div>
      }
    >
      {children ? <div className="confirm-body">{children}</div> : null}
      {needText && (
        <div className="form-field" style={{ marginTop: 12 }}>
          <label htmlFor="confirm-typed">
            {requireLabel ?? `请输入「${requireText}」以确认`}
          </label>
          <input
            id="confirm-typed"
            className="input"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={requireText}
            autoComplete="off"
          />
        </div>
      )}
    </Modal>
  );
}
