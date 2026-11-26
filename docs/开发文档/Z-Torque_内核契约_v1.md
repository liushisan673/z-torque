# Z-Torque 内核契约 v1（一页纸）

> **冻结基线** ｜ 2026-10-07 ｜ 变更须走 §7 版本流程
> 本契约是**社区版与企业版的共同地基**：企业版重设计能力面 / 数据模型 / 部署，但**不重设计本契约**。
> 术语：**Z-Torque（中文「极矩」）** ＝ 对外正式产品名；`AI Runtime / AI 中台 / Z-Runtime` 均为过程名（对内可用，对外不用）。
> 自我介绍固定话术：**"极矩——极致的极，力矩的矩。"**

---

## 1. 定义（一句话）

> **Z-Torque（极矩）= 一份内核 + 官方插件 + 两档授权 + 三套装配预设。内核只做六件事，其余一切能力都经 Port 进出。**

---

## 2. 六件不变量（不可插件化、不可裁剪、不可关闭）

| # | 不变量 | 内核负责什么 |
|:--|:---|:---|
| 1 | **身份与会话** | 构造 SecurityContext，并保证其贯穿 API → Task → Worker → Skill → 外部调用 |
| 2 | **Task 状态机与事务边界** | 状态迁移合法性、Outbox 一致性、终态不可变 |
| 3 | **Agent 版本快照** | DRAFT / PUBLISHED / ARCHIVED 与不可变语义 |
| 4 | **审计事件流** | 事件模型 + 写入路径 + **不可关闭** |
| 5 | **权限裁决点（PEP）** | 裁决点位置由内核定；**策略来源可换** |
| 6 | **两个出入口** | ModelGateway 与 Skill Gateway 的拦截链、结果标准化、记账 |

---

## 3. Port 目录 v1（21 个，锁定）

| 族 | Port |
|:---|:---|
| **入口（5）** | `identity.provider`、`authz.provider`、`secret.provider`、`notify.channel`、`approval.provider` |
| **执行（7）** | `model.provider`、`model.selector`、`skill.executor`、`sandbox.driver`、`connector`、`queue.driver`、`lock.provider` |
| **信息（6）** | `memory.working`、`memory.short`、`memory.long`、`vector.store`、`knowledge.provider`、`object.store` |
| **横切（3）** | `interceptor`、`observability.sink`、`policy.source` |

**必需性**：`required` 缺失 → 进程拒绝启动（fail fast）；`optional` 缺失 → 能力标记 UNAVAILABLE，调用返回 `AI_CAPABILITY_UNAVAILABLE` + 审计，**不崩、不静默**。

**解析优先级**：显式配置 > 插件 priority > 内置默认 > UNAVAILABLE（priority 相同且未显式指定 → 启动失败报错，不允许随机）。

---

## 4. 装配三层（"新能力怎么接"的标准方法）

```text
Profile（装配预设：装哪些模块）  →  Module（声明需要哪些 Port）  →  Plugin（提供 Port 实现）
   standalone / multi-container / k8s        知识库 / A2A / 审批中心 …      官方插件 / 内置插件
```

- **授权 ≠ 装配**：社区版（免费开源）/ 企业版（付费）是商业概念；`standalone / multi-container / k8s` 是技术概念，两者解耦。
- 任何新能力先归类：**Profile？Module？Plugin？** 再按对应契约接入。未列入 Port 的能力**先做成 Module**。

---

## 5. 插件契约（五条纪律）

1. **manifest 唯一入口**：`apiVersion: zsoftym.ai/plugin.v1`，声明 `provides / requires / permissions / config_schema`。
2. **只实现内核已定义的 Port**；**不允许插件自定义新 Port**（否则内核边界会被插件反向定义）。
3. **只经内核 SDK 读写状态**；**禁止直连内核表**（不暴露数据库连接）。
4. **权限声明式**：未声明的行为（网络 / 文件 / 进程 / 密钥）内核直接拒绝。
5. **插件级熔断与配额强制**：单个插件不得拖垮内核。

---

## 6. 三条红线（API 类型层面不提供绕过路径）

| 红线 | 含义 |
|:---|:---|
| **审计不可关闭** | 不允许插件或配置让审计事件不落账；最多 UI 不展示 |
| **快照不可热改** | 不允许"运行中直接改已发布 Agent 配置" |
| **身份不可绕过** | 不存在"以任意身份执行""跳过权限裁决"的 API |

---

## 7. 变更规则

- 六件不变量与 Port 目录属**不可逆契约**：**新增 Port 需至少两个真实实现 + 定版本号**；移除须走弃用周期（先标记 → 一个发布周期 → 移除 + 兼容适配层）。
- 破坏性变更升 `apiVersion` 大版本，保留一个过渡期双栈加载。
- 本契约的冻结与解释权：**与《AI_Runtime_形态确认书_v1.md》一致；冲突以形态确认书为准。**

---

**版本**：v1（2026-10-07）｜ 关联：形态确认书 v1、内核契约与插件标准 v0.1（本文为其收敛版）、社区版设计文档 v0.1
