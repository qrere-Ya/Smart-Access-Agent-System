"""
guard.py：問題送進語言模型之前的惡意輸入把關（純規則，不呼叫模型）。

主要功能：
  - screen(question)：依「類型」判斷問題是不是在做下列事情，是就回傳類型名稱，不是回傳 None
      sql         夾帶 SQL 語句（DROP、DELETE FROM、UNION SELECT、; -- 等）
      payload     夾帶 JSON 結構想直接指定要查什麼或怎麼回答（{"data": ...}）
      override    要模型忽略規則、換角色、進入開發者模式、洩漏系統提示詞
      write       要求刪除、修改、新增資料庫裡的紀錄（助理只能唯讀查詢）
      exfiltrate  要求輸出金鑰、密碼、人臉特徵、原始碼、資料庫檔或路徑穿越
  - 判斷前先做 NFKC 正規化（全形轉半形）並去掉零寬字元，避免用全形或隱形字元躲過規則
  - 規則是「類型」而不是特定句子；是補強而不是唯一防線：語言模型的提示詞也要求忽略夾帶的指令，
    工具本身只有白名單內的唯讀查詢，語言模型不寫 SQL
  - 已知取捨：問「依法可不可以刪除員工資料」這類詢問規定的句子不算 write；偏向少誤擋正常問題，
    所以換個說法的寫入要求可能漏掉，漏掉時仍只會走唯讀查詢

串接模組：
  - router.make_plan：命中就不呼叫模型，直接回傳拒絕的計畫
  - evaluation.tool_eval：評估時套用同一個把關，結果才和正式流程一致
"""
import re
import unicodedata
from typing import Optional

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))

_SQL = re.compile(
    r"\b(drop|truncate|alter|create)\s+(table|database|index|view|trigger)\b"
    r"|\bdelete\s+from\b|\binsert\s+into\b|\bupdate\s+\w+\s+set\b|\bunion\s+(all\s+)?select\b"
    r"|\bselect\b.{1,80}\bfrom\b|\bpragma\s+\w+|\battach\s+database\b|\bsqlite_master\b"
    r"|(;|--|/\*)\s*(drop|delete|select|insert|update)\b|\bor\s+1\s*=\s*1\b")

_PAYLOAD = re.compile(r"\{\s*[\"'][\w\-]+[\"']\s*:|<\s*/?\s*(script|iframe|img|svg)\b|javascript:|\bon(error|load)\s*=")

_OVERRIDE = re.compile(
    r"(忽略|無視|不要理會|不要管|忘記|忘掉|繞過)[^。！？\n]{0,12}(規則|指令|指示|提示|限制|設定|安全|防護|審查|過濾)"
    r"|(你現在|從現在起|接下來你|假設你|假裝你)\s*(就)?\s*(是|變成|扮演|不受|不再受)|(請你扮演|請扮演|扮演)"
    r"|(開發者|開發人員|除錯|管理員|上帝|越獄|無限制|不受限)\s*模式|\bdeveloper\s+mode\b|\bjailbreak\b|\bgod\s+mode\b"
    r"|(系統|隱藏|內部|完整|原始|你的|你收到的)\s*(提示詞?|指令|prompt)|\bsystem\s+prompt\b"
    r"|\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,25}\b(instructions?|rules?|prompts?|guidelines?|restrictions?)\b"
    r"|\byou\s+are\s+now\b|\bpretend\s+(to\s+be|you)\b|\bact\s+as\b")

_WRITE_VERB = r"(刪除|刪掉|清空|清除|竄改|篡改|修改|更改|改成|改掉|新增|補登|偽造|寫入|覆寫|移除|重設|抹除|銷毀)"
_WRITE_OBJECT = r"(紀錄|記錄|打卡|資料|資料庫|資料表|名單|出勤|異常|事件|證據|員工|帳號|權限|人臉|照片|時間|狀態|欄位)"
_WRITE = re.compile(_WRITE_VERB + r"[^。！？\n]{0,10}" + _WRITE_OBJECT
                    + "|" + _WRITE_OBJECT + r"[^。！？\n]{0,6}" + _WRITE_VERB)
# 詢問規定、法律、可不可以的句子是在問文件，不是要動資料
_POLICY_CUE = re.compile(r"規定|法規|法律|依法|條文|辦法|合法|違法|是否|可不可以|能不能|可以.{0,12}嗎|權益|罰|責任")
# 有明確的命令口氣就不當成在問規定（避免加一句「可以嗎」就繞過）
_COMMAND = re.compile(r"請|幫我|替我|麻煩|給我|^把|，把|直接")

_PRIVATE_THINGS = (r"(金鑰|api\s*key|apikey|密碼|口令|token|憑證|特徵向量|人臉特徵|臉部特徵|embedding|原始碼"
                   r"|source\s*code|資料庫檔|環境變數|設定檔)")
_SHOW = r"(顯示|輸出|告訴|給我|列出|匯出|傳送|傳給|貼出|洩漏|透露|dump|print|show|reveal|leak|cat)"
_EXFIL = re.compile(_SHOW + r"[^。！？\n]{0,12}" + _PRIVATE_THINGS
                    + "|" + _PRIVATE_THINGS + r"[^。！？\n]{0,8}" + _SHOW
                    + r"|\.\./|/etc/passwd|[a-z]:\\|\.(db|sqlite|env|pem|json)\b|\b(runtime|data)\s*/"
                    + "|" + _SHOW + r"[^。！？\n]{0,10}(資料表|全部欄位|所有欄位|全部資料|整個資料庫|檔案內容)"
                    + r"|(資料夾|目錄)[^。！？\n]{0,8}(內容|檔案)|base64")


def _normalise(text: str) -> str:
    """全形轉半形、去掉零寬字元、英文轉小寫，讓規則不被排版花樣繞過。"""
    return unicodedata.normalize("NFKC", text or "").translate(_ZERO_WIDTH).lower()


def screen(question: str) -> Optional[str]:
    """回傳命中的類型（sql、payload、override、write、exfiltrate），沒有命中回傳 None。"""
    text = _normalise(question)
    if _SQL.search(text):
        return "sql"
    if _PAYLOAD.search(text):
        return "payload"
    if _OVERRIDE.search(text):
        return "override"
    if _EXFIL.search(text):
        return "exfiltrate"
    if _WRITE.search(text) and (_COMMAND.search(text) or not _POLICY_CUE.search(text)):
        return "write"
    return None


REFUSAL = ("這個要求我不會執行。我只能唯讀查詢公司文件與考勤資料，不能修改或刪除資料，"
           "也不會輸出金鑰、系統設定或內部資料，更不會因為問題裡的指令而改變規則。\n\n")
