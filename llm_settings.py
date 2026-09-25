"""
出題官／裁判官 LLM 的彈性設定（雲端 API 或本機 Ollama 模型，API 失效自動改用本機）。

【2026-09-20 新增】原本評估用的出題官／裁判官一律走 LiteLLM Proxy -> NVIDIA API，
金鑰沒設或失效就整輪評估每一題都顯示 API 錯誤。改成：
  1. 使用者在網頁右上角「⚙️ 設定」自己填 API 金鑰（或選「只用本機模型」），設定存在專案根目錄的
     llm_settings.json（只存在你自己的電腦，請不要上傳到 GitHub）。
  2. 按「Execute Evaluate」時先做一次快速連線檢查：API 不能用就自動改用本機 Ollama 模型，
     只在畫面上用一行白話提示，不會再整輪噴 API 錯誤；評估途中 API 才壞掉也會自動切換。
  3. 兩個測試按鈕（測試 API／測試本機模型）：結果同時顯示在設定面板與後台輸出（[LLM設定] 開頭），
     失敗時用白話說明是什麼壞了、什麼沒裝、請使用者自己確認什麼。

只用標準函式庫（urllib）呼叫「OpenAI 相容」介面與 Ollama 原生介面，不需要 LiteLLM，
也不依賴 llama_index 的 OpenAI 類別（它會在本機先檢查模型名稱，NVIDIA 的模型名稱過不了）。
金鑰不會被印到後台或任何錯誤訊息裡。
"""
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(_ROOT, "llm_settings.json")

NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
DEFAULTS = {
    "mode": "local",                       # "api"＝優先用雲端 API（失效自動改本機）／"local"＝只用本機
    "api_base": NVIDIA_BASE,
    "api_key": "",
    "api_model": "google/gemma-4-31b-it",
    "local_model": "qwen2:7b",
    "local_host": "",                      # 空白＝用環境變數 OLLAMA_HOST，再不然 http://127.0.0.1:11434
}
LOCAL_NUM_CTX = 8192  # 跟 main_guardrail_rag 的 Ollama(context_window=8192) 一致，避免 Ollama 為了不同 ctx 重新載入模型


def _log(msg):
    print(f"[LLM設定] {msg}", flush=True)


# ----------------------------------------------------------------------------
# 設定讀寫
# ----------------------------------------------------------------------------
def load_settings():
    s = dict(DEFAULTS)
    file_exists = os.path.isfile(SETTINGS_PATH)
    if file_exists:
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                s.update({k: data[k] for k in DEFAULTS if k in data and isinstance(data[k], str)})
        except Exception as e:
            _log(f"讀取 llm_settings.json 失敗（檔案可能損壞），改用預設值：{e}")
    elif os.environ.get("NVIDIA_API_KEY"):
        s["mode"] = "api"  # 沒存過設定、但環境變數有金鑰：沿用舊行為
    return s


def effective_api_key(s):
    """設定檔的金鑰優先；沒填且是 NVIDIA 網址時才退回環境變數 NVIDIA_API_KEY。"""
    key = (s.get("api_key") or "").strip()
    if not key and "nvidia" in (s.get("api_base") or "").lower():
        key = (os.environ.get("NVIDIA_API_KEY") or "").strip()
    return key


def local_host(s):
    host = (s.get("local_host") or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip().rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    return host


def save_settings(new, clear_key=False):
    """new 只需包含要改的欄位；api_key 留空表示保留原本已存的金鑰（clear_key=True 才清除）。"""
    s = load_settings()
    old_key = s.get("api_key", "")
    for k in DEFAULTS:
        if k in new and isinstance(new[k], str):
            s[k] = new[k].strip()
    if clear_key:
        s["api_key"] = ""
    elif not (new.get("api_key") or "").strip():
        s["api_key"] = old_key
    if s["mode"] not in ("api", "local"):
        s["mode"] = "local"
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except Exception:
        pass
    return s


def key_status_text(s):
    if (s.get("api_key") or "").strip():
        return "已儲存金鑰（為了安全不顯示；留空代表沿用）"
    if effective_api_key(s):
        return "使用環境變數 NVIDIA_API_KEY 的金鑰"
    return "尚未設定金鑰"


# ----------------------------------------------------------------------------
# 錯誤分類與白話說明
# ----------------------------------------------------------------------------
class LLMCallError(Exception):
    def __init__(self, kind, source, detail="", status=None):
        super().__init__(f"{source}:{kind}:{status}:{detail[:200]}")
        self.kind, self.source, self.detail, self.status = kind, source, detail, status


def _classify_http(status, body, source):
    low = (body or "").lower()
    if source == "local":
        if status == 404 or "not found" in low:
            return "local_model_missing"
        return "local_other"
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "model_or_url"
    if status == 429:
        return "rate"
    if status in (400, 422):
        return "bad_request"
    if status >= 500:
        return "server"
    return "api_other"


def _classify_exc(exc, source):
    """把 urllib / socket 例外轉成 LLMCallError。"""
    if isinstance(exc, LLMCallError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            body = ""
        return LLMCallError(_classify_http(exc.code, body, source), source, body, exc.code)
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return LLMCallError("timeout", source, str(reason))
    if isinstance(reason, ssl.SSLError):
        return LLMCallError("ssl", source, str(reason))
    if isinstance(reason, socket.gaierror):
        return LLMCallError("ollama_down" if source == "local" else "dns", source, str(reason))
    if isinstance(reason, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, OSError)):
        return LLMCallError("ollama_down" if source == "local" else "network", source, str(reason))
    return LLMCallError("local_other" if source == "local" else "api_other", source, f"{type(exc).__name__}: {exc}")


def explain(err, s=None):
    """回傳 (一句話原因, [請使用者確認的事項])，全部白話。"""
    s = s or load_settings()
    k = err.kind
    model = s.get("local_model", "")
    if k == "empty_key":
        return "還沒有填 API 金鑰。", ["在「⚙️ 設定」貼上你的 API 金鑰再測試；沒有金鑰的話，選「只用本機模型」即可。"]
    if k == "auth":
        return "API 金鑰不被接受：金鑰打錯、已過期，或這把金鑰沒有這個模型的使用權限。", [
            "確認金鑰是完整複製的（前後沒有多的空白或換行）。",
            "到提供 API 的網站（NVIDIA 是 build.nvidia.com）確認金鑰還有效，必要時重新產生一把。",
            "確認你的帳號有權限使用設定裡的那個模型。"]
    if k == "model_or_url":
        return "找不到這個模型，或 API 網址不對。", [
            f"確認 API 網址是否正確（NVIDIA 是 {NVIDIA_BASE}）。",
            f"確認模型名稱拼字完全正確（目前設定：{s.get('api_model')}）。"]
    if k == "rate":
        return "呼叫太頻繁，或這個帳號的免費額度用完了。", ["等幾分鐘後再試，或到 API 網站看帳號用量與額度。"]
    if k == "bad_request":
        return "API 服務收到請求但拒絕處理，最常見是模型名稱格式不對。", [
            f"確認模型名稱格式（目前設定：{s.get('api_model')}），照 API 網站上模型頁面的名稱一字不差地填。"]
    if k == "server":
        return "對方的 API 服務暫時出問題（不是你的設定錯）。", ["過幾分鐘再試；一直不行就改選「只用本機模型」。"]
    if k == "timeout":
        return "等了很久對方都沒有回應。", ["確認這台電腦的網路連線正常。", "如果是本機模型，第一次載入可能要一兩分鐘，可再按一次測試。"]
    if k == "dns":
        return "連不到這個網址：網址拼錯，或這台電腦目前沒有網路。", [
            "確認網路是否連線。", "確認 API 網址有沒有拼錯。"]
    if k == "network":
        return "網路連不上對方的服務：可能斷網，或被公司／學校／場地的防火牆擋住。", [
            "確認網路是否連線，可以先用瀏覽器打開 API 網站看看。", "在有限制網路的場地，請改選「只用本機模型」。"]
    if k == "ssl":
        return "安全連線（憑證）出問題，常見於公司或場地的網路會攔截加密連線。", ["換一個網路再試，或改選「只用本機模型」。"]
    if k == "ollama_down":
        return "連不到本機的 Ollama：它還沒有啟動，或這台電腦還沒有安裝。", [
            "還沒安裝的話，到 https://ollama.com 下載安裝。",
            "已安裝的話，先啟動 Ollama（開始選單搜尋 Ollama 開啟，或在後台按「啟動 RAG」）。",
            f"確認位址是否正確（目前：{local_host(s)}）。"]
    if k == "local_model_missing":
        return f"Ollama 有在運作，但還沒有下載「{model}」這個模型。", [
            f"在 PowerShell 執行：ollama pull {model}（下載需要一些時間與硬碟空間）。",
            "下載完成後再按一次測試。"]
    if k == "local_other":
        return "本機模型呼叫失敗（原因不明）。", [
            "確認 Ollama 正常運作、記憶體／顯存是否足夠（可先關掉其他吃資源的程式）。",
            f"技術訊息：{err.detail[:150]}"]
    return "呼叫 API 失敗（原因不明）。", [f"技術訊息：{err.detail[:150]}"]


def format_error(err, s=None, markdown=True):
    what, todo = explain(err, s)
    if markdown:
        return f"❌ **{what}**\n\n請你自行確認：\n" + "\n".join(f"- {t}" for t in todo)
    return f"{what}\n請自行確認：\n" + "\n".join(f"  - {t}" for t in todo)


def describe_exception(exc, source="api"):
    """給 evaluate_rag 用：把任何例外變成一行白話原因。"""
    err = _classify_exc(exc, source)
    what, _ = explain(err)
    return what


# ----------------------------------------------------------------------------
# LLM 用戶端（stream_complete 介面跟 llama_index 一樣：迭代出來的物件有 .delta）
# ----------------------------------------------------------------------------
class Delta:
    def __init__(self, text):
        self.delta = text
        self.text = text


class OpenAICompatLLM:
    """OpenAI 相容的 /chat/completions（NVIDIA NIM、OpenAI、其他相容服務都可用）。"""
    source = "api"

    def __init__(self, base_url, api_key, model, timeout=120.0, temperature=0.0, max_tokens=None):
        self.base_url = (base_url or NVIDIA_BASE).rstrip("/")
        self.api_key, self.model = api_key, model
        self.timeout, self.temperature, self.max_tokens = timeout, temperature, max_tokens

    def label(self):
        return f"雲端 API（{self.model}）"

    def stream_complete(self, prompt):
        body = {"model": self.model, "stream": True, "temperature": self.temperature,
                "messages": [{"role": "user", "content": prompt}]}
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except Exception as e:
            raise _classify_exc(e, "api") from None
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                choices = obj.get("choices") or [{}]
                piece = (choices[0].get("delta") or {}).get("content") or ""
                if piece:
                    yield Delta(piece)
        except Exception as e:
            raise _classify_exc(e, "api") from None
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def complete(self, prompt):
        return "".join(d.delta for d in self.stream_complete(prompt))


class OllamaChatLLM:
    """本機 Ollama 原生 /api/chat（可指定 num_ctx，跟考生模型用同一組設定，避免模型被重新載入）。"""
    source = "local"

    def __init__(self, host, model, timeout=600.0, temperature=0.0, max_tokens=None):
        self.host, self.model = host.rstrip("/"), model
        self.timeout, self.temperature, self.max_tokens = timeout, temperature, max_tokens

    def label(self):
        return f"本機模型（{self.model}）"

    def stream_complete(self, prompt):
        options = {"temperature": self.temperature, "num_ctx": LOCAL_NUM_CTX}
        if self.max_tokens:
            options["num_predict"] = self.max_tokens
        body = {"model": self.model, "stream": True, "options": options,
                "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request(f"{self.host}/api/chat", data=json.dumps(body).encode("utf-8"),
                                     method="POST", headers={"Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except Exception as e:
            raise _classify_exc(e, "local") from None
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("error"):
                    err_text = str(obj["error"])
                    kind = "local_model_missing" if "not found" in err_text.lower() else "local_other"
                    raise LLMCallError(kind, "local", err_text)
                piece = (obj.get("message") or {}).get("content") or ""
                if piece:
                    yield Delta(piece)
                if obj.get("done"):
                    break
        except LLMCallError:
            raise
        except Exception as e:
            raise _classify_exc(e, "local") from None
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def complete(self, prompt):
        return "".join(d.delta for d in self.stream_complete(prompt))


class FailoverLLM:
    """
    主要 LLM 失敗（API 壞了）就永久改用備援（本機模型），並記一則通知讓畫面用一行白話提示。
    主要 LLM 的輸出會先整段收完才吐出（這樣中途失敗不會留下一半的內容混進答案）。
    """

    def __init__(self, primary, fallback=None):
        self.primary, self.fallback = primary, fallback
        self.switched = False
        self._notices = []

    def label(self):
        return (self.fallback if self.switched else self.primary).label()

    def pop_notices(self):
        n, self._notices = self._notices, []
        return n

    def _switch(self, err):
        self.switched = True
        what, _ = explain(err)
        msg = f"⚠️ 雲端 API 中途無法使用（{what}），已自動改用{self.fallback.label()}繼續。"
        _log(msg)
        self._notices.append(msg)

    def stream_complete(self, prompt):
        if not self.switched and self.fallback is not None:
            try:
                text = "".join(d.delta for d in self.primary.stream_complete(prompt))
            except LLMCallError as e:
                self._switch(e)
            else:
                yield Delta(text)
                return
        target = self.fallback if self.switched else self.primary
        yield from target.stream_complete(prompt)

    def complete(self, prompt):
        return "".join(d.delta for d in self.stream_complete(prompt))


# ----------------------------------------------------------------------------
# 連線檢查與測試按鈕
# ----------------------------------------------------------------------------
def _tiny_prompt():
    return "請只回覆兩個字：OK"


def check_api(s, timeout=20.0):
    """回傳 (ok, LLMCallError|None, 回應文字, 秒數)。"""
    key = effective_api_key(s)
    if not key:
        return False, LLMCallError("empty_key", "api"), "", 0.0
    llm = OpenAICompatLLM(s["api_base"], key, s["api_model"], timeout=timeout, max_tokens=8)
    t0 = time.time()
    try:
        text = llm.complete(_tiny_prompt())
    except LLMCallError as e:
        return False, e, "", time.time() - t0
    return True, None, text.strip(), time.time() - t0


def list_local_models(s, timeout=4.0):
    """回傳 (ok, 模型名稱清單 或 LLMCallError)。"""
    try:
        with urllib.request.urlopen(f"{local_host(s)}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return True, [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception as e:
        return False, _classify_exc(e, "local")


def _model_installed(req, names):
    if req in names or f"{req}:latest" in names:
        return True
    return ":" not in req and any(n.split(":")[0] == req for n in names)


def check_local(s, actually_generate=False, timeout=180.0):
    """回傳 (ok, LLMCallError|None, 已安裝模型清單, 回應文字, 秒數)。"""
    ok, res = list_local_models(s)
    if not ok:
        return False, res, [], "", 0.0
    names = res
    if not _model_installed(s["local_model"], names):
        return False, LLMCallError("local_model_missing", "local", ""), names, "", 0.0
    if not actually_generate:
        return True, None, names, "", 0.0
    llm = OllamaChatLLM(local_host(s), s["local_model"], timeout=timeout, max_tokens=8)
    t0 = time.time()
    try:
        text = llm.complete(_tiny_prompt())
    except LLMCallError as e:
        return False, e, names, "", time.time() - t0
    return True, None, names, text.strip(), time.time() - t0


def test_api(s=None):
    """「測試 API」按鈕。回傳 (ok, markdown)，同時把結果印到後台。"""
    s = s or load_settings()
    _log(f"開始測試雲端 API（{s['api_base']}，模型 {s['api_model']}）...")
    ok, err, text, sec = check_api(s)
    if ok:
        msg = f"✅ **API 測試成功**（模型 `{s['api_model']}`，回應「{text[:20]}」，花了 {sec:.1f} 秒）"
        _log(f"API 測試成功（{sec:.1f} 秒）")
    else:
        msg = "**API 測試失敗**\n\n" + format_error(err, s)
        _log("API 測試失敗：\n" + format_error(err, s, markdown=False))
    return ok, msg


def test_local(s=None):
    """「測試本機模型」按鈕。第一次可能要等模型載入。"""
    s = s or load_settings()
    _log(f"開始測試本機模型 {s['local_model']}（{local_host(s)}），第一次可能要等模型載入...")
    ok, err, names, text, sec = check_local(s, actually_generate=True)
    if ok:
        msg = f"✅ **本機模型測試成功**（`{s['local_model']}`，回應「{text[:20]}」，花了 {sec:.1f} 秒）"
        _log(f"本機模型測試成功（{sec:.1f} 秒）")
    else:
        extra = ""
        if err.kind == "local_model_missing" and names:
            extra = "\n\n目前 Ollama 裡已有的模型：" + "、".join(f"`{n}`" for n in names)
        msg = "**本機模型測試失敗**\n\n" + format_error(err, s) + extra
        _log("本機模型測試失敗：\n" + format_error(err, s, markdown=False)
             + (("\n  已安裝的模型：" + "、".join(names)) if extra else ""))
    return ok, msg


# ----------------------------------------------------------------------------
# 給 evaluate_rag.py：組出出題官／裁判官
# ----------------------------------------------------------------------------
def build_judge_llm(s=None):
    """
    回傳 (llm 或 None, 給畫面看的一行說明)。llm 為 None 代表連本機模型也不能用，評估無法進行。
    規則：模式＝api 且金鑰可用 -> 用 API（本機模型可用時當備援）；API 不能用 -> 自動改本機（一行白話提示，
    不是錯誤）；模式＝local -> 直接本機。
    """
    s = s or load_settings()
    local_ok, local_err, _names, _t, _sec = check_local(s)
    local_llm = OllamaChatLLM(local_host(s), s["local_model"]) if local_ok else None

    note_prefix = ""
    if s["mode"] == "api":
        ok, err, _text, _sec2 = check_api(s, timeout=15.0)
        if ok:
            api_llm = OpenAICompatLLM(s["api_base"], effective_api_key(s), s["api_model"])
            _log(f"評估使用雲端 API（{s['api_model']}）" + ("，本機模型待命當備援。" if local_llm else "，沒有可用的本機備援。"))
            return FailoverLLM(api_llm, local_llm), f"出題官／裁判官：{api_llm.label()}" + (
                "（API 失效時自動改用本機模型）" if local_llm else "")
        what, _ = explain(err, s)
        note_prefix = f"雲端 API 目前無法使用（{what}）。"
        _log(note_prefix + "自動改用本機模型。")

    if local_llm is None:
        _log("本機模型也無法使用，評估無法進行：\n" + format_error(local_err, s, markdown=False))
        return None, (f"{note_prefix}\n\n" if note_prefix else "") + "🛑 **本機模型也無法使用，評估無法進行。**\n\n" \
            + format_error(local_err, s) + "\n\n（右上角 ⚙️ 設定可以測試與修改）"
    if note_prefix:
        text = f"ℹ️ {note_prefix}已自動改用{local_llm.label()}當出題官／裁判官（不影響評估進行）。"
    elif s["mode"] == "api":
        text = f"出題官／裁判官：{local_llm.label()}"
    else:
        text = f"出題官／裁判官：{local_llm.label()}（設定為「只用本機模型」）"
    text += "\n\n⚠️ 出題、答題、評分都是同一顆本機模型，評分會偏寬鬆；要正式的評估數字，建議用雲端 API。"
    return local_llm, text
