@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 找不到程式環境（.venv），請先執行「安裝前台.bat」。
  pause
  exit /b 1
)
if "%SMART_ACCESS_LINK_TOKEN%"=="" (
  echo 沒有通行碼：請先執行「安裝前台.bat」，或重新登入 Windows 讓環境變數生效。
  pause
  exit /b 1
)
rem 只開儀表板（用 pythonw，沒有黑色視窗）；連線代理由儀表板在背景啟動，人臉辨識用儀表板右上角的開關啟動。記錄在 runtime\logs。
start "" ".venv\Scripts\pythonw.exe" -m access.front_dashboard
