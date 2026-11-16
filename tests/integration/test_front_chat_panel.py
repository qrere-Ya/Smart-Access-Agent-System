# ruff: noqa: F811  （seeded 是從 test_front_dashboard 匯入的 fixture，函式參數會「重新定義」它，這是 pytest 的正常用法）
"""
前台儀表板的「知識庫問答」面板整合測試：真的 Tk 視窗（需要顯示器），問答控制用假的（可以指定任何狀態）。
驗證：模組卡片與各狀態的畫面（可以問、查詢中、準備中、後台忙碌、離線、未開放、錯誤）、輸入框與送出鈕的啟用與隱藏、
Enter 送出、回答與來源只當純文字、閒置 60 秒清除輸入與回答、離開面板就清掉、打字不會被閒置清掉、
長回答換行不破版、兩個主題都有截圖、鍵盤焦點在輸入框、沒有任何儲存。CI 的虛擬螢幕沒有視窗管理員，
鍵盤事件直接呼叫處理函式。
"""
import time

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("customtkinter")

from test_front_dashboard import TS, build, seeded  # noqa: E402, F401  重用同一組假資料與建立函式


class FakeChat:
    """假的問答控制：view() 回傳指定的狀態；submit() 記下問題並把狀態切到 asking。"""

    def __init__(self, state="idle", message="", answer="", sources=(), can_ask=True, availability="idle"):
        self.state, self.message, self.answer, self.sources = state, message, answer, list(sources)
        self.can_ask, self._availability = can_ask, availability
        self.submitted, self.cleared = [], 0

    def availability(self):
        return self._availability

    def view(self):
        return {"state": self.state, "message": self.message, "answer": self.answer, "sources": list(self.sources),
                "can_ask": self.can_ask}

    def submit(self, question):
        if not question or not self.can_ask:
            return False
        self.submitted.append(question)
        self.state, self.message, self.can_ask = "asking", "查詢中…", False
        return True

    def clear(self):
        self.cleared += 1
        self.state, self.message, self.answer, self.sources, self.can_ask = "idle", "", "", [], True


def open_chat(root, seeded, chat, theme="dark"):
    from access.front_dashboard import FrontDashboard  # noqa: F401
    store, screen = seeded
    screen.set_theme(theme)
    app = build(root, store)
    app.chat = chat
    app.open_module("chat")
    root.update()
    app.refresh()
    root.update()
    return app


def answer_text(app):
    return app.chat_answer.get("1.0", "end").strip()


def test_the_card_says_not_open_until_the_back_opens_it_and_opens_the_panel(root, seeded):
    store, _screen = seeded
    app = build(root, store)
    assert app.tiles["chat"]["title"].cget("text") == "知識庫問答"
    app.tiles["chat"]["title"]._label.event_generate("<Button-1>")
    root.update()
    root.update()
    assert app.current == "chat" and app.panels["chat"].winfo_ismapped()
    assert "未開放" in app.chat_status.cget("text") and not app.chat_input_row.winfo_ismapped()


def test_ready_shows_the_input_and_a_hint_and_focuses_the_entry(root, seeded):
    app = open_chat(root, seeded, FakeChat())
    root.update()
    time.sleep(0.15)
    root.update()
    assert app.chat_input_row.winfo_ismapped() and app.chat_send.cget("state") == "normal"
    assert app.chat_entry.cget("state") == "normal" and app.chat_status.cget("text") == ""
    assert app.chat_entry.cget("placeholder_text") == "用鍵盤輸入問題，按 Enter 送出"


def test_pressing_enter_sends_the_question_and_clears_the_box_and_shows_it(root, seeded):
    chat = FakeChat()
    app = open_chat(root, seeded, chat)
    app.chat_entry.insert(0, "  特休有幾天？  ")
    app._focus_chat()                                                # 鍵盤事件要有焦點：真機是進入面板 80 毫秒後取得
    root.update()
    app.chat_entry.event_generate("<Return>")
    root.update()
    assert chat.submitted == ["特休有幾天？"] and app.chat_entry.get() == ""
    assert "你問：特休有幾天？" in answer_text(app)
    assert app.chat_status.cget("text") == "查詢中…"
    assert app.chat_send.cget("state") == "disabled" and app.chat_entry.cget("state") == "disabled"   # 不能重複送出


def test_the_send_button_works_and_an_empty_box_sends_nothing(root, seeded):
    chat = FakeChat()
    app = open_chat(root, seeded, chat)
    app.chat_send.invoke()
    assert chat.submitted == []
    app.chat_entry.insert(0, "問題")
    app.chat_send.invoke()
    assert chat.submitted == ["問題"]


def test_an_answer_is_plain_text_with_the_source_names(root, seeded):
    chat = FakeChat()
    app = open_chat(root, seeded, chat)
    app.chat_entry.insert(0, "特休有幾天？")
    app._send_chat()
    chat.state, chat.can_ask = "answer", True
    chat.answer = "工作滿一年每年七天。<b>粗</b> **a** http://evil.example"
    chat.sources = ["勞動基準法", "員工手冊"]
    app.refresh()
    root.update()
    text = answer_text(app)
    assert "你問：特休有幾天？" in text and "工作滿一年每年七天。<b>粗</b> **a** http://evil.example" in text
    assert text.endswith("來源文件：勞動基準法、員工手冊")
    assert app.chat_answer.cget("state") == "disabled" and app.chat_input_row.winfo_ismapped()
    assert app.chat_status.cget("text") == "" and app.chat_send.cget("state") == "normal"


@pytest.mark.parametrize("state, message, input_visible, input_enabled", [
    ("asking", "查詢中…", True, False),
    ("preparing", "知識庫準備中，第一次提問會久一點…", True, False),
    ("busy", "目前有人在使用，請稍後再試", True, True),
    ("error", "這一題沒有成功，請稍後再試", True, True),
    ("offline", "後台未連線，無法使用知識庫問答", False, False),
    ("closed", "知識庫問答目前未開放", False, False)])
def test_each_state_shows_its_fixed_text_and_the_right_input_state(root, seeded, state, message, input_visible,
                                                                  input_enabled):
    asking = state in ("asking", "preparing")
    chat = FakeChat(state=state, message=message, can_ask=state in ("busy", "error"))
    chat._availability = state if state in ("offline", "closed") else "idle"
    app = open_chat(root, seeded, chat)
    assert message in app.chat_status.cget("text") + answer_text(app)
    assert bool(app.chat_input_row.winfo_ismapped()) is input_visible
    if input_visible:
        assert (app.chat_send.cget("state") == "normal") is input_enabled
        assert (app.chat_entry.cget("state") == "disabled") is asking


def test_the_home_card_follows_the_back_state(root, seeded):
    store, _screen = seeded
    from access import front_agent
    app = build(root, store)
    assert app.tiles["chat"]["summary"].cget("text") == "目前未開放"
    store.set_meta(front_agent.BACK_CHAT_KEY, True)
    store.set_link_state(True)
    app.refresh()
    assert app.tiles["chat"]["summary"].cget("text") == "用鍵盤問公司文件的內容"
    store.set_link_state(False)
    app.refresh()
    assert app.tiles["chat"]["summary"].cget("text") == "後台未連線"


def test_the_panel_clears_itself_after_a_minute_of_nobody_typing(root, seeded):
    chat = FakeChat(state="answer", answer="舊的回答", can_ask=True)
    app = open_chat(root, seeded, chat)
    app._chat_question = "舊的問題"
    app.chat_entry.insert(0, "打了一半")
    now = [app._chat_touched]
    app._clock = lambda: now[0]
    now[0] += 59
    app.refresh()
    assert chat.cleared == 0 and app.chat_entry.get() == "打了一半"
    now[0] += 2
    app.refresh()
    root.update()
    assert chat.cleared == 1 and app.chat_entry.get() == "" and answer_text(app) == ""
    assert app.current == "chat"                                     # 清掉輸入與回答，但留在問答畫面


def test_typing_counts_as_someone_being_there(root, seeded):
    chat = FakeChat()
    app = open_chat(root, seeded, chat)
    now = [app._chat_touched]
    app._clock = lambda: now[0]
    now[0] += 50
    app._on_chat_key()                                               # 按鍵事件的處理函式（CI 的虛擬螢幕沒有焦點，直接呼叫）
    now[0] += 50                                                     # 距離上次打字只有 50 秒
    app.refresh()
    assert chat.cleared == 0


def test_leaving_the_panel_clears_everything(root, seeded):
    chat = FakeChat(state="answer", answer="回答", sources=["文件"], can_ask=True)
    app = open_chat(root, seeded, chat)
    app._chat_question = "問題"
    app.chat_entry.insert(0, "還沒送出的字")
    app.back_button.invoke()
    root.update()
    assert chat.cleared == 1 and app.chat_entry.get() == "" and app._chat_question == ""
    assert app.current is None and app.home.winfo_ismapped()
    app.open_module("chat")
    root.update()
    assert answer_text(app) == ""


def test_a_question_longer_than_the_limit_is_cut_in_the_box(root, seeded):
    from shared import link_protocol as lp
    app = open_chat(root, seeded, FakeChat())
    app.chat_entry.insert(0, "字" * (lp.MAX_CHAT_QUESTION + 50))
    app._limit_chat_input()                                          # 放開按鍵時的處理函式（直接呼叫，不靠視窗焦點）
    root.update()
    assert len(app.chat_entry.get()) == lp.MAX_CHAT_QUESTION


def test_a_very_long_answer_wraps_and_does_not_change_the_window_size(root, seeded):
    long_answer = "這是一段很長的回答，用來確認自動換行而且視窗不會被撐大。" * 60
    chat = FakeChat(state="answer", answer=long_answer, sources=["勞動基準法"], can_ask=True)
    app = open_chat(root, seeded, chat)
    root.geometry("1280x720+0+0")
    root.update()
    app.refresh()
    root.update()
    assert root.winfo_reqwidth() <= root.winfo_width() + 2 and root.winfo_reqheight() <= root.winfo_height() + 2
    assert app.chat_answer.winfo_width() <= root.winfo_width()


def test_the_panel_never_writes_to_the_store(root, seeded):
    store, _screen = seeded
    before = {k: store.get_meta(k) for k in ("morning", "link_state", "clock_offset")}
    chat = FakeChat()
    app = open_chat(root, seeded, chat)
    app.chat_entry.insert(0, "不能被保存的問題")
    app._send_chat()
    app.back_button.invoke()
    assert {k: store.get_meta(k) for k in before} == before and store.get_meta("chat") is None


@pytest.mark.parametrize("theme", ["dark", "frosted"])
@pytest.mark.parametrize("name, chat", [
    ("可以問", FakeChat()),
    ("回答", FakeChat(state="answer", answer="員工繼續工作滿一年者，每年給予七日特別休假。", sources=["勞動基準法"])),
    ("查詢中", FakeChat(state="asking", message="查詢中…", can_ask=False)),
    ("忙碌", FakeChat(state="busy", message="目前有人在使用，請稍後再試")),
    ("未開放", FakeChat(state="closed", message="知識庫問答目前未開放", can_ask=False, availability="closed")),
    ("離線", FakeChat(state="offline", message="後台未連線，無法使用知識庫問答", can_ask=False,
                    availability="offline"))])
def test_screenshots_in_both_themes(root, seeded, screenshot, theme, name, chat):
    app = open_chat(root, seeded, chat, theme)
    app._chat_question = "公司員工繼續工作滿一年，每年有幾天特別休假？" if name in ("回答", "查詢中") else ""
    app._signatures.pop("chat", None)
    root.geometry("1920x1000+0+0")
    root.update()
    app.refresh()
    assert screenshot(root, f"前台知識庫問答_{theme}_{name}")
