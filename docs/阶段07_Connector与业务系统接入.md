# Z-Torque 社区版阶段 7：Connector 与业务系统接入

**文档编号：** ZT-COMMUNITY-P7  
**文档版本：** 1.1（P1/P2 优化）  
**阶段名称：** Connector 与业务系统接入  
**前置阶段：** 阶段 0—6  
**阶段状态：** 待实施  
**错误码：** Connector 不可用使用 `AI_CONNECTOR_UNAVAILABLE`；外部调用失败必须使用已登记错误码，并保留 `trace_id` 和脱敏诊断信息。

## 1. 阶段目标

本阶段建设 Z-Torque 的业务系统连接能力，使平台能够通过受控的 REST API、只读数据库和 MCP 服务访问企业现有系统，并将可用的业务操作注册为 Tool，供 Skill 组合调用。

完成后，系统应具备以下能力：

1. 管理外部系统连接配置及其凭据引用。
2. 支持 REST API Connector，覆盖常见 HTTP 请求与响应处理。
3. 支持只读数据库 Connector，限制可访问的数据源、表和查询类型。
4. 支持 MCP 的服务配置、能力发现与调用。
5. 将连接配置转换为可复用的 Tool 定义。
6. 允许多个 Skill 复用同一个 Tool。
7. 所有调用经过 SkillGateway，执行前进行权限、参数和安全策略检查。
8. 将连接、Tool 调用和失败情况纳入 TaskEvent、执行日志及审计体系。

**本阶段不重新设计 Tool、Skill、SkillVersion 或 AgentVersion。** 需要复用前面阶段已确定的数据模型、版本规则、权限体系与执行入口。

## 2. 需求范围

### 2.1 本阶段必须完成

| 模块           | 具体要求                                                    |
| -------------- | ----------------------------------------------------------- |
| Connector 管理 | 新增、查询、编辑、停用连接配置                              |
| REST API       | 支持 HTTP 方法、路径、请求参数、请求头及响应解析            |
| 身份认证       | 支持 API Key、Bearer Token、Basic Auth 和明确配置的匿名连接 |
| 凭据管理       | 敏感凭据只保存 Secret URI，不在数据库中保存明文             |
| 数据库连接     | 支持只读查询、数据源白名单、表白名单和结果限制              |
| MCP            | 支持 JSON-RPC 2.0、`tools/list` 和 `tools/call`             |
| MCP 传输       | 支持 stdio 与 HTTP 两种传输方式                             |
| Tool 注册      | 将允许暴露的连接操作登记为可复用 Tool                       |
| Skill 集成     | SkillVersion 绑定精确的 ToolVersion                         |
| 权限治理       | 执行时检查用户权限、Tool 权限及目标资源范围                 |
| 调用审计       | 记录调用人、目标、操作、结果、耗时和脱敏摘要                |

### 2.2 本阶段暂不实现

- 任意 SQL 写入、DDL 或数据库管理功能。
- 无限制的内网地址访问或自动扫描企业网络。
- 任意远程代码下载和执行。
- 自动将 MCP 服务提供的全部工具无条件开放给所有用户。
- 复杂的企业级凭据轮换平台。
- 完整的分布式连接代理、连接池集群或高可用网关。
- 绕过现有身份与权限体系的服务调用。

## 3. 技术方案

### 3.1 核心对象关系

Connector、Tool 和 Skill 是三个不同层次的对象。

```
Connector
  │
  ├── REST API Connector
  ├── Read-only DB Connector
  └── MCP Connector
          │
          ▼
    Tool / ToolVersion
          │
          ▼
    Skill / SkillVersion
          │
          ▼
    Agent / AgentVersion
          │
          ▼
    SkillGateway
          │
          ▼
      外部业务系统
```

各对象职责如下：

- **Connector**：描述连接到哪个系统、使用什么传输方式、如何认证以及允许访问哪些资源。
- **Tool**：描述一个最小可执行动作，例如查询订单状态、读取客户资料或调用某个 MCP 工具。
- **ToolVersion**：固定 Tool 的参数 Schema、执行配置及必要的来源信息。
- **Skill**：组合多个 Tool，并提供相应的业务指令和执行规则。
- **SkillGateway**：统一执行校验、权限判断、人工确认、超时控制、结果规范化和日志记录。

Connector 不直接执行 Agent 任务，也不能成为绕过 SkillGateway 的第二条执行通道。

### 3.2 Connector 基础设计

每个 Connector 至少应包含以下信息：

| 字段             | 含义                               |
| ---------------- | ---------------------------------- |
| `id`             | Connector 唯一标识                 |
| `tenant_id`      | 所属租户标识，社区版使用单租户模式 |
| `name`           | 连接名称                           |
| `connector_type` | REST、DB 或 MCP                    |
| `status`         | 草稿、启用、停用或不可用等状态     |
| `config`         | 非敏感连接配置                     |
| `secret_ref`     | 凭据引用，不保存明文               |
| `target_policy`  | 允许访问的目标地址或资源范围       |
| `created_by`     | 创建者                             |
| `created_at`     | 创建时间                           |
| `updated_at`     | 更新时间                           |

字段应优先融入前面阶段已有的 Connector 模型。只有现有结构确实无法表达时，才增加必要字段。

连接配置的读取与实际连接执行需要分离：用户可以看到经过脱敏的连接信息，但不能通过管理 API 读取 Secret Store 中的明文凭据。

### 3.3 REST API Connector

REST Connector 用于连接现有业务系统提供的 HTTP API。

每个 REST Connector 可以配置多个操作，但每个操作应独立注册为一个 Tool。

示例：

```
Connector：客户管理系统
  ├── Tool：查询客户基本信息
  ├── Tool：查询客户订单
  └── Tool：查询客户服务记录
```

单个操作建议包含：

- HTTP 方法。
- 允许访问的目标地址或预先登记的基础 URL。
- 固定路径模板。
- 路径参数、查询参数和请求体 Schema。
- 请求头配置。
- 认证方式和凭据引用。
- 连接超时与总请求超时。
- 响应解析规则。
- 允许返回的字段或数据范围。
- Tool 所需权限。
- 是否需要人工确认。

请求目标不能由模型自由拼接完整 URL。模型只能提供符合 Schema 的业务参数，实际目标地址由已登记的 Connector 配置决定。

#### 请求处理流程

1. SkillGateway 接收 Tool 调用请求。
2. 校验当前用户是否有权调用该 Tool。
3. 校验参数 Schema。
4. 检查 Connector 状态与目标地址策略。
5. 从 Secret Store 解析所需凭据。
6. 使用配置好的 HTTP 客户端发起请求。
7. 检查 HTTP 状态码和响应内容。
8. 对响应进行字段过滤、长度限制和敏感信息脱敏。
9. 返回标准化 Tool Result。
10. 记录执行日志与审计事件。

对于 GET、POST 等 HTTP 方法，不能仅根据方法名称判断是否安全。即使是 GET，也应由管理员确认其实际业务语义和允许访问范围。

### 3.4 数据库 Connector：严格只读

社区版数据库连接只提供受控查询能力。

首版建议采用以下限制：

- 只允许管理员配置数据源。
- 只允许登记的数据库地址和数据库名称。
- 只允许访问明确授权的表或视图。
- 优先使用数据库只读账号。
- 应用层使用 SQL 解析或等效校验机制限制查询类型。
- 禁止 INSERT、UPDATE、DELETE、DDL、多语句执行及其他写操作。
- 对查询结果数量、执行时间和返回数据量设置上限。
- 对敏感字段进行限制或脱敏。
- 禁止模型直接提交任意数据库连接字符串。

推荐的查询路径：

```
用户问题
  ↓
Agent / Skill
  ↓
SkillGateway
  ↓
DB_QUERY Tool
  ↓
参数与权限校验
  ↓
只读数据库账号
  ↓
允许的表 / 视图
  ↓
结果限制与脱敏
  ↓
标准化 Tool Result
```

数据库只读不能只依赖提示词要求模型不要写入。必须同时通过数据库账号权限、应用层查询校验和资源限制控制风险。

#### DB_QUERY Tool 的输入设计

优先采用结构化参数，例如：

- `table`：允许访问的表或视图。
- `columns`：允许返回的字段。
- `filters`：受控筛选条件。
- `limit`：结果数量上限内的查询数量。
- `order_by`：允许排序的字段。

如果项目需要支持 SQL 输入，也必须经过严格的语句解析、表和字段白名单检查，并通过只读账号执行。不能只使用简单的字符串匹配来判断 SQL 是否安全。

### 3.5 MCP Connector

MCP 用于接入遵循 Model Context Protocol 的外部服务。

社区版支持：

- JSON-RPC 2.0 消息格式。
- `tools/list`：发现服务提供的工具。
- `tools/call`：调用指定工具。
- stdio 传输。
- HTTP 传输。
- 服务健康检查。
- 配置化启动参数、超时和认证。
- 对 MCP 返回结果进行统一校验与脱敏。

#### MCP 工具注册流程

1. 管理员创建 MCP Connector。
2. 配置传输类型、目标、启动参数或 HTTP 地址。
3. 配置认证方式、Secret URI 和目标策略。
4. 系统连接服务并执行能力发现。
5. 展示服务返回的工具名称、描述和参数 Schema。
6. 管理员选择允许暴露的工具。
7. 系统将已批准的工具登记为 Tool。
8. 生成或更新 ToolVersion。
9. Skill 通过 SkillVersion 绑定相应 ToolVersion。
10. 运行时调用仍然通过 SkillGateway。

MCP 服务发现到的工具定义属于外部输入，必须经过校验。不能因为服务支持 `tools/list`，就默认信任所有工具描述、参数 Schema 或调用结果。

#### stdio 传输

- 使用管理员配置的启动命令和参数。
- 禁止从模型输入中动态生成启动命令。
- 限制工作目录、环境变量和可执行文件范围。
- 敏感环境变量只允许使用 Secret URI。
- 管理进程启动、健康状态、异常退出和清理。
- 服务异常退出后按配置进行有限重启，避免无限重启。

#### HTTP 传输

- 管理员显式配置 HTTP 目标地址、传输方式和认证策略。
- 对 HTTPS、匿名连接及允许的目标范围执行策略检查。
- 生产配置默认拒绝不安全的连接方式，除非管理员根据策略显式批准。
- 阻止通过重定向或动态目标参数绕过地址白名单。
- 设置连接超时、请求超时和响应体大小限制。

### 3.6 凭据和目标地址安全

所有 Connector 共用统一的 Secret Store 和目标策略机制。

#### 凭据管理

- API Key、Bearer Token、Basic Auth 密码等敏感信息不写入业务数据库。
- 数据库仅保存 `secret_ref` 和必要的配置状态。
- 查询 API 只能返回是否已配置凭据，不返回凭据本身。
- 日志、TaskEvent、Audit 和错误信息不得记录明文凭据。
- 修改凭据采用单独的安全操作，不通过普通详情接口回显。

#### 目标地址限制

REST 与 MCP HTTP 连接都必须执行目标地址策略检查。

至少需要防范：

- 访问未经登记的公网地址。
- 访问本机管理接口。
- 访问未经授权的内网服务。
- 利用重定向访问其他目标。
- 利用 DNS 解析变化绕过地址策略。

目标地址检查应在实际连接阶段执行，而不只是保存 Connector 配置时检查一次。部署环境应结合网络出口限制，降低应用层策略失效的影响。

### 3.7 权限与调用身份

所有调用都必须明确使用哪种身份访问外部系统。

**用户身份（User Identity）**：代表当前用户的授权范围。适用于需要严格遵守个人业务权限的操作。

**服务身份（Service Identity）**：使用管理员配置的系统级连接凭据。适用于经过批准的共享服务调用，但不能自动获得对所有业务数据的访问权限。

必须遵守以下规则：

1. `SecurityContext` 从请求入口贯穿 Task、Agent、Skill 和 Tool 执行链路。
2. Connector 的可用性不等于当前用户有权使用它。
3. 每次 Tool 调用都要检查用户权限和资源范围。
4. Service Identity 必须经过显式配置，不能由模型自行选择。
5. 不能将服务账号权限误当成当前用户的个人权限。
6. 用户没有权限的数据，即使服务账号可以读取，也不能因此自动向用户披露。
7. 用户申请访问某个 Connector 时，系统可以记录申请并交由管理员处理，但不能自动授予权限。

### 3.8 失败处理与标准化结果

Connector 失败时，应将标准化错误返回给 SkillGateway 和模型执行循环，而不是直接泄露底层异常。

统一结果结构沿用前面阶段约定：

```
{
  "success": false,
  "data": null,
  "error": {
    "code": "AI_CONNECTOR_TIMEOUT",
    "message": "外部系统请求超时"
  },
  "metadata": {
    "latency": 3000,
    "truncated": false
  }
}
```

需要区分：

- 连接失败。
- 身份认证失败。
- 权限不足。
- 参数校验失败。
- 外部系统返回业务错误。
- 请求超时。
- 响应格式错误。
- 返回内容超过限制。
- MCP 协议或工具调用错误。

失败结果应让模型能够解释当前操作无法完成，但不能包含明文凭据、内部网络拓扑、完整异常堆栈或其他敏感信息。

重试必须谨慎。对可能产生写操作或其他副作用的 HTTP 操作，不能仅因为超时就自动重试。首版优先支持明确安全、可幂等的查询操作。

## 4. 接口与数据设计

### 4.1 数据模型

优先扩展已有 `zr_connector`，并复用现有 Tool、ToolVersion 和 SkillVersion 数据模型。

| 对象                     | 主要职责                                 |
| ------------------------ | ---------------------------------------- |
| `zr_connector`           | 连接类型、状态、配置、凭据引用和目标策略 |
| `zr_tool`                | 单个可执行动作的基础定义                 |
| `zr_tool_version`        | 参数 Schema、执行配置和版本快照          |
| `zr_skill_version`       | 绑定准确的 ToolVersion                   |
| `zr_skill_execution_log` | 记录 Tool / Skill 执行结果及耗时         |
| `zr_task_event`          | 记录任务执行过程摘要                     |
| `zr_audit_log`           | 记录连接配置变更和敏感操作               |

如果现有数据模型尚未包含 Connector 与 Tool 的关联，应增加必要的关联关系，使一个 Connector 可以提供多个 Tool，一个 Tool 也能被多个 SkillVersion 复用。

关联关系必须能追溯到具体的连接配置及 ToolVersion。不能让历史任务在 Connector 配置变化后，无条件使用新的目标或参数定义。

### 4.2 建议 API

统一使用 `/api/z-torque/*` 前缀。开发时应优先扩展已有同类接口，避免重复创建路由。

| 方法    | 路径                                                     | 用途                              |
| ------- | -------------------------------------------------------- | --------------------------------- |
| `GET`   | `/api/z-torque/connectors`                               | 查询连接列表                      |
| `POST`  | `/api/z-torque/connectors`                               | 创建连接配置                      |
| `GET`   | `/api/z-torque/connectors/{connector_id}`                | 查询连接详情，敏感值脱敏          |
| `PATCH` | `/api/z-torque/connectors/{connector_id}`                | 更新非敏感配置                    |
| `POST`  | `/api/z-torque/connectors/{connector_id}/test`           | 测试连接                          |
| `POST`  | `/api/z-torque/connectors/{connector_id}/disable`        | 停用连接                          |
| `POST`  | `/api/z-torque/connectors/{connector_id}/discover`       | 发现 MCP 工具或读取允许的接口定义 |
| `GET`   | `/api/z-torque/connectors/{connector_id}/tools`          | 查询由连接提供的 Tool             |
| `POST`  | `/api/z-torque/connectors/{connector_id}/tools/register` | 登记经过批准的 Tool               |
| `GET`   | `/api/z-torque/tools`                                    | 查询当前用户可访问的 Tool         |
| `GET`   | `/api/z-torque/tools/{tool_id}/versions`                 | 查询 ToolVersion                  |

### 4.3 API 约束

- 创建和修改 Connector 必须校验当前用户权限。
- 凭据写入采用独立的安全操作，普通接口不返回明文。
- 测试连接也必须执行目标地址和凭据策略检查。
- 工具发现不等于工具授权，发现与注册必须分离。
- 数据库连接测试不得执行业务写操作。
- 停用 Connector 后，新调用应被拒绝或标记为不可用。
- 已有任务的执行结果和历史审计不能因 Connector 停用而被删除。
- Tool 注册、版本变化、Connector 修改和停用均应写入审计。
- 连接 API 的错误响应不能包含完整连接字符串、认证头或数据库凭据。

## 5. 任务排期

### T7-01：Connector 契约与公共配置

工作内容：

- 检查已有 Connector 数据模型与接口。
- 统一连接状态、认证方式、目标策略和错误码。
- 设计 Connector 与 Tool 的关联。
- 定义凭据引用及脱敏规则。
- 编写公共连接配置校验。

交付物：Connector 契约、数据模型变更方案、公共校验模块。

### T7-02：REST API Connector

工作内容：

- 实现 HTTP 客户端封装。
- 支持固定目标、路径模板、参数 Schema 和响应解析。
- 实现 API Key、Bearer、Basic Auth 和匿名连接配置。
- 增加超时、响应大小限制和错误规范化。
- 将操作注册为 Tool。

交付物：REST Connector、示例连接、接口测试。

### T7-03：只读数据库 Connector

工作内容：

- 实现数据源配置和连接测试。
- 配置只读账号。
- 实现数据库、表、视图和字段白名单。
- 实现查询参数校验与结果限制。
- 验证写入、DDL、多语句和未授权表访问会被拒绝。

交付物：只读数据库 Connector、安全测试报告。

### T7-04：MCP Connector

工作内容：

- 实现 JSON-RPC 2.0 通信封装。
- 支持 stdio 与 HTTP 传输。
- 实现 `tools/list` 和 `tools/call`。
- 实现服务健康检查、异常退出和受控重启。
- 对发现的工具进行管理员审核后再登记为 Tool。

交付物：MCP Connector、模拟服务与协议测试。

### T7-05：Tool 注册与 SkillGateway 集成

工作内容：

- 将连接操作登记为 Tool。
- 建立 ToolVersion 与连接配置的追溯关系。
- 确保 SkillVersion 绑定精确的 ToolVersion。
- 执行权限、Schema、确认、超时和结果规范化。
- 将失败返回模型执行循环。

交付物：Tool 注册流程、执行链路集成测试。

### T7-06：身份、审计与安全治理

工作内容：

- 完成用户身份与服务身份的区分。
- 完成 Secret URI 的读写边界。
- 完成目标地址白名单与实际连接阶段的检查。
- 完成日志脱敏、连接测试审计和配置变更审计。
- 验证 Connector 停用后的调用行为。

交付物：权限测试、安全检查清单和审计记录。

### T7-07：端到端验收

工作内容：

- 通过 Agent → Skill → Tool → Connector 访问测试系统。
- 测试 REST 查询、数据库只读查询和 MCP 工具调用。
- 验证超时、断连、认证失败和异常响应。
- 验证多个 Skill 复用同一个 Tool。
- 验证权限不足时不会访问外部系统。
- 编写启动、配置和故障排查说明。

交付物：阶段验收报告、示例配置和运行说明。

## 6. 测试验收

### 6.1 功能验收

- 

  可以创建、查询、更新和停用 Connector。

- 

  REST Connector 能调用已登记的业务 API。

- 

  REST Connector 能正确处理参数校验和异常响应。

- 

  数据库 Connector 能读取允许的表或视图。

- 

  数据库 Connector 拒绝写入和未授权查询。

- 

  MCP Connector 能完成工具发现和调用。

- 

  MCP 支持 stdio 与 HTTP 两种传输。

- 

  管理员可以选择性地登记 MCP 工具。

- 

  一个 Tool 可以被多个 Skill 复用。

- 

  SkillVersion 能绑定准确的 ToolVersion。

- 

  外部调用结果能够被 SkillGateway 规范化。

- 

  Connector 失败时，模型能获得安全、明确的错误结果。

### 6.2 安全验收

- 

  明文凭据不会进入数据库、API 响应、日志或审计。

- 

  未授权用户不能使用受保护的 Connector 或 Tool。

- 

  目标地址策略能阻止未登记目标。

- 

  重定向不能绕过目标地址限制。

- 

  数据库写操作和越权表访问被拒绝。

- 

  MCP 工具不会因自动发现而自动获得执行权限。

- 

  Tool 调用无法绕过 SkillGateway。

- 

  服务身份不能自动扩大当前用户的数据访问权限。

- 

  Connector 停用后不能发起新的业务调用。

- 

  敏感连接配置变化能够被审计。

### 6.3 稳定性验收

- 

  外部系统超时不会无限占用 Worker。

- 

  MCP 服务异常退出后能够被识别并按策略恢复。

- 

  大响应会被限制或截断，不会无限占用内存。

- 

  错误结果不会泄露底层异常和敏感连接信息。

- 

  连接失败不会导致整个 Runtime 崩溃。

- 

  并发调用不会混淆不同用户的 SecurityContext。

- 

  多个 Skill 复用同一 Tool 时，调用配置和结果互不串线。

## 7. 风险项

| 风险                           | 影响                       | 应对措施                                            |
| ------------------------------ | -------------------------- | --------------------------------------------------- |
| Connector 成为权限绕过入口     | 用户访问原本无权访问的数据 | 每次调用执行用户权限和资源范围检查                  |
| 凭据泄露                       | 外部系统被未授权访问       | Secret URI、统一脱敏和独立凭据管理                  |
| SSRF 或内网探测                | 访问未授权目标             | 目标白名单、连接时校验、限制重定向与网络出口        |
| 数据库误写                     | 破坏业务数据               | 只读账号、语句校验、表白名单和结果限制              |
| MCP 工具定义恶意或错误         | 模型调用危险操作           | 管理员审核、Schema 校验、权限和确认机制             |
| 外部接口响应过大               | 内存压力或上下文溢出       | 响应大小限制、字段过滤和截断                        |
| 服务身份权限过大               | 个人权限被服务账号放大     | 显式身份策略、业务范围检查和最小权限                |
| 自动重试造成副作用             | 重复提交业务操作           | 默认仅重试明确安全、可幂等的操作                    |
| Connector 配置变更影响历史任务 | 行为难以复现               | 固定 ToolVersion 和必要配置快照，记录执行时目标摘要 |

## 8. 上线交付

### 8.1 配置项

建议通过统一配置模块管理以下配置：

| 配置项                           | 用途                 |
| -------------------------------- | -------------------- |
| `CONNECTOR_CONNECT_TIMEOUT`      | 外部连接超时         |
| `CONNECTOR_REQUEST_TIMEOUT`      | 单次请求总超时       |
| `CONNECTOR_MAX_RESPONSE_BYTES`   | 最大响应体限制       |
| `CONNECTOR_DEFAULT_RESULT_LIMIT` | 默认结果数量限制     |
| `CONNECTOR_ALLOWED_TARGETS`      | 允许访问的目标范围   |
| `CONNECTOR_ALLOW_ANONYMOUS`      | 是否允许匿名连接配置 |
| `MCP_PROCESS_RESTART_LIMIT`      | MCP 进程重启次数限制 |
| `MCP_DEFAULT_TIMEOUT`            | MCP 默认请求超时     |

具体配置名应与前面阶段的配置体系统一，避免重复定义同义参数。匿名连接只代表不携带认证凭据，不代表不需要管理员登记、目标限制或业务权限检查。

### 8.2 部署前检查

上线前必须确认：

1. Secret Store 可用，敏感凭据能正确保存和读取。
2. REST Connector 的目标策略生效。
3. 数据库只读账号和表白名单配置正确。
4. MCP stdio 进程能够正常启动、检查健康状态并关闭。
5. MCP HTTP 连接符合传输、认证和目标策略。
6. Connector 生成的 Tool 能够通过 SkillGateway 执行。
7. 连接失败和权限拒绝均能写入必要日志及审计。
8. API 响应、错误日志和审计中不存在明文凭据。
9. 停用 Connector 后，新调用被拒绝。
10. 应用重启后，连接配置和 Tool 登记信息能够正确恢复。

### 8.3 交付内容

本阶段应交付：

- Connector 公共数据模型和接口。
- REST API Connector。
- 只读数据库 Connector。
- MCP Connector（stdio 与 HTTP）。
- Connector 与 Tool 的注册和关联机制。
- Secret URI 与目标地址策略集成。
- SkillGateway 调用链路集成。
- 功能、安全和稳定性测试报告。
- REST、数据库和 MCP 示例配置。
- 部署、配置及故障排查文档。

## 9. 阶段总结

阶段 7 的核心成果，是让 Z-Torque 能够在不绕过现有权限和执行治理的前提下，安全访问企业已有业务能力。

最终关系应保持清晰：

- Connector 管理外部系统连接。
- Tool 定义最小执行动作。
- ToolVersion 固定动作配置和参数契约。
- Skill 组合多个 Tool，形成可复用业务能力。
- AgentVersion 固定使用的 SkillVersion。
- SkillGateway 统一执行权限检查、确认、调用和结果规范化。
- TaskEvent、执行日志与 Audit 提供全链路追踪。

完成本阶段后，Z-Torque 将具备连接外部业务系统的基础能力。下一阶段建议进入**阶段 8：Task 执行治理、状态机、取消机制与故障恢复**，重点把多轮模型调用、Tool 执行、任务状态和系统重启后的恢复机制整合成稳定的运行时闭环。