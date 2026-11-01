"""
後台登入整合測試：真的 Tk 視窗（需要顯示器），鏡頭與視覺模型用假的。
驗證：人臉通過後建立工作階段、非活體失敗與鎖定並留下紀錄、初次設定第一位管理者、鏡頭打不開的說明、
後台登出與閒置逾時、助理視窗跟著登出而關閉、助理視窗沒有登入代碼就拒絕開啟。
"""
import gc
import sqlite3
import time
import types

import numpy as np
import pytest

pytestmark = pytest.mark.integration

ONES = np.ones(4, dtype=np.float32)
FACE = {"x1": 10, "y1": 10, "x2": 90, "y2": 90}


class FakeCamera:
    def __init__(self):
        self.released = False

    def read(self):
        time.sleep(0.002)
        return True, np.zeros((360, 480, 3), dtype=np.uint8)

    def release(self):
        self.released = True


class FakeVision:
    def __init__(self, live=0.99):
        self.live, self.closed = live, False

    def detect(self, frame):
        return "rgb", [dict(FACE)]

    def liveness(self, frame, box):
        return self.live

    def embed(self, rgb, face):
        return ONES

    def close(self):
        self.closed = True


def pump(win, until, seconds=8.0):
    end = time.time() + seconds
    while time.time() < end:
        win.update()
        if until():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(scope="module")
def _tk_root():
    """整個模組共用一個主視窗：同一個行程反覆建立 ttkbootstrap 視窗會讓 Tk 中止（正式程式也只建一個）。"""
    import tkinter

    import ttkbootstrap as ttk

    try:
        app = ttk.Window(themename="superhero")
    except tkinter.TclError:
        pytest.skip("沒有可用的顯示器")
    app.withdraw()
    yield app
    try:
        app.destroy()
    except tkinter.TclError:
        pass
    gc.collect()


@pytest.fixture
def root(_tk_root):
    yield _tk_root
    for child in _tk_root.winfo_children():
        child.destroy()
    _tk_root.unbind_all("<Key>")
    _tk_root.unbind_all("<Button>")
    _tk_root.unbind_all("<Motion>")
    _tk_root.withdraw()
    gc.collect()


@pytest.fixture
def clean_session():
    from shared import auth

    auth.clear_session()
    yield
    auth.clear_session()


def make_login(root, camera=None, vision=None, guard=None, camera_factory=None):
    from admin import login_window

    cam = camera or FakeCamera()
    window = login_window.LoginWindow(
        root, camera_factory=camera_factory or (lambda: cam), vision_factory=lambda: vision or FakeVision(),
        guard=guard)
    return window, cam


def event_types(db):
    con = sqlite3.connect(db)
    rows = [r[0] for r in con.execute("SELECT event_type FROM AnomalyEvents ORDER BY id")]
    con.close()
    return rows


# --- 登入 ---

def test_an_admin_face_logs_in_and_creates_a_session(root, db, add_user, clean_session):
    from shared import auth

    add_user("管理者甲", "A1", embedding=ONES, role="admin")
    window, cam = make_login(root)
    assert pump(root, lambda: window.session is not None)
    assert window.session.admin_name == "管理者甲"
    assert auth.verify_token(window.session.token) == "管理者甲"
    assert pump(root, lambda: cam.released)                   # 登入完成就釋放鏡頭


def test_a_fake_face_is_refused_and_every_failure_is_recorded(root, db, add_user, clean_session):
    add_user("管理者甲", "A1", embedding=ONES, role="admin")
    window, _ = make_login(root, vision=FakeVision(live=0.2))
    assert pump(root, lambda: "login_failed" in event_types(db))
    assert window.session is None
    window._cancel()


def test_repeated_failures_lock_the_login_and_it_is_recorded(root, db, add_user, clean_session):
    from shared import auth

    add_user("管理者甲", "A1", embedding=ONES, role="admin")
    window, _ = make_login(root, vision=FakeVision(live=0.2), guard=auth.LoginGuard(max_failures=2, lock_seconds=60))
    assert pump(root, lambda: "login_locked" in event_types(db), seconds=15)
    assert pump(root, lambda: "秒後再試" in window.status.cget("text"))
    assert window.session is None
    window._cancel()


def test_closing_the_login_window_means_no_session(root, db, add_user, clean_session):
    add_user("管理者甲", "A1", embedding=ONES, role="admin")
    window, _ = make_login(root)
    window._cancel()
    assert window.session is None


def test_a_missing_camera_is_explained_and_can_be_retried(root, db, add_user, clean_session):
    add_user("管理者甲", "A1", embedding=ONES, role="admin")
    window, _ = make_login(root, camera_factory=lambda: None)
    assert pump(root, lambda: "打不開鏡頭" in window.status.cget("text"))
    assert window.retry.winfo_ismapped()
    window._cancel()


# --- 初次設定 ---

def test_with_no_admin_the_first_one_is_chosen_then_logs_in_by_face(root, db, add_user, clean_session, monkeypatch):
    from tkinter import messagebox

    add_user("組員乙", "E2", embedding=ONES)
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    window, _ = make_login(root)
    assert pump(root, lambda: hasattr(window, "setup_button"))
    window.setup_button.invoke()
    assert pump(root, lambda: window.session is not None)
    assert window.session.admin_name == "組員乙"
    assert "admin_changed" in event_types(db)


def test_with_no_employees_the_setup_screen_explains_what_to_do(root, db, clean_session):
    window, _ = make_login(root)
    texts = [w.cget("text") for w in window.body.winfo_children() if "text" in w.keys()]
    assert any("還沒有可用的員工" in t for t in texts)
    window._cancel()


# --- 後台：登出與閒置逾時 ---

def make_backend(root, name="管理者甲"):
    from admin.backend_main import BackendManagerApp
    from shared import auth

    session = auth.Session(name)
    auth.save_session(session)
    root.deiconify()
    return BackendManagerApp(root, session), session


def test_logout_clears_the_session_and_asks_for_a_new_login(root, db, clean_session, monkeypatch):
    from admin import service_manager
    from shared import auth

    monkeypatch.setattr(service_manager, "stop_assistant_window", lambda: True)
    app, session = make_backend(root)
    app.logout()
    assert app.relogin is True and app.session is None
    assert auth.verify_token(session.token) is None


def test_an_expired_session_logs_the_backend_out(root, db, clean_session, monkeypatch):
    from admin import service_manager
    from shared import auth

    monkeypatch.setattr(service_manager, "stop_assistant_window", lambda: True)
    app, session = make_backend(root)
    session.touch(time.time() - 2 * auth.SESSION_TIMEOUT_SECONDS)
    auth.save_session(session)                                # 檔案裡的最後活動時間太舊
    app._tick_session()
    assert app.relogin is True


def test_activity_in_the_backend_keeps_the_session_alive(root, db, clean_session):
    from shared import auth

    app, session = make_backend(root)
    app._last_touch = 0.0
    app._note_activity()
    assert auth.verify_token(session.token) == "管理者甲"


def test_the_backend_still_builds_without_login_protection(root, db):
    from admin.backend_main import BackendManagerApp

    root.deiconify()
    app = BackendManagerApp(root)
    assert app.session is None and app.relogin is False


# --- 助理視窗 ---

class FakeStore:
    documents = []

    def __len__(self):
        return 0


class FakeRuntime:
    def __init__(self):
        self.assistant = types.SimpleNamespace(last=types.SimpleNamespace(outcome="answered"))
        self.store = FakeStore()
        self.sync = types.SimpleNamespace(failed={})
        self.llm_note = ""

    def close(self):
        pass


def test_the_assistant_window_closes_when_the_session_ends(root, clean_session):
    import tkinter

    from rag import chat_window
    from shared import auth

    session = auth.Session("管理者甲")
    auth.save_session(session)
    win = chat_window.AssistantWindow(runtime_factory=lambda progress: FakeRuntime(), auto_start_ollama=False,
                                      session_token=session.token)
    win.update()
    auth.clear_session()                                      # 後台登出
    win._check_session()
    with pytest.raises(tkinter.TclError):
        win.title()                                           # 視窗已銷毀


def test_the_assistant_window_refuses_to_start_without_a_login_code(monkeypatch):
    import tkinter
    from tkinter import messagebox

    from rag import chat_window
    from shared import auth

    shown = []
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    monkeypatch.setattr(messagebox, "showerror", lambda title, text, **k: shown.append((title, text)))
    try:
        with pytest.raises(SystemExit) as stop:
            chat_window.main()
    except tkinter.TclError:
        pytest.skip("沒有可用的顯示器")
    assert stop.value.code == 2 and shown and "管理後台" in shown[0][1]
