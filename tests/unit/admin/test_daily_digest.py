"""
daily_digest 與 digest_store 的單元測試：明細內容、語言模型摘要的核對（數字、中文數字、
明細外的人名、模型失敗、沒有模型）、排程判斷（時間前後、休息日、補產、已有就不重做）、存檔與設定的容錯、
開窗顯示文字（過期不顯示、待處理異常）、本機模式不建語言模型。
"""
import datetime
import json
import sqlite3

import pytest

from admin import daily_digest as dd
from shared import digest_store

DAY = "2026-10-14"   # 星期三


@pytest.fixture
def punch(db):
    def _p(name, time, status, day=DAY):
        con = sqlite3.connect(db)
        con.execute("INSERT INTO Attendance (name, timestamp, status) VALUES (?, ?, ?)",
                    (name, f"{day} {time}", status))
        con.commit()
        con.close()

    return _p


@pytest.fixture
def office(add_user, punch, db):
    """甲準時、乙遲到、丙早退、丁與戊缺勤。"""
    for n, i in [("甲", "1"), ("乙", "2"), ("丙", "3"), ("丁", "4"), ("戊", "5")]:
        add_user(n, i)
    punch("甲", "07:50:00", "上班")
    punch("甲", "18:00:00", "下班")
    punch("乙", "08:31:00", "遲到")
    punch("乙", "18:00:00", "下班")
    punch("丙", "07:50:00", "上班")
    punch("丙", "17:05:00", "早退")


class FakeLLM:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.prompts = reply, error, []

    def complete(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.reply


def facts_for(day=DAY):
    from admin import attendance_report as ar

    return dd.build_facts(ar.build_report(day))


def test_facts_list_each_group_and_have_no_leave_section(office):
    text = facts_for()
    assert "出勤日報 2026-10-14（週三）" in text
    assert "在職 5 人：準時 1、遲到 1、早退 1、缺勤 2" in text
    assert "遲到：乙（08:31）" in text and "早退：丙（17:05）" in text
    assert "缺勤：丁、戊" in text and "請假" not in text
    assert text.endswith("當天異常事件 0 筆。")


def test_long_name_lists_are_cut(add_user, db):
    for i in range(14):
        add_user(f"員工{i:02d}", f"E{i}")
    text = facts_for()
    assert "缺勤：" in text and "等 14 人" in text and text.count("員工") == dd.MAX_NAMES


def test_a_good_summary_is_kept(office):
    facts = facts_for()
    llm = FakeLLM("10 月 14 日有 5 位在職員工，乙遲到、丙早退、戊缺勤，丁請病假。")
    assert dd.narrate(facts, ["甲", "乙", "丙", "丁", "戊"], llm).startswith("10 月 14 日")


@pytest.mark.parametrize("reply, why", [
    ("今天共有 6 人到班", "數字 6"),
    ("今天有三位員工遲到", "中文數字"),
    ("甲準時，己也準時", "己"),
    ("   ", "空白"),
])
def test_a_summary_that_does_not_check_out_is_dropped(office, reply, why):
    facts = facts_for()
    assert dd.narrate(facts, ["甲", "乙", "丙", "丁", "戊", "己"], FakeLLM(reply)) == "", why


def test_no_model_or_a_failing_model_still_leaves_the_facts(office):
    facts = facts_for()
    assert dd.narrate(facts, ["甲"], None) == ""
    assert dd.narrate(facts, ["甲"], FakeLLM(error=TimeoutError())) == ""


def test_generate_saves_the_digest_with_or_without_a_summary(office):
    saved = []
    digest = dd.generate(DAY, FakeLLM("共有 5 位員工，乙遲到。"), now=datetime.datetime(2026, 10, 14, 18, 0),
                         save=saved.append)
    assert saved == [digest] and digest["ai"] is True and digest["generated_at"] == "2026-10-14 18:00"
    plain = dd.generate(DAY, None, now=datetime.datetime(2026, 10, 14, 18, 0), save=saved.append)
    assert plain["summary"] == "" and plain["ai"] is False and "遲到：乙" in plain["facts"]


def test_the_prompt_only_carries_the_facts(office):
    llm = FakeLLM("共有 5 位員工。")
    dd.generate(DAY, llm, save=lambda d: None)
    assert "診斷" not in llm.prompts[0] and "出勤日報 2026-10-14" in llm.prompts[0]


# --- 排程 ---

def weekdays_only(d):
    return d.weekday() < 5


def at(y, m, d, h, mi):
    return datetime.datetime(y, m, d, h, mi)


def test_before_the_time_it_looks_at_the_previous_workday():
    assert dd.pending_day(at(2026, 10, 14, 17, 59), "18:00", None, weekdays_only) == "2026-10-13"
    assert dd.pending_day(at(2026, 10, 14, 17, 59), "18:00", "2026-10-13", weekdays_only) is None


def test_after_the_time_it_makes_today_once():
    assert dd.pending_day(at(2026, 10, 14, 18, 0), "18:00", "2026-10-13", weekdays_only) == "2026-10-14"
    assert dd.pending_day(at(2026, 10, 14, 22, 0), "18:00", "2026-10-14", weekdays_only) is None


def test_days_off_are_skipped_and_only_the_latest_missed_workday_is_made():
    # 週日晚上開機：最近的上班日是週五；已經有週五的就不做
    assert dd.pending_day(at(2026, 10, 18, 20, 0), "18:00", "2026-10-15", weekdays_only) == "2026-10-16"
    assert dd.pending_day(at(2026, 10, 18, 20, 0), "18:00", "2026-10-16", weekdays_only) is None


def test_never_a_workday_means_nothing_to_make():
    assert dd.pending_day(at(2026, 10, 14, 20, 0), "18:00", None, lambda d: False) is None


def test_default_workday_check_uses_the_work_calendar(db, tmp_path, monkeypatch):
    from shared import paths, work_calendar

    monkeypatch.setattr(paths, "WORK_CALENDAR_DIR", str(tmp_path / "wc"))
    work_calendar._cache.clear()
    days, day = [], datetime.date(2026, 1, 1)
    while day.year == 2026:
        special = day == datetime.date(2026, 10, 14)
        days.append({"date": day.strftime("%Y%m%d"), "week": "x", "isHoliday": day.weekday() >= 5 or special,
                     "description": "測試假日" if special else ""})
        day += datetime.timedelta(days=1)
    assert work_calendar.save_year(2026, days)
    assert dd.pending_day(at(2026, 10, 14, 20, 0), "18:00", None) == "2026-10-13"


def test_local_mode_never_builds_a_model(monkeypatch):
    assert dd.build_ai({"mode": "local", "api_base": "x", "api_key": "k", "api_model": "m"}) is None
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert dd.build_ai({"mode": "api", "api_base": "https://integrate.api.nvidia.com/v1", "api_key": "",
                        "api_model": "m"}) is None


# --- digest_store ---

def test_store_round_trip_and_bad_files(tmp_path):
    path = str(tmp_path / "d.json")
    assert digest_store.load(path) is None
    digest = {"date": DAY, "generated_at": "2026-10-14 18:00", "facts": "明細", "summary": ""}
    assert digest_store.save(digest, path) is True and digest_store.load(path) == digest
    (tmp_path / "d.json").write_text("{壞掉", encoding="utf-8")
    assert digest_store.load(path) is None
    (tmp_path / "d.json").write_text(json.dumps({"date": DAY}), encoding="utf-8")
    assert digest_store.load(path) is None
    assert digest_store.save(digest, str(tmp_path / "沒有這個資料夾" / "d.json")) is False


def test_time_setting_defaults_and_validation(tmp_path):
    path = str(tmp_path / "s.json")
    assert digest_store.load_time(path) == "18:00"
    digest_store.save_time("17:30", path)
    assert digest_store.load_time(path) == "17:30"
    for bad in ("25:00", "7:30", "18:60", "", "18-00"):
        with pytest.raises(ValueError):
            digest_store.save_time(bad, path)
    (tmp_path / "s.json").write_text(json.dumps({"time": "99:99"}), encoding="utf-8")
    assert digest_store.load_time(path) == "18:00"


def test_welcome_text_shows_latest_digest_and_pending_anomalies(tmp_path):
    path = str(tmp_path / "d.json")
    digest_store.save({"date": DAY, "generated_at": "2026-10-14 18:00", "facts": "出勤日報 …",
                       "summary": "摘要文字"}, path)
    text = digest_store.welcome_text(datetime.date(2026, 10, 15), path, unread=3)
    assert "資料日期 2026-10-14" in text and "AI 摘要（數字與人名已核對）：摘要文字" in text
    assert "3 筆異常事件尚未標為已讀" in text
    assert "尚未標為已讀" not in digest_store.welcome_text(datetime.date(2026, 10, 15), path, unread=0)


def test_welcome_text_is_empty_without_a_fresh_digest(tmp_path):
    path = str(tmp_path / "d.json")
    assert digest_store.welcome_text(datetime.date(2026, 10, 15), path, unread=0) == ""
    digest_store.save({"date": DAY, "generated_at": "x", "facts": "f"}, path)
    assert digest_store.welcome_text(datetime.date(2026, 10, 14) + datetime.timedelta(days=8), path, unread=0) == ""


def test_unread_count_needs_a_progress_file(tmp_path):
    fetch = lambda limit: [{"id": 3}, {"id": 5}, {"id": 9}]  # noqa: E731
    assert digest_store.unread_anomaly_count(str(tmp_path / "none.json"), fetch) == 0
    (tmp_path / "seen.json").write_text(json.dumps({"last_seen_id": 4}), encoding="utf-8")
    assert digest_store.unread_anomaly_count(str(tmp_path / "seen.json"), fetch) == 2


def test_atomic_write_retries_a_busy_target_and_leaves_no_temp_file(tmp_path, monkeypatch):
    import os

    path = str(tmp_path / "d.json")
    real_replace, failures = os.replace, {"left": 2}

    def flaky(src, dst):
        if failures["left"]:
            failures["left"] -= 1
            raise PermissionError("WinError 32")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(digest_store.time, "sleep", lambda s: None)
    digest = {"date": DAY, "generated_at": "x", "facts": "f"}
    assert digest_store.save(digest, path) is True and digest_store.load(path) == digest
    failures["left"] = 99
    assert digest_store.save(digest, path) is False                 # 一直失敗就放棄，回報失敗而不是丟例外
    assert [f.name for f in tmp_path.iterdir()] == ["d.json"]       # 暫存檔都清掉了


def test_two_writers_use_different_temp_files(tmp_path):
    import threading

    path = str(tmp_path / "d.json")
    errors = []

    def write(n):
        try:
            for i in range(30):
                digest_store._atomic_write(path, {"n": n, "i": i})
        except OSError as e:
            errors.append(e)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and [f.name for f in tmp_path.iterdir()] == ["d.json"]
