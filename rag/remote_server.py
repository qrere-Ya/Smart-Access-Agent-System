"""
remote_server.py：企業知識庫與出勤查詢的遠端測試入口（Flask 網頁，手機與電腦都能用）。

主要功能：
  - 組員用自己的存取金鑰登入，取得 HttpOnly 的工作階段 Cookie；15 分鐘沒操作就失效，登入失敗依來源位址計次並鎖定
  - 每個工作階段有自己的 Assistant 與對話記憶（互不影響），共用同一份向量庫、重排與語言模型
  - 問答經過流量閘門（同時處理數與排隊上限），網頁顯示「前面還有幾人」；提問寫入提問紀錄
  - 只有提問與回答：沒有設定頁、不能改語言模型、不能上傳文件；內部錯誤只在伺服器端印出，不回給使用者
  - 命令列管理金鑰：--add-key、--list-keys、--revoke-key
  這是測試用入口，不是完整的帳號系統：金鑰外洩就等於能查詢；用 Flask 內建伺服器，只適合少數人。

串接模組：
  - remote_access：金鑰庫、流量閘門、提問紀錄、GatedAssistant
  - chat_session：ChatController（背景執行問答）、plain_text（整理 Markdown 標記）
  - bootstrap：載入向量模型、重排與語言模型
  - remote_web/：網頁、腳本與樣式（腳本與樣式獨立成檔，頁面不含內嵌程式，方便設內容安全政策）
啟動：python -m rag.remote_server（先用 --add-key 名稱 建立金鑰；建議只綁 Tailscale 位址或 127.0.0.1）
"""
import argparse
import os
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
SESSION_IDLE_SECONDS = 15 * 60
MAX_SESSIONS = 20
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
}


class UserSession:
    """一個登入中的使用者：名稱、自己的對話控制，以及網頁要顯示的歷史與目前這題。"""

    def __init__(self, label: str, assistant, gate, log, now: float):
        self.label = label
        self.token = secrets.token_urlsafe(32)
        self.gated = remote_access.GatedAssistant(assistant, gate, log, label)
        self.controller = chat_session.ChatController(self.gated)
        self.last_activity = now
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
               clock: Callable[[], float] = time.time, allowed_hosts: Optional[Iterable[str]] = None) -> Flask:
    """allowed_hosts：允許的 Host 名稱（不含埠號），不在清單內的請求一律拒絕；None 表示不檢查（測試用）。"""
    app = Flask(__name__)
    hosts = {h.lower() for h in allowed_hosts} if allowed_hosts is not None else None
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    sessions: Dict[str, UserSession] = {}
    guards: Dict[str, auth.LoginGuard] = {}
    lock = threading.Lock()

    def reply(payload: dict, status: int = 200):
        resp = make_response(jsonify(payload), status)
        for k, v in HEADERS.items():
            resp.headers[k] = v
        return resp

    def purge(now: float):
        for token in [t for t, s in sessions.items() if now - s.last_activity >= SESSION_IDLE_SECONDS]:
            del sessions[token]

    def current_session() -> Optional[UserSession]:
        now = clock()
        with lock:
            purge(now)
            s = sessions.get(request.cookies.get(COOKIE_NAME, ""))
            if s:
                s.last_activity = now
            return s

    def json_body() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    @app.before_request
    def guard_request():
        """擋下 Host 不在白名單（DNS 重綁）、Origin 不同源（跨站）、以及不是 JSON 的 POST。"""
        if hosts is not None and (urlsplit("//" + (request.host or "")).hostname or "") not in hosts:
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
        return resp

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/static/<name>")
    def static_file(name):
        if name not in STATIC_FILES:
            return reply({"ok": False}, 404)
        return send_from_directory(WEB_DIR, name)

    @app.post("/api/login")
    def login():
        now = clock()
        ip = request.remote_addr or "?"
        with lock:
            guard = guards.setdefault(ip, auth.LoginGuard())
            wait = guard.locked_for(now)
        if wait > 0:
            return reply({"ok": False, "error": f"嘗試次數過多，請 {int(wait) + 1} 秒後再試"}, 429)
        label = keystore.verify(json_body().get("key"))
        with lock:
            if label is None:
                guard.record_failure(now)
                return reply({"ok": False, "error": "金鑰不正確"}, 401)
            guard.record_success()
            purge(now)
            if len(sessions) >= MAX_SESSIONS:
                return reply({"ok": False, "error": "目前登入的人數已達上限，請稍後再試"}, 503)
            session = UserSession(label, assistant_factory(label), gate, log, now)
            sessions[session.token] = session
        resp = reply({"ok": True, "name": label})
        resp.set_cookie(COOKIE_NAME, session.token, httponly=True, samesite="Strict",
                        secure=request.is_secure, max_age=SESSION_IDLE_SECONDS)
        return resp

    @app.post("/api/logout")
    def logout():
        with lock:
            sessions.pop(request.cookies.get(COOKIE_NAME, ""), None)
        resp = reply({"ok": True})
        resp.delete_cookie(COOKIE_NAME)
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
        return assistant

    return factory


def _manage_keys(args, keystore: remote_access.KeyStore) -> bool:
    """處理金鑰管理參數；有處理回傳 True。"""
    if args.add_key:
        key = keystore.add(args.add_key)
        print(f"已為「{args.add_key}」建立金鑰（只會顯示這一次，請私下交給本人）：\n{key}")
    elif args.revoke_key:
        print("已撤銷。" if keystore.revoke(args.revoke_key) else "找不到這個名稱。")
    elif args.list_keys:
        print("\n".join(keystore.labels()) or "（還沒有任何金鑰）")
    else:
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="企業知識庫與出勤查詢的遠端測試入口")
    parser.add_argument("--host", default="127.0.0.1", help="綁定位址；給組員連線請用 Tailscale 位址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--allowed-host", action="append", default=[], metavar="名稱",
                        help="額外允許的 Host 名稱（例如 Tailscale 的主機名稱），可重複；預設只有綁定位址與 localhost")
    parser.add_argument("--no-log", action="store_true", help="不記錄提問")
    parser.add_argument("--allow-all-interfaces", action="store_true", help="允許綁 0.0.0.0（不建議）")
    parser.add_argument("--add-key", metavar="名稱")
    parser.add_argument("--revoke-key", metavar="名稱")
    parser.add_argument("--list-keys", action="store_true")
    args = parser.parse_args(argv)

    keystore = remote_access.KeyStore()
    if _manage_keys(args, keystore):
        return
    if args.host in WILDCARD_HOSTS and not args.allow_all_interfaces:
        raise SystemExit("不接受綁定所有網路介面。請改用 Tailscale 位址（100.x.x.x）或 127.0.0.1；"
                         "真的要用請加 --allow-all-interfaces。")
    if not keystore.labels():
        raise SystemExit("還沒有任何金鑰。請先執行：python -m rag.remote_server --add-key 組員名稱")

    from rag import bootstrap

    runtime = bootstrap.create_runtime(progress=print)
    print(f"[remote] {runtime.llm_note}")
    allowed = {args.host, "localhost", "127.0.0.1", *args.allowed_host}
    app = create_app(keystore, make_assistant_factory(runtime), remote_access.Gate(),
                     remote_access.QuestionLog(enabled=not args.no_log), allowed_hosts=allowed)
    print(f"[remote] 提問紀錄：{'關閉' if args.no_log else '開啟'}；網址 http://{args.host}:{args.port}/")
    try:
        from werkzeug.serving import run_simple

        run_simple(args.host, args.port, app, threaded=True)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
