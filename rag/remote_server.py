"""
remote_server.py：專題RAG測試的遠端入口（企業知識庫與出勤查詢的網頁版，Flask，手機與電腦都能用）。

主要功能：
  - 組員用自己的存取金鑰登入（金鑰預設 7 天到期），取得 HttpOnly、SameSite=Strict 的工作階段 Cookie；
    15 分鐘沒操作或登入滿 8 小時就失效；登入失敗依來源位址計次並鎖定，另有不看來源的全站失敗預算
  - 經 Tailscale Funnel 等本機代理對外時用 --behind-proxy：Cookie 加 Secure 與 __Host- 前綴、送 HSTS、
    只信任本機代理連線附上的來源標頭
  - Host 白名單、Origin 同源、POST 必須是 JSON；回應帶內容安全政策等標頭並禁止搜尋引擎收錄；登入事件寫入安全事件紀錄
  - 每個工作階段有自己的 Assistant 與對話記憶（互不影響），共用同一份向量庫、重排與語言模型
  - 問答經過流量閘門（同時處理數與排隊上限），網頁顯示「前面還有幾人」；提問、回答與檢索資訊預設寫入
    runtime/remote_questions.jsonl（--no-log-answers 只記提問、--no-log 都不記）
  - 只有提問與回答：沒有設定頁、不能改語言模型、不能上傳文件；內部錯誤只在伺服器端印出，不回給使用者
  - 命令列管理金鑰：--add-key、--list-keys、--revoke-key
  這是測試用入口，不是完整的帳號系統：金鑰外洩就等於能查詢；用 Flask 內建伺服器，只適合少數人。

串接模組：
  - remote_access：金鑰庫、流量閘門、提問紀錄、GatedAssistant
  - chat_session：ChatController（背景執行問答）、plain_text（整理 Markdown 標記）
  - bootstrap：載入向量模型、重排與語言模型
  - remote_web/：網頁、腳本與樣式（腳本與樣式獨立成檔，頁面不含內嵌程式，方便設內容安全政策）
啟動：python -m rag.remote_server（先用 --add-key 名稱 建立金鑰）；
對外請另開 tailscale funnel，並加 --behind-proxy --allowed-host 網域
"""
import argparse
import datetime
import ipaddress
import os
import re
import secrets
import threading
import time
from typing import Callable, Dict, Iterable, Optional
from urllib.parse import urlsplit

from flask import Flask, jsonify, make_response, request, send_from_directory

from rag import answer, chat_session, remote_access
from shared import auth

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "remote_web")
STATIC_FILES = {"app.js", "style.css"}
COOKIE_NAME = "sa_remote"
PROXY_COOKIE_NAME = "__Host-sa_remote"      # 經反向代理對外時使用：瀏覽器保證只在 https、不帶 Domain 時才收下
SESSION_IDLE_SECONDS = 15 * 60
SESSION_MAX_SECONDS = 8 * 3600              # 不論有沒有操作，登入後最長這麼久就要重新登入
MAX_REJECTED_HOSTS_SHOWN = 20               # 被 Host 白名單拒絕時，終端機最多印幾種不同的 Host
MAX_GUARDS = 1000                          # 依來源位址記錄的登入失敗計數最多保留幾個
LOOPBACK = ("127.0.0.1", "::1")
HOSTNAME_PATTERN = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?")   # 主機名稱本身，不含協定、斜線、埠號
HSTS_SECONDS = 86400
MAX_SESSIONS = 20
MAX_SESSIONS_PER_USER = 3                   # 同一把金鑰最多同時登入幾台裝置，超過就擠掉最舊的
QUESTION_COOLDOWN_SECONDS = 3.0
MAX_TURNS_KEPT = 30
MAX_BODY_BYTES = 4096
DEFAULT_PORT = 8765
WILDCARD_HOSTS = ("", "0.0.0.0", "::")  # noqa: S104  # nosec B104  拒絕綁定的位址清單，不是綁定
ERROR_TEXT = "處理時發生錯誤，請稍後再試。"

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Robots-Tag": "noindex, nofollow",
}


class UserSession:
    """一個登入中的使用者：名稱、自己的對話控制，以及網頁要顯示的歷史與目前這題。"""

    def __init__(self, label: str, assistant, gate, log, now: float):
        self.label = label
        self.token = secrets.token_urlsafe(32)
        self.gated = remote_access.GatedAssistant(assistant, gate, log, label)
        self.controller = chat_session.ChatController(self.gated)
        self.created = self.last_activity = now
        self.fingerprint: Optional[str] = None   # 登入當下金鑰的雜湊，之後每次請求重新比對
        self.last_submit = float("-inf")
        self.turns: list = []
        self.current: Optional[dict] = None
        self._lock = threading.RLock()   # 同一個人的網頁可能同時發出兩個請求

    def collect(self):
        """把背景執行緒累積的事件整理進 turns／current。"""
        with self._lock:
            for ev in self.controller.poll():
                if ev.kind == "start":
                    self.current = {"q": ev.text, "a": ""}
                elif ev.kind == "chunk" and self.current:
                    self.current["a"] += ev.text
                elif ev.kind in ("done", "error") and self.current:
                    if ev.kind == "error":
                        print(f"[remote] {self.label} 的問答出錯：{ev.text}")
                        self.current["a"] = ERROR_TEXT
                    self.turns.append({"q": self.current["q"], "a": chat_session.plain_text(self.current["a"])})
                    self.turns = self.turns[-MAX_TURNS_KEPT:]
                    self.current = None

    def view(self) -> dict:
        """網頁要顯示的內容：歷史、目前這題（含前面還有幾人）、是否處理中。"""
        with self._lock:
            self.collect()
            current = None
            if self.current:
                current = {"q": self.current["q"], "a": chat_session.plain_text(self.current["a"]),
                           "ahead": self.gated.ahead()}
            return {"turns": list(self.turns), "current": current, "busy": self.controller.busy}

    def clear(self):
        with self._lock:
            self.turns, self.current = [], None


def create_app(keystore: remote_access.KeyStore, assistant_factory: Callable[[str], object],
               gate: remote_access.Gate, log: remote_access.QuestionLog,
               clock: Callable[[], float] = time.time, allowed_hosts: Optional[Iterable[str]] = None,
               behind_proxy: bool = False, events: Optional[remote_access.EventLog] = None,
               budget: Optional[remote_access.FailureBudget] = None) -> Flask:
    """allowed_hosts：允許的 Host 名稱（不含埠號），不在清單內的請求一律拒絕；None 表示不檢查（測試用）。
    behind_proxy：經本機的反向代理（例如 Tailscale Funnel）對外時開啟：Cookie 一律加 Secure 並用 __Host- 前綴、
    送出 HSTS，登入失敗改依代理附上的來源位址計次（只信任來自本機代理連線的 X-Forwarded-For 最右邊一筆）。"""
    app = Flask(__name__)
    hosts = {h.lower() for h in allowed_hosts} if allowed_hosts is not None else None
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    cookie_name = PROXY_COOKIE_NAME if behind_proxy else COOKIE_NAME
    events = events or remote_access.EventLog()
    budget = budget or remote_access.FailureBudget()
    sessions: Dict[str, UserSession] = {}
    rejected_hosts: set = set()
    guards: Dict[str, auth.LoginGuard] = {}
    lock = threading.Lock()

    def reply(payload: dict, status: int = 200):
        resp = make_response(jsonify(payload), status)
        for k, v in HEADERS.items():
            resp.headers[k] = v
        return resp

    def purge(now: float):
        expired = [t for t, s in sessions.items()
                   if now - s.last_activity >= SESSION_IDLE_SECONDS or now - s.created >= SESSION_MAX_SECONDS]
        for token in expired:
            del sessions[token]

    def current_session() -> Optional[UserSession]:
        now = clock()
        with lock:
            purge(now)
            token = request.cookies.get(cookie_name, "")
            s = sessions.get(token)
            if s and keystore.fingerprint(s.label, now) != s.fingerprint:
                del sessions[token]     # 金鑰已被撤銷、換新或到期：已登入的工作階段一併失效
                return None
            if s:
                s.last_activity = now
            return s

    def client_ip() -> str:
        """登入失敗計次用的來源位址。代理模式下只在連線來自本機代理時，才採用 X-Forwarded-For 最右邊（代理加上的）一筆，
        而且必須是合法位址；其他情況一律用實際連線位址。這個值可能被偽造，所以另有不看來源的全站失敗預算兜底。"""
        peer = request.remote_addr or "?"
        if behind_proxy and peer in LOOPBACK:
            parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
            if parts:
                try:
                    return str(ipaddress.ip_address(parts[-1]))
                except ValueError:
                    pass
        return peer

    def guard_for(ip: str, now: float) -> auth.LoginGuard:
        """取得某個來源的登入失敗計數；數量超過上限時先清掉沒有失敗紀錄的，還是滿就全部歸到同一個桶子。"""
        if ip not in guards and len(guards) >= MAX_GUARDS:
            for key in [k for k, g in guards.items() if g.failures == 0 and g.locked_for(now) <= 0]:
                del guards[key]
            if len(guards) >= MAX_GUARDS:
                ip = "overflow"
        return guards.setdefault(ip, auth.LoginGuard())

    def json_body() -> dict:
        try:
            data = request.get_json(silent=True)
        except RecursionError:      # 巢狀太深的 JSON 不是合法請求，當成沒有內容
            data = None
        return data if isinstance(data, dict) else {}

    @app.before_request
    def guard_request():
        """擋下 Host 不在白名單（DNS 重綁）、Origin 不同源（跨站）、以及不是 JSON 的 POST。"""
        if hosts is not None and (urlsplit("//" + (request.host or "")).hostname or "") not in hosts:
            seen = rejected_hosts
            shown = "".join(c for c in (request.host or "") if c.isprintable())[:60]
            if shown not in seen and len(seen) < MAX_REJECTED_HOSTS_SHOWN:   # 只印前幾種，掃描器灌流量也不會洗版
                seen.add(shown)
                print(f"[remote] 拒絕不在白名單的 Host：{shown!r}（連線位址 {request.remote_addr}）")
            return reply({"ok": False}, 400)
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin is not None and urlsplit(origin).netloc.lower() != (request.host or "").lower():
                return reply({"ok": False}, 403)
            if not request.is_json:
                return reply({"ok": False}, 415)
        return None

    @app.after_request
    def add_headers(resp):
        for k, v in HEADERS.items():
            resp.headers[k] = v
        if behind_proxy:
            resp.headers["Strict-Transport-Security"] = f"max-age={HSTS_SECONDS}"
        return resp

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/robots.txt")
    def robots():
        resp = make_response("User-agent: *\nDisallow: /\n")
        resp.mimetype = "text/plain"
        return resp

    @app.get("/static/<name>")
    def static_file(name):
        if name not in STATIC_FILES:
            return reply({"ok": False}, 404)
        return send_from_directory(WEB_DIR, name)

    @app.post("/api/login")
    def login():
        now = clock()
        ip = client_ip()
        # 先驗證金鑰：正確的金鑰任何時候都能登入，鎖定與預算只管「失敗」，所以有人狂送錯誤金鑰也鎖不住組員。
        # 金鑰有 192 位元，猜不到；鎖定的作用是降低雜訊與留下紀錄，不是主要防線。
        label = keystore.verify(json_body().get("key"), now=now)
        with lock:
            if label is None:
                guard = guard_for(ip, now)
                wait = max(guard.locked_for(now), budget.blocked_for(now))
                if wait > 0:        # 鎖定期間的失敗不再計次也不寫紀錄，紀錄檔不會被灌爆
                    return reply({"ok": False, "error": f"嘗試次數過多，請 {int(wait) + 1} 秒後再試"}, 429)
                was_blocked = budget.blocked_for(now) > 0
                locked = guard.record_failure(now)
                budget.record_failure(now)
                events.record("login_fail", ip=ip, peer=request.remote_addr or "?",
                              xff=request.headers.get("X-Forwarded-For", ""), locked=locked)
                if not was_blocked and budget.blocked_for(now) > 0:
                    events.record("budget_block", seconds=remote_access.BUDGET_BLOCK_SECONDS)
                print(f"[remote] 登入失敗 來源={ip} 連線位址={request.remote_addr}")
                return reply({"ok": False, "error": "金鑰不正確"}, 401)
            if ip in guards:
                guards[ip].record_success()
            purge(now)
            mine = sorted((s for s in sessions.values() if s.label == label), key=lambda s: s.created)
            for old in mine[:max(0, len(mine) - MAX_SESSIONS_PER_USER + 1)]:   # 同一人超過上限，擠掉最舊的
                sessions.pop(old.token, None)
            if len(sessions) >= MAX_SESSIONS:
                return reply({"ok": False, "error": "目前登入的人數已達上限，請稍後再試"}, 503)
            session = UserSession(label, assistant_factory(label), gate, log, now)
            session.fingerprint = keystore.fingerprint(label, now)
            sessions[session.token] = session
        events.record("login_ok", user=label, ip=ip)
        resp = reply({"ok": True, "name": label})
        resp.set_cookie(cookie_name, session.token, httponly=True, samesite="Strict",
                        secure=behind_proxy or request.is_secure)   # 不設 max_age：關閉瀏覽器就失效，期限由伺服器管
        return resp

    @app.post("/api/logout")
    def logout():
        with lock:
            sessions.pop(request.cookies.get(cookie_name, ""), None)
        resp = reply({"ok": True})
        resp.delete_cookie(cookie_name, secure=behind_proxy or request.is_secure, httponly=True, samesite="Strict")
        return resp

    @app.get("/api/state")
    def state():
        s = current_session()
        if s is None:
            return reply({"ok": False}, 401)
        return reply({"ok": True, "name": s.label, **s.view()})

    @app.post("/api/ask")
    def ask():
        s = current_session()
        if s is None:
            return reply({"ok": False}, 401)
        question = json_body().get("question")
        if not isinstance(question, str) or not question.strip():
            return reply({"ok": False, "error": "請輸入問題"}, 400)
        if len(question) > chat_session.MAX_INPUT_CHARS:
            return reply({"ok": False, "error": f"問題太長，上限 {chat_session.MAX_INPUT_CHARS} 字"}, 400)
        now = clock()
        s.collect()
        # 上一題的結束事件還沒收到（current 還在）也算忙碌，否則它會把新問題的內容當成自己的結尾收掉
        if s.controller.busy or s.current is not None or now - s.last_submit < QUESTION_COOLDOWN_SECONDS:
            return reply({"ok": False, "error": "上一題還沒處理完，請稍候"}, 429)
        s.last_submit = now
        if not s.controller.submit(question):
            return reply({"ok": False, "error": "上一題還沒處理完，請稍候"}, 429)
        return reply({"ok": True})

    @app.post("/api/new")
    def new_chat():
        s = current_session()
        if s is None:
            return reply({"ok": False}, 401)
        s.collect()
        if not s.controller.new_chat():
            return reply({"ok": False, "error": "處理中不能開新對話"}, 429)
        s.clear()
        return reply({"ok": True})

    return app


def make_assistant_factory(runtime) -> Callable[[str], answer.Assistant]:
    """每位使用者一個新的 Assistant（各自的對話記憶），共用 runtime 裡已載入的零件。"""
    base = runtime.assistant

    def factory(_label: str) -> answer.Assistant:
        assistant = answer.Assistant(base.store, base.reranker, base.llm, base.resolver, router_llm=base.router_llm)
        assistant.roster_provider = base.roster_provider
        assistant.show_evidence_paths = False   # 遠端不顯示伺服器上的檔案路徑
        assistant.show_internal_details = False  # 遠端不顯示語言模型名稱、位址等內部設定
        return assistant

    return factory


def _expiry_text(expires: Optional[float]) -> str:
    if expires is None:
        return "不會到期"
    when = datetime.datetime.fromtimestamp(expires).strftime("%Y-%m-%d %H:%M")
    return f"{when} 到期" + ("（已過期）" if expires <= time.time() else "")


def _manage_keys(args, keystore: remote_access.KeyStore) -> bool:
    """處理金鑰管理參數；有處理回傳 True。"""
    if args.add_key:
        key = keystore.add(args.add_key, days=args.days)
        print(f"已為「{args.add_key}」建立金鑰，{'不會到期' if not args.days else f'{args.days:g} 天後到期'}"
              f"（只會顯示這一次，請私下交給本人）：\n{key}")
    elif args.revoke_key:
        print("已撤銷。" if keystore.revoke(args.revoke_key) else "找不到這個名稱。")
    elif args.list_keys:
        print("\n".join(f"{label}　{_expiry_text(exp)}" for label, exp in keystore.describe())
              or "（還沒有任何金鑰）")
    else:
        return False
    return True


def check_proxy_args(args):
    """經代理對外的檢查：伺服器只能綁本機（只有代理連得進來，來源標頭才有意義），而且要指定對外網域。"""
    if args.allowed_host and not args.behind_proxy:
        raise SystemExit("--allowed-host 只用在經代理對外的模式，請同時加上 --behind-proxy"
                         "（否則 Cookie 不會加 Secure，所有人也會被當成同一個來源）。")
    for name in args.allowed_host:
        if not HOSTNAME_PATTERN.fullmatch(name):
            raise SystemExit(f"--allowed-host 只能填主機名稱本身（例如 msi.tail1234.ts.net），不能含 https://、斜線、"
                             f"埠號或中文：收到「{name}」。")
    if not args.behind_proxy:
        return
    if args.host not in LOOPBACK:
        raise SystemExit("--behind-proxy 只能搭配 --host 127.0.0.1（伺服器只讓本機的代理連入）。")
    if not args.allowed_host:
        raise SystemExit("--behind-proxy 需要用 --allowed-host 指定對外的網域名稱（例如 Funnel 顯示的網址主機名稱）。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="專題RAG測試的遠端入口")
    parser.add_argument("--host", default="127.0.0.1",
                        help="綁定位址；用 Tailscale Funnel 對外時維持 127.0.0.1，只讓本機的代理連入")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--behind-proxy", action="store_true",
                        help="經本機代理（例如 Tailscale Funnel）對外：Cookie 加 Secure、送 HSTS、依代理給的來源計次")
    parser.add_argument("--allowed-host", action="append", default=[], metavar="名稱",
                        help="額外允許的 Host 名稱（例如 Funnel 的 xxx.ts.net），可重複；預設只有本機位址")
    parser.add_argument("--no-log", action="store_true", help="不記錄提問（連回答也不記）")
    parser.add_argument("--no-log-answers", action="store_true",
                        help="只記提問，不記系統回答與檢索資訊（預設都記，用來檢查 RAG 對不對）")
    parser.add_argument("--allow-all-interfaces", action="store_true", help="允許綁 0.0.0.0（不建議）")
    parser.add_argument("--add-key", metavar="名稱")
    parser.add_argument("--days", type=float, default=remote_access.DEFAULT_KEY_DAYS,
                        help=f"新金鑰的有效天數，預設 {remote_access.DEFAULT_KEY_DAYS}；0 表示不會到期")
    parser.add_argument("--revoke-key", metavar="名稱")
    parser.add_argument("--list-keys", action="store_true")
    args = parser.parse_args(argv)

    keystore = remote_access.KeyStore()
    if _manage_keys(args, keystore):
        return
    if args.host in WILDCARD_HOSTS and not args.allow_all_interfaces:
        raise SystemExit("不接受綁定所有網路介面。請維持 127.0.0.1（搭配 Funnel）或用 Tailscale 位址（100.x.x.x）；"
                         "真的要用請加 --allow-all-interfaces。")
    check_proxy_args(args)
    if not keystore.labels():
        raise SystemExit("還沒有任何金鑰。請先執行：python -m rag.remote_server --add-key 組員名稱")

    from rag import bootstrap

    runtime = bootstrap.create_runtime(progress=print)
    print(f"[remote] {runtime.llm_note}")
    allowed = {args.host, "localhost", "127.0.0.1", *args.allowed_host}
    app = create_app(keystore, make_assistant_factory(runtime), remote_access.Gate(),
                     remote_access.QuestionLog(enabled=not args.no_log, with_answers=not args.no_log_answers),
                     allowed_hosts=allowed, behind_proxy=args.behind_proxy)
    print(f"[remote] 允許的 Host 名稱：{', '.join(sorted(allowed))}")
    log_mode = "關閉" if args.no_log else ("只記提問" if args.no_log_answers else "提問、回答與檢索資訊")
    print(f"[remote] 紀錄：{log_mode}；本機網址 http://{args.host}:{args.port}/"
          f"；{'經代理對外模式' if args.behind_proxy else '直接連線模式'}")
    try:
        from werkzeug.serving import WSGIRequestHandler, run_simple

        class QuietHandler(WSGIRequestHandler):
            def version_string(self):
                return "server"   # 不在回應標頭透露 Werkzeug 與 Python 版本

        run_simple(args.host, args.port, app, threaded=True, request_handler=QuietHandler)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
