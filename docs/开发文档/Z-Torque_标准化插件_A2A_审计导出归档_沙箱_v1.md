# Z-Torque 标准化插件详细开发文档
## A2A · 审计导出与归档 · 沙箱（v1.1，按代码基线校准）

> **文档性质**：官方标准化插件开发规格（遵循插件五条纪律与 Port 契约，并按 2026-10-22 代码现状校准）
> **适用版本**：企业版为主；沙箱社区版为「已有薄实现 + 本批增强」，企业版替换 sidecar
> **上游依据**：
> - `AI_Runtime_形态确认书_v1.md`（官方 6–8 插件；企业版含沙箱 / A2A 网关）
> - `Z-Torque_内核契约_v1.md`（六件不变量、21 Port、插件五条纪律、装配三层）
> - `Z-Torque_核心插件详细开发文档_v1.1.md`（正文 v1.2：19 个内置插件、9 个 Module、21 Port 全量收口）
> - 代码基线：`app/assembly.py`、`app/builtin_plugins/`、`app/kernel/ports/`、`app/kernel/audit/service.py`
> **关键裁决**：
> - A2A **不是**社区版已有 的「单层父子 Task 协作」，而是独立 Module + 标准化官方插件。
> - 三插件均按标准化插件标准交付：manifest 唯一入口、只实现已定义 Port、只经 PluginSdk、声明式权限、插件级熔断与配额。
> - **v1.1 校准原则**：伪代码让位于现有 Port 签名。凡与代码冲突处，一律写「现状签名 + 演进目标」，避免按文档实现无法装配。
> **修订记录**：
> - v1（2026-10-22）——初版（YAML manifest、`execute(SandboxExecuteRequest)`、sink 订阅审计）。
> - **v1.1（2026-10-22）**——按代码基线重写：Manifest 改 Python dataclass、沙箱接口改现有 `run(...)`、审计导出明确「无事件流，需先补内核只读缝」、A2A 去掉「复用 connector」、补装配硬约束与 M0 裁决清单。

---

## 一、总览与纪律

### 1.1 三层叙事中的位置

| 层级 | 本批能力落点 | 现状 |
|------|--------------|------|
| 产品层（官方 6–8） | 沙箱、审计合规导出、A2A 网关均属企业版差异化官方插件 | 未启动 |
| 能力域层（Module） | `execution`（沙箱，已有）、`governance`（审计导出，已有）、`a2a`（**不存在，需新建**） | `MODULE_REGISTRY` 9 个（`modules.py:11`） |
| 装配层（Plugin） | `sandbox_driver`（已有，增强）、`audit_export_archive`（新建）、`a2a_gateway`（新建） | 19 个内置插件（`plugins.py:163`） |
| 契约层（Port） | **只用已锁定的 21 个 Port**，不新增 Port | 21/21 `pending=False`（`specs.py:67` 断言） |

### 1.2 插件五条纪律（含现状落点）

| # | 纪律 | 现状落点 |
|---|------|----------|
| 1 | manifest 唯一入口：`api_version / provides / requires / permissions / config_schema` | `PluginManifest`（`manifest.py:27`）；Python dataclass，**无 YAML 加载器** |
| 2 | 只实现内核已定义 Port，不得自定义 | `validate_manifest`（`manifest.py:133`）对未知 Port 直接抛错 |
| 3 | 只经 PluginSdk 读写状态，禁止直连内核表 | `PluginSdk`（`manifest.py:39`）：`settings / registry / engine / register_port / publish / shared / expose_skill_gateway / attach_stdio_registry` |
| 4 | 权限声明式：未声明的行为内核直接拒绝 | **仅 `db.query` 有强制点**（`PluginSdk.engine`，`manifest.py:75`）；`network/files/process/secrets` 无运行时拒绝（下文 G-08） |
| 5 | 插件级熔断与配额强制 | `PluginGuardRegistry`（`plugin_guard.py:82`），阈值来自 `settings.plugin_breaker_*`；当前只接 `model.provider` 与 `skill.executor` 分派 |

### 1.3 三条红线（插件同样遵守）

- **审计不可关闭**：导出/归档插件只消费，不得提供关闭写入的配置项或代码路径。
- **快照不可热改**：不得修改已发布 AgentVersion 的冻结配置。
- **身份不可绕过**：A2A 必须携带并校验 `SecurityContext`；禁止"任意身份执行"接口。

### 1.4 与「父子 Task」的硬边界

| 能力 | 社区版现状 | 本批插件 |
|------|------------|----------|
| 单层父子 Task | 已实现：`Task.parent_task_id / root_task_id / delegation_depth / child_sequence`（`models/task.py:23-27`） | **不扩展、不替代** |
| A2A | 无（全仓 `a2a` 0 命中） | 标准化插件：发现、路由、会话、身份透传、配额、审计 |

A2A 插件**不得**写入 `parent_task_id / child_sequence / delegation_id`（默认不桥接）；如需桥接必须产品显式配置且写入审计。

### 1.5 现状基线（10 条硬约束，先读再设计）

| # | 约束 | 影响 |
|---|------|------|
| B-1 | `sandbox.driver` 签名为 `async run(*, script_path: Path, arguments: dict, cwd: Path, timeout_seconds: float, max_output_bytes: int) -> dict`（`ports/sandbox_driver.py:15`），返回**脚本 stdout 解析出的 JSON（技能结果本身）**，非执行元信息；超时 → `AI_SKILL_TIMEOUT`(504)，输出超限 / 非零退出 / 非 JSON → `AI_SKILL_FAILED`(500)；**无 `health()`、无 `denied/usage/exit_code`** | 沙箱章节不得引入 `SandboxExecuteRequest/Result`；标准件改用「错误码 + 审计 + capabilities」承载 |
| B-2 | `observability.sink` 为**同步单条** `emit(event: str, payload: dict \| None)`（`ports/observability_sink.py:13`） | 不能作为审计事件流；审计导出必须另开数据通道 |
| B-3 | 审计写入是 **DB 直写**（`record_audit(db, ...)` / `record_audit_detached(...)`，`audit/service.py:35/62`），无订阅、无事件总线 | 导出插件**当前无合法数据源**，需先补内核只读缝（M0-2） |
| B-4 | 唯一审计读取面 `GET /audit`：需 `audit.read_all` 才看全租户，`limit ≤ 100`，`offset` 分页（`routes/audit.py:183`） | 不适合大批量导出；只能作抽样/校验辅助 |
| B-5 | `object.store` 只有 `put(key, data, max_bytes) -> ObjectPutResult(size_bytes, sha256)` / `local_path(key)` / `delete(key)`（`ports/object_store.py:22`） | **无 `list`**：归档索引、过期清理、完整性回读需插件自管索引（M0-3） |
| B-6 | `secret.provider` 只有 `encrypt/decrypt(text)` 语义，**没有 `secret://` 名称解析 API** | `auth_secret_ref` / `encryption_secret_ref` 由插件自行「读配置 + decrypt」实现，不得假设 `resolve(ref)` |
| B-7 | `queue.driver` 是**任务队列**：`put(task_id: UUID) / get() -> UUID / task_done()`（`ports/queue_driver.py:16`） | 导出/归档**不能**直接当通用 Job 队列用；需自建 Job 执行器或走内核 Outbox 扩展 |
| B-8 | 装配硬校验：`skill.executor`、`model.provider`、`connector stdio registry` 任一缺失即 `AssemblyError`（`assembly.py:137-144`） | 任何企业版 Profile 必须保留 `skill_executors` + `model_provider` + `mcp_connector`（或提供等价 stdio registry 的插件） |
| B-9 | `PluginManifest` 无 `kind/metadata/priority/license/healthcheck/circuit_breaker/quota`；`permissions` 是扁平元组；`config_schema` 字段存在但**未校验** | 本文档模板按现有字段写；扩展字段列为「可选向后兼容」目标 |
| B-10 | 沙箱隔离只有：独立解释器 `-I`、最小 env、超时 kill、输出上限（`md_skill_runtime.py:28`）。**无 rlimit / 网络策略 / 工作目录写保护**；Windows 无等价隔离 | 沙箱验收必须分平台，`isolation_level` 如实上报，禁止宣称能力 |

---

## 二、Port 与 Module 映射

### 2.1 三插件映射（现状校准版）

| 插件 | 归属 Module | provides（现状可装配） | requires | 说明 |
|------|-------------|------------------------|----------|------|
| `sandbox_driver` | `execution`（已在册） | `sandbox.driver` | 无 | **已有内置插件**（`builtin_plugins/sandbox_driver.py:14`），本批为「补标准件」而非新建 |
| `audit_export_archive` | `governance`（追加） | **空 `()`** | `object.store`、`secret.provider`、`observability.sink` | v1 不实现/不抢占 `observability.sink`（B-2）；不入 `queue.driver`（仅支持 Task UUID，B-7）；能力经受控 API + 内部服务暴露 |
| `a2a_gateway` | 新建 `a2a` | **空 `()`** | `skill.executor`、`identity.provider`、`authz.provider`、`secret.provider`、`observability.sink`（可选 `approval.provider`、`notify.channel`） | 不用 `connector`（B-11：该 Port 是 MCP JSON-RPC 专用，不能当通用出站 HTTP 客户端）；不入 `queue.driver`（B-7） |

> **B-11（新增）**：`connector` Port 语义 = MCP 连接器（`initialize / tools/list / tools/call`，`ports/connector.py`），用于 A2A 出站属于语义误用。A2A 出站自建 HTTP 客户端，并在 manifest 声明 `network`（同时在 M0 补齐该权限的强制点，否则声明无效）。

### 2.2 命名与归属裁决

| 议题 | 裁决 |
|------|------|
| 命名风格 | 统一 **snake_case**（`sandbox_driver` / `audit_export_archive` / `a2a_gateway`），与 19 个内置插件一致；原文档 `sandbox-driver` 等连字符写法作废 |
| 沙箱是否新建插件 | **否**。现有 `sandbox_driver` 已 `register_port("sandbox.driver")`，重构会破坏 `skill_executors` 装配期注入；本批在文件内增强 |
| 审计导出是否实现 sink | **否**。不注册 `observability.sink`（避免与内置 `observability_sink` 同优先级歧义/覆盖日志出口）；改为新增内核只读缝 + 插件自有 API |
| A2A 是否新增 Port | **否**（v1）。`provides: ()`，能力经 Module 装配 + `capabilities` 扩展 + 受控 API 暴露 |

### 2.3 企业版 Profile 的装配硬约束

`assemble()` 结束时强制检查（`assembly.py:137-144`）：

- 必须有 `skill.executor` 实现（否则 `AssemblyError`）；
- 必须有 `model.provider` 实现；
- 必须有 `connector stdio registry`（即必须装配 `mcp_connector`，或提供等价产出）。

因此新 Profile（`PROFILE_ENTERPRISE`）不能"只装企业插件"，必须包含上述三项。文档 §7.1 给出可装配的 Module 组合。

---

## 三、通用开发规范（以代码为准）

### 3.1 目录与模块形态

当前**没有** `plugin.yaml` 加载器：插件是「Python 类 + `manifest` 类属性 + `contribute(sdk)` 方法」。推荐结构：

```text
app/builtin_plugins/
├── sandbox_driver.py            # 已有：增强（保持文件名与 manifest.name 不变）
├── audit_export_archive/        # 新建包
│   ├── __init__.py              # 导出 Plugin 类（供 BUILTIN_PLUGINS 注册）
│   ├── plugin.py                # PluginManifest + AuditExportArchivePlugin.contribute
│   ├── exporter.py              # 导出：分批游标 + 脱敏 + 序列化（jsonl/csv）
│   ├── archiver.py              # 归档：object.store.put + 索引 + 过期清理
│   └── config.py                # 配置读取与校验（settings → 数据类）
└── a2a_gateway/                 # 新建包
    ├── __init__.py
    ├── plugin.py                # PluginManifest + contribute（provides=()）
    ├── gateway.py               # 发现 / 路由 / 会话
    ├── protocol.py              # 内部消息模型 + 协议适配
    └── config.py
```

硬性约束：

- 内核（`app/kernel`、`app/api`、`app/services`）**不得** import 插件包；仅 `app/assembly.py` 加载。
- 企业版专有插件可放独立包，经 `assemble(..., extra_plugins=(plugin_instance,))` 接入，`manifest.official` 类字段暂不存在（B-9），以包归属 + license 校验流程区分。

### 3.2 Manifest 现状模板（可直接跑）

```python
from app.builtin_plugins.manifest import PluginManifest

class AuditExportArchivePlugin:
    manifest = PluginManifest(
        name="audit_export_archive",              # 唯一标识；Module/Profile 按此名引用
        description="审计导出与冷归档（不关闭、不绕过审计写入）",
        provides=(),                              # 不抢占 Port（B-2）
        requires=("object.store", "secret.provider", "observability.sink"),
        permissions=(),                           # 只允许 [] / db.query；无网络与文件语义（B-9）
        version="0.1.0",
        config_schema={...},                      # 字段存在但当前不校验（B-9）
    )

    def contribute(self, sdk) -> None:
        ...
```

字段语义（与 `PluginManifest` 一一对应）：

| 字段 | 约束 |
|------|------|
| `name` | snake_case 唯一；与 `BUILTIN_PLUGINS` 中类一一对应 |
| `provides` | 只能是 21 个 Port 名；空元组合法（A2A / 审计导出） |
| `requires` | 装配 `resolve()` 后逐项校验，未 RESOLVED 即 `AssemblyError` |
| `permissions` | 当前**只有** `db.query` 有强制效果；其他字符串仅登记 |
| `api_version` | 默认 `zsoftym.ai/plugin.v1`，勿改 |
| `version` | 插件版本（与契约大版本无关） |
| `config_schema` | 仅元数据，未校验（G-04） |

**目标态（向后兼容，非本批必须）**：`healthcheck`、`circuit_breaker`、`quota`、`display_name`、`official` 作为**带默认值**的新增字段加入 dataclass，既有 19 个插件零改动。

### 3.3 实现约定

- 入口统一 `contribute(sdk: PluginSdk)`；需要其他 Port 实例时用 `sdk.shared("<port>")` 并显式判空（参照 `model_provider` 读取 `secret.provider`）。
- 装配期对象共享键：`SKILL_GATEWAY_KEY = "__skill_gateway__"`（`manifest.py:24`）；跨插件挂载用 `sdk.attach_stdio_registry(...)`。
- 数据库访问：仅声明 `db.query` 的插件可用 `sdk.engine`；否则一律经 Port。
- 拒绝/异常路径审计：必须用 `record_audit_detached(...)`（独立事务，`service.py:62`），否则业务回滚会丢审计。
- 超时与取消：沙箱走 `ApiError("AI_SKILL_TIMEOUT")` 现状；A2A 走 `asyncio.timeout` + `AI_A2A_TIMEOUT`。
- 敏感字段：导出/归档前脱敏（沿用 `audit` 现有 detail 白名单思路），禁止明文密钥落对象存储。

### 3.4 交付检查清单（可核验）

- [ ] `validate_manifest` 通过（无未知 Port，`requires` 可解析）
- [ ] 只经 `PluginSdk` 访问配置/状态；未声明 `db.query` 时无 `sdk.engine` 调用
- [ ] 拒绝路径走 `record_audit_detached`，事件动作已在 `_AUDIT_CATEGORY_BY_ACTION` 登记（`routes/audit.py:50`）
- [ ] 新增错误码已加入 `app/core/errors.py` 对应集合
- [ ] 单测 + 装配测试（含缺 Port / 歧义 / 越权负面用例）
- [ ] 能力面（`/capabilities`）如实反映：不可用即不宣称

---

## 四、插件一：`sandbox_driver`（沙箱，增强既有插件）

### 4.1 定位

| 项 | 内容 |
|----|------|
| Port | `sandbox.driver`（optional，required=False） |
| Module | `execution`（已在册：`sandbox_driver`、`skill_executors`、`queue_driver`、`lock_provider`） |
| 现状 | 已注册：`ProcessSandboxDriver` → `run_isolated_process`（`md_skill_runtime.py:28`） |
| 本批目标 | 补「标准件」：结构化拒绝/超时、审计留痕、能力上报、配置项收敛；**不改 Port 签名** |
| 企业版 | 同 Port 替换为 sidecar / 容器实现；`run(...) -> dict` 语义保持一致 |

### 4.2 功能范围

已有：脚本路径校验、独立解释器 `-I`、最小环境变量（PATH / PYTHONNOUSERSITE）、stdin JSON 参数 / stdout JSON 结果、超时 kill、stdout/stderr 字节上限。

缺口（本批补齐）：

1. 结构化元信息：`duration_ms` / `truncated` / `deny_reason` **不写入返回 dict**（返回 dict 是脚本输出，下游直接当技能结果消费），改由 `observability.sink.emit` + 审计承载；拒绝用 `AI_SANDBOX_DENIED` 错误码表达。
2. 拒绝原因审计：`AI_SANDBOX_DENIED` 时写 `SANDBOX_DENIED` 审计（detached 事务）。
3. 能力上报：`/capabilities` 增 `sandbox: {available, impl, isolation_level}`；Windows → `isolation_level="none"`。
4. 配置收敛：`workdir_root`、`default_timeout_seconds`、`max_output_bytes` 从 settings 读取并校验。

### 4.3 Manifest（现状 + 增强）

```python
class SandboxDriverPlugin:
    manifest = PluginManifest(
        name="sandbox_driver",
        description="进程内受限脚本执行（独立解释器 -I + 最小环境变量）",
        provides=("sandbox.driver",),
        requires=(),                 # 企业版 sidecar 需密钥时再补 secret.provider（用 sdk.shared 判空）
        permissions=("process.spawn",),   # 仅登记；当前无运行时强制（G-08）
    )
```

注意：

- 声明 `process.spawn`（以及后续可能加的 `files.sandbox_workdir`）**不会**被内核拦截（B-9/G-08），能力边界由实现保证；需在 capabilities / 文档如实说明。
- 不得为沙箱新增 Port 方法（如 `health()`）——现有 `SandboxDriver` 是 ABC（`ports/sandbox_driver.py:6`），加抽象方法会破坏所有实现与测试；健康态经插件自有方法 + capabilities 暴露。

### 4.4 配置（settings → 数据类）

| 键 | 现状来源 | 目标 |
|----|----------|------|
| `md_skill_script_enabled` | `settings`（脚本执行总开关） | 保留；关闭时由 `MdSkillRuntime.execute` 在调用前返回 `AI_CAPABILITY_UNAVAILABLE`（`md_skill_runtime.py:68`） |
| `timeout_seconds` | 调用方传入 | 增加 `sandbox_default_timeout_seconds` 兜底 |
| `max_output_bytes` | 调用方传入 | 增加 `sandbox_max_output_bytes` 上限收敛 |
| `workdir_root` | 调用方 `cwd` | 增加根目录包含校验（`cwd` 必须位于 `md_skills_dir` 之下） |
| `mode` / `network_policy` | 不存在 | 列为企业版 sidecar 配置；社区版恒为 `in_process` + 无网络策略 |

`config_schema` 可先写全量声明（便于控制台展示），但**当前不会被执行**（G-04），校验必须在插件 `contribute()` 内自行完成。

### 4.5 Port 接口（现状，勿改）

```python
class SandboxDriver(ABC):
    @abstractmethod
    async def run(
        self, *, script_path: Path, arguments: dict[str, Any],
        cwd: Path, timeout_seconds: float, max_output_bytes: int,
    ) -> dict[str, Any]: ...
```

返回语义（现状，**本批不改**）：

- 返回 dict = 脚本 stdout 解析出的 JSON 对象（`md_skill_runtime.py:42-49`），即技能结果本身；不得往里塞执行元信息，否则 `SkillGateway → TaskRuntime` 的消费方会收到被包装的脏数据。
- 元信息（`duration_ms` / `truncated` / `deny_reason`）经 `observability.sink.emit` + 审计上报。

失败语义：

| 场景 | 现状 | 目标 |
|------|------|------|
| 超时 | `ApiError("AI_SKILL_TIMEOUT", 504)`，进程 kill（`md_skill_runtime.py:34-37`） | 保留（`tests/unit/test_p2_information_ports.py:173` 有断言），`detail` 加 `sandbox_timeout` |
| 输出超限 | `ApiError("AI_SKILL_FAILED", 500)`（**直接报错，不截断返回**） | 保留；如需"截断返回"属行为变更，须单独评审并同步改 SkillGateway |
| 非零退出 / 非 JSON | `ApiError("AI_SKILL_FAILED", 500)` | 保留 |
| 策略拒绝 | 无（当前无策略层） | `ApiError("AI_SANDBOX_DENIED", 403)`（新码，登记 `core/errors.py`） |

### 4.6 实现要求（分平台）

| 平台 | 可达能力 | 验收口径 |
|------|----------|----------|
| Linux / macOS | `-I` 隔离、`resource.setrlimit`（CPU/内存/文件大小）、进程组 kill、cwd 包含校验 | 内存超限、CPU 超时、cwd 越界三类用例必须通过 |
| Windows | `-I` 隔离、超时 kill、输出上限、cwd 包含校验；**无 rlimit、无网络策略、无 chroot** | 只验收超时 / 输出超限报错 / cwd 越界；`isolation_level="none"` 如实上报 |
| 企业版 sidecar | 容器命名空间隔离 + 网络策略 + 资源配额 | 同 `run()` 契约，`isolation_level="container"` |

拒绝路径必须 `record_audit_detached(tenant_id=..., action="SANDBOX_DENIED", resource_type="SKILL", ...)`，并在 `_AUDIT_CATEGORY_BY_ACTION` 登记（建议新增 `CATEGORY_SANDBOX` 或归 `CATEGORY_KERNEL`）。

### 4.7 测试要求

| 类型 | 用例 | 现状文件 |
|------|------|----------|
| 单元 | 超时 kill、输出超限报错、非零退出、cwd 越界 | `tests/unit/test_md_skill_runtime.py` |
| 单元 | 拒绝写审计（detached）、元信息上报 sink、返回 dict 与脚本 stdout 一致 | 新增 `tests/unit/test_sandbox_driver.py` |
| 集成 | SkillGateway 走 MD_SCRIPT → 沙箱执行 | 现有 `tests/integration/test_p2_hardening.py` 扩展 |
| 负面 | `md_skill_script_enabled=false` → `AI_CAPABILITY_UNAVAILABLE` | 新增 |

### 4.8 验收清单

- [ ] `run()` 签名未变；现有调用方与测试全绿
- [ ] 返回 dict 语义不变（脚本输出原样）；`duration_ms / truncated / deny_reason` 经 sink 与审计上报
- [ ] 拒绝路径有审计（detached），事件动作已登记类别
- [ ] `/capabilities` 出现 sandbox 实现度（含 `isolation_level`），Windows 不虚报

---

## 五、插件二：`audit_export_archive`（审计导出与归档）

### 5.1 定位与阻塞点

| 项 | 内容 |
|----|------|
| Module | `governance`（现有插件：`policy_source`、`observability_sink`、`interceptor`；本插件追加） |
| provides | `()`——**不实现 `observability.sink`**（内置已有实现，同优先级会歧义；且 sink 是同步日志出口，非事件流） |
| requires | `object.store`、`secret.provider`、`observability.sink`（事件上报，非审计数据源） |
| 与内核关系 | 审计**写入**仍是内核不变量；本插件只做**读取 → 导出 → 归档** |

**阻塞点（必须 M0 先解）**：

1. 审计无事件流：`record_audit` 直写 `zr_audit_log`（`audit/service.py:35`），`observability.sink` 只打日志（B-2/B-3）。
2. 公开读取面不够用：`GET /audit` 限 `limit ≤ 100`、`offset` 分页、需 `audit.read_all`（B-4）。
3. `object.store` 无 `list`（B-5），归档索引与过期清理无 Port 能力。
4. `queue.driver` 只接受 `Task UUID`（B-7），导出 Job 无法直接入队。

> **禁止事项**：不得提供"关闭审计写入"的配置项；不得绕过脱敏导出明文；不得在插件内 `select(AuditLog)` 直连内核表（违反纪律 3）。

### 5.2 功能范围

| 能力 | v1 范围 | 明确不做 |
|------|---------|----------|
| 导出 | 按时间/租户/操作者/动作前缀过滤；JSONL / CSV；分片 + 清单（含 sha256） | 任意 SQL / 跨租户合并导出 |
| 归档 | 热数据窗口后转冷存储（`object.store.put`）；可选加密（`secret.provider.decrypt` 后的密钥用于 AES-GCM） | 归档对象原地编辑 |
| 索引 | 插件自有索引（Job 表 / 清单对象），支持按时间与批次查询 | 替代 `/audit` 在线查询 |
| 审计 | 导出与归档动作自身写审计（`AUDIT_EXPORTED` / `AUDIT_ARCHIVED`） | — |

### 5.3 Manifest

```python
class AuditExportArchivePlugin:
    manifest = PluginManifest(
        name="audit_export_archive",
        description="消费审计只读数据，提供导出与冷归档；不关闭、不绕过审计写入",
        provides=(),
        requires=("object.store", "secret.provider", "observability.sink"),
        # 不入 queue.driver：该 Port 仅接受 Task UUID（B-7），Job 由插件自有执行器承担
        permissions=(),          # 不申请 db.query：审计读取必须走 M0-2 的只读缝
        version="0.1.0",
    )
```

> 若最终裁决选择"插件声明 `db.query` 直读审计表"（方案 A），则 `permissions=("db.query",)`，但需在文档与代码评审中记录"直连内核表"的例外理由；**推荐方案 C**（内核只读缝）。

### 5.4 配置

| 键 | 默认 | 说明 |
|----|------|------|
| `hot_retention_days` | 90 | 超过窗口进入归档候选 |
| `export_max_rows` | 100000 | 单批上限；超出分片 |
| `export_batch_size` | 1000 | keyset 游标每批行数 |
| `export_formats` | `["jsonl"]` | `jsonl` / `csv` |
| `archive.prefix` | `audit-archive/` | `object.store` 键前缀（实现自管目录结构） |
| `archive.encryption_enabled` | false | 开启时需 `SECRET_STORE_KEY` 可解密得到 DEK |
| `redaction_profile` | `standard` | `standard` / `strict`（严格模式哈希 user/tenant） |

配置由插件在 `contribute()` 内读取并校验失败即 `AssemblyError`（`config_schema` 当前不校验，G-04）。

### 5.5 接口约定

**（1）内核只读缝（M0-2，推荐方案 C）**

```python
# 目标：app/kernel/audit/reader.py
def iter_audit_events(
    db: Session, *, tenant_id: UUID,
    start: datetime | None, end: datetime | None,
    action_prefix: str | None, actor_user_id: UUID | None,
    after: tuple[datetime, UUID] | None,   # keyset 游标
    limit: int,
) -> Iterator[dict[str, Any]]: ...

# PluginSdk 暴露（只读、限本租户、强制脱敏字段白名单）
sdk.read_audit(query) -> Iterator[dict]
```

要求：不允许 `ORDER BY ... OFFSET` 深翻页（大表性能）；必须 keyset（`created_at, id`）；单次事务内不锁表；导出中不得调用 `record_audit`（避免自我放大）。

**（2）插件内部服务（非 Port）**

```python
class AuditExportService:
    def submit(self, query: AuditExportQuery, ctx: SecurityContext) -> str: ...      # 返回 job_id
    def status(self, job_id: str, ctx: SecurityContext) -> ExportJobView: ...
    def archive_due(self, ctx: SecurityContext) -> ArchiveResult: ...
```

**（3）HTTP 面（新增，走既有鉴权依赖）**

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| POST | `/api/z-torque/audit/exports` | `audit.export`（新权限码，需写入 `Permission` 种子数据） | 创建导出 Job |
| GET | `/api/z-torque/audit/exports/{id}` | `audit.export` | 查询进度 / 下载签名链接 |
| POST | `/api/z-torque/audit/archive/run` | `audit.archive`（新） | 手动触发归档 |

依赖 `Depends(require_permissions(["audit.export"]))`（`app/security/permissions.py:28`）。注意：`policy_source` 只读 DB 角色-权限图（`Permission / role_permission / user_role`，`policy_source.py:21`），**没有静态权限清单**；新码未写入种子数据时不可授予，`authz.has_all` 的 default-deny 会把请求一律拒绝。

### 5.6 实现要求

1. **Job 执行**：`queue.driver` 只处理 Task（B-7）→ v1 用**进程内线程池 + 自有 `zr_audit_export_job` 表**（插件自有表，不碰内核表；建表归属插件迁移脚本）。禁止伪造 Task 记录塞进队列。
2. **分批与幂等**：每批 `export_batch_size` 行，落到 `object.store.put(f"audit-export/{job_id}/part-{n}.jsonl")`，最后写 `manifest.json`（含批次列表、行数、每批 sha256、总 sha256）。
3. **脱敏**：`detail` 中的 `token / password / secret / api_key / authorization` 键一律替换为 `***`；`strict` 模式对 `actor_user_id` 做 HMAC 哈希（密钥取 `secret.provider` 解密结果，不出现在导出物中）。
4. **归档**：按 `created_at` 分片导出为对象后，**不做物理删除**（审计不可关闭，禁止删除审计行）；"归档"= 生成冷副本 + 索引，是否清理热表由运维走独立审批流程（本插件不提供该 API）。
5. **审计自身**：`submit` / `archive_due` 完成与失败都要写审计（`AUDIT_EXPORTED` / `AUDIT_EXPORT_FAILED` / `AUDIT_ARCHIVED`），并在 `_AUDIT_CATEGORY_BY_ACTION` 登记（建议新增 `CATEGORY_AUDIT`），否则前端归类为"配置变更"。
6. **熔断/配额**：键 `plugin.audit_export_archive`，走 `PluginGuardRegistry`（`check` 前置于 `submit`；`record` 记结果）；当前需在插件内显式调用（内核只接了 model/skill 两处，G-10）。
7. **失败处理**：单批失败重试 3 次（指数退避），仍失败则 Job 标 `FAILED` 并保留已完成分片；不得静默跳行。

### 5.7 测试要求

| 用例 | 断言 |
|------|------|
| 脱敏 | 导出物不含明文 token / 密钥；`detail` 敏感键被替换 |
| 租户隔离 | 租户 A 的 Job 无法导出租户 B 事件（`read_audit` 强制 `tenant_id`） |
| 分页正确性 | keyset 游标无重复、无遗漏（构造同 `created_at` 多行） |
| 完整性 | `manifest.json` 的 sha256 与分片实际摘要一致 |
| 关闭审计 | 配置 schema / API 中不存在"关闭审计"键；单测断言 settings 无该属性 |
| 归档 | `object.store.put` 调用参数、键前缀、幂等重跑不产生重复对象 |
| 权限 | 无 `audit.export` → 403；跨租户 → 403 |

### 5.8 前置依赖与验收

- [ ] **M0-2** 只读缝落地（`iter_audit_events` + `sdk.read_audit`）
- [ ] **M0-3** `object.store` 索引方案定稿（插件自有索引表 or 键前缀 + `local_path` 推导）
- [ ] 权限码 `audit.export` / `audit.archive` 进入权限目录与种子数据
- [ ] 审计动作类别登记完成，前端"类型"列显示正常

---

## 六、插件三：`a2a_gateway`（A2A 标准化网关）

### 6.1 定位

| 项 | 内容 |
|----|------|
| Module | **新建 `a2a`**（`modules.py` 追加；`PROFILE_COMMUNITY` 不包含） |
| provides | `()`——v1 不新增 Port |
| requires | `skill.executor`、`identity.provider`、`authz.provider`、`secret.provider`、`observability.sink`；可选 `approval.provider`、`notify.channel` |
| 与父子 Task | 完全独立；默认不桥接 `parent_task_id` |
| 现状 | 全仓 `a2a` 零命中；无入站协议、无对端注册表、无出站客户端 |

### 6.2 功能范围

1. **发现**：本实例已发布 Agent 的可寻址描述（来自 DB 的 `agent / agent_version` 只读投影，经插件自有查询服务，不直连内核表需声明 `db.query` 或走服务层 API）。
2. **路由与会话**：`local` 目标委托 `skill.executor`（`SkillGateway.execute`）；`remote` 目标自建 HTTP 客户端（B-11）。
3. **身份**：
   - 出站：`identity_propagation` 策略（`none` / `user_id_header` / `full_context_token`）；
   - 入站：**M0-1 必须先定标准**（见 6.5），当前 `identity.provider.authenticate(db, token)` 依赖本地 `Session` + `AuthSession`（`ports/identity_provider.py:19`），跨实例无"远端 Session"抽象。
4. **治理**：全链路审计（`A2A_INVOKED` / `A2A_DENIED` / `A2A_FAILED`）、深度/扇出硬限、按目标对端的熔断与配额。
5. **审批**：可选挂 `approval.provider`（高风险目标需人工确认）。

### 6.3 Manifest

```python
class A2AGatewayPlugin:
    manifest = PluginManifest(
        name="a2a_gateway",
        description="Agent-to-Agent 发现、路由与会话；独立于单层父子 Task",
        provides=(),
        requires=("skill.executor", "identity.provider", "authz.provider",
                  "secret.provider", "observability.sink"),
        permissions=("network.outbound",),   # 仅登记；当前无强制点（G-08）
        version="0.1.0",
    )
```

> **不要**把 `connector` 写进 `requires`：该 Port 是 MCP 专用，`requires` 会把 A2A 装配与 MCP 实现绑死，且语义不符（B-11）。

### 6.4 配置

| 键 | 默认 | 说明 |
|----|------|------|
| `enabled` | false | 默认关闭；开启需显式配置 |
| `local_discovery` | true | 暴露本地 Agent 描述 |
| `remote_peers` | `[]` | `[{id, base_url, auth_secret_name}]`（`auth_secret_name` 指向 `secret.provider` 可解密内容，**不是** `secret://` 解析） |
| `max_depth` | 2 | 调用链深度硬限（基于 `X-A2A-Depth` 头，超限即拒） |
| `max_fanout` | 5 | 单次调用派生并发上限 |
| `default_timeout_seconds` | 120 | 出站超时 |
| `identity_propagation` | `user_id_header` | `none` / `user_id_header` / `full_context_token` |
| `allowed_peers` | `[]` | 出站白名单（host:port 或 origin），空=全部拒绝 |

### 6.5 接口与入站身份（M0-1 裁决项）

```python
class A2AGateway:
    async def discover(self, ctx: SecurityContext, flt: dict) -> list[dict]: ...
    async def invoke(self, ctx: SecurityContext, req: A2AInvokeRequest) -> A2AInvokeResult: ...
```

**入站凭证三选一（需在 M0 定稿）**：

| 方案 | 机制 | 代价 |
|------|------|------|
| A. 受审 token（推荐） | 调用方携带本地 `JWT`，入站直接 `identity_provider.authenticate(db, token)` → `authz_provider.has_all(db, user_id, ["a2a.invoke"])` | 跨实例需共享 `JWT_SECRET` 与用户表（同租户部署可行，联邦不行） |
| B. 对端密钥 | `remote_peers[].auth_secret_name` 共享密钥 + HMAC 签名请求；入站映射为**租户级 service account** | 需构造 service account 与 `SecurityContext`；需扩展 `identity.provider` 或新增内部适配层 |
| C. 双向 mTLS | 传输层认证 + 头部声明主体 | 依赖网关/证书体系，首期成本最高 |

v1 **只支持 A**；B/C 在 M0 立项时再定。禁止"信任 `X-User-Id` 头"的实现（违反身份不可绕过）。

调用链：`入站认证 → 深度检查 → authz 裁决 →（可选 approval）→ 路由（local: skill.executor / remote: HTTP）→ 结果标准化 → 审计`。

### 6.6 实现要求

- `enabled=false` 时 `invoke` 立即 `ApiError("AI_A2A_DISABLED", 503)`，不注册任何出站。
- **不桥接 Task**：不得写 `parent_task_id / child_sequence / delegation_id / root_task_id`；本地目标创建的是独立 Task（`parent_task_id=NULL`）。
- 深度：入站读 `X-A2A-Depth`（缺省 0），出站写 `depth+1`；`> max_depth` → `AI_A2A_DEPTH_EXCEEDED` 且拒绝。
- 扇出：`asyncio.Semaphore(max_fanout)`；超限排队而非静默丢弃。
- 出站安全：强制 https（回环可 http）、host 白名单（`allowed_peers`）、响应体上限、禁重定向。
- 熔断/配额：键 `plugin.a2a_gateway`（全局限）+ `plugin.a2a_gateway.peer:{id}`（对端级），需在插件内调用 `PluginGuardRegistry.check/record`（G-10）。
- 审计：`A2A_INVOKED`（发起，含 target/depth/correlation_id）、`A2A_DENIED`（detached 事务）、`A2A_FAILED`；均在 `_AUDIT_CATEGORY_BY_ACTION` 登记（新增 `CATEGORY_A2A`）。
- 错误映射：对端 4xx → `AI_A2A_DENIED`(403)；超时 → `AI_A2A_TIMEOUT`(504)；连接失败 → `AI_A2A_PEER_UNAVAILABLE`(503)；路由缺失 → `AI_A2A_ROUTE_FAILED`(404)。

### 6.7 测试要求

| 用例 | 断言 |
|------|------|
| 关闭态 | `enabled=false` → `AI_A2A_DISABLED`，无出站连接 |
| 深度 | depth 超限拒绝并写审计 |
| 扇出 | 并发 > `max_fanout` 时被排队/拒绝，不超发 |
| 身份 | 伪造 `X-User-Id` 无效；`full_context_token` 传播后对端可校验 |
| Task 隔离 | 调用后 `parent_task_id IS NULL`，父子表无新增行 |
| 对端熔断 | 连续失败 → `AI_PLUGIN_CIRCUIT_OPEN` |
| 审计 | 四类动作均落库且分类正确 |

### 6.8 立项前置

- [ ] **M0-1** 入站凭证标准定稿（推荐方案 A）
- [ ] **M0-4** `network.outbound` 权限强制点（否则"声明式网络白名单"是装饰）
- [ ] `a2a` Module + `PROFILE_ENTERPRISE` 装配验证（必须仍满足 §2.3 三项硬约束）
- [ ] `capabilities.features.a2a` 联动（Module 装配与否），不新增 Port 名

---

## 七、装配与启用

### 7.1 Module 声明（现状代码形态）

```python
# app/builtin_plugins/modules.py
ModuleSpec(
    name="a2a",
    description="Agent-to-Agent 发现、路由与会话（企业版）",
    plugins=("a2a_gateway",),                 # 必须是 manifest.name
    requires_ports=("skill.executor", "identity.provider", "authz.provider"),
)
```

已存在的权威定义（`modules.py:11`）：

| Module | 插件 | `requires_ports` |
|--------|------|------------------|
| `governance` | `policy_source`、`observability_sink`、`interceptor`（本批追加 `audit_export_archive`） | — |
| `execution` | `sandbox_driver`、`skill_executors`、`queue_driver`、`lock_provider` | `authz.provider` |
| `connectivity` | `mcp_connector` | `skill.executor` |
| `a2a` | `a2a_gateway`（新建） | `skill.executor`、`identity.provider`、`authz.provider` |

### 7.2 Profile

- `PROFILE_COMMUNITY`（`modules.py:65`）= 全部 9 个 Module，**不含**本批三插件的企业版部分（`sandbox_driver` 已在）。
- 企业版需**新增常量** `PROFILE_ENTERPRISE`：在 `PROFILE_COMMUNITY` 基础上追加 Module `"a2a"`，并把 `audit_export_archive` 追加进 `governance` 模块的 `plugins`（`audit_export_archive` 是**插件名**，不是 Module 名；Profile 条目虽也接受插件名，但推荐按模块装配）。同时保证 §2.3 三项硬约束仍满足。
- `assemble(settings, engine, profile=..., *, extra_plugins=())`（`assembly.py:81`）：**没有** `overrides` 形参；单 Port 替换走 `settings.port_override_map`（环境变量 `PORT_OVERRIDES`），注册优先级 `EXPLICIT`。
- 运行期无 `settings.profile` 开关：Profile 是装配期常量，选择方式为 `main.py` 显式传参或独立入口，**不要**用环境变量隐式切换。

### 7.3 启用方式

| 场景 | 做法 |
|------|------|
| 社区版（当前） | 不变；`sandbox_driver` 已装配 |
| 企业版（同仓） | 新增 `PROFILE_ENTERPRISE`，`main.py` 按部署选择 |
| 私有化（不改主仓） | `extra_plugins=(AuditExportArchivePlugin(), A2AGatewayPlugin())`，注册优先级 `PLUGIN` |

### 7.4 能力面

`GET /api/z-torque/capabilities`（`main.py:176`）现状：`_FEATURES` 为静态列表（`main.py:157`），仅 `working_memory/short_term_memory` 与 Port 状态联动；`stage` 由 `derive_stage` 门槛（3/7/9/11）推导（`kernel/capabilities.py:13`）。

本批需追加（如实反映）：

```json
"sandbox": {"available": true, "impl": "in_process", "isolation_level": "none|rlimit|container"},
"audit_export": {"available": true, "formats": ["jsonl", "csv"], "archive": true},
"features": ["...", "a2a"]   // 仅当 a2a Module 装配
```

不得把未实现的隔离能力写成 `isolation_level="container"`。

### 7.5 启用顺序

1. `sandbox_driver` 增强（无新依赖，风险最低）
2. `audit_export_archive`（依赖 M0-2 只读缝 + M0-3 索引方案）
3. `a2a_gateway`（依赖 M0-1 凭证标准 + M0-4 网络强制点）

---

## 八、错误码（现状 + 新增登记）

现状：`app/core/errors.py` 仅两组集合（`MD_SKILL_ERROR_CODES:5`、`TASK_ERROR_CODES:18`）+ `ApiError(code, message, status_code, data)`；沙箱超时实际抛 `AI_SKILL_TIMEOUT`，非新增码。

| 码 | 状态码 | 归属 | 现状 |
|----|:------:|------|------|
| `AI_SANDBOX_DENIED` | 403 | sandbox | 待新增 |
| `AI_SANDBOX_RESOURCE_EXCEEDED` | 422 | sandbox | 待新增（Windows 不触发） |
| `AI_SKILL_TIMEOUT` | 504 | sandbox | **已在使用**，沙箱超时沿用，不替换 |
| `AI_AUDIT_EXPORT_DENIED` | 403 | audit_export | 待新增 |
| `AI_AUDIT_EXPORT_TOO_LARGE` | 413 | audit_export | 待新增 |
| `AI_AUDIT_ARCHIVE_FAILED` | 502 | audit_export | 待新增 |
| `AI_A2A_DISABLED` | 503 | a2a | 待新增 |
| `AI_A2A_DENIED` | 403 | a2a | 待新增 |
| `AI_A2A_TIMEOUT` | 504 | a2a | 待新增 |
| `AI_A2A_PEER_UNAVAILABLE` | 503 | a2a | 待新增 |
| `AI_A2A_DEPTH_EXCEEDED` | 400 | a2a | 待新增 |
| `AI_A2A_ROUTE_FAILED` | 404 | a2a | 待新增 |

落地要求：

- 新码加入 `MD_SKILL_ERROR_CODES`（或新建 `PLUGIN_ERROR_CODES` 集合并同步校验点）；`ApiError` 直接可用，但**入集合**才能被统一错误响应/文档校验覆盖。
- 前端错误文案映射同步（否则显示原始 code）。
- 不得复用 `AI_CAPABILITY_UNAVAILABLE` 表达"业务拒绝"（语义不同，且会触发 capability 告警）。

---

## 九、里程碑（按现状可达性）

| 阶段 | 内容 | 前置 | 验收 |
|------|------|------|------|
| **M0** | 契约裁决 4 项：M0-1 A2A 入站凭证、M0-2 审计只读缝、M0-3 object.store 索引、M0-4 network/files 权限强制点 | — | 裁决写入本文档与内核契约；无代码 |
| M1 | `sandbox_driver` 增强（结构化返回、拒绝审计、capabilities、cwd 校验） | — | Linux 三类限制用例通过；Windows 如实降级 |
| M2 | `audit_export_archive` v1（导出 + 清单 + 索引；归档可选） | M0-2、M0-3 | 脱敏/隔离/完整性用例通过；无法关闭审计 |
| M3 | `a2a_gateway` v1（本地调用 + 深度/扇出 + 审计 + 熔断） | M0-1、M0-4 | 不桥接 Task；伪造身份失败；对端熔断生效 |
| M4 | 企业版：sidecar 沙箱、远端 peer、归档加密 | M1–M3 | 同 Port / 同 Module 契约下替换，capabilities 如实 |

---

## 十、变更与兼容

| 变更 | 类型 | 处理 |
|------|------|------|
| `object.store` 增 `list(prefix)` | **Port 破坏性**（ABC 加抽象方法） | 走契约 §7：默认实现（返回 `[]`）+ 版本标注，或新增可选方法并 `NotImplementedError` 兜底 |
| `PluginManifest` 增可选字段 | 向后兼容 | 全部给默认值，19 个内置插件零改动 |
| `observability.sink` 增 `health()` / 批量 | 破坏性 | v1 **不动**；健康态走插件自有方法 |
| `sandbox.driver` 增 `health()` | 破坏性 | v1 **不动**；同上 |
| 新增 `a2a` Module / `PROFILE_ENTERPRISE` | 增量 | 不改变 `PROFILE_COMMUNITY` 行为 |
| 新增权限码 `audit.export / audit.archive / a2a.invoke` | 增量 | 必须写入 `Permission` 种子数据（`policy_source` 只读 DB 角色-权限图，无静态清单） |

---

## 十一、与 v1 的差异摘要

| # | v1 表述 | v1.1 修正 |
|---|---------|-----------|
| 1 | 三插件均"新建" | `sandbox_driver` **已存在**，本批为增强；另两个新建 |
| 2 | `plugin.yaml` + YAML manifest 模板 | 无 YAML 加载器；改为 Python `PluginManifest` + `contribute(sdk)` |
| 3 | `kind/metadata/priority/license/healthcheck/circuit_breaker/quota` | 现有 dataclass 无这些字段；列为向后兼容目标（B-9） |
| 4 | `permissions: {network/secrets/files/process}` 四类字典 | 现有为扁平 `tuple[str, ...]`，且仅 `db.query` 强制（G-08） |
| 5 | `sandbox.driver.execute(SandboxExecuteRequest) -> SandboxExecuteResult` | 现签名为 `run(*, script_path, arguments, cwd, timeout_seconds, max_output_bytes) -> dict`（B-1） |
| 6 | 审计插件 `provides: observability.sink` 并"订阅事件流" | sink 是同步单条日志出口（B-2）；审计 DB 直写无流（B-3）→ 改为 `provides: ()` + 内核只读缝 |
| 7 | 导出走 `queue.driver` 异步 Job | `queue.driver` 仅接受 Task UUID（B-7）→ 改为插件自有 Job 表 + 线程池 |
| 8 | 归档后用对象存储列举/清理 | `object.store` 无 `list`（B-5）→ M0-3 定索引方案 |
| 9 | A2A 传输"复用 connector" | `connector` 为 MCP 专用（B-11）→ 自建 HTTP + 网络白名单 |
| 10 | `assemble(..., overrides=...)` | 实际签名无 `overrides`；覆盖走 `settings.port_override_map`（`assembly.py:60`） |
| 11 | 错误码"本批新增 12 个" | 现状仅两张码表；沙箱超时沿用 `AI_SKILL_TIMEOUT`（§八） |
| 12 | 三插件命名用连字符 | 统一 snake_case（与 19 个内置插件一致） |
| 13 | 未提装配硬约束 | 新增 §2.3：企业版 Profile 必须含 `skill.executor` / `model.provider` / stdio registry（`assembly.py:137-144`） |

---

## 十二、维护说明

- 三插件任一 manifest / Port 映射 / 红线行为变更时，同步更新本文档并在 §十一 追加差异。
- 本文档中标注「待新增」「M0-x」的条目在落地后必须回填行号与状态，避免再次出现"文档能力 > 代码能力"。
- 与《核心插件详细开发文档_v1.1（正文 v1.2）》的插件清单、Module 矩阵保持一致；若该文档升级，本文档同步复核 §1.5 现状基线。

---

**版本**：v1.1（2026-10-22，按代码基线校准）
**关联**：形态确认书 v1、内核契约 v1、核心插件详细开发文档 v1.1（正文 v1.2）
**代码基线**：`app/assembly.py`、`app/builtin_plugins/manifest.py`、`app/kernel/ports/specs.py`、`app/kernel/audit/service.py`、`app/models/task.py`、`app/main.py`

