# Z-Torque 社区版阶段 3：Agent、版本快照与会话消息

**文档编号：** ZT-COMMUNITY-P3  
**文档版本：** 1.1（P1/P2 优化）  
**阶段名称：** Agent、版本快照与会话消息  
**前置阶段：** 阶段 0、阶段 1、阶段 2  
**阶段状态：** 待实施  
**错误码：** 本阶段所有 API 引用阶段 0 主表。Agent、版本和会话资源使用对应领域错误码，版本未发布使用 `AI_AGENT_VERSION_NOT_PUBLISHED`，不得创建同义错误码。

## 父 Agent、默认 Agent 与委派快照（本阶段必须实现）

系统必须支持一个平台级已发布默认父 Agent。用户未在聊天界面选择 Agent 时，Conversation 使用该默认父 Agent；用户显式选择 Agent 时，仍按原有会话绑定规则执行，不自动替换为默认父 Agent。

### AgentVersion 委派配置快照字段表

| 字段 | 类型原则 | 说明 |
|:---|:---|:---|
| `delegation_enabled` | bool | 本版本是否允许委派子任务 |
| `allowed_child_agent_versions` | JSON 数组 | 显式白名单：[{agent_id, agent_version_id}, ...]，禁止“所有 Agent” |
| `child_necessity_policy` | string | 子任务必要性策略（如 required / optional / auto） |
| `delegation_capability_version` | string | 委派能力契约版本，便于后续演进 |

已发布版本不可原地修改；Task 创建时同时固定父 AgentVersion、每个子 AgentVersion 和授权关系快照。子 Agent 停用、归档或发布版本不可用时，已创建 Task 不得静默切换版本，应按阶段 8 的不可用策略失败、降级或终止。

父 Agent 的委派能力只影响执行计划，不改变用户身份。子 Agent 不得创建下一层子 Task，也不得直接向用户 Conversation 写入消息；子任务结果由父 Task 汇总后生成最终助手消息。

# 一、阶段目标

## 1.1 核心目标

本阶段完成四项核心能力：

1. **Agent 管理**：支持创建、查询、编辑、复制和归档 Agent。
2. **Agent 版本管理**：支持草稿、发布、归档以及不可变版本快照。
3. **Conversation 会话管理**：每个会话绑定一个 Agent，完整保存对话历史。
4. **Message 消息管理**：保存用户消息、助手消息及其与 Task 的关联关系，为后续流式聊天和任务执行提供数据基础。

本阶段完成后，系统应能够建立如下业务链路：

```
用户 → Agent → AgentVersion → Conversation → Message → Task
```

需要注意，这不是每次聊天都必须产生 Task 的意思。

普通闲聊只创建和保存消息，不创建 Task；当后续模型判断需要调用 Skill 或执行具体任务时，再创建 Task，并绑定当时有效的 AgentVersion。

## 1.2 本阶段的交付结果

完成本阶段后，系统应具备以下能力：

- 能够创建属于当前用户的 Agent。
- 能够复制官方 Agent，并保留来源信息。
- 能够创建、修改和发布 Agent 版本。
- 已发布版本不能被直接修改。
- 每个 Conversation 只能绑定一个 Agent。
- 用户能够查询完整历史消息。
- 每条用户消息最多关联一个 Task。
- 每个 Task 必须绑定创建时使用的确切 AgentVersion。
- 后续修改 Agent，不影响已创建任务使用的版本。
- 不同用户之间不能通过接口读取或修改彼此的私有 Agent、Conversation 和 Message。
- 能够通过自动化测试验证版本冻结、会话隔离和消息关联规则。

## 1.3 本阶段暂不实现

以下能力不属于本阶段：

- 真实大模型调用。
- Function Calling（函数调用）。
- Skill 和 Tool 的实际执行。
- SSE（服务器发送事件）聊天流式输出。
- 短期记忆自动总结。
- 完整的前端控制台。
- 多 Agent 协作。
- 复杂审批流程。

这些能力会在后续阶段逐步接入。本阶段可以使用 Mock（模拟响应）验证基础消息流程，但必须明确标识模拟结果，不能伪装成真实模型输出。

# 二、需求范围

## 2.1 Agent 资源管理

Agent 是面向某种业务目标的智能执行角色，定义其行为规则、模型选择策略、可用 Skill 以及相关执行策略。

Agent 本身是一个可管理的业务对象，AgentVersion 才是某次任务真正使用的配置快照。

### Agent 需要支持的操作

| 操作     | 普通用户自己的 Agent   | 官方 Agent                   |
| -------- | ---------------------- | ---------------------------- |
| 查询详情 | 允许                   | 允许                         |
| 创建     | 允许                   | 由具备相应权限的管理人员创建 |
| 编辑     | 允许编辑自己的草稿配置 | 不允许直接编辑               |
| 发布     | 根据权限允许           | 由授权管理人员发布           |
| 复制     | 允许                   | 允许                         |
| 归档     | 根据权限允许           | 不允许直接删除官方资源       |
| 物理删除 | 受清理策略限制         | 不允许                       |

官方资源是平台提供的公共模板。用户想修改官方 Agent 时，必须先复制为自己的 Agent。

复制之后：

- 原官方 Agent 不发生变化。
- 新 Agent 有自己的资源 ID。
- 新 Agent 记录来源 Agent ID 和来源版本 ID。
- 新 Agent 后续独立演进。
- 来源版本必须保留可追溯信息。

### Agent 的主要属性

- `id`：Agent 唯一标识。
- `tenant_id`：所属租户标识，社区版单租户运行，但仍保留此字段。
- `owner_user_id`：资源所有者；官方资源可以为空。
- `name`：Agent 名称。
- `description`：Agent 用途说明。
- `source_type`：官方资源或用户资源。
- `source_agent_id`：复制来源 Agent，可为空。
- `source_agent_version_id`：复制来源版本，可为空。
- `current_published_version_id`：当前发布版本。
- `status`：资源当前状态。
- `created_at`、`updated_at`：创建和更新时间。

资源来源类型建议使用明确的枚举值，例如 `OFFICIAL`、`USER`。

不要只依靠 `owner_user_id` 是否为空来判断资源类型，因为资源来源、所有权和发布权限是不同概念。

## 2.2 Agent 版本管理

AgentVersion 是本阶段最重要的设计对象。

每次发布都必须形成一个完整、可追溯、不可变的配置快照。不能仅保存“当前 Agent 配置”，否则后续修改会影响历史任务的解释与复现。

### 版本状态

| 状态        | 含义         | 是否允许修改配置 |
| ----------- | ------------ | ---------------- |
| `DRAFT`     | 草稿版本     | 允许             |
| `PUBLISHED` | 已发布版本   | 不允许           |
| `ARCHIVED`  | 历史归档版本 | 不允许           |

### 版本规则

1. 每个 Agent 的版本号从 1 开始递增。
2. 版本号使用整数，例如 `1`、`2`、`3`。
3. 同一 Agent 下版本号唯一。
4. 草稿版本允许修改。
5. 发布前必须验证配置完整性。
6. 发布成功后，版本配置永久冻结。
7. 再次修改已发布 Agent 时，必须创建新的草稿版本。
8. 发布新版本时，更新 Agent 的 `current_published_version_id`。
9. 旧版本不因为新版本发布而自动变成可修改状态。
10. 历史 Task 必须继续引用原来的 AgentVersion。
11. 归档不能破坏历史 Task 对版本的引用。
12. 版本状态变化必须留下审计记录。

**重要约束：不能在已发布版本上直接修改 JSON 配置，也不能通过数据库更新绕过版本冻结规则。**

发布流程建议如下：

```
创建草稿 → 编辑配置 → 校验配置 → 发布 → 形成不可变快照
```

修改已发布配置时：

```
复制当前配置到新草稿 → 修改 → 校验 → 发布新版本
```

## 2.3 AgentVersion 快照内容

AgentVersion 必须保存足够的信息，使系统能够准确知道该版本当时的行为配置。

本阶段先定义数据结构，实际关联 SkillVersion 在阶段 4 实现，模型调用策略在阶段 5 实现。

快照至少包含：

- Agent 基本配置。
- System Prompt（系统提示词）。
- Agent 行为规则。
- 模型绑定策略。
- 模型调用参数与策略。
- 精确的 SkillVersion 引用列表。
- 相关执行策略。
- 发布时的配置摘要或校验信息。
- 配置结构版本。
- 快照创建时间。

建议将数据分成两部分：

**关系表：**

用于保存明确的资源关系、外键约束和查询条件。

**JSON 快照：**

用于冻结完整配置、支持调试、导出、恢复以及历史追溯。

两者不是互相替代的关系。

例如，AgentVersion 的 Skill 关联表保存具体的 `skill_version_id`，快照 JSON 同时保存发布时使用的 Skill 配置引用。阶段 4 完成 SkillVersion 后，再补齐实际外键约束和发布校验。

模型自身不需要 Z-Torque 版本号。AgentVersion 只需要冻结当时的模型绑定、选择策略及相关参数；后续模型管理模块提供具体模型和端点信息。

## 2.4 Conversation 会话管理

Conversation 表示用户与某个 Agent 之间的一段独立对话。

必须遵循以下规则：

1. 一个 Conversation 只绑定一个 Agent。
2. Conversation 创建后，不允许直接切换 Agent。
3. 用户切换 Agent 时，创建新的 Conversation。
4. Conversation 保存完整历史消息。
5. Conversation 必须关联创建者。
6. 私有 Conversation 只能被具备相应权限的用户访问。
7. Conversation 归档后，历史记录仍然可查询。
8. 不允许通过传入其他用户的 Conversation ID 绕过所有权检查。

### 会话字段

建议至少包含：

- `id`
- `tenant_id`
- `owner_user_id`
- `agent_id`
- `title`
- `status`
- `last_message_at`
- `created_at`
- `updated_at`
- `archived_at`

会话标题可以先由用户设置，或者根据第一条用户消息生成一个简短标题。真实的大模型标题总结不属于本阶段。

## 2.5 Message 消息管理

Message 是用户与助手交互的基本单位。

即使一次普通聊天不需要创建 Task，也必须保存 Message。

消息角色至少包括：

- `USER`：用户消息。
- `ASSISTANT`：助手消息。
- `SYSTEM`：系统消息，仅在业务需要时使用。
- `TOOL`：工具结果消息，可为后续阶段预留；实际执行链路和结果关联在阶段 5 实现。

消息状态建议支持：

- `COMPLETED`：消息处理完成。
- `PROCESSING`：正在处理。
- `FAILED`：消息处理失败。
- `CANCELLED`：消息处理取消。

本阶段可以先实现完整消息记录和 Mock 处理流程，不必立即实现所有流式状态转换。

### Message 主要字段

- `id`
- `tenant_id`
- `conversation_id`
- `sender_type`
- `content`
- `content_parts`
- `status`
- `task_id`
- `error_code`
- `error_message`
- `created_at`
- `updated_at`

其中：

- `content` 用于保存纯文本消息。
- `content_parts` 用于后续扩展文本、文件、图片、音频等多模态内容。
- `task_id` 允许为空。
- `task_id` 非空时，表示这条消息已经触发或关联一个 Task。

附件的实际文件存储和权限校验在阶段 9 实现；本阶段只需避免把数据模型设计成只能存储纯文本。

## 2.6 Message 与 Task 的关联规则

必须明确区分两类数据：

**Message：用户看到的交互记录。**

保存用户说了什么、助手回复了什么、消息处理状态如何。

**Task：系统内部的执行记录。**

保存任务状态、执行事件、运行结果、取消状态以及相关审计信息。

两者不能混为一张表。

核心约束：

1. 一条用户 Message 最多关联一个 Task。
2. 一条 Message 可以完全不关联 Task。
3. 一个 Task 只属于一个 Conversation。
4. 一个 Task 必须引用确切的 AgentVersion。
5. Task 的最终执行结果保存在 Task 运行结果字段中。
6. 用户最终看到的回复保存为 Assistant Message。
7. Task 与 Assistant Message 通过明确的关联关系连接。
8. Task 执行期间产生的过程信息进入 TaskEvent，而不是无限制地堆积到 Message 内容中。

建议在 Task 上保留 `trigger_message_id` 和 `result_message_id`。

- `trigger_message_id`：触发任务的用户消息。
- `result_message_id`：最终结果对应的助手消息，可为空。

阶段 2 已经建立了 Task 内核，本阶段需要补齐这些关联字段、外键和校验逻辑。

## 2.7 闲聊与任务的区别

这是后续 Chat API 的核心规则，本阶段必须在业务层提前明确。

### 普通闲聊

用户发送消息后：

1. 校验用户身份与 Conversation 权限。
2. 保存用户 Message。
3. 通过 Mock 聊天处理器生成测试回复，或明确返回待接入模型的状态。
4. 保存 Assistant Message。
5. 不创建 Task。

### 需要执行 Skill 的请求

当后续模型判断需要执行具体操作时：

1. 保存用户 Message。
2. 组装 Agent 配置和必要对话上下文。
3. 确认需要进入任务执行链路。
4. 创建 Task，并绑定当前有效的已发布 AgentVersion。
5. 将 `trigger_message_id` 写入 Task。
6. 将 `task_id` 写入对应用户 Message。
7. 后续执行过程由任务内核负责。
8. 最终回复写入 Assistant Message，并将其 ID 关联到 Task。

**不要在阶段 3 为了提前跑通流程，就规定所有用户消息都必须创建 Task。**

实际模型判断与 Function Calling 在阶段 5 实现。本阶段只建立能够正确支持这种业务规则的数据模型与接口契约。

# 三、技术方案

## 3.1 总体架构

继续使用模块化单体架构，不新增独立微服务。

建议的职责划分：

| 模块                     | 主要职责                                   |
| ------------------------ | ------------------------------------------ |
| Agent 模块               | Agent 创建、查询、复制、编辑和归档         |
| AgentVersion 模块        | 版本创建、配置校验、发布和冻结             |
| Conversation 模块        | 会话创建、查询、归档和访问控制             |
| Message 模块             | 消息持久化、查询和关联                     |
| Chat Application Service | 协调用户消息、Mock 回复和后续 Task 创建    |
| Task Kernel              | 提供统一任务创建能力，不重复实现任务状态机 |
| Audit 模块               | 记录关键管理操作与版本发布事件             |

建议代码结构：

```
app/
├── kernel/
│   ├── agent/
│   │   ├── service.py
│   │   ├── version_service.py
│   │   └── schemas.py
│   ├── task/
│   ├── identity/
│   ├── audit/
│   └── authz/
├── modules/
│   └── conversation/
│       ├── service.py
│       ├── message_service.py
│       ├── chat_service.py
│       └── schemas.py
├── api/
│   └── ...
└── models/
    └── ...
```

以上仅为职责组织建议。实施时应优先沿用阶段 1 已确定的目录结构，避免为了符合示例而重命名已有模块。

## 3.2 数据库技术原则

沿用项目已经确定的技术栈：

- Python 3.12。
- FastAPI。
- Pydantic v2。
- SQLAlchemy 2。
- Alembic。
- SQLite WAL 默认，保留 PostgreSQL 兼容能力。
- UUIDv7 作为业务对象 ID。
- 所有业务表保留 `tenant_id`。

所有可变业务对象包含 `created_at`、`updated_at`。

对于已经创建的 AgentVersion 快照、TaskEvent 和审计事件，不应因为修改其他对象而更新其历史记录。不可变记录只需要在创建时写入 `created_at`。

数据库迁移必须通过 Alembic 管理，禁止在运行过程中依靠临时 SQL 自动补字段。

## 3.3 核心数据表

本阶段至少涉及以下表：

| 表名                     | 职责                                                         |
| ------------------------ | ------------------------------------------------------------ |
| `zr_agent`               | Agent 基本信息、所有权与当前发布版本                         |
| `zr_agent_version`       | Agent 的版本及不可变配置快照                                 |
| `zr_agent_version_skill` | AgentVersion 与 SkillVersion 的结构化关联，阶段 4 补齐完整约束 |
| `zr_conversation`        | 会话基本信息                                                 |
| `zr_message`             | 用户与助手消息                                               |
| `zr_task`                | 扩展阶段 2 的 Task 关联字段                                  |
| `zr_task_event`          | 保留阶段 2 的任务事件记录                                    |
| `zr_audit_log`           | 记录版本发布、复制和关键管理操作                             |

如果阶段 1 已经创建其中部分表，应使用迁移扩展，不要重新创建重复表。

## 3.4 AgentVersion 发布机制

发布不能只是把状态字段从 `DRAFT` 改成 `PUBLISHED`。

发布流程应包含：

1. 校验 Agent 存在且当前用户具有发布权限。
2. 校验草稿版本属于该 Agent。
3. 校验版本状态仍为 `DRAFT`。
4. 校验必需配置字段完整。
5. 校验 JSON 快照格式及配置结构版本。
6. 预留 SkillVersion 存在性、状态和权限校验接口；阶段 4 实现完整检查。
7. 在数据库事务中冻结草稿版本。
8. 更新 Agent 当前发布版本引用。
9. 写入审计事件。
10. 返回发布后的版本信息。

同一 Agent 并发发布时，必须防止两个发布请求互相覆盖。应使用数据库事务和适当的乐观锁或条件更新机制。

发布操作失败时，不得出现“版本已发布，但 Agent 当前发布版本未更新”之类的不一致状态。

建议通过唯一约束、事务和状态条件共同保证数据正确性，而不是只依赖 Python 代码中的 `if` 判断。

## 3.5 版本快照与历史任务

每次创建 Task 时，必须明确传入 `agent_version_id`。

Task 创建服务需要验证：

- AgentVersion 存在。
- AgentVersion 属于对应 Agent。
- AgentVersion 已发布。
- 当前用户可以访问对应 Agent。
- Conversation 绑定的 Agent 与任务使用的 Agent 一致。
- 触发消息属于该 Conversation。
- 该触发消息尚未关联其他 Task。

上述验证应在创建 Task 的业务事务中完成，并与阶段 2 的 Task 创建、TaskEvent、Outbox 原子性规则兼容。

不能只在前端选择一个 AgentVersion ID 后直接信任该值。

对于已经创建的 Task，即使 Agent 发布了新版本，Task 仍然使用原来的 `agent_version_id`。后续阶段执行模型调用和 Skill 时，应读取该版本的冻结配置，而不是重新读取 Agent 当前配置。

## 3.6 消息保存与事务边界

消息处理需要区分“消息已经保存”和“任务已经创建”。

### 普通消息

保存用户消息与助手回复时，应避免因为回复生成失败而丢失已经收到的用户消息。

### 触发任务的消息

用户消息、Task 创建以及关联字段的更新需要明确事务边界。

建议在同一数据库事务中完成：

- 确认用户消息属于当前会话。
- 确认用户消息尚未关联 Task。
- 创建 Task。
- 创建初始 TaskEvent。
- 创建 Outbox 记录。
- 更新用户 Message 的 `task_id`。

这些操作必须复用阶段 2 的 Task 创建服务和 Outbox 机制，不能在 Chat 模块里另写一套任务入队逻辑。

如果阶段 2 的任务创建接口暂时无法在同一事务中扩展，应先调整服务层边界，再实现 Chat 关联，避免留下无法恢复的半关联数据。

## 3.7 会话上下文规则

数据库保存完整历史记录，但并不意味着每次模型调用都要将所有历史消息发送给模型。

本阶段定义默认上下文规则：

- Agent 系统提示词和行为规则。
- AgentVersion 冻结的配置。
- 短期记忆摘要；记忆模块未完成时允许为空。
- 最近 5 轮对话。
- 当前用户消息。
- 后续阶段允许使用的 Skill 定义。

较早的历史消息只有在后续检索逻辑、权限校验及上下文长度限制允许时才补充。

阶段 3 只负责确保完整历史可查询、上下文构建接口清晰，不负责实现短期记忆总结和自动检索。

## 3.8 权限与隔离

即使社区版只有一个租户，也必须在查询层保留租户隔离和用户所有权校验。

所有私有资源的查询都需要验证：

- 当前 `tenant_id`。
- 当前用户身份。
- 资源的所有权或明确授权关系。
- 当前操作所需的资源权限。

不能先根据 ID 查询任意资源，再仅在返回前隐藏部分字段。

官方 Agent 可供多个用户读取，但修改和发布操作必须要求对应管理权限。

Agent 复制操作必须保留来源记录，同时创建新的资源 ID 和独立的配置副本。

本阶段实现基本隔离与接口权限校验；阶段 8 进一步统一权限决策点、完整审计和运行时权限闭环。

# 四、接口与数据设计

## 4.1 API 设计规范

统一使用以下 API 前缀：

```
/api/z-torque/*
```

不强制在路径中增加 `/v1`。API 版本管理通过接口契约、兼容策略和变更规范实施。

统一要求：

- 需要登录的接口必须完成身份验证。
- 根据操作类型检查资源权限。
- 请求和响应使用 Pydantic Schema。
- 返回结构遵循项目统一响应规范。
- 错误码使用 `AI_` 前缀。
- 记录必要的 `trace_id`。
- 不返回 Secret 明文或敏感配置。
- 重要写操作记录审计信息。

以下接口是建议的业务契约。实施时应与阶段 1 已建立的路由和响应格式保持一致。

## 4.2 Agent API

| 方法  | 路径                                       | 用途                       |
| ----- | ------------------------------------------ | -------------------------- |
| GET   | `/api/z-torque/agents`                     | 查询当前用户可访问的 Agent |
| POST  | `/api/z-torque/agents`                     | 创建用户自己的 Agent       |
| GET   | `/api/z-torque/agents/{agent_id}`          | 获取 Agent 详情            |
| PATCH | `/api/z-torque/agents/{agent_id}`          | 修改允许编辑的 Agent 信息  |
| POST  | `/api/z-torque/agents/{agent_id}/copy`     | 复制 Agent                 |
| POST  | `/api/z-torque/agents/{agent_id}/archive`  | 归档 Agent                 |
| GET   | `/api/z-torque/agents/{agent_id}/versions` | 查询版本列表               |

Agent 查询接口应支持必要的分页和筛选，例如名称、资源来源和状态。

不能因为用户知道官方 Agent 的 ID，就允许其直接修改官方资源。

## 4.3 AgentVersion API

| 方法  | 路径                                                         | 用途         |
| ----- | ------------------------------------------------------------ | ------------ |
| POST  | `/api/z-torque/agents/{agent_id}/versions`                   | 创建草稿版本 |
| GET   | `/api/z-torque/agents/{agent_id}/versions/{version_id}`      | 获取版本详情 |
| PATCH | `/api/z-torque/agents/{agent_id}/versions/{version_id}`      | 修改草稿配置 |
| POST  | `/api/z-torque/agents/{agent_id}/versions/{version_id}/publish` | 发布版本     |
| POST  | `/api/z-torque/agents/{agent_id}/versions/{version_id}/archive` | 归档历史版本 |

注意：这里的 `version_id` 是版本记录的 UUIDv7 ID，不是整数版本号。

整数版本号单独使用 `version_number` 表示。

对于已发布或已归档版本的修改请求，应返回明确的业务错误，而不是静默忽略。

## 4.4 Conversation API

| 方法  | 路径                                                     | 用途                         |
| ----- | -------------------------------------------------------- | ---------------------------- |
| POST  | `/api/z-torque/conversations`                            | 创建会话                     |
| GET   | `/api/z-torque/conversations`                            | 查询当前用户的会话列表       |
| GET   | `/api/z-torque/conversations/{conversation_id}`          | 查询会话详情                 |
| PATCH | `/api/z-torque/conversations/{conversation_id}`          | 修改会话标题等允许修改的字段 |
| POST  | `/api/z-torque/conversations/{conversation_id}/archive`  | 归档会话                     |
| GET   | `/api/z-torque/conversations/{conversation_id}/messages` | 分页查询历史消息             |

创建会话时必须指定 Agent。系统需要验证当前用户是否有权使用该 Agent。

如果未来的 Chat API 没有收到 `conversation_id`，可以自动创建会话；但 Conversation 仍然必须绑定明确的 Agent。

## 4.5 Message API

本阶段提供基础消息查询和测试流程所需的接口。

| 方法 | 路径                                                     | 用途             |
| ---- | -------------------------------------------------------- | ---------------- |
| GET  | `/api/z-torque/messages/{message_id}`                    | 查询消息详情     |
| GET  | `/api/z-torque/conversations/{conversation_id}/messages` | 查询会话消息列表 |

真实聊天入口在阶段 5 实现，建议最终使用统一的 Chat API 接收用户消息，而不是让前端分别调用多个接口手动创建用户消息、助手消息和 Task。

聊天接口的预期契约为：

- 输入：`agent_id`、可选的 `conversation_id`、用户消息内容。
- 幂等：支持 `Idempotency-Key`，防止网络重试导致重复消息或重复任务。
- 输出：消息 ID、会话 ID、响应类型，以及需要执行 Task 时返回的 `task_id`。
- 普通闲聊：不创建 Task。
- 需要工具执行：创建 Task，并返回任务标识。
- 后续流式响应：由阶段 5 接入 Chat SSE 和 Task SSE。

## 4.6 核心数据结构

### `zr_agent`

| 字段                           | 类型建议   | 说明            |
| ------------------------------ | ---------- | --------------- |
| `id`                           | UUID       | 主键            |
| `tenant_id`                    | UUID       | 租户标识        |
| `owner_user_id`                | UUID，可空 | 用户资源所有者  |
| `name`                         | 字符串     | Agent 名称      |
| `description`                  | 文本       | 用途说明        |
| `source_type`                  | 枚举       | OFFICIAL / USER |
| `source_agent_id`              | UUID，可空 | 复制来源        |
| `source_agent_version_id`      | UUID，可空 | 复制来源版本    |
| `current_published_version_id` | UUID，可空 | 当前发布版本    |
| `status`                       | 枚举       | 资源状态        |
| `created_at`                   | 时间       | 创建时间        |
| `updated_at`                   | 时间       | 更新时间        |
| `deleted_at`                   | 时间，可空 | 软删除时间      |

`current_published_version_id` 与 AgentVersion 存在关联，但 Agent 和 AgentVersion 之间会形成双向引用。数据库建表时应合理安排外键创建顺序，必要时通过后续迁移补充外键约束。

### `zr_agent_version`

| 字段                    | 类型建议     | 说明                         |
| ----------------------- | ------------ | ---------------------------- |
| `id`                    | UUID         | 版本记录 ID                  |
| `tenant_id`             | UUID         | 租户标识                     |
| `agent_id`              | UUID         | 所属 Agent                   |
| `version_number`        | 整数         | 同一 Agent 内递增            |
| `status`                | 枚举         | DRAFT / PUBLISHED / ARCHIVED |
| `config_schema_version` | 字符串或整数 | 配置结构版本                 |
| `config_snapshot`       | JSON         | 完整冻结配置                 |
| `snapshot_hash`         | 字符串，可空 | 快照校验值                   |
| `published_at`          | 时间，可空   | 发布时间                     |
| `created_at`            | 时间         | 创建时间                     |
| `updated_at`            | 时间         | 仅草稿阶段允许更新           |

必须建立唯一约束：

```
UNIQUE(agent_id, version_number)
```

快照发布后，禁止通过普通更新接口修改 `config_snapshot`、`version_number` 或其他会影响版本语义的字段。

### `zr_agent_version_skill`

| 字段               | 类型建议 | 说明                 |
| ------------------ | -------- | -------------------- |
| `id`               | UUID     | 主键                 |
| `tenant_id`        | UUID     | 租户标识             |
| `agent_version_id` | UUID     | AgentVersion ID      |
| `skill_version_id` | UUID     | 精确 SkillVersion ID |
| `created_at`       | 时间     | 创建时间             |

建议建立：

```
UNIQUE(agent_version_id, skill_version_id)
```

阶段 4 实现 SkillVersion 后，应补齐外键和完整性校验。不得在这里仅保存 Skill 名称或当前 Skill ID，否则 Skill 后续更新会改变历史 Agent 的实际能力定义。

### `zr_conversation`

| 字段              | 类型建议   | 说明              |
| ----------------- | ---------- | ----------------- |
| `id`              | UUID       | 主键              |
| `tenant_id`       | UUID       | 租户标识          |
| `owner_user_id`   | UUID       | 创建者            |
| `agent_id`        | UUID       | 绑定 Agent        |
| `title`           | 字符串     | 会话标题          |
| `status`          | 枚举       | ACTIVE / ARCHIVED |
| `last_message_at` | 时间，可空 | 最近消息时间      |
| `created_at`      | 时间       | 创建时间          |
| `updated_at`      | 时间       | 更新时间          |
| `deleted_at`      | 时间，可空 | 软删除时间        |

### `zr_message`

| 字段              | 类型建议     | 说明                             |
| ----------------- | ------------ | -------------------------------- |
| `id`              | UUID         | 主键                             |
| `tenant_id`       | UUID         | 租户标识                         |
| `conversation_id` | UUID         | 所属会话                         |
| `sender_type`     | 枚举         | USER / ASSISTANT / SYSTEM / TOOL |
| `content`         | 文本         | 文本内容                         |
| `content_parts`   | JSON         | 多模态扩展内容                   |
| `status`          | 枚举         | 消息处理状态                     |
| `task_id`         | UUID，可空   | 关联 Task                        |
| `error_code`      | 字符串，可空 | 错误码                           |
| `error_message`   | 文本，可空   | 安全处理后的错误信息             |
| `created_at`      | 时间         | 创建时间                         |
| `updated_at`      | 时间         | 更新时间                         |
| `deleted_at`      | 时间，可空   | 软删除时间                       |

需要通过服务层和数据库约束，尽可能保证用户消息的 Task 关联唯一性。若通过独立关联表或额外字段实现，还要确保不能将一条消息错误关联到多个任务。

### `zr_task` 扩展字段

阶段 2 已有 Task 表，本阶段只扩展，不重建。

| 字段                 | 说明                          |
| -------------------- | ----------------------------- |
| `conversation_id`    | 所属 Conversation             |
| `trigger_message_id` | 触发 Task 的用户消息          |
| `result_message_id`  | 最终结果对应的助手消息        |
| `agent_id`           | 对应 Agent                    |
| `agent_version_id`   | 创建时绑定的精确 AgentVersion |

如果阶段 2 已经包含其中部分字段，应优先复用已有字段并通过迁移补齐缺失字段。

## 4.7 软删除和数据保留

核心对象采用软删除策略。

默认清理周期为 90 天，必须支持配置调整。

但“超过 90 天”不代表可以直接物理删除。

执行物理清理前，必须检查：

- 是否仍被 Task 引用。
- 是否仍被 TaskEvent 或审计记录引用。
- 是否属于历史 AgentVersion 的必要配置。
- 是否需要保留会话和消息用于历史查询。
- 是否存在其他外键或业务引用。

被历史任务引用的版本快照不能因为 Agent 已归档或删除而一并删除。

对于官方资源，不提供普通用户物理删除能力。

# 五、任务排期

本阶段建议拆分为 6 个开发任务。按每天约 4—6 小时有效开发时间估算，整体约 5—7 个工作日；如果你需要边学习边开发，可以适当延长。

| 编号  | 任务                         | 预计时间 | 交付物                           |
| ----- | ---------------------------- | -------- | -------------------------------- |
| T3-01 | 数据模型与数据库关系设计     | 1 天     | 模型定义、ER 关系、迁移文件      |
| T3-02 | Agent 基础管理               | 1 天     | Agent CRUD、资源来源和所有权规则 |
| T3-03 | AgentVersion 与发布机制      | 1—2 天   | 版本创建、草稿修改、发布与冻结   |
| T3-04 | Conversation 与 Message      | 1 天     | 会话管理、消息持久化与查询       |
| T3-05 | Task 关联与 Mock 聊天流程    | 0.5—1 天 | 消息到任务的关联服务、测试流程   |
| T3-06 | 权限测试、迁移测试与阶段验收 | 0.5—1 天 | 自动化测试、阶段总结             |

## 5.1 T3-01：先完成数据模型

执行顺序：

1. 检查阶段 1、阶段 2 已有的数据模型。
2. 对照本阶段设计，列出已有表、缺失字段和需要扩展的约束。
3. 确认 Agent、AgentVersion、Conversation、Message 与 Task 的关系。
4. 使用 SQLAlchemy 定义模型。
5. 编写 Alembic 迁移。
6. 在测试数据库中执行迁移。
7. 验证外键、唯一约束和索引。

不要在数据关系尚未确认时同时编写所有 API。

## 5.2 T3-02：完成 Agent 管理

依次实现：

1. 创建用户 Agent。
2. 查询 Agent 列表。
3. 查询 Agent 详情。
4. 修改允许编辑的字段。
5. 复制官方 Agent。
6. 归档 Agent。
7. 校验官方资源不可被普通用户直接修改。
8. 校验不同用户之间的资源隔离。

先保证资源边界正确，再考虑高级筛选与性能优化。

## 5.3 T3-03：完成版本快照

依次实现：

1. 创建初始草稿版本。
2. 查询版本列表。
3. 修改草稿配置。
4. 校验配置完整性。
5. 发布草稿版本。
6. 冻结发布版本。
7. 从当前发布配置创建新草稿。
8. 发布新版本后保留旧版本。
9. 验证历史 Task 不会自动切换到新版本。

建议单独为版本发布服务编写测试，不要只通过 API 集成测试覆盖。

## 5.4 T3-04：完成会话和消息

依次实现：

1. 创建 Conversation。
2. 查询会话列表。
3. 查询会话详情。
4. 保存用户 Message。
5. 保存 Assistant Message。
6. 查询完整历史消息。
7. 支持分页和稳定排序。
8. 实现会话归档。
9. 验证跨用户访问限制。

历史消息建议按照 `created_at` 和唯一 ID 组合排序，避免同一时间戳下出现顺序不稳定。

## 5.5 T3-05：打通最小业务流程

使用 Mock 处理器验证：

1. 用户进入某个 Agent 的 Conversation。
2. 提交一条普通闲聊消息。
3. 系统保存用户消息。
4. Mock 处理器返回一条测试回复。
5. 系统保存助手消息。
6. 验证没有创建 Task。
7. 使用单独的测试路径模拟“需要执行 Skill”的判断。
8. 调用阶段 2 的 Task 创建服务。
9. 验证 Task 绑定当前发布版本。
10. 验证用户消息和 Task 的关联正确。

Mock 处理器应与未来真实模型调用保持接口分离。阶段 5 接入模型时，应替换实现或注入真实处理器，而不是推翻整个 Chat 服务。

## 5.6 T3-06：完整验收

最后执行：

- 数据库迁移与回滚验证。
- Agent CRUD 测试。
- 版本发布和冻结测试。
- Conversation、Message 持久化测试。
- 用户隔离测试。
- Task 关联与幂等测试。
- 历史版本引用测试。
- API 错误处理测试。

全部通过后，再进入阶段 4。

# 六、测试验收

## 6.1 Agent 管理验收

- 

  可以创建用户自己的 Agent。

- 

  可以查询当前用户有权访问的 Agent。

- 

  可以编辑允许编辑的用户 Agent。

- 

  可以复制官方 Agent。

- 

  复制后得到新的 Agent ID。

- 

  复制后保留来源 Agent ID 和来源版本 ID。

- 

  修改复制后的 Agent 不会影响官方 Agent。

- 

  普通用户不能直接编辑官方资源。

- 

  普通用户不能删除官方资源。

- 

  不同用户无法通过修改 URL 中的 ID 读取其他用户的私有 Agent。

## 6.2 AgentVersion 验收

- 

  可以创建草稿版本。

- 

  同一 Agent 的版本号唯一且递增。

- 

  草稿版本允许修改。

- 

  发布前执行配置校验。

- 

  发布成功后形成完整配置快照。

- 

  已发布版本无法通过 API 修改。

- 

  已归档版本无法通过 API 修改。

- 

  发布新版本不会覆盖旧版本。

- 

  Agent 当前发布版本引用始终指向有效的已发布版本。

- 

  并发发布不会造成版本号冲突或当前版本指针不一致。

- 

  历史 Task 始终引用创建时使用的 AgentVersion。

## 6.3 Conversation 验收

- 

  可以创建 Conversation。

- 

  每个 Conversation 只绑定一个 Agent。

- 

  不能直接修改会话绑定的 Agent。

- 

  切换 Agent 时可以创建新会话。

- 

  会话列表只返回当前用户有权访问的记录。

- 

  历史消息完整保存。

- 

  消息查询分页与排序稳定。

- 

  归档会话不会直接删除历史消息。

## 6.4 Message 与 Task 验收

- 

  普通闲聊可以只创建 Message，不创建 Task。

- 

  一条用户 Message 最多关联一个 Task。

- 

  Task 必须绑定 Conversation。

- 

  Task 必须绑定已发布的 AgentVersion。

- 

  Task 的 Agent 必须与 Conversation 的 Agent 一致。

- 

  Task 的触发消息必须属于对应 Conversation。

- 

  任务创建、TaskEvent、Outbox 和消息关联不会产生半完成状态。

- 

  Task 运行结果和用户可见回复分别保存。

- 

  Task 最终回复能够正确关联到 Assistant Message。

- 

  重复请求不会创建重复 Task。

## 6.5 权限与数据完整性验收

- 

  未登录请求不能访问受保护的资源。

- 

  用户不能通过伪造 ID 越权访问私有资源。

- 

  官方资源读取与修改权限分离。

- 

  不同用户的 Conversation 和 Message 相互隔离。

- 

  关键版本发布操作写入审计记录。

- 

  软删除不会破坏历史任务和版本引用。

- 

  数据库外键和唯一约束有效。

- 

  Alembic 迁移可以在干净数据库中完整执行。

## 6.6 阶段通过标准

只有同时满足以下条件，才视为阶段 3 完成：

1. 数据模型与关系约束已经落地。
2. Agent 与 AgentVersion 的核心接口可运行。
3. Conversation 与 Message 能够完整持久化。
4. 版本冻结机制通过自动化测试。
5. Task 能够引用精确的 AgentVersion。
6. 用户资源隔离测试通过。
7. 所有数据库变更均有迁移记录。
8. 没有引入真实模型调用或 Skill 执行的隐式依赖。

# 七、风险项

## 7.1 发布版本仍然可以被修改

**风险：** 历史任务读取到的配置与任务创建时不一致。

**解决方式：**

- 在服务层禁止修改已发布版本。
- 发布后冻结配置快照。
- 使用数据库事务和状态条件控制发布。
- 对版本冻结编写独立测试。
- 后续如果引入后台管理或直接数据库操作，也必须保留不可变性检查。

## 7.2 Agent 当前版本与历史任务版本混淆

**风险：** Agent 发布新版本后，旧任务错误地使用新版本配置。

**解决方式：**

Task 创建时必须保存明确的 `agent_version_id`。执行任务时读取任务绑定的版本，不得根据 Agent 的当前发布版本重新选择配置。

## 7.3 复制官方资源时产生共享引用

**风险：** 用户修改副本后，意外影响官方模板或其他用户。

**解决方式：**

- 创建新的 Agent ID。
- 创建独立的用户配置快照。
- 保存来源资源 ID 和来源版本 ID。
- 对复制后的资源执行独立修改测试。

需要注意，来源追溯关系不等于运行时配置共享关系。副本不应在每次运行时自动读取官方 Agent 的最新配置。

## 7.4 消息与 Task 关联不一致

**风险：** 用户消息显示已执行任务，但 Task 不存在；或者一个消息重复触发多个任务。

**解决方式：**

- 复用阶段 2 的 Task 创建服务。
- 统一处理幂等键。
- 对关联关系添加数据库约束。
- 将 Task、TaskEvent、Outbox 与关联字段放在明确的事务边界内。
- 对失败场景进行事务回滚测试。

## 7.5 数据模型过度设计

**风险：** 为尚未开发的 Skill、Memory、模型调用提前实现复杂逻辑，导致阶段范围失控。

**解决方式：**

本阶段只定义必要的字段、关联与接口契约。SkillVersion 的完整实现放在阶段 4，模型调用和 Function Calling 放在阶段 5，记忆机制放在阶段 9。

可以为未来能力预留扩展字段或接口，但不要提前构建完整子系统。

## 7.6 权限只在前端实现

**风险：** 用户绕过页面直接调用 API，访问其他用户的私有资源。

**解决方式：**

所有权限判断必须在后端完成。前端隐藏按钮只能改善交互，不能替代后端鉴权和资源所有权检查。

## 7.7 Agent 快照与 SkillVersion 不一致

**风险：** AgentVersion 中保存了 Skill 名称，但没有保存准确的 SkillVersion，后续 Skill 更新会改变历史 Agent 的能力。

**解决方式：**

阶段 3 先设计精确版本引用；阶段 4 实现 SkillVersion 后，补齐关系表外键、发布校验和快照一致性验证。

# 八、上线交付

本阶段不是对外发布的完整产品版本，但必须形成可独立验证的开发成果。

## 8.1 交付清单

### 数据层

- 

  Agent 数据模型。

- 

  AgentVersion 数据模型。

- 

  Conversation 数据模型。

- 

  Message 数据模型。

- 

  AgentVersion 与 SkillVersion 的关联结构。

- 

  Task 表扩展迁移。

- 

  必要的唯一约束、外键和索引。

- 

  Alembic 迁移文件。

### 业务层

- 

  Agent 管理服务。

- 

  Agent 复制服务。

- 

  AgentVersion 草稿管理服务。

- 

  AgentVersion 发布服务。

- 

  Conversation 管理服务。

- 

  Message 持久化和查询服务。

- 

  Chat Mock 处理器。

- 

  Task 关联服务。

- 

  基础权限检查与审计调用。

### API 层

- 

  Agent API。

- 

  AgentVersion API。

- 

  Conversation API。

- 

  Message 查询 API。

- 

  统一请求校验与错误处理。

### 测试层

- 

  Agent 单元测试。

- 

  AgentVersion 单元测试。

- 

  发布冻结测试。

- 

  Conversation 与 Message 测试。

- 

  Task 关联测试。

- 

  权限隔离测试。

- 

  数据库迁移测试。

- 

  关键事务回滚测试。

## 8.2 文档交付

建议将以下内容写入项目文档：

- `docs/architecture/agent-and-version.md`
- `docs/architecture/conversation-and-message.md`
- `docs/api/agent-api.md`
- `docs/api/conversation-api.md`
- `docs/database/stage-3-schema.md`
- `docs/testing/stage-3-acceptance.md`

如果项目已有统一文档目录，应沿用原有位置，不要重复建立同一用途的文档。

## 8.3 本地验收流程

在本地开发环境中完成：

1. 拉取或切换到当前开发分支。
2. 检查环境变量和数据库配置。
3. 执行 Alembic 迁移。
4. 启动 FastAPI 服务。
5. 通过自动化测试验证数据模型与业务规则。
6. 使用 API 调试工具创建一个用户 Agent。
7. 创建并发布一个 AgentVersion。
8. 创建 Conversation 并保存几条测试消息。
9. 验证普通闲聊流程不会创建 Task。
10. 模拟需要执行任务的消息，验证 Task 绑定正确的 AgentVersion。
11. 发布新的 AgentVersion，确认旧 Task 仍引用旧版本。
12. 检查审计记录和数据库数据是否一致。

## 8.4 阶段交付边界

本阶段不要求提供完整前端页面，也不要求真实大模型回复。

验收重点是数据模型、业务服务、API 和测试。

如果 Mock 流程可以正常运行，但版本冻结、任务关联或权限隔离测试未通过，仍然不能视为本阶段完成。

# 九、阶段总结

## 9.1 本阶段解决了什么

阶段 3 主要建立 Z-Torque 的业务对象层。

此前阶段 1 建立了工程基础与安全框架，阶段 2 建立了 Task 内核和可靠执行机制。本阶段进一步明确：

- Agent 是可管理的业务资源。
- AgentVersion 是不可变的运行配置快照。
- Conversation 是绑定单个 Agent 的会话。
- Message 是用户与助手交互的基本记录。
- Task 是独立的运行时执行对象。
- Task 必须绑定确切的 AgentVersion。
- 普通闲聊不需要创建 Task。
- 需要执行 Skill 时才进入任务执行链路。
- 用户资源、会话和消息必须受到后端权限控制。

## 9.2 下一阶段的衔接

阶段 4：**Skill、Tool 与 SkillGateway**。

下一阶段将建立能力执行体系：

- Tool 是最小可执行动作。
- Skill 是组合和组织多个 Tool 的能力包。
- Tool 与 Skill 都具备自己的版本。
- SkillVersion 绑定精确的 ToolVersion。
- AgentVersion 绑定精确的 SkillVersion。
- 所有 Tool 调用统一经过 SkillGateway。
- 工具调用必须执行权限检查、参数校验、超时控制、结果标准化和审计记录。

阶段 4 完成后，Z-Torque 才真正具备 Agent 配置可引用、能力对象可管理、工具执行入口统一的基础。

阶段 5 再将 ModelGateway、Function Calling 和 Chat 流式响应接入完整执行链路。

## 9.3 最终验收结论

只有当 Agent、AgentVersion、Conversation、Message 和 Task 之间的关系清晰、数据持久化可靠、发布版本不可变、用户隔离有效，并且自动化测试通过后，才能进入阶段 4。

**本阶段最重要的原则：Agent 可以持续演进，但已经发布的版本和已经创建的任务必须保持历史一致性。**