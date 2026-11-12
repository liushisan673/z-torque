"""通用入站 Webhook：外部系统回调消息 → 指定智能体对话。

与企微订阅不同，这里是租户自配置的通用接入点：
- 每条 webhook 指向一个已发布智能体；
- 外部系统按配置的签名方式调用 `POST /webhooks/{id}`；
- 入站事件落 `zr_inbound_webhook_event` 做幂等，重复投递不会重复执行。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, TenantModel, UUIDModel


class InboundWebhook(TenantModel):
    __tablename__ = "zr_inbound_webhook"

    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("zr_user.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 签名密钥密文（SecretStore/Fernet）；明文只在创建/轮换时返回一次
    secret_ciphertext: Mapped[str] = mapped_column(String(1024), nullable=False)
    # 鉴权方式：HMAC（签名）或 TOKEN（请求头直接带密钥）
    auth_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="HMAC")
    # HMAC 请求头名，兼容外部系统自定义命名
    signature_header: Mapped[str] = mapped_column(String(64), nullable=False, default="X-Webhook-Signature")
    timestamp_header: Mapped[str] = mapped_column(String(64), nullable=False, default="X-Webhook-Timestamp")
    event_id_header: Mapped[str] = mapped_column(String(64), nullable=False, default="X-Webhook-Event-Id")
    # TOKEN 模式的请求头名与前缀（如 Authorization / Bearer ）
    token_header: Mapped[str] = mapped_column(String(64), nullable=False, default="Authorization")
    token_prefix: Mapped[str] = mapped_column(String(32), nullable=False, default="Bearer ")
    # 签名算法与编码：SHA256/SHA1 + HEX/BASE64
    signature_algorithm: Mapped[str] = mapped_column(String(8), nullable=False, default="SHA256")
    signature_encoding: Mapped[str] = mapped_column(String(8), nullable=False, default="HEX")
    # 签名值前缀（如 sha256= / v1= / 空）与签名内容模板（必须包含 {body}）
    signature_prefix: Mapped[str] = mapped_column(String(32), nullable=False, default="sha256=")
    signature_template: Mapped[str] = mapped_column(String(128), nullable=False, default="{timestamp}.{body}")
    # 时间戳容忍窗口（秒）；0 表示不校验
    timestamp_tolerance_seconds: Mapped[int] = mapped_column(nullable=False, default=300)
    target_agent_id: Mapped[UUID] = mapped_column(ForeignKey("zr_agent.id", ondelete="RESTRICT"), nullable=False, index=True)
    # 从 JSON 请求体取值的点路径（支持 a.b / a[0].b）
    message_path: Mapped[str] = mapped_column(String(128), nullable=False, default="text")
    conversation_key_path: Mapped[str] = mapped_column(String(128), nullable=False, default="conversation_key")
    event_id_path: Mapped[str] = mapped_column(String(128), nullable=False, default="event_id")
    # 媒体映射：media_path 为空表示该 Webhook 不解析媒体（保持纯文本行为）
    media_path: Mapped[str | None] = mapped_column(String(128), nullable=True)
    media_type_path: Mapped[str] = mapped_column(String(64), nullable=False, default="type")
    media_source_path: Mapped[str] = mapped_column(String(64), nullable=False, default="filePath")
    media_mime_path: Mapped[str] = mapped_column(String(64), nullable=False, default="mimeType")
    media_file_name_path: Mapped[str] = mapped_column(String(64), nullable=False, default="file_name")
    # 占位文本（如 [图片]）不作为真实用户输入；为空时使用内置默认列表
    media_placeholder_texts: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # 可选回写：任务终态后把最终答复 POST 回外部系统（如微信 Bridge 的 /v1/messages/send）
    reply_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 回写请求的 Bearer 令牌密文（SecretStore/Fernet），为空表示不鉴权
    reply_token_ciphertext: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 从入站 payload 取回写目标账号 / 用户的点路径（默认对齐 wechat-Bot 契约）
    reply_account_path: Mapped[str] = mapped_column(String(128), nullable=False, default="account_id")
    reply_peer_path: Mapped[str] = mapped_column(String(128), nullable=False, default="peer_id")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InboundWebhookEvent(UUIDModel):
    __tablename__ = "zr_inbound_webhook_event"
    __table_args__ = (UniqueConstraint("webhook_id", "event_id", name="uq_zr_inbound_webhook_event"),)

    tenant_id: Mapped[UUID] = mapped_column(GUID(), nullable=False, index=True)
    webhook_id: Mapped[UUID] = mapped_column(ForeignKey("zr_inbound_webhook.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True)
    # ACCEPTED / PROCESSING / COMPLETED / FAILED
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACCEPTED")
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
