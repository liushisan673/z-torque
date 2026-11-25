# A2A 统一调度与 Agent 版本生命周期

## 目标架构

Z-Torque 使用 A2A 作为 Agent-to-Agent 发现与委派的唯一入口，不再使用 AgentVersion 静态父子关系作为可调用能力来源。

- Agent 是稳定身份；AgentVersion 是不可变执行配置快照。
- Agent 不需要配置父 Agent、子 Agent 或子版本白名单。
- 站内 Discovery 按租户、Agent 状态和当前用户可见范围发现候选 Agent。
- Discovery 只表示可发现；执行入口必须再次校验目标 Agent 当前发布版本、状态和用户可见性。
- 所有 Agent（包括用户默认 Agent、系统默认 Agent）通过同一 `route_specialist` 调度入口委派。
- 新委派默认解析到目标 Agent 的 `current_published_version_id`；目标版本在执行开始时固定，后续发布不改变已有任务的版本引用。
- 原始用户 SecurityContext 沿调用链传递；目标 Agent 的工具、Skill、数据和操作权限仍按原始用户实时校验。

## 兼容策略

`zr_agent_version` 中历史字段 `delegation_enabled`、`allowed_child_agent_versions`、`child_necessity_policy` 和 `delegation_capability_version` 暂不删除，不执行破坏性数据库迁移。

- 新建版本不再继承旧白名单配置。
- 发布不再验证或依赖旧白名单。
- 新工具映射不再生成 `delegate_*` 工具。
- 旧 API 请求字段标记为 deprecated，服务层忽略其写入；响应暂保留字段以兼容旧客户端。
- 版本归档不再因旧 AgentVersion 白名单引用而被阻断，因为运行时发现不再依赖这些静态引用。

## 统一调度行为

1. 当前 Agent 获得一个结构化 A2A 调度工具。
2. Discovery 只返回当前用户可见且处于 ACTIVE、具有当前已发布版本的 Agent。
3. 模型提交目标 AgentVersion ID 与任务描述。
4. 执行侧再次检查目标仍是该 Agent 的当前发布版本且对原始用户可见。
5. 本地执行使用 in-process Agent runtime；远程 Agent 使用配置的 A2A Peer 传输适配器。两者应共享发现、授权、任务关联、超时、循环/深度限制和审计契约。

站内 A2A 委派已通过统一入口创建独立 Child Task：父任务保存模型上下文 checkpoint 并进入 `WAITING_CHILD`，随即释放 Worker 执行槽；子任务由正常 Task Worker 调度，使用原始用户身份执行。子任务进入人工审批时仅挂起子任务，父任务继续等待；子任务进入终态后，系统通过父子 Task 关系唤醒父任务，回填子任务结果并从 checkpoint 继续模型执行。父任务取消会级联取消未终态的后代任务，进程恢复时也会检查已完成但尚未通知父任务的子任务。

这完成的是站内本地 Agent 的独立任务生命周期，不等于远程线协议已完整标准化。现有 A2A Gateway 插件仍保留历史 `/a2a/invoke` 自定义 envelope 以兼容既有 Peer，尚不能宣称完整覆盖 A2A 官方 JSON-RPC/HTTP binding。后续应以官方 Agent Card、SendMessage、GetTask、CancelTask、SubscribeToTask 和标准 Task/Artifact 数据结构替换旧 envelope，并通过互操作测试后再移除兼容适配器。

## AgentVersion 生命周期

- Draft：可编辑配置与 Skill 绑定。
- Published：不可变，可作为新任务默认目标版本。
- Archived：停止新调用；历史 Task 仍保留原版本引用。
- 回滚：通过更新 Agent 的当前发布版本指针实现，不修改快照内容。归档版本重新激活前，必须确认其固定 SkillVersion / ToolVersion 依赖仍可用。
- 发布快照 hash 覆盖 Agent 配置、SkillVersion ID 和 ToolVersion ID，避免仅对 Prompt/模型 JSON 做哈希而遗漏执行依赖。
- 归档禁止归档当前发布版本或仍被非终态 Task 引用的版本；不再检查已废弃的静态父子白名单引用。

## 当前边界与待完成项

- 站内本地 A2A 已具备独立 Child Task、父任务 checkpoint、等待/恢复、审批等待与终态通知；仍需补充端到端模型工具调用测试、并发/超时/崩溃恢复压力测试。
- 远程 Peer 仍是旧自定义 A2A envelope，需完成标准 A2A JSON-RPC/HTTP binding 和 Agent Card 互操作后，才能宣称完整标准协议兼容。
- 以上边界不得在 UI、文档或产品介绍中描述为已经完成。


## 独立 Child Task 生命周期（2026-11-25）

- 模型通过统一 `route_specialist` 工具提出站内委派时，执行循环不再直接递归执行子 Agent，而是返回委派意图与序列化消息上下文。
- TaskRuntime 重新校验原始用户的 `a2a.invoke` 权限、目标 Agent 当前发布版本和用户可见性，再创建 Child Task。
- Child Task 使用原始用户的 tenant/user SecurityContext；其工具映射、Skill/Tool 权限检查继续以该用户为准。
- Parent Task 保存 `TaskCheckpoint(status=WAITING_CHILD)`，状态转为 `WAITING_CHILD` 并清空 lease 字段。Worker 返回，释放并发执行槽。
- Child Task 通过正常 Outbox/Queue/Worker 执行；其状态、工具事件、审批记录和审计独立归属于 Child Task。
- Child Task 进入 `WAITING_HUMAN` 时，Parent Task 保持 `WAITING_CHILD`。审批恢复只恢复 Child Task；Child Task 终态后通过父子 Task 关联把 Parent Task 重新入队。
- Parent Task 从 checkpoint 恢复，将 Child Task 的结构化终态/结果作为 A2A tool result 回填，再继续模型推理。
- Parent Task 取消会级联取消未终态后代；超时与进程恢复路径也识别 `WAITING_CHILD`。
- 委派深度默认 3 层，可配置为 1–5 层；单个模型执行轮次仍限制一次 A2A 派发，避免重复委派。
- A2A Invocation 在 Child Task 创建时再次授权，Discovery 候选列表不构成授权。


## Remote A2A wire protocol target

The remote interoperability target is the official A2A v1.0 JSON-RPC 2.0 binding over HTTPS, not the repository's historical `a2a.invoke` envelope.

- Publish an Agent Card at the well-known Agent Card URI. The card declares the Agent's `supportedInterfaces`, including interface URL, protocol binding and protocol version, plus supported capabilities and skills.
- Use the standard JSON-RPC methods `SendMessage`, `GetTask`, `CancelTask` and `SubscribeToTask`; use `SendStreamingMessage` when streaming is advertised.
- Send standard A2A Message/Part objects and return exactly one of a Message or Task from SendMessage. Long-running calls must expose Task state and Artifacts rather than inventing a proprietary response envelope.
- Discover the peer's Agent Card and choose a declared interface; do not hardcode `/api/z-torque/a2a/invoke` as the remote protocol endpoint.
- Validate JSON-RPC request IDs, method names, params, payload size, response size, timeouts, cancellation and Task state transitions.
- Authentication must follow the peer's declared security schemes and configured trust. Never trust a naked `X-User-Id` header as identity, and never forward a user's bearer token to a peer unless the trust relationship explicitly authorizes that exact token audience.
- Keep the current custom `a2a.invoke` envelope only as a legacy compatibility adapter during migration. It must not be described as A2A v1.0 JSON-RPC interoperability and should be removed after supported peers have migrated.

The local in-process adapter and remote JSON-RPC adapter must expose the same internal delegation contract: target identity, original caller context, child-task lifecycle, deadline, cancellation, structured result, and audit correlation.
