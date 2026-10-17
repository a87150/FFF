<#
  一键启动脚本（Windows PowerShell）
  用法：
    powershell -ExecutionPolicy Bypass -File .\run.ps1
    powershell -ExecutionPolicy Bypass -File .\run.ps1 -Port 8080 -MediaDir "D:\Videos"

  说明：
    - 自动查找 Python（python / py）
    - 自动创建虚拟环境 .venv 并安装依赖（首次运行需要联网）
    - 自动检查 FFmpeg 文件夹下的 ffmpeg.exe / ffprobe.exe
    - Ctrl+C 即可停止服务
#>
param(
    [int]$Port = 0,
    [string]$MediaDir = ""
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "==========================================" -ForegroundColor DarkCyan
Write-Host "  FFmpeg Web 转码工具 - 一键启动 (Windows)" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor DarkCyan

# ---------- 1. 查找可用的 Python ----------
$pythonCmd = $null
foreach ($cand in @("python", "py")) {
    if (-not (Get-Command $cand -ErrorAction SilentlyContinue)) { continue }
    & $cand --version 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $pythonCmd = $cand; break }
}
if (-not $pythonCmd) {
    Write-Host "[错误] 未找到可用的 Python，请安装 Python 3.8+ 并勾选 'Add python.exe to PATH'。" -ForegroundColor Red
    Write-Host "       下载：https://www.python.org/downloads/"
    exit 1
}
Write-Host "[1/4] 使用 Python：$pythonCmd"

# ---------- 2. 准备虚拟环境 ----------
$venvDir = Join-Path $PSScriptRoot ".venv"
$venvPy = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "[2/4] 创建虚拟环境 .venv ..."
    if ($pythonCmd -eq "py") { & py -3 -m venv $venvDir } else { & python -m venv $venvDir }
    if (-not (Test-Path $venvPy)) {
        Write-Host "[错误] 虚拟环境创建失败，请检查 Python 安装是否完整。" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "[2/4] 虚拟环境已存在，跳过创建"
}

# ---------- 3. 安装依赖 ----------
& $venvPy -c "import flask" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[3/4] 安装依赖中（首次运行需要联网，请稍候）..."
    & $venvPy -m pip install --upgrade pip --disable-pip-version-check -q
    if (Test-Path (Join-Path $PSScriptRoot "requirements.txt")) {
        & $venvPy -m pip install -r (Join-Path $PSScriptRoot "requirements.txt") --disable-pip-version-check
    } else {
        & $venvPy -m pip install flask --disable-pip-version-check
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] 依赖安装失败。国内网络可手动安装后重试：" -ForegroundColor Red
        Write-Host "  .\.venv\Scripts\python.exe -m pip install flask -i https://pypi.tuna.tsinghua.edu.cn/simple"
        exit 1
    }
} else {
    Write-Host "[3/4] 依赖已安装，跳过"
}

# ---------- 4. 检查 FFmpeg 并启动 ----------
$hasFfmpeg = (Test-Path (Join-Path $PSScriptRoot "FFmpeg\ffmpeg.exe")) -or (Test-Path (Join-Path $PSScriptRoot "ffmpeg.exe"))
if (-not $hasFfmpeg) {
    Write-Host "[警告] FFmpeg 文件夹里没有 ffmpeg.exe / ffprobe.exe，将尝试使用系统 PATH 中的 FFmpeg。" -ForegroundColor Yellow
    Write-Host "       下载：https://github.com/BtbN/FFmpeg-Builds/releases"
    Write-Host "       解压 win64-gpl.zip 后把 bin 里的 ffmpeg.exe、ffprobe.exe 复制到 FFmpeg 文件夹即可。"
}

if ($Port -gt 0) { $env:PORT = "$Port" }
if ($MediaDir) { $env:MEDIA_DIR = $MediaDir }

$listenPort = if ($env:PORT) { $env:PORT } else { "5000" }
Write-Host "[4/4] 启动服务：http://localhost:$listenPort" -ForegroundColor Green
Write-Host "      局域网内可用 http://本机IP:$listenPort 访问（Ctrl+C 停止）"
Write-Host ""

& $venvPy (Join-Path $PSScriptRoot "app.py")
