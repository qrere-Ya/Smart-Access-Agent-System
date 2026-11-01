"""
chat_window 執行緒規則單元測試：背景執行緒不可直接呼叫 Tk（after、元件），結果要放進佇列由畫面執行緒處理。
不開真正的視窗，用替身物件呼叫未綁定的方法。
"""
import queue
import types

import pytest

pytest.importorskip("customtkinter")
from rag import chat_window  # noqa: E402


class NoTk:
    """任何 Tk 呼叫都會失敗，用來證明背景執行緒沒有碰它。"""

    def after(self, *a, **k):
        raise AssertionError("背景執行緒呼叫了 after")


def test_settings_test_connection_hands_result_to_the_ui_thread_via_queue():
    ls = types.SimpleNamespace(load_settings=lambda: {"api_key": ""}, test_local=lambda s: (True, "**ok** `x`"))
    dialog = NoTk()
    dialog.ls, dialog._form, dialog._show = ls, (lambda: {}), (lambda text: None)
    dialog._test_results = queue.Queue()
    chat_window.SettingsDialog._test(dialog, "local")
    assert dialog._test_results.get(timeout=5) == "ok x"


def test_apply_settings_hands_result_to_the_ui_thread_via_event_queue(monkeypatch):
    monkeypatch.setattr(chat_window.bootstrap, "reload_llm", lambda runtime: "已套用")
    window = NoTk()
    window._runtime, window._events = object(), queue.Queue()
    chat_window.AssistantWindow._apply_settings(window, lambda note: None)
    kind, (show, note) = window._events.get(timeout=5)
    assert kind == "settings_applied" and note == "已套用"


def test_closing_the_window_stops_only_the_ollama_it_started(monkeypatch):
    from admin import service_manager

    stopped = []
    monkeypatch.setattr(service_manager, "stop_service", lambda key, port=None: stopped.append((key, port)))
    window = types.SimpleNamespace(_runtime=None, _auto_start_ollama=True, destroy=lambda: stopped.append("destroy"))
    chat_window.AssistantWindow._on_close(window)
    assert stopped == [("ollama", None), "destroy"]  # 不給 port，所以不會依 port 誤殺外部已在跑的 Ollama
    stopped.clear()
    window._auto_start_ollama = False
    chat_window.AssistantWindow._on_close(window)
    assert stopped == ["destroy"]
