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

### 下一步建议（2026-11-24 更正）
**Phase 0 已在第 5 项全部完成，路线图没有第 6 项**（见路线图 §六 表格第 254-262 行，仅 1～5 项）。此前写成"第 6 项 §2.4"是错误顺推：§2.4 实际对应表格第 2 项（上下文治理），已完成；`zr_context_compaction` 表（迁移 e8f4a2c6d317）只是该项的表结构落地。
正确下一步：**Phase 1（P1a 后端 → P1b 前端）**，按路线图 §三 / §三-b 表格顺序逐项推进，每项先补负向用例再实现，不再自行编号。

---

## Phase 0 全量开启收口（2026-11-24，最终状态）

用户决定取消开发期"默认关闭/先不做"标准，Phase 0 已实现能力全部开启，立项偏差一并收口。状态：**全部通过，可进入 Phase 1**。

### 本次改动
1. **开关全开**：`memory_long_enabled=False → True`、`knowledge_message_search_enabled=False → True`。
   - 长期记忆：新增真实端到端用例，默认开启后写入 AGENT 记忆能经 `build_context` 注入 prompt（不再靠测试 monkeypatch 开开关）。
   - 消息级检索：此前**只有配置没有实现**，现补齐 `knowledge.provider` 窄扩展 `search_messages(db, tenant_id, user_id, query, top_k, conversation_id)`；内置实现按服务端派生 `user_id` 限定会话属主（LIKE 短文本检索，不依赖 FTS5），与文档 chunk 结果合并去重；新增 `KNOWLEDGE_MESSAGE_SEARCH` metadata-only 审计（只记 query_hash + hits）；capabilities 上报 `knowledge.message_search = AVAILABLE`。
2. **FTS schema 对齐路线图 §八**：新增迁移 `d7e9f2a4b6c8`（down_revision `c6e8a1b3d704`），FTS5 虚表/降级普通表均重建为 `(tenant_id, chunk_id, text)` 并从 chunk 表回填；应用侧写入与检索（MATCH + LIKE 两路）均带 tenant_id；conftest 同步。alembic head 现为 `d7e9f2a4b6c8`。
3. **卫生缺口**：`memory.long` PortSpec 描述改为"内置 SQLite 持久化（单机固定租户）"；`TASK_FENCING_REJECT` 与 `KNOWLEDGE_MESSAGE_SEARCH` 登记审计分类；`tool_pairs_balanced()` 从"仅测试调用"升级为生产硬断言（`heal_context` 末尾 + `build_context` 压缩/硬上限后各一次，不变量破坏抛 `AI_CONTEXT_TOOL_PAIR_VIOLATION`）。
4. 回归适配：`test_p2_robustness` 的 working-memory 分支改在用例内临时关闭长期记忆以保留覆盖，生产默认保持全开。

### 验证结论
- `pytest -q`：**641 passed, 24 skipped**（新增 2 条全开启验收），全绿。
- `ruff check .`：**All checks passed**。
- alembic 临时库 `upgrade head` 全链通过：head=`d7e9f2a4b6c8`，`zr_knowledge_fts` 列 = `['tenant_id','chunk_id','text']`；临时库已清理。
- Phase 0 表格 1～5 项逐项复核：核心实现 + 真实入口测试齐全，无默认关闭能力遗留。

### 供新会话使用
Phase 1（P1a 后端 → P1b 前端）按路线图 §三 / §三-b 表格顺序推进；若新迁移，down_revision 用 `d7e9f2a4b6c8`。

---

## Phase 1 第 1 项：MCP 执行链路接线（§三表格第 1 项）— 已完成，待用户确认

日期：2026-11-24
状态：负向用例先行，生产占位清理 + 真实网关执行验收完成；全量 pytest 645 passed / 24 skipped，ruff 全绿。

### 三项最高约束遵守情况
1. 单机安装：无租户隔离/矩阵改动；测试按单库 fixture 清理本地数据。
2. 内核 Port 冻结：`PORT_SPECS` 未动，未新增 Port；真实执行仍走既有 `SkillGateway` + `connector` Port 窄契约，能力实现放 `app/builtin_plugins/`。
3. exe 打包：无新依赖、无新表/迁移、无外部服务。

### 改动文件清单
新增：
- `tests/integration/test_phase1_mcp_execution.py`：4 条测试（生产占位负向、未协商 `AI_MCP_UNAVAILABLE` 负向、capabilities 真实 API、网关真实 `tools/call` + 执行日志）。

修改：
- `app/builtin_plugins/executors.py`：移除恒失败 `McpExecutor` 类，模块说明改为 MCP 经网关协商执行。
- `app/builtin_plugins/plugins.py`：`SkillExecutorPlugin` 不再注册 `McpExecutor()`，manifest 描述同步。
- `app/builtin_plugins/__init__.py`：移除 `McpExecutor` 惰性导出与 `__all__` 项。
- `app/main.py`：新增 `_mcp_capability()`，capabilities 返回 `mcp.execution / http_discovery / stdio_discovery`，如实反映装配与传输发现能力。

### diff 摘要
- 删除生产恒失败 MCP 占位执行器：`McpExecutor` 类、装配注册、包导出全部移除；`AI_MCP_UNAVAILABLE` 仅由 `NegotiatedMcpExecutor` 在未协商状态返回。
- capabilities 不再通过 `tool_executors` 暴露 `"MCP"` 占位执行器，新增独立 `mcp` 状态段。
- 真实 MCP 执行链路仍是：`ToolVersion(tool_type=MCP)` → `SkillGateway.execute_async()` → `_execute_mcp()` → `connector.call_tool()` → `SkillExecutionLog`；本项不再新增共享 ToolRegistry/分页发现（属第 2 项）。

### 真实入口验收结果（4/4 通过）
1. 生产 Manifest 负向：`SkillExecutorPlugin` 源文件中不再出现 `McpExecutor()`。
2. 未协商负向：`NegotiatedMcpExecutor.execute_async()` 未连接时返回 `AI_MCP_UNAVAILABLE`，不会调用工具。
3. `GET /api/z-torque/capabilities`：`tool_executors` 不含 `"MCP"`，`mcp.execution == "AVAILABLE"`，`http_discovery == True`，`stdio_discovery == False`（如实反映当前 stdio 无 `list_tools`）。
4. 已登记 MCP 工具经真实 `SkillGateway.execute_async()` 调用传输层：`call_tool("search", {"q":"route"})` 命中，`SkillExecutionLog.status == SUCCEEDED` 且关联正确 `tool_version_id`。

### 测试结果
- 新增测试：`4 passed`。
- 全量 `pytest -q`：**645 passed, 24 skipped**。
- 项目级 `ruff check .`：**All checks passed**。

### 关键设计决定（后续项勿踩坑）
- 本项严格只清第 1 项出口指标：执行链路真实可用、`AI_MCP_UNAVAILABLE` 仅在未连接时出现；共享 ToolRegistry、独占 owner、失败回滚、分页发现后白名单注册与 connect/aclose 归属调用方属于第 2 项，不在本项实现。
- `mcp.stdio_discovery=False` 是如实上报：当前 `StdioMcpConnector` 没有 `list_tools()`；不要为了让 capabilities 好看而伪报 true。
- 负向用例用源码扫描锁定生产装配不得回挂占位执行器，保护 §2.5「移除假成功」的既有成果。

### 下一步（须用户确认后再做）
Phase 1 第 2 项：MCP 生命周期（共享 ToolRegistry、独占 owner、失败回滚、分页发现后白名单注册、connect/aclose 归属调用方）。

---

## Phase 1 第 2 项：MCP 生命周期（§三表格第 2 项）— 已完成，待用户确认

日期：2026-11-24
状态：共享生命周期服务 + 分页发现 + 白名单单事务登记完成；全量 pytest 650 passed / 24 skipped，ruff 全绿。

### 三项最高约束遵守情况
1. 单机安装：`McpToolRegistry` 只做**进程内**共享门与快照，不新增租户矩阵、不做跨租户隔离；owner key 含 tenant_id/connector_id，值来自当前会话。
2. 内核 Port 冻结：`PORT_SPECS` 未动；`mcp.discoverable` 仅窄扩展 `list_tools(cursor=None)`；生命周期能力是 `app/builtin_plugins/` 领域服务，经 `sdk.publish("mcp_lifecycle")` → `app.state` 暴露，API 用字面量 key 反向解耦。
3. exe 打包：无新依赖、无新表/迁移、无外部服务；分页上限 `max_pages<=10` 防止游标异常导致无限请求。

### 改动文件清单
新增：
- `app/builtin_plugins/mcp_lifecycle.py`：`McpToolRegistry`（进程内 owner lease + 工具名快照）与 `McpLifecycleService`（统一分页发现入口）。
- `tests/integration/test_phase1_mcp_lifecycle.py`：5 条真实入口验收。

修改：
- `app/kernel/ports/connector.py` / `app/builtin_plugins/mcp.py`：`list_tools(cursor=None)` 窄扩展，HTTP `tools/list` 传 `cursor`。
- `app/kernel/skill_gateway.py`：新增 `discover_mcp_http()`：一次 negotiate、按游标翻页、创建方 finally close，`max_pages` 上限。
- `app/builtin_plugins/plugins.py`：`McpConnectorPlugin` 发布共享 `mcp_lifecycle` 服务；绑定 `sdk.shared(SKILL_GATEWAY_KEY)`。
- `app/api/routes/connectors.py`：抽出 `_mcp_probe_options`；新增 owner 门、分页发现兼容单页旧路径、`POST /{connector_id}/tools/register-from-discovery` 白名单单事务登记与 detached 失败审计。
- `app/schemas/connector.py`：`McpDiscoverRequest` / `McpRegisterFromDiscoveryRequest`。
- `app/main.py`：capabilities 增加 `mcp.lifecycle/page_discovery/whitelist_registration/transactional_registration/tool_registry/active_owners`；lifespan 退出时 `aclose_mcp_sessions()` + 清理生命周期注册表。
- `app/core/errors.py` / `app/kernel/audit/categories.py`：新增 `AI_MCP_OWNER_CONFLICT` / `AI_MCP_LIFECYCLE_UNAVAILABLE` / `AI_MCP_TOOL_REGISTER_FAILED` 及 `MCP_OWNER_CONFLICT` / `MCP_TOOL_REGISTER_FAILED` 审计类别。
- `tests/conftest.py`：测试装配 fixture 同步 `result.objects` → `app.state`（与 main.py lifespan 一致）。
- `tests/unit/test_plugin_error_codes.py`：同步错误码契约集合。

### diff 摘要
- 共享 `ToolRegistry`：进程内按 `(tenant_id, connector_id)` 记录 owner lease 与已登记工具名；DB 仍是工具登记真实来源。
- 独占 owner：发现/白名单登记前 `acquire_owner`，同 connector 不同 owner 直接 409 `AI_MCP_OWNER_CONFLICT` + detached 审计；同 owner 可重入，结束/异常均释放。
- 分页发现：显式 `cursor`/`max_pages>1` 时走 `McpLifecycleService → SkillGateway.discover_mcp_http`，单连接 negotiate 一次、逐页 `tools/list`、`finally close`；旧单页 `/discover` 路径完全保留。
- 白名单登记：新入口 `POST /tools/register-from-discovery` 从远端重新分页发现，仅登记 `allowed_names`，所有工具在同一请求事务内创建；任一步失败 `db.rollback()` + detached `MCP_TOOL_REGISTER_FAILED`，不留半成品。
- 模型可见/热更新：登记后 `build_tool_mappings` 能取到 `ToolVersion.input_schema`；`tools/sync` 生成新发布版本并切换 `Tool.current_published_version_id`，新绑定/模型工具目录无需重启即见新 schema；**既有绑定 pin 旧版本的语义不变**（路线图未要求自动改绑定）。
- connect/aclose 归属：`discover_mcp_http()` 谁创建谁关闭；执行期缓存会话由 `main.py` lifespan 统一 `aclose_mcp_sessions()`，不再泄漏到进程退出。

### 真实入口验收结果（5/5 通过）
1. `GET /api/z-torque/capabilities`：`mcp.lifecycle=AVAILABLE`、`page_discovery=true`、`whitelist_registration=true`、`transactional_registration=true`。
2. `POST /discover` 带 `max_pages=2`：一次连接、一次 negotiate、`list_tools` 游标 `[None,"p2"]`、返回 3 个工具与 `next_cursor="p3"`、`close_count=1`。
3. `POST /tools/register-from-discovery` 白名单仅 `search`：只登记 `search`；`build_tool_mappings` 可见；`tools/sync` 后 v2 schema 被新绑定/模型工具目录看到。
4. owner 冲突：先占用 `holder`，API 以 `intruder` 调 `/discover` → 409 `AI_MCP_OWNER_CONFLICT` + detached 审计。
5. 回滚：批量登记第二个工具注入异常 → 500 `AI_MCP_TOOL_REGISTER_FAILED`，测试租户下 `zr_tool` 无残留，detached 审计存在，远端连接仍 close 一次。

### 测试结果
- 新增测试：`5 passed`。
- 全量 `pytest -q`：**650 passed, 24 skipped**。
- 项目级 `ruff check .`：**All checks passed**。

### 关键设计决定（后续项勿踩坑）
- 单页 `/discover` 仍走旧 `_probe_mcp`，保护既有 monkeypatch 扩展点与回归用例；只有显式分页参数才切到共享生命周期服务。
- owner 是**进程内 lease**（默认 60s TTL），不是 DB 租约；单机 exe 下防同进程并发/残留，重启后自然清空，不做多副本抢占。
- 白名单登记不新增唯一索引，靠应用层 duplicate check + 单事务；若将来放开多进程并行，再考虑 `(tenant_id, connector_id, external_name)` 唯一索引。
- `mcp.stdio_discovery` 仍为 false（Stdio 无 `list_tools`），不要伪报；第 2 项只接 HTTP 分页发现。

### 下一步（须用户确认后再做）
Phase 1 第 3 项：审计字段收紧 + usage 归一化（§三表格第 3 项）。

---

## Phase 1 第 3 项：审计字段收紧 + usage 归一化（§三表格第 3 项）— 已完成，待用户确认

日期：2026-11-24
状态：审计 detail 写入侧 metadata-only 收口 + usage 未知语义归一化 + `zr_usage_summary` 落表完成；全量 pytest 654 passed / 24 skipped，ruff 全绿，迁移一致性 2 passed / 1 skipped（PG 校验需外部 URL）。

### 三项最高约束遵守情况
1. 单机安装：`zr_usage_summary` 按 `tenant_id` 过滤，新增唯一索引 `(tenant_id, agent_id, model, window)`；无租户矩阵、无跨租户查询。
2. 内核 Port 冻结：`PORT_SPECS` 未动；`ModelUsage` 只在既有 model gateway 契约上窄扩展；usage 汇总服务放 `app/services/`，API 只读入口放 `app/api/routes/usage.py`。
3. exe 打包：无新依赖；新增一张 SQLite/PG 兼容表与迁移；无外部服务。

### 改动文件清单
新增：
- `app/kernel/audit/metadata.py`：审计 detail 白名单归一化（`normalize_audit_detail`）。
- `app/models/usage_summary.py`：`UsageSummary` ORM（`zr_usage_summary`，NULL = 未知）。
- `app/services/usage_service.py`：usage 归一化写入/序列化服务。
- `app/api/routes/usage.py`：`GET /api/z-torque/usage/summary` 只读入口。
- `alembic/versions/e6f1a7b3c9d2_usage_summary.py`：新迁移，down_revision=`d7e9f2a4b6c8`，当前 head 改为 `e6f1a7b3c9d2`。
- `tests/integration/test_phase1_audit_usage.py`：4 条真实入口验收。

修改：
- `app/kernel/model_gateway/contracts.py`：`ModelUsage` 增加 cache 字段与 `*_known` 标记；新增 `parse_model_usage()` / `normalize_usage()` / `UNKNOWN_TOKEN_VALUE`。
- `app/kernel/model_gateway/__init__.py`：导出 usage 归一化契约。
- `app/builtin_plugins/model_gateway.py`：OpenAI 响应解析统一改走 `parse_model_usage()`，缺失 usage 不再默认 0。
- `app/kernel/audit/service.py`：所有 `record_audit` 写入统一经过 metadata-only 归一化。
- `app/services/stream_service.py`：流式模型调用写 usage 汇总；审计 usage 改归一化结构。
- `app/kernel/task/runtime.py`：任务链路同样写 usage 汇总；`task.result.usage` 与审计 usage 归一化。
- `app/api/router.py`：挂载 usage 路由。
- `app/main.py`：capabilities 增加 `usage.summary=true`、`usage.unknown_token_value="unknown"`。
- `app/models/__init__.py`：注册 `UsageSummary` 到 Base.metadata。

### diff 摘要
- 审计 detail 在**写入侧单点收口**：白名单键 + 安全后缀 + 深度/长度/条目数上限；`prompt/messages/content/arguments/api_key/before_text/after_text/query/payload` 等正文/凭据键一律丢弃；usage 独立归一化。
- token 语义：只有上游真实返回且为非负整数才标记 known；未报告字段审计写 `"unknown"`，DB 汇总列写 `NULL`；`0` 只代表真实 0。
- 新增 `zr_usage_summary(tenant_id, agent_id, model, window, token_in, token_out, cache_read, cache_write)`；未知字段聚合时会把该窗口对应列污染为 NULL，避免部分未知被误算成 0。
- 模型调用两处真实入口（chat 流式 + task runtime）均写 usage 汇总；`GET /usage/summary` 只读返回元数据，未知统一显示 `"unknown"`。
- 既有审计 `query_hash`、`code`、`permission`、`chunk_ids` 等元数据键保留，沙箱拒绝审计断言未破坏。

### 真实入口验收结果（4/4 通过）
1. `parse_model_usage`/`normalize_usage`：已知 token 保留数值；缺失 cache 与空 usage 全部 `"unknown"`，无 0。
2. `OpenAICompatibleGateway._normalize()` 无 usage 响应：`normalize_usage()` 全字段 `"unknown"`。
3. `record_audit` → `GET /audit/by-trace/{trace}`：正文/凭据/diff 原文字段全部消失；`usage.prompt_tokens`、`usage.cache_read_tokens` 为 `"unknown"`；`query_hash` 等元数据保留。
4. `record_usage_summary` → `GET /usage/summary`：已知模型返回 10/5，未知模型返回 `"unknown"`；DB 中未知模型 `token_in IS NULL`。

### 测试结果
- 新增测试：`4 passed`。
- 全量 `pytest -q`：**654 passed, 24 skipped**。
- 迁移门禁 `pytest tests/integration/test_alembic_migrations.py -q`：**2 passed, 1 skipped**（PG 外部 URL 未设置）。
- 项目级 `ruff check .`：**All checks passed**。

### 关键设计决定（后续项勿踩坑）
- 审计收紧放在 `record_audit` 一处，不逐个调用点改；detached 审计复用同一路径，拒绝路径也不会漏。
- 不用 `token_in/token_out` 作为审计 JSON 键名：现有 `app/core/redaction.py` 会把含 `token` 的键视为敏感；归一化出口使用 `prompt_tokens/completion_tokens/cache_*_tokens`。
- `zr_usage_summary` 的 NULL 代表「上游未报告」，不是「没调用」；后续 token 图/成本统计必须按 unknown 单独分桶。
- 旧 `task.result.usage` 由 `__dict__` 改为归一化 dict；前端若按 `prompt_tokens` 读取仍兼容，但未报告字段会拿到字符串 `"unknown"`。
- 新 head：`e6f1a7b3c9d2`；后续迁移 down_revision 用它。

### 下一步（须用户确认后再做）
Phase 1 第 4 项：工具选择器 + depends_on + orchestration_hint（§三表格第 4 项，依赖 §2.6）。

---

## Phase 1 剩余全部项（P1a-4~9 + P1b-1~6）— 已完成

日期：2026-11-24
状态：Phase 1 全部收口；全量 pytest 662 passed / 24 skipped，ruff 全绿，`npm run build` 成功。

### 三项最高约束遵守情况
1. 单机安装：所有新增状态均为本机 JSON/SQLite/PG 兼容字段；无租户矩阵、无跨租户查询。
2. 内核 Port 冻结：`PORT_SPECS` 未动；工具治理、记忆整合、token 估算都走既有 Port 或 builtin 领域服务。
3. exe 打包：无新 Python/Node 依赖；前端 highlight/diff 用内置轻量实现，不引 shiki/diff/motion；新增一张绑定列迁移，无外部服务。

### P1a 后端完成内容
4. **工具选择器 + depends_on + orchestration_hint**
   - `zr_skill_version_tool` 新增 `selector` / `depends_on` JSON 列；迁移 `b1c2d3e4f5a6`（down_revision=`e6f1a7b3c9d2`）。
   - 一键发布/单条绑定/更新绑定 API 保存并校验依赖（含环检测）；发布快照包含新字段。
   - 提示注入按 `WORKSPACE → USER → AGENT` 顺序输出结构化选择条件与依赖。
   - 工具目录最终仍按名排序，保证稳定前缀；仅调用提示按依赖优先。
5. **工具错误回喂**
   - function calling 改用 JSON Schema Draft 2020-12 严格校验。
   - 错误带 `json_path`、期望建议 `did_you_mean`、结构化 `self_correction`，最多一次修复回合。
   - MCP 原始 `inputSchema` 固化进 `config_snapshot._mcp_raw_input_schema`，同步时更新。
   - 新审计：`TOOL_ARGUMENT_INVALID` / `TOOL_CALL_REPAIRED` / `TOOL_CALL_REPAIR_FAILED`。
6. **子代理上下文继承**
   - 子代理调用继续继承父会话、用户与子智能体三级长期记忆；复用 `memory.long` Port 的 `recall_bundle`。
   - 记忆块统一标记 untrusted 并走 `sanitize_untrusted`。
7. **Skills 三级注入 + 硬预算**
   - `agent_version.config_snapshot.skill_inject_scopes` 可开启 WORKSPACE/USER/AGENT 三级注入。
   - 新增 `skill_inject_max_chars=6000` / `skill_inject_max_items=12`，按级别与 ID 稳定排序后截断。
8. **Dream 式整合**
   - 新增 `POST /api/z-torque/memory/consolidate`。
   - 复用 `zr_memory_long` 存游标（key=`__dream:cursor:*`）；重复事实原位替换（版本链保留旧值）、重复项 soft-delete、diff 写入事实信封。
   - `MEMORY_CONSOLIDATED` 审计记录 `scanned/conflicts_removed/merged`；游标保证重复调用幂等。
9. **token 精确计量（轻量方案）**
   - 不引入 tokenizer 依赖；新增 `MODEL_CHARS_PER_TOKEN` 模型族系数表与 `resolve_chars_per_token()`。
   - context builder 压缩预算按模型族选择系数；capabilities 上报 `estimator=model_chars_per_token`、`tokenizer_dependency=false`。

### P1b 前端完成内容
1. **文件 diff 与代码高亮**
   - 新增 `Phase1Panels.tsx`：从最近助手消息提取 fenced code block，LCS 行级 diff，颜色区分 add/remove/same，Drawer 承载；未引入新 npm 依赖。
2. **Agent 活动可视化**
   - 复核既有 `chatRun` 时间线与 Think 折叠渲染已满足出口，无额外改动。
3. **多会话分栏**
   - `PaneResizer` 支持键盘方向键 resize（tabIndex + aria）。
   - Chat 头部新增分栏开关；`SplitConversationPanel` 支持选择另一会话并只读展示其最近消息。
4. **Composer 增强**
   - 输入区支持附件拖拽上传（最多 3 个文件）。
   - 新增模型预设 Select、`@` 提及候选人插入、排队发送与队列徽标；队列在当前流式结束后自动按序发送。
5. **上下文/token 用量图**
   - `CapabilityPanel` Drawer 调 `/usage/summary`，用纯 CSS 双色柱状条显示输入/输出 token；未知值显示 `unknown`。
6. **能力三态展示**
   - capabilities 映射为 AVAILABLE/DEGRADED/UNAVAILABLE 标签；Chat 头部与 Drawer 同步展示。

### 关键新增/修改文件（本轮）
新增：
- `alembic/versions/b1c2d3e4f5a6_tool_binding_selector.py`
- `app/api/routes/memory.py`
- `app/api/routes/usage.py`
- `app/services/memory_consolidation.py`
- `app/services/usage_service.py`
- `app/kernel/audit/metadata.py`
- `app/models/usage_summary.py`
- `console/src/components/Phase1Panels.tsx`
- `tests/integration/test_phase1_audit_usage.py`
- `tests/integration/test_phase1_memory_consolidation.py`
- `tests/integration/test_phase1_mcp_lifecycle.py`
- `tests/integration/test_phase1_mcp_execution.py`
- `tests/integration/test_phase1_tool_selector.py`
- `tests/unit/test_p1a_child_memory.py`
- `tests/unit/test_p1a_skill_budget.py`
- `tests/unit/test_p1a_token_estimate.py`
- `tests/unit/test_p1a_tool_repair.py`

修改（核心）：
- `app/api/routes/connectors.py` / `app/api/routes/skills_tools.py` / `app/api/router.py`
- `app/builtin_plugins/model_gateway.py` / `app/config.py` / `app/main.py`
- `app/kernel/audit/{service,categories}.py` / `app/kernel/model_gateway/{contracts,__init__}.py`
- `app/kernel/skill_service.py` / `app/kernel/task/runtime.py`
- `app/models/{__init__,tool_skill}.py`
- `app/services/{context_builder,context_governance,execution_loop,function_calling,stream_service,tool_mapping_builder}.py`
- `console/src/pages/Chat.tsx` / `console/src/components/PaneResizer.tsx` / `console/src/index.css`
- `tests/conftest.py` / `tests/unit/test_plugin_error_codes.py`

### 真实入口与验收结果
- 工具绑定 API：selector/depends_on 可保存并回显；提示中出现依赖与选择条件。
- function calling：错误回喂测试验证一次修复后成功，审计顺序为 `TOOL_ARGUMENT_INVALID → TOOL_CALL_REPAIRED`。
- 子代理：三级记忆继承测试验证 user/agent/conversation 口径与 untrusted 清洗。
- 技能预算：WORKSPACE/USER/AGENT 三级注入顺序与总长度受控。
- 记忆整合 API：重复事实从 2 条收敛为 1 ACTIVE + 1 DELETED，diff 信封与 MEMORY_CONSOLIDATED 审计存在，二次调用幂等。
- token 估算：capabilities 上报轻量 estimator，无 tokenizer 依赖。
- 前端：`npm run build` 成功（tsc + vite）；Chat 头部可见能力/用量/Diff/分栏，Composer 增强可用。

### 测试与构建
- 全量 `pytest -q`：**662 passed, 24 skipped**。
- `ruff check .`：**All checks passed**。
- alembic 迁移门禁：全量测试内通过（SQLite 升级 + 元数据一致性）。
- 前端 `npm run build`：**成功**（仅有 Chat chunk >500kB 的 Vite 体积提示，非错误）。

### 关键设计决定（后续项勿踩坑）
- 工具目录稳定前缀仍按名排序；依赖只影响调用提示顺序，不破坏既有 function name 前缀。
- 工具参数校验使用 Draft 2020-12；修复回合最多一次，二次失败直接抛 `AI_FUNCTION_CALL_INVALID`。
- 未报告的 token 一律 `unknown` / NULL；前端用量图未按 0 绘制。
- Dream 游标复用 long memory 表，不新增表；cursor 事实本身通过 `source_ref=dream:consolidator` 排除出普通召回。
- 前端不引入 shiki/diff/motion；轻量实现满足 Drawer/键盘/排队/三态/用量图出口，避免 exe 体积与 npm 新依赖。
- 当前 alembic head：`b1c2d3e4f5a6`。

### Phase 1 状态（2026-11-24 更正）
**Phase 1 工程实现完成（P1a-1~9 + P1b-1~6 技术入口），但 P1b UX 返工尚未完成，不能说「Phase 1 真正完成」，暂不进入 Phase 2。**
P1b 技术入口的用户体验不合格，现按 P1b-UX-1~6 逐项返工，每项完成后须经真实浏览器入口验收并等待用户确认。当前进度：UX-1 已完成，UX-2~6 待做。

---

## P1b-UX-1：能力/用量头部横向自然语言化 — 已完成，等待用户确认

日期：2026-11-24
状态：头部由「按钮 + 抽屉 + 专业状态标签」改为「横向自然语言能力条 + 用量」，真实浏览器验收通过；未改后端。

### 三项最高约束遵守情况
1. 单机安装：纯前端展示改造，无租户逻辑。
2. 内核 Port 冻结：未动 `PORT_SPECS`；未新增 Port。
3. exe 打包：未新增任何 Python/Node 依赖；只改 TSX/CSS，`package.json` 未动。

### 本任务做了什么（大白话）
Chat 头部现在直接写着「外部工具 可用 / 知识检索 可用 / 模型 可用」和「今日用量：输入 36.4k / 输出 1.3k」，不用点开抽屉。点任意一项会弹出说明：现在是什么状态、如果不可用是什么原因、可以去点什么按钮解决。不可用时显示「未连接外部工具服务 / 知识库未初始化 / 未配置模型」，并给出「去配置连接器 / 重新测试连接 / 去上传资料 / 重建索引 / 去模型管理」。窄屏时文字仍完整，用量自动换到第二行；接口失败不再永久显示「加载中」，改为「暂时无法获取 + 重试」。

### 改动文件清单
修改：
- `console/src/components/Phase1Panels.tsx`：删除抽屉式 `CapabilityPanel`，新增 `CapabilityStrip`（自然语言三态、今日用量、原因与动作弹出层、失败重试）。
- `console/src/pages/Chat.tsx`：头部移除 `<CapabilityPanel />`，新增 `<CapabilityStrip onUpload=... />`；隐藏附件 input 加 ref 供「去上传资料」复用。
- `console/src/index.css`：删除旧 `.phase1-usage-bars` 等样式，新增 `.chat-cap-*` 横条/弹出层样式和窄屏媒体查询。

### diff 摘要
- 状态文案映射：`mcp.execution`→「外部工具」、`knowledge.local.search`→「知识检索」、`model.configured`→「模型」；`AVAILABLE/DEGRADED/UNAVAILABLE`→「可用/受限/不可用」。
- 原因与动作完全由前端按三项状态确定性派生（不裸露 MCP/token/unknown）；后端 `/capabilities` 无需扩展 reason/action 字段，故无后端改动、无需迁移。
- 用量只读当天窗口行，数字 ≥1000 缩写为 `k`，未报告显示「未上报」，不再出现 token 字样与柱状条。
- 「去配置连接器」→ `/connectors`，「去模型管理」→ `/models`；「去上传资料」复用聊天输入框附件选择器；「重新测试连接」重新拉取 capabilities；「重建索引」`POST /api/z-torque/knowledge/reindex`。

### 真实入口验收结果（浏览器实际点击）
- 启动前端：Vite dev server `http://127.0.0.1:5174`（后端已运行于 8000，CORS 只允许 5174）。用 `admin/admin123` 登录后进入 `/chat`。
- 真实状态点击：头部直接看到「外部工具 可用 / 知识检索 可用 / 模型 可用」「今日用量：输入 36.4k / 输出 1.3k」；点「外部工具」弹出「外部工具：可用 / 外部工具让助手在对话里查询或操作外部系统。/ 去配置连接器」。
- 构造不可用（playwright 拦截 `/capabilities` 返回 UNAVAILABLE + 未配置模型，`/usage/summary` 返回空）：三项均显示「不可用」，用量显示「未上报」；逐项点击确认：
  - 外部工具：原因「未连接外部工具服务」+ [去配置连接器] [重新测试连接]
  - 知识检索：原因「知识库未初始化」+ [去上传资料] [重建索引]
  - 模型：原因「未配置模型」+ [去模型管理]
- 构造受限：`DEGRADED` 显示「受限」，原因「本地检索已降级，结果可能不完整」+ [去上传资料] [重建索引]。
- 动作真实可用：点「去模型管理」实际跳到 `/models`；点「重新测试连接」后模拟响应改为可用/受限，头部随之刷新为「外部工具 可用 / 知识检索 受限 / 模型 可用」；点「重建索引」真实调用后端并返回「索引重建已提交」；点「去上传资料」真实触发文件选择器（Playwright 捕获到 filechooser 事件）。
- 窄屏：760px 与 480px 均截图复核，文字保持完整、480px 下用量换到第二行，未退化成纯图标。
- 页面快照全程 grep 无 `MCP`/`AVAILABLE`/`token`/`unknown` 裸术语。
- 截图（可复核）：`.screenshots/ux1-real.png`、`.screenshots/ux1-unavailable.png`、`.screenshots/ux1-narrow.png`、`.screenshots/ux1-mobile.png`。
- 验收中发现并修复：能力接口失败时原实现永久停在「能力状态加载中…」，现改为「能力状态暂时无法获取 + 重试」。

### 测试结果
- 全量 `pytest -q`：**662 passed, 24 skipped**（与基线一致）。
- 项目级 `ruff check .`：**All checks passed**。
- `npm run build`：**成功**（tsc + vite；仅 Chat chunk >500kB 的 Vite 体积提示，非错误）。
- 后端未改：无迁移，alembic head 仍为 `b1c2d3e4f5a6`。

### 后续更新（2026-11-24，新需求清单 docs/question.md 覆盖上一版做法）
用户提供了新清单并要求一次做完六项、跳过浏览器验证（用户自行验收）。以下为最终状态，覆盖上面 UX-1 的「前端自行编原因」做法。

## Phase 1 / P1b UX 返工总记录（六项全部完成）

### 三项最高约束
1. 单机安装：无租户矩阵/跨租户逻辑。
2. 内核 Port 冻结：未新增 Port；站内 A2A 派发放 `app/services` + builtin plugin 复用，生成预设放 `app/services`。
3. exe 打包：未新增任何 Python/Node 依赖，`package.json` 未动。

### 任务1 能力/用量头部（按新清单重做：原因与动作由后端提供）
- 先写 `tests/integration/test_capability_cards.py`（5 项）失败用例，再实现。
- 新增 `app/services/capability_presentation.py`：`build_capability_cards` 按真实装配推导三张卡片 `state/label/reason_code/reason/actions`；`/capabilities` 新增 `capability_cards`，旧字段全部保留（向后兼容）。
- `Phase1Panels.tsx` 的 `CapabilityStrip` 改为消费后端 reason/action，前端不再编造原因；动作按 `route`/`action_code` 分发；接口失败显示「暂时无法获取 + 重试」。

### 任务2 对比上一版
- 删除 `CodeDiffDrawer`、`codeBlocks`、`lcsLines` 与代码 diff 样式；新增 `VersionCompareDrawer`，比较同一会话最近两次助手文本，展示「本次新增/本次删减/未变化」，无两次输出时按钮禁用并提示「暂无可对比的上一版」；无 Diff/LCS/hunk 等术语。

### 任务3 真正左右双会话
- 删除底部只读 `SplitConversationPanel`；新增 `console/src/components/ConversationPane.tsx`，右栏可选会话、独立消息/输入/发送/滚动，通过 `POST /chat` 显式 `conversation_id` 独立发送。
- 左栏保留原 Chat 状态；`PaneResizer invert` + `usePaneWidth("chat-split","--chat-split-right-w")` 控制右栏宽度；关闭分栏恢复单栏。
- 先补 `tests/integration/test_dual_conversation.py`（2 项）证明后端按 conversation_id 隔离，再实现前端。

### 任务4 @提及语义（用户确认的完整站内 A2A 架构）
用户确认：取消会话页选择执行智能体；默认系统智能体自动派发需求；@指定智能体则直接调用它；闲聊不建 Task；审计归属实际处理的智能体。
- 先补 `tests/unit/test_in_site_a2a.py`（2 项）与 `tests/integration/test_mention_dispatch.py`（3 项）失败用例，再实现。
- `ToolMapping` 新增 `dispatch_agent_version_id`；系统智能体（用户标记为默认 `is_default`）自动获得 `call_agent_xxx` 派发工具，目标为全部已发布智能体；`execution_loop` 把派发工具复用 `_run_child_agent` 子执行链路，审计/Task 归属目标智能体。
- `ChatRequest.mentions` + `resolve_agent_mentions`：@智能体直接把任务建在被提及智能体的已发布版本上；未知/跨租户/未发布目标显式 404。
- `create_task` 增加可选 `allow_conversation_agent_mismatch`，仅在显式 @派发时允许任务智能体与会话绑定智能体不同，默认行为完全不变。
- 前端删除会话页智能体选择器，默认选 `is_default` 系统智能体；@入口插入 `@[agent:<id>]` 并明确「将调用该智能体处理本条需求」；发送时解析 mentions 随请求提交。旧父子委派字段保留兼容，不删除。

### 任务5 模型预设用户化
- 先补 `test_generation_preset_actually_changes_model_request`（记录网关请求）失败用例，再实现。
- 新增 `app/services/generation_presets.py`：`concise/balanced/deep/creative` → 真实 temperature/max_tokens；`ChatRequest.generation_preset` 白名单校验；`/chat` 与 `/chat/stream` 均叠加到版本快照/全局默认之上。
- 前端预设改为「简洁准确/平衡/深度分析/创意发散」，每项有解释；不出现 temperature/top_p/max_tokens/model id。

### 任务6 排队发送融入主发送按钮
- 删除独立「排队发送」按钮；忙碌时主发送按钮把输入加入队列、清空输入框并提示「已加入队列，将在当前回答完成后自动发送」。
- 头部显示「排队中：N」，输入框上方显示逐条队列与「取消」；当前回答结束后 FIFO 自动发送；停止入口降级为头部小按钮。
- 队列仅页面状态，刷新不保留（界面已明确提示）。

### 最终验收结果
- `pytest -q`：**674 passed, 24 skipped**（基线 662 passed / 24 skipped，新增 12 项）。
- `ruff check .`：**All checks passed**（2 处导入顺序已自动修复）。
- `npm run build`：**成功**（tsc + vite，仅 Chat chunk >500kB 体积提示，非错误）。
- 浏览器验证：按用户要求跳过，由用户自行在前端验收。
- alembic head 未变：`b1c2d3e4f5a6`，无新增迁移。

## 用户浏览器实测反馈修复（2026-11-24 第二轮）

### 问题1：分栏会话输入后看不到回复
根因：`ConversationPane` 用非流式 `POST /chat` + 一次性重载消息，任务异步执行时回复尚未落库。
修复：完整复刻主页面实时链路——`openPostStream("/chat/stream")` 处理 `message.delta/result/task_created/error`；`openGetStream("/tasks/{id}/stream")` 订阅任务步骤与 `task.done`；`RunBlock` 展示执行过程、打字机输出结论；2.5s 轮询任务状态兜底；支持上传附件、停止、Markdown 渲染。修改：`console/src/components/ConversationPane.tsx`、`console/src/pages/Chat.tsx`（传 generationPreset）、`console/src/index.css`。

### 问题2：对比改为「文件对比」
用户明确：对比对象是从会话产出的文件 vs 用户自己上传的文件，不是两版回答文本。
修复：
- 后端新增 `GET /api/z-torque/files?conversation_id=`：按会话列出本人文件（`app/api/routes/files.py`）。
- 前端 `VersionCompareDrawer` 重做为 `FileCompareDrawer`：侧栏 Drawer 选择当前会话文件 + 上传对比文件 → 读 `/files/{id}/content` → 复用 `compareText` 展示「本次新增/删减/未变化」。按钮名「文件对比」。修改：`Phase1Panels.tsx`、`Chat.tsx`、`index.css`。

### 问题3：站内 A2A 前端测试不能派发
根因三条：(1) 派发工具只对 `is_default` 智能体生效，但开通向导创建的智能体未设默认；(2) 系统提示未告知模型有 `call_agent_*` 工具可用；(3) 用户不知道要在智能体页设置。
修复：
- `app/api/routes/onboarding.py`：向导创建的首个助手自动 `is_default=True`（即系统智能体）。
- `app/services/context_builder.py`：系统智能体系统提示注入「[智能体协作]」段落，要求有专业能力需求时调用 `call_agent_*` 工具，仅闲聊直答。
- `console/src/pages/Agents.tsx`：「设为默认」改名为「设为系统智能体」，提示语说明派发语义。
- `console/src/pages/Chat.tsx`：系统智能体未发布/无其他已发布智能体时给出明确提示。
- 增加回归测试：未显式设默认的智能体保持原有闲聊行为（不注入派发工具）。

### 本轮验收
- `pytest -q`：675 passed, 24 skipped（含新增 1 项回归测试）。
- `ruff check .`：All checks passed。
- `npm run build`：成功（仅 Vite 体积提示）。
- 浏览器验证：用户自行验收（按用户要求跳过自动化浏览器验证）。

## 系统自带智能体重构（2026-11-24 第三轮，覆盖第二轮问题3的做法）

用户明确：系统智能体是系统自带的——第一次打开系统配置好模型就能用，不需要用户创建/标记；用户只在系统里创建专业智能体，系统智能体自动识别并派发。

### 最终架构
- 系统自带智能体 = `is_default_parent`（平台内置、租户级唯一、启动时 bootstrap 并已发布）。
- 用户 `is_default` 回归原义（个人默认智能体，外部接入用），**不获得**跨智能体调度权限。
- `app/kernel/agent/service.py` 新增 `ensure_system_agent()`：以 `Z-Torque 系统智能体` 名称 + router 系统提示创建/复用默认父；启动 lifespan 对每个租户调用；`GET /agents`、`POST /conversations`、`onboarding` 懒调用兜底（启动后新建的租户首次打开即可用）。
- `tool_mapping_builder.py` / `context_builder.py`：派发工具与「[智能体协作]」系统提示只对 `is_default_parent` 注入（`is_default` 不再触发）。
- `onboarding.py`：只配置模型 + 创建用户助手（普通智能体，不再设 `is_default`）；会话绑定到系统自带智能体（幂等路径同样处理）。
- 前端 `Chat.tsx`：默认智能体优先选 `is_default_parent`；`Agents.tsx`「设为默认」恢复原义。
- 测试：`test_in_site_a2a.py` 重写（系统自带智能体暴露派发工具、用户默认智能体不暴露、ensure 幂等）；`test_onboarding_wizard.py` 断言 2 个智能体且会话绑定系统智能体；`test_mention_dispatch.py` 默认父 `/chat` 非流式创建 TASK。

### 验收
- `pytest -q`：676 passed, 24 skipped。
- `ruff check .`：All checks passed。
- `npx tsc --noEmit` / `npm run build`：通过（仅 Vite 体积提示）。

## 用户实测反馈修复（2026-11-24 第四轮）：新建会话绑定错误 + 派发无能力

### 现象
1. 前端新建会话绑定的是「企业微信…」智能体，不是系统自带智能体。
2. 发送需求回复「当前没有解决这个需求的功能」。

### 根因
- 前端 `selectConversation()` 把 `agentId` 改成被点击会话的 binding；「新建会话」和「首条消息建会话」都用这个陈旧 `agentId` 作为 `agent_id` 传给后端，于是继承了企业微信智能体（真库里多条新会话确实绑定到它）。
- 企业微信智能体不是系统自带智能体，没有 `call_agent_*` 派发工具 → 模型回复「没有解决这个需求的功能」。
- 旧安装的默认父快照是早期 `{"mode":"mock"}` 空壳，`ensure_system_agent` 早退未升级。

### 修复
- `Chat.tsx`：新增 `systemAgentId` 状态（加载时取 `is_default_parent`），与当前查看会话的 `agentId` 解耦；`newConversation()` 与首条消息建会话都改为 `POST /conversations` 不传 `agent_id`，让后端用 `is_default_parent` 绑定系统智能体。
- 两条发送 body（`/chat` 附件链路、`/chat/stream` 主链路）移除 `agent_id`，避免与会话绑定智能体冲突 409，也避免误用陈旧智能体；mentions 仍走显式派发。
- `app/kernel/agent/service.py`：`ensure_system_agent` 就地升级旧默认父（名称、描述、已发布版本快照改为 `mode=router` + router 系统提示），不新建版本。
- 回归测试：`test_ensure_system_agent_migrates_legacy_mock_snapshot`。

### 验收
- `pytest -q`：677 passed, 24 skipped。
- `ruff check .`：All checks passed。
- `npx tsc --noEmit` / `npm run build`：通过。
- 真库已就地升级：两个租户的默认父快照均为 `router`；主租户派发工具 20 个。

## 阶段状态（最终）
- Phase 1 工程实现完成（P1a-1~9 + P1b-1~6）
- P1b UX 返工完成（六项全部实现，按 docs/question.md 口径）
- 用户实测三项反馈已修复（分栏实时链路 / 文件对比 / 站内 A2A 开箱可用）
- 真实用户可用验收：**待用户浏览器确认**（自动化测试与构建已全绿）
- 可以进入 Phase 2（用户浏览器验收通过后）

---

# Phase 2 实施记忆

## 阶段一：manifest-first 校验 + capability ladder（§四第 1 项）— 已完成，待用户确认

日期：2026-11-24
状态：负向用例先行，门禁 + 四级阶梯 + capabilities 上报完成；全量 pytest 684 passed / 24 skipped（基线 677 + 7 条新增），`ruff check .` 全绿。

### 三项最高约束遵守情况
1. 单机安装：能力声明是进程内装配期治理，无租户矩阵、无跨租户查询。
2. 内核 21 Port 冻结：`PORT_SPECS` / `assert len(PORT_SPECS)==21` 未动；新增 `CapabilityDeclaration` 是 `app.plugin_sdk` 公开契约类型，**不是新 Port**；治理摘要随 `AssemblyResult` 回收。
3. exe 打包：无新依赖、无新表/迁移、无外部服务。

### 改动文件清单
新增：
- `app/plugin_sdk/capability.py`：`CapabilityLevel` 四级枚举（extend_existing / existing_contract / narrow_extension / core_change）、`CapabilityDeclaration`（core_change 强制 `approval_ref`）、`validate_capability_declarations`、`summarize_governance`。
- `tests/integration/test_phase2_manifest_governance.py`：7 条真实入口验收（5 负向 + 2 正向）。

修改：
- `app/plugin_sdk/contract.py`：`PluginManifest` 新增 `published_services` / `capabilities`；`validate_manifest` 追加能力声明校验；`PluginSdk.__init__` 新增 `enforce_manifest_first`（生产装配传 manifest 时强制，adhoc 直构测试宽松）；`register_port` 必须在 `provides` 声明、`publish` 必须在 `provides` 或 `published_services` 声明，否则 `AssemblyError`。
- `app/plugin_sdk/__init__.py`：导出四级阶梯与汇总函数。
- `app/assembly.py`：`AssemblyResult` 新增 `capability_governance`，装配结束汇总全部插件声明。
- `app/main.py`：lifespan 转挂 `app.state.capability_governance`；`/capabilities` 新增 `governance` 段（旧字段全保留）。
- `app/builtin_plugins/a2a_gateway/plugin.py`、`audit_export_archive/plugin.py`、`plugins.py`（McpConnectorPlugin）、`sandbox_driver/plugin.py`：4 个领域服务插件补齐 `published_services` 与一条能力声明（L1/L2/L3 各覆盖）。
- `tests/conftest.py`：装配 fixture 同步 `capability_governance`（与 main.py lifespan 一致）。
- `tests/unit/test_plugin_discovery.py`：三方插件 manifest 补 `provides=("notify.channel",)`，与新门禁对齐。

### 关键设计决定（后续项勿踩坑）
- **门禁点选在 `register_port` / `publish` 两个执行点**，而非逐扫描源码：这是插件对外提供能力的唯一合法出口，能覆盖内置与 entry point 插件，且不影响 Port 实现体自身的 `publish`（key 等于 `provides` 中的 Port 名自动放行）。
- **adhoc manifest 放行**：测试直构 `PluginSdk(objects={})` 不传 manifest 时 `_enforce_manifest_first=False`，保护 `test_p2_assembly` 等既有测试；生产装配始终传真实 manifest，无绕过路径。
- **core_change 必须带 approval_ref**：装配期 `validate_manifest` 拒绝无审批的核心变更，防止"悄悄加 Port"（21 Port 冻结的制度性护栏）。
- **governance 摘要不暴露绝对路径/凭据**，只含 name → {plugin, level, description, approval_ref} 与四级计数；匿名可读，与既有 capabilities 敏感字段分级一致。
- 生产 22 插件当前只有 4 条能力声明（L1×1 / L2×1 / L3×2，L4=0），这是如实登记而非补全；后续新增能力必须先声明再实现。

### 真实入口验收（7 条，均通过）
1. 负向：`provides=()` 的插件 `register_port("memory.long")` → `AssemblyError`（undeclared port）。
2. 负向：未列 `published_services` 的插件 `publish("mcp_lifecycle")` → `AssemblyError`（undeclared capability）。
3. 正向：声明后的 Port 与领域服务可注册/发布。
4. 负向/正向：`core_change` 无 `approval_ref` 构造即拒绝；带 `ADR-001` 可通过 `validate_manifest`。
5. 负向：未知阶梯层级 / 重复能力名被拒。
6. 生产 22 插件 manifest 全部通过门禁，四级摘要可汇总，`a2a.dispatch` 归属 `a2a_gateway`。
7. 匿名 `GET /api/z-torque/capabilities` → `governance.manifest_first=true`，`mcp.lifecycle=narrow_extension`、`audit.export=existing_contract`，旧字段未丢。

### 测试结果
- `pytest -q`：**684 passed, 24 skipped**（全量回归；新增 7 条）。
- `ruff check .`：**All checks passed**（2 处 import 排序已人工归位）。
- 无迁移，alembic head 仍为 `b1c2d3e4f5a6`。
- 附注：单独按自定义顺序跑 `test_capability_cards → test_p2_robustness` 会暴露一个**存量**测试隔离问题（working memory 快照断言受前序 monkeypatch 影响 + teardown FK 约束失败）；全量按默认顺序运行不复现，与本项无关。

### 下一步
阶段二：repositories 属主过滤收口（§四第 2 项）。当前 `app/repositories/` 为空目录，需先建收口层；重点覆盖 `tool_mapping_builder` / `execution_loop` / `stream_service` / `inbound_webhook` / `webhooks` 等直接 `db.get(id)` 无属主校验的查询；禁止设计跨租户矩阵（单机固定租户），只防跨 agent / 跨用户越权。

---

## 阶段二：repositories 属主过滤收口（§四第 2 项）— 已完成，待用户确认

日期：2026-11-24
状态：负向用例先行，属主收口层 + 3 类真实入口接入完成；全量 pytest 689 passed / 24 skipped（基线 684 + 5 条新增），`ruff check .` 全绿。

### 三项最高约束遵守情况
1. 单机安装：只做同租户内跨用户 / 跨 agent 防护；无跨租户查询矩阵、无租户隔离测试；`tenant_id` 过滤仍在所有查询中保留。
2. 内核 21 Port 冻结：未新增 Port；属主判定是 `app/repositories/` 领域服务，服务层调用，不动内核契约。
3. exe 打包：无新依赖、无新表/迁移、无外部服务。

### 改动文件清单
新增：
- `app/repositories/ownership.py`：`visible_owner_clause`（本人 / OFFICIAL / official / 管理员创建）、`is_visible`、`get_owned`、`get_owned_version`（版本表经父资源判属主）；不可见统一 `ApiError(404)`。
- `app/repositories/__init__.py`：导出收口函数。
- `tests/integration/test_phase2_owner_filter.py`：5 条真实入口验收（含双用户 fixture 与完整私有/OFFICIAL 资源构造）。

修改：
- `app/api/routes/webhooks.py`：`_require_agent` 改用 `get_owned`，禁止 webhook 指向他人私有智能体。
- `app/services/stream_service.py`：新增 `_owned_stream_resources`；流式写入前校验会话属主 + 消息归属该会话 + 版本经 Agent 判属主；错误分支标记 FAILED 也只允许写本人会话消息。
- `app/services/tool_mapping_builder.py`：新增 `user_id` 参数；预取当前用户可见的 Skill/Tool id 集合（2 次查询，避免循环 N+1），过滤他人私有工具。
- 6 处调用点显式传 `user_id`：`chat.py`、`chat_application.py`、`execution_loop.py`（子代理）、`task/runtime.py`（2 处，取 `task.owner_user_id`）。
- `tests/unit/test_stage3_chat.py`：2 个 `fake_builder` 桩补 `user_id` 参数并断言透传。

### 关键设计决定（后续项勿踩坑）
- **可见性口径复用既有语义**：`owner == user OR source_type == OFFICIAL OR official=True OR admin_owned_clause`，与 `resolve_conversation_agent` 完全一致；系统自带智能体是 `source_type=OFFICIAL`、`owner_user_id=NULL`，普通用户仍可正常使用。
- **统一 404 而非 403**：不可见与不存在返回同一个 `AI_RESOURCE_NOT_FOUND`，不泄露资源是否存在。
- **版本表无 owner 列**：`get_owned_version` 先按 tenant 取版本，再按父资源（Agent/Skill/Tool）判定，不新增列。
- **`build_tool_mappings(user_id=None)` 是渐进兼容开关**：仅用于无用户上下文的内核内部调用；所有面向用户的入口必须显式传 `user_id`，负向用例锁定他人私有工具不进入模型工具目录。
- 本轮**未覆盖** `inbound_webhook` 内部多处按 id 直取：入站端点无用户上下文（外部系统按签名调用），其资源由 webhook 绑定关系与 tenant 约束，不套用用户属主判定。

### 真实入口验收（5 条，均通过）
1. 负向/正向：他人私有 Agent 取之 404；本人可取；OFFICIAL Agent 对他人可见。
2. 负向：他人私有 Agent 的 `AgentVersion` 经 `get_owned_version` 拒绝，本人可取。
3. 真实工具目录：A 可见自己发布的私有 Skill/Tool，B 调用 `build_tool_mappings(user_id=B)` 得到空映射。
4. 流式入口：`_owned_stream_resources` 对他人会话 404，本人会话返回会话/消息。
5. API 守卫：`_require_agent` 拒绝 B 指向 A 的私有 Agent（`AI_AGENT_NOT_FOUND`），A 可指向自己。

### 测试结果
- `pytest -q`：**689 passed, 24 skipped**（全量回归；新增 5 条）。
- `ruff check .`：**All checks passed**。
- 无迁移，alembic head 仍为 `b1c2d3e4f5a6`。

### 下一步
阶段三：Windows 能力显式降级与文档化（§四第 3 项）。代码侧 `sandbox.untrusted_execution` / `full_requires_container` 已在 Phase 0 落地，本阶段重点是补正式文档（无容器即拒绝不可信执行、Windows 三重闸门）并校验 capabilities 与审计一致性；不新增能力、不放宽任何闸门。

---

## 阶段三：Windows 能力显式降级与文档化（§四第 3 项）— 已完成，待用户确认

日期：2026-11-24
状态：正式文档 + capabilities 一致性 + 错误码登记缺口修复完成；全量 pytest 695 passed / 24 skipped（基线 689 + 6 条新增），`ruff check .` 全绿。

### 三项最高约束遵守情况
1. 单机安装：纯能力如实上报与文档化；无租户逻辑、无外部服务。
2. 内核 21 Port 冻结：未新增 Port、未改契约；只补错误码登记与 capabilities 结构一致性。
3. exe 打包：无新依赖、无新表/迁移；文档为 Markdown，不进运行时包。

### 改动文件清单
新增：
- `docs/Z-Torque_Windows_能力降级与沙箱边界_v1.md`：平台能力矩阵、Windows 四道闸门、`full` scope fail-closed、capabilities 字段契约、错误码/审计、禁止事项与验收清单。
- `tests/integration/test_phase2_windows_capability.py`：6 条验收（capabilities 不变量、未装配结构、运行时拒绝、错误码登记、文档存在、闸门源码静态检查）。

修改：
- `app/main.py`：`_sandbox_capability()` 将 `script` 闸门声明提到分支之前，未装配时也返回结构完整的 `script` 段（此前缺失，前端读取会误判）。
- `app/core/errors.py`：补登记 `AI_SKILL_WINDOWS_UNTRUSTED`（`MD_SKILL_ERROR_CODES` + `PLUGIN_ERROR_CODES` + HTTP 403 + 语义常量 `SKILL_WINDOWS_UNTRUSTED`）。此前该码已在运行时使用却未登记，违反"错误码先登记"约定。
- `app/kernel/md_skill_runtime.py`：字面量改用语义常量。
- `tests/unit/test_plugin_error_codes.py`：期望集合与状态映射同步新错误码。

### 关键设计决定（后续项勿踩坑）
- **闸门声明与装配状态无关**：`windows_requires_trusted` / `windows_requires_confirmation` / `full_requires_container` 恒为 true，即使未装配也返回，避免前端把"字段缺失"理解为放行。
- **一致性不变量**：`untrusted_execution == "container"` 当且仅当 `isolation_level == "container"`；其余必须 `unavailable`。集成用例锁定，后续改 capabilities 不得破坏。
- **不放宽任何闸门**：本阶段只补文档与登记，未改 `md_skill_runtime` 的判定顺序、`SkillGateway` 的确认 checkpoint、沙箱驱动的 scope 复校验。
- Windows 脚本实际是**四道**闸门（总开关 + windows_trusted + allowed_roots + 人工确认），路线图正文写"三重"是指运行时三道 + 网关确认；文档按四道如实记录。

### 真实入口验收（6 条，均通过）
1. 匿名 `/capabilities`：`scope.full_requires_container=true`、`gateway_only=true`、`script.windows_requires_trusted=true`、`windows_requires_confirmation=true`，`untrusted_execution` 与 `isolation_level` 自洽。
2. 模拟未装配：上述结构字段仍完整返回，`untrusted_execution=unavailable`。
3. monkeypatch `os.name="nt"` + `windows_trusted=False`：真实 `MdSkillRuntime.execute()` 抛 `AI_SKILL_WINDOWS_UNTRUSTED` 403。
4. 错误码已登记且 HTTP 403。
5. 正式文档存在且含全部关键约束字符串（机器校验）。
6. 运行时源码含三道闸门判定与 `AI_SANDBOX_DENIED`（静态红线）。

### 测试结果
- `pytest -q`：**695 passed, 24 skipped**（全量回归；新增 6 条）。
- `ruff check .`：**All checks passed**。
- 无迁移，alembic head 仍为 `b1c2d3e4f5a6`。

### 下一步
阶段四：自动化调度 / cron（§四第 5 项）。当前无统一调度模块，只有 `MdSkillWatcher`、`TaskRuntime`、`audit_export_archive/jobs.py` 的散落 Timer / asyncio task。需建统一本机调度器（interval + cron，单机无需租户隔离），迁移散落定时器；先补负向用例再实现，不得改变各任务原有语义与关闭开关。

---

## 阶段四：自动化调度 / cron（§四第 5 项）— 已完成，待用户确认

日期：2026-11-24
状态：负向用例先行，统一本机调度器 + watcher 托管 + 受控 API 完成；全量 pytest 702 passed / 24 skipped（基线 695 + 7 条新增），`ruff check .` 全绿。

### 三项最高约束遵守情况
1. 单机安装：纯进程内 asyncio 调度，无租户矩阵、无外部服务、无 Redis/APScheduler。
2. 内核 21 Port 冻结：调度器是 `app/kernel/local_scheduler.py` 基础设施，不新增 Port；错误码单独登记 `SCHEDULER_ERROR_CODES`。
3. exe 打包：零新依赖、零新表/迁移；cron 用标准库解析（5 字段：分/时/日/月/周）。

### 改动文件清单
新增：
- `app/kernel/local_scheduler.py`：`ScheduleSpec`（interval 与 cron 二选一强校验）、`LocalScheduler`（register/unregister/start/stop/trigger/status）、`parse_cron` / `cron_matches` / `next_cron_delay`；同步回调走 `asyncio.to_thread`，失败只记 stats 不停循环。
- `app/api/routes/scheduler.py`：`GET /scheduler/status`（登录可读）、`POST /{name}/trigger`（需 `system.config` 权限）。
- `tests/integration/test_phase2_local_scheduler.py`：7 条验收（非法定义、周期执行、失败隔离、cron 解析、手动触发/重复注册、watcher 托管、capabilities + 受控 API）。
- `app/core/errors.py` 新增 `SCHEDULER_ERROR_CODES` 集合（4 个 `AI_SCHEDULE_*` 码）。

修改：
- `app/main.py`：lifespan 创建 `LocalScheduler` 挂 `app.state.scheduler`，传给每个 `MdSkillWatcher`；所有 watcher 注册后统一 `await scheduler.start()`；退出时先 `await scheduler.stop()` 再幂等 stop watcher；capabilities 新增 `scheduler.available/interval/cron/jobs`。
- `app/kernel/md_skill_watcher.py`：新增可选 `scheduler` 参数；注入后 `start()` 注册 `md_skill_watch:{tenant_id}` 任务、`stop()` 注销；未注入时保留原自管循环（兼容直构测试）；抽出 `_tick()` 供两种模式共用。
- `tests/conftest.py`：装配 fixture 挂空调度器实例（与 main.py 对齐，不启动任务）。
- `app/api/router.py`：挂载 scheduler 路由。

### 关键设计决定（后续项勿踩坑）
- **统一入口但渐进迁移**：`MdSkillWatcher` 已迁到调度器；`TaskRuntime` 的 `_dispatch_loop`/`_timeout_loop` 是高频内部循环而非"调度任务"，保持 asyncio task；`audit_export_archive` 的 `threading.Timer` 保留（插件在 contribute 期启动，早于 lifespan 创建调度器，强迁会破坏装配时序）。后续若要全迁，需先把插件的调度注册改为延迟发布。
- **cron 分钟粒度**：`next_cron_delay` 逐分钟搜索最多一年；`*/5 * * * *` 等标准表达式可用；周字段 0=周日，内部转 Python weekday。
- **失败隔离**：`_execute` 捕获所有非 CancelledError 异常，只累加 `failures` 与 `last_error`；健康任务不受影响（负向用例锁定）。
- **`status()` 不暴露 payload**：只给 name/enabled/interval/cron/running/runs/failures/last_run_at/last_error，不含凭据或路径。
- trigger 需 `system.config` 权限（管理员等价），避免普通用户触发归档/清理副作用。

### 真实入口验收（7 条，均通过）
1. 负向：缺周期 / 双周期 / 非正 interval / 非法 cron 均抛 `AI_SCHEDULE_INVALID`。
2. interval=0.02s 任务周期执行，`status()` runs≥2、failures=0、stop 后 running=False。
3. 失败任务 failures≥1、runs=0、`last_error="boom"`，健康任务照常执行。
4. `parse_cron("*/5 * * * *")` 得到 12 个分钟点；固定时刻下一次延迟 90 秒。
5. 手动 `trigger("manual")` 执行且 runs=1；重复注册 409、未知任务 404。
6. 注入调度器的 `MdSkillWatcher.start()` 注册 `md_skill_watch:*`，stop 后无遗留运行任务。
7. 匿名 `/capabilities` 上报 `scheduler.available=true`；admin 登录后 `/scheduler/status` 200、触发不存在任务 404。

### 测试结果
- `pytest -q`：**702 passed, 24 skipped**（全量回归；新增 7 条）。
- `ruff check .`：**All checks passed**。
- 无迁移，alembic head 仍为 `b1c2d3e4f5a6`。

---

## 会话文件变更审查：Git 风格 diff + Keep / Undo — 已实现，待用户前端验收

日期：2026-11-26
状态：真实补丁产物接入前端审查；全量 pytest 752 passed / 24 skipped / 6 failed（失败均为既有工具目录/MCP 用例，与本项无关），相关 ruff 全绿，`npm run build` 通过；浏览器实测：打开抽屉、空态、样本变更 diff、统一/并排切换、Keep、Undo 真实写回、自定义对比 Tab 均正常。本地后端已按原命令重启并升级迁移到 `a6d2e8f4b7c1`，验证用临时数据已清理。

### 三项最高约束遵守情况
1. 单机安装：按会话 + 任务属主过滤，无租户矩阵、无跨租户逻辑。
2. 内核 21 Port 冻结：未新增 Port；撤销复用 `SkillGateway.apply_patch`，只加 `record_artifact=False` 开关。
3. exe 打包：无新 Python/Node 依赖；前端 diff 为自研行级 LCS，未引入 `diff/monaco/shiki`。

### 改动文件清单
新增：
- `alembic/versions/a6d2e8f4b7c1_patch_artifact_review_state.py`：`zr_patch_artifact` 增 `base_dir / review_state / reviewed_at`。
- `console/src/components/PatchReview.tsx`：变更文件列表、统一/并排 diff、Keep/Undo、刷新与状态标签。
- `tests/integration/test_patch_artifact_review.py`：真实 HTTP 链路验收（列变更 → 详情 → Keep → Undo → 重复撤销拒绝）。

修改：
- `app/models/patch_artifact.py`：新增审查状态常量与三列。
- `app/kernel/skill_gateway.py`：写入 artifact 时保存 `base_dir`；新增 `record_artifact=False` 供撤销复用，不造反向产物。
- `app/api/routes/workspace.py`：新增 `/workspace/patch-artifacts` 列表、详情、`keep`、`undo`；审计 `PATCH_KEEP` / `PATCH_UNDO` 仍 metadata-only。
- `console/src/types/index.ts`：新增 `PatchArtifactSummary / PatchArtifactDetail / PatchReviewState`。
- `console/src/components/Phase1Panels.tsx`：`文件对比` 改为 `文件变更` 抽屉，默认审查视图；原手动对比保留为「自定义对比」Tab。
- `console/src/index.css`：审查布局与 diff 样式。

### 关键设计决定
- 旧 `docs/question.md` 中“移除代码 Diff / 不暴露 diff 术语”以本次用户明确需求为准被覆盖；自定义对比保留但不再作为默认入口。
- Undo 是真实写回：交换 `before_text/after_text`，`expected_hash = content_hash`；哈希冲突返回 409，最新版本之外/已过期/已撤销不允许撤销。
- 24 小时 TTL 后未处理项显示「已过期」，不可撤销；审计不写入 diff 正文。
- alembic head 更新为 `a6d2e8f4b7c1`。

---

## 会话文件变更升级为同名文件版本审查（Git 风格，支持各类输出文件）— 已实现，待用户前端验收

日期：2026-11-26
状态：用户澄清需求为「智能体再次输出同名文件（如 Word 优化版）应自动触发文件变更对比」，不是仅文本 patch；本轮补全文件版本链路。相关 ruff 全绿、`npm run build` 通过、相关集成测试 10 passed；全量 pytest 753 passed / 24 skipped / 6 failed（失败均为既有工具目录/MCP 用例，与本项无关）。浏览器实测同名 docx 两版自动生成变更、统一 diff、Keep、Undo 真实恢复上一版均正常。测试用文件与记录已清理，alembic head 更新为 `f7c2a9d4e6b8`，本地后端已重启。

### 三项最高约束遵守情况
1. 单机安装：按会话 + 用户属主过滤，无租户矩阵。
2. 内核 21 Port 冻结：未新增 Port；文档输出仍走既有 `output_document` / `create_chat_document`，只在落库后追加版本记录。
3. exe 打包：无新 Python/Node 依赖；diff 统计用标准库 `difflib`，前端用自研行级 LCS。

### 改动文件清单
新增：
- `app/models/file_version_change.py` + `alembic/versions/f7c2a9d4e6b8_file_version_change.py`：`zr_file_version_change` 表，保存同名文件前后版本的可读文本、增删行数、审查状态。
- `app/services/text_diff.py`：共享行级增删统计。
- `tests/integration/test_file_version_change.py`：同名 docx 两次输出 → 列变更 → 详情 → Keep → Undo → 上一版恢复验收。

修改：
- `app/services/chat_document.py`：同名文件再次输出时，用 `extracted_text` 生成版本变更；审计只记元数据。
- `app/services/execution_loop.py`：智能体输出文件时把 `task_id` 传入版本变更。
- `app/api/routes/workspace.py`：新增 `/workspace/file-changes` 列表/详情/`keep`/`undo`；补丁产物 payload 补 `source` 字段。
- `console/src/types/index.ts`、`console/src/components/PatchReview.tsx`、`console/src/index.css`：前端合并展示「文件版本」和「文本补丁」两类变更，按来源分发 Keep/Undo。

### 关键设计决定
- 触发条件：同一会话、同一用户、**同名文件**再次由 `output_document` / `chat/export-document` 输出，且正文不同；自动产生一条文件版本变更。旧文件本身不删除。
- 对比对象是文件的 **extracted_text**（docx/pdf/md/txt 均为可读文本），不是二进制字节；因此可覆盖各类文件而非仅代码。
- Keep：仅标记采纳，不改附件状态。
- Undo：把新版本 `File.status` 置为 `DELETED`，上一版自动成为当前可见文件；非最新版本不可撤销。
- 原「自定义对比」Tab 保持不变；原文本补丁变更仍保留，和文件版本变更在同一列表混合展示。

---

## ChangeSet 提案优先改造 + 文字类 diff 视图优化 — 已实现，待用户前端验收

日期：2026-11-26
状态：按用户给出的 ChangeSet 规则重做文件变更语义：智能体输出文档不再直接写盘，先生成提案，用户 Keep 才落地；Cancel 不写盘；回退基于历史快照生成反向提案后再确认。浏览器实测提案列表、目的/风险、文字化 diff、Keep 落地、回退提案均正常。相关 ruff 全绿，`npm run build` 通过，相关集成测试 11 passed；全量 pytest 754 passed / 24 skipped / 5 failed（剩余失败均为内置 output_document 与既有工具映射断言冲突，本轮未新增失败）。

### 三项最高约束遵守情况
1. 单机安装：按会话 + 用户属主过滤，无租户矩阵。
2. 内核 21 Port 冻结：未新增 Port；提案服务只复用既有 object.store、document_export、audit。
3. exe 打包：无新 Python/Node 依赖；提示词压缩后通过技能注入硬预算测试。

### 改动文件清单
新增：
- `app/services/file_change_review.py`：ChangeSet 服务（propose / keep 落地 / cancel / 反向回退提案）。

修改：
- `app/models/file_version_change.py` + `alembic/versions/f7c2a9d4e6b8_file_version_change.py`：增加 `kind / fmt / purpose / risk / message_id` 与 `CANCELLED` 状态。
- `app/services/chat_document.py`：文档输出改为生成提案，默认不写对象存储、不建 `File` 附件。
- `app/services/context_builder.py`、`app/services/tool_mapping_builder.py`：注入精简 ChangeSet 规则，工具参数加 `purpose / risk`，说明改为“先提案后 Keep”。
- `app/services/execution_loop.py`、`app/api/routes/chat.py`：传递任务 ID、目的、风险与旁路开关；导出接口同步提案语义。
- `app/api/routes/workspace.py`：`/file-changes` 增加 `/cancel`，`/undo` 改为生成反向提案；`/keep` 才写盘并创建/切换附件。
- `console/src/components/PatchReview.tsx`、`console/src/types/index.ts`、`console/src/components/MessageBubble.tsx`、`console/src/index.css`：采纳/放弃/回退按钮、目的与风险展示、消息提案标签、文字类 diff 样式（普通字体、默认隐藏行号、业务化变更标题）。
- `tests/integration/test_file_version_change.py`：按提案语义重写验收。

### 关键设计决定
- Keep 前：`zr_file_version_change` 有提案正文，但对象存储和 `zr_file` 不变；消息气泡显示“变更提案 · 文件名”。
- Keep 后：才调用 `build_document` + `object.store.put` 创建 `File`，并把助手消息切换为可下载附件。
- Cancel：只标记 `CANCELLED`，原文件不变。
- 回退：`/undo` 只生成 `ROLLBACK` 提案，仍需用户 Keep 才把当前版本置为 `DELETED` 并恢复上一版。
- 旁路快速模式：`file_change_auto_keep` 配置默认 false，显式开启后才跳过确认。
- diff 视图改为面向文字文档：默认无行号、普通字体、自动换行，保留统一/并排视图和可选行号。

### 下一步
阶段五：i18n、主题/背景/字号进阶（§四第 6 项）。主题体系已有 `ThemeProvider`/`tokens.ts`/`antdTheme.ts` 可复用；需补轻量中英 i18n、用户可选背景/字号与统一设置页；不引入新 npm 依赖（`package.json` 不动），不破坏 antd 6 + ConfigProvider。

---

## 阶段五：i18n、主题/背景/字号进阶（§四第 6 项）— 已完成，待用户确认

日期：2026-11-24
状态：轻量中英字典 + 偏好扩展 + 统一设置页完成；前端 `npm run build` 成功，后端全量 pytest 702 passed / 24 skipped，`ruff check .` 全绿。

### 三项最高约束遵守情况
1. 单机安装：所有偏好只写本机 `localStorage`，不写后端、不影响其他用户。
2. 内核 21 Port 冻结：纯前端改动，未动后端契约。
3. exe 打包：**未新增任何 npm 依赖**（`package.json` 未动）；i18n 用自写字典 + Context，不引 react-i18next。

### 改动文件清单
新增：
- `console/src/i18n/messages.ts`：中英字典（导航 / 顶栏 / 设置页），`translate()` 缺失 key 回退中文再回退 key。
- `console/src/pages/Settings.tsx`：统一偏好设置页（语言 / 主题 / 背景 / 字号 + 实时预览）。

修改：
- `console/src/theme/ThemeProvider.tsx`：扩展 context 增加 `locale/setLocale`、`background/setBackground`、`fontScale/setFontScale`、`t()`；三个新偏好各有 localStorage 白名单校验；背景/字号落到 `<html data-bg/data-font>`；antd `ConfigProvider` 按语言切 `zhCN`/`enUS`；导出 `usePreferences` 别名（保留 `useThemeMode` 兼容）。
- `console/src/components/AppTopbar.tsx`：`NavItem` 加 `labelKey`；`APP_NAV` 补 key 与 `/settings` 入口；顶栏加中/英切换按钮；主题/退出文案走字典。
- `console/src/App.tsx`：注册 `/settings` 懒加载路由（ConsoleShell 包裹）。
- `console/src/index.css`：新增 `--base-font-size` 与 `[data-font]` / `[data-bg]` / 暗色背景变体规则；设置页与语言按钮样式。

### 关键设计决定（后续项勿踩坑）
- **渐进 i18n**：字典只覆盖导航、顶栏、设置页；业务页保持中文硬编码，避免为"全量翻译"引入大字典拖慢 exe 前端。`labelKey` 缺失时回退中文 label，不破坏任何既有调用点。
- **偏好单一来源**：不新建第二个 Provider，直接扩展 `ThemeProvider`；`useThemeMode` 保留原签名兼容既有页面，`usePreferences` 是新语义别名。
- **脏 localStorage 防护**：`storedChoice()` 白名单校验，非法值回退默认，不让手改 localStorage 把界面弄坏。
- **字号只三档**（紧凑 13 / 标准 14 / 舒适 16），背景三态（默认 / 柔和 / 暖色），暗色各自派生；不引入第四档，沿用令牌"不新增档位"纪律。

### 验收结果
- `npm run build`：**成功**（tsc --noEmit + vite build；新增 `Settings` 独立 chunk 56.93 kB，未出现体积错误）。
- 后端 `pytest -q`：**702 passed, 24 skipped**（与基线一致，本阶段纯前端）。
- `ruff check .`：**All checks passed**。
- 浏览器验证：按此前用户口径跳过自动化，由用户自行验收。

### 下一步
阶段六：Channels 凭据托管与审计（§四第 7 项）。已有 `secret.provider` / Fernet `SecretStore` / `secret://` 引用与 Webhook/Connector/Model 审计事件可复用；缺口是集中 `resolve_secret` 解析、`/secrets/convert` 无审计、channel 维度审计事件。先补负向用例再实现，不新增内核 Port。

---

## 阶段六：Channels 凭据托管与审计（§四第 7 项）— 已完成，待用户确认

日期：2026-11-24
状态：负向用例先行，集中解析 + convert 审计 + 渠道凭据生命周期审计完成；全量 pytest 706 passed / 24 skipped（基线 702 + 4 条新增），`ruff check .` 全绿。

### 三项最高约束遵守情况
1. 单机安装：凭据仍走本机 Fernet `SECRET_STORE_KEY`，无 KMS/外部服务；审计只记 metadata。
2. 内核 21 Port 冻结：`secret.provider` Port 契约未改；resolver 是 `app/security/` 领域服务，经 `try_port` 取已解析 Port。
3. exe 打包：无新依赖、无新表/迁移；错误码单独登记 `SECRET_ERROR_CODES`。

### 改动文件清单
新增：
- `app/security/secret_resolver.py`：`is_secret_ref` / `secret_ref_of` / `resolve_secret`；空引用与非法密文显式拒绝，未装配 `secret.provider` 报 `AI_SECRET_UNAVAILABLE`，错误信息不回显密文/明文。
- `tests/integration/test_phase2_secret_custody.py`：4 条验收（负向解析、往返与明文兼容、审计分类、convert metadata-only 审计）。
- `app/core/errors.py` 新增 `SECRET_ERROR_CODES`。

修改：
- `app/api/routes/secrets.py`：`/secrets/convert` 写 `SECRET_CONVERTED` 审计，detail 仅 `{ref_hash, value_length, channel}`，明文与密文都不入审计。
- `app/api/routes/webhooks.py`：创建/轮换 webhook 签名密钥时补 `CHANNEL_CREDENTIAL_STORED` / `CHANNEL_CREDENTIAL_ROTATED` 审计，detail 只记渠道与密钥 hash。
- `app/kernel/audit/categories.py`：新增 `CATEGORY_CREDENTIAL` 与 4 个凭据审计事件映射。
- `app/kernel/audit/metadata.py`：白名单补 `channel` / `value_length`。

### 关键设计决定（后续项勿踩坑）
- **集中解析不强制一次性替换全部调用点**：resolver 提供统一语义与错误码；现有 10+ 处 `removeprefix` 调用保留（行为不变），新增渠道代码必须走 resolver。
- **明文兼容保留**：非 `secret://` 的值原样返回，避免存量配置（明文 api_key）在升级后全部失效；`required=True` 时才拒绝空值。
- **审计只记 hash**：`ref_hash` 是引用/密钥的 SHA256，不可逆；`value_length` 只记长度；`channel` 标识来源渠道，为后续 Channels 扩展预留维度而不新建表。
- 未新建 Channels 实体/表——路线图原文是"Channels **扩展前**的凭据托管与审计"，本阶段只准备凭据与审计基础设施。

### 真实入口验收（4 条，均通过）
1. 负向：`secret://` 空引用、非法密文 → `AI_SECRET_INVALID_REF`，错误信息不含原文。
2. 正向：密文引用往返解密；非引用明文原样返回。
3. 4 个凭据审计事件均归入 `credential` 类别。
4. 登录后 `POST /secrets/convert` → 审计 `detail` 恰好 3 个键，明文与密文均不出现。

### 测试结果
- `pytest -q`：**706 passed, 24 skipped**（全量回归；新增 4 条）。
- `ruff check .`：**All checks passed**。
- 无迁移，alembic head 仍为 `b1c2d3e4f5a6`。

---

# Phase 2 收口总结（2026-11-24）

六个阶段全部实现完成，路线图 §四 7 项中「对外 MCP Server」按原文「单机下先不做」明确排除。

| 阶段 | 内容 | 主要产物 | 验收基线 |
|------|------|---------|---------|
| 一 | manifest-first + capability ladder | `app/plugin_sdk/capability.py`、`PluginSdk` 双门禁、`governance` 上报 | 684 passed |
| 二 | repositories 属主过滤 | `app/repositories/ownership.py`、工具目录/流式/webhook 接入 | 689 passed |
| 三 | Windows 能力降级文档化 | 正式文档、`AI_SKILL_WINDOWS_UNTRUSTED` 登记、capabilities 一致性 | 695 passed |
| 四 | 自动化调度 / cron | `app/kernel/local_scheduler.py`、watcher 托管、`/scheduler` API | 702 passed |
| 五 | i18n / 主题 / 背景 / 字号 | 轻量字典、`Settings.tsx`、偏好持久化 | 702 passed（纯前端） |
| 六 | Channels 凭据托管与审计 | `secret_resolver.py`、convert/渠道审计事件 | 706 passed |

**最终验证**：`pytest -q` **706 passed / 24 skipped**；`ruff check .` **All checks passed**；`npm run build` **成功**；无新表/迁移（head `b1c2d3e4f5a6`），无新 Python/Node 依赖，21 Port 数量未变。

**最高约束复核**：单机固定租户、无跨租户设计；21 Port 冻结、新增能力全放 builtin/kernel 基础设施或 plugin_sdk 公开面；SQLite 唯一、exe 打包无外部服务与新依赖。

**遗留说明**：`audit_export_archive` 的 `threading.Timer` 未迁入统一调度器（插件 contribute 期早于 lifespan 创建调度器，强迁会破坏装配时序）；`TaskRuntime` 的高频内部循环保留 asyncio task（非调度任务语义）。两项均不影响 Phase 2 出口指标。
