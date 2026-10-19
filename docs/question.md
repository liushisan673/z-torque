根据《Z-Torque 内核契约 v1》与《阶段00 项目基线与工程规范》的要求，对照当前插件化与接口封装现状，需要优化的地方如下。

---

## 一、与内核契约的核心差距（优先）

### 1. Port 体系几乎未落地（契约 §3 / 阶段00 §3.5）

| 要求 | 现状 | 差距 |
|------|------|------|
| 锁定 **21 个 Port**，分入口/执行/信息/横切四族 | `app/kernel/ports/` 仅有 `queue_driver` | 其余 20 个未定义接口与实现绑定 |
| 必需 Port 缺失 → **fail fast**；可选缺失 → `UNAVAILABLE` + 审计 | 无统一 Port 注册/解析层 | TaskRuntime 等直接依赖具体类 |
| 解析优先级：显式配置 > 插件 priority > 内置默认 > UNAVAILABLE | 无 | 无歧义处理与启动校验 |

**需优化：**
- 建立完整 Port 契约目录（Protocol / ABC），至少覆盖社区版对照表中的 **required** 项。
- 引入 Port 注册表与启动时解析逻辑，禁止内核直接 `import` 具体实现（SessionLocal、具体模型、具体执行器等）。
- 可选 Port 缺失时返回明确的 `AI_CAPABILITY_UNAVAILABLE`，不得静默或伪装可用。

### 2. 装配三层（Profile → Module → Plugin）缺失（契约 §4）

契约要求：

```text
Profile（装配预设） → Module（声明需要的 Port） → Plugin（提供 Port 实现）
```

**现状：**
- `app/builtin_plugins/` 为空壳。
- 执行器在 `main.py` lifespan 中 **硬编码** `gateway.register(...)`。
- 无 Profile / Module 声明机制，无“装哪些模块”的装配预设。

**需优化：**
- 实现内置插件的声明式装配（至少支持 community/standalone 预设）。
- 将 MOCK / HTTP / DB_QUERY / DB_SQL / MCP / MD_SCRIPT 等执行器迁入 `builtin_plugins`，通过 Port 提供能力。
- Kernel **不得** import `builtin_plugins`（阶段00 §3.8 约束 1）。

### 3. 插件契约五条纪律未实现（契约 §5）

| 纪律 | 现状 |
|------|------|
| manifest 唯一入口（`apiVersion: zsoftym.ai/plugin.v1`，声明 provides / requires / permissions / config_schema） | 无 |
| 只实现内核已定义的 Port，不允许插件自定义新 Port | 当前通过 tool_type 字符串扩展，边界模糊 |
| 只经内核 SDK 读写状态，禁止直连内核表 | 部分执行器/运行时直接依赖 DB Session |
| 权限声明式，未声明行为内核拒绝 | 有基础权限检查，但非插件级声明 |
| 插件级熔断与配额强制 | 网关有超时/重试，但非插件级隔离 |

**需优化：** 为内置执行器补齐最小 manifest 与权限声明；后续官方插件按此契约接入。

---

## 二、SkillGateway / 执行器插件层

### 4. 扩展方式仍依赖改代码

- 新增执行器必须改 `main.py` 注册。
- 无 entry_points、无目录自动发现、无基于 manifest 的加载。

**需优化：** 官方内置执行器走插件加载路径；社区版仍可限制“仅加载受控内置”，但装配方式应声明式而非硬编码。

### 5. skill_gateway.py 职责过重（500+ 行）

当前集中了：
- Protocol 定义
- 多个执行器实现
- MCP 连接器
- 网关本身（超时、重试、校验、审计、WAITING_HUMAN）

**需优化：** 按职责拆分，例如：
- `ports/skill_executor.py`（契约）
- `builtin_plugins/` 下各执行器
- MCP 连接器独立模块
- SkillGateway 只做注册、拦截链、分发、结果标准化

### 6. 执行器与 Port 的对应关系未对齐

契约中执行相关 Port 包括：`skill.executor`、`sandbox.driver`、`connector`、`queue.driver` 等。  
当前是按 `tool_type` 注册具体类，尚未映射到上述 Port 族。

**需优化：** 明确 `skill.executor` 为统一执行入口 Port，各类 tool_type 作为其内部策略或子实现，而不是绕过 Port 的平行注册。

---

## 三、ModelGateway 与接口封装

### 7. Mock 仍在生产装配路径

- MockModelGateway 作为 fallback 参与生产装配。

**需优化：** 生产配置仅装配 OpenAICompatible（或真实 provider）；Mock 仅用于测试/开发 profile，且不得在 required Port 解析失败时静默降级。

### 8. 端口层“有名无实”

- `ports/` 仅有 QueueDriver。
- TaskRuntime 等直接依赖具体实现，六边形边界未形成。

**需优化：** 按 21 Port 目录补齐接口；Worker / Outbox / 模型调用 / 执行器调用全部经 Port，便于社区版内置实现与企业版替换（Redis 队列、sidecar 沙箱等）时不改内核。

### 9. QueueDriver 可替换性不足

- 当前为纯内存队列；恢复依赖 Outbox 补偿。
- 文档允许社区版用「DB Outbox + Worker」，但 Port 层面应可替换。

**需优化：** 将队列能力严格收敛到 `queue.driver` Port；内存实现与 DB Outbox 实现可互换，内核不感知具体后端。

---

## 四、依赖与分层约束（阶段00 §3.8）

需对照检查并修正：

1. Kernel 不得导入 `builtin_plugins` / 业务 modules  
2. 所有模型调用必须经 ModelGateway  
3. 所有 Tool 执行必须经 SkillGateway  
4. 所有外部能力必须经明确 Port 契约  
5. 未实现能力必须显式不可用（不可伪装）  
6. API 层只做协议转换，不承担复杂业务  

当前主要风险点：**装配硬编码在 main**、**ports 层过薄**、**执行器与连接器实现与网关混在同一文件**。

---

## 五、建议落地优先级（按契约不可逆程度）

| 优先级 | 项 | 理由 |
|--------|----|------|
| P0 | 建立 21 Port 契约目录 + 注册/解析 + 启动校验 | 内核契约锁定，后续不可逆 |
| P0 | 消除 Kernel 对具体实现的直接依赖（尤其是 DB/模型） | 六边形与依赖约束 |
| P1 | builtin_plugins 落地 + 去掉 main.py 硬编码注册 | 装配三层与扩展方式 |
| P1 | SkillGateway 拆分职责 + 执行器迁入插件包 | 可维护性与 Port 对齐 |
| P1 | Mock 移出生产装配路径 | 能力可用性语义正确 |
| P2 | 内置插件最小 manifest + 权限声明 | 为官方插件与企业版铺路 |
| P2 | QueueDriver / 其他 Port 可替换实现骨架 | 企业版 multi-container/k8s 预留 |

---

**结论：**  
当前 **SkillGateway 注册表 + MD-Skill 目录扫描 + MCP 连接器** 已经能跑通执行链路，但与《内核契约 v1》要求的 **「内核只做六件事，其余一切经 Port 进出」** 以及 **Profile / Module / Plugin 装配三层** 仍有系统性差距。优先补齐 Port 契约与装配机制，再拆分网关职责、去掉硬编码注册，才能符合项目开发文档对插件化与接口封装的要求。