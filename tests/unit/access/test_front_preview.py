"""前台畫面預覽的假資料：只寫進傳進來的資料庫，內容要夠讓畫面各區塊都有東西可看；假的人臉辨識不啟動任何程式。"""
import time

import pytest

from access import front_preview
from access.front_store import FrontStore, ScreenState


def test_seed_fills_news_greetings_and_link_state(tmp_path):
    store = FrontStore(str(tmp_path / "p.db"))
    now = time.time()
    screen = front_preview.seed(store, now=lambda: now)
    assert len(store.get_meta("morning")["news"]) == len(front_preview.NEWS) >= 3
    assert [p["name"] for p in screen.recent_punches(now=lambda: now)] == ["測試乙", "測試甲"]   # 最新的在前
    assert store.link_online(now=lambda: now) is True
    assert screen.theme() == "dark" and screen.current_alert(now=lambda: now) is None


def test_seed_options_offline_no_news_and_theme(tmp_path):
    store = FrontStore(str(tmp_path / "p.db"))
    now = time.time()
    front_preview.seed(store, offline=True, news=False, theme="frosted", now=lambda: now)
    assert store.get_meta("morning") is None
    assert store.link_online(now=lambda: now) is False
    assert ScreenState(store).theme() == "frosted"


def test_fake_recognition_only_tracks_state():
    fake = front_preview.FakeRecognition()
    assert fake.running() is False and fake.last_exit is None
    ok, _message = fake.start()
    assert ok and fake.running() is True
    fake.stop()
    assert fake.running() is False


def test_the_preview_never_touches_the_real_runtime_folder():
    import inspect

    source = inspect.getsource(front_preview)
    assert "tempfile.TemporaryDirectory" in source and "paths.RUNTIME_DIR" not in source


def test_the_preview_chat_answers_after_a_delay_and_follows_the_scenario():
    now = [0.0]
    chat = front_preview.PreviewChat("open", clock=lambda: now[0])
    assert chat.view()["state"] == "idle" and chat.submit("   ") is False
    assert chat.submit("問題") is True and chat.submit("再問") is False
    assert chat.view()["state"] == "asking" and chat.view()["can_ask"] is False
    now[0] += front_preview.PreviewChat.DELAY + 0.1
    view = chat.view()
    assert view["state"] == "answer" and view["answer"].startswith("（預覽用的假回答）") and view["sources"]
    chat.clear()
    assert chat.view()["state"] == "idle"


@pytest.mark.parametrize("mode, state", [("busy", "busy"), ("error", "error")])
def test_the_preview_chat_can_show_busy_and_failure(mode, state):
    now = [0.0]
    chat = front_preview.PreviewChat(mode, clock=lambda: now[0])
    chat.submit("問題")
    now[0] += 2
    assert chat.view()["state"] == state and chat.view()["can_ask"] is True


@pytest.mark.parametrize("mode", ["closed", "offline"])
def test_the_preview_chat_closed_or_offline_hides_the_input_and_refuses_questions(mode):
    chat = front_preview.PreviewChat(mode)
    assert chat.availability() == mode and chat.view()["can_ask"] is False and chat.submit("問題") is False
