# 前台工程師的 AI 指引（交接用，動工前請整篇讀完）

這份是給**協助前台工程師的 AI**（Claude Code、Codex、Cursor 等）讀的。它規定你能動什麼、**不能碰什麼**。
使用方式：把整份貼進 AI 的第一則訊息，或複製成專案根目錄的 `AGENTS.md`（不要 commit）。人類工程師請先看 `前台開發指南.md` 與 `GitHub新手指南.md`。

負責：前台功能工程師　範圍：前台**畫面與畫面資料**（改畫面不需要模型、攝影機、後台）　送出方式：一個功能一個 PR
共通規則：專案根目錄的 `CLAUDE.md` 是整個專案的規則，先讀；這裡不重複，只補前台的範圍與禁區。

## 1. 這張要做到什麼

改前台儀表板（第二個螢幕）的畫面或顯示內容，讓工程師用 `python -m access.front_preview`（假資料）自己看過、跑過測試，再送 PR。
前台是給一般使用者看的畫面：**只看、不設定。**

## 2. 先讀這些

| 檔案 | 為什麼 |
|---|---|
| `前台開發指南.md` | 前台程式地圖、用到的模型、驗證方式、PR 規則 |
| `CLAUDE.md` 的「前台」那一段 | 前台的設計原則與限制 |
| `access/front_dashboard.py` 檔頭註解 | 儀表板畫面：卡片、警示橫幅、主題、全螢幕 |
| `access/front_view.py` 檔頭註解 | 畫面資料的純邏輯（打卡問候、晨報分頁、警示文字） |
| `access/front_preview.py` | 用假資料開儀表板，不碰真實資料 |
| `tests/unit/access/test_front_*.py`、`tests/integration/test_front_dashboard.py` | 既有測試，改了要跟著調整 |

## 3. 你可以改的檔案

> **若 issue 另外列出「可改檔案」，以 issue 為準**（例如畫面文字改名的任務會需要改 `admin/` 裡的畫面檔）。issue 沒列到的檔案，仍適用第 4 節的禁區；第 5、6、7、8 節的規則在任何任務都照舊。

- `access/front_dashboard.py`、`access/front_view.py`、`access/front_qr.py`、`access/front_resources.py`、`access/front_screens.py`、`access/front_preview.py`
- `access/alert_sound.py`（提示音型）、`access/audio_player.py` 開頭的常數（語速、停頓、音量，**只改常數**）
- 對應的測試：`tests/unit/access/test_front_*.py`、`tests/integration/test_front_dashboard.py`
- `前台開發指南.md`（要補充說明時）

## 4. 絕對不要碰（要改請停下來問人類）

| 範圍 | 原因 |
|---|---|
| `admin/`、`rag/` | 後台與知識庫，不是前台 |
| `deploy/`、`dist/`、`requirements/`、`.github/`、`pyproject.toml`、`*.bat`、`.gitattributes` | 打包、安裝、CI、依賴；由專案負責人處理（`.bat` 必須維持 CRLF） |
| `models/`、`data/`、`runtime/` | 模型、資料、執行期資料，不進版控，也不要讀寫或「修」裡面的檔案（語音資料夾裡的 `tokens.txt` 一個位元組都不能動） |
| `access/front_agent.py`、`shared/link_protocol.py`、`shared/link_tls.py`、`shared/role_config.py`、`shared/lan_discovery.py`、`shared/network_policy.py` | 前後台連線與網路規則；前後台要一起改，不能只改一邊 |
| `access/terminal_app.py`、`terminal_backend.py`、`vision_core.py`、`pose.py`、`security.py`、`pass_through.py`、`punch_state.py` | 辨識、活體、尾隨、打卡；牽涉安全與準確度，沒有被明確指派就不要動 |
| `shared/ui_theme.py` | 前後台**共用**，改了會同時改變後台畫面；一定要動時先問，並附後台截圖 |
| `shared/database_mgr.py`、`front_store.py` 的資料表結構 | 改了會讓既有資料或另一台電腦壞掉 |
| `CLAUDE.md`、`GitHub新手指南.md`、本檔 | 規則文件，不要自己改規則 |

## 5. 前台的設計原則（違反就是做錯）

- **前台不能有任何設定或管理員功能**：不加設定按鈕、不加用管理員臉登入的維護、不加「停止辨識」。前台只能**啟動**人臉辨識，停止由後台下指令。
- **前台不上網**：不加任何連外網的功能（不開瀏覽器、不抓網路資料、不載入外部圖片與字型）。新聞由後台抓，前台只顯示收到的內容；原文只以 QR Code 給手機掃。
- **前台永遠是主動連後台**，後台不連前台。
- 畫面是對外的：不要長期顯示員工姓名（打卡問候超過保留時間要消失）、不要在畫面或記錄裡顯示通行碼、金鑰、雜湊。
- 外部來的文字（新聞標題等）要截斷，避免撐壞版面。

## 6. 工作規則

- **先讀後改**：改之前先讀相關檔案與呼叫端，不憑記憶推測。
- **不確定就停**：不確定的地方回報給人類，不要自己猜；不要用「只是預覽」「風險很低」當理由放行。
- **只動被指派的範圍**：不順手整理、重新格式化、改名或搬動不相關的檔案。PR 越小越好審。
- **不新增依賴套件**；需要的話先問。
- **不寫死特例**：不要把修復綁在某一份假資料或某個畫面大小上，要考慮不同螢幕大小與不同主題（dark、frosted）。
- **註解風格**：沿用周邊程式碼；檔頭有主要功能與串接模組的說明，行內註解只寫「為什麼」。
- **畫面測試與截圖不得使用真人臉或真實員工姓名**：用 `access/front_preview.py` 的假資料（測試甲、測試乙）；需要人像就用插畫或虛擬人物。「半透明白」主題是透明視窗，截圖會帶到桌面上的其他視窗，貼出去前要確認沒有私人內容。

## 7. Git 規則

- 只在人類**明確要求**時才 commit 或 push；預設不動 `main`，在 `feat/front-xxx`、`fix/front-xxx` 分支上做。
- `git add` 一律**指定檔案**，不要用 `git add .` 或 `git add -A`。
- 不要使用 `git push --force`、`git reset --hard`、`git checkout -- .`、`git clean -f` 這類會丟掉修改的指令，除非人類明確要求。
- 不要 commit：`models/`、`data/`、`runtime/`、`dist/`、`.vscode/`、任何金鑰或通行碼（`.env`、`SMART_ACCESS_LINK_TOKEN`）。
- commit 說明用一句中文寫「改了什麼」。
- **不要用名稱或記憶體大小去結束行程**（例如 `taskkill /IM python.exe`、`Stop-Process -Name python`），那可能砍掉別的程式；只結束自己啟動、且知道 PID 的行程。

## 8. 做過才知道的地雷（一定要看）

- **CTkLabel 的 `image=None` 會被忽略**：標籤仍指著舊圖，舊圖被回收後會出現 `image "pyimageNN" doesn't exist`。要清掉圖，換成一張永遠存活的透明小圖。
- **CTkFrame 的 `bind` 其實綁在內部畫布上**：事件的 `widget` 是畫布，不是框架本身，所以 `event.widget is self` 會永遠是假。要同時比對 `self._canvas`。
- **在會被銷毀的元件上排 `after`**：元件銷毀後排程還會觸發，噴 `invalid command name`。計時器掛在不會被銷毀的主視窗上，並在觸發時檢查頁面還在不在。
- **ttk 表格樣式是全域的**：換主題後要重新呼叫 `ui_theme.style_treeview`，否則顏色不會跟著換。
- **CI 在 Linux 虛擬螢幕（xvfb）上跑**：沒有視窗管理員，全螢幕與鍵盤事件不會送達。測這類功能時要直接呼叫處理函式，不能只靠 `event_generate`。
- **Windows 的 Bash 會吃掉 `\n` 之類的跳脫字元**：用 Python 或編輯工具寫檔，不要在 shell 裡用 heredoc 塞含反斜線的內容；改 CRLF 檔案時要先統一換行。
- **CI 的 Semgrep 會擋用 f-string 組 SQL**，連測試裡也一樣；用固定的完整查詢語句。
- **CodeQL 會擋把敏感資料寫進記錄**：記錄只寫類別、數量，不寫金鑰、通行碼、完整例外內容。
- **前台是 pythonw 啟動（沒有標準輸出）**：`print` 會出錯，輸出要走 `front_launcher.redirect_output_if_windowless`。
- **連到沒在聽的本機埠，Windows 會卡約 2 秒**：不要在畫面執行緒上做這種連線。
- **`front_preview` 的人臉辨識按鈕是假的**：預覽看得到的按鈕行為，不代表真實辨識程式的行為。

## 9. 測試（每個 PR 都要貼結果）

```
python -m ruff check access shared tests
python -m pytest tests/unit/access/test_front_*.py
python -m pytest -m integration tests/integration/test_front_dashboard.py     # 會閃出視窗，約 1 分半
python -m access.front_preview --alert tailgate                                # 自己看一次畫面，兩個主題各看一次
```

- 純邏輯的改動（`front_view.py`）要補單元測試；畫面行為要補整合測試。
- 整合測試偶爾在整批一起跑時因時間差失敗，單獨重跑會過；先單獨重跑再判斷。
- 沒測到的部分（沒有攝影機、沒有後台、沒有真機）在 PR 裡**明講**，不要寫成「已驗證」。

## 10. PR 說明（照這個格式）

```
## 改了什麼、為什麼
## 改了哪些檔案（有沒有碰到 shared/、連線、辨識）
## 畫面截圖（深色與半透明白各一張；改畫面必附）
## 我測了什麼（貼上第 9 節的指令與結果）
## 沒測的部分
## 需要兩台電腦一起更新或重建前台包嗎
```

## 11. 完成定義

- 只改了第 3 節列的檔案（用 `git status` 確認沒有多的）
- 沒有違反第 5 節的設計原則
- ruff 全過、前台單元測試與畫面整合測試通過，用 `front_preview` 看過兩個主題
- PR 說明完整，截圖沒有真人臉、真實姓名與私人內容
- 有不確定的地方已經回報人類，沒有自己猜
