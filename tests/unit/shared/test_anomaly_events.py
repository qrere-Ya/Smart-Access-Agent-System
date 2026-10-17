"""
異常事件單元測試：事件的新增與查詢（日期範圍、類型、排序、上限）、舊證據照片的匯入（時間取自檔名、不重複匯入、
忽略無關檔案）、第一次建表時自動匯入、舊資料庫升級。
"""
import sqlite3

from shared import database_mgr as dbm


def _photos(folder, *names):
    folder.mkdir(exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"jpg")
    return str(folder)


def test_event_is_stored_with_its_details(db):
    event_id = dbm.add_anomaly_event("tailgate", "C:/x/tailgate_1.jpg", "畫面有 2 人", timestamp="2026-10-14 09:15:00")
    [event] = dbm.list_anomaly_events()
    assert event == {"id": event_id, "timestamp": "2026-10-14 09:15:00", "event_type": "tailgate",
                     "detail": "畫面有 2 人", "evidence_path": "C:/x/tailgate_1.jpg"}


def test_timestamp_defaults_to_now(db, clock):
    clock(2026, 10, 14, 9, 15, 0)
    dbm.add_anomaly_event("tailgate")
    assert dbm.list_anomaly_events()[0]["timestamp"] == "2026-10-14 09:15:00"


def test_events_are_listed_newest_first(db):
    for ts in ("2026-10-12 08:00:00", "2026-10-14 08:00:00", "2026-10-13 08:00:00"):
        dbm.add_anomaly_event("tailgate", timestamp=ts)
    assert [e["timestamp"][:10] for e in dbm.list_anomaly_events()] == ["2026-10-14", "2026-10-13", "2026-10-12"]


def test_date_range_includes_the_whole_last_day(db):
    for ts in ("2026-10-11 23:59:59", "2026-10-12 00:00:00", "2026-10-13 23:59:59", "2026-10-14 00:00:00"):
        dbm.add_anomaly_event("tailgate", timestamp=ts)
    got = dbm.list_anomaly_events(start="2026-10-12", end="2026-10-13")
    assert sorted(e["timestamp"] for e in got) == ["2026-10-12 00:00:00", "2026-10-13 23:59:59"]


def test_filter_by_type_and_limit(db):
    dbm.add_anomaly_event("tailgate", timestamp="2026-10-14 08:00:00")
    dbm.add_anomaly_event("other", timestamp="2026-10-14 09:00:00")
    dbm.add_anomaly_event("tailgate", timestamp="2026-10-14 10:00:00")
    assert [e["event_type"] for e in dbm.list_anomaly_events(event_type="tailgate")] == ["tailgate", "tailgate"]
    assert len(dbm.list_anomaly_events(limit=1)) == 1


def test_search_values_are_not_interpreted_as_sql(db):
    dbm.add_anomaly_event("tailgate", timestamp="2026-10-14 08:00:00")
    assert dbm.list_anomaly_events(event_type="tailgate' OR '1'='1") == []
    assert len(dbm.list_anomaly_events()) == 1


def test_old_photos_are_imported_with_the_time_from_the_filename(db, tmp_path):
    folder = _photos(tmp_path / "logs", "tailgate_20261014_091530.jpg")
    assert dbm.import_anomaly_photos(folder) == 1
    [event] = dbm.list_anomaly_events()
    assert event["timestamp"] == "2026-10-14 09:15:30" and event["event_type"] == "tailgate"
    assert event["evidence_path"].endswith("tailgate_20261014_091530.jpg")


def test_importing_twice_does_not_duplicate(db, tmp_path):
    folder = _photos(tmp_path / "logs", "tailgate_20261014_091530.jpg")
    dbm.import_anomaly_photos(folder)
    assert dbm.import_anomaly_photos(folder) == 0
    assert len(dbm.list_anomaly_events()) == 1


def test_photo_already_recorded_by_the_live_system_is_not_imported_again(db, tmp_path):
    folder = _photos(tmp_path / "logs", "tailgate_20261014_091530.jpg")
    dbm.add_anomaly_event("tailgate", folder + "/tailgate_20261014_091530.jpg", "即時記錄")
    assert dbm.import_anomaly_photos(folder) == 0


def test_unrelated_and_malformed_files_are_ignored(db, tmp_path):
    folder = _photos(tmp_path / "logs", "notes.txt", "tailgate_bad.jpg", "tailgate_20261399_250000.jpg",
                     "tailgate_20261014_091530.jpg")
    assert dbm.import_anomaly_photos(folder) == 1


def test_missing_folder_imports_nothing(db, tmp_path):
    assert dbm.import_anomaly_photos(str(tmp_path / "nope")) == 0


def test_first_creation_of_the_table_imports_existing_photos(tmp_path, monkeypatch):
    from shared import paths

    logs = tmp_path / "anomaly"
    _photos(logs, "tailgate_20261014_091530.jpg")
    monkeypatch.setattr(paths, "ANOMALY_DIR", str(logs))
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "fresh.db"))
    dbm.init_db()
    dbm.init_db()
    assert len(dbm.list_anomaly_events()) == 1


def test_database_without_the_table_is_upgraded(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    sqlite3.connect(path).executescript(
        "CREATE TABLE Attendance (id INTEGER PRIMARY KEY, name TEXT, timestamp DATETIME, status TEXT);"
        "INSERT INTO Attendance (name, timestamp, status) VALUES ('王小明', '2026-09-02 08:00:00', '上班');")
    monkeypatch.setattr(dbm, "DB_PATH", path)
    dbm.init_db()
    assert dbm.list_anomaly_events() == []
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM Attendance").fetchone()[0] == 1
