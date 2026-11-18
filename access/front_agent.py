"""
front_agent.py：前台代理——和後台保持連線，同步人臉與晨報、補送離線事件、校正時鐘，並提供狀態給後台查詢。

主要功能：
  - BackClient：用通行碼呼叫後台（link_server）；連線錯誤、逾時、5xx 是「暫時失敗」（BackOffline，可重試），
    400／422 是「永遠不會成功」（BackRejected），401 是通行碼不對（BackAuthError，不丟事件）；不跟隨轉址
  - FrontAgent.sync_once：ping（算時鐘差）→ 人臉快照（帶版本，沒變不重載）→ 晨報 → 補送佇列。
    後台離線時什麼都不丟：事件留在佇列，辨識照用本機快取
  - 時鐘：事件時間 = 前台時鐘 + 最近一次量到的差（取請求前後的中點扣掉網路延遲）；差超過 MAX_CLOCK_SKEW_SECONDS
    狀態會警告；差存在本機，重開機後離線也能沿用
  - submit_punch／submit_event：事件先寫進磁碟佇列才算數，之後由同步迴圈補送（後台在線時一個週期內就送出）
  - 狀態回報：每個同步週期用 POST /v1/heartbeat 把待補送事件數、人臉版本與數量、時鐘差，以及辨識程式寫的心跳
    （辨識是否在跑、鏡頭是否正常；心跳超過 HEARTBEAT_STALE_SECONDS 視為沒在跑）送給後台顯示。
    前台不開任何對外服務、後台也從不主動連前台，呼叫方向永遠是前台往後台
  - 前台不上網：只連設定裡的後台內網位址；位址不是內網就拒絕建立；後台位址填 auto 時用 shared.lan_discovery 自動找，
    後台離線時每 10 秒重新探測（後台位址變了會自己換）

串接模組：
  - shared.link_protocol、shared.role_config：協定與角色設定；access.front_store：快取與佇列
  - admin.link_server：對端；terminal_app（之後 L4）：用 FaceIndex 辨識、submit_punch 打卡、寫心跳
"""
import datetime
import json
import os
import ssl
import sys
import threading
import time

import requests
from requests.adapters import HTTPAdapter

from access.front_store import FaceIndex, FrontStore, Outbox
from shared import lan_discovery, link_protocol as lp, link_tls, paths, role_config

SYNC_SECONDS = 10
BACK_RECOGNITION_KEY = "back_recognition_enabled"   # 後台是否允許人臉辨識（同步時取得，離線沿用最後一次的值；預設允許）
BACK_LOCAL_ONLY_KEY = "back_local_only"      # 後台的全本地開關（同步時取得，離線沿用最後一次的值）
BACK_TALK_KEY = "back_talk_enabled"          # 後台是否開放前台 Smart_Chat 一般對話（同上，預設未開放）
BACK_CHAT_KEY = "back_chat_enabled"          # 後台是否開放前台知識庫問答（同步取得，離線沿用最後的值；預設未開放）
BACK_TARGET_KEY = "back_target"              # 目前連線的後台位址與憑證指紋（代理寫，儀表板的問答讀；都不是祕密）
MAX_BACKOFF_SECONDS = 60
MAX_CLOCK_SKEW_SECONDS = 30
HEARTBEAT_STALE_SECONDS = 15
FLUSH_BATCH = 50
EARLY_FLUSH_SECONDS = 2          # 在線時，佇列裡有新事件最慢這麼久就會送出（不用等完整同步週期）
HEARTBEAT_PATH = os.path.join(paths.RUNTIME_DIR, "front_heartbeat.json")


class BackOffline(Exception):
    """後台連不上或暫時出錯（可以重試）。"""


class BackRejected(Exception):
    """後台明確拒絕這筆事件（重送也不會成功）。"""


class BackAuthError(Exception):
    """通行碼不對（兩台電腦的 SMART_ACCESS_LINK_TOKEN 要一樣）。"""


class ChatClosed(Exception):
    """後台沒有開放知識庫問答，或是舊版後台沒有這個介面（前台一律顯示「未開放」，不報錯）。"""


class ChatBusy(Exception):
    """後台忙碌（busy）或前台送太快（slow_down）；code 是後台回的固定代碼。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class BackCertError(BackAuthError):
    """後台出示的憑證指紋和信任的不一樣（可能是後台重新產生了憑證，也可能有人冒充）；事件留在佇列，不送出任何東西。"""


class PinnedAdapter(HTTPAdapter):
    """只信任指紋相符的那一張憑證（urllib3 的 assert_fingerprint；不看憑證機構與主機名稱），TLS 1.2 以上。"""

    def __init__(self, fingerprint, *args, **kwargs):
        self._fingerprint = link_tls.normalize(fingerprint)
        if not self._fingerprint:
            raise role_config.ConfigError("憑證指紋格式不對（要 64 字元十六進位的 SHA-256）")
        super().__init__(*args, **kwargs)

    def cert_verify(self, conn, url, verify, cert):
        """憑證機構的驗證改由指紋取代（自簽憑證沒有機構可驗）；指紋比對由 urllib3 在連線時強制執行，不符就中斷。"""
        super().cert_verify(conn, url, False, cert)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["assert_fingerprint"] = self._fingerprint
        pool_kwargs["ssl_minimum_version"] = ssl.TLSVersion.TLSv1_2
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


VERIFY_DONE_KEY = "verify_request_done"        # 前台最後做完的自動驗證請求編號


def default_verify(agent):
    """前台自動驗證：本機狀態（數字與開關）、PySide6 版本、Smart_Chat 與 Smart_RAG 各問一題（不記回答內容）。"""
    from access import front_verify

    status = agent.heartbeat_payload()
    running = "執行中" if status["recognition_running"] else "沒在跑"
    camera = "正常" if status["camera_ok"] else "沒有回報"
    lines = [f"前台代理：在線，臉資料 {status['faces']} 筆，待補送 {status['pending_events']}，"
             f"被拒 {status['rejected_events']}",
             f"人臉辨識：{running}；攝影機：{camera}",
             f"時鐘差：{status['clock_offset_seconds']} 秒"]
    try:
        import PySide6
        lines.append(f"PySide6：{PySide6.__version__}")
    except ImportError:
        lines.append("PySide6：沒有安裝（新版儀表板無法啟動）")
    return lines + front_verify.qa_probe()


class BackClient:
    def __init__(self, host, port, token, fingerprint=None, timeout=(3.0, 10.0), session=None):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout, self.session = timeout, session or requests.Session()
        self._injected_session = session is not None
        self.set_address(host, port, fingerprint)

    def set_address(self, host, port, fingerprint=None):
        """設定（或自動尋找到新位址時更換）連線目標；連線一律 HTTPS，只信任這個指紋的憑證，沒有明文後備。"""
        self.host, self.port = role_config.private_ipv4(host, "後台位址"), role_config.valid_port(port, "後台埠")
        self.base = f"https://{self.host}:{self.port}"
        self.fingerprint = link_tls.normalize(fingerprint)
        if not self._injected_session:                       # 測試會注入假的 session，正式使用一定要有指紋
            if not self.fingerprint:
                raise role_config.ConfigError("缺少後台憑證指紋，不會用沒有驗證的連線")
            self.session.close()
            self.session = requests.Session()
            self.session.trust_env = False        # 不吃系統代理設定：經代理時 requests 不會套用指紋比對
            self.session.mount("https://", PinnedAdapter(self.fingerprint))

    def target(self):
        return (self.host, self.port, self.fingerprint)

    def _call(self, method, path, accept=(200,), chat=False, **kwargs):
        """chat=True 是知識庫問答的呼叫：403、404 表示未開放（含舊版後台）、429 表示忙碌；accept 是成功的狀態碼。"""
        try:
            response = self.session.request(method, self.base + path, headers=self.headers, timeout=self.timeout,
                                            allow_redirects=False, **kwargs)
        except requests.exceptions.SSLError:
            raise BackCertError("後台憑證指紋不符（後台重新產生了憑證，或有人冒充後台）") from None
        except requests.RequestException as e:
            raise BackOffline(type(e).__name__) from None
        if response.status_code == 401:
            raise BackAuthError("通行碼不被後台接受")
        if chat and response.status_code in (403, 404):
            raise ChatClosed(f"HTTP {response.status_code}")
        if chat and response.status_code == 429:
            code = self._error_of(response).split(" ")[0]
            raise ChatBusy(code if code in ("busy", "slow_down") else "busy")
        if response.status_code in (400, 409, 413, 422):
            raise BackRejected(f"{response.status_code} {self._error_of(response)}")
        if response.status_code not in accept:
            raise BackOffline(f"HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise BackOffline("回應不是 JSON") from None
        if not isinstance(body, dict):
            raise BackOffline("回應格式不對")
        return body

    @staticmethod
    def _error_of(response):
        try:
            body = response.json()
            return f"{body.get('error', '')} {body.get('message', '')}".strip()
        except (ValueError, AttributeError):
            return ""

    def ping(self):
        return self._call("GET", lp.API_PREFIX + "/ping")

    def faces(self, have=None):
        return self._call("GET", lp.API_PREFIX + "/faces", params={"have": have} if have else None)

    def morning(self):
        return self._call("GET", lp.API_PREFIX + "/morning")

    def verify_report(self, request_id, lines):
        """回傳自動驗證報告（只有短文字行）；後台只收它目前那一次請求的回報，過期的會被拒收（BackRejected）。"""
        return self._call("POST", lp.API_PREFIX + lp.VERIFY_REPORT_PATH, json={"request": request_id, "lines": lines})

    def send_event(self, event):
        return self._call("POST", lp.API_PREFIX + "/events", json=event)

    def heartbeat(self, status):
        return self._call("POST", lp.API_PREFIX + "/heartbeat", json=status)

    def chat_submit(self, question, path=lp.CHAT_PATH):
        """送出問題（只有這一個欄位）；path 預設是知識庫問答，一般對話傳 lp.TALK_PATH；回傳後台的回應（含 job_id）。"""
        return self._call("POST", lp.API_PREFIX + self._qa_path(path), accept=(202,), chat=True,
                          json={"question": question})

    def chat_result(self, job_id, path=lp.CHAT_PATH):
        """取問答的結果；編號不合格直接拒絕，不送出請求。"""
        if not lp.valid_job_id(job_id):
            raise BackRejected("工作編號不合格")
        return self._call("GET", lp.API_PREFIX + self._qa_path(path) + "/" + job_id, chat=True)

    @staticmethod
    def _qa_path(path):
        if path not in (lp.CHAT_PATH, lp.TALK_PATH):
            raise BackRejected("不認得的問答路徑")
        return path


class FrontAgent:
    def __init__(self, store, client, now=datetime.datetime.now, clock=time.time, heartbeat_path=None):
        self.store, self.client, self._now, self._clock = store, client, now, clock
        self.outbox = Outbox(store, now)
        self.heartbeat_path = heartbeat_path or HEARTBEAT_PATH
        self.back_online = False
        self.auth_error = False
        self.cert_error = False
        self.last_error = ""
        self.last_sync_ok = None
        self._index = FaceIndex(store.load_faces())
        self._wake = threading.Event()
        self.verify_fn = default_verify              # 自動驗證要做的事（測試可換掉）；回傳報告文字行
        self._verifying = False

    # --- 時鐘與辨識用的快取 ---
    def clock_offset(self):
        return self.outbox.clock_offset()

    def now(self):
        """校正後的現在時間（事件時間用這個）。"""
        return self.outbox.now()

    @property
    def index(self):
        return self._index

    # --- 送事件 ---
    def submit_event(self, kind, user_id=None, detail="", evidence=None):
        """組出事件並寫進佇列；回傳事件編號。事件時間是校正後的現在。"""
        event_id = self.outbox.submit_event(kind, user_id=user_id, detail=detail, evidence=evidence)
        self._wake.set()
        return event_id

    def submit_punch(self, user_id):
        return self.submit_event("punch", user_id=user_id)

    # --- 同步 ---
    def sync_once(self):
        """做一輪同步並把在線狀態寫進本機檔（辨識程式據此提示）；回傳 True 表示後台在線。"""
        online = self._sync_once()
        self.store.set_link_state(online, now=self._clock)
        self.store.set_meta(REASON_KEY, "" if online else self.offline_reason())
        return online

    def offline_reason(self):
        """連不上後台的原因（一句話，給儀表板顯示；不含位址、通行碼等細節）。"""
        if self.cert_error:
            return "後台憑證指紋不符（後台重新產生了憑證，或有人冒充後台）"
        if self.auth_error:
            return "通行碼不被後台接受（前後台的通行碼要一致）"
        return "連不上後台：後台沒開、網路線沒插好，或防火牆擋住了 TCP 8770"

    def _sync_once(self):
        """做一輪同步；任何暫時失敗都不丟例外。"""
        try:
            self._sync_clock()
            self._sync_faces()
            self._sync_morning()
            self.flush_queue()
            self._send_heartbeat()
        except BackAuthError as e:
            self.back_online, self.auth_error, self.last_error = False, True, str(e)
            self.cert_error = isinstance(e, BackCertError)
            return False
        except BackOffline as e:
            self.back_online, self.auth_error, self.last_error = False, False, str(e)
            return False
        except (BackRejected, lp.ProtocolError) as e:
            self.back_online, self.last_error = True, f"同步資料不合格：{e}"   # 後台在線，但這項資料有問題
            return True
        self.back_online, self.auth_error, self.last_error = True, False, ""
        self.last_sync_ok = self._clock()
        return True

    def _sync_clock(self):
        before = self._now()
        body = self.client.ping()
        after = self._now()
        midpoint = before + (after - before) / 2
        server = lp.parse_ts(body.get("server_time"))
        self.store.set_meta("clock_offset", (server - midpoint).total_seconds())

    def _sync_faces(self):
        body = self.client.faces(have=self.store.faces_version())
        if body.get("unchanged") is True:
            return
        version, faces = lp.parse_faces(body)
        self.store.replace_faces(version, faces)
        self._index = FaceIndex(self.store.load_faces())

    def _sync_morning(self):
        body = self.client.morning()
        brief = body.get("brief")
        if isinstance(brief, dict) or brief is None:
            self.store.set_meta("morning", brief)
        if isinstance(body.get("local_only"), bool):          # 只收布林；缺欄位或別的型別就保留上一次的值
            self.store.set_meta(BACK_LOCAL_ONLY_KEY, body["local_only"])
        if isinstance(body.get("recognition_enabled"), bool):  # 同上：只收布林
            self.store.set_meta(BACK_RECOGNITION_KEY, body["recognition_enabled"])
        if isinstance(body.get("chat_enabled"), bool):         # 同上：只收布林；舊後台沒有這欄位就維持上次的值
            self.store.set_meta(BACK_CHAT_KEY, body["chat_enabled"])
        if isinstance(body.get("talk_enabled"), bool):         # 一般對話開關，同上
            self.store.set_meta(BACK_TALK_KEY, body["talk_enabled"])
        request = body.get("verify_request")                   # 後台下達的自動驗證請求編號（整數，變大才跑）
        if isinstance(request, int) and not isinstance(request, bool):
            self._maybe_verify(request)
        target = {"host": self.client.host, "port": self.client.port, "fingerprint": self.client.fingerprint}
        if self.store.get_meta(BACK_TARGET_KEY) != target:     # 位址沒變就不重寫；儀表板的問答用它找到後台
            self.store.set_meta(BACK_TARGET_KEY, target)

    def _maybe_verify(self, request):
        """收到比上次做過的更大的請求編號，就在背景跑一次自動驗證並回傳；同時只跑一個。"""
        done = self.store.get_meta(VERIFY_DONE_KEY)
        if request <= 0 or self._verifying or (isinstance(done, int) and request <= done):
            return
        self._verifying = True
        threading.Thread(target=self._run_verify, args=(request,), daemon=True).start()

    def _run_verify(self, request):
        try:
            lines = self.verify_fn(self)
            self.client.verify_report(request, lines)
            self.store.set_meta(VERIFY_DONE_KEY, request)
        except (BackOffline, BackAuthError, BackRejected) as e:
            self.last_error = f"自動驗證回傳失敗：{type(e).__name__}"   # 下一輪同步還會再試
        except Exception as e:  # noqa: BLE001  自動驗證出意外不能影響同步
            self.last_error = f"自動驗證出錯：{type(e).__name__}"
            try:
                self.client.verify_report(request, [f"自動驗證出錯（{type(e).__name__}）"])
                self.store.set_meta(VERIFY_DONE_KEY, request)
            except (BackOffline, BackAuthError, BackRejected):
                pass
        finally:
            self._verifying = False

    def heartbeat_payload(self):
        """要回報給後台的狀態（只有數字與開關，不含人臉、姓名或事件內容）。"""
        status = self.status()
        keys = ("pending_events", "rejected_events", "dropped_events", "faces", "faces_version",
                "clock_offset_seconds", "clock_warning", "recognition_running", "camera_ok")
        return {key: status[key] for key in keys}

    def _send_heartbeat(self):
        """回報狀態給後台；後台是舊版（沒有這個功能）或拒收都不影響同步，只有連線類錯誤才往上丟。"""
        try:
            self.client.heartbeat(self.heartbeat_payload())
        except BackRejected as e:
            self.last_error = f"心跳被後台拒收：{e}"

    def flush_queue(self):
        """依序補送；暫時失敗就停下（保持順序），永久失敗的移到 rejected 後繼續下一筆。回傳這次送出的筆數。"""
        sent = 0
        while True:
            batch = self.store.pending(FLUSH_BATCH)
            if not batch:
                return sent
            for seq, event in batch:
                try:
                    self.client.send_event(event)
                except BackRejected as e:
                    self.store.mark_rejected(seq, e)
                    continue
                except (BackOffline, BackAuthError):
                    self.store.mark_attempt(seq)
                    raise
                self.store.mark_sent(seq)
                sent += 1

    def run(self, stop, interval=SYNC_SECONDS):
        """同步迴圈：在線每 interval 秒一輪，離線逐步拉長（最多 MAX_BACKOFF_SECONDS）；有新事件時提早醒來。"""
        delay, next_sync = interval, 0.0
        while not stop.is_set():
            if self._clock() >= next_sync:
                delay = interval if self.sync_once() else min(max(delay * 2, interval), MAX_BACKOFF_SECONDS)
                next_sync = self._clock() + delay
            elif self.back_online and self.store.pending_count():
                self._flush_early()
            self._wake.wait(EARLY_FLUSH_SECONDS)
            self._wake.clear()

    def _flush_early(self):
        try:
            self.flush_queue()
        except (BackOffline, BackAuthError) as e:
            self.back_online, self.last_error = False, str(e)
            self.store.set_link_state(False, now=self._clock)
            return

    # --- 狀態 ---
    def heartbeat(self):
        """辨識程式寫的心跳；太舊或讀不到視為沒在跑。"""
        return read_terminal_heartbeat(self.heartbeat_path, self._clock)

    def status(self):
        skew = self.clock_offset()
        return {"back_online": self.back_online, "token_rejected": self.auth_error, "last_error": self.last_error,
                "last_sync_age_seconds": (None if self.last_sync_ok is None
                                          else round(self._clock() - self.last_sync_ok)),
                "pending_events": self.store.pending_count(), "rejected_events": self.store.rejected_count(),
                "dropped_events": self.store.dropped_count(), "faces": len(self._index),
                "faces_version": (self.store.faces_version() or "")[:12],
                "clock_offset_seconds": round(skew, 1), "clock_warning": abs(skew) > MAX_CLOCK_SKEW_SECONDS,
                **self.heartbeat()}


def read_terminal_heartbeat(path=None, clock=time.time):
    """讀辨識程式寫的心跳（前台代理與儀表板共用）；太舊或讀不到視為沒在跑、鏡頭異常。"""
    try:
        with open(path or HEARTBEAT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        fresh = clock() - float(data["ts"]) <= HEARTBEAT_STALE_SECONDS
    except (OSError, ValueError, KeyError, TypeError):
        return {"recognition_running": False, "camera_ok": False}
    return {"recognition_running": bool(data.get("recognition_running")) and fresh,
            "camera_ok": bool(data.get("camera_ok")) and fresh}


def write_heartbeat(recognition_running, camera_ok, path=None, clock=time.time):
    """辨識程式（terminal_app）定期呼叫，讓代理知道辨識與鏡頭的狀態。"""
    target = path or HEARTBEAT_PATH
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ts": clock(), "recognition_running": bool(recognition_running), "camera_ok": bool(camera_ok)}, f)
    os.replace(tmp, target)


def _wait_for(stop, what, attempt, interval=3.0):
    """重複嘗試直到成功（或被停止）；第一次失敗時印一次原因，網路線剛插好時位址要等 30 到 60 秒。"""
    announced = False
    while not stop.is_set():
        try:
            result = attempt()
        except role_config.ConfigError as e:
            result, reason = None, str(e)
        else:
            reason = "後台沒有回應（後台還沒開、網路線或防火牆）"
        if result:
            return result
        if not announced:
            print(f"[front_agent] 等待{what}：{reason}")
            announced = True
        stop.wait(interval)
    return None


REASON_KEY = "link_reason"                   # 目前連不上後台的原因（給儀表板顯示；連上就清空）
REASON_NOT_FOUND = "找不到後台：後台沒開、網路線沒插好，或後台防火牆沒開 UDP 8772"


def _find_back(stop, store, token, discover=lan_discovery.discover_back):
    """自動模式找後台：先探測一次；探測不到就用上次記住的位址直接試（後台的自動尋找暫時壞了也連得上）；
    都沒有才一直等並在儀表板顯示原因。回傳 (位址, 埠, 憑證指紋) 或 None。"""
    found = discover(token)
    if found:
        return found
    cached = store.get_meta(BACK_TARGET_KEY)
    if isinstance(cached, dict) and cached.get("host") and cached.get("port") and cached.get("fingerprint"):
        print("[front_agent] 自動尋找沒有回應，改用上次記住的後台位址")
        return cached["host"], cached["port"], cached["fingerprint"]
    store.set_meta(REASON_KEY, REASON_NOT_FOUND)
    return _wait_for(stop, "自動找到後台", lambda: discover(token))


def _rediscover_loop(agent, token, stop, interval=10.0, discover=lan_discovery.discover_back):
    """後台離線時重新探測位址（重開機或重插網路線後後台位址可能變了）；找到就換連線目標。"""
    while not stop.wait(interval):
        if agent.back_online:
            continue
        found = discover(token)
        if found and found != agent.client.target():
            agent.client.set_address(*found)
            print(f"[front_agent] 後台位址或憑證更新：{found[0]}:{found[1]}")


def main():
    settings = role_config.load()
    if settings.role != role_config.FRONT:
        print("[front_agent] 目前的角色不是前台（python -m shared.role_config set --role front ...）")
        return 2
    stop = threading.Event()
    try:
        token = role_config.require_token()
        auto = settings.back_host.strip().lower() == role_config.AUTO
        store = FrontStore(os.path.join(paths.RUNTIME_DIR, "front_agent.db"))
        if auto:
            host, port, fingerprint = _find_back(stop, store, token) or (None, None, None)
            if host is None:
                return 0
        else:
            host, port, fingerprint = settings.back_host, settings.back_port, settings.back_fingerprint
        client = BackClient(host, port, token, fingerprint)
        agent = FrontAgent(store, client)
    except role_config.ConfigError as e:
        print(f"[front_agent] 無法啟動：{e}")
        return 2
    except KeyboardInterrupt:
        return 0
    if auto:
        threading.Thread(target=_rediscover_loop, args=(agent, token, stop), daemon=True).start()
    print(f"[front_agent] 前台代理啟動：後台 {client.base}（前台不開任何對外服務，狀態由前台主動回報）")
    try:
        agent.run(stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
