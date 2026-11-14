@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 找不到程式環境（.venv），請聯絡開發者。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m admin.preflight
echo.
echo 前台也要檢查：請在前台電腦雙擊「前台檢查.bat」（或「展示前台.bat」）。
echo 看完請按任意鍵關閉。
pause >nul
