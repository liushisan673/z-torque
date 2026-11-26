# Z-Torque 沙箱执行与审批说明

## 1. 执行范围

社区版通过 `sandbox.driver` Port 执行 MD-Skill 的 Python 脚本。内核负责权限、审批、Task Checkpoint 和审计；具体进程/容器启动由 `sandbox_driver` 插件负责。

当前 MD-Skill 脚本执行约束：
- 一个 MD-Skill 版本只允许声明一个 `.py` 脚本入口。
- 输入通过标准输入传递 JSON，脚本必须通过标准输出返回 JSON 对象。
- 脚本输出必须符合 Skill 的 `output_schema`。
- 执行有超时、标准输出字节数、路径白名单和 POSIX 资源限制。
- 未装配 `sandbox.driver`、脚本能力关闭、路径越界或隔离能力不匹配时，必须 fail-closed。

## 2. 执行前审批

所有通过 SkillGateway 发起的 `md_script` 执行，在所有操作系统上都必须先获得人工批准：
1. Task 暂停为 `WAITING_HUMAN`。
2. 创建 Confirmation 和 TaskCheckpoint，冻结 Skill 版本、脚本文件指纹、输入参数和 SandboxScope。
3. 当前执行位释放，Worker 可继续处理其他任务。
4. 其他有权限的用户批准后，任务进入队列，从 checkpoint 恢复。
5. 恢复时重新校验脚本版本、文件指纹、入口文件、原始输入和 SandboxScope；任一变化都拒绝执行，必须重新发起任务与审批。
6. 拒绝后任务进入 `CANCELLED`，不会启动脚本。

社区版由租户管理员审批；发起人不能审批自己的请求，管理员也不能绕过该规则。管理员可从「确认记录」查看并审批其他用户的待办。审批只授权本次冻结的调用，不会扩大沙箱路径、权限或资源边界。

会话页面在执行链路中自动展开审批卡片，并展示脚本名称、入口文件、隔离方式和范围；不向前端返回脚本正文、原始输入参数或绝对路径。

## 3. 隔离模式

### process

- 通过独立 Python 解释器（隔离模式）运行。
- 使用最小环境变量、Skill 工作目录和路径白名单。
- POSIX 可启用 CPU、地址空间、文件大小、进程数量等 rlimit。
- 超时/输出超限会终止执行进程及其进程组。

**安全边界说明：** process 是受限的独立进程，不是完整操作系统沙箱。它仍以 Z-Torque 服务进程的操作系统用户身份运行，不能单独提供网络隔离或对该用户可访问文件系统的强隔离。人工审批也不能替代操作系统隔离。

### container

- 仅在支持的 Linux 环境启用，启动前检查 Docker CLI 和 Daemon。
- 容器使用 `--network none`、只读根文件系统、`no-new-privileges`、丢弃 Linux capabilities。
- 工作目录按策略挂载，临时目录使用受限 tmpfs。
- 配置内存、CPU 和 PID 上限。
- 容器不可用时启动失败，不允许静默回退到 process。

`sandbox.full` 工作区级权限必须使用 container；process 模式不承接 full scope。

### Windows

- 当前 Windows 使用 process 模式，不支持本项目定义的 container 隔离语义。
- 必须同时满足全局 `MD_SKILL_WINDOWS_PROCESS_ENABLED` 开关和 Skill 的 `windows_trusted: true`。
- 每次脚本执行仍必须人工审批。
- Windows 没有 POSIX rlimit；因此不能把 Windows process 模式描述为完整 OS 级资源隔离。
- 对不可信脚本、未声明可信的 Skill 或关闭总开关的环境，执行必须拒绝。

## 4. 配置项

| 配置 | 默认值 | 作用 |
|---|---:|---|
| `MD_SKILL_SCRIPT_ENABLED` | true | 是否启用 MD-Skill 脚本能力 |
| `MD_SKILL_WINDOWS_PROCESS_ENABLED` | true | Windows process 执行总开关 |
| `MD_SKILL_SCRIPT_TIMEOUT` | 10 秒 | 单次脚本超时 |
| `MD_SKILL_SCRIPT_MAX_OUTPUT` | 65536 字节 | 标准输出上限 |
| `SANDBOX_ISOLATION_MODE` | process | process / container |
| `SANDBOX_ALLOWED_ROOTS` | MD_SKILLS_DIR | 沙箱路径白名单 |
| `SANDBOX_RLIMIT_ENABLED` | true | POSIX rlimit 开关 |
| `SANDBOX_MAX_PROCESSES` | 64 | POSIX 进程数上限 |
| `SANDBOX_MAX_FILE_BYTES` | 16 MiB | POSIX 单文件大小上限 |

Container 模式还需要配置镜像、内存、CPU、PID 上限和 Docker CLI。镜像需要提前准备，运行期不自动拉取。

## 5. 推荐部署

- Windows 单机 EXE：仅执行可信 Skill，保留人工审批，使用 restricted scope；不要把 process 描述为强隔离。
- Linux 本地/服务器：普通可信脚本可使用 process；执行不可信代码或需要更强边界时使用 container。
- 企业部署：优先容器隔离，并将脚本目录、业务数据目录、密钥目录分离；不要把凭证注入脚本环境。
- 不要通过扩大 `SANDBOX_ALLOWED_ROOTS` 来解决脚本文件访问问题；优先将输入输出设计为显式参数和结果。

## 6. 验收重点

- 所有平台的 MD-Skill 执行都先进入人工确认。
- 未经批准不产生脚本副作用。
- 发起人自审被服务端拒绝。
- 批准后仅恢复完全一致的脚本指纹、入口、参数和 SandboxScope。
- 拒绝、取消、超时、输出超限和资源超限均能收敛任务状态并留下审计记录。
- 容器缺失/启动失败时不回退到 process。
- UI 不泄露脚本正文、原始参数、绝对路径或凭证。
