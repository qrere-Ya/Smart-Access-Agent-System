"""前台儀表板的畫面內容：晨報分頁與換頁、各種沒有晨報的說明、打卡問候與警示的過期、狀態文字與色調、超長文字截斷。"""
import datetime as dt

import pytest

from access import front_view as fv
from access.front_store import FrontStore, ScreenState

NOW = dt.datetime(2026, 11, 11, 8, 30, 0)
TS = 1_000_000.0


def card(i, **extra):
    return {"category": "AI", "title": f"新聞 {i}", "what": f"發生了第 {i} 件事", "why": "因為重要",
            "how": "可以這樣用", "source": "iThome", "link": "https://example.com/x", **extra}


def brief(n=7, day="2026-11-11", **extra):
    return {"date": day, "generated_at": "2026-11-11 08:30", "news": [card(i) for i in range(n)], "news_problem": "",
            **extra}


# --- 時間 ---

def test_clock_and_date_texts_in_traditional_chinese():
    assert fv.clock_texts(NOW) == ("08:30", "2026 年 11 月 11 日　星期三")
    assert fv.clock_texts(dt.datetime(2026, 11, 15, 23, 5))[1].endswith("星期日")


# --- 晨報 ---

def test_news_is_split_into_pages_of_three_and_rotates_every_page_interval():
    pages = {fv.news_view(brief(7), NOW.date(), TS + k * fv.PAGE_SECONDS)["page"] for k in range(9)}
    assert pages == {1, 2, 3}                                              # 7 則 → 3、3、1 三頁，輪流顯示
    view = fv.news_view(brief(7), NOW.date(), 0.0)
    assert view["state"] == "ok" and view["pages"] == 3 and len(view["cards"]) == 3
    last = fv.news_view(brief(7), NOW.date(), 2 * fv.PAGE_SECONDS)
    assert last["page"] == 3 and len(last["cards"]) == 1


def test_a_page_stays_the_same_within_one_interval():
    a = fv.news_view(brief(7), NOW.date(), 100.0)
    b = fv.news_view(brief(7), NOW.date(), 100.0 + fv.PAGE_SECONDS - 1)
    assert a["page"] == b["page"] and a["cards"] == b["cards"]


def test_no_brief_yet_says_so_in_plain_words():
    for missing in (None, "text", [], 5):
        view = fv.news_view(missing, NOW.date(), TS)
        assert view["state"] == "empty" and "還沒有晨報" in view["message"] and view["cards"] == []


def test_a_brief_without_news_explains_the_problem_and_that_the_back_will_retry():
    view = fv.news_view(brief(0, news_problem="沒有網路"), NOW.date(), TS)
    assert view["state"] == "problem" and "沒有網路" in view["message"] and "自動重抓" in view["message"]
    assert fv.news_view(brief(0), NOW.date(), TS)["message"] == "這一天沒有新聞。"


def test_yesterdays_brief_is_labelled_not_passed_off_as_todays():
    view = fv.news_view(brief(3, day="2026-11-10"), NOW.date(), TS)
    assert view["state"] == "ok" and "2026-11-10" in view["heading"] and "今天的還沒產生" in view["heading"]
    assert fv.news_view(brief(3), NOW.date(), TS)["heading"] == "今日科技早報"


def test_very_long_or_hostile_news_text_cannot_blow_up_the_layout():
    nasty = card(1, title="長" * 500, what="說" * 500, why="x" * 500, how="y" * 500, source="s" * 500,
                 category="類" * 99)
    shown = fv.news_view({"date": "2026-11-11", "news": [nasty]}, NOW.date(), TS)["cards"][0]
    assert len(shown["title"]) <= fv.MAX_TITLE and shown["title"].endswith("…")
    assert len(shown["what"]) <= fv.MAX_WHAT and len(shown["why"]) <= fv.MAX_NOTE and len(shown["how"]) <= fv.MAX_NOTE
    assert len(shown["source"]) <= 30 and len(shown["category"]) <= 12
    assert "link" not in shown                                              # 前台不上網，不顯示原文連結


def test_malformed_news_entries_are_skipped_not_fatal():
    messy = {"date": "2026-11-11", "news": [None, "x", 5, {"no_title": 1}, card(1)]}
    view = fv.news_view(messy, NOW.date(), TS)
    assert [c["title"] for c in view["cards"]] == ["新聞 1"]


# --- 警示與問候 ---

def test_alert_texts_cover_the_three_kinds_and_unknown_kinds():
    assert fv.alert_view(None) is None and fv.alert_view({}) is None
    assert fv.alert_view({"kind": "tailgate", "detail": "畫面有 2 人"})["title"] == "偵測到尾隨"
    assert fv.alert_view({"kind": "no_scan_walkby", "detail": ""})["title"] == "有人經過鏡頭但沒有打卡"
    assert "非真人" in fv.alert_view({"kind": "spoof"})["title"]
    unknown = fv.alert_view({"kind": "something_new", "detail": "x" * 500})
    assert unknown["title"] == "偵測到異常" and len(unknown["detail"]) <= 80


def test_greeting_rows_are_limited_and_keep_newest_first():
    punches = [{"name": f"人{i}", "greeting": "早安", "clock": f"08:{i:02d}"} for i in range(9, 0, -1)]
    rows = fv.greeting_rows(punches)
    assert len(rows) == fv.GREETING_ROWS and rows[0] == ("08:09", "人9", "早安")


# --- 狀態 ---

def test_offline_back_is_a_warning_not_an_error_because_the_front_keeps_working():
    assert fv.link_view(True, 0) == ("success", "後台連線中")
    assert fv.link_view(True, 3) == ("success", "後台連線中（待補送 3 筆）")
    assert fv.link_view(False, 0)[0] == "warning" and "先暫存" in fv.link_view(False, 0)[1]
    assert "已暫存 4 筆" in fv.link_view(False, 4)[1]


def test_status_rows_flag_a_dead_camera_and_missing_faces():
    rows = {name: (text, tone) for name, text, tone in fv.status_rows(True, 0, 0, {"camera_ok": False,
                                                                                    "recognition_running": False})}
    assert rows["鏡頭"][1] == "danger" and rows["人臉辨識"][1] == "danger" and rows["人臉資料"][1] == "warning"
    healthy = {name: tone for name, _t, tone in fv.status_rows(True, 0, 12, {"camera_ok": True,
                                                                              "recognition_running": True})}
    assert set(healthy.values()) == {"success"}


# --- 整合：用真的前台資料庫 ---

@pytest.fixture
def store(tmp_path):
    return FrontStore(str(tmp_path / "front.db"))


def test_snapshot_assembles_everything_from_the_local_front_data(store):
    screen = ScreenState(store)
    store.set_meta("morning", brief(4))
    screen.note_punch("王小明", "早安", now=lambda: TS - 30, clock_text="08:29")
    screen.set_alert("tailgate", "畫面有 2 人", now=lambda: TS - 5)
    store.set_link_state(True, now=lambda: TS - 3)
    view = fv.snapshot(store, screen, lambda: {"camera_ok": True, "recognition_running": True}, NOW, TS)
    assert view["time"] == "08:30" and view["link"][0] == "success"
    assert view["greetings"] == [("08:29", "王小明", "早安")]
    assert view["alert"]["title"] == "偵測到尾隨" and view["news"]["state"] == "ok" and view["theme"] == "dark"


def test_old_punches_and_alerts_disappear_from_the_screen(store):
    screen = ScreenState(store)
    screen.note_punch("王小明", "早安", now=lambda: TS - fv.GREETING_MAX_AGE - 1, clock_text="08:00")
    screen.set_alert("spoof", "", now=lambda: TS - fv.ALERT_SECONDS - 1)
    view = fv.snapshot(store, screen, lambda: {}, NOW, TS)
    assert view["greetings"] == [] and view["alert"] is None
    assert view["link"][0] == "warning"                                    # 代理沒回報過 → 當作離線


def test_snapshot_survives_broken_stored_values(store):
    screen = ScreenState(store)
    store.set_meta("recent_punches", "壞掉")
    store.set_meta("alert", {"kind": "tailgate"})                          # 缺 ts
    store.set_meta("morning", "不是 dict")
    view = fv.snapshot(store, screen, lambda: {}, NOW, TS)
    assert view["greetings"] == [] and view["alert"] is None and view["news"]["state"] == "empty"


def test_module_tiles_preview_the_news_the_last_punch_and_the_worst_status():
    news = {"state": "ok", "cards": [{"title": "某則新聞標題"}], "message": ""}
    ok_status = [("後台連線", "後台連線中", "success"), ("鏡頭", "正常", "success")]
    tiles = fv.module_tiles(news, [("08:03", "陳大華", "早安"), ("08:01", "王小明", "早安")], ok_status)
    assert [t["key"] for t in tiles] == ["news", "greetings", "status"]
    assert tiles[0]["summary"] == "某則新聞標題" and tiles[1]["summary"] == "陳大華 早安"
    assert tiles[2]["summary"] == "全部正常" and tiles[2]["accent"] == "success"


def test_module_tiles_explain_empty_states_and_raise_the_worst_status_tone():
    news = {"state": "empty", "cards": [], "message": "還沒有晨報"}
    status = [("後台連線", "後台離線，打卡先暫存", "warning"), ("鏡頭", "異常或未啟動", "danger")]
    tiles = fv.module_tiles(news, [], status)
    assert tiles[0]["summary"] == "還沒有晨報" and tiles[1]["summary"] == "目前沒有人打卡"
    assert tiles[2]["accent"] == "danger" and tiles[2]["summary"].startswith("鏡頭：")


def test_load_tone_and_speed_text():
    assert [fv.load_tone(x) for x in (10, 70, 95)] == ["success", "warning", "danger"]
    assert fv.speed_text("↓", None) == "↓ —" and fv.speed_text("↓", 12.34) == "↓ 12.3 KB/s"
    assert fv.speed_text("↑", 2048) == "↑ 2.0 MB/s"
