"""
rag_sql_engine.py：考勤與員工名冊的即時 SQL 查詢引擎。

主要功能：
  - 建立唯讀的 NL-to-SQL 查詢引擎，只開放 Attendance、EmployeeRoster（不含人臉特徵的視圖）、calendar
  - SafeSQLDatabase：執行前用 sqlglot 驗證，只允許單一 SELECT 且只能查白名單資料表
  - 確定性查詢（優先於 LLM 翻譯 SQL）：姓名 + 日期、姓名 + 遲到/早退、姓名 + 打卡紀錄筆數
  - 員工姓名比對：先精確比對，再容許 1 字錯字，多個候選人時放棄不猜
  - try_attendance_answer：先走確定性查詢，沒命中再交給 LLM 查詢引擎，並回報是否真的查到資料

串接模組：
  - fuzzy_match：姓名與關鍵字的錯字容錯
  - ablation_config：消融實驗開關（是否使用確定性查詢）
  - rag_guardrail：查詢員工名冊與姓名比對
  - main_guardrail_rag：建立引擎並呼叫 try_attendance_answer
"""

import os
import re
import sqlite3
import pathlib
import threading
import time

import sqlglot
from sqlglot import exp
from sqlalchemy import create_engine
from llama_index.core import SQLDatabase
from llama_index.core.query_engine import NLSQLTableQueryEngine
from llama_index.core.prompts import PromptTemplate
from llama_index.core.prompts.prompt_type import PromptType

from rag import ablation_config
from rag.fuzzy_match import min_edit_distance_in_text
from shared import paths

# 門禁系統資料庫位置，可用環境變數 ACCESS_SYSTEM_DB_PATH 覆蓋
ACCESS_SYSTEM_DB_PATH = os.environ.get(
    "ACCESS_SYSTEM_DB_PATH",
    paths.DB_PATH
)


# 把 SQL 查詢結果組成繁體中文回答的提示詞
_SQL_RESPONSE_PROMPT_ZH = PromptTemplate(
    "你是門禁系統的即時資料查詢助理，請根據使用者的問題與 SQL 查詢結果，"
    "用繁體中文回答。\n"
    "問題: {query_str}\n"
    "SQL 查詢語句: {sql_query}\n"
    "SQL 查詢結果: {context_str}\n"
    "回答規則：\n"
    "1. 只根據 SQL 查詢結果回答，不要自己瞎猜或補充查詢結果以外的資訊。\n"
    "2. 如果 SQL 查詢結果是空的或 0 筆，請直接誠實回答「查無符合條件的資料」，"
    "不要自己過度推論成別的意思（例如不要把『這個條件下查無資料』講成『員工人數是 0 人』）。\n"
    "3. 回答要精準、簡短，不要加上跟問題無關的說明。\n"
    "4. 如果 SQL 查詢結果裡同時有「人數」跟「姓名清單」兩種資料，不要只回姓名清單、也"
    "不要只回數字，兩個都要講，姓名之間用頓號「、」分隔。例如 SQL 查詢結果是"
    "「人數=3, 姓名=陳柏豫,葉君緯,周宇祥」，就要回答"
    "「目前員工有 3 人：陳柏豫、葉君緯、周宇祥」這樣的完整句子。\n"
    "5. 【非常重要，2026-09-02 修正】如果 SQL 查詢結果『只有一個數字、沒有姓名清單』"
    "（例如結果是 [(3,)] 這種格式，只有一個欄位），就只需要回答人數，句尾用句號，"
    "例如「目前員工有 3 人。」——絕對不可以自己在句尾加冒號、更絕對不可以在完全沒有"
    "拿到姓名資料的情況下自己編出姓名。只有在 SQL 查詢結果真的同時包含人數跟姓名兩種"
    "資料時，才可以用第 4 點的「人數：姓名、姓名」格式；沒有姓名資料時，寧可只回"
    "數字，也不要照抄第 4 點例句的『冒號』格式，因為那樣會變成句子講一半、後面沒有"
    "內容可以填。\n"
    "回答: "
)

# 把問題翻成 SQL 的提示詞：規則加上 few-shot 範例，讓小模型照範例寫而不是自己發明欄位
_SQL_TEXT_TO_SQL_PROMPT_ZH = PromptTemplate(
    "請根據使用者問題，寫出一句在 {dialect} 資料庫上可以正確執行的 SQL 查詢語句。\n"
    "規則（務必嚴格遵守）：\n"
    "1. 只能使用下面「schema」段落裡列出的資料表與欄位名稱，絕對不可以自己發明資料庫裡\n"
    "   沒有的欄位（例如 Attendance 表沒有 employee_id 這個欄位，不要假裝它有）。\n"
    "2. 只挑跟問題有關的少數幾個欄位查詢就好，不要整張表 SELECT *。\n"
    "3. 不要自己多加問題沒問到的篩選條件（例如問題沒問「缺席」、「遲到」，就不要自己加上\n"
    "   status = 'absent' 這種條件）。\n"
    "4. 名冊類問題（員工人數、有哪些員工、員工姓名）請查 EmployeeRoster；打卡/出缺勤/\n"
    "   遲到早退類問題請查 Attendance。EmployeeRoster 與 Attendance 之間沒有可靠的\n"
    "   對應鍵可以 JOIN，需要交叉比對時請用 name 欄位對應，不要用 EmployeeRoster.id。\n"
    "務必嚴格照下面這個格式，每個項目各佔一行：\n\n"
    "Question: 問題內容\n"
    "SQLQuery: 要執行的 SQL 查詢語句\n"
    "SQLResult: SQL 查詢語句的結果\n"
    "Answer: 最終答案\n\n"
    "5. 姓名(name)欄位裡可能會有重複值（同一個人不小心被登記了好幾筆一模一樣的資料），\n"
    "   問到「人數」或「有誰」的時候，要用 DISTINCT 去掉重複姓名，不然人數會算錯。\n"
    "6. 這是 SQLite 資料庫，不是 MySQL、不是 PostgreSQL，不可以使用 TIMESTAMPDIFF()、\n"
    "   DATEDIFF() 這些 SQLite 沒有的函式，會直接查詢失敗。真的需要算時間差請用\n"
    "   SQLite 內建的 julianday() 或 strftime()。\n"
    "7. work_start / work_end（上下班時間）只存在 EmployeeRoster 表，Attendance 表\n"
    "   沒有這兩個欄位，不要在查 Attendance 的語句裡使用它們。\n"
    "8. 【非常重要，回答完就要停筆】下面的範例只是示範格式長什麼樣子，不是要你把它們\n"
    "   也一起回答一遍。你『只能』針對最下面「實際要回答的問題」寫『一組』\n"
    "   Question/SQLQuery/SQLResult/Answer，四行都寫完、Answer 那一行寫完之後就要\n"
    "   立刻停筆，絕對不可以自己接著編造下一個 Question 或下一輪範例——這點做錯的話，\n"
    "   後面接的內容會被誤判成 SQL 語句的一部分，整句查詢就會直接執行失敗。\n"
    "9. 【2026-09-13 新增，非常重要】Attendance 表的 status 欄位，實際存進資料庫的值\n"
    "   『只有』這幾種寫死的繁體中文字串：'上班'（準時上班）、'遲到'、'早退'、'下班'、\n"
    "   '下班 (加班)'。絕對不可以自己發明或翻譯成英文（例如 'present'、'late'、\n"
    "   'absent'、'on time' 這些統統不是資料庫裡真正存在的值），查詢條件裡只要用到\n"
    "   status，一定要從上面這幾個繁體中文字串裡面選，選錯的話 SQL 會執行成功但\n"
    "   一筆資料都比對不到，你會誤以為『查無資料』，但其實只是條件寫錯了。\n"
    "10. 問到『這個月』相關的統計（例如『這個月遲到幾次』），要用\n"
    "    strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now') 這種同時比對年份跟\n"
    "    月份的寫法，不要只比對 strftime('%m', timestamp)，不然會把去年同一個月的\n"
    "    紀錄也算進來，變成不同年份混在一起統計。\n"
    "11. 【2026-09-14 新增，非常重要】Attendance 表跟 EmployeeRoster 表『都沒有』薪資／\n"
    "    工資／salary／wage 這種欄位，資料庫裡完全沒有存任何金額資料。問題裡如果同時\n"
    "    出現『遲到』『扣薪』『薪水』這種字（例如『陳柏豫這個月遲到扣薪上限多少』），\n"
    "    你這裡『只』負責回答『遲到/早退次數是多少』這種純資料庫可以回答的部分，"
    "    絕對不要嘗試自己去 SUM 或計算金額——你會找不到欄位、查詢失敗，或更糟是自己\n"
    "    編出一個不存在的欄位名稱。扣薪金額該怎麼算是法規問題，不是這裡的工作。\n"
    "12. 【2026-09-26 新增】calendar 表（員工行事曆/會議事項）有三個欄位：emp_id\n"
    "    （員工編號，注意不是姓名）、event_time（時間）、event_title（事項標題）。\n"
    "    問題如果是用姓名問某人的行程（例如『陳柏豫今天有什麼行程』），要先用\n"
    "    EmployeeRoster.name 對應到 EmployeeRoster.employee_id，再用這個員工編號去\n"
    "    JOIN calendar.emp_id，不可以直接拿姓名去比對 calendar.emp_id（型別對不上，\n"
    "    一定查不到）。\n\n"
    "以下是幾個範例，請模仿這種簡單、直接對應資料表的寫法，特別注意每個範例都是\n"
    "「完整的四行」，最後一定有 Answer 收尾：\n\n"
    "Question: 目前員工有幾人？\n"
    "SQLQuery: SELECT COUNT(DISTINCT name) FROM EmployeeRoster\n"
    "SQLResult: [(3,)]\n"
    "Answer: 目前員工有 3 人。\n\n"
    "Question: 列出所有員工的姓名\n"
    "SQLQuery: SELECT DISTINCT name FROM EmployeeRoster\n"
    "SQLResult: [('陳柏豫',), ('葉君緯',), ('周宇祥',)]\n"
    "Answer: 目前員工有：陳柏豫、葉君緯、周宇祥。\n\n"
    "Question: 目前員工有幾人，需列出姓名\n"
    "SQLQuery: SELECT COUNT(DISTINCT name) AS 人數, GROUP_CONCAT(DISTINCT name) AS 姓名 FROM EmployeeRoster\n"
    "SQLResult: [(3, '陳柏豫,葉君緯,周宇祥')]\n"
    "Answer: 目前員工有 3 人：陳柏豫、葉君緯、周宇祥。\n\n"
    "Question: 陳柏豫今天幾點打卡？\n"
    "SQLQuery: SELECT timestamp, status FROM Attendance WHERE name = '陳柏豫' ORDER BY timestamp DESC LIMIT 1\n"
    "SQLResult: [('2026-09-02 09:03:00', '上班')]\n"
    "Answer: 陳柏豫今天 09:03 打卡，狀態為正常出勤。\n\n"
    "Question: 葉君緯這個月遲到幾次？\n"
    "SQLQuery: SELECT COUNT(*) FROM Attendance WHERE name = '葉君緯' AND status = '遲到' "
    "AND strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now')\n"
    "SQLResult: [(2,)]\n"
    "Answer: 葉君緯這個月遲到 2 次。\n\n"
    "Question: 陳柏豫這個月遲到扣薪上限多少？\n"
    "SQLQuery: SELECT COUNT(*) FROM Attendance WHERE name = '陳柏豫' AND status = '遲到' "
    "AND strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now')\n"
    "SQLResult: [(1,)]\n"
    "Answer: 陳柏豫這個月遲到 1 次。\n\n"
    "Question: 陳柏豫今天有什麼行程？\n"
    "SQLQuery: SELECT c.event_time, c.event_title FROM calendar c "
    "JOIN EmployeeRoster e ON c.emp_id = e.employee_id WHERE e.name = '陳柏豫'\n"
    "SQLResult: [('14:00', '專案會議')]\n"
    "Answer: 陳柏豫今天 14:00 有「專案會議」。\n\n"
    "範例到此結束。只能使用下面列出的資料表：\n"
    "{schema}\n\n"
    "現在請針對下面這一題，寫出『一組』Question/SQLQuery/SQLResult/Answer，"
    "寫完 Answer 就結束，不要再接著編下一題：\n\n"
    "Question: {query_str}\n"
    "SQLQuery: ",
    prompt_type=PromptType.TEXT_TO_SQL,
)


def _ensure_safe_employee_roster_view(db_path: str):
    """建立 EmployeeRoster 視圖，只含姓名、性別、員工編號、上下班時間，不含人臉特徵，供 LLM 查詢名冊。"""
    # 建立視圖需要寫入權限，所以用一般連線；實際供 LLM 查詢的連線仍是唯讀
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE VIEW IF NOT EXISTS EmployeeRoster AS "
            "SELECT id, name, gender, employee_id, work_start, work_end FROM Users"
        )
        conn.commit()
    finally:
        conn.close()


# include_tables 只影響提示詞內容，不限制實際執行，所以在 run_sql 用白名單驗證
_ALLOWED_SQL_TABLES = {"attendance", "employeeroster", "calendar"}


class SafeSQLDatabase(SQLDatabase):
    """執行前驗證 SQL：只允許單一 SELECT，且只能引用白名單資料表，驗證失敗拋出 ValueError。"""
    def run_sql(self, command: str):
        try:
            statements = [s for s in sqlglot.parse(command, read="sqlite") if s is not None]
        except Exception as e:
            raise ValueError(f"SQL 解析失敗，拒絕執行: {e}") from e

        if len(statements) != 1:
            raise ValueError(f"只允許單一 SELECT 陳述式，拒絕執行（偵測到 {len(statements)} 句）。")

        statement = statements[0]
        if not isinstance(statement, exp.Select):
            raise ValueError(f"只允許 SELECT 陳述式，拒絕執行（偵測到 {type(statement).__name__}）。")

        referenced_tables = {table.name.lower() for table in statement.find_all(exp.Table)}
        disallowed = referenced_tables - _ALLOWED_SQL_TABLES
        if disallowed:
            raise ValueError(f"查詢引用了白名單外的資料表 {disallowed}，拒絕執行。")

        return super().run_sql(command)


def build_sql_engine():
    """
    建立即時查詢門禁資料庫的 SQL 查詢引擎，找不到資料庫或建立失敗時回傳 None。
    開放：Attendance、EmployeeRoster 視圖、calendar；連線強制唯讀。
    不開放：Users 原始表（含人臉特徵）與 llm_usage_log（其他人的對話紀錄）。
    """
    if not os.path.exists(ACCESS_SYSTEM_DB_PATH):
        print(f"⚠️ [SQL Engine] 找不到門禁系統資料庫: {ACCESS_SYSTEM_DB_PATH}，考勤即時查詢功能將無法使用。")
        print("    請確認 runtime/database.db 是否存在，或設定環境變數 ACCESS_SYSTEM_DB_PATH。")
        return None

    try:
        _ensure_safe_employee_roster_view(ACCESS_SYSTEM_DB_PATH)
    except Exception as e:
        print(f"⚠️ [SQL Engine] 建立安全版員工名冊視圖(EmployeeRoster)失敗：{e}")
        print("    員工名冊類問題（人數、有誰…）可能無法查詢，但打卡紀錄查詢不受影響。")

    def _readonly_connection():
        abs_path = os.path.abspath(ACCESS_SYSTEM_DB_PATH)
        uri = pathlib.Path(abs_path).as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, check_same_thread=False)

    try:
        engine = create_engine("sqlite://", creator=_readonly_connection)
        sql_database = SafeSQLDatabase(
            engine,
            include_tables=["Attendance", "EmployeeRoster", "calendar"],
            view_support=True,  # EmployeeRoster 是視圖，必須開啟
        )
        return NLSQLTableQueryEngine(
            sql_database=sql_database,
            tables=["Attendance", "EmployeeRoster", "calendar"],
            text_to_sql_prompt=_SQL_TEXT_TO_SQL_PROMPT_ZH,
            response_synthesis_prompt=_SQL_RESPONSE_PROMPT_ZH,
            verbose=False,
        )
    except Exception as e:
        print(f"⚠️ [SQL Engine] 建立即時考勤查詢引擎失敗: {e}")
        return None


# 員工姓名清單的短時間快取（預設 5 秒，可用 EMPLOYEE_ROSTER_CACHE_TTL 調整），
# 時間設得短是為了讓剛註冊的員工能很快被查到
_EMPLOYEE_NAMES_CACHE_TTL = float(os.environ.get("EMPLOYEE_ROSTER_CACHE_TTL", "5"))
_employee_names_cache_lock = threading.Lock()
_employee_names_cache = {"names": None, "at": 0.0}


def _get_known_employee_names():
    """從 EmployeeRoster 視圖查出所有員工姓名（短時間快取）；查不到回傳空清單，失敗時不快取。"""
    now = time.time()
    with _employee_names_cache_lock:
        cached_names = _employee_names_cache["names"]
        if cached_names is not None and (now - _employee_names_cache["at"]) < _EMPLOYEE_NAMES_CACHE_TTL:
            return cached_names

    if not os.path.exists(ACCESS_SYSTEM_DB_PATH):
        return []
    try:
        abs_path = os.path.abspath(ACCESS_SYSTEM_DB_PATH)
        uri = pathlib.Path(abs_path).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT name FROM EmployeeRoster")
        names = [row[0] for row in cursor.fetchall() if row[0]]
        conn.close()
    except Exception as e:
        print(f"⚠️ [SQL Engine] 查詢員工名冊失敗: {e}")
        return []

    with _employee_names_cache_lock:
        _employee_names_cache["names"] = names
        _employee_names_cache["at"] = now
    return names


_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")

# 姓名模糊比對只容許 1 個字元的編輯距離，避免把別人的資料當成這個人的回答
_EMPLOYEE_NAME_FUZZY_MAX_DISTANCE = 1


def _match_known_employee_name(message: str, known_names):
    """
    在訊息裡找出一個真實員工姓名：先精確比對，找不到再容許 1 字錯字；
    模糊比對有多個候選人時放棄，回傳 None（寧可查不到也不猜錯人）。
    """
    for name in known_names:
        if name and name in message:
            return name

    if not known_names:
        return None

    fuzzy_candidates = [
        name for name in known_names
        if name and (
            (distance := min_edit_distance_in_text(name, message)) is not None
            and distance <= _EMPLOYEE_NAME_FUZZY_MAX_DISTANCE
        )
    ]
    if len(fuzzy_candidates) == 1:
        matched_name = fuzzy_candidates[0]
        print(f"  [Debug SQL] 姓名模糊比對命中「{matched_name}」（訊息中沒有完全相符的姓名，判定為打字誤差）")
        return matched_name
    if len(fuzzy_candidates) > 1:
        print(f"  [Debug SQL] 姓名模糊比對有多個候選人 {fuzzy_candidates}，無法判斷是哪一位，放棄比對")
    return None


# 問題中的關鍵字對應到 Attendance.status 的值
_ATTENDANCE_STATUS_KEYWORDS = {"遲到": "遲到", "早退": "早退"}


def _detect_status_keyword(message: str):
    """訊息含「遲到」或「早退」就回傳對應的 status 值，否則回傳 None（不做錯字容錯）"""
    for keyword, status in _ATTENDANCE_STATUS_KEYWORDS.items():
        if keyword in message:
            return status
    return None


def _try_deterministic_status_lookup(message: str):
    """
    確定性查詢：姓名 + 遲到/早退（不指定日期），例如「葉君緯有無遲到紀錄」。
    訊息含「這個月」或「本月」時只查當月，否則查全部。
    回傳答案字串；句型不符合回傳 None，交給後面的路線處理。
    """
    status = _detect_status_keyword(message)
    if status is None:
        return None

    known_names = _get_known_employee_names()
    matched_name = _match_known_employee_name(message, known_names)
    if not matched_name:
        return None

    is_month_scoped = ("這個月" in message) or ("本月" in message)

    try:
        abs_path = os.path.abspath(ACCESS_SYSTEM_DB_PATH)
        uri = pathlib.Path(abs_path).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        cursor = conn.cursor()
        if is_month_scoped:
            cursor.execute(
                "SELECT timestamp FROM Attendance WHERE name = ? AND status = ? "
                "AND strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now') "
                "ORDER BY timestamp ASC",
                (matched_name, status),
            )
        else:
            cursor.execute(
                "SELECT timestamp FROM Attendance WHERE name = ? AND status = ? "
                "ORDER BY timestamp ASC",
                (matched_name, status),
            )
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"⚠️ [SQL Engine] 確定性遲到/早退查詢失敗: {e}")
        return None

    scope_desc = "這個月" if is_month_scoped else ""

    if not rows:
        return f"{matched_name}{scope_desc}沒有{status}紀錄。"

    timestamps = [row[0] for row in rows]
    return (
        f"{matched_name}{scope_desc}共有 {len(timestamps)} 次{status}紀錄："
        + "、".join(timestamps) + "。"
    )


# 廣義打卡紀錄關鍵字（容許 1 字錯字）
_ATTENDANCE_RECORD_KEYWORDS = ("打卡紀錄", "出勤紀錄", "打卡狀態", "考勤紀錄")
_ATTENDANCE_RECORD_KEYWORD_FUZZY_MAX_DISTANCE = 1


def _detect_record_keyword(message: str) -> bool:
    """訊息含廣義打卡紀錄關鍵字（容許 1 字錯字）就回傳 True"""
    for keyword in _ATTENDANCE_RECORD_KEYWORDS:
        distance = min_edit_distance_in_text(keyword, message)
        if distance is not None and distance <= _ATTENDANCE_RECORD_KEYWORD_FUZZY_MAX_DISTANCE:
            return True
    return False


def _try_deterministic_record_count_lookup(message: str):
    """
    確定性查詢：姓名 + 廣義打卡紀錄關鍵字，例如「周宇祥是否有打卡紀錄」，回傳該員工的打卡總筆數。
    句型不符合回傳 None。
    """
    if not _detect_record_keyword(message):
        return None

    known_names = _get_known_employee_names()
    matched_name = _match_known_employee_name(message, known_names)
    if not matched_name:
        return None

    try:
        abs_path = os.path.abspath(ACCESS_SYSTEM_DB_PATH)
        uri = pathlib.Path(abs_path).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM Attendance WHERE name = ?", (matched_name,))
        count = cursor.fetchone()[0]
        conn.close()
    except Exception as e:
        print(f"⚠️ [SQL Engine] 確定性打卡紀錄筆數查詢失敗: {e}")
        return None

    if count == 0:
        return f"{matched_name}目前沒有打卡紀錄。"
    return f"{matched_name}共有 {count} 筆打卡紀錄。"


def _try_deterministic_date_lookup(message: str):
    """
    確定性查詢：姓名 + 具體日期（YYYY-MM-DD），列出該員工當天全部打卡紀錄。
    句型不符合回傳 None。
    """
    date_match = _DATE_PATTERN.search(message)
    if not date_match:
        return None
    target_date = date_match.group(0)

    known_names = _get_known_employee_names()
    matched_name = _match_known_employee_name(message, known_names)
    if not matched_name:
        return None

    try:
        abs_path = os.path.abspath(ACCESS_SYSTEM_DB_PATH)
        uri = pathlib.Path(abs_path).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT timestamp, status FROM Attendance WHERE name = ? AND timestamp LIKE ? "
            "ORDER BY timestamp ASC",
            (matched_name, f"{target_date}%"),
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"⚠️ [SQL Engine] 確定性日期查詢失敗: {e}")
        return None

    if not rows:
        return f"查無{matched_name}在{target_date}的打卡紀錄。"

    if len(rows) == 1:
        ts, status = rows[0]
        time_part = ts.split(" ")[1] if " " in ts else ts
        return f"{matched_name}在{target_date}的打卡時間為 {time_part}，系統判定狀態為{status}。"

    parts = []
    for ts, status in rows:
        time_part = ts.split(" ")[1] if " " in ts else ts
        parts.append(f"{time_part}（{status}）")
    return f"{matched_name}在{target_date}共有 {len(rows)} 筆打卡紀錄：" + "、".join(parts) + "。"


def try_attendance_answer(engine, message: str):
    """
    嘗試用考勤資料庫回答問題，回傳 (是否真的查到資料, 答案文字或 None)。
    順序：確定性查詢（日期 → 遲到/早退 → 筆數，由窄到寬）→ LLM 查詢引擎。
    是否查到資料以 SQL 實際回傳的資料列為準；查不到或執行失敗都回傳 (False, None)，
    讓呼叫端改問法規。engine 由呼叫端傳入，可為 None。
    """
    # 確定性查詢可在消融實驗中關閉，全部交給 LLM 翻譯 SQL
    if ablation_config.USE_DETERMINISTIC_SQL:
        deterministic_answer = _try_deterministic_date_lookup(message)
        if deterministic_answer is not None:
            return True, deterministic_answer

        deterministic_status_answer = _try_deterministic_status_lookup(message)
        if deterministic_status_answer is not None:
            return True, deterministic_status_answer

        deterministic_record_answer = _try_deterministic_record_count_lookup(message)
        if deterministic_record_answer is not None:
            return True, deterministic_record_answer

    if engine is None:
        return False, None
    try:
        sql_response = engine.query(message)
        metadata = getattr(sql_response, "metadata", None) or {}
        raw_rows = metadata.get("result")
        # 印出模型產生的 SQL 與結果，方便診斷
        print(f"  [Debug SQL] 產生的 SQL 語句: {metadata.get('sql_query')!r}")
        print(f"  [Debug SQL] SQL 查詢結果: {raw_rows!r}")
        if raw_rows:
            return True, str(sql_response)
        return False, None
    except Exception as e:
        print(f"DEBUG: SQL QueryEngine Error: {e}")
        return False, None


def sample_random_attendance_record():
    """隨機抽一筆考勤紀錄供自動化評估出題，回傳 {'name', 'timestamp', 'status'}，抽不到回傳 None。"""
    if not os.path.exists(ACCESS_SYSTEM_DB_PATH):
        return None
    try:
        abs_path = os.path.abspath(ACCESS_SYSTEM_DB_PATH)
        uri = pathlib.Path(abs_path).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT name, timestamp, status FROM Attendance ORDER BY RANDOM() LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {"name": row[0], "timestamp": row[1], "status": row[2]}
    except Exception as e:
        print(f"⚠️ [Eval] 抽樣考勤紀錄失敗: {e}")
        return None
