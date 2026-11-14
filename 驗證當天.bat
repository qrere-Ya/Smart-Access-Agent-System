@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 找不到程式環境（.venv），請聯絡開發者。
  pause
  exit /b 1
)
echo ============================================================
echo  驗證當天（後台）：一步一步帶著做真機驗證，自動擷取證據並產生報告
echo ============================================================
echo  開始前請確認：
echo   1. 前後台之間的網路線已接好
echo   2. 後台介面已開（雙擊「啟動系統.bat」，連線服務會自動啟動）
echo   3. 前台已開（雙擊桌面「啟動前台門禁」），並按了「啟動人臉辨識」
echo  過程中：做好按 Enter、s 略過這一項、q 結束（已完成的項目會留在報告裡）
echo ============================================================
echo.
".venv\Scripts\python.exe" -m admin.verify_day
echo.
echo 報告在 runtime\verification\ 底下最新的資料夾（report.md）。
echo 前台那份報告請用隨身碟從前台電腦複製過來，一起看。
echo 看完請按任意鍵關閉。
pause >nul
