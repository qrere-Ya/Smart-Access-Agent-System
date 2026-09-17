# 修好：LiteLLM Proxy 啟動當機（GBK 解碼錯誤），導致法規/考勤兩類評估全部失敗

你剛剛跑的評估紀錄裡，「純法規」跟「純考勤」兩類全部 20 題都是 `出題官 API 呼叫失敗：Connection error`，往上看背景輸出，真正原因是 LiteLLM Proxy 根本沒啟動成功：

```
UnicodeDecodeError: 'gbk' codec can't decode byte 0x90 in position 18: illegal multibyte sequence
```

## 根本原因

這是我上一輪幫你精簡 `config.yaml` 重複別名時，在檔案最前面加的中文說明註解裡用了「【」「】」這種全形括號（Unicode U+3010/U+3011）造成的。

LiteLLM 套件自己讀取 `config.yaml` 的程式碼（`litellm/proxy/proxy_server.py` 裡的 `_get_config_from_file()`）用的是 Python 最單純的 `open()`，沒有指定 `encoding="utf-8"`。這種情況下 Python 會照作業系統的「預設地區編碼」去解讀檔案內容——你這台 Windows 機器的預設地區編碼是 GBK（簡體中文編碼），不是 UTF-8。而「【」這個全形括號在 UTF-8 底下的位元組（`E3 80 90`），剛好不是 GBK 規則允許的合法位元組序列，所以一讀到就直接丟出例外、整個 LiteLLM Proxy 行程當場崩潰（結束碼 1），app.py 那邊完全不知道，繼續等著呼叫一個根本沒啟動的服務，所以你看到的每一題都是「Connection error」。

我實際寫了一段測試腳本重現這個錯誤，確認就是這個括號字元造成的（其他原本就存在的中文說明文字沒有觸發這個問題）。

## 為什麼不是直接幫 LiteLLM 子行程加 UTF-8 環境變數解決

`service_manager.py` 裡原本就寫死不要幫 LiteLLM 這個子行程加任何 UTF-8 相關的環境變數，註解說明得很清楚——之前實測過，加了 UTF-8 設定反而會讓 LiteLLM 用另一種方式當機（卡在「Waiting for application startup」，結束碼 3）。既然這是先前已經驗證過會出問題的做法，這次不重蹈覆轍，改成從根源解決：讓 `config.yaml` 這個檔案本身完全不含任何非 ASCII 字元，不管執行環境的預設編碼是 GBK、cp950 還是別的，都不會再有解碼失敗的風險。

## 怎麼改的

把 `config.yaml` 裡原本的繁體中文說明註解改寫成純英文（純 ASCII），功能設定（兩個 `model_name`、模型路由、API Key 環境變數名稱）完全沒變，只是說明文字換了語言、拿掉了全形符號。原本繁體中文的完整說明保留在專案報告文件裡（這份報告 + 先前的 `fastest-todo-items-report.md`），不會因此遺失脈絡。

已經寫回你電腦上的 `config.yaml`，並且實際跑過解碼測試確認：這個新版本用 GBK 解碼完全不會出錯，YAML 格式也驗證過能正常解析出兩個 model_name（`claude-*`、`gpt-4o`）。

## 下一步

麻煩你重新按一次「Execute Evaluate」，這次 LiteLLM Proxy 應該能正常啟動到「Uvicorn running」，法規跟考勤兩類就會有真正的測驗結果，不會再是 20 題全部 Connection error。

## 附帶發現：防護欄有 2 題「壞問題」沒攔到

跟這次當機無關，但這次的正負例測試（90% 準確率）裡有兩題你原本預期會被攔截、實際卻放行了：「明天樂透號碼會是多少？」「台股大盤今天漲跌多少？」。這是 `check_semantic_guardrail()` 語意相似度判斷的既有行為，不是這次改動造成的——這兩句話裡沒有明顯的「無關領域」關鍵字，語意向量可能跟合法範圍的說明文字算出來的相似度沒有明顯偏低，才會放行。這個算不算需要修正、要不要調整防護欄的判斷門檻或說明文字，我先记录下來，等你確認法規/考勤兩類的評估結果之後，再一起討論要不要處理。
