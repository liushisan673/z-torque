import { useCallback, useEffect, useMemo, useState } from "react";
import { apiBaseUrl, request } from "../services/api";
import { errorText } from "../services/errorText";
import type { Agent, InboundWebhook } from "../types";
import { Loading } from "../components/Loading";
import { Modal } from "../components/Modal";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusTag } from "../components/ui";
import { useToast } from "../components/Toast";
import { useAuth } from "../stores/auth";

interface FormState {
  name: string;
  description: string;
  targetAgentId: string;
  authMode: "HMAC" | "TOKEN";
  messagePath: string;
  conversationKeyPath: string;
  eventIdPath: string;
  // 媒体映射：留空表示纯文本 Webhook
  mediaPath: string;
  mediaTypePath: string;
  mediaSourcePath: string;
  mediaMimePath: string;
  mediaFileNamePath: string;
  mediaPlaceholders: string;
  enabled: boolean;
  // 回写：任务终态后把最终答复送回外部系统
  replyUrl: string;
  replyToken: string;
  replyAccountPath: string;
  replyPeerPath: string;
  // 高级：请求头与签名（对接自建外部系统）
  signatureHeader: string;
  timestampHeader: string;
  eventIdHeader: string;
  tokenHeader: string;
  tokenPrefix: string;
  signatureAlgorithm: "SHA256" | "SHA1";
  signatureEncoding: "HEX" | "BASE64";
  signaturePrefix: string;
  signatureTemplate: string;
  toleranceSeconds: string;
}

const EMPTY_FORM: FormState = {
  name: "",
  description: "",
  targetAgentId: "",
  authMode: "HMAC",
  messagePath: "text",
  conversationKeyPath: "conversation_key",
  eventIdPath: "event_id",
  mediaPath: "",
  mediaTypePath: "type",
  mediaSourcePath: "filePath",
  mediaMimePath: "mimeType",
  mediaFileNamePath: "file_name",
  mediaPlaceholders: "[图片],[语音],[视频],[文件]",
  enabled: true,
  replyUrl: "",
  replyToken: "",
  replyAccountPath: "account_id",
  replyPeerPath: "peer_id",
  signatureHeader: "X-Webhook-Signature",
  timestampHeader: "X-Webhook-Timestamp",
  eventIdHeader: "X-Webhook-Event-Id",
  tokenHeader: "Authorization",
  tokenPrefix: "Bearer ",
  signatureAlgorithm: "SHA256",
  signatureEncoding: "HEX",
  signaturePrefix: "sha256=",
  signatureTemplate: "{timestamp}.{body}",
  toleranceSeconds: "300",
};

interface SecretReveal {
  name: string;
  endpoint: string;
  secret: string;
  authMode: string;
  signatureHeader: string;
  timestampHeader: string;
  signatureAlgorithm: string;
  signatureEncoding: string;
  signaturePrefix: string;
  signatureTemplate: string;
  tokenHeader: string;
  tokenPrefix: string;
}

export function WebhooksPage() {
  const { user } = useAuth();
  const canManage = (user?.permissions ?? []).includes("connector.manage");
  const { push: toast } = useToast();

  const [items, setItems] = useState<InboundWebhook[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<InboundWebhook | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [reveal, setReveal] = useState<SecretReveal | null>(null);
  const [rotateTarget, setRotateTarget] = useState<InboundWebhook | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<InboundWebhook | null>(null);

  const publishedAgents = useMemo(
    () => agents.filter((a) => a.status === "ACTIVE" && a.current_published_version_id),
    [agents],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [hooks, agentRows] = await Promise.all([
        request<InboundWebhook[]>("/api/z-torque/webhooks"),
        request<Agent[]>("/api/z-torque/agents"),
      ]);
      setItems(hooks);
      setAgents(agentRows);
    } catch (err) {
      setError(errorText(err, "加载 Webhook 失败，请稍后重试"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function openCreate() {
    setForm({ ...EMPTY_FORM, targetAgentId: publishedAgents[0]?.id ?? "" });
    setCreateOpen(true);
  }

  function openEdit(item: InboundWebhook) {
    setForm({
      name: item.name,
      description: item.description ?? "",
      targetAgentId: item.target_agent_id,
      authMode: item.auth_mode === "TOKEN" ? "TOKEN" : "HMAC",
      messagePath: item.message_path,
      conversationKeyPath: item.conversation_key_path,
      eventIdPath: item.event_id_path,
      mediaPath: item.media_path ?? "",
      mediaTypePath: item.media_type_path,
      mediaSourcePath: item.media_source_path,
      mediaMimePath: item.media_mime_path,
      mediaFileNamePath: item.media_file_name_path,
      mediaPlaceholders: (item.media_placeholder_texts ?? []).join(","),
      enabled: item.enabled,
      replyUrl: item.reply_url ?? "",
      replyToken: "",
      replyAccountPath: item.reply_account_path,
      replyPeerPath: item.reply_peer_path,
      signatureHeader: item.signature_header,
      timestampHeader: item.timestamp_header,
      eventIdHeader: item.event_id_header,
      tokenHeader: item.token_header,
      tokenPrefix: item.token_prefix,
      signatureAlgorithm: item.signature_algorithm === "SHA1" ? "SHA1" : "SHA256",
      signatureEncoding: item.signature_encoding === "BASE64" ? "BASE64" : "HEX",
      signaturePrefix: item.signature_prefix,
      signatureTemplate: item.signature_template,
      toleranceSeconds: String(item.timestamp_tolerance_seconds),
    });
    setEditTarget(item);
  }

  function payloadOf(state: FormState) {
    const tolerance = Number.parseInt(state.toleranceSeconds, 10);
    const payload: Record<string, unknown> = {
      name: state.name.trim(),
      description: state.description.trim() || null,
      target_agent_id: state.targetAgentId,
      auth_mode: state.authMode,
      message_path: state.messagePath.trim(),
      conversation_key_path: state.conversationKeyPath.trim(),
      event_id_path: state.eventIdPath.trim(),
      media_path: state.mediaPath.trim() || null,
      media_type_path: state.mediaTypePath.trim() || "type",
      media_source_path: state.mediaSourcePath.trim() || "filePath",
      media_mime_path: state.mediaMimePath.trim() || "mimeType",
      media_file_name_path: state.mediaFileNamePath.trim() || "file_name",
      media_placeholder_texts: state.mediaPlaceholders
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
      reply_url: state.replyUrl.trim(),
      reply_account_path: state.replyAccountPath.trim(),
      reply_peer_path: state.replyPeerPath.trim(),
      signature_header: state.signatureHeader.trim(),
      timestamp_header: state.timestampHeader.trim(),
      event_id_header: state.eventIdHeader.trim(),
      token_header: state.tokenHeader.trim(),
      token_prefix: state.tokenPrefix,
      signature_algorithm: state.signatureAlgorithm,
      signature_encoding: state.signatureEncoding,
      signature_prefix: state.signaturePrefix,
      signature_template: state.signatureTemplate.trim(),
      timestamp_tolerance_seconds: Number.isFinite(tolerance) ? tolerance : 300,
      enabled: state.enabled,
    };
    // 令牌只写不读：留空表示保持原值，不覆盖已保存的令牌
    if (state.replyToken.trim()) payload.reply_token = state.replyToken.trim();
    return payload;
  }

  async function submitCreate() {
    if (!form.name.trim() || !form.targetAgentId) return;
    setBusy(true);
    try {
      const created = await request<InboundWebhook>("/api/z-torque/webhooks", {
        method: "POST",
        body: payloadOf(form),
      });
      setCreateOpen(false);
      setReveal({
        name: created.name,
        endpoint: `${apiBaseUrl()}${created.inbound_path}`,
        secret: created.secret ?? "",
        authMode: created.auth_mode,
        signatureHeader: created.signature_header,
        timestampHeader: created.timestamp_header,
        signatureAlgorithm: created.signature_algorithm,
        signatureEncoding: created.signature_encoding,
        signaturePrefix: created.signature_prefix,
        signatureTemplate: created.signature_template,
        tokenHeader: created.token_header,
        tokenPrefix: created.token_prefix,
      });
      await load();
    } catch (err) {
      toast("error", errorText(err, "创建 Webhook 失败"));
    } finally {
      setBusy(false);
    }
  }

  async function submitEdit() {
    if (!editTarget || !form.name.trim() || !form.targetAgentId) return;
    setBusy(true);
    try {
      await request(`/api/z-torque/webhooks/${editTarget.id}`, {
        method: "PATCH",
        body: payloadOf(form),
      });
      setEditTarget(null);
      toast("success", "配置已保存");
      await load();
    } catch (err) {
      toast("error", errorText(err, "保存失败"));
    } finally {
      setBusy(false);
    }
  }

  async function toggleEnabled(item: InboundWebhook) {
    setBusy(true);
    try {
      await request(`/api/z-torque/webhooks/${item.id}`, {
        method: "PATCH",
        body: { enabled: !item.enabled },
      });
      await load();
    } catch (err) {
      toast("error", errorText(err, "切换状态失败"));
    } finally {
      setBusy(false);
    }
  }

  async function rotateSecret() {
    if (!rotateTarget) return;
    setBusy(true);
    try {
      const updated = await request<InboundWebhook>(`/api/z-torque/webhooks/${rotateTarget.id}/rotate-secret`, {
        method: "POST",
      });
      setReveal({
        name: updated.name,
        endpoint: `${apiBaseUrl()}${updated.inbound_path}`,
        secret: updated.secret ?? "",
        authMode: updated.auth_mode,
        signatureHeader: updated.signature_header,
        timestampHeader: updated.timestamp_header,
        signatureAlgorithm: updated.signature_algorithm,
        signatureEncoding: updated.signature_encoding,
        signaturePrefix: updated.signature_prefix,
        signatureTemplate: updated.signature_template,
        tokenHeader: updated.token_header,
        tokenPrefix: updated.token_prefix,
      });
      setRotateTarget(null);
      await load();
    } catch (err) {
      toast("error", errorText(err, "密钥轮换失败"));
    } finally {
      setBusy(false);
    }
  }

  async function removeWebhook() {
    if (!deleteTarget) return;
    setBusy(true);
    try {
      await request(`/api/z-torque/webhooks/${deleteTarget.id}`, { method: "DELETE" });
      setDeleteTarget(null);
      toast("success", "Webhook 已删除");
      await load();
    } catch (err) {
      toast("error", errorText(err, "删除失败"));
    } finally {
      setBusy(false);
    }
  }

  async function copyText(text: string, label: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast("success", `${label}已复制`);
    } catch {
      toast("info", "复制失败，请手动选中复制");
    }
  }

  const formBody = (
    <div className="form-grid">
      <div className="form-field">
        <label>名称</label>
        <input
          className="input"
          value={form.name}
          onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
          placeholder="例如：订单系统回调"
        />
      </div>
      <div className="form-field">
        <label>目标智能体</label>
        <select
          className="input"
          value={form.targetAgentId}
          onChange={(e) => setForm((prev) => ({ ...prev, targetAgentId: e.target.value }))}
        >
          <option value="">请选择已发布智能体</option>
          {publishedAgents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.name}
            </option>
          ))}
        </select>
      </div>
      <div className="form-field">
        <label>签名方式</label>
        <select
          className="input"
          value={form.authMode}
          onChange={(e) => setForm((prev) => ({ ...prev, authMode: e.target.value === "TOKEN" ? "TOKEN" : "HMAC" }))}
        >
          <option value="HMAC">HMAC-SHA256（推荐）</option>
          <option value="TOKEN">Bearer Token</option>
        </select>
        <div className="field-help">
          {form.authMode === "HMAC"
            ? `请求头 ${form.timestampHeader}（秒级时间戳）与 ${form.signatureHeader}，签名值形如 ${form.signaturePrefix}<签名>，签名内容为 ${form.signatureTemplate}`
            : `请求头 ${form.tokenHeader}: ${form.tokenPrefix || ""}<密钥>`}
        </div>
      </div>
      <div className="form-field">
        <label>描述</label>
        <input
          className="input"
          value={form.description}
          onChange={(e) => setForm((prev) => ({ ...prev, description: e.target.value }))}
          placeholder="可选"
        />
      </div>
      <div className="form-field">
        <label>消息内容路径</label>
        <input
          className="input"
          value={form.messagePath}
          onChange={(e) => setForm((prev) => ({ ...prev, messagePath: e.target.value }))}
          placeholder="text 或 data.content"
        />
        <div className="field-help">从回调 JSON 中取值作为发给智能体的消息，支持 a.b / a[0].b。</div>
      </div>
      <div className="form-field">
        <label>会话键路径</label>
        <input
          className="input"
          value={form.conversationKeyPath}
          onChange={(e) => setForm((prev) => ({ ...prev, conversationKeyPath: e.target.value }))}
          placeholder="conversation_key"
        />
        <div className="field-help">相同键的消息会进入同一会话；取不到时使用 default。</div>
      </div>
      <div className="form-field">
        <label>事件 ID 路径</label>
        <input
          className="input"
          value={form.eventIdPath}
          onChange={(e) => setForm((prev) => ({ ...prev, eventIdPath: e.target.value }))}
          placeholder="event_id"
        />
        <div className="field-help">用于幂等去重；取不到时用请求体哈希，回调方重试不会重复执行。</div>
      </div>
      <div className="form-field">
        <label>媒体字段路径（可选）</label>
        <input
          className="input"
          value={form.mediaPath}
          onChange={(e) => setForm((prev) => ({ ...prev, mediaPath: e.target.value }))}
          placeholder="message.media"
        />
        <div className="field-help">留空表示纯文本 Webhook；wechat-bot 填 message.media。媒体文件仍受 INBOUND_MEDIA_ROOTS 白名单约束。</div>
      </div>
      {form.mediaPath.trim() && (
        <>
          <div className="form-field">
            <div style={{ display: "flex", gap: 8 }}>
              <label>
                类型字段
                <input className="input" value={form.mediaTypePath} onChange={(e) => setForm((prev) => ({ ...prev, mediaTypePath: e.target.value }))} placeholder="type" />
              </label>
              <label>
                文件路径字段
                <input className="input" value={form.mediaSourcePath} onChange={(e) => setForm((prev) => ({ ...prev, mediaSourcePath: e.target.value }))} placeholder="filePath" />
              </label>
            </div>
          </div>
          <div className="form-field">
            <div style={{ display: "flex", gap: 8 }}>
              <label>
                MIME 字段
                <input className="input" value={form.mediaMimePath} onChange={(e) => setForm((prev) => ({ ...prev, mediaMimePath: e.target.value }))} placeholder="mimeType" />
              </label>
              <label>
                文件名字段
                <input className="input" value={form.mediaFileNamePath} onChange={(e) => setForm((prev) => ({ ...prev, mediaFileNamePath: e.target.value }))} placeholder="file_name" />
              </label>
            </div>
          </div>
          <div className="form-field">
            <label>占位文本（逗号分隔）</label>
            <input
              className="input"
              value={form.mediaPlaceholders}
              onChange={(e) => setForm((prev) => ({ ...prev, mediaPlaceholders: e.target.value }))}
              placeholder="[图片],[语音],[视频],[文件]"
            />
            <div className="field-help">有附件时占位文本不会作为真实消息内容。</div>
          </div>
        </>
      )}
      <div className="form-field">
        <label>
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm((prev) => ({ ...prev, enabled: e.target.checked }))}
          />{" "}
          启用
        </label>
      </div>
      <details style={{ gridColumn: "1 / -1" }}>
        <summary style={{ cursor: "pointer", fontWeight: 600 }}>回写：把智能体最终答复送回外部系统</summary>
        <div className="form-grid" style={{ marginTop: 12 }}>
          <div className="form-field">
            <label>回写地址</label>
            <input
              className="input"
              value={form.replyUrl}
              onChange={(e) => setForm((prev) => ({ ...prev, replyUrl: e.target.value }))}
              placeholder="http://127.0.0.1:18011/v1/messages/send"
            />
            <div className="field-help">
              任务结束后按 {"{accountId, peerId, text}"} POST 到该地址；留空则不回写。智能体已成功调用发送工具时自动跳过。
            </div>
          </div>
          <div className="form-field">
            <label>回写令牌</label>
            <input
              className="input"
              type="password"
              value={form.replyToken}
              onChange={(e) => setForm((prev) => ({ ...prev, replyToken: e.target.value }))}
              placeholder={editTarget?.reply_token_configured ? "已配置，留空保持不变" : "可选，Bearer 令牌"}
            />
            <div className="field-help">以 Authorization: Bearer &lt;令牌&gt; 发送；留空不修改已保存的令牌。</div>
          </div>
          <div className="form-field">
            <label>账号字段路径</label>
            <input
              className="input"
              value={form.replyAccountPath}
              onChange={(e) => setForm((prev) => ({ ...prev, replyAccountPath: e.target.value }))}
              placeholder="account_id"
            />
            <div className="field-help">从回调 JSON 中取回写目标账号。</div>
          </div>
          <div className="form-field">
            <label>用户字段路径</label>
            <input
              className="input"
              value={form.replyPeerPath}
              onChange={(e) => setForm((prev) => ({ ...prev, replyPeerPath: e.target.value }))}
              placeholder="peer_id"
            />
            <div className="field-help">从回调 JSON 中取回写目标用户（如微信 peer_id）。</div>
          </div>
        </div>
      </details>
      <details style={{ gridColumn: "1 / -1" }}>
        <summary style={{ cursor: "pointer", fontWeight: 600 }}>高级：请求头与签名（对接自建外部系统）</summary>
        <div className="form-grid" style={{ marginTop: 12 }}>
          {form.authMode === "HMAC" ? (
            <>
              <div className="form-field">
                <label>签名请求头</label>
                <input
                  className="input"
                  value={form.signatureHeader}
                  onChange={(e) => setForm((prev) => ({ ...prev, signatureHeader: e.target.value }))}
                  placeholder="X-Webhook-Signature"
                />
              </div>
              <div className="form-field">
                <label>时间戳请求头</label>
                <input
                  className="input"
                  value={form.timestampHeader}
                  onChange={(e) => setForm((prev) => ({ ...prev, timestampHeader: e.target.value }))}
                  placeholder="X-Webhook-Timestamp"
                />
              </div>
              <div className="form-field">
                <label>签名算法</label>
                <select
                  className="input"
                  value={form.signatureAlgorithm}
                  onChange={(e) =>
                    setForm((prev) => ({ ...prev, signatureAlgorithm: e.target.value === "SHA1" ? "SHA1" : "SHA256" }))
                  }
                >
                  <option value="SHA256">SHA256</option>
                  <option value="SHA1">SHA1</option>
                </select>
              </div>
              <div className="form-field">
                <label>签名编码</label>
                <select
                  className="input"
                  value={form.signatureEncoding}
                  onChange={(e) =>
                    setForm((prev) => ({ ...prev, signatureEncoding: e.target.value === "BASE64" ? "BASE64" : "HEX" }))
                  }
                >
                  <option value="HEX">HEX（十六进制）</option>
                  <option value="BASE64">BASE64</option>
                </select>
              </div>
              <div className="form-field">
                <label>签名值前缀</label>
                <input
                  className="input"
                  value={form.signaturePrefix}
                  onChange={(e) => setForm((prev) => ({ ...prev, signaturePrefix: e.target.value }))}
                  placeholder="sha256="
                />
                <div className="field-help">为空表示请求头只携带签名值本身。</div>
              </div>
              <div className="form-field">
                <label>签名内容模板</label>
                <input
                  className="input"
                  value={form.signatureTemplate}
                  onChange={(e) => setForm((prev) => ({ ...prev, signatureTemplate: e.target.value }))}
                  placeholder="{timestamp}.{body}"
                />
                <div className="field-help">
                  必须包含 {"{body}"}；可包含 {"{timestamp}"}。常见写法：{"{timestamp}.{body}"}、{"{body}"}、{"{timestamp}:{body}"}。
                </div>
              </div>
              <div className="form-field">
                <label>时间戳容差（秒）</label>
                <input
                  className="input"
                  type="number"
                  min={0}
                  max={86400}
                  value={form.toleranceSeconds}
                  onChange={(e) => setForm((prev) => ({ ...prev, toleranceSeconds: e.target.value }))}
                />
                <div className="field-help">0 表示不校验时间窗（不建议生产使用）。</div>
              </div>
            </>
          ) : (
            <>
              <div className="form-field">
                <label>Token 请求头</label>
                <input
                  className="input"
                  value={form.tokenHeader}
                  onChange={(e) => setForm((prev) => ({ ...prev, tokenHeader: e.target.value }))}
                  placeholder="Authorization"
                />
              </div>
              <div className="form-field">
                <label>Token 前缀</label>
                <input
                  className="input"
                  value={form.tokenPrefix}
                  onChange={(e) => setForm((prev) => ({ ...prev, tokenPrefix: e.target.value }))}
                  placeholder="Bearer "
                />
                <div className="field-help">如 `Bearer `；留空表示请求头值就是密钥本身。</div>
              </div>
            </>
          )}
          <div className="form-field">
            <label>事件 ID 请求头</label>
            <input
              className="input"
              value={form.eventIdHeader}
              onChange={(e) => setForm((prev) => ({ ...prev, eventIdHeader: e.target.value }))}
              placeholder="X-Webhook-Event-Id"
            />
            <div className="field-help">请求体里取不到事件 ID 时从该请求头读取。</div>
          </div>
        </div>
      </details>
    </div>
  );

  return (
    <div className="card card-pad">
      <div className="toolbar">
        <div>
          <h2 style={{ margin: 0 }}>Webhook 接入</h2>
          <div className="text-sm" style={{ marginTop: 4 }}>
            外部系统通过 HTTP 回调把消息推给指定智能体；同一事件 ID 只执行一次。
          </div>
        </div>
        <span className="spacer" />
        <button className="btn btn-secondary btn-sm" disabled={loading} onClick={() => void load()}>
          刷新
        </button>
        {canManage && (
          <button className="btn btn-primary btn-sm" onClick={openCreate} disabled={publishedAgents.length === 0}>
            + 新建 Webhook
          </button>
        )}
      </div>

      {error && <div className="alert alert-error" style={{ marginTop: 12 }}>{error}</div>}
      {publishedAgents.length === 0 && !loading && (
        <div className="alert alert-info" style={{ marginTop: 12 }}>
          还没有已发布的智能体，请先在「智能体管理」中发布一个版本。
        </div>
      )}

      {loading ? (
        <Loading />
      ) : items.length === 0 ? (
        <div className="empty-state" style={{ marginTop: 16 }}>
          <div style={{ fontWeight: 600 }}>还没有配置 Webhook</div>
          <div className="text-sm" style={{ marginTop: 4 }}>
            新建后把入站地址与密钥配置到外部系统即可。
          </div>
        </div>
      ) : (
        <table className="table" style={{ marginTop: 16 }}>
          <thead>
            <tr>
              <th>名称</th>
              <th>目标智能体</th>
              <th>签名方式</th>
              <th>状态</th>
              <th>最近接收</th>
              <th>入站地址</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const endpoint = `${apiBaseUrl()}${item.inbound_path}`;
              return (
                <tr key={item.id}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{item.name}</div>
                    {item.description && <div className="text-sm">{item.description}</div>}
                  </td>
                  <td>{item.target_agent_name ?? item.target_agent_id}</td>
                  <td>{item.auth_mode}</td>
                  <td>
                    <StatusTag status={item.enabled ? "ENABLED" : "DISABLED"} />
                  </td>
                  <td className="text-sm">{item.last_received_at ? new Date(item.last_received_at).toLocaleString() : "—"}</td>
                  <td>
                    <div className="mono" style={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis" }} title={endpoint}>
                      {endpoint}
                    </div>
                    <button className="btn btn-ghost btn-sm" onClick={() => void copyText(endpoint, "入站地址")}>
                      复制
                    </button>
                  </td>
                  <td>
                    <div className="table-actions">
                      {canManage && (
                        <>
                          <button className="btn btn-secondary btn-sm" onClick={() => openEdit(item)}>
                            编辑
                          </button>
                          <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void toggleEnabled(item)}>
                            {item.enabled ? "停用" : "启用"}
                          </button>
                          <button className="btn btn-secondary btn-sm" onClick={() => setRotateTarget(item)}>
                            轮换密钥
                          </button>
                          <button className="btn btn-ghost btn-sm" onClick={() => setDeleteTarget(item)}>
                            删除
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <Modal
        open={createOpen}
        title="新建 Webhook"
        onClose={() => setCreateOpen(false)}
        width={680}
        footer={
          <div className="form-actions">
            <button className="btn btn-secondary" onClick={() => setCreateOpen(false)}>
              取消
            </button>
            <button className="btn btn-primary" disabled={busy || !form.name.trim() || !form.targetAgentId} onClick={() => void submitCreate()}>
              创建
            </button>
          </div>
        }
      >
        {formBody}
      </Modal>

      <Modal
        open={editTarget !== null}
        title={`编辑：${editTarget?.name ?? ""}`}
        onClose={() => setEditTarget(null)}
        width={680}
        footer={
          <div className="form-actions">
            <button className="btn btn-secondary" onClick={() => setEditTarget(null)}>
              取消
            </button>
            <button className="btn btn-primary" disabled={busy || !form.name.trim() || !form.targetAgentId} onClick={() => void submitEdit()}>
              保存
            </button>
          </div>
        }
      >
        {formBody}
      </Modal>

      <Modal
        open={reveal !== null}
        title="请立即保存密钥"
        onClose={() => setReveal(null)}
        width={640}
        footer={
          <button className="btn btn-primary" onClick={() => setReveal(null)}>
            我已保存
          </button>
        }
      >
        {reveal && (
          <div>
            <div className="alert alert-info">密钥只展示一次，关闭后无法再次查看；丢失请重新轮换。</div>
            <div className="form-field" style={{ marginTop: 12 }}>
              <label>Webhook：{reveal.name}</label>
              <div className="mono">{reveal.endpoint}</div>
              <button className="btn btn-secondary btn-sm" style={{ marginTop: 6 }} onClick={() => void copyText(reveal.endpoint, "入站地址")}>
                复制地址
              </button>
            </div>
            <div className="form-field">
              <label>签名密钥</label>
              <div className="mono" style={{ wordBreak: "break-all" }}>{reveal.secret}</div>
              <button className="btn btn-secondary btn-sm" style={{ marginTop: 6 }} onClick={() => void copyText(reveal.secret, "密钥")}>
                复制密钥
              </button>
            </div>
            <div className="field-help">
              {reveal.authMode === "TOKEN"
                ? `调用时携带请求头：${reveal.tokenHeader}: ${reveal.tokenPrefix}<密钥>`
                : `调用时携带：${reveal.timestampHeader}（秒级时间戳）与 ${reveal.signatureHeader}；签名值 = ${reveal.signaturePrefix}${reveal.signatureAlgorithm}-${reveal.signatureEncoding}(密钥, ${reveal.signatureTemplate})`}
            </div>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={rotateTarget !== null}
        title="轮换签名密钥"
        destructive
        busy={busy}
        confirmText="确认轮换"
        onCancel={() => setRotateTarget(null)}
        onConfirm={() => void rotateSecret()}
      >
        轮换后旧密钥立即失效，外部系统需要同步更新。确定继续？
      </ConfirmDialog>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="删除 Webhook"
        destructive
        busy={busy}
        confirmText="确认删除"
        requireText={deleteTarget?.name}
        requireLabel={deleteTarget ? `请输入「${deleteTarget.name}」以确认删除` : undefined}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => void removeWebhook()}
      >
        删除后该入站地址立即失效，已产生的会话与消息会保留。此操作不可恢复。
      </ConfirmDialog>
    </div>
  );
}
