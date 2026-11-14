# setup_back_network.ps1：後台（這台）一次設好雙機連線——角色設成後台、位址自動找、防火牆只放行網路線直連那一段，
# 另外開一條讓同網段手機連得進 QR 註冊頁（TCP 5000）的規則。
# 要用系統管理員執行（由「設定後台網路.bat」呼叫）。不改任何網卡設定，Wi-Fi 與上網不受影響。
# 做的事：①角色設成 back（綁定位址 auto）②清掉所有「SmartAccess 後台」開頭的舊規則 ③建立下面兩條入站規則（只放行 169.254.0.0/16）
#        ④建立 QR 註冊規則（TCP 5000，只放行 LocalSubnet＝和這台同網段的手機，所有網路類別）
# 規則名稱要和 admin\network_check.py 一致（檢查程式靠名稱找規則）。重複執行不會留下重複或過期的規則。
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "找不到 .venv，請在專案根目錄使用。" }
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "要用系統管理員身分執行（右鍵「設定後台網路.bat」→ 以系統管理員身分執行），否則建立不了防火牆規則。"
}
$env:PYTHONUTF8 = "1"
& $py -m shared.role_config set --role back --bind-host auto
if ($LASTEXITCODE -ne 0) { throw "角色設定失敗" }
# 連線埠用設定檔裡的值（預設 8770），規則才會跟著對
$port = [int](& $py -c "from shared import role_config; print(role_config.load().back_port)")
if ($LASTEXITCODE -ne 0 -or $port -lt 1024) { throw "讀不到後台連線埠設定" }
$rules = @(
    @{ Name = "SmartAccess 後台連線（只允許網路線直連）"; Protocol = "TCP"; Port = $port },
    @{ Name = "SmartAccess 後台尋找回應（只允許網路線直連）"; Protocol = "UDP"; Port = 8772 }
)
$old = Get-NetFirewallRule -DisplayName "SmartAccess 後台*" -ErrorAction SilentlyContinue
if ($old) { Write-Host "移除舊的後台規則 $(@($old).Count) 條。"; $old | Remove-NetFirewallRule }
foreach ($r in $rules) {
    New-NetFirewallRule -DisplayName $r.Name -Direction Inbound -Protocol $r.Protocol -LocalPort $r.Port `
        -RemoteAddress "169.254.0.0/16" -Action Allow | Out-Null
    Write-Host ("已建立規則：{0}（入站允許 {1} {2}，只放行 169.254.0.0/16）" -f $r.Name, $r.Protocol, $r.Port) -ForegroundColor Green
}
$qrRule = "SmartAccess 後台 QR 註冊（區網手機）"
New-NetFirewallRule -DisplayName $qrRule -Direction Inbound -Protocol TCP -LocalPort 5000 `
    -RemoteAddress LocalSubnet -Profile Any -Action Allow | Out-Null
Write-Host ("已建立規則：{0}（入站允許 TCP 5000，只放行和這台同網段的手機 LocalSubnet）" -f $qrRule) -ForegroundColor Green
Write-Host "完成：後台角色已設定，連線服務只放行網路線直連那一段（169.254.0.0/16），QR 註冊只放行同網段的手機。" -ForegroundColor Green
Write-Host "通行碼請用環境變數 SMART_ACCESS_LINK_TOKEN 設定（和前台同一組）；接著開後台介面（會自動啟動連線服務）。"
Write-Host "之後可雙擊「檢查後台網路.bat」確認角色、通行碼、網路線位址、連線服務與防火牆規則。"
