# Muika 全部服务启动脚本（后台模式）
# 用法: .\scripts\start_all.ps1

param(
    [int]$CorePort = 8765,
    [switch]$NoBot = $false
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "=== Muika-After-Story ===" -ForegroundColor Magenta

# 1. 启动 Core
Write-Host "[1/2] Starting Core (ws://127.0.0.1:${CorePort}/ws)..." -ForegroundColor Cyan
$CoreJob = Start-Process -FilePath ".\.venv\Scripts\python.exe" `
    -ArgumentList "core_main.py --host 127.0.0.1 --port $CorePort" `
    -NoNewWindow `
    -PassThru

Write-Host "  Core PID: $($CoreJob.Id)" -ForegroundColor Gray

# 等待 Core 就绪
Write-Host "  Waiting for Core to be ready..." -ForegroundColor Gray
$retries = 0
do {
    Start-Sleep -Seconds 1
    $retries++
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:${CorePort}/health" -TimeoutSec 2 -ErrorAction SilentlyContinue
    } catch {
        $health = $null
    }
} while (($null -eq $health -or $health.status -ne "ok") -and $retries -lt 15)

if ($health.status -eq "ok") {
    Write-Host "  Core is ready!" -ForegroundColor Green
} else {
    Write-Host "  Core failed to start within timeout" -ForegroundColor Red
    Stop-Process -Id $CoreJob.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

# 2. 启动 Bot (NoneBot)
if (-not $NoBot) {
    Write-Host "[2/2] Starting Bot..." -ForegroundColor Cyan
    $BotJob = Start-Process -FilePath ".\.venv\Scripts\python.exe" `
        -ArgumentList "bot.py" `
        -NoNewWindow `
        -PassThru
    Write-Host "  Bot PID: $($BotJob.Id)" -ForegroundColor Gray
}

Write-Host "=== All services started ===" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop all services."

# 等待 Ctrl+C
try {
    while ($true) { Start-Sleep -Seconds 1 }
} finally {
    Write-Host "Shutting down..." -ForegroundColor Yellow
    Stop-Process -Id $CoreJob.Id -Force -ErrorAction SilentlyContinue
    if ($BotJob) { Stop-Process -Id $BotJob.Id -Force -ErrorAction SilentlyContinue }
    Write-Host "All services stopped." -ForegroundColor Yellow
}
