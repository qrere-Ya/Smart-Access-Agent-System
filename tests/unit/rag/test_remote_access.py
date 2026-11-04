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
