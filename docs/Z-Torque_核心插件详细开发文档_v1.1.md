# Z-Torque 核心插件详细开发文档 v1.1

> **文档性质**：核心插件开发规格与落地指南（现状基线 + 目标规格双栏）  
> **适用版本**：Z-Torque 社区版（Community）为主，兼顾企业版扩展方向  
> **上游依据**：  
> - `AI_Runtime_形态确认书_v1.md`（官方 6–8 个插件、暂不开放第三方）  
> - `Z-Torque_内核契约_v1.md`（六件不变量、21 Port、插件五条纪律）  
> - 阶段 0 / 阶段 1 / 阶段 4 / 阶段 5 / 阶段 7 开发文档  
> - 2026-10-20 代码对照分析（`app/builtin_plugins` 实装 vs 文档 v1）  
> **版本说明**：v1.1 在 v1 基础上对齐**已落地实现**，区分 as-is / to-be，修正插件清单与启动策略。  
> **修订记录**：v1.1a（2026-10-20）——G2 扩容为 G2a–G2d（实现类全部留在内核的问题不止 DB_SQL）；新增 G7（specs.py queue.driver 描述与实现不符）；§7.2 Profile 示例对齐实际 manifest 名。  
> v1.1b（2026-10-20）——**G2c 裁决：SkillGateway 本质是内核组件**（统一执行入口、不变量载体），保留内核不迁出；「插件是 Port 实现的物理载体」原则限定于**能力实现**；G1 已修复（探测下沉 `SkillGateway.probe_mcp_http`）；G2a/G2b/G2d 执行迁移。  
> v1.1c（2026-10-20）——里程碑 2/3/5 落地：**rule-based-model-selector 插件**（`model.selector`，pending=false，任务链路消费接线）；**G3 落地**（新增 secret_provider 插件，`secret.provider` pending=false，ModelProvider 支持 `secret://` 注入 + 明文兼容）；**G7 修复**（specs.py queue.driver 描述对齐 InMemory 实现）。已落地插件 4 → 6。  
> **日期**：2026-10-20  

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

解析优先级（强制，与代码一致）：

**显式配置（EXPLICIT）> 插件（PLUGIN）> 内置默认（BUILTIN）> UNAVAILABLE**

同优先级且未显式指定 → 启动失败（`AssemblyError`）。

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

### 1.5 导入与装配边界（强制）

| 允许 | 禁止 |
|------|------|
| `app/assembly.py` 加载并注册 `builtin_plugins` | `app.kernel` 导入 `builtin_plugins` |
| 插件通过 `PortRegistry.register` 提供实现 | `app.api` 直接导入 `builtin_plugins`（含函数内惰性导入） |
| 插件依赖已解析的其他 Port（via requires） | 业务 `modules` / API 层绕过 Registry 使用插件内部类型 |

> **已修复（2026-10-20，G1）**：原 `app/api/routes/connectors.py` 在 MCP 探测路径中运行时导入 `app.builtin_plugins.mcp`，属边界违规；已修复——探测能力下沉为内核 `SkillGateway.probe_mcp_http`（传输实现经装配注入的 `http_connector_factory` 构造，内核不感知插件类型），API 改经 `app.state.skill_gateway` 调用。

---

## 二、现状基线（as-is）与目标形态（to-be）

### 2.1 目录与 Manifest 形态

| 维度 | as-is（当前代码） | to-be（目标） |
|------|-------------------|---------------|
| 目录结构 | 扁平包：`__init__.py`、`manifest.py`、`plugins.py`、`executors.py`、`mcp.py` | 每插件子目录（可选渐进）：`openai_compatible/`、`mcp_connector/` 等 |
| Manifest 载体 | Python dataclass（`PluginManifest`） | 保持 Python 或增加 YAML；字段补齐 |
| Manifest 字段 | name / description / provides / requires / permissions / config_schema | 补齐：displayName、version、official、priority、healthcheck、circuit_breaker；config_schema 真正使用 |
| 加载入口 | 仅 `app/assembly.py` | 不变 |
| 优先级模型 | `PortPriority`: BUILTIN=1 / PLUGIN=2 / EXPLICIT=3 | 与文档语义一致，保持 |

**说明**：v1 推荐的「每插件一目录 + YAML」为**中期整理目标**，不是社区版首期阻塞项。首期以契约与边界正确为优先。

### 2.2 社区版启动策略（pending Port）

与内核契约「必需 Port 缺失 → fail fast」兼容的**渐进策略**（已在 `specs.py` 落地）：

- Port 标记 `pending=True`：启动时**不**因缺失而失败，解析结果为 `NOT_PORTED` / 能力查询展示为不可用。
- Port 标记 `pending=False` 且为 required：缺失则 **fail fast**。
- 当前 `pending=False` 的 Port 共 6 个，与已注册的 6 个内置插件一一对应。

| Port | pending | 说明 |
|------|---------|------|
| secret.provider | false | 已由 SecretProviderPlugin 提供（G3，2026-10-20 落地） |
| model.provider | false | 已由 ModelProviderPlugin 提供 |
| model.selector | false | 已由 RuleBasedModelSelectorPlugin 提供（2026-10-20 落地） |
| skill.executor | false | 已由 SkillExecutorPlugin 提供 |
| connector | false | 已由 McpConnectorPlugin 提供 |
| queue.driver | false | 已由 QueueDriverPlugin 提供 |
| 其余 15 个 | true | 启动豁免；调用时返回 `AI_CAPABILITY_UNAVAILABLE` 或等价 |

文档与代码均以此为准，避免把「未实现」误读为「契约破坏」。

---

## 三、插件清单（对齐后）

### 3.1 已落地插件（as-is，6 个）

| # | 插件名（代码） | 主要 Port | 实现要点 | 文档 v1 对应 |
|---|---------------|-----------|----------|--------------|
| 1 | SecretProviderPlugin | `secret.provider` | 本地 Fernet 加密存储（SECRET_STORE_KEY）；供 `secret://` 引用解密（G3） | 文档 v1 未单列，属补充 |
| 2 | ModelProviderPlugin | `model.provider` | OpenAI 兼容 + Mock 降级；API Key 经 secret.provider 注入 | openai-compatible-model-provider |
| 3 | RuleBasedModelSelectorPlugin | `model.selector` | 快照绑定直通 → 白名单校验 → 默认回退（2026-10-20 落地） | rule-based-model-selector |
| 4 | SkillExecutorPlugin | `skill.executor` | SkillGateway + MOCK/HTTP/DB_QUERY/DB_SQL/MD_SCRIPT/MCP 执行器 | 文档 v1 未单列，属补充 |
| 5 | McpConnectorPlugin | `connector` | MCP Streamable HTTP + Stdio；tools/list、tools/call | mcp-connector |
| 6 | QueueDriverPlugin | `queue.driver` | 进程内 InMemory 队列 | 文档 v1 未单列，属补充 |

### 3.2 目标清单中尚未落地（to-be）

| # | 目标插件 | Port | 优先级 | 备注 |
|---|----------|------|--------|------|
| A | ~~rule-based-model-selector~~ | `model.selector` | ~~P0~~ **已落地**（2026-10-20，见 §6.1） | |
| B | local-memory | `memory.working` + `memory.short` | P0 | 会话上下文 + 短记忆；当前 pending |
| C | local-approval-queue | `approval.provider` | P1 | 人工确认队列；当前 pending |
| D | http-rest 作为 connector 实现（可选） | `connector` 或保持 Tool 执行器 | P1 | REST 能力已在 HttpExecutor（skill.executor 下）；是否提升为独立 connector 实现待定 |

### 3.3 能力拆分说明：HTTP REST

| 能力 | 落点（as-is） | 说明 |
|------|---------------|------|
| REST Tool 执行 | `HttpExecutor`（挂在 skill.executor） | GET-only、https、SSRF 防护、白名单、响应截断 |
| MCP over HTTP | `StreamableHttpMcpConnector`（connector Port） | MCP 协议，不是通用 REST 业务连接器 |
| 通用 REST Connector Port | 未单独实现 | 若产品需要「连接配置 → 注册 Tool」的完整 Connector 管理，在阶段 7 能力上增强，不必强行与 MCP 挤在同一插件 |

### 3.4 官方规划总量

仍控制在 **官方 6–8 个插件**（形态确认书）。社区版首期以 6 个已落地 + 上述 to-be（B/C）为主；企业向（knowledge、sandbox、SSO、notify 增强等）不占用社区版必做名额。

---

## 四、通用开发规范（契约层，持续有效）

### 4.1 Manifest 规范

**最低字段（as-is 已满足）**：

```python
# 概念示意，与 app/builtin_plugins/manifest.py 对齐
apiVersion: "zsoftym.ai/plugin.v1"   # PLUGIN_API_VERSION
name: str
description: str
provides: list[str]                  # Port 名
requires: list[str]                  # 依赖 Port
permissions: list[str]               # 权限码（当前为字符串码）
config_schema: optional
```

**目标补齐字段（to-be）**：

```yaml
metadata:
  displayName: "..."
  version: "1.0.0"
  official: true
spec:
  priority: 100                      # 或继续仅用注册时 PortPriority
  healthcheck:
    type: port_method
    method: health
  circuit_breaker:
    failure_threshold: 5
    recovery_timeout_seconds: 60
```

- 未知 Port → 注册失败（`validate_manifest` + Registry 双重校验）。
- `requires` 在装配阶段解析，未满足则失败。

### 4.2 权限模型说明

| 层级 | as-is | to-be |
|------|-------|-------|
| 声明粒度 | 权限码字符串（如 `skill.execute`、`tool.execute`、`db.query`） | 可演进为结构化（network / secrets / files / process），与内核 PEP 对齐 |
| 强制点 | Gateway / 执行器内校验 | 保持；未声明行为拒绝 |

二者可并存：权限码用于业务授权，结构化声明用于运行时沙箱策略。

### 4.3 配置与密钥

| 项 | as-is | to-be |
|----|-------|-------|
| 模型 API Key | 支持 `secret://` 引用（经 secret.provider 解密）与明文 env 双形态（G3 已落地，明文为存量兼容） | 存量部署逐步迁移到 `secret://` 引用 |
| MCP / Connector 密钥 | 支持 `secret://` 前缀解析 | 保持并推广到所有插件配置 |
| 插件级 config_schema | 字段存在，插件侧基本未用 | 各插件逐步声明并校验 |

### 4.4 测试要求

| 类型 | 要求 |
|------|------|
| 单元测试 | 实现类核心逻辑、配置解析、权限与安全边界 |
| 集成测试 | 与 ModelGateway / SkillGateway / 装配流程联调 |
| 负面测试 | 缺权限、超时、熔断、错误码、SSRF/注入拒绝 |
| 健康检查 | 具备 health 时覆盖；暂无则在能力查询中体现状态 |

**as-is**：已有 `test_mcp_http.py`、`test_mcp_connectors.py`、`test_port_registry.py`、`test_stage7_connectors.py` 等，按功能而非按插件目录组织——可接受；整理目录时再映射。

---

## 五、已落地插件规格（as-is 基线）

### 5.1 ModelProviderPlugin（`model.provider`）

**状态**：已落地（pending=false）

**能力**：

- OpenAI Chat Completions 兼容协议
- 无有效配置时显式降级 Mock，保证 CI / 离线可跑
- 由装配注册为 `model.provider` 实现
- **API Key 双形态（G3，已落地 2026-10-20）**：`secret://` 引用经 secret.provider 装配期解密注入；明文 env 配置向后兼容；manifest `requires: [secret.provider]`

**已知差距（to-be）**：

- [x] **API Key 改为经 `secret.provider` 注入，manifest `requires` 声明 `secret.provider`（G3，已落地 2026-10-20）**
- [ ] 流式取消、标准错误码、用量上报与 ModelGateway / Interceptor 对齐（若尚未完全覆盖）
- [ ] 插件级 config_schema（base_url、timeout、max_retries 等）
- [x] **实现类迁出内核（G2b，已落地 2026-10-20）**：`OpenAICompatibleGateway` / `MockModelGateway` 迁入 `app/builtin_plugins/model_gateway.py`，kernel 仅留 contracts（协议与 DTO）

**安全与约束**：不在插件内绕过审计；调用链仍经 ModelGateway。

---

### 5.2 SkillExecutorPlugin（`skill.executor`）

**状态**：已落地（pending=false）

**能力**：

- 提供 SkillGateway 及执行器集合：
  - MOCK
  - HTTP（`HttpExecutor`：仅 GET、强制 https、DNS 解析后拒绝内网 IP、host 白名单、响应大小上限、禁用重定向）
  - DB_QUERY（sqlglot 解析、单条 SELECT、表/列白名单、行数上限）
  - DB_SQL（见下方迁移说明）
  - MD_SCRIPT（当前占位：返回 `AI_CAPABILITY_UNAVAILABLE`）
  - MCP（执行路径与 connector / stdio_registry 协同；占位与真调用需保持一致）

**已知差距（to-be）**：

- [x] **实现类迁出内核（G2a，已落地 2026-10-20）**：`ConnectorDbExecutor`（DB_SQL）迁入 `app/builtin_plugins/db_executors.py`；kernel 仅保留校验/探测契约策略（`validate_sql_statement` / `probe_database`）
- [ ] MD_SCRIPT 受控运行时就绪后取消占位
- [ ] MCP 执行器与 connector 路径统一，避免「占位恒失败」与真实调用分叉不清

**定位澄清（SkillGateway，原 G2c 迁移项已裁决撤销）**：`SkillGateway` 本质是**内核组件**——统一执行入口、执行侧不变量（审计、权限、确认暂停、结果标准化）的载体；当前经 `SkillExecutorPlugin` 注册到 `skill.executor` Port **仅为装配形式**，不改变其内核属性。在实现类迁出内核之前，不得把它按「可随便替换的插件实现」对待：内核（`task/runtime` 等）与 `app/services` 直接依赖该类属于**合法的内核内依赖**，不构成 G1 式边界违规。任何未来的迁出提案必须走内核契约变更流程评审。

**安全亮点（保持）**：HttpExecutor SSRF 防护、DB_QUERY 白名单与单语句限制——作为基线写入验收标准。

---

### 5.3 McpConnectorPlugin（`connector`）

**状态**：已落地（pending=false）

**能力**：

- MCP JSON-RPC 2.0；传输：Streamable HTTP + Stdio
- `tools/list`、`tools/call`；能力发现
- Stdio：可执行文件须为绝对路径且真实存在；env 防注入（拒绝 `\x00`、非法 `=`）；超时 terminate→kill；stderr 排空
- HTTP：回环可用 http；外网强制 https + allowed_hosts；响应大小限制
- 注册侧支持 env 中 `secret://` 解密
- **不**自动把远端全部工具开放给所有用户（需管理端确认/注册流程）

**已知差距（to-be）**：

- [ ] manifest 补齐 healthcheck / 熔断声明（若产品需要可观测与自动摘除）
- [ ] 与 API 层解耦：探测逻辑不得让 `app.api` 直接依赖 `builtin_plugins.mcp` 类型

---

### 5.4 QueueDriverPlugin（`queue.driver`）

**状态**：已落地（pending=false）

**能力**：

- 进程内 InMemory 队列，满足社区版单机 Outbox / Worker 需求
- 无外部 MQ 依赖

**已知差距（to-be）**：

- [ ] 若企业版引入 Redis/等，以**新插件**实现同一 Port，不改内核契约
- [ ] 可选：持久化与重启恢复策略说明写入运维文档
- [x] **实现类迁出内核（G2d，已落地 2026-10-20）**：`InMemoryQueueDriver` 迁入 `app/builtin_plugins/queue_driver.py`；kernel 仅留 `QueueDriver` ABC 契约，`task/runtime` 的兜底默认值移除，队列改为装配根显式注入（构造签名 `queue` 必填关键字）
- [x] **描述与实现对齐（G7，已修复 2026-10-20）**：`specs.py` 中 queue.driver 的 community_default 已改为「进程内 InMemory 队列（Outbox 补偿恢复）」，能力查询展示与真实实现一致

---

## 六、待落地插件规格（to-be）

### 6.1 rule-based-model-selector（`model.selector`）

**优先级**：P0  
**状态**：**已落地（2026-10-20，pending=false）**

**功能范围**：

- 根据 AgentVersion 冻结的模型绑定选择 model + endpoint
- 支持默认回退；不因全局配置变更改变已创建 Task 的选择结果（快照语义）
- 由 ModelGateway 在调用前查询

**落地形态（as-is）**：

- 内核契约：`app/kernel/ports/model_selector.py`（`ModelSelector` ABC + `ModelSelectionRequest` / `ModelSelection` DTO + `resolve_model` 消费便捷函数）
- 插件实现：`app/builtin_plugins/model_selector.py`（`RuleBasedModelSelectorPlugin`，manifest `requires: [model.provider]`）
- 规则：① 快照绑定直通（`config_snapshot.model` 非空即用）→ ② 白名单校验（`settings.model_allowed_names` 非空时绑定须命中，未命中回退默认并标记 `whitelist_fallback`）→ ③ 默认回退（`settings.model_name`）
- 消费接线：TaskRuntime 任务执行与审批恢复两处 ModelRequest 构造、子智能体委派（`AgentExecutionLoop._run_child_agent`）均经 selector 解析；直答/流式分流路径暂用网关默认模型（后续接入）
- 单元测试：`tests/unit/test_model_selector.py`（绑定优先于默认、快照不被热改、白名单、装配解析）

---

### 6.2 local-memory（`memory.working` + `memory.short`）

**优先级**：P0  
**必需性**：working/short 为 required（落地后 pending 置 false）；long 保持 optional

**功能范围**：

- Working：请求级上下文组装（当前轮次、系统提示、相关 Skill 摘要）
- Short：会话级短记忆落库，按 `conversation_id` + `user_id` 隔离
- 裁剪策略：轮次上限 / token 预算

**交付要点**：

- 读写均带 SecurityContext
- 不实现 long memory（保持 UNAVAILABLE 或简单 KV 占位并文档声明）
- 与阶段 9 会话模型对齐

---

### 6.3 local-approval-queue（`approval.provider`）

**优先级**：P1  
**必需性**：optional

**功能范围**：

- Tool/Skill 需人工确认时创建审批单并挂起 Task
- 批准/拒绝后写回状态机，触发恢复
- 审批超时可配置；记录进入审计流

**不在范围**：多级审批、外部审批系统（后续官方插件）

**交付要点**：

- 与阶段 2/8 Task 状态机、Outbox 协同
- 控制台或 API 提供 approve/reject

---

## 七、装配与启用

### 7.1 加载流程（as-is，保持）

1. `app/assembly.py` 为唯一加载方  
2. 收集内置插件 → 校验 manifest → `PortRegistry.register`  
3. 解析 requires → 必需且非 pending 的 Port 必须可用  
4. 能力查询：`GET /api/z-torque/capabilities`（基于 `PortRegistry.status()`，不暴露密钥）

### 7.2 Profile 定义（as-is，以实际 manifest 名为准）

实际 Profile 为 manifest 名（小写字符串）元组，assembly 按 `manifest.name` 匹配加载
（见 `app/builtin_plugins/plugins.py` 的 `PROFILE_COMMUNITY` 与 `app/assembly.py`）：

```python
PROFILE_COMMUNITY = ("secret_provider", "model_provider", "rule_based_model_selector", "skill_executors", "mcp_connector", "queue_driver")
```

> 顺序即 contribute 顺序：`secret_provider` 必须先于 `model_provider`（G3 密钥注入在 contribute 阶段经 `AssemblyContext.objects` 共享对象完成）。

概念等价的 YAML 视图（注意：名称是 manifest.name，**不是插件类名**）：

```yaml
profile: community
plugins:
  - name: secret_provider
  - name: model_provider
  - name: rule_based_model_selector
  - name: skill_executors
  - name: mcp_connector
  - name: queue_driver
  # to-be:
  # - name: local_memory
  # - name: local_approval_queue
```

### 7.3 放入项目的方式（再次明确）

- 实现放入 `app/builtin_plugins/`（当前扁平；未来可分子目录）
- 提供合法 manifest 并由 assembly 注册
- **不是**前端插件市场下载安装
- 控制台管理的是已装配的 Agent / Skill / Tool / Connector 等资源对象

---

## 八、已知问题与修复优先级

| ID | 问题 | 严重度 | 建议动作 |
|----|------|--------|----------|
| G1 | `app/api/routes/connectors.py` 运行时导入 `builtin_plugins.mcp` | ~~P0~~ **已修复** | 探测下沉 `SkillGateway.probe_mcp_http`（工厂注入），API 经 `app.state` 调用 |
| G2a | `ConnectorDbExecutor`（DB_SQL）仍在 `app.kernel.db_executor` | ~~P1~~ **已落地** | 已迁入 `app/builtin_plugins/db_executors.py`；kernel 留校验/探测契约策略 |
| G2b | `MockModelGateway` / `OpenAICompatibleGateway` 仍在 `app.kernel.model_gateway` | ~~P1~~ **已落地** | 已迁入 `app/builtin_plugins/model_gateway.py`；kernel 仅留 contracts |
| G2c | SkillGateway 定位（原计划迁出内核） | **已裁决** | **保留内核**：本质是统一执行入口与不变量载体，插件注册仅为装配形式；未来迁出须走内核契约变更流程 |
| G2d | `InMemoryQueueDriver` 仍在 `app.kernel.ports.queue_driver`（契约与实现同文件） | ~~P1~~ **已落地** | 已迁入 `app/builtin_plugins/queue_driver.py`；runtime 兜底默认移除，队列改为装配根显式注入 |
| G3 | ModelProvider 直读 `settings.model_api_key` | ~~P1~~ **已落地** | 新增 SecretProviderPlugin（`secret.provider` pending=false）；ModelProvider 支持 `secret://` 装配期解密注入，明文 env 向后兼容；manifest requires 声明 |
| G4 | manifest 缺少 healthcheck / circuit_breaker；config_schema 未用 | P2 | 按插件渐进补齐 |
| G5 | MD_SCRIPT / 部分 MCP 执行器占位与真路径不一致风险 | P2 | 统一执行入口与错误语义 |
| G6 | 测试未按插件目录组织 | P3 | 不阻塞；整理时映射现有 `test_mcp_*` 等即可 |
| G7 | `specs.py` queue.driver 的 community_default 描述（「内置 DB Outbox + Worker」）与实际 InMemory 实现不符 | ~~P2~~ **已修复** | specs.py 描述已对齐实际（「进程内 InMemory 队列（Outbox 补偿恢复）」） |

> **G2 系列说明**：「插件是 Port 实现的物理载体」原则适用于**能力实现**（模型适配器、工具执行器、连接器、队列驱动）；
> `SkillGateway` 除外——它是内核不变量载体（统一执行入口），保留内核（G2c 已裁决，见 §5.2 定位澄清）。
> G2a/G2b/G2d 已于 2026-10-20 作为一次统一迁移落地：能力实现全部位于 `builtin_plugins`
> （`db_executors.py` / `model_gateway.py` / `queue_driver.py`），内核仅留契约与策略函数；
> 迁移中同步消除了内核对实现的内部引用（`task/runtime` 队列兜底默认值移除，改为装配根显式注入）。

---

## 九、开发优先级与里程碑

| 顺序 | 项 | 类型 | 依赖 |
|------|----|------|------|
| 1 | ~~修复 G1 导入边界~~ | 缺陷 | **已落地**（探测下沉 `SkillGateway.probe_mcp_http`） |
| 2 | ~~rule-based-model-selector 落地~~ | 新插件 P0 | **已落地**（2026-10-20：内核契约 + 插件 + 任务链路消费接线 + 单测；见 §6.1） |
| 3 | ~~ModelProvider 走 secret（G3）~~ | 缺陷/增强 | **已落地**（2026-10-20：secret_provider 插件 + `secret://` 注入；见 §5.1） |
| 4 | ~~能力实现统一迁入 builtin_plugins（G2a/G2b/G2d）~~ | 重构 | **已落地**（kernel 仅留契约；SkillGateway 依 G2c 裁决保留内核） |
| 5 | ~~specs.py queue.driver 描述与实现对齐（G7）~~ | 文档修正 | **已修复**（2026-10-20） |
| 6 | local-memory 落地 | 新插件 P0 | 会话与 Message 模型（阶段 9） |
| 7 | local-approval-queue 落地 | 新插件 P1 | Task 状态机（阶段 8） |
| 8 | manifest 增强与熔断/健康检查 | 增强 P2 | 可观测需求 |

---

## 十、交付检查清单

### 10.1 每个插件（已有或新增）

- [ ] 合法 manifest（apiVersion、provides、requires、permissions）
- [ ] 只实现已定义 Port
- [ ] 经 assembly 注册，无 kernel/api 反向依赖插件内部模块
- [ ] 密钥不进明文配置（优先 secret:// 或 secret.provider）
- [ ] 单元测试 + 与 Gateway/装配的集成测试
- [ ] 错误码使用阶段 0 主表
- [ ] 安全边界可测（网络/SQL/进程视插件类型而定）

### 10.2 仓库级

- [x] `pending=False` 的 required Port 均有实现（当前 6 个：secret.provider / model.provider / model.selector / skill.executor / connector / queue.driver）
- [ ] `GET /api/z-torque/capabilities` 与 Registry 一致
- [x] 无 `app.api` / `app.kernel` → `app.builtin_plugins` 的运行时导入（G1 已清零）
- [x] 能力实现类全部位于 `builtin_plugins`，内核仅留契约与不变量载体（G2a/G2b/G2d 已清零；SkillGateway 依 G2c 裁决保留内核）
- [x] `specs.py` 中各 Port 的 community_default 描述与实际落地一致（G7 已清零）

---

## 十一、与 v1 的差异摘要

| 项 | v1 | v1.1 |
|----|----|------|
| 插件清单 | 理想 6 个 | 已落地 4 个 + to-be 3 个，并承认 skill.executor / queue.driver |
| 目录结构 | 强制分目录+YAML 观感 | 承认扁平 Python manifest 为 as-is；分目录为 to-be |
| 启动策略 | 仅写 fail fast | 补充 pending 豁免策略，与 specs.py 一致 |
| HTTP | 独立 http-rest-connector | 拆分为 Tool 执行器（已有）与可选 Connector 增强 |
| 问题清单 | 无 | 增加 §八 已知问题与优先级 |
| 密钥 | 要求走 secret.provider | 已落地：secret_provider 插件 + ModelProvider `secret://` 注入（G3）；推广到其余插件配置为后续项 |

---

## 十二、后续演进（非首期）

- knowledge-provider（`knowledge.provider` + `vector.store`）
- sandbox 插件（`sandbox.driver`，企业版可 sidecar）
- SSO / OIDC 身份插件
- notify.channel 增强（邮件、Webhook）
- observability 导出插件
- 企业版插件签名与离线 license

新增 Port 必须走内核契约变更流程（至少两个真实实现 + 定版本号）。

---

**版本**：v1.1（2026-10-20）  
**关联**：形态确认书 v1、内核契约 v1、核心插件文档 v1、阶段 0/1/4/5/7/9、2026-10-20 代码对照分析  
**维护**：实现与清单变更时同步更新「as-is」表与 §八；契约变更走内核版本流程。
