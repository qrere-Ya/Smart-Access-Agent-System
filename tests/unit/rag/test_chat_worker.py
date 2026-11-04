"""
chat_worker 單元測試：背景工作只把結果放進事件佇列，不碰 Tk；不需要模型、顯示器或服務。
"""
import queue
import types

from rag import chat_worker


def test_describe_report_lists_changes_and_failures():
    report = types.SimpleNamespace(added=["a.pdf"], updated=[], removed=["b.txt"], failed={"c.pdf": "加密"})
    text = chat_worker.describe_report(report)
    assert "新增：a.pdf" in text and "移除：b.txt" in text and "c.pdf（加密）" in text and "更新" not in text


def test_describe_report_without_changes():
    report = types.SimpleNamespace(added=[], updated=[], removed=[], failed={})
    assert chat_worker.describe_report(report).endswith("沒有變動")


def test_load_runtime_reports_progress_then_ready():
    events = queue.Queue()
    runtime = object()

    def factory(progress):
        progress("載入中…")
        return runtime

    chat_worker.load_runtime(events, factory, auto_start_ollama=False)
    assert events.get_nowait() == ("status", "載入中…")
    assert events.get_nowait() == ("ready", runtime)


def test_load_runtime_failure_becomes_a_failed_event():
    events = queue.Queue()

    def factory(progress):
        raise RuntimeError("沒有模型")

    chat_worker.load_runtime(events, factory, auto_start_ollama=False, retries=0)
    assert events.get_nowait() == ("failed", "RuntimeError：沒有模型")


def _drain(events):
    items = []
    while not events.empty():
        items.append(events.get_nowait())
    return items


def test_a_temporary_failure_is_retried_until_it_works():
    events, sleeps, runtime, calls = queue.Queue(), [], object(), []

    def factory(progress):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("資源不足")
        return runtime

    chat_worker.load_runtime(events, factory, auto_start_ollama=False, retries=3, delay=7, sleep=sleeps.append)
    items = _drain(events)
    assert sleeps == [7, 7] and len(calls) == 3
    assert items[-1] == ("ready", runtime) and not any(k == "failed" for k, _ in items)
    assert any("重試" in str(p) for k, p in items if k == "status")


def test_resource_denied_counts_as_a_temporary_failure():
    from shared.resource_manager import ResourceDenied

    events, calls = queue.Queue(), []

    def factory(progress):
        calls.append(1)
        raise ResourceDenied("記憶體不足")

    chat_worker.load_runtime(events, factory, auto_start_ollama=False, retries=2, delay=0, sleep=lambda s: None)
    items = _drain(events)
    assert len(calls) == 3                                   # 第一次加上兩次重試
    assert items[-1][0] == "failed" and "ResourceDenied" in items[-1][1]


def test_a_permanent_failure_is_not_retried():
    events, calls = queue.Queue(), []

    def factory(progress):
        calls.append(1)
        raise FileNotFoundError("找不到向量模型資料夾")

    chat_worker.load_runtime(events, factory, auto_start_ollama=False, retries=3, delay=0, sleep=lambda s: None)
    assert len(calls) == 1 and events.get_nowait()[0] == "failed"


def test_refresh_failure_still_posts_a_docs_event(monkeypatch):
    def boom(runtime):
        raise RuntimeError("顯存不足")

    monkeypatch.setattr(chat_worker.bootstrap, "refresh_documents", boom)
    events = queue.Queue()
    chat_worker.start_refresh_docs(object(), events)
    kind, text = events.get(timeout=5)
    assert kind == "docs" and "重新掃描失敗" in text and "顯存不足" in text
