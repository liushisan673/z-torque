# 后端带日志启动脚本：日志同步写入文件，会话测试后 AI 可直接读取日志文件分析。
# 用法：在项目根目录执行  powershell -ExecutionPolicy Bypass -File scripts\run_backend.ps1
# 停止：窗口内 Ctrl+C
#
# 重要：不要点击/选中本窗口内的文字！
# Windows 控制台"快速编辑模式"下，用鼠标选中文字会冻结整个 Python 进程
# （表现为：任务卡 RUNNING、接口无响应、超时不触发），按 Esc 或回车恢复。

# 正确解码 python 输出（UTF-8），避免中文乱码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force -Path "data\logs" | Out-Null

$log = Join-Path $root ("data\logs\backend-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
Write-Host "=== Z-Torque backend ==="
Write-Host "=== log file: $log ==="
Write-Host "=== 提示：不要用鼠标选中本窗口文字（会冻结进程）；看日志直接打开日志文件 ==="

$env:PYTHONUNBUFFERED = "1"
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-use-colors 2>&1 |
    ForEach-Object {
        # stderr 行被 PowerShell 5 包装为 ErrorRecord（NativeCommandError），还原为原始日志行
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { [string]$_ }
        Write-Host $line
        Add-Content -Path $log -Value $line -Encoding UTF8
    }
