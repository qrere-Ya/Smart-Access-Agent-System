"""
verify_common.py：驗證當天流程（後台 admin.verify_day、前台 access.front_verify）共用的小工具。

主要功能：
  - scrub：寫進報告前把通行碼、金鑰、雜湊這類長字串蓋掉（報告可能會貼給別人或交給 Claude 分析）
  - normalize／parse_answer／yes_no 判讀：y、n、s（略過）、q（結束）容錯，全形、大小寫、「是／否」都認得
  - Console：互動輸入輸出（可替換成假的，單元測試不需要鍵盤）；輸入結束（EOF）當作 q
  - md_table／atomic_write：產生 Markdown 表格（跳脫 | 與換行）、先寫暫存檔再取代，中途關掉也留得住已完成的內容

串接模組：
  - admin.verify_day（後台引導驗證與報告）、access.front_verify（前台自動收集與報告）
"""
import os
import re
import unicodedata

HIDDEN = "［已隱藏］"
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{40,}")          # 通行碼（至少 43 字元）、金鑰、base64 之類
_HEX_DIGEST = re.compile(r"\b[0-9a-fA-F]{32,}\b")          # 雜湊、指紋

YES = {"y", "yes", "ok", "是", "對", "好", "通過", "可以"}
NO = {"n", "no", "否", "不", "不行", "沒有", "fail", "失敗"}
SKIP = {"s", "skip", "略過", "跳過"}
QUIT = {"q", "quit", "exit", "結束"}


class QuitSession(Exception):
    """使用者要結束（輸入 q，或輸入來源已經結束）。"""


class SkipStep(Exception):
    """使用者要略過這一項（輸入 s）。"""


def scrub(text, secrets=()):
    """把已知的祕密字串與長得像金鑰、雜湊的長字串換成［已隱藏］。"""
    out = str(text)
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, HIDDEN)
    return _HEX_DIGEST.sub(HIDDEN, _LONG_TOKEN.sub(HIDDEN, out))


def normalize(text):
    """全形轉半形、去頭尾空白、轉小寫。"""
    return unicodedata.normalize("NFKC", str(text or "")).strip().lower()


def parse_answer(text):
    """判讀一句回答 → (代碼, 備註)：代碼是 "y"／"n"／"s"／"q"，看不懂回傳 (None, 原文)；其餘文字當備註。"""
    cleaned = normalize(text)
    if not cleaned:
        return None, ""
    first, _, rest = cleaned.partition(" ")
    for code, words in (("y", YES), ("n", NO), ("s", SKIP), ("q", QUIT)):
        if first in words:
            return code, rest.strip()
    if cleaned[0] in "是對好":
        return "y", cleaned[1:].strip()
    if cleaned[0] in "否不沒":
        return "n", cleaned[1:].strip()
    return None, cleaned


class Console:
    """互動輸入輸出；reader／writer 可由測試換成假的。"""

    def __init__(self, reader=input, writer=print):
        self._read, self._write = reader, writer

    def say(self, text=""):
        self._write(text)

    def ask(self, prompt):
        """問一句，回傳去頭尾空白的原文；輸入結束或輸入 q 就丟 QuitSession，輸入 s 丟 SkipStep。"""
        try:
            raw = self._read(prompt)
        except EOFError:
            raise QuitSession() from None
        code, _note = parse_answer(raw)
        if code == "q":
            raise QuitSession()
        if code == "s":
            raise SkipStep()
        return str(raw).strip()

    def pause(self, prompt="做好了按 Enter（s 略過這項、q 結束）"):
        """等人按 Enter。"""
        self.ask(prompt)

    def yes_no(self, question, retries=2):
        """問 y/n（可接備註），回傳 (True／False／None, 備註)；連續看不懂就回傳 (None, 原文)。"""
        text = ""
        for _ in range(retries + 1):
            text = self.ask(f"{question}（y 是／n 否，可接備註；s 略過、q 結束）> ")
            code, note = parse_answer(text)
            if code in ("y", "n"):
                return code == "y", note
            self.say("看不懂，請輸入 y 或 n。")
        return None, text


def md_escape(value):
    """表格儲存格內容：| 跳脫、換行換成 <br>。"""
    return str(value).replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>")


def md_table(headers, rows):
    """Markdown 表格文字。"""
    lines = ["| " + " | ".join(md_escape(h) for h in headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(md_escape(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def atomic_write(path, text):
    """先寫暫存檔再取代（讀的人、或中途被關掉，都不會留下寫一半的檔案）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
