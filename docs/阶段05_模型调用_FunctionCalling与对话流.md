# Z-Torque 社区版阶段 5：模型调用、Function Calling 与对话流

**文档编号：** ZT-COMMUNITY-P5  
**文档版本：** 1.1（P1/P2 优化）  
**阶段名称：** 模型调用、Function Calling 与对话流  
**前置阶段：** 阶段 0—4  
**阶段状态：** 待实施  
**错误码：** 模型不可用、超时和上游错误分别使用 `AI_MODEL_NOT_AVAILABLE`、`AI_MODEL_TIMEOUT` 和 `AI_MODEL_UPSTREAM_ERROR`。SSE 建连前使用普通 HTTP 错误，建连后发送包含错误码、任务标识和 `trace_id` 的错误事件。

## 默认父 Agent 与受控委派（本阶段必须实现）

当 Chat API 未收到具体 Agent 选择时，使用当前有效的默认父 AgentVersion。模型只有在该版本启用委派且目标 AgentVersion 命中显式白名单时，才能提出委派请求；请求必须转换为阶段 2/8 管理的子 Task，不得直接调用另一个 Agent。

委派输入至少包含目标 AgentVersion、结构化任务目标、必要上下文引用、是否必要和幂等键。创建子 Task 前必须重新执行用户权限、版本可用性、深度、数量、并发和预算检查。子 Task 的结构化结果、状态和必要错误进入父 Task 上下文，子 Agent 的中间推理默认不返回给父 Agent 或用户。

父 Agent 支持在同一个根 Task 内根据前序子 Task 的结构化结果继续创建后续子 Task。例如“分析三月份销售数据”可以先委派数据获取 Agent，待其成功返回数据结果后，再由父 Agent 委派数据分析 Agent。两个子 Task 都必须是父 Task 的直接子任务，后续子 Task 只能引用前序结果中经过权限校验的结构化结果、文件引用或临时结果引用，不能由前一个子 Agent 直接创建。

父 Agent 必须在每次串行委派前校验前置子 Task 状态、结果格式、数据权限和剩余预算。前置结果为空、格式错误、不可解析或权限不足时，父 Agent 应按任务策略重试、降级或终止，不得把未经校验的模型输出直接作为后续 Agent 的可信指令。

> 项目：Z-Torque / 极矩
> 阶段：Stage 5
> 前置阶段：Stage 0—4
> 本阶段性质：模型接入、对话编排、工具调用与流式响应
> 核心目标：打通从用户消息到模型推理、Tool 执行和最终回复的完整链路。
> 开发原则：模型负责理解与决策，Task Kernel 负责可靠执行，SkillGateway 负责所有工具调用的统一控制。

# 一、阶段目标

## 1.1 核心目标

本阶段需要完成六项核心能力：

1. **ModelGateway**：统一管理模型调用、请求格式、响应解析、超时、失败处理和用量记录。
2. **模型选择机制**：按照 AgentVersion 冻结的模型绑定和策略选择模型，不因全局模型配置变化而意外改变历史任务的模型选择规则。
3. **Function Calling**：支持模型提出工具调用请求，并将其映射到 AgentVersion 允许使用的 SkillVersion 和 ToolVersion。
4. **多轮执行循环**：支持同一个 Task 内多轮“模型调用 → Tool 执行 → 模型继续推理”。
5. **Chat Application Service**：统一处理用户消息、上下文组装、普通闲聊与任务执行的分流。
6. **SSE 流式响应**：分别实现普通聊天流和任务执行流，并支持断线重连和任务状态查询。

本阶段完成后，应能够跑通以下两条链路。

### 普通闲聊链路

```
用户消息 → Conversation → Message → ModelGateway → 助手回复 → Message
```

该链路不创建 Task。

### 工具执行链路

```
用户消息 → AgentVersion → 创建 Task → ModelGateway → Function Calling → SkillGateway → Tool Result → ModelGateway → 最终回复
```

该链路必须使用任务创建时绑定的 AgentVersion，并复用阶段 2 的 Task 状态机、Outbox 和可靠执行机制。

## 1.2 本阶段交付结果

完成后，系统应具备：

- 能够配置和调用至少一种真实模型服务。
- 统一的模型请求和响应接口。
- 模型调用失败时能够返回规范化错误。
- 能够根据 AgentVersion 选择模型和可用 Skill。
- 能够解析模型返回的 Function Calling 请求。
- 能够校验模型请求的 Tool 是否在允许范围内。
- 所有 Tool 调用均通过 SkillGateway。
- 同一个 Task 支持最多 5 轮模型与工具交互。
- 普通闲聊不创建 Task。
- 需要执行工具时创建 Task，并保留之前相关的对话上下文。
- 支持 Chat SSE 与 Task SSE。
- 支持 `Last-Event-ID` 事件恢复。
- 支持 Chat API 幂等处理。
- 支持 Task 执行取消与超时。
- 能够记录模型调用、工具调用和最终结果的必要审计信息。
- 能够通过自动化测试验证完整链路。

## 1.3 本阶段暂不实现

以下能力不在本阶段完整实现：

- 完整前端控制台：阶段 10。
- 短期记忆自动总结：阶段 9。
- 完整文件解析与多模态文件处理：阶段 9。
- 完整 Connector 管理界面：阶段 7。
- 完整的统一权限决策与运行安全闭环：阶段 8。
- 多 Agent 协作。
- A2A（Agent-to-Agent）通信。
- 自动生成或修改 Skill。
- 任意代码执行。
- 复杂的模型路由与成本优化系统。

本阶段可以保留多模态请求的扩展接口，但应优先完成稳定的文本聊天与工具调用链路。

# 二、需求范围

## 2.1 ModelGateway：统一模型调用入口

ModelGateway 是所有模型调用的统一入口。

业务模块不能各自直接调用不同模型厂商的 SDK，否则会造成请求格式、错误处理、超时、计费和审计规则不一致。

ModelGateway 至少需要完成：

- 解析模型调用请求。
- 校验调用身份和模型访问权限。
- 根据模型配置选择提供商和端点。
- 将统一请求转换为提供商所需的请求格式。
- 调用模型服务。
- 解析普通文本响应。
- 解析 Function Calling 响应。
- 支持流式响应。
- 统一处理超时、限流、服务不可用和格式错误。
- 记录必要的调用耗时和用量信息。
- 对敏感信息进行脱敏。

### 统一模型请求

内部模型请求建议包含：

- `trace_id`
- `task_id`，可为空。
- `conversation_id`
- `agent_version_id`
- `model_id`
- `messages`
- `tools`
- `tool_choice`
- `temperature` 等允许配置的生成参数。
- `max_tokens` 或对应的输出长度限制。
- `timeout_seconds`
- `stream`

其中，`task_id` 允许为空，是因为普通闲聊可以直接调用模型，不创建 Task。

请求中的用户身份和权限必须来自服务端的 SecurityContext，不能从模型生成的内容中获取。

### 统一模型响应

模型响应至少需要区分：

- 普通文本内容。
- Function Calling 请求。
- 模型结束原因。
- 提供商返回的请求标识。
- 用量统计。
- 错误信息。

建议使用内部统一结构表达这些内容，不要让 Chat Service 直接依赖某个厂商 SDK 的原始响应对象。

## 2.2 模型管理与选择

阶段 1 已确定采用统一模型管理思路，模型、提供商和端点分别管理。

模型记录本身不需要 Z-Torque 版本号，但 AgentVersion 必须冻结模型绑定与选择策略。

模型选择顺序应遵循已冻结的 AgentVersion 配置，不得在执行过程中随意回退到 Agent 当前版本的配置。

建议选择流程：

1. 读取 Task 绑定的 AgentVersion；普通闲聊读取当前会话对应的有效发布版本。
2. 获取该版本冻结的模型绑定策略。
3. 检查模型是否启用、是否可访问。
4. 根据显式模型绑定或策略选择可用模型端点。
5. 通过 ModelGateway 发起调用。
6. 记录实际使用的模型和端点信息。

### 模型配置边界

需要区分两类配置：

**AgentVersion 冻结的配置：**

- 主要模型绑定。
- 模型选择策略。
- 生成参数。
- 可使用的模型能力要求。
- 允许的回退策略。

**模型管理模块中的动态配置：**

- API 端点。
- 凭据引用。
- 服务可用状态。
- 限流状态。
- 运维健康信息。

因此，历史 Task 可以保持原有的模型选择规则，但不能保证外部模型服务本身永远不变。模型端点、厂商行为或可用状态变化时，应通过运维配置和审计明确记录。

如果某个模型不可用，系统只能使用 AgentVersion 明确允许的回退策略。未配置回退时，应返回明确的模型不可用错误，而不是任意切换其他模型。

## 2.3 Function Calling：模型提出工具调用请求

Function Calling 是模型返回结构化工具调用请求的机制。

模型不会直接执行 Tool。它只是提出一个调用建议，Z-Torque 必须在服务端校验后才能执行。

完整过程：

1. Chat Service 组装上下文。
2. ModelGateway 将允许使用的工具定义传给模型。
3. 模型返回普通文本，或返回一个或多个 Tool Call。
4. 系统解析 Tool Call。
5. 校验 Tool Call 是否属于当前 AgentVersion 允许使用的 SkillVersion。
6. 校验对应 SkillVersion 是否允许调用目标 ToolVersion。
7. 校验参数 Schema。
8. 通过 SkillGateway 执行 Tool。
9. 将 Tool Result 按协议回填模型上下文。
10. 再次调用模型。
11. 直到生成最终回复，或达到轮次、超时、取消等终止条件。

必须注意：**模型返回的 Tool Call 是不可信输入，不是执行授权。**

即使模型请求了某个工具，也不能跳过资源关系、用户权限、参数校验和确认策略。

## 2.4 AgentVersion 与可用工具

模型收到的工具定义必须来自当前有效的 AgentVersion。

工具列表的生成规则：

1. 读取该 AgentVersion 绑定的 SkillVersion。
2. 获取这些 SkillVersion 允许暴露的 Tool 定义。
3. 根据用户权限过滤不可用能力。
4. 根据当前任务上下文和能力限制生成模型可见的工具列表。
5. 将工具名称、描述和输入 Schema 传给模型。

不得直接把系统中的全部 Tool 暴露给模型。

即使某个 Tool 已经存在，也只有当它通过当前 AgentVersion 的 SkillVersion 引用关系、用户权限和策略检查后，才能被模型调用。

### 工具名称映射

模型返回的工具名称不应直接被当作数据库主键。

建议维护一份当前模型请求的工具映射：

- 模型可见的工具名称。
- SkillVersion ID。
- ToolVersion ID。
- 允许的参数 Schema。
- 当前请求内的映射标识。

收到模型 Tool Call 后，通过这份映射解析到实际版本 ID，再交由 SkillGateway 执行。

这样可以防止模型伪造其他资源 ID，或通过猜测内部 ID 调用未授权工具。

## 2.5 多轮模型与工具执行

一个 Task 允许多轮模型与 Tool 交互，最大轮数默认 5 轮。

这里的“一轮”统一定义为：

> 一次模型调用及其后续返回的所有 Tool Call 处理完成，之后将结果交回模型继续推理，构成一个执行轮次。

例如：

- 第 1 轮：模型要求查询客户信息。
- 执行 Tool。
- 第 2 轮：模型要求查询业务记录。
- 执行 Tool。
- 第 3 轮：模型综合结果并生成最终回复。

### 执行限制

- 最大模型与工具执行轮次：默认 5 轮。
- 单个 Tool 的超时：遵循 ToolVersion 与全局限制。
- 整个 Task 的超时：遵循阶段 2 的任务策略。
- Tool Result：限制大小，默认最多 20,000 字符。
- 模型上下文：遵循模型上下文长度限制。
- 工具调用数量：每轮设置上限，避免单轮请求无限扩张。
- 模型输出：遵循统一的最大输出长度限制。

达到最大轮数后，如果模型仍然请求调用工具，不得继续无限循环。系统应进入受控终止流程，生成明确的失败或中止结果，并保证 Task 状态和审计记录一致。

## 2.6 Chat API：统一聊天入口

Chat API 是前端和外部系统发送聊天消息的统一入口。

建议输入：

- `agent_id`
- `conversation_id`，可选。
- `message`
- `content_parts`，可选。
- 其他允许的请求选项。

处理流程：

1. 验证用户身份。
2. 验证 Agent 的访问权限。
3. 如果没有 Conversation ID，创建新会话。
4. 验证 Conversation 是否绑定当前 Agent。
5. 读取当前有效的 AgentVersion。
6. 持久化用户 Message。
7. 组装上下文。
8. 执行初始模型调用。
9. 根据模型响应决定普通聊天或进入 Task 执行链路。
10. 返回响应类型、消息标识和必要的 Task 标识。

同一个 Conversation 不允许直接切换 Agent。如果请求中的 Agent 与会话绑定不一致，应返回明确错误，并要求创建新会话。

### 普通闲聊

- 保存用户 Message。
- 调用 ModelGateway。
- 将模型回复保存为 Assistant Message。
- 不创建 Task。

### 需要工具执行

- 保存用户 Message。
- 进行初始模型判断。
- 如果模型返回有效的 Tool Call，进入任务执行链路。
- 创建 Task 并绑定对应 AgentVersion。
- 将 `task_id` 写入触发 Message。
- 将后续执行交给 Task Kernel 和 Worker。
- 返回 `task_id`，由客户端通过 Task SSE 获取执行过程。

初始模型判断可以发生在 Task 创建之前，因此系统需要保留初始判断的上下文和必要结果。不能因为初始模型调用发生在 Task 创建之前，就把它错误记录为一次已经执行了 Tool 的任务。

## 2.7 闲聊转任务时的上下文

用户可能先进行普通闲聊，再提出需要执行工具的具体需求。

例如：

1. 用户询问某个业务概念。
2. 助手解释。
3. 用户继续追问当前业务数据。
4. 模型判断需要查询业务系统。

此时新 Task 需要携带相关历史上下文，不能只把最后一句话发给模型。

默认上下文规则：

- System Prompt。
- AgentVersion 冻结的行为规则。
- 允许使用的 Skill 和 Tool 定义。
- Memory 摘要；阶段 9 未完成时可以为空。
- 最近 5 轮对话。
- 当前用户消息。
- 必要的其他历史消息；在权限和上下文长度限制内补充。

完整对话历史保存在数据库，但每次模型调用不应默认加载全部历史。

初始判断与 Task Worker 后续模型调用之间，应保留必要的消息、上下文和模型调用关联信息，确保任务能够正确继续。

## 2.8 Chat SSE 与 Task SSE

系统需要明确区分两类流式响应。

### Chat SSE

用于普通闲聊的模型流式回复。

建议事件：

- `message.delta`
- `message.result`
- `message.error`
- `message.done`

普通闲聊结束后，助手消息必须完成持久化。客户端不能把浏览器收到的最后一段文本当作唯一的数据来源。

### Task SSE

用于任务执行过程。

沿用阶段 2 的任务事件契约，并扩展必要的模型事件：

- `task.snapshot`
- `task.status`
- `task.delta`
- `task.tool_call`
- `task.tool_result`
- `task.waiting_human`
- `task.human_decision`
- `task.result`
- `task.error`
- `task.done`

Task SSE 主要呈现任务状态、模型增量文本、工具调用摘要、工具结果摘要和最终结果。

### 事件恢复

客户端重连时可通过 `Last-Event-ID` 恢复事件。

事件需要具备稳定的事件 ID 和可重放的数据来源。不能仅依赖内存中临时保存的 SSE 数据，否则服务重启后无法恢复。

Task 事件应从持久化的 TaskEvent 读取。普通聊天流需要明确区分已持久化消息与尚未完成的流式增量；重连后可以重新查询消息状态，但不能假设所有未完成增量都已被永久保存。

## 2.9 幂等与重复请求

网络重试不能导致重复创建 Message 或重复执行 Task。

需要区分两个层级：

**Chat 层幂等：**

使用 `Idempotency-Key` 识别重复聊天请求，防止重复创建消息、会话和 Task。

**Task 层幂等：**

沿用阶段 2 的任务幂等规则，保证同一任务不会因 Outbox 重试或 Worker 恢复而重复执行。

Chat 幂等记录需要与请求用户、租户和请求内容关联。

同一个幂等键被重复使用时：

- 请求内容一致：返回原请求结果或原有处理状态。
- 请求内容不一致：返回明确的幂等冲突错误。
- 不能因为重复请求而重新调用模型并创建第二个 Task。

对于模型已经开始输出、但客户端断线的情况，应优先查询原请求的持久化状态，而不是直接重新发起一轮独立模型调用。

## 2.10 模型调用失败与错误处理

模型调用可能出现：

- 认证失败。
- 模型不可用。
- 请求超时。
- 提供商限流。
- 网络连接失败。
- 响应格式错误。
- 不支持 Function Calling。
- 模型输出不完整。
- 上下文超出长度限制。

ModelGateway 应将这些错误统一转换为内部错误结构。

需要区分普通闲聊和 Task：

- 普通闲聊失败：将消息标记为失败，保存必要的错误信息。
- Task 内模型调用失败：交由任务执行逻辑判断是否重试或终止。
- Tool 调用失败：先返回标准化 Tool Result，由模型或任务逻辑决定下一步。
- 权限不足：不得自动重试。
- 未配置回退策略的模型不可用：明确失败，不得静默切换模型。

错误消息不能泄露 Secret、内部请求头、模型提供商凭据或完整敏感上下文。

# 三、技术方案

## 3.1 总体架构

本阶段继续使用模块化单体架构。

建议的调用关系：

```
Chat API
   │
   ▼
Chat Application Service
   │
   ├── Conversation / Message Service
   │
   ├── Context Builder
   │
   ├── ModelGateway
   │      │
   │      ├── Model Provider Adapter
   │      └── Model Endpoint Selector
   │
   └── Task Kernel
          │
          ▼
        Worker
          │
          ▼
    Agent Execution Loop
          │
          ├── ModelGateway
          │
          └── SkillGateway
                 │
                 ├── HTTP Executor
                 ├── DB_QUERY Executor
                 ├── MCP Executor
                 └── MD-Skill Executor
```

需要特别注意：

- Chat Application Service 负责用户消息与普通聊天分流。
- Task Kernel 负责可靠任务生命周期。
- Worker 负责执行已创建的任务。
- ModelGateway 负责模型调用。
- SkillGateway 负责 Tool 执行控制。
- 各模块不得绕过其对应的统一入口。

## 3.2 建议代码结构

以下为职责示意，实际实施时沿用已有目录，不要重复建立模块。

```
app/
├── kernel/
│   ├── model_gateway/
│   │   ├── gateway.py
│   │   ├── contracts.py
│   │   ├── selector.py
│   │   └── usage.py
│   ├── task/
│   ├── agent/
│   ├── skill_gateway/
│   ├── audit/
│   └── identity/
├── modules/
│   └── chat/
│       ├── service.py
│       ├── context_builder.py
│       ├── function_calling.py
│       ├── execution_loop.py
│       └── stream_service.py
├── builtin_plugins/
│   └── model_providers/
│       ├── ...
│       └── ...
└── api/
    └── ...
```

## 3.3 ModelGateway 的抽象边界

ModelGateway 应定义统一接口，不直接暴露提供商 SDK 的复杂对象。

建议统一能力包括：

- `generate`：执行一次非流式模型调用。
- `stream`：执行一次流式模型调用。
- `normalize_response`：转换模型响应。
- `normalize_error`：转换模型错误。
- `estimate_usage`：记录可获得的用量数据。

这些名称是职责示例，不要求照搬。

底层不同模型提供商通过 Adapter（适配器）转换为统一请求和响应结构。

这样未来新增模型时，只需要实现相应适配器，而不需要修改整个 Chat 执行循环。

## 3.4 模型提供商适配

模型提供商适配器需要处理：

- 认证。
- 请求参数映射。
- 文本响应解析。
- Tool Call 解析。
- 流式增量解析。
- 使用量信息解析。
- 错误转换。
- 超时和取消。

不能假设所有模型提供商对 Function Calling 和流式输出的协议都完全一致。

对于不支持 Function Calling 的模型，系统应明确标识其能力限制。不能把普通文本中出现的 JSON 字符串自动视为可信 Tool Call。

第一版优先支持一种经过真实测试的模型提供商，建立标准接口后再扩展其他提供商。

## 3.5 Function Calling 执行循环

任务执行循环建议采用以下步骤：

1. 从 Task 读取固定的 AgentVersion。
2. 校验 Task 当前状态允许继续执行。
3. 组装上下文。
4. 根据 AgentVersion 解析可用 SkillVersion 和 ToolVersion。
5. 通过 ModelGateway 调用模型。
6. 解析模型响应。
7. 如果是普通文本且模型正常结束，保存最终助手回复。
8. 如果返回 Tool Call，验证调用映射和参数。
9. 将每个合法调用交给 SkillGateway。
10. 将结果转换为模型可识别的工具结果消息。
11. 更新任务事件和上下文。
12. 检查取消、超时和最大轮数。
13. 满足继续条件时再次调用模型。
14. 达到终止条件时提交最终结果或明确错误。

对于一轮内包含多个 Tool Call 的情况，应设置最大调用数量，并对调用顺序作出明确规定。

第一版可以顺序执行 Tool Call，降低并发控制和上下文同步的复杂度。只有在能够证明工具之间没有依赖关系、权限和取消处理可靠时，才考虑并行执行。

## 3.6 Function Calling 参数校验

模型返回的参数可能是 JSON，也可能存在缺失字段、类型错误或额外字段。

执行前必须：

- 校验工具名称是否在当前请求映射中。
- 校验目标 ToolVersion 是否属于当前 AgentVersion 允许的 SkillVersion。
- 校验参数是否符合 ToolVersion 的输入 Schema。
- 拒绝不符合 Schema 的请求。
- 检查当前用户权限。
- 检查是否需要人工确认。
- 对敏感参数执行脱敏。
- 将合法请求传递给 SkillGateway。

不能仅根据模型提供的 Tool 名称或参数中的资源 ID 决定执行哪个工具。

## 3.7 Tool Result 回填

Tool 执行后，应将统一结果转换为当前模型协议支持的工具结果消息。

要求：

- 保留对应的 Tool Call ID。
- 保留执行成功或失败状态。
- 限制结果长度。
- 对敏感内容脱敏。
- 明确错误类型。
- 不将内部异常堆栈直接发送给模型。
- 保证工具结果与原始调用正确对应。

如果模型返回多个 Tool Call，结果回填必须能够区分每一个调用，不能把多个结果混在一个没有关联标识的文本块中。

## 3.8 Task 与 Worker 集成

阶段 2 已建立 Task 状态机、Outbox 和 Worker，本阶段不得重写这些机制。

任务执行时：

- Worker 获取任务后，读取固定 AgentVersion。
- 检查任务状态。
- 开始模型与工具执行循环。
- 通过 Task Kernel 更新状态和事件。
- 通过 ModelGateway 调用模型。
- 通过 SkillGateway 调用 Tool。
- 将最终回复保存为 Assistant Message。
- 将 `result_message_id` 写入 Task。
- 按合法状态转换完成任务。

Task 的成功、失败、超时和取消状态必须通过任务内核管理。

工具执行结果、模型回复和任务终态需要有清晰的事务边界，避免出现“Task 已成功，但最终 Message 未保存”的不一致情况。

## 3.9 Chat SSE 实现

普通聊天流式响应建议采用以下流程：

1. 接收 Chat API 请求。
2. 完成身份和资源校验。
3. 保存用户 Message。
4. 调用 ModelGateway 的流式接口。
5. 将模型增量内容转换为统一 SSE 事件。
6. 持续输出 `message.delta`。
7. 模型结束后保存完整 Assistant Message。
8. 输出 `message.result`。
9. 输出 `message.done`。

如果流式调用失败，应保存明确的失败状态并输出错误事件。

客户端断线不能直接认为模型调用已取消。是否继续调用由服务端的请求生命周期策略决定，并应与持久化消息状态保持一致。

## 3.10 Task SSE 实现

Task SSE 复用阶段 2 的持久化 TaskEvent 和事件恢复机制。

模型输出可以通过 `task.delta` 发送增量文本，Tool 执行过程通过 `task.tool_call` 和 `task.tool_result` 呈现。

事件至少需要包含：

- 稳定的事件 ID。
- Task ID。
- 事件类型。
- 事件时间。
- 脱敏后的事件数据。

SSE 断线重连时，应先返回必要的当前任务快照，再按事件 ID 恢复后续事件。

不能只依靠内存中的 Pub/Sub 保存历史事件。内存事件总线可以用于实时通知，但持久化 TaskEvent 才是任务执行过程的可靠记录来源。

## 3.11 数据库变更

本阶段需要检查并按实际情况扩展以下数据结构：

- `zr_message`
- `zr_task`
- `zr_task_event`
- `zr_model_provider`
- `zr_model`
- `zr_model_endpoint`
- `zr_audit_log`

如果已有结构满足需求，不要为了统一命名而重建。

建议为 Message 增加或完善：

- 消息处理状态。
- 错误码和错误摘要。
- 必要的请求幂等关联字段。

建议为 TaskEvent 增加或完善：

- 模型调用开始与结束事件。
- 模型调用摘要。
- Tool Call 关联标识。
- 轮次编号。
- 必要的模型使用量摘要。

如果模型用量需要独立统计，可建立专用记录表；不要把完整模型请求、所有上下文和敏感原始响应无条件存进审计表。

数据库变更必须使用 Alembic。

# 四、接口与数据设计

## 4.1 Chat API

建议接口：

```
POST /api/z-torque/chat
```

请求字段：

| 字段              | 含义               |
| ----------------- | ------------------ |
| `agent_id`        | 当前使用的 Agent   |
| `conversation_id` | 可选，会话 ID      |
| `message`         | 用户文本           |
| `content_parts`   | 可选，扩展消息内容 |
| `stream`          | 是否使用流式响应   |

幂等键通过请求头 `Idempotency-Key` 传入。

### 返回行为

普通闲聊返回：

- `response_type = CHAT`
- `conversation_id`
- `user_message_id`
- `assistant_message_id`，完成后提供
- `status`

需要执行 Task 时返回：

- `response_type = TASK`
- `conversation_id`
- `user_message_id`
- `task_id`
- `status`

如果采用 SSE 作为统一响应方式，必须明确事件契约，让客户端可以在收到任务创建事件后切换到 Task SSE。

无论采用哪种传输形式，都不能要求客户端猜测当前请求究竟创建了 Task 还是普通消息。

## 4.2 Chat SSE

建议路径：

```
POST /api/z-torque/chat/stream
```

如果现有路由已使用其他路径，应保持统一路由规范，避免同时维护重复接口。

事件：

| 事件             | 含义             |
| ---------------- | ---------------- |
| `message.delta`  | 助手文本增量     |
| `message.result` | 已完成的助手消息 |
| `message.error`  | 消息处理错误     |
| `message.done`   | 普通聊天结束     |

当模型判断需要工具执行时，应返回明确的任务分流事件或标准任务响应，包含 `task_id`，让客户端切换到 Task SSE。

Chat SSE 和 Task SSE 不应被合并成无法区分的混合协议。

## 4.3 Task SSE

沿用阶段 2 的事件路径，并使用统一 API 前缀：

```
GET /api/z-torque/tasks/{task_id}/stream
```

支持：

- `Last-Event-ID`。
- 当前任务快照。
- 事件顺序恢复。
- 权限检查。
- 终态事件。
- 断线重连。

客户端必须先通过任务权限检查，再获取事件。

## 4.4 模型管理 API

应复用项目已有的模型管理接口。

如果阶段 1 已经实现模型、提供商和端点管理，本阶段只需补齐必要字段和能力声明，不要重复开发一套模型管理 API。

建议至少能够查询：

- 模型是否启用。
- 模型是否支持流式输出。
- 模型是否支持 Function Calling。
- 模型对应的提供商和端点状态。
- 模型的可用配置项。
- 模型凭据是否已配置。

API 不得返回凭据明文。

## 4.5 内部 ModelGateway 契约

建议定义以下内部操作：

- 普通生成请求。
- 流式生成请求。
- Function Calling 请求。
- 模型选择。
- 错误标准化。
- 用量记录。

内部调用需要携带 `trace_id`、`agent_version_id` 和必要的调用关联信息。

Task 内调用还需要携带 `task_id` 和轮次编号。

这些字段用于追踪调用链，不应被模型输出覆盖。

## 4.6 错误码建议

| 错误码                            | 含义                         |
| --------------------------------- | ---------------------------- |
| `AI_MODEL_NOT_AVAILABLE`          | 模型不可用                   |
| `AI_MODEL_AUTH_FAILED`            | 模型认证失败                 |
| `AI_MODEL_RATE_LIMITED`           | 模型请求被限流               |
| `AI_MODEL_TIMEOUT`                | 模型调用超时                 |
| `AI_MODEL_RESPONSE_INVALID`       | 模型响应格式错误             |
| `AI_MODEL_CAPABILITY_UNAVAILABLE` | 当前模型不支持所需能力       |
| `AI_FUNCTION_CALL_INVALID`        | Function Calling 请求不合法  |
| `AI_TOOL_MAPPING_INVALID`         | 工具名称无法映射到允许的版本 |
| `AI_MAX_ROUNDS_EXCEEDED`          | 超过最大执行轮数             |
| `AI_CONTEXT_TOO_LARGE`            | 上下文超出限制               |
| `AI_CHAT_IDEMPOTENCY_CONFLICT`    | 幂等键与请求内容冲突         |

具体错误码应与项目现有错误码规范合并，避免重复定义同义错误。

# 五、任务排期

建议拆分为 7 个开发任务，按每天约 4—6 小时有效开发时间估算，整体约 7—10 个工作日。实际时间取决于模型提供商适配和流式协议调试的复杂度。

| 编号  | 任务                        | 预计时间 | 交付物                    |
| ----- | --------------------------- | -------- | ------------------------- |
| T5-01 | ModelGateway 抽象与模型适配 | 1—2 天   | 统一模型请求、响应和错误  |
| T5-02 | 上下文构建与普通聊天        | 1 天     | Context Builder、闲聊流程 |
| T5-03 | Function Calling 解析       | 1 天     | Tool Call 解析和映射校验  |
| T5-04 | Task 执行循环               | 1—2 天   | 多轮模型与工具交互        |
| T5-05 | Chat SSE 与 Task SSE 集成   | 1—2 天   | 两类流式响应和事件恢复    |
| T5-06 | 幂等、取消、超时与用量记录  | 1 天     | 可靠性补齐                |
| T5-07 | 集成测试与阶段验收          | 1 天     | 完整链路测试              |

## 5.1 T5-01：ModelGateway

实施顺序：

1. 检查现有模型管理和配置结构。
2. 定义统一请求与响应契约。
3. 定义模型提供商适配接口。
4. 接入一种真实模型服务。
5. 实现非流式调用。
6. 实现错误标准化。
7. 实现流式调用。
8. 记录必要的用量信息。
9. 验证模型凭据不出现在 API 响应和日志中。

建议先完成非流式调用，再接入流式调用，降低调试复杂度。

## 5.2 T5-02：上下文构建与普通聊天

依次实现：

1. 读取当前 Conversation。
2. 读取当前有效 AgentVersion。
3. 读取必要的历史 Message。
4. 按最近 5 轮默认规则构建上下文。
5. 加入 Agent 规则。
6. 加入允许的 Skill 和 Tool 定义。
7. 调用 ModelGateway。
8. 保存助手回复。
9. 验证普通闲聊不会创建 Task。

上下文构建应独立于模型提供商，不能把上下文拼接逻辑全部写在某个厂商适配器中。

## 5.3 T5-03：Function Calling

依次实现：

1. 根据 AgentVersion 生成工具定义。
2. 建立当前请求的工具映射。
3. 解析模型 Tool Call。
4. 验证调用名称。
5. 验证 SkillVersion 和 ToolVersion 关系。
6. 校验参数 Schema。
7. 处理非法调用。
8. 将合法调用交给 SkillGateway。
9. 将结果回填模型上下文。

先使用少量可控测试 Tool 验证流程，不要一开始就暴露所有工具。

## 5.4 T5-04：Task 执行循环

依次实现：

1. 判断模型响应是否需要工具执行。
2. 创建 Task 并绑定 AgentVersion。
3. 将触发消息关联到 Task。
4. 通过 Worker 启动任务执行。
5. 执行模型与工具循环。
6. 设置最大 5 轮限制。
7. 支持一轮多个 Tool Call 的受控处理。
8. 检查取消和超时。
9. 保存最终助手消息。
10. 更新 Task 最终结果与状态。

必须复用阶段 2 的 Task Kernel 和 Outbox，不能在 Chat Service 中自行创建后台任务队列来替代。

## 5.5 T5-05：SSE

先完成普通聊天流：

1. 读取模型流式增量。
2. 转换为统一事件。
3. 保存最终消息。
4. 发送完成事件。

然后完成 Task 流：

1. 订阅持久化任务事件。
2. 发送当前任务快照。
3. 按事件 ID 输出后续事件。
4. 支持 `Last-Event-ID`。
5. 处理客户端断线。
6. 验证服务重启后的恢复行为。

## 5.6 T5-06：可靠性补齐

依次处理：

- Chat 幂等。
- Task 幂等。
- 模型超时。
- Tool 超时。
- Task 取消。
- 模型限流。
- 模型回退策略。
- 轮次限制。
- 上下文长度限制。
- 敏感信息脱敏。
- 用量记录。

不要把所有异常简单地捕获后返回空字符串。错误必须具有可追踪的类型和状态。

## 5.7 T5-07：集成验收

使用真实模型与受控测试工具完成：

1. 普通闲聊。
2. 单 Tool 调用。
3. 多轮 Tool 调用。
4. 非法 Tool Call。
5. 工具权限拒绝。
6. Tool 执行失败。
7. 模型超时。
8. 超过最大轮数。
9. Chat 重复请求。
10. Task SSE 断线重连。
11. Task 取消。
12. Agent 发布新版本后的历史任务验证。

全部通过后，再进入阶段 6。

# 六、测试验收

## 6.1 ModelGateway

- 

  能够调用至少一种真实模型服务。

- 

  普通文本响应能够正确解析。

- 

  Function Calling 响应能够正确解析。

- 

  流式输出能够正确处理。

- 

  模型认证失败有明确错误。

- 

  模型超时有明确错误。

- 

  模型限流有明确处理。

- 

  不支持 Function Calling 的模型不会被错误当作支持。

- 

  模型不可用时遵循明确的回退策略。

- 

  凭据不会进入 API 响应、日志或审计内容。

## 6.2 普通聊天

- 

  能够创建或复用 Conversation。

- 

  Conversation 始终绑定一个 Agent。

- 

  使用当前有效的已发布 AgentVersion。

- 

  用户消息被持久化。

- 

  助手消息被持久化。

- 

  普通闲聊不会创建 Task。

- 

  上下文包含必要的 Agent 规则和近期消息。

- 

  历史消息分页和排序正确。

- 

  失败消息能够查询并重试，而不会自动产生重复记录。

## 6.3 Function Calling

- 

  模型只能看到当前 AgentVersion 允许的工具定义。

- 

  模型不能通过伪造工具名称调用任意 Tool。

- 

  模型不能通过猜测资源 ID 调用其他版本。

- 

  参数必须通过 Tool Schema 校验。

- 

  SkillVersion 和 ToolVersion 的关联必须有效。

- 

  权限检查在实际执行前完成。

- 

  所有调用均经过 SkillGateway。

- 

  Tool Call ID 与 Tool Result 正确匹配。

- 

  工具执行失败能够返回标准化错误。

- 

  非法调用不会直接导致未授权的外部操作。

## 6.4 Task 执行循环

- 

  需要工具执行时创建 Task。

- 

  Task 绑定创建时使用的 AgentVersion。

- 

  用户 Message 关联到正确的 Task。

- 

  同一个 Task 支持多轮模型与工具交互。

- 

  最大执行轮数有效。

- 

  单轮 Tool Call 数量受到限制。

- 

  Task 使用阶段 2 的 Outbox 和 Worker。

- 

  Task 状态变化符合状态机。

- 

  Tool 失败不会直接绕过 Task Kernel。

- 

  最终 Assistant Message 与 Task 正确关联。

- 

  任务超时和取消能够正常终止。

- 

  已终止 Task 的迟到结果不会覆盖终态。

## 6.5 SSE 与事件恢复

- 

  Chat SSE 可以输出增量文本。

- 

  Chat SSE 正常结束后助手消息已持久化。

- 

  Task SSE 能显示状态变化。

- 

  Task SSE 能显示 Tool Call 和 Tool Result 摘要。

- 

  支持 `Last-Event-ID`。

- 

  断线重连后能够恢复任务事件。

- 

  服务重启后仍能查询已持久化的 TaskEvent。

- 

  不同用户不能订阅无权访问的 Task。

- 

  客户端断线不会导致消息状态与数据库状态不一致。

## 6.6 幂等与版本一致性

- 

  相同 Chat 幂等键和相同请求不会重复创建 Message 或 Task。

- 

  相同幂等键和不同请求内容会返回冲突。

- 

  Worker 重试不会导致重复任务。

- 

  新 AgentVersion 发布后，旧 Task 仍然引用原版本。

- 

  旧任务执行时使用原版本的 SkillVersion 和 ToolVersion。

- 

  不会因为 Tool 当前版本变化而自动改变历史任务配置。

## 6.7 阶段通过标准

满足以下条件，才算阶段 5 完成：

1. ModelGateway 可调用真实模型。
2. 普通闲聊完整可用且不创建 Task。
3. Function Calling 能够触发合法 Tool 执行。
4. 所有 Tool 调用均通过 SkillGateway。
5. 一个 Task 能完成多轮模型与工具交互。
6. 最大轮次、超时和取消机制有效。
7. Chat SSE 和 Task SSE 职责明确。
8. Task 事件可以恢复。
9. Chat 和 Task 幂等机制有效。
10. 任务、消息、版本和审计记录一致。
11. 核心自动化测试全部通过。

# 七、风险项

## 7.1 模型返回的 Tool Call 被直接执行

**风险：** 模型可能生成未授权的工具名称、错误参数或伪造的资源标识。

**处理方式：**

- 将模型 Tool Call 视为不可信输入。
- 使用当前请求的工具映射。
- 校验版本关系和输入 Schema。
- 在 SkillGateway 再次校验权限。
- 不允许模型直接指定任意 URL、数据库连接或脚本路径。

## 7.2 闲聊和 Task 分流错误

**风险：** 所有消息都创建 Task，导致任务记录膨胀；或者需要执行工具时仍然只保存普通聊天消息。

**处理方式：**

明确普通闲聊与工具执行的分流规则。模型返回有效 Tool Call 后才进入 Task 执行链路，并保证消息与 Task 的关联一致。

## 7.3 初始模型调用与 Task 创建之间的数据不一致

**风险：** 初始模型已经提出 Tool Call，但 Task 创建失败，造成调用过程无法追踪；或者重复请求导致重复执行。

**处理方式：**

- 先持久化用户消息。
- 记录请求幂等信息。
- 保留必要的初始判断上下文。
- 创建 Task 时复用 Task Kernel。
- 对消息关联和 Outbox 使用明确的事务边界。
- 创建失败时保留可恢复的请求状态，避免盲目重新执行。

## 7.4 模型上下文与历史版本不一致

**风险：** Task 已经绑定旧 AgentVersion，但上下文构建时重新读取 Agent 当前配置。

**处理方式：**

Task 执行期间只读取 Task 固定的 AgentVersion。SkillVersion、ToolVersion 和模型选择规则均从该版本的冻结配置解析，不得动态替换成最新版本。

## 7.5 无限工具调用循环

**风险：** 模型不断提出 Tool Call，造成资源消耗和请求成本持续增加。

**处理方式：**

- 限制最多 5 轮。
- 限制每轮 Tool Call 数量。
- 限制 Task 总执行时间。
- 限制模型输出长度和 Tool Result 大小。
- 终止时保存明确错误或中止结果。

## 7.6 模型输出与 SSE 数据不一致

**风险：** 客户端收到完整回复，但数据库只保存部分内容；或者数据库已保存最终消息，但 SSE 没有正确结束。

**处理方式：**

以数据库持久化结果为最终依据。流式增量只是传输过程，最终消息必须完整保存并具有明确状态。客户端重连后可以查询消息和任务状态。

## 7.7 多模型接口差异

**风险：** 不同模型提供商的 Function Calling、流式协议和错误结构存在差异。

**处理方式：**

- 统一 ModelGateway 契约。
- 通过提供商适配器处理协议差异。
- 为每个适配器单独编写测试。
- 对不支持的模型能力明确报错。
- 第一版先稳定接入一种模型，不急于支持所有模型。

## 7.8 任务状态与工具执行结果冲突

**风险：** Tool 已执行成功，但 Task 已被取消或超时，迟到结果又覆盖任务终态。

**处理方式：**

- 终态不可变。
- 执行器接收取消信号。
- 结果提交前检查 Task 状态。
- 对迟到结果只记录必要事件，不修改已经终止的 Task。
- 继续复用阶段 2 的状态转换规则。

# 八、上线交付

## 8.1 数据层交付

- 

  完善 Message 状态与错误字段。

- 

  完善 Task 与 Message 的关联。

- 

  完善模型调用和 Tool Call 事件字段。

- 

  完善模型用量记录结构。

- 

  必要的幂等记录结构。

- 

  Alembic 迁移文件。

- 

  必要的索引与约束。

已有字段满足要求时，不要重复新增。

## 8.2 业务层交付

- 

  ModelGateway。

- 

  至少一个真实模型提供商适配器。

- 

  模型请求和响应标准化。

- 

  模型选择服务。

- 

  Context Builder。

- 

  Chat Application Service。

- 

  Function Calling 解析器。

- 

  工具映射和参数校验。

- 

  Task 执行循环。

- 

  Chat SSE。

- 

  Task SSE 集成。

- 

  Chat 幂等。

- 

  模型用量记录。

- 

  错误处理、超时和取消。

## 8.3 API 交付

- 

  Chat API。

- 

  Chat SSE 接口。

- 

  Task SSE 接口。

- 

  必要的模型能力查询接口。

- 

  统一错误码。

- 

  请求幂等处理。

- 

  SSE 事件契约文档。

## 8.4 文档交付

建议维护：

- `docs/architecture/model-gateway.md`
- `docs/architecture/function-calling.md`
- `docs/architecture/chat-execution-flow.md`
- `docs/architecture/chat-and-task-stream.md`
- `docs/api/chat-api.md`
- `docs/api/stream-events.md`
- `docs/testing/stage-5-acceptance.md`

如已有同类文档，直接更新原文件。

## 8.5 本地验收流程

1. 执行数据库迁移。
2. 配置一个可用的模型提供商。
3. 验证 ModelGateway 普通调用。
4. 验证模型流式输出。
5. 创建一个已发布的 AgentVersion。
6. 为其绑定一个已发布的 SkillVersion。
7. 确认该 SkillVersion 绑定有效的 ToolVersion。
8. 发送普通闲聊消息。
9. 验证没有创建 Task。
10. 发送需要调用 Tool 的请求。
11. 验证创建 Task，并绑定正确的 AgentVersion。
12. 验证模型 Tool Call 通过 SkillGateway 执行。
13. 验证 Tool Result 被正确回填。
14. 验证模型生成最终回复。
15. 验证 Task 与 Assistant Message 正确关联。
16. 测试最大轮数、超时、取消和重复请求。
17. 测试 SSE 断线重连。
18. 发布新 AgentVersion，验证旧任务仍然使用原版本。

建议先用测试环境和只读 Tool 验收，再接入真实业务系统。

# 九、阶段总结

## 9.1 本阶段完成了什么

阶段 5 将前四个阶段建立的基础能力串成真正可用的 AI Runtime 执行链路。

阶段 2 提供可靠任务执行，阶段 3 提供 AgentVersion 与会话消息，阶段 4 提供 Skill、Tool 和统一执行网关。本阶段则完成：

- 模型统一调用。
- 上下文组装。
- 普通聊天与 Task 分流。
- Function Calling 解析。
- 多轮模型与工具执行。
- Chat SSE 与 Task SSE。
- 幂等、超时、取消和用量记录。

核心执行链路为：

```
用户消息 → ModelGateway → Task Kernel → Worker → ModelGateway ↔ SkillGateway → 最终回复
```

其中，普通闲聊不需要经过 Task Kernel 的任务创建流程。

## 9.2 本阶段必须保持的架构边界

1. 模型负责理解需求与提出工具调用请求，不拥有实际执行权限。
2. Task Kernel 负责任务生命周期，不由 Chat Service 重写。
3. SkillGateway 是所有 Tool 调用的唯一执行入口。
4. AgentVersion、SkillVersion 和 ToolVersion 必须精确绑定。
5. 普通闲聊不创建 Task。
6. Task 的终态不可被迟到结果覆盖。
7. Chat SSE 和 Task SSE 分离。
8. 数据库中的持久化记录优先于客户端内存中的流式状态。

## 9.3 下一阶段

阶段 6：**MD-Skill 能力完善与落地**。

阶段 4 已建立 MD-Skill 的基本类型和执行路径，阶段 5 又让模型具备了调用 Skill 的能力。阶段 6 将进一步完善：

- SKILL.md 标准和元数据规范。
- Skill 文件扫描与校验。
- 文件与 SkillVersion 的关联。
- 纯指令模式的加载和上下文注入。
- 受控脚本执行模式。
- 文件变更检测与版本更新流程。
- 执行权限和脚本安全限制。
- MD-Skill 的完整测试与示例。

阶段 6 不应重新实现 SkillGateway，而是复用阶段 4 已建立的统一执行入口。

**本阶段最重要的原则：模型可以提出行动建议，但只有经过版本校验、权限检查和 SkillGateway 控制的调用，才能真正执行。**

