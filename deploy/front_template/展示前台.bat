@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 找不到程式環境（.venv），請先執行「安裝前台.bat」。
  pause
  exit /b 1
)
echo ==================================================
echo  展示前台  先做前台檢查、再啟動前台
echo ==================================================
echo.
echo 順序：後台（另一台電腦）先開 -^> 這裡檢查 -^> 啟動前台 -^> 按「▶ 啟動人臉辨識」
echo.
".venv\Scripts\python.exe" -m access.front_check
echo.
echo --------------------------------------------------
echo 檢查有「失敗」的項目請先處理（看上面的說明）。
choice /c YN /m "要現在啟動前台嗎"
if errorlevel 2 (
  echo 沒有啟動。要啟動時再雙擊「啟動前台.bat」或桌面「啟動前台門禁」。
  pause
  exit /b 0
)
echo.
echo 啟動前台中，儀表板會出現在第二個螢幕；請按右上角「▶ 啟動人臉辨識」。
call "%~dp0啟動前台.bat"
