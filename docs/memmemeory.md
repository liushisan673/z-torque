# Phase 0 实施记忆

## 第 2 项：context_builder 接预算 + 压缩 + 自愈 + 稳定前缀（§2.4）— 已完成，待用户确认

日期：2026-11-24
状态：实现完成，全量 pytest 633 passed / 24 skipped，本项 ruff 全绿，alembic 链验证通过。

### 三项最高约束遵守情况
1. 单机安装：`zr_context_summary` 保留 `tenant_id` 但值由会话继承（固定租户），未写任何跨租户查询隔离/测试。
2. 内核 Port 冻结：未新增 Port，未改 `app/kernel/ports/specs.py` 的 `assert len(PORT_SPECS)==21`；新增能力以 `app/services/context_governance.py` 纯函数 + 现有 `memory.working` 缓存实现。
3. exe 打包：未新增任何依赖（`pyproject.toml` 未动）；token 用「字符数 / 系数」估算，无 tokenizer；SQLite 迁移已验证。

### 改动文件
新增：
- `app/services/context_governance.py`：token 估算、5 步自愈管线、稳定前缀、H/delta 分离、抽取式摘要、不可信模板 token 剔除。
- `app/models/context_summary.py` + `alembic/versions/e8f4a2c6d317_context_summary.py`（head 从 `d9e4f7a2b6c1` 递进）。
- `tests/integration/test_phase0_context_governance.py`：5 条真实入口验收。

修改：
- `app/services/context_builder.py`：接线 `context_recent_rounds`/`context_max_tokens`；去掉逐条字符裁剪（避免静默丢历史）；装配后自愈→稳定前缀→H/delta 压缩→`max_chars` 硬上限兜底；cache fingerprint 含 provider/model/compaction_seq；压缩失败落 `CONTEXT_COMPACTION_FAILED` 审计。
- `app/services/execution_loop.py`：每轮 `generate()` 前跑自愈管线。
- `app/services/tool_mapping_builder.py`：工具目录按 name 排序。
- `app/config.py`：新增 `context_chars_per_token=2.0`、`context_tool_result_max_chars=20000`。
- `app/kernel/audit/categories.py`：登记 `CONTEXT_COMPACTION_FAILED` → `model_call`。
- `app/main.py`：`GET /api/z-torque/capabilities` 上报 `context.token_estimate=true`。
- 3 处 `build_context` 调用点传 `model`/`trace_id`：`chat.py`、`chat_application.py`、`task/runtime.py`。

### 关键设计决定（后续项勿踩坑）
- **预算口径**：`context_max_tokens` 为目标（字符等价 = max_tokens × chars_per_token），`max_chars=48000` 仅硬上限，`min()` 取小；不在装配期逐条裁剪，否则压缩永远拿不到可压历史（第一版踩过，测试暴露后已改）。
- **自愈顺序写死**：`strip_placeholder → strip_malformed_tool_calls → drop_orphan_tool_results → backfill_missing_tool_results → apply_tool_result_budget`；backfill 的 `[tool result unavailable]` 不在占位符剔除名单里，保证跨轮成对稳定。
- **压缩摘要是确定性抽取式**（`extractive_summary`），不依赖 LLM；失败由 monkeypatch 注入验证回退路径。
- **`zr_context_summary` 无 `project_id`**（§2.2 决定），status 为 ACTIVE/SUPERSEDED 预留但本项只写 ACTIVE。
- **capabilities 的 context 段**为匿名可读；敏感细节仍未暴露。

### 真实入口验收（5 条，均通过）
1. `GET /api/z-torque/capabilities` → `data.context.token_estimate === true`。
2. 压缩失败注入 → `build_context` 返回原 8 条历史全在，audit `CONTEXT_COMPACTION_FAILED` 1 条，summary 表 0 行。
3. 压缩成功 → `zr_context_summary` seq=1、hash 64 位、`result[-1]=="turn-7"`（delta 保留）。
4. `AgentExecutionLoop.run()` 注入畸形 tool call + 孤儿 result → 下次 `generate()` 前成对（`tool_pairs_balanced`），孤儿被剔除、缺失结果回填。
5. `<|im_start|>` 等模板 token 被结构性剔除。

### 测试结果
- `pytest -q`：**633 passed, 24 skipped**（含全量回归）。
- `ruff check`（本项 14 个文件）：**All checks passed**。
- 项目级 `ruff check .` 仍有 3 条**存量**问题（`sandbox_driver/container.py` 的 2×ASYNC240、`tests/unit/test_chat_seed_and_progress.py` 的 I001），与本项无关。
- alembic 临时库 `upgrade head` 全链通过，`zr_context_summary` 9 列建成。

### 下一步建议
Phase 0 第 3 项：长期记忆落库 + 三级作用域 + 不可变键（§2.2）。
注意：不建 `project_id` 列；`zr_memory_long` / `zr_memory_version` 按 §八 Schema；并发冲突返回 `AI_MEMORY_CONFLICT`；本项已登记的 `CONTEXT_COMPACTION_FAILED` 属 `model_call` 类，记忆类审计事件需另登记。

---

## 前置修复：I001
`tests/unit/test_chat_seed_and_progress.py` 的 import 排序已 `ruff --fix` 修复。
项目级 `ruff check .` 现仅剩 2 条存量：`sandbox_driver/container.py` 的 167/185 行 ASYNC240，
归属 Phase 0 第 4 项沙箱改造（用户已确认留到该项处理）。

## 第 3 项：长期记忆落库 + 三级作用域 + 不可变键（§2.2）— 已完成，待用户确认

日期：2026-11-24
状态：实现完成，全量 pytest 637 passed / 24 skipped，项目 ruff 仅剩 2 条沙箱存量，迁移验证通过。

### 三项最高约束遵守情况
1. 单机固定租户：`zr_memory_long` / `zr_memory_version` 均带 `tenant_id`，值由会话继承；**无 project_id 列**，项目级由 AGENT scope 承载；无跨租户测试。
2. 内核 21 Port 冻结：`memory.long` 契约只做**窄扩展**（保留原 3 个无 db 方法 + 新增 `remember_scoped`/`recall_scoped`/`recall_bundle`/`forget_scoped`），未改 `PORT_SPECS` 数量；实现放 `app/builtin_plugins/long_memory.py`。
3. exe 打包：无新依赖；只用 SQLite；新表全部带索引；单条 value 硬上限 20000 字符、记录上限 5000（超限显式 `AI_MEMORY_QUOTA_EXCEEDED`，不静默增长）。

### 改动文件
新增：
- `app/models/memory_long.py`：`LongMemory` / `LongMemoryVersion` ORM（无 project_id）。
- `alembic/versions/f9a7b3c5d182_memory_long_scoped.py`（head: e8f4a2c6d317 → f9a7b3c5d182）。
- `tests/integration/test_phase0_long_memory.py`：4 条真实入口验收。

修改：
- `app/kernel/ports/memory_long.py`：三级作用域常量 + scoped 方法（默认实现显式 `AI_CAPABILITY_UNAVAILABLE`，不静默退化）。
- `app/builtin_plugins/long_memory.py`：`DbLongTermMemoryStore` 替换原进程内 KV；兼容入口按 key 前缀推导 scope；`InProcessKvLongMemory` 保留为 TEST-ONLY。
- `app/kernel/registry/active.py`：`call_optional` 加 `pass_db` 开关，把调用方 Session 注入 scoped 方法。
- `app/services/context_builder.py`：`memory_long_enabled` 时用 `recall_bundle` 做 AGENT/USER/CONVERSATION 分流，全部经 `sanitize_untrusted`。
- `app/kernel/task/runtime.py`：摘要写 CONVERSATION 作用域，`source_ref=short_memory:{id}`。
- `app/config.py`：`memory_long_max_value_chars=20000`、`memory_long_max_records=5000`。
- `app/kernel/audit/categories.py`：新增 `CATEGORY_MEMORY` + `MEMORY_WRITE`/`MEMORY_CONFLICT`/`MEMORY_DELETED`。
- `app/main.py`：capabilities 新增 `memory.long_persistent=true` 与 `long_scopes`。

### 关键设计决定（后续项勿踩坑）
- **CONVERSATION 边界是 key 里的 conversation_id，agent_id 可为空**（兼容老 KV 写入），查询用 `or_(agent_id IS NULL, agent_id == 当前)`；跨会话靠 key 前缀 `conversation:{id}:` 校验。第一版写成 AND 导致召回失败，测试暴露后已改。
- **immutable append-only**：旧行 `status=SUPERSEDED`，新行 version+1；`zr_memory_version` 同时记录「旧版本被谁取代」（superseded_by）。mutable 键原地更新但写版本行。
- **乐观锁**：`expected_version` / `expected_hash` 任一不匹配 → 抛 `AI_MEMORY_CONFLICT` + audit `MEMORY_CONFLICT`，不吞异常。
- **软删除只改 status=DELETED，不物理删**；`recall_scoped`/`recall_bundle` 过滤 ACTIVE。
- **未写 `project_id` 列**，未建 Project 表；未来有 Project 实体再加列迁移。
- **Dream 式整合（游标 + 原位替换）仍是 P1**，本项只做持久化与版本化。

### 真实入口验收（4 条，均通过）
1. 写入 → 全新 store 实例 + 新 Session（模拟重启）三级作用域全部召回；跨 agent 读 AGENT 级返回 None；错会话读 CONVERSATION 返回 None。
2. immutable 键两次写入 → ACTIVE v2 + SUPERSEDED v1 + 版本链；带 `expected_version=1` 冲突写抛 `AI_MEMORY_CONFLICT` 且 audit 1 条。
3. soft-delete → 不可召回 + `MEMORY_DELETED` audit 带 key。
4. `GET /api/z-torque/capabilities` → `data.memory.long_persistent === true`、`long_scopes == ["CONVERSATION","AGENT","USER"]`。

### 测试结果
- `pytest -q`：**637 passed, 24 skipped**（全量回归，含上一项 633）。
- 项目级 `ruff check .`：仅剩 2 条沙箱存量 ASYNC240（第 4 项处理），I001 已修。
- alembic 临时库 `upgrade head` 全链通过：`zr_memory_long` 14 列、`zr_memory_version` 8 列建成。

### 下一步建议
Phase 0 第 4 项：沙箱显式 Scope + SkillGateway 唯一入口 + PatchTransaction（§2.3）。
顺带修 `sandbox_driver/container.py` 的 2×ASYNC240（`_kill_container` 中 `cid_file.read_text()` / `unlink()` 改 `asyncio.to_thread`，与第 4 项「超时可中断 + 确认线程退出」一并设计，避免返工）。

---

## 单系统能力收口 + Windows 有限解禁（2026-11-24，用户明确决策）

用户决策：取消社区版/企业版能力差距，本项目就是单一系统；Windows 脚本「解禁但加显式确认」，按最终 .exe 单机打包标准。

### 改动要点
- `app/builtin_plugins/modules.py`：`PROFILE_COMMUNITY` 全量 21 插件（追加 audit_export_archive + a2a），`PROFILE_ENTERPRISE = PROFILE_COMMUNITY`；`+` 选配语法保留给外部插件，重复选配内置插件拒绝。
- `app/config.py` 默认全开：`a2a_enabled=True`、`md_skill_script_enabled=True`、`md_skill_windows_process_enabled=True`（新增）、`audit_export_formats=['jsonl','csv']`；归档沿用 `audit_archive_schedule_hours=24`。
- `app/kernel/md_skill.py`：`MdSkillManifest` 新增 `windows_trusted: bool = False`（SKILL.md frontmatter 解析 + snapshot 透传）。
- `app/kernel/md_skill_runtime.py`：Windows 从「一律拒绝」改为三重闸门——总开关 + 技能 `windows_trusted` 声明 + allowed_roots 路径强制收口；未信任=`AI_SKILL_WINDOWS_UNTRUSTED` 403，越界=`AI_SANDBOX_DENIED` 403。
- `app/kernel/skill_gateway.py`：Windows 上每次 md_script 调用前 `pause_task_for_confirmation` 挂人工确认（有 task 上下文时），恢复执行凭 checkpoint；manifest 重建透传 `windows_trusted`。
- `app/main.py`：capabilities 的 sandbox 段新增 `script.enabled/windows_process/windows_requires_trusted/windows_requires_confirmation`，如实反映闸门。
- `app/builtin_plugins/audit_export_archive/jobs.py`：`start()` 对「首次安装尚未迁移建表」容忍（只吞 no such table，其它异常照抛），否则默认装配会让新装 exe 启动失败。
- `.env.example` 同步为单一系统默认全开说明。
- 测试前提随决策更新：`test_p2_assembly`、`test_profile_selection`、`test_plugin_config_schema`、`test_sandbox_hardening`（Windows 用例从「拒绝一切」改为「拒绝未声明可信」）、`test_v13_a2a_api`（显式关 A2A 才 503）；新增 `tests/integration/test_phase0_single_system.py`。
- `tests/conftest.py` 装配 fixture 补 `app.state.assembly_plugins`（capabilities 派生需要，与 main.py lifespan 对齐）。

### 约束遵守
无新依赖；21 Port 数量未变；无新表；tenant_id 语义不变；单机 .exe 默认全能力可用。

### 测试结果
- `pytest -q`：**641 passed, 24 skipped**（全量）。
- 项目级 `ruff check .`：仅剩 `sandbox_driver/container.py` 167/185 两条 ASYNC240（留 Phase 0 第 4 项）。
- 真实入口验证：默认装配 `community == enterprise`（21 插件）；capabilities 显示 A2A enabled / CSV / archive / Windows 需可信+确认。

### 下一步
用户在**新会话**开启 Phase 0 第 4 项（沙箱显式 Scope + SkillGateway 唯一入口 + PatchTransaction，§2.3），并一并修 2×ASYNC240。本会话不再继续。

---

## 第 4 项：沙箱显式 Scope + SkillGateway 唯一入口 + PatchTransaction（§2.3）— 已完成，待用户确认

日期：2026-11-24
状态：实现完成，全量 pytest 651 passed / 24 skipped（基线 641 + 10 条新增），项目 `ruff check .` 全绿（2×ASYNC240 存量清零），alembic 全链验证通过。

### 三项最高约束遵守情况
1. 单机固定租户：`zr_patch_artifact` 保留 `tenant_id` 但值由会话继承；无跨租户查询/测试；TTL 24h 只做本地清理语义。
2. 内核 21 Port 冻结：未新增 Port（`assert len(PORT_SPECS)==21` 未动）；`sandbox.driver` 仅窄扩展 `execute(scope, ...)` 与 `apply_patch(...)`；文件写入实现放 `app/builtin_plugins/sandbox_driver/patch.py`。
3. exe 打包：无新依赖、无外部服务；只用 SQLite；容器模式仍为可选 docker CLI（默认 process，无容器时 full scope 显式不可用，不降级）。

### 改动文件（diff 摘要）
新增：
- `app/kernel/sandbox_policy.py`：`(tool, mode, agent) → allow/deny` 的 deny-wins gate；消费 manifest permissions。
- `app/builtin_plugins/sandbox_driver/patch.py`：`FilePatchMixin`（唯一匹配 + hash 复校验 + 备份回滚 + 父目录链 realpath/O_NOFOLLOW + 独立锁文件；PARTIAL 上报告警）。
- `app/models/patch_artifact.py` + `alembic/versions/b4d8f0a2c913_sandbox_scope_patch_artifact.py`（head: f9a7b3c5d182 → b4d8f0a2c913，10 列 + 4 索引）。
- `app/api/routes/workspace.py` + `app/schemas/patch.py`：`POST /api/z-torque/workspace/patch` 真实入口。
- `tests/integration/test_phase0_sandbox_scope_patch.py`：8 条真实入口验收。

修改：
- `app/kernel/ports/sandbox_driver.py`：新增不可变 `SandboxScope` / `PatchSpec` / `PatchReport`；`execute(scope, ...)` 成为唯一入口（校验隔离形态、路径边界、full+process 拒绝）；`run` 降为实现细节；`apply_patch` 默认显式不可用。
- `app/plugin_sdk/ports.py`、`app/plugin_sdk/__init__.py`：公开面再导出，插件不越层 import 内核。
- `app/kernel/md_skill_runtime.py`：`execute(..., scope=...)` 显式透传，调用 `execute` 而非 `run`。
- `app/kernel/skill_gateway.py`：构造冻结 `_scope_base`；按 manifest 推导 scope；policy gate 前置 + 执行点二次校验；确认 checkpoint 的 `pending_call` 写入冻结 scope；`apply_patch()` 统一落 artifact + metadata-only 审计。
- `app/builtin_plugins/sandbox_driver/process.py`、`container.py`：继承 `FilePatchMixin`；container 声明 `isolation="container"`；`_kill_container` 与 finally 的文件 IO 改 `_bounded_to_thread`（超时 cancel + gather 确认线程退出），docker kill 超时也 `_terminate_process_tree` + `_wait_exit` 确认进程退出。
- `app/kernel/audit/categories.py`：`PATCH_COMMIT` / `PATCH_CONFLICT` / `PATCH_ROLLBACK` / `PATCH_PARTIAL` 归 `CATEGORY_SANDBOX`；`SANDBOX_DENIED` 复用。
- `app/main.py`：capabilities 新增 `sandbox.scope` / `sandbox.patch` / `sandbox.untrusted_execution`。
- `app/api/router.py`、`app/models/__init__.py`、`tests/unit/test_plugin_redlines.py`（新增 2 条静态红线：唯一执行入口 + 补丁机制存在）。

### 关键设计决定（后续项勿踩坑）
- **Scope 不用 ContextVar**：`SandboxScope` frozen dataclass 逐层显式传（Gateway → Runtime → Driver.execute），checkpoint 中用 `to_dict/from_dict` 冻结；异步/线程/subprocess 都不依赖隐式继承。
- **restricted vs full**：restricted 根 = md_skills_dir（可信开发态）；full 根 = 配置白名单且**必须 container**。`full + process` 显式 `AI_CAPABILITY_UNAVAILABLE`，绝不静默降级；capabilities 的 `untrusted_execution` 如实上报。
- **审批后二次校验**：Windows 脚本确认的 `pending_call` 加入 `sandbox_scope`，`pending_call_hash` 自动覆盖；恢复时 `_check_approved_scope` 在 gate 后与执行点各校验一次，篡改即 `SANDBOX_DENIED` + detached audit。老 checkpoint 无 scope 一律拒绝（fail-closed）。
- **gate 顺序**：deny-wins，路径越界 → `AI_SANDBOX_DENIED`；manifest 声明 `sandbox.full` 而隔离为 process → `AI_CAPABILITY_UNAVAILABLE`（不是通用权限错误）；所有 gate 拒绝都落 `SANDBOX_DENIED` 审计，detail 带真实 code。
- **补丁 TOCTOU**：prepare 阶段校验 expected_hash + old_text 唯一；commit 阶段**再次读取复校验 hash**；写入用独立 `.ztlock` 锁文件；回滚失败收敛 `PARTIAL` 并 emit `sandbox.patch_partial`。
- **diff 与审计分离**：before/after 只进 `zr_patch_artifact`（TTL 24h），审计 detail 只有 artifact_id/hash/path/size；Windows 上文本读写用 `newline=""` / `write_bytes`，避免 CRLF 改写导致 hash 误判。
- **容器清理**：`asyncio.to_thread` 只单向复制上下文，所有清理路径用有界包装，超时 `cancel` 后 `gather` 确认包装任务收敛；极端超时 emit `sandbox.thread_exit_unconfirmed`，不静默吞。

### 真实入口验收（8 条，均通过）
1. 匿名 `GET /api/z-torque/capabilities` → `sandbox.scope.explicit/gateway_only/full_requires_container=true`、`patch.available=true`、`untrusted_execution="unavailable"`。
2. 登录 → `POST /workspace/patch` 合法补丁 → 文件内容变更、`zr_patch_artifact` 有 before/after、`PATCH_COMMIT` 审计 detail 仅 metadata（无正文）。
3. 登录 → `POST /workspace/patch` 提交前并发改文件 → 409 `AI_PATCH_CONFLICT`、文件保持并发写入值、无 artifact 残留、`PATCH_CONFLICT` 审计存在。
4. `SkillGateway.execute_async` 执行 manifest 越界路径 → FAILED `AI_SANDBOX_DENIED` + `SANDBOX_DENIED` 审计（gate 消费 manifest 声明与路径）。
5. manifest 声明 `sandbox.full` 但无容器 → FAILED `AI_CAPABILITY_UNAVAILABLE` + 审计，不降级为 process。
6. 审批 checkpoint 中 `sandbox_scope` 被篡改 → 执行点二次校验 FAILED `AI_SANDBOX_DENIED` + 审计。
7. `MdSkillRuntime.execute(..., scope=full)` 落 process 驱动 → 显式 `AI_CAPABILITY_UNAVAILABLE`。
8. `_bounded_to_thread` 超时 → cancel + gather 确认退出（无遗留 to_thread 任务）；CI 静态红线禁止 kernel/services/api 直调 spawn/run。

### 测试结果
- `pytest -q`：**651 passed, 24 skipped**（全量回归；新增 8 集成 + 2 红线）。
- 项目级 `ruff check .`：**All checks passed**（2×ASYNC240 存量已清零，无新增 lint）。
- alembic 临时库 `upgrade head` 全链通过：head=`b4d8f0a2c913`，`zr_patch_artifact` 10 列建成；临时库已清理。

### 下一步建议
Phase 0 第 5 项：知识库 FTS5 + 结构化 parser + 强制引用 + golden set（§2.1）。
注意：本项 artifact 表已把 head 推到 `b4d8f0a2c913`，第 5 项新表须以它为 down_revision；路线图 §八 要求 FTS5 降级迁移脚本一并准备（普通索引 + LIKE），启动/装配期探测 SQLite FTS5 可用性并上报；不要引入 tokenizer 或外部检索服务。

---

## 第 5 项：知识库 FTS5 + 结构化 parser + 强制引用 + golden set（§2.1）— 已完成，待用户确认

日期：2026-11-24
状态：实现完成，全量 pytest 657 passed / 24 skipped（上一项 651 + 6 条新增），`ruff check .` 全绿，alembic 全链 `upgrade head` 至 `c6e8a1b3d704` 成功。

### 三项最高约束遵守情况
1. 单机固定租户：doc/chunk 保留 `tenant_id`，检索按 tenant_id 过滤；allowed_paths 由服务端按当前用户文件推导，无跨租户设计。
2. 21 Port 冻结：`PORT_SPECS` 未动；`knowledge.provider` 从 UNAVAILABLE 变为内置实现（窄扩展 search(db=...) / index_file / delete_file / reindex / mode），实现放 `app/builtin_plugins/knowledge/`；内置插件数 21→22，相关测试断言同步更新。
3. exe 打包：无新依赖、无外部服务；PDF/XLSX 仍用现有 pypdf/openpyxl；FTS5 不可用时同构普通表 + LIKE 降级，不引入 tokenizer/外部检索。

### 改动文件（diff 摘要）
新增：
- `app/kernel/ports/knowledge_provider.py` 内新增 `ParsedBlock` / `ParsedFile` / `KnowledgeChunk` 不可变契约，Port 窄扩展 5 个方法（默认显式不可用）。
- `app/builtin_plugins/knowledge/plugin.py` + `__init__.py`：`SqliteKnowledgeProvider`（FTS5 MATCH/bm25、LIKE 降级、坐标检索、应用侧双写、reindex 容错）、`ensure_schema()` 启动幂等建表、`probe_fts5()` 运行期探测。
- `app/models/knowledge.py` + `alembic/versions/c6e8a1b3d704_knowledge_fts.py`（head: b4d8f0a2c913 → c6e8a1b3d704；doc/chunk 表 + FTS5 虚表或同构降级表 + 普通索引）。
- `app/api/routes/knowledge.py` + `app/schemas/knowledge.py`：`GET /knowledge/status`、`POST /knowledge/search`、`POST /knowledge/verify-answer`、`POST /knowledge/reindex`。
- `app/services/knowledge_service.py`：内核编排（装配取 Port、降级审计去重、入库/删除/检索/白名单推导）。
- `app/services/citation_guard.py`：结构化引用标记校验。
- `tests/fixtures/knowledge_golden.json` + `tests/integration/test_phase0_knowledge_fts.py`：6 条真实入口验收（含最小 PDF 生成器，无新测试依赖）。

修改：
- `app/services/file_parser.py`：新增 `parse_file_structured()`，PDF 逐页 page_no、XLSX 逐单元格 sheet/row/col、txt/md/csv 行号；块数/块字符/总字节三档上限 + `truncated`。
- `app/api/routes/files.py`：上传走结构化解析并索引（索引失败不阻断上传、detached audit 降级）；删除级联清 doc/chunk/FTS。
- `app/services/context_builder.py`：不再整段注入 `extracted_text`，改注入带 `[chunk:id]` + 坐标的检索片段；system prompt 追加强制引用规则。
- `app/main.py`：capabilities 新增 `knowledge.local.search / fts5 / citation_enforcement` 三态。
- `app/kernel/audit/categories.py`：新增 `CATEGORY_KNOWLEDGE` + `KNOWLEDGE_DEGRADED/INDEXED/TRUNCATED/CITATION_MISSING`。
- `app/config.py`：`knowledge_chunk_max_chars / max_chunks_per_file / max_total_bytes / top_k / message_search_enabled=False`。
- `app/builtin_plugins/plugins.py`、`modules.py`：装配 knowledge 插件；`tests/conftest.py` 测试库显式建/清 FTS 表；装配/档位测试断言 21→22 插件、resolved 19→20。

### 关键设计决定（后续项勿踩坑）
- **FTS 应用侧双写，不用 external-content + 触发器**：曾实测触发器在 create_all / 既有库路径下漏建，导致 FTS 内容可读但 MATCH 索引为空。现在 FTS5 与降级表列集合一致（`chunk_id, text`），`index_file` 显式 `INSERT INTO zr_knowledge_fts(rowid, chunk_id, text)`，删除按 chunk rowid 清；测试 cleanup 需显式 `DELETE FROM zr_knowledge_fts`。
- **三态如实上报**：`probe_fts5()` 运行期探测；`mode()` 返回 AVAILABLE/DEGRADED（无 provider 则 UNAVAILABLE）；DEGRADED 每租户只写一次 `KNOWLEDGE_DEGRADED` detached audit（进程内 `_DEGRADED_AUDITED` 去重）。
- **检索 scope 绑定**：`filters.allowed_paths` 由服务端从当前用户 `zr_file`（status=PARSED）的 object_store 本地路径推导，客户端不能指定；无白名单 fail-closed 返回空。
- **引用可回查**：`ParsedBlock` 坐标原样进 `KnowledgeChunk.metadata`，chunk 表列 page_no/sheet/row_no/col_no/line_start/line_end 与 prompt 的 `[chunk:id] (coord)` 一一对应；`validate_citations` 只认 `[chunk:id]` / `[source:...]` 结构标记，无引用 → REJECTED + audit。
- **prompt 体积**：`_media_content` 仍收集 doc_texts，但注入点改走 `_knowledge_excerpt()`（按用户最后一条消息关键词排序取 top 5，每条截断 500 字符，总 4000），不再有整段 extracted_text。
- **测试库特例**：FTS 虚表不在 ORM metadata（避免 alembic autogenerate 漂移），`test_alembic_migrations` 对 `zr_knowledge_fts*` 表的 remove_table diff 做了显式过滤；`conftest._test_database` 先 drop/建 FTS 表（无 FTS5 时建同构普通表）再 create_all。

### 真实入口验收（6 条，均通过）
1. 匿名 `GET /capabilities` → `knowledge.local.search ∈ {AVAILABLE, DEGRADED}`、`citation_enforcement=true`。
2. golden set：最小 PDF 上传 → `POST /knowledge/search` → 命中带 `source` + `page_no=1`，`chunk_id` 能在 `zr_knowledge_chunk` 回查且 page_no 一致。
3. `DELETE /files/{id}` → 再检索不命中，`zr_knowledge_doc` 无残留（删除级联）。
4. `POST /knowledge/verify-answer` 无引用 → `REJECTED` + `KNOWLEDGE_CITATION_MISSING` 审计；带 `[chunk:id]` → `OK`。
5. `build_context` 真实会话+附件消息 → 含 `[chunk:` 且注入长度 < 原文一半（不再整段 extracted_text）。
6. monkeypatch provider 为 DEGRADED → `GET /knowledge/status` 与 /capabilities 均显示 DEGRADED，且有降级审计。

### 测试结果
- `pytest -q`：**657 passed, 24 skipped**（全量回归；新增 6 条知识库真实入口验收）。
- 项目级 `ruff check .`：**All checks passed**。
- alembic 临时库 `upgrade head` 全链通过：head=`c6e8a1b3d704`，迁移日志显示 f9a7b3c5d182→b4d8f0a2c913→c6e8a1b3d704 依次执行；临时库已清理。
- 中间过程发现并修复：FTS5 external-content 触发器漏建 → 改应用双写；测试间 FTS 残留污染 → cleanup 显式清 FTS。

### 下一步建议
Phase 0 第 6 项：上下文治理（§2.4）——预算 + 压缩 + 稳定前缀 + 失败回退。注意 `zr_context_compaction` 表已由迁移 `e8f4a2c6d317` 建立（down_revision 链在 c6e8a1b3d704 之前），新改动应优先复用该表而不是再建；与本次 `_knowledge_excerpt` 的 prompt 注入点合并考虑稳定前缀，避免 chunk 片段与压缩摘要互相顶掉预算。
