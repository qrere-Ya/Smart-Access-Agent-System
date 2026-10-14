"""
ablation_config 與 rag_index 的單元測試：環境變數開關的解讀、法規資料夾指紋的變動偵測。
"""
import os

import pytest

from rag import ablation_config as ac
from rag import rag_index


@pytest.mark.parametrize("value, expected", [
    ("0", False), ("false", False), ("NO", False), ("off", False), ("", False), ("  ", False),
    ("1", True), ("true", True), ("yes", True), ("anything", True),
])
def test_env_bool_values(monkeypatch, value, expected):
    monkeypatch.setenv("SMART_TEST_FLAG", value)
    assert ac._env_bool("SMART_TEST_FLAG", True) is expected


@pytest.mark.parametrize("default", [True, False])
def test_env_bool_uses_the_default_when_unset(monkeypatch, default):
    monkeypatch.delenv("SMART_TEST_FLAG", raising=False)
    assert ac._env_bool("SMART_TEST_FLAG", default) is default


def test_current_config_reports_all_four_switches():
    cfg = ac.current_config()
    assert set(cfg) == {"use_long_context_reorder", "use_semantic_guardrail",
                        "use_keyword_fastpath", "use_deterministic_sql"}
    assert all(isinstance(v, bool) for v in cfg.values())


def test_fingerprint_of_a_missing_folder(tmp_path):
    assert rag_index._compute_law_dir_fingerprint(str(tmp_path / "nope")) == "no_law_dir"


def test_fingerprint_is_stable_and_differs_from_the_missing_marker(tmp_path):
    (tmp_path / "a.txt").write_text("第一條", encoding="utf-8")
    first = rag_index._compute_law_dir_fingerprint(str(tmp_path))
    assert first == rag_index._compute_law_dir_fingerprint(str(tmp_path))
    assert first != "no_law_dir"
    assert rag_index._compute_law_dir_fingerprint(str(tmp_path / "x")) != first


def test_fingerprint_changes_when_files_are_added_edited_or_removed(tmp_path):
    base = rag_index._compute_law_dir_fingerprint(str(tmp_path))
    law = tmp_path / "labor.txt"
    law.write_text("第一條", encoding="utf-8")
    added = rag_index._compute_law_dir_fingerprint(str(tmp_path))
    assert added != base
    law.write_text("第一條，已修訂", encoding="utf-8")
    edited = rag_index._compute_law_dir_fingerprint(str(tmp_path))
    assert edited != added
    law.unlink()
    assert rag_index._compute_law_dir_fingerprint(str(tmp_path)) == base


def test_fingerprint_changes_when_only_the_modification_time_changes(tmp_path):
    law = tmp_path / "labor.txt"
    law.write_text("第一條", encoding="utf-8")
    before = rag_index._compute_law_dir_fingerprint(str(tmp_path))
    stat = law.stat()
    os.utime(law, (stat.st_atime, stat.st_mtime + 100))
    assert rag_index._compute_law_dir_fingerprint(str(tmp_path)) != before


def test_fingerprint_includes_subfolders(tmp_path):
    (tmp_path / "sub").mkdir()
    before = rag_index._compute_law_dir_fingerprint(str(tmp_path))
    (tmp_path / "sub" / "x.txt").write_text("內容", encoding="utf-8")
    assert rag_index._compute_law_dir_fingerprint(str(tmp_path)) != before
