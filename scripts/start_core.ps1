# Muika Core 独立启动脚本
# 用法: .\scripts\start_core.ps1 [-Port 8765]

param(
    [string]$HostAddr = "127.0.0.1",
    [int]$Port = 8765,
    [string]$DataDir = "data"
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

Set-Location $ProjectRoot

Write-Host "Starting Muika Core on ws://${HostAddr}:${Port}/ws ..." -ForegroundColor Cyan

.\.venv\Scripts\python.exe core_main.py `
    --host $HostAddr `
    --port $Port `
    --data-dir $DataDir

Write-Host "Muika Core stopped." -ForegroundColor Yellow
