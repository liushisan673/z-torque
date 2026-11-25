# Windows 单机版打包

Z-Torque 的 Windows 桌面交付采用 **PyInstaller onedir**：用户双击 `Z-Torque.exe` 启动，但分发时必须保留整个 `dist/Z-Torque/` 目录（建议压缩成 ZIP 或后续制作安装程序），不能只拷贝 exe。

## 构建环境

- Windows 10/11 x64
- CPython 3.12.x x64
- Node.js + npm
- 可访问 python.org（仅构建时下载 Python embeddable runtime）

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

脚本会依次构建 Console、准备独立 MD-Skill Python runtime、安装 PyInstaller、生成桌面目录。

构建产物：

```text
dist/
  Z-Torque/
    Z-Torque.exe
    _internal/
      console/dist/
      alembic/
      runtime/python.exe
      ...
```

## 用户首次启动

- 双击 `Z-Torque.exe`。
- 启动器自动执行 Alembic 数据库迁移、初始化默认租户和管理员，然后启动本机 Web 服务并打开浏览器。
- 首次启动的管理员账号和随机初始密码写入数据目录下的 `FIRST_RUN_CREDENTIALS.txt`。
- 首次登录后立即修改密码，并删除该凭据文件。

## 用户数据目录

默认位于：

```text
%LOCALAPPDATA%\Z-Torque\
  z_torque.db
  files/
  secrets/
  md-skills/
  logs/z-torque.log
  runtime-secrets.json
```

可通过环境变量 `ZT_DATA_DIR` 指定其他数据目录。相对 SQLite URL 在冻结程序中也会锚定到数据目录，不依赖启动时的当前工作目录。

## 运行边界

- SQLite 是默认本地业务数据库，启用 WAL、busy timeout 和单机任务并发上限。
- MD-Skill 使用随包分发的独立 Python embeddable runtime；不会错误地把 `Z-Torque.exe` 当作 Python 解释器。
- Windows 进程超时清理使用 `taskkill /T /F` 尽可能清理子进程树；Windows 仍不具备 POSIX rlimit 或容器级强隔离，脚本能力继续受可信声明、用户确认和路径白名单约束。
- 本地单机部署不等于离线 AI。调用云模型、远程 MCP、REST API 或企业数据库仍需要网络和对应凭据。
- 用户数据与密钥保存在 LOCALAPPDATA，不放进 exe 或 PyInstaller 临时目录。
