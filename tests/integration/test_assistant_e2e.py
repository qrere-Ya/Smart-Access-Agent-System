"""
管理者助理端到端整合測試：真的 bge-m3、重排模型、本機 Ollama（qwen2:7b）、真的文件與臨時資料庫。
需要 Ollama 正在執行；沒有的話跳過。LLM 用溫度 0，但說明文字仍可能小幅變動，所以只檢查關鍵事實與結構。
"""
import datetime
import os
import sqlite3

import numpy as np
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_models")]

TODAY = datetime.date(2026, 10, 17)
ROSTER = [("王小明", "E001"), ("李大華", "E002"), ("葉君緯", "E003"), ("陳志遠", "E004")]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    from assistant import llm as llm_mod
    from assistant.doc_store import DocStore
    from assistant.rerank import Reranker
    from rag import llm_settings as ls
    from rag.rag_resources import ManagedHFEmbedding
    from shared import paths

    rerank_dir = os.path.join(paths.MODELS_DIR, "bge-reranker-v2-m3")
    docs = os.path.join(paths.PROJECT_ROOT, "data", "laws")
    if not os.path.isdir(rerank_dir) or not os.path.isdir(docs) or not os.listdir(docs):
        pytest.skip("缺少重排模型或文件資料夾")
    model, note = llm_mod.build_llm({**ls.DEFAULTS, "mode": "local"})
    router_llm, _ = llm_mod.build_llm({**ls.DEFAULTS, "mode": "local"}, max_tokens=200)
    if model is None:
        pytest.skip("本機 Ollama 沒有在執行：" + note)
    embedding = ManagedHFEmbedding(os.path.join(paths.MODELS_DIR, "bge-m3"), device="cuda")

    def embed(texts):
        return np.array(embedding.get_text_embedding_batch(list(texts)))

    store = DocStore(docs, str(tmp_path_factory.mktemp("store")), embed)
    store.sync()
    reranker = Reranker.from_model_dir(rerank_dir)
    yield {"store": store, "reranker": reranker, "llm": model, "router_llm": router_llm, "embed": embed}
    reranker.release()
    embedding.release()


@pytest.fixture
def assistant(stack, db):
    from assistant import answer
    from assistant.entity_resolver import NameResolver

    con = sqlite3.connect(db)
    for name, emp in ROSTER:
        con.execute("INSERT INTO Users (name, gender, employee_id, embedding, role, is_active) VALUES (?,?,?,?,?,1)",
                    (name, "M", emp, np.ones(4, dtype=np.float32), "employee"))
    for ts, status in [("2026-10-13 08:12:00", "遲到"), ("2026-10-14 08:30:00", "遲到"),
                       ("2026-10-16 07:55:00", "上班"), ("2026-10-17 08:20:00", "遲到")]:
        con.execute("INSERT INTO Attendance (name, timestamp, status) VALUES ('王小明', ?, ?)", (ts, status))
    con.commit()
    con.close()
    from shared import database_mgr as dbm

    dbm.add_anomaly_event("tailgate", r"C:\runtime\anomaly_logs\tailgate_20261016_091500.jpg", "畫面有 2 人",
                          timestamp="2026-10-16 09:15:00")
    resolver = NameResolver([n for n, _ in ROSTER], stack["embed"])
    return answer.Assistant(stack["store"], stack["reranker"], stack["llm"], resolver,
                            router_llm=stack["router_llm"], today=lambda: TODAY)


def ask(assistant, question):
    return "".join(assistant.ask(question))


def test_leave_question_is_answered_from_the_labour_law_with_sources(assistant):
    text = ask(assistant, "特別休假的規定是什麼？")
    assert "📎 出處" in text and "勞動基準法" in text and "第38條" in text
    assert assistant.last.hits[0].chunk["article"] == "第38條"


def test_attendance_count_is_exact_and_the_name_is_matched(assistant):
    text = ask(assistant, "王小明這個月遲到幾次？")
    assert "遲到：3 次" in text and assistant.last.plan.data == "count"


def test_follow_up_question_keeps_the_same_person(assistant):
    ask(assistant, "王小明這個月遲到幾次？")
    text = ask(assistant, "那上週呢？")
    assert "王小明" in text and "2026-10-05 至 2026-10-11" in text


def test_who_was_late_today(assistant):
    text = ask(assistant, "今天誰遲到？")
    assert "2026-10-17 08:20:00" in text and "王小明" in text


def test_tailgate_events_show_the_file_path_and_no_picture(assistant):
    text = ask(assistant, "最近有沒有尾隨事件？")
    assert "tailgate_20261016_091500.jpg" in text and "不在此顯示照片" in text


def test_misspelled_name_is_confirmed_not_silently_swapped(assistant):
    text = ask(assistant, "葉軍偉這個月遲到幾次")
    assert "你是指「葉君緯」嗎" in text or "已視為「葉君緯」" in text


def test_unknown_person_is_reported(assistant):
    text = ask(assistant, "張三這個月遲到幾次")
    assert "找不到" in text


def test_question_outside_the_documents_is_not_made_up(assistant):
    text = ask(assistant, "個人綜合所得稅的免稅額是多少？")
    assert assistant.last.outcome in ("no_basis", "answered")
    assert "沒有提到" in text or "找不到" in text
