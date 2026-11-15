"""
calendar_fetch 的單元測試（全部用假的網路回應，不連網）：抓取成功寫入快取、各種失敗（狀態碼、轉址、太大、
不是 JSON、驗證不過、連線錯誤、全本地模式）都只回傳原因且不動到上一份快取、什麼時候該更新、
背景更新的重試間隔、問題說明。
"""
import datetime
import json
import time

import pytest
import requests

from admin import calendar_fetch as cf
from shared import network_policy, paths, work_calendar

NOW = datetime.datetime(2026, 11, 15, 9, 0)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_CALENDAR_DIR", str(tmp_path / "wc"))
    monkeypatch.setattr(network_policy, "local_only", lambda path=None: False)
    work_calendar._cache.clear()
    cf._set_problem("")


def make_year(year=2026, overrides=None):
    overrides = overrides or {}
    day, out = datetime.date(year, 1, 1), []
    while day.year == year:
        key = day.strftime("%Y%m%d")
        flag, name = overrides.get(key, (day.weekday() >= 5, ""))
        out.append({"date": key, "week": "x", "isHoliday": flag, "description": name})
        day += datetime.timedelta(days=1)
    return out


class FakeResponse:
    def __init__(self, body=b"", status=200, chunks=None):
        self.status_code = status
        self._chunks = chunks if chunks is not None else [body]

    def iter_content(self, chunk_size=0):
        yield from self._chunks


def json_response(data, status=200):
    return FakeResponse(json.dumps(data).encode("utf-8"), status)


class FakeGet:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def test_a_good_download_is_validated_and_cached():
    get = FakeGet(json_response(make_year(overrides={"20261014": (True, "測試連假")})))
    ok, message = cf.fetch_year(2026, get=get, now=NOW)
    assert ok and "已更新" in message
    assert work_calendar.day_off_reason("2026-10-14") == "測試連假"
    assert work_calendar.load_year(2026)[1] == "2026-11-15 09:00"


def test_the_request_is_fixed_https_without_redirects_and_with_a_timeout():
    get = FakeGet(json_response(make_year()))
    cf.fetch_year(2026, get=get, now=NOW)
    url, kwargs = get.calls[0]
    assert url == "https://cdn.jsdelivr.net/gh/ruyut/TaiwanCalendar/data/2026.json"
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True and kwargs["timeout"] == cf.TIMEOUT


@pytest.mark.parametrize("year", [1999, 2101, "2026", 2026.0, None, True])
def test_a_strange_year_is_refused_before_any_request(year):
    get = FakeGet(json_response(make_year()))
    ok, _ = cf.fetch_year(year, get=get)
    assert not ok and get.calls == []


@pytest.mark.parametrize("response, expected", [
    (FakeResponse(b"", 404), "404"),
    (FakeResponse(b"", 302), "302"),                                   # 不跟隨轉址：轉址回應直接當失敗
    (FakeResponse(chunks=[b"x" * 600_000, b"x" * 600_000]), "內容過大"),
    (FakeResponse(b"<html>not json</html>"), "不是有效的 JSON"),
    (FakeResponse('{"a": 1}'.encode("utf-16")), "不是有效的 JSON"),
    (json_response({"not": "a list"}), "驗證不過"),
    (json_response(make_year()[:-5]), "驗證不過"),
    (requests.ConnectionError("boom"), "連線失敗"),
    (requests.Timeout("slow"), "連線失敗"),
])
def test_every_failure_is_reported_without_raising_or_touching_the_good_cache(response, expected):
    work_calendar.save_year(2026, make_year(overrides={"20261014": (True, "上一份")}))
    ok, message = cf.fetch_year(2026, get=FakeGet(response), now=NOW)
    assert not ok and expected in message
    assert work_calendar.day_off_reason("2026-10-14") == "上一份"


def test_an_exception_message_never_leaks_into_the_reason():
    ok, message = cf.fetch_year(2026, get=FakeGet(requests.ConnectionError("secret-token-123")), now=NOW)
    assert not ok and "secret-token-123" not in message and "ConnectionError" in message


def test_local_only_mode_never_touches_the_network(monkeypatch):
    monkeypatch.setattr(network_policy, "local_only", lambda path=None: True)
    get = FakeGet(json_response(make_year()))
    ok, message = cf.fetch_year(2026, get=get, now=NOW)
    assert not ok and message == cf.OFFLINE_PROBLEM and get.calls == []


# --- 什麼時候該更新 ---

def test_a_missing_cache_is_due_and_a_fresh_one_is_not():
    assert cf.years_to_refresh(NOW.date(), NOW) == [2026, 2027]       # 十一月起加抓明年
    work_calendar.save_year(2026, make_year(), fetched_at="2026-11-10 08:00")
    assert cf.years_to_refresh(NOW.date(), NOW) == [2027]
    work_calendar.save_year(2027, make_year(2027), fetched_at="2026-11-10 08:00")
    assert cf.years_to_refresh(NOW.date(), NOW) == []


def test_next_year_is_only_fetched_from_november():
    assert cf.years_to_refresh(datetime.date(2026, 10, 31), datetime.datetime(2026, 10, 31, 9, 0)) == [2026]


def test_a_cache_older_than_thirty_days_is_due_again():
    work_calendar.save_year(2026, make_year(), fetched_at="2026-10-01 08:00")
    work_calendar.save_year(2027, make_year(2027), fetched_at="2026-11-10 08:00")
    assert cf.years_to_refresh(NOW.date(), NOW) == [2026]


def test_refresh_downloads_only_what_is_due_and_clears_the_problem_on_success():
    work_calendar.save_year(2026, make_year(), fetched_at="2026-11-10 08:00")
    get = FakeGet(json_response(make_year(2027)))
    results = cf.refresh(get=get, now=NOW)
    assert [(y, ok) for y, ok, _m in results] == [(2027, True)] and len(get.calls) == 1
    assert cf.problem() == ""


def test_refresh_records_what_went_wrong():
    cf.refresh(get=FakeGet(FakeResponse(b"", 500)), now=NOW)
    assert "500" in cf.problem()


def test_the_offline_problem_is_shown_only_when_there_is_no_cache(monkeypatch):
    monkeypatch.setattr(network_policy, "local_only", lambda path=None: True)
    cf.refresh(get=FakeGet(json_response([])), now=NOW)
    assert cf.problem() == cf.OFFLINE_PROBLEM


# --- 背景更新 ---

def wait_idle(refresher, seconds=3):
    end = time.time() + seconds
    while refresher._busy and time.time() < end:
        time.sleep(0.01)
    assert not refresher._busy


def test_tick_updates_in_the_background_and_then_goes_quiet():
    get = FakeGet(json_response(make_year()), json_response(make_year(2027)))
    r = cf.CalendarRefresher(get=get, clock=lambda: NOW)
    assert r.tick() is True
    wait_idle(r)
    assert len(get.calls) == 2 and cf.years_to_refresh(NOW.date(), NOW) == []
    assert r.tick() is False and len(get.calls) == 2


def test_a_failed_update_waits_an_hour_before_trying_again():
    clock = [1000.0]
    get = FakeGet(requests.ConnectionError("offline"))
    r = cf.CalendarRefresher(get=get, clock=lambda: NOW, monotonic=lambda: clock[0])
    assert r.tick() is True
    wait_idle(r)
    first = len(get.calls)
    assert "連線失敗" in cf.problem()
    clock[0] += 60
    assert r.tick() is False and len(get.calls) == first             # 一分鐘後不會狂重試
    clock[0] += cf.RETRY_AFTER_SECONDS
    assert r.tick() is True
    wait_idle(r)
    assert len(get.calls) > first


def test_only_one_update_runs_at_a_time():
    r = cf.CalendarRefresher(get=FakeGet(json_response(make_year())), clock=lambda: NOW)
    r._busy = True
    assert r.tick() is False


def test_an_unexpected_error_in_the_background_is_contained(monkeypatch):
    def boom(**_k):
        raise RuntimeError("internal detail")

    monkeypatch.setattr(cf, "refresh", boom)
    r = cf.CalendarRefresher(clock=lambda: NOW)
    assert r.tick() is True
    wait_idle(r)
    assert "RuntimeError" in cf.problem() and "internal detail" not in cf.problem()
