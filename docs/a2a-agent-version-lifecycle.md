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

当前代码已统一站内 Agent 的工具入口和动态候选发现（包括普通 Agent 的下一跳委派）；现有 A2A Gateway 插件仍保留历史 `/a2a/invoke` 自定义 envelope 以兼容既有 Peer。它尚不能据此宣称已完整覆盖 A2A 官方 JSON-RPC/HTTP binding。后续应以官方 Agent Card、SendMessage、GetTask、CancelTask、SubscribeToTask 和标准 Task/Artifact 数据结构替换旧 envelope，并通过互操作测试后再移除兼容适配器。

## AgentVersion 生命周期

- Draft：可编辑配置与 Skill 绑定。
- Published：不可变，可作为新任务默认目标版本。
- Archived：停止新调用；历史 Task 仍保留原版本引用。
- 回滚：通过更新 Agent 的当前发布版本指针实现，不修改快照内容。归档版本重新激活前，必须确认其固定 SkillVersion / ToolVersion 依赖仍可用。
- 发布快照 hash 覆盖 Agent 配置、SkillVersion ID 和 ToolVersion ID，避免仅对 Prompt/模型 JSON 做哈希而遗漏执行依赖。
- 归档禁止归档当前发布版本或仍被非终态 Task 引用的版本；不再检查已废弃的静态父子白名单引用。

## 当前边界与待完成项

- 站内 A2A 的本地执行目前仍复用父 Task 的执行循环；统一了入口，但尚未把每次委派都转为独立 Child Task。
- 子 Agent 的人工审批仍通过 fail-closed 策略排除需确认的工具；尚未实现父 Task 等待子 Task、子 Task checkpoint 恢复后父 Task 继续的完整调度链。
- 远程 Peer 仍是旧自定义 A2A envelope，需完成标准 A2A JSON-RPC/HTTP binding 和 Agent Card 互操作后，才能宣称完整标准协议兼容。
- 以上边界不得在 UI、文档或产品介绍中描述为已经完成。
