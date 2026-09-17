import gradio as gr
from main_guardrail_rag import get_chat_response, stream_chat_response, init_system
from evaluate_rag import run_dynamic_evaluation

def chat_interface(message, history, route):
    # 【相容新版 Gradio】舊版 Chatbot 是用 [user訊息, bot訊息] 這種兩格一組的 tuple
    # 格式；新版 Gradio（你現在裝的版本）已經完全不支援這種寫法了，只吃「messages」
    # 格式：一則訊息一個 dict，裡面有 role（user/assistant）跟 content 兩個 key，
    # 硬用舊格式塞進去，Gradio 會直接報錯：
    # "Data incompatible with messages format. Each message should be a dictionary
    #  with 'role' and 'content' keys..." —— 這就是你剛剛遇到的那個錯誤的真正原因。
    # 所以這裡改成：先各自 append 一則 user 訊息、一則 assistant 訊息（內容先留空）。
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": ""})

    # 【2026-09-02 修正】把對話歷史 history 一起傳給 stream_chat_response()，讓防護欄
    # 跟路由分類看得到「使用者上一句話」，才聽得懂「有名子嗎？」這種需要上下文才懂
    # 的簡短追問，不會被誤判成跟人事差勤無關而被攔截。
    # 使用串流 API 逐字更新「最後一則」訊息（也就是剛剛 append 的 assistant 訊息）
    for token in stream_chat_response(message, route, history):
        history[-1]["content"] += token
        yield history, ""

def trigger_evaluation(history):
    # 呼叫 Generator，逐步更新對話歷史。
    # 【2026-09-08，三度修正】評估流程改回跟真人聊天共用同一個「對話視窗」
    # （不再另外開獨立視窗、也不是左右並排的兩個 Chatbot），靠 evaluate_rag.py
    # 內部嚴格的角色分配（出題官／裁判官／系統訊息一律 role="user" 靠右，
    # 考生的作答一律 role="assistant" 靠左）在同一個視窗裡分左右。
    for updated_history in run_dynamic_evaluation(history):
        yield updated_history

def main():
    # 預先初始化系統，避免第一個請求延遲太久
    init_system()

    with gr.Blocks(title="Edge AI RAG - 人資法務助手") as demo:
        gr.Markdown("# 🛡️ 企業內資深人資法務 AI 助手")

        chatbot = gr.Chatbot(label="對話視窗", height=650)

        with gr.Row():
            with gr.Column(scale=4):
                msg = gr.Textbox(
                    placeholder="輸入問題並按 Enter...",
                    label="訊息輸入"
                )
            with gr.Column(scale=1):
                route = gr.Radio(
                    choices=["Basic", "RAG"],
                    value="RAG",
                    label="路由模式"
                )
                eval_btn = gr.Button("🧪 Execute Evaluate")

        # 綁定 Enter 鍵發送訊息
        msg.submit(chat_interface, [msg, chatbot, route], [chatbot, msg])

        # 【2026-09-08，三度修正：改回統一在最上面這個「對話視窗」裡執行，不另外開視窗】
        # 評估流程一樣讀寫上面這個 chatbot（跟真人聊天共用同一個視窗），靠
        # evaluate_rag.py 內部嚴格的角色分配，讓考生的作答固定用 role="assistant"
        # （Gradio 預設靠左），出題官／裁判官／系統訊息／診斷固定用 role="user"
        # （Gradio 預設靠右）——同一個視窗裡，左邊就只會出現考生的題目跟作答，
        # 右邊就只會出現出題／評分／系統這些幕後過程，不用另外開新的區塊或視窗。
        eval_btn.click(trigger_evaluation, [chatbot], [chatbot])

    # inbrowser=True：這裡查過 Gradio 原始碼，launch() 的 inbrowser 參數預設是 False，
    # 也就是預設「不會」自動幫你開瀏覽器分頁，只是把網站架起來而已，這就是後台按下
    # 「🧠 啟動 RAG」之後「沒有自動開啟」的真正原因。加上 inbrowser=True，
    # app.py 啟動完成、網頁真的就緒時，就會自動幫你跳出瀏覽器分頁顯示 RAG 助理畫面。
    demo.launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)

if __name__ == "__main__":
    main()
