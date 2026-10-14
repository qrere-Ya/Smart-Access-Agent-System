"""
rag_chat_engine 單元測試：引用編號後處理（不改動原始節點）、中文 BM25（修正過「中文分數全為 0」的問題）。
"""
import types

from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode

from rag import rag_chat_engine as rce


def test_citation_numbers_are_added_in_order_without_touching_the_originals():
    originals = [
        NodeWithScore(node=TextNode(text="第一段", id_="a"), score=0.9),
        NodeWithScore(node=TextNode(text="第二段", id_="b"), score=0.5),
    ]
    processor = rce._CitationNumberingPostprocessor()
    numbered = processor.postprocess_nodes(originals, QueryBundle("問題"))
    assert [n.node.get_content() for n in numbered] == ["[1] 第一段", "[2] 第二段"]
    assert [n.score for n in numbered] == [0.9, 0.5]
    assert [n.node.get_content() for n in originals] == ["第一段", "第二段"]


def test_citation_numbers_do_not_pile_up_when_the_same_nodes_come_back():
    nodes = [NodeWithScore(node=TextNode(text="內容", id_="a"), score=1.0)]
    processor = rce._CitationNumberingPostprocessor()
    processor.postprocess_nodes(nodes, QueryBundle("q"))
    again = processor.postprocess_nodes(nodes, QueryBundle("q"))
    assert again[0].node.get_content() == "[1] 內容"


def test_chinese_text_is_segmented_into_space_separated_words():
    segmented = rce._jieba_segment("特別休假的規定")
    assert len(segmented.split()) > 1
    assert "".join(segmented.split()) == "特別休假的規定"


def _fake_index():
    nodes = {
        "a": TextNode(text="特別休假的天數依年資計算，滿一年者給予七日", id_="a"),
        "b": TextNode(text="加班費的計算方式為平日每小時工資額加成", id_="b"),
        "c": TextNode(text="勞工遇有分娩事實者得請產假八星期", id_="c"),
    }
    return types.SimpleNamespace(docstore=types.SimpleNamespace(docs=nodes)), nodes


def test_chinese_bm25_finds_the_relevant_chunk_with_a_positive_score():
    index, nodes = _fake_index()
    retriever = rce._build_chinese_bm25_retriever(index, similarity_top_k=2)
    results = retriever.retrieve("特別休假的規定")
    assert results[0].node.node_id == "a" and results[0].score > 0
    results = retriever.retrieve("加班費怎麼計算")
    assert results[0].node.node_id == "b" and results[0].score > 0


def test_chinese_bm25_returns_the_original_unsegmented_text():
    index, nodes = _fake_index()
    retriever = rce._build_chinese_bm25_retriever(index, similarity_top_k=1)
    result = retriever.retrieve("產假")[0]
    assert result.node.get_content() == nodes["c"].get_content()
    assert " " not in result.node.get_content()
