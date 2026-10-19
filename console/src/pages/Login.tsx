import { useState } from "react";
import type { FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../stores/auth";
import { homePath } from "../components/AppTopbar";
import { ApiRequestError } from "../services/api";
import { Alert } from "../components/ui";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!username || !password) {
      setError("请输入用户名和密码");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await login(username, password).then((u) => {
        // 会话过期被踢到登录页时回跳来源页；常规登录进入角色默认主页
        const from = (location.state as { from?: string } | null)?.from;
        const target = from && from.startsWith("/") && from !== "/login" ? from : homePath(u);
        navigate(target, { replace: true });
      });
    } catch (err) {
      if (err instanceof ApiRequestError) {
        setError(err.code === "AI_AUTH_FAILED" ? "用户名或密码错误" : err.message);
      } else {
        setError("登录失败，请稍后重试");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-shell">
      <div className="login-panel">
        <div className="login-left" aria-hidden="true">
          <div className="login-left-inner">
            <div className="login-left-brand">
              <span className="login-left-mark">Z</span>
              <span className="login-left-name">Z-Torque</span>
            </div>
            <div className="login-left-title">极矩 · 企业 AI 运行平台</div>
            <div className="login-left-subtitle">开源内核 + 轻量控制台<br />让 AI 真正产生扭矩，数据完全私有化部署</div>

            <div className="login-left-features">
              <div className="login-left-feature">
                <div className="login-left-feature-title">智能体 / 技能编排</div>
                <div className="login-left-feature-desc">可版本化智能体，可插拔技能，支持委派与父子任务</div>
              </div>
              <div className="login-left-feature">
                <div className="login-left-feature-title">可审计任务执行</div>
                <div className="login-left-feature-desc">状态机 + Outbox，支持故障恢复与人工确认</div>
              </div>
              <div className="login-left-feature">
                <div className="login-left-feature-title">私有化与多模型</div>
                <div className="login-left-feature-desc">OpenAI / DeepSeek / 通义 / GLM / 本地 Ollama 自由切换</div>
              </div>
            </div>
          </div>
        </div>

        <div className="login-right">
          <div className="login-right-card card">
            <div className="login-right-head">
              <div className="login-right-brand">
                <span className="login-right-mark" aria-hidden="true">Z</span>
                <span>Z-Torque</span>
              </div>
              <div className="login-right-title">欢迎回来</div>
              <div className="login-right-hint">请输入用户名和密码登录控制台</div>
            </div>

            <form onSubmit={onSubmit}>
              {error ? (
                <div style={{ marginBottom: 12 }}>
                  <Alert kind="error">{error}</Alert>
                </div>
              ) : null}

              <div className="form-field">
                <label htmlFor="username">用户名</label>
                <input
                  id="username"
                  className="input"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="username"
                  autoFocus
                />
              </div>

              <div className="form-field">
                <label htmlFor="password">密码</label>
                <div className="login-password-wrap">
                  <input
                    id="password"
                    className="input"
                    type={showPassword ? "text" : "password"}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoComplete="current-password"
                  />
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm login-password-toggle"
                    onClick={() => setShowPassword((v) => !v)}
                    aria-label={showPassword ? "隐藏密码" : "显示密码"}
                    title={showPassword ? "隐藏密码" : "显示密码"}
                  />
                </div>
              </div>

              <button className="btn btn-primary login-submit" type="submit" disabled={submitting}>
                {submitting ? "登录中…" : "登录"}
              </button>

              <div className="login-right-foot">
                <div>由 Z-Torque Community 驱动 · 极矩社区版</div>
                <div>内核协议 v1 · AGPL-3.0</div>
              </div>
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}
