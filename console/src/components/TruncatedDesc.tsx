import { useState } from "react";
import { Modal } from "./Modal";

// 超过该长度视为"长说明"：表格内单行省略展示，点击「详情」弹窗查看全文
const LONG_LIMIT = 60;

interface TruncatedDescProps {
  text?: string | null;
  /** 弹窗标题，默认「详情」 */
  modalTitle?: string;
  /** 单行省略的最大宽度（px），默认 340，避免长文本撑宽表格列 */
  maxWidth?: number;
}

/**
 * 长文本说明的统一展示：默认单行省略（不改变页面布局），长文本额外提供
 * 「详情」入口，在弹窗中查看完整内容。短文本直接展示，无额外按钮。
 */
export function TruncatedDesc({ text, modalTitle = "详情", maxWidth = 340 }: TruncatedDescProps) {
  const [open, setOpen] = useState(false);
  const value = (text ?? "").trim();
  if (!value) return <>—</>;
  return (
    <>
      <span className="trunc-desc" style={{ maxWidth }} title={value}>
        {value}
      </span>
      {value.length > LONG_LIMIT && (
        <>
          <button className="btn btn-ghost btn-sm trunc-desc-more" onClick={() => setOpen(true)}>
            详情
          </button>
          <Modal open={open} title={modalTitle} onClose={() => setOpen(false)}>
            <pre className="trunc-desc-full">{value}</pre>
          </Modal>
        </>
      )}
    </>
  );
}
