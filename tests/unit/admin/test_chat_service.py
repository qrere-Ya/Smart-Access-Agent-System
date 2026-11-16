"""
chat_service 的單元測試（假的知識庫，不載入任何模型）：送出後得到編號、輪詢到完成與來源名稱、第一次才載入且只載一次、
載入中顯示準備中、排隊上限與送太快、單題逾時、失敗只有固定說明（不含例外內容）、結果 10 分鐘後刪除、閒置釋放模型、
同時只做 1 題、問答寫進對話紀錄（front_knowledge）、回答去控制字元與限長。
"""
import threading
import time

import pytest

from admin import chat_service as cs
from shared import chat_log


class Trace:
    def __init__(self):
        self.outcome, self.sources, self.hits, self.data_text, self.tools = "", [], [], "", []


class FakeAssistant:
    def __init__(self, reply="七天。", sources=("請假辦法",), delay=0.0, error=None, outcome="answered", gate=None):
        self.reply, self.sources, self.delay, self.error, self.outcome = reply, list(sources), delay, error, outcome
        self.gate = gate
        self.last = Trace()
        self.on_finished = None
        self.show_internal_details = True
        self.show_evidence_paths = True
        self.questions = []
        self.running = 0
        self.max_running = 0

    def ask_documents(self, question):
        self.questions.append(question)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if self.gate is not None:
                self.gate.wait(5)
            if self.error:
                raise self.error
            for piece in (self.reply[:2], self.reply[2:]):
                time.sleep(self.delay)
                yield piece
            self.last.outcome, self.last.sources = self.outcome, self.sources
            if self.on_finished is not None:
                self.on_finished(question, self.reply, self.last)
        finally:
            self.running -= 1


class FakeRuntime:
    def __init__(self, assistant):
        self.assistant, self.closed = assistant, 0

    def close(self):
        self.closed += 1


@pytest.fixture
def make(db):
    made = []

    def build(assistant=None, **kw):
        assistant = assistant or FakeAssistant()
        loads = []
        load_error = kw.pop("load_error", False)

        def factory(progress=None):
            loads.append(1)
            if load_error:
                raise RuntimeError("找不到模型 C:/secret/models/bge-m3")
            return FakeRuntime(assistant)

        service = cs.ChatService(runtime_factory=factory, min_interval=kw.pop("min_interval", 0.0), **kw)
        service.loads, service.assistant = loads, assistant
        made.append(service)
        return service

    yield build
    for service in made:
        service.close()


def wait_for(service, job_id, want=(cs.DONE, cs.FAILED), seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        result = service.get(job_id)
        if result and result["status"] in want:
            return result
        time.sleep(0.01)
    raise AssertionError(f"逾時，狀態：{service.get(job_id)}")


def test_a_question_gets_a_job_id_and_is_answered_with_source_names(make):
    service = make()
    job_id = service.submit("每年給幾天特別休假")
    assert len(job_id) == 32 and int(job_id, 16) >= 0
    result = wait_for(service, job_id)
    assert result == {"status": "done", "answer": "七天。", "sources": ["請假辦法"]}


def test_the_knowledge_base_is_loaded_on_the_first_question_only_and_once(make):
    service = make()
    assert service.loads == []                                  # 啟動時不載入
    for i in range(3):
        wait_for(service, service.submit(f"問題 {i}"))
    assert len(service.loads) == 1


def test_the_first_question_shows_preparing_while_the_models_load(make):
    gate = threading.Event()
    seen = []
    service = make()

    def slow_factory(progress=None):
        gate.wait(5)
        return FakeRuntime(FakeAssistant())

    service._factory = slow_factory
    job_id = service.submit("問題")
    time.sleep(0.1)
    seen.append(service.get(job_id)["status"])
    gate.set()
    wait_for(service, job_id)
    assert seen == [cs.PREPARING]


def test_questions_are_answered_one_at_a_time_in_order(make):
    gate = threading.Event()
    assistant = FakeAssistant(gate=gate)
    service = make(assistant)
    ids = [service.submit(f"問題 {i}") for i in range(3)]
    time.sleep(0.1)
    assert [service.get(i)["status"] for i in ids] == [cs.RUNNING, cs.QUEUED, cs.QUEUED]
    gate.set()
    for job_id in ids:
        wait_for(service, job_id)
    assert assistant.questions == ["問題 0", "問題 1", "問題 2"] and assistant.max_running == 1


def test_a_fourth_open_question_is_refused_as_busy(make):
    gate = threading.Event()
    service = make(FakeAssistant(gate=gate))
    for i in range(cs.MAX_OPEN_JOBS):
        service.submit(f"問題 {i}")
    with pytest.raises(cs.Busy) as info:
        service.submit("再一題")
    assert info.value.code == "busy"
    gate.set()


def test_a_finished_question_frees_a_place(make):
    gate = threading.Event()
    service = make(FakeAssistant(gate=gate))
    ids = [service.submit(f"問題 {i}") for i in range(cs.MAX_OPEN_JOBS)]
    gate.set()
    for job_id in ids:
        wait_for(service, job_id)
    wait_for(service, service.submit("新的一題"))


def test_sending_too_fast_is_refused(make):
    now = [100.0]
    service = make(min_interval=3.0, clock=lambda: now[0])
    service.submit("第一題")
    with pytest.raises(cs.TooFast) as info:
        service.submit("第二題")
    assert info.value.code == "slow_down"
    now[0] += 3.1
    service.submit("第三題")


def test_a_question_that_takes_too_long_is_cancelled_with_a_fixed_message(make):
    service = make(FakeAssistant(delay=0.2), timeout=0.05)
    result = wait_for(service, service.submit("問題"))
    assert result["status"] == "failed" and result["error"] == "timeout"
    assert result["message"] == cs.ERROR_TEXT["timeout"]


def test_a_model_loading_failure_gives_only_a_fixed_message_and_no_exception_text(make, capsys):
    service = make(load_error=True)
    result = wait_for(service, service.submit("問題"))
    assert result["status"] == "failed" and result["error"] == "unavailable"
    assert result["message"] == cs.ERROR_TEXT["unavailable"]
    assert "secret" not in str(result) and "bge-m3" not in str(result)
    out = capsys.readouterr().out
    assert "RuntimeError" in out and "secret" not in out                # 日誌只有例外型別
    assert [r["source"] for r in chat_log.list_logs()] == ["front_knowledge"]   # 載入失敗的題目也有記錄


def test_a_llm_failure_gives_a_fixed_message(make):
    service = make(FakeAssistant(error=RuntimeError("key=sk-secret url=http://127.0.0.1:11434")))
    result = wait_for(service, service.submit("問題"))
    assert result["status"] == "failed" and result["error"] == "llm_unavailable"
    assert "sk-secret" not in str(result) and "11434" not in str(result)


def test_an_error_outcome_from_the_assistant_is_a_failure_not_an_answer(make):
    service = make(FakeAssistant(reply="⚠️ 產生回答時出了問題：金鑰無效", outcome="error"))
    result = wait_for(service, service.submit("問題"))
    assert result["status"] == "failed" and "金鑰" not in str(result)


def test_the_worker_survives_a_failed_question(make):
    service = make(FakeAssistant(error=RuntimeError("x")))
    wait_for(service, service.submit("壞的一題"))
    service.assistant.error = None
    assert wait_for(service, service.submit("好的一題"))["status"] == "done"


def test_results_are_kept_ten_minutes_then_forgotten(make):
    now = [0.0]
    service = make(clock=lambda: now[0])
    job_id = service.submit("問題")
    wait_for(service, job_id)
    now[0] += cs.RESULT_KEEP_SECONDS - 1
    assert service.get(job_id)["status"] == "done"
    now[0] += 2
    assert service.get(job_id) is None


def test_unknown_job_ids_are_not_found(make):
    service = make()
    assert service.get("0" * 32) is None and service.get("nope") is None


def test_job_ids_are_random_and_not_sequential(make):
    service = make()
    ids = {service.submit(f"q{i}") for i in range(3)}
    assert len(ids) == 3 and all(len(i) == 32 for i in ids)


def test_the_models_are_released_after_being_idle_and_reloaded_on_the_next_question(make):
    now = [0.0]
    service = make(clock=lambda: now[0], idle_release=900.0)
    wait_for(service, service.submit("問題"))
    runtime = service._runtime
    assert runtime is not None
    now[0] += 901
    end = time.time() + 5
    while service._runtime is not None and time.time() < end:
        time.sleep(0.05)
    assert service._runtime is None and runtime.closed == 1
    wait_for(service, service.submit("再問一題"))
    assert len(service.loads) == 2


def test_close_releases_the_models(make):
    service = make()
    wait_for(service, service.submit("問題"))
    runtime = service._runtime
    service.close()
    assert runtime.closed == 1 and service._runtime is None


def test_each_question_is_recorded_as_front_knowledge(make):
    service = make()
    wait_for(service, service.submit("每年給幾天特別休假"))
    rows = chat_log.list_logs()
    assert len(rows) == 1 and rows[0]["source"] == "front_knowledge"
    full = chat_log.get_log(rows[0]["id"])
    assert full["question"] == "每年給幾天特別休假" and full["answer"] == "七天。"


def test_the_front_assistant_hides_internal_details_and_evidence_paths(make):
    service = make()
    wait_for(service, service.submit("問題"))
    assert service.assistant.show_internal_details is False and service.assistant.show_evidence_paths is False


def test_answers_lose_control_characters_and_are_capped():
    assert cs.clean_answer("你好\x00\x07\u202e世界\u2028") == "你好世界"
    long = cs.clean_answer("字" * 9000)
    assert len(long) == 4001 and long.endswith("…")
    assert cs.clean_answer(None) == ""


# --- 卡住的語言模型呼叫不能讓整個服務卡住 ---

class HangingAssistant(FakeAssistant):
    """第一題卡在語言模型呼叫上（等 release 事件才返回），之後的題目正常。"""

    def __init__(self, release):
        super().__init__()
        self.release, self.calls = release, 0

    def ask_documents(self, question):
        self.calls += 1
        if self.calls == 1:
            self.release.wait(10)
            yield "遲來的回答"
            return
        yield from super().ask_documents(question)


def test_a_hung_model_call_times_out_and_the_next_question_still_gets_answered(db):
    release = threading.Event()
    runtimes = []

    def factory(progress=None):
        runtime = FakeRuntime(HangingAssistant(release) if not runtimes else FakeAssistant(reply="新的回答。"))
        runtimes.append(runtime)
        return runtime

    service = cs.ChatService(runtime_factory=factory, min_interval=0.0, timeout=0.3)
    try:
        first = service.submit("會卡住的一題")
        result = wait_for(service, first, seconds=3)
        assert result["status"] == "failed" and result["error"] == "timeout"
        assert result["message"] == cs.ERROR_TEXT["timeout"]
        assert runtimes[0].closed == 0                       # 卡住的那一題還在用它，先不釋放
        release.set()                                        # 卡住的呼叫結束後才釋放
        end = time.time() + 3
        while runtimes[0].closed == 0 and time.time() < end:
            time.sleep(0.02)
        assert runtimes[0].closed == 1
        second = service.submit("下一題")
        assert wait_for(service, second)["answer"] == "新的回答。" and len(runtimes) == 2
    finally:
        release.set()
        service.close()


def test_a_late_reply_from_the_abandoned_question_never_overwrites_the_timeout(db):
    release = threading.Event()
    service = cs.ChatService(runtime_factory=lambda progress=None: FakeRuntime(HangingAssistant(release)),
                             min_interval=0.0, timeout=0.2)
    try:
        job_id = service.submit("會卡住的一題")
        assert wait_for(service, job_id, seconds=3)["error"] == "timeout"
        release.set()
        time.sleep(0.3)
        assert service.get(job_id)["status"] == "failed"
    finally:
        release.set()
        service.close()


def test_the_language_model_read_timeouts_are_capped_so_a_stuck_call_ends_soon():
    class Backend:
        def __init__(self, timeout):
            self.timeout = timeout

    class Failover:
        def __init__(self):
            self.primary, self.fallback, self.timeout = Backend(120.0), Backend(600.0), None

    class Assistant:
        llm, router_llm = Failover(), Backend(600.0)

    cs.cap_llm_timeouts(Assistant, 90.0)
    assert Assistant.llm.primary.timeout == 90.0 and Assistant.llm.fallback.timeout == 90.0
    assert Assistant.router_llm.timeout == 90.0
    short = Backend(30.0)
    Assistant.llm = short
    cs.cap_llm_timeouts(Assistant, 90.0)
    assert short.timeout == 30.0                                   # 已經比較短的不放長


def test_loading_is_refused_while_the_previous_models_are_still_being_released(db, monkeypatch):
    monkeypatch.setattr(cs, "RELEASE_WAIT_SECONDS", 0.1)
    gate = threading.Event()
    service = cs.ChatService(runtime_factory=lambda progress=None: FakeRuntime(FakeAssistant()), min_interval=0.0)
    service._releasing = threading.Thread(target=gate.wait, args=(5,), daemon=True)
    service._releasing.start()
    try:
        result = wait_for(service, service.submit("問題"))
        assert result["status"] == "failed" and result["error"] == "unavailable"
    finally:
        gate.set()
        service.close()
