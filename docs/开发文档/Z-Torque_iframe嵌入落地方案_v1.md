# Z-Torque 社区版 iframe 嵌入落地方案

> **文档性质**：产品与前端落地设计
> **关联**：形态确认书 D4 / D9（自带控制台为主、嵌入宿主为辅；API 优先、前端可替换）
> **前置**：轻量控制台已落地（`console/` 前端 + 后端业务路由）；已有稳定 API 文档
> **版本**：v1.1 ｜ 2026-10-19
> **适用范围**：社区版；企业版可在此基础上扩展白标与深度定制

---

## 变更记录（v1.0 → v1.1）

| 变更 | 说明 |
|------|------|
| **身份模型定稿** | 采纳「平台登录 + 登录态复用」模式：用户先登录 Z-Torque 平台，嵌入窗口直接复用登录态；不引入访客身份，不新增嵌入 Token 签发接口 |
| **认证方式重排** | Cookie 直通为主路径（同站部署）；postMessage 注入 Token 为跨站兜底；v1 的「URL 短期 Token」不再采用（存在浏览器历史 / Referrer / 网关日志泄露面，且与本身份模型无必要耦合） |
| **点击劫持防护提级** | CSP `frame-ancestors` 从「可选」改为「默认收紧：未配置白名单即拒绝被嵌」 |
| **新增频控与配额** | 防止登录态被用于无节制消耗模型额度 |
| **后端新增项清零** | v1 §6.2 的「嵌入 Token 签发 / 权限收敛」取消，仅保留配置项与安全加固，无新增业务接口 |
| **事实修正** | 前端目录按 `console/src` 实际结构对齐；「六页导航」修正为七页；示例路径 `doce/` 修正为 `docs/`；补充同站 / 跨站 Cookie 的浏览器硬约束 |

---

## 1. 目标与原则

### 1.1 目标

在不破坏现有轻量控制台的前提下，提供一套**可嵌入宿主系统的对话窗口**，使第三方系统可以通过 iframe 快速接入 Z-Torque 的对话与任务执行能力。用户先在 Z-Torque 平台登录一次，之后在宿主系统内即可直接通过嵌入对话框交互，无需跳转页面。

### 1.2 设计原则

| 原则 | 说明 |
|------|------|
| **使用与配置分离** | 嵌入窗口只负责「对话与执行」；Agent / Skill / Model / Connector 等配置仍在完整控制台完成 |
| **登录态复用** | 嵌入窗口不引入第二套身份体系，完全复用平台现有登录态（Cookie 直通为主，postMessage 注入为兜底） |
| **API 优先** | 嵌入页只是官方前端的一种精简形态，所有能力必须可通过 API 独立完成 |
| **最小界面** | 默认只展示对话核心区，不出现完整七页导航 |
| **安全边界清晰** | 默认拒绝被嵌、origin 校验、频控、不暴露密钥与管理能力 |
| **兼容老系统** | 不依赖过新浏览器特性；跨域场景优先 postMessage 注入 Token |

### 1.3 非目标（本方案不做）

- 在嵌入窗口内创建 / 编辑 Agent、Skill、Connector
- 完整审计、用户管理、模型配置页面
- 可视化工作流编排
- 第三方插件市场
- 复杂白标主题编辑器（企业版再扩展）
- 匿名访客会话（曾评估「影子访客用户」方案，因现有资源表均强制归属用户且引入公共暴露面，不采用）

---

## 2. 嵌入形态定义

### 2.1 默认形态：对话窗口（Chat Embed）

嵌入后用户看到的是一个**干净的对话框**，而不是完整控制台。

```
┌──────────────────────────────────────────┐
│  Agent 显示名                    [可选×] │  ← 极简顶栏（可隐藏）
├──────────────────────────────────────────┤
│                                          │
│  消息列表                                 │
│  · 用户消息                               │
│  · AI 回复（支持流式）                     │
│  · 工具调用过程 / 状态                     │
│  · 人工确认卡片（就地完成，不跳转）         │
│                                          │
├──────────────────────────────────────────┤
│  [输入框……………………]              [发送]   │
└──────────────────────────────────────────┘
```

可选扩展：

- 左侧或顶部可折叠的「当前会话历史」
- 多 Agent 切换（第一期可不做，固定一个 agent_id）

### 2.2 与完整控制台的关系

| 能力 | 完整控制台 | iframe 嵌入窗口 |
|------|------------|-----------------|
| 登录 / 身份 | ✅ | ✅ 复用平台登录态（Cookie 直通 / postMessage 兜底） |
| 对话 / 发消息 | ✅ | ✅ 核心 |
| 查看工具调用与确认 | ✅ | ✅ 就地完成 |
| 创建 / 编辑 Agent | ✅ | ❌ |
| Skill / Tool / Connector 管理 | ✅ | ❌ |
| 模型与密钥配置 | ✅ | ❌ |
| 审计查询 | ✅ | ❌（可只读展示当前 Task 相关事件） |
| 用户管理 | ✅ | ❌ |

**结论**：配置在控制台，使用在嵌入窗口。

---

## 3. 接入方式

### 3.1 URL 形式（推荐）

```text
https://{host}/embed/chat
  ?agent_id={agentId}          # 必填：使用的 Agent
  &conversation_id={id}        # 可选：恢复已有会话
  &theme=light|dark            # 可选，默认 light
  &lang=zh|en                  # 可选，默认 zh
  &hide_header=1               # 可选：隐藏顶栏
  &hide_history=1              # 可选：隐藏历史
  &primary_color=#1677ff       # 可选：主色（有限白标）
```

### 3.2 宿主页面示例

```html
<iframe
  id="zt-embed"
  src="https://z-torque.example.com/embed/chat?agent_id=agt_xxx&theme=light"
  style="width:100%;height:600px;border:none;border-radius:8px;"
  allow="clipboard-write"
></iframe>
```

### 3.3 认证方式

#### 主路径：Cookie 直通（同站部署，推荐）

- 用户先在 Z-Torque 平台完成登录。登录 / 刷新接口已下发 HttpOnly Cookie（`access_token`，`SameSite=Lax`，见 `app/api/routes/auth.py`）。
- 嵌入页部署在平台同源（`/embed/chat`），页面内的 API 调用为**同源请求**，Cookie 自动携带；`get_current_user` 既有 Cookie 兜底（`app/api/deps.py`）直接放行。
- **零后端新增接口**，嵌入页与 API 同源，方案本身不依赖 CORS。

**浏览器硬约束（决定部署要求）：**

| 宿主与平台关系 | iframe 内 Cookie 行为 | 结果 |
|----------------|------------------------|------|
| **同站**（同一根域，如 `app.xxx.com` 嵌 `console.xxx.com`） | 正常携带（`SameSite=Lax` 允许同站子域） | ✅ 主路径，体验即「登录一次、处处可用」 |
| **跨站**（不同根域） | 平台 Cookie 属第三方 Cookie，Safari / 新版 Chrome 默认拦截 | ❌ 会出现「用户已登录但 iframe 内未认证」，走兜底路径 |

#### 兜底路径：postMessage 注入 Token（跨站部署）

- 宿主在 iframe load 后通过 postMessage 注入 `access_token`（协议见 §5）；嵌入页改用 `Authorization: Bearer` 调用 API（页面本身仍在平台域下，与 API 同源，同样不依赖 CORS）。
- **Token 获取由宿主侧自行解决**（如与平台账号打通、集成账号代登录），平台不提供面向宿主的 Token 签发接口。
- 未配置 `frame-ancestors` 白名单时嵌入会被浏览器拒绝（见 §8），因此跨站宿主必须先进入白名单。

#### 不采用：URL 短期 Token

v1 曾规划「URL 短期 Token」方式，v1.1 起不再采用：query 中的 Token 存在浏览器历史、Referrer 与网关日志泄露面，且在「登录态复用」身份模型下无必要。

### 3.4 登录态生命周期

| 项 | 说明 |
|------|------|
| access_token | 有效期由 `ACCESS_TOKEN_TTL_MINUTES` 控制（当前 60 分钟）。过期后嵌入页发 `auth.expired`：主路径下引导用户回平台刷新，兜底路径下由宿主重新注入 |
| refresh_token | 属于该用户的 AuthSession（默认 7 天）。兜底路径下宿主可持有以自动续期，代价是宿主获得该身份的长期行动能力 |
| 吊销 | 用户 / 管理员在控制台吊销对应 AuthSession 后，嵌入窗口立即失效 |

---

## 4. 页面与路由设计

### 4.1 新增路由

| 路径 | 说明 |
|------|------|
| `/embed/chat` | 嵌入对话页（本方案核心） |
| `/embed/unauthorized` | 未登录 / Token 无效或无权限时的提示页 |
| `/embed/error` | 通用错误页 |

完整控制台路由保持不变（`/login`、`/chat`、`/agents`、`/skills`、`/models`、`/connectors`、`/audit`、`/users`）。

### 4.2 布局差异

| 项目 | 完整控制台 | Embed 布局 |
|------|------------|------------|
| 侧边栏 | 有 | 无 |
| 顶部导航 | 有（七页） | 无或极简 Agent 名 |
| 登录页 | 有 | 不出现（登录态驱动） |
| 页脚 / 关于 | 可有 | 无 |
| 最小宽度 | 桌面优先 | 适配 360px+ 窄屏 |

实现建议：共用消息列表、输入框、确认卡片等组件，仅替换 Layout。

---

## 5. postMessage 协议（跨站兜底与事件通知）

本协议是**跨站场景的认证兜底通道**与宿主事件通知通道；同站主路径不依赖它。所有消息使用统一信封：

```ts
interface ZTEmbedMessage {
  source: "z-torque-embed";
  type: string;
  payload?: unknown;
  requestId?: string;   // 可选，用于请求-响应配对
}
```

### 5.1 宿主 → 嵌入页

| type | payload | 说明 |
|------|----------|------|
| `auth.token` | `{ access_token: string, expires_at?: number }` | 注入或刷新 Token |
| `config.update` | `{ theme?, lang?, primary_color? }` | 运行时更新外观 |
| `conversation.open` | `{ conversation_id?: string, agent_id?: string }` | 打开/切换会话 |
| `ui.resize` | `{ height?: number }` | 宿主主动控制高度（少用） |

### 5.2 嵌入页 → 宿主

| type | payload | 说明 |
|------|----------|------|
| `ready` | `{ version: string }` | iframe 已加载，可接收 Token |
| `auth.required` | `{ reason: string }` | Cookie 未命中，需要宿主提供 Token |
| `auth.expired` | `{}` | Token 失效 |
| `conversation.started` | `{ conversation_id, agent_id }` | 新会话开始 |
| `message.sent` | `{ conversation_id, message_id }` | 用户发送消息 |
| `task.created` | `{ task_id, conversation_id }` | 创建了 Task |
| `task.updated` | `{ task_id, status }` | Task 状态变化 |
| `approval.required` | `{ task_id, approval_id, summary }` | 需要人工确认 |
| `task.completed` | `{ task_id, status, summary? }` | Task 终态 |
| `error` | `{ code, message }` | 可展示错误 |
| `ui.height` | `{ height: number }` | 建议宿主调整 iframe 高度 |
| `ui.close` | `{}` | 用户点击关闭（若显示关闭按钮） |

### 5.3 安全约定

- 嵌入页**只接受**来自配置白名单 origin 的消息（或启动参数指定的 parent origin）。
- 宿主应校验 `event.origin` 是否为 Z-Torque 域名。
- 不在 postMessage 中传递长期密钥或管理 Token；仅传递当前用户的短期 access_token。

---

## 6. 后端配合要求

### 6.1 已有能力复用（零新增业务接口）

- 对话与消息 API（含 Cookie 认证兜底）
- Task 创建 / 查询 / 取消
- SSE 事件流（含工具调用、确认、终态）
- 人工确认接口
- 审计链路（嵌入产生的操作归属登录用户，天然落审计）

### 6.2 需要新增的配置与安全项

| 项 | 说明 |
|------|------|
| **frame-ancestors 白名单** | 新增配置项（如 `embed_frame_ancestors`）：`/embed/*` 响应按白名单输出 `Content-Security-Policy: frame-ancestors 'self' <宿主 origin>`；**默认为空 = 输出拒绝被嵌（`X-Frame-Options: DENY` / `frame-ancestors 'none'`）**。完整控制台路由建议固定输出 `frame-ancestors 'none'`，防止控制台本身被嵌 |
| **频控与配额** | 对嵌入窗口触发的对话 / 任务接口按「用户 + IP」频控（如 `embed_rate_limit_per_minute`）；触发时返回明确错误码并写审计 |
| **Agent 校验** | 嵌入页传入的 `agent_id` 必须存在、已启用（含已发布版本），且在当前用户可访问范围内（与控制台一致），否则返回明确错误码 |
| **CORS（可选）** | 嵌入页与 API 同源，本方案不依赖 CORS；仅当宿主前端需要**直调**平台 API 时按白名单配置（可循 `mcp_allowed_hosts` 的白名单模式新增配置项） |

### 6.3 配置边界（再次强调）

- Agent 是否允许被嵌入：可在 Agent 上增加「仅控制台 / 可嵌入」标记（可选，第一期用 agent 校验 + frame-ancestors 白名单代替）。
- 所有 Agent / Skill / 模型 / 连接器配置仍在完整控制台完成。

### 6.4 明确不做

- 不新增嵌入 Token 签发 / 刷新接口
- 不引入访客身份、匿名会话或影子用户
- 不在 Token / Cookie 层面做权限收敛（登录态即完整用户身份），收敛依靠 UI 层面与宿主可信前提

---

## 7. 前端实现要点

### 7.1 技术栈

与现有控制台一致：React 18 + TypeScript + Vite + React Router（路由级 lazy 拆包，`/embed` 增加独立 chunk 即可）。

### 7.2 目录增量（按现有 `console/src` 结构）

```text
console/src/
  layouts/
    EmbedLayout.tsx            # 无侧边栏极简布局（新增 layouts/ 目录）
  pages/
    embed/
      ChatEmbed.tsx            # 嵌入对话页
      EmbedUnauthorized.tsx    # 未登录 / Token 失效提示
  services/
    embedBridge.ts             # postMessage 收发与 origin 校验（跨站兜底用）
```

认证状态复用现有 `stores/auth.tsx`，嵌入场景仅切换「登录态来源」（Cookie 直通 / 注入 Token），不新建状态库。

### 7.3 关键交互

1. **加载与登录态探测**
   - 解析 URL 参数 → 请求当前用户信息（同源 Cookie 自动携带）
   - 成功 → 渲染对话框
   - 失败 → 若宿主 origin 在白名单，发 `auth.required` 等待注入，注入后重试
   - 仍失败 → 展示「请先登录 Z-Torque 平台」引导（不强制在嵌入页内登录）
2. **发消息**：走现有 Chat / Task API；SSE 展示流式与工具过程，组件复用完整版 Chat。
3. **人工确认**：在消息流内展示确认卡片，调用现有确认接口，不跳转。
4. **高度自适应**：内容变化时通过 `ui.height` 通知宿主（可选，宿主也可固定高度）。
5. **错误**：Token 失效 → `auth.expired` + 友好提示；其他错误用统一错误码展示。

### 7.4 与完整版 Chat 的复用

- 消息列表、流式渲染（Markdown）、工具调用卡片、确认卡片、输入框等组件**必须复用**。
- 仅 Layout、路由守卫、登录态来源不同。

---

## 8. 安全清单

| 项 | 要求 |
|----|------|
| 认证 | 主路径复用平台登录 Cookie（HttpOnly）；跨站兜底用 postMessage 注入；禁止匿名访问 |
| 权限 | 嵌入窗口使用完整用户登录态；后端不因嵌入放开任何接口，UI 层面不出现管理入口 |
| 点击劫持 | `frame-ancestors` 白名单**默认收紧**：未配置 = 拒绝被嵌；仅白名单宿主可嵌；控制台路由禁止被嵌 |
| 频控 | 嵌入对话 / 任务接口按「用户 + IP」频控，防止登录态被用于消耗模型额度；触发事件入审计 |
| Origin | postMessage 双向校验 origin |
| XSS | 消息与模型输出按现有策略转义 / 消毒 |
| 密钥 | 永不下发模型 Key、Connector 凭据到前端 |
| 审计 | 嵌入产生的对话与 Task 同样写入审计（归属登录用户），不可关闭 |

---

## 9. 落地步骤建议

| 步骤 | 内容 | 产出 |
|------|------|------|
| **S1** | 确定 URL 参数与路由；实现 EmbedLayout + 空壳 ChatEmbed | 可打开的嵌入页骨架 |
| **S2** | 接入现有对话 / SSE / 确认能力（Cookie 直通）；Agent 校验 | 同站宿主可完整对话 |
| **S3** | postMessage 兜底（ready / auth.token / auth.expired / 关键事件）+ 未登录引导页 | 跨站宿主可注入 Token 并收事件 |
| **S4** | 安全配置化：frame-ancestors 白名单、频控与配额、嵌入专用错误码、审计核验 | 安全可配置 |
| **S5** | 高度自适应、主题参数、错误页、宿主集成示例（`docs/examples/host-embed-example.html`）与集成文档 | 可交付集成方 |
| **S6** | 安全与兼容性测试（同站 Cookie 直通、跨站 Cookie 拦截 → 兜底注入、Token 过期、无 agent 权限、频控触发、窄屏） | 验收通过 |

---

## 10. 验收标准（摘要）

1. 用户在平台登录后，**同站宿主**页面用 iframe 打开 `/embed/chat?agent_id=...`，无需再次登录即可完成一次完整对话（含流式与工具展示）。
2. 需要人工确认时，在嵌入窗口内完成，不跳转到完整控制台。
3. 未登录或会话过期时，有明确提示；**跨站宿主**通过 postMessage 注入 Token 后可正常使用（含模拟第三方 Cookie 被拦截的场景）。
4. 未配置 `frame-ancestors` 白名单时，嵌入被浏览器拒绝；配置后仅白名单宿主可嵌。
5. 嵌入窗口不出现侧边栏、Agent 管理、Skill 管理、模型配置等入口。
6. 频控触发时有明确提示，且事件写入审计。
7. 嵌入产生的操作全部进入现有审计链路，归属登录用户。
8. 提供至少一份宿主集成示例（HTML + postMessage）。

---

## 11. 后续可扩展（非本方案范围）

- 多 Agent 切换、会话列表完整管理
- 更丰富的白标（Logo、自定义 CSS）
- 轻量 JS SDK（封装 iframe 创建与 postMessage）
- 企业版：按租户配置 frame-ancestors、强制 SSO、跨租户账号打通

---

## 12. 文档关系

| 文档 | 关系 |
|------|------|
| 形态确认书 v1 D4 / D9 | 本方案落实「嵌入宿主为辅」「前端可替换」「API 优先」 |
| 轻量控制台（已落地） | 复用其 Chat、Task、SSE、确认等能力与组件 |
| 现有 API 文档 | 嵌入页仅调用已有对话与 Task 类接口，后端零新增业务接口 |
| 内核契约 | 不新增 Port；不改变六件不变量 |

---

**版本**：v1.1（2026-10-19）
**维护**：与控制台及 API 文档同步修订；破坏性变更需升版本并说明兼容策略。
