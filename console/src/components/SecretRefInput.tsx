import { useState } from "react";
import { request, ApiRequestError } from "../services/api";

interface SecretRefInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}

/**
 * Secret URI 输入组件：既可直接粘贴 secret:// 引用，
 * 也可粘贴明文密钥一键转换为 Secret URI（明文不落库，后端 Fernet 加密后仅返回引用）。
 */
export function SecretRefInput({ value, onChange, placeholder }: SecretRefInputProps) {
  const [plain, setPlain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function convert() {
    if (!plain.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const data = await request<{ secret_ref: string }>("/api/z-torque/secrets/convert", {
        method: "POST",
        body: { value: plain.trim() },
      });
      onChange(data.secret_ref);
      setPlain("");
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "转换失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="secret-ref">
      <input
        className="input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder ?? "secret://…"}
        autoComplete="off"
      />
      <div className="secret-ref-convert">
        <input
          className="input"
          value={plain}
          onChange={(e) => setPlain(e.target.value)}
          placeholder="或粘贴明文密钥，一键转换为 Secret URI"
          autoComplete="off"
        />
        <button type="button" className="btn btn-secondary" disabled={busy || !plain.trim()} onClick={() => void convert()}>
          {busy ? "转换中…" : "转换"}
        </button>
      </div>
      <span className="hint">{error ?? "明文仅用于本次转换，不保存；转换后以 secret:// 引用交付后端加密存储"}</span>
    </div>
  );
}
