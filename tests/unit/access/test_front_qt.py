"""
前台儀表板（PySide6 + QML 版）的測試：資料模型（卡片順序與模組色、只有變了才發訊號、開關模組、問答面板的顯示規則、
閒置 90／60 秒、辨識按鈕狀態、主題、全螢幕偏好、圖示 SVG、QR）與 QML 畫面真的載得起來（離屏、軟體繪製，不開視窗）。
沒有安裝 PySide6 的環境（例如輕量的 CI）整個檔案略過。
"""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
pytest.importorskip("PySide6")

from PySide6.QtGui import QGuiApplication  # noqa: E402

from access import front_agent, front_preview, front_screens  # noqa: E402
from access.front_qt import app as qapp  # noqa: E402
from access.front_qt import model as qmodel  # noqa: E402
from access.front_store import FrontStore  # noqa: E402
from shared import svg_icon, ui_theme  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QGuiApplication.instance() or QGuiApplication([])


class FakeController:
    def __init__(self, state="idle", availability="idle", answer="", sources=(), message=""):
        self.state, self._availability, self.answer = state, availability, answer
        self.sources, self.message = list(sources), message
        self.submitted, self.cleared = [], 0

    def availability(self):
        return self._availability

    def view(self):
        return {"state": self.state, "message": self.message, "answer": self.answer, "sources": self.sources,
                "can_ask": self.state not in ("asking", "preparing")}

    def submit(self, question):
        if not question or self.state in ("asking", "preparing") or self._availability != "idle":
            return False
        self.submitted.append(question)
        self.state, self.message = "asking", "查詢中…"
        return True

    def clear(self):
        self.cleared += 1
        self.state, self.message, self.answer, self.sources = "idle", "", "", []


@pytest.fixture
def store(tmp_path):
    s = FrontStore(str(tmp_path / "front.db"))
    front_preview.seed(s)
    return s


@pytest.fixture
def clock():
    now = [1_000_000.0]
    return now


def make_model(qt_app, store, clock, **kwargs):
    args = {"read_heartbeat": lambda: {"camera_ok": True, "recognition_running": False},
            "read_resources": lambda: front_preview.FAKE_RESOURCES, "clock": lambda: clock[0],
            "chat": FakeController(), "talk": FakeController()}
    args.update(kwargs)
    return qmodel.DashboardModel(store, **args)


# --- 顯示規則 ---

def test_the_qa_display_rules(qt_app):
    user = {"role": "user", "text": "特休幾天", "sources": []}
    idle = qmodel.qa_display({"state": "idle", "message": "", "answer": "", "sources": [], "can_ask": True}, [])
    assert idle["welcome"] is True and idle["inputVisible"] is True and idle["inputEnabled"] is True
    assert idle["messages"] == [] and idle["thinking"] is False
    asking = qmodel.qa_display({"state": "asking", "message": "查詢中…", "answer": "", "sources": [], "can_ask": False},
                               [user])
    assert asking["inputEnabled"] is False and asking["status"] == "查詢中…" and asking["thinking"] is True
    assert asking["welcome"] is False and asking["messages"] == [user]
    answer = qmodel.qa_display({"state": "answer", "message": "", "answer": "七日", "sources": ["勞動基準法"],
                                "can_ask": True}, [user])
    assert answer["welcome"] is False and answer["status"] == "" and answer["thinking"] is False
    closed = qmodel.qa_display({"state": "closed", "message": "未開放", "answer": "", "sources": [],
                                "can_ask": False}, [])
    assert closed["inputVisible"] is False and closed["welcome"] is False
    offline = qmodel.qa_display({"state": "offline", "message": "後台未連線", "answer": "", "sources": [],
                                 "can_ask": False}, [])
    assert offline["inputVisible"] is False and offline["status"] == "後台未連線"


# --- 模型 ---

def test_the_tiles_are_ordered_chat_rag_news_greet_status_each_with_its_own_color(qt_app, store, clock):
    model = make_model(qt_app, store, clock)
    tiles = model.tiles
    assert [t["key"] for t in tiles] == ["talk", "chat", "news", "greetings", "status"]
    assert [t["title"] for t in tiles[:2]] == ["Smart_Chat", "Smart_RAG"]
    assert len({t["token"] for t in tiles}) == 5                              # 每個模組一個自己的顏色
    for tile in tiles:
        assert tile["token"] in ui_theme.COLORS["dark"] and tile["token"] in ui_theme.COLORS["light"]


def test_properties_only_signal_when_the_content_changed(qt_app, store, clock):
    model = make_model(qt_app, store, clock)
    hits = []
    for name in ("clock", "link", "tiles", "news", "greetings", "status", "resources", "qaTalk", "qaChat"):
        getattr(model, name + "Changed").connect(lambda n=name: hits.append(n))
    model.refresh()
    assert hits == []                                                          # 沒變就不發
    store.set_link_state(False)
    model.refresh()
    assert "link" in hits or "status" in hits


def test_open_and_go_home_and_unknown_keys_are_ignored(qt_app, store, clock):
    talk = FakeController()
    model = make_model(qt_app, store, clock, talk=talk)
    seen = []
    model.currentChanged.connect(lambda: seen.append(model.current))
    model.openModule("nope")
    assert model.current == "" and seen == []
    model.openModule("talk")
    assert model.current == "talk"
    model.goHome()
    assert model.current == "" and talk.cleared == 0                          # 離開問答面板不清，半小時沒人操作才重置
    model.openModule("news")
    model.goHome()
    assert talk.cleared == 0


def test_submit_clear_and_the_panel_cleared_signal(qt_app, store, clock):
    chat = FakeController()
    model = make_model(qt_app, store, clock, chat=chat)
    cleared = []
    model.panelCleared.connect(cleared.append)
    model.openModule("chat")
    assert model.submit("chat", "   ") is False and chat.submitted == []
    assert model.submit("chat", "特休幾天") is True and chat.submitted == ["特休幾天"]
    assert model.qaChat["state"] == "asking" and model.qaChat["messages"][0] == {
        "role": "user", "text": "特休幾天", "sources": []}
    assert model.submit("chat", "第二題") is False                              # 處理中不能重複送
    assert model.submit("nope", "x") is False
    model.clearPanel("chat")
    assert chat.cleared == 1 and cleared == ["chat"]
    assert model.qaChat["welcome"] is True and model.qaChat["messages"] == []


def test_the_talk_and_rag_panels_are_independent(qt_app, store, clock):
    talk, chat = FakeController(), FakeController()
    model = make_model(qt_app, store, clock, talk=talk, chat=chat)
    model.submit("talk", "你好")
    assert talk.submitted == ["你好"] and chat.submitted == []
    assert model.qaTalk["state"] == "asking" and model.qaChat["state"] == "idle"


def test_idle_rules_90_seconds_for_modules_and_half_an_hour_for_the_panels(qt_app, store, clock):
    talk, chat = FakeController(), FakeController()
    model = make_model(qt_app, store, clock, talk=talk, chat=chat)
    model.openModule("talk")
    clock[0] += 91
    model.refresh()
    assert model.current == "" and talk.cleared == 0                          # 模組畫面閒置回主畫面，對話內容還在
    clock[0] += 1700                                                           # 距離上次操作共 1791 秒
    model.refresh()
    assert talk.cleared == 0 and chat.cleared == 0
    clock[0] += 20                                                             # 超過半小時：兩個面板各自重置
    model.refresh()
    assert talk.cleared == 1 and chat.cleared == 1
    model.openModule("chat")
    clock[0] += 50
    model.touch()                                                              # 有人操作：重新計時
    clock[0] += 50
    model.refresh()
    assert model.current == "chat" and chat.cleared == 1                       # 有操作就不重置、不回主畫面


def test_the_background_read_hands_data_to_the_screen_thread_and_skips_overlaps(qt_app, store, clock):
    import time as _time
    model = make_model(qt_app, store, clock)
    model._set("clock", {}, model.clockChanged)
    model.refresh_async()
    model.refresh_async()                                                      # 上一次還沒套用：略過
    end = _time.time() + 5
    while model._collecting and _time.time() < end:
        qt_app.processEvents()
        _time.sleep(0.01)
    assert not model._collecting and model.clock.get("time")                   # 資料經背景讀取後套用到屬性


class FakeRecognition:
    def __init__(self):
        self.on, self.last_exit, self.started, self.stopped = False, None, 0, 0

    def running(self):
        return self.on

    def start(self):
        self.started += 1
        self.on = True
        return True, ""

    def stop(self):
        self.stopped += 1
        self.on = False


def test_the_recognition_button_follows_the_backs_switch_and_can_only_start(qt_app, store, clock):
    rec = FakeRecognition()
    model = make_model(qt_app, store, clock, recognition=rec)
    assert model.recognitionState == {"visible": True, "text": "啟動人臉辨識", "enabled": True, "tone": "primary"}
    model.startRecognition()
    assert rec.started == 1 and model.recognitionState["enabled"] is False
    assert "執行中" in model.recognitionState["text"]
    model.startRecognition()
    assert rec.started == 1                                                    # 執行中不會再啟動
    store.set_meta(front_agent.BACK_RECOGNITION_KEY, False)                   # 後台停止辨識
    model.refresh()
    assert rec.stopped == 1 and model.recognitionState["tone"] == "warning"
    assert model.recognitionState["enabled"] is False
    model.startRecognition()
    assert rec.started == 1                                                    # 後台停止時按不動
    store.set_meta(front_agent.BACK_RECOGNITION_KEY, True)
    rec.last_exit = 3
    model.refresh()
    assert "重新啟動" in model.recognitionState["text"] and model.recognitionState["enabled"] is True
    assert make_model(qt_app, store, clock).recognitionState == {"visible": False}


def test_the_theme_toggle_switches_the_palette_remembers_it_and_signals_the_alpha(qt_app, store, clock):
    model = make_model(qt_app, store, clock)
    assert model.theme == "dark" and model.palette["mode"] == "dark"
    assert model.palette["bg"] == ui_theme.COLORS["dark"]["bg"]
    alphas = []
    model.themeApplied.connect(alphas.append)
    model.toggleTheme()
    assert model.theme == "frosted" and model.palette["mode"] == "light" and alphas == [qmodel.FROSTED_ALPHA]
    assert store.get_meta("theme") == "frosted"
    assert make_model(qt_app, store, clock).theme == "frosted"                # 下次開啟沿用
    model.toggleTheme()
    assert model.theme == "dark" and alphas[-1] == 1.0


def test_fullscreen_requests_and_remembers_the_choice(qt_app, store, clock, monkeypatch):
    saved = {}
    monkeypatch.setattr(front_screens, "save_pref", lambda name, value: saved.update({name: value}))
    model = make_model(qt_app, store, clock)
    requests = []
    model.fullscreenRequested.connect(requests.append)
    model.toggleFullscreen()
    model.toggleFullscreen()
    model.leaveFullscreen()                                                    # 已經不是全螢幕：不重複送
    assert requests == [True, False] and saved == {"dashboard_fullscreen": False} and model.fullscreen is False


def test_icons_become_data_uris_with_the_requested_colors_and_bad_names_give_nothing(qt_app, store, clock):
    model = make_model(qt_app, store, clock)
    uri = model.iconUri("smart_chat", "#112233", "#ff00aa")
    assert uri.startswith("data:image/svg+xml;utf8,") and "%23ff00aa" in uri and "%23112233" in uri
    assert "currentColor" not in uri and "accent" not in uri
    assert model.iconUri("../secret", "#000000", "#000000") == "" and model.iconUri("no_such", "#000", "#000") == ""
    assert svg_icon.colored_svg("smart_send", "#010203").count("#010203") >= 1


def test_news_cards_get_a_qr_only_when_the_link_is_allowed(qt_app, store, clock, monkeypatch):
    model = make_model(qt_app, store, clock)
    cards = model.news["cards"]
    assert cards and cards[0]["qr"].startswith("data:image/png;base64,")
    store.set_meta(front_agent.BACK_LOCAL_ONLY_KEY, True)                     # 全本地模式：不顯示 QR
    model.refresh()
    assert all(c["qr"] == "" for c in model.news["cards"])


def test_a_failing_resource_reader_does_not_break_the_refresh(qt_app, store, clock):
    def boom():
        raise OSError("讀不到")

    model = make_model(qt_app, store, clock, read_resources=boom)
    assert model.resources["gpu"]["text"] == "未偵測到" and model.resources["cpu"]["text"] == "—"


# --- QML 畫面 ---

def test_the_qml_loads_without_warnings_and_every_module_opens(qt_app, store, clock):
    dash = qapp.create(store, app=qt_app, read_heartbeat=lambda: {"camera_ok": True, "recognition_running": False},
                       read_resources=lambda: front_preview.FAKE_RESOURCES, chat=FakeController(),
                       talk=FakeController(), recognition=FakeRecognition())
    try:
        assert dash.warnings == [] and dash.window.title() == "Smart Access 前台"
        end = time.time() + 0.3
        while time.time() < end:
            qt_app.processEvents()
        for key in ("talk", "chat", "news", "greetings", "status"):
            dash.model.openModule(key)
            for _ in range(5):
                qt_app.processEvents()
        dash.model.goHome()
        dash.model.toggleTheme()
        for _ in range(5):
            qt_app.processEvents()
        assert dash.warnings == []
        image = dash.window.grabWindow()
        assert image.width() > 100 and image.height() > 100
    finally:
        dash.close()


def test_a_broken_qml_directory_raises_with_the_qml_error(qt_app, store, tmp_path):
    (tmp_path / "Main.qml").write_text("import QtQuick\nItem { 這不是 QML }\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="QML"):
        qapp.create(store, qml_dir=str(tmp_path), app=qt_app)


def test_answers_and_failures_become_bubbles_once_and_the_conversation_builds_up(qt_app, store, clock):
    talk = FakeController()
    model = make_model(qt_app, store, clock, talk=talk)
    model.submit("talk", "你好")
    talk.state, talk.answer, talk.sources = "answer", "您好！", ["天氣查詢"]
    model.refresh()
    model.refresh()                                                            # 同一個回答不會重複加進對話
    roles = [m["role"] for m in model.qaTalk["messages"]]
    assert roles == ["user", "assistant"] and model.qaTalk["messages"][1]["sources"] == ["天氣查詢"]
    talk.state = "idle"
    assert model.submit("talk", "再問一題") is True
    talk.state, talk.message = "error", "這一題沒有成功"
    model.refresh()
    assert [m["role"] for m in model.qaTalk["messages"]] == ["user", "assistant", "user", "error"]
    model.clearPanel("talk")
    assert model.qaTalk["messages"] == []
