"""
chat_log.py：對話紀錄（後台出缺勤查詢與前台知識庫問答）的資料層——寫入、查詢、審查標記、到期清理、匯出（純邏輯）。

主要功能：
  - record：寫入一筆（時間、來源、問題、AI 回答、引用段落）；問題、回答、引用都有長度上限，超過截斷並註明；
    來源只有 back_attendance（後台出缺勤）與 front_knowledge（前台知識庫）；不記錄 IP 與通行碼
  - try_record：給問答流程用——寫入失敗（資料庫被鎖、磁碟問題）只印例外型別，絕不讓問答因此失敗
  - list_logs／get_log：依來源、日期、審查結果篩選，新的在前；列表只回摘要，詳情才有全文與引用
  - set_review：標記 正確／錯誤／幻覺（或清除回未審查），記下審查時間與審查者
  - review_summary：「已審查 N 筆：正確 a、錯誤 b、幻覺 c；未審查 M 筆」。**不算準確率**：審查是人工、
    只有被看過的紀錄才有標記、判斷主觀、樣本有偏差，不能當準確率；要量準確率用固定題庫評估（retrieval_eval、tool_eval）
  - purge_expired：刪除超過保存期限（30 天）的紀錄；標為錯誤或幻覺但還沒匯出的不會被特別留下，所以刪除前要先匯出
    （purge 回傳會被刪的筆數，呼叫端可先問 count_expiring 提示）
  - export_csv：匯出 CSV（utf-8-sig 讓 Excel 開得起來），防 CSV 公式注入；內含個資（員工姓名、前台使用者的提問）
  後台聊天視窗是另一個行程，所以每次操作都開自己的連線，逾時 10 秒；寫入用立即寫入交易。

串接模組：
  - shared.database_mgr：DB_PATH 與 ChatLogs 資料表
  - rag.chat_logging：把問答流程（Assistant）的結果整理成引用段落並呼叫 try_record
  - admin.chat_log_panel：報告頁的「對話紀錄」分頁（瀏覽、篩選、審查、匯出）
"""
import csv
import datetime
import json
import sqlite3
from contextlib import closing

from shared import database_mgr

SOURCE_BACK = "back_attendance"
SOURCE_FRONT = "front_knowledge"
SOURCES = (SOURCE_BACK, SOURCE_FRONT)
SOURCE_LABELS = {SOURCE_BACK: "後台出缺勤", SOURCE_FRONT: "前台知識庫"}

REVIEW_NONE, REVIEW_CORRECT, REVIEW_WRONG, REVIEW_HALLUCINATION = "unreviewed", "correct", "wrong", "hallucination"
REVIEWS = (REVIEW_NONE, REVIEW_CORRECT, REVIEW_WRONG, REVIEW_HALLUCINATION)
REVIEW_LABELS = {REVIEW_NONE: "未審查", REVIEW_CORRECT: "正確", REVIEW_WRONG: "錯誤", REVIEW_HALLUCINATION: "幻覺"}

RETENTION_DAYS = 30
MAX_QUESTION_CHARS = 1000
MAX_ANSWER_CHARS = 8000
MAX_CITATIONS = 10
MAX_CITATION_TEXT = 3000          # 查詢結果表要能完整對照，所以比單純的文字段落寬鬆
MAX_REVIEWER_CHARS = 40
TRUNCATED = "…（已截斷）"
LIST_LIMIT = 500            # 畫面列表一次最多列幾筆
MAX_LIMIT = 20000            # 匯出的上限
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
CSV_HEADER = ["時間", "來源", "問題", "AI 回答", "引用段落", "審查結果", "審查時間", "審查者"]


def _connect():
    return sqlite3.connect(database_mgr.DB_PATH, timeout=10)


def _clip(text, limit):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + TRUNCATED


def _clean_citations(citations):
    """引用段落整理成 [{"title","label","score","text"}…]，每個欄位限長，筆數有上限；不合格的項目略過。"""
    out = []
    for item in list(citations or [])[:MAX_CITATIONS]:
        if not isinstance(item, dict):
            continue
        score = item.get("score")
        out.append({
            "title": _clip(item.get("title", ""), 200),
            "label": _clip(item.get("label", ""), 200),
            "score": round(float(score), 4) if isinstance(score, (int, float)) and score is not True
            and score is not False else None,
            "text": _clip(item.get("text", ""), MAX_CITATION_TEXT),
        })
    return out


def record(source, question, answer, citations=None, now=None):
    """寫入一筆對話紀錄，回傳編號；來源不合格丟 ValueError。"""
    if source not in SOURCES:
        raise ValueError(f"來源必須是 {SOURCES} 其中之一")
    stamp = (now or datetime.datetime.now()).strftime(_TS_FORMAT)
    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO ChatLogs (ts, source, question, answer, citations) VALUES (?, ?, ?, ?, ?)",
            (stamp, source, _clip(question, MAX_QUESTION_CHARS), _clip(answer, MAX_ANSWER_CHARS),
             json.dumps(_clean_citations(citations), ensure_ascii=False)))
        conn.commit()
        return cur.lastrowid


def try_record(source, question, answer, citations=None, now=None):
    """同 record，但任何失敗只印例外型別、回傳 None；問答流程用這個，紀錄寫不進去不能讓問答失敗。"""
    try:
        return record(source, question, answer, citations, now)
    except Exception as e:  # noqa: BLE001
        print(f"[對話紀錄] 寫入失敗（略過）：{type(e).__name__}")
        return None


def _row(r, full):
    out = {"id": r[0], "ts": r[1], "source": r[2], "review": r[6], "reviewed_at": r[7], "reviewer": r[8]}
    if full:
        try:
            citations = json.loads(r[5])
        except ValueError:
            citations = []
        out.update(question=r[3], answer=r[4], citations=citations if isinstance(citations, list) else [])
    else:
        out.update(question=_clip(" ".join(str(r[3]).split()), 60))
    return out


_COLUMNS = "id, ts, source, question, answer, citations, review, reviewed_at, reviewer"


def list_logs(source=None, review=None, start=None, end=None, limit=LIST_LIMIT):
    """列表（新的在前，只有摘要）；start、end 是 YYYY-MM-DD，含頭尾整天。條件不合格丟 ValueError。"""
    clauses, params = [], []
    if source:
        if source not in SOURCES:
            raise ValueError("來源不合格")
        clauses.append("source = ?")
        params.append(source)
    if review:
        if review not in REVIEWS:
            raise ValueError("審查結果不合格")
        clauses.append("review = ?")
        params.append(review)
    if start:
        clauses.append("ts >= ?")
        params.append(_day(start) + " 00:00:00")
    if end:
        clauses.append("ts <= ?")
        params.append(_day(end) + " 23:59:59")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = max(1, min(int(limit), MAX_LIMIT))
    with closing(_connect()) as conn:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM ChatLogs{where} ORDER BY ts DESC, id DESC LIMIT ?",  # noqa: S608
                            (*params, limit)).fetchall()
    return [_row(r, full=False) for r in rows]


def _day(text):
    try:
        return datetime.datetime.strptime(str(text).strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError("日期格式需為 YYYY-MM-DD") from None


def get_log(log_id):
    """一筆的全文與引用；找不到回傳 None。"""
    with closing(_connect()) as conn:
        r = conn.execute(f"SELECT {_COLUMNS} FROM ChatLogs WHERE id = ?", (int(log_id),)).fetchone()  # noqa: S608
    return None if r is None else _row(r, full=True)


def set_review(log_id, review, reviewer="", now=None):
    """標記審查結果（正確、錯誤、幻覺，或 unreviewed 清除標記）；回傳是否真的更新到。"""
    if review not in REVIEWS:
        raise ValueError("審查結果不合格")
    stamp = None if review == REVIEW_NONE else (now or datetime.datetime.now()).strftime(_TS_FORMAT)
    who = None if review == REVIEW_NONE else _clip(" ".join(str(reviewer or "").split()), MAX_REVIEWER_CHARS) or None
    with closing(_connect()) as conn:
        n = conn.execute("UPDATE ChatLogs SET review = ?, reviewed_at = ?, reviewer = ? WHERE id = ?",
                         (review, stamp, who, int(log_id))).rowcount
        conn.commit()
    return n > 0


def review_counts(source=None, start=None, end=None):
    """{"total", "unreviewed", "correct", "wrong", "hallucination"}（同一組篩選條件）。"""
    clauses, params = [], []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if start:
        clauses.append("ts >= ?")
        params.append(_day(start) + " 00:00:00")
    if end:
        clauses.append("ts <= ?")
        params.append(_day(end) + " 23:59:59")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with closing(_connect()) as conn:
        rows = conn.execute(f"SELECT review, COUNT(*) FROM ChatLogs{where} GROUP BY review", params).fetchall()  # noqa: S608
    counts = dict.fromkeys(REVIEWS, 0) | {r: n for r, n in rows if r in REVIEWS}
    counts["total"] = sum(counts[r] for r in REVIEWS)
    return counts


def review_summary(counts):
    """畫面用的一行說明；只有各標記的筆數，不寫準確率。"""
    reviewed = counts["correct"] + counts["wrong"] + counts["hallucination"]
    return (f"已審查 {reviewed} 筆：正確 {counts['correct']}、錯誤 {counts['wrong']}、幻覺 {counts['hallucination']}；"
            f"未審查 {counts['unreviewed']} 筆")


def _cutoff(now, days):
    return ((now or datetime.datetime.now()) - datetime.timedelta(days=days)).strftime(_TS_FORMAT)


def count_expiring(days=RETENTION_DAYS, now=None):
    """超過保存期限、下一次清理會被刪掉的筆數。"""
    with closing(_connect()) as conn:
        return conn.execute("SELECT COUNT(*) FROM ChatLogs WHERE ts < ?", (_cutoff(now, days),)).fetchone()[0]


def purge_expired(days=RETENTION_DAYS, now=None):
    """刪除超過保存期限的紀錄，回傳刪除筆數。"""
    with closing(_connect()) as conn:
        n = conn.execute("DELETE FROM ChatLogs WHERE ts < ?", (_cutoff(now, days),)).rowcount
        conn.commit()
    return n


def sanitize_cell(value):
    """防 CSV 公式注入：以 = + - @（或 tab、換行）開頭的文字前面加單引號。"""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def _citation_text(citations):
    lines = []
    for i, c in enumerate(citations, start=1):
        score = "" if c.get("score") is None else f"（分數 {c['score']}）"
        lines.append(f"[{i}] {c.get('title') or c.get('label') or ''}{score}　{c.get('text', '')}")
    return "\n".join(lines)


def export_csv(path, source=None, review=None, start=None, end=None):
    """依篩選條件匯出 CSV（含全文與引用），回傳匯出筆數；內含個資，呼叫端要先提示。"""
    ids = [r["id"] for r in list_logs(source, review, start, end, limit=MAX_LIMIT)]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        for log_id in ids:
            r = get_log(log_id)
            writer.writerow([sanitize_cell(x) for x in (
                r["ts"], SOURCE_LABELS.get(r["source"], r["source"]), r["question"], r["answer"],
                _citation_text(r["citations"]), REVIEW_LABELS.get(r["review"], r["review"]),
                r["reviewed_at"] or "", r["reviewer"] or "")])
    return len(ids)
