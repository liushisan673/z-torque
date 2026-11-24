# Z-Torque Windows 能力降级与沙箱边界 v1.0

> **适用范围**：单机 `.exe` 交付形态；不依赖容器、WSL 或任何外部服务。
> **上位约束**：21 个内核 Port 冻结；`tenant_id` 保留但为固定值；数据库只用 SQLite。
> **状态**：Phase 2 §四第 3 项正式文档；本文只描述既有实现与不变量，不放宽任何闸门。
> **日期**：2026-11-24

---

## 一、总原则：如实上报，fail-closed

1. **无 OS 隔离就没有不可信执行**。缺少容器时，工作区级（`full`）执行显式返回 `AI_CAPABILITY_UNAVAILABLE`，**绝不**降级为 `process` 模式兜底。路径白名单只是纵深防御，不能替代 OS 隔离。
2. **capabilities 是能力契约，不是广告**。`untrusted_execution` / `isolation_level` / `script.*` 必须由真实装配与平台探测推导，任何"看起来可用"的虚报都视为缺陷。
3. **Windows 可执行脚本，但不是无条件放行**。process 模式在 Windows 上属于"可信开发态"：必须同时满足总开关、技能声明可信、路径白名单、人工确认四道约束。
4. **拒绝必须可观测**。所有闸门拒绝都写 `SANDBOX_DENIED` 审计（detail 只记 metadata，不记脚本正文）。

---

## 二、平台能力矩阵

| 能力 | Linux / macOS（process） | Windows（process） | 容器（docker CLI） |
|------|--------------------------|--------------------|--------------------|
| `python -I` 独立解释器 | 有 | 有 | 有 |
| 最小环境变量 / 输出上限 / 超时 kill | 有 | 有 | 有 |
| `resource.setrlimit`（CPU/内存） | 可选（`sandbox_rlimit_enabled`） | **无**，不宣称 | 由 `--memory` / `--cpus` / `--pids-limit` 替代 |
| 进程组 kill | 有 | **无**（仅 kill 进程树） | docker kill + 进程树确认 |
| cwd 白名单 + Scope 边界 | 有 | 有 | 有（挂载只暴露白名单根） |
| `restricted` scope | 可用 | 可用 | 可用 |
| `full` scope（不可信执行） | **不可用**（无容器） | **不可用**（无容器） | 可用 |
| `isolation_level` | `rlimit` 或 `none` | **必须 `none`** | `container` |
| `untrusted_execution` | `unavailable` | `unavailable` | `container` |

超过资源限制的错误码：`AI_SANDBOX_RESOURCE_EXCEEDED`（429）。Windows 上该分支**不会触发**，不得在文档或前端宣称其可用。

---

## 三、Windows 脚本四道闸门（缺一不可）

脚本执行唯一入口是 `SkillGateway`；`MdSkillRuntime.execute()` 在执行点依次判定：

| # | 闸门 | 失败码 | HTTP | 判定位置 |
|---|------|--------|:----:|----------|
| 1 | 总开关 `md_skill_windows_process_enabled` | `AI_CAPABILITY_UNAVAILABLE` | 503 | `md_skill_runtime.py` |
| 2 | 技能 frontmatter `windows_trusted: true` | `AI_SKILL_WINDOWS_UNTRUSTED` | 403 | `md_skill_runtime.py` |
| 3 | 脚本根落在 `sandbox_allowed_roots` 内 | `AI_SANDBOX_DENIED` | 403 | `md_skill_runtime.py` + 驱动侧复校验 |
| 4 | 每次调用人工确认（有 task 上下文时挂 checkpoint） | 挂起等待审批 | — | `skill_gateway.py` |

补充说明：

- `windows_trusted` 默认 `false`；未声明的技能在 Windows 上一律拒绝，不能靠"路径恰好在白名单里"绕过。
- 审批 checkpoint 内冻结 `sandbox_scope`；恢复时在执行点二次校验，篡改即 `AI_SANDBOX_DENIED`（fail-closed，老 checkpoint 无 scope 一律拒绝）。
- 所有平台统一使用显式 `SandboxScope` 对象贯穿，**禁止**用 `ContextVar` 或环境变量传递（异步 / 线程 / subprocess 均不继承线程内改动）。

---

## 四、`full` scope 与不可信执行

- `SandboxScope(mode="full")` 只允许 `isolation="container"` 的驱动执行；落到 `process` 驱动时显式 `AI_CAPABILITY_UNAVAILABLE`。
- capabilities 用两个字段如实表达：

```json
{
  "sandbox": {
    "available": true,
    "isolation_level": "none",
    "scope": {
      "explicit": true,
      "modes": ["restricted", "full"],
      "full_requires_container": true,
      "gateway_only": true
    },
    "script": {
      "enabled": true,
      "windows_process": true,
      "windows_requires_trusted": true,
      "windows_requires_confirmation": true
    },
    "patch": {"available": true, "artifact_ttl_hours": 24},
    "untrusted_execution": "unavailable"
  }
}
```

| 字段 | 含义 | 不允许的取值 |
|------|------|--------------|
| `isolation_level` | 当前装配的真实隔离级别 | Windows 上不得为 `rlimit` / `container` |
| `scope.full_requires_container` | `full` 是否强制容器 | 恒为 `true` |
| `script.windows_requires_trusted` | 是否要求 `windows_trusted` | 恒为 `true` |
| `script.windows_requires_confirmation` | 是否每次挂人工确认 | 恒为 `true` |
| `untrusted_execution` | 不可信执行是否可用 | 无容器时必须 `unavailable` |

**一致性不变量**：`untrusted_execution == "container"` 当且仅当 `isolation_level == "container"`；其余情况必须为 `"unavailable"`。capabilities 端点有集成用例锁定该不变量。

---

## 五、错误码与审计

| 事件 / 错误 | 错误码 | 类别 | 说明 |
|-------------|--------|------|------|
| 闸门 1 / `full`+process / 无沙箱实现 | `AI_CAPABILITY_UNAVAILABLE` | `sandbox` / `tool_call` | 能力不存在，不是权限不足 |
| 闸门 2 | `AI_SKILL_WINDOWS_UNTRUSTED` | `sandbox` | 技能未声明可信 |
| 闸门 3 / 审批篡改 / policy deny | `AI_SANDBOX_DENIED` | `sandbox` | 写 `SANDBOX_DENIED` 审计，detail 仅 metadata |
| 补丁并发冲突 | `AI_PATCH_CONFLICT` | `sandbox` | 写 `PATCH_CONFLICT` 审计 |
| 补丁回滚失败 | `AI_PATCH_PARTIAL` | `sandbox` | 写 `PATCH_PARTIAL` 告警，不静默吞 |

diff 正文只写独立 artifact 表（TTL 24h）；审计只记 `artifact_id + hash + path + size`，符合 metadata-only 原则。

---

## 六、禁止事项

1. 禁止把 `process` 模式声明为"不可信执行可用"。
2. 禁止在 Windows 上虚报 `rlimit` / `isolation_level=container`。
3. 禁止绕过 `SkillGateway` 直接调用 sandbox driver / `subprocess.spawn`（CI 静态红线）。
4. 禁止用提示词警告代替结构性剔除：不可信内容在构建 prompt 前剔除。
5. 禁止在 capabilities 中暴露绝对路径、出站白名单等敏感细节（仅管理员可见）。

---

## 七、验收清单

- [x] Windows 上 `untrusted_execution="unavailable"`，`full_requires_container=true`
- [x] Windows 脚本必须 `windows_trusted=true` 且每次人工确认
- [x] 越界路径 / 审批参数篡改 → `AI_SANDBOX_DENIED` + detached 审计
- [x] `full` + process → `AI_CAPABILITY_UNAVAILABLE`，不降级
- [x] `AI_SKILL_WINDOWS_UNTRUSTED` 已登记错误码集合与 HTTP 403
- [x] capabilities 一致性有集成用例锁定
- [x] 前端能力条只展示自然语言三态，不裸露术语

**关联文档**：`Z-Torque_平台能力优化路线图_v1.md` §2.3 / §四、`Z-Torque_标准化插件_A2A_审计导出归档_沙箱_v1.3.md` §4.7、`docs/memmemeory.md` Phase 0 第 4 项。
