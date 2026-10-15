# Z-Torque 社区版阶段 4：Skill、Tool 与 SkillGateway

**文档编号：** ZT-COMMUNITY-P4  
**文档版本：** 1.1（P1/P2 优化）  
**阶段名称：** Skill、Tool 与 SkillGateway  
**前置阶段：** 阶段 0—3  
**阶段状态：** 待实施  
**错误码：** 本阶段只引用或登记阶段 0 主表，优先使用 `AI_SKILL_NOT_FOUND`、`AI_TOOL_NOT_FOUND`、`AI_SKILL_TIMEOUT`、`AI_SKILL_FAILED` 和 `AI_PARAM_INVALID`，不得创建同义错误码。  
**开发原则：** 先完成数据模型与版本关系，再完成执行网关，最后接入各类执行器。
> 核心约束：任何 Tool 执行都必须经过 SkillGateway，禁止业务模块直接调用底层执行器。

# 一、阶段目标

## 1.1 核心目标

本阶段需要完成四项核心能力：

1. **Tool 管理**：将 HTTP、只读数据库查询、MCP 等执行动作抽象为可管理、可复用的独立资源。
2. **Skill 管理**：将多个 Tool 组织为可复用的业务能力，并支持纯指令和 MD-Skill 两种实现方式。
3. **版本管理**：建立 ToolVersion、SkillVersion 和 AgentVersion 之间的精确引用关系。
4. **SkillGateway**：建设统一执行入口，对权限、参数、确认策略、超时、重试、结果标准化和执行审计进行集中控制。

本阶段结束后，系统应能够完成以下测试链路：

```
AgentVersion → SkillVersion → ToolVersion → SkillGateway → 执行器 → 标准化结果
```

其中，AgentVersion 和 SkillVersion 都必须引用精确版本，而不是运行时动态读取某个资源的最新配置。

## 1.2 本阶段交付结果

完成后应具备：

- Tool 的创建、查询、编辑、版本发布和归档能力。
- Skill 的创建、查询、编辑、版本发布和归档能力。
- ToolVersion 的不可变版本快照。
- SkillVersion 对精确 ToolVersion 的引用。
- AgentVersion 对精确 SkillVersion 的引用。
- 官方 Skill、Tool 的复制机制。
- 统一的 SkillGateway 执行入口。
- 统一的 Tool 输入校验、权限判断和结果格式。
- Tool 级别与全局策略共同控制的确认机制。
- HTTP、只读数据库查询、MCP 的基础执行能力。
- MD-Skill 的纯指令和脚本执行模式。
- 工具调用失败时向上层返回标准化错误，而不是直接绕过执行链路。
- 可追溯的执行事件与审计记录。
- 完整的单元测试和集成测试。

## 1.3 本阶段暂不实现

以下功能留给后续阶段：

- 真实模型的 Function Calling 循环：阶段 5。
- 完整的 MCP/Connector 管理控制台：阶段 7。
- 完整的统一权限决策和运行安全闭环：阶段 8。
- 短期记忆和文件处理：阶段 9。
- 完整前端控制台：阶段 10。
- 第三方插件代码加载与执行。
- 任意 Python 代码包执行。
- 复杂的多级审批工作流。
- 多 Agent 协作调度。

本阶段可以通过测试接口或内部服务直接调用 SkillGateway，验证执行链路，不必提前开发完整聊天界面。

# 二、需求范围

## 2.1 Tool：最小可执行动作

Tool 是系统能够独立调用的最小执行单元。

它负责描述“可以执行什么动作”，不负责决定完整业务任务应该如何规划。

例如：

- 调用一个查询客户信息的 HTTP API。
- 执行一条符合白名单要求的只读 SQL。
- 调用一个 MCP 服务暴露的工具。
- 调用一个业务系统的查询接口。

### Tool 的基本属性

每个 Tool 至少需要：

- 唯一 ID。
- 所属租户。
- 所有者。
- 名称和描述。
- 来源类型。
- Tool 类型。
- 输入参数 Schema。
- 输出数据 Schema 或结果约束。
- 所需权限列表。
- 是否需要人工确认。
- 超时和重试策略。
- 当前发布版本。
- 创建、更新时间与归档信息。

Tool 描述必须尽可能明确。后续模型可能根据 Tool 的名称、描述和参数 Schema 决定是否调用它，因此不能使用只有开发人员才能理解的内部名称。

### Tool 类型

社区版第一阶段支持以下执行类型：

| 类型       | 用途                    | 主要约束                                   |
| ---------- | ----------------------- | ------------------------------------------ |
| `HTTP`     | 调用 HTTP API           | 目标地址、认证方式、超时和目标白名单       |
| `DB_QUERY` | 查询业务数据库          | 只读、表白名单、参数化查询和结果限制       |
| `MCP`      | 调用 MCP 服务提供的工具 | JSON-RPC 2.0、传输配置、目标限制和权限校验 |

**MD-Skill 不作为普通 Tool 类型处理。** 它是 Skill 的一种实现类型。MD-Skill 中的脚本如果需要执行，必须通过 SkillGateway 管理的受控执行路径运行，不能直接由 Agent 或 Skill 绕过网关启动脚本。

### Tool 版本原则

Tool 本身是资源，ToolVersion 是某一时刻的执行配置快照。

例如，某个 HTTP Tool 的第 1 版访问接口 A，第 2 版访问接口 B。历史 SkillVersion 引用第 1 版时，不应因为 Tool 发布了第 2 版而自动切换。

因此：

- ToolVersion 使用整数版本号。
- 同一 Tool 下版本号唯一且递增。
- 草稿版本允许修改。
- 发布版本不可修改。
- 历史 SkillVersion 保留原 ToolVersion 引用。
- 修改已发布 Tool 时，创建新的草稿版本。
- 官方 Tool 不允许普通用户直接修改或删除。

## 2.2 Skill：可复用的业务能力包

Skill 是对多个 Tool 以及执行规则的组织和封装。

它表达的是“能够完成什么类型的工作”，而不只是一个接口调用。

例如，一个业务信息分析 Skill 可以包含：

1. 查询客户基础资料的 Tool。
2. 查询业务记录的 Tool。
3. 汇总数据的处理步骤。
4. 输出结果的格式要求。
5. 执行这些动作所需的权限和确认规则。

Skill 可以被多个 Agent 复用，但每个已发布 SkillVersion 必须冻结自己依赖的 ToolVersion。

### Skill 与 Tool 的关系

- 一个 Skill 可以引用多个 ToolVersion。
- 一个 ToolVersion 可以被多个 SkillVersion 复用。
- Skill 不能直接访问底层 HTTP 客户端、数据库连接或 MCP 客户端来执行 Tool。
- 所有实际 Tool 调用都必须经过 SkillGateway。
- SkillVersion 必须声明自己的权限要求。
- ToolVersion 也可以声明自身权限要求。
- 所有必需权限默认按 AND 规则判断，即必须同时满足。

这里需要区分“复用关系”和“执行权限”：某个 Skill 引用了一个 Tool，不代表当前用户就一定有权调用该 Tool。

### Skill 的实现类型

社区版支持两种 Skill 实现方式。

| 实现类型      | 说明                       | 执行特点                                                 |
| ------------- | -------------------------- | -------------------------------------------------------- |
| `INSTRUCTION` | 纯指令型 Skill             | 提供规则、步骤、输入要求和输出要求，由后续模型执行和编排 |
| `MD_SKILL`    | 基于 Markdown 文件的 Skill | 通过 SKILL.md 描述能力，可使用纯指令模式或受控脚本模式   |

`MD_SKILL` 是 Skill 的实现类型，不是普通 Tool 类型。它可以通过 Skill 执行器加载指令；需要执行脚本时，则进入受控的脚本执行路径。

纯指令型 Skill 本身不应被误认为已经完成了所有业务操作。真实模型驱动的步骤规划与 Tool 调用循环由阶段 5 接入。

## 2.3 MD-Skill 能力范围

MD-Skill 是社区版的核心能力之一，需要在本阶段建立最小可用实现。

### 文件结构

建议采用以下组织方式：

```
md-skills/
├── customer-analysis/
│   ├── SKILL.md
│   ├── scripts/
│   └── examples/
├── report-summary/
│   └── SKILL.md
└── ...
```

实际目录必须沿用阶段 1 已确定的项目路径配置。如果已经存在 `md-skills` 目录，不要重复创建另一套目录。

### SKILL.md 最小内容

至少包括：

- 名称。
- 说明。
- 输入要求。
- 输出要求。
- 执行步骤。
- 可使用的 Tool 或能力引用。
- 所需权限。
- 是否允许脚本模式。
- 其他必要的运行约束。

建议使用 YAML Front Matter（文件头元数据）配合 Markdown 正文。

### 纯指令模式

纯指令模式下：

- 读取并校验 SKILL.md。
- 将内容作为 Skill 配置的一部分。
- 明确其允许使用的 Tool。
- 不直接执行任意代码。
- 后续由模型根据指令决定调用哪些 Tool。

### 脚本模式

脚本模式需要额外的安全控制：

- 只有明确允许脚本的 Skill 才能启用。
- 仅允许受支持的脚本语言和文件扩展名。
- 禁止通过路径穿越访问允许目录以外的文件。
- 限制 CPU、内存、执行时间和输出大小。
- 默认禁止网络访问。
- 不允许任意读取系统环境变量。
- 不允许将 Secret 明文传递给脚本。
- 不允许执行未经校验的外部命令。
- 脚本结果需要经过统一结果标准化。
- 涉及写操作时执行确认策略检查。

社区版可以先实现最小受控脚本执行器。如果当前运行环境无法提供可靠的操作系统级隔离，应限制脚本功能范围，而不是仅靠 Python 代码中的路径判断就宣称具备完整沙箱能力。

## 2.4 官方资源与用户资源

Tool 和 Skill 都需要区分官方资源与用户资源。

| 规则     | 官方资源           | 用户资源                 |
| -------- | ------------------ | ------------------------ |
| 普通查询 | 允许               | 按所有权与权限控制       |
| 直接编辑 | 不允许             | 允许编辑自己的草稿       |
| 复制     | 允许               | 可按业务规则复制         |
| 发布     | 由授权管理人员执行 | 由具备发布权限的用户执行 |
| 普通删除 | 不允许             | 软删除或归档             |
| 物理删除 | 不允许             | 遵循可配置清理策略       |

复制官方资源时必须：

- 创建新的资源 ID。
- 保留 `source_resource_id`。
- 保留 `source_version_id`。
- 创建独立的草稿版本。
- 不共享可变配置对象。
- 不因来源资源后续升级而自动改变用户副本。

## 2.5 SkillGateway：唯一执行入口

SkillGateway 是本阶段的核心运行组件。

所有 Tool 执行都必须经过它，包括：

- HTTP Tool。
- DB_QUERY Tool。
- MCP Tool。
- MD-Skill 脚本执行所需的受控执行路径。
- 后续模型 Function Calling 产生的工具请求。

### SkillGateway 统一执行流程

每次调用按以下顺序处理：

1. 接收调用请求。
2. 获取并验证 SecurityContext。
3. 校验 Task、AgentVersion、SkillVersion 和 ToolVersion 的关联关系。
4. 检查调用者是否有权使用对应 Skill 和 Tool。
5. 校验 Tool 输入参数。
6. 检查 Tool 与全局策略共同决定的确认要求。
7. 记录执行开始事件。
8. 根据 Tool 类型分发到对应执行器。
9. 执行超时、重试和取消控制。
10. 对执行结果进行标准化。
11. 记录执行结果与必要的审计信息。
12. 将结果返回给上层调用者。

如果任何前置校验失败，不能继续执行底层操作。

### 统一入口的意义

不能允许不同模块各自实现一套 HTTP 调用、数据库查询或 MCP 调用逻辑。

否则很容易出现：

- 某个调用没有做权限检查。
- 某个调用没有写审计记录。
- 某个调用没有超时控制。
- 某个调用绕过人工确认。
- 某个调用返回不同格式，导致模型无法理解结果。

统一执行入口是后续 Agent 执行能力可控、可审计的基础。

## 2.6 权限与人工确认

### 权限规则

Tool 和 Skill 都可以声明 `required_permissions`。

例如：

- `customer.read`
- `customer.update`
- `report.read`

多个必需权限默认采用 AND 规则。只有所有必需权限都满足，才允许执行。

权限检查至少包括：

- 当前用户身份有效。
- Task 所属用户和会话关系有效。
- AgentVersion 和 SkillVersion 关系有效。
- 当前 SkillVersion 确实引用目标 ToolVersion。
- 用户具有调用 Skill 所需权限。
- 用户具有调用 Tool 所需权限。
- 外部目标与当前配置允许的范围一致。

具体的统一 PEP（Policy Enforcement Point，策略执行点）与全链路权限闭环在阶段 8 完成；本阶段必须保留接口并实现基础检查，不能先让执行器无条件放行。

### 人工确认规则

是否需要确认，由 Tool 自身的确认配置和全局策略共同决定。

全局策略可以要求某类操作始终确认，即使 Tool 自身声明不需要确认。

本阶段只实现简单确认机制，不建设复杂审批工作流。

确认流程需要明确记录：

- 为什么需要确认。
- 当前待确认的动作。
- 发起用户。
- 确认人。
- 确认或拒绝结果。
- 对应的 Task 和 Skill/Tool 版本。

未获得必要确认之前，不允许执行受保护的操作。

## 2.7 执行失败与结果处理

Tool 失败不等于 Task 必须立即失败。

例如：

- HTTP 超时。
- MCP 服务暂时不可用。
- 数据库查询参数不合法。
- 外部服务返回业务错误。

这些情况应先转化为标准化 Tool Result，交给上层执行逻辑处理。

阶段 5 接入模型后，模型可以根据错误信息决定是否调整参数、尝试其他允许的 Tool 或向用户解释问题。

如果任务超过最大执行轮次、触发不可恢复错误或达到任务终止条件，才由任务执行逻辑决定 Task 的最终状态。

**Tool 执行失败时不能绕过 SkillGateway 直接修改 Task 状态。** Task 的状态变化仍由阶段 2 的任务内核负责。

# 三、技术方案

## 3.1 模块架构

继续采用模块化单体，不新增独立微服务。

建议的职责划分：

| 模块                 | 职责                                 |
| -------------------- | ------------------------------------ |
| Tool 模块            | Tool 资源管理、版本创建、发布与归档  |
| Skill 模块           | Skill 资源管理、版本创建、发布与归档 |
| SkillGateway         | 唯一执行入口、校验、策略与结果标准化 |
| HTTP Executor        | 执行受控 HTTP 请求                   |
| DB Query Executor    | 执行受限只读查询                     |
| MCP Executor         | 调用 MCP 工具                        |
| MD-Skill Executor    | 加载 Skill 指令及执行受控脚本        |
| Confirmation Service | 处理简单人工确认                     |
| Audit 模块           | 记录调用、确认、配置变更和执行结果   |
| Task Kernel          | 管理 Task 状态、事件和可靠执行       |

建议的代码组织如下：

```
app/
├── kernel/
│   ├── skill_gateway/
│   │   ├── gateway.py
│   │   ├── contracts.py
│   │   ├── result.py
│   │   └── policy.py
│   ├── skill/
│   │   ├── service.py
│   │   └── version_service.py
│   ├── tool/
│   │   ├── service.py
│   │   └── version_service.py
│   ├── task/
│   ├── identity/
│   ├── authz/
│   └── audit/
├── builtin_plugins/
│   ├── http_executor/
│   ├── db_query_executor/
│   ├── mcp_executor/
│   └── md_skill_executor/
└── modules/
    └── ...
```

这是职责示意图。实施时必须遵循阶段 1 的实际目录结构与内核依赖规则，不能为了匹配示例而破坏已经建立的工程结构。

尤其需要遵循内核契约：

- Kernel 不能反向依赖 `builtin_plugins`。
- 执行器通过约定的 Port 或注册机制提供能力。
- 所有执行器必须经过 SkillGateway 调用。
- 缺少必需能力时必须明确报错，不能静默绕过。
- 社区版只加载受控的内置执行器，不执行任意第三方插件代码。

## 3.2 数据库设计

沿用现有数据库技术栈：

- SQLAlchemy 2。
- Alembic。
- SQLite WAL 默认。
- PostgreSQL 兼容。
- UUIDv7。
- 所有业务表保留 `tenant_id`。

本阶段主要新增或扩展以下表：

| 表名                     | 职责                                                   |
| ------------------------ | ------------------------------------------------------ |
| `zr_tool`                | Tool 基本信息                                          |
| `zr_tool_version`        | Tool 配置快照与版本                                    |
| `zr_skill`               | Skill 基本信息                                         |
| `zr_skill_version`       | Skill 配置快照与版本                                   |
| `zr_skill_version_tool`  | SkillVersion 与 ToolVersion 的关联                     |
| `zr_agent_version_skill` | AgentVersion 与 SkillVersion 的关联，补齐阶段 3 的设计 |
| `zr_skill_execution_log` | Skill 执行记录索引与摘要                               |
| `zr_task_event`          | 保留 Task 的运行事件与 Tool 调用过程摘要               |
| `zr_audit_log`           | 记录关键执行和管理审计事件                             |

如果前序阶段已经创建部分表，应使用 Alembic 迁移扩展，不得重新创建同名表。

## 3.3 ToolVersion 数据模型

ToolVersion 至少需要以下信息：

- `id`
- `tenant_id`
- `tool_id`
- `version_number`
- `status`
- `tool_type`
- `input_schema`
- `output_schema`
- `config_snapshot`
- `required_permissions`
- `confirmation_required`
- `timeout_seconds`
- `retry_policy`
- `config_schema_version`
- `created_at`
- `updated_at`
- `published_at`

`config_snapshot` 保存该版本的执行配置，但敏感凭据只能保存 Secret URI 引用，不允许保存 API Key 明文。

例如，HTTP Tool 的配置可以保存：

- 目标服务配置引用。
- 请求方法。
- 允许的目标路径。
- 请求参数映射。
- 认证方式。
- 超时和重试策略。

真正的 Secret 内容由本地加密 Secret Store 管理，API 只返回 `secret_ref` 及是否已配置的状态。

版本约束：

- `UNIQUE(tool_id, version_number)`。
- 发布版本不可修改。
- SkillVersion 必须引用具体 ToolVersion。
- Tool 的当前发布版本变化不影响历史引用。

## 3.4 SkillVersion 数据模型

SkillVersion 至少需要：

- `id`
- `tenant_id`
- `skill_id`
- `version_number`
- `status`
- `implementation_type`
- `description`
- `instructions`
- `config_snapshot`
- `required_permissions`
- `confirmation_policy`
- `config_schema_version`
- `created_at`
- `updated_at`
- `published_at`

`implementation_type` 至少支持：

- `INSTRUCTION`
- `MD_SKILL`

SkillVersion 与 ToolVersion 通过关联表维护关系。

### `zr_skill_version_tool`

建议字段：

- `id`
- `tenant_id`
- `skill_version_id`
- `tool_version_id`
- `alias`
- `description_override`
- `created_at`

约束：

- 同一 SkillVersion 内不应重复绑定相同 ToolVersion。
- 被引用的 ToolVersion 必须存在。
- 发布 SkillVersion 时，必须校验引用的 ToolVersion 状态和可访问性。
- 已发布 SkillVersion 的关联关系不可修改。

如果 Skill 中需要使用同一 ToolVersion 的多个不同参数配置，应在关系层增加明确的配置标识或独立引用，不要依赖不稳定的列表顺序。

## 3.5 AgentVersion 与 SkillVersion 的关系

阶段 3 已经定义 `zr_agent_version_skill`，本阶段需要补齐实际的 SkillVersion 关联与校验。

每个 AgentVersion 应明确引用一组 SkillVersion，而不是引用 Skill 当前版本。

发布 AgentVersion 时必须验证：

- 每个 SkillVersion 存在。
- SkillVersion 已发布。
- SkillVersion 属于预期的 Skill。
- 其依赖的 ToolVersion 均有效。
- 所有资源的租户关系和权限符合规则。

版本关系应形成如下链路：

```
AgentVersion → SkillVersion → ToolVersion
```

这条关系链是历史可追溯性的核心。

例如：

- AgentVersion 3 使用 SkillVersion 2。
- SkillVersion 2 使用 ToolVersion 1。
- Tool 后来发布了 ToolVersion 2。

历史 AgentVersion 3 仍然使用 SkillVersion 2 和 ToolVersion 1，不得自动切换。

如果需要使用新版 Tool，应创建新的 Skill 草稿版本，更新引用后发布新 SkillVersion，再通过新的 AgentVersion 使用它。

## 3.6 SkillGateway 接口契约

建议统一定义 Tool 调用请求。

请求至少包括：

- `task_id`
- `agent_version_id`
- `skill_version_id`
- `tool_version_id`
- `tool_call_id`
- `input`
- `trace_id`
- `security_context`

其中，`security_context` 必须来自经过验证的身份上下文，不能信任模型生成的用户 ID、角色或权限。

### 执行器接口要求

所有执行器必须实现统一的输入输出约定：

- 接收已通过基础校验的执行请求。
- 只处理自己支持的 Tool 类型。
- 不自行跳过权限校验或确认要求。
- 不能绕过网关写入业务执行结果。
- 返回统一结构。
- 支持超时与取消信号。
- 不向调用方泄露凭据。
- 记录必要的执行摘要和错误信息。

网关负责执行顺序、策略和审计；执行器负责完成具体类型的操作。

## 3.7 统一 Tool Result

所有执行器必须返回统一结构，避免 HTTP、DB_QUERY、MCP 和脚本执行返回完全不同的格式。

建议结果结构：

```
{
  "success": true,
  "data": {},
  "error": null,
  "metadata": {
    "latency_ms": 120,
    "truncated": false,
    "tool_version_id": "..."
  }
}
```

失败时：

```
{
  "success": false,
  "data": null,
  "error": {
    "code": "AI_TOOL_TIMEOUT",
    "message": "工具执行超时"
  },
  "metadata": {
    "latency_ms": 30000,
    "truncated": false,
    "tool_version_id": "..."
  }
}
```

错误码应区分：

- 参数校验失败。
- 权限不足。
- 需要人工确认。
- 执行超时。
- 外部服务不可用。
- MCP 协议或调用失败。
- 数据库查询被拒绝。
- 脚本执行被拒绝。
- 执行结果格式不合法。

错误消息必须经过脱敏，不允许把 Secret、数据库连接串、内部文件路径或完整敏感请求内容返回给模型和普通用户。

## 3.8 执行记录与审计

Tool 调用过程需要同时满足两个目标：

1. 能够从 Task 事件中看清任务执行过程。
2. 能够通过审计记录追溯谁在什么时间调用了什么能力。

建议职责如下：

- `zr_task_event`：记录任务过程中的 Skill 调用、Tool 调用、开始、结束、失败和确认事件。
- `zr_skill_execution_log`：记录执行查询所需的结构化摘要和索引。
- `zr_audit_log`：记录安全相关操作、权限决策、敏感动作和配置变更。

不需要把所有内容重复写入三张表。

事件记录应包含：

- `task_id`
- `skill_version_id`
- `tool_version_id`
- `tool_call_id`
- `trace_id`
- 执行状态
- 耗时
- 错误码
- 脱敏后的参数摘要
- 脱敏后的结果摘要
- 创建时间

完整敏感参数和大体积原始结果不应无条件写入日志。

## 3.9 超时、重试与取消

执行器必须支持明确的超时控制。

### 超时

- ToolVersion 可以声明默认超时。
- 全局策略可以限制最大超时。
- 实际超时不能超过系统允许的上限。
- 超时后返回标准化错误。
- 超时不能绕过 Task 内核直接改变 Task 状态。

### 重试

重试策略必须明确配置，并且区分可重试与不可重试错误。

建议：

- 网络连接错误可以按策略重试。
- HTTP 429、部分 5xx 可以按策略重试。
- 参数校验失败不重试。
- 权限不足不重试。
- 需要确认时不重试。
- 写操作默认不自动重试，除非目标系统明确支持幂等机制。

### 取消

用户停止任务时，Task 内核发出取消信号。

SkillGateway 必须向当前执行器传递取消请求：

- 可以取消的操作应尽快停止。
- 不能立即取消的外部请求应保留取消中状态。
- 取消后到达的迟到结果不能覆盖已经终止的任务状态。
- 不允许因取消失败而绕过审计记录。

## 3.10 HTTP Executor

HTTP Tool 用于调用经过允许的业务 API。

配置至少需要：

- 目标 URL 或服务引用。
- HTTP 方法。
- 允许的路径。
- 请求头和参数映射。
- 认证类型。
- Secret URI。
- 超时时间。
- 响应大小限制。
- 是否需要确认。

认证方式支持：

- API Key / Bearer。
- Basic Auth。
- 显式配置的匿名访问。

匿名访问不能作为默认放行策略。管理员必须明确配置目标与访问方式，并受目标白名单和全局安全策略约束。

### 安全要求

- 防止 SSRF（服务端请求伪造）。
- 限制目标域名、IP、端口和路径。
- 阻止访问未经授权的内网地址、云元数据端点和本机敏感服务。
- 防止重定向绕过目标白名单。
- 不允许模型直接指定任意 URL。
- 限制响应体大小。
- 处理连接超时和读取超时。
- 不记录完整 Authorization Header。
- 不允许用户输入覆盖受保护的认证头。

## 3.11 DB Query Executor

DB_QUERY 仅支持只读数据库查询。

本阶段不允许通过该执行器执行 INSERT、UPDATE、DELETE、DDL 或任意存储过程。

需要实现：

- 数据源配置与授权。
- 表白名单。
- 只读数据库账号优先。
- 参数化查询。
- 查询超时。
- 最大返回行数。
- 查询结果大小限制。
- 敏感字段限制。
- 执行前权限校验。
- 执行结果脱敏。

不要仅通过字符串查找 `SELECT` 就认定 SQL 安全。应使用受限查询构造或可靠的 SQL 解析与验证策略，并在数据库层使用只读账号和权限约束作为第二道防线。

社区版应先支持明确受限的查询场景，不追求通用 SQL 执行器的无限制能力。

## 3.12 MCP Executor

MCP（Model Context Protocol，模型上下文协议）执行器负责调用 MCP 服务提供的工具。

社区版要求支持：

- JSON-RPC 2.0。
- `tools/list` 工具发现。
- `tools/call` 工具调用。
- stdio 传输。
- HTTP 传输。
- 工具输入参数校验。
- 统一 Tool Result。
- 连接健康检查。
- 失败后的受控恢复。

### stdio 传输

需要管理 MCP 子进程的生命周期：

- 启动。
- 初始化协议会话。
- 工具发现。
- 工具调用。
- 健康检查。
- 异常退出检测。
- 受控重启。
- 应用关闭时清理进程。

不能在每次 Tool 调用时无条件创建新进程，否则会增加开销，也容易造成资源泄漏。

### HTTP 传输

管理员必须明确配置：

- 目标地址。
- HTTPS 使用策略。
- 认证方式。
- 目标白名单。
- 超时。
- 允许调用的服务。

生产默认配置应拒绝不安全的传输与认证组合；如果确需使用不加密的 HTTP 或匿名访问，应由管理员显式配置，并遵循全局安全策略。

### MCP 凭据

- 敏感环境变量使用 Secret URI。
- 普通非敏感环境变量可以保存在配置中。
- 不向 API 返回 Secret 明文。
- 不在日志中记录敏感环境变量。
- MCP 返回的工具说明、参数和结果都应作为不可信数据处理。

### 工具发现与版本冻结

`tools/list` 的发现结果需要被转换为可管理的 Tool 定义。

工具发现结果发生变化时，不能静默覆盖已经发布的 ToolVersion。需要由管理员确认变化，并创建新的 Tool 草稿版本。

## 3.13 MD-Skill Executor

MD-Skill 执行器负责：

- 发现 Skill 文件。
- 解析元数据。
- 校验文件结构。
- 加载纯指令内容。
- 校验脚本模式是否允许。
- 将脚本执行请求转交给受控执行路径。
- 对执行结果进行标准化。

如果使用文件监听机制，文件变化可以触发重新扫描；但扫描到文件变化不代表已发布的 SkillVersion 可以被原地修改。

建议将文件系统中的内容视为候选配置：

```
文件变化 → 重新扫描 → 校验 → 创建草稿或提示更新 → 发布新版本
```

已发布版本应保存冻结快照，必要时保存内容摘要，以便发现文件内容与版本记录不一致的情况。

# 四、接口与数据设计

## 4.1 API 设计原则

统一使用 `/api/z-torque/*` 前缀，不强制在路径中增加 `/v1`。

所有接口必须：

- 使用统一响应格式。
- 使用统一错误码体系。
- 校验身份与资源权限。
- 对敏感字段执行脱敏。
- 对关键写操作记录审计。
- 对执行接口设置请求大小和超时限制。

以下是建议接口，需与前序阶段已实现的路由保持一致。

## 4.2 Tool API

| 方法  | 路径                                                         | 用途                   |
| ----- | ------------------------------------------------------------ | ---------------------- |
| GET   | `/api/z-torque/tools`                                        | 查询可访问的 Tool      |
| POST  | `/api/z-torque/tools`                                        | 创建 Tool              |
| GET   | `/api/z-torque/tools/{tool_id}`                              | 查询 Tool 详情         |
| PATCH | `/api/z-torque/tools/{tool_id}`                              | 修改允许编辑的基础信息 |
| POST  | `/api/z-torque/tools/{tool_id}/copy`                         | 复制 Tool              |
| POST  | `/api/z-torque/tools/{tool_id}/archive`                      | 归档 Tool              |
| GET   | `/api/z-torque/tools/{tool_id}/versions`                     | 查询版本列表           |
| POST  | `/api/z-torque/tools/{tool_id}/versions`                     | 创建 Tool 草稿版本     |
| PATCH | `/api/z-torque/tools/{tool_id}/versions/{version_id}`        | 修改 Tool 草稿         |
| POST  | `/api/z-torque/tools/{tool_id}/versions/{version_id}/publish` | 发布 ToolVersion       |

已发布版本不得通过 PATCH 修改。

## 4.3 Skill API

| 方法  | 路径                                                         | 用途                   |
| ----- | ------------------------------------------------------------ | ---------------------- |
| GET   | `/api/z-torque/skills`                                       | 查询可访问的 Skill     |
| POST  | `/api/z-torque/skills`                                       | 创建 Skill             |
| GET   | `/api/z-torque/skills/{skill_id}`                            | 查询 Skill 详情        |
| PATCH | `/api/z-torque/skills/{skill_id}`                            | 修改允许编辑的基础信息 |
| POST  | `/api/z-torque/skills/{skill_id}/copy`                       | 复制 Skill             |
| POST  | `/api/z-torque/skills/{skill_id}/archive`                    | 归档 Skill             |
| GET   | `/api/z-torque/skills/{skill_id}/versions`                   | 查询版本列表           |
| POST  | `/api/z-torque/skills/{skill_id}/versions`                   | 创建 Skill 草稿版本    |
| PATCH | `/api/z-torque/skills/{skill_id}/versions/{version_id}`      | 修改 Skill 草稿        |
| POST  | `/api/z-torque/skills/{skill_id}/versions/{version_id}/publish` | 发布 SkillVersion      |

SkillVersion 创建和发布时，应能够管理其 ToolVersion 引用。

建议使用独立的关联管理接口，或者在草稿版本的更新契约中定义明确的关联配置。无论采用哪种方式，都必须保证已发布版本不能改变引用关系。

## 4.4 SkillGateway 内部接口

SkillGateway 是内核执行能力，不需要将其内部调用接口全部开放给外部用户。

建议通过应用服务或内核服务调用，例如：

- `execute_tool`：执行指定 ToolVersion。
- `validate_skill_version`：校验 SkillVersion 及其依赖。
- `validate_tool_version`：校验 ToolVersion。
- `evaluate_confirmation`：评估是否需要确认。
- `normalize_result`：统一执行结果。
- `request_cancellation`：向执行器传递取消请求。

上述名称是职责约定，不要求机械照搬为函数名。

如果确实需要 HTTP 调试接口，应限制为具备相应权限的管理操作，并且仍然必须经过 SkillGateway，不能直接调用执行器。

## 4.5 统一执行请求

请求字段建议：

| 字段               | 含义                      |
| ------------------ | ------------------------- |
| `task_id`          | 所属 Task                 |
| `agent_version_id` | 任务使用的 AgentVersion   |
| `skill_version_id` | 本次调用所属 SkillVersion |
| `tool_version_id`  | 精确的 ToolVersion        |
| `tool_call_id`     | 当前调用的唯一关联标识    |
| `input`            | 工具输入参数              |
| `trace_id`         | 链路追踪标识              |

身份上下文由服务端注入，不允许请求体中的角色、用户 ID 或权限覆盖真实的 SecurityContext。

执行请求还需要通过关联校验，防止攻击者将其他 Task 的 ID 与无关 ToolVersion 拼接后发起调用。

## 4.6 数据库约束

必须建立以下约束或等效的可靠校验：

- `UNIQUE(tool_id, version_number)`。
- `UNIQUE(skill_id, version_number)`。
- `UNIQUE(skill_version_id, tool_version_id)`。
- `UNIQUE(agent_version_id, skill_version_id)`。
- SkillVersion 引用的 ToolVersion 必须存在。
- AgentVersion 引用的 SkillVersion 必须存在。
- 已发布版本的配置和关系不可修改。
- 官方资源不可被普通用户删除。
- 资源必须遵循租户隔离规则。
- Task 的执行引用必须保持与创建时快照一致。

需要特别注意：外键只能保证记录存在，不能单独保证被引用的版本已经发布，也不能保证调用者拥有权限。这些规则必须由业务服务共同完成。

## 4.7 错误码建议

| 错误码                      | 含义                   |
| --------------------------- | ---------------------- |
| `AI_SKILL_NOT_FOUND`        | Skill 不存在或不可访问 |
| `AI_SKILL_VERSION_INVALID`  | SkillVersion 不可用    |
| `AI_TOOL_NOT_FOUND`         | Tool 不存在或不可访问  |
| `AI_TOOL_VERSION_INVALID`   | ToolVersion 不可用     |
| `AI_TOOL_INPUT_INVALID`     | 输入参数不符合 Schema  |
| `AI_PERMISSION_DENIED`      | 权限不足               |
| `AI_CONFIRMATION_REQUIRED`  | 需要人工确认           |
| `AI_TOOL_TIMEOUT`           | Tool 执行超时          |
| `AI_TOOL_UNAVAILABLE`       | 执行器不可用           |
| `AI_MCP_CALL_FAILED`        | MCP 调用失败           |
| `AI_DB_QUERY_REJECTED`      | 查询不符合只读策略     |
| `AI_SKILL_EXECUTION_FAILED` | Skill 执行失败         |

错误响应不能暴露内部堆栈、数据库连接串、Secret 或敏感的外部服务返回内容。

# 五、任务排期

本阶段建议拆分为 7 个开发任务，按每天约 4—6 小时有效开发时间估算，整体约 7—10 个工作日。如果需要边学习边开发，可以适当延长。

| 编号  | 任务                         | 预计时间 | 交付物                               |
| ----- | ---------------------------- | -------- | ------------------------------------ |
| T4-01 | 数据模型与版本关系           | 1 天     | Tool、Skill、版本与关联表            |
| T4-02 | Tool/Skill 资源管理          | 1—2 天   | CRUD、复制、归档、草稿管理           |
| T4-03 | 发布与快照校验               | 1 天     | 版本冻结与引用完整性校验             |
| T4-04 | SkillGateway 核心            | 1—2 天   | 统一执行请求、校验与结果标准化       |
| T4-05 | HTTP 与 DB_QUERY 执行器      | 1—2 天   | 受控 HTTP 与只读查询                 |
| T4-06 | MCP 与 MD-Skill 最小执行能力 | 1—2 天   | MCP 基础调用、指令加载与受控脚本路径 |
| T4-07 | 安全、审计与集成测试         | 1 天     | 自动化测试与阶段验收                 |

## 5.1 T4-01：数据模型与版本关系

执行步骤：

1. 检查阶段 3 的 AgentVersion 与 Skill 关联结构。
2. 检查阶段 2 的 TaskEvent、Task 和审计表。
3. 定义 Tool、ToolVersion、Skill、SkillVersion 模型。
4. 定义 SkillVersion 与 ToolVersion 关联表。
5. 补齐 AgentVersion 与 SkillVersion 的关联。
6. 编写 Alembic 迁移。
7. 验证唯一约束和外键。

完成后，应先通过数据库测试，确认版本关系可正确保存，再进入资源管理开发。

## 5.2 T4-02：Tool 与 Skill 管理

依次实现：

1. Tool 基本信息管理。
2. Tool 草稿版本管理。
3. Skill 基本信息管理。
4. Skill 草稿版本管理。
5. 官方资源与用户资源的区分。
6. 官方资源复制机制。
7. 用户资源归档。
8. 基础权限与所有权检查。

本任务暂不要求真实执行 Tool。先确保资源管理和版本引用正确。

## 5.3 T4-03：发布机制与版本冻结

依次实现：

1. ToolVersion 发布前校验。
2. ToolVersion 发布与冻结。
3. SkillVersion 引用 ToolVersion。
4. SkillVersion 发布前校验。
5. SkillVersion 发布与冻结。
6. AgentVersion 引用 SkillVersion 的完整性校验。
7. 版本关系与审计事件写入。
8. 并发发布测试。

完成后应能够证明：更新 Tool 或 Skill 不会自动改变已发布的上层版本。

## 5.4 T4-04：实现 SkillGateway

先完成与具体执行器无关的网关核心：

1. 定义统一执行请求。
2. 获取 SecurityContext。
3. 校验 Task 与版本关系。
4. 校验 Tool 输入 Schema。
5. 检查权限。
6. 检查人工确认策略。
7. 记录开始事件。
8. 调用执行器注册接口。
9. 统一处理超时、取消和异常。
10. 标准化结果。
11. 写入执行事件和审计信息。

建议先用 Mock Executor 验证完整链路。等网关行为稳定后，再接入 HTTP、DB_QUERY、MCP 和 MD-Skill 执行能力。

## 5.5 T4-05：HTTP 与 DB_QUERY

HTTP 执行器建议从只读 GET 场景开始，随后逐步支持经过明确配置的其他方法。

DB_QUERY 执行器优先完成只读查询：

1. 数据源连接与 Secret URI。
2. 表白名单。
3. 参数化查询。
4. 只读权限。
5. 查询超时。
6. 行数和结果大小限制。
7. 敏感字段脱敏。
8. 统一结果格式。

不要在第一版中支持任意 SQL 或任意 HTTP 目标。

## 5.6 T4-06：MCP 与 MD-Skill

建议分两部分开发。

**MCP：**

1. 建立连接配置模型。
2. 支持 stdio 和 HTTP 传输。
3. 实现初始化与 `tools/list`。
4. 将发现结果转换为候选 Tool 配置。
5. 管理员确认后创建 ToolVersion。
6. 实现 `tools/call`。
7. 实现连接健康检查、异常恢复与关闭清理。

**MD-Skill：**

1. 扫描 Skill 文件。
2. 解析和校验 SKILL.md。
3. 支持纯指令模式。
4. 支持受控脚本模式的配置校验。
5. 实现脚本路径和资源限制。
6. 将结果返回统一执行入口。
7. 验证文件更新不会修改已发布版本。

如果时间有限，优先完成 MCP 的基本调用和 MD-Skill 纯指令加载，再完成脚本执行隔离。不要为了赶进度而取消脚本安全限制。

## 5.7 T4-07：集成验收

最后统一执行：

- 资源 CRUD 测试。
- 版本冻结测试。
- 版本引用测试。
- 网关权限测试。
- HTTP 执行器测试。
- DB_QUERY 只读测试。
- MCP 协议测试。
- MD-Skill 加载测试。
- 取消和超时测试。
- 审计和脱敏测试。
- 迁移测试。

所有执行器都必须通过 SkillGateway 的统一测试，不允许单独通过直接调用执行器来替代网关验收。

# 六、测试验收

## 6.1 Tool 与 Skill 资源管理

- 

  可以创建用户 Tool。

- 

  可以创建用户 Skill。

- 

  可以查询官方和有权访问的用户资源。

- 

  可以复制官方 Tool。

- 

  可以复制官方 Skill。

- 

  复制后保留来源资源 ID 和来源版本 ID。

- 

  修改用户副本不会影响官方资源。

- 

  普通用户不能直接修改官方资源。

- 

  已发布版本不能通过普通编辑接口修改。

- 

  归档不会破坏历史版本引用。

## 6.2 版本快照

- 

  ToolVersion 使用整数递增版本号。

- 

  SkillVersion 使用整数递增版本号。

- 

  同一资源下版本号唯一。

- 

  SkillVersion 能引用多个 ToolVersion。

- 

  AgentVersion 能引用多个 SkillVersion。

- 

  版本引用指向具体版本 ID。

- 

  Tool 发布新版本不会修改旧 SkillVersion。

- 

  Skill 发布新版本不会修改旧 AgentVersion。

- 

  发布失败不会留下半完成的当前版本指针。

- 

  无效或未发布的依赖版本不能被正式发布的上层配置引用。

- 

  版本发布事件能够被审计。

## 6.3 SkillGateway

- 

  所有 Tool 调用都经过 SkillGateway。

- 

  不存在业务模块绕过网关直接执行 Tool 的路径。

- 

  输入参数必须符合 Schema。

- 

  身份上下文来自服务端，而不是模型输入。

- 

  Skill 和 Tool 的权限要求均得到检查。

- 

  人工确认要求在执行前得到验证。

- 

  执行器异常能够转换为标准结果。

- 

  结果字段结构保持一致。

- 

  超时不会破坏 Task 状态机。

- 

  取消请求能够传递给执行器。

- 

  迟到结果不会覆盖终态 Task。

- 

  执行过程产生必要的 TaskEvent 和审计记录。

## 6.4 HTTP 执行器

- 

  可以调用允许的测试 API。

- 

  不允许访问未经授权的目标。

- 

  不允许通过重定向绕过目标限制。

- 

  不允许访问受保护的本机或云元数据地址。

- 

  认证信息使用 Secret URI。

- 

  日志和错误响应不包含凭据。

- 

  超时、异常和响应过大均有明确处理。

- 

  重试策略符合 Tool 配置与全局限制。

## 6.5 DB_QUERY 执行器

- 

  只允许只读查询。

- 

  不允许 INSERT、UPDATE、DELETE、DDL 等写操作。

- 

  不能查询白名单以外的表。

- 

  使用参数化查询。

- 

  具有查询超时。

- 

  具有最大行数和响应大小限制。

- 

  敏感字段按策略处理。

- 

  数据库层权限与应用层限制一致。

## 6.6 MCP 执行器

- 

  支持 JSON-RPC 2.0。

- 

  支持 `tools/list`。

- 

  支持 `tools/call`。

- 

  支持 stdio 传输。

- 

  支持 HTTP 传输。

- 

  stdio 子进程能够正常启动和关闭。

- 

  异常退出能够被检测。

- 

  重新连接不会无限创建子进程。

- 

  敏感环境变量使用 Secret URI。

- 

  工具发现变化不会覆盖已发布 ToolVersion。

- 

  MCP 错误能够转换为标准化结果。

- 

  HTTP 目标、认证和传输策略经过管理员配置。

## 6.7 MD-Skill

- 

  可以发现合法的 SKILL.md。

- 

  可以解析并校验文件元数据。

- 

  纯指令模式可以加载。

- 

  未授权的脚本模式不能执行。

- 

  路径穿越访问被拒绝。

- 

  超时和资源限制有效。

- 

  默认不允许脚本任意访问网络。

- 

  脚本不能直接读取 Secret 明文。

- 

  文件变化不会原地修改已发布版本。

- 

  脚本结果进入统一结果标准化流程。

## 6.8 阶段通过标准

满足以下条件，才算阶段 4 完成：

1. Tool、Skill 及其版本模型已经落地。
2. AgentVersion → SkillVersion → ToolVersion 的引用链正确。
3. SkillGateway 是所有 Tool 调用的唯一执行入口。
4. 至少一个真实执行器通过完整网关链路验收。
5. HTTP、DB_QUERY、MCP 的支持范围与安全限制有明确实现。
6. MD-Skill 纯指令模式可用，脚本模式具备相应的安全控制。
7. 执行结果统一，失败、超时和取消可追踪。
8. 关键执行操作有审计记录。
9. 核心自动化测试全部通过。
10. 不破坏阶段 2 的 Task 状态机、Outbox 和恢复机制。

# 七、风险项

## 7.1 直接执行器调用绕过 SkillGateway

**风险：** 部分业务模块自行调用 HTTP、数据库或 MCP，导致权限、审计、超时策略不一致。

**处理方式：**

- 统一执行服务入口。
- 限制执行器只通过注册接口暴露能力。
- 对执行器直接调用编写架构层测试。
- 后续模型和 Skill 执行代码只能调用网关。

## 7.2 版本关系使用当前版本而非固定版本

**风险：** Tool 或 Skill 更新后，历史 Agent 的行为发生变化。

**处理方式：**

所有版本关系都保存明确的版本 ID。发布新版本时创建新的快照，不修改旧版本的引用关系。

## 7.3 把 Skill 和 Tool 混为一谈

**风险：** Skill 同时承担业务描述、执行器、工具接口和版本管理，导致模块职责不清。

**处理方式：**

- Tool 负责最小执行动作。
- Skill 负责组织能力、指令和 Tool 引用。
- SkillGateway 负责统一执行控制。
- 执行器负责具体协议和底层操作。
- MD-Skill 作为 Skill 实现类型处理，而不是额外混入普通 Tool 类型。

## 7.4 HTTP SSRF 与敏感凭据泄露

**风险：** 模型构造恶意 URL，访问内部服务或通过日志泄露凭据。

**处理方式：**

- 目标白名单。
- DNS/IP 与重定向限制。
- 受保护目标拒绝访问。
- Secret URI。
- 请求头脱敏。
- 超时和响应体限制。
- 对匿名访问进行显式配置。

## 7.5 DB_QUERY 变成任意 SQL 执行器

**风险：** 用户或模型通过 SQL 执行写操作、访问未授权表或拖垮数据库。

**处理方式：**

- 只读账号。
- 表白名单。
- 参数化查询。
- 受限 SQL 解析或查询构造。
- 查询超时和结果限制。
- 敏感字段控制。

不能仅依靠字符串黑名单实现 SQL 安全。

## 7.6 MCP 子进程泄漏与连接失效

**风险：** MCP 子进程异常退出、反复创建、连接长期失效，造成资源耗尽。

**处理方式：**

- 管理子进程生命周期。
- 定期健康检查。
- 受控重启。
- 限制重启频率。
- 应用关闭时清理。
- 对异常状态生成可追踪事件。

## 7.7 MD-Skill 脚本安全不足

**风险：** 脚本访问任意文件、网络、系统环境或执行外部命令。

**处理方式：**

- 默认不启用任意脚本执行。
- 使用受控执行路径。
- 限制文件访问范围。
- 限制资源和运行时间。
- 默认禁止网络。
- Secret 不进入脚本环境。
- 无法提供可靠隔离时，缩小脚本能力范围。

## 7.8 过早实现复杂编排

**风险：** 在模型 Function Calling 尚未接入时，就尝试完成复杂的多轮 Agent 编排和多 Agent 协作。

**处理方式：**

阶段 4 只完成能力对象、版本和统一执行网关。多轮模型调用由阶段 5 完成，多 Agent 协作暂不实现。

# 八、上线交付

## 8.1 数据层交付

- 

  `zr_tool`

- 

  `zr_tool_version`

- 

  `zr_skill`

- 

  `zr_skill_version`

- 

  `zr_skill_version_tool`

- 

  完整的 `zr_agent_version_skill` 关系

- 

  `zr_skill_execution_log`

- 

  必要的 TaskEvent 和 Audit 结构扩展

- 

  Alembic 迁移文件

- 

  唯一约束、外键和索引

如果某些表已经存在，应提交迁移变更而不是重复建表。

## 8.2 业务层交付

- 

  Tool 管理服务。

- 

  ToolVersion 版本服务。

- 

  Skill 管理服务。

- 

  SkillVersion 版本服务。

- 

  版本引用完整性校验。

- 

  官方资源复制服务。

- 

  SkillGateway。

- 

  统一 Tool Result。

- 

  HTTP Executor。

- 

  DB Query Executor。

- 

  MCP Executor。

- 

  MD-Skill Executor。

- 

  简单人工确认机制。

- 

  超时、取消与错误处理。

- 

  执行事件和审计写入。

## 8.3 API 交付

- 

  Tool API。

- 

  ToolVersion API。

- 

  Skill API。

- 

  SkillVersion API。

- 

  版本发布与归档 API。

- 

  必要的执行调试接口或内部服务入口。

- 

  统一错误码。

- 

  身份与资源权限检查。

执行调试接口不应成为绕过 SkillGateway 的后门。

## 8.4 文档交付

建议维护以下文档：

- `docs/architecture/tool-and-skill.md`
- `docs/architecture/skill-gateway.md`
- `docs/architecture/version-binding.md`
- `docs/security/http-executor.md`
- `docs/security/db-query-executor.md`
- `docs/security/mcp-executor.md`
- `docs/security/md-skill-executor.md`
- `docs/testing/stage-4-acceptance.md`

如果项目已经存在同类文档，更新原有文档即可。

## 8.5 本地验收流程

建议按以下顺序进行：

1. 执行数据库迁移。
2. 启动 FastAPI 服务。
3. 创建一个测试 Tool。
4. 为 Tool 创建草稿版本并发布。
5. 创建一个测试 Skill。
6. 将指定 ToolVersion 绑定到 Skill 草稿版本。
7. 发布 SkillVersion。
8. 创建或更新 Agent 草稿版本。
9. 将指定 SkillVersion 绑定到 AgentVersion。
10. 发布 AgentVersion。
11. 通过 SkillGateway 调用测试 Tool。
12. 验证结果结构与错误处理。
13. 验证权限拒绝、超时、取消和确认机制。
14. 检查 TaskEvent、执行日志和审计记录。
15. 发布新版 Tool，确认历史 SkillVersion 不发生变化。

建议先使用本地 Mock 服务和测试数据库完成验证，不要一开始就连接生产业务系统。

# 九、阶段总结

## 9.1 本阶段建立的能力

阶段 4 将 Z-Torque 从“能够管理 Agent 与会话”推进到“能够定义并受控执行 AI 能力”。

本阶段建立了四个核心概念：

- **Tool**：最小执行动作。
- **Skill**：可复用的业务能力包。
- **SkillVersion**：冻结的能力配置与精确 ToolVersion 引用。
- **SkillGateway**：所有 Tool 调用必须经过的统一执行入口。

同时明确了版本链路：

```
AgentVersion → SkillVersion → ToolVersion
```

这条链路保证 Agent、Skill 和 Tool 后续不断演进时，历史任务仍然能够追溯当时使用的配置。

## 9.2 与前序阶段的衔接

- 阶段 2 提供 Task 状态机、Outbox、可靠执行和任务事件。
- 阶段 3 提供 AgentVersion、Conversation、Message 与 Task 关联。
- 阶段 4 提供 Skill、Tool、版本引用和统一执行网关。

本阶段不得重新实现 Task 状态机，也不得让执行器直接修改 Task 的终态。

## 9.3 下一阶段

阶段 5：**模型调用、Function Calling 与对话流**。

下一阶段将把模型调用正式接入当前体系：

1. 通过 ModelGateway 统一调用模型。
2. 从 AgentVersion 读取冻结的模型绑定和策略。
3. 向模型提供当前 AgentVersion 允许使用的 Skill。
4. 接收模型产生的 Tool 调用请求。
5. 验证调用是否属于当前 SkillVersion 和 ToolVersion。
6. 通过 SkillGateway 执行 Tool。
7. 将标准化结果返回模型。
8. 支持一项任务中的多轮模型与工具交互。
9. 限制最大调用轮次，避免无限循环。
10. 分离普通闲聊的 Chat SSE 与任务执行的 Task SSE。

**本阶段最重要的原则：所有能力可以被组合、复用和升级，但每一次实际执行都必须经过统一网关，并且绑定明确、可追溯的版本。**