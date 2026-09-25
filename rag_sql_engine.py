"""
即時查詢門禁系統資料庫（考勤/員工名冊）的 SQL 查詢引擎。

【2026-09-02，架構重構拆出】原本這些邏輯放在 main_guardrail_rag.py 裡，
跟法規索引、防護欄路由、對話引擎混在同一個 700 多行的檔案裡。這裡只負責
一件事：把使用者的自然語言問題，安全地轉成 SQL、查詢即時的門禁系統資料庫，
並把結果組成一句話回答。
"""

import os
import re
import sqlite3
import pathlib
import threading
import time

from sqlalchemy import create_engine
from llama_index.core import SQLDatabase
from llama_index.core.query_engine import NLSQLTableQueryEngine
from llama_index.core.prompts import PromptTemplate
from llama_index.core.prompts.prompt_type import PromptType

import ablation_config

# ==========================================
# 門禁系統即時資料庫位置
# ==========================================
# 【合併專案調整】main_guardrail_rag.py 現在跟 database/ 資料夾同一層（單一合併專案，
# 不再分成兩個資料夾），所以直接找同層的 database/database.db 即可。
# 如果你的實際擺放位置不同，設定環境變數 ACCESS_SYSTEM_DB_PATH 覆蓋即可，不用改程式碼。
ACCESS_SYSTEM_DB_PATH = os.environ.get(
    "ACCESS_SYSTEM_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "database", "database.db")
)


# 【修正】SQL 查詢引擎(NLSQLTableQueryEngine)預設的「把查詢結果組成一句話」的提示詞
# 是英文寫的，也完全沒有規定要用繁體中文回答——這跟對話引擎的 system_prompt
# 已經寫死「請統一使用繁體中文專業回答」不一致，導致實測時同一個系統一下用英文回答
# （"The number of employees is 0."）、一下用簡體中文回答，使用者體驗很奇怪。
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

# 【修正：AI 把問題翻成 SQL 語句這一步會亂翻】實測發現問「目前員工有幾人需列出姓名」
# 這種名冊類問題，NLSQLTableQueryEngine 用的本地 7B 小模型（qwen2:7b）翻出來的 SQL
# 語句是：
#   SELECT COUNT(DISTINCT e.name) FROM EmployeeRoster e
#   WHERE NOT EXISTS (SELECT * FROM Attendance a WHERE a.employee_id = e.id AND status = 'absent')
# 這句話有兩個明顯的錯誤：
#   1. Attendance 這張表根本沒有 employee_id 這個欄位（只有 id / name / timestamp /
#      status），是模型自己憑空發明出來的欄位名稱，資料庫裡真的查不到東西。
#   2. 問題本身根本沒問「缺席(absent)」，模型卻自己多加了這個跟問題無關的條件——
#      這是小模型在做「自然語言轉 SQL」時常見的過度延伸，問題越模糊、越容易亂猜。
# llama-index 預設的 text_to_sql_prompt 雖然已經有寫「不要查詢不存在的欄位」，但
# 小模型還是沒有確實遵守。這裡改用「加了具體範例」的客製化提示詞，直接示範幾種
# 最常見的名冊/考勤問題該怎麼寫成 SQL，讓小模型「照抄範例的寫法」而不是自己憑空
# 亂造，準確率會好很多——這是一般業界處理小模型 NL-to-SQL 不穩定最常見的做法
# （加 few-shot 範例），不是碰運氣亂猜的修法。
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
    "    編出一個不存在的欄位名稱。扣薪金額該怎麼算是法規問題，不是這裡的工作。\n\n"
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
    "範例到此結束。只能使用下面列出的資料表：\n"
    "{schema}\n\n"
    "現在請針對下面這一題，寫出『一組』Question/SQLQuery/SQLResult/Answer，"
    "寫完 Answer 就結束，不要再接著編下一題：\n\n"
    "Question: {query_str}\n"
    "SQLQuery: ",
    prompt_type=PromptType.TEXT_TO_SQL,
)


def _ensure_safe_employee_roster_view(db_path: str):
    """
    建立一個「安全版」的員工名冊視圖(VIEW)，只給 SQL 查詢引擎使用。

    背景：原本 SQL 查詢引擎只准查 Attendance（打卡紀錄）表，完全擋住整個 Users
    （員工名冊）表，理由是 Users 表裡有 embedding 這一欄（512 維人臉生物特徵向量），
    絕對不能讓 LLM 有機會查到或外洩。但這樣做「連帶」把姓名、性別、員工編號、上下班
    時間這些完全不敏感的名冊資訊也一起擋住了，導致像「員工人數有幾人」「員工有誰」
    這種很正常的名冊類問題，AI 只能看打卡紀錄硬猜，猜不出正確答案。

    真正該防的只有 embedding 這一欄，不是整張 Users 表。解法：建立一個「視圖(VIEW)」
    EmployeeRoster，只挑出 Users 表裡的安全欄位（id、name、gender、employee_id、
    work_start、work_end，故意不含 embedding），SQL 查詢引擎改成同時看得到
    Attendance + EmployeeRoster 這兩張「表」。這樣一來：
      - 名冊類問題（人數、有誰、員工編號查詢…）可以用 EmployeeRoster 正確回答。
      - embedding 欄位在資料庫結構層級就從來沒有出現在 LLM 看得到的任何一張表裡，
        不是靠「叮嚀 LLM 不要查」這種口頭約定，是它實體上根本查不到、也組不出能查到
        它的 SQL 語句——安全性比原本「整張表都擋掉」更精準，也沒有比較不安全。

    這裡故意用「一般（可寫）連線」建立視圖，因為 CREATE VIEW 需要寫入權限；建立完
    立刻關閉這個連線。build_sql_engine() 裡實際供 LLM 查詢用的，仍然是另外開的
    「唯讀」連線，LLM 自己產生的 SQL 語句還是不可能真的改到資料庫，這個安全保證
    完全沒有被削弱。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE VIEW IF NOT EXISTS EmployeeRoster AS "
            "SELECT id, name, gender, employee_id, work_start, work_end FROM Users"
        )
        conn.commit()
    finally:
        conn.close()


def build_sql_engine():
    """
    建立即時查詢門禁系統資料庫 (database.db) 的 SQL 查詢引擎。

    可查詢範圍（⚠️ 安全性設計，請勿隨意更動）：
      1. Attendance 表（打卡紀錄）：完整開放查詢。
      2. EmployeeRoster 視圖（員工名冊的安全欄位：姓名/性別/員工編號/上下班時間）：
         開放查詢，但這是特意建立的「視圖」，不是 Users 原始表，視圖裡完全不含
         embedding（人臉 512 維特徵向量）欄位，LLM 從結構上就不可能查到生物特徵資料，
         詳見 _ensure_safe_employee_roster_view() 的說明。
      3. 資料庫連線強制以「唯讀模式」開啟，就算 LLM 產生的 SQL 語句寫錯（例如誤生成
         UPDATE/DELETE），也不可能真的改到門禁系統正在使用中的資料庫。
    """
    if not os.path.exists(ACCESS_SYSTEM_DB_PATH):
        print(f"⚠️ [SQL Engine] 找不到門禁系統資料庫: {ACCESS_SYSTEM_DB_PATH}，考勤即時查詢功能將無法使用。")
        print("    請確認 database/database.db 是否存在，或設定環境變數 ACCESS_SYSTEM_DB_PATH。")
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
        sql_database = SQLDatabase(
            engine,
            include_tables=["Attendance", "EmployeeRoster"],
            view_support=True,  # 一定要打開，EmployeeRoster 是視圖不是表，沒開這個會被忽略
        )
        return NLSQLTableQueryEngine(
            sql_database=sql_database,
            tables=["Attendance", "EmployeeRoster"],
            text_to_sql_prompt=_SQL_TEXT_TO_SQL_PROMPT_ZH,
            response_synthesis_prompt=_SQL_RESPONSE_PROMPT_ZH,
            # 【2026-09-02：改回 False】原本先開著 True 方便你驗證 SQL 生成修正有沒有生效
            # （印出「Predicted SQL query」等除錯訊息）。你已經實測確認「目前員工有幾個人」
            # 「只列出名子」都正確、沒有再出現幻覺或無效 SQL，修正確認生效，改回 False，
            # 畫面/console 訊息不會再那麼雜。真的要除錯時再臨時改回 True 即可。
            verbose=False,
        )
    except Exception as e:
        print(f"⚠️ [SQL Engine] 建立即時考勤查詢引擎失敗: {e}")
        return None


# 【2026-09-22 效能】_get_known_employee_names() 在同一題問答裡可能被呼叫兩次
# （check_query_route() 的 _detect_mixed_question() 一次、try_attendance_answer() 的
# _try_deterministic_date_lookup() 一次），原本每次呼叫都重新開一個 SQLite 連線查一次
# 整張 EmployeeRoster 視圖。員工名冊不是每毫秒都在變的資料，這裡加一個很短的 TTL
# 快取：預設 5 秒內的重複呼叫直接回傳快取結果；可用環境變數 EMPLOYEE_ROSTER_CACHE_TTL
# 調整。刻意設得很短（不是常見的分鐘級快取），是因為這個系統有 QR 掃碼即時註冊員工的
# 功能（見 web_server.py），新註冊的員工最慢也要在幾秒內就能被對話系統查到，不能因為
# 快取太久而讓「剛註冊完馬上問」查不到人。
_EMPLOYEE_NAMES_CACHE_TTL = float(os.environ.get("EMPLOYEE_ROSTER_CACHE_TTL", "5"))
_employee_names_cache_lock = threading.Lock()
_employee_names_cache = {"names": None, "at": 0.0}


def _get_known_employee_names():
    """
    從安全的 EmployeeRoster 視圖查出目前所有真實員工姓名，供
    _try_deterministic_date_lookup() 判斷問題裡有沒有出現「真實存在」的員工姓名用。
    查不到（例如資料庫還沒建立視圖）就回傳空清單，不讓整個查詢流程掛掉。

    結果會被短暫快取（見上面 _EMPLOYEE_NAMES_CACHE_TTL 的說明），查詢失敗時不快取
    空清單，下一次呼叫會照樣重新查一次，不會被一次失敗卡住。
    """
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


def _try_deterministic_date_lookup(message: str):
    """
    【六/七度更新，2026-09-08】「姓名 + 具體日期」這種句型的打卡查詢，不再讓
    本地端小模型自己翻譯 SQL——實測證實小模型不會穩定照著
    few-shot 範例的規則寫（該用 `timestamp LIKE '日期%'`、該把同一天
    多筆紀錄全部列出來，小模型常常還是只挑
    `ORDER BY timestamp DESC LIMIT 1` 抓最新一筆），導致查出來的答案
    跟出題官抽到的那一筆對不上，被裁判官誤判「矛盾」。

    這裡改成：問題裡「同時」出現一個真實員工姓名、跟一個
    YYYY-MM-DD 格式的日期時，直接用寫死、保證正確的 SQL 查出
    那個人那一天『全部』的打卡紀錄，依時間排序組成一句完整
    答案（多筆就全部列出來，不會再只回最新一筆）。不符合這個
    句型（例如問「今天」而非具體日期、或是員工人數這種名冊類
    問題）就回傳 None，讓呼叫端照舊交給原本的小模型
    NLSQLTableQueryEngine 路線處理，不影響其他問題類型。

    回傳答案字串，或 None（代表這題不符合「姓名+具體日期」
    句型，不是這個函式該管的）。
    """
    date_match = _DATE_PATTERN.search(message)
    if not date_match:
        return None
    target_date = date_match.group(0)

    known_names = _get_known_employee_names()
    matched_name = None
    for name in known_names:
        if name and name in message:
            matched_name = name
            break
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
    【查無資料時可以改問法規，不要死在考勤這條路上】使用者提出的需求：與其死板地
    「二選一」判斷這題該查考勤還是查法規、猜錯就直接跟使用者說查無資料，不如考勤路線
    查不到東西的時候，順便去問問看法規那邊——說不定使用者問的其實是「規則」（例如
    「遲到扣薪合不合法」），只是句子裡剛好有「遲到」這種聽起來像考勤的字眼，被誤判了。

    參數 `engine` 是呼叫端（main_guardrail_rag.py）目前手上那個已經建好的 SQL 查詢
    引擎（build_sql_engine() 的回傳值）——刻意用參數傳進來，不是在這個模組裡自己養一份
    全域變數：查詢引擎「現在是誰」這件事，屬於整個系統的執行狀態，應該由最上層的
    main_guardrail_rag.py 統一持有，這個模組只負責「給我一個引擎，我就幫你查」。

    回傳 (是否真的查到東西, 答案文字或 None)。

    這裡「有沒有真的查到東西」的判斷方式，是直接看 SQL 實際執行後回傳的原始資料列數
    （sql_response.metadata['result']），不是去比對回答字串裡有沒有出現「查無符合條件
    的資料」這幾個字——原因是那句話本來就是我們自己寫在 _SQL_RESPONSE_PROMPT_ZH 裡、
    要求 AI 在「查出 0 筆」時要講的固定台詞，換句話說 AI 有可能換句話說、講出意思一樣
    但字面不同的句子，用字串比對來判斷「有沒有查到東西」並不可靠；用 SQL 實際查回來的
    原始資料列數判斷，才是根本不會出錯的方式。SQL 語句本身執行失敗（例如又生成出不存在
    的欄位或函式）也算「沒查到」，一樣觸發改問法規。
    """
    # 【七度更新，2026-09-08】「姓名+具體日期」這種句型，先用確定性
    # 查詢保證正確答案，不用再讓小模型冒險翻譯 SQL；查不到符合這個
    # 句型才繼續往下走原本的小模型路線。
    # 【2026-09-14，學術強化方向 1：消融實驗】預設維持正式系統既有行為（有確定性查詢）。
    # 只有消融實驗明確關掉 ablation_config.USE_DETERMINISTIC_SQL 時，才會整段跳過、
    # 全部交給下面的小模型 NLSQLTableQueryEngine 自己翻譯 SQL，藉此量化確定性查詢
    # 對答題正確率的實際貢獻（這次除錯過程已經實測過小模型常常只抓最新一筆、算錯月份
    # 範圍等問題，消融實驗可以把這個差異量化成正式數字）。
    if ablation_config.USE_DETERMINISTIC_SQL:
        deterministic_answer = _try_deterministic_date_lookup(message)
        if deterministic_answer is not None:
            return True, deterministic_answer

    if engine is None:
        return False, None
    try:
        sql_response = engine.query(message)
        metadata = getattr(sql_response, "metadata", None) or {}
        raw_rows = metadata.get("result")
        # 【2026-09-13 新增】印出小模型實際翻出來的 SQL 語句跟查詢結果，方便診斷
        # 「明明資料庫裡有紀錄，AI 卻說查無資料」這種問題——通常是小模型翻譯 SQL
        # 時條件下錯（例如欄位值猜錯、日期範圍算錯），不是資料庫真的沒有資料。
        # 只印出來、不影響任何判斷邏輯，判斷「有沒有查到東西」還是只看 raw_rows。
        print(f"  [Debug SQL] 產生的 SQL 語句: {metadata.get('sql_query')!r}")
        print(f"  [Debug SQL] SQL 查詢結果: {raw_rows!r}")
        if raw_rows:
            return True, str(sql_response)
        return False, None
    except Exception as e:
        print(f"DEBUG: SQL QueryEngine Error: {e}")
        return False, None


def sample_random_attendance_record():
    """
    隨機抽取一筆真實的考勤紀錄，供 evaluate_rag.py 的「純考勤」類自動化測驗出題使用。
    直接對資料庫做唯讀查詢，不經過向量索引（考勤資料本來就沒有被索引）。
    回傳 {'name':..., 'timestamp':..., 'status':...}，抽不到資料時回傳 None。
    """
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
