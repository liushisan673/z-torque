# Z-Torque 核心插件详细开发文档 v1

> **文档性质**：核心插件开发规格与落地指南  
> **适用版本**：Z-Torque 社区版（Community）为主，兼顾企业版扩展方向  
> **上游依据**：  
> - `AI_Runtime_形态确认书_v1.md`（官方 6–8 个插件、暂不开放第三方）  
> - `Z-Torque_内核契约_v1.md`（六件不变量、21 Port、插件五条纪律）  
> - 阶段 0 / 阶段 1 / 阶段 4 / 阶段 5 / 阶段 7 开发文档  
> **定稿建议日期**：2026-10-20  

---

## 一、总览与原则

### 1.1 产品定位回顾

Z-Torque = **一份开源内核 + 官方插件 + 两档授权 + 三套装配预设**。

- 内核只做六件不变量，其余能力全部经 **Port** 进出。
- 插件是 Port 的具体实现提供者。
- 社区版优先内置实现 + 少量官方/内置插件；企业版通过官方专有插件扩展合规、连接、规模能力。
- **第三方插件 API 暂不开放**，先做官方 6–8 个插件。

### 1.2 插件在架构中的位置

```
Profile（装配预设）
    ↓
Module（声明需要哪些 Port）
    ↓
Plugin（提供 Port 实现）  ← 本文档聚焦此处
```

解析优先级（强制）：

**显式配置 > 插件 priority > 内置默认 > UNAVAILABLE**

同 priority 且未显式指定 → 启动失败。

### 1.3 插件五条纪律（不可违反）

1. **manifest 唯一入口**：`apiVersion: zsoftym.ai/plugin.v1`，声明 `provides / requires / permissions / config_schema`。
2. **只实现内核已定义的 Port**，不允许插件自定义新 Port。
3. **只经内核 SDK 读写状态**，禁止直连内核表。
4. **权限声明式**：未声明的行为（网络 / 文件 / 进程 / 密钥）内核直接拒绝。
5. **插件级熔断与配额强制**：单个插件不得拖垮内核。

### 1.4 三条红线（插件同样遵守）

- 审计不可关闭
- 快照不可热改
- 身份不可绕过

---

## 二、插件通用开发规范

### 2.1 推荐目录结构

```
z-torque/
├── app/
│   ├── kernel/
│   │   ├── ports/              # Port 接口定义（内核拥有）
│   │   └── registry/           # 注册与解析逻辑（内核拥有）
│   └── builtin_plugins/        # 内置 / 官方插件实现
│       ├── __init__.py
│       ├── openai_compatible/
│       │   ├── __init__.py
│       │   ├── plugin.yaml     # 或 plugin.json / manifest.py
│       │   ├── provider.py
│       │   └── config_schema.py
│       ├── http_connector/
│       ├── mcp_connector/
│       ├── local_memory/
│       ├── approval_queue/
│       └── ...
```

**硬性约束**：

- Kernel **不得** import `builtin_plugins`。
- 插件通过 Registry 注册，由装配层在启动时加载。
- 社区版进程内加载；企业版可演进为 sidecar，但接口保持一致。

### 2.2 Manifest 规范（强制）

每个插件必须提供唯一 manifest，推荐使用 YAML（也可用 JSON 或 Python 声明）：

```yaml
apiVersion: zsoftym.ai/plugin.v1
kind: Plugin
metadata:
  name: openai-compatible-model-provider
  version: "1.0.0"
  displayName: "OpenAI 兼容模型提供商"
  description: "提供 OpenAI 兼容协议的 model.provider 实现"
  official: true                    # 官方插件标记
  priority: 100                     # 数值越大优先级越高（在未显式配置时）

spec:
  provides:                         # 本插件实现的 Port
    - port: model.provider
      implementation: openai_compatible.provider.OpenAICompatibleProvider
  requires:                         # 依赖的其他 Port（可选）
    - secret.provider
  permissions:                      # 声明式权限
    network:
      - "https://api.openai.com"
      - "https://*.openai.azure.com"
      - "http://localhost:*"        # 本地模型
    secrets:
      - "model.api_key"
    files: []                       # 不需要文件权限则留空
    process: []
  config_schema:                    # JSON Schema 或引用
    $ref: "./config_schema.json"
  healthcheck:
    type: "port_method"
    method: "health"
  circuit_breaker:
    failure_threshold: 5
    recovery_timeout_seconds: 60
```

### 2.3 配置 Schema 要求

- 使用 JSON Schema 或 Pydantic 模型定义。
- 敏感字段（api_key 等）必须引用 `secret.provider`，不得在配置中明文存储。
- 配置变更后应支持热重载（可选 Port）或要求重启（required Port）。

### 2.4 实现接口约定

每个 Port 在 `app/kernel/ports/` 下定义抽象接口（Protocol 或 ABC）。插件只实现该接口。

示例（伪代码）：

```python
# kernel/ports/model_provider.py
from typing import Protocol, AsyncIterator

class ModelProvider(Protocol):
    async def chat_completion(self, request: ChatRequest) -> ChatResponse: ...
    async def stream_chat_completion(self, request: ChatRequest) -> AsyncIterator[ChatChunk]: ...
    async def health(self) -> HealthStatus: ...
```

插件实现必须：

- 接收内核注入的 `SecurityContext`。
- 所有外部调用经过权限与审计拦截（由内核 Gateway / Interceptor 保证）。
- 返回标准化错误码（引用阶段 0 主表）。
- 支持超时与取消。

### 2.5 注册与加载流程

1. 启动时扫描 `builtin_plugins/`（或配置指定路径）。
2. 解析每个 plugin 的 manifest。
3. 校验 `provides` 是否为已定义 Port。
4. 校验 `permissions` 与运行环境策略。
5. 按 priority + 显式配置解析最终实现。
6. 必需 Port 缺失 → 启动失败（fail fast）。
7. 可选 Port 缺失 → 标记 `UNAVAILABLE`，调用时返回 `AI_CAPABILITY_UNAVAILABLE`。

### 2.6 测试要求（每个插件必须覆盖）

| 类型 | 要求 |
|------|------|
| 单元测试 | 实现类核心逻辑、配置解析、权限边界 |
| 集成测试 | 与对应 Gateway（ModelGateway / SkillGateway）联调 |
| 负面测试 | 缺少权限、超时、熔断、错误码正确 |
| 健康检查 | health 方法返回正确状态 |

---

## 三、社区版首期核心插件清单（推荐 6 个）

根据 21 个 Port 对照表与社区版范围，首期建议落地以下 **6 个核心插件**（覆盖最关键路径）：

| # | 插件名称 | 主要 Port | 必需性 | 优先级 | 说明 |
|---|----------|-----------|--------|--------|------|
| 1 | openai-compatible-model-provider | `model.provider` | required | P0 | OpenAI 兼容 + Mock，支撑模型调用 |
| 2 | rule-based-model-selector | `model.selector` | required | P0 | 按 AgentVersion 绑定规则选择 |
| 3 | http-rest-connector | `connector` | optional | P0 | REST API 业务系统接入 |
| 4 | mcp-connector | `connector` | optional | P0 | MCP 协议支持（stdio + HTTP） |
| 5 | local-memory | `memory.working` + `memory.short` | required | P0 | 会话上下文 + 短记忆 |
| 6 | local-approval-queue | `approval.provider` | optional | P1 | 人工确认队列（最小实现） |

**后续官方插件候选（企业向，不在社区版首期强制）**：

- knowledge-provider（`knowledge.provider` + `vector.store`）
- sandbox-sidecar（`sandbox.driver`）
- sso-identity-provider（`identity.provider` 扩展）
- webhook-notify-channel（`notify.channel`）
- observability-exporter（`observability.sink`）
- external-policy-source（`policy.source`）

总计官方规划控制在 **6–8 个** 范围内。

---

## 四、各插件详细开发规格

### 4.1 openai-compatible-model-provider

**目标 Port**：`model.provider`（required）

**功能范围**：

- 支持 OpenAI Chat Completions 兼容协议（含 Azure OpenAI、本地兼容服务）。
- 支持非流式与流式（SSE）调用。
- 支持 Function Calling / Tool Calls 结构透传。
- 内置 Mock Provider，用于离线测试与 CI。
- API Key 通过 `secret.provider` 注入。

**目录建议**：

```
builtin_plugins/openai_compatible/
├── __init__.py
├── plugin.yaml
├── provider.py          # OpenAICompatibleProvider
├── mock_provider.py     # Mock 实现
├── config_schema.py
└── tests/
```

**Manifest 关键字段**：

```yaml
provides:
  - port: model.provider
    implementation: openai_compatible.provider.OpenAICompatibleProvider
requires:
  - secret.provider
permissions:
  network:
    - "https://api.openai.com"
    - "https://*.openai.azure.com"
    - "http://127.0.0.1:*"
    - "http://localhost:*"
  secrets:
    - "model.*"
```

**配置 Schema 要点**：

```json
{
  "type": "object",
  "properties": {
    "base_url": { "type": "string", "default": "https://api.openai.com/v1" },
    "api_key_secret_ref": { "type": "string" },
    "default_model": { "type": "string" },
    "timeout_seconds": { "type": "integer", "default": 60 },
    "max_retries": { "type": "integer", "default": 2 }
  },
  "required": ["api_key_secret_ref"]
}
```

**实现要求**：

- 必须接收并传递 `SecurityContext`（用于审计与配额）。
- 流式响应必须支持取消（Task 取消时中断）。
- 错误映射到标准错误码（如 `AI_MODEL_TIMEOUT`、`AI_MODEL_UNAVAILABLE`）。
- 用量信息（tokens）必须可被拦截器记录。

**测试要点**：

- Mock 模式下完整 chat + tool_calls 往返。
- 真实兼容服务（可配置）连通性。
- 超时、鉴权失败、熔断场景。

---

### 4.2 rule-based-model-selector

**目标 Port**：`model.selector`（required）

**功能范围**：

- 根据 AgentVersion 冻结的模型绑定规则选择具体 model + endpoint。
- 支持默认模型回退。
- 不因全局配置变更影响已创建 Task 的模型选择（快照语义）。

**实现要点**：

- 输入：AgentVersion 快照中的 model 绑定信息 + 可选运行时约束。
- 输出：选定的 `model.provider` 实例标识 + 模型名。
- 优先级：AgentVersion 显式绑定 > Profile 默认 > 全局默认。

**与内核关系**：

- 由 ModelGateway 在调用前查询本 Port。
- 选择结果写入 Task 执行上下文，供审计。

---

### 4.3 http-rest-connector

**目标 Port**：`connector`（optional）

**功能范围**（对齐阶段 7）：

- 管理 REST 连接配置（base_url、认证方式、请求头模板）。
- 支持 API Key / Bearer / Basic / 匿名。
- 将允许的操作注册为 Tool。
- 所有实际调用必须经过 SkillGateway，本插件只提供底层执行能力。

**目录建议**：

```
builtin_plugins/http_connector/
├── plugin.yaml
├── connector.py
├── tool_factory.py      # 从 Connector 生成 Tool 定义
└── config_schema.py
```

**权限声明示例**：

```yaml
permissions:
  network:
    - "*"                    # 实际由管理员配置的 Connector 白名单进一步收窄
  secrets:
    - "connector.*"
```

**安全约束**：

- 禁止任意内网扫描。
- 响应体大小与超时受全局策略限制。
- 敏感头与 body 在审计中脱敏。

**与 SkillGateway 协作**：

```
SkillGateway → 校验权限 / 参数 / 确认策略
           → 调用 connector 实现
           → 标准化结果返回
```

---

### 4.4 mcp-connector

**目标 Port**：`connector`（与 http-rest 可并存，通过不同实现或 type 区分）

**功能范围**：

- 支持 MCP JSON-RPC 2.0。
- 传输：stdio + HTTP。
- 能力发现：`tools/list`。
- 工具调用：`tools/call`。
- 将 MCP 工具映射为系统 Tool（需管理员确认开放范围）。

**实现要点**：

- stdio 子进程生命周期由插件管理，必须支持超时杀死。
- 不得自动把 MCP 服务全部工具无条件开放给所有用户。
- 结果标准化后交回 SkillGateway。

**权限声明**：

```yaml
permissions:
  process:
    - "spawn_mcp_server"     # 明确声明可启动子进程
  network:
    - "http://*"             # HTTP 传输
  files: []                  # 默认不允许读任意文件
```

---

### 4.5 local-memory

**目标 Port**：`memory.working` + `memory.short`（required）

**功能范围**：

- **Working Memory**：请求级上下文组装（当前对话轮次 + 系统提示 + 相关 Skill 摘要）。
- **Short Memory**：会话级短记忆（最近 N 轮消息、关键实体摘要），落库。
- Long Memory 本插件不实现（标记 optional 或 UNAVAILABLE）。

**实现要点**：

- Working：纯内存或请求上下文，不跨请求持久化。
- Short：使用 DB（社区版 SQLite）存储，按 `conversation_id` + `user_id` 隔离。
- 提供裁剪策略（token 预算 / 轮次上限）。
- 写入与读取均需携带 SecurityContext，防止越权。

**配置示例**：

```yaml
short_memory:
  max_turns: 20
  max_tokens: 4000
  summary_enabled: true
```

---

### 4.6 local-approval-queue

**目标 Port**：`approval.provider`（optional）

**功能范围**（最小实现）：

- 当 Tool / Skill 需要人工确认时，创建审批单并挂起 Task。
- 管理员 / 指定用户在控制台或 API 上 approve / reject。
- 审批结果写回 Task 状态机，触发恢复执行。

**实现要点**：

- 与阶段 2 / 阶段 8 的状态机、Outbox 协同。
- 审批超时策略可配置。
- 审批记录进入审计流（不可关闭）。

**不在本插件范围**：

- 复杂多级审批流（企业版后续）。
- 外部审批系统对接（可作为后续官方插件）。

---

## 五、装配与启用方式

### 5.1 社区版默认装配（standalone Profile）

```yaml
# 示例 profile 片段
profile: standalone
plugins:
  - name: openai-compatible-model-provider
    enabled: true
    priority: 100
    config:
      base_url: "https://api.openai.com/v1"
      api_key_secret_ref: "secret://model/openai_api_key"
  - name: rule-based-model-selector
    enabled: true
  - name: http-rest-connector
    enabled: true
  - name: mcp-connector
    enabled: true
  - name: local-memory
    enabled: true
  - name: local-approval-queue
    enabled: true
```

### 5.2 放入项目的方式（再次明确）

1. 开发完成 → 代码放入 `app/builtin_plugins/<plugin_name>/`。
2. 提供合法 `plugin.yaml`（或等价 manifest）。
3. 通过 Profile 配置或启动参数启用。
4. 系统启动时自动扫描、校验、注册。
5. **不是**前端插件市场下载安装；用户在控制台管理的是已装配的 Skill / Tool / Connector 等资源对象。

### 5.3 能力查询

启动后可通过：

```
GET /api/z-torque/capabilities
```

查看各 Port 的最终解析结果与状态（AVAILABLE / UNAVAILABLE），不暴露密钥。

---

## 六、开发优先级与里程碑建议

| 优先级 | 插件 | 建议阶段 | 依赖 |
|--------|------|----------|------|
| P0 | openai-compatible-model-provider | 阶段 5 | secret.provider、ModelGateway |
| P0 | rule-based-model-selector | 阶段 5 | AgentVersion 快照 |
| P0 | http-rest-connector | 阶段 7 | SkillGateway、secret.provider |
| P0 | mcp-connector | 阶段 7 | SkillGateway |
| P0 | local-memory | 阶段 9 | 会话与 Message 模型 |
| P1 | local-approval-queue | 阶段 8 | Task 状态机、Outbox |

**开发顺序建议**：

1. 先完成 Port 接口定义与 Registry（阶段 1 延伸）。
2. 落地 model.provider + model.selector（打通模型调用）。
3. 落地 connector 两类实现（打通外部系统）。
4. 落地 memory（完善上下文）。
5. 落地 approval（完善治理闭环）。

---

## 七、交付物检查清单（每个插件）

- [ ] 合法 manifest（apiVersion、provides、requires、permissions、config_schema）
- [ ] 实现对应 Port 接口
- [ ] 配置 Schema 与敏感信息处理正确
- [ ] 单元测试 + 与 Gateway 的集成测试
- [ ] 错误码使用规范
- [ ] 健康检查可用
- [ ] 熔断 / 超时 / 配额行为符合预期
- [ ] 文档中说明本插件覆盖的 Port 与边界
- [ ] 不违反五条纪律与三条红线

---

## 八、后续演进方向（非首期）

- 官方知识库插件（knowledge.provider + vector.store）
- 沙箱 sidecar 插件（sandbox.driver）
- SSO / OIDC 身份插件
- 更丰富的 notify.channel（邮件、Webhook、企业IM）
- 可观测性导出插件
- 企业版插件签名与离线 license 校验机制

以上内容与当前内核契约、形态确认书保持一致。若后续新增 Port，必须走内核契约变更流程（至少两个真实实现 + 定版本号）。

---

**版本**：v1（2026-10-20）  
**关联文档**：形态确认书 v1、内核契约 v1、阶段 0/1/4/5/7/9 开发文档  
**维护说明**：插件清单与规格变更时同步更新本文档，并在变更说明中标注影响的 Port 与 Profile。
