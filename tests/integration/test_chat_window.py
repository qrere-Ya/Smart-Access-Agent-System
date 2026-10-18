"""
聊天視窗整合測試：真的 CustomTkinter 視窗（需要有顯示器），助理與模型用假的，只驗證畫面行為：
載入前後的按鈕狀態、送出問題與串流顯示、範例問題、新對話、出處灰色標記、Markdown 符號清掉、關閉時釋放資源。
"""
import time
import types

import pytest

pytestmark = pytest.mark.integration


class FakeAssistant:
    def __init__(self):
        self.last = types.SimpleNamespace(outcome="answered")
        self.questions, self.resets = [], 0

    def reset(self):
        self.resets += 1

    def ask(self, question):
        self.questions.append(question)
        yield "**重點**：七日"
        yield "\n\n📎 出處\n[1] 勞動基準法 第38條"


class FakeStore:
    documents = ["a", "b"]

    def __len__(self):
        return 12


class FakeRuntime:
    def __init__(self, failed=None):
        self.assistant = FakeAssistant()
        self.store = FakeStore()
        self.sync = types.SimpleNamespace(failed=failed or {})
        self.llm_note = "雲端 API 目前無法使用，已改用本機模型。 助理使用本機模型（qwen2:7b）"
        self.closed = 0

    def close(self):
        self.closed += 1


@pytest.fixture
def window():
    import tkinter

    from rag.chat_window import AssistantWindow

    runtime = FakeRuntime(failed={"壞檔.pdf": "沒有可擷取的文字"})
    try:
        win = AssistantWindow(runtime_factory=lambda progress: (progress("載入中…"), runtime)[1],
                              auto_start_ollama=False)
    except tkinter.TclError:
        pytest.skip("沒有可用的顯示器")
    win.runtime = runtime
    yield win
    try:
        win.destroy()
    except tkinter.TclError:
        pass


def pump(win, until, seconds=8.0):
    end = time.time() + seconds
    while time.time() < end:
        win.update()
        if until():
            return True
        time.sleep(0.02)
    return False


def test_window_becomes_ready_and_shows_a_short_status(window):
    assert pump(window, lambda: window._controller is not None)
    status = window.status.cget("text")
    assert "本機模型（qwen2:7b）" in status and "文件 2 份" in status and "12 段" in status
    assert "無法使用" not in status  # 備援原因改成提示，不擠在狀態列
    assert window.send_btn.cget("state") == "normal" and window.docs_btn.cget("state") == "normal"


def test_buttons_are_disabled_until_the_models_are_loaded(window):
    assert window._controller is None and window.send_btn.cget("state") == "disabled"
    assert window.send("你好") is False


def test_question_is_streamed_into_bubbles_with_markdown_removed(window):
    assert pump(window, lambda: window._controller is not None)
    assert window.send("特別休假幾天") is True
    assert pump(window, lambda: len(window._bubbles) == 2 and not window._controller.busy and window.send_btn.cget(
        "state") == "normal")
    user, bot = window._bubbles
    assert (user.role, user.get_text()) == ("user", "特別休假幾天")
    assert bot.get_text().startswith("重點：七日") and "**" not in bot.get_text()
    assert window.runtime.assistant.questions == ["特別休假幾天"]


def test_source_lines_are_marked_grey(window):
    assert pump(window, lambda: window._controller is not None)
    window.send("問題")
    assert pump(window, lambda: len(window._bubbles) == 2 and window.send_btn.cget("state") == "normal"
                and "出處" in window._bubbles[1].get_text())
    marked = window._bubbles[1].box._textbox.tag_ranges("meta")
    assert len(marked) == 4 and str(marked[0]) == "3.0"  # 「📎 出處」與它下面的出處行兩段，正文沒有被標成灰色


def test_welcome_screen_disappears_after_the_first_question_and_returns_with_a_new_chat(window):
    assert pump(window, lambda: window._controller is not None)
    assert window.welcome is not None
    window.send("問題")
    pump(window, lambda: len(window._bubbles) == 2 and window.send_btn.cget("state") == "normal")
    assert window.welcome is None
    window._new_chat()
    assert window.welcome is not None and window._bubbles == [] and window.runtime.assistant.resets == 1


def test_sample_question_buttons_send_their_text(window):
    assert pump(window, lambda: window._controller is not None)
    window.chips[1].invoke()
    assert pump(window, lambda: window.runtime.assistant.questions == ["今天誰遲到？"])


def test_unreadable_documents_and_the_fallback_reason_are_shown_as_notes(window):
    assert pump(window, lambda: window._controller is not None)
    shown = " ".join(w.cget("text") for w in window._notes_widgets)
    assert "壞檔.pdf" in shown and "雲端 API 目前無法使用" in shown


def test_closing_the_window_releases_the_models(window):
    assert pump(window, lambda: window._controller is not None)
    window._on_close()
    assert window.runtime.closed == 1


def test_startup_failure_is_explained(window):
    import tkinter

    from rag.chat_window import AssistantWindow

    def boom(progress):
        raise RuntimeError("本機模型也無法使用")

    try:
        failing = AssistantWindow(runtime_factory=boom, auto_start_ollama=False)
    except tkinter.TclError:
        pytest.skip("沒有可用的顯示器")
    try:
        assert pump(failing, lambda: "啟動失敗" in failing.status.cget("text"))
        assert any("本機模型也無法使用" in w.cget("text") for w in failing._notes_widgets)
        assert failing.send("問題") is False
    finally:
        failing.destroy()


# --- 語言模型設定視窗：本機模型改成下拉選單 ---

@pytest.fixture
def settings_dialog(window, monkeypatch, tmp_path):
    from rag import chat_window, llm_settings

    monkeypatch.setattr(llm_settings, "SETTINGS_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(llm_settings, "list_local_models",
                        lambda s, timeout=4.0: (True, ["gemma3:4b", "llama3:latest", "qwen3:8b"]))
    dialog = chat_window.SettingsDialog(window, on_saved=lambda show: None)
    yield dialog
    dialog.destroy()


def test_local_model_is_chosen_from_the_installed_models(settings_dialog, window):
    installed = ["gemma3:4b", "llama3:latest", "qwen3:8b"]
    assert pump(window, lambda: settings_dialog.local_model.cget("values") == installed)
    assert settings_dialog.local_model.get() == "qwen3:8b"  # 預設值剛好有安裝
    settings_dialog.local_model.set("gemma3:4b")
    assert settings_dialog._form()["local_model"] == "gemma3:4b"


def test_a_saved_model_that_is_not_installed_is_marked_and_saved_without_the_mark(settings_dialog, window):
    settings_dialog.local_model.set("phi3:mini")
    settings_dialog._fill_model_names(["gemma3:4b"])
    assert settings_dialog.local_model.get() == "phi3:mini（尚未安裝）"
    assert settings_dialog._form()["local_model"] == "phi3:mini"


def test_a_saved_name_without_a_tag_matches_the_installed_latest_model(settings_dialog, window):
    settings_dialog.local_model.set("llama3")
    settings_dialog._fill_model_names(["llama3:latest", "qwen3:8b"])
    assert settings_dialog.local_model.get() == "llama3:latest"


def test_a_failed_document_rescan_is_reported_and_the_button_comes_back(window):
    assert pump(window, lambda: window._controller is not None)

    def broken():
        raise RuntimeError("顯存不足")

    window.runtime.store.sync = broken
    window._refresh_docs()
    assert window.docs_btn.cget("state") == "disabled"
    assert pump(window, lambda: window.docs_btn.cget("state") == "normal")
    assert any("重新掃描失敗" in w.cget("text") and "顯存不足" in w.cget("text") for w in window._notes_widgets)
