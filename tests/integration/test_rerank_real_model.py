"""
重排整合測試：真的 bge-reranker-v2-m3 與 bge-m3、真的法規。確認重排把正確條文排前面、沒有依據的問題分數接近 0，
並整體跑一次題庫，確保檢索品質不低於已知水準。
"""
import os

import numpy as np
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_models")]


@pytest.fixture(scope="module")
def retrieval(tmp_path_factory):
    from assistant.doc_store import DocStore
    from assistant.rerank import Reranker
    from rag.rag_resources import ManagedHFEmbedding
    from shared import paths

    rerank_dir = os.path.join(paths.MODELS_DIR, "bge-reranker-v2-m3")
    laws = os.path.join(paths.PROJECT_ROOT, "data", "laws")
    if not os.path.isdir(rerank_dir) or not os.path.isdir(laws) or not os.listdir(laws):
        pytest.skip("缺少重排模型或法規文件")
    embedding = ManagedHFEmbedding(os.path.join(paths.MODELS_DIR, "bge-m3"), device="cuda")
    store = DocStore(laws, str(tmp_path_factory.mktemp("store")),
                     lambda texts: np.array(embedding.get_text_embedding_batch(list(texts))))
    store.sync()
    reranker = Reranker.from_model_dir(rerank_dir)
    yield store, reranker
    reranker.release()
    embedding.release()


def test_overtime_pay_question_finds_article_24_in_the_top_three(retrieval):
    """官方版另有第32-1條（補休代替加班費），也和加班費有關，所以只要求第24條在前三名。"""
    store, reranker = retrieval
    hits = reranker.rerank("加班費怎麼計算？", store.search("加班費怎麼計算？", 20), 3)
    assert "第24條" in [h.chunk["article"] for h in hits if h.chunk["title"] == "勞動基準法"]


def test_question_without_basis_gets_a_near_zero_relevance(retrieval):
    store, reranker = retrieval
    hits = reranker.rerank("個人綜合所得稅的免稅額是多少？", store.search("個人綜合所得稅的免稅額是多少？", 20), 1)
    assert hits[0].rerank < 0.01


def test_question_bank_quality_does_not_regress(retrieval):
    from assistant.evaluation import retrieval_eval as ev

    store, reranker = retrieval

    def search(question):
        hits = reranker.rerank(question, store.search(question, 20), 5)
        return [(ev.chunk_key(h.chunk), h.rerank) for h in hits]

    summary, _ = ev.evaluate(search)
    assert summary["全部"]["hit@3"] >= 0.95 and summary["全部"]["hit@5"] >= 0.95
    assert summary["詳細"]["hit@1"] == 1.0 and summary["標準"]["hit@3"] == 1.0
