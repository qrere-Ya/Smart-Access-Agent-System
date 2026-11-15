"""
出勤日報與科技早報的真 Tk 測試：主選單多一張「出勤日報與科技早報」卡片（設定與立即產生都在裡面）、
底部只有一顆「出勤日報」按鈕、
查看視窗只看出勤日報不設定（科技早報在前台螢幕顯示，後台不看）、時間不合法不儲存且不動到另一個、
立即產生會更新畫面與查看視窗、日期欄不合法不產生。
"""
import datetime
import time
import tkinter

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("customtkinter")


@pytest.fixture
def settings_files(tmp_path, monkeypatch):
    """報告與設定檔改放臨時資料夾，不碰真實 runtime。"""
    from shared import digest_store, paths

    for name, filename in (("DIGEST_PATH", "d.json"), ("BRIEF_PATH", "b.json"), ("DIGEST_SETTINGS_PATH", "s.json")):
        monkeypatch.setattr(paths, name, str(tmp_path / filename))
    return digest_store


def host_view(root, cls, *args, size="960x860", **kw):
    """出勤日報頁與排程頁現在是嵌在主視窗的頁面；測試用一個空的頂層視窗當主視窗。"""
    import customtkinter as ctk

    from shared import ui_theme

    top = ctk.CTkToplevel(root)
    top.geometry(size)
    ui_theme.style_window(top)
    view = cls(top, *args, **kw)
    view.pack(fill="both", expand=True)
    view.host = top
    top.deiconify()
    return view


def pump(root, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def texts_of(widget):
    found = []
    for child in widget.winfo_children():
        try:
            found.append(child.cget("text"))
        except (ValueError, AttributeError, tkinter.TclError):
            pass
        found += texts_of(child)
    return found


def fake_runner(root, name, settings_files, calls):
    from admin import digest_window

    def generate(day, llm):
        calls.append((name, day))
        digest = {"date": day, "generated_at": "2026-11-05 19:00", "facts": f"出勤日報 {day}", "summary": ""}
        if name == "科技早報":
            from admin import news_feed

            card = news_feed._to_card({"title": f"測試新聞 {day}", "summary": "", "link": "", "time": None}, "來源")
            settings_files.save_brief({"date": day, "generated_at": digest["generated_at"], "news": [card],
                                       "news_problem": "", "news_tries": 1})
        else:
            settings_files.save(digest)
        return digest

    return digest_window.DigestRunner(root, lambda now: None, generate, make_ai=lambda: None, name=name)


def test_the_main_menu_has_the_new_module_and_the_footer_has_one_button(root, db, settings_files, monkeypatch):
    from admin import digest_window
    from admin.backend_main import BackendManagerApp

    monkeypatch.setattr(digest_window.DigestRunner, "tick", lambda self: None)     # 這個測試不要自動產生
    root.deiconify()
    app = BackendManagerApp(root)
    pump(root)
    texts = texts_of(root)
    assert "出勤日報與科技早報" in texts and "📋 出勤日報" in texts and "📰 出勤日報／科技早報" not in texts
    assert not {"🌅 科技早報", "⚙ 排程"} & set(texts)
    app.open_schedule()
    pump(root)
    assert app.page_key == "schedule" and "📰 出勤日報與科技早報" in texts_of(root)      # 直接換頁，不另開視窗
    app.open_report("digest")
    pump(root)
    assert app.page_key == "report" and "📋 出勤日報" in texts_of(root)
    first = app.report_view
    app.open_report("morning")                                   # 舊呼叫端傳 morning 也只顯示日報，且不重複建
    assert app.report_view is first and app.report_view.tabs.get() == "📋 出勤日報" and "morning" not in first.bubbles
    app.show_home()
    assert app.page_key == "home"
    app.teardown()


def test_the_viewer_only_views(root, db, settings_files):
    from admin import digest_window

    calls = []
    win = host_view(root, digest_window.ReportView, fake_runner(root, "出勤日報", settings_files, calls),
                    fake_runner(root, "科技早報", settings_files, calls), "digest")
    pump(root)
    labels = texts_of(win)
    assert not any("HH:MM" in t or "儲存" in t or "立即產生" in t for t in labels if isinstance(t, str))
    assert win.tabs.get() == "📋 出勤日報" and "還沒有出勤日報" in win.bubbles["digest"].get_text()
    assert list(win.bubbles) == ["digest"]                        # 晨報不在後台看
    win.destroy()


def test_module_window_saves_both_times_and_refuses_a_bad_one_without_touching_the_other(
        root, db, settings_files, monkeypatch):
    from admin import schedule_window
    from shared import ui_theme

    calls = []
    messages = []
    monkeypatch.setattr(ui_theme, "show_message", lambda master, title, message, kind="info": messages.append(message))
    win = host_view(root, schedule_window.ScheduleView, fake_runner(root, "出勤日報", settings_files, calls),
                    fake_runner(root, "科技早報", settings_files, calls), size="900x640")
    pump(root)
    assert win.entries["morning"].get() == "08:30" and win.entries["digest"].get() == "18:00"
    win.entries["morning"].delete(0, "end")
    win.entries["morning"].insert(0, "7:5")
    win.save()
    assert messages and "科技早報" in messages[0] and "出勤日報" not in messages[0]
    assert settings_files.load_morning_time() == "08:30"
    win.entries["morning"].delete(0, "end")
    win.entries["morning"].insert(0, "07:15")
    win.entries["digest"].delete(0, "end")
    win.entries["digest"].insert(0, "17:45")
    win.save()
    assert (settings_files.load_morning_time(), settings_files.load_time()) == ("07:15", "17:45")
    assert "已儲存" in win.status.cget("text")
    win.destroy()


def test_generate_now_updates_the_module_and_an_open_viewer(root, db, settings_files, monkeypatch):
    from admin import digest_window, schedule_window
    from shared import ui_theme

    calls = []
    digest = fake_runner(root, "出勤日報", settings_files, calls)
    morning = fake_runner(root, "科技早報", settings_files, calls)
    messages = []
    monkeypatch.setattr(ui_theme, "show_message", lambda master, title, message, kind="info": messages.append(message))
    viewer = host_view(root, digest_window.ReportView, digest, morning)
    win = host_view(root, schedule_window.ScheduleView, digest, morning, size="900x640")
    win.date_entry.delete(0, "end")
    win.date_entry.insert(0, "亂打")
    win._run_digest()
    assert messages and calls == []
    win.date_entry.delete(0, "end")
    win.date_entry.insert(0, "2026-11-03")
    win._run_digest()
    assert str(win.digest_button.cget("state")) == "disabled"            # 產生中不能重複按
    win._run_morning()
    end = time.time() + 5
    while time.time() < end and not (any(n == "科技早報" for n, _ in calls) and "前台" in win.status.cget("text")):
        pump(root, 0.1)
    assert ("出勤日報", "2026-11-03") in calls and any(n == "科技早報" for n, _ in calls)
    assert "出勤日報 2026-11-03" in viewer.bubbles["digest"].get_text()
    assert "前台" in win.status.cget("text") and str(win.digest_button.cget("state")) == "normal"
    assert "測試新聞" in settings_files.load_brief()["news"][0]["title"]       # 晨報有存檔（給前台同步），後台不顯示
    win.destroy()
    viewer.destroy()


def long_digest(settings_files, links=5):
    """一份很長、內含網址的出勤日報（出勤日報明細裡可能有文件連結）。"""
    lines = [f"第 {i} 段 " + "明細。" * 30 + f" 參考 https://example.com/n/{i}" for i in range(links)]
    lines += ["補充說明。" * 20] * 24                                      # 夠長才需要捲動
    settings_files.save({"date": "2026-11-06", "generated_at": "2026-11-05 19:00", "facts": "\n".join(lines),
                         "summary": ""})


def open_viewer(root, settings_files):
    from admin import digest_window

    win = host_view(root, digest_window.ReportView, digest_window.digest_runner(root))
    pump(root, 0.6)
    return win


def test_the_wheel_over_a_message_scrolls_the_outer_area(root, db, settings_files):
    long_digest(settings_files)
    win = open_viewer(root, settings_files)
    bubble = win.bubbles["digest"]
    canvas = bubble._scroll_parent()
    assert canvas.yview() != (0.0, 1.0)                            # 內容比視窗長，需要捲動
    text = bubble.box._textbox
    for _ in range(4):
        text.event_generate("<MouseWheel>", delta=-120, x=60, y=40)
        root.update()
    assert canvas.yview()[0] > 0
    for _ in range(8):
        text.event_generate("<MouseWheel>", delta=120, x=60, y=40)
        root.update()
    assert canvas.yview()[0] == 0
    win.destroy()


def test_only_safe_https_urls_become_clickable_and_a_click_opens_them(root, db, settings_files, monkeypatch):
    from rag import chat_view

    opened = []
    monkeypatch.setattr(chat_view.webbrowser, "open", lambda url: opened.append(url))
    long_digest(settings_files, links=2)
    win = open_viewer(root, settings_files)
    bubble = win.bubbles["digest"]
    text = bubble.box._textbox
    spans = text.tag_ranges("link")
    assert [text.get(str(spans[i]), str(spans[i + 1])) for i in range(0, len(spans), 2)] == [
        "https://example.com/n/0", "https://example.com/n/1"]
    bubble.set_text("a http://insecure.example.com/x b https://127.0.0.1/y c https://ok.example.com/z.")
    bubble.linkify(lambda u: __import__("shared.link_check", fromlist=["x"]).link_problem(u) == "")
    spans = text.tag_ranges("link")
    assert [text.get(str(spans[i]), str(spans[i + 1])) for i in range(0, len(spans), 2)] == ["https://ok.example.com/z"]
    text.see(str(spans[0]))
    root.update()
    x, y, _w, _h = text.bbox(str(spans[0]))
    text.event_generate("<Motion>", x=x + 3, y=y + 3)               # 先滑過去（Tk 的標籤事件靠滑鼠位置）
    root.update()
    assert text.cget("cursor") == "hand2"
    text.event_generate("<Button-1>", x=x + 3, y=y + 3)
    root.update()
    assert opened == ["https://ok.example.com/z"]
    win.destroy()


def test_the_viewer_is_big_and_the_messages_follow_the_window_width(root, db, settings_files):
    long_digest(settings_files, links=1)
    win = open_viewer(root, settings_files)
    assert win.winfo_width() >= 900 and win.winfo_height() >= 800
    bubble = win.bubbles["digest"]
    before = int(bubble.box.cget("width"))
    win.host.geometry("1300x860")
    pump(root, 0.6)
    assert int(bubble.box.cget("width")) > before
    win.destroy()


def test_a_brief_without_news_is_retried_later_and_pops_up_only_once_it_has_news(root):
    from admin import digest_window, morning_brief

    popped, started = [], []
    now = [datetime.datetime(2026, 10, 14, 8, 40)]
    runner = digest_window.DigestRunner(root, lambda _n: "2026-10-14", lambda d, _l: None, on_new=popped.append,
                                        clock=lambda: now[0], make_ai=lambda: None, name="科技早報",
                                        retry_wait=morning_brief.retry_wait)
    runner.start = lambda day, announce=False: started.append(day) or True
    failed = {"date": "2026-10-14", "news": [], "news_problem": "x：連線失敗", "news_tries": 1}
    runner._finish(failed, None, True)
    # 連不上：先不彈窗，1 分鐘後再試
    assert popped == [] and runner._retry_at == now[0] + datetime.timedelta(minutes=1)
    runner.tick()
    assert started == []                                                                   # 還沒到重試時間
    now[0] += datetime.timedelta(minutes=2)
    runner.tick()
    assert started == ["2026-10-14"]
    good = {"date": "2026-10-14", "news": [{"title": "t"}], "news_problem": "", "news_tries": 2}
    runner._finish(good, None, True)
    assert popped == [good] and runner._retry_at is None                                   # 抓到了才彈窗，不再重試
