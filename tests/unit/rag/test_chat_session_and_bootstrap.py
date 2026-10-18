"""
chat_session 與 bootstrap 單元測試：背景執行緒回答、事件順序、忙碌與空白輸入、錯誤不卡住、新對話；
組裝流程（進度回報、零件失敗時清理、重新整理文件、換 LLM）全部用假零件，不需要模型。
"""
import threading
import time
import types

import numpy as np
import pytest

from rag import answer, bootstrap
from rag.chat_session import MAX_INPUT_CHARS, ChatController, plain_text


class FakeAssistant:
    def __init__(self, pieces=("你", "好"), error=None, gate=None):
        self.pieces, self.error, self.gate = pieces, error, gate
        self.asked, self.reset_count = [], 0
        self.last = types.SimpleNamespace(outcome="answered")

    def ask(self, question):
        self.asked.append(question)
        if self.gate:
            self.gate.wait(5)
        if self.error:
            raise self.error
        yield from self.pieces

    def reset(self):
        self.reset_count += 1


def drain(controller):
    controller.wait()
    return controller.poll()


def test_events_arrive_in_order_start_chunks_done():
    controller = ChatController(FakeAssistant())
    assert controller.submit("你好嗎") is True
    events = drain(controller)
    assert [(e.kind, e.text) for e in events] == [("start", "你好嗎"), ("chunk", "你"), ("chunk", "好"), ("done", "")]
    assert events[-1].outcome == "answered" and not controller.busy


def test_blank_question_is_ignored():
    controller = ChatController(FakeAssistant())
    assert controller.submit("   ") is False and controller.submit(None) is False and controller.poll() == []


def test_a_second_question_while_busy_is_refused():
    gate = threading.Event()
    assistant = FakeAssistant(gate=gate)
    controller = ChatController(assistant)
    assert controller.submit("第一題") is True and controller.busy
    assert controller.submit("第二題") is False
    gate.set()
    drain(controller)
    assert assistant.asked == ["第一題"] and controller.submit("第三題") is True


def test_long_input_is_cut():
    assistant = FakeAssistant()
    controller = ChatController(assistant)
    controller.submit("問" * (MAX_INPUT_CHARS + 500))
    drain(controller)
    assert len(assistant.asked[0]) == MAX_INPUT_CHARS


def test_unexpected_exception_becomes_an_error_event_and_frees_the_controller():
    controller = ChatController(FakeAssistant(error=RuntimeError("boom")))
    controller.submit("問題")
    events = drain(controller)
    assert events[-1].kind == "error" and "RuntimeError：boom" in events[-1].text and not controller.busy


def test_new_chat_clears_memory_but_not_while_answering():
    gate = threading.Event()
    assistant = FakeAssistant(gate=gate)
    controller = ChatController(assistant)
    controller.submit("問題")
    assert controller.new_chat() is False and assistant.reset_count == 0
    gate.set()
    drain(controller)
    assert controller.new_chat() is True and assistant.reset_count == 1


def test_poll_returns_nothing_when_idle():
    assert ChatController(FakeAssistant()).poll() == []


def test_the_caller_is_not_blocked_while_the_assistant_works():
    gate = threading.Event()
    controller = ChatController(FakeAssistant(gate=gate))
    started = time.time()
    controller.submit("問題")
    assert time.time() - started < 1.0
    gate.set()
    drain(controller)


# --- 組裝 ---

class FakeStore:
    def __init__(self, docs_dir, store_dir, embed):
        self.args, self.syncs = (docs_dir, store_dir), 0

    def sync(self):
        self.syncs += 1
        return types.SimpleNamespace(added=["a.txt"], failed={})

    def titles(self):
        return ["A"]


@pytest.fixture
def parts(monkeypatch):
    monkeypatch.setattr(bootstrap, "DocStore", FakeStore)
    log = {"closed": [], "progress": []}

    def embedder():
        return (lambda texts: np.ones((len(texts), 3))), lambda: log["closed"].append("embed")

    def reranker_factory():
        return types.SimpleNamespace(), lambda: log["closed"].append("rerank")

    def llm_builder(settings=None, max_tokens=None):
        return types.SimpleNamespace(tag=f"llm{max_tokens}"), "助理使用本機模型"

    return log, {"embedder": embedder, "reranker_factory": reranker_factory, "llm_builder": llm_builder,
                 "roster": lambda: ["王小明", "李大華"], "docs_dir": "D", "store_dir": "S"}


def test_runtime_is_assembled_with_progress_updates(parts):
    log, kwargs = parts
    runtime = bootstrap.create_runtime(progress=log["progress"].append, **kwargs)
    assert isinstance(runtime.assistant, answer.Assistant) and runtime.llm_note == "助理使用本機模型"
    assert runtime.store.args == ("D", "S") and runtime.sync.added == ["a.txt"]
    assert runtime.assistant.llm.tag == "llmNone" and runtime.assistant.router_llm.tag == "llm200"
    assert log["progress"][0].startswith("載入向量模型") and log["progress"][-1] == "準備完成"
    assert runtime.assistant.roster_provider() == ["王小明", "李大華"]


def test_close_releases_every_model_once(parts):
    log, kwargs = parts
    runtime = bootstrap.create_runtime(progress=lambda s: None, **kwargs)
    runtime.close()
    runtime.close()
    assert log["closed"] == ["embed", "rerank"]


def test_unusable_llm_aborts_and_releases_what_was_loaded(parts):
    log, kwargs = parts
    kwargs["llm_builder"] = lambda settings=None, max_tokens=None: (None, "本機模型也無法使用")
    with pytest.raises(RuntimeError, match="本機模型也無法使用"):
        bootstrap.create_runtime(progress=lambda s: None, **kwargs)
    assert log["closed"] == ["embed", "rerank"]


def test_missing_embedding_model_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.paths, "MODELS_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="向量模型"):
        bootstrap._default_embedder()


def test_refresh_documents_rescans_the_folder(parts):
    log, kwargs = parts
    runtime = bootstrap.create_runtime(progress=lambda s: None, **kwargs)
    report = bootstrap.refresh_documents(runtime)
    assert runtime.store.syncs == 2 and report is runtime.sync


def test_reload_llm_swaps_the_models_or_keeps_the_old_ones(parts):
    log, kwargs = parts
    runtime = bootstrap.create_runtime(progress=lambda s: None, **kwargs)
    old = runtime.assistant.llm
    assert "失敗" in bootstrap.reload_llm(runtime, llm_builder=lambda s=None, max_tokens=None: (None, "失敗：連不上"))
    assert runtime.assistant.llm is old

    def new_builder(settings=None, max_tokens=None):
        return types.SimpleNamespace(tag=f"new{max_tokens}"), "已換成雲端"

    assert bootstrap.reload_llm(runtime, llm_builder=new_builder) == "已換成雲端"
    assert runtime.assistant.llm.tag == "newNone" and runtime.assistant.router_llm.tag == "new200"
    assert runtime.llm_note == "已換成雲端"


def test_roster_is_read_from_the_database_without_face_data(db, add_user):
    add_user("王小明", "E1")
    add_user("李大華", "E2", active=0)
    assert sorted(bootstrap.roster_names()) == ["李大華", "王小明"]


# --- 純文字整理 ---

@pytest.mark.parametrize("raw, clean", [
    ("**延長工時的加班費**：加給三分之一", "延長工時的加班費：加給三分之一"),
    ("## 標題\n內容", "標題\n內容"),
    ("用 `code` 表示", "用 code 表示"),
    ("* 第一項\n* 第二項", "• 第一項\n• 第二項"),
    ("  * 縮排項目", "  • 縮排項目"),
    ("單獨的 ** 星號", "單獨的  星號"),
    ("跨\n行**粗\n體**", "跨\n行粗\n體"),
    ("1. 條列\n   - 子項不動", "1. 條列\n   - 子項不動"),
    ("乘法 3*4 不會被當成項目", "乘法 3*4 不會被當成項目"),
])
def test_markdown_marks_are_removed_from_the_answer(raw, clean):
    assert plain_text(raw) == clean


@pytest.mark.parametrize("assistant", [FakeAssistant(), FakeAssistant(error=RuntimeError("boom"))],
                         ids=["正常結束", "發生例外"])
def test_the_controller_is_already_free_when_the_finishing_event_is_queued(assistant):
    import queue

    controller = ChatController(assistant)
    busy_at_end = []

    class Spy(queue.Queue):
        def put(self, item, *args, **kwargs):
            if item.kind in ("done", "error"):
                busy_at_end.append(controller.busy)
            super().put(item, *args, **kwargs)

    controller._events = Spy()
    controller.submit("問題")
    controller.wait()
    assert busy_at_end == [False]   # 畫面收到結束事件後馬上再送出問題，不能被擋下
    assert controller.submit("馬上再問") is True
    controller.wait()


def test_models_are_released_when_a_later_step_fails(parts):
    log, kwargs = parts

    def broken_roster():
        raise OSError("資料庫被鎖住")

    kwargs["roster"] = broken_roster
    with pytest.raises(OSError):
        bootstrap.create_runtime(progress=lambda s: None, **kwargs)
    assert log["closed"] == ["embed", "rerank"]
