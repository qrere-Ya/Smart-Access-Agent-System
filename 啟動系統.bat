@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 找不到程式環境（.venv），請聯絡開發者。
  pause
  exit /b 1
)
echo 系統啟動中，請稍候，登入畫面會自己跳出來。
echo （這個黑色視窗請不要關閉，展示結束再關。）
".venv\Scripts\python.exe" -m admin.backend_main
echo.
echo 系統已結束。按任意鍵關閉這個視窗。
pause >nul
