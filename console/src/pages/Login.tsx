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
              <span className="login-left-mark">苍擎</span>
              <span className="login-left-name">苍擎 AI</span>
              <span className="login-left-brand-sub">企业数字员工平台</span>
            </div>
            <div className="login-left-body">
              <p className="login-left-eyebrow">企业数字员工平台</p>
              <h1 className="login-left-title">让 AI 真正上岗。</h1>
              <p className="login-left-subtitle">AI 不缺大脑，缺的是一份工作。</p>
            </div>
            <ol className="login-points">
              <li className="login-point">
                <span className="login-point-index">01</span>
                <span className="login-point-copy">
                  <strong>干得成</strong>
                  <span>一件事管到底，从会议室到产线</span>
                </span>
              </li>
              <li className="login-point">
                <span className="login-point-index">02</span>
                <span className="login-point-copy">
                  <strong>管得住</strong>
                  <span>权限与全程留痕，越界即拦截</span>
                </span>
              </li>
              <li className="login-point">
                <span className="login-point-index">03</span>
                <span className="login-point-copy">
                  <strong>说得清</strong>
                  <span>结论带原始依据，可回溯可导出</span>
                </span>
              </li>
            </ol>
            <p className="login-left-foot">西安栈上明月软件科技有限公司</p>
          </div>
        </div>

        <div className="login-right">
          <div className="login-right-card card">
            <div className="login-right-head">
              <div className="login-right-brand">
                <span className="login-right-mark" aria-hidden="true">苍擎</span>
                <span>苍擎 AI</span>
              </div>
              <div className="login-right-title">欢迎回来</div>
              <div className="login-right-hint">登录企业数字员工平台控制台</div>
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

              <div className="field-help">桌面版内置账号：admin / admin123；首次登录后请按提示配置模型密钥。</div>

              <button className="btn btn-primary login-submit" type="submit" disabled={submitting}>
                {submitting ? "登录中…" : "登录"}
              </button>

              <div className="login-right-foot">
                <div>西安栈上明月软件科技有限公司 · zsoftym.com</div>
                <div>苍擎 AI · 企业数字员工平台</div>
              </div>
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}
