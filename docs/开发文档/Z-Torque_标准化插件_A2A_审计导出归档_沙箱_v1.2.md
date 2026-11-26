# Z-Torque 标准化插件详细开发文档
## A2A · 审计导出与归档 · 沙箱（v1.2）

> **文档性质**：官方标准化插件开发规格（遵循插件五条纪律与 Port 契约，并按 2026-10-22 代码现状校准）  
> **适用版本**：企业版为主；沙箱社区版为「已有薄实现 + 本批增强」，企业版可替换 sidecar  
> **上游依据**：  
> - `AI_Runtime_形态确认书_v1.md`（官方 6–8 插件；企业版含沙箱 / A2A 网关）  
> - `Z-Torque_内核契约_v1.md`（六件不变量、21 Port、插件五条纪律、装配三层）  
> - `Z-Torque_核心插件详细开发文档_v1.1.md` / 正文 v1.2（19 个内置插件、9 个 Module、21 Port 全量收口）  
> - 代码基线：`app/assembly.py`、`app/builtin_plugins/`、`app/kernel/ports/`、`app/kernel/audit/service.py`  
> **关键裁决**：  
> - A2A **不是**社区版已有的「单层父子 Task 协作」，而是独立 Module + 标准化官方插件。  
> - 三插件均按标准化插件标准交付：manifest 唯一入口、只实现已定义 Port、只经 PluginSdk、声明式权限、插件级熔断与配额。  
> - **校准原则**：伪代码让位于现有 Port 签名。凡与代码冲突处，一律写「现状签名 + 演进目标」，避免按文档实现无法装配。  
> **修订记录**：  
> - v1（2026-10-22）——初版（YAML manifest、理想化接口、sink 订阅审计）。  
> - v1.1（2026-10-22）——按代码基线重写：Manifest 改 Python dataclass、沙箱改现有 `run(...)`、审计明确「无事件流，需内核只读缝」、A2A 去掉「复用 connector」。  
> - **v1.2（2026-10-23）**——落地前纠偏：否决审计直读方案 A；修正 M1「结构化返回」歧义；写死资源超限错误码范围；A2A 可选 Port 不进 requires；固定审计 action/category 表；新码统一进 `PLUGIN_ERROR_CODES`；明确 Job 单进程范围、A2A 不直接建 Task、capabilities 枚举含义。  

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
| 2 | 只实现内核已定义 Port，不得自定义 | `validate_manifest` 对未知 Port 直接抛错 |
| 3 | 只经 PluginSdk 读写状态，禁止直连内核表 | `PluginSdk`：`settings / registry / engine / register_port / publish / shared / …` |
| 4 | 权限声明式：未声明的行为内核直接拒绝 | **仅 `db.query` 有强制点**；`network/files/process/secrets` 当前无运行时拒绝（见 M0-4） |
| 5 | 插件级熔断与配额强制 | `PluginGuardRegistry`；当前内核只接 `model.provider` 与 `skill.executor` 分派，本批插件入口须**显式**调用 |

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
| B-1 | `sandbox.driver` 签名为 `async run(*, script_path: Path, arguments: dict, cwd: Path, timeout_seconds: float, max_output_bytes: int) -> dict`；返回值 = **脚本 stdout 解析出的 JSON（技能结果本身）**，非执行元信息；超时 → `AI_SKILL_TIMEOUT`(504)；输出超限 / 非零退出 / 非 JSON → `AI_SKILL_FAILED`(500)；**无 `health()`、无 `denied/usage/exit_code` 返回字段** | 不得引入 `SandboxExecuteRequest/Result`；元信息走 sink + 审计 |
| B-2 | `observability.sink` 为同步单条 `emit(event: str, payload: dict \| None)` | 不能作为审计事件流 |
| B-3 | 审计写入是 **DB 直写**（`record_audit` / `record_audit_detached`），无订阅、无事件总线 | 导出插件当前无合法数据源 → **必须先补内核只读缝（M0-2）** |
| B-4 | `GET /audit`：需 `audit.read_all` 才看全租户，`limit ≤ 100`，offset 分页 | 不适合大批量导出 |
| B-5 | `object.store` 仅 `put` / `local_path` / `delete`，**无 `list`** | 归档索引与过期清理由插件自管；**v1 不扩展 Port** |
| B-6 | `secret.provider` 仅 `encrypt/decrypt(text)`，**无 `secret://` 名称解析** | 配置存密钥材料名或密文，插件自行 decrypt；不得假设 `resolve(ref)` |
| B-7 | `queue.driver` 仅 `put/get` Task UUID | 导出/归档 Job **不能**入该队列；用插件自有 Job 表 + 执行器 |
| B-8 | 装配硬校验：缺 `skill.executor` / `model.provider` / connector stdio registry → `AssemblyError` | 企业版 Profile 必须仍包含这三项 |
| B-9 | `PluginManifest` 无 `kind/metadata/priority/license/healthcheck/circuit_breaker/quota`；`permissions` 为扁平元组；`config_schema` **不自动校验** | 模板按现有字段；扩展字段为可选向后兼容目标 |
| B-10 | 沙箱隔离现状：独立解释器 `-I`、最小 env、超时 kill、输出上限；**无 rlimit / 网络策略 / 工作目录写保护**；Windows 无等价隔离 | 验收分平台；`isolation_level` 如实上报 |
| B-11 | `connector` Port = MCP（`initialize / tools/list / tools/call`） | A2A 出站**不得**复用 connector；自建 HTTP 客户端 |

---

## 二、Port 与 Module 映射

### 2.1 三插件映射

| 插件 | 归属 Module | provides | requires | 说明 |
|------|-------------|----------|----------|------|
| `sandbox_driver` | `execution`（已在册） | `sandbox.driver` | 无 | **已有内置插件**，本批为增强，不新建、不改名 |
| `audit_export_archive` | `governance`（追加插件名） | `()` | `object.store`、`secret.provider`、`observability.sink` | **不**实现 `observability.sink`；**不**入 `queue.driver`；能力经受控 API + 内部服务暴露 |
| `a2a_gateway` | 新建 `a2a` | `()` | `skill.executor`、`identity.provider`、`authz.provider`、`secret.provider`、`observability.sink` | **不** require `connector`；可选 `approval`/`notify` 仅 `sdk.shared` 判空，**不进** requires |

### 2.2 命名与归属裁决

| 议题 | 裁决 |
|------|------|
| 命名 | 统一 **snake_case**：`sandbox_driver` / `audit_export_archive` / `a2a_gateway` |
| 沙箱是否新建 | **否**。在现有文件内增强，保持 `manifest.name` 与装配引用不变 |
| 审计是否实现 sink | **否**。避免与内置 `observability_sink` 冲突；改为内核只读缝 + 插件 API |
| 审计数据访问 | **仅方案 C（内核只读缝）**。插件 `db.query` 直读审计表（方案 A）**已否决**，不得采用 |
| A2A 是否新增 Port | **否**（v1）。`provides: ()`，能力经 Module + capabilities + 受控 API 暴露 |

### 2.3 企业版 Profile 装配硬约束

`assemble()` 结束时必须仍满足：

- 有 `skill.executor` 实现  
- 有 `model.provider` 实现  
- 有 connector stdio registry（通常由 `mcp_connector` 提供）  

因此 `PROFILE_ENTERPRISE` **不能**只装企业插件，必须在社区 Module 集合基础上追加 `a2a`，并在 `governance` 的 plugins 中追加 `audit_export_archive`，同时保留上述三项。

---

## 三、通用开发规范（以代码为准）

### 3.1 目录与模块形态

当前**没有** `plugin.yaml` 加载器。插件形态为：Python 类 + `manifest` 类属性 + `contribute(sdk)`。

```text
app/builtin_plugins/
├── sandbox_driver.py              # 已有：本批增强（保持文件名与 manifest.name）
├── audit_export_archive/          # 新建包
│   ├── __init__.py
│   ├── plugin.py                  # PluginManifest + contribute
│   ├── exporter.py
│   ├── archiver.py
│   └── config.py
└── a2a_gateway/                   # 新建包
    ├── __init__.py
    ├── plugin.py
    ├── gateway.py
    ├── protocol.py
    └── config.py
```

硬性约束：

- `app/kernel`、`app/api`、`app/services` **不得** import 插件包；仅 `app/assembly.py` 加载。  
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
        permissions=(),  # 不申请 db.query：读取必须走 M0-2 只读缝
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
| `permissions` | 当前仅 `db.query` 有强制效果；`process.spawn` / `network.outbound` 等为**登记**，供 M0-4 与文档意图使用，验收不以内核拦截为准 |
| `config_schema` | 不自动校验；插件在 `contribute()` 内校验失败即 `AssemblyError` |

**目标态（非本批必须）**：为 dataclass 增加带默认值的 `display_name` / `official` / `healthcheck` / `circuit_breaker` / `quota`，既有 19 插件零改动。

### 3.3 实现约定

- 入口统一 `contribute(sdk: PluginSdk)`；其他 Port 用 `sdk.shared("<port>")` 并显式判空。  
- 拒绝/异常路径审计：使用 `record_audit_detached(...)`（独立事务），避免业务回滚丢审计。  
- 敏感字段：导出/归档前脱敏；禁止明文密钥写入 object.store。  
- 熔断/配额：本批在插件入口显式调用 `PluginGuardRegistry.check/record`（内核尚未统一挂钩除 model/skill 外的路径）。

### 3.4 交付检查清单

- [ ] `validate_manifest` 通过（无未知 Port，`requires` 可解析）  
- [ ] 只经 PluginSdk 访问配置/状态；未声明 `db.query` 时无 `sdk.engine`  
- [ ] 拒绝路径走 `record_audit_detached`；动作已在类别表登记（见 §3.5）  
- [ ] 新错误码已加入 `PLUGIN_ERROR_CODES`（见 §八）  
- [ ] 单测 + 装配测试（缺 Port / 歧义 / 越权负面用例）  
- [ ] `/capabilities` 如实反映，不虚报隔离或 A2A  

### 3.5 审计动作与类别固定表（本批统一）

| action | 建议 category | 说明 |
|--------|---------------|------|
| `SANDBOX_DENIED` | `CATEGORY_KERNEL`（或新建 `CATEGORY_SANDBOX`） | 沙箱策略拒绝 |
| `AUDIT_EXPORTED` | `CATEGORY_AUDIT` | 导出成功 |
| `AUDIT_EXPORT_FAILED` | `CATEGORY_AUDIT` | 导出失败 |
| `AUDIT_ARCHIVED` | `CATEGORY_AUDIT` | 归档批次完成 |
| `A2A_INVOKED` | `CATEGORY_A2A` | 发起调用 |
| `A2A_DENIED` | `CATEGORY_A2A` | 身份/权限/深度拒绝 |
| `A2A_FAILED` | `CATEGORY_A2A` | 对端或路由失败 |

若暂不新增 category 枚举，上述动作**禁止**归入「配置变更」类；须在 `_AUDIT_CATEGORY_BY_ACTION` 显式登记。

### 3.6 权限码落地约定

| 权限码 | 用途 | 种子与默认角色 |
|--------|------|----------------|
| `audit.export` | 创建/查询导出 Job | 写入 Permission 种子；默认仅管理员/企业角色 |
| `audit.archive` | 手动触发归档 | 同上 |
| `a2a.invoke` | 发起 A2A 调用 | 同上 |

- 社区版可存在权限码定义，但**默认角色不授予**。  
- 未装配对应插件时：路由不注册，或返回能力不可用（避免仅 403 难排查）。  
- `policy_source` 只读 DB 角色-权限图；新码未进种子则无法授予。

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

1. **元信息不上返回值**：`duration_ms` / 是否截断意图 / `deny_reason` 经 `observability.sink.emit` + 审计上报；**不得**写入 `run()` 返回的 dict（该 dict 是技能结果，下游直接消费）。  
2. **策略拒绝**：`ApiError("AI_SANDBOX_DENIED", 403)` + `record_audit_detached(..., action="SANDBOX_DENIED", ...)`。  
3. **能力上报**：`/capabilities` 增加 `sandbox: { available, impl, isolation_level }`。  
4. **配置收敛**：默认超时、输出上限、workdir 根目录包含校验（`cwd` 必须位于允许根之下，与 MD Skill 目录策略一致）。

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

| 键 | 说明 |
|----|------|
| `md_skill_script_enabled` | 现有总开关；关闭时调用前返回 `AI_CAPABILITY_UNAVAILABLE` |
| `sandbox_default_timeout_seconds` | 调用方未传超时时的兜底 |
| `sandbox_max_output_bytes` | 输出上限收敛 |
| workdir 根 | `cwd` 必须落在允许根（如 `md_skills_dir`）之下 |
| `mode` / 网络策略 | 仅企业版 sidecar；社区版恒为 `in_process`、无网络策略 |

校验在 `contribute()` 或首次执行路径内完成（`config_schema` 不自动校验）。

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

**失败语义（锁定）**：

| 场景 | 错误码 | 说明 |
|------|--------|------|
| 超时 | `AI_SKILL_TIMEOUT` (504) | **沿用现状**，本批不替换 |
| 输出超限 | `AI_SKILL_FAILED` (500) | **沿用现状**（直接失败，不截断返回） |
| 非零退出 / 非 JSON | `AI_SKILL_FAILED` (500) | 沿用 |
| 策略拒绝（路径越界等） | `AI_SANDBOX_DENIED` (403) | **本批新增** |
| CPU/内存 rlimit 触发 | `AI_SANDBOX_RESOURCE_EXCEEDED` (422) | **仅 Linux/macOS**；Windows 不触发、不宣称 |

### 4.6 实现要求（分平台）

| 平台 | 可达能力 | `isolation_level` | 验收 |
|------|----------|-------------------|------|
| Linux / macOS | `-I`、可选 `resource.setrlimit`、进程组 kill、cwd 包含校验 | 有 rlimit 时为 `rlimit`，否则 `none` | 内存/CPU 超限、cwd 越界、超时 |
| Windows | `-I`、超时 kill、输出上限、cwd 校验；无 rlimit | **必须** `none` | 仅超时 / 输出超限 / cwd 越界 |
| 企业 sidecar | 容器隔离 + 网络策略 | `container` | 同 `run()` 契约 |

### 4.7 测试要求

| 类型 | 用例 |
|------|------|
| 单元 | 超时 kill、输出超限 → `AI_SKILL_FAILED`、非零退出、cwd 越界 → `AI_SANDBOX_DENIED` |
| 单元 | 拒绝写审计（detached）；返回 dict 与脚本 stdout 一致；元信息出现在 sink/审计而非返回值 |
| 集成 | SkillGateway → MD_SCRIPT → 沙箱 |
| 负面 | 脚本总开关关闭 → `AI_CAPABILITY_UNAVAILABLE` |

### 4.8 验收清单

- [ ] `run()` 签名与返回语义未变；现有调用方与相关单测全绿  
- [ ] 元信息仅经 sink + 审计；返回 dict 无包装字段  
- [ ] `SANDBOX_DENIED` 有 detached 审计且类别已登记  
- [ ] capabilities 含 `isolation_level`，Windows 不虚报  

---

## 五、插件二：`audit_export_archive`（审计导出与归档）

### 5.1 定位与阻塞点

| 项 | 内容 |
|----|------|
| Module | `governance`（追加插件，**建议仅企业 Profile 启用**） |
| provides | `()` |
| requires | `object.store`、`secret.provider`、`observability.sink` |
| 数据访问 | **仅**内核只读缝（方案 C）。**方案 A（插件 db.query 直读审计表）已否决** |

**阻塞（必须 M0 先完成）**：

1. **M0-2** 审计只读缝：`iter_audit_events` + `sdk.read_audit`  
2. **M0-3** 索引方案：插件自有索引表（**不**给 `object.store` 增加 `list`）  
3. 权限码 `audit.export` / `audit.archive` 进入种子数据  

**禁止**：关闭审计写入的配置；绕过脱敏导出明文；插件内 `select(AuditLog)`；删除审计行的 API。

### 5.2 功能范围

| 能力 | v1 范围 | 明确不做 |
|------|---------|----------|
| 导出 | 时间/租户/操作者/动作前缀过滤；JSONL/CSV；分片 + 清单（含 sha256） | 任意 SQL、跨租户合并 |
| 归档 | 热窗口后冷副本至 `object.store`；可选加密 | 归档对象原地编辑；**删除审计行** |
| 索引 | 插件自有 Job/索引表，按时间与批次查询 | 替代在线 `/audit` |
| 审计 | 导出/归档动作自身写审计 | — |

### 5.3 Manifest

```python
class AuditExportArchivePlugin:
    manifest = PluginManifest(
        name="audit_export_archive",
        description="经内核只读缝消费审计数据，提供导出与冷归档；不关闭、不绕过写入",
        provides=(),
        requires=("object.store", "secret.provider", "observability.sink"),
        permissions=(),  # 禁止 db.query 直读审计表
        version="0.1.0",
    )
```

### 5.4 配置

| 键 | 默认 | 说明 |
|----|------|------|
| `hot_retention_days` | 90 | 超过窗口进入归档候选 |
| `export_max_rows` | 100000 | 单 Job 上限；超出分片 |
| `export_batch_size` | 1000 | keyset 每批行数 |
| `export_formats` | `["jsonl"]` | `jsonl` / `csv` |
| `archive.prefix` | `audit-archive/` | 对象键前缀 |
| `archive.encryption_enabled` | false | 开启时用 secret.provider 解密得到的材料做 AES-GCM |
| `redaction_profile` | `standard` | `standard` / `strict` |

### 5.5 接口约定

**（1）内核只读缝（M0-2，唯一合法数据源）**

```python
# 目标：app/kernel/audit/reader.py
def iter_audit_events(
    db: Session, *, tenant_id: UUID,
    start: datetime | None, end: datetime | None,
    action_prefix: str | None, actor_user_id: UUID | None,
    after: tuple[datetime, UUID] | None,  # keyset：created_at, id
    limit: int,
) -> Iterator[dict[str, Any]]: ...

# PluginSdk
sdk.read_audit(query) -> Iterator[dict]
```

要求：强制本租户；keyset 分页（禁止深 OFFSET）；字段白名单脱敏；导出循环内不得 `record_audit`（防自我放大）。

**（2）插件内部服务**

```python
class AuditExportService:
    def submit(self, query: AuditExportQuery, ctx: SecurityContext) -> str: ...  # job_id
    def status(self, job_id: str, ctx: SecurityContext) -> ExportJobView: ...
    def archive_due(self, ctx: SecurityContext) -> ArchiveResult: ...
```

**（3）HTTP 面（新权限码，见 §3.6）**

| 方法 | 路径 | 权限 |
|------|------|------|
| POST | `/api/z-torque/audit/exports` | `audit.export` |
| GET | `/api/z-torque/audit/exports/{id}` | `audit.export` |
| POST | `/api/z-torque/audit/archive/run` | `audit.archive` |

未装配本插件时不注册上述路由。

### 5.6 实现要求

1. **Job 执行**：自有表（如 `zr_audit_export_job`，插件迁移）+ **单进程**线程池。禁止伪造 Task 入 `queue.driver`。  
   - **范围**：v1 仅保证单进程部署；多实例需后续外部分发或 Outbox，不在本批。  
2. **分批与幂等**：每批写入 `object.store`；最后写 `manifest.json`（批次列表、行数、每批 sha256、总 sha256）。  
3. **脱敏**：`token/password/secret/api_key/authorization` 等键替换为 `***`；`strict` 可对主体 id 做 HMAC（密钥不出现在导出物中）。  
4. **归档**：只生成冷副本 + 索引；**不提供删除审计行 API**；运维清理走独立审批/离线流程。  
5. **自身审计**：成功/失败均写 `AUDIT_*` 动作（见 §3.5）。  
6. **熔断**：键 `plugin.audit_export_archive`；`submit` 前 `check`，结束后 `record`。  
7. **失败**：单批重试 3 次（指数退避），仍失败则 Job=`FAILED` 并保留已完成分片；不得静默跳行。

### 5.7 测试要求

| 用例 | 断言 |
|------|------|
| 脱敏 | 导出物无明文密钥类字段 |
| 租户隔离 | 不能导他租户事件 |
| 分页 | keyset 无重复、无遗漏（含同 created_at 多行） |
| 完整性 | manifest 中 sha256 与分片一致 |
| 关闭审计 | 配置与 API 均无「关闭审计」能力 |
| 权限 | 无 `audit.export` → 403；跨租户 → 403 |
| 数据路径 | 无插件直连 `AuditLog` 表的代码路径 |

### 5.8 验收前置

- [ ] M0-2、M0-3 落地  
- [ ] 权限码进种子；类别表登记完成  
- [ ] 社区 Profile 默认不装配本插件（或装配但不授默认角色权限）  

---

## 六、插件三：`a2a_gateway`（A2A 标准化网关）

### 6.1 定位

| 项 | 内容 |
|----|------|
| Module | **新建 `a2a`**；`PROFILE_COMMUNITY` **不包含** |
| provides | `()` |
| requires | `skill.executor`、`identity.provider`、`authz.provider`、`secret.provider`、`observability.sink` |
| 可选依赖 | `approval.provider`、`notify.channel`：仅 `sdk.shared` 判空，**不写进 requires** |
| 与父子 Task | 完全独立；默认不桥接 |

### 6.2 功能范围

1. **发现**：本实例已发布 Agent 的可寻址描述（经服务层/只读投影，不直连内核表 unless 显式 `db.query` 且评审通过；优先走现有服务 API）。  
2. **路由**：`local` → 委托 `skill.executor`（SkillGateway）；`remote` → 自建 HTTP（B-11）。  
3. **身份**：出站按 `identity_propagation`；入站见 §6.5（v1 仅方案 A）。  
4. **治理**：全链路审计、深度/扇出硬限、对端级熔断与配额；可选审批。  

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
        permissions=("network.outbound",),  # 登记；强制点见 M0-4
        version="0.1.0",
    )
```

**不要**把 `connector` 写入 requires。

### 6.4 配置

| 键 | 默认 | 说明 |
|----|------|------|
| `enabled` | false | 默认关闭 |
| `local_discovery` | true | 暴露本地 Agent 描述 |
| `remote_peers` | `[]` | `[{id, base_url, auth_secret_name}]`；`auth_secret_name` 指向可 decrypt 材料，非 `secret://` |
| `max_depth` | 2 | 基于 `X-A2A-Depth` |
| `max_fanout` | 5 | 单次派生并发上限 |
| `default_timeout_seconds` | 120 | 出站超时 |
| `identity_propagation` | `user_id_header` | `none` / `user_id_header` / `full_context_token` |
| `allowed_peers` | `[]` | 出站白名单；空 = 全部拒绝 |

### 6.5 入站身份（M0-1，v1 仅方案 A）

| 方案 | 机制 | v1 |
|------|------|-----|
| **A. 本域 JWT（采用）** | 调用方携带本域 JWT → `identity_provider.authenticate` → `authz.has_all(..., ["a2a.invoke"])` | **唯一支持** |
| B. 对端共享密钥 → service account | 需扩展 identity 适配 | 不在 v1 |
| C. mTLS | 证书体系 | 不在 v1 |

- 跨实例前提：**共享 JWT 密钥与用户体系**（同租户/同部署域）；联邦身份不在 v1。  
- **禁止**信任裸 `X-User-Id` 头。

调用链：`入站认证 → 深度检查 → authz →（可选 approval）→ 路由 → 结果标准化 → 审计`。

### 6.6 实现要求

- `enabled=false` → `AI_A2A_DISABLED` (503)，无出站。  
- **不桥接 Task**：不写父子字段；**不**由 A2A 插件直接 `insert Task`。本地调用只经 `skill.executor`；是否创建 Task 由现有 Gateway/执行链决定。  
- 深度：入站读 `X-A2A-Depth`（缺省 0），出站写 `depth+1`；超限 → `AI_A2A_DEPTH_EXCEEDED`。  
- 扇出：`Semaphore(max_fanout)`；超限排队或拒绝，不静默丢。  
- 出站：强制 https（回环可 http）、`allowed_peers` 白名单、响应体上限、禁重定向。  
- 熔断键：`plugin.a2a_gateway` 与 `plugin.a2a_gateway.peer:{id}`。  
- 审计动作见 §3.5。  
- 错误映射：对端 4xx → `AI_A2A_DENIED`；超时 → `AI_A2A_TIMEOUT`；连接失败 → `AI_A2A_PEER_UNAVAILABLE`；路由缺失 → `AI_A2A_ROUTE_FAILED`。

### 6.7 测试要求

| 用例 | 断言 |
|------|------|
| 关闭态 | `AI_A2A_DISABLED`，无出站 |
| 深度 | 超限拒绝并审计 |
| 扇出 | 不超发 |
| 身份 | 伪造 `X-User-Id` 无效 |
| Task 隔离 | 无新增父子关系字段 |
| 熔断 | 连续失败开路 |
| 审计 | `A2A_*` 落库且分类正确 |

### 6.8 立项前置

- [ ] M0-1（入站 = 方案 A）  
- [ ] M0-4（`network.outbound` 最小强制点，至少覆盖 A2A 出站路径）  
- [ ] `a2a` Module + `PROFILE_ENTERPRISE` 仍满足 §2.3  
- [ ] `capabilities.features` 含 `a2a` 当且仅当 Module 已装配  

---

## 七、装配与启用

### 7.1 Module 声明（示意）

```python
ModuleSpec(
    name="a2a",
    description="Agent-to-Agent 发现、路由与会话（企业版）",
    plugins=("a2a_gateway",),
    requires_ports=("skill.executor", "identity.provider", "authz.provider"),
)
```

| Module | 插件（本批相关） |
|--------|------------------|
| `governance` | 现有 + **企业 Profile 追加** `audit_export_archive` |
| `execution` | 含已有 `sandbox_driver`（增强） |
| `a2a` | `a2a_gateway`（新建，仅企业） |

### 7.2 Profile

- `PROFILE_COMMUNITY`：现有 9 Module；**不含** `a2a`；**默认不**装配 `audit_export_archive`。  
- `PROFILE_ENTERPRISE`（新建）：在社区基础上增加 Module `a2a`，governance 追加 `audit_export_archive`，并**显式保留** `skill_executors`、`model_provider`、`mcp_connector`（满足 §2.3）。  
- 覆盖单 Port：走 `settings.port_override_map` / `PORT_OVERRIDES`，无 `assemble(overrides=...)` 形参。  
- Profile 为装配期常量，由入口显式传入，不用环境变量隐式切换。

### 7.3 能力面

`GET /api/z-torque/capabilities` 需与装配结果联动，示例：

```json
{
  "sandbox": {
    "available": true,
    "impl": "in_process",
    "isolation_level": "none"
  },
  "audit_export": {
    "available": false,
    "formats": [],
    "archive": false
  },
  "features": ["..."]
}
```

`isolation_level` 枚举含义：

| 值 | 含义 |
|----|------|
| `none` | 仅 -I / 超时 / 输出上限（含 Windows） |
| `rlimit` | 在 none 基础上具备 CPU/内存等 rlimit（Linux/macOS） |
| `container` | sidecar/容器（企业版） |

未实现级别**禁止**上报。`features` 含 `a2a` 当且仅当 `a2a` Module 已装配。

### 7.4 启用顺序

1. **M1** `sandbox_driver` 增强（无新内核依赖）  
2. **M2** `audit_export_archive`（依赖 M0-2、M0-3）  
3. **M3** `a2a_gateway`（依赖 M0-1、M0-4）  
4. **M4** 企业 sidecar / 远端 peer / 归档加密  

---

## 八、错误码

现状：`ApiError` + 有限码表。本批**新建集合** `PLUGIN_ERROR_CODES`，避免把业务插件码塞进 `MD_SKILL_ERROR_CODES`。

| 码 | HTTP | 归属 | 说明 |
|----|:----:|------|------|
| `AI_SANDBOX_DENIED` | 403 | sandbox | 策略拒绝（本批新增） |
| `AI_SANDBOX_RESOURCE_EXCEEDED` | 422 | sandbox | **仅** rlimit；Windows 不用 |
| `AI_SKILL_TIMEOUT` | 504 | sandbox | **已有**，超时沿用 |
| `AI_SKILL_FAILED` | 500 | sandbox | **已有**，输出超限等沿用 |
| `AI_AUDIT_EXPORT_DENIED` | 403 | audit | 权限或范围非法 |
| `AI_AUDIT_EXPORT_TOO_LARGE` | 413 | audit | 超出单 Job 上限 |
| `AI_AUDIT_ARCHIVE_FAILED` | 502 | audit | 归档写入失败 |
| `AI_A2A_DISABLED` | 503 | a2a | 未启用 |
| `AI_A2A_DENIED` | 403 | a2a | 身份/权限拒绝 |
| `AI_A2A_TIMEOUT` | 504 | a2a | 出站超时 |
| `AI_A2A_PEER_UNAVAILABLE` | 503 | a2a | 对端不可用 |
| `AI_A2A_DEPTH_EXCEEDED` | 400 | a2a | 深度超限 |
| `AI_A2A_ROUTE_FAILED` | 404 | a2a | 路由失败 |

- 新码加入 `PLUGIN_ERROR_CODES` 并纳入统一错误响应/文档校验。  
- 不得用 `AI_CAPABILITY_UNAVAILABLE` 表达业务拒绝（语义不同）。

---

## 九、里程碑

| 阶段 | 内容 | 前置 | 验收 |
|------|------|------|------|
| **M0** | 四项裁决落地（可先文档+内核小改） | — | 见下节 |
| **M1** | `sandbox_driver`：拒绝审计、元信息上报、capabilities、cwd 校验 | — | 签名/返回语义不变；Windows 如实 `none` |
| **M2** | `audit_export_archive` v1 | M0-2、M0-3 | 脱敏/隔离/完整性；无直读表；无法关闭审计 |
| **M3** | `a2a_gateway` v1（本地 + 治理） | M0-1、M0-4 | 不桥接 Task；伪造身份失败；熔断生效 |
| **M4** | sidecar、远端 peer、归档加密 | M1–M3 | 同 Port/Module 契约替换；capabilities 如实 |

### M0 裁决清单（开发前必须关闭）

| ID | 议题 | 决议（v1.2 已定） |
|----|------|-------------------|
| M0-1 | A2A 入站凭证 | **方案 A**：本域 JWT + `a2a.invoke`；禁止信任裸用户头 |
| M0-2 | 审计导出数据源 | **内核只读缝** `iter_audit_events` + `sdk.read_audit`；**否决**插件直读表 |
| M0-3 | object.store 无 list | **插件自有索引表**；v1 **不**扩展 `object.store.list` |
| M0-4 | 网络等权限强制点 | 至少在 A2A 出站路径落实 `network.outbound` / 白名单检查；其余 permissions 仍可先登记 |

---

## 十、变更与兼容

| 变更 | 类型 | 处理 |
|------|------|------|
| `object.store` 增 `list` | Port 破坏性 | **本批不做**；未来走契约 §7 |
| `PluginManifest` 增可选字段 | 向后兼容 | 全部给默认值 |
| `sandbox.driver` / `observability.sink` 增方法 | 破坏性 | **v1 不动** |
| 新建 `a2a` Module / `PROFILE_ENTERPRISE` | 增量 | 不改变社区 Profile 行为 |
| 新权限码 | 增量 | 必须写种子；默认角色策略见 §3.6 |
| `PLUGIN_ERROR_CODES` | 增量 | 本批新码统一进入该集合 |

---

## 十一、相对 v1.1 的纠偏摘要

| # | v1.1 问题 | v1.2 处理 |
|---|-----------|-----------|
| 1 | 审计仍保留「方案 A 直读」口子 | **正式否决**；仅只读缝 |
| 2 | M1 写「结构化返回」易误解为改 `run()` 返回值 | 改为「拒绝审计 + 元信息上报 + capabilities + cwd」 |
| 3 | `AI_SANDBOX_RESOURCE_EXCEEDED` 与输出超限边界不清 | 仅 rlimit；输出超限仍 `AI_SKILL_FAILED` |
| 4 | A2A 可选 Port 可能被写进 requires | requires 仅五项必需；approval/notify 判空 |
| 5 | 权限码缺少种子与默认角色约定 | §3.6 固定 |
| 6 | 审计 action/category 未表固化 | §3.5 固定表 |
| 7 | 错误码可进 MD_SKILL 集合 | 统一 `PLUGIN_ERROR_CODES` |
| 8 | A2A 本地是否直接建 Task 未写死 | **不**由 A2A insert Task；只经 skill.executor |
| 9 | Job 多进程语义未声明 | v1 仅单进程 |
| 10 | capabilities isolation_level 含义模糊 | 三档枚举写死 |
| 11 | 暗示可扩展 object.store.list | 明确本批不做 |

---

## 十二、维护说明

- 三插件任一 manifest / Port 映射 / 红线行为变更时，同步更新本文档并在修订记录追加。  
- 标注「M0-x / 待新增」的条目落地后回填状态，避免再次「文档能力 > 代码能力」。  
- 与《核心插件详细开发文档》的 Module/插件清单保持一致；核心文档升级时复核 §1.5。  

---

**版本**：v1.2（2026-10-23）  
**关联**：形态确认书 v1、内核契约 v1、核心插件详细开发文档 v1.1（正文 v1.2）  
**代码基线**：`app/assembly.py`、`app/builtin_plugins/manifest.py`、`app/kernel/ports/`、`app/kernel/audit/service.py`、`app/models/task.py`、`app/main.py`
