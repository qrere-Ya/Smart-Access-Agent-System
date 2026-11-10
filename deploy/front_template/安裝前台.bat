@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_front.ps1"
if errorlevel 1 (
  echo.
  echo 安裝中斷了，請把上面的錯誤訊息給開發者。重新執行本檔會從頭檢查，已完成的步驟會略過。
  pause
)
