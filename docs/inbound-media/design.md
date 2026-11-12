# 通用多媒体回调接入：设计与实施说明

> 状态：已实施  
> 范围：输入侧支持回调多媒体消息；输出仍只支持文本  
> 首个适配样例：wechat-bot（`docs/111.md`），但核心设计不绑定该渠道  
>
> **最终落地方式（2026-11-10 修订）**：不新增独立服务。多媒体能力并入主应用现有
> `POST /webhooks/{id}`，在 Webhook 配置里增加媒体字段映射；与模型网关 / skill 网关
> 一样运行在同一个 uvicorn 进程。回调地址仍然是项目已有的 Webhook 地址。
> 下文第 3 节“通用接入服务”为早期方案，已废弃，仅保留作设计记录。

## 1. 背景与目标

当前系统存在四个明确缺口：

1. 通用入站 Webhook 只解析一个文本字段，取不到就返回 422，没有媒体下载、落库、附件绑定能力。
   - `app/services/inbound_webhook.py` 的 `parse()` 只取 `message_path`。
   - `ParsedInbound` 只有 `event_id / text / conversation_key / reply_*`。
2. 企业微信入站虽然解析了 `media`，但没有进入模型上下文；纯媒体消息只会得到空文本上下文。
   - `app/services/wecom_events.py` 的 `_event_parts` 只传元数据。
   - `app/services/wecom_chat_bridge.py` 的 `record_inbound` 在 `not event.text` 时直接返回；
     `_process_with_agent` 不判断文本，仍会以空字符串调用 `handle_chat`（媒体未进上下文，并非真正丢弃）。
3. 文件上传只允许 PDF/TXT/MD/CSV/XLSX，图片/音频/视频会 415。
   - `app/services/file_parser.py` 的 `SUPPORTED` 白名单。
   - `app/api/routes/files.py` 的上传校验。
4. 模型网关只发送纯文本，即使配置了多模态模型，图片也不会传给模型。
   - `app/kernel/model_gateway/contracts.py` 的 `ModelMessage.content` 是 `str`。
   - `app/builtin_plugins/model_gateway.py` 的 `_message()` 只输出 `{role, content}`。
   - `app/services/context_builder.py` 只注入文档的 `extracted_text`。

目标：

- **不新增独立服务**：在现有通用入站 Webhook 上扩展媒体字段映射，渠道差异通过 Webhook 配置表达。
- 支持回调消息中的图片、文件、语音、视频进入会话；首期落地图片 + 可解析文档。
- 媒体消息进入正常任务链路，控制台能看到实时执行过程。
- 智能体使用多模态模型时，图片真正进入模型上下文；不支持视觉的模型必须明确降级，不能假装理解。
- 自动回写仍然只支持文本；媒体回复由智能体调用 `weixin.send_media` 完成，
  文件需先导出到 Bridge 共享卷（`POST /files/{id}/bridge-export`）。

## 2. 总体架构

```text
外部回调（wechat-bot / 其他渠道）
  -> 主应用 POST /webhooks/{id}（已有地址，同一 uvicorn 进程）
       HMAC/TOKEN 验签、字段映射、媒体获取与校验、幂等
  -> 媒体落库 File（object.store）
  -> handle_chat：建/复用会话、写消息、触发任务、SSE 推送
  -> 多模态上下文组装
       文本 + 当前消息图片（可选音频）
  -> 多模态模型执行任务
  -> 文本回复
       控制台展示 + 可选 reply_url 回写外部系统
```

核心边界：

- 不新增独立服务、不新增端口；多媒体是主应用的入站能力，与模型网关 / skill 网关同级。
- 回调地址就是控制台 Webhook 页生成的 `POST /webhooks/{id}`，外部系统无需改指向。
- 渠道差异落在 Webhook 配置的字段映射（媒体节点、类型、文件路径、MIME、文件名、占位文本）。

## 3. （已废弃）通用接入服务

> 最终实现未采用独立服务。本节仅保留早期方案记录，实际以第 4 节 Webhook 媒体映射为准。

### 3.1 目录结构

```text
services/inbound-gateway/
  pyproject.toml
  Dockerfile
  .env.example
  configs/channels/
    wechat-bot.yaml
  app/
    main.py
    config.py
    models.py
    channels/
      loader.py
      registry.py
      generic.py
      wechat_bot.py
    media/
      fetcher.py
      validator.py
    transport/
      auth.py
      idempotency.py
    sinks/
      ztorque.py
  tests/
    unit/
    integration/
```

### 3.2 统一消息模型

```python
class Attachment(BaseModel):
    type: Literal["image", "file", "audio", "video"]
    file_name: str
    mime_type: str
    data: bytes
    sha256: str | None = None

class CanonicalMessage(BaseModel):
    channel: str
    event_id: str
    conversation_key: str
    owner_username: str
    agent_id: str
    text: str = ""
    quote: str | None = None
    occurred_at: int | None = None
    attachments: list[Attachment] = []
    raw: dict | None = None
```

### 3.3 渠道配置

`configs/channels/wechat-bot.yaml`：

```yaml
channel: wechat-bot

auth:
  mode: bearer
  secret_env: WECHAT_BOT_WEBHOOK_SECRET

idempotency:
  key_path: message.message_id
  fallback_path: event_id

conversation:
  key_path: conversation_key
  owner_username: admin
  # 留空 = 使用租户内默认已发布 Agent；多媒体是平台通用能力，不在渠道里绑定具体智能体。
  # 如需按业务路由，由回调 payload.agent_id 显式指定。
  agent_id: ""

message:
  text_path: message.text
  quote_path: message.quote.text
  placeholder_texts:
    - "[图片]"
    - "[语音]"
    - "[视频]"
    - "[文件]"
  media:
    path: message.media
    type_path: type
    source_path: filePath
    mime_path: mimeType
    file_name_path: file_name

sink:
  type: ztorque
  base_url: http://127.0.0.1:8000
  endpoint: /api/z-torque/internal/inbound
  token_env: ZTORQUE_INBOUND_INTERNAL_TOKEN
```

映射规则：

- `message.media.type`：`image / voice / file / video`，适配器统一转为 `image / audio / file / video`。
- `message.media.filePath`：优先按本地路径读取；也支持 `http(s)://` 和 `base64:` 前缀。
- `message.media.mimeType`：wechat-bot 图片固定为 `image/*`，不能直接信任；必须结合扩展名和魔术字节判定。
- `message.text` 为 `[图片]`、`[语音]` 等占位文本时，不把占位符当真实内容；`text` 置空，保留附件。
- `message.quote.text` 作为引用文本附加到 `quote`，z-torque 侧拼入用户消息上下文。

### 3.4 入站接口

```http
POST /ingress/{channel}
Authorization: Bearer <secret>   # 或 HMAC，按渠道配置
Content-Type: application/json
```

返回：

- `202 {ok:true, accepted:true, event_id}`
- `200 {ok:true, duplicate:true}`
- `401` / `413` / `415` / `422`

### 3.5 媒体获取与校验

`media/fetcher.py`：

- `local_path`：只允许配置的根目录（如 `/var/lib/wechat-bot/media`），拒绝 `..`、绝对路径逃逸、符号链接逃逸。
- `http_url`：只允许 http/https；拒绝回环、内网、云元数据地址；超时和重定向次数限制。
- `base64`：限制解码后大小。

`media/validator.py`：

- 单文件上限、单请求总上限、附件数量上限。
- MIME + 扩展名 + 魔术字节三方校验；不一致拒绝。
- 默认白名单：
  - image: jpeg/png/gif/webp
  - audio: mpeg/wav/ogg/amr/mp4(audio)
  - video: mp4/quicktime/webm
  - document: pdf/txt/md/csv/xlsx

### 3.6 幂等

- 优先使用 `message.message_id`；没有时用 `event_id`。
- 通用服务本地做短 TTL 去重，最终以 z-torque 数据库唯一约束为准。
- 重复投递返回 `200 duplicate`，不重复建消息、不重复触发任务。

## 4. Webhook 媒体映射与处理（主应用内）

### 4.1 Webhook 配置字段

在 `zr_inbound_webhook` 上新增（控制台 Webhook 表单同步暴露）：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `media_path` | NULL | 媒体节点路径，如 `message.media`；NULL = 纯文本 |
| `media_type_path` | `type` | 媒体类型字段（image/voice/file/video） |
| `media_source_path` | `filePath` | 媒体来源（本地路径 / http(s) / `base64:`） |
| `media_mime_path` | `mimeType` | 声明 MIME，可为 `image/*` 这类不精确值 |
| `media_file_name_path` | `file_name` | 文件名；取不到时用来源路径 basename |
| `media_placeholder_texts` | 内置四项 | `[图片]` 等占位文本，有附件时不算真实输入 |

回调地址不变，仍是 `POST /webhooks/{id}`；外部系统无需新增服务或改地址。

### 4.2 媒体来源

`app/services/media_fetcher.py`：

- 本地路径：只能落在 `INBOUND_MEDIA_ROOTS` 白名单内，拒绝 `..`、绝对路径逃逸与符号链接逃逸；
- http(s)：拒绝回环、内网、链路本地与云元数据地址，禁止跟随重定向；
- `base64:`：解码后受大小上限约束。

### 4.3 处理步骤

1. 现有 Webhook 验签（HMAC / TOKEN）不变。
2. `parse()` 取文本与媒体节点；占位文本在有附件时置空；两者都为空才返回 422。
3. 现有 `zr_inbound_webhook_event` 幂等占位不变，重复投递返回 200 duplicate。
4. 创建/复用会话，seed 保持 `z-torque:inbound-webhook:conversation:{tenant}:{webhook_id}:{key}`。
5. 逐个媒体：获取字节 → `store_media()` 三方校验 → 落 `File` → 组装 `content_parts`：
   ```json
   [{"type": "image", "file_id": "...", "file_name": "photo.jpg", "mime_type": "image/jpeg"}]
   ```
6. 调用 `handle_chat(...)`，传入文本、`content_parts`、既有幂等键。
7. 提交事务并发布 `chat_event_bus`；用户消息带 `pending_task_id`，控制台订阅任务事件流。
8. 事件行更新为 `COMPLETED`。

因为 Webhook 采用“先 ACK 再异步处理”，媒体获取/校验失败发生在 202 之后：
事件行标记 `FAILED` 并记录原因，不重复建消息；如需同步 413/415，需另行改造 ACK 时机。

## 5. z-torque 媒体落库

### 5.1 新增服务

`app/services/media_ingest.py`：

```python
def store_media(
    db,
    store,
    *,
    tenant_id,
    owner_user_id,
    conversation_id,
    file_name,
    media_type,
    stream,
    max_bytes,
) -> File
```

职责：

- 流式写入 `object.store`，计算 size/sha256。
- 校验 MIME、扩展名、魔术字节。
- 文档类调用 `parse_file`，状态 `PARSED`；失败则 `PARSE_FAILED`。
- 媒体类跳过解析，状态 `UPLOADED`。
- 不修改 `File` 表结构，用 `media_type` 区分类型。
- **注意**：现有 `context_builder` 的附件查询只取 `status=PARSED`，图片/音频/视频会被过滤。
  多模态改造必须拆成「文档按 PARSED 注入文本 + 媒体按 UPLOADED 注入 content part」两条路径。

### 5.2 原始文件读取

现有 `GET /files/{id}/content` 返回抽取文本，不返回二进制。新增：

```http
GET /api/z-torque/files/{id}/raw
```

- 鉴权同现有文件接口；
- 从 `object.store` 流式返回；
- 设置正确的 `Content-Type` 和 `Content-Disposition`；
- 增加 `FILE_READ` 审计。

控制台图片/音频/视频预览统一通过带鉴权的 fetch 取 blob，再转 object URL，避免 `<img>` 无法携带 Authorization 的问题。

## 6. 多模态模型链路

### 6.1 契约调整

`app/kernel/model_gateway/contracts.py`：

```python
@dataclass(frozen=True)
class ModelContentPart:
    type: Literal["text", "image_url"]
    text: str | None = None
    image_url: str | None = None

@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str | None = None  # 现有契约已是 str | None；新增 content_parts 时保持兼容
    content_parts: list[ModelContentPart] | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_content: str | None = None
```

兼容策略：`content_parts` 为空时行为与现在完全一致。

### 6.2 上下文组装

`app/services/context_builder.py`：

1. 遍历历史消息时，识别 `Message.content_parts` 中的 `file_id`。
2. 文档类（PARSED）继续追加 `extracted_text`。
3. 图片类：
   - 只在当前用户消息上注入，历史图片默认不注入；
   - 从 `object.store` 读取字节，转 base64 data URI；
   - 受 `model_max_images_per_message` 和单图大小限制；
   - 若 `supports_vision=false`，替换为 `[已接收图片: file_name]` 文本。
4. 含媒体的上下文跳过 `memory.working` 缓存，避免缓存巨大 base64。
5. `attachment_ids` 参数保留，用于兼容当前请求的附件；与消息内 `file_id` 去重。
6. 现有代码在累加长度时直接 `len(item.content)`，引入 `content=None + content_parts` 后必须统一改为 `len(item.content or "")`，否则会 `TypeError`。
7. 现有 `content_parts` 循环只识别 `wecom_event` / `webhook_event`，其余类型直接 `continue`；新增 image/file part 时必须在同一循环内扩展，且不得破坏事件元数据注入顺序。

任务链路也必须生效：`app/kernel/task/runtime.py` 调用 `build_context` 时不再依赖单独的 `attachment_ids`，而是从触发消息的 `content_parts` 自动读取。

### 6.3 网关 payload

`app/builtin_plugins/model_gateway.py` 的 `_message()`：

- 无 `content_parts`：保持 `{role, content}`。
- 有 `content_parts`：
  ```json
  {
    "role": "user",
    "content": [
      {"type": "text", "text": "帮我看看这张图"},
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
    ]
  }
  ```
- 空文本时只输出图片 part。

### 6.4 模型能力

- 在 `ModelResource.config` 中增加 `supports_vision`、`supports_audio`，不新增数据库列。
- 模型页表单增加“支持视觉/支持音频”开关。
- `chat_application.py`、`task/runtime.py` 解析模型配置后传给 `build_context`。
- 运行时只有模型标识字符串（`selection.model` / `gateway.default_model`），没有 `ModelResource` 对象；
  按 `tenant_id + model_identifier + status=ENABLED` 查询（复用 `app/services/model_limits.py` 的查询模式），
  建议抽到同一处 helper，避免 chat / task 两条链路各写一份。
- 不支持视觉时，图片降级为文本提示；不支持音频时，音频降级为文本提示或跳过。

### 6.5 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `INBOUND_MEDIA_ROOTS` | 空 | 本地媒体根目录白名单 |
| `INBOUND_MEDIA_TIMEOUT_SECONDS` | 15 | HTTP 媒体下载超时 |
| `INBOUND_MAX_FILE_MB` | 20 | 单个附件上限 |
| `INBOUND_MAX_FILES` | 5 | 单条消息附件数 |
| `INBOUND_ALLOWED_MEDIA_TYPES` | 见白名单 | MIME 白名单 |
| `MODEL_MAX_IMAGES_PER_MESSAGE` | 4 | 单条消息最多图片数 |
| `MODEL_MAX_IMAGE_MB` | 5 | 单张图片上限 |
| `MODEL_IMAGE_HISTORY_LIMIT` | 0 | 历史消息图片注入条数，0=只当前消息 |

所有配置必须进入 `Settings` 并同步 `.env.example`，禁止硬编码。

## 7. 控制台

### 7.1 消息气泡

`console/src/pages/Chat.tsx`：

- `content_parts` 中 `type=image`：带鉴权 fetch `/files/{id}/raw`，生成 object URL，显示图片预览。
- `type=file`：文件卡片 + 下载。
- `type=audio`：`<audio controls>`。
- `type=video`：`<video controls>`。
- 文本占位 `[图片]` 只在没有附件时作为兜底。
- 媒体消息继续显示内联执行块，执行链路不单独改。
- **实时链路必须同步扩展**：现有 `ChatMessageEvent` 无 `content_parts`，`/chat/events` payload 无该字段，
  前端收到 SSE 后硬编码 `content_parts: null`。必须同时扩展事件总线、SSE payload 与前端消息类型，
  否则入站媒体消息要刷新页面才会出现图片。

### 7.2 模型配置

`console/src/pages/Models.tsx`：

- 模型档案增加“支持视觉”“支持音频”两个开关。
- 保存到 `ModelResource.config`。
- `console/src/types/index.ts` 同步扩展类型。

## 8. 代码改动清单

### z-torque

| 文件 | 改动 |
| --- | --- |
| `app/config.py` | 新增 `INBOUND_MEDIA_ROOTS`、媒体限制、模型图片限制与校验 |
| `.env.example` | 同步新增配置说明 |
| `app/models/webhook.py` | Webhook 新增媒体映射字段 |
| `alembic/versions/b4c5d6e7f8a9_inbound_webhook_media.py` | Webhook 媒体字段迁移 |
| `app/schemas/webhook.py` | 创建/更新请求增加媒体字段与路径校验 |
| `app/api/routes/webhooks.py` | 序列化/写入媒体字段；注入 object.store |
| `app/services/media_fetcher.py` | 本地/URL/base64 获取与 SSRF/路径逃逸防护 |
| `app/services/media_ingest.py` | 媒体落库服务（文档解析、媒体 UPLOADED） |
| `app/services/inbound_webhook.py` | 解析媒体、落库、组装 content_parts |
| `app/api/routes/files.py` | 新增 `GET /files/{id}/raw` |
| `app/kernel/model_gateway/contracts.py` | `ModelMessage.content_parts` + `ModelContentPart` |
| `app/services/context_builder.py` | 媒体附件加载、图片 data URI、能力降级、缓存策略 |
| `app/services/model_limits.py` | 新增按模型标识解析视觉/音频能力 |
| `app/builtin_plugins/model_gateway.py` | `_message()` 输出 OpenAI content 数组 |
| `app/services/chat_event_bus.py` | 事件增加 `content_parts`，供 SSE 实时渲染媒体 |
| `app/api/routes/chat_events.py` | SSE payload 输出 `content_parts` |
| `app/services/chat_application.py` | 传递模型视觉能力 |
| `app/kernel/task/runtime.py` | 传递模型视觉能力；任务上下文自动读取消息附件 |
| `tests/unit/test_media_ingest.py` | 新增 |
| `tests/unit/test_model_multimodal_payload.py` | 新增 |
| `tests/integration/test_inbound_webhook_media.py` | 新增 |
| `tests/integration/test_inbound_media_context.py` | 新增 |

> 早期 `services/inbound-gateway` 独立服务未采用，已删除；其媒体获取/校验逻辑已合并到
> `app/services/media_fetcher.py` 与 `app/services/media_ingest.py`。

### 控制台

| 文件 | 改动 |
| --- | --- |
| `console/src/types/index.ts` | `content_parts` 类型化附件；Webhook 媒体字段 |
| `console/src/pages/Webhooks.tsx` | 媒体节点/类型/来源/MIME/文件名/占位文本配置 |
| `console/src/pages/Chat.tsx` | 图片/文件/音频/视频气泡；SSE 消息携带 `content_parts` |
| `console/src/pages/Models.tsx` | 视觉/音频能力开关（保存时合并现有 `config`） |
| `console/src/services/api.ts` | 新增带鉴权取 blob / object URL 的预览 helper |
| `console/src/index.css` | 媒体气泡样式，全部使用 CSS 变量 |

## 9. 实施顺序

### P0-1 媒体落库

1. `app/services/media_ingest.py`
2. `GET /files/{id}/raw`
3. 单元测试：类型校验、大小限制、文档解析、媒体跳过解析。

### P0-2 Webhook 媒体映射

1. `zr_inbound_webhook` 媒体字段模型、schema 与迁移。
2. `app/services/media_fetcher.py`（本地/URL/base64 + 安全校验）。
3. `inbound_webhook.parse()` 解析媒体、`_ingest_media()` 落库并组装 content_parts。
4. `webhooks.py` 序列化/写入媒体字段，注入 object.store。
5. 集成测试：图片落库、content_parts、占位文本、纯文本回归。

### P0-3 多模态链路

1. `ModelContentPart` / `ModelMessage.content_parts`。
2. `context_builder` 图片注入与能力降级。
3. `model_gateway._message()` content 数组。
4. `chat_application` / `task/runtime` 传递能力。
5. 单测：payload 形态、图片 data URI、能力关闭降级、任务链路。

### P0-4 Webhook 媒体配置（控制台）

1. Webhook 表单增加媒体节点/类型/来源/MIME/文件名/占位文本。
2. 类型定义与 payload 同步。
3. `npm run build`。

### P0-5 控制台媒体展示

1. 媒体气泡与原始文件读取。
2. 模型能力开关。
3. `npm run build`。

### P0-6 端到端

1. 只启动 z-torque 主应用（无需第二个服务）。
2. 在控制台 Webhook 页把媒体路径设为 `message.media`，配置共享媒体目录 `INBOUND_MEDIA_ROOTS`。
3. 发送文本、图片、引用回调到既有 `/webhooks/{id}`。
4. 验证会话消息、执行链路、图片预览、模型请求含图片、文本回复。
5. 回归：后端全量 `pytest`，前端构建。

### P1/P2

- P1：文档文件、语音 ASR、更多渠道适配器。
- P2：视频音轨/关键帧、媒体回复。

## 10. 风险与约束

1. wechat-bot 的 `media.filePath` 是 Bot 容器内路径；必须让它落在 `INBOUND_MEDIA_ROOTS` 内（共享卷），或 Bot 提供 URL/base64，否则主应用读不到媒体。
2. `mimeType` 可能是 `image/*` 这种不精确值，必须用扩展名 + 魔术字节校正。
3. 图片 base64 会显著增加 token 和请求体积；默认只注入当前消息，历史图片关闭。
4. 多模态能力必须按模型配置显式判断；不能假设所有模型都支持图片。
5. 现有 `memory.working` 会缓存上下文；含媒体的上下文必须跳过缓存，避免缓存巨大 data URI。
6. Webhook 已有 HMAC/TOKEN 验签；媒体入口不额外增加未鉴权地址。
7. 媒体文件读取必须复用 `object.store`，禁止直接拼接本地路径。
8. 本期不做媒体输出；`reply_url` 仍然只回写文本。

## 11. wechat-bot 适配参考

`docs/111.md` 已给出三类真实 payload：

- 文本：`message.text = "在吗"`，`message.media = null`
- 图片：`message.text = "[图片]"`，`message.media = {type:"image", filePath:"/tmp/...jpg", mimeType:"image/*"}`
- 引用：`message.text = "[引用: ...]\\n..."`，`message.quote.text = "订单 A123 还没到"`

关键行为：

- `event_id` 每次投递都会变，幂等优先 `message.message_id`。
- `conversation_id == peer_id`，会话键可直接用 `conversation_key`。
- 图片真实内容在 `filePath` 指向的本地文件；`data.media_url` 为 null。
- 图片占位文本 `[图片]` 不能当作真实用户输入。

## 12. 代码核对修订（实施前定稿）

以下为实现前对现有代码核对后补充的约束，属于设计的一部分：

1. **SSE 实时链路**：`ChatMessageEvent` / `/chat/events` payload / 前端 SSE 分支都必须携带 `content_parts`。
2. **会话 seed**：沿用现有 Webhook seed `z-torque:inbound-webhook:conversation:{tenant}:{webhook_id}:{key}`，不新增派生规则。
3. **媒体状态**：文档 `PARSED`、媒体 `UPLOADED`；`context_builder` 必须按类型分别查询，不能沿用单一 `PARSED` 条件。
4. **上传范围**：本期控制台手动上传仍只支持原五类文档；媒体入口是 Webhook 媒体映射。若后续需要手动上传图片，再单独放宽 `SUPPORTED` 与魔术字节校验。
5. **模型能力查询**：按 `tenant_id + model_identifier + ENABLED` 读取 `ModelResource.config`（建议 helper 放 `model_limits.py`）。
6. **`ObjectStore`**：Port 只有 `put/local_path/delete`；`/files/{id}/raw` 使用 `local_path` 读取，不新增 Port 方法。
7. **Alembic**：Webhook 媒体字段迁移的 `down_revision` 指向 `f3a4b5c6d7e8`。
8. **测试基座**：`tests/conftest.py` 用 `Base.metadata.create_all`，新列在测试中自动生效；真实库仍需迁移。
9. **幂等**：复用 `zr_inbound_webhook_event`，不新增幂等表。
10. **ACK 时机**：Webhook 先返回 202 再异步处理；媒体获取/校验失败在事件行标记 `FAILED`，不改变既有 ACK 语义。
11. **模型页保存**：`config` 为整体替换语义，保存视觉/音频开关时必须合并现有 `config`，避免丢失 `interface_type`。
12. **`build_context` 长度计算**：统一使用 `content or ""`，兼容 `content=None + content_parts`。
13. **Agent 路由**：复用 Webhook 既有 `target_agent_id`，在控制台为 Webhook 选择已发布 Agent；
    媒体能力本身不改变 Agent 路由规则。需要按业务分流时，创建多个 Webhook 指向不同 Agent。
