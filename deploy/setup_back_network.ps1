# setup_back_network.ps1：後台（這台）一次設好雙機連線——角色設成後台、位址自動找、防火牆只放行網路線直連那一段。
# 要用系統管理員執行（由「設定後台網路.bat」呼叫）。不改任何網卡設定，Wi-Fi 與上網不受影響。
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "找不到 .venv，請在專案根目錄使用。" }
$env:PYTHONUTF8 = "1"
& $py -m shared.role_config set --role back --bind-host auto
if ($LASTEXITCODE -ne 0) { throw "角色設定失敗" }
foreach ($r in @(@("SmartAccess 後台連線（只允許網路線直連）", "TCP", 8770), @("SmartAccess 後台尋找回應（只允許網路線直連）", "UDP", 8772))) {
    Get-NetFirewallRule -DisplayName $r[0] -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $r[0] -Direction Inbound -Protocol $r[1] -LocalPort $r[2] `
        -RemoteAddress "169.254.0.0/16" -Action Allow | Out-Null
}
Write-Host "完成：防火牆只放行 169.254.0.0/16 的 TCP 8770 與 UDP 8772。" -ForegroundColor Green
Write-Host "通行碼請用環境變數 SMART_ACCESS_LINK_TOKEN 設定（和前台同一組）；接著執行 python -m admin.link_server。"
