"""
rag_sql_engine 單元測試：
  - SafeSQLDatabase 的白名單（只准單一 SELECT、只准查指定資料表）
  - 員工姓名比對（精確、錯字、多位候選人時放棄）
  - 三種確定性查詢（姓名 + 日期、姓名 + 遲到/早退、姓名 + 打卡紀錄）
  - try_attendance_answer 的查詢順序與「是否真的查到資料」的判斷
  - 對 LLM 開放的資料表不含人臉特徵、連線為唯讀
"""
import sqlite3

import pytest
from llama_index.core import Settings
from llama_index.core.embeddings import MockEmbedding
from llama_index.core.llms import MockLLM
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from rag import rag_sql_engine as rs


@pytest.fixture
def safe_db():
    engine = create_engine("sqlite://")
    with engine.begin() as con:
        con.execute(text("CREATE TABLE Attendance (id INTEGER PRIMARY KEY, name TEXT, timestamp TEXT, status TEXT)"))
        con.execute(text("CREATE TABLE Users (id INTEGER PRIMARY KEY, name TEXT, embedding BLOB)"))
        con.execute(text("INSERT INTO Attendance (name, timestamp, status) "
                         "VALUES ('王小明', '2026-09-02 08:10:00', '遲到')"))
    return rs.SafeSQLDatabase(engine, include_tables=["Attendance"])


@pytest.fixture
def mock_llm():
    """建立引擎時 llama-index 會讀全域的 LLM 與 embedding 設定，正式流程一定先設好，測試用假的取代。"""
    old_llm, old_embed = Settings._llm, Settings._embed_model
    Settings.llm = MockLLM()
    Settings.embed_model = MockEmbedding(embed_dim=8)
    yield
    Settings._llm, Settings._embed_model = old_llm, old_embed


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM Attendance",
    "select * from ATTENDANCE",
    "SELECT name FROM Attendance WHERE status = '遲到'",
    "SELECT name FROM Attendance WHERE id IN (SELECT id FROM Attendance)",
    "SELECT 1",
])
def test_safe_sql_allows_select_on_whitelisted_tables(safe_db, sql):
    _, meta = safe_db.run_sql(sql)
    assert meta["result"]


def test_safe_sql_returns_the_real_rows(safe_db):
    text_result, meta = safe_db.run_sql("SELECT name, status FROM Attendance")
    assert meta["result"] == [("王小明", "遲到")]
    assert "王小明" in text_result


@pytest.mark.parametrize("sql", [
    "DROP TABLE Attendance",
    "DELETE FROM Attendance",
    "UPDATE Attendance SET status = '上班'",
    "INSERT INTO Attendance (name) VALUES ('x')",
    "PRAGMA table_info(Users)",
    "ATTACH DATABASE 'other.db' AS other",
])
def test_safe_sql_rejects_anything_that_is_not_a_select(safe_db, sql):
    with pytest.raises(ValueError):
        safe_db.run_sql(sql)


def test_safe_sql_rejects_multiple_statements(safe_db):
    with pytest.raises(ValueError, match="單一"):
        safe_db.run_sql("SELECT 1; DROP TABLE Attendance")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM Users",
    "SELECT embedding FROM users",
    "SELECT * FROM llm_usage_log",
    "SELECT name FROM Attendance WHERE name IN (SELECT name FROM Users)",
    "SELECT a.name FROM Attendance a JOIN Users u ON a.name = u.name",
])
def test_safe_sql_rejects_tables_outside_the_whitelist(safe_db, sql):
    with pytest.raises(ValueError, match="白名單"):
        safe_db.run_sql(sql)


def test_safe_sql_rejects_sql_that_cannot_be_parsed(safe_db):
    with pytest.raises(ValueError):
        safe_db.run_sql("SELECT * FROM (")


def test_safe_sql_rejects_cte_aliases_conservatively(safe_db):
    with pytest.raises(ValueError, match="白名單"):
        safe_db.run_sql("WITH x AS (SELECT * FROM Attendance) SELECT * FROM x")


NAMES = ["陳柏豫", "葉君緯"]


def test_name_match_exact():
    assert rs._match_known_employee_name("陳柏豫今天有打卡嗎", NAMES) == "陳柏豫"


def test_name_match_tolerates_one_wrong_character():
    assert rs._match_known_employee_name("陳柏諭有無遲到", NAMES) == "陳柏豫"
    assert rs._match_known_employee_name("葉君為有無遲到", NAMES) == "葉君緯"


def test_name_match_gives_up_when_two_names_are_equally_close():
    assert rs._match_known_employee_name("陳柏與遲到了嗎", ["陳柏豫", "陳柏羽"]) is None


def test_name_match_does_not_confuse_people_two_characters_apart():
    """差兩個字就視為不同的人：寧可查不到，也不能把 A 的紀錄回答給 B。"""
    assert rs._match_known_employee_name("陳小羽今天遲到了嗎", ["陳柏豫"]) is None
    assert rs._EMPLOYEE_NAME_FUZZY_MAX_DISTANCE == 1


def test_name_match_prefers_the_exact_name_over_a_close_one():
    assert rs._match_known_employee_name("陳柏羽遲到了嗎", ["陳柏豫", "陳柏羽"]) == "陳柏羽"


@pytest.mark.parametrize("message, names", [
    ("今天天氣很好", NAMES), ("陳柏豫", []), ("陳柏豫", ["", None]),
])
def test_name_match_returns_none_when_nobody_matches(message, names):
    assert rs._match_known_employee_name(message, names) is None


def current_month(db):
    con = sqlite3.connect(db)
    month = con.execute("SELECT strftime('%Y-%m', 'now')").fetchone()[0]
    con.close()
    return month


def test_date_lookup_single_record(rag_db, add_user, add_attendance):
    add_user("陳柏豫", "E001")
    add_attendance("陳柏豫", "2026-09-03 07:55:00", "上班")
    assert rs._try_deterministic_date_lookup("陳柏豫2026-09-03的打卡紀錄") == \
        "陳柏豫在2026-09-03的打卡時間為 07:55:00，系統判定狀態為上班。"


def test_date_lookup_lists_every_record_of_that_day(rag_db, add_user, add_attendance):
    add_user("陳柏豫", "E001")
    add_attendance("陳柏豫", "2026-09-02 07:50:00", "上班")
    add_attendance("陳柏豫", "2026-09-02 17:45:00", "下班")
    assert rs._try_deterministic_date_lookup("陳柏豫2026-09-02幾點打卡") == \
        "陳柏豫在2026-09-02共有 2 筆打卡紀錄：07:50:00（上班）、17:45:00（下班）。"


def test_date_lookup_reports_missing_records_and_handles_typos(rag_db, add_user):
    add_user("陳柏豫", "E001")
    assert rs._try_deterministic_date_lookup("陳柏諭2026-09-05有打卡嗎") == "查無陳柏豫在2026-09-05的打卡紀錄。"


@pytest.mark.parametrize("message", ["陳柏豫今天有打卡嗎", "2026-09-05有誰打卡", "路人甲2026-09-05有打卡嗎"])
def test_date_lookup_ignores_messages_it_cannot_answer(rag_db, add_user, message):
    add_user("陳柏豫", "E001")
    assert rs._try_deterministic_date_lookup(message) is None


def test_status_lookup_lists_all_matching_records(rag_db, add_user, add_attendance):
    add_user("陳柏豫", "E001")
    add_attendance("陳柏豫", "2020-01-05 08:10:00", "遲到")
    add_attendance("陳柏豫", f"{current_month(rag_db)}-02 08:20:00", "遲到")
    add_attendance("陳柏豫", f"{current_month(rag_db)}-03 07:55:00", "上班")
    answer = rs._try_deterministic_status_lookup("陳柏豫有無遲到紀錄")
    assert answer == f"陳柏豫共有 2 次遲到紀錄：2020-01-05 08:10:00、{current_month(rag_db)}-02 08:20:00。"


def test_status_lookup_limits_to_this_month_when_asked(rag_db, add_user, add_attendance):
    add_user("陳柏豫", "E001")
    add_attendance("陳柏豫", "2020-01-05 08:10:00", "遲到")
    add_attendance("陳柏豫", f"{current_month(rag_db)}-02 08:20:00", "遲到")
    answer = rs._try_deterministic_status_lookup("陳柏豫這個月遲到幾次")
    assert answer == f"陳柏豫這個月共有 1 次遲到紀錄：{current_month(rag_db)}-02 08:20:00。"


def test_status_lookup_reports_none_found(rag_db, add_user):
    add_user("陳柏豫", "E001")
    assert rs._try_deterministic_status_lookup("陳柏豫有早退嗎") == "陳柏豫沒有早退紀錄。"
    assert rs._try_deterministic_status_lookup("陳柏豫這個月遲到幾次") == "陳柏豫這個月沒有遲到紀錄。"


def test_status_lookup_needs_both_a_status_word_and_a_real_name(rag_db, add_user):
    add_user("陳柏豫", "E001")
    assert rs._try_deterministic_status_lookup("陳柏豫今天有打卡嗎") is None
    assert rs._try_deterministic_status_lookup("員工遲到可以扣薪嗎") is None


def test_record_count_lookup_with_typos_in_name_and_keyword(rag_db, add_user, add_attendance):
    add_user("周宇祥", "E002")
    add_attendance("周宇祥", "2026-09-02 07:50:00", "上班")
    add_attendance("周宇祥", "2026-09-02 17:45:00", "下班")
    assert rs._try_deterministic_record_count_lookup("周宇翔是否有打考紀錄") == "周宇祥共有 2 筆打卡紀錄。"


def test_record_count_lookup_when_there_are_no_records(rag_db, add_user):
    add_user("周宇祥", "E002")
    assert rs._try_deterministic_record_count_lookup("周宇祥有出勤紀錄嗎") == "周宇祥目前沒有打卡紀錄。"
    assert rs._try_deterministic_record_count_lookup("周宇祥吃飯了嗎") is None


class _Resp:
    def __init__(self, rows):
        self.metadata = {"result": rows, "sql_query": "SELECT 1"}

    def __str__(self):
        return "引擎的答案"


class _Engine:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows, error, 0

    def query(self, message):
        self.calls += 1
        if self.error:
            raise self.error
        return _Resp(self.rows)


def test_deterministic_answers_are_used_before_the_language_model(rag_db, add_user, add_attendance):
    add_user("陳柏豫", "E001")
    add_attendance("陳柏豫", "2026-09-03 07:55:00", "上班")
    engine = _Engine(error=AssertionError("不該呼叫 LLM 引擎"))
    ok, answer = rs.try_attendance_answer(engine, "陳柏豫2026-09-03的打卡紀錄")
    assert ok and "07:55:00" in answer and engine.calls == 0


def test_the_date_lookup_wins_over_the_status_lookup(rag_db, add_user, add_attendance):
    add_user("陳柏豫", "E001")
    add_attendance("陳柏豫", "2026-09-03 08:20:00", "遲到")
    ok, answer = rs.try_attendance_answer(None, "陳柏豫2026-09-03有遲到嗎")
    assert ok and answer.startswith("陳柏豫在2026-09-03的打卡時間為")


def test_engine_answer_is_used_when_rows_come_back(rag_db):
    ok, answer = rs.try_attendance_answer(_Engine(rows=[(3,)]), "公司有多少員工")
    assert (ok, answer) == (True, "引擎的答案")


@pytest.mark.parametrize("engine", [_Engine(rows=[]), _Engine(error=RuntimeError("boom")), None])
def test_no_rows_or_errors_mean_nothing_was_found(rag_db, engine):
    assert rs.try_attendance_answer(engine, "公司有多少員工") == (False, None)


def test_deterministic_queries_can_be_switched_off(rag_db, add_user, add_attendance, monkeypatch):
    add_user("陳柏豫", "E001")
    add_attendance("陳柏豫", "2026-09-03 07:55:00", "上班")
    monkeypatch.setattr(rs.ablation_config, "USE_DETERMINISTIC_SQL", False)
    assert rs.try_attendance_answer(None, "陳柏豫2026-09-03的打卡紀錄") == (False, None)


def test_known_employee_names_are_cached_for_a_short_time(rag_db, add_user):
    add_user("陳柏豫", "E001")
    assert rs._get_known_employee_names() == ["陳柏豫"]
    add_user("葉君緯", "E002")
    assert rs._get_known_employee_names() == ["陳柏豫"]
    rs._employee_names_cache["at"] = 0.0
    assert sorted(rs._get_known_employee_names()) == ["葉君緯", "陳柏豫"]


def test_known_employee_names_degrade_to_empty_list(tmp_path, monkeypatch):
    monkeypatch.setitem(rs._employee_names_cache, "names", None)
    monkeypatch.setattr(rs, "ACCESS_SYSTEM_DB_PATH", str(tmp_path / "missing.db"))
    assert rs._get_known_employee_names() == []
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    monkeypatch.setattr(rs, "ACCESS_SYSTEM_DB_PATH", str(empty))
    assert rs._get_known_employee_names() == []


def test_sample_random_attendance_record(rag_db, add_attendance, monkeypatch, tmp_path):
    assert rs.sample_random_attendance_record() is None
    add_attendance("陳柏豫", "2026-09-03 07:55:00", "上班")
    assert rs.sample_random_attendance_record() == {
        "name": "陳柏豫", "timestamp": "2026-09-03 07:55:00", "status": "上班"}
    monkeypatch.setattr(rs, "ACCESS_SYSTEM_DB_PATH", str(tmp_path / "missing.db"))
    assert rs.sample_random_attendance_record() is None


def test_build_sql_engine_returns_none_without_a_database(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "ACCESS_SYSTEM_DB_PATH", str(tmp_path / "missing.db"))
    assert rs.build_sql_engine() is None


def database_of(engine):
    """取出引擎實際使用的資料庫物件（llama-index 的私有屬性，升級後若改名只需修改這裡）。"""
    return engine.sql_retriever._sql_database


def test_llm_sees_only_safe_tables(rag_db, add_user, mock_llm):
    add_user("陳柏豫", "E001")
    database = database_of(rs.build_sql_engine())
    assert isinstance(database, rs.SafeSQLDatabase)
    assert sorted(database.get_usable_table_names()) == ["Attendance", "EmployeeRoster", "calendar"]
    with pytest.raises(ValueError, match="白名單"):
        database.run_sql("SELECT * FROM Users")
    with pytest.raises((SQLAlchemyError, NotImplementedError), match="no such column: embedding"):
        database.run_sql("SELECT embedding FROM EmployeeRoster")
    _, meta = database.run_sql("SELECT name, employee_id FROM EmployeeRoster")
    assert meta["result"] == [("陳柏豫", "E001")]


def test_the_database_connection_given_to_the_llm_is_read_only(rag_db, mock_llm):
    database = database_of(rs.build_sql_engine())
    with pytest.raises(SQLAlchemyError, match="readonly"):
        with database.engine.begin() as con:
            con.execute(text("INSERT INTO Attendance (name, timestamp, status) VALUES ('x', 'y', 'z')"))
