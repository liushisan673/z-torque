import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { request } from "../services/api";
import { errorText } from "../services/errorText";
import { useAuth } from "../stores/auth";
import { isAdminUser } from "../components/AppTopbar";
import { GuideCallout } from "../components/GuideCallout";
import { useToast } from "../components/Toast";

const PRESETS = [
  { name: "DeepSeek", baseUrl: "https://api.deepseek.com/v1", modelId: "deepseek-chat" },
  { name: "OpenAI", baseUrl: "https://api.openai.com/v1", modelId: "gpt-4o-mini" },
  { name: "通义千问", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", modelId: "qwen-plus" },
  { name: "Moonshot Kimi", baseUrl: "https://api.moonshot.cn/v1", modelId: "moonshot-v1-8k" },
  { name: "Ollama 本地", baseUrl: "http://127.0.0.1:11434/v1", modelId: "qwen2.5:7b" },
];

interface OnboardingResult {
  already_exists: boolean;
  agent_id: string;
  conversation_id: string;
}

interface OnboardingFormProps {
  /** 配置完成后的动作；页面形态下跳会话，弹窗形态下仅关闭弹窗 */
  onDone?: () => void;
  /** 弹窗内使用时隐藏大标题，避免与 Modal 标题重复 */
  compact?: boolean;
}

/** 开通向导表单：独立页面使用，compact 模式可嵌入弹窗。 */
export function OnboardingForm({ onDone, compact = false }: OnboardingFormProps) {
  const { push: toast } = useToast();
  const [vendor, setVendor] = useState("DeepSeek");
  const [baseUrl, setBaseUrl] = useState("https://api.deepseek.com/v1");
  const [modelId, setModelId] = useState("deepseek-chat");
  const [apiKey, setApiKey] = useState("");
  const [assistantName, setAssistantName] = useState("我的助手");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function applyPreset(name: string) {
    const preset = PRESETS.find((item) => item.name === name);
    if (!preset) return;
    setVendor(preset.name);
    setBaseUrl(preset.baseUrl);
    setModelId(preset.modelId);
  }

  async function submit() {
    if (!vendor.trim() || !baseUrl.trim() || !modelId.trim() || !apiKey.trim() || !assistantName.trim()) {
      setError("请填写厂商、接入地址、模型标识、API Key 与助手名称。");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await request<OnboardingResult>("/api/z-torque/onboarding", {
        method: "POST",
        body: {
          vendor: vendor.trim(),
          base_url: baseUrl.trim(),
          model_id: modelId.trim(),
          api_key: apiKey.trim(),
          assistant_name: assistantName.trim(),
          system_prompt: systemPrompt.trim() || undefined,
          interface_type: "openai_compatible",
        },
      });
      setApiKey("");
      toast("success", result.already_exists ? "助手已就绪，可以直接对话" : "配置完成，开始对话吧");
      onDone?.();
    } catch (err) {
      setError(errorText(err, "开通失败，请检查填写内容后重试"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="onboarding-form-wrap">
      {!compact && (
        <>
          <h1>三步开始使用</h1>
          <p className="onboarding-lead">只需配置一次：选择厂商、填入 API Key、确认模型。系统会自动创建助手并打开会话。</p>
        </>
      )}

      <div className="onboarding-form">
        <div className="form-field">
          <label>厂商预设</label>
          <select className="input" value={vendor} onChange={(event) => applyPreset(event.target.value)}>
            {PRESETS.map((preset) => (
              <option key={preset.name} value={preset.name}>{preset.name}</option>
            ))}
            <option value="自定义">自定义</option>
          </select>
          <div className="field-help">选择预设会自动填入接入地址与常用模型标识，仍可修改。</div>
        </div>

        <div className="form-field">
          <label>API Key</label>
          <input
            className="input"
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder="粘贴服务商提供的密钥"
            autoComplete="off"
          />
          <div className="field-help">密钥会加密存储，不会明文落库，也不会在页面回显。</div>
        </div>

        <div className="form-field">
          <label>接入地址 Base URL</label>
          <input className="input" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.example.com/v1" />
        </div>

        <div className="form-field">
          <label>模型标识</label>
          <input className="input" value={modelId} onChange={(event) => setModelId(event.target.value)} placeholder="deepseek-chat" />
          <div className="field-help">服务商控制台里的模型名，例如 deepseek-chat、gpt-4o-mini。</div>
        </div>

        <div className="form-field">
          <label>助手名称</label>
          <input className="input" value={assistantName} onChange={(event) => setAssistantName(event.target.value)} placeholder="我的助手" />
        </div>

        <div className="form-field">
          <label>助手定位（可选）</label>
          <input className="input" value={systemPrompt} onChange={(event) => setSystemPrompt(event.target.value)} placeholder="一句话说明助手负责什么" />
        </div>

        {error && <div className="chat-composer-error"><GuideCallout compact title="配置未完成" why={error} /></div>}

        <div className="onboarding-actions">
          <button type="button" className="btn btn-primary" onClick={() => void submit()} disabled={busy}>
            {busy ? "正在创建..." : "完成配置并开始对话"}
          </button>
        </div>
      </div>

      <div className="onboarding-note">
        之后可以在「设置 → 模型接入」里更换模型或添加更多供应商；助手提示词与技能可在「智能体」中继续完善。
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
            why="开通向导会创建模型接入与平台助手，仅管理员或具备模型管理权限的账号可用。"
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
