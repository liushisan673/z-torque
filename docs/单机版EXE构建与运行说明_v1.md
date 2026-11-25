# Z-Torque 单机版 EXE 构建与运行说明 v1

## 1. 构建

要求：
- Windows 10/11 x64
- Python 3.12
- Node.js（与 Console lockfile 兼容）
- npm

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
```

构建产物：

```text
dist/
└── ZTorque/
    ├── ZTorque.exe
    └── _internal/
        ├── app/
        ├── alembic/
        └── console/
```

**分发时必须提供完整的 `dist/ZTorque` 目录**，不能只复制 `ZTorque.exe`。目录可压缩为 ZIP 提供给最终用户。EXE 是 onedir 形态，避免 onefile 每次启动/调用脚本都重新解压整套运行时。

## 2. 首次启动

双击 `ZTorque.exe`：

1. 启动器在当前 Windows 用户的 LocalAppData 下创建数据目录。
2. 自动生成 JWT 密钥、SecretStore Fernet 密钥和管理员引导密码。
3. 执行 Alembic `upgrade head`。
4. 初始化管理员账号。
5. 启动本地 FastAPI 服务并打开浏览器。

首次管理员凭据写入：

```text
%LOCALAPPDATA%\ZTorque\first-run-admin.txt
```

请首次登录后修改密码，并妥善保管凭据文件。

## 3. 数据目录

所有用户可变数据都放在：

```text
%LOCALAPPDATA%\ZTorque\
├── z_torque.db       # 主 SQLite 数据库
├── files/            # 上传文件和知识库源文件
├── secrets/          # SecretStore 加密数据
├── md-skills/        # 用户可维护的 MD-Skill
├── logs/             # 运行日志（滚动保留）
├── backups/          # 备份目标目录
├── desktop.env       # 本机密钥和路径配置
└── first-run-admin.txt
```

不要删除 `desktop.env` 或 `secrets/`，否则原有密钥可能无法解密。备份时应在应用停止后同时备份 `z_torque.db`、`files/`、`secrets/` 和 `desktop.env`。不要只复制 SQLite 主数据库而遗漏 WAL 相关文件；应用停止后再复制最安全。

## 4. 数据库与依赖

- 主应用数据库默认 SQLite，不需要安装 PostgreSQL、MySQL、Redis 或 Docker。
- Alembic 迁移在每次启动时执行。
- MCP、REST 和 DB Connector 是可选外部集成；只有用户配置连接时才访问对应服务。
- 知识库使用 SQLite FTS5；若当前 Python SQLite 构建不支持 FTS5，会使用 LIKE 降级检索。
- 当前知识检索是词法检索和中文片段补召回，不宣称具备向量语义检索能力。

## 5. Sandbox 安全边界

Windows 上 process 模式不具备 POSIX rlimit，也不是容器级隔离：
- 仅允许 frontmatter 显式声明 `windows_trusted: true` 的技能。
- 每次执行必须经过人工确认。
- 脚本根路径必须在允许目录内。
- 超时使用 Windows `taskkill /T /F` 清理进程树。
- 不可信执行和 `full` scope 仍不可用；不能将 Windows process 模式描述为强安全沙箱。

## 6. 网络与本机安全

- 服务默认只绑定 `127.0.0.1`，不对局域网或公网开放。
- 不要通过端口映射、反向代理或修改监听地址将未加固的本机实例直接暴露到公网。
- 日志目录可能包含执行元数据；密钥明文不会写入普通执行日志。
- EXE 更新时先停止应用，备份数据目录，再替换整个 `dist/ZTorque` 目录。

## 7. 当前限制

- 尚未提供自动升级器和代码签名。
- 备份/恢复目前是操作约定，自动化恢复工具仍需补充。
- Windows process sandbox 不是强隔离。
- 真实模型调用需要用户在应用中配置模型服务与凭据。
