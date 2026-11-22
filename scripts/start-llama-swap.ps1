# 启动本地 AI 调度器 llama-swap 和 IrisAI 网页服务（开机自启也调用这个脚本）
# 密钥从 config\api_key.txt 读取，不写进配置文件。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot      # 项目根目录（本脚本在 scripts 里），不写死本机路径
$env:LLAMA_SWAP_KEY = (Get-Content "$root\config\api_key.txt" -Raw).Trim()

New-Item -ItemType Directory -Force "$root\logs" | Out-Null

# 直接连一下端口判断服务在不在（Get-NetTCPConnection 偶尔查不到）
function Test-Port([int]$port) {
    $c = New-Object Net.Sockets.TcpClient
    try { $c.Connect('127.0.0.1', $port); $true } catch { $false } finally { $c.Close() }
}

# 网页服务（端口 8088），后台运行、不显示窗口
if (-not (Test-Port 8088)) {
    Start-Process -FilePath "$root\hub\.venv\Scripts\python.exe" -ArgumentList "`"$root\hub\server.py`"" `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardError "$root\logs\hub.log" -RedirectStandardOutput "$root\logs\hub.out.log"
}

if (Test-Port 8080) {
    Write-Host '端口 8080 已被占用，llama-swap 可能已经在运行。'
    exit 0
}

# 按模型清单 config\models.yaml 重新生成调度配置（没变就不写；清单有错时保留旧配置照常启动，原因记进日志）
if (Test-Path "$root\config\models.yaml") {
    & "$root\hub\.venv\Scripts\python.exe" "$root\scripts\gen_models.py" *>> "$root\logs\llama-swap.log"
}

# 调度器程序：bin 下面名字最新的 llama-swap*\llama-swap.exe
$swap = Get-ChildItem "$root\bin\llama-swap*\llama-swap.exe" | Sort-Object { $_.Directory.Name } -Descending | Select-Object -First 1
if (-not $swap) { Write-Host "没找到 bin\llama-swap*\llama-swap.exe"; exit 1 }

& $swap.FullName `
    -config "$root\config\llama-swap.yaml" `
    -listen '0.0.0.0:8080' `
    -watch-config *>> "$root\logs\llama-swap.log"
