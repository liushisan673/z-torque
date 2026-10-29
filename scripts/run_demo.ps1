# Z-Torque 演示环境一键启动（Docker Compose）
# 用法（仓库根目录）：
#   powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1            # 构建并启动
#   powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1 -Port 8080 # 换宿主端口
#   powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1 -Stop      # 停止并保留数据
#   powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1 -Reset     # 清空容器卷后重来（重新导入宿主 data\ 的库）
#   powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1 -Logs      # 跟踪容器日志
param(
    [int]$Port = 8000,
    [switch]$Stop,
    [switch]$Reset,
    [switch]$Logs
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Fail([string]$Message) {
    Write-Host ""
    Write-Host "[失败] $Message" -ForegroundColor Red
    exit 1
}

# ---------- docker 可用性 ----------
docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Fail "Docker 守护进程未运行。请先启动 Docker Desktop（等待鲸鱼图标变绿）后重试。"
}

if ($Stop) {
    docker compose down
    Write-Host "[完成] 容器已停止，容器卷 zt_data 中的数据保留。" -ForegroundColor Green
    exit 0
}

if ($Reset) {
    # 删卷：下次启动时用宿主 data\ 里的库重新导入（宿主改了数据但容器没刷新时用这个）
    docker compose down -v
    Write-Host "[完成] 容器与数据卷已清除；重新执行本脚本即从 data\ 重新导入。" -ForegroundColor Green
    exit 0
}

if ($Logs) {
    docker compose logs -f --tail 100
    exit 0
}

# ---------- 随机密钥 ----------
function New-RandomKey {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    return ([Convert]::ToBase64String($bytes)).Replace('+', '-').Replace('/', '_')
}

function Read-EnvFile([string]$Path) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    Get-Content -LiteralPath $Path -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $idx = $line.IndexOf("=")
            $map[$line.Substring(0, $idx).Trim()] = $line.Substring($idx + 1).Trim()
        }
    }
    return $map
}

# ---------- 生成 .env.demo ----------
$demoEnv = Join-Path $root ".env.demo"
$exampleEnv = Join-Path $root ".env.demo.example"
if (-not (Test-Path -LiteralPath $demoEnv)) {
    if (-not (Test-Path -LiteralPath $exampleEnv)) { Fail "缺少 .env.demo.example，无法生成演示环境变量。" }
    Copy-Item -LiteralPath $exampleEnv -Destination $demoEnv
    Write-Host "[生成] .env.demo（首次创建）"
}

$content = Get-Content -LiteralPath $demoEnv -Encoding UTF8
$generated = @{}
foreach ($key in @("JWT_SECRET_KEY", "SECRET_STORE_KEY")) {
    $pattern = "^\s*$key\s*=\s*(CHANGE_ME|replace-.*)?\s*$"
    if ($content -match $pattern) {
        $generated[$key] = New-RandomKey
        $content = $content -replace $pattern, "$key=$($generated[$key])"
    }
}

# 本机 .env 里的模型密钥带入演示环境，避免现场手工填（.env.demo 已 gitignore）
$localEnv = Read-EnvFile (Join-Path $root ".env")
foreach ($key in @("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME")) {
    if (-not $localEnv.ContainsKey($key) -or -not $localEnv[$key]) { continue }
    $value = $localEnv[$key]
    if ($content -match "^\s*$key\s*=\s*$") {
        $content = $content -replace "^\s*$key\s*=\s*$", "$key=$value"
        Write-Host "[带入] $key（来自 .env）"
    }
}

if ($generated.Count -gt 0) {
    Set-Content -LiteralPath $demoEnv -Value $content -Encoding UTF8
    Write-Host "[生成] 随机密钥：$($generated.Keys -join ', ')"
}

$demoMap = Read-EnvFile $demoEnv
if (-not $demoMap["MODEL_API_KEY"]) {
    Write-Host "[警告] .env.demo 中 MODEL_API_KEY 为空：容器能启动，但对话调用模型会失败。" -ForegroundColor Yellow
    Write-Host "       填好密钥后重新执行本脚本即可。" -ForegroundColor Yellow
}
if ($demoMap["JWT_SECRET_KEY"] -eq "CHANGE_ME" -or $demoMap["SECRET_STORE_KEY"] -eq "CHANGE_ME") {
    Fail ".env.demo 中仍有 CHANGE_ME 密钥，请先替换（或删除该文件让脚本重新生成）。"
}

# ---------- 端口占用预检 ----------
# 本地若已有 dev 后端占着 8000，容器会绑定失败，而宿主端 health 检查会打到那个旧进程上
# （看起来"就绪"其实根本没起容器），所以必须提前拦掉。
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
    $foreign = $listeners | Where-Object {
        $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
        $p -and $p.ProcessName -notmatch "docker"
    }
    if ($foreign) {
        $names = ($foreign | ForEach-Object {
                $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
                "$($p.ProcessName)(PID $($_.OwningProcess))"
            }) -join ", "
        Fail "宿主端口 $Port 已被占用：$names。先停掉它，或改用 -Port 8080。"
    }
}

# ---------- 构建并启动 ----------
$env:DEMO_PORT = "$Port"
Write-Host "[1/3] 构建镜像（首次约 3-6 分钟，前端在镜像内构建）..." -ForegroundColor Cyan
docker compose build
if ($LASTEXITCODE -ne 0) { Fail "镜像构建失败，请检查上方日志。" }

Write-Host "[2/3] 启动容器..." -ForegroundColor Cyan
docker compose up -d
if ($LASTEXITCODE -ne 0) { Fail "容器启动失败，请检查上方日志。" }

# ---------- 等待就绪 ----------
# 就绪判据取自容器自身的 HEALTHCHECK（容器内 curl /health），不依赖宿主端口，
# 避免宿主端口被别的进程占用时误判成功。
Write-Host "[3/3] 等待后端就绪（迁移 + 初始化管理员 + 端口监听）..." -ForegroundColor Cyan
$url = "http://127.0.0.1:$Port"
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    $state = docker inspect -f "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}" z-torque-demo 2>$null
    if ($state -match "^exited") {
        Write-Host ""
        Write-Host "[失败] 容器已退出，最近日志：" -ForegroundColor Red
        docker compose logs --tail 60
        exit 1
    }
    if ($state -match "\|healthy$") { $ready = $true; break }
}
if (-not $ready) {
    Write-Host ""
    Write-Host "[失败] 120 秒内未就绪，最近日志：" -ForegroundColor Red
    docker compose logs --tail 60
    exit 1
}

# 宿主端口上是否真的能拿到控制台页面（SERVE_CONSOLE 生效 + 端口映射正确）
try {
    $index = Invoke-WebRequest -UseBasicParsing "$url/" -TimeoutSec 5
    $served = $index.Content -match "<div id=`"root`">"
} catch {
    $served = $false
}
if (-not $served) {
    Write-Host "[警告] 宿主 $url 未返回控制台页面：端口可能被其它进程占用，或 SERVE_CONSOLE/构建产物有问题。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "================ Z-Torque 演示环境已就绪 ================" -ForegroundColor Green
Write-Host "  控制台:   $url"
Write-Host "  API 文档: $url/docs"
Write-Host "  管理员:   $($demoMap['ADMIN_INITIAL_USERNAME'] -replace '^$', 'admin') / $($demoMap['ADMIN_INITIAL_PASSWORD'])"
Write-Host "  装配档位: $($demoMap['ASSEMBLY_PROFILE'])"
Write-Host "  停止:     powershell -File scripts\run_demo.ps1 -Stop"
Write-Host "=========================================================" -ForegroundColor Green
if (Test-Path -LiteralPath (Join-Path $root "data\z_torque.db")) {
    Write-Host "[重要] 宿主 data\z_torque.db 会被整份导入容器卷，此时 admin 口令沿用该库原密码。" -ForegroundColor Yellow
    Write-Host "       上面显示的初始口令只在空库首次启动时生效，验收前请确认你记得原口令。" -ForegroundColor Yellow
}
Start-Process $url
