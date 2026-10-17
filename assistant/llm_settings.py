"""
llm_settings.py：語言模型（雲端 API 與本機 Ollama）的設定與呼叫層。

主要功能：
  - 設定讀寫：存在 runtime/llm_settings.json（含金鑰，已被 .gitignore 排除，不可上傳）
  - 兩種 LLM 用戶端：OpenAI 相容的雲端 API、本機 Ollama
  - FailoverLLM：雲端 API 失效時自動改用本機模型
  - 連線檢查與測試、錯誤分類並轉成白話說明（金鑰不會印出）
  只用標準函式庫（urllib）直接呼叫雲端 API 與 Ollama，不需要額外的轉接服務。

串接模組：
  - llm：依設定組出助理使用的 LLM
  - admin.assistant_window：設定面板的讀寫與測試按鈕
"""
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request

from shared import paths

SETTINGS_PATH = paths.LLM_SETTINGS_PATH

NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
DEFAULTS = {
    "mode": "local",                       # "api"＝優先用雲端 API（失效自動改本機）／"local"＝只用本機
    "api_base": NVIDIA_BASE,
    "api_key": "",
    "api_model": "google/gemma-4-31b-it",
    "local_model": "qwen2:7b",
    "local_host": "",                      # 空白＝用環境變數 OLLAMA_HOST，再不然 http://127.0.0.1:11434
}
LOCAL_NUM_CTX = 8192  # 固定的上下文長度，每次呼叫都用同一個值，避免 Ollama 重新載入模型


def _log(msg):
    print(f"[LLM設定] {msg}", flush=True)


# --- 設定讀寫 ---
def load_settings():
    """讀取設定，檔案不存在或損壞時用預設值；沒存過設定但有環境變數金鑰時預設用 API 模式"""
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
        s["mode"] = "api"
    return s


def effective_api_key(s):
    """設定檔金鑰優先；沒填且是 NVIDIA 網址時才退回環境變數 NVIDIA_API_KEY"""
    key = (s.get("api_key") or "").strip()
    if not key and "nvidia" in (s.get("api_base") or "").lower():
        key = (os.environ.get("NVIDIA_API_KEY") or "").strip()
    return key


def local_host(s):
    """回傳 Ollama 位址：設定 > 環境變數 OLLAMA_HOST > 預設本機"""
    host = (s.get("local_host") or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip().rstrip("/")
    if not host.startswith("http"):
        host = "http://" + host
    return host


def save_settings(new, clear_key=False):
    """儲存設定：只更新 new 內有的欄位；api_key 留空保留原本的，clear_key=True 才清除"""
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
    # 先寫暫存檔再取代，避免寫到一半損壞
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except Exception:  # noqa: S110
        pass
    return s


def key_status_text(s):
    """回傳金鑰狀態的說明文字（不顯示金鑰本身）"""
    if (s.get("api_key") or "").strip():
        return "已儲存金鑰（為了安全不顯示；留空代表沿用）"
    if effective_api_key(s):
        return "使用環境變數 NVIDIA_API_KEY 的金鑰"
    return "尚未設定金鑰"


# --- 錯誤分類與白話說明 ---
class LLMCallError(Exception):
    def __init__(self, kind, source, detail="", status=None):
        super().__init__(f"{source}:{kind}:{status}:{detail[:200]}")
        self.kind, self.source, self.detail, self.status = kind, source, detail, status


def _classify_http(status, body, source):
    """依 HTTP 狀態碼與內容分類錯誤"""
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
    """把 urllib / socket 例外轉成 LLMCallError"""
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
    """回傳 (一句話原因, [請使用者確認的事項])，全部用白話"""
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
    """把錯誤格式化成「原因 + 請確認事項」的文字"""
    what, todo = explain(err, s)
    if markdown:
        return f"❌ **{what}**\n\n請你自行確認：\n" + "\n".join(f"- {t}" for t in todo)
    return f"{what}\n請自行確認：\n" + "\n".join(f"  - {t}" for t in todo)


def describe_exception(exc, source="api"):
    """把任何例外轉成一行白話原因"""
    err = _classify_exc(exc, source)
    what, _ = explain(err)
    return what


# --- LLM 用戶端：stream_complete 迭代出的物件有 .delta，與 llama_index 一致 ---
class Delta:
    def __init__(self, text):
        self.delta = text
        self.text = text


class OpenAICompatLLM:
    """OpenAI 相容的 /chat/completions（NVIDIA NIM、OpenAI 等相容服務都可用）"""
    source = "api"

    def __init__(self, base_url, api_key, model, timeout=120.0, temperature=0.0, max_tokens=None):
        self.base_url = (base_url or NVIDIA_BASE).rstrip("/")
        self.api_key, self.model = api_key, model
        self.timeout, self.temperature, self.max_tokens = timeout, temperature, max_tokens

    def label(self):
        return f"雲端 API（{self.model}）"

    def stream_complete(self, prompt):
        """串流呼叫，逐段產生 Delta；錯誤轉成 LLMCallError"""
        body = {"model": self.model, "stream": True, "temperature": self.temperature,
                "messages": [{"role": "user", "content": prompt}]}
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        req = urllib.request.Request(  # noqa: S310
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)  # noqa: S310
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
            except Exception:  # noqa: S110
                pass

    def complete(self, prompt):
        return "".join(d.delta for d in self.stream_complete(prompt))


class OllamaChatLLM:
    """本機 Ollama 的 /api/chat，使用與考生模型相同的 num_ctx，避免模型被重新載入"""
    source = "local"

    def __init__(self, host, model, timeout=600.0, temperature=0.0, max_tokens=None):
        self.host, self.model = host.rstrip("/"), model
        self.timeout, self.temperature, self.max_tokens = timeout, temperature, max_tokens

    def label(self):
        return f"本機模型（{self.model}）"

    def stream_complete(self, prompt):
        """串流呼叫，逐段產生 Delta；錯誤轉成 LLMCallError"""
        options = {"temperature": self.temperature, "num_ctx": LOCAL_NUM_CTX}
        if self.max_tokens:
            options["num_predict"] = self.max_tokens
        body = {"model": self.model, "stream": True, "think": False, "options": options,  # 關閉思考模式（qwen3 等）
                "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request(f"{self.host}/api/chat", data=json.dumps(body).encode("utf-8"),  # noqa: S310
                                     method="POST", headers={"Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)  # noqa: S310
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
            except Exception:  # noqa: S110
                pass

    def complete(self, prompt):
        return "".join(d.delta for d in self.stream_complete(prompt))


class FailoverLLM:
    """主要 LLM（雲端 API）失敗就永久改用備援（本機模型）並記錄通知；主要 LLM 的輸出先整段收完才吐出，避免中途失敗混入半截內容"""

    def __init__(self, primary, fallback=None):
        self.primary, self.fallback = primary, fallback
        self.switched = False
        self._notices = []

    def label(self):
        return (self.fallback if self.switched else self.primary).label()

    def pop_notices(self):
        """取出並清空待顯示的切換通知"""
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


# --- 連線檢查與測試按鈕 ---
def _tiny_prompt():
    return "請只回覆兩個字：OK"


def check_api(s, timeout=20.0):
    """檢查雲端 API，回傳 (是否成功, LLMCallError 或 None, 回應文字, 秒數)"""
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
    """列出 Ollama 已安裝的模型，回傳 (是否成功, 名稱清單 或 LLMCallError)"""
    try:
        with urllib.request.urlopen(f"{local_host(s)}/api/tags", timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return True, [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception as e:
        return False, _classify_exc(e, "local")


def _model_installed(req, names):
    """判斷模型是否已安裝（名稱可省略 :latest 或標籤）"""
    if req in names or f"{req}:latest" in names:
        return True
    return ":" not in req and any(n.split(":")[0] == req for n in names)


def check_local(s, actually_generate=False, timeout=180.0):
    """檢查本機模型，回傳 (是否成功, LLMCallError 或 None, 已安裝模型清單, 回應文字, 秒數)；actually_generate=True 才實際生成一次"""
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
    """「測試 API」按鈕：回傳 (是否成功, markdown 訊息)，結果同時印到後台"""
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
    """「測試本機模型」按鈕：第一次可能要等模型載入"""
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
