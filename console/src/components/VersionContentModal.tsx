import { Modal } from "./Modal";

export interface VersionContentSection {
  label: string;
  value: string;
  mono?: boolean;
}

interface VersionContentModalProps {
  open: boolean;
  title: string;
  onClose: () => void;
  sections: VersionContentSection[];
}

export function VersionContentModal({ open, title, onClose, sections }: VersionContentModalProps) {
  return (
    <Modal open={open} title={title} onClose={onClose} width={720} footer={<button className="btn btn-secondary" onClick={onClose}>关闭</button>}>
      <div className="version-content-modal">
        {sections.length === 0 ? (
          <div className="skill-readonly-banner">该版本暂无可展示内容。</div>
        ) : (
          sections.map((s) => (
            <section key={s.label} className="version-content-section">
              <h4>{s.label}</h4>
              <pre className={s.mono ? "mono" : undefined}>{s.value || "—"}</pre>
            </section>
          ))
        )}
      </div>
    </Modal>
  );
}
