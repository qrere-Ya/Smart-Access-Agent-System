"""
test_remote_access.py：遠端測試入口的金鑰庫、流量閘門、提問紀錄與排隊包裝（不需要網路與模型）。
"""
import json
import threading
import time

import pytest

from rag import answer, remote_access


class FakeAssistant:
    def __init__(self, text="回答", tools=("count",), outcome="answered", raises=False):
        self.text, self.tools, self.outcome, self.raises = text, list(tools), outcome, raises
        self.last = answer.Trace()

    def reset(self):
        pass

    def ask(self, question):
        self.last = answer.Trace(outcome=self.outcome, tools=self.tools)
        if self.raises:
            raise RuntimeError("boom")
        yield self.text


@pytest.fixture
def keystore(tmp_path):
    return remote_access.KeyStore(str(tmp_path / "keys.json"))


def test_key_is_shown_once_and_only_hash_is_stored(keystore):
    key = keystore.add("小明")
    assert keystore.verify(key) == "小明"
    assert key not in open(keystore.path, encoding="utf-8").read()


def test_wrong_or_empty_key_is_rejected(keystore):
    keystore.add("小明")
    assert keystore.verify("not-the-key") is None
    assert keystore.verify("") is None
    assert keystore.verify(None) is None
    assert keystore.verify(123) is None


def test_each_member_gets_own_key_and_revoke_takes_effect_immediately(keystore):
    a, b = keystore.add("甲"), keystore.add("乙")
    assert a != b and keystore.verify(a) == "甲" and keystore.verify(b) == "乙"
    assert keystore.revoke("甲") is True
    assert keystore.verify(a) is None and keystore.verify(b) == "乙"
    assert keystore.revoke("甲") is False
    assert keystore.labels() == ["乙"]


@pytest.mark.parametrize("label", ["", "   ", "x" * 31, "a\nb"])
def test_bad_labels_are_rejected(keystore, label):
    with pytest.raises(ValueError):
        keystore.add(label)


def test_duplicate_label_is_rejected(keystore):
    keystore.add("甲")
    with pytest.raises(ValueError):
        keystore.add("甲")


def test_missing_or_corrupt_key_file_means_nobody_can_log_in(keystore):
    assert keystore.verify("anything") is None
    with open(keystore.path, "w", encoding="utf-8") as f:
        f.write("{壞掉")
    assert keystore.verify("anything") is None and keystore.labels() == []


def _try_acquire(gate, ticket):
    try:
        gate.acquire(ticket)
    except remote_access.Busy:
        pass


def test_keys_expire_after_their_days_and_zero_means_never(keystore):
    short = keystore.add("短期", days=1, now=1000.0)
    forever = keystore.add("長期", days=0, now=1000.0)
    assert keystore.verify(short, now=1000.0 + 86399) == "短期"
    assert keystore.verify(short, now=1000.0 + 86400) is None
    assert keystore.verify(forever, now=1000.0 + 86400 * 3650) == "長期"
    assert dict(keystore.describe()) == {"短期": 1000.0 + 86400, "長期": None}


def test_default_key_lifetime_is_seven_days(keystore):
    key = keystore.add("小明")
    expires = dict(keystore.describe())["小明"]
    assert 6.9 * 86400 < expires - time.time() < 7.1 * 86400
    assert keystore.verify(key) == "小明" and keystore.verify(key, now=time.time() + 8 * 86400) is None


def test_negative_days_are_rejected(keystore):
    with pytest.raises(ValueError):
        keystore.add("小明", days=-1)


def test_old_format_key_file_still_works_and_never_expires(keystore):
    key = "legacy-key"
    with open(keystore.path, "w", encoding="utf-8") as f:
        json.dump({"舊成員": remote_access._hash(key)}, f)
    assert keystore.verify(key, now=time.time() + 86400 * 9999) == "舊成員"
    assert dict(keystore.describe()) == {"舊成員": None}


def test_failure_budget_blocks_everyone_after_too_many_failures():
    budget = remote_access.FailureBudget(max_failures=3, window=100, block_seconds=50)
    for t in (0, 1):
        budget.record_failure(t)
    assert budget.blocked_for(2) == 0
    budget.record_failure(2)
    assert budget.blocked_for(3) == 49 and budget.blocked_for(52) == 0


def test_failure_budget_forgets_failures_outside_the_window():
    budget = remote_access.FailureBudget(max_failures=3, window=10, block_seconds=50)
    for t in (0, 20, 40, 60):
        budget.record_failure(t)
    assert budget.blocked_for(61) == 0


def test_question_log_neutralizes_unicode_line_separators(tmp_path):
    log = remote_access.QuestionLog(str(tmp_path / "q.jsonl"))
    log.record("甲 乙", "第一行 {\"user\": \"偽造\"}\u0085\n第二行", [], "answered")
    text = open(log.path, encoding="utf-8").read()
    assert len(text.splitlines()) == 1 and " " not in text and "\u0085" not in text


def test_fingerprint_changes_when_a_key_is_replaced_and_vanishes_on_revoke(keystore):
    keystore.add("甲")
    first = keystore.fingerprint("甲")
    keystore.revoke("甲")
    assert keystore.fingerprint("甲") is None
    keystore.add("甲")
    assert keystore.fingerprint("甲") not in (None, first)
    assert keystore.fingerprint("不存在") is None


def test_event_log_strips_control_characters_and_truncates(tmp_path):
    log = remote_access.EventLog(str(tmp_path / "e.jsonl"))
    log.record("login_fail", xff="1.1.1.1\n{\"event\": \"login_ok\"}\x00" + "x" * 500, user="甲")
    lines = open(log.path, encoding="utf-8").read().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "login_fail" and "\n" not in row["xff"] and len(row["xff"]) <= 100


def test_gate_second_caller_waits_until_release():
    gate = remote_access.Gate(max_concurrent=1, max_waiting=3, wait_seconds=5)
    first, second = object(), object()
    gate.acquire(first)
    got = threading.Event()

    def waiter():
        gate.acquire(second)
        got.set()

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)
    assert not got.is_set() and gate.ahead(second) == 1
    gate.release()
    t.join(2)
    assert got.is_set() and gate.ahead(second) is None
    gate.release()


def test_gate_rejects_when_queue_is_full():
    gate = remote_access.Gate(max_concurrent=1, max_waiting=1, wait_seconds=5)
    gate.acquire(object())
    waiter = object()
    waiting = threading.Thread(target=lambda: _try_acquire(gate, waiter), daemon=True)
    waiting.start()
    time.sleep(0.1)
    with pytest.raises(remote_access.Busy):
        gate.acquire(object())
    gate.release()          # 排隊中的那位拿到名額，執行緒正常結束
    waiting.join(2)
    assert not waiting.is_alive()


def test_gate_gives_up_after_waiting_too_long_and_frees_its_place():
    gate = remote_access.Gate(max_concurrent=1, max_waiting=2, wait_seconds=0.1)
    gate.acquire(object())
    late = object()
    with pytest.raises(remote_access.Busy):
        gate.acquire(late)
    assert gate.ahead(late) is None
    gate.release()
    gate.acquire(object())   # 放棄的人不再佔位置，下一個可以直接進


def test_gate_serves_in_arrival_order():
    gate = remote_access.Gate(max_concurrent=1, max_waiting=5, wait_seconds=5)
    gate.acquire(object())
    order, tickets = [], [object() for _ in range(3)]

    def run(i):
        gate.acquire(tickets[i])
        order.append(i)
        gate.release()

    threads = []
    for i in range(3):
        t = threading.Thread(target=run, args=(i,))
        t.start()
        threads.append(t)
        time.sleep(0.05)
    gate.release()
    for t in threads:
        t.join(2)
    assert order == [0, 1, 2]


def test_question_log_records_question_but_not_answer(tmp_path):
    log = remote_access.QuestionLog(str(tmp_path / "q.jsonl"))
    log.record("小明", "誰遲到", ["status_list"], "answered")
    row = json.loads(open(log.path, encoding="utf-8").read())
    assert row["user"] == "小明" and row["question"] == "誰遲到" and row["tools"] == ["status_list"]
    assert set(row) == {"time", "user", "question", "tools", "outcome"}


def sample_trace():
    from rag import dates, doc_store, router

    hit = doc_store.Hit(chunk={"source": "規章.pdf", "title": "請假辦法", "section": "第二章", "article": "第5條",
                               "page": 3, "body": "特休假依年資給假。" + "字" * 600},
                        score=0.8123456, dense=0.71, rerank=0.9321)
    plan = router.Plan(docs=True, data="count", person="王小明", status="遲到",
                       steps=[router.Step(data="leaves", person="王小明")])
    return answer.Trace(plan=plan, date_range=dates.DateRange("2026-11-01", "2026-11-30", "這個月"),
                        person="王小明", hits=[hit], data_text="王小明 遲到：3 次\n第二行", tools=["count", "leaves"],
                        outcome="answered", note="")


def test_answers_and_retrieval_details_are_logged_only_when_enabled(tmp_path):
    from rag import remote_access as ra

    off = ra.QuestionLog(str(tmp_path / "off.jsonl"))
    off.record("甲", "問題", ["count"], "answered", ra.trace_details(sample_trace(), "回答"))
    assert "answer" not in json.loads(open(off.path, encoding="utf-8").read())
    on = ra.QuestionLog(str(tmp_path / "on.jsonl"), with_answers=True)
    on.record("甲", "問題", ["count"], "answered", ra.trace_details(sample_trace(), "第一行\n第二行"))
    row = json.loads(open(on.path, encoding="utf-8").read())
    assert row["answer"] == "第一行\n第二行" and row["person"] == "王小明" and row["data_text"].startswith("王小明")
    assert row["plan"]["data"] == "count" and row["plan"]["steps"] == [
        {"data": "leaves", "person": "王小明", "status": None, "event_type": None}]
    assert row["date_range"] == {"start": "2026-11-01", "end": "2026-11-30", "phrase": "這個月"}
    hit = row["hits"][0]
    assert hit["source"] == "規章.pdf" and hit["label"] == "請假辦法 第二章 第5條" and hit["page"] == 3
    assert hit["rerank"] == 0.9321 and hit["score"] == 0.8123 and len(hit["text"]) == 400


def test_answer_log_is_capped_and_line_safe(tmp_path):
    from rag import remote_access as ra

    log = ra.QuestionLog(str(tmp_path / "a.jsonl"), with_answers=True)
    log.record("甲", "問題", [], "answered", ra.trace_details(answer.Trace(), "字" * 20000 + " 偽造"))
    text = open(log.path, encoding="utf-8").read()
    assert len(text.splitlines()) == 1 and " " not in text
    assert len(json.loads(text)["answer"]) == ra.ANSWER_LOG_CHARS


def test_gated_assistant_logs_the_full_streamed_answer_and_trace(tmp_path):
    from rag import remote_access as ra

    class Streaming(FakeAssistant):
        def ask(self, question):
            self.last = sample_trace()
            yield "前半"
            yield "後半"

    log = ra.QuestionLog(str(tmp_path / "q.jsonl"), with_answers=True)
    gated = ra.GatedAssistant(Streaming(), ra.Gate(), log, "小明")
    assert "".join(gated.ask("問題")) == "前半後半"
    row = logged(log)[0]
    assert row["answer"] == "前半後半" and row["tools"] == ["count", "leaves"] and row["hits"][0]["rerank"] == 0.9321


def test_busy_questions_log_no_answer_details(tmp_path):
    from rag import remote_access as ra

    gate = ra.Gate(max_concurrent=1, max_waiting=0, wait_seconds=0.1)
    gate.acquire(object())
    log = ra.QuestionLog(str(tmp_path / "q.jsonl"), with_answers=True)
    list(ra.GatedAssistant(FakeAssistant(), gate, log, "小明").ask("問題"))
    assert "answer" not in logged(log)[0]


def test_disabled_log_writes_nothing(tmp_path):
    log = remote_access.QuestionLog(str(tmp_path / "q.jsonl"), enabled=False)
    log.record("小明", "誰遲到", [], "answered")
    assert not (tmp_path / "q.jsonl").exists()


def make_gated(tmp_path, assistant, gate=None):
    log = remote_access.QuestionLog(str(tmp_path / "q.jsonl"))
    return remote_access.GatedAssistant(assistant, gate or remote_access.Gate(), log, "小明"), log


def logged(log):
    return [json.loads(line) for line in open(log.path, encoding="utf-8")]


def test_gated_assistant_answers_logs_tools_and_releases_slot(tmp_path):
    gate = remote_access.Gate(max_concurrent=1, max_waiting=1, wait_seconds=1)
    gated, log = make_gated(tmp_path, FakeAssistant(), gate)
    assert "".join(gated.ask("問題")) == "回答"
    assert gated.last.outcome == "answered"
    assert logged(log)[0]["tools"] == ["count"]
    assert "".join(gated.ask("再問")) == "回答"   # 名額有釋放，第二題才進得來


def test_gated_assistant_releases_slot_and_logs_error_when_assistant_raises(tmp_path):
    gate = remote_access.Gate(max_concurrent=1, max_waiting=1, wait_seconds=0.2)
    gated, log = make_gated(tmp_path, FakeAssistant(raises=True), gate)
    with pytest.raises(RuntimeError):
        list(gated.ask("問題"))
    assert logged(log)[0]["outcome"] == "error"
    gate.acquire(object())   # 名額已釋放


def test_gated_assistant_reports_busy_when_queue_is_full(tmp_path):
    gate = remote_access.Gate(max_concurrent=1, max_waiting=0, wait_seconds=0.1)
    gate.acquire(object())
    gated, log = make_gated(tmp_path, FakeAssistant(), gate)
    assert "".join(gated.ask("問題")) == remote_access.BUSY_TEXT
    assert gated.last.outcome == "busy"
    assert logged(log)[0]["outcome"] == "busy"
