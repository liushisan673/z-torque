# 通用多媒体回调接入：实施提示词

> 用途：把下列提示词逐段粘贴到新的编码会话中实施。  
> 先读 `docs/inbound-media/design.md`，再读 `docs/111.md`（wechat-bot 真实回调样例）。
>
> **重要修订（2026-11-10）**：最终实现复用现有 `POST /webhooks/{id}`，多媒体能力并入主应用，
> 与模型网关 / skill 网关运行在同一进程；不再部署 `services/inbound-gateway`。
> 下文的“P0-2 内部接入接口”和“P0-4 通用接入服务”已废弃，实际以 `design.md` 第 4 节为准。

## 0. 全局约束（每个阶段都先粘贴这一段）

```text
你是 z-torque 项目的高级工程师。当前任务：按 docs/inbound-media/design.md 实现“通用多媒体回调接入”，范围仅输入侧多媒体，输出仍然只有文本。

必须遵守：
1. 先完整阅读 docs/inbound-media/design.md 和 docs/111.md，再读相关源码，不要凭猜测改代码。
2. 技术栈：后端 Python 3.12 + FastAPI + SQLAlchemy + Alembic；前端 React + TypeScript + Vite。
3. 禁止硬编码配置：所有可调参数进入 app/config.py 的 Settings 并同步 .env.example；通用网关的渠道差异进入 YAML 配置。
4. 不得破坏现有文本 Webhook、企微文本链路、普通 Chat 任务链路；改动后必须跑回归。
5. 不实现媒体输出，不实现媒体回复；reply_url 仍然只回写文本。
6. 前端样式必须使用 CSS 变量，禁止硬编码颜色/尺寸；所有功能接真实后端接口，禁止 mock 数据。
7. 遵循现有分层与目录约定；不要新增与任务无关的抽象。
8. 每完成一个阶段：运行 ruff、相关 pytest、前端 npm run build（涉及前端时），并汇报结果。
9. 不要提交 git commit，不要修改用户未要求的内容。
10. 如果发现设计与现有代码冲突，先停下说明冲突点和建议，不要静默改设计。

项目根目录：d:\project\z-torque-community-m1
虚拟环境：.\.venv\Scripts\python.exe
```

## 1. 总控提示词（建议第一个会话使用）

```text
请阅读 docs/inbound-media/design.md 和 docs/111.md，然后按设计文档中的 P0-1 到 P0-6 顺序实施。

要求：
- 每完成一个 P0 阶段就停下来汇报：改了哪些文件、关键实现、测试结果、下一步。
- 不要一次性把所有阶段混在一起改；每个阶段可独立验证。
- 每个阶段开始前先用 Grep/Read 确认相关文件当前状态，避免基于过期假设修改。
- 如果某阶段需要用户决策或环境依赖（例如 wechat-bot 媒体目录共享卷），先说明再继续。
- 最终目标：wechat-bot 的文本、图片、引用三类回调都能进入 z-torque 会话，图片进入多模态模型，控制台实时展示执行链路，最终返回文本。
```

## 2. P0-1：媒体落库与原始文件读取

```text
阶段目标：让 z-torque 能安全地接收图片/音频/视频/文档附件并落库，同时提供原始字节读取接口。此阶段不改模型链路，不改回调入口。

先阅读：
- app/api/routes/files.py
- app/services/file_parser.py
- app/models/file.py
- app/models/message.py
- app/kernel/ports/object_store.py
- app/builtin_plugins/object_store.py
- app/config.py（max_upload_size_mb 等）

实现：
1. 新增 app/services/media_ingest.py：
   - 函数 store_media(db, store, *, tenant_id, owner_user_id, conversation_id, file_name, media_type, stream, max_bytes) -> File
   - 流式写入 object.store，计算 size 和 sha256。
   - 校验扩展名、MIME、魔术字节；图片支持 jpeg/png/gif/webp，音频支持 mpeg/wav/ogg/amr/mp4，视频支持 mp4/quicktime/webm，文档保持 pdf/txt/md/csv/xlsx。
   - 文档类调用 parse_file，成功 status=PARSED，失败 PARSE_FAILED；媒体类跳过解析，status=UPLOADED。
   - 不修改 File 表结构。
2. 在 app/api/routes/files.py 新增 GET /files/{id}/raw：
   - 鉴权与 /files/{id} 一致（get_current_user + owned_file）。
   - 从 object.store 读取原始字节，StreamingResponse 返回。
   - 设置 Content-Type 为 File.media_type，Content-Disposition 使用原文件名。
   - 记录 FILE_READ 审计。
3. 抽取可复用的 MIME/魔术字节校验函数，避免和上传路由重复。
4. 新增 tests/unit/test_media_ingest.py：
   - 文档解析成功/失败；
   - 图片跳过解析且 status=UPLOADED；
   - 超限、类型不支持、伪装扩展名被拒绝。

验收：
- 现有文件上传测试全部通过。
- 新增测试通过。
- /files/{id}/raw 返回正确字节和 Content-Type。

验证命令：
.\.venv\Scripts\python.exe -m ruff check app/services/media_ingest.py app/api/routes/files.py tests/unit/test_media_ingest.py
.\.venv\Scripts\python.exe -m pytest tests/unit/test_media_ingest.py tests/integration -q

禁止：
- 不要改 File 表结构。
- 不要改现有 /files/{id}/content 的“返回抽取文本”语义。
- 不要让媒体文件走 parse_file。
```

## 3. P0-2：内部接入接口与会话分发

```text
阶段目标：新增一个供通用接入服务调用的内部接口，支持“文本 + 媒体附件”创建会话/消息/任务，并保持幂等和实时推送。

先阅读：
- app/services/inbound_webhook.py（现有文本 Webhook 的 ensure conversation / handle_chat / publish 逻辑）
- app/services/chat_application.py（handle_chat、content_parts、_attach_files）
- app/services/chat_event_bus.py
- app/api/routes/chat_events.py
- app/api/routes/webhooks.py
- app/models/webhook.py
- app/models/message.py
- app/kernel/task/states.py

实现：
1. 新增模型 app/models/inbound_channel_event.py：
   - 表 zr_inbound_channel_event
   - 字段：tenant_id, source, event_id, conversation_id, message_id, task_id, status, error, created_at/updated_at
   - 唯一约束 (tenant_id, source, event_id)
   - 在 app/models/__init__.py 注册
   - 新增 Alembic 迁移
2. 新增 app/services/inbound_message_service.py：
   - 从 InboundWebhookService 抽出公共逻辑：解析 owner/agent、ensure conversation、附件落库、调用 handle_chat、发布 chat_event_bus 事件。
   - 支持 attachments 输入：[(field, file_name, media_type, stream)]。
   - 附件通过 P0-1 的 media_ingest 落库，组装 content_parts。
   - 用户消息事件必须带 pending_task_id，控制台才能订阅任务执行链路。
   - 现有 app/services/inbound_webhook.py 改为复用该服务，保持文本行为不变。
3. 新增 app/api/routes/inbound_internal.py：
   - POST /api/z-torque/internal/inbound
   - multipart：payload JSON + attachment_0..N
   - Authorization: Bearer <INBOUND_INTERNAL_TOKEN> 常量时间比较
   - 错误码：401/404/413/415/422/409；重复返回 200 duplicate。
   - 在 app/main.py 或 app/api/router.py 注册；不挂用户鉴权。
4. app/config.py 新增：
   - inbound_internal_enabled: bool = False
   - inbound_internal_token: SecretStr | None = None
   - inbound_max_file_mb / inbound_max_files / inbound_allowed_media_types
   - validate_runtime 校验开关与 Token 的关系。
   - 同步 .env.example。
5. 测试：
   - tests/integration/test_inbound_internal.py：401、404（未启用）、413、415、422、202、200 duplicate。
   - 附件落库、content_parts、message_file 关联、任务创建、chat_event_bus 事件。
   - 现有 test_inbound_webhook_events.py / test_wecom_events.py 回归通过。

验收：
- 内部接口可用 multipart 接收 payload + 图片，创建会话和任务。
- 幂等表生效，重复投递不重复建消息。
- 控制台 SSE 能收到用户消息事件和 pending_task_id。

验证命令：
.\.venv\Scripts\python.exe -m ruff check app/services/inbound_message_service.py app/services/inbound_webhook.py app/api/routes/inbound_internal.py app/models/inbound_channel_event.py tests/integration/test_inbound_internal.py
.\.venv\Scripts\python.exe -m pytest tests/integration/test_inbound_internal.py tests/integration/test_inbound_webhook_events.py tests/integration/test_wecom_events.py -q
.\.venv\Scripts\python.exe -m alembic upgrade head

禁止：
- 不要直接写数据库绕过 handle_chat。
- 不要让内部接口复用用户登录态。
- 不要改变现有 Webhook 的 202/200/422 语义。
```

## 4. P0-3：多模态上下文与模型网关

```text
阶段目标：让图片真正进入多模态模型请求；模型不支持视觉时明确降级；任务链路和普通 Chat 链路都生效。

先阅读：
- app/kernel/model_gateway/contracts.py
- app/builtin_plugins/model_gateway.py（_message、_payload、generate、stream）
- app/services/context_builder.py
- app/services/chat_application.py
- app/kernel/task/runtime.py（build_context 调用点）
- app/models/model_resource.py
- app/services/model_limits.py
- tests/unit 下模型网关相关测试

实现：
1. app/kernel/model_gateway/contracts.py：
   - 新增 ModelContentPart（type: text|image_url，text/image_url 字段）。
   - ModelMessage 新增 content_parts: list[ModelContentPart] | None。
   - 保持 content 字段兼容。
2. app/services/context_builder.py：
   - 遍历消息 content_parts，收集 file_id。
   - 文档类继续注入 extracted_text。
   - 图片类：读取 object.store 字节，转 data URI，放入当前用户消息的 content_parts。
   - 默认只注入当前用户消息；MODEL_IMAGE_HISTORY_LIMIT=0。
   - 受 MODEL_MAX_IMAGES_PER_MESSAGE、MODEL_MAX_IMAGE_MB 限制。
   - supports_vision=false 时替换为文本提示 [已接收图片: file_name]。
   - 含媒体的上下文跳过 memory.working 缓存。
   - 任务链路：runtime 调用 build_context 时自动从触发消息 content_parts 读取，不依赖单独 attachment_ids。
3. app/builtin_plugins/model_gateway.py：
   - _message() 有 content_parts 时输出 OpenAI 兼容 content 数组。
   - 文本 part + image_url part；空文本只输出图片。
   - 无 content_parts 保持原样。
4. 模型能力：
   - 从 ModelResource.config 读取 supports_vision / supports_audio，默认 false。
   - chat_application.py、task/runtime.py 解析模型配置并传给 build_context。
   - 不新增数据库列（用 config JSON）。
5. 测试：
   - tests/unit/test_model_multimodal_payload.py：有/无 content_parts 的 payload 形态、data URI、空文本。
   - tests/integration/test_inbound_media_context.py：图片消息落库后 build_context 包含 image part；supports_vision=false 时降级。
   - 回归现有模型网关/上下文/task 测试。

验收：
- 图片消息触发任务时，模型请求包含 image_url content part。
- 不支持视觉的模型不会收到 image part，而是收到文本提示。
- 纯文本链路 payload 与改动前一致。

验证命令：
.\.venv\Scripts\python.exe -m ruff check app/kernel/model_gateway/contracts.py app/services/context_builder.py app/builtin_plugins/model_gateway.py app/services/chat_application.py app/kernel/task/runtime.py tests/unit/test_model_multimodal_payload.py tests/integration/test_inbound_media_context.py
.\.venv\Scripts\python.exe -m pytest tests/unit/test_model_multimodal_payload.py tests/integration/test_inbound_media_context.py -q
.\.venv\Scripts\python.exe -m pytest tests/unit tests/integration -q

禁止：
- 不要把所有历史图片都塞进上下文。
- 不要在 memory.working 缓存里保存 base64。
- 不要假设模型一定支持视觉。
```

## 5. P0-4：通用接入服务

```text
阶段目标：新建 services/inbound-gateway 独立服务，接收 wechat-bot 回调，完成字段映射、媒体获取、幂等和转发。

先阅读：
- docs/inbound-media/design.md 第 3 节
- docs/111.md
- app/api/routes/inbound_internal.py（P0-2 产物）
- pyproject.toml、.env.example、Dockerfile、docker-compose.yml（了解项目约定）

实现：
1. 新建 services/inbound-gateway：
   - pyproject.toml（FastAPI + uvicorn + httpx + pydantic + pyyaml + pytest）
   - app/main.py：GET /health、POST /ingress/{channel}
   - app/config.py：Settings（channels_dir、ztorque_base_url、token、媒体限制）
   - app/models.py：CanonicalMessage / Attachment
2. 渠道配置：
   - configs/channels/wechat-bot.yaml，按设计文档示例。
   - app/channels/loader.py 加载 YAML；registry.py 注册适配器。
   - app/channels/wechat_bot.py 处理 message.text / message.media / quote / 占位文本。
   - app/channels/generic.py 提供 JSONPath 通用适配器。
3. 媒体：
   - app/media/fetcher.py：local_path / http_url / base64。
   - app/media/validator.py：大小、MIME、扩展名、魔术字节、路径逃逸、SSRF。
   - wechat-bot 的 filePath 只允许配置的媒体根目录。
4. 鉴权与幂等：
   - app/transport/auth.py：Bearer（wechat-bot 当前实际方式），预留 HMAC。
   - app/transport/idempotency.py：message.message_id 优先，event_id 兜底。
5. 下游：
   - app/sinks/ztorque.py：构造 multipart，payload JSON + attachment_N，POST 到 /api/z-torque/internal/inbound；带 Bearer。
   - 202/200 duplicate 正常处理；4xx 记录日志并返回上游。
6. 测试：
   - 文本、图片、引用三类 payload 映射。
   - 占位文本 [图片] 不进入 text，附件正确。
   - 本地路径逃逸、URL SSRF、超限、类型不支持。
   - 幂等重复投递。
   - httpx MockTransport 模拟 z-torque 内部接口，断言 multipart 内容。

验收：
- 启动服务后 POST /ingress/wechat-bot 返回 202。
- 图片回调能把本地文件读成附件并转发。
- 重复 message_id 不重复转发。
- 服务代码不 import app.*。

验证命令：
cd services/inbound-gateway
python -m ruff check app tests
python -m pytest -q

禁止：
- 不要在网关里硬编码 wechat-bot 分支到核心流程。
- 不要允许任意本地路径或任意 URL。
- 不要直接连 z-torque 数据库。
```

## 6. P0-5：控制台媒体展示

```text
阶段目标：Chat 页能正常展示图片、文件、音频、视频消息；模型页能配置视觉/音频能力。

先阅读：
- console/src/types/index.ts
- console/src/pages/Chat.tsx（消息渲染、附件 chip、执行块）
- console/src/pages/Models.tsx
- console/src/services/api.ts（fetchWithAuth）
- console/src/index.css（CSS 变量）

实现：
1. types/index.ts：
   - 扩展 Message.content_parts 类型：type: image|file|audio|video，file_id。
2. Chat.tsx：
   - 图片：带鉴权 fetch /api/z-torque/files/{id}/raw，生成 object URL，显示缩略图/预览。
   - 文件：卡片 + 下载按钮。
   - 音频：<audio controls>。
   - 视频：<video controls>。
   - [图片] 等占位文本只在没有附件时兜底显示。
   - 媒体消息继续展示内联执行块，不改任务链路。
   - 组件卸载时 revokeObjectURL，避免内存泄漏。
3. Models.tsx：
   - 模型档案增加“支持视觉”“支持音频”开关。
   - 保存到 config.supports_vision / config.supports_audio。
   - 列表/详情展示能力标签。
4. index.css：
   - 新增媒体气泡样式，全部使用现有 CSS 变量。
   - 统一字体，不新增字体族。
5. 验证：
   - 后端构造一条带图片 file_id 的消息，前端能显示预览。
   - 无附件时占位文本仍正常显示。
   - npm run build 通过。

验证命令：
cd console
npm run build

禁止：
- 不要用 mock 图片或假数据。
- 不要在 <img src> 直接放需要 Authorization 的接口。
- 不要硬编码颜色/尺寸。
```

## 7. P0-6：端到端验证与回归

```text
阶段目标：完成 wechat-bot 文本/图片/引用三类回调的端到端验证，并跑全量回归。

准备：
1. 启动 z-torque：
   .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
2. 启动通用网关：
   cd services/inbound-gateway
   .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload
3. 配置环境变量：
   - ZTORQUE_INBOUND_INTERNAL_TOKEN
   - INBOUND_INTERNAL_ENABLED=true
   - WECHAT_BOT_WEBHOOK_SECRET
   - 渠道 YAML 中 owner_username / agent_id
4. 让 wechat-bot 的媒体目录与通用网关共享卷；或在渠道配置中改用 HTTP URL/base64。

端到端用例：
1. 文本回调：message.text=在吗，media=null。
   - 期望：z-torque Chat 页出现用户消息，智能体执行/回复，最终文本正常。
2. 图片回调：message.text=[图片]，media.filePath 指向真实 jpg。
   - 期望：用户消息显示图片预览；执行链路出现；多模态模型请求包含 image_url；最终文本回复。
3. 引用回调：message.text 含 [引用: ...]，message.quote.text 有值。
   - 期望：引用文本进入模型上下文。
4. 重复投递同一 message.message_id。
   - 期望：不重复建消息、不重复执行任务。
5. 非法类型/超大文件/路径逃逸。
   - 期望：415/413/400，不进入 z-torque。

回归命令：
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest -q
cd console && npm run build

汇报：
- 逐项列出通过/失败。
- 失败项给出日志、复现步骤和建议。
- 不要用“应该可以”代替实际验证。
```

## 8. 常用调试提示词

```text
图片没有进入模型时，按顺序检查：
1. 内部接入接口是否收到 attachment 并创建了 File（status=UPLOADED，conversation_id 正确）。
2. Message.content_parts 是否包含 {"type":"image","file_id":"..."}。
3. zr_message_file 是否有对应关联行。
4. build_context 是否命中当前用户消息、supports_vision 是否为 true。
5. ModelRequest.messages 中是否出现 image_url content part。
6. 模型网关最终 HTTP payload 是否为 OpenAI content 数组。
7. 模型本身是否真的支持视觉；不支持时应降级而不是报错。
```

```text
图片显示不出来时，按顺序检查：
1. /files/{id}/raw 是否 200，Content-Type 是否 image/jpeg 等。
2. 前端是否用 fetchWithAuth 取 blob，而不是直接 <img src>。
3. object URL 是否在组件卸载时 revoke。
4. File.status 是否被误设为 PARSED/DELETED。
5. content_parts 的 type/file_id 是否完整。
```

```text
wechat-bot 媒体读不到时，按顺序检查：
1. 网关与 Bot 是否挂载了同一个媒体目录。
2. 渠道配置的媒体根目录是否包含 /tmp/weixin-agent/media。
3. filePath 是否为绝对路径、是否符号链接逃逸。
4. 容器内文件是否存在、可读。
5. 如果无法共享卷，改为让 Bot 提供 HTTP URL 或直接上传字节。
```
