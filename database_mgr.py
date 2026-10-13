"""
database_mgr.py：SQLite 資料層。

主要功能：
  - 建立與升級資料表：Users（員工與人臉特徵）、Attendance（打卡）、calendar（行事曆）、llm_usage_log（RAG 稽核）
  - 員工註冊（姓名或員工編號重複即拒絕）、1:N 人臉比對
  - 打卡邏輯：依時間窗判斷上班、遲到、早退、下班、加班
  - 寫入 RAG 問答稽核紀錄

串接模組：
  - terminal_app：人臉比對、打卡、行事曆
  - web_server：員工註冊
  - backend_main、account_admin、dashboard_stats：管理後台查詢與帳號管理
  - rag_guardrail、main_guardrail_rag：寫入 llm_usage_log
"""
import sqlite3
import io
import os
import numpy as np
import datetime
import queue
import threading
import time

# --- Numpy 陣列與資料庫轉換 ---

def adapt_array(arr):
    """將 Numpy 陣列轉成二進制格式存入 SQLite"""
    out = io.BytesIO()
    np.save(out, arr)
    out.seek(0)
    return sqlite3.Binary(out.read())

def convert_array(text):
    """將資料庫內的二進制資料轉回 Numpy 陣列"""
    out = io.BytesIO(text)
    out.seek(0)
    return np.load(out)

sqlite3.register_adapter(np.ndarray, adapt_array)
sqlite3.register_converter("ARRAY", convert_array)

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_PROJECT_ROOT, "database", "database.db")

# --- 考勤時間窗 ---
# 準時上班時間窗（含頭尾）
SHIFT_ON_TIME_START = "07:40:00"
SHIFT_ON_TIME_END = "08:00:00"
# 超過準時窗但在此之前算遲到；之後第一次刷臉不計為上班
MORNING_CUTOFF = "12:00:00"
# 中午到此時間為午休，不計紀錄
AFTERNOON_RESUME = "16:30:00"
# 此時間之前算早退；之後到下班時間（含）算準時下班；更晚算加班
SHIFT_EARLY_LEAVE_END = "17:40:00"
SHIFT_END = "18:00:00"

def init_db():
    """初始化資料庫：建立員工表、考勤表、行事曆表與 RAG 稽核表"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn_db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)

    # 員工表（姓名、性別、編號、人臉特徵）
    conn_db.execute("""CREATE TABLE IF NOT EXISTS Users
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    gender TEXT,
                    employee_id TEXT,
                    embedding ARRAY NOT NULL,
                    work_start TEXT DEFAULT '09:00',
                    work_end TEXT DEFAULT '18:00')""")

    # 舊資料庫補上 role、is_active 欄位（不存在才新增）
    existing_user_columns = {row[1] for row in conn_db.execute("PRAGMA table_info(Users)").fetchall()}
    if "role" not in existing_user_columns:
        conn_db.execute("ALTER TABLE Users ADD COLUMN role TEXT DEFAULT 'employee'")
    if "is_active" not in existing_user_columns:
        conn_db.execute("ALTER TABLE Users ADD COLUMN is_active INTEGER DEFAULT 1")

    # 考勤紀錄表
    conn_db.execute("""CREATE TABLE IF NOT EXISTS Attendance
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    timestamp DATETIME NOT NULL,
                    status TEXT NOT NULL)""")

    # 行事曆表
    conn_db.execute("""CREATE TABLE IF NOT EXISTS calendar
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    emp_id TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    event_title TEXT NOT NULL)""")

    # RAG 稽核表：一筆代表一個決策事件（防護欄判斷、路由判斷、直接對話），以 event_type 區分
    conn_db.execute("""CREATE TABLE IF NOT EXISTS llm_usage_log
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    question TEXT,
                    event_type TEXT NOT NULL,
                    route TEXT,
                    guardrail_pass INTEGER,
                    similarity_score REAL,
                    is_pass INTEGER,
                    source TEXT DEFAULT 'gradio')""")

    conn_db.commit()
    conn_db.close()
    print("資料庫初始化完成!")

def insert_mock_calendar(emp_id):
    """為新員工插入一筆今天 14:00 的假行程"""
    conn_db = sqlite3.connect(DB_PATH)
    conn_db.execute("INSERT INTO calendar (emp_id, event_time, event_title) VALUES (?, ?, ?)",
                    (emp_id, "14:00", "專案會議"))
    conn_db.commit()
    conn_db.close()
    print(f"已為員工 {emp_id} 生成今日假行程")

def get_today_event(emp_id):
    """查詢該員工的行程，回傳 (event_time, event_title) 或 None"""
    conn_db = sqlite3.connect(DB_PATH)
    cursor = conn_db.cursor()
    cursor.execute("SELECT event_time, event_title FROM calendar WHERE emp_id=? LIMIT 1", (emp_id,))
    result = cursor.fetchone()
    conn_db.close()
    return result

def register_user(name, gender, emp_id, face_embedding):
    """
    註冊新員工與人臉特徵。姓名或員工編號已存在就拒絕，不覆蓋舊資料。
    回傳 (成功與否, 訊息文字)。
    """
    conn_db = sqlite3.connect(DB_PATH)
    cursor = conn_db.cursor()
    cursor.execute("SELECT name, employee_id FROM Users WHERE name=? OR employee_id=?", (name, emp_id))
    existing = cursor.fetchone()

    if existing is not None:
        conn_db.close()
        existing_name, existing_emp_id = existing
        if existing_name == name:
            reason = f"姓名「{name}」"
        else:
            reason = f"員工編號「{emp_id}」"
        message = f"註冊失敗：{reason}已經註冊過了，如需更新請聯絡管理員。"
        print(f"[Database] 拒絕重複註冊: {message}")
        return False, message

    conn_db.execute("INSERT INTO Users (name, gender, employee_id, embedding) VALUES (?, ?, ?, ?)",
                    (name, gender, emp_id, face_embedding))
    conn_db.commit()
    conn_db.close()
    print(f"註冊成功: {name} (ID: {emp_id})")
    insert_mock_calendar(emp_id)
    return True, f"註冊成功: {name} (ID: {emp_id})"

def get_employee_id(name):
    """根據員工姓名查詢對應的員工編號"""
    conn_db = sqlite3.connect(DB_PATH)
    cursor = conn_db.cursor()
    cursor.execute("SELECT employee_id FROM Users WHERE name=?", (name,))
    result = cursor.fetchone()
    conn_db.close()
    return result[0] if result else "N/A"

def is_user_registered(dna_or_name):
    """檢查使用者是否已註冊（可傳姓名或人臉特徵）"""
    if isinstance(dna_or_name, str):
        return dna_or_name not in ["Unknown", "Empty DB", "N/A"]

    name, _ = recognize_face(dna_or_name)
    return name not in ["Unknown", "Empty DB"]

def log_attendance(name):
    """考勤核心邏輯：依今天已有的紀錄與目前時間，判斷上班、遲到、早退、下班，並寫入或更新紀錄"""
    conn_db = sqlite3.connect(DB_PATH)
    cursor = conn_db.cursor()

    now = datetime.datetime.now()
    today_date = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M:%S")
    full_timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("SELECT id, status FROM Attendance WHERE name=? AND timestamp LIKE ? ORDER BY timestamp ASC",
                   (name, f"{today_date}%"))
    records = cursor.fetchall()

    if len(records) == 0:
        # 今天第一次刷臉：上班或遲到
        if SHIFT_ON_TIME_START <= current_time <= SHIFT_ON_TIME_END:
            status = "上班"
        elif SHIFT_ON_TIME_END < current_time < MORNING_CUTOFF:
            status = "遲到"
        else:
            conn_db.close()
            return "非打卡時段"
        msg = f"In: {name} ({status})"

    elif len(records) == 1:
        # 今天第二次刷臉：下班、早退或加班
        if current_time < MORNING_CUTOFF:
            conn_db.close()
            return f"Keep going: {name}!" # 早上第二次刷臉，不計為下班

        if MORNING_CUTOFF <= current_time < AFTERNOON_RESUME:
            conn_db.close()
            return f"Keep going: {name}!" # 午休時段不計紀錄

        if AFTERNOON_RESUME <= current_time < SHIFT_EARLY_LEAVE_END:
            status = "早退"
        elif SHIFT_EARLY_LEAVE_END <= current_time <= SHIFT_END:
            status = "下班"
        else:
            status = "下班 (加班)"
        msg = f"Out: {name} ({status})"

    else:
        # 已有上下班兩筆：更新最後一筆下班紀錄
        last_record_id = records[-1][0]
        if current_time < MORNING_CUTOFF:
            conn_db.close()
            return f"Keep going: {name}!" # 早上多次刷臉，不覆寫紀錄

        if AFTERNOON_RESUME <= current_time < SHIFT_EARLY_LEAVE_END:
            status = "早退"
        elif SHIFT_EARLY_LEAVE_END <= current_time <= SHIFT_END:
            status = "下班"
        elif current_time > SHIFT_END:
            status = "下班 (加班)"
        else:
            conn_db.close()
            return f"Done: {name} (All Day)"

        conn_db.execute("UPDATE Attendance SET timestamp=?, status=? WHERE id=?",
                        (full_timestamp, status, last_record_id))
        conn_db.commit()
        conn_db.close()
        print(f"系統日誌: Updated: {name} ({status}) - 時間: {full_timestamp}")
        return f"Updated: {name} ({status})"

    conn_db.execute("INSERT INTO Attendance (name, timestamp, status) VALUES (?, ?, ?)",
                    (name, full_timestamp, status))
    conn_db.commit()
    conn_db.close()
    print(f"系統日誌: {msg} - 時間: {full_timestamp}")
    return msg

def recognize_face(target_embedding, threshold=1.0):
    """
    1:N 人臉比對，回傳 (最接近的員工姓名, 距離)。
    只比對啟用中的帳號；距離大於等於門檻回傳 "Unknown"，資料庫無資料回傳 "Empty DB"。
    """
    # 逐列迭代，只保留目前最小距離，避免一次把所有特徵讀進記憶體
    best_name, best_dist = None, None
    conn_db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    try:
        cursor = conn_db.cursor()
        cursor.execute("SELECT name, embedding FROM Users WHERE is_active = 1 OR is_active IS NULL")
        for name, emb in cursor:
            dist = round(float(np.linalg.norm(emb - target_embedding)), 2)
            if best_dist is None or dist < best_dist:  # 距離相同時保留先出現者
                best_name, best_dist = name, dist
    finally:
        conn_db.close()

    if best_dist is None:
        return ("Empty DB", 99.9)

    return (best_name, best_dist) if best_dist < threshold else ("Unknown", best_dist)


def log_llm_usage(event_type, question, route=None, guardrail_pass=None, similarity_score=None, source="gradio"):
    """
    寫入一筆 RAG 問答稽核紀錄。
    event_type：guardrail（防護欄判斷）、route（law/attendance 路由判斷）、basic（Basic 模式直接問 LLM）。
    寫入失敗只印警告，不拋出例外，避免影響問答流程。
    """
    try:
        conn_db = sqlite3.connect(DB_PATH, timeout=5)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn_db.execute(
            """INSERT INTO llm_usage_log
               (timestamp, question, event_type, route, guardrail_pass, similarity_score, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                question,
                event_type,
                route,
                None if guardrail_pass is None else int(guardrail_pass),
                similarity_score,
                source,
            ),
        )
        conn_db.commit()
        conn_db.close()
    except Exception as e:
        print(f"[Database][WARN] llm_usage_log 寫入失敗（不影響本次問答結果）: {e}")
