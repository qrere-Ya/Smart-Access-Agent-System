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
echo 看完請按任意鍵關閉。
pause >nul
