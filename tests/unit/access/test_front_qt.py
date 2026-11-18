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

def test_the_qa_display_rules_match_the_old_panel():
    idle = qmodel.qa_display({"state": "idle", "message": "", "answer": "", "sources": [], "can_ask": True}, "")
    assert idle["welcome"] is True and idle["inputVisible"] is True and idle["inputEnabled"] is True
    asking = qmodel.qa_display({"state": "asking", "message": "查詢中…", "answer": "", "sources": [], "can_ask": False},
                               "特休幾天")
    assert asking["inputEnabled"] is False and asking["status"] == "查詢中…"
    assert asking["text"].startswith("你問：特休幾天")
    answer = qmodel.qa_display({"state": "answer", "message": "", "answer": "七日", "sources": ["勞動基準法"],
                                "can_ask": True}, "特休幾天")
    assert answer["welcome"] is False and answer["status"] == "" and "來源文件：勞動基準法" in answer["text"]
    closed = qmodel.qa_display({"state": "closed", "message": "未開放", "answer": "", "sources": [], "can_ask": False},
                               "舊問題")
    assert closed["inputVisible"] is False and closed["welcome"] is False and "舊問題" not in closed["text"]
    offline = qmodel.qa_display({"state": "offline", "message": "後台未連線", "answer": "", "sources": [],
                                 "can_ask": False}, "舊問題")
    assert offline["inputVisible"] is False and offline["status"] == "後台未連線"
    error = qmodel.qa_display({"state": "error", "message": "這一題沒有成功", "answer": "", "sources": [],
                               "can_ask": True}, "問")
    assert "這一題沒有成功" in error["text"] and error["status"] == "這一題沒有成功"


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
    assert model.current == "" and talk.cleared == 1                          # 離開問答面板就清掉
    model.openModule("news")
    model.goHome()
    assert talk.cleared == 1                                                   # 其他模組離開不用清


def test_submit_clear_and_the_panel_cleared_signal(qt_app, store, clock):
    chat = FakeController()
    model = make_model(qt_app, store, clock, chat=chat)
    cleared = []
    model.panelCleared.connect(cleared.append)
    model.openModule("chat")
    assert model.submit("chat", "   ") is False and chat.submitted == []
    assert model.submit("chat", "特休幾天") is True and chat.submitted == ["特休幾天"]
    assert model.qaChat["state"] == "asking" and model.qaChat["text"].startswith("你問：特休幾天")
    assert model.submit("chat", "第二題") is False                              # 處理中不能重複送
    assert model.submit("nope", "x") is False
    model.clearPanel("chat")
    assert chat.cleared == 1 and cleared == ["chat"] and model.qaChat["welcome"] is True and model.qaChat["text"] == ""


def test_the_talk_and_rag_panels_are_independent(qt_app, store, clock):
    talk, chat = FakeController(), FakeController()
    model = make_model(qt_app, store, clock, talk=talk, chat=chat)
    model.submit("talk", "你好")
    assert talk.submitted == ["你好"] and chat.submitted == []
    assert model.qaTalk["state"] == "asking" and model.qaChat["state"] == "idle"


def test_idle_rules_90_seconds_for_modules_and_60_for_panels(qt_app, store, clock):
    talk = FakeController()
    model = make_model(qt_app, store, clock, talk=talk)
    model.openModule("talk")
    clock[0] += 61
    model.refresh()
    assert talk.cleared == 1 and model.current == "talk"                      # 面板清掉，人還留在面板
    clock[0] += 30                                                             # 距離上次操作共 91 秒
    model.refresh()
    assert model.current == ""                                                 # 模組畫面閒置回主畫面
    model.openModule("talk")
    clock[0] += 50
    model.touch()                                                              # 有人操作：重新計時
    clock[0] += 50
    model.refresh()
    assert model.current == "talk" and talk.cleared == 2                       # 有操作就不清、不回主畫面


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
