@echo off
chcp 65001 >nul
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
  echo 請在這個檔案上按右鍵 → 以系統管理員身分執行。
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_back_network.ps1"
pause
