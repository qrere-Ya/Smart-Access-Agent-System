# GitHub 新手指南（第一次用 GitHub 的組員看這份）

你只需要記住一件事：**不要直接改別人在用的版本，從自己的「分支」改，改完開一個「PR」請專案負責人看。**
這份指南從零開始，照順序做就能把你改的東西送進專案。

名詞對照：

| 名詞 | 白話解釋 |
|---|---|
| Repository（repo） | 專案的資料夾，含所有檔案與修改歷史 |
| Clone | 把 repo 從 GitHub 下載一份到你的電腦 |
| Branch（分支） | 專案的一條獨立副本，你在自己的分支改，不會影響別人 |
| Commit | 把目前的修改「存檔」成一個紀錄，附一句說明 |
| Push | 把你電腦上的 commit 上傳到 GitHub |
| Pull Request（PR） | 在 GitHub 網頁上跟負責人說「我改好了，請看一下、合併進去」 |
| `main` | 正式版本的分支，**不要直接改它、不要直接推它** |

### 你下載的是整個專案，但你送出去的只有「你改的檔案」

clone 會把整個專案下載到電腦，裡面有後台、知識庫等你用不到的資料夾，**不用管它們**。
Git 只會記錄你改過的檔案：你只改了 `access/front_view.py`，PR 裡就只會出現這一個檔案，其他幾百個檔案不會動，也不會被推上去。
做法就是第三節步驟 4 的 `git add 你改的檔案`（一個一個指定），再 `git commit`、`git push`。
想讓資料夾看起來更乾淨，可以用第二節的「只下載前台需要的資料夾」。

## 一、第一次要準備的（只做一次）

1. **註冊 GitHub 帳號**：到 github.com 註冊，把你的帳號名稱告訴專案負責人，請他把你加成專案的協作者（Collaborator）。你會收到邀請信，按「接受」。
2. **安裝 Git**：到 git-scm.com 下載 Windows 版，一路按「下一步」安裝。裝完在檔案總管的空白處按右鍵，會看到「Git Bash Here」。
3. **告訴 Git 你是誰**：開啟 Git Bash（或 PowerShell），輸入下面兩行（換成你自己的名字與註冊 GitHub 的信箱）：

   ```
   git config --global user.name "你的名字"
   git config --global user.email "你的信箱@example.com"
   ```
4. **安裝 Python 3.11 與編輯器**：Python 到 python.org 下載 3.11 版（安裝時勾「Add python.exe to PATH」）；編輯器建議用 VS Code。
5. **（可選）安裝 VS Code 的 Git 功能**：VS Code 內建，左邊第三個圖示就是「原始檔控制」，可以用按鈕代替下面的指令。

## 二、把專案下載到你的電腦（只做一次）

專案負責人會給你專案網址（長得像 `https://github.com/某某/專案名稱`）。

```
cd C:\Users\你的名字\Documents
git clone 專案網址
cd 專案資料夾名稱
```

第一次 clone 時，瀏覽器會跳出 GitHub 登入視窗，登入並按「Authorize」即可。

### 只想要前台的檔案（可選，專案看起來比較乾淨）

整個專案包含前台、後台、知識庫，檔案很多。如果你只改前台，可以只取出需要的資料夾：

```
git clone --sparse 專案網址
cd 專案資料夾名稱
git sparse-checkout set access shared tests/unit/access tests/integration pyproject.toml
```

這樣硬碟上只會出現 `access`（前台程式）、`shared`（共用程式）與相關測試，沒有後台和知識庫的資料夾（根目錄的 `CLAUDE.md` 等少數檔案還是會有）。
之後想看全部，輸入 `git sparse-checkout disable` 就會還原。

### 準備跑起來

接著照 `前台開發指南.md` 第 3 節建好 Python 環境，用 `python -m access.front_preview` 看畫面。**不需要下載模型。**

## 三、每次要改東西的流程（最重要）

### 步驟 1：先更新到最新，再開自己的分支

```
git checkout main
git pull
git checkout -b feat/front-你要做的事
```

分支名稱規則：`feat/front-簡短英文`（新功能或改畫面）或 `fix/front-簡短英文`（修問題），例如 `feat/front-clock-size`。
**之後所有修改都在這條分支上做。** 輸入 `git branch` 會看到目前在哪條分支（前面有 `*`）。

### 步驟 2：改檔案、自己測

照 `前台開發指南.md` 改檔案，用 `python -m access.front_preview` 看畫面，跑測試。

### 步驟 3：看改了什麼

```
git status          # 列出哪些檔案被改過
git diff            # 看每個檔案具體改了哪幾行
```

先確認列出來的檔案都是你**預期要改的**。如果看到不認識的檔案，先不要繼續，問專案負責人。

### 步驟 4：存成 commit

```
git add access/front_dashboard.py access/front_view.py
git commit -m "調整前台時鐘字體大小"
```

- `git add` 後面接你改的檔案（用空格隔開）。**不要用 `git add .` 或 `git add -A`**，這會把不該上傳的東西一起加進去。
- 說明（`-m` 後面的字）用一句話寫「改了什麼」，中文就可以。
- 可以分成好幾個 commit，每個 commit 做一件事。

### 步驟 5：上傳到 GitHub

```
git push -u origin feat/front-你要做的事
```

（第一次推這條分支才需要 `-u origin 分支名稱`，之後在同一條分支只要 `git push`。）

### 步驟 6：在 GitHub 網頁開 PR

1. 打開專案網頁，頁面上會出現黃色提示「Compare & pull request」，按它。（沒看到就點上方「Pull requests」→「New pull request」，選你的分支。）
2. 標題寫一句話；說明欄照下面的格式填（直接複製貼上再改）：

   ```
   ## 改了什麼、為什麼
   （例：把前台時鐘的字放大，因為門口看不清楚）

   ## 改了哪些檔案
   （例：access/front_dashboard.py）

   ## 畫面截圖
   （把截圖直接拖進這個輸入框；改畫面一定要附）

   ## 我測了什麼
   - [ ] python -m ruff check access shared tests 全過
   - [ ] python -m pytest tests/unit/access/test_front_*.py 全過
   - [ ] 用 python -m access.front_preview 看過畫面
   沒有測的部分：（例如：沒有接攝影機、沒有接後台）
   ```
3. 按「Create pull request」。接下來等專案負責人看、留言或合併。

### 步驟 7：如果負責人請你修改

在**同一條分支**上繼續改、`git add`、`git commit`、`git push`，PR 會自動更新，不需要重開。

### 步驟 8：PR 被合併之後

```
git checkout main
git pull
```

下一件事再從步驟 1 開新分支。**不要在舊分支上繼續做新的事。**

## 四、絕對不要做的事

- 不要直接在 `main` 上改，也不要 `git push origin main`。
- 不要 commit 這些東西：`models/`（模型檔案）、`data/`、`runtime/`、`dist/`、`.vscode/`、任何密碼、金鑰、通行碼（例如 `SMART_ACCESS_LINK_TOKEN`、`.env`）。專案的 `.gitignore` 已經擋掉大部分，但請用 `git status` 確認。
- 不要用 `git push --force`（強制推送）。
- 不要把檔案換行格式改掉（`.bat` 檔必須是 CRLF）。
- 不要順手整理或改格式化不相關的檔案，PR 越小越好看。

## 五、常見問題

| 狀況 | 怎麼辦 |
|---|---|
| 不小心在 `main` 上改了檔案，還沒 commit | `git checkout -b feat/front-新分支`：修改會跟著帶到新分支，再照步驟 4 開始 |
| 不小心在 `main` 上 commit 了，還沒 push | 先不要動，把 `git log --oneline -3` 的結果貼給專案負責人 |
| `git push` 說沒有權限 | 你還沒被加成協作者，或沒接受邀請信；請專案負責人確認 |
| `git pull` 說有衝突（conflict） | 先不要自己硬解，把訊息貼給專案負責人 |
| 想把某個檔案改回原本的樣子 | `git checkout -- 檔案路徑`（**這會丟掉那個檔案還沒 commit 的修改**） |
| 不確定現在在哪條分支、改了什麼 | `git status`，它會告訴你 |
| commit 說明寫錯了（還沒 push） | `git commit --amend -m "新說明"` |
| 看到 `.gitignore` 擋掉的檔案想上傳 | 通常是正確的，不要硬加（`git add -f`）；有疑問先問 |

## 六、一頁速查

```
更新並開新分支： git checkout main ; git pull ; git checkout -b feat/front-xxx
看改了什麼：     git status ; git diff
存檔：           git add 檔案1 檔案2 ; git commit -m "說明"
上傳：           git push -u origin feat/front-xxx   （第一次）／ git push
然後到 GitHub 網頁開 Pull Request，照格式填說明並附截圖。
```

有不確定的指令，先貼給專案負責人問，不要自己試。
