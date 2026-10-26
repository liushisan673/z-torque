# Z-Torque 标准化插件详细开发文档

## A2A · 审计导出与归档 · 沙箱（v1.3 · 定稿）

> **文档性质**：官方标准化插件开发规格（遵循插件五条纪律与 Port 契约，按 2026-10-23 代码现状校准，可直接进入开发）
> **适用版本**：企业版为主；沙箱社区版为「已有薄实现 + 本批增强」，企业版可替换 sidecar
> **取代关系**：本文取代 `_v1.md`、`_v1.1.md`、`_v1.2.md`，旧版可删除，不再维护
> **上游依据**：
> - `AI_Runtime_形态确认书_v1.md`（官方 6–8 插件；企业版含沙箱 / A2A 网关）
> - `Z-Torque_内核契约_v1.md`（六件不变量、21 Port、插件五条纪律、装配三层）
> - `Z-Torque_核心插件详细开发文档_v1.1.md`（19 个内置插件、9 个 Module、21 Port 全量收口）
> - 代码基线：`app/assembly.py`、`app/builtin_plugins/`、`app/kernel/ports/`、`app/kernel/audit/service.py`、`app/kernel/plugin_guard.py`、`app/core/errors.py`、`app/main.py`、`alembic.ini`
>
> **关键裁决（v1.3 新增三条）**：
> 1. **插件自有持久化必须显式声明 `db.query`**（v1.2 的 `permissions=()` 无法写 Job 表，自相矛盾）→ 见 §1.6。
> 2. **A2A 方案 A 需要 DB 会话**（`PluginSdk.engine` 受 `db.query` 门禁，且无 session 入口）→ 新增 M0-5。
> 3. **「未装配不注册路由」在本项目不可行**（装配在 lifespan、路由在 import 期）→ 改为「路由常注册 + 装配态判定」→ 见 §5.5。
>
> **校准原则**：伪代码让位于现有 Port 签名。凡与代码冲突处，一律写「现状签名 + 演进目标」，避免按文档实现无法装配。
>
> **修订记录**：
> - v1（2026-10-22）——初版（YAML manifest、理想化接口、sink 订阅审计）。
> - v1.1（2026-10-22）——按代码基线重写：Manifest 改 Python dataclass、沙箱改现有 `run(...)`、审计明确「无事件流，需内核只读缝」、A2A 去掉「复用 connector」。
> - v1.2（2026-10-23）——落地前纠偏：否决审计直读；修正「结构化返回」歧义；写死资源超限错误码范围；A2A 可选 Port 不进 requires；固定审计 action/category 表；新码统一进 `PLUGIN_ERROR_CODES`；明确 Job 单进程、A2A 不直接建 Task、capabilities 枚举含义。
> - **v1.3（2026-10-23）——定稿**：补齐 v1.2 阻塞开发的 3 处硬缺口（插件 DB 会话、插件自有表迁移归属与路由注册、错误码/类别/能力的实际落点）；修正审计插件 `permissions` 自相矛盾；避开 `/audit/exports` 与 `/audit/{event_id}` 路由冲突；`422` 语义冲突；进程组 kill 与现状描述；workdir 根过窄。新增 §1.6 数据访问三类模型、§3.7 红线机器化校验、§12 交付验证命令。
> - v1.3.3（2026-10-26）——标准插件可移植性落地：`app/plugin_sdk/` 公开契约包、沙箱实现下沉、方案 b 表归属、entry_points 发现、结构化 `config_schema`、`api_version` 校验，见 §12.7。
> - **v1.3.4（2026-10-26）——登记能力语义变更**：未注入 `sandbox.driver` 时 `MdSkillRuntime` 由「内核默认实现」改为拒绝执行 `AI_CAPABILITY_UNAVAILABLE`(503)，在 §十 变更与兼容表中注明属**升级收紧**（对齐《内核契约 v1》§3 optional 缺失语义），非能力回退。
> - **v1.3.8（2026-10-26）——Port 协议与 ORM 解耦（标准插件遗留一期）**：`app/kernel/ports/{identity_provider,memory_short,approval_provider}.py` 的模型 import 移入 `TYPE_CHECKING`，import Port 协议不再连带拉起 `app.models`，单插件「零内核依赖」收口；新增子进程探针机器断言，见 §12.7。
> - **v1.4（2026-10-26）——M4 收口：sidecar 容器隔离 + A2A 联邦身份**：`sandbox.driver` 新增 docker CLI 容器实现（`sandbox_isolation_mode=container`，同 Port 同契约、社区默认 process 零变化）；A2A 联邦入站方案 B（对端 service account 共享密钥）+ 出站 per-peer mTLS；入站真 mTLS 如实登记为部署层职责，见 §4.10 / §6.5 / §12.4 / §12.5。

---

## 一、总览与纪律

### 1.1 四层叙事中的位置

| 层级 | 本批能力落点 | 现状 |
|------|--------------|------|
| 产品层（官方 6–8） | 沙箱、审计合规导出、A2A 网关均属企业版差异化官方插件 | 未启动 |
| 能力域层（Module） | `execution`（沙箱，已有）、`governance`（审计导出，已有）、`a2a`（**不存在，需新建**） | `MODULE_REGISTRY` 9 个 |
| 装配层（Plugin） | `sandbox_driver`（已有，增强）、`audit_export_archive`（新建）、`a2a_gateway`（新建） | 19 个内置插件 |
| 契约层（Port） | **只用已锁定的 21 个 Port**，不新增 Port | 21/21 收口 |

### 1.2 插件五条纪律（含现状落点）

| # | 纪律 | 现状落点 |
|---|------|----------|
| 1 | manifest 唯一入口：`api_version / provides / requires / permissions / config_schema` | `PluginManifest`：Python dataclass，**无 YAML 加载器** |
| 2 | 只实现内核已定义 Port，不得自定义 | `validate_manifest` 对未知 Port 直接抛错（装配期执行） |
| 3 | 只经 PluginSdk 读写状态，禁止直连内核表 | `PluginSdk`：`settings / registry / engine / register_port / publish / shared`；**本批新增 `session()` / `read_audit()`（M0-5 / M0-2）** |
| 4 | 权限声明式：未声明的行为内核直接拒绝 | 现状**仅 `db.query` 有强制点**（`PluginSdk.engine`）；本批新增 `sdk.require_permission()` 作为通用强制点（M0-4） |
| 5 | 插件级熔断与配额强制 | `PluginGuardRegistry.check(key)` / `record(key, succeeded=...)`；内核仅在 `model.provider` 与 `skill.executor` 分派处挂钩，本批插件入口须**显式**调用 |

### 1.3 三条红线（插件同样遵守）

- **审计不可关闭**：导出/归档插件只消费，不得提供关闭写入的配置项或代码路径；不得删除审计行。
- **快照不可热改**：不得修改已发布 AgentVersion 的冻结配置。
- **身份不可绕过**：A2A 必须携带并校验 `SecurityContext`；禁止「任意身份执行」与「信任 `X-User-Id`」类接口。

### 1.4 与「父子 Task」的硬边界

| 能力 | 社区版现状 | 本批插件 |
|------|------------|----------|
| 单层父子 Task | 已实现：`parent_task_id / root_task_id / delegation_depth / child_sequence` | **不扩展、不替代** |
| A2A | 无 | 标准化插件：发现、路由、会话、身份透传、配额、审计 |

A2A 插件**不得**写入 `parent_task_id / child_sequence / delegation_id / root_task_id`（默认不桥接）。若产品要求桥接，必须显式配置且写入审计；v1 不实现桥接。

### 1.5 现状基线（硬约束，设计前必读）

| # | 约束 | 影响 |
|---|------|------|
| B-1 | `sandbox.driver` 签名 `async run(*, script_path: Path, arguments: dict, cwd: Path, timeout_seconds: float, max_output_bytes: int) -> dict`；返回值 = **脚本 stdout 解析出的 JSON（技能结果本身）**；超时 → `AI_SKILL_TIMEOUT`(504)；输出超限 / 非零退出 / 非 JSON → `AI_SKILL_FAILED`(500)；**无 `health()`、无 `denied/usage/exit_code` 返回字段** | 不得引入 `SandboxExecuteRequest/Result`；元信息走 sink + 审计 |
| B-2 | `observability.sink` 为同步单条 `emit(event: str, payload: dict \| None)` | 不能作为审计事件流 |
| B-3 | 审计写入是 **DB 直写**（`record_audit(db, ...)` / `record_audit_detached(...)`），无订阅、无事件总线 | 导出插件当前无合法数据源 → **必须先补内核只读缝（M0-2）** |
| B-4 | `GET /api/z-torque/audit` 需 `audit.read_all` 才看全租户、`limit ≤ 100`、offset 分页；另有 `GET /audit/{event_id}`（**UUID 强类型路径参数**）与 `/audit/by-trace/{trace_id}` | 不适合大批量导出；**新增路由不得挂在 `/audit/` 下**（见 §5.5） |
| B-5 | `object.store` 仅 `put` / `local_path` / `delete`，**无 `list`** | 归档索引与过期清理由插件自管；**v1 不扩展 Port** |
| B-6 | `secret.provider` 仅 `encrypt/decrypt(text)`，**无 `secret://` 名称解析** | 配置存密钥材料名或密文，插件自行 decrypt；不得假设 `resolve(ref)` |
| B-7 | `queue.driver` 仅 `put/get` Task UUID | 导出/归档 Job **不能**入该队列；用插件自有 Job 表 + 执行器 |
| B-8 | 装配硬校验：缺 `skill.executor` / `model.provider` / connector stdio registry → `AssemblyError` | 企业版 Profile 必须仍包含这三项 |
| B-9 | `PluginManifest` 无 `kind/metadata/priority/license/healthcheck/circuit_breaker/quota`；`permissions` 为扁平元组；`config_schema` **不自动校验** | 模板按现有字段；扩展字段为可选向后兼容目标 |
| B-10 | 沙箱隔离现状：独立解释器 `-I`、最小 env、超时 `process.kill()`（**未做进程组 kill**）、输出上限；**无 rlimit / 网络策略 / 工作目录写保护**；Windows 无等价隔离 | 验收分平台；`isolation_level` 如实上报 |
| B-11 | `connector` Port = MCP（`initialize / tools/list / tools/call`） | A2A 出站**不得**复用 connector；自建 HTTP 客户端 |
| **B-12** | `PluginSdk` **无 session 入口**；`engine` 属性被 `db.query` 门禁硬卡（未声明即 `PermissionError`，`manifest.py:75`） | A2A 认证/鉴权需 `Session`；审计 Job 表需写库 → **必须 M0-5** |
| **B-13** | 装配在 **lifespan startup** 执行（`main.py:38`），路由在 **import 期**无条件 `include_router(api_router)`（`main.py:107`）；`_FEATURES` 为静态清单，仅靠 `_FEATURE_PORT_DEPS` 按 Port 裁剪 | 「未装配不注册路由」不可行；新 feature 需按**插件名**派生 → M0-6 / M0-7 |
| **B-14** | 迁移目录为仓库根 `alembic/versions`（`alembic.ini`：`script_location = alembic`）；内核与 API 不得 import 插件；插件可建子包（`app/builtin_plugins/__init__.py` 存在） | 插件自有表 DDL 必须由**内核侧 alembic revision** 创建，插件不自带迁移器 |
| **B-15** | `errors.py` 仅 `MD_SKILL_ERROR_CODES` / `TASK_ERROR_CODES`，`ApiError` 是裸 dataclass，**不校验 code 是否在集合内** | `PLUGIN_ERROR_CODES` 只能是「登记 + 测试断言」→ 见 §8 |
| **B-16** | `BUILTIN_PLUGINS` 在 import 期对全部 19 个 manifest 跑 `validate_manifest`（`plugins.py:163`）；`AssemblyResult.plugins` 已返回全部 manifest | 新增包须同步注册；capabilities 可直接消费 `result.plugins` |

### 1.6 插件数据访问三类模型（v1.3 新增）

「禁止直连内核表」不等于「禁止持久化」。同一 DB 内，插件只能落到以下三类之一：

| 类型 | 允许的数据面 | `db.query` | 本批插件 |
|------|--------------|:----------:|----------|
| **I. 无持久化** | 只用 Port（含只读缝），不碰 DB | 否 | `sandbox_driver` |
| **II. 自有表** | 只读写 `zr_<plugin>_*` 命名空间自有表；审计事件**只能**经 `sdk.read_audit` | 是 | `audit_export_archive` |
| **III. 身份/策略适配** | 只读身份/策略投影 + 调用 `identity.provider` / `authz.provider`；禁写内核业务表 | 是 | `a2a_gateway` |

裁决细节：

1. **`db.query` 是机制许可，不是行为许可**。声明后可触达全库，因此必须配合 §3.7 静态校验（表名/模型名白名单）+ PR 逐条评审。
2. **审计业务数据永远走只读缝**：`audit_export_archive` 即便有 `db.query`，也不得 `select(AuditLog)`；`sdk.read_audit()` 内部自建会话，与插件权限无关。
3. **禁止长生命周期 Session**：每次 `sdk.session()` 为短事务，退出即关；禁止把 Session 放进 `sdk.publish` 或模块级变量。
4. **禁止 DDL**：插件代码不得出现 `create_all` / `CREATE TABLE`；表由 M0-6 的 alembic revision 建立。

---

## 二、Port 与 Module 映射

### 2.1 三插件映射

| 插件 | 归属 Module | provides | requires | permissions | 说明 |
|------|-------------|----------|----------|-------------|------|
| `sandbox_driver` | `execution`（已在册） | `sandbox.driver` | 无 | `process.spawn`（**仅声明，内核不拦截**） | **已有内置插件**，本批增强，不新建、不改名 |
| `audit_export_archive` | `governance`（追加） | `()` | `object.store`、`secret.provider`、`observability.sink` | `db.query`（**仅自有 Job/索引表**） | **不**实现 sink；**不**入 `queue.driver`；审计数据经 `sdk.read_audit` |
| `a2a_gateway` | 新建 `a2a` | `()` | `skill.executor`、`identity.provider`、`authz.provider`、`secret.provider`、`observability.sink` | `network.outbound`、`db.query`（**仅身份/策略适配与只读 Agent 投影**） | **不** require `connector`；可选 `approval`/`notify` 仅 `sdk.shared` 判空，**不进** requires |

### 2.2 命名与归属裁决

| 议题 | 裁决 |
|------|------|
| 命名 | 统一 **snake_case**：`sandbox_driver` / `audit_export_archive` / `a2a_gateway` |
| 沙箱是否新建 | **否**。在现有文件内增强，保持 `manifest.name` 与装配引用不变 |
| 审计是否实现 sink | **否**。避免与内置 `observability_sink` 冲突；改为内核只读缝 + 插件 API |
| 审计数据访问 | **仅只读缝**。插件 `db.query` 直读审计表（方案 A）**已否决** |
| 审计是否需要 db.query | **是**（v1.3 修正）。Job/索引表必须持久化，`permissions=()` 无法落库；边界由 §3.7 兜底 |
| A2A 是否新增 Port | **否**（v1）。`provides: ()`，能力经 Module + capabilities + 受控 API 暴露 |
| A2A 是否需要 db.query | **是**（v1.3 修正）。`identity.provider.authenticate(db, token)` 与 `authz.has_all(db, ...)` 均需 Session；改 Port 签名为破坏性变更，v1 不采用 |

### 2.3 企业版 Profile 装配硬约束

`assemble()` 结束时必须仍满足：有 `skill.executor` 实现、有 `model.provider` 实现、有 connector stdio registry。

因此 `PROFILE_ENTERPRISE` **不能**只装企业插件，必须在社区 Module 集合基础上追加 `a2a`，并在 `governance` 的 plugins 中追加 `audit_export_archive`，同时保留上述三项。

### 2.4 权限矩阵（本批唯一口径）

| 插件 | `permissions` | 强制点 | 登记用途 |
|------|---------------|--------|----------|
| `sandbox_driver` | `("process.spawn",)` | v1 **不强制**（子进程经 `asyncio.create_subprocess_exec` 创建，无拦截层） | 供企业 sidecar / M0-4 后续使用 |
| `audit_export_archive` | `("db.query",)` | `sdk.session()` / `sdk.engine`（机制强制） | 自有表读写 |
| `a2a_gateway` | `("network.outbound", "db.query")` | `sdk.require_permission("network.outbound")` 在**每次出站前**调用（M0-4）；`db.query` 由 `sdk.session()` 机制强制 | 出站与身份适配 |

**禁止**为「方便」给 `sandbox_driver` 或任何插件追加 `db.query`。`process.spawn` 本批仍是登记，不得在文档/验收中宣称「已拦截」。

---

## 三、通用开发规范（以代码为准）

### 3.1 目录与模块形态

当前**没有** `plugin.yaml` 加载器。插件形态为：Python 类 + `manifest` 类属性 + `contribute(sdk)`。

```text
app/builtin_plugins/
├── __init__.py
├── sandbox_driver.py              # 已有：本批增强（保持文件名与 manifest.name）
├── audit_export_archive/          # 新建包
│   ├── __init__.py                # 仅导出 AuditExportArchivePlugin
│   ├── plugin.py                  # PluginManifest + contribute
│   ├── exporter.py                # 分片 / 脱敏 / manifest.json
│   ├── archiver.py                # 冷副本 + 批次索引
│   ├── jobs.py                    # Job 状态机 + 线程池执行器
│   └── config.py                  # contribute() 内校验（config_schema 不自动校验）
└── a2a_gateway/                   # 新建包
    ├── __init__.py
    ├── plugin.py
    ├── gateway.py                 # 路由 / 深度 / 扇出 / 熔断
    ├── protocol.py                # 请求响应标准化
    ├── peers.py                   # 白名单 / 出站 HTTP / 响应上限
    └── config.py
```

硬性约束：

- `app/kernel`、`app/api`、`app/services` **不得** import 插件包；仅 `app/assembly.py` 加载。
- 新增插件须在 `app/builtin_plugins/plugins.py` 的 `BUILTIN_PLUGINS` 元组登记；否则 `expand_profile` 抛 `AssemblyError: profile references unknown module or plugin`。
- 包内 `__init__.py` 只做符号导出，**不得**在 import 期做 IO / 读配置 / 连库。
- 企业专有实现可通过 `assemble(..., extra_plugins=(...))` 接入。

### 3.2 Manifest 模板（可直接装配）

```python
from app.builtin_plugins.manifest import PluginManifest


class AuditExportArchivePlugin:
    manifest = PluginManifest(
        name="audit_export_archive",
        description="审计导出与冷归档（不关闭、不绕过审计写入）",
        provides=(),
        requires=("object.store", "secret.provider", "observability.sink"),
        # 仅用于自有 Job/索引表；审计事件数据必须走 sdk.read_audit（M0-2）
        permissions=("db.query",),
        version="0.1.0",
        config_schema={...},  # 仅元数据；校验在 contribute() 内自行完成
    )

    def contribute(self, sdk) -> None:
        ...
```

| 字段 | 约束 |
|------|------|
| `name` | snake_case 唯一 |
| `provides` | 只能是 21 个 Port 名；空元组合法 |
| `requires` | 未 RESOLVED → `AssemblyError`；**可选能力不要写入** |
| `permissions` | `db.query` 有机制强制；`process.spawn` / `network.outbound` 本批为登记 + 自证检查（§3.7） |
| `config_schema` | 不自动校验；插件在 `contribute()` 内校验失败即 `AssemblyError` |

**目标态（非本批必须）**：为 dataclass 增加带默认值的 `display_name` / `official` / `healthcheck` / `circuit_breaker` / `quota`，既有 19 插件零改动。

### 3.3 实现约定

- 入口统一 `contribute(sdk: PluginSdk)`；其他 Port 用 `sdk.shared("<port>")` 并显式判空。
- 拒绝/异常路径审计用 `record_audit_detached(...)`（独立事务，内部自建 `SessionLocal`），避免业务回滚丢审计：

```python
from app.kernel.audit.service import record_audit_detached

record_audit_detached(
    tenant_id=ctx.tenant_id,
    action="A2A_DENIED",
    resource_type="a2a_call",
    resource_id=None,
    actor_user_id=ctx.user_id,
    summary="a2a inbound denied: missing a2a.invoke",
    detail={"reason": "authz"},
    trace_id=None,
)
```

- 敏感字段：导出/归档前脱敏；禁止明文密钥写入 `object.store`。
- 熔断/配额：本批在插件入口显式调用（签名已锁定，`check` 通过即计一次配额）：

```python
guard = PluginGuardRegistry.from_settings(sdk.settings)  # 或装配期 publish 共享实例
guard.check("plugin.a2a_gateway")
try:
    ...
    guard.record("plugin.a2a_gateway", succeeded=True)
except Exception:
    guard.record("plugin.a2a_gateway", succeeded=False)
    raise
```

- 数据库访问只允许两种写法：`sdk.read_audit(query)`（审计数据）、`with sdk.session() as db:`（自有表）；禁止 `sdk.engine.connect()`、禁止模块级 Session。
- `process.spawn` 是**登记项而非强制点**：`sandbox_driver` 用 `asyncio.create_subprocess_exec` 自建子进程（`sandbox_driver/process.py`），内核不参与 spawn，也没有任何拦截层 / 代理层。能力边界完全由插件自身实现保证（cwd 白名单、`-I` 独立解释器、最小 env、rlimit、进程组 `killpg`、输出上限、fail-closed），因此不得把 `process.spawn` 描述为「已拦截」或「沙箱边界」。

### 3.4 交付检查清单

- [ ] `validate_manifest` 通过（无未知 Port，`requires` 可解析）
- [ ] 只经 PluginSdk 访问配置/状态；未声明 `db.query` 时无 `sdk.engine`
- [ ] 拒绝路径走 `record_audit_detached`；动作已在类别表登记（§3.5）
- [ ] 新错误码已加入 `PLUGIN_ERROR_CODES`，且测试断言「插件抛出的码 ⊆ 集合」
- [ ] 单测 + 装配测试（缺 Port / 歧义 / 越权负面用例）
- [ ] `/capabilities` 如实反映，不虚报隔离或 A2A
- [ ] §3.7 红线静态校验通过
- [ ] 新增表已在内核侧 alembic revision 建立，`upgrade` / `downgrade` 双向可跑

### 3.5 审计动作与类别固定表（本批统一）

现状类别常量在 `app/api/routes/audit.py:40-48`，共 9 个：`config / model_call / tool_call / publish / file / confirmation / task / iam / kernel`，**没有** `audit / a2a / sandbox`。本批新增三个常量并登记动作：

| action | category（新增常量） | 说明 |
|--------|----------------------|------|
| `SANDBOX_DENIED` | `CATEGORY_SANDBOX = "sandbox"` | 沙箱策略拒绝 |
| `SANDBOX_RESOURCE_EXCEEDED` | `CATEGORY_SANDBOX` | rlimit 触发（仅 Linux/macOS） |
| `AUDIT_EXPORTED` | `CATEGORY_AUDIT = "audit"` | 导出成功 |
| `AUDIT_EXPORT_FAILED` | `CATEGORY_AUDIT` | 导出失败 |
| `AUDIT_ARCHIVED` | `CATEGORY_AUDIT` | 归档批次完成 |
| `A2A_INVOKED` | `CATEGORY_A2A = "a2a"` | 发起调用 |
| `A2A_DENIED` | `CATEGORY_A2A` | 身份/权限/深度拒绝 |
| `A2A_FAILED` | `CATEGORY_A2A` | 对端或路由失败 |

**必须同步的三处**（漏一处即前端「类型」列显示为「配置变更」）：

1. `audit.py` 新增 `CATEGORY_AUDIT / CATEGORY_A2A / CATEGORY_SANDBOX` 常量；
2. `_AUDIT_CATEGORY_BY_ACTION` 逐条登记（`_ACTIONS_BY_CATEGORY` 由其自动派生，**无需**手改）；
3. 前端 `console` 审计页的类别标签映射补三项中文名。

**禁止**把上述动作落进 `CATEGORY_CONFIG` 兜底（会把「拒绝」读成「配置变更」）。

### 3.6 权限码落地约定

种子位置已存在：`app/cli.py` 的 `ALL_PERMISSIONS`；`admin` 角色自动获得全部权限，`user` 角色按 `USER_ROLE_PERMISSIONS` 白名单授予。

| 权限码 | 用途 | 种子与默认角色 |
|--------|------|----------------|
| `audit.export` | 创建/查询导出 Job | 加入 `ALL_PERMISSIONS`；admin 自动拥有；`USER_ROLE_PERMISSIONS` **不加** |
| `audit.archive` | 手动触发归档 | 同上 |
| `a2a.invoke` | 发起/参与 A2A 调用 | 同上 |

- 新码未进种子 → `policy_source` 读不到 → 永远 403，且管理员无法自助授权；**种子与代码必须同 PR 提交**。
- 未装配对应插件时：路由常注册，返回 `AI_CAPABILITY_UNAVAILABLE`(503)，避免仅 403 难排查（与 §5.5 一致）。
- 社区版可存在权限码定义，但默认角色不授予。

### 3.7 红线机器化校验（v1.3 新增）

红线不能只靠文档，必须有一条会失败的测试。新增 `tests/unit/test_plugin_redlines.py`：

```python
from pathlib import Path

BANNED = {
    # 审计导出：禁止直读审计表 / 删除审计行 / 自建 DDL
    "audit_export_archive": ("select(AuditLog", "zr_audit_log", "delete(AuditLog",
                             "create_all", "CREATE TABLE"),
    # A2A：禁止桥接父子 Task、禁止直接建 Task、禁止直读审计表
    "a2a_gateway": ("parent_task_id", "child_sequence", "delegation_id",
                    "Task(", "zr_audit_log"),
}


def test_plugin_redlines() -> None:
    root = Path("app/builtin_plugins")
    for pkg, tokens in BANNED.items():
        for path in (root / pkg).rglob("*.py"):
            src = path.read_text("utf-8")
            for token in tokens:
                assert token not in src, f"{path} 触犯红线：{token}"
```

- 该测试是「机制声明 + 静态断言」的补偿：`db.query` 给的是全库触达能力，表级边界只能靠它 + 评审。
- 命中但确属必要（例如注释里出现 `parent_task_id`）→ 改写注释措辞，**不要**放宽断言。
- 另加一条装配断言：`audit_export_archive` 与 `a2a_gateway` **不在** `PROFILE_COMMUNITY` 展开结果内。

---

## 四、插件一：`sandbox_driver`（增强既有插件）

### 4.1 定位

| 项 | 内容 |
|----|------|
| Port | `sandbox.driver`（optional） |
| Module | `execution`（已在册） |
| 现状 | 已注册：`ProcessSandboxDriver` → 进程内受限脚本执行 |
| 本批目标 | 补标准件：**拒绝审计、元信息上报、capabilities、cwd 校验、配置收敛**；**不改 Port 签名、不改返回 dict 语义** |
| 企业版 | 同 Port 可替换为 sidecar；`run(...) -> dict` 语义保持一致 |

### 4.2 功能范围

**已有**：脚本路径校验、解释器 `-I`、最小环境变量、stdin JSON / stdout JSON、超时 kill、输出字节上限。

**本批补齐**：

1. **元信息不上返回值**：`duration_ms` / 是否截断 / `deny_reason` 经 `observability.sink.emit` + 审计上报；**不得**写入 `run()` 返回的 dict（该 dict 是技能结果，下游直接消费）。
2. **策略拒绝**：`ApiError("AI_SANDBOX_DENIED", ..., 403)` + `record_audit_detached(action="SANDBOX_DENIED")`。
3. **能力上报**：`/capabilities` 增加 `sandbox: { available, impl, isolation_level }`（派生方式见 §7.3）。
4. **配置收敛**：默认超时、输出上限、**允许工作根列表**（`cwd` 必须落在允许根之下，与 MD Skill 目录策略一致）。
5. **进程组 kill（本批新增，非现状）**：Linux/macOS 用 `start_new_session=True` 启动 + `os.killpg(os.getpgid(pid), SIGKILL)`，避免脚本派生子进程逃逸；Windows 退化为 `process.kill()`。

### 4.3 Manifest

```python
class SandboxDriverPlugin:
    manifest = PluginManifest(
        name="sandbox_driver",
        description="进程内受限脚本执行（独立解释器 -I + 最小环境变量）",
        provides=("sandbox.driver",),
        requires=(),
        permissions=("process.spawn",),  # 登记；当前无运行时强制
        version="0.1.0",
    )
```

- 不得为沙箱 ABC 新增抽象方法（如 `health()`）。健康态经 capabilities / 插件自有方法暴露。
- 企业版 sidecar 若需密钥，在实现内 `sdk.shared("secret.provider")` 判空；需要时再把 `secret.provider` 升为硬 requires。

### 4.4 配置

| 键 | 默认 | 说明 |
|----|------|------|
| `md_skill_script_enabled` | 现有 | 总开关；关闭时调用前返回 `AI_CAPABILITY_UNAVAILABLE` |
| `sandbox_default_timeout_seconds` | 现有默认 | 调用方未传超时时的兜底 |
| `sandbox_max_output_bytes` | 现有默认 | 输出上限收敛 |
| `sandbox_allowed_roots` | `[str(settings.md_skills_dir)]` | **新增**：`cwd` 必须位于其一之下（`Path.resolve()` 后比较前缀）；替代「硬绑 md_skills_dir」 |
| `sandbox_rlimit_enabled` | `True`（Linux/macOS） | **新增**：控制是否设 `RLIMIT_AS/RLIMIT_CPU`；Windows 忽略 |
| `mode` / 网络策略 | `in_process` / 无 | 仅企业版 sidecar；社区版恒为进程内 |

校验在 `contribute()` 或首次执行路径内完成（`config_schema` 不自动校验）。`sandbox_allowed_roots` 为空时必须 `AssemblyError`（否则等于无校验）。

### 4.5 Port 接口（现状，本批不改）

```python
class SandboxDriver(ABC):
    @abstractmethod
    async def run(
        self, *, script_path: Path, arguments: dict[str, Any],
        cwd: Path, timeout_seconds: float, max_output_bytes: int,
    ) -> dict[str, Any]: ...
```

**返回语义（锁定）**：返回 dict = 脚本 stdout 的 JSON 对象（技能结果）。不得塞入执行元信息。

### 4.6 失败语义（锁定）

| 场景 | 错误码 | HTTP | 说明 |
|------|--------|:----:|------|
| 超时 | `AI_SKILL_TIMEOUT` | 504 | **沿用现状**，本批不替换 |
| 输出超限 | `AI_SKILL_FAILED` | 500 | **沿用现状**（直接失败，不截断返回） |
| 非零退出 / 非 JSON | `AI_SKILL_FAILED` | 500 | 沿用 |
| 策略拒绝（路径越界 / cwd 越界） | `AI_SANDBOX_DENIED` | 403 | **本批新增** |
| CPU/内存 rlimit 触发 | `AI_SANDBOX_RESOURCE_EXCEEDED` | **429** | **仅 Linux/macOS**；Windows 不触发、不宣称。v1.2 曾写 422，与项目「422 = 参数非法」惯例冲突，v1.3 改 429（与插件配额超限同族，靠 code 区分） |

### 4.7 实现要求（分平台）

| 平台 | 可达能力 | `isolation_level` | 验收 |
|------|----------|-------------------|------|
| Linux / macOS | `-I`、可选 `resource.setrlimit`、进程组 kill（本批新增）、cwd 包含校验 | 有 rlimit 时为 `rlimit`，否则 `none` | 内存/CPU 超限、cwd 越界、超时、派生进程被清理 |
| Windows | `-I`、超时 kill、输出上限、cwd 校验；无 rlimit、无进程组 kill | **必须** `none` | 仅超时 / 输出超限 / cwd 越界 |
| 企业 sidecar | 容器隔离 + 网络策略 | `container` | 同 `run()` 契约 |

rlimit 触发判定：子进程被信号杀死且 `returncode` 为 `-SIGKILL`/`-SIGXCPU`，且本次执行设置了 rlimit → 报 `AI_SANDBOX_RESOURCE_EXCEEDED`；否则按非零退出处理（`AI_SKILL_FAILED`），**不得**把所有 SIGKILL 都归为资源超限（超时 kill 也会是 SIGKILL，需先判超时分支）。

### 4.8 测试要求

| 类型 | 用例 |
|------|------|
| 单元 | 超时 kill → `AI_SKILL_TIMEOUT`；输出超限 → `AI_SKILL_FAILED`；非零退出 / 非 JSON → `AI_SKILL_FAILED` |
| 单元 | cwd 越界 → `AI_SANDBOX_DENIED` 且写 detached 审计（`SANDBOX_DENIED`） |
| 单元 | 返回 dict 与脚本 stdout 完全一致（无包装字段）；元信息出现在 sink/审计 |
| 单元（Linux） | rlimit 内存超限 → `AI_SANDBOX_RESOURCE_EXCEEDED`；`sandbox_rlimit_enabled=False` 时退化为 `AI_SKILL_FAILED` |
| 集成 | SkillGateway → MD_SCRIPT → 沙箱 |
| 负面 | 脚本总开关关闭 → `AI_CAPABILITY_UNAVAILABLE` |

### 4.9 验收清单

- [ ] `run()` 签名与返回语义未变；现有调用方与相关单测全绿
- [ ] 元信息仅经 sink + 审计；返回 dict 无包装字段
- [ ] `SANDBOX_DENIED` 有 detached 审计且类别已登记
- [ ] capabilities 含 `isolation_level`，Windows 不虚报
- [ ] `sandbox_allowed_roots` 生效（越界拒绝 + 单测）

### 4.10 sidecar 容器隔离（v1.4 M4，已落地）

**形态**：`sandbox.driver` Port 的第二个实现 `ContainerSandboxDriver`（`app/builtin_plugins/sandbox_driver/container.py`），技术 = **docker CLI 子进程**（零新 Python 依赖；`sandbox_docker_binary` 兼容 podman 等同参数面 CLI）。`contribute()` 按 `sandbox_isolation_mode`（`process`（默认）| `container`）分支选择——社区默认 process，行为零变化。

**容器参数面**：`--rm -i --network none --read-only --security-opt no-new-privileges --memory --cpus --pids-limit`；白名单根只读挂载（`{root}:{root}:ro`）、workdir 同路径可写挂载（后挂覆盖父只读）；`-w {cwd}`；镜像内 `python -I` 执行脚本；stdin JSON 参数 / stdout JSON 结果。

**与 process 实现共享的语义**：cwd 白名单（`enforce_cwd_policy`，越界 `AI_SANDBOX_DENIED` 403）、错误码分类（超时 `AI_SKILL_TIMEOUT` 504、输出超限/非零退出/非 JSON `AI_SKILL_FAILED` 500）；**容器新增**：退出码 137/139（资源限制已显式配置）→ `AI_SANDBOX_RESOURCE_EXCEEDED` 429；脚本必须落在白名单根内（容器内只挂载白名单根，外部脚本不可见，spawn 前拒绝）。

**fail-closed 边界**：Windows 装配期直接 `AssemblyError`（容器运行时不承诺脚本级隔离等价性）；docker CLI/守护进程不可用 → 装配期 `AssemblyError`（`docker version` 探测）；镜像不预拉 → 运行期 125/127 归 `AI_SKILL_FAILED`。

**capabilities**：registry label = `builtin_container_sandbox(image=...)` 含 "container" → `isolation_level=container`（main.py 既有判定，零改动）；观测事件仍为 `sandbox.execution`，容器实现的 payload 用 `isolation="container"` 替代 `rlimit_enabled`（键集合固定，机器断言）。

---

## 五、插件二：`audit_export_archive`（审计导出与归档）

### 5.1 定位与阻塞点

| 项 | 内容 |
|----|------|
| Module | `governance`（追加插件，**仅企业 Profile 启用**） |
| provides | `()` |
| requires | `object.store`、`secret.provider`、`observability.sink` |
| permissions | `db.query`（**仅自有 `zr_audit_export_*` / `zr_audit_archive_*` 表**） |
| 审计数据源 | **仅**内核只读缝 `sdk.read_audit`（方案 A 直读审计表已否决） |

**阻塞（必须 M0 先完成）**：

1. **M0-2** 审计只读缝：`iter_audit_events` + `sdk.read_audit`
2. **M0-3** 索引方案：插件自有索引表（**不**给 `object.store` 增加 `list`）
3. **M0-5** `sdk.session()`（写 Job/索引表）
4. **M0-6** 表 DDL 的 alembic revision + 路由常注册策略
5. 权限码 `audit.export` / `audit.archive` 进入 `app/cli.py: ALL_PERMISSIONS`

**禁止**：关闭审计写入的配置；绕过脱敏导出明文；插件内 `select(AuditLog)`；删除审计行的 API；插件内 `create_all`。

### 5.2 功能范围

| 能力 | v1 范围 | 明确不做 |
|------|---------|----------|
| 导出 | 时间/租户/操作者/动作前缀过滤；JSONL/CSV；分片 + 清单（含 sha256） | 任意 SQL、跨租户合并 |
| 归档 | 热窗口后冷副本至 `object.store`；可选加密 | 归档对象原地编辑；**删除审计行** |
| 索引 | 插件自有 Job/索引表，按时间与批次查询 | 替代在线 `/audit` |
| 审计 | 导出/归档动作自身写审计（`AUDIT_*`） | 审计自身事件被再次导出造成递归放大 |

**递归防护**：导出查询默认排除 `action` 前缀 `AUDIT_`（可配置关闭，但关闭时必须显式审计 `AUDIT_EXPORTED` 的 `detail.recursive=True`），避免「导出动作把自己导进下一次导出」。

### 5.3 Manifest

```python
class AuditExportArchivePlugin:
    manifest = PluginManifest(
        name="audit_export_archive",
        description="经内核只读缝消费审计数据，提供导出与冷归档；不关闭、不绕过写入",
        provides=(),
        requires=("object.store", "secret.provider", "observability.sink"),
        # 仅自有 Job/索引表；审计业务数据只能走 sdk.read_audit
        permissions=("db.query",),
        version="0.1.0",
    )
```

> v1.2 写 `permissions=()` 与「插件自有 Job 表 + 写入」自相矛盾：`PluginSdk` 无 session 入口、`engine` 被门禁。v1.3 改为声明 `db.query`，并用法条与静态校验限定范围（§1.6 / §3.7）。

### 5.4 配置

| 键 | 默认 | 说明 |
|----|------|------|
| `hot_retention_days` | 90 | 超过窗口进入归档候选 |
| `export_max_rows` | 100000 | 单 Job 上限；超出直接拒绝（`AI_AUDIT_EXPORT_TOO_LARGE`），不隐式截断 |
| `export_batch_size` | 1000 | keyset 每批行数（同时是分片粒度） |
| `export_formats` | `["jsonl"]` | `jsonl` / `csv`；`csv` 需固定列序 |
| `archive.prefix` | `audit-archive/` | 对象键前缀 |
| `archive.encryption_enabled` | false | 开启时用 `secret.provider.decrypt(...)` 得到的材料做 AES-GCM |
| `archive.encrypt_key_ciphertext` | 空 | 密钥材料密文；明文密钥**不得**出现在配置文件 |
| `redaction_profile` | `standard` | `standard` / `strict` |
| `exclude_own_actions` | true | 导出时排除 `AUDIT_*` 自身动作 |

`archive.encryption_enabled=true` 且 `encrypt_key_ciphertext` 为空 → `contribute()` 抛 `AssemblyError`（不得运行期才发现）。

### 5.5 接口约定

**（1）内核只读缝（M0-2，唯一合法数据源）**

```python
# 目标：app/kernel/audit/reader.py
def iter_audit_events(
    db: Session, *, tenant_id: UUID,
    start: datetime | None, end: datetime | None,
    action_prefix: str | None, actor_user_id: UUID | None,
    after: tuple[datetime, UUID] | None,   # keyset：created_at, id
    limit: int,
) -> Iterator[dict[str, Any]]: ...

# PluginSdk（内部自建 SessionLocal，按批提交，不占用插件长事务）
def read_audit(self, query: AuditQuery) -> Iterator[dict[str, Any]]: ...
```

要求：强制本租户（`tenant_id` 不从客户端取，取 `SecurityContext`）；keyset 分页（禁止深 OFFSET）；字段白名单脱敏；`read_audit` **不得**在迭代过程中调用 `record_audit`（防自我放大）。

**（2）插件内部服务**

```python
class AuditExportService:
    def submit(self, query: AuditExportQuery, ctx: SecurityContext) -> str: ...   # job_id
    def status(self, job_id: str, ctx: SecurityContext) -> ExportJobView: ...
    def archive_due(self, ctx: SecurityContext) -> ArchiveResult: ...
```

**（3）HTTP 面（独立前缀，不得挂 `/audit/` 之下）**

| 方法 | 路径 | 权限 |
|------|------|------|
| POST | `/api/z-torque/audit-exports` | `audit.export` |
| GET | `/api/z-torque/audit-exports/{job_id}` | `audit.export` |
| POST | `/api/z-torque/audit-archive-runs` | `audit.archive` |

**路径裁决（v1.3 修正）**：`app/api/routes/audit.py` 已存在 `GET /audit/{event_id}`（`event_id: UUID` 强类型）。若把导出写成 `/audit/exports`，请求会被 `{event_id}` 先行匹配并因 UUID 解析失败返回 422。因此本批用**独立 router**（`prefix="/audit-exports"`、`prefix="/audit-archive-runs"`），不进入现有 `audit` router 的路径空间。

**路由注册策略（B-13 决定的唯一可行方案）**：装配发生在 lifespan、路由注册在 import 期，故「未装配不注册路由」**做不到**（除非在 lifespan 内 `include_router`，会破坏 OpenAPI 一致性）。v1 采用：

- 路由**始终**注册（`app/api/router.py` 正常 include）；
- handler 首行判定 `request.app.state.assembly_plugins` 中是否存在 `audit_export_archive`：
  - 不存在 → `ApiError("AI_CAPABILITY_UNAVAILABLE", "audit export plugin is not assembled", 503)`；
  - 存在 → 继续权限校验与业务逻辑。
- 权限校验顺序：**先能力、后权限**（未装配时不应要求用户具备 `audit.export`）。

### 5.6 实现要求

1. **自有表（M0-6，内核侧 alembic revision 建表，插件无 DDL）**：

  | 表 | 关键列 | 说明 |
  |----|--------|------|
  | `zr_audit_export_job` | `id`(UUID) / `tenant_id` / `status`(PENDING/RUNNING/SUCCEEDED/FAILED) / `query_json` / `format` / `rows_total` / `shards_total` / `object_prefix` / `error_code` / `lease_owner` / `lease_expires_at` / `attempts` / `created_at` / `finished_at` | 单 Job 一行；后三列为多副本租约（§5.9） |
  | `zr_audit_export_shard` | `job_id` / `seq` / `object_key` / `rows` / `sha256` / `created_at` | 每分片一行，唯一约束 `(job_id, seq)` |
  | `zr_audit_archive_batch` | `id` / `tenant_id` / `window_start` / `window_end` / `rows` / `objects`(JSON) / `sha256_total` / `created_at` | 归档批次索引 |

  > **修订（2026-10-26，方案 b 定稿）**：上表结构不变，但**模型与迁移文件的物理位置**改为跟随插件目录 ——
  > `app/builtin_plugins/audit_export_archive/{models.py, migrations/f6b7c8d9e0a1_audit_export_archive_tables.py}`。
  >
  > 三条不变式仍然成立，故不构成对「插件不建表」的推翻：
  > 1. **插件运行时仍无 DDL 权**（代码里不得出现 `create_all` / `CREATE TABLE`）；
  > 2. **迁移仍由内核唯一执行** —— `alembic.ini::version_locations` 声明两个目录（`alembic/versions` + 插件 `migrations/`），
  >    仍是同一条链、单一 head `c7a1e4b90d23`（v1.3.6 起；此前为 `b3d6f9a2c5e8`）；
  > 3. **`Base.metadata` 注册由内核显式完成** —— `alembic/env.py` 与 `tests/conftest.py` 显式 import 插件 `models.py`。
  >
  > 动因：企业版要能「拷插件目录即用」，模型留在 `app/models/` 会让插件反向依赖内核模型层（原 20 处越界之一）。
  > 验收：`alembic heads` 单一 head；空 SQLite / 空 PG 上 `upgrade head` + `alembic check` 零漂移。

2. **Job 执行**：线程池（`concurrent.futures.ThreadPoolExecutor(max_workers=AUDIT_EXPORT_MAX_CONCURRENCY)`），提交即返回 `job_id`。
   - 多副本：执行权由**行级租约**仲裁，见 §5.9；单副本行为与既有语义一致。
   - 启动恢复：`contribute()` 内把「租约可判定为本实例或已过期」的 PENDING/RUNNING Job 标为
     `FAILED(error_code="AI_AUDIT_EXPORT_FAILED")`，避免永久挂起；其他副本持有效租约的行**不动**。
   - **禁止**伪造 Task 入 `queue.driver`。
3. **分批与幂等**：每批写 `object.store` 后立即落 `zr_audit_export_shard`；全部完成写 `manifest.json`（批次列表、行数、每批 sha256、总 sha256）；失败重试不得产生重复分片（`(job_id, seq)` 唯一 + `object_key` 覆盖写）。
4. **脱敏**：`token/password/secret/api_key/authorization` 等键替换为 `***`；`strict` 对主体 id 做 HMAC（密钥不出现在导出物中）；`detail` 递归脱敏，深度上限 8。
5. **归档**：只生成冷副本 + 索引；**不提供删除审计行 API**；运维清理走独立审批/离线流程。
6. **自身审计**：成功/失败均写 `AUDIT_*`（§3.5）；失败路径用 `record_audit_detached`。
7. **熔断**：键 `plugin.audit_export_archive`；`submit` / `archive_due` 入口 `check`，结束 `record`。
8. **失败**：单批重试 3 次（指数退避 1/2/4s），仍失败则 Job=`FAILED` 并保留已完成分片；不得静默跳行。

### 5.7 测试要求

| 用例 | 断言 |
|------|------|
| 脱敏 | 导出物无明文密钥类字段；`detail` 深层键亦被替换 |
| 租户隔离 | 不能导他租户事件（`tenant_id` 取 `SecurityContext`，改请求参数无效） |
| 分页 | keyset 无重复、无遗漏（含同 `created_at` 多行，靠 `(created_at, id)` 兜底） |
| 完整性 | `manifest.json` 中 sha256 与分片一致；总 sha256 可复算 |
| 关闭审计 | 配置与 API 均无「关闭审计」能力（grep `record_audit` 不受配置分支控制） |
| 权限 | 无 `audit.export` → 403；跨租户 → 403；未装配 → 503 |
| 数据路径 | §3.7 静态校验通过（无 `select(AuditLog)`、无 `zr_audit_log`） |
| 幂等 | 同 Job 重试不产生重复分片；并发提交不共享 Session |
| 递归 | 默认导出不含 `AUDIT_*` 自身事件 |

### 5.8 验收前置

- [ ] M0-2、M0-3、M0-5、M0-6 落地
- [ ] 权限码进种子；类别三处同步完成
- [ ] 社区 Profile 默认不装配本插件；未装配时路由 503
- [ ] alembic `upgrade head` / `downgrade -1` 双向验证

### 5.9 多副本租约

**问题**：单进程实现里「重启即把所有 PENDING/RUNNING 标 FAILED」在多副本下会误杀其他副本正在跑的任务（用户看到假失败，且分片被对方继续写、状态却已 FAILED）。

**机制**（`jobs.py`，三列加在 `zr_audit_export_job`）：

| 列 | 类型 | 语义 |
|---|---|---|
| `lease_owner` | `varchar(128)` null | 持有者 `host:pid:rand`；NULL = 无主（PENDING 或迁移前存量行） |
| `lease_expires_at` | `timestamptz` null | 到期即视为持有者失联；续租间隔必须 `(0, ttl)` |
| `attempts` | `int` not null default 0 | 被抢占次数；>1 说明发生过接管 |

- **抢占**：`_claim` 是唯一仲裁点，一条条件 UPDATE（`id=? AND status='PENDING' AND (owner IS NULL OR expired)`）→ `rowcount==1` 才算赢。禁止「先 SELECT 再判断」，那是竞态。
- **续租**：每个分片边界（`flush()`）按 `AUDIT_EXPORT_LEASE_RENEW_INTERVAL_SECONDS` 续；**丢租即抛错中止**——继续写会让两个实例同时追加 `part-0000N`，导出件不可复算。
- **收尾**：`_mark_failed` 先校验 owner ∈ {NULL, 本实例}，否则直接返回（不写失败、不删分片）；成功与失败都清空 `lease_owner/lease_expires_at`。
- **恢复**：`recover_interrupted` 只回收 `owner IS NULL OR owner=me OR lease_expires_at<now` 的行，有效租约行一律跳过。
- **配置**：`AUDIT_EXPORT_LEASE_TTL_SECONDS`（默认 60，≥5）、`AUDIT_EXPORT_LEASE_RENEW_INTERVAL_SECONDS`（默认 20）、`AUDIT_EXPORT_INSTANCE_ID`（留空自动生成）。
- **不变的部分**：单副本下所有行 owner 要么是 NULL 要么是本实例，回收行为与旧实现完全一致；`attempts` 只在抢占时 +1，不影响状态机。
- **验收**：`tests/unit/test_audit_export_archive.py` 五条租约用例（互斥抢占、跳过对端有效租约、过期后回收、续租/丢租、丢租不写失败、终态释放）。

---

## 六、插件三：`a2a_gateway`（A2A 标准化网关）

### 6.1 定位

| 项 | 内容 |
|----|------|
| Module | **新建 `a2a`**；`PROFILE_COMMUNITY` **不包含** |
| provides | `()` |
| requires | `skill.executor`、`identity.provider`、`authz.provider`、`secret.provider`、`observability.sink` |
| permissions | `network.outbound`（出站强制点）、`db.query`（仅身份/策略适配 + 只读 Agent 投影） |
| 可选依赖 | `approval.provider`、`notify.channel`：仅 `sdk.shared` 判空，**不写进 requires** |
| 与父子 Task | 完全独立；默认不桥接 |

### 6.2 功能范围与数据边界

| 能力 | v1 实现方式 | 明确不做 |
|------|-------------|----------|
| 发现 | **只读投影**：查已发布 Agent 描述（`agent_versions` 中 `status=PUBLISHED` 的 name/description/capabilities），只读不写 | 暴露草稿/未发布配置；暴露提示词全文 |
| 路由 | `local` → 委托 `skill.executor`（SkillGateway）；`remote` → 自建 HTTP（B-11） | 复用 `connector`（MCP 专用） |
| 身份 | 出站按 `identity_propagation`；入站见 §6.5 | 联邦身份、跨域信任 |
| 治理 | 全链路审计、深度/扇出硬限、对端级熔断与配额；可选审批 | 修改已发布版本、代调用方创建 Task |

**数据边界（v1.3 写死）**：`db.query` 只允许出现在 `identity.py` / `discovery.py` 两个模块，且只读；任何写入内核业务表（`zr_task`、`zr_agent_version`、`zr_audit_log`）的代码路径一律禁止（§3.7 静态校验覆盖）。

### 6.3 Manifest

```python
class A2AGatewayPlugin:
    manifest = PluginManifest(
        name="a2a_gateway",
        description="Agent-to-Agent 发现、路由与会话；独立于单层父子 Task",
        provides=(),
        requires=(
            "skill.executor",
            "identity.provider",
            "authz.provider",
            "secret.provider",
            "observability.sink",
        ),
        # network.outbound：每次出站前 require_permission 自证（M0-4）
        # db.query：仅身份/策略适配与只读 Agent 投影（M0-5 提供 session）
        permissions=("network.outbound", "db.query"),
        version="0.1.0",
    )
```

**不要**把 `connector` 写入 requires。

### 6.4 配置

| 键 | 默认 | 说明 |
|----|------|------|
| `enabled` | false | 默认关闭 |
| `local_discovery` | true | 暴露本地 Agent 描述 |
| `remote_peers` | `[]` | `[{id, base_url, auth_secret_name}]`；`auth_secret_name` 指向可 `decrypt` 的材料，非 `secret://` |
| `max_depth` | 2 | 基于 `X-A2A-Depth` |
| `max_fanout` | 5 | 单次派生并发上限 |
| `default_timeout_seconds` | 120 | 出站超时 |
| `identity_propagation` | `user_id_header` | `none` / `user_id_header` / `full_context_token` |
| `allowed_peers` | `[]` | 出站白名单；空 = 全部拒绝 |
| `max_response_bytes` | 1_000_000 | 响应体上限（超限即失败，不截断解析） |

`allowed_peers` 为空且 `remote_peers` 非空 → `contribute()` 抛 `AssemblyError`（否则等于默认全开，违背白名单语义）。

### 6.5 入站身份（M0-1 + M0-5，v1 仅方案 A）

| 方案 | 机制 | v1 / v1.4 |
|------|------|-----|
| **A. 本域 JWT（采用）** | 调用方携带本域 JWT → `identity_provider.authenticate(db, token)` → `authz.has_all(db, ctx, ["a2a.invoke"])` | **支持**（默认通道） |
| **B. 对端共享密钥 → service account（v1.4 采用）** | 对端以共享密钥（`auth_secret_name` 同源，凭据即身份）作 Bearer 直打本域 A2A；`a2a_federation_enabled=true` 时先试联邦、未命中静默回落本域 JWT | **支持**（`FederatedPrincipalRegistry`，见下） |
| C. mTLS | 证书体系 | **部署层职责**（uvicorn 不向 ASGI 应用暴露客户端证书，应用层无法实现；出站方向 v1.4 已支持 per-peer mTLS） |

**方案 B 实现约束（v1.4）**：

- 联邦对端 = peer 配置 `inbound_federation=true` + `auth_secret_name` + `tenant_id`（**显式声明租户**，装配期查 `sdk.list_tenant_ids()` 校验存在性，不做启发式）；
- 共享密钥经 secret.provider 在**装配期**预解密（明文只在本进程内存，不落库/不进日志/不进审计）；解不开 / tenant 不存在 / 开关开启却零主体 → `AssemblyError`；
- 命中 → `SecurityContext(tenant_id=配置声明, user_id=None)`——**联邦主体不是本地用户**：合成 UUID 会撞 `zr_audit_log.actor_user_id` 的 `zr_user` 外键，归属走审计 `detail.principal_type`（local_jwt/federated_peer）与 `detail.caller_peer_id`；
- 凭据即授权（service account 语义）：命中即视为已授权 `a2a.invoke`，不走本地角色体系；技能级 `required_permissions` 由内核 permission_checker default-deny（`user_id=None` + 非空权限集 → 拒绝）；
- `match` 用 `hmac.compare_digest` 恒定时间比较；未命中**不写审计**（静默回落本域 JWT，避免双留痕）。

实现约束：

- 方案 A 的 `authenticate` / `has_all` 都吃 `Session` → 必须用 `with sdk.session() as db:`（M0-5）。**不得**改 `identity.provider` 签名（破坏性）。
- 跨实例前提：**共享 JWT 密钥与用户体系**（同租户/同部署域）；联邦身份不在 v1。
- **禁止**信任裸 `X-User-Id` 头；`identity_propagation=user_id_header` 仅用于**出站**携带，入站一律重新认证。
- 会话（Session）：入站认证与鉴权用同一短事务；路由执行阶段另开事务，禁止跨 await 持有 Session。

调用链：`入站认证 → 深度检查 → authz →（可选 approval）→ 路由 → 结果标准化 → 审计`。

### 6.6 实现要求

- `enabled=false` → `AI_A2A_DISABLED` (503)，无出站。
- **不桥接 Task**：不写父子字段；**不**由 A2A 插件直接 `insert Task`。本地调用只经 `skill.executor`；是否创建 Task 由现有 Gateway/执行链决定。
- 深度：入站读 `X-A2A-Depth`（缺省 0，非整数 → `AI_A2A_DENIED`）；出站写 `depth+1`；超限 → `AI_A2A_DEPTH_EXCEEDED`。
- 扇出：`asyncio.Semaphore(max_fanout)`；超限排队或拒绝，不静默丢。
- 出站：强制 https（回环可 http）、`allowed_peers` 白名单、`allow_redirects=False`、响应体上限、每次出站前 `sdk.require_permission("network.outbound")`。
- **出站 mTLS（v1.4）**：peer 配置 `client_cert_secret_name` / `client_key_secret_name`（必须成对）与 `ca_bundle_secret_name`（可独立），材料经 secret.provider 解密 → owner-only（0600）临时文件 → 预构建 `SSLContext`（`load_cert_chain` + CA）以 `verify=` 传 httpx，用完即删（密钥不驻留磁盘）。实测本机 httpx 版本下 `cert=(certfile, keyfile)` 形式握手失败（`ReadError`），SSLContext 是可靠路径。材料解不开/非法 → `AI_A2A_PEER_UNAVAILABLE`(503)；PEM 不进日志/观测事件。
- **出站载荷语义（v1.3.7 明确）**：出站载荷描述「对端要做什么」，不是「我是怎么路由的」。
  - 调用方给了 `params.skill_version_id` → 出站改写为 `route=local` + 该版本 id，对端直接经 `skill.executor` 执行（`peer` 仅作观测字段保留）。
  - 未给版本（只有 `params.skill` 名）→ 保持 `route=remote` 原样转发，由对端的 peer 表继续路由。**v1 没有「技能名 → 版本」解析**（插件 DB 边界只允许 identity/discovery 查询），所以这条路径无法钉死要执行什么，跨实例场景请显式传 `skill_version_id`。
- 熔断键：`plugin.a2a_gateway` 与 `plugin.a2a_gateway.peer:{id}`。
- 审计动作见 §3.5；拒绝路径全部走 `record_audit_detached`。
- 错误映射：对端 4xx → `AI_A2A_DENIED`；超时 → `AI_A2A_TIMEOUT`；连接失败 → `AI_A2A_PEER_UNAVAILABLE`；路由缺失 → `AI_A2A_ROUTE_FAILED`；自身未启用 → `AI_A2A_DISABLED`。

### 6.7 测试要求

| 用例 | 断言 |
|------|------|
| 关闭态 | `AI_A2A_DISABLED`，无出站（用假 HTTP 客户端断言零请求） |
| 深度 | `X-A2A-Depth` 超限拒绝并审计；非整数头部拒绝 |
| 扇出 | 并发不超过 `max_fanout` |
| 身份 | 伪造 `X-User-Id` 无效；过期/异域 JWT → `AI_A2A_DENIED` |
| 未声明权限 | 移除 `network.outbound` 后出站抛 `PermissionError`（`require_permission` 生效） |
| Task 隔离 | 静态校验无 `parent_task_id` / `Task(`（§3.7） |
| 熔断 | 连续失败开路，恢复后半开 |
| 审计 | `A2A_*` 落库且 category=`a2a` |
| 真联调（v1.3.7，`ZTORQUE_A2A_LIVE=1`） | 两实例互调并真实执行技能版本；4xx→`AI_A2A_DENIED`、超时→`AI_A2A_TIMEOUT`、5xx/超限/自签 https→`AI_A2A_PEER_UNAVAILABLE`；连续失败达阈值→`AI_PLUGIN_CIRCUIT_OPEN`（详见 §13.5） |

### 6.8 立项前置

- [ ] M0-1（入站 = 方案 A）+ M0-5（`sdk.session()`）
- [ ] M0-4（`sdk.require_permission`，覆盖 A2A 出站路径）
- [ ] `a2a` Module + `PROFILE_ENTERPRISE` 仍满足 §2.3
- [ ] `capabilities.features` 含 `a2a` 当且仅当 `a2a_gateway` 已装配（§7.3）

---

## 七、装配与启用

### 7.1 Module 声明

```python
# app/builtin_plugins/modules.py 追加
ModuleSpec(
    name="a2a",
    description="Agent-to-Agent 发现、路由与会话（企业版）",
    plugins=("a2a_gateway",),
    requires_ports=("skill.executor", "identity.provider", "authz.provider"),
)
```

`ModuleSpec` 字段以现状为准：`name / description / plugins / requires_ports`；`plugins` 顺序即 `contribute` 顺序。

| Module | 插件（本批相关） |
|--------|------------------|
| `governance` | 现有 + **企业 Profile 追加** `audit_export_archive` |
| `execution` | 含已有 `sandbox_driver`（增强） |
| `a2a` | `a2a_gateway`（新建，仅企业） |

### 7.2 Profile

- `PROFILE_COMMUNITY`：现有 9 Module；**不含** `a2a`；**默认不**装配 `audit_export_archive`。
- `PROFILE_ENTERPRISE`（新建）：社区集合 + Module `a2a` + governance 追加 `audit_export_archive`，并**显式保留** `skill_executors` / `model_provider` / `mcp_connector`（满足 §2.3）。
- 覆盖单 Port：走 `settings.port_override_map` / `PORT_OVERRIDES`，无 `assemble(overrides=...)` 形参。
- Profile 选择：`app/main.py` 显式传入 `assemble(settings, engine, profile=...)`，来源为 `settings.assembly_profile`（默认 `"community"`）。**禁止**在 `assemble()` 内部读环境变量隐式切换。
- 新增配置项，默认值保证社区行为不变：

```python
# app/config.py
assembly_profile: str = "community"   # community | enterprise
```

### 7.3 能力面（必须与装配结果联动）

现状：`main.py:157` 是静态 `_FEATURES`，仅 `_FEATURE_PORT_DEPS` 按 Port 裁剪；本批三个新能力**不新增 Port**，因此必须按「插件名」派生。落地三处：

1. `main.py` lifespan 暴露装配插件清单（`AssemblyResult.plugins` 已存在，零成本）：

```python
app.state.assembly_plugins = tuple(m.name for m in result.plugins)
```

2. 派生映射 + 可用性判定：

```python
_FEATURE_PLUGIN_DEPS = {
    "sandbox": "sandbox_driver",
    "audit_export": "audit_export_archive",
    "a2a": "a2a_gateway",
}

def _available_features(registry, plugins: tuple[str, ...] = ()) -> list[str]:
    ...
    for feature, plugin_name in _FEATURE_PLUGIN_DEPS.items():
        if feature in available and plugin_name not in plugins:
            available.remove(feature)
```

3. `/capabilities` 响应示例：

```json
{
  "sandbox": {"available": true, "impl": "in_process", "isolation_level": "none"},
  "audit_export": {"available": false, "formats": [], "archive": false},
  "features": ["...", "sandbox"]
}
```

字段来源（写死，避免再出现「静态清单」）：

| 字段 | 来源 |
|------|------|
| `sandbox.available` | `sandbox_driver` 在 `assembly_plugins` 且 `registry.resolution("sandbox.driver")` 非空 |
| `sandbox.impl` | `registry.resolution("sandbox.driver").label`（内置为 `builtin_process`） |
| `sandbox.isolation_level` | `"rlimit"` if `settings.sandbox_rlimit_enabled and sys.platform != "win32"` else `"none"`；容器 sidecar 由 label 含 `container` 判定 |
| `audit_export.available` / `formats` / `archive` | 插件已装配 → 配置值；未装配 → `false / [] / false` |
| `features` 含 `a2a` | 当且仅当 `a2a_gateway` 已装配 |

`isolation_level` 枚举含义：

| 值 | 含义 |
|----|------|
| `none` | 仅 `-I` / 超时 / 输出上限（含 Windows） |
| `rlimit` | 在 none 基础上具备 CPU/内存 rlimit（Linux/macOS） |
| `container` | sidecar/容器（企业版） |

未实现级别**禁止**上报。新增 feature 后需回归 `derive_stage` 相关单测（`app/kernel/capabilities.py`），避免 stage 判定被新 feature 影响。

### 7.4 启用顺序

1. **M0** 七项裁决内核改造（§9）
2. **M1** `sandbox_driver` 增强（无新内核依赖，可并行启动）
3. **M2** `audit_export_archive`（依赖 M0-2/3/5/6）
4. **M3** `a2a_gateway`（依赖 M0-1/4/5）
5. **M4** 企业 sidecar / 远端 peer / 归档加密

---

## 八、错误码

现状：`ApiError(code, message, status_code, data)` 是裸 dataclass，`errors.py` 仅两个集合（`MD_SKILL_ERROR_CODES` / `TASK_ERROR_CODES`），**运行时不做 code 校验**（B-15）。本批新建集合 `PLUGIN_ERROR_CODES`，并**明确三个消费点**，否则集合只是装饰：

| 消费点 | 落点 | 作用 |
|--------|------|------|
| 测试断言 | `tests/unit/test_plugin_error_codes.py`：断言 §8 表内所有码 ⊆ `PLUGIN_ERROR_CODES`，且插件抛出的码 ⊆ 集合 | 防手滑写新码不提登记 |
| 文档校验 | 本文 §8 与代码集合逐条比对（PR 评审清单项） | 防「文档码 ≠ 代码码」 |
| 前端映射 | `console` 错误提示字典（可选码给通用兜底文案） | 防用户看到裸 code |

| 码 | HTTP | 归属 | 说明 |
|----|:----:|------|------|
| `AI_SANDBOX_DENIED` | 403 | sandbox | 策略拒绝（本批新增） |
| `AI_SANDBOX_RESOURCE_EXCEEDED` | 429 | sandbox | **仅** rlimit；Windows 不用；v1.3 由 422 改为 429（422 在本项目专表参数非法） |
| `AI_SKILL_TIMEOUT` | 504 | sandbox | **已有**，超时沿用 |
| `AI_SKILL_FAILED` | 500 | sandbox | **已有**，输出超限 / 非零退出 / 非 JSON |
| `AI_AUDIT_EXPORT_DENIED` | 403 | audit | 权限或范围非法（跨租户、越权过滤） |
| `AI_AUDIT_EXPORT_TOO_LARGE` | 413 | audit | 超出 `export_max_rows` |
| `AI_AUDIT_EXPORT_FAILED` | 500 | audit | Job 内部失败（分片写失败、恢复时超龄） |
| `AI_AUDIT_ARCHIVE_FAILED` | 502 | audit | 归档写入 `object.store` 失败（含密钥引用解不开 / 上一把密钥引用非法） |
| `AI_AUDIT_ARCHIVE_KEY_MISSING` | 502 | audit | 归档对象只能被已下线（retire）的密钥解开，或当前 + 上一把都解不开；**不得静默跳过** |
| `AI_A2A_DISABLED` | 503 | a2a | 未启用 |
| `AI_A2A_DENIED` | 403 | a2a | 身份/权限拒绝、深度头非法 |
| `AI_A2A_TIMEOUT` | 504 | a2a | 出站超时 |
| `AI_A2A_PEER_UNAVAILABLE` | 503 | a2a | 对端不可用 / 不在白名单 |
| `AI_A2A_DEPTH_EXCEEDED` | 400 | a2a | 深度超限 |
| `AI_A2A_ROUTE_FAILED` | 404 | a2a | 路由失败（本地无此 skill / 远端无此 peer） |

- 新码加入 `PLUGIN_ERROR_CODES`（`app/core/errors.py`），与既有两个集合并列，**不合并**（语义分层，避免 `MD_SKILL_*` 语义被稀释）。
- 不得用 `AI_CAPABILITY_UNAVAILABLE` 表达业务拒绝（它专表「能力/插件未装配」）。
- `429` 与插件配额共享状态码，前端以 `code` 区分文案，不得只看状态码。

---

## 九、里程碑

| 阶段 | 内容 | 前置 | 验收 |
|------|------|------|------|
| **M0** | 七项裁决落地（内核小改 + 常量/种子/迁移） | — | 见下节 |
| **M1** | `sandbox_driver`：拒绝审计、元信息上报、capabilities、cwd 允许根、进程组 kill | — | 签名/返回语义不变；Windows 如实 `none` |
| **M2** | `audit_export_archive` v1 | M0-2/3/5/6 | 脱敏/隔离/完整性；无直读审计表；无法关闭审计 |
| **M3** | `a2a_gateway` v1（本地 + 治理） | M0-1/4/5 | 不桥接 Task；伪造身份失败；熔断生效 |
| **M4** | sidecar、远端 peer、归档加密 | M1–M3 | 同 Port/Module 契约替换；capabilities 如实 |

### M0 裁决清单（开发前必须关闭）

| ID | 议题 | 决议（v1.3 定稿） | 落地位置 |
|----|------|-------------------|----------|
| M0-1 | A2A 入站凭证 | **方案 A**：本域 JWT + `a2a.invoke`；禁止信任裸用户头 | `a2a_gateway` 入站中间件 |
| M0-2 | 审计导出数据源 | **内核只读缝** `iter_audit_events` + `sdk.read_audit`；否决插件直读审计表 | 新增 `app/kernel/audit/reader.py`；`manifest.py` 加 `PluginSdk.read_audit` |
| M0-3 | `object.store` 无 list | **插件自有索引表**；v1 **不**扩展 `object.store.list` | §5.6 三张表 |
| M0-4 | 网络等权限强制点 | 新增 `PluginSdk.require_permission(name)`：未声明即 `PermissionError`；A2A 每次出站前调用 | `app/builtin_plugins/manifest.py` |
| **M0-5** | 插件 DB 会话（新增，P0） | 新增 `PluginSdk.session()`：受 `db.query` 门禁的**短事务**上下文管理器（异常回滚、正常提交、退出即关）；插件不得持有 | `app/builtin_plugins/manifest.py` |
| **M0-6** | 迁移归属 + 路由常注册（新增，P1） | ① 插件自有表由内核侧 alembic revision 创建；② `main.py` 暴露 `app.state.assembly_plugins`；③ 路由常注册 + handler 首行判定装配态返回 503 | `alembic/versions/`、`app/main.py`、`app/api/router.py` |
| **M0-7** | 错误码/类别/能力的实际落点（新增，P2） | ① `PLUGIN_ERROR_CODES` + 测试断言；② 三个 category 常量 + `_AUDIT_CATEGORY_BY_ACTION` 登记 + 前端标签；③ `_FEATURE_PLUGIN_DEPS` 派生 features | `app/core/errors.py`、`app/api/routes/audit.py`、`app/main.py`、`console` |

**M0-5 参考实现（照抄即可）**：

```python
from contextlib import contextmanager

@contextmanager
def session(self):
    """受 db.query 门禁的短事务会话；插件不得持有返回的 Session。"""
    if ENGINE_PERMISSION not in self._manifest.permissions:
        raise PermissionError(
            f"plugin {self._manifest.name} lacks '{ENGINE_PERMISSION}' permission"
        )
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
```

**M0-4 参考实现**：

```python
def require_permission(self, name: str) -> None:
    if name not in self._manifest.permissions:
        raise PermissionError(
            f"plugin {self._manifest.name} lacks '{name}' permission; action denied"
        )
```

---

## 十、变更与兼容

| 变更 | 类型 | 处理 |
|------|------|------|
| `PluginSdk.session()` / `read_audit()` / `require_permission()` | 增量（向后兼容） | 现有 19 插件不调用即零影响 |
| `PluginManifest` 增可选字段 | 向后兼容 | 全部给默认值 |
| `app.state.assembly_plugins` | 增量 | 仅新增属性，无消费方改动 |
| 新增 category 常量 | 增量 | 未登记动作仍兜底 `config`，前端补标签 |
| `PLUGIN_ERROR_CODES` | 增量 | 与既有集合并列，不改 `ApiError` 结构 |
| 插件自有表（3 张） | 增量 | 新 revision，`downgrade` 可删表 |
| `object.store` 增 `list` | Port 破坏性 | **本批不做**；未来走契约 §7 |
| `sandbox.driver` / `observability.sink` 增方法 | 破坏性 | **v1 不动** |
| 新建 `a2a` Module / `PROFILE_ENTERPRISE` | 增量 | 不改变社区 Profile 行为（默认 `assembly_profile=community`） |
| 新权限码 | 增量 | 必须写种子；默认角色策略见 §3.6 |
| **未注入 `sandbox.driver` 时 `MdSkillRuntime.execute` 返回 `AI_CAPABILITY_UNAVAILABLE`(503)** | **语义收紧（升级，非回退）** | 沙箱实现已下沉到 `sandbox_driver` 插件，内核不再保持默认后端。这是把《内核契约 v1》§3「optional 缺失 → UNAVAILABLE」**落实到沙箱**，而不是删能力：`community` 默认档已含该插件，升级后只要装配未被人为裁剪即无感；若部署方自定义了 Profile 并去掉 `sandbox_driver`，MD-Script 会由「内核兜底执行」变为「显式 503」，属于预期行为 |

> 上表最后一行是**升级说明**：依赖「内核兜底执行脚本」的部署必须先确认 `sandbox_driver` 在装配结果里（`app.state.assembly_plugins` 含该名 / `/capabilities` 的 `sandbox.available` 为 true），否则升级后该能力会立即不可用。

---

## 十一、相对 v1.2 的纠偏摘要（v1.3 变更点）

| # | v1.2 问题 | v1.3 处理 |
|---|-----------|-----------|
| 1 | 审计插件 `permissions=()` 却要写自有 Job 表 | 改为 `("db.query",)`，并用 §1.6 三类模型 + §3.7 静态校验限定范围 |
| 2 | A2A 方案 A 需 `Session`，但 SDK 无 session 入口、`engine` 被门禁 | 新增 M0-5 `sdk.session()`；A2A 声明 `db.query` 并限定到身份/发现两模块 |
| 3 | 「未装配不注册路由」与「装配在 lifespan、路由在 import 期」冲突 | 改为路由常注册 + handler 判定 `app.state.assembly_plugins` → 503；并补 M0-6 暴露清单 |
| 4 | `/audit/exports` 会被 `/audit/{event_id}`（UUID）先匹配并 422 | 改用独立前缀 `/audit-exports`、`/audit-archive-runs` |
| 5 | `AI_SANDBOX_RESOURCE_EXCEEDED` 用 422，与项目「422=参数非法」冲突 | 改 429，并规定超时 / 资源超限 / 非零退出的判定顺序，避免误归因 |
| 6 | 「插件迁移」无归属；内核不得 import 插件 | 表 DDL 归内核侧 `alembic/versions` revision，给出三张表结构 |
| 7 | 类别只提「登记动作」 | 写死三处同步：常量、`_AUDIT_CATEGORY_BY_ACTION`、前端标签 |
| 8 | `PLUGIN_ERROR_CODES` 无消费方 | §8 固定三个消费点（测试断言 / 文档比对 / 前端映射），并补 `AI_AUDIT_EXPORT_FAILED` |
| 9 | 进程组 kill 被列为「可达能力」，实际未实现 | 标注「本批新增」，并给出 Linux/macOS 与 Windows 的差异 |
| 10 | cwd 硬绑 `md_skills_dir` 过窄 | 新增 `sandbox_allowed_roots`（默认含 `md_skills_dir`，空配置即 `AssemblyError`） |
| 11 | capabilities 只说「需联动」，无落点 | 给出 `app.state.assembly_plugins` + `_FEATURE_PLUGIN_DEPS` + 字段来源表 |
| 12 | 未防导出递归放大（导出动作被再次导出） | 新增 `exclude_own_actions`（默认 true），关闭时须显式审计标记 |
| 13 | 缺少「红线会不会失效」的机制 | 新增 §3.7 `test_plugin_redlines.py` 静态校验 |

---

## 十二、交付与验证

### 12.1 建议提交顺序（每步可独立验收）

| # | 提交 | 验证 |
|---|------|------|
| 1 | M0-5 + M0-4（SDK 三个能力） | `pytest tests/unit/test_plugin_sdk*` + 新权限负面用例 |
| 2 | M0-2 只读缝 | keyset 分页单测（同 `created_at` 多行） |
| 3 | M0-6 迁移 + `assembly_plugins` + 路由骨架 | `alembic upgrade head` / `downgrade -1`；未装配时 503 |
| 4 | M0-7 常量/种子/features | category 单测 + `/capabilities` 断言 |
| 5 | M1 沙箱增强 | 分平台用例；现有 `run()` 调用方全绿 |
| 6 | M2 审计导出 | §5.7 全表；§3.7 静态校验 |
| 7 | M3 A2A 本地 + 治理 | §6.7 全表；§3.7 静态校验 |
| 8 | M4 sidecar / 远端 / 加密 | 同契约替换；capabilities 如实 |

### 12.2 验证命令（实测口径）

```bash
# 1) 全量（默认 SQLite，Windows / Linux 均可）
pytest tests -q

# 2) Linux 专属分支（rlimit / killpg）必须在 Linux 上跑，Windows 会 skip
docker run --rm -v "$PWD:/w" -w /w python:3.12-slim \
  sh -c "pip install -q -e '.[test]' pg8000 && pytest tests/unit/test_sandbox_hardening.py -q"

# 3) v1.3 专项（连跑可完整覆盖 §4.7 / §5.7 / §6.7 三张表）
pytest tests/unit/test_audit_reader.py tests/unit/test_audit_export_archive.py \
       tests/unit/test_a2a_gateway.py tests/unit/test_sandbox_hardening.py \
       tests/integration/test_v13_audit_export_api.py \
       tests/integration/test_v13_a2a_api.py \
       tests/integration/test_v13_permission_seed.py -q

# 4) PostgreSQL 方言验证：测试库必须显式指定，否则落到默认 SQLite（防误清生产库）
export ZTORQUE_TEST_DATABASE_URL=postgresql+pg8000://postgres:pass@127.0.0.1:5432/zt
pytest tests -q
#    注意：tests/support/ddl.py 的清理辅助在 PG 上依赖 session_replication_role，
#    测试库连接角色需要超级用户或具备 SET 权限。

# 5) 迁移链（PG，完整降级往返，不许只在 SQLite 上验）
export DATABASE_URL=postgresql+pg8000://postgres:pass@127.0.0.1:5432/zt_mig
alembic upgrade head && alembic downgrade base && alembic upgrade head && alembic current

# 5b) 迁移 == 模型 门禁（干净库才算数：历史库里可能被 create_all 修正过，看不出问题）
alembic check                                  # 0 差异才算通过
python scripts/check_migration_drift.py        # 有差异时输出逐条明细
# 在测试套件里顺手锁死（PG 侧需一个刚 upgrade 到 head 的空库，本用例只读）
export ZTORQUE_MIGRATION_CHECK_URL=postgresql+pg8000://postgres:pass@127.0.0.1:5432/zt_clean
pytest tests/integration/test_alembic_migrations.py -q

# 5c) 离线 DDL（面向空库；本项目仅 PG 分支可达）
alembic upgrade head --sql > schema.sql
psql -v ON_ERROR_STOP=1 -d zt_ddl -f schema.sql

# 6) 权限种子（CLI 无子命令，直接执行 init_admin；生产需强密码，否则启动前校验失败）
ADMIN_INITIAL_PASSWORD='<12 位以上强口令>' python -m app.cli

# 7) capabilities 投影
curl -s localhost:8000/api/z-torque/capabilities | python -m json.tool

# 8) A2A 真联调（起 uvicorn 子进程 + 真 TCP/TLS 对端；默认 skip，约 20s）
$env:ZTORQUE_A2A_LIVE="1"; python -m pytest tests/integration/test_a2a_live_peers.py -q
```

### 12.3 实施结果（2026-10-23 实测）

**用例规模**：`tests/` 全集 416 项（含迁移门禁 3 项：SQLite 零漂移、降级往返、PG 干净库零漂移）。

| v1.3 专项 | 数量 | 覆盖重点 |
|-----------|------|----------|
| `test_audit_reader.py` | 10 | 只读投影、keyset 分页、递归排除 |
| `test_audit_export_archive.py` | 15 | 分片/manifest/redact、失败回收、并发、重启恢复、归档工件 |
| `test_a2a_gateway.py` | 28 | 协议校验、深度/扇出、熔断、peer 白名单、身份传播、discovery 投影 |
| `test_sandbox_hardening.py` | 12 | cwd 白名单、超时/输出上限、rlimit、killpg 孙进程、Windows fail-closed |
| `test_v13_audit_export_api.py` | 5 | POST→轮询→下载→独立复算 sha256、AUDIT_* 排除、403 留痕、超限不产截断件 |
| `test_v13_a2a_api.py` | 5 | 未装配 503、入站鉴权拒绝（不写无法归属的审计行）、缺权限留痕、404、发现不外泄 |
| `test_v13_permission_seed.py` | 7 | 种子幂等、存量库补授权、user 角色不含高危权限 |

**三方言结果**：

| 环境 | 结果 | 说明 |
|------|------|------|
| Windows + SQLite | 411 passed / 2 skipped | skip 的是 POSIX rlimit 与 killpg 两条 |
| Linux(容器) + SQLite | 412 passed / 1 skipped | skip 的是 Windows fail-closed 一条 |
| PostgreSQL 16 + pg8000 | 412 passed / 1 skipped | 与 Linux 一致，无方言专属失败 |

**迁移**：全新库 `upgrade head` → `downgrade base` → `upgrade head` 全通过，`alembic current` = `f6b7c8d9e0a1 (head)`。

### 12.4 运维配置项（v1.3 新增，env 名 = 字段名大写）

| 环境变量 | 默认 | 影响 | 备注 |
|----------|------|------|------|
| `ASSEMBLY_PROFILE` | `community` | 决定是否装配 M2/M3 三插件 | `enterprise` 才启用；社区档行为不变 |
| `SANDBOX_ALLOWED_ROOTS` | 未配置 → `[MD_SKILLS_DIR]` | 脚本 cwd 白名单 | 显式空数组 = 装配失败（不允许"空配置=全放开"） |
| `SANDBOX_RLIMIT_ENABLED` | `true` | POSIX rlimit（CPU/FSIZE/NPROC） | Windows 无此语义，能力位如实为 false |
| `SANDBOX_MAX_PROCESSES` | `64` | `RLIMIT_NPROC` | |
| `SANDBOX_MAX_FILE_BYTES` | `16 MiB` | `RLIMIT_FSIZE` | 命中判定见 §12.5 第 1 条 |
| `AUDIT_EXPORT_DIR` | `./data/audit-exports` | 导出件落盘根 | 需与 `AUDIT_ARCHIVE_DIR` 分离 |
| `AUDIT_EXPORT_MAX_ROWS` | `100000` | 单任务行上限 | 超限 `AI_AUDIT_EXPORT_TOO_LARGE`，**不产截断件** |
| `AUDIT_EXPORT_CHUNK_ROWS` | `1000` | 分片行数 | 必须 ≤ `MAX_ROWS` |
| `AUDIT_EXPORT_MAX_CONCURRENCY` | `1` | 导出线程池大小 | 多副本下可 >1：执行权由行级租约仲裁，见 §5.9 |
| `AUDIT_EXPORT_LEASE_TTL_SECONDS` | `60` | 任务租约 TTL（≥5） | 到期即视为持有者失联，可被其他副本回收 |
| `AUDIT_EXPORT_LEASE_RENEW_INTERVAL_SECONDS` | `20` | 续租间隔 | 必须落在 `(0, TTL)`，否则一次抖动就被判失联 |
| `AUDIT_EXPORT_INSTANCE_ID` | `""` | 实例标识 | 留空自动 `host:pid:rand`；多副本务必保持唯一 |
| `AUDIT_EXPORT_RETRY_LIMIT` | `3` | 单任务重试次数 | 失败重试前会回收已写分片 |
| `AUDIT_EXPORT_FORMATS` | `["jsonl"]` | 允许的导出格式 | 需含 `csv` 才能导出 CSV |
| `AUDIT_EXPORT_EXCLUDE_OWN_ACTIONS` | `true` | 排除 `AUDIT_*` 自身事件 | 关闭时导出 detail 带 `recursive=true` |
| `AUDIT_EXPORT_REDACTION_PROFILE` | `standard` | 脱敏档位 | `strict` 时主体 id 走 HMAC |
| `AUDIT_EXPORT_REDACTION_KEY` | 空 | HMAC 密钥 | `strict` 档必填，否则启动校验失败 |
| `AUDIT_EXPORT_HOT_RETENTION_DAYS` | `90` | 热区保留天数 | 清理只删对象与分片，任务行保留 |
| `AUDIT_ARCHIVE_DIR` / `AUDIT_ARCHIVE_PREFIX` | `./data/audit-archive` / `audit-archive` | 归档落盘 | |
| `AUDIT_ARCHIVE_ENCRYPTION_ENABLED` | `false` | AES-256-GCM 归档 | 开启必须给 `AUDIT_ARCHIVE_SECRET_REF` |
| `AUDIT_ARCHIVE_SECRET_REF` | 空 | 当前密钥密文引用 | 明文密钥不得进配置；新写入恒用这把 |
| `AUDIT_ARCHIVE_PREVIOUS_SECRET_REF` | 空 | 上一把密钥密文引用（**只读**） | 轮换过渡期用；须与当前引用不同，且仅在加密开启时允许 |
| `AUDIT_ARCHIVE_SCHEDULE_HOURS` | `24` | 归档窗口 | 必须 > 0 |
| `A2A_ENABLED` | `false` | 入站/出站总开关 | 关闭时路由 503，不暴露内部实现 |
| `A2A_ALLOWED_PEERS` | 空 | 出站白名单 | 空 = 全部拒绝；`REMOTE_PEERS` 非空而此为空 → 装配失败 |
| `A2A_REMOTE_PEERS` | `[]` | peer JSON 数组 | `base_url` 必须 https（回环可 http） |
| `A2A_MAX_DEPTH` / `A2A_MAX_FANOUT` | `2` / `5` | 委派深度 / 扇出 | 越界直接拒绝并留痕 |
| `A2A_IDENTITY_PROPAGATION` | `user_id_header` | 出站身份传播方式 | `none` / `user_id_header` / `full_context_token` |
| `A2A_MAX_RESPONSE_BYTES` | `1 MB` | 入站响应上限 | 超限按失败计，参与熔断 |
| `A2A_CIRCUIT_FAILURE_THRESHOLD` / `A2A_CIRCUIT_COOLDOWN_SECONDS` | `5` / `30` | 熔断阈值与冷却 | |
| `A2A_FEDERATION_ENABLED` | `false` | 联邦入站总开关（v1.4） | 开启后 `inbound_federation` peer 可用共享密钥入站；开启却零主体 → 装配失败 |
| `SANDBOX_ISOLATION_MODE` | `process` | 沙箱隔离形态（v1.4） | `process`（默认）/ `container`；Windows 选 container → 装配失败 |
| `SANDBOX_CONTAINER_IMAGE` | `python:3.11-slim` | 容器模式镜像 | 需预拉取；运行期不自动 pull |
| `SANDBOX_CONTAINER_MEMORY` / `SANDBOX_CONTAINER_CPUS` / `SANDBOX_CONTAINER_PIDS_LIMIT` | `512m` / `1.0` / `128` | 容器资源上限 | docker --memory/--cpus/--pids-limit |
| `SANDBOX_DOCKER_BINARY` | `docker` | 容器 CLI 二进制 | 兼容 podman 等同参数面实现；装配期探测不可用即失败 |

### 12.5 已知限制（本批如实登记）

1. **FSIZE 命中依赖 errno 兜底**：Linux 上 CPython 启动即把 `SIGXFSZ` 置为 `SIG_IGN`，子进程超 `RLIMIT_FSIZE` 不会"被信号杀死"，而是 `write` 返回 `EFBIG` 后以非 0 退出。因此判定为「信号退出码 **或** rlimit 已开启且 stderr 出现内核 errno 文案（`[Errno 27]` / `[Errno 11]`）」。脚本若自行捕获 `OSError` 并正常退出，父进程侧无法判定配额命中。
2. **Windows 沙箱 fail-closed**：无 rlimit / 无进程组，脚本执行直接拒绝（返回能力位 false），不做"降级放行"。
3. **导出任务已支持多副本（v1.3.6）**：执行权由 `zr_audit_export_job` 的行级租约仲裁（§5.9），重启恢复只回收「无主 / 本实例 / 租约已过期」的行，不再误杀其他副本的在途任务。残留限制：租约依赖各副本时钟同源（NTP 漂移超过 TTL 会误判失联）；`PENDING` 任务不做跨副本排队，只在提交副本执行。
4. **M4 已落地（v1.4）**：容器沙箱（§4.10）与出站 mTLS / 联邦入站方案 B（§6.5）已实现并真联调；仍未落地：`object.store.list`（契约 §7 后再动）。
5. **迁移链漂移已收敛，但只在 `TIMESTAMP`/UUID 两族上做过归一**：干净库（SQLite / PostgreSQL）`upgrade head` 后 `compare_metadata` 均为 0 条差异，`alembic check` 可作为门禁。比较策略在 `app/db/alembic_support.compare_type`：UUID 族（`GUID` / `Uuid` / `CHAR(36)` / `VARCHAR(36)` / PG 原生 `uuid`）视为等价，时间戳族仍严格比较 tz 语义。若后续新增列再出现「迁移写 `sa.DateTime()`、模型写 `DateTime(timezone=True)`」这类错配，门禁会立刻报出来。
6. **`--sql` 离线模式的边界**：`alembic upgrade head --sql` 现在能生成可执行脚本（含历史迁移里两处反射式增量修正的静态退化分支），且已在空 PG 库上实测「执行脚本 → `alembic current` = head → 零漂移」。但离线脚本前提是「从空库一路升到 head」；拿它去补一个半旧的库不适用，增量场景请直连数据库 `upgrade head`。
7. **`process.spawn` 不构成安全边界**：该权限码只在 `sandbox_driver` manifest 里**登记**，内核没有 spawn 拦截点（插件用 `asyncio.create_subprocess_exec` 自建子进程），因此它既不限制子进程数量，也不阻止插件以任意参数起进程。真正的能力边界由 `sandbox_driver/process.py` 自身实现保证（cwd 白名单 / `-I` / 最小 env / rlimit / `killpg` / 输出上限 / fail-closed）；**验收与对外文案不得以「已拦截」表述**，只能说「已登记 + 插件内自证实现」。真正的运行时强制点要等企业 sidecar / M0-4。
8. **A2A 无「技能名 → 版本」解析**：`params.skill` 只是名字，插件 DB 边界不允许按名查库，因此跨实例调用必须显式传 `skill_version_id`（§6.6 出站载荷语义）；只传名字的 `route=remote` 由对端自行路由，两个本实例之间不存在这条路径。
9. **入站真 mTLS 不在应用层（v1.4 如实登记）**：uvicorn 不向 ASGI 应用暴露对端证书，应用内无法校验客户端证书——入站 mTLS 由部署层（反向代理/Caddy/nginx）终止后转发，应用层联邦身份只有方案 B（共享密钥 service account）。出站方向已支持 per-peer mTLS（§6.6）。联邦主体的 `user_id` 恒为 `None`（不是本地用户，合成 UUID 会撞审计 actor 外键），归属走审计 `detail`；其技能级 `required_permissions` 一律 default-deny， federation 主体默认无本地授权。容器沙箱 live 冒烟（`ZTORQUE_SANDBOX_CONTAINER_LIVE=1`）只在「有 docker 守护进程的 POSIX 主机」可跑；CI 与 zt-test 容器内无 docker，不默认执行。

### 12.6 维护说明

- 三插件任一 manifest / Port 映射 / 红线行为变更时，同步更新本文档并在修订记录追加。
- 标注「M0-x」的条目落地后回填状态，避免再次「文档能力 > 代码能力」。
- 与《核心插件详细开发文档》的 Module/插件清单保持一致；核心文档升级时复核 §1.5。
- §3.5 动作表、§8 错误码表、§2.4 权限矩阵三张表为**唯一口径**，代码改动必须回改此处。

### 12.7 标准插件可移植性（2026-10-26 追加，代码已落地）

目标：三个插件（`sandbox_driver` / `audit_export_archive` / `a2a_gateway`）可整目录拷到企业版，只补一个注册项即可装配。

| 项 | 落地内容 | 机器验收 |
|---|---|---|
| 公开契约包 | `app/plugin_sdk/`（`PluginManifest`/`PluginSdk`/`AuditQuery`/`AuditRecord`/`SecurityContext`/`SandboxDriver`/`emit_event`/`Base`）；插件只许依赖它 + `app.core.*` | `test_plugin_redlines.py::test_plugins_import_only_public_sdk_surface`（白名单机器校验，越界清单只减不增，现为 0） |
| 实现下沉 | 沙箱执行实现从 `app/kernel/md_skill_runtime.py` 下沉到 `sandbox_driver/process.py`；内核不再自带默认后端，未注入 `sandbox.driver` 时拒绝执行 | `test_sandbox_hardening.py` + 内核文件特征符号断言（`setrlimit`/`killpg` 等不得回流） |
| 表归属 | **方案 b**：模型与迁移文件跟随插件目录，仍由内核单一 Alembic 链执行（见 §5.6 修订块） | `alembic heads` 单 head；空库 `alembic check` 零漂移 |
| 发现机制 | `builtin_plugins/discovery.py`：`BUILTIN_PLUGINS` 元组**兜底在前**，entry_points（group `ztorque.plugins`）**追加在后**；重名内置优先；坏包默认跳过（`assemble(plugin_discovery_strict=True)` 改为拒绝启动） | `test_plugin_discovery.py`（11 条） |
| 配置契约 | `config_schema` 落地为 JSON Schema（三插件已升级）；装配期按 Settings **白名单投影**后校验类型/枚举/必填；历史描述性 schema 保持不校验；边界语义仍归插件 `config.validate()` | `test_plugin_config_schema.py`（15 条，含"装配期在 contribute 之前失败"） |
| 版本契约 | `api_version` 必须在 `SUPPORTED_API_VERSIONS` 内；`extra_plugins` 与 Profile 重名/自身重名一律拒绝 | `test_plugin_manifest_contract.py`（4 条） |
| 导入隔离 | `app/builtin_plugins/__init__.py` 改 PEP 562 惰性导出；单插件 import 不得拉起兄弟插件 / `app.services` / `app.api` / `app.ws` / 内核实现模块 | `tests/integration/test_plugin_minimal_assembly.py`（subprocess 探针 + 双 profile 装配） |

**已知遗留（2026-10-26 已收口）**：~~内核 Port 协议模块（`app/kernel/ports/{identity_provider,memory_short,approval_provider}.py`）直接 `from app.models import ...`，import 任何 Port 协议都会连带拉起 `app.models`~~ → 三个协议模块的模型类型改入 `TYPE_CHECKING` 守卫（`from __future__ import annotations`，运行时 duck typing），import Port 协议不再拉起 `app.models`；由子进程探针 `tests/integration/test_plugin_minimal_assembly.py::test_port_protocols_do_not_pull_app_models` 机器断言。冻结契约文件（`ports/{__init__,model_selector,specs}.py`）未改动。

### 12.8 归档对象对账（2026-10-26 追加，脚本已落地）

`object.store` 契约没有 `list`（§12.5 第 4 条），所以存储侧对象**不可枚举**：对账只能拿插件自有索引表反查，不能正扫。脚本 `scripts/reconcile_audit_archive_objects.py`，默认 dry-run。

**索引源（唯一事实源）**：导出 = `zr_audit_export_job.object_prefix` + `zr_audit_export_shard.object_key`；归档 = `zr_audit_archive_batch.object_prefix` + `objects` JSON 清单。manifest 键不在分片表里，由任务行自己记录的 `object_prefix` 按写入拼法推导。

| 类别 | 判定 | `--apply` 行为 |
|---|---|---|
| `orphan` 孤儿对象（有键无任务行） | 索引写下过该键，但没有任何**有效任务行**拥有它：① 分片行的 `job_id` 在任务表查不到（外键缺位的历史脏数据）；② 任务行已 FAILED 终态——`_mark_failed` 会回收，但 `recover_interrupted` 只改状态不回收，残留对象对任何 API 都不可达（`open_shard` 要求 SUCCEEDED）。只报"对象确实还在存储里"的键 | **删**，逐条打印 |
| `dangling` 悬空引用 | 任务行仍在，应产出的对象查不到。**只对 SUCCEEDED 判定**：PENDING/RUNNING 正在写、FAILED 本就不该有、热区已清理（`expires_at IS NULL`）是"按设计删了对象" | 不删（索引指向不存在的东西，删无可删，交给人工） |
| `invalid` 非法键 | 不满足 `{root}/{tenant}/{owner}/{leaf}` 规范，或租户 / 任务 / 叶子（分片序号、`jsonl|csv` 格式、`.enc` 加密后缀）与所属行记录不一致 | 不删（归属都判定不了，删就是猜） |

**安全边界**

1. 默认 dry-run **零写入**：唯一 I/O 是 `SELECT` 与 `local_path(...).exists()`；不 delete、不写库、不写审计。脚本刻意不 import `app.db.session`（它在 import 期会对 SQLite 开发库补列 ALTER），改为自建 engine。
2. 锁形校验先于探测：非法键在 `local_path()` 之前就被拦下，避免拿越界键去碰存储根目录。
3. `--apply` 删除前复核"该键是否被有效任务行引用"（同名键多引用时改为 `protected_skipped`，不删）；单条删除失败只记录并继续，退出码 2。
4. 不改索引行：`--apply` 只删对象，残留分片行留给人工裁决（删除行会改变审计可追溯范围，不属对账职责）。
5. 不新增迁移、不动内核 Port 契约（`app/kernel/ports/*` 冻结）。
6. 悬空检测依赖 `local_path()` 返回**真实本地路径**（社区版本地文件系统实现即如此）。企业版若把 `local_path` 实现成"下载到临时文件"，存在性判定恒真，悬空引用在该后端上检测不出来——如实登记，不用扫存储绕过去。同理，失败批次已落盘但未登记的 `part-*` 无法枚举，只能靠前缀级清理或存储侧生命周期策略。

**用法与退出码**：`--tenant-id` 限定租户、`--sample N` 控制每类样例数、`--json` 输出机器可读报告、`--object-root` / `--archive-prefix` / `--database-url` 覆盖配置。退出码 `0` = 三类均空；`1` = 仍有待处理项（dry-run 有孤儿也算）；`2` = 删除失败或存储/数据库探测异常。

**机器验收**：`tests/unit/test_reconcile_audit_archive_objects.py`（14 条）——三类判定边界、非法键不探测、进行中任务不判定、dry-run 零写入（对象未删 + 行数/审计行不变 + `delete` 调用为空）、`--apply` 只删孤儿（有效对象与非法键对象均在）。

### 12.9 归档冷副本密钥轮换（2026-10-26 追加，代码与脚本已落地）

**为什么不能"换掉 secret 就完事"**：归档密文是自描述的（`ZAE1` magic + 12 字节 nonce + AES-256-GCM），但**密文里刻意不内嵌密钥标识**——密文本身不该携带任何能缩小密钥搜索空间的线索。于是「这坨是谁加密的」只能看索引/manifest 的 `key_id`，「能不能解开」只能真解。直接替换 `AUDIT_ARCHIVE_SECRET_REF` 会让全部存量对象**永久不可读**且不报任何错（只在下次解包时才炸）。因此引入密钥环：**写恒用当前密钥，读按当前 → 上一把降级**，轮换拆成三步，任何时刻都不存在"中间态读不出"。

**密钥环**（`app/builtin_plugins/audit_export_archive/keys.py`）

| 项 | 语义 |
|---|---|
| `ArchiveKeyRing` | `current`（写 + 首解候选）与 `previous`（只读兜底）。两者 `key_id` 相同则自动**塌缩**为一把（不同引用解出同一密钥不算轮换） |
| `key_id` | `sha256(派生密钥).hexdigest()[:16]`，派生密钥 = `sha256(密钥材料)`。**不可反推密钥**，只用于索引/manifest 标记归属与脚本对账 |
| `build_key_ring` | 未启用加密 → `None`（走明文路径）；缺当前引用 → `AI_AUDIT_ARCHIVE_FAILED`；无 `secret.provider` → `AI_CAPABILITY_UNAVAILABLE`（503）；previous 与 current **同引用** → `AI_AUDIT_ARCHIVE_FAILED`；引用解不开 / 解出空串 → `AI_AUDIT_ARCHIVE_FAILED`。全部发生在**创建批次之前**，不留永久 FAILED 空批次 |
| 解包 `ring.decrypt` | 当前 → 上一把依次试；全失败抛 `AI_AUDIT_ARCHIVE_KEY_MISSING`（502，消息里只带 key_id 列表）。**绝不**返回脏数据、**绝不**"跳过这条继续" |
| 闸门 `ring.decrypt_with_current` | 只用当前密钥，专供轮换脚本判定"对象是否仍依赖旧密钥" |

**配置项**：`AUDIT_ARCHIVE_SECRET_REF`（写密钥，`secret://<ciphertext>` 引用）+ `AUDIT_ARCHIVE_PREVIOUS_SECRET_REF`（只读密钥，空 = 无上一把）。两条硬约束在 `Settings` 与插件 `AuditExportConfig.validate()` **双入口**同时校验：仅加密开启时允许配置上一把；上一把不得等于当前。注意这是 dataclass 校验，**只在 `validate()` 里生效**，改配置不改代码不会自动报错。

**索引侧归属**（不新增迁移）：复用既有 JSON 列，`zr_audit_archive_batch.objects[]` 每项增 `key_id`，成功批次的 `manifest.json(.enc)` 载荷增 `key_id`（明文件字段集不变，只加一个键）；审计 `detail` 同样只记指纹。密钥材料本身不进索引、不进 manifest、不进日志、不进审计。旧批次没有 `key_id`，脚本按 `legacy(no-key-id)` 归类——**这不是缺陷**，只是"当时没记"，真解才是唯一判据。

**三档流程**（`scripts/rotate_audit_archive_key.py`，脚本不改 `.env`、不改数据库，只打印待粘贴的 env）

| 档 | 命令 | 行为 | 退出码 |
|---|---|---|---|
| 1 登记 | `--new-secret-file <path>` / `--new-secret-stdin` | 交 `secret.provider` 加密新密钥材料 + **回读自检**，打印 `secret://` 引用与 `key_id`，以及要落地的两行 env（新引用 + 旧引用）。明文走文件或 stdin（不进 shell 历史/进程列表） | `0` 成功 / `2` 注册失败 |
| 2 切换后核对 | 默认（无 `--retire-old`） | 只读索引：对象按 `key_id` 的分布 + `index_retire_safe`（索引视角是否全部由当前密钥写入）。零写入、不碰对象存储 | `0` |
| 3 退役旧密钥 | `--retire-old` | 读对象存储 + **真解密**：每个加密对象都必须只用当前密钥解开 | `0` 安全（打印 `AUDIT_ARCHIVE_PREVIOUS_SECRET_REF=`）/ `3` 被阻塞 / `2` 运行失败 |

`--retire-old` 的判据与分类：

| 情形 | 判定 | 是否阻塞 |
|---|---|---|
| 当前密钥能解 | 通过 | 否 |
| 仅上一把能解 | `decrypts with the previous key only` | **是**（退出码 3，`KEY_MISSING`） |
| 当前 + 上一把都解不开（数据损坏 / 密钥丢失） | `not decryptable with any configured key` | **是** |
| 索引说加密、对象是明文 | `index says encrypted but the object is plaintext` | **是**（轮换救不了它） |
| 索引 `key_id` 滞后（实际当前密钥可解） | 告警并计入 `readable_with_current` | 否（只报告，避免把索引脏数据当密钥依赖） |
| 对象缺失 | 单列 `missing`，提示走 §12.8 对账 | 否（本身已不可解，删无可删） |
| 未启用加密的批次 | 跳过 | 否 |

**失败路径不静默**：旧密钥被 retire 后仍遇到只有它能解的对象 → 解包当场 `AI_AUDIT_ARCHIVE_KEY_MISSING`（502），走 §8 错误码表与控制台文案；脚本侧同一场景以退出码 `3` 阻塞并逐条打印阻塞对象（`--sample N` 控制条数，其余折叠为计数）。

**边界**：不新增数据库迁移（`key_id` 落在既有 JSON 列）；不改 `app/kernel/ports/*`；不做自动重加密（存量对象保持旧密钥加密直到自然过期清理，需要更早退役就先用当前密钥重新归档）。多副本滚动期间新旧实例可能同时在线，只要各自 env 的 `current`/`previous` 覆盖了"在用密钥集合"即可互解——**滚动前先把新密钥登记为所有副本的 previous**，再统一切 current。

**机器验收**：`tests/unit/test_audit_archive_key_rotation.py`（7 条）——写恒用当前密钥 + 旧对象可被上一把解开；retire 后 `KEY_MISSING`（含 502 状态与错误码注册表断言）；`build_key_ring` 同引用拒绝 / 同密钥塌缩 / 坏引用失败；配置双入口校验；端到端归档记录 `key_id` + manifest 解密 + `collect_archive_objects`/`summarize`/`check_retire` 双视角一致；`check_retire` 缺失 / 索引滞后 / 明文三类边界；`register_new_secret` 往返与空值拒绝。

---

**版本**：v1.4（2026-10-26，M4 收口：sidecar 容器隔离（`SANDBOX_ISOLATION_MODE=container`，docker CLI 子进程，同 Port 同契约）+ A2A 联邦身份（方案 B service account 入站 + per-peer 出站 mTLS；入站真 mTLS 登记为部署层职责），见 §4.10 / §6.5 / §12.4 / §12.5）
**上一版**：v1.3.8（2026-10-26，Port 协议与 ORM 解耦：`app/kernel/ports/{identity_provider,memory_short,approval_provider}.py` 模型 import 移入 `TYPE_CHECKING`，import Port 协议不再拉起 `app.models`，单插件「零内核依赖」收口，见 §12.7）
**上一版**：v1.3.7（2026-10-26，A2A 远端真联调收口（§13.5）：统一封装解包 + 出站载荷语义修正（`skill_version_id` → `route=local`），8 条真链路用例）
**上一版**：v1.3.6（2026-10-26，多副本租约（§5.9 + 迁移 `c7a1e4b90d23`）与 ruff 门禁登记）
**上一版**：v1.3.5（2026-10-26，D 组：`observability.sink` 接线登记 `audit_export.job` / `a2a.outbound` 两个事件及 payload 白名单；`process.spawn` 明确「仅登记、无运行时强制点」）
**上一版**：v1.3.4（2026-10-26，标准插件可移植性 + 沙箱能力语义变更登记：公开契约包 `app/plugin_sdk/`、沙箱实现下沉、方案 b 表归属、entry_points 发现、结构化配置契约；未注入 `sandbox.driver` → 503 属升级收紧）
**取代**：v1 / v1.1 / v1.2（旧版可删除）
**关联**：形态确认书 v1、内核契约 v1、核心插件详细开发文档 v1.1
**代码基线**：`app/assembly.py`、`app/plugin_sdk/`、`app/builtin_plugins/{manifest,plugins,modules,discovery}.py`、`app/builtin_plugins/{sandbox_driver,audit_export_archive,a2a_gateway}/`、`app/kernel/{ports,plugin_reads,audit}/`、`app/kernel/md_skill_runtime.py`、`app/core/errors.py`、`app/main.py`、`app/api/routes/{audit,audit_exports,a2a}.py`、`app/cli.py`、`alembic.ini`

---

## 十三、验证期发现的缺陷与修复（修订记录）

### 13.1 v1.3.1：行为层缺陷

补齐专项单测与跨方言验证时共发现 **6 个实现缺陷 + 1 处测试基建缺陷**，均已修复并有用例锁定：

| # | 缺陷 | 影响 | 修复 |
|---|------|------|------|
| 1 | A2A 出站身份头被当作伪造头过滤 | `a2a_identity_propagation` 静默失效，下游拿不到调用者身份 | `peers.invoke` 增独立 `identity_headers` 参数（可信），与过白名单的 `extra_headers` 分离 |
| 2 | 导出失败任务残留已写分片与 manifest | 失败任务仍可下载不完整导出件 | `_mark_failed` 回收 shard 行与 `manifest.json` 并清零计数 |
| 3 | FSIZE 命中被归为普通脚本失败 | 调用方无法区分配额与自身 bug（返回 `AI_SKILL_FAILED` 而非 `AI_SANDBOX_RESOURCE_EXCEEDED`） | 判定改为「信号退出码 或 rlimit 开启且 stderr 含 `[Errno 27]`/`[Errno 11]`」，见 §12.5 |
| 4 | 关停后提交仍先落库再 503 | 任务列表里留下永远 PENDING 的孤儿任务 | `create_job` 先 `_ensure_executor()` 再写行 |
| 5 | `app/db/session.py` 对所有连接发 `PRAGMA` | PostgreSQL 上建连即语法错误，该方言完全不可用 | 连接监听器仅对 `sqlite3.Connection` 生效 |
| 6 | v1.2 迁移用 `sa.Uuid()`、模型用 `GUID()` | **PG 上外键建不出来，迁移链在 v1.2 就断** | 迁移统一为 `GUID(length=36)`；v1.3 新表时间戳补 `DateTime(timezone=True)` 与迁移对齐 |
| 7 | 测试 fixture 硬编码 `PRAGMA foreign_keys` | PG 上整片 ERROR，方言验证失去信号 | 新增 `tests/support/ddl.py`（SQLite→PRAGMA，PG→`session_replication_role`） |

> 结论：M0–M3 的"已实现"在**跨方言 + Linux 真实执行**下需要用例兜底；本次修订后三方言全绿，v1.3 交付口径由"代码存在"提升为"行为可验证"。

### 13.2 v1.3.2：迁移层缺陷

触发点是「`alembic check` 能不能当门禁」。原结论「86 处历史漂移」是 SQLite 侧的 UUID 渲染噪音；真正的坑在**全新 PostgreSQL 库**上才暴露——也就是说，此前所有「PG 零漂移」的结论都建立在被 `create_all` 修正过的库上，干净环境装出来的 schema 与 ORM 并不一致。

| # | 缺陷 | 影响 | 修复 |
|---|------|------|------|
| 8 | 12 张表的 27 个时间戳列在迁移里是 `TIMESTAMP`（naive），模型是 `DateTime(timezone=True)` | 干净库用 alembic 建出来是 naive 列；写入 aware UTC 时 PG 静默丢弃偏移，时间语义已被破坏但看不出来 | 新增修订 `b3d6f9a2c5e8`：`ALTER COLUMN ... TYPE timestamptz USING col AT TIME ZONE 'UTC'`（对已是 timestamptz 的存量库幂等） |
| 9 | `zr_agent_version.published_at`、`zr_conversation.last_message_at/archived_at` 迁移是 timestamptz，模型没写 `timezone=True` | PG 读出 aware、代码按 naive 处理，跨类型比较抛 `TypeError` | 模型补 `DateTime(timezone=True)`，并在同一修订里把存量库这三列也收敛到 timestamptz |
| 10 | `zr_task_checkpoint` 多一个 `..._task_id_key` 唯一约束 | 模型走「唯一索引」路径，DB 多出约束，`check` 永远不干净；下行迁移重建表时还可能撞名 | 修订里 `DROP CONSTRAINT IF EXISTS`；downgrade 不回建（该对象本就是缺陷） |
| 11 | `alembic check` 在 SQLite 报几十条 `VARCHAR(36) → GUID(36)` | 门禁无法启用，新旧漂移混在一起看不清 | `app/db/alembic_support.compare_type`：UUID 族内视为等价，时间戳仍严格比较 |
| 12 | `alembic.ini` 的 `sqlalchemy.url` 抢在 `DATABASE_URL` 之前；`env.py` 又无条件覆盖 | 「以为连测试库、其实连默认库」，是误清数据的经典来源 | ini 留空 + `env.py` 三级解析（注入 > `DATABASE_URL` > ini）；ini 保持纯 ASCII（`ConfigParser` 用系统 locale 读，中文注释会在 GBK 环境直接炸） |
| 13 | `alembic upgrade head --sql` 全链不可用（两处迁移依赖反射） | 交不出可审计的 DDL 脚本 | 离线分支静态退化（列视为不存在、旧约束视为存在、孤儿校验跳过），PG 空库实测「执行脚本 → head → 零漂移」 |
| 14 | `fileConfig()` 默认 `disable_existing_loggers=True` | 在测试进程内跑迁移会把 `app.*` logger 全禁掉，表现为后续 `caplog` 断言莫名失败 | 显式 `disable_existing_loggers=False` |

**防回归**：`tests/integration/test_alembic_migrations.py` 三项——SQLite 零漂移、`upgrade→downgrade→upgrade` 往返、PG 干净库零漂移（由 `ZTORQUE_MIGRATION_CHECK_URL` 开启）；`scripts/check_migration_drift.py` 输出逐条差异，替代只有一行结论的 `alembic check`。

### 13.3 v1.3.5：观测接线与 `process.spawn` 语义登记

**触发点**：`audit_export_archive` / `a2a_gateway` 的 `requires` 里有 `observability.sink`，但代码从未调用——声明与行为不一致。裁决为**接线**而非删声明：删掉会缩小能力面，且与 capabilities 描述不符。

| 插件 | 事件名 | 收口点 | payload 白名单 | 取值域 |
|---|---|---|---|---|
| `audit_export_archive` | `audit_export.job` | `jobs.py`：`create_job`（queued）/ `_execute`（running、succeeded）/ `_mark_failed`、`recover_interrupted`（failed） | `job_id`、`format`、`status`、`rows_written`、`chunk_count`、`duration_ms`（失败额外 `error_code`） | `status` ∈ queued / running / succeeded / failed |
| `a2a_gateway` | `a2a.outbound` | `peers.py`：`PeerRegistry.invoke`（`_send` 只管 HTTP） | `peer`、`direction`、`result`、`duration_ms`、`response_bytes`、`depth` | `result` ∈ ok / error / timeout / circuit_open |

- 一律经 `app.plugin_sdk.observability.emit_event`（惰性解析内核实现），**禁止**直接 `import app.kernel.registry.active`；上报失败只 `logger.warning`，绝不影响导出 / 出站主流程（有用例锁死）。
- payload 明确**禁放**：租户 id 明文、文件名、审计正文、脚本内容、token、header 原文、请求/响应 body；`a2a.outbound` 失败路径 `response_bytes=0`（不做响应体推断），熔断快速失败单独记为 `circuit_open`。
- `process.spawn` 语义登记：见 §2.1 表格、§3.3 实现约定、§12.5 第 7 条——**仅登记、无运行时强制点**，验收与对外文案不得表述为「已拦截」。

### 13.4 v1.3.6：多副本租约 + ruff 门禁

**租约**（P0 关闭）：`zr_audit_export_job` 加 `lease_owner` / `lease_expires_at` / `attempts` 三列（迁移 `c7a1e4b90d23`，接在 `b3d6f9a2c5e8` 之后，纯加法）。抢占、续租、丢租中止、终态释放、恢复门禁的完整规则见 §5.9；§12.5 第 3 条由「只允许单副本」改为「已支持多副本 + 两条残留限制（时钟同源、PENDING 不跨副本排队）」。

**为什么不是删声明而是加列**：多副本误杀的根因是「恢复逻辑无法区分自己和他人的在途任务」，而 DB 里没有任何字段能表达执行权归属。租约是这个判断的最小充分信息，且不触碰内核不变量与 21 Port（`zr_audit_export_job` 是插件自有表）。

**ruff 门禁**：`[tool.ruff.lint]` 显式 `select = F,E4,E7,E9,I,B,ASYNC`，`ignore` 八条逐条注明原因（`E402/E701/E702/B008/B904/B905/B017/ASYNC109`），版本由 `[project.optional-dependencies].lint` 钉死；CI `python-static` job 从「只跑 compileall」升级为「`ruff check` + `compileall`」。清障一次性处理 169 项（其中 161 项为 `I001`/`F401` 自动修复，剩余 8 项手工：4 处未使用变量、1 处 `getattr` 常量属性、1 处无用表达式、2 处异步阻塞加行内 `noqa` 并注明理由）。

**内核 Port 契约文件冻结**：`app/kernel/ports/{__init__,model_selector,specs}.py` 属契约层，改内容=改契约。`ruff --fix` 曾顺手对它们做 import 排序/删空行，已逐行还原为 HEAD 内容，并在 `[tool.ruff.lint.per-file-ignores]` 为这三个文件豁免 `I001`（`git diff -- app/kernel/ports` 为空）。

### 13.5 v1.3.7：A2A 远端真联调（M3 收口）

**做法**：`tests/integration/test_a2a_live_peers.py`（默认 skip，`ZTORQUE_A2A_LIVE=1` 打开）不再用 `httpx.MockTransport`，而是起 **uvicorn 子进程**（真实装配 + 真实路由 + 真实 DB）与 **真 TCP/TLS** 的 `ThreadingHTTPServer` 对端，8 条用例覆盖：真实例互调、统一封装解包、4xx/超时/5xx/超限响应映射、自签 https 被拒、熔断开路。

**缺陷 1（出站解析不了统一响应封装）**：对端按本项目 `{code,message,data,trace_id}` 返回成功响应时，出站把外层当 A2A 响应，永远读不到 `status`（真实例之间必然 100% 失败；mock 里手写的扁平响应把它掩盖了）。修法：`peers._send` 收到后做一次严格解包——外层没有 `status` 而 `data` 是含 `status` 的 dict 时下沉一层，只解一层、不误伤扁平响应（`test_a2a_gateway.py` 两条单测锁住）。

**缺陷 2（出站载荷把路由信息当成执行指令）**：`params.peer` 是**发送方命名空间**里的 id，原样转发会让对端拿它去查自己的 peer 表 → 要么 503，要么打错人；两个本实例之间根本调不通。修法：出站载荷描述「对端要做什么」——调用方钉了 `skill_version_id` 就改写 `route=local`，对端经 `skill.executor` 直接执行（`peer` 降级为观测字段）；只给 `skill` 名时保持 `route=remote` 原样转发。语义写进 §6.6，残留限制写进 §12.5 第 8 条。

**熔断用例单独起实例**：熔断是**进程内状态**，与主实例共用会让失败计数互相污染、再靠用例顺序"消灾"就是假通过；因此该用例用独立实例 + `PLUGIN_BREAKER_FAILURE_THRESHOLD=2` 断言「连续两次 5xx → 第三次 `AI_PLUGIN_CIRCUIT_OPEN`」。顺带确认：A2A 自己的 `circuit_failure_threshold` 目前只做装配期校验，实际阈值来自内核 `plugin_breaker_*`。





