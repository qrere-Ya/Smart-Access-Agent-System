@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
rem 舊版（customtkinter）儀表板：新版（QML）有問題時的備用。
start "" ".venv\Scripts\pythonw.exe" -m access.front_dashboard
