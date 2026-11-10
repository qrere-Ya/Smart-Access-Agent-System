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

    def _call(self, method, path, **kwargs):
        try:
            response = self.session.request(method, self.base + path, headers=self.headers, timeout=self.timeout,
                                            allow_redirects=False, **kwargs)
        except requests.exceptions.SSLError:
            raise BackCertError("後台憑證指紋不符（後台重新產生了憑證，或有人冒充後台）") from None
        except requests.RequestException as e:
            raise BackOffline(type(e).__name__) from None
        if response.status_code == 401:
            raise BackAuthError("通行碼不被後台接受")
        if response.status_code in (400, 413, 422):
            raise BackRejected(f"{response.status_code} {self._error_of(response)}")
        if response.status_code != 200:
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

    def send_event(self, event):
        return self._call("POST", lp.API_PREFIX + "/events", json=event)

    def heartbeat(self, status):
        return self._call("POST", lp.API_PREFIX + "/heartbeat", json=status)


class FrontAgent:
    def __init__(self, store, client, now=datetime.datetime.now, clock=time.time, heartbeat_path=None):
        self.store, self.client, self._now, self._clock = store, client, now, clock
        self.outbox = Outbox(store, now)
        self.heartbeat_path = heartbeat_path or HEARTBEAT_PATH
        self.back_online = False
        self.auth_error = False
        self.last_error = ""
        self.last_sync_ok = None
        self._index = FaceIndex(store.load_faces())
        self._wake = threading.Event()

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
        return online

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
        try:
            with open(self.heartbeat_path, encoding="utf-8") as f:
                data = json.load(f)
            fresh = self._clock() - float(data["ts"]) <= HEARTBEAT_STALE_SECONDS
        except (OSError, ValueError, KeyError, TypeError):
            return {"recognition_running": False, "camera_ok": False}
        return {"recognition_running": bool(data.get("recognition_running")) and fresh,
                "camera_ok": bool(data.get("camera_ok")) and fresh}

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
        if auto:
            host, port, fingerprint = (_wait_for(stop, "自動找到後台", lambda: lan_discovery.discover_back(token))
                                       or (None, None, None))
            if host is None:
                return 0
        else:
            host, port, fingerprint = settings.back_host, settings.back_port, settings.back_fingerprint
        client = BackClient(host, port, token, fingerprint)
        agent = FrontAgent(FrontStore(os.path.join(paths.RUNTIME_DIR, "front_agent.db")), client)
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
