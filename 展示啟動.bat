@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 找不到程式環境（.venv），請聯絡開發者。
  pause
  exit /b 1
)
echo ==================================================
echo  展示啟動（後台）  先檢查、再啟動後台介面
echo ==================================================
echo.
echo 展示順序（前台是另一台電腦）：
echo   1. 後台（這台）：先看下面的檢查結果，再啟動後台介面
echo   2. 前台電腦：雙擊「展示前台.bat」（或桌面「啟動前台門禁」）
echo   3. 前台畫面右上角按「▶ 啟動人臉辨識」（辨識不會自己開）
echo.
echo 正在做展示前檢查（含前台連線狀態；前台還沒開時會顯示離線，這是正常的）...
echo.
".venv\Scripts\python.exe" -m admin.preflight
echo.
echo --------------------------------------------------
echo 看完檢查結果後：按任意鍵啟動後台；不想啟動請直接關掉這個視窗。
pause >nul
echo.
echo 系統啟動中，請稍候，登入畫面會自己跳出來。
echo 後台介面開著時會自動啟動連線服務，前台才連得上。
echo 前台那台請雙擊「啟動前台門禁」，再按「▶ 啟動人臉辨識」。
echo （這個黑色視窗請不要關閉，展示結束再關。）
".venv\Scripts\python.exe" -m admin.backend_main
echo.
echo 系統已結束。按任意鍵關閉這個視窗。
pause >nul
