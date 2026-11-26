# Z-Torque 核心插件详细开发文档 v1.2

> **文档性质**：核心插件开发规格与落地指南（现状基线 as-is + 目标规格 to-be 双栏）
> **适用版本**：Z-Torque 社区版（Community）为主，兼顾企业版扩展方向
> **上游依据**：
> - `AI_Runtime_形态确认书_v1.md`（官方专有插件 6–8 个、暂不开放第三方）
> - `Z-Torque_内核契约_v1.md`（六件不变量、21 Port、插件五条纪律、三条红线）
> - 阶段 0 / 1 / 4 / 5 / 7 / 9 开发文档
> - 2026-10-22 代码对照：`app/builtin_plugins`、`app/kernel/ports`、`app/assembly.py`
> **版本说明**：v1.2 在 v1.1 基础上做**基线刷新**——21 个 Port 已全量收口（19 个内置插件 + 2 个可选 Port 显式 UNAVAILABLE）。v1.1 中「已落地 6 个 / 15 个 pending」的表述已整体失效；本版同时修正 Manifest 规范、权限强制点、装配流程与待落地清单。
> **修订记录**：
> - v1.1a–v1.1d（2026-10-20）——G1/G3/G7 修复、G2a/G2b/G2d 迁移落地、G2c 裁决（SkillGateway 属内核）。
> - **v1.2（2026-10-22）**——P2 收口后基线重写：插件 6 → **19**，Module **9** 个，`pending` 全量置 false；新增 §3.2 模块装配矩阵、§4.5 新插件接入步骤；§六 待落地改为沙箱硬化 / 审计导出 / A2A / 知识库；§八 问题清单按代码实际能力重排。
> **日期**：2026-10-22

---

## 一、总览与原则

### 1.1 产品定位与术语澄清

Z-Torque = **一份开源内核 + 官方插件 + 两档授权 + 三套装配预设**。

| 术语 | 含义 | 数量约束 |
|------|------|----------|
| **内置插件（Builtin Plugin）** | 社区版随内核分发的 Port 实现，位于 `app/builtin_plugins/`，随 `PROFILE_COMMUNITY` 装配 | 当前 **19 个**（= 有实现的 19 个 Port） |
| **官方专有插件（Official Plugin）** | 企业版付费解锁的合规 / 连接 / 规模类插件（签名 + 离线 license） | 形态确认书约束 **6–8 个** |
| **第三方插件** | 外部开发者插件（`assemble(extra_plugins=...)` 已预留，默认 PLUGIN 优先级） | **暂不开放** |

> **v1.1 歧义已修正**：v1.1 把「官方 6–8 个插件」当成社区版内置插件总量，账面永远对不上。二者不同层：**19 个内置插件是 Port 实现载体**，**6–8 个官方插件是企业版商业增量**。

### 1.2 插件在架构中的位置

```
Profile（装配预设：社区 / 企业）
    ↓
Module（能力域：声明构成插件 + 所需 Port）
    ↓
Plugin（Port 实现 + manifest 契约）  ← 本文档聚焦此处
    ↓
Port（21 个，锁定，内核唯一边界）
```

解析优先级（`PortPriority`，强制）：**EXPLICIT(3) > PLUGIN(2) > BUILTIN(1) > UNAVAILABLE**。
同优先级存在多个候选且未显式指定 → 启动失败（`AssemblyError`），不允许随机选择。

### 1.3 插件五条纪律（不可违反）

| # | 纪律 | 代码落点（as-is） |
|---|------|-------------------|
| 1 | manifest 唯一入口：`apiVersion` + `provides/requires/permissions/config_schema` | `PluginManifest`（`manifest.py:28`）、`validate_manifest`（`:133`） |
| 2 | 只实现内核已定义 Port，不得自定义新 Port | `validate_manifest` 对未知 Port 抛错；21 Port 由 `specs.py:67` 断言锁定 |
| 3 | 只经内核 SDK 读写状态，禁止直连内核表 | `PluginSdk`（`manifest.py:39`）：`settings/register_port/publish/shared/engine` |
| 4 | 权限声明式：未声明的行为内核直接拒绝 | **仅 `db.query` 有强制点**（`PluginSdk.engine`，`manifest.py:75`）；网络/文件/进程/密钥尚无运行时拒绝（G8） |
| 5 | 插件级熔断与配额强制 | `PluginGuardRegistry`（`plugin_guard.py:82`）；当前接线 `model.provider` 全量 + `skill.executor` 执行器分派（G10） |

### 1.4 三条红线（插件同样遵守）

- **审计不可关闭**：插件不得提供"关闭审计写入"的配置或代码路径（最多 UI 不展示）。
- **快照不可热改**：不得修改已发布 AgentVersion 的冻结配置。
- **身份不可绕过**：不得以任意身份执行、不得跳过 PEP 裁决点。

### 1.5 导入与装配边界（强制）

| 允许 | 禁止 |
|------|------|
| `app/assembly.py` 加载并注册 `builtin_plugins` | `app.kernel` 导入 `app.builtin_plugins` |
| 插件经 `PluginSdk.register_port` 提供实现 | `app.api` 直接导入 `builtin_plugins`（含函数内惰性导入） |
| 插件依赖已解析的其他 Port（via `requires`） | 业务层绕过 Registry 使用插件内部类型 |

> 历史违规 G1（`routes/connectors.py` 运行时导入 `builtin_plugins.mcp`）已于 2026-10-20 修复：MCP 探测下沉为 `SkillGateway.probe_mcp_http`，传输实现经装配注入的工厂构造。

---

## 二、现状基线（as-is）

### 2.1 目录与 Manifest 形态

| 维度 | as-is（2026-10-22 代码） | to-be（目标） |
|------|--------------------------|---------------|
| 目录结构 | 扁平包 `app/builtin_plugins/`：`manifest.py`（契约与 SDK）、`plugins.py`（模型/执行/连接/队列插件）、`modules.py`（Module + Profile）、各 Port 实现各一文件 | 逐步按能力域分子目录（非阻塞） |
| Manifest 载体 | Python `@dataclass(frozen=True) PluginManifest` | 保持 Python；可选增加 YAML 导出视图 |
| Manifest 字段 | `name/description/provides/requires/permissions/api_version/version/config_schema` | 补 `displayName/healthcheck/circuit_breaker`（G4） |
| 加载入口 | 仅 `app/assembly.py` | 不变 |
| 优先级 | BUILTIN=1 / PLUGIN=2 / EXPLICIT=3；内置默认 BUILTIN，`extra_plugins` 默认 PLUGIN | 不变（manifest 不声明 priority，G14） |
| 健康检查 | 无（无 `health` 方法论） | 可选 `healthcheck` + capabilities 上报（G9） |

**实现文件分布（as-is）**：

```
app/builtin_plugins/
├── manifest.py            # PluginManifest / PluginSdk / ModuleSpec / validate_*
├── modules.py             # MODULE_REGISTRY（9 个 Module）+ PROFILE_COMMUNITY
├── plugins.py             # BUILTIN_PLUGINS（19 个）+ 模型/执行/连接/队列插件
├── model_gateway.py       # OpenAICompatibleGateway / MockModelGateway
├── model_selector.py      # RuleBasedModelSelectorPlugin
├── executors.py           # MOCK / HTTP / DB_QUERY / MD_SCRIPT(占位) / MCP
├── db_executors.py        # DB_SQL（ConnectorDbExecutor）
├── mcp.py                 # Streamable HTTP / Stdio 传输 + StdioServerRegistry
├── secret_provider.py     # Fernet 本地加密
├── identity_provider.py / authz_provider.py / policy_source.py
├── approval_provider.py / notify_channel.py
├── sandbox_driver.py      # 进程内受限执行（实现体在内核 md_skill_runtime）
├── interceptor.py         # 审计拦截链（实现体在内核 audit/service）
├── observability_sink.py  # 结构化日志出口
├── object_store.py        # 本地文件系统（sha256 + 路径逃逸防护）
├── working_memory.py / short_memory.py / long_memory.py
└── queue_driver.py / lock_provider.py
```

### 2.2 Port 收口状态（21/21）

`specs.py` 是 Port 唯一权威描述源（`PORT_SPECS` + `assert len == 21`）。**当前 21 个 Port 全部 `pending=False`**；`pending` 已不代表"未实现"，仅作历史迁移字段保留。

| 族 | Port | required | 社区版实现（插件） | 状态 |
|----|------|:--------:|--------------------|------|
| 入口 | `identity.provider` | ✅ | `identity_provider`（本地用户 + JWT + AuthSession 吊销） | RESOLVED |
| | `authz.provider` | ✅ | `authz_provider`（RBAC 角色-权限图，default-deny） | RESOLVED |
| | `secret.provider` | ✅ | `secret_provider`（Fernet，`secret://`） | RESOLVED |
| | `notify.channel` | — | `notify_channel`（日志/控制台） | RESOLVED |
| | `approval.provider` | — | `approval_provider`（DB Confirmation + 自审拦截 + 幂等） | RESOLVED |
| 执行 | `model.provider` | ✅ | `model_provider`（OpenAI 兼容 + Mock 降级） | RESOLVED |
| | `model.selector` | ✅ | `rule_based_model_selector` | RESOLVED |
| | `skill.executor` | ✅ | `skill_executors`（SkillGateway + 6 执行器） | RESOLVED |
| | `sandbox.driver` | — | `sandbox_driver`（进程内 `-I` + 最小 env） | RESOLVED |
| | `connector` | — | `mcp_connector`（MCP HTTP / Stdio） | RESOLVED |
| | `queue.driver` | ✅ | `queue_driver`（InMemory + Outbox 补偿） | RESOLVED |
| | `lock.provider` | ✅ | `lock_provider`（asyncio 命名锁） | RESOLVED |
| 信息 | `memory.working` | ✅ | `working_memory`（进程内 LRU） | RESOLVED |
| | `memory.short` | ✅ | `short_memory`（DB + SUPERSEDED 摘要链） | RESOLVED |
| | `memory.long` | — | `long_memory`（进程内 KV，重启即失） | RESOLVED |
| | `vector.store` | — | **无** | **UNAVAILABLE** |
| | `knowledge.provider` | — | **无** | **UNAVAILABLE** |
| | `object.store` | ✅ | `object_store`（本地 FS） | RESOLVED |
| 横切 | `interceptor` | ✅ | `interceptor`（委托内核 `record_audit`） | RESOLVED |
| | `observability.sink` | ✅ | `observability_sink`（结构化日志） | RESOLVED |
| | `policy.source` | ✅ | `policy_source`（配置 + DB 权限图） | RESOLVED |

**统计**：required 14 个（全部 RESOLVED，否则进程不启动）；optional 7 个 → 5 个有实现、**2 个显式 UNAVAILABLE**。

**UNAVAILABLE 语义**（契约 §3）：可选 Port 缺失 → 占位对象 `UnavailableCapability`，任何属性访问抛 `AI_CAPABILITY_UNAVAILABLE` 并落审计 `CAPABILITY_UNAVAILABLE`——**不崩、不静默**；调用方经 `registry.active.call_optional` 降级。

### 2.3 pending 语义（保留但已清空）

- 历史用途：区分「尚未迁移为 Port」与「已实现但当前不可用」；`pending=True` 启动豁免 fail-fast，只标 `NOT_PORTED` 并告警。
- **现状**：`pending=False` × 21，无 `NOT_PORTED` 港。新增 Port 必须走内核契约变更流程（§7/§12）。

---

## 三、插件清单

### 3.1 内置插件全量（19 个，as-is）

按 `BUILTIN_PLUGINS` 声明顺序（`plugins.py:163`），该顺序同时是 Module 内的 contribute 顺序。

| # | manifest.name | provides | requires | permissions | 实现文件 |
|---|---------------|----------|----------|-------------|----------|
| 1 | `policy_source` | `policy.source` | — | — | `policy_source.py` |
| 2 | `observability_sink` | `observability.sink` | — | — | `observability_sink.py` |
| 3 | `interceptor` | `interceptor` | — | — | `interceptor.py` |
| 4 | `notify_channel` | `notify.channel` | — | — | `notify_channel.py` |
| 5 | `approval_provider` | `approval.provider` | — | — | `approval_provider.py` |
| 6 | `identity_provider` | `identity.provider` | — | — | `identity_provider.py` |
| 7 | `authz_provider` | `authz.provider` | — | — | `authz_provider.py` |
| 8 | `secret_provider` | `secret.provider` | — | — | `secret_provider.py` |
| 9 | `sandbox_driver` | `sandbox.driver` | — | — | `sandbox_driver.py` |
| 10 | `object_store` | `object.store` | — | — | `object_store.py` |
| 11 | `working_memory` | `memory.working` | — | — | `working_memory.py` |
| 12 | `short_memory` | `memory.short` | — | — | `short_memory.py` |
| 13 | `long_memory` | `memory.long` | — | — | `long_memory.py` |
| 14 | `model_provider` | `model.provider` | `secret.provider` | — | `plugins.py` + `model_gateway.py` |
| 15 | `rule_based_model_selector` | `model.selector` | `model.provider` | — | `model_selector.py` |
| 16 | `skill_executors` | `skill.executor` | `authz.provider` | `skill.execute` `tool.execute` `db.query` | `plugins.py` + `executors.py` + `db_executors.py` |
| 17 | `mcp_connector` | `connector` | `skill.executor` | `connector.manage` | `plugins.py` + `mcp.py` |
| 18 | `queue_driver` | `queue.driver` | — | — | `queue_driver.py` |
| 19 | `lock_provider` | `lock.provider` | — | — | `lock_provider.py` |

**依赖顺序约束**（contribute 期生效，违反即 `AssemblyError`）：

- `secret_provider` 先于 `model_provider`（装配期对象共享解密 `secret://`）。
- `authz_provider` / `approval_provider` / `sandbox_driver` / `interceptor` 先于 `skill_executors`（构造 SkillGateway 时注入）。
- `skill_executors` 先于 `mcp_connector`（stdio 注册表挂到网关）。

### 3.2 模块装配矩阵（Module → 插件）

`MODULE_REGISTRY`（`modules.py:11`）定义 9 个能力域；`PROFILE_COMMUNITY` 按模块名装配。

| Module | 构成插件 | Module 级 `requires_ports` |
|--------|----------|----------------------------|
| `governance` | `policy_source`、`observability_sink`、`interceptor` | — |
| `entry_channels` | `notify_channel`、`approval_provider` | — |
| `identity_access` | `identity_provider`、`authz_provider` | — |
| `secret_storage` | `secret_provider` | — |
| `model_access` | `model_provider`、`rule_based_model_selector` | `secret.provider` |
| `memory` | `working_memory`、`short_memory`、`long_memory` | — |
| `storage` | `object_store` | — |
| `execution` | `sandbox_driver`、`skill_executors`、`queue_driver`、`lock_provider` | `authz.provider` |
| `connectivity` | `mcp_connector` | `skill.executor` |

> Profile 条目既可为 Module 名（推荐），也可直接为插件名（细粒度装配/测试）。Module 内插件顺序 = contribute 顺序。

### 3.3 能力拆分说明：HTTP REST

| 能力 | 落点（as-is） | 说明 |
|------|---------------|------|
| REST Tool 执行 | `HttpExecutor`（挂在 `skill.executor`） | 方法白名单 `SUPPORTED_HTTP_METHODS = ("GET","POST")`、强制 https、DNS 解析后拒内网 IP（`allow_private_network` 可显式放行）、host 白名单、公网目标 IP 钉扎、响应截断、禁重定向 |
| MCP over HTTP | `StreamableHttpMcpConnector`（`connector` Port） | MCP JSON-RPC 协议，**不是**通用 REST 业务连接器 |
| 通用 REST Connector | 未单独实现 | 连接配置 → 注册 Tool 的完整管理走阶段 7 连接器能力，不强行塞进 MCP 插件 |

### 3.4 与「官方 6–8 个插件」的关系

- 19 个内置插件是**社区版 Port 实现**，不计入官方专有名额。
- 官方专有插件面向企业版合规/连接/规模（审计导出归档、A2A 网关、沙箱 sidecar、知识库、SSO、通知增强等），见 §六。
- 官方插件若需新 Port：**先做成 Module**，或走契约 §7 新增 Port 流程（≥2 个真实实现 + 定版本）。

---

## 四、通用开发规范（契约层）

### 4.1 Manifest 规范（以代码为准）

**as-is 字段（`PluginManifest`，`manifest.py:28`）**：

```python
PluginManifest(
    name: str,                              # 唯一标识；Profile/Module 按此匹配
    description: str,
    provides: tuple[str, ...],              # 本插件实现的 Port（必须 ∈ 21 Port）
    requires: tuple[str, ...] = (),         # 装配期依赖的其他 Port
    permissions: tuple[str, ...] = (),      # 权限码（仅 db.query 有强制点，见 §4.2）
    api_version: str = "zsoftym.ai/plugin.v1",
    version: str = "0.1.0",
    config_schema: dict | None = None,      # 字段已存在，尚无语义校验（G4）
)
```

**有效性规则**：

- `provides` / `requires` 出现未知 Port → `validate_manifest` 抛 `ValueError`，装配终止。
- `requires` 在 `registry.resolve()` 之后逐项校验，未 RESOLVED → `AssemblyError`。
- 内置插件注册优先级默认 BUILTIN；`extra_plugins` 默认 PLUGIN（可覆盖内置）；`PORT_OVERRIDES` 为 EXPLICIT（最高）。

**to-be 补齐（不阻塞首期）**：

```yaml
metadata: {displayName: "...", version: "1.0.0", official: true}
spec:
  healthcheck: {type: port_method, method: health}
  circuit_breaker: {failure_threshold: 5, recovery_timeout_seconds: 60}
```

### 4.2 权限模型（现状与差距）

| 权限码 | 声明插件 | 强制点（as-is） |
|--------|----------|-----------------|
| `db.query` | `skill_executors` | `PluginSdk.engine`：未声明即 `PermissionError`（唯一硬强制点） |
| `skill.execute` / `tool.execute` | `skill_executors` | 业务授权语义，非运行时拦截 |
| `connector.manage` | `mcp_connector` | 管理面权限语义，非运行时拦截 |

**差距（G8）**：`network / files / process / secrets` 四类声明**没有运行时拒绝**——目前是"声明清单"而非"沙箱策略"。企业版插件（A2A 出站、归档写盘、沙箱子进程）在补齐强制点前，能力边界只能靠实现自觉。

### 4.3 配置与密钥

| 项 | as-is |
|----|-------|
| 模型 API Key | `secret://` 引用（装配期经 `secret.provider` 解密）与明文 env 双形态；manifest `requires: [secret.provider]` |
| MCP / Connector 密钥 | 支持 `secret://` 前缀解析 |
| 存储根 | `settings.file_storage_dir`（object.store 根） |
| 插件级 `config_schema` | 字段存在但未校验（G4）；插件直接读 `sdk.settings` |
| 配置只读 | `PluginSdk.settings` 为只读引用，插件不得修改运行时配置 |

### 4.4 测试要求与现状映射

| 类型 | 要求 | as-is 对应 |
|------|------|------------|
| 单元 | 实现核心逻辑、配置解析、安全边界 | `test_secret_provider.py`、`test_identity_provider.py`、`test_authz_provider.py`、`test_lock_provider.py`、`test_model_selector.py`、`test_http_executor.py`、`test_mcp_http.py`、`test_mcp_connectors.py`、`test_md_skill_runtime.py` |
| Port 族 | 按 Port 族覆盖装配与语义 | `test_p2_entry_ports.py`、`test_p2_information_ports.py`、`test_p2_cross_cutting_ports.py` |
| 装配/注册表 | Module/Profile 展开、优先级、歧义失败 | `test_p2_assembly.py`、`test_port_registry.py` |
| 熔断/配额 | 阈值、冷却、半开、配额窗口 | `test_plugin_guard.py` |
| 集成 | 与 Gateway / 审计 / 任务链路联调 | `integration/test_p2_hardening.py`、`test_p2_robustness.py`、`test_stage11_security_and_audit.py` |
| 负面 | 缺权限、超时、SSRF、注入、越权 | 各 Port 单测 + `integration/test_tenant_isolation_regressions.py` |

**G6（保留）**：测试按功能组织而非按插件目录组织，可接受；整理目录时再映射。

### 4.5 新插件接入步骤（强制顺序）

1. **归类**：Profile → Module → Plugin（未列入 Port 的能力先做成 Module）。
2. **定义 manifest**：`name` 用 snake_case；`provides` 只填已存在 Port；`requires` 显式声明依赖。
3. **实现 `contribute(sdk)`**：只经 SDK（`settings/register_port/publish/shared/engine`）。
4. **处理装配期依赖**：contribute 期需其他 Port 实例时用 `sdk.shared("<port>")` 并显式检查 None（参照 `model_provider`）。
5. **加入 Module**：`MODULE_REGISTRY[module].plugins` 追加；需要跨 Port 保证时填 `requires_ports`。
6. **加入 Profile**：确认目标 Profile 包含该 Module（社区版 = `PROFILE_COMMUNITY`）。
7. **补依赖顺序**：contribute 顺序必须满足依赖（被依赖插件在前）。
8. **测试**：单测 + 装配测试（含缺 Port / 歧义 / 未声明 Port 的负面用例）。
9. **更新能力面**：涉及新特性时同步 `capabilities` 的 features 与端口状态展示。

---

## 五、已落地插件规格（as-is）

### 5.1 模型接入与选择

**`model_provider`（`model.provider`，required）**

- OpenAI Chat Completions 兼容；无有效配置时**显式降级 Mock** 并在 `capabilities` 可见（禁止生产误用）。
- `secret://` 密钥装配期解密；明文 env 向后兼容。
- 统一经 `GuardedModelGateway` 包裹（熔断 + 配额，`plugin_guard.py:117`）；流式决策轮（带 tools）走 `generate`，无 tools 走真流式并透传 tool_calls/usage。

**`rule_based_model_selector`（`model.selector`，required）**

- 规则：① 快照绑定直通 → ② 白名单校验（`settings.model_allowed_names`，未命中回退并标 `whitelist_fallback`）→ ③ 默认回退。
- 快照语义：已创建 Task 的选择结果不因全局配置变更而漂移。
- 消费接线：TaskRuntime 任务执行 / 审批恢复 / 子 Agent 委派；直答与流式分流暂用网关默认模型（后续接入）。

### 5.2 执行底座

**`skill_executors`（`skill.executor`，required）**

- 提供 `SkillGateway`（内核组件，经装配注册到 Port，不改变内核属性；见 G2c 裁决）。
- 执行器：`MOCK` / `HTTP` / `DB_QUERY`（sqlglot 单条 SELECT + 表列白名单 + 行数上限）/ `DB_SQL`（`ConnectorDbExecutor`）/ `MD_SCRIPT`（tool_type 执行器占位）/ `MCP`（HTTP + Stdio）。
- **MD_SCRIPT 双路径（G5）**：网关对"无工具绑定且 `config_snapshot.implementation == md_script`"的技能走 `MdSkillRuntime.execute`（真执行，经 sandbox）；而按 tool_type 分派的 `MdSkillScriptExecutor` 仍返回 `AI_CAPABILITY_UNAVAILABLE`。两条路径必须统一，避免"同为 MD_SCRIPT，命中哪条取决于绑定形态"。
- 执行侧不变量：审计发射、PEP 裁决（执行前）、确认暂停/恢复、结果标准化、熔断配额闸门。

**`sandbox_driver`（`sandbox.driver`，optional）**

- 实现体：`ProcessSandboxDriver` → `run_isolated_process`（`kernel/md_skill_runtime.py:28`）。
- 约束：独立解释器 `-I`、最小环境变量（仅 PATH / PYTHONNOUSERSITE）、stdin JSON 参数 / stdout JSON 结果、超时 kill、stdout/stderr 字节上限、工作目录限定在技能根内。
- **不具备**：CPU/内存 rlimit、网络 deny/allowlist、结构化 `denied/usage` 结果、`health()`（G11）。Windows 无可靠进程级隔离，企业版以 sidecar 替换同一 Port。

**`queue_driver` / `lock_provider`（required）**

- 队列：进程内 InMemory，支撑单机 Outbox 与 Worker 补偿恢复；装配根显式注入（无内核兜底默认值）。
- 锁：asyncio 命名互斥锁，用于 Outbox 认领防重；企业版可换 DB/Redis 实现（新插件，不改内核契约）。

### 5.3 连接器

**`mcp_connector`（`connector`，optional）**

- MCP JSON-RPC 2.0；Streamable HTTP + Stdio 双传输；`tools/list`、`tools/call`。
- Stdio：可执行文件须绝对路径且真实存在；env 防注入（拒绝 `\x00`、非法 `=`）；超时 terminate→kill；stderr 排空。
- HTTP：回环可 http；外网强制 https + `allowed_hosts`；响应大小上限。
- 注册侧支持 env 中 `secret://` 解密；远端工具不自动全量开放，需管理端确认/注册流程。

### 5.4 记忆与存储

| 插件 | 实现要点 | 边界 |
|------|----------|------|
| `working_memory` | 进程内 LRU（容量 `memory_working_cache_size`），键 `(tenant, user, conversation)` | 单进程，多副本不共享 |
| `short_memory` | DB 存储 + `SUPERSEDED` 摘要链；密钥脱敏；`active_memory` 按会话取最新 | 依赖 DB |
| `long_memory` | 进程内 KV（`remember/recall/forget`，按 tenant/user/key） | 重启即失，社区版占位 |
| `object_store` | 本地 FS：流式写入 + sha256 + 路径逃逸防护；`put / local_path / delete` | **无 list/open_read**，归档类能力需先补契约（G12） |

### 5.5 入口与身份

- `identity_provider`：本地用户 + JWT + AuthSession 吊销校验 → 构造 `SecurityContext`；失败 `AI_AUTH_FAILED(401)`。
- `authz_provider`：RBAC 角色-权限图，`has_all` **default-deny**；PEP 位置固定（SkillGateway 执行前），仅策略来源可换。
- `policy_source`：权限目录 + 平台白名单（MCP hosts / connector targets / model names），与内置裁决器同源。
- `secret_provider`：Fernet 本地加密（`SECRET_STORE_KEY`）；`encrypt/decrypt` 仅处理密文，**无按 ref 名解析**语义。
- `approval_provider`：DB `Confirmation` 队列；自审拦截、幂等裁决；创建/裁决写审计（`CONFIRMATION_REQUESTED` / `CONFIRMATION_RESOLVED`）；状态机衔接（WAITING_HUMAN / checkpoint）仍在内核。
- `notify_channel`：日志/控制台输出（社区版最小实现）。

### 5.6 横切

- `interceptor`：`AuditExecutionInterceptor` 委托内核 `record_audit`（审计不可关闭）；实现体在内核，插件仅装配登记并共享实例。
- `observability_sink`：结构化日志出口 `emit(event, payload)`（同步单条）；**不是审计事件流**（G12）。
- `policy_source`：见 §5.5。

---

## 六、待落地与增强（to-be）

### 6.1 `vector.store` / `knowledge.provider`（可选 Port，当前 UNAVAILABLE）

- 现状：契约已定义、`specs.py` 已登记、**无内置实现** → 能力查询显式 UNAVAILABLE。
- 决策点：社区版是否提供轻量实现（SQLite + 暴力检索 / 本地向量库）；不提供则保持 UNAVAILABLE 并在控制台提示。
- 若提供：新增 `vector_store` / `knowledge_provider` 两个内置插件，归入 `memory` 或新 Module，required 保持 false。

### 6.2 沙箱硬化（P1，安全）

- 目标：`rlimit`（CPU / 内存 / 文件大小）、工作目录写保护、网络策略、结构化 `denied/deny_reason/usage`、`health()`。
- 约束：Windows 无法提供等价隔离 → **按平台降级并在 capabilities 如实标注 `isolation_level`**（禁止虚报）。
- 拒绝路径必须独立事务留痕（参照 `record_audit_detached`），不能因抛错回滚丢失。

### 6.3 审计导出与归档（P2）

- 阻塞点：审计为 DB 直写（`zr_audit_log`），**无事件流/订阅**；`observability.sink` 只是日志出口，不能充当审计通道。
- 建议顺序：
  1. 内核补只读导出缝（如 `iter_audit_events(query)`）+ SDK 暴露（`sdk.read_audit()`），插件不直连表结构；
  2. 导出 Job（`queue.driver` 异步）+ 完整性校验（sha256）+ `object.store` 落盘；
  3. 导出行为自身写审计（`AUDIT_EXPORTED`），并在 `routes/audit.py` 分类表登记；
  4. 明确"**不得提供关闭审计写入的配置项**"为代码级约束（配置 schema 中不存在该键 + 单测断言）。
- 依赖补齐：`object.store` 需 `list(prefix)`（G12）；大批量导出不复用 `GET /audit`（`limit ≤ 100`）。

### 6.4 A2A 网关（企业版，单独立项）

- 现状：零代码，无 `a2a` Module；`connector` Port 是 **MCP 专用**，不能当通用出站 HTTP 客户端。
- 关键空白：入站身份凭据标准未定（现有 `identity.provider` 需 token → 本地 `Session`；`authz.provider` 需 DB Session）；跨实例无"远端 Session"抽象。
- 约束：`provides` 为空（只 requires 现有 Port）；深度/扇出硬限；**默认不创建父子 Task**（`parent_task_id` 不桥接）；新增 `AI_A2A_*` 错误码；`capabilities.features.a2a` 上线前保持 false。

### 6.5 Manifest 与插件治理增强（P2/P3）

| 项 | 目标 | 优先级 |
|----|------|:------:|
| `healthcheck` 声明 + 健康态上报 | 装配期/运行期可判定插件可用性（G9） | P2 |
| `circuit_breaker` / `quota` 声明 | 阈值从代码常量迁到 manifest（G4/G10） | P2 |
| `config_schema` 校验 | 启动期校验插件配置，拒绝非法配置 | P2 |
| `displayName/official/license` | 控制台展示与授权校验（G16） | P3 |
| `priority` 声明 | 第三方插件不依赖 `builtin` 标志（G14） | P3 |

---

## 七、装配与启用

### 7.1 加载流程（as-is，`assembly.py:81`）

```
assemble(settings, engine, profile=PROFILE_COMMUNITY, *, extra_plugins=())
 1. expand_profile(profile)            # Module 名 → 插件名列表（去重保序）
 2. 逐插件 validate_manifest(manifest)  # 未知 Port / 非法字段 → ValueError
 3. PluginSdk(...) 逐插件 contribute()  # 内置 BUILTIN；extra PLUGIN；含装配期对象共享
 4. 收敛 stdio 注册表 → SkillGateway.attach_stdio_registry
 5. 应用 settings.port_override_map    # EXPLICIT；"port.name=module:factory"
 6. registry.resolve()                 # 校验 required 缺失 / 同优先级歧义
 7. 校验插件 requires + Module.requires_ports
 8. 产出 AssemblyResult(registry, skill_gateway, model_gateway, stdio_registry, plugins)
```

**失败语义**：`AssemblyError` 直接中断启动（不降级、不猜）；`NOT_PORTED`（pending 港）仅告警。

### 7.2 Module 与 Profile

- `MODULE_REGISTRY`：9 个 Module（§3.2）；Module 是装配单位，也是控制台能力域展示单位。
- `PROFILE_COMMUNITY`：当前为**全部 9 个 Module**（等价于 19 个内置插件全量装配）。
- `PROFILE_ENTERPRISE`：**尚未创建**；官方插件上线时新增 Profile（`extra_plugins` 亦可承载）。
- Profile 是**代码常量**，暂无 `settings.profile` 开关；如需运行期选择，需在装配根增加显式参数（不建议用环境变量隐式切换）。

### 7.3 实现替换（PORT_OVERRIDES）

- 替换单个 Port 走配置项 `PORT_OVERRIDES`，格式 `port.name=module:factory`（多组逗号分隔），解析在 `settings.port_override_map`（`config.py:44`）。例：`PORT_OVERRIDES=connector=tests.fakes:make_connector`。
- `assemble()` **没有** `overrides` 形参（`assembly.py:81`），私有化部署与测试均经该配置注入。
- 替换实现必须满足 Port 协议；`EXPLICIT` 优先级最高，不会与 BUILTIN/PLUGIN 冲突。

### 7.4 新增实现放入项目的方式

| 场景 | 做法 |
|------|------|
| 社区版能力升级（如 long_memory 换 DB） | 改内置插件实现（保持 manifest name/provides），补 Port 单测 |
| 企业版专有插件 | 独立包 + `extra_plugins`，manifest `official=true`，走 license 校验；`provides` 复用现有 Port |
| 客户私有实现 | `extra_plugins` 覆盖对应 Port（PLUGIN 优先于 BUILTIN），不改内核 |
| 测试替身 | `PORT_OVERRIDES`（EXPLICIT） |

---

## 八、已知问题与优先级

| ID | 问题 | 影响 | 优先级 |
|----|------|------|:------:|
| G8 | 权限声明仅 `db.query` 有运行时强制；`network/files/process/secrets` 未强制 | 第三方/企业插件越权无阻断 | **P1** |
| G11 | 沙箱无 CPU/内存/网络限制，Windows 无隔离；无 `health()` / `denied/usage` | 资源耗尽、越权访问；能力易被高估 | **P1** |
| G5 | MD_SCRIPT 双路径（网关真执行 vs tool_type 执行器占位） | 行为不一致，排查困难 | **P1** |
| G4 | Manifest 缺 `healthcheck/circuit_breaker`；`config_schema` 未校验 | 无法声明式治理，配置错误后置暴露 | P2 |
| G9 | 无插件健康探测与健康态上报 | 故障只能靠日志发现 | P2 |
| G10 | 熔断/配额只接 `model.provider` + `skill.executor` | 其他高成本 Port 无限流 | P2 |
| G12 | 审计 DB 直写、无事件流/导出 API；`object.store` 无 `list` | 审计导出归档**无法按契约实现** | P2 |
| G13 | `vector.store` / `knowledge.provider` 无实现；`capabilities._FEATURES` 为静态列表，仅 working/short 与 Port 状态联动 | 能力面展示与实际可能不一致 | P2 |
| G14 | 插件优先级不可由 manifest 声明（仅 builtin 标志 / overrides） | 第三方插件排序不可控 | P3 |
| G16 | manifest 缺 `displayName/official/license` | 控制台展示与授权校验受限 | P3 |
| G6 | 测试按功能而非插件目录组织 | 可维护性（可接受） | P3 |

**已关闭（保留追溯）**：G1（API 层运行时导入插件，2026-10-20 修复）、G3（MCP env `secret://` 解析）、G7（审批自审拦截 + 幂等裁决）、G2a/G2b/G2d（治理/执行/入口 Port 迁移）、G2c（SkillGateway 归属裁决）。

---

## 九、里程碑

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M1 插件契约与首批实现 | Manifest/SDK/Registry/Profile、模型与执行底座 | ✅ 已交付（P0/P1） |
| M2 Port 全量收口 | 21 Port 全部 `pending=False`；入口/信息/横切 Port 实现 | ✅ 2026-10-22 完成 |
| M3 治理增强 | 权限强制点补齐（G8）、MD_SCRIPT 路径统一（G5） | ⬜ 待排期（P1） |
| M4 沙箱硬化 | rlimit / 网络策略 / `health` / 结构化拒绝（G11） | ⬜ 待排期（P1） |
| M5 Manifest 治理 | `healthcheck` / `circuit_breaker` / `config_schema`（G4/G9/G10） | ⬜ 待排期（P2） |
| M6 审计导出归档 | 依赖内核只读缝 + `object.store.list`（G12） | ⬜ 待排期（P2） |
| M7 知识库 | `vector.store` / `knowledge.provider` 决策与实现（G13） | ⬜ 待决策（P2） |
| M8 A2A 网关 | 企业版独立立项（入站凭据标准先行） | ⬜ 未启动（P3） |

---

## 十、交付清单

| 项 | 位置 | 状态 |
|----|------|------|
| 插件契约与 SDK | `app/builtin_plugins/manifest.py` | ✅ |
| Module + Profile | `app/builtin_plugins/modules.py` | ✅ |
| 内置插件（19） | `app/builtin_plugins/*.py` | ✅ |
| 装配根 | `app/assembly.py` | ✅ |
| Port 权威描述 | `app/kernel/ports/specs.py`（21 项断言） | ✅ |
| Port 协议 | `app/kernel/ports/*.py` | ✅ |
| 能力面 | `GET /api/z-torque/capabilities` | ✅（features 静态，G13） |
| 插件单测/集成测 | `tests/unit/test_p2_*`、`tests/integration/test_p2_*` | ✅ |
| 插件文档（本文） | `docs/Z-Torque_核心插件详细开发文档_v1.1.md`（内容 v1.2） | ✅ |
| 官方专有插件 | — | ⬜ 未启动 |

---

## 十一、与 v1.1 的差异摘要

| # | v1.1 表述 | v1.2 修正 |
|---|-----------|-----------|
| 1 | 已落地 6 个插件，待落地 15 个 Port | **19 个内置插件，21 Port 全量 `pending=False`**，仅 2 个可选 Port 显式 UNAVAILABLE |
| 2 | 待落地 `rule-based-model-selector` / `local-memory` / `local-approval-queue` | 三者均已落地（`rule_based_model_selector` / `long_memory` 占位实现 / `approval_provider`）；待落地改为沙箱硬化、审计导出、A2A、知识库 |
| 3 | 「官方 6–8 个插件」未与内置插件区分 | 明确：19 个内置=Port 实现载体；6–8 个官方=企业版增量 |
| 4 | Manifest 伪代码含 `apiVersion`/`metadata`/`priority`/`permissions` 分类 | 以代码字段为准；分类与 `priority`/`healthcheck` 列为 to-be（G4/G14/G16） |
| 5 | 权限纪律表述为"内核直接拒绝四类行为" | 仅 `db.query` 有强制点，其余为声明（G8，P1） |
| 6 | Profile 由"通用模块名元组"自动决定 | `PROFILE_COMMUNITY` = 9 个 Module 全量；无企业版 Profile，无运行期 profile 开关 |
| 7 | HealthCheck/CircuitBreaker 出现在 manifest 示例 | 未实现，列入 M5（G4/G9） |
| 8 | 未描述 MD_SCRIPT 双路径 | 明确网关真执行与 tool_type 占位并存（G5，P1） |
| 9 | 未描述 UNAVAILABLE 语义与 `call_optional` | §2.2 明确：占位对象 + `AI_CAPABILITY_UNAVAILABLE` + 审计 + `call_optional` 降级 |
| 10 | 测试清单为"要求"，无对应文件 | §4.4 给出 as-is 测试文件映射 |

---

## 十二、演进规则（契约层）

1. **21 Port 已锁定**：新增 Port 必须满足「≥2 个真实实现 + 至少一个 Profile 使用 + 版本化」；仅 1 个实现 → 先做 Module 或私有实现。
2. **改 Port 签名 = 破坏性变更**：需内核契约版本升级（v1 → v1.1+），同步更新 `specs.py`、协议文件、全部实现与测试。
3. **插件不得反向依赖**：`kernel` 不导入 `builtin_plugins`；需要内核配合时通过 Port 或 `AssemblyResult` 注入（参照 `SkillGateway.attach_stdio_registry`）。
4. **审计/身份/快照三条红线不因插件而松动**；企业版插件只能"更严"，不能"更松"。
5. **能力面诚实**：任何降级（Mock 模型、UNAVAILABLE Port、无隔离沙箱）必须在 `capabilities` 可见，禁止虚报。

---

**版本**：v1.2（2026-10-22）
**关联**：形态确认书 v1、内核契约 v1、阶段 0/1/4/5/7/9 文档、2026-10-22 代码对照
**维护**：实现与清单变更时同步更新「as-is」表与 §八；契约变更走内核版本流程。

