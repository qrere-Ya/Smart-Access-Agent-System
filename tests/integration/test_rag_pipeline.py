"""
RAG 流程整合測試：真的 bge-m3 與真的法規文件，建立 FAISS 索引、混合檢索、語意防護欄與查詢路由。
不需要 Ollama（只測檢索與防護欄，不生成回答）。
"""
import os

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_models")]


@pytest.fixture(scope="module")
def pipeline(require_models):
    from rag import main_guardrail_rag as m, rag_chat_engine, rag_index
    from shared import paths

    # 索引建立一次給整個模組用，所以在這裡直接把法規資料夾換成專案內真正的文件
    real_laws = os.path.join(paths.PROJECT_ROOT, "data", "laws")
    if not os.path.isdir(real_laws) or not os.listdir(real_laws):
        pytest.skip("data/laws 內沒有法規文件")
    patcher = pytest.MonkeyPatch()
    patcher.setattr(paths, "LAWS_DIR", real_laws)
    m.setup_environment()
    index = rag_index.load_or_build_index()
    engine = rag_chat_engine.build_chat_engine(index)
    yield {"index": index, "engine": engine}
    m.release_system()
    patcher.undo()


def test_index_is_built_and_persisted_in_the_runtime_folder(pipeline):
    from shared import paths

    assert len(pipeline["index"].docstore.docs) > 0
    assert os.path.isdir(paths.FAISS_DIR) and os.listdir(paths.FAISS_DIR)


def test_leave_question_retrieves_the_relevant_article(pipeline):
    from llama_index.core.schema import QueryBundle

    nodes = pipeline["engine"]._retriever.retrieve(QueryBundle("特別休假的規定是什麼"))
    assert nodes
    assert any("特別休假" in n.get_content() for n in nodes)


def test_off_topic_question_is_blocked_and_on_topic_passes(pipeline):
    from rag import rag_guardrail

    assert rag_guardrail.check_semantic_guardrail("幫我寫一首關於愛情的詩") is False
    assert rag_guardrail.check_semantic_guardrail("特休假的規定是什麼？") is True


def test_attendance_and_law_questions_take_different_routes(pipeline):
    from rag import rag_guardrail

    attendance = rag_guardrail.check_query_route("王小明昨天的打卡紀錄")
    law = rag_guardrail.check_query_route("特休假的規定是什麼？")
    assert attendance != law
