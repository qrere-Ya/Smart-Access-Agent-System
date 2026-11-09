"""
morning_brief.py：每天早上的晨報（科技早報）的產生與排程判斷（純邏輯，不碰畫面元件）。

主要功能：
  - build_brief：只有科技新聞（news_feed：科技媒體、AI 官方部落格、Hacker News，每則有類別、發生什麼事、
    為什麼重要、可以怎麼用、來源與原文）；早報文字是來源原文加固定句子，不交給語言模型。
    出勤資料（昨日出勤、今天請假）不在晨報裡，只在後台的日報
  - generate：產生並存檔今天的晨報；新聞抓不到（例如沒有網路）時照樣存檔並記下失敗原因與已試次數，之後會自動重抓
  - pending_morning：排程判斷——到了設定時間（預設 08:30）且今天要上班、今天還沒有晨報就產生；
    後台比設定時間晚開（例如開機時已經 10:00）也會在開啟後補產今天的晨報，前幾天沒產生的不補；
    今天的晨報若沒有新聞（連線失敗）且試的次數還沒到上限，也會再回傳今天，讓排程重抓
  - retry_wait：新聞抓不到時下一次重抓前要等多久（1、2、5 分鐘，之後每 10 分鐘），試滿上限就不再等

串接模組：
  - news_feed：新聞；database_mgr：只用來判斷今天是不是上班日；shared.digest_store：存檔與設定
  - admin.digest_window：背景執行、重抓排程與顯示；backend_main：每分鐘判斷
"""
import datetime

from admin import news_feed
from shared import database_mgr
from shared import network_policy
from shared import digest_store

MAX_NEWS_TRIES = 30                                       # 約 5 小時內持續重抓
RETRY_MINUTES = (1, 2, 5)                                 # 前幾次的間隔，之後固定 10 分鐘
RETRY_MINUTES_AFTER = 10


def _default_workday(d):
    return database_mgr.day_off_reason(d.isoformat()) is None


def build_brief(today, fetch=news_feed.fetch_ai_news, now=None, tries=1):
    """組出今天的晨報內容（dict）；新聞失敗不丟例外，只記原因。tries 是這是第幾次抓。"""
    cards, news_problem = fetch()
    return {
        "date": today.isoformat(),
        "generated_at": (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M"),
        "news": [{k: v for k, v in c.items() if k != "time"} for c in cards],   # 時間不是 JSON 型別，不存檔
        "news_problem": news_problem,
        "news_tries": tries,
    }


def generate(day, now=None, save=digest_store.save_brief, load=digest_store.load_brief,
             fetch=news_feed.fetch_ai_news):
    """產生並存檔 day（YYYY-MM-DD）的晨報；回傳晨報內容。同一天重抓時累計次數。"""
    previous = load()
    tries = (previous.get("news_tries", 1) + 1) if previous and previous.get("date") == day else 1
    brief = build_brief(datetime.date.fromisoformat(day), fetch=fetch, now=now, tries=tries)
    if not brief["news"] and previous and previous.get("date") == day and previous.get("news"):
        brief["news"] = previous["news"]               # 重抓失敗不能把已經有的新聞洗掉
    save(brief)
    return brief


def wants_news_retry(brief) -> bool:
    """這份晨報是不是因為連不上而沒有新聞、而且還該再試（全本地模式是刻意不連網，不重試）。"""
    return bool(brief) and not network_policy.local_only() and not brief.get("news") \
        and bool(brief.get("news_problem")) and brief.get("news_tries", 1) < MAX_NEWS_TRIES


def retry_wait(brief):
    """還要重抓時回傳要等的時間（timedelta），不用再試回傳 None。"""
    if not wants_news_retry(brief):
        return None
    tries = brief.get("news_tries", 1)
    minutes = RETRY_MINUTES[tries - 1] if tries - 1 < len(RETRY_MINUTES) else RETRY_MINUTES_AFTER
    return datetime.timedelta(minutes=minutes)


def pending_morning(now, hhmm, latest, is_workday=None):
    """現在該不該產生（或重抓）今天的晨報：該產生回傳今天的日期字串，否則回傳 None。
    latest 是最新一份晨報（dict）或 None。"""
    is_workday = is_workday or _default_workday
    hour, minute = map(int, hhmm.split(":"))
    if (now.hour, now.minute) < (hour, minute) or not is_workday(now.date()):
        return None
    today = now.date().isoformat()
    latest_date = latest["date"] if latest else None
    if latest_date != today:
        return today
    return today if wants_news_retry(latest) else None


def brief_text(brief) -> str:
    """視窗顯示的晨報文字。"""
    if brief is None:
        return "還沒有晨報。到了設定的時間會自動產生，也可以按「立即產生晨報」。"
    parts = [f"🌅 晨報 {brief['date']}（產生於 {brief['generated_at']}）", ""]
    if brief.get("news"):
        parts.append(news_feed.format_news(brief["news"], datetime.date.fromisoformat(brief["date"])))
        parts.append("\n（類別與說明句由固定規則產生，內容是來源原文，未經 AI 改寫）")
        return "\n".join(parts)
    problem = brief.get("news_problem")
    if network_policy.local_only():
        parts.append("全本地模式已開啟，晨報不連網抓新聞。要看新聞，先在「日報與晨報」視窗關閉全本地模式，"
                     "再按「立即產生晨報」。")
    elif problem and wants_news_retry(brief):
        wait = retry_wait(brief)
        parts.append(f"目前沒有取得新聞（可能沒有網路或來源暫時連不上），已試 {brief.get('news_tries', 1)} 次，"
                     f"約 {int(wait.total_seconds() // 60)} 分鐘後會自動重試。")
        parts.append(f"原因：{problem}")
    elif problem:
        parts.append(f"今天沒有取得新聞，已試 {brief.get('news_tries', 1)} 次，不再自動重試；"
                     "可以按「立即產生晨報」再抓。")
        parts.append(f"原因：{problem}")
    else:
        parts.append("今天沒有找到新的科技新聞。")
    return "\n".join(parts)
