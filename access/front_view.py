"""
front_view.py：前台儀表板（第二個螢幕）要顯示的內容整理——純邏輯，不碰 tkinter，方便測試。

主要功能：
  - snapshot：把前台本機的資料（晨報、最近打卡問候、警示、後台連線狀態、辨識心跳、待補送數）整理成一份畫面用的 dict
  - 晨報：每頁 NEWS_PER_PAGE 則，每 PAGE_SECONDS 秒自動換頁；沒有晨報、新聞抓不到、晨報不是今天的都有白話說明
  - 打卡問候：只顯示最近 GREETING_MAX_AGE 秒內、最多 GREETING_ROWS 筆；過期就消失（螢幕對外，不長期留姓名）
  - 警示：尾隨、經過未打卡、非活體三種，顯示 ALERT_SECONDS 秒後自動消失
  - 狀態：後台連線（前台離線也照樣辨識，所以離線是黃色提醒不是錯誤）、鏡頭、辨識、待補送事件
  - 文字長度在這裡截斷，避免外部新聞的超長標題把版面撐壞

串接模組：
  - access.front_store（ScreenState、FrontStore）：資料來源；access.front_agent：辨識心跳讀取
  - access.front_dashboard：畫面；晨報內容來自後台（admin.morning_brief）經前台代理同步
"""
NEWS_PER_PAGE = 3
PAGE_SECONDS = 20
GREETING_ROWS = 5
GREETING_MAX_AGE = 600.0
ALERT_SECONDS = 60.0
MAX_TITLE, MAX_WHAT, MAX_NOTE = 60, 110, 80
WEEKDAYS = "一二三四五六日"
ALERT_TEXT = {"tailgate": "偵測到尾隨", "no_scan_walkby": "有人經過鏡頭但沒有打卡",
              "spoof": "偵測到非真人（疑似照片或螢幕翻拍）"}


def clock_texts(now):
    """回傳 (時間 HH:MM, 日期文字)。"""
    return now.strftime("%H:%M"), f"{now.year} 年 {now.month} 月 {now.day} 日　星期{WEEKDAYS[now.weekday()]}"


def _cut(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def alert_view(alert):
    """警示 dict（kind、detail、ts）→ 畫面用的 {kind, title, detail}；沒有警示回傳 None。"""
    if not alert:
        return None
    kind = alert.get("kind", "")
    detail = _cut(alert.get("detail", ""), 80)
    title = ALERT_TEXT.get(kind, "偵測到異常")
    return {"kind": kind, "title": title, "detail": "" if detail == title else detail}


def greeting_rows(punches):
    """打卡問候列表 → [(時間, 姓名, 問候)]，最多 GREETING_ROWS 筆，新的在前。"""
    return [(p.get("clock", ""), _cut(p.get("name", ""), 20), p.get("greeting", ""))
            for p in punches[:GREETING_ROWS]]


def news_view(brief, today, now_ts, per_page=NEWS_PER_PAGE, page_seconds=PAGE_SECONDS):
    """晨報 → 畫面用的 dict：state（ok／empty／problem）、標題文字、目前這一頁的新聞卡片與頁碼。"""
    if not isinstance(brief, dict):
        return {"state": "empty", "heading": "今日科技早報", "message": "還沒有晨報：後台尚未產生，或前台還沒同步到。",
                "cards": [], "page": 0, "pages": 0}
    day = str(brief.get("date", ""))
    heading = "今日科技早報" if day == today.isoformat() else f"科技早報（{day}，今天的還沒產生）"
    cards = [c for c in (brief.get("news") or []) if isinstance(c, dict) and c.get("title")]
    if not cards:
        problem = _cut(brief.get("news_problem", ""), 100)
        message = f"新聞還沒抓到：{problem}（後台會自動重抓）" if problem else "這一天沒有新聞。"
        return {"state": "problem", "heading": heading, "message": message, "cards": [], "page": 0, "pages": 0}
    pages = [cards[i:i + per_page] for i in range(0, len(cards), per_page)]
    index = int(now_ts // page_seconds) % len(pages)
    shown = [{"category": _cut(c.get("category", ""), 12), "title": _cut(c["title"], MAX_TITLE),
              "what": _cut(c.get("what", ""), MAX_WHAT), "why": _cut(c.get("why", ""), MAX_NOTE),
              "how": _cut(c.get("how", ""), MAX_NOTE), "source": _cut(c.get("source", ""), 30)}
             for c in pages[index]]
    return {"state": "ok", "heading": heading, "message": "", "cards": shown, "page": index + 1, "pages": len(pages)}


def link_view(link_online, pending):
    """後台連線 → (色調, 文字)。前台離線時照樣辨識與暫存事件，所以是提醒（warning）不是錯誤。"""
    if link_online:
        return "success", "後台連線中" + (f"（待補送 {pending} 筆）" if pending else "")
    return "warning", "後台離線，打卡先暫存" + (f"（已暫存 {pending} 筆）" if pending else "")


def status_rows(link_online, pending, faces, heartbeat):
    """右下角的狀態列表 → [(名稱, 內容, 色調)]。"""
    link_tone, link_text = link_view(link_online, pending)
    camera, running = heartbeat.get("camera_ok"), heartbeat.get("recognition_running")
    return [
        ("後台連線", link_text, link_tone),
        ("鏡頭", "正常" if camera else "異常或未啟動", "success" if camera else "danger"),
        ("人臉辨識", "執行中" if running else "未執行", "success" if running else "danger"),
        ("人臉資料", f"{faces} 筆", "success" if faces else "warning"),
    ]


TILE_SUMMARY_MAX = 26
_TONE_RANK = {"success": 0, "warning": 1, "danger": 2}


def module_tiles(news, greetings, status):
    """主畫面的功能模組卡片 → [{key, icon, title, desc, accent, summary}]；summary 是即時預覽。"""
    if news["state"] == "ok":
        news_summary = _cut(news["cards"][0]["title"], TILE_SUMMARY_MAX)
    else:
        news_summary = _cut(news["message"], TILE_SUMMARY_MAX)
    if greetings:
        _clock, name, greeting = greetings[0]
        greet_summary = f"{name} {greeting}"
    else:
        greet_summary = "目前沒有人打卡"
    worst = max(status, key=lambda row: _TONE_RANK.get(row[2], 0))
    healthy = _TONE_RANK.get(worst[2], 0) == 0
    status_summary = "全部正常" if healthy else f"{worst[0]}：{worst[1]}"
    status_accent = worst[2] if worst[2] in _TONE_RANK else "success"
    return [
        {"key": "news", "icon": "📰", "title": "今日科技早報", "desc": "後台每天整理的科技新聞，自動換頁",
         "accent": "info", "summary": news_summary},
        {"key": "greetings", "icon": "👋", "title": "剛剛打卡", "desc": "最近 10 分鐘辨識成功的人與問候",
         "accent": "success", "summary": greet_summary},
        {"key": "status", "icon": "📡", "title": "系統狀態", "desc": "後台連線、鏡頭、人臉辨識與人臉資料",
         "accent": status_accent, "summary": _cut(status_summary, TILE_SUMMARY_MAX)},
    ]


def load_tone(percent):
    """資源使用率 → 色調：70% 以上黃、90% 以上紅。"""
    return "danger" if percent >= 90 else "warning" if percent >= 70 else "success"


def speed_text(arrow, kbps):
    """網路速度文字：讀不到顯示「—」，超過 1024 KB/s 換成 MB/s。"""
    if kbps is None:
        return f"{arrow} —"
    return f"{arrow} {kbps / 1024:.1f} MB/s" if kbps >= 1024 else f"{arrow} {kbps:.1f} KB/s"


def snapshot(store, screen, read_heartbeat, now, now_ts, pending=None):
    """整理一份畫面內容。store 是 FrontStore，screen 是 ScreenState，read_heartbeat() 回傳辨識心跳。"""
    time_text, date_text = clock_texts(now)
    pending = store.pending_count() if pending is None else pending
    link_online = store.link_online(now=lambda: now_ts)
    faces = len(store.load_faces())
    greetings = greeting_rows(screen.recent_punches(GREETING_MAX_AGE, now=lambda: now_ts))
    news = news_view(store.get_meta("morning"), now.date(), now_ts)
    status = status_rows(link_online, pending, faces, read_heartbeat())
    return {
        "time": time_text, "date": date_text,
        "link": link_view(link_online, pending),
        "alert": alert_view(screen.current_alert(ALERT_SECONDS, now=lambda: now_ts)),
        "greetings": greetings, "news": news, "status": status,
        "tiles": module_tiles(news, greetings, status),
        "theme": screen.theme(),
    }

