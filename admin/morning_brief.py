"""
morning_brief.py：每天早上的晨報（昨日出勤＋今天提醒＋新聞標題）的產生與排程判斷（純邏輯，不碰畫面元件）。

主要功能：
  - build_brief：昨日（前一個上班日）的出勤明細與語言模型摘要（沿用 daily_digest 的核對）、
    今天的提醒（今天是不是休息日、誰請假）、新聞標題（news_feed）；新聞只原文顯示，不交給語言模型
  - generate：產生並存檔今天的晨報；新聞抓不到只在晨報裡寫原因，出勤部分照常
  - pending_morning：排程判斷——到了設定時間（預設 08:30）且今天要上班、今天還沒有晨報就產生；
    後台比設定時間晚開（例如開機時已經 10:00）也會在開啟後補產今天的晨報，前幾天沒產生的不補

串接模組：
  - daily_digest：出勤明細、摘要核對、休息日判斷；news_feed：新聞；shared.digest_store：存檔與設定
  - admin.digest_window：背景執行與顯示；backend_main：每分鐘判斷
"""
import datetime

from admin import attendance_report as report_mod
from admin import daily_digest
from admin import news_feed
from shared import database_mgr
from shared import digest_store

SEARCH_BACK_DAYS = daily_digest.SEARCH_BACK_DAYS


def previous_workday(day, is_workday):
    """day 之前最近的上班日（不含 day）；往前找不到回傳 None。"""
    for back in range(1, SEARCH_BACK_DAYS + 1):
        candidate = day - datetime.timedelta(days=back)
        if is_workday(candidate):
            return candidate
    return None


def _default_workday(d):
    return database_mgr.day_off_reason(d.isoformat()) is None


def today_lines(report) -> str:
    """今天的提醒：誰請假（只寫假別）、已知的缺勤不在早上列（還沒到上班時間）。"""
    on_leave = [daily_digest.who(r) for r in report["rows"] if r["status"] == report_mod.STATUS_LEAVE]
    if on_leave:
        return "今天請假：" + "、".join(on_leave[:daily_digest.MAX_NAMES])
    return "今天沒有人請假。"


def build_brief(today, llm=None, feeds=None, is_workday=None, fetch=news_feed.fetch_headlines, now=None):
    """組出今天的晨報內容（dict）；昨日出勤查詢失敗會丟例外，新聞失敗不會。"""
    is_workday = is_workday or _default_workday
    prev = previous_workday(today, is_workday)
    yesterday, summary = "", ""
    if prev is not None:
        report = report_mod.build_report(prev.isoformat())
        yesterday = daily_digest.build_facts(report)
        summary = daily_digest.narrate(yesterday, [r["name"] for r in report["rows"]], llm)
    today_report = report_mod.build_report(today.isoformat())
    headlines, news_problem = fetch(feeds)
    return {
        "date": today.isoformat(),
        "generated_at": (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M"),
        "yesterday": yesterday or "找不到前一個上班日，沒有昨日出勤。",
        "summary": summary,
        "today": today_lines(today_report),
        "news": [h["title"] for h in headlines],
        "news_problem": news_problem,
        "ai": bool(summary),
    }


def generate(day, llm=None, now=None, save=digest_store.save_brief):
    """產生並存檔 day（YYYY-MM-DD）的晨報；回傳晨報內容。"""
    brief = build_brief(datetime.date.fromisoformat(day), llm, digest_store.load_news_feeds(), now=now)
    save(brief)
    return brief


def pending_morning(now, hhmm, latest_date, is_workday=None):
    """現在該不該產生今天的晨報：該產生回傳今天的日期字串，否則回傳 None。"""
    is_workday = is_workday or _default_workday
    hour, minute = map(int, hhmm.split(":"))
    if (now.hour, now.minute) < (hour, minute) or not is_workday(now.date()):
        return None
    today = now.date().isoformat()
    return today if latest_date != today else None


def brief_text(brief) -> str:
    """視窗與歡迎畫面顯示的晨報文字。"""
    if brief is None:
        return "還沒有晨報。到了設定的時間會自動產生，也可以按「立即產生晨報」。"
    parts = [f"🌅 晨報 {brief['date']}（產生於 {brief['generated_at']}）", ""]
    parts.append("【昨日出勤】")
    if brief.get("summary"):
        parts.append("AI 摘要（數字與人名已核對）：" + brief["summary"])
    parts.append(brief["yesterday"])
    parts += ["", "【今天提醒】", brief.get("today", "")]
    parts += ["", "【新聞標題】（來源原文，未經 AI 改寫）"]
    if brief.get("news"):
        parts += [f"{i}. {title}" for i, title in enumerate(brief["news"], 1)]
    else:
        parts.append("今天沒有抓到新聞" + (f"（{brief['news_problem']}）" if brief.get("news_problem") else "") + "。")
    return "\n".join(parts)
