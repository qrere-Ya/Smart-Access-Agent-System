"""
後台聊天視窗只查出缺勤（#59）的端到端整合測試：真的 create_attendance_runtime、真的本機 Ollama（qwen3:8b）、臨時資料庫，
最後用真的聊天視窗（真 Tk）問一題。需要 Ollama 正在執行；沒有就跳過。
驗證：出缺勤題照常用資料庫回答、錯字的人名要使用者確認、問到文件會明確說明請到前台（不問語言模型、不默默失敗）、
同時問文件與出缺勤時只回出缺勤並附說明、每一題都記進對話紀錄（back_attendance）、視窗不載入任何知識庫模型。
"""
import datetime
import time

import pytest

pytestmark = [pytest.mark.integration]

TODAY = datetime.date.today()


@pytest.fixture
def runtime(db, add_user, add_attendance):
    from rag import bootstrap, chat_worker
    from rag import llm_settings as ls

    add_user("王小明", "E001")
    add_user("李大華", "E002")
    month = TODAY.strftime("%Y-%m")
    add_attendance("王小明", f"{month}-02 08:12:00", "遲到")
    add_attendance("王小明", f"{month}-03 08:31:00", "遲到")
    add_attendance("李大華", f"{TODAY.isoformat()} 08:20:00", "遲到")
    try:
        rt = bootstrap.create_attendance_runtime(progress=lambda t: None, settings={**ls.DEFAULTS, "mode": "local"})
    except RuntimeError as e:
        pytest.skip(f"本機 Ollama 沒有在執行：{e}")
    chat_worker._attach_chat_log(rt)
    yield rt
    rt.close()


def ask(runtime, question):
    return "".join(runtime.assistant.ask(question))


def test_the_runtime_has_no_knowledge_base(runtime):
    assert runtime.store is None and runtime.sync is None and runtime.assistant.docs_enabled is False


def test_an_attendance_count_comes_from_the_database(runtime):
    text = ask(runtime, "王小明這個月遲到幾次？")
    assert "遲到：2 次" in text and runtime.assistant.last.outcome == "answered"
    assert runtime.assistant.last.tools == ["count"] and runtime.assistant.last.hits == []


def test_who_was_late_today(runtime):
    text = ask(runtime, "今天誰遲到？")
    assert "李大華" in text and "王小明" not in text.split("【查詢結果】")[-1].split("\n\n")[0]


def test_a_misspelled_name_is_confirmed_not_silently_swapped(runtime):
    text = ask(runtime, "王小名這個月遲到幾次？")
    assert "王小明" in text and "你是指" in text and runtime.assistant.last.outcome == "clarify"


def test_an_unknown_person_is_reported(runtime):
    text = ask(runtime, "張無忌這個月遲到幾次？")
    assert "找不到" in text and runtime.assistant.last.outcome == "clarify"


@pytest.mark.parametrize("question", ["特別休假工作滿一年有幾天？", "颱風天公司要放假嗎？", "加班費怎麼計算？"])
def test_a_document_question_is_explained_instead_of_answered(runtime, question):
    from rag import answer

    text = ask(runtime, question)
    assert text == answer.DOCS_MOVED_TEXT and "前台" in text
    assert runtime.assistant.last.outcome == "docs_moved" and runtime.assistant.last.hits == []


def test_a_mixed_question_gets_only_the_attendance_part_with_a_note(runtime):
    text = ask(runtime, "王小明這個月遲到幾次？依照勞基法公司可以怎麼處理？")
    assert "遲到：2 次" in text and "請到前台的「知識庫問答」查詢" in text
    assert runtime.assistant.last.hits == []


def test_every_question_lands_in_the_chat_log_as_back_attendance(runtime):
    from shared import chat_log

    ask(runtime, "王小明這個月遲到幾次？")
    ask(runtime, "特別休假工作滿一年有幾天？")
    rows = chat_log.list_logs(source=chat_log.SOURCE_BACK)
    assert len(rows) == 2
    docs_row = next(chat_log.get_log(r["id"]) for r in rows if "特別休假" in r["question"])
    assert docs_row["answer"].startswith("📚 文件與法規的問答已經移到前台") and docs_row["citations"] == []
    count_row = next(chat_log.get_log(r["id"]) for r in rows if "遲到幾次" in r["question"])
    assert any("出缺勤查詢" in c["title"] for c in count_row["citations"])


def test_the_real_window_opens_with_the_attendance_runtime_and_answers_a_question(db, add_user, add_attendance, root):
    import customtkinter as ctk

    from rag import chat_window

    add_user("李大華", "E002")
    add_attendance("李大華", f"{TODAY.isoformat()} 08:20:00", "遲到")
    window = chat_window.AssistantWindow(auto_start_ollama=False)
    try:
        end = time.time() + 120
        while time.time() < end and window._controller is None and window.status.cget("text") != "🔴 啟動失敗":
            window.update()
            time.sleep(0.05)
        if window._controller is None:
            pytest.skip("本機 Ollama 沒有在執行")
        assert "出缺勤查詢" in window.status.cget("text") and window.title() == "Smart_Query 出缺勤查詢"
        assert window.send("今天誰遲到？") is True
        end = time.time() + 120
        while time.time() < end and not any("李大華" in b.get_text() for b in window._bubbles if b.role != "user"):
            window.update()
            time.sleep(0.05)
        assert any("李大華" in b.get_text() for b in window._bubbles if b.role != "user")
        assert isinstance(window, ctk.CTk)
    finally:
        window._on_close()
