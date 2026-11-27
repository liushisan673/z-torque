import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { request } from "../services/api";
import { errorText } from "../services/errorText";
import { useAuth } from "../stores/auth";
import { isAdminUser } from "../components/AppTopbar";
import { GuideCallout } from "../components/GuideCallout";
import { useToast } from "../components/Toast";

interface OnboardingResult {
  configured: boolean;
  already_configured: boolean;
  provider_id: string;
}

interface OnboardingFormProps {
  /** 配置完成后的动作；页面形态下跳会话，弹窗形态下仅关闭弹窗 */
  onDone?: () => void;
  /** 弹窗内使用时隐藏大标题，避免与 Modal 标题重复 */
  compact?: boolean;
}

/** 桌面分发版：模型标识、接入地址和智能体已内置，用户只需填写 API Key。 */
export function OnboardingForm({ onDone, compact = false }: OnboardingFormProps) {
  const { push: toast } = useToast();
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (!apiKey.trim()) {
      setError("请填写模型 API Key。");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await request<OnboardingResult>("/api/z-torque/onboarding", {
        method: "POST",
        body: { api_key: apiKey.trim() },
      });
      setApiKey("");
      toast("success", result.already_configured ? "密钥已更新，原有智能体配置保持不变" : "配置完成，开始对话吧");
      onDone?.();
    } catch (err) {
      setError(errorText(err, "配置失败，请检查 API Key 后重试"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="onboarding-form-wrap">
      {!compact && (
        <>
          <h1>配置模型密钥</h1>
          <p className="onboarding-lead">
            系统已内置 DeepSeek 模型、智能体、技能与知识库，模型标识均已绑定。你只需填写 API Key，其它配置不要改动。
          </p>
        </>
      )}

      <div className="onboarding-form">
        <div className="form-field">
          <label>API Key</label>
          <input
            className="input"
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder="粘贴 DeepSeek 服务商提供的密钥"
            autoComplete="off"
          />
          <div className="field-help">密钥会加密存储，不会明文落库，也不会在页面回显。</div>
        </div>

        {error && <div className="chat-composer-error"><GuideCallout compact title="配置未完成" why={error} /></div>}

        <div className="onboarding-actions">
          <button type="button" className="btn btn-primary" onClick={() => void submit()} disabled={busy}>
            {busy ? "正在保存..." : "保存密钥并开始使用"}
          </button>
        </div>
      </div>

      <div className="onboarding-note">
        智能体、技能、知识库与模型标识均为系统内置配置，配置密钥后即可直接使用。
      </div>
    </div>
  );
}

/** 独立页面形态（从一键开通入口进入）。 */
export function OnboardingPage() {
  const { user } = useAuth();
  const navigate = useNavigate();

  if (!isAdminUser(user)) {
    return (
      <div className="onboarding-page">
        <div className="onboarding-card">
          <GuideCallout
            title="需要模型配置权限"
            why="首次配置密钥仅管理员或具备模型管理权限的账号可用。"
            actionLabel="返回会话"
            onAction={() => navigate("/chat", { replace: true })}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="onboarding-page">
      <div className="onboarding-card">
        <OnboardingForm onDone={() => navigate("/chat", { replace: true })} />
      </div>
    </div>
  );
}
