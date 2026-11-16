"""
chat_logging.py：把問答流程（Assistant）的結果整理成對話紀錄並寫入 shared.chat_log（兩種來源共用同一個寫入點）。

主要功能：
  - citations_of：引用段落——文件問答是檢索到的段落（文件名稱、段落標籤、重排分數、內容），
    出缺勤查詢是查詢用的工具與查出來的結果表（審查時才看得出 AI 有沒有照著資料回答）
  - recorder：回傳給 Assistant.on_finished 用的函式（問題、完整回答、這題的 Trace）；來源在這裡指定
    （後台聊天視窗是 back_attendance，前台知識庫是 front_knowledge），寫入失敗不會影響問答

串接模組：
  - rag.answer.Assistant：每題結束後呼叫 on_finished
  - rag.chat_worker：後台聊天視窗載入完成後掛上 back_attendance 的記錄函式
  - admin.chat_service：前台知識庫問答完成後呼叫（front_knowledge）
  - shared.chat_log：資料層
"""
from shared import chat_log


def citations_of(trace):
    """這題的引用段落清單（給 chat_log.record 的 citations）。"""
    out = []
    data_text = getattr(trace, "data_text", "") or ""
    if data_text:
        tools = "、".join(getattr(trace, "tools", []) or []) or "查詢"
        out.append({"title": f"出缺勤查詢：{tools}", "label": "查詢結果", "score": None, "text": data_text})
    for hit in (getattr(trace, "hits", []) or []):
        score = hit.rerank if hit.rerank is not None else hit.score
        out.append({"title": hit.chunk.get("title", ""), "label": hit.label, "score": score,
                    "text": hit.chunk.get("body", "")})
    return out


def recorder(source):
    """建立 Assistant.on_finished 用的函式：fn(question, answer_text, trace)。"""
    def on_finished(question, answer_text, trace):
        chat_log.try_record(source, question, answer_text, citations_of(trace))

    return on_finished
