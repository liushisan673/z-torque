# Z-Torque Community M1 — 项目记忆（会话交接）

**更新**：2026-10-26　**状态**：本地开发中，**已提交并推送 GitHub**（master = `2e7b7a1`，github 远端；2026-10-26 一次性授权代提交，后续改动仍默认由用户本人提交，禁止代重置）
**最新文档**：`docs/Z-Torque_标准化插件_A2A_审计导出归档_沙箱_v1.3.md`（v1.4，M4 收口；§12 验证 / §13 缺陷修订）

---

## 1. 项目是什么

AI Runtime 内核（M0–M3 阶段成果）+ 轻量控制台。核心架构是「内核 + 插件 + Port」：所有外部能力通过 `PluginSdk` 注入；插件声明 `permissions` / `requires` / `provides`；插件表由内核迁移创建；插件路由**常注册**，未装配时返回 503 而不是 404。

技术栈：Python 3.11+ / FastAPI / SQLAlchemy 2.0（`Mapped[]` 风格）/ Alembic / pytest；前端 `console/` 为 React + TS + Vite。

## 2. 目录地图

| 路径 | 职责 |
|---|---|
| `app/kernel/` | 内核：agent / task / skill / gateway / memory / tool / audit / ports |
| `app/kernel/audit/reader.py` | **审计只读缝** `iter_audit_events`（插件唯一合法读审计的入口） |
| `app/plugin_sdk/` | **插件公开契约包**（标准插件唯一允许依赖的 `app.*` 面）：再导出 `PluginManifest`/`PluginSdk`/`AuditQuery`/`AuditRecord`/`SecurityContext`/`SandboxDriver`/`emit_event`/`Base` |
| `app/kernel/plugin_reads.py` | 内核向插件开放的只读投影缝：`list_published_agents` / `list_tenant_ids` |
| `app/builtin_plugins/` | 内置插件：model_provider、skill_executor、mcp_connector、queue_driver、`sandbox_driver/`（`process.py` = 执行实现）、`audit_export_archive/`（含 `models.py` + `migrations/`）、`a2a_gateway/`；**`__init__.py` 已改 PEP 562 惰性导出**（聚合导入会毁掉单插件可移植性） |
| `app/builtin_plugins/discovery.py` | 插件发现：`BUILTIN_PLUGINS` 元组兜底在前 + entry_points（group `ztorque.plugins`）追加；重名内置优先、坏包默认跳过 |
| `app/api/routes/` | HTTP 路由（`audit_exports.py`、`a2a.py` 为 v1.3 新增） |
| `app/models/` `app/db/` | 模型与连接；`app/db/base.py::GUID` 是 UUID 统一类型 |
| `alembic/versions/` | 迁移链，head = `c7a1e4b90d23`（v1.3.6 租约迁移，插件目录下） |
| `tests/` | pytest；`tests/support/` 为清理辅助（`ddl.py` / `fakes.py`） |
| `scripts/check_migration_drift.py` | 输出「DB 结构 vs 模型」逐条差异 |
| `scripts/reconcile_audit_archive_objects.py` | 归档/导出对象对账（默认 dry-run 零写入，`--apply` 只删孤儿对象；口径见 v1.3 §12.8） |
| `console/` | React 控制台（列表页统一用 `hooks/useListQuery.ts` + `components/list/`） |
| `.github/workflows/ci.yml` | CI 门禁：pytest 矩阵（SQLite / PG16）+ 干净库迁移门禁 + console 构建 + compileall，详见 §6.1 |
| `docs/` | 阶段文档 + v1.3 插件文档（v1 / v1.2 已被 v1.3 取代） |

## 3. 完成状态（实测口径）

| 里程碑 | 状态 | 说明 |
|---|---|---|
| M0 七项内核裁决 | ✅ 完成 | 只读缝、`require_permission`、`sdk.session()`、`assembly_plugins`、错误码/类别、`features` 派生 |
| M1 `sandbox_driver`（md_skill 沙箱加固） | ✅ 完成 | cwd 白名单、`-I` 独立解释器、最小 env、rlimit、进程组 killpg、输出上限、fail-closed |
| M2 `audit_export_archive` | ⚠️ 代码完成 | 导出/归档/脱敏/清理/下载留痕齐备；**仅单副本语义**（`AUDIT_EXPORT_MAX_CONCURRENCY=1`） |
| M3 `a2a_gateway` | ✅ 完成 | 协议/深度扇出/熔断/白名单/发现投影齐备；**远端已真联调**（`tests/integration/test_a2a_live_peers.py`，uvicorn 子进程 + 真 TCP/TLS，`ZTORQUE_A2A_LIVE=1` 打开），真联调修掉「统一封装不解包」「出站载荷把路由信息当执行指令」两个缺陷 |
| M4 | ✅ 完成（v1.4，2026-10-26） | sidecar 容器隔离（`SANDBOX_ISOLATION_MODE=container`，docker CLI 子进程，同 Port 同契约、社区默认 process 零变化）+ A2A 联邦身份（方案 B service account 入站 + per-peer 出站 mTLS）；入站真 mTLS 登记为部署层职责（uvicorn 不向 ASGI 暴露客户端证书）。未做：`object.store.list` |

**测试**：全集 **491** 项（阶段 2/3 新增 36 项：发现 11、配置契约 15、manifest 契约 4、单插件隔离 6；D 组新增 12 项观测接线用例；E 组新增 5 项租约用例；F 组新增 2 项出站语义单测；归档对象对账新增 14 项；归档密钥轮换新增 7 项）。**另有 8 项 A2A 真联调用例**（`tests/integration/test_a2a_live_peers.py`）默认 skip，`ZTORQUE_A2A_LIVE=1` 才跑（约 12~20s，起 uvicorn 子进程 + 真 TCP/TLS 对端）。**2026-10-26 三平台实测（F 组后）**：Windows+SQLite `470 passed / 11 skipped`（含 8 项 live skip）；Linux+SQLite（`zt-test` 容器）`471 passed / 10 skipped`；Linux+PostgreSQL 16（`zt_suite` 临时库）`471 passed / 10 skipped`；live 8 项在 Windows 与 Linux 容器内均 `8 passed`。**2026-10-26 对账脚本落地后三平台复跑**：Windows+SQLite `484 passed / 11 skipped`；Linux+SQLite `485 passed / 10 skipped`；Linux+PG16 `485 passed / 10 skipped`（Linux 多出 1 项为仅 POSIX 的沙箱用例）。**2026-10-26 密钥轮换落地后三平台复跑**：Windows+SQLite `491 passed / 11 skipped`；Linux+SQLite `492 passed / 10 skipped`；Linux+PG16 `492 passed / 10 skipped`；`ruff check app scripts tests` 全绿。**2026-10-26 Port 协议解耦后三平台复跑（新增 1 项子进程探针）**：Windows+SQLite `492 passed / 11 skipped`；Linux+SQLite `493 passed / 10 skipped`；Linux+PG16 `493 passed / 10 skipped`。**2026-10-26 M4 落地后三平台复跑**：Windows+SQLite `507 passed / 23 skipped`；Linux+SQLite `513 passed / 17 skipped`；Linux+PG16 `513 passed / 17 skipped`（A2A live 10 条全过：8 旧 + 出站 mTLS 双向认证 + 联邦入站真执行）。skip 恒为「未设 `ZTORQUE_MIGRATION_CHECK_URL`」+「Windows-only 沙箱用例」+「未开 `ZTORQUE_A2A_LIVE`」+「未开 `ZTORQUE_SANDBOX_CONTAINER_LIVE`（且 Windows 恒 skip）」四类，不是失败。
**迁移**（A 组 2026-10-26 实测）：`zt_clean` 上 `alembic upgrade head` → `alembic check` 输出 `No new upgrade operations detected`、`scripts/check_migration_drift.py` `total=0`；`upgrade head --sql` 生成的 1018 行离线脚本在空库 `zt_ddl` 执行后同样 `total=0`；`zt_mig` 先 `upgrade head` 再跑 `tests/integration/test_alembic_migrations.py` → `3 passed`。存量 PG / 干净 SQLite 两处此前已零漂移。
**CI**：`.github/workflows/ci.yml` 已建（见 §6.1）。**2026-10-26 真实 GitHub run 全绿**：run #1（`2e7b7a1`，Port 协议解耦）与 run #2（`a1dd72a`，M4 收口）均 7 job 全 success（公开仓库 `liushisan673/z-torque`：pytest-sqlite 含 POSIX guard / pytest-postgres / migrations / plugin-conformance / console / python-static / pytest-windows）。
**标准插件**（2026-10-26）：三插件（`sandbox_driver` / `audit_export_archive` / `a2a_gateway`）已按「可整目录搬到企业版」解耦 —— 越界 `app.kernel.*`/`app.models.*` import 从 **20 处降到 0**，由红线测试机器校验；沙箱执行实现已从内核下沉到插件，内核不再保留任何可执行实现。

## 4. 关键约定（改代码前必读）

1. 插件只能经 `PluginSdk` 取能力，**禁止** import 内核内部模块。标准插件可依赖的 `app.*` 只有 `app.plugin_sdk`（公开契约包）与 `app.core.*`（errors/ids/servertime/redaction）；`tests/unit/test_plugin_redlines.py::test_plugins_import_only_public_sdk_surface` 机器执法（基线只减不增，现为 **0**）。
2. 插件读审计只能走 `sdk.read_audit(query)`（类型从 `app.plugin_sdk` 取，实现在 `app/kernel/audit/reader.py`），禁止直连 `zr_audit_log`；插件自证留痕走 `sdk.record_audit(...)`（独立事务，失败只记日志）。
3. 插件**运行时不建表、无 DDL 权**：DDL 只由内核统一 Alembic 链执行。**方案 b（2026-10-26 定稿）**：插件私有表的**模型与迁移文件跟随插件目录**（`audit_export_archive/{models.py,migrations/}`），但必须注册进同一条链——`alembic.ini::version_locations` 加该目录，`alembic/env.py` 显式 import 模型注册 `Base.metadata`。
4. 插件路由常注册；未装配 → `503 AI_PLUGIN_NOT_ASSEMBLED`。
5. 高危操作必须 `require_permission` + 写审计，两者不可拆开。
6. 装配档位 `ASSEMBLY_PROFILE`：`community`（默认）/ `enterprise`（社区 + `audit_export_archive` + `a2a`；`sandbox_driver` 在 `execution` 模块里，社区档就有）。需要"社区版按需选配"时用追加写法 `<基础档>+<Module|插件>[,...]`（如 `community+audit_export_archive,a2a`）：只追加不覆盖，条目不存在或重复选配基础档已有插件都拒绝启动（`resolve_profile`），两个内置档展开结果不变。
7. `SANDBOX_ALLOWED_ROOTS` 显式空数组 → 装配失败（不允许「空配置 = 全放开」）。
8. 错误码集中在 `app/core/errors.py` + `app/kernel/audit/categories.py`；前端文案在 `console/src/services/errorText.ts`。
9. 代码风格：中文注释/文档字符串、类型标注齐全、不用 `as any`。

## 5. 踩过的坑（别重复踩）

1. **PG 建连**：`app/db/session.py` 的 `PRAGMA` 只能对 `sqlite3.Connection` 发，否则 PostgreSQL 一连就语法错。
2. **UUID 列**：迁移里统一 `GUID(length=36)`，不要 `sa.Uuid()`（PG 上外键建不出来，迁移链会断）。
3. **时间戳**：模型与迁移都必须是 `DateTime(timezone=True)`；写成 naive 时 PG 会静默丢弃时区偏移，而 **SQLite 完全看不出这类问题**。
4. **迁移里的反射式增量**（`sa.inspect`）在 `--sql` 下必炸，需要 offline 分支（列视为不存在、旧约束视为存在、数据校验跳过）。
5. **`alembic.ini`**：`sqlalchemy.url` 必须留空（否则抢在 `DATABASE_URL` 前，误连库）；ini 内不能有中文（`ConfigParser` 按系统 locale 读，GBK 环境直接崩）。
6. **`fileConfig`**：必须 `disable_existing_loggers=False`，否则进程内跑迁移会把 `app.*` logger 全禁掉，后续 `caplog` 断言莫名失败。
7. **测试清理外键**：用 `tests/support/ddl.py`（SQLite→`PRAGMA`，PG→`session_replication_role`），别硬编码 `PRAGMA`。
8. **沙箱 FSIZE**：Linux 上 CPython 把 `SIGXFSZ` 置 `SIG_IGN`，超限靠 `EFBIG`（`[Errno 27]`）而非信号 → 判定要「信号退出码 或 rlimit 开启且 stderr 含 errno 文案」。
9. **killpg 之后**：孙进程变僵尸，`os.kill(pid, 0)` 仍返回成功 → 判活要读 `/proc/<pid>/stat` 状态位。
10. **Windows 沙箱**：无 rlimit / 无进程组，直接 fail-closed 拒绝执行；不要假装支持。
11. **`process.spawn`**：目前只登记、**无强制点**（`sandbox_driver` 用 `asyncio.create_subprocess_exec` 自建子进程，内核无 spawn 拦截层）。v1.3 文档已如实登记（§2.1 表格 / §3.3 实现约定 / §12.5 限制第 7 条），**勿在 capabilities 或验收里说成「已拦截」**；能力边界由 `process.py` 自身实现（cwd 白名单 / rlimit / killpg / 输出上限）保证。
12. **判断迁移是否漂移，必须用干净库**：历史库可能被 `create_all` / batch 重建修正过，看不出问题。
13. **没有 `.env` 就起不来**：`app/config.py` 的 `JWT_SECRET_KEY` / `SECRET_STORE_KEY` 是必填字段，`.env` 又被 gitignore → fresh checkout（CI）直接 `ValidationError`。CI 必须显式给假值，且 `SECRET_STORE_KEY` 必须是**合法 Fernet key**（urlsafe base64 的 32 字节），否则 `SecretStore` 初始化抛错。
14. **`noUnusedLocals` 的「假引用」**：JSX 注释块 `{ /* ... */ }` 里的 `{fn(...)}` 会被 grep 命中，但 tsc 视为未使用 → `TS6133`，而 `console` 的 `npm run build` 先跑 `tsc`，整条构建一直红。判断函数「是否真被引用」要先看是否落在注释里。
15. **`alembic.ini` 的 `%(here)s` 指 ini 所在目录（仓库根），不是 `script_location`**：写 `version_locations = %(here)s/versions ...` 会解析成 `<repo>/versions`（不存在），`alembic heads` **输出空且退出码 0**——表现为「没有迁移」而不是报错。必须写 `%(here)s/alembic/versions`。
16. **模型搬出 `app/models` 后 `Base.metadata` 不再自动含它**：插件表模型随插件目录时，`alembic/env.py` 与 `tests/conftest.py` 必须显式 import 该模块，否则 `alembic check` 会要求 `DROP` 这三张表（元数据里没有它们）。
17. **父包的聚合导入会毁掉单插件可移植性**：`app/builtin_plugins/__init__.py` 里 `import` 全部插件后，`import app.builtin_plugins.<one>` 会连带拉起 19 个插件 + `app.models` + `app.services`（实测）。已改 PEP 562 惰性 `__getattr__`，`from app.builtin_plugins import X` 仍可用。判断「插件是否真解耦」不能只看插件目录内的文本 import（红线测试的盲区），要看 `sys.modules`。
18. **§5.16 的连坐：漂移脚本也要显式 import 插件模型**：模型随插件目录后，`scripts/check_migration_drift.py` 若只 import `app.models`，`Base.metadata` 里没有插件三表，会把它们判成 `remove_table` —— 在**完全干净**的 PG 库上实测 `total=14`（3 张表 + 11 个索引/约束），看着像「迁移链坏了」，实际是脚本假阳性。已补 `import app.builtin_plugins.audit_export_archive.models` 后 `total=0`。凡是自建 `Base.metadata` 的独立脚本（漂移比对、离线 DDL、种子校验）都要跟着 `alembic/env.py` 一起补 import。
19. **内核 Port 契约文件是冻结的**：`app/kernel/ports/{__init__,model_selector,specs}.py` 不许改内容（改内容=改契约）。E 组 ruff `--fix` 曾顺手把它们排序/删空行，已逐行还原，并在 `[tool.ruff.lint.per-file-ignores]` 给这三个文件豁免 `I001`（注明理由：不为格式化动契约）。**别再对 `app/kernel/ports` 跑 `ruff --fix`**。
20. **A2A 出站载荷别把「我自己的路由」当「对端要做什么」**：`params.peer` 是发送方命名空间里的名字，原样转发会让对端拿它查自己的 peer 表（真联调里两个本实例互调直接 503）。出站时若调用方钉了 `skill_version_id`，必须改写 `route=local` 让对端执行。同理，本项目所有接口返回统一封装 `{code,message,data,trace_id}`，任何「对端也是本项目」的出站都要先解一层（`PeerRegistry._send` 已做，只解一层、不误伤扁平响应）。
21. **熔断是进程内状态**：真联调/集成用例断言「连续 N 次失败后开路」时，若与其他用例共用同一实例，失败计数互相污染，靠用例顺序"消灾"就是假通过 → 该用例单独起实例并显式给 `PLUGIN_BREAKER_FAILURE_THRESHOLD`。另外 `A2AConfig.circuit_failure_threshold` 目前只做装配期校验，实际阈值来自内核 `plugin_breaker_*`。
22. **插件 config 是 dataclass，校验只在 `validate()` 里**：`AuditExportConfig(...)` 直接构造/`from_settings` 反序列化都**不会**触发边界校验（`__post_init__` 没接），所以「配了非法组合」在测试里不会自己炸——断言必须调 `config.validate()`，脚本入口也必须显式调一次再动密钥/数据，否则带着错配置继续跑。
23. **「索引说旧密钥」≠「真依赖旧密钥」**：`objects[].key_id` 是人写进去的，可能滞后（对象已用当前密钥写入、索引还没更新）。轮换闸门的**唯一**判据是「真解密能不能只用当前密钥解开」，索引只能做参考——把索引滞后当阻塞会让 retire 永远过不去；反过来只信索引则可能误放行。两者不一致时按「告警但放行」处理，并在脚本输出里单独列出来。

## 6. 环境与命令

```powershell
# Windows 全量
.\.venv\Scripts\python.exe -m pytest tests -q

# A2A 真联调（默认 skip；起 uvicorn 子进程 + 真 TCP/TLS 对端，约 12~20s；v1.4 含 mTLS/联邦 2 条新用例）
$env:ZTORQUE_A2A_LIVE="1"; .\.venv\Scripts\python.exe -m pytest tests/integration/test_a2a_live_peers.py -q
docker exec -e ZTORQUE_A2A_LIVE=1 zt-test bash -lc "cd /w && python -m pytest tests/integration/test_a2a_live_peers.py -q"

# 沙箱容器隔离真 docker 冒烟（v1.4；仅在「有 docker 的 POSIX 主机」可跑，Windows 恒 skip）
$env:ZTORQUE_SANDBOX_CONTAINER_LIVE="1"; .\.venv\Scripts\python.exe -m pytest tests/integration/test_sandbox_container_live.py -q

# 容器（Linux 验证）：zt-test（代码挂载 /w，已 pip install -e .[test]）、zt-pg（PG16，密码 pass），同 zt-net 网络
docker exec -e ZTORQUE_TEST_DATABASE_URL=sqlite:////tmp/zt_linux.db zt-test python -m pytest tests -q -p no:cacheprovider
docker exec -e ZTORQUE_TEST_DATABASE_URL=postgresql+pg8000://postgres:pass@zt-pg:5432/zt zt-test python -m pytest tests -q -p no:cacheprovider

# PG 干净库迁移门禁（zt_mig 先 upgrade 再只读比对，否则空库会报 124 条 add_table）
docker exec -e DATABASE_URL=postgresql+pg8000://postgres:pass@zt-pg:5432/zt_mig zt-test sh -c "alembic upgrade head && alembic check"
docker exec -e ZTORQUE_MIGRATION_CHECK_URL=postgresql+pg8000://postgres:pass@zt-pg:5432/zt_mig zt-test python -m pytest tests/integration/test_alembic_migrations.py -q

# 漂移明细 / 迁移
docker exec -e DATABASE_URL=postgresql+pg8000://postgres:pass@zt-pg:5432/zt zt-test python scripts/check_migration_drift.py
docker exec -e DATABASE_URL=... zt-test sh -c "alembic upgrade head && alembic check"

# 归档冷副本密钥轮换（先 set AUDIT_ARCHIVE_ENCRYPTION_ENABLED=true 与现有 SECRET_REF）
.\.venv\Scripts\python.exe scripts/rotate_audit_archive_key.py --new-secret-file .\new-key.txt   # 1 登记（打印待粘贴 env）
.\.venv\Scripts\python.exe scripts/rotate_audit_archive_key.py                                  # 2 只读核对（按 key_id 分布）
.\.venv\Scripts\python.exe scripts/rotate_audit_archive_key.py --retire-old                     # 3 真解密闸门，退出码 3 = 仍依赖旧密钥
```

> `zt-pg` 未映射宿主端口，只能在 `zt-net` 内互访。需要从宿主机（psql / GUI 客户端）连的时候另起一个映射了端口的实例 `zt-pg-gate`（`postgres:16-alpine`，`-p 55432:5432`，密码 `pass`）；A 组离线 DDL 复验的三个库 `zt_clean`/`zt_ddl`/`zt_mig` 就建在它上面。

> **git 与远端**：`git` 不在 PATH，用 GitHub Desktop 内置 git（`$env:LOCALAPPDATA\GitHubDesktop\app-3.6.4\resources\app\git\cmd\git.exe`，credential.helper=manager）。远端：`origin` = Gitee（SSH）、`github` = `https://github.com/liushisan673/z-torque.git`（公开仓库）。GitHub 直连会被 reset：**推送必须带代理** `-c http.proxy=http://127.0.0.1:12000`（系统代理，git 不自动读取；gitee SSH 不受影响）。`data/logs/*`、`data/audit-archive/*` 已入 .gitignore。

容器若不存在，重建：

```powershell
docker network create zt-net
docker run -d --name zt-pg --network zt-net -e POSTGRES_PASSWORD=pass -e POSTGRES_DB=zt postgres:16
docker run -d --name zt-test --network zt-net -v d:/project/z-torque-community-m1:/w -w /w python:3.12-slim sleep infinity
docker exec zt-test pip install -q -e ".[test]"
# 需要宿主端口时：docker run -d --name zt-pg-gate -p 55432:5432 -e POSTGRES_PASSWORD=pass postgres:16-alpine
# zt_clean/zt_ddl/zt_mig 用于迁移门禁：CREATE DATABASE <name>; 再对它跑 alembic upgrade head
```

已有测试库：`zt`（主）、`zt_clean`（干净迁移库）、`zt_ddl`/`zt_ddl2`/`zt_ddl3`（离线 DDL 验证，可删）、`zt_mig`（A 组 PG 迁移用例专用，先 `upgrade head` 再比对）。

> `ZTORQUE_TEST_DATABASE_URL` 不设会落到默认 SQLite —— 这是防误清生产库的开关，别去掉。

### 6.1 CI 门禁（`.github/workflows/ci.yml`，2026-10-24 新增）

| job | runner | 内容 |
|---|---|---|
| `pytest-sqlite` | ubuntu + SQLite | `pip install -e ".[test]"` → `pytest tests -q`；**guard 步**：日志里出现 `POSIX rlimit / killpg`（即 POSIX 沙箱用例被 skip）就 `exit 1` |
| `pytest-postgres` | ubuntu + `postgres:16` service | 设 `ZTORQUE_TEST_DATABASE_URL=postgresql+pg8000://postgres:pass@localhost:5432/zt` 跑全量 |
| `migrations` | ubuntu + 自带 `postgres:16` service，`needs: [pytest-sqlite, pytest-postgres]` | 建空库 `zt_clean`/`zt_ddl` → `alembic upgrade head` + `alembic current` + `alembic check` → `ZTORQUE_MIGRATION_CHECK_URL=zt_clean pytest tests/integration/test_alembic_migrations.py -q` → `alembic upgrade head --sql > schema.sql` 灌进 `zt_ddl` → 断言 `alembic_version == alembic heads` → 两库各跑 `scripts/check_migration_drift.py` 并 `grep -qx total=0`；`schema.sql` 上传为 artifact |
| `console` | ubuntu + node 20 | `console/` 下 `npm ci` → `npx tsc --noEmit` → `npm run build` |
| `python-static` | ubuntu | 仅 `python -m compileall -q app`（`[tool.ruff]` 只有 `line-length`、ruff 也不是依赖 → 不设 ruff 门禁） |
| `plugin-conformance` | ubuntu | **标准插件一致性**（2026-10-26）：`test_plugin_{redlines,error_codes,discovery,config_schema,manifest_contract}.py` + `tests/integration/test_plugin_minimal_assembly.py`（单插件 import 隔离走 subprocess，双 profile 装配冒烟） |
| `pytest-windows` | windows-latest | 仅 `pytest tests -q`（POSIX 沙箱用例 skip 属预期） |

要点：

- service container **不能跨 job 共享**，所以 `migrations` 自起一个 PG，用 `needs` 串在 `pytest-postgres` 之后，而不是去读 B 的容器。
- `alembic.ini::version_locations` 现有**两个**目录（`alembic/versions` + `app/builtin_plugins/audit_export_archive/migrations`），所以迁移门禁天然覆盖插件私有表；改 ini 后先跑 `alembic heads`（必须是单一 head `c7a1e4b90d23`，见 §5.15），再跑门禁。
- 顶层 `env` 固定 `JWT_SECRET_KEY` / `SECRET_STORE_KEY`（CI 专用假值，见 §5.13）；`pyproject.toml` 的 `[test]` extras 补了 `pg8000==1.31.5`，所以统一 `pip install -e ".[test]"` 就够。
- 迁移门禁的两个库都要求漂移 `total=0`：`zt_clean`（alembic 直建）与 `zt_ddl`（离线脚本建），分别验证「迁移链」和「`--sql` 脚本」。
- workflow 文件保持纯 ASCII（guard 用 skip 原因的 ASCII 前缀匹配）与 LF 换行。

本地等价验证（容器 + 一份删掉 `.env`/`.venv`/`node_modules` 的干净副本，PG host 换成容器名）：

| 检查 | 结果 |
|---|---|
| Linux + SQLite 全量 | `451 passed, 2 skipped`（A 组实测）；2 个 skip = Windows-only 用例 + 未配 `ZTORQUE_MIGRATION_CHECK_URL` |
| Linux + PG16 全量 | `451 passed, 2 skipped`（A 组实测） |
| Windows + SQLite 全量 | `491 passed, 11 skipped`（密钥轮换落地后实测，此前 484；比 Linux 少过的 1 项是 POSIX 沙箱用例；skip = Windows 沙箱 fail-closed + 未配 `ZTORQUE_MIGRATION_CHECK_URL` + 8 项 live 未开） |
| Linux + SQLite（`zt-test`） | `492 passed, 10 skipped`（密钥轮换落地后实测；此前 485） |
| Linux + PG16（`zt_suite`） | `492 passed, 10 skipped`（密钥轮换落地后实测；库须新建，conftest 会 `drop_all`） |
| A2A 真联调（`ZTORQUE_A2A_LIVE=1`） | Windows 与 Linux 容器均 `8 passed`（约 12~20s；起 uvicorn 子进程 + 真 TCP/TLS 对端） |
| 迁移门禁（`zt_clean`） | `upgrade head` → `alembic check` 输出 `No new upgrade operations detected`、head `c7a1e4b90d23`；`check_migration_drift.py` `total=0` |
| 离线 DDL（`zt_ddl`） | `upgrade head --sql` 1018 行 → 空库执行后 `alembic current` = head、drift `total=0` |
| PG 迁移用例（`zt_mig`） | 先 `upgrade head` 再跑 `tests/integration/test_alembic_migrations.py` → `3 passed`（空库直接跑会因 124 条 `add_table` 失败，属用法不对） |
| console | `tsc --noEmit` 通过、vite build 成功（修掉 `Skills.tsx` 的 `TS6133` 后） |
| compileall | 通过（`compileall_exit=0`） |
| workflow 自身 | `yaml.safe_load` 通过、job 注册齐全（含 `plugin-conformance`）、纯 ASCII、`rhysd/actionlint` 0 findings |

**真实 runner 验证（2026-10-26，run #1 全绿）**：托管 PG service 的 5432 连通与 health-check、`windows-latest` 镜像行为（POSIX 用例按预期 skip、guard 不误报）、checkout/setup-python/setup-node/upload-artifact 流程均已在真实 GitHub runner 上落地验证通过。

> 本机跑 Linux 等价验证时可用一次性容器（`--rm`，代码只读挂载后 `cp` 到 `/w`），不必依赖 §6 的常驻 `zt-test`/`zt-pg`；`git status` 不受影响。若本机 shell 带 IDE 注入的 `PYTHONPATH`（sitecustomize shim），在临时目录跑测试可能被 safe-delete 守卫打断，模拟 runner 时用 `$env:PYTHONPATH=""`。

## 7. 工作区状态（重要）

`git status` 曾有 **188 项改动未提交**（2026-10-24 快照：144 改 / 42 新增 / 2 删除，全部有效工作产物）。**2026-10-26 更新**：主体已由用户提交并推送 GitHub（master = `2e7b7a1`，含 Port 协议解耦、.gitignore 补 `data/logs/*` 与 `data/audit-archive/*`、取消跟踪运行日志）；当前未提交仅剩本文件的会话内更新。**始终禁止** `git checkout/reset/clean/stash` 破坏工作区。其中上百个 `M` 是 E 组 ruff `--fix` 的 `I001`（import 排序）/`F401`（未用导入）自动改动，**不改变行为**；看 diff 时先排除它们。A 组产生的临时文件 `offline_ddl.sql` 已删除，不要重新生成到仓库根（用 `--sql > %TEMP%` 或容器内路径）。关键新增：

```
app/kernel/audit/{reader.py,categories.py}       app/plugin_sdk/{__init__,contract,audit,errors,context,db,ports,observability}.py
app/builtin_plugins/{audit_export_archive/,a2a_gateway/,sandbox_driver/}
app/builtin_plugins/discovery.py                  app/builtin_plugins/__init__.py（PEP 562 惰性导出）
app/builtin_plugins/audit_export_archive/{models.py,migrations/f6b7c8d9e0a1_*,migrations/c7a1e4b90d23_*}
app/api/routes/{audit_exports.py,a2a.py}          app/db/alembic_support.py
app/kernel/plugin_reads.py                        alembic/versions/b3d6f9a2c5e8_*（head 已被 c7a1e4b90d23 接续）
scripts/check_migration_drift.py                  tests/support/{ddl.py,fakes.py}
app/builtin_plugins/audit_export_archive/keys.py  scripts/rotate_audit_archive_key.py（密钥轮换）
tests/unit/test_audit_archive_key_rotation.py     （密钥环/轮换 7 条）
.github/workflows/ci.yml                          （CI 门禁，见 §6.1）
tests/unit/{test_audit_reader,test_audit_export_archive,test_a2a_gateway,test_sandbox_hardening,test_plugin_redlines,test_plugin_error_codes}.py
tests/unit/{test_plugin_discovery,test_plugin_config_schema,test_plugin_manifest_contract}.py
tests/integration/{test_v13_audit_export_api,test_v13_a2a_api,test_v13_permission_seed,test_alembic_migrations,test_plugin_minimal_assembly}.py
tests/integration/test_a2a_live_peers.py          （F 组：A2A 真联调，默认 skip，`ZTORQUE_A2A_LIVE=1` 跑）
```

**已删除/迁移的路径（别按旧路径找）**：`app/models/audit_export.py`（→ 插件 `models.py`）、`app/builtin_plugins/sandbox_driver.py`（→ 同名包 `sandbox_driver/`）、`alembic/versions/f6b7c8d9e0a1_audit_export_archive_tables.py`（→ 插件 `migrations/`）。这些都在未提交集合里，`git status` 看不出删除，改动前先 `Test-Path`。

本次为 CI 顺带改的两处（非业务逻辑）：`pyproject.toml` 的 `[test]` extras 增加 `pg8000==1.31.5`；`console/src/pages/Skills.tsx` 删掉只被注释块"引用"的 `prettyJson`（否则 `tsc` 报 `TS6133`，`npm run build` 必红）。

## 8. 待办（按优先级）

**P0**
- ~~定部署形态 / 导出任务租约~~ ✅ 2026-10-26（E 组）：`zr_audit_export_job` 加 `lease_owner` / `lease_expires_at` / `attempts`（迁移 `c7a1e4b90d23`），抢占=条件 UPDATE 的 `rowcount`，续租在分片边界，丢租抛错中止，恢复只回收「无主/本实例/已过期」，终态释放。多副本可放心起；残留限制两条（租约依赖时钟同源；PENDING 不跨副本排队）见 v1.3 §12.5 第 3 条。

**P1**
- ~~建 CI~~ ✅ 2026-10-24：`.github/workflows/ci.yml`（见 §6.1）。~~**A4** 推到远端后确认首个真实 run~~ ✅ 2026-10-26：已推送 `github` 远端（master，commit `2e7b7a1`），首个真实 run 全绿（7 job 全 success，见 §3）。
- ~~ruff 门禁~~ ✅ 2026-10-26（E 组）：`[tool.ruff.lint]` 显式 `select = F,E4,E7,E9,I,B,ASYNC` + 八条带原因的 `ignore`，版本钉在 `[project.optional-dependencies].lint`；CI `python-static` 改为 `ruff check` + `compileall`；一次性清掉 169 项（161 项 `I001`/`F401` 自动，8 项手工）。
- ~~**A 组：漂移门禁 + Linux 全量 + 离线 DDL**~~ ✅ 2026-10-26：`zt_clean` 与 `zt_ddl` 双库 `total=0`、`alembic check` 无新操作；Linux+SQLite / Linux+PG16 各 `451 passed, 2 skipped`；`zt_mig` PG 迁移用例 `3 passed`（数字见 §3 与 §6.1）。连带修掉 `check_migration_drift.py` 漏 import 插件模型的假阳性，见 §5.18。
- ~~**E 组：文档收口**~~ ✅ 2026-10-26：A 组实测数字回填 §3/§6.1；`check_migration_drift.py` 修复登记 §5.18；沙箱语义变更（未注入 `sandbox.driver` → 503）登记在 v1.3 §十「变更与兼容」+《内核契约 v1》§3 脚注，明确属**升级收紧而非能力回退**。
- ~~A2A 远端真联调~~ ✅ 2026-10-26（F 组）：`tests/integration/test_a2a_live_peers.py` 起 uvicorn 子进程 + 真 TCP/TLS `ThreadingHTTPServer` 对端，8 条用例（真实例互调并真实执行技能版本、统一封装解包、4xx/超时/5xx/超限、自签 https 被拒、熔断开路）。真联调修掉两个缺陷：①`PeerRegistry._send` 不解本项目统一响应封装（真对端 100% 拿不到 `status`）；②出站载荷把**发送方命名空间**的 `params.peer` 当执行指令转发，导致两个实例互调 503/打错人 —— 改为「钉了 `skill_version_id` 就走 `route=local` 让对端执行」。残留：v1 无技能名→版本解析，跨实例必须显式给 `skill_version_id`。
- ~~`observability.sink`~~ ✅ 2026-10-26（D1 接线，不是删声明）：三个 `requires: observability.sink` 的插件全部真调用 `app.plugin_sdk.observability.emit_event`（惰性解析内核实现，**禁止**直接 `import app.kernel.registry.active`）；上报失败只 `logger.warning`，主流程照旧成功。
  - `sandbox_driver` → `sandbox.execution`
  - `audit_export_archive` → `audit_export.job`，payload 白名单 = `{job_id, format, status, rows_written, chunk_count, duration_ms}`（失败额外 `error_code`），status ∈ queued/running/succeeded/failed（重启恢复的遗留任务也补一条 failed）；常量在 `jobs.py`（`AUDIT_EXPORT_EVENT*`）。禁止放租户 id 明文 / 文件名 / 审计正文 / 脚本内容。
  - `a2a_gateway` → `a2a.outbound`，唯一收口点 `PeerRegistry.invoke`（`_send` 只管 HTTP，`invoke` 管权限→熔断→观测），payload 白名单 = `{peer, direction, result, duration_ms, response_bytes, depth}`，result ∈ ok/error/timeout/circuit_open；`depth` 由网关 `_invoke_remote` 传入（出站深度，非入站值）；常量在 `peers.py`（`A2A_OUTBOUND_EVENT*`）。禁止放 token / header 原文 / body，失败路径 `response_bytes=0`（不做响应体推断）。
  - 验收护栏：`tests/unit/test_audit_export_archive.py`、`tests/unit/test_a2a_gateway.py` 各断言「键集合精确等于白名单 + 无敏感键 + sink 抛错不影响主流程」（共 12 条）。
- ~~**标准插件阶段 2**~~ ✅ 2026-10-26：①`builtin_plugins/discovery.py`：元组兜底在前、entry_points 追加在后，重名内置优先、坏包默认跳过（`assemble(plugin_discovery_strict=True)` 改为拒绝启动）；②`config_schema` 落地为 JSON Schema（三插件已升级，其余 21 个保持历史描述形态不校验），装配期按 Settings **白名单投影**后校验类型/枚举/必填，边界语义仍归插件 `config.validate()`；③`api_version` 兼容性校验 + `extra_plugins` 重名拒绝；④`app/builtin_plugins/__init__.py` 改 PEP 562 惰性导出（否则 `import 单插件` 会拉起全部 19 个插件 + `app.models` + `app.services`）。
- ~~**标准插件遗留（阶段 2 之后，独立一期）**~~ ✅ 2026-10-26：三个 Port 协议模块（`app/kernel/ports/{identity_provider,memory_short,approval_provider}.py`）的模型 import 移入 `TYPE_CHECKING`（`from __future__ import annotations`，运行时 duck typing），import Port 协议不再连带拉起 `app.models`，单插件「零内核依赖」收口。机器探针 `tests/integration/test_plugin_minimal_assembly.py::test_port_protocols_do_not_pull_app_models`（子进程断言 `app.models` 不在 `sys.modules`）；冻结契约文件（`ports/{__init__,model_selector,specs}.py`）未动。v1.3 §12.7 已销项（v1.3.8）。

**P2**
- ~~M4：sidecar 隔离、联邦身份（mTLS）~~ ✅ 2026-10-26（v1.4）：①`ContainerSandboxDriver`（`sandbox_driver/container.py`，docker CLI 子进程，`--network none --read-only --security-opt no-new-privileges` + 资源上限，Windows/无 docker 装配期 fail-closed，label 含 container → capabilities 如实 `isolation_level=container`）；②A2A 联邦入站方案 B（`a2a_gateway/federated.py`：`A2A_FEDERATION_ENABLED` + peer `inbound_federation/tenant_id/auth_secret_name`，装配期预解密 + 租户存在性校验，命中凭据即授权 `a2a.invoke`，`user_id=None` 不撞审计外键，归属走 `detail.principal_type/caller_peer_id`）；③出站 per-peer mTLS（`client_cert/key/ca_bundle_secret_name` → SSLContext 传 httpx `verify=`——实测 `cert=(files)` 形式握手失败，PEM 临时文件 0600 用完即删）。口径见 v1.3 文档 §4.10/§6.5/§12.4/§12.5。**未做**：`object.store.list`（契约 §7 后再动）；容器 live 冒烟需有 docker 的 POSIX 主机手动开 `ZTORQUE_SANDBOX_CONTAINER_LIVE=1`。
- ~~归档密钥轮换~~ ✅ 2026-10-26：`audit_export_archive/keys.py` 新增 `ArchiveKeyRing`（**写恒用 current，读按 current → previous 降级**），配置加 `AUDIT_ARCHIVE_PREVIOUS_SECRET_REF`（只读，`Settings` 与插件 `validate()` 双入口校验：仅加密开启时允许、不得与 current 同引用）；索引侧记密钥归属——`zr_audit_archive_batch.objects[].key_id` + manifest `key_id` + 审计 detail，全部只存指纹（`sha256(派生密钥)[:16]`，不可反推），密钥材料不进索引/manifest/日志；解包全环失败抛 `AI_AUDIT_ARCHIVE_KEY_MISSING`（502，已注册错误码表 + 控制台文案），不静默跳过。脚本 `scripts/rotate_audit_archive_key.py` 三档：登记（`--new-secret-file`/`--new-secret-stdin`，加密 + 回读自检 + 打印待粘贴 env）/ 只读核对（默认）/ `--retire-old`（真读对象存储 + 真解密，只有「每个加密对象都能只用当前密钥解开」才放行），退出码 0/3/2。**不新增迁移**（key_id 落在既有 JSON 列），不动 `app/kernel/ports/*`。口径见 v1.3 §12.9；`tests/unit/test_audit_archive_key_rotation.py` 7 条。**未做**：存量对象不自动重加密（保持旧密钥加密直到过期清理）；多副本滚动需先把新密钥登记为所有副本的 previous。
- ~~归档孤儿对象对账~~ ✅ 2026-10-26：`scripts/reconcile_audit_archive_objects.py`（默认 dry-run 零写入，`--apply` 只删孤儿对象）。索引源 = `zr_audit_export_job` + `zr_audit_export_shard` + `zr_audit_archive_batch.objects`；三类输出 orphan（有键无有效任务行，含 FAILED 残留）/ dangling（SUCCEEDED 行指向的对象/ manifest 不存在）/ invalid（前缀、租户、任务、叶子不合规）。判定口径与安全边界见 v1.3 §12.8；`tests/unit/test_reconcile_audit_archive_objects.py` 14 条。**未做**：`--apply` 不删索引行（残留分片行留人工裁决）；失败批次已落盘未登记的 `part-*` 无法枚举（契约无 `list`）。
- 文档回填：v1.3 §5.8 / §6.8 验收前置项。~~`process.spawn` 需注明「仅登记不强制」~~ ✅ 2026-10-26（D3）：v1.3 §2.1 表格、§3.3 实现约定、§12.5 限制第 7 条三处写死「仅登记 / 无运行时强制点 / 不得说成已拦截」。

## 9. 已知的产品级边界

- 沙箱只支持单脚本 manifest（多脚本返回 422）；Windows 不支持脚本执行（明确 fail-closed）。
- A2A 入站仅支持方案 A（本域 JWT + `a2a.invoke` 权限），拒绝裸 `X-User-Id`；不桥接内部 Task（无 A2A 任务持久化）。
- A2A 没有「技能名 → 版本」解析（插件 DB 边界只允许 identity/discovery 查询），跨实例调用必须显式传 `skill_version_id`（出站会改写成 `route=local` 由对端执行）；只给 `skill` 名时只能 `route=remote` 让对端自行路由，两个本实例之间没有这条路径。
- 审计导出多副本安全（行级租约，见 v1.3 §5.9），但租约依赖各副本时钟同源，且 PENDING 任务只在提交副本执行；热区清理只删对象与分片，任务行保留。
- 归档加密可选（AES-256-GCM），开启必须配 `AUDIT_ARCHIVE_SECRET_REF`，明文密钥不得进配置。
