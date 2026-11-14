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
echo ============================================================
echo  前台驗證：自動收集檢查結果與螢幕、攝影機、記錄檔，並請你聽語音與警示音
echo ============================================================
echo  建議先把前台開起來（雙擊「啟動前台.bat」並按「啟動人臉辨識」）再跑這個，
echo  這樣才看得到辨識與連線的真實狀態。過程中：Enter 繼續、s 略過、q 結束。
echo.
".venv\Scripts\python.exe" -m access.front_verify
echo.
echo 報告在 runtime\verification\ 底下（前台_日期_時間.md）。
echo 前台不上網：請用隨身碟把它複製到後台電腦的 runtime\verification\ 資料夾。
echo 看完請按任意鍵關閉。
pause >nul
