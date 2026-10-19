import type { ReactNode } from "react";

/**
 * 「为什么不可用 + 去哪修」引导块。
 *
 * 全站统一的空态/不可用态模板：任何"这个功能现在用不了"的场景都必须同时给出
 * 原因（why）与出路（action），不再让用户面对一句干巴巴的"暂无数据"。
 */
export interface GuideCalloutProps {
  /** 一句话结论，如"暂无可用智能体" */
  title: string;
  /** 为什么不可用（可选，能给出具体数量/状态更好） */
  why?: ReactNode;
  /** 出路：跳转或动作 */
  actionLabel?: string;
  onAction?: () => void;
  /** 次要说明，如"也可以联系管理员发布" */
  hint?: ReactNode;
  compact?: boolean;
}

export function GuideCallout({ title, why, actionLabel, onAction, hint, compact = false }: GuideCalloutProps) {
  return (
    <div className={`guide-callout${compact ? " compact" : ""}`} role="note">
      <div className="guide-callout-icon" aria-hidden="true">!</div>
      <div className="guide-callout-body">
        <div className="guide-callout-title">{title}</div>
        {why && <div className="guide-callout-why">{why}</div>}
        {actionLabel && onAction && (
          <button className="btn btn-primary btn-sm guide-callout-action" type="button" onClick={onAction}>
            {actionLabel}
          </button>
        )}
        {hint && <div className="guide-callout-hint">{hint}</div>}
      </div>
    </div>
  );
}
