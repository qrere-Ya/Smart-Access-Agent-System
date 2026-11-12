# install_front.ps1：前台電腦一鍵安裝（由「安裝前台.bat」呼叫）。
# 步驟：核對檔案完整 → 找 Python 3.11 → 建 .venv → 裝套件（有 NVIDIA 顯卡裝 GPU 版）→ 防火牆（可略過，位址自動找）
#       → 通行碼（只存使用者環境變數）→ 角色設定 → 桌面捷徑 → 自我檢查。
# 可重複執行：已完成的步驟會略過或詢問。-NetworkOnly 只做網路設定（要用系統管理員執行）。
param([switch]$NetworkOnly)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$Root = $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$TokenEnv = "SMART_ACCESS_LINK_TOKEN"
$env:PYTHONUTF8 = "1"

function Step($n, $text) { Write-Host ""; Write-Host "=== [$n] $text ===" -ForegroundColor Cyan }
function Ask($question, $default = "Y") {
    $suffix = if ($default -eq "Y") { "[Y/n]" } else { "[y/N]" }
    $answer = (Read-Host "$question $suffix").Trim()
    if ($answer -eq "") { return $default -eq "Y" }
    return $answer -match "^[Yy]"
}
function Run($exe, $arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "指令失敗（結束碼 $LASTEXITCODE）：$exe $($arguments -join ' ')" }
}
function IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Set-FirewallRules {
    # 兩台用網路線直連、網卡維持「自動取得 IP」：Windows 會各自分配 169.254.x.x，程式自動互相找到，不用手動設 IP；
    # 前台不設閘道也不改任何網卡設定。這裡只建防火牆規則，並且只放行 169.254.0.0/16（網路線直連那一段）。
    if (-not (IsAdmin)) {
        Write-Host "目前不是系統管理員，略過防火牆。之後在「設定前台網路.bat」上按右鍵 → 以系統管理員身分執行。" -ForegroundColor Yellow
        return
    }
    # 前台不開任何對外服務（狀態由前台主動回報給後台），舊版安裝留下的 8771 規則順便移除
    Get-NetFirewallRule -DisplayName "SmartAccess 前台狀態（只允許網路線直連）" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    # 探測回覆只允許給前台的 Python（虛擬環境的啟動器與實際執行的基底直譯器各一條），不開放給其他程式
    $programs = @($VenvPy)
    if (Test-Path $VenvPy) {
        $base = (& $VenvPy -c "import sys; print(sys._base_executable)" 2>$null)
        if ($base -and (Test-Path $base) -and ($programs -notcontains $base)) { $programs += $base }
    }
    $n = 0
    foreach ($program in $programs) {
        $n++
        $rule2 = "SmartAccess 前台尋找後台回覆 $n（只允許網路線直連與前台 Python）"
        Get-NetFirewallRule -DisplayName $rule2 -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        New-NetFirewallRule -DisplayName $rule2 -Direction Inbound -Protocol UDP -RemoteAddress "169.254.0.0/16" `
            -RemotePort 8772 -Program $program -Action Allow | Out-Null
    }
    Write-Host "已建立防火牆規則：只放行網路線直連那一段（169.254.x.x）。" -ForegroundColor Green
    Write-Host "網路線兩端的網卡請維持「自動取得 IP」（預設就是）。位址分配約需 30 到 60 秒。"
}

if ($NetworkOnly) {
    Set-FirewallRules
    Read-Host "完成，按 Enter 結束"
    return
}

Write-Host "Smart Access 前台一鍵安裝" -ForegroundColor Green
Write-Host "安裝位置：$Root"

# --- 1. 核對檔案 ---
Step 1 "核對檔案是否完整"
$manifestPath = Join-Path $Root "MANIFEST.json"
if (-not (Test-Path $manifestPath)) { throw "找不到 MANIFEST.json，安裝包不完整，請重新複製。" }
$manifest = Get-Content $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$bad = @()
foreach ($entry in $manifest.files.PSObject.Properties) {
    $file = Join-Path $Root $entry.Name
    if (-not (Test-Path -LiteralPath $file)) { $bad += "缺檔：$($entry.Name)"; continue }
    if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower() -ne $entry.Value) { $bad += "內容不符：$($entry.Name)" }
}
if ($bad.Count -gt 0) { $bad | Select-Object -First 20 | ForEach-Object { Write-Host $_ -ForegroundColor Red }; throw "檔案不完整或複製時損壞（共 $($bad.Count) 項），請重新複製安裝包。" }
Write-Host "$(@($manifest.files.PSObject.Properties).Count) 個檔案核對通過。"

# --- 2. Python 3.11 ---
Step 2 "找 Python 3.11"
function Find-Python311 { try { $v = & py -3.11 --version 2>$null; if ($LASTEXITCODE -eq 0 -and $v -match "3\.11") { return $true } } catch { }; return $false }
if (-not (Find-Python311)) {
    Write-Host "這台電腦沒有 Python 3.11。"
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "用 winget 安裝 Python 3.11？（需要網路）")) {
        Run "winget" @("install", "-e", "--id", "Python.Python.3.11", "--accept-package-agreements", "--accept-source-agreements")
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    }
    if (-not (Find-Python311)) { throw "仍找不到 Python 3.11。請到 python.org 安裝 3.11（勾選 py launcher）後重新執行本安裝。" }
}
Write-Host "Python 3.11 可用。"

# --- 3. 虛擬環境 ---
Step 3 "建立程式環境（.venv）"
if (-not (Test-Path $VenvPy)) { Run "py" @("-3.11", "-m", "venv", $Venv) } else { Write-Host ".venv 已存在，沿用。" }
Run $VenvPy @("-m", "pip", "install", "--upgrade", "pip", "--quiet")

# --- 4. 套件 ---
Step 4 "安裝套件（需要網路，約 10 到 30 分鐘；之後前台不需要上網）"
$hasGpu = $false; $cudaTag = "cu126"; $torchVer = "2.13.0"; $tvVer = "0.28.0"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $info = & nvidia-smi --query-gpu=driver_version,name --format=csv,noheader 2>$null
    if ($LASTEXITCODE -eq 0 -and $info) {
        $hasGpu = $true
        $parts = ($info | Select-Object -First 1) -split ",\s*"
        $driver = [int](($parts[0] -split "\.")[0]); $gpuName = $parts[1]
        Write-Host "NVIDIA 顯卡：$gpuName，驅動 $($parts[0])"
        # 用 CUDA 12.8 版 PyTorch（torch 2.11，驅動 570 以上）：RTX 50 系列（Blackwell）要 CUDA 12.8 以上，
        # 而且 onnxruntime-gpu 是 CUDA 12 版，兩邊同樣是 CUDA 12 才不會互搶 cuDNN
        # （PyTorch 的 cu130 版和 onnxruntime 的 CUDA 12 版同時載入時，cuDNN 子檔案版本對不上，會彈出「無法找到輸入點」，
        # 先載入 onnxruntime 再載入 PyTorch 時 PyTorch 甚至載入失敗）
        if ($driver -ge 570) { $cudaTag = "cu128"; $torchVer = "2.11.0"; $tvVer = "0.26.0" }
        elseif ($gpuName -match "RTX\s*50") { throw "$gpuName 需要 NVIDIA 驅動 570 以上（目前 $($parts[0])）。請先到 nvidia.com 或 GeForce Experience 更新驅動，再重新執行安裝。" }
    }
}
$lock = Get-Content (Join-Path $Root "requirements\lock.txt") -Encoding UTF8
$constraints = $lock | Where-Object { $_ -notmatch "^(torch|torchvision|onnxruntime|onnxruntime-gpu|retinaface)(==|\s|$)" }
# 版本限制檔用每次都不同的暫存檔名：上次安裝還沒關的 pip、防毒或檔案總管若還握著舊檔，不會讓這次安裝卡住
$constraintsPath = Join-Path ([IO.Path]::GetTempPath()) ("smartaccess-constraints-" + [guid]::NewGuid().ToString("N") + ".txt")
$constraints | Set-Content -Path $constraintsPath -Encoding UTF8
if ($hasGpu) {
    Write-Host "安裝 GPU 版（$cudaTag，辨識較快）。"
    Run $VenvPy @("-m", "pip", "install", "torch==$torchVer", "torchvision==$tvVer", "--index-url", "https://download.pytorch.org/whl/$cudaTag", "--extra-index-url", "https://pypi.org/simple", "-c", $constraintsPath)
    $ort = "onnxruntime-gpu==1.23.2"
} else {
    Write-Host "沒有 NVIDIA 顯卡，安裝 CPU 版（辨識較慢，門禁仍可用）。"
    Run $VenvPy @("-m", "pip", "install", "torch==2.13.0", "torchvision==0.28.0", "--index-url", "https://download.pytorch.org/whl/cpu", "--extra-index-url", "https://pypi.org/simple", "-c", $constraintsPath)
    $ort = "onnxruntime==1.23.2"
}
Run $VenvPy @("-m", "pip", "install", $ort, "-r", (Join-Path $Root "requirements\front.txt"), "-c", $constraintsPath)
if ($hasGpu -and $cudaTag -eq "cu128") {
    # onnxruntime-gpu 1.23 要 CUDA 12 與 cuDNN 9 的程式庫；不論 onnxruntime 或 PyTorch 誰先載入都找得到（約 2.4 GB）
    Run $VenvPy @("-m", "pip", "install", "nvidia-cuda-runtime-cu12==12.9.79", "nvidia-cublas-cu12==12.9.2.10", "nvidia-cudnn-cu12==9.28.1.4", "nvidia-cufft-cu12==11.4.1.4", "nvidia-curand-cu12==10.3.10.19", "nvidia-cuda-nvrtc-cu12==12.9.86")
}
Run $VenvPy @("-m", "pip", "install", "retinaface==1.1.1", "--no-deps")

# --- 5. 網路 ---
Step 5 "防火牆（網路位址由程式自動找，不用設 IP）"
if (Ask "現在建立防火牆規則？（需要系統管理員；稍後可用「設定前台網路.bat」）") { Set-FirewallRules }

# --- 6. 通行碼 ---
Step 6 "通行碼（要和後台一模一樣；只存在這個使用者的環境變數，不寫進檔案）"
$existing = [Environment]::GetEnvironmentVariable($TokenEnv, "User")
if ($existing -and $existing -match "^[A-Za-z0-9_-]{43,}$" -and -not (Ask "已經設定過通行碼，要換成新的嗎？" "N")) { Write-Host "沿用目前的通行碼。" }
else {
    Write-Host "在後台執行 python -m shared.role_config token 產生，再複製貼到這裡（貼上時畫面不會顯示）。"
    do {
        $secure = Read-Host "通行碼" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        $token = $token.Trim()
        $valid = ($token -match "^[A-Za-z0-9_-]{43,}$") -and (($token.ToCharArray() | Select-Object -Unique).Count -ge 16)
        if (-not $valid) { Write-Host "通行碼不合格：要用後台 python -m shared.role_config token 產生的那一組（43 個英數字元、底線或連字號），請重貼。" -ForegroundColor Yellow }
    } while (-not $valid)
    [Environment]::SetEnvironmentVariable($TokenEnv, $token, "User")
    $token = $null
    Write-Host "通行碼已存入使用者環境變數。" -ForegroundColor Green
}

# --- 7. 角色 ---
Step 7 "設定成前台"
Run $VenvPy @("-m", "shared.role_config", "set", "--role", "front", "--back-host", "auto")

# --- 8. 捷徑 ---
Step 8 "桌面捷徑"
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "啟動前台門禁.lnk"))
$link.TargetPath = Join-Path $Root "啟動前台.bat"; $link.WorkingDirectory = $Root; $link.Save()
Write-Host "已在桌面建立「啟動前台門禁」（不做開機自動啟動，要用時自己雙擊）。"
$oldStartup = Join-Path ([Environment]::GetFolderPath("Startup")) "啟動前台門禁.lnk"
if (Test-Path $oldStartup) { Remove-Item $oldStartup -Force; Write-Host "已移除之前設定的開機自動啟動。" }

# --- 9. 自我檢查 ---
Step 9 "自我檢查"
$env:SMART_ACCESS_LINK_TOKEN = [Environment]::GetEnvironmentVariable($TokenEnv, "User")
& $VenvPy -m access.front_check
$code = $LASTEXITCODE
Write-Host ""
if ($code -eq 0) { Write-Host "安裝完成。後台開好之後，雙擊桌面「啟動前台門禁」。" -ForegroundColor Green }
else { Write-Host "安裝已完成，但自我檢查有失敗項目（見上方 FAIL），處理後可雙擊「前台檢查.bat」再檢查。" -ForegroundColor Yellow }
Read-Host "按 Enter 結束"
