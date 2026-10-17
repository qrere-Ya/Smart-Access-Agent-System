"""
doc_store 單元測試：逐份文件同步（新增、修改、刪除、沒變不重算）、讀不出來的文件保留舊內容、重新載入後狀態一致、
混合檢索（語意加關鍵字）、空庫與沒有重疊字詞的查詢。向量用假的字元雙連詞雜湊，不需要模型。
"""
import zlib

import numpy as np
import pytest

from rag.doc_store import DocStore

DIM = 64


def fake_embed(texts):
    """字元雙連詞雜湊成固定長度向量：內容相近的文字向量相近，不需要真的模型。"""
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for row, text in enumerate(texts):
        for i in range(len(text) - 1):
            out[row, zlib.crc32(text[i:i + 2].encode()) % DIM] += 1.0
    return out


@pytest.fixture
def docs(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    return folder


@pytest.fixture
def make_store(tmp_path, docs):
    def _make(embed=fake_embed):
        return DocStore(str(docs), str(tmp_path / "store"), embed, dim=DIM)
    return _make


def put(docs, name, text):
    path = docs / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


LEAVE = "特別休假\n\n勞工繼續工作滿一年者，每年給予七天特別休假。"
OVERTIME = "加班規定\n\n延長工作時間者，雇主應加給工資三分之一以上。"


def test_new_documents_are_added_and_searchable(docs, make_store):
    put(docs, "leave.txt", LEAVE)
    put(docs, "overtime.md", OVERTIME)
    store = make_store()
    report = store.sync()
    assert sorted(report.added) == ["leave.txt", "overtime.md"] and not report.failed
    assert store.documents == ["leave.txt", "overtime.md"] and len(store) == 2
    assert store.search("特別休假幾天", 1)[0].chunk["source"] == "leave.txt"
    assert store.search("加給工資", 1)[0].chunk["source"] == "overtime.md"


def test_unchanged_documents_are_not_embedded_again(docs, make_store):
    put(docs, "leave.txt", LEAVE)
    calls = []

    def counting(texts):
        calls.append(len(texts))
        return fake_embed(texts)

    store = make_store(counting)
    store.sync()
    before = len(calls)
    report = store.sync()
    assert report.unchanged == ["leave.txt"] and not report.changed
    assert len(calls) == before


def test_only_the_modified_document_is_rebuilt(docs, make_store):
    put(docs, "a.txt", LEAVE)
    put(docs, "b.txt", OVERTIME)
    store = make_store()
    store.sync()
    put(docs, "a.txt", "特別休假\n\n勞工繼續工作滿一年者，每年給予十天特別休假。")
    report = store.sync()
    assert report.updated == ["a.txt"] and report.unchanged == ["b.txt"]
    assert len(store) == 2
    assert "十天" in store.search("特別休假", 1)[0].chunk["body"]


def test_removed_documents_disappear_from_search(docs, make_store):
    path = put(docs, "leave.txt", LEAVE)
    put(docs, "overtime.md", OVERTIME)
    store = make_store()
    store.sync()
    path.unlink()
    report = store.sync()
    assert report.removed == ["leave.txt"] and store.documents == ["overtime.md"]
    assert all(h.chunk["source"] == "overtime.md" for h in store.search("特別休假", 5))


def test_state_survives_a_restart(docs, make_store):
    put(docs, "leave.txt", LEAVE)
    make_store().sync()
    reopened = make_store()
    assert reopened.documents == ["leave.txt"] and len(reopened) == 1
    assert reopened.search("特別休假", 1)[0].chunk["source"] == "leave.txt"
    assert not reopened.sync().changed


def test_chunk_ids_are_not_reused_after_removal(docs, make_store):
    put(docs, "a.txt", LEAVE)
    store = make_store()
    store.sync()
    first_ids = set(store._chunks)
    put(docs, "a.txt", OVERTIME)
    store.sync()
    assert first_ids.isdisjoint(store._chunks)


def test_unreadable_new_document_is_reported_and_others_still_load(docs, make_store):
    put(docs, "good.txt", LEAVE)
    (docs / "empty.txt").write_text("   ", encoding="utf-8")
    (docs / "broken.docx").write_bytes(b"not a zip")
    report = make_store().sync()
    assert report.added == ["good.txt"] and set(report.failed) == {"empty.txt", "broken.docx"}


def test_document_that_becomes_unreadable_keeps_its_previous_content(docs, make_store):
    path = put(docs, "leave.txt", LEAVE)
    store = make_store()
    store.sync()
    path.write_text("   ", encoding="utf-8")
    report = store.sync()
    assert "leave.txt" in report.failed and not report.changed
    assert store.search("特別休假", 1)[0].chunk["source"] == "leave.txt"
    path.write_text(LEAVE, encoding="utf-8")
    assert store.sync().unchanged == ["leave.txt"]


def test_subfolders_are_scanned_and_temp_or_unsupported_files_ignored(docs, make_store):
    put(docs, "hr/leave.txt", LEAVE)
    put(docs, "~$lock.docx", "x")
    put(docs, ".hidden.txt", "x")
    put(docs, "photo.png", "x")
    report = make_store().sync()
    assert report.added == ["hr/leave.txt"] and not report.failed


def test_missing_documents_folder_is_an_empty_store(tmp_path):
    store = DocStore(str(tmp_path / "nope"), str(tmp_path / "store"), fake_embed, dim=DIM)
    assert not store.sync().changed and store.search("任何問題") == []


def test_empty_query_and_empty_store_return_nothing(docs, make_store):
    store = make_store()
    assert store.search("特別休假") == []
    put(docs, "leave.txt", LEAVE)
    store.sync()
    assert store.search("   ") == []


def test_keyword_only_match_is_found_and_gets_a_similarity(docs, make_store):
    put(docs, "a.txt", "甲部門的規定：所有人員須佩戴識別證進入廠區。")
    put(docs, "b.txt", "乙部門的規定：訪客須先登記才能進入大樓。" * 3)
    store = make_store()
    store.sync()
    hit = store.search("識別證", 1)[0]
    assert hit.chunk["source"] == "a.txt" and hit.dense is not None


def test_query_with_unknown_words_does_not_crash(docs, make_store):
    put(docs, "leave.txt", LEAVE)
    store = make_store()
    store.sync()
    assert isinstance(store.search("zzzqqq xyzabc"), list)


def test_k_limits_the_number_of_results(docs, make_store):
    for i in range(5):
        put(docs, f"d{i}.txt", f"文件{i}\n\n這是第{i}份文件的內容，講到特別休假。")
    store = make_store()
    store.sync()
    assert len(store.search("特別休假", 3)) == 3


def test_results_include_the_source_label_and_position(docs, make_store):
    put(docs, "law.txt", "法規\n\n" + "\n".join(f"第{i}條\n第{i}條的內容。" for i in (1, 2, 3)))
    store = make_store()
    store.sync()
    hit = store.search("第2條的內容", 1)[0]
    assert hit.label == "law 第2條" and hit.chunk["source"] == "law.txt"


def test_store_with_a_different_dimension_is_ignored_not_crashed(docs, tmp_path):
    put(docs, "leave.txt", LEAVE)
    DocStore(str(docs), str(tmp_path / "store"), fake_embed, dim=DIM).sync()
    other = DocStore(str(docs), str(tmp_path / "store"), lambda t: np.ones((len(t), 32)), dim=32)
    assert len(other) == 0 and other.sync().added == ["leave.txt"]


def test_corrupt_store_files_start_a_fresh_store(docs, tmp_path):
    put(docs, "leave.txt", LEAVE)
    store_dir = tmp_path / "store"
    DocStore(str(docs), str(store_dir), fake_embed, dim=DIM).sync()
    (store_dir / "manifest.json").write_text("{broken", encoding="utf-8")
    store = DocStore(str(docs), str(store_dir), fake_embed, dim=DIM)
    assert len(store) == 0 and store.sync().added == ["leave.txt"]


@pytest.mark.parametrize("keyword, expected", [("打掃", "a.txt"), ("開會", "b.txt"), ("識別證", "c.txt")])
def test_keywords_alone_can_pull_the_right_document_up(docs, make_store, keyword, expected):
    """向量完全分不出差別（全部相同）時，含有該詞的文件要排第一，不論它在清單的哪個位置。"""
    for name, text in [("a.txt", "甲部門規定：每日打掃環境。"), ("b.txt", "乙部門規定：每週開會一次。"),
                       ("c.txt", "丙部門規定：離職須交接識別證與鑰匙。")]:
        put(docs, name, text)
    store = make_store(lambda texts: np.ones((len(texts), DIM), dtype=np.float32))
    store.sync()
    assert store.search(keyword, 1)[0].chunk["source"] == expected
