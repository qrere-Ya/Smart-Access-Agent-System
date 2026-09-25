import sqlite3
import io
import os
import numpy as np
import datetime
import queue
import threading
import time

# --- Numpy 陣列與資料庫轉換工具 ---

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

# 註冊轉換器
sqlite3.register_adapter(np.ndarray, adapt_array)
sqlite3.register_converter("ARRAY", convert_array)

# 【合併專案調整】改用 __file__ 相對路徑，不再依賴「一定要從 src/ 資料夾啟動」這個假設，
# 不管從哪個資料夾執行這支程式，都能正確找到專案根目錄底下的 database/database.db。
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_PROJECT_ROOT, "database", "database.db")

# ==========================================
# 【09-12 健檢 P3・中等項目】log_attendance() 考勤時間窗設定
# 原本 "07:40:00" 這幾個時間字串直接寫死散在判斷式裡，這裡抽成有名字的常數，方便
# 之後看程式碼的人知道每個時間點代表什麼意思。刻意只做「改名字」這一步：判斷式用
# 的運算子（<=／<／>）跟時間數值完全沒有改變，行為 100% 跟改之前一樣。
#
# 【範圍說明】這裡不做「從設定檔讀取」或「員工個人化上下班時間」——Users 資料表
# 雖然已經有 work_start/work_end 兩個欄位，但 log_attendance() 目前還沒有讀它們，
# 那是進度看板上另一個獨立的「尚未開始」項目，不在這次改動範圍內。
# ==========================================
# 早上可視為「準時上班」的時間窗：[SHIFT_ON_TIME_START, SHIFT_ON_TIME_END]（含頭尾）
SHIFT_ON_TIME_START = "07:40:00"
SHIFT_ON_TIME_END = "08:00:00"
# 超過準時上班時間窗、但還在中午之前，視為「遲到」；超過中午才第一次刷臉，視為
# 「非打卡時段」（不判定為任何上班狀態）。
MORNING_CUTOFF = "12:00:00"
# 中午到這個時間之間是午休空窗期，不論刷幾次臉都不計紀錄。
AFTERNOON_RESUME = "16:30:00"
# 這個時間之前算「早退」；這之後、下班時間之前（含）算準時「下班」；再更晚算加班下班。
SHIFT_EARLY_LEAVE_END = "17:40:00"
SHIFT_END = "18:00:00"

def init_db():
    """初始化資料庫：建立員工表、考勤表與行事曆表"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn_db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)

    # 建立員工表 (包含姓名、性別、編號、特徵碼)
    conn_db.execute("""CREATE TABLE IF NOT EXISTS Users
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    gender TEXT,
                    employee_id TEXT,
                    embedding ARRAY NOT NULL,
                    work_start TEXT DEFAULT '09:00',
                    work_end TEXT DEFAULT '18:00')""")

    # 【P2，2026-09-12 新增：帳號管理欄位】role（角色）、is_active（是否啟用）是新加的。
    # CREATE TABLE IF NOT EXISTS 只在資料表完全不存在時才會生效，對你電腦上已經有資料的
    # 舊資料庫不會自動補上新欄位，所以這裡用 PRAGMA table_info 檢查欄位存不存在，不存在
    # 才 ALTER TABLE 補上——這樣不管是全新安裝、還是已經在用的舊資料庫，都能正確升級，
    # 不會因為欄位已經存在而噴錯，也不會漏掉舊資料庫沒有這兩個欄位的情況。
    # ALTER TABLE ADD COLUMN ... DEFAULT ... 在 SQLite 裡會直接把預設值套用到既有資料列，
    # 所以升級完舊資料（例如陳柏豫那幾筆）role 會是 'employee'、is_active 會是 1（啟用），
    # 行為跟升級前完全一樣，不會意外把原本能用的帳號變成停用。
    existing_user_columns = {row[1] for row in conn_db.execute("PRAGMA table_info(Users)").fetchall()}
    if "role" not in existing_user_columns:
        conn_db.execute("ALTER TABLE Users ADD COLUMN role TEXT DEFAULT 'employee'")
    if "is_active" not in existing_user_columns:
        conn_db.execute("ALTER TABLE Users ADD COLUMN is_active INTEGER DEFAULT 1")

    # 建立考勤紀錄表
    conn_db.execute("""CREATE TABLE IF NOT EXISTS Attendance
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    timestamp DATETIME NOT NULL,
                    status TEXT NOT NULL)""")

    # 【任務一】建立行事曆資料表
    conn_db.execute("""CREATE TABLE IF NOT EXISTS calendar
                    (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    emp_id TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    event_title TEXT NOT NULL)""")

    # 【P0，2026-09-12 新增】AI 用量與決策稽核紀錄表
    # 對應 Syscom Cubi 截圖裡「AI 用量與成本透明化」的概念：把 guardrail 攔截判斷、
    # law/attendance 路由判斷這些原本「跑完就消失」的決策事件存下來，之後可以統計
    # 攔截率、路由正確率的實際趨勢，也能餵給 evaluate_rag.py 的自動化評估擴充樣本。
    # 一筆紀錄代表「一個決策事件」（guardrail 判斷 / 路由判斷 / 直接對話），不是
    # 「一整個問題的完整流程」——同一個問題可能產生多筆紀錄，用 event_type 區分。
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
    """【任務一】為新員工插入今天的假行程"""
    conn_db = sqlite3.connect(DB_PATH)
    # 插入一筆 14:00 的專案會議
    conn_db.execute("INSERT INTO calendar (emp_id, event_time, event_title) VALUES (?, ?, ?)",
                    (emp_id, "14:00", "專案會議"))
    conn_db.commit()
    conn_db.close()
    print(f"已為員工 {emp_id} 生成今日假行程")

def get_today_event(emp_id):
    """【任務一】查詢該員工今天的行程"""
    conn_db = sqlite3.connect(DB_PATH)
    cursor = conn_db.cursor()
    cursor.execute("SELECT event_time, event_title FROM calendar WHERE emp_id=? LIMIT 1", (emp_id,))
    result = cursor.fetchone()
    conn_db.close()
    return result # 回傳 (event_time, event_title) 或 None

def register_user(name, gender, emp_id, face_embedding):
    """
    註冊新員工資訊與人臉特徵。

    【重複註冊防呆】寫入前會先檢查「姓名」或「員工編號」是否已經存在，
    只要其中一項重複，就直接拒絕註冊、不寫入資料庫（採用「拒絕」而非
    「覆蓋」策略，避免舊有臉部特徵被意外覆寫掉）。

    回傳 (成功與否: bool, 訊息文字: str)，呼叫端（例如 web_server.py）
    應該依照這個回傳值告訴使用者結果，而不是假設一定會成功。
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
    # 註冊後自動新增假行程
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
    """檢查使用者是否已註冊"""
    if isinstance(dna_or_name, str):
        return dna_or_name not in ["Unknown", "Empty DB", "N/A"]

    name, _ = recognize_face(dna_or_name)
    return name not in ["Unknown", "Empty DB"]

def log_attendance(name):
    """考勤核心邏輯：判斷上班、遲到、下班及更新機制"""
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
        if SHIFT_ON_TIME_START <= current_time <= SHIFT_ON_TIME_END:
            status = "上班"
        elif SHIFT_ON_TIME_END < current_time < MORNING_CUTOFF:
            status = "遲到"
        else:
            conn_db.close()
            return "非打卡時段"
        msg = f"In: {name} ({status})"

    elif len(records) == 1:
        if current_time < MORNING_CUTOFF:
            conn_db.close()
            return f"Keep going: {name}!" # 早上第二次刷臉，不計為下班

        if MORNING_CUTOFF <= current_time < AFTERNOON_RESUME:
            conn_db.close()
            return f"Keep going: {name}!" # 中午區間不計紀錄

        if AFTERNOON_RESUME <= current_time < SHIFT_EARLY_LEAVE_END:
            status = "早退"
        elif SHIFT_EARLY_LEAVE_END <= current_time <= SHIFT_END:
            status = "下班"
        else:
            status = "下班 (加班)"
        msg = f"Out: {name} ({status})"

    else:
        last_record_id = records[-1][0]
        if current_time < MORNING_CUTOFF:
            conn_db.close()
            return f"Keep going: {name}!" # 早上多次刷臉，不覆寫任何紀錄

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
    1:N 人臉比對，回傳最接近的員工姓名。

    【P2，2026-09-12 修改】只拿 is_active=1（或者這一列還沒跑過升級、is_active 是
    NULL——保守視為啟用，相容舊資料）的員工特徵去比對。這是配合帳號管理模組的「停用」
    功能做的修改：停用一個帳號之後，這個人應該要真的刷不到臉、被系統判定為查無資料，
    不能只是帳號管理畫面上打個叉好看而已。
    """
    # 【資源規範・RAM】不再 fetchall() 把所有員工的 512 維向量一次讀進記憶體（每一幀都會呼叫），
    # 改成逐列迭代、只保留目前最小距離，任何時刻記憶體只有一列。
    best_name, best_dist = None, None
    conn_db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    try:
        cursor = conn_db.cursor()
        cursor.execute("SELECT name, embedding FROM Users WHERE is_active = 1 OR is_active IS NULL")
        for name, emb in cursor:
            dist = round(float(np.linalg.norm(emb - target_embedding)), 2)
            if best_dist is None or dist < best_dist:  # 嚴格小於：距離相同時保留先出現者，與原 argmin 行為一致
                best_name, best_dist = name, dist
    finally:
        conn_db.close()

    if best_dist is None:
        return ("Empty DB", 99.9)

    return (best_name, best_dist) if best_dist < threshold else ("Unknown", best_dist)


def log_llm_usage(event_type, question, route=None, guardrail_pass=None, similarity_score=None, source="gradio"):
    """
    【P0，2026-09-12 新增】RAG 問答稽核紀錄的唯一寫入入口。

    event_type 目前用三種："guardrail"（防護欄放行/攔截判斷）、"route"（law/attendance
    路由判斷）、"basic"（Basic 模式，跳過防護欄直接問 LLM）。呼叫端（rag_guardrail.py、
    main_guardrail_rag.py）只在既有函式「快回傳之前」多加這一行，不會改到任何判斷邏輯。

    is_pass 欄位刻意留空（NULL），保留給之後 evaluate_rag.py 的裁判官驗證結果回填用，
    這裡不處理自動化評估的邏輯，避免一次改動牽扯太多支程式。

    這個函式只負責「寫入」，任何寫入失敗（例如資料庫檔案剛好被鎖住）都只印警告、絕不
    往外拋出例外——稽核紀錄寫不進去，不該連帶讓真正的問答流程也跟著中斷。
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
