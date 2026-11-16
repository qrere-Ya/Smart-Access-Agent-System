"""
calendar_fetch.py：自動抓取國定假日與補班日（只在後台，前台不上網），驗證後存成 work_calendar 的快取。

主要功能：
  - fetch_year：抓某一年的資料（網址寫死、只收 https、不跟隨轉址、大小上限、逾時），
    交給 work_calendar 嚴格驗證後才寫入；
    任何失敗（沒網路、狀態碼、太大、不是 JSON、驗證不過）都只回傳原因，不丟例外，也不動到上一份可用的快取
  - years_to_refresh：該更新哪幾年——今年、十一月起再加明年；沒有快取、或快取超過 30 天才更新（不是每次都抓）
  - CalendarRefresher：後台每分鐘呼叫 tick()，到期就在背景執行緒更新；失敗後至少隔 1 小時才再試，不會狂重試
  - 全本地模式一律不抓（用快取，沒有快取就只算週六日），並留下提示給後台畫面
  - problem：最近一次更新的問題說明（沒有問題回傳空字串），給出勤報告頁顯示

資料來源：ruyut/TaiwanCalendar（社群維護，不是官方站）經 jsDelivr 提供；網址不可設定。
  備案（政府資料開放平臺的政府行政機關辦公日曆）尚未查證，需要換來源時再確認。

串接模組：
  - shared.work_calendar：parse_year／save_year／load_year（驗證與快取）
  - shared.network_policy：全本地模式
  - backend_main：每分鐘呼叫 tick；calendar_panel：顯示來源、最後更新與 problem
"""
import datetime
import json
import threading
import time

import requests

from shared import network_policy, work_calendar

URL_TEMPLATE = "https://cdn.jsdelivr.net/gh/ruyut/TaiwanCalendar/data/{year}.json"
TIMEOUT = (5, 15)
MAX_BYTES = 1_000_000                       # 一整年約 60KB，超過 1MB 一定不對
MIN_YEAR, MAX_YEAR = 2000, 2100
REFRESH_AFTER_DAYS = 30                     # 快取超過這麼多天才再抓
NEXT_YEAR_FROM_MONTH = 11                   # 十一月起也抓明年的（通常這時已公布）
RETRY_AFTER_SECONDS = 3600                  # 失敗後至少隔多久才再試
OFFLINE_PROBLEM = "全本地模式已開啟，沒有連網更新國定假日；使用現有快取，沒有快取就只算週六、週日"

_problem = ""
_problem_lock = threading.Lock()


def problem():
    """最近一次更新的問題說明；沒有問題回傳空字串。"""
    with _problem_lock:
        return _problem


def _set_problem(text):
    global _problem
    with _problem_lock:
        _problem = text


def _read_limited(response):
    data = b""
    for chunk in response.iter_content(chunk_size=65536):
        data += chunk
        if len(data) > MAX_BYTES:
            raise ValueError("內容過大")
    return data


def fetch_year(year, get=requests.get, now=None):
    """抓並驗證存入某一年；回傳 (成功與否, 說明)。不丟例外，失敗時不動到上一份可用的快取。"""
    if not isinstance(year, int) or isinstance(year, bool) or not MIN_YEAR <= year <= MAX_YEAR:
        return False, f"年份不合理：{year!r}"
    try:
        network_policy.require_online("國定假日資料")
    except network_policy.OfflineError:
        return False, OFFLINE_PROBLEM
    try:
        response = get(URL_TEMPLATE.format(year=year), timeout=TIMEOUT, allow_redirects=False, stream=True,
                       headers={"User-Agent": "Mozilla/5.0 (SmartAccessAgent)"})
        if response.status_code != 200:
            return False, f"{year} 年假日資料來源回應 {response.status_code}"
        raw = _read_limited(response)
    except requests.RequestException as e:
        return False, f"{year} 年假日資料連線失敗（{type(e).__name__}）"
    except ValueError as e:
        return False, f"{year} 年假日資料：{e}"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False, f"{year} 年假日資料不是有效的 JSON"
    stamp = (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M")
    if not work_calendar.save_year(year, data, fetched_at=stamp):
        return False, f"{year} 年假日資料驗證不過，已丟棄（保留上一份可用的）"
    return True, f"{year} 年假日資料已更新"


def years_to_refresh(today=None, now=None):
    """該更新哪幾年：缺資料或快取超過 REFRESH_AFTER_DAYS 天的。"""
    today = today or datetime.date.today()
    now = now or datetime.datetime.now()
    years = [today.year] + ([today.year + 1] if today.month >= NEXT_YEAR_FROM_MONTH else [])
    due = []
    for year in years:
        days, fetched_at = work_calendar.load_year(year)
        if days is None:
            due.append(year)
            continue
        try:
            age = now - datetime.datetime.strptime(fetched_at, "%Y-%m-%d %H:%M")
        except ValueError:
            due.append(year)
            continue
        if age >= datetime.timedelta(days=REFRESH_AFTER_DAYS):
            due.append(year)
    return due


def refresh(get=requests.get, now=None):
    """更新所有該更新的年份，回傳 [(年, 成功與否, 說明)]，並記下 problem。"""
    now = now or datetime.datetime.now()
    results = []
    for year in years_to_refresh(now.date(), now):
        ok, message = fetch_year(year, get=get, now=now)
        results.append((year, ok, message))
    failed = list(dict.fromkeys(message for _y, ok, message in results if not ok))      # 同樣的原因只寫一次
    if failed:
        _set_problem("；".join(failed))
    elif network_policy.local_only():
        _set_problem(OFFLINE_PROBLEM if work_calendar.load_year(now.year)[0] is None else "")
    else:
        _set_problem("")
    return results


class CalendarRefresher:
    """後台每分鐘呼叫 tick()：到期就在背景更新一次；失敗後至少隔 RETRY_AFTER_SECONDS 才再試。"""

    def __init__(self, get=requests.get, clock=datetime.datetime.now, monotonic=time.monotonic):
        self._get, self._clock, self._monotonic = get, clock, monotonic
        self._busy = False
        self._next_try = 0.0
        self._lock = threading.Lock()

    def tick(self):
        """沒有東西到期就什麼都不做；回傳是否啟動了背景更新。"""
        with self._lock:
            if self._busy or self._monotonic() < self._next_try:
                return False
            now = self._clock()
            if not years_to_refresh(now.date(), now):
                if network_policy.local_only() and work_calendar.load_year(now.year)[0] is None:
                    _set_problem(OFFLINE_PROBLEM)
                return False
            self._busy = True
        threading.Thread(target=self._work, daemon=True).start()
        return True

    def _work(self):
        try:
            results = refresh(get=self._get, now=self._clock())
            if any(not ok for _y, ok, _m in results):
                self._next_try = self._monotonic() + RETRY_AFTER_SECONDS
        except Exception as e:  # noqa: BLE001  背景執行緒不能讓例外往外丟
            _set_problem(f"更新假日資料時發生錯誤（{type(e).__name__}）")
            self._next_try = self._monotonic() + RETRY_AFTER_SECONDS
        finally:
            with self._lock:
                self._busy = False
