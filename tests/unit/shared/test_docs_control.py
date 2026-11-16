"""
docs_control 的單元測試：重新掃描請求與結果檔的讀寫（壞檔當作沒有）、只處理比 since 新的請求、文件資料夾的份數
（只算 PDF、Word、txt、md，不算暫存檔與別的類型）、結果說明有長度上限。
"""
import json
import time

import pytest

from shared import docs_control as dc
from shared import paths


@pytest.fixture(autouse=True)
def _files(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DOCS_REFRESH_PATH", str(tmp_path / "req" / "refresh.json"))
    monkeypatch.setattr(paths, "DOCS_STATUS_PATH", str(tmp_path / "req" / "status.json"))


def test_a_request_is_seen_once_and_only_when_newer_than_what_was_handled():
    assert dc.pending(0.0) is None
    stamp = dc.request_refresh(now=1000.0)
    assert stamp == 1000.0 and dc.pending(0.0) == 1000.0
    assert dc.pending(1000.0) is None and dc.pending(999.0) == 1000.0
    dc.request_refresh(now=2000.0)
    assert dc.pending(1000.0) == 2000.0


@pytest.mark.parametrize("content", ["", "壞掉", "[]", '{"requested_at": "x"}', '{"requested_at": true}', "null"])
def test_a_damaged_request_file_is_ignored(content):
    dc.request_refresh(now=1.0)
    with open(paths.DOCS_REFRESH_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    assert dc.pending(0.0) is None


def test_the_status_round_trips_and_is_capped():
    assert dc.read_status() is None
    dc.write_status("📚 文件已重新掃描　新增：新文件.txt", now=time.mktime((2026, 11, 16, 10, 30, 0, 0, 0, -1)))
    status = dc.read_status()
    assert status == {"time": "2026-11-16 10:30:00", "text": "📚 文件已重新掃描　新增：新文件.txt"}
    dc.write_status("字" * 2000)
    assert len(dc.read_status()["text"]) == 500


@pytest.mark.parametrize("content", ["", "壞掉", "[]", '{"time": 5, "text": "x"}', '{"time": "t"}'])
def test_a_damaged_status_file_means_no_status(content):
    dc.write_status("x")
    with open(paths.DOCS_STATUS_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    assert dc.read_status() is None


def test_the_folder_summary_counts_only_supported_documents(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in ("勞動基準法.pdf", "手冊.DOCX", "規定.txt", "說明.md", "圖.png", "~$暫存.docx", "程式.py"):
        (docs / name).write_text("x", encoding="utf-8")
    count, folder = dc.folder_summary(str(docs))
    assert count == 4 and folder == str(docs)
    assert dc.folder_summary(str(tmp_path / "missing"))[0] == 0


def test_the_request_file_is_valid_json_written_atomically():
    dc.request_refresh(now=5.0)
    with open(paths.DOCS_REFRESH_PATH, encoding="utf-8") as f:
        assert json.load(f) == {"requested_at": 5.0}
    import os

    assert not [n for n in os.listdir(os.path.dirname(paths.DOCS_REFRESH_PATH)) if n.endswith(".tmp")]
