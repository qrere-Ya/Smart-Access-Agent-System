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
echo 前台啟動中：先開連線代理，再開辨識畫面。這個視窗請不要關閉。
start "SmartAccessFrontAgent" /min ".venv\Scripts\python.exe" -m access.front_agent
".venv\Scripts\python.exe" -m access.terminal_app
echo.
echo 辨識畫面已結束，關閉連線代理。
taskkill /fi "WINDOWTITLE eq SmartAccessFrontAgent*" /f >nul 2>&1
pause
