# Z-Torque 平台能力优化路线图 v2.0（定稿）

> **目标**：对照 nanobot 与成熟运行时实践，补齐 z-torque 在知识检索、记忆、沙箱、上下文治理、任务可靠性、工具/插件治理等机制层短板，使平台从「契约完备、实现偏空」走向「真实可跑、可审计、可扩展」。  
> **原则**：复用算法、状态机与数据流设计，重构成以内核 Port 为基础的能力；不移植代码；以单机安装为交付形态。  
> **日期**：2026-11-24  
> **状态**：定稿基线，供 Phase 0 逐项实施与验收  
> **版本记录**：v1.0 初版 → v1.1 校正假成功描述（部分误判）→ **v2.0 依据 `docs/111.md` 事实复核与最高级别约束重写**

---

## 〇、最高级别约束（所有设计的前提，不可违背）

| # | 约束 | 对设计的硬性影响 |
|---|------|------------------|
| 1 | **先不做多租户，采用单机安装** | 保留现有 `tenant_id` 列（不改历史 schema），但统一填固定租户值；**不设计跨租户查询隔离、不做多租户测试矩阵**；重点转向「本机数据不丢失、不越权、可回滚」 |
| 2 | **以项目内核为基础** | 21 个 Port 冻结；新能力优先通过现有 Port 实现或**窄扩展**（加方法/加字段），不新增内核 Port；能力实现放 `app/builtin_plugins/` |
| 3 | **最终打包为 .exe，双击可运行** | 数据库**只用 SQLite 嵌入式**（单文件、零配置、随 exe 分发）；不引入 Postgres/Redis/外部服务；新增 Python 依赖须评估打包体积；FTS5 需在目标 Python 的 sqlite3 上验证可用，不可用则降级 |

> 约束 1 与「保留 tenant_id」不矛盾：字段是内核既有契约，单机只是值固定。这样既不动历史迁移，又为将来企业版多租户留接口。**本轮不写的**：跨租户检索测试、租户注销级联、多副本 lease 抢占（单机无多副本）。

---

## 一、总体判断

z-torque 机制层骨架较强：21 Port 契约、约 160 端点、Function Calling + Tool 内核 + 任务状态机 + SSE 恢复；A2A/沙箱插件、RBAC、审计、MCP 客户端（Streamable HTTP + Stdio，但执行入口未接线）；前端已有对齐 DeepSeek Harness 的令牌体系。

核心问题仍是 **「契约有、实现空/弱」**：

- 知识库、向量存储、长期记忆多为空实现或进程内字典
- 沙箱缺少统一 Scope 与路径边界收口
- 上下文预算/压缩配置未接线
- 工具错误处理、插件能力声明存在静默降级
- **任务层与工具层存在同源的假成功路径**（详见 §2.5，v2.0 已纠正）

nanobot 更严谨之处在于「让 Agent 真正在真实文件/记忆/上下文里跑通」。优化策略：**吸收其不变量与数据流，落地为内核 Port 上的单机能力**，不是复制实现。

**明确不抄**：dreaming 六信号加权与 cron 托管、QMD/JSONL 兼容层、per-agent 多库 + seal、WebSocket 通道、单用户假设下的 workspace 模型、多租户隔离机制。

**v1.1 校正的纠错说明**：v1.1 曾把 `execute_simulated` 误判为「恢复路径、应保留」。经 `dispatcher.py:166-183` 复核，它在校验后**未执行 pending call**，直接写 `result={"mode":"simulated","status":"completed"}` + `[MOCK] task completed` 消息 + `SUCCESS`，调用条件是无 model_gateway 或执行返回 None（`runtime.py:181-185`），与恢复路径无关。v2.0 已改回：这是**必须移除的假成功**。

---

## 二、P0 — 正确性与安全优先（先修再加功能）

### 2.1 知识库：SQLite FTS5 本地检索闭环（强制引用）

**现状**  
`KnowledgeProvider.search` 与 `VectorStore` 仅有 ABC；`file_parser.py` 把 PDF 前 100 页、Excel 20 表×10000 行拼成纯文本并截断，**页码/行号在拼接时已丢失**，无法生成引用坐标；整段 `extracted_text` 直接注入 context。

**前置依赖（必须先做）**  
`file_parser.py` 升级为**结构化解析**：输出 `blocks[{text, page_no?, sheet?, row?, col?, line_start?, line_end?}]`，保留坐标；解析结果同时写 `extracted_text`（兼容旧路径）与新 chunk 表。xlsx 用 `sheet+row+col` 定位，PDF 用 `page_no`，md/txt 用 `line_start/end`。

**落地要点**

1. **主入口是 FTS5 + chunk**，find/grep 仅作代码类文档补充（nanobot 的 grep 面向代码工作区，z-torque 主体是 PDF/Excel）
2. 表设计（详见 §八）：`zr_knowledge_doc` / `zr_knowledge_chunk` / `zr_knowledge_fts`（FTS5 虚表），全部带 `tenant_id`（单机固定值）
3. **FTS5 同步机制**：采用 external-content + 触发器，或应用侧双写；chunk 入库失败时文档仍可按 path 精确读取，但检索标记 `DEGRADED`
4. **能力上报三态**：`knowledge.local.search = AVAILABLE | DEGRADED | UNAVAILABLE`；DEGRADED 必须在前端/工具可见，并写一条 audit 记录降级原因（不静默）
5. **检索 scope 绑定**：find/grep/fts 查询必须受 `allowed_paths` 约束，禁止「能 grep 整台机器」；路径经 `resolve_allowed_path` 收口（§2.3）
6. **超大文件保护**：单文件 chunk 数上限、单 chunk token 上限、总入库字节上限；超限截断并在 doc 上标 `truncated=true`，audit 记录
7. **删除级联**：文件删除（`files.py`）必须触发对应 doc/chunk/fts 清理；提供 `knowledge reindex` 命令用于存量回填
8. **会话检索**：先做短记忆摘要级检索；消息级全文检索做**显式开关（默认关）**，避免隐私成本
9. 禁止继续把整段 `extracted_text` 注入 prompt

**验收（真实入口）**

- 上传一个 PDF → 调用检索工具 → 返回片段带 `source+page`，该坐标能在 chunk 表命中
- 用 golden set（问题 → 期望文档/片段）验命中率与引用准确率；**可验证的是「无引用回答被系统标记/拒绝」**，而不是空泛的"模型无法编造来源"
- 删除文件后检索不再命中；FTS5 不可用时 capabilities 显示 DEGRADED 且 audit 有记录

---

### 2.2 长期记忆：持久化 + 作用域清晰 + 写入带约束

**现状**  
`long_memory.py:14-25` 为进程内字典，重启即失，默认关闭；短记忆是一次性 LLM 摘要 + SUPERSEDED 标记，无事实抽取/合并/冲突消解/去重。

**关键前置决定（v2.0 收敛）**  
全仓 `project_id` 0 命中，**没有 project 实体**。单机约束下不新建 Project 表（避免内核大改），改用三级作用域：`(agent_id, user_id, scope)`，其中 `scope ∈ {CONVERSATION, AGENT, USER}`；「项目级」记忆在单机下由 AGENT 级承载，未来有 Project 实体再加列迁移。不要写永远为 NULL 的 `project_id` 列。

**落地要点**

1. 长期记忆落 DB（`zr_memory_long`，详见 §八），替换进程内字典；保留 `memory.long` Port 契约，内置实现换为 DB 版
2. **不可变键语义**：稳定事实键标 `immutable=true`，写入后只能 append 新版本或标记 `SUPERSEDED`，禁止原地覆盖；可变键允许更新但必须留版本
3. **乐观并发**：`content_hash + version` 冲突返回明确错误码 `AI_MEMORY_CONFLICT`，不吞掉、不静默覆盖；失败退化为 append-only 需 audit 记录
4. **删除/过期接口**：至少 soft-delete + 审计，否则记忆只增不减会成为隐患；文件删除/用户删除需级联清理（单机无租户注销）
5. **隔离边界写死**：跨 agent 读取记忆必须显式授权；CONVERSATION 级默认只对所属会话可见
6. 写入约束：来源引用必填、改写丢失率上限、硬上限 + 淘汰策略
7. `context_builder` 做作用域分流：会话摘要走 CONVERSATION，稳定画像走 USER，项目/agent 指令走 AGENT（与 §2.4 来源分层合并实现）
8. Dream 式整合（游标表 + 原位替换 + 变更 diff）**推迟到持久化稳定之后**（P1）

**验收（真实入口）**

- 写入记忆 → 重启进程 → 同一 agent 能召回（不再丢失）
- 两个 agent 间无授权不能互读记忆；并发更新同一 immutable 键返回 `AI_MEMORY_CONFLICT`
- soft-delete 后不可召回且 audit 有记录

---

### 2.3 沙箱：显式 Scope、唯一执行入口、事务式补丁

**现状**  
process 模式已有 `python -I`、最小 env、cwd 白名单、POSIX rlimit、输出上限、超时杀进程树；container 模式有 `--network none`/`--read-only`/cap-drop。缺口：无 restricted/full 模式、无统一路径边界入口、审批与执行点脱节。

**落地要点**

1. **Scope 用显式不可变参数，不用 ContextVar**：`asyncio.to_thread` 只单向复制上下文（线程内改动不回传），subprocess 完全不继承。Scope（路径白名单 + 权限模式）作为 `SandboxDriver.execute(scope, options)` 的显式参数贯穿，禁止靠 ContextVar 传递
2. **SkillGateway 是唯一执行入口**：任何绕过它直接调 `sandbox_driver` / `process.spawn` 的路径视为违规，加 CI 静态检查拦截
3. **审批后二次校验**：confirmation 通过后在**执行点**再校验一次 scope，防审批后参数被篡改
4. **process 模式定位**：声明为「可信开发态」。单机 exe 若既无容器也无 WSL，**直接拒绝不可信执行**（保持 `AI_CAPABILITY_UNAVAILABLE`），scope 只作纵深防御，绝不把「无 OS 隔离 + 有路径访问」设为默认兜底
5. **policy gate**：`(tool, sandbox_mode, agent) → allow/deny`，deny-wins；manifest 声明的 permissions 必须被 gate 消费
6. **PatchTransaction 并发安全**：
   - `old_text` 唯一匹配 + 提交前 hash 复校验（防 TOCTOU）
   - 多文件先备份全部，失败逐项回滚；回滚本身失败时收敛为 `PARTIAL` 并上报告警
   - 符号链接：不仅 `O_NOFOLLOW` 最后一级，父目录链也要 `realpath` 校验
   - 文件锁避免并发写同一路径
7. **diff 与审计分离**：before/after diff 存**独立 artifact 表**（TTL、脱敏、租户隔离），审计只记 `artifact_id + hash + 路径 + 大小`，不违反 metadata-only 审计原则
8. **超时可中断**：`kill` 进程树 + 确认线程退出，避免 `to_thread` + `wait_for` 超时后线程仍在跑产生幽灵副作用

**验收（真实入口）**

- 越权脚本经 SkillGateway 被 gate 拒绝，audit 有 `sandbox.deny` 记录
- 篡改审批后参数在执行点被二次校验拦下
- 补丁写入前目标文件被并发修改 → hash 复校验失败并回滚，无残留
- 无容器环境上报 `AI_CAPABILITY_UNAVAILABLE`，不静默降级为 process

---

### 2.4 上下文治理：预算 + 压缩 + 稳定前缀 + 失败回退

**现状**  
最近 5 条 + `max_chars=48000` 字符预算；`context_max_tokens`/`context_recent_rounds` 仅在 config 定义未接线；无压缩、无自愈；每次重排破坏前缀复用；pyproject **无任何 tokenizer 依赖**。

**前置决定**

- **token 计量**：单机 exe 不引入重型 tokenizer；P0 先用可配置估算（字符数 / 系数）+ 保留 `context_max_tokens` 作为预算目标，capabilities 上报 `context.token_estimate = true`；若打包体积允许，P1 再引入轻量 tokenizer 做精确计量
- **`max_chars=48000` 定位**：作为 token 预算的**兼容兜底**保留，接线后以 token 预算为准，字符预算仅作硬上限二次保护，二者取小

**落地要点**

1. 接线 `context_max_tokens` / `context_recent_rounds`，预算 = context_window − max_output − safety buffer
2. **硬不变量**：工具调用与 toolResult 必须成对保留；任何压缩/裁剪算法不得单独丢弃其中一方
3. **压缩失败回退**：禁止写坏 summary 覆盖原历史；失败保留原消息 + 写 audit
4. **自愈管线顺序**（写死）：`strip_placeholder → strip_malformed_tool_calls → drop_orphan_tool_results → backfill_missing_tool_results → apply_tool_result_budget`
5. **accepted-history 边界压缩（H/delta 分离）**：只摘要模型已接受的历史前缀，保留未发送增量；摘要失败保留原历史
6. **稳定前缀**：工具目录按名排序、技能/workspace 文件在前、时间戳等 per-turn 元数据在后；同一会话内 prompt 只追加不改写
7. **cache key** 含 provider/model：`(conversation_id, provider, model, compaction_seq)`
8. **来源分层**（合并 v1 §2.4-7 与记忆注入）：不可信内容在构建 prompt 前结构性剔除，不靠提示词警告；内部续跑提示用 marker 注入、展示时按 suffix 精确剥离

**验收（真实入口）**

- 长会话（>N 轮）关键历史不再静默丢失；工具调用成对可验证
- 注入畸形 tool call / 孤儿 result → 自愈管线下一次请求前修复，不卡死会话
- 压缩失败时历史完整保留且 audit 有 `context.compaction_failed`

---

### 2.5 任务可靠性：移除同源假成功 + lease fencing

**现状（v2.0 纠正）**  
假成功有**两处同源路径**：

- `MockExecutor.execute()` 无条件返回 `SUCCEEDED`，且生产装配无条件注册（`executors.py:39-43`、`plugins.py:155`）
- `execute_simulated()` 校验 checkpoint 后**不执行 pending call**，直接写 `{"mode":"simulated","status":"completed"}` + `[MOCK] task completed` + `SUCCESS`（`dispatcher.py:166-183`），触发条件是无 model_gateway 或执行返回 None（`runtime.py:181-185`）

两者都是「看似成功、实则什么都没做」。**恢复路径是另一条**（`_resume_chat_task`/`_resume_skill_task`），不要混为一谈。

**落地要点**

1. **移除工具层假成功**：`MockExecutor` 从生产 `skill_executors` manifest 移除，仅测试 DI 保留（写进测试约定，防回挂）
2. **拆分 `execute_simulated`** 为 `verify_checkpoint()` + `execute_pending_call()`：校验通过后**必须真实执行**；无法执行显式 `FAILED(AI_EXECUTOR_UNAVAILABLE)`，禁止编造成功消息
3. **扫查其他假成功**：检查所有 executor/插件是否在无真实实现时默认返回 SUCCEEDED（含 `McpExecutor` 占位，见 P1a-1）
4. **lease + fencing token**：`zr_task` 加 `lease_owner`/`lease_expires_at`/`lease_heartbeat_at`/`attempt_epoch`；终态写入前校验 epoch，防过期持有者写终态。单机下 lease 主要防「同一进程内并发 + 崩溃残留」，不做多副本抢占
5. **取消协作**：`cancel_requested_at` 已存在；持有执行体的 worker 在下一次心跳/执行点检查标志并退出，需 ack + 在途副作用对账（幂等键 + 执行日志），不只靠 `asyncio.Event`
6. `recover()` 判定依据写清楚：checkpoint 处于已提交阶段 / 工具是否已真实执行（查执行日志）；不可回滚副作用存在时不得把 RUNNING 改回 QUEUED
7. 幂等 reply 存储与 boot ledger 落入 §八清单

**验收（真实入口）**

- 拔掉 model_gateway 后发任务 → 显式 FAILED(AI_EXECUTOR_UNAVAILABLE)，**不再出现 `[MOCK]` 消息**
- 生产 capabilities 不出现无条件成功的 MOCK 执行器
- 崩溃重启后 recover 不重复产生副作用；取消标志被 worker 在检查点消费并 ack

---

### 2.6 工具错误回喂与 Schema 校验

**落地要点**

1. JSON Schema Draft 2020-12，**严格校验 + 最多一次结构化修复回合**（纯无 coercion 在模型类型漂移时成功率低且不自愈）
2. 错误带 `json_path` + `Did you mean` + 自我纠正提示回喂模型
3. MCP 用原始 `inputSchema` 校验、展示 schema 才归一化；**并写明 `McpExecutor` 目前是占位（返回 `AI_MCP_UNAVAILABLE`），执行链路未接线**，P1a-1 才补
4. 静默降级改显式失败或登记降级能力，可观测

**验收**：类型漂移的工具调用经一次修复回合后成功；仍失败时错误可读且审计有记录。

---

## 三、P1a — 后端正确性与治理（先于前端）

| 项 | 说明 | 依赖项 | 出口指标 |
|----|------|--------|----------|
| 1 | MCP 执行链路接线 | §2.5 假成功清理 | MCP 工具真实执行，`AI_MCP_UNAVAILABLE` 仅在未连接时出现 |
| 2 | MCP 生命周期 | 共享 ToolRegistry、独占 owner、失败回滚、分页发现后白名单注册、connect/aclose 归属调用方 | 工具对模型可见且可热更新 |
| 3 | 审计字段收紧 + usage 归一化 | §2.3 diff artifact | metadata-only；token 分类记「未知」而非 0 |
| 4 | 工具选择器 + depends_on + orchestration_hint | §2.6 | 大量工具绑定正确率 |
| 5 | 工具错误回喂（§2.6） | — | 一次修复回合成功率 |
| 6 | 子代理上下文继承（scope + 记忆） | §2.2 | 委派结果连贯性 |
| 7 | Skills 三级注入 + 硬预算 | §2.4 | prompt 膨胀受控 |
| 8 | Dream 式整合（游标 + 原位替换 + diff） | §2.2 稳定后 | 事实冲突数下降 |
| 9 | tokenizer 精确计量（可选，评估 exe 体积） | §2.4 | token 估算误差 |

## 三-b、P1b — 前端体验（不阻塞机制层）

| 项 | 说明 |
|----|------|
| 1 | 文件 diff 与代码高亮（shiki + diff，Drawer 承载） |
| 2 | Agent 活动可视化（复用 chatRun 时间线 + Think） |
| 3 | 多会话分栏（复用 PaneResizer，Grid 拖拽 + 键盘 resize） |
| 4 | Composer 增强（附件拖拽、@ 提及、模型预设、流式排队） |
| 5 | 上下文/token 用量图（CSS 柱状图） |
| 6 | 前端能力三态展示（AVAILABLE/DEGRADED/UNAVAILABLE） |

**前端迁移原则**：保留 antd 6 + ConfigProvider；只移植 CSS 变量、排版/圆角/交互态与组件行为；最多新增 shiki、diff、motion；不引入 Tailwind/Radix。

---

## 四、P2 — 治理与选择性扩展（单机语境）

- manifest-first 校验与 capability ladder（扩展现有 → 用现有契约 → 缺契约才加窄能力 → 最后进核心）
- repositories 收口**属主过滤**（单机无跨租户，但要防跨 agent/用户越权）
- Windows 能力显式降级与文档化（无容器即拒绝不可信执行）
- 对外 MCP Server（涉及鉴权设计，单机下先不做）
- 自动化调度 / cron（单本机调度，无需租户隔离）
- i18n、主题/背景/字号进阶
- Channels 扩展前的凭据托管与审计

---

## 五、架构方向（不阻塞 P0）

1. **分层解耦**：Channel → Bus → AgentLoop → Runner → Provider → Tools 的可插拔思路，用于强化 kernel 事件总线语义
2. **Provider registry 单一元数据 + 自动路由五段式**：显式 provider → registry 关键词 → API key 前缀 → apiBase 本地回退 → gateway 兜底
3. **安全边界映射**：workspace scope / shell 沙箱 / SSRF / token 分层 → 现有 RBAC + `secret://`
4. **CI 按变更范围裁剪 + 覆盖率门槛**：适合内核与样式重构治理
5. **验收思维**：每条改动必须回答「哪个真实入口能复现、修复后同一路径能通过」

> 说明：本节多为需要改代码的方向性建设，不列入 P0 阻塞项。

---

## 六、推进顺序与验收场景

**Phase 0（正确性与安全）**

| # | 项 | 真实入口验收场景 |
|---|----|------------------|
| 1 | 移除 `MockExecutor` + 拆 `execute_simulated` + lease/epoch | 拔掉模型配置发任务 → 显式 FAILED，无 `[MOCK]` 消息 |
| 2 | `context_builder` 接预算 + 压缩 + 自愈 + 稳定前缀 | 注入畸形 tool call → 下次请求前自愈；压缩失败历史完整 |
| 3 | 长期记忆落库 + 三级作用域 + 不可变键 | 重启后召回成功；跨 agent 无授权不可读 |
| 4 | 沙箱显式 Scope + SkillGateway 唯一入口 + PatchTransaction | 越权脚本被拒且 audit 有记录；并发改文件 → 回滚无残留 |
| 5 | 知识库 FTS5 + 结构化 parser + 强制引用 + golden set | 上传 PDF → 检索返回带 page 引用且能命中 chunk 表 |

**Phase 1（P1a 后端 → P1b 前端）**：按 §三 / §三-b 表格顺序，每项先补负向用例再实现。

**Phase 2（治理）**：manifest 校验、属主过滤收口、Skills 预算、子代理继承、可选能力扩展。

---

## 七、适配约束与边界（单机定稿）

- **单机安装**：保留 `tenant_id` 但固定值；不做跨租户设计与测试
- **SQLite 嵌入式**：数据库只有 SQLite；FTS5 可用性在启动/装配期探测并上报，不假设存在；不引入 Postgres/Redis
- **内核为基础**：21 Port 冻结，优先现有 Port 与窄扩展；实现放 builtin_plugins
- **exe 打包**：新增依赖须评估体积；无外部进程依赖；`python -I` 与相对路径在打包环境下需专门验证
- 每个敏感读写都落 audit；metadata-only（diff 走独立 artifact）
- 保持 SSE 通道，不引入 WebSocket

---

## 八、DB Schema 变更清单（供评审，本轮不实施）

| 关联项 | 变更对象 | 字段/结构 | 说明 |
|---|---|---|---|
| §2.1 | 新表 `zr_knowledge_doc` | `doc_id`, `tenant_id`, `source_path`, `hash`, `status`, `truncated`, `created_at` | 文档元数据 + hash 去重 |
| §2.1 | 新表 `zr_knowledge_chunk` | `chunk_id`, `tenant_id`, `doc_id`, `seq`, `text`, `line_start`, `line_end`, `page_no`, `sheet`, `row_no`, `col_no`, `token_count` | 精确引用坐标（含 xlsx 的 sheet/row/col） |
| §2.1 | 新表 `zr_knowledge_fts` | FTS5 虚表 + `tenant_id` + `chunk_id` + `text` | 带租户列（单机固定值），external-content + 触发器同步 |
| §2.1 | `zr_file` 加列 | `indexed_at`, `index_status` | 索引状态可见 |
| §2.2 | 新表 `zr_memory_long` | `id`, `tenant_id`, `agent_id`, `user_id`, `scope`, `key`, `value`, `immutable`, `source_ref`, `content_hash`, `version`, `status`, `created_at`, `updated_at` | 三级作用域；**无 project_id** |
| §2.2 | 新表 `zr_memory_version` | `memory_id`, `version`, `value`, `superseded_by` | 不可变键版本链 |
| §2.3 | 新表 `zr_patch_artifact` | `artifact_id`, `tenant_id`, `task_id`, `path`, `before_text`, `after_text`, `hash`, `expires_at` | diff 独立存储，审计只记 id |
| §2.5 | `zr_task` 加列 | `lease_owner`, `lease_expires_at`, `lease_heartbeat_at`, `attempt_epoch` | fencing token 防旧持有者写终态 |
| §2.5 | 新表 `zr_boot_ledger` | `boot_id`, `started_at`, `shutdown_at`, `recovered_count` | 崩溃判定依据 |
| §2.5 | 新表 `zr_idempotent_reply` | `tenant_id`, `idempotency_key`, `task_id`, `reply_payload`, `created_at` | 幂等回复，避免重试产生第二答复 |
| §2.4 | 新表 `zr_context_summary` | `conversation_id`, `compaction_seq`, `summary`, `accepted_history_hash`, `status`, `created_at` | 压缩摘要版本化 + 失败回退依据 |
| §2.2/短记忆 | 改现有表加索引 | `zr_short_memory(conversation_id, source_end)` 唯一索引 | 改现有表，不是新表 |
| §2.6/usage | 新表 `zr_usage_summary` | `tenant_id`, `agent_id`, `model`, `token_in`, `token_out`, `cache_read`, `cache_write`, `window` | usage 归一化落地 |

**说明**：`zr_task.cancel_requested_at` 已存在，不重复加列；FTS5 降级迁移脚本（普通索引 + LIKE）必须一并准备；所有新表带 `tenant_id` 与索引策略。

---

## 九、治理章节（v2.0 新增）

1. **回滚与降级**：每项 P0 能力配 feature flag、双写期、回退路径；知识索引与记忆写入尤其需要
2. **存量数据**：`zr_file.extracted_text` 回填索引；短记忆迁移；提供 `knowledge reindex` / `memory verify` 命令
3. **删除与保留**：文件删除、用户删除的级联语义（索引、记忆、patch artifact）；单机无租户注销
4. **可观测性**：检索命中率/延迟、无引用率、记忆写入拒绝率、摘要失败率、沙箱拒绝率、取消 ack 延迟
5. **测试矩阵**：每条 P0 需「基线可复现失败」负向用例——越权脚本、并发摘要重叠、hash 冲突、崩溃后重复副作用；外加 Windows / 容器两条 lane（单机 exe 至少覆盖 Windows 打包态）
6. **审计事件清单（必须落）**：`knowledge.search`、`knowledge.index.degraded`、`memory.write`、`memory.conflict`、`sandbox.deny`、`patch.commit`、`context.compaction_failed`、`task.executor_unavailable`、`lease.fencing_reject`
7. **`capabilities` 一致性**：知识、记忆、沙箱、上下文能力变更后，接口实时反映，避免前端/模型以为可用实际 UNAVAILABLE

---

**文档维护**：能力变更或对照结论更新时同步修订版本号；§九合并节奏——Phase 0 结束后把 P0 补充项正式并入正文，避免正文与补充长期分裂。  
**关联文档**：`Z-Torque_核心插件详细开发文档_v1.2.md`、`Z-Torque_工具绑定与依赖治理优化方案_v1.md`、`Z-Torque_标准化插件_A2A_审计导出归档_沙箱_v1.2.md`、`docs/111.md`。  
**对照来源**：nanobot 仓库 + 内部逐模块代码对照记录。
