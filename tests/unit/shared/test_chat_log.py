"""
chat_log 的單元測試（臨時資料庫）：寫入與長度上限、來源與審查結果的檢查、列表篩選與摘要、審查標記與清除、
審查統計（沒有準確率）、30 天到期清理、CSV 匯出（中文、公式注入）、寫入失敗不丟例外、多執行緒同時寫入。
"""
import csv
import datetime
import json
import sqlite3
import threading

import pytest

from shared import chat_log as cl

NOW = datetime.datetime(2026, 11, 16, 10, 0, 0)


def cite(title="請假辦法", text="第1條 每年七天特別休假", score=0.91):
    return {"title": title, "label": f"{title} 第1條", "score": score, "text": text}


def test_a_record_keeps_every_field(db):
    log_id = cl.record(cl.SOURCE_FRONT, "特休有幾天？", "七天 [1]", [cite()], now=NOW)
    row = cl.get_log(log_id)
    assert row["ts"] == "2026-11-16 10:00:00" and row["source"] == "front_knowledge"
    assert row["question"] == "特休有幾天？" and row["answer"] == "七天 [1]"
    assert row["citations"] == [{"title": "請假辦法", "label": "請假辦法 第1條", "score": 0.91,
                                 "text": "第1條 每年七天特別休假"}]
    assert row["review"] == "unreviewed" and row["reviewed_at"] is None and row["reviewer"] is None


def test_unknown_sources_and_reviews_are_refused(db):
    with pytest.raises(ValueError):
        cl.record("somewhere", "q", "a")
    log_id = cl.record(cl.SOURCE_BACK, "q", "a")
    with pytest.raises(ValueError):
        cl.set_review(log_id, "great")
    with pytest.raises(ValueError):
        cl.list_logs(source="x")
    with pytest.raises(ValueError):
        cl.list_logs(review="x")
    with pytest.raises(ValueError):
        cl.list_logs(start="2026/11/01")


def test_long_text_is_truncated_and_says_so(db):
    log_id = cl.record(cl.SOURCE_BACK, "問" * 5000, "答" * 20000,
                       [cite(text="字" * 9000)] + [cite(title=str(i)) for i in range(30)])
    row = cl.get_log(log_id)
    assert len(row["question"]) == cl.MAX_QUESTION_CHARS + len(cl.TRUNCATED) and row["question"].endswith(cl.TRUNCATED)
    assert len(row["answer"]) == cl.MAX_ANSWER_CHARS + len(cl.TRUNCATED)
    assert len(row["citations"]) == cl.MAX_CITATIONS
    assert len(row["citations"][0]["text"]) == cl.MAX_CITATION_TEXT + len(cl.TRUNCATED)


def test_bad_citation_items_are_dropped_and_a_bad_score_becomes_none(db):
    bad = ["字串", None, 5, {"title": "x", "score": "高"}, {"title": "y", "score": True}]
    log_id = cl.record(cl.SOURCE_BACK, "q", "a", bad)
    assert [(c["title"], c["score"]) for c in cl.get_log(log_id)["citations"]] == [("x", None), ("y", None)]


def test_the_list_has_only_a_short_summary_and_the_newest_first(db):
    cl.record(cl.SOURCE_BACK, "第一題\n換行", "答一", now=NOW - datetime.timedelta(hours=2))
    cl.record(cl.SOURCE_FRONT, "x" * 200, "答二", now=NOW)
    rows = cl.list_logs()
    assert [r["source"] for r in rows] == ["front_knowledge", "back_attendance"]
    assert "answer" not in rows[0] and "citations" not in rows[0]
    assert rows[1]["question"] == "第一題 換行" and len(rows[0]["question"]) <= 60 + len(cl.TRUNCATED)


def test_filters_by_source_review_and_day(db):
    a = cl.record(cl.SOURCE_BACK, "a", "a", now=datetime.datetime(2026, 11, 10, 9, 0))
    b = cl.record(cl.SOURCE_FRONT, "b", "b", now=datetime.datetime(2026, 11, 12, 23, 59, 59))
    c = cl.record(cl.SOURCE_FRONT, "c", "c", now=datetime.datetime(2026, 11, 13, 0, 0, 0))
    cl.set_review(b, cl.REVIEW_WRONG, "管理者甲", now=NOW)
    assert [r["id"] for r in cl.list_logs(source=cl.SOURCE_FRONT)] == [c, b]
    assert [r["id"] for r in cl.list_logs(review=cl.REVIEW_WRONG)] == [b]
    assert [r["id"] for r in cl.list_logs(start="2026-11-12", end="2026-11-12")] == [b]      # 含頭尾整天
    assert [r["id"] for r in cl.list_logs(start="2026-11-11")] == [c, b]
    assert [r["id"] for r in cl.list_logs(end="2026-11-10")] == [a]


def test_reviewing_and_clearing_a_review(db):
    log_id = cl.record(cl.SOURCE_FRONT, "q", "a")
    assert cl.set_review(log_id, cl.REVIEW_HALLUCINATION, "  管理者甲  ", now=NOW)
    row = cl.get_log(log_id)
    assert row["review"] == "hallucination" and row["reviewer"] == "管理者甲"
    assert row["reviewed_at"] == "2026-11-16 10:00:00"
    assert cl.set_review(log_id, cl.REVIEW_NONE)
    row = cl.get_log(log_id)
    assert row["review"] == "unreviewed" and row["reviewed_at"] is None and row["reviewer"] is None
    assert cl.set_review(99999, cl.REVIEW_CORRECT) is False


def test_the_summary_counts_labels_and_never_says_accuracy(db):
    ids = [cl.record(cl.SOURCE_FRONT, str(i), "a") for i in range(7)]
    cl.set_review(ids[0], cl.REVIEW_CORRECT)
    cl.set_review(ids[1], cl.REVIEW_CORRECT)
    cl.set_review(ids[2], cl.REVIEW_WRONG)
    cl.set_review(ids[3], cl.REVIEW_HALLUCINATION)
    counts = cl.review_counts()
    assert counts == {"unreviewed": 3, "correct": 2, "wrong": 1, "hallucination": 1, "total": 7}
    text = cl.review_summary(counts)
    assert text == "已審查 4 筆：正確 2、錯誤 1、幻覺 1；未審查 3 筆"
    assert "準確" not in text and "%" not in text
    assert cl.review_counts(source=cl.SOURCE_BACK)["total"] == 0


def test_counts_are_over_everything_not_just_the_first_page(db):
    with sqlite3.connect(db) as con:
        con.executemany("INSERT INTO ChatLogs (ts, source, question, answer) VALUES (?, 'back_attendance', 'q', 'a')",
                        [("2026-11-16 10:00:00",)] * (cl.LIST_LIMIT + 50))
    assert cl.review_counts()["total"] == cl.LIST_LIMIT + 50 and len(cl.list_logs()) == cl.LIST_LIMIT


def test_records_older_than_thirty_days_are_purged_and_newer_ones_stay(db):
    old = cl.record(cl.SOURCE_BACK, "old", "a", now=NOW - datetime.timedelta(days=31))
    edge = cl.record(cl.SOURCE_BACK, "edge", "a", now=NOW - datetime.timedelta(days=30))
    new = cl.record(cl.SOURCE_BACK, "new", "a", now=NOW - datetime.timedelta(days=1))
    cl.set_review(old, cl.REVIEW_HALLUCINATION)                        # 標了幻覺也一樣到期（所以刪除前要先匯出）
    assert cl.count_expiring(now=NOW) == 1
    assert cl.purge_expired(now=NOW) == 1
    assert {r["id"] for r in cl.list_logs()} == {edge, new}
    assert cl.purge_expired(now=NOW) == 0


def test_a_31_day_old_row_edited_by_hand_is_removed_by_the_purge(db):
    log_id = cl.record(cl.SOURCE_BACK, "q", "a", now=NOW)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE ChatLogs SET ts = '2026-10-16 09:59:59' WHERE id = ?", (log_id,))
    assert cl.purge_expired(now=NOW) == 1 and cl.get_log(log_id) is None


def test_csv_export_opens_in_excel_with_chinese_and_blocks_formula_injection(db, tmp_path):
    a = cl.record(cl.SOURCE_FRONT, "=HYPERLINK(\"http://x\")", "+cmd", [cite()], now=NOW)
    cl.set_review(a, cl.REVIEW_CORRECT, "@甲", now=NOW)
    cl.record(cl.SOURCE_BACK, "王小明今天遲到嗎", "有", [{"title": "出缺勤查詢：status_list", "text": "表格"}], now=NOW)
    out = tmp_path / "log.csv"
    assert cl.export_csv(str(out)) == 2
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                                  # utf-8-sig，Excel 不會亂碼
    rows = list(csv.reader(out.read_text(encoding="utf-8-sig").splitlines(True)))
    assert rows[0] == cl.CSV_HEADER
    by_question = {r[2]: r for r in rows[1:]}
    formula = by_question["'=HYPERLINK(\"http://x\")"]
    assert formula[3] == "'+cmd" and formula[5] == "正確" and formula[7] == "'@甲" and formula[1] == "前台知識庫"
    assert "請假辦法" in formula[4] and "0.91" in formula[4]
    assert by_question["王小明今天遲到嗎"][1] == "後台出缺勤" and by_question["王小明今天遲到嗎"][5] == "未審查"


def test_csv_export_respects_the_filters(db, tmp_path):
    cl.record(cl.SOURCE_BACK, "a", "a", now=NOW)
    cl.record(cl.SOURCE_FRONT, "b", "b", now=NOW)
    out = tmp_path / "f.csv"
    assert cl.export_csv(str(out), source=cl.SOURCE_FRONT) == 1


def test_try_record_never_raises_even_when_the_database_is_broken(db, monkeypatch, capsys):
    monkeypatch.setattr(cl.database_mgr, "DB_PATH", str(db) + ".missing_dir/x.db")
    assert cl.try_record(cl.SOURCE_BACK, "q", "a") is None
    assert "寫入失敗" in capsys.readouterr().out
    assert cl.try_record("bad source", "q", "a") is None


def test_the_table_has_no_ip_or_token_column(db):
    with sqlite3.connect(db) as con:
        columns = {r[1] for r in con.execute("PRAGMA table_info(ChatLogs)")}
    assert columns == {"id", "ts", "source", "question", "answer", "citations", "review", "reviewed_at", "reviewer"}


def test_simultaneous_writers_from_many_threads_all_land(db):
    errors = []

    def write(i):
        try:
            cl.record(cl.SOURCE_FRONT if i % 2 else cl.SOURCE_BACK, f"q{i}", f"a{i}", [cite()])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and cl.review_counts()["total"] == 24
    assert json.loads(json.dumps(cl.get_log(cl.list_logs()[0]["id"])["citations"]))
