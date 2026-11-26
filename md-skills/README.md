# MD-Skill 本地运行说明

## 目录

默认目录为 `md-skills/`，可通过 `MD_SKILLS_DIR` 覆盖。

每个技能必须有一个目录和 `SKILL.md`。技能文件被发现后只会登记为草稿；只有发布的 `SkillVersion` 才允许运行。

## 扫描

启动后可通过认证接口触发扫描：

- `POST /api/z-torque/skills/scan`
- `GET /api/z-torque/skills/scan/status`
- `GET /api/z-torque/skills`
- `GET /api/z-torque/skills/{skill_id}/versions`

文件变更不会覆盖已发布版本。内容指纹变化会生成新的草稿版本，旧版本继续保持不可变。

## 脚本能力

Windows 环境默认不启用 MD-Skill 脚本执行。当前运行时无法提供可靠的进程、文件系统和网络隔离时，系统返回 `AI_CAPABILITY_UNAVAILABLE`，不会降级为任意本机执行。

脚本相关配置：

- `MD_SKILL_SCRIPT_ENABLED`
- `MD_SKILL_SCRIPT_TIMEOUT`
- `MD_SKILL_SCRIPT_MAX_OUTPUT`
- `MD_SKILL_SCRIPT_MAX_CONCURRENCY`
- `MD_SKILL_WATCH_ENABLED`
- `MD_SKILL_SCAN_INTERVAL`

Markdown 指令始终视为不可信输入，不能改变系统权限、SecurityContext 或 SkillGateway 的执行规则。


## 教辅出版流程技能包

本仓库附带 `book-publishing-workflow`、`curriculum-standards-analysis`、`field-research`、`typesetting-production`、`proofreader`、`responsible-editor-review` 和 `book-publishing-deterministic-checks`。

启动应用后调用 `POST /api/z-torque/skills/scan` 扫描，技能先进入草稿状态。审阅并发布后，再按 `docs/project-library/教辅出版Agent技能与交接规范.md` 的角色矩阵绑定到 Agent。脚本技能会在执行前要求 Task checkpoint 人工确认，并受沙箱权限约束。
