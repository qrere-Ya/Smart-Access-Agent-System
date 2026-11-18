"""
database_mgr.py：SQLite 資料層。

主要功能：
  - 建立與升級資料表：Users（員工與人臉特徵）、Attendance（打卡，含 user_id 員工唯一識別）、calendar（行事曆）、
    AnomalyEvents（異常事件，如尾隨；只存照片路徑，不存照片）；Holidays、Leaves、Settings 資料表保留（舊資料不刪），
    但程式不再讀寫：假日與請假功能已移除，休息日改由 work_calendar 自動判斷
  - 員工註冊（姓名或員工編號重複即拒絕）、1:N 人臉比對
  - 打卡邏輯：依時間窗判斷上班、遲到、早退、下班、加班
  - 異常事件的寫入（連續同類型事件於時間窗內合併成一筆，保留次數與起訖時間）、查詢，以及把舊的證據照片一次匯入事件表

串接模組：
  - terminal_app：人臉比對、打卡、行事曆、記錄異常事件
  - web_server：員工註冊
  - backend_main、account_admin、dashboard_stats：管理後台查詢與帳號管理
  - rag.queries：唯讀查詢考勤與異常事件
"""
import sqlite3
import io
from contextlib import closing
import os
import re
import numpy as np
import datetime

from shared import paths

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

DB_PATH = paths.DB_PATH

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

# 同類型異常事件與前一筆相隔不超過此秒數，就合併成一筆
ANOMALY_MERGE_SECONDS = 30

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
    # 舊資料庫補上 user_id（員工唯一識別，舊紀錄可為 NULL）
    existing_att_columns = {row[1] for row in conn_db.execute("PRAGMA table_info(Attendance)").fetchall()}
    if "user_id" not in existing_att_columns:
        conn_db.execute("ALTER TABLE Attendance ADD COLUMN user_id INTEGER")
    # 以姓名回填舊紀錄；姓名對到多位員工（或沒有）就保持 NULL，不猜測
    conn_db.execute("""UPDATE Attendance SET user_id =
                    (SELECT u.id FROM Users u WHERE u.name = Attendance.name)
                    WHERE user_id IS NULL
                    AND (SELECT COUNT(*) FROM Users u WHERE u.name = Attendance.name) = 1""")
    conn_db.execute("CREATE INDEX IF NOT EXISTS idx_attendance_user ON Attendance(user_id)")

    # 假日表與請假表（已不使用，資料表保留不刪舊資料）
    conn_db.execute("""CREATE TABLE IF NOT EXISTS Holidays
                    (date TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '')""")
    conn_db.execute("""CREATE TABLE IF NOT EXISTS Leaves
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '')""")
    conn_db.execute("CREATE INDEX IF NOT EXISTS idx_leaves_user ON Leaves(user_id, start_date, end_date)")

    # 系統設定（鍵值）：已不使用，資料表保留
    conn_db.execute("""CREATE TABLE IF NOT EXISTS Settings
                    (key TEXT PRIMARY KEY,
                    value TEXT NOT NULL)""")

    # 行事曆表
    conn_db.execute("""CREATE TABLE IF NOT EXISTS calendar
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    emp_id TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    event_title TEXT NOT NULL)""")

    # 對話紀錄表（後台出缺勤與前台知識庫的問答，保存 1 個月，含人工審查欄位；見 shared/chat_log）
    conn_db.execute("""CREATE TABLE IF NOT EXISTS ChatLogs
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    source TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    citations TEXT NOT NULL DEFAULT '[]',
                    review TEXT NOT NULL DEFAULT 'unreviewed',
                    reviewed_at TEXT,
                    reviewer TEXT)""")
    conn_db.execute("CREATE INDEX IF NOT EXISTS idx_chatlogs_ts ON ChatLogs(ts)")

    # 異常事件表（尾隨等）：只存證據照片的路徑
    anomaly_table_existed = conn_db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='AnomalyEvents'").fetchone() is not None
    conn_db.execute("""CREATE TABLE IF NOT EXISTS AnomalyEvents
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    event_type TEXT NOT NULL,
                    detail TEXT,
                    evidence_path TEXT)""")
    # 舊資料庫補上合併用欄位：end_timestamp（最後一次發生）、event_count（合併次數）
    existing_anomaly_columns = {row[1] for row in conn_db.execute("PRAGMA table_info(AnomalyEvents)").fetchall()}
    if "end_timestamp" not in existing_anomaly_columns:
        conn_db.execute("ALTER TABLE AnomalyEvents ADD COLUMN end_timestamp DATETIME")
    if "event_count" not in existing_anomaly_columns:
        conn_db.execute("ALTER TABLE AnomalyEvents ADD COLUMN event_count INTEGER DEFAULT 1")
    conn_db.execute("CREATE INDEX IF NOT EXISTS idx_anomaly_timestamp ON AnomalyEvents(timestamp)")

    # 前台送來的事件編號（補送時不會重複寫入）
    conn_db.execute("""CREATE TABLE IF NOT EXISTS LinkEvents
                    (event_id TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL)""")

    conn_db.commit()
    conn_db.close()
    # 第一次建立事件表時，把資料夾裡已有的舊證據照片匯入
    if not anomaly_table_existed:
        import_anomaly_photos()
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
    # 查重與寫入放在同一個「立即寫入」交易裡：兩個請求同時進來時，後面的會等前面的寫完，再查到已存在
    # （原本先查、後寫之間沒有鎖，同時送出會讓同一個人被註冊好幾次）
    conn_db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn_db.execute("BEGIN IMMEDIATE")
    cursor = conn_db.cursor()
    cursor.execute("SELECT name, employee_id FROM Users WHERE name=? OR employee_id=?", (name, emp_id))
    existing = cursor.fetchone()

    if existing is not None:
        conn_db.execute("ROLLBACK")
        conn_db.close()
        existing_name, existing_emp_id = existing
        if existing_name == name:
            reason = f"姓名「{name}」"
        else:
            reason = f"員工編號「{emp_id}」"
        message = f"註冊失敗：{reason}已經註冊過了，如需更新請聯絡管理員。"
        print(f"[Database] 拒絕重複註冊: {message}")
        return False, message

    try:
        conn_db.execute("INSERT INTO Users (name, gender, employee_id, embedding) VALUES (?, ?, ?, ?)",
                        (name, gender, emp_id, face_embedding))
        conn_db.execute("COMMIT")
    except Exception:
        conn_db.execute("ROLLBACK")
        raise
    finally:
        conn_db.close()
    print(f"註冊成功: {name} (ID: {emp_id})")
    insert_mock_calendar(emp_id)
    return True, f"註冊成功: {name} (ID: {emp_id})"

def registered_name_for_face(face_embedding, threshold=0.9):
    """這張臉已經註冊在哪個姓名下（啟用中的帳號、距離小於門檻）；沒有就回傳 None。註冊時用來擋同一個人的第二筆。"""
    name, _dist = recognize_face(face_embedding, threshold)
    return None if name in ("Unknown", "Empty DB") else name


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

def punch_window_open(current_time):
    """這個時間（HH:MM:SS）有沒有可能寫入打卡：準時窗開始到中午前，或午休結束之後。
    只看時間；今天已經打過幾次要查資料庫，前台不知道，所以前台只用它擋掉一定不會記的時段。"""
    return SHIFT_ON_TIME_START <= current_time < MORNING_CUTOFF or current_time >= AFTERNOON_RESUME


def log_attendance(name, user_id=None, now=None):
    """考勤核心邏輯：依今天已有的紀錄與目前時間，判斷上班、遲到、早退、下班，
    並寫入或更新紀錄；未給 user_id 時由姓名查（姓名唯一才採用）；now 是事件發生時間（前台離線補送時用），預設現在"""
    # 數今天幾筆與寫入放在同一個「立即寫入」交易：同一人的兩個事件同時進來（離線補送加本機打卡）時，
    # 後面的會等前面的寫完再數，不會兩個都看到 0 筆而寫出兩筆「上班」；提早 return 時關閉連線會自動放棄交易
    conn_db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn_db.execute("BEGIN IMMEDIATE")
    cursor = conn_db.cursor()
    if user_id is None:
        matches = cursor.execute("SELECT id FROM Users WHERE name=?", (name,)).fetchall()
        user_id = matches[0][0] if len(matches) == 1 else None

    now = now or datetime.datetime.now()
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
        conn_db.execute("COMMIT")
        conn_db.close()
        print(f"系統日誌: Updated: {name} ({status}) - 時間: {full_timestamp}")
        return f"Updated: {name} ({status})"

    conn_db.execute("INSERT INTO Attendance (name, timestamp, status, user_id) VALUES (?, ?, ?, ?)",
                    (name, full_timestamp, status, user_id))
    conn_db.execute("COMMIT")
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


# --- 管理者（後台登入用） ---

ROLE_ADMIN = "admin"
ROLE_EMPLOYEE = "employee"


def list_admins():
    """啟用中的管理者清單（只有編號、姓名、員工編號，不含人臉特徵）。"""
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn_db:
        rows = conn_db.execute(
            "SELECT id, name, employee_id FROM Users WHERE role = ? AND (is_active = 1 OR is_active IS NULL) "
            "ORDER BY id", (ROLE_ADMIN,)).fetchall()
    return [{"id": r[0], "name": r[1], "employee_id": r[2]} for r in rows]


def recognize_admin(target_embedding, threshold=1.0):
    """
    1:N 比對，只和啟用中的管理者比。回傳 (姓名, 距離)；距離不小於門檻回傳 "Unknown"，
    沒有任何管理者回傳 "No Admin"。這是單幀門檻（與打卡相同）；後台登入另外要求連續幀的平均距離更小，見 face_login。
    """
    best_name, best_dist = None, None
    with closing(sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, timeout=5)) as conn_db:
        for name, emb in conn_db.execute(
                "SELECT name, embedding FROM Users WHERE role = ? AND (is_active = 1 OR is_active IS NULL)",
                (ROLE_ADMIN,)):
            if getattr(emb, "shape", None) != getattr(target_embedding, "shape", None):
                continue                       # 特徵資料損壞或維度不同的帳號略過，不讓登入整個出錯
            dist = round(float(np.linalg.norm(emb - target_embedding)), 3)
            if best_dist is None or dist < best_dist:
                best_name, best_dist = name, dist
    if best_dist is None:
        return ("No Admin", 99.9)
    return (best_name, best_dist) if best_dist < threshold else ("Unknown", best_dist)


# --- 前台連線（L2）：給前台的人臉快照、收件去重 ---

def list_link_faces():
    """給前台的人臉資料：啟用中員工的編號、姓名、員工編號、角色、特徵（numpy）；依編號排序，順序固定。"""
    with closing(sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, timeout=5)) as conn_db:
        rows = conn_db.execute(
            "SELECT id, name, employee_id, COALESCE(role, 'employee'), embedding FROM Users "
            "WHERE is_active = 1 OR is_active IS NULL ORDER BY id").fetchall()
    return [{"id": r[0], "name": r[1], "employee_id": r[2], "role": r[3], "embedding": r[4]} for r in rows]


def get_active_user(user_id):
    """依編號取啟用中的員工 (姓名, 員工編號)；找不到或已停用回傳 None。"""
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn_db:
        row = conn_db.execute("SELECT name, employee_id FROM Users "
                              "WHERE id = ? AND (is_active = 1 OR is_active IS NULL)", (user_id,)).fetchone()
    return (row[0], row[1]) if row else None


def claim_link_event(event_id):
    """登記前台事件編號；第一次登記回傳 True，已經收過回傳 False（補送重複的事件不再處理）。"""
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn_db:
        cur = conn_db.execute("INSERT OR IGNORE INTO LinkEvents (event_id, received_at) VALUES (?, ?)",
                              (event_id, datetime.datetime.now().strftime(_TS_FORMAT)))
        conn_db.commit()
        return cur.rowcount == 1


def release_link_event(event_id):
    """處理失敗時取消登記，讓前台之後可以重送。"""
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn_db:
        conn_db.execute("DELETE FROM LinkEvents WHERE event_id = ?", (event_id,))
        conn_db.commit()


# --- 異常事件 ---

_EVIDENCE_NAME = re.compile(r"^(?P<type>[a-z]+)_(?P<d>\d{8})_(?P<t>\d{6})\.jpg$")


def add_anomaly_event(event_type, evidence_path=None, detail="", timestamp=None):
    """新增一筆異常事件（如 tailgate），回傳事件編號；timestamp 預設為現在。"""
    when = timestamp or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn_db:
        cur = conn_db.execute(
            "INSERT INTO AnomalyEvents (timestamp, event_type, detail, evidence_path) VALUES (?, ?, ?, ?)",
            (when, event_type, detail, evidence_path))
        conn_db.commit()
        return cur.lastrowid


_MERGE_MARK = "（連續 "
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _merged_detail(detail, count, first_ts, last_ts):
    """在原說明後加上合併資訊（次數與起訖時間）；已有合併資訊時先去掉舊的。"""
    base = (detail or "").split(_MERGE_MARK)[0]
    return f"{base}{_MERGE_MARK}{count} 次，{first_ts[11:]} ~ {last_ts[11:]}）"


def record_anomaly_event(event_type, detail="", make_evidence=None, merge_window=ANOMALY_MERGE_SECONDS, now=None):
    """
    記錄異常事件；同類型事件與前一筆的最後發生時間相隔不超過 merge_window 秒時，合併進前一筆
    （次數加一、更新結束時間與說明），不再新增、也不再呼叫 make_evidence 產生新證據照片。
    make_evidence 為無參數函式，只在需要新增一筆時才呼叫，回傳證據路徑。回傳 (事件編號, 是否為合併)。
    now 是事件發生時間（前台離線補送時用），預設現在。
    """
    now = now or datetime.datetime.now()
    now_str = now.strftime(_TS_FORMAT)
    # 讀最後一筆、決定合併或新增、寫入放在同一個「立即寫入」交易：事件同時進來時，後面的會等前面的寫完，
    # 才讀到它並合併（原本各自讀到同一筆舊資料，會少算次數或多出重複列）。
    # make_evidence 只是存一張證據照，交易內呼叫；失敗時放棄交易並把例外往外丟
    conn_db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    try:
        conn_db.execute("BEGIN IMMEDIATE")
        last = conn_db.execute(
            "SELECT id, timestamp, COALESCE(end_timestamp, timestamp), COALESCE(event_count, 1), detail "
            "FROM AnomalyEvents WHERE event_type = ? ORDER BY COALESCE(end_timestamp, timestamp) DESC, id DESC LIMIT 1",
            (event_type,)).fetchone()
        if last and merge_window > 0:
            try:
                gap = (now - datetime.datetime.strptime(last[2], _TS_FORMAT)).total_seconds()
            except ValueError:
                gap = None
            if gap is not None and gap <= merge_window:
                count = last[3] + 1
                conn_db.execute(
                    "UPDATE AnomalyEvents SET end_timestamp = ?, event_count = ?, detail = ? WHERE id = ?",
                    (now_str, count, _merged_detail(detail or last[4], count, last[1], now_str), last[0]))
                conn_db.execute("COMMIT")
                return last[0], True
        evidence_path = make_evidence() if make_evidence else None
        cur = conn_db.execute(
            "INSERT INTO AnomalyEvents (timestamp, event_type, detail, evidence_path) VALUES (?, ?, ?, ?)",
            (now_str, event_type, detail, evidence_path))
        conn_db.execute("COMMIT")
        return cur.lastrowid, False
    except BaseException:
        if conn_db.in_transaction:
            conn_db.execute("ROLLBACK")
        raise
    finally:
        conn_db.close()


def list_anomaly_events(start=None, end=None, event_type=None, limit=200):
    """
    查詢異常事件，新的在前，回傳字典清單（end_timestamp 為最後一次發生時間，event_count 為合併次數）。
    start、end 為 "YYYY-MM-DD" 或完整時間字串（含頭尾；只給日期時 end 涵蓋當天整天）。
    """
    clauses, params = [], []
    if start:
        clauses.append("timestamp >= ?")
        params.append(start)
    if end:
        clauses.append("timestamp <= ?")
        params.append(end + " 23:59:59" if len(end) == 10 else end)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn_db:
        rows = conn_db.execute(
            "SELECT id, timestamp, event_type, detail, evidence_path, "  # noqa: S608
            f"COALESCE(end_timestamp, timestamp), COALESCE(event_count, 1) FROM AnomalyEvents {where} "
            "ORDER BY timestamp DESC, id DESC LIMIT ?", (*params, limit)).fetchall()
    return [{"id": r[0], "timestamp": r[1], "event_type": r[2], "detail": r[3], "evidence_path": r[4],
             "end_timestamp": r[5], "event_count": r[6]} for r in rows]


def import_anomaly_photos(folder=None):
    """
    把證據資料夾內尚未登記的照片（檔名如 tailgate_20261014_091500.jpg）匯入事件表，時間取自檔名。
    已登記過的檔案（以檔名判斷）會跳過，重複執行不會重複匯入；回傳這次匯入的筆數。
    """
    folder = folder or paths.ANOMALY_DIR
    if not os.path.isdir(folder):
        return 0
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn_db:
        known = {os.path.basename(r[0]) for r in conn_db.execute(
            "SELECT evidence_path FROM AnomalyEvents WHERE evidence_path IS NOT NULL")}
        added = 0
        for fname in sorted(os.listdir(folder)):
            m = _EVIDENCE_NAME.match(fname)
            if not m or fname in known:
                continue
            try:
                when = datetime.datetime.strptime(m["d"] + m["t"], "%Y%m%d%H%M%S")
            except ValueError:
                continue
            conn_db.execute(
                "INSERT INTO AnomalyEvents (timestamp, event_type, detail, evidence_path) VALUES (?, ?, ?, ?)",
                (when.strftime("%Y-%m-%d %H:%M:%S"), m["type"], "由既有證據照片匯入（當時的人數未記錄）",
                 os.path.join(folder, fname)))
            added += 1
        conn_db.commit()
        return added
