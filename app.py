import sys

# 【2026-09-18，控制台編碼保護】不同電腦的 Windows「非 Unicode 程式的語言」設定
# （系統地區設定裡的作用中字碼頁）不一定相同：在家裡測試機是一種字碼頁，帶到外部
# 場地展示的電腦可能是另一種（例如簡體中文 936）。這支程式啟動時會 print 中文訊息
# （例如下面 init_system()、main_guardrail_rag.py 內部的初始化訊息），Python 預設
# 用系統字碼頁去輸出這些文字，字碼頁不合就會在終端機顯示成亂碼——但程式本身其實
# 沒有壞掉，是印出來的「文字」壞了。這裡直接把標準輸出/錯誤輸出重新設定成
# UTF-8，不管換到哪一台電腦，終端機印出來的中文字都不會亂碼（errors='replace'
# 是保險，萬一終端機本身真的無法顯示某個字元，顯示 ? 而不是直接噴例外中斷程式）。
# 放在所有其他 import 之前，確保後面任何模組在 import 階段印出的訊息也受保護。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import os

# 【2026-09-18 第三次修正，這次真正抓到 HF_HUB_OFFLINE 設了也沒用的根本原因】
# huggingface_hub 套件把 HF_HUB_OFFLINE 這個旗標，在它「第一次被 import 進來的
# 當下」就讀一次 os.environ、算成一個模組層級常數（huggingface_hub.constants.
# HF_HUB_OFFLINE），不是每次要不要連網都重新讀一次 os.environ。之前的做法是在
# main_guardrail_rag.py 的 setup_environment() 函式「執行的當下」才設定
# os.environ["HF_HUB_OFFLINE"]="1"——但這時候 huggingface_hub（透過
# sentence_transformers、transformers，也可能透過下面 `import gradio` 間接被
# 拉進來，Gradio 本身也依賴 huggingface_hub）早就已經被 import 過、已經把這個
# 旗標算成 False 並且快取住了，之後再怎麼設定 os.environ 都不會讓已經 import
# 好的模組重新讀取——這正是你實測「真的斷網、也設了 HF_HUB_OFFLINE=1，
# HuggingFaceEmbedding() 卻還是真的發了 HTTP 請求」的根本原因，不是快取資料夾
# 找不找得到的問題。
#
# 修正方式：把這個判斷搬到全部檔案的最上面——app.py 是整條啟動流程真正的
# 進入點，比 gradio、main_guardrail_rag 都還早被執行。在這裡、在
# `import gradio as gr` 之前，就先判斷本機是不是已經有 BAAI/bge-m3 的完整
# 快取、決定好要不要設定 HF_HUB_OFFLINE，這樣不管後面哪個套件第一次把
# huggingface_hub import 進來，讀到的都已經是正確的值。這裡刻意不 import
# huggingface_hub 本身來查詢快取路徑（那樣反而會提早觸發它自己把
# HF_HUB_OFFLINE 讀成 False 並快取住，變成雞生蛋蛋生雞的問題），改成用最基本
# 的 os.path 邏輯重現 huggingface_hub 官方文件記載的預設快取路徑規則（HF_HOME
# 預設在 ~/.cache/huggingface，實際快取資料夾在它底下的 hub/）——這個算法
# 已經拿你電腦實測驗證過：Python 裡 `huggingface_hub.constants.HF_HUB_CACHE`
# 印出來確實就是 C:\Users\qrere\.cache\huggingface\hub，跟這裡算出來的完全
# 一致，不是憑空猜的路徑。
_hf_home = os.environ.get("HF_HOME") or os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"),
    "huggingface",
)
_hf_hub_cache = os.environ.get("HF_HUB_CACHE") or os.path.join(_hf_home, "hub")
if os.path.isdir(os.path.join(_hf_hub_cache, "models--BAAI--bge-m3")):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    print(f"🤖 [啟動] 偵測到本機已有 BAAI/bge-m3 快取（{_hf_hub_cache}），"
          f"已在載入任何 Hugging Face 相關套件之前設定離線模式。")
else:
    print(f"⚠️ [啟動] 本機 {_hf_hub_cache} 底下還沒看到 BAAI/bge-m3 的快取，"
          f"維持連網模式，第一次需要網路下載約 2GB。")

# 【2026-09-18 第二次修正，analytics_enabled 這個修法本身版本不相容】上一版
# 把 analytics_enabled=False 加在 demo.launch() 裡，結果你這台電腦裝的 Gradio
# 版本噴了 TypeError: Blocks.launch() got an unexpected keyword argument
# 'analytics_enabled'——查證後發現我記錯了：analytics_enabled 從來就不是
# launch() 的參數，是 gr.Blocks()/gr.Interface() 建構子的參數，不同版本對
# launch() 收哪些參數的規則也一直在變，直接把它加進 launch() 這個做法本身
# 就不夠穩妥。
#
# 改用 Gradio 官方真正推薦、且跨版本最穩定的方式：Gradio 內部無論哪個版本，
# 判斷要不要送遙測都是讀 GRADIO_ANALYTICS_ENABLED 這個環境變數（不是讀某個
# 建構子/launch() 參數），這裡在 import gradio 之前就把它設成 "False"，
# 不管你電腦上裝的是哪個 Gradio 版本、也不管以後升級版本參數名稱又怎麼變，
# 都能穩定關掉這個對外遙測請求，不再依賴任何可能因版本而異的函式簽名。
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr
from main_guardrail_rag import get_chat_response, stream_chat_response, init_system
from evaluate_rag import run_dynamic_evaluation
import llm_settings

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

# ==========================================
# 【2026-09-20 新增】右上角「⚙️ 設定」：評估用的出題官／裁判官 LLM 設定
# （雲端 API 或只用本機模型；API 失效自動改本機；測試按鈕結果同時印到後台）
# 邏輯全在 llm_settings.py，這裡只負責畫面與把表單內容交給它。
# ==========================================
def _settings_from_form(mode, api_key, api_base, api_model, local_model, local_host):
    s = llm_settings.load_settings()
    s["mode"] = mode if mode in ("api", "local") else "local"
    s["api_base"] = (api_base or "").strip() or llm_settings.NVIDIA_BASE
    s["api_model"] = (api_model or "").strip() or llm_settings.DEFAULTS["api_model"]
    s["local_model"] = (local_model or "").strip() or llm_settings.DEFAULTS["local_model"]
    s["local_host"] = (local_host or "").strip()
    if (api_key or "").strip():
        s["api_key"] = api_key.strip()  # 只有這次有填才覆蓋；留空＝沿用已儲存的
    return s


def toggle_settings_panel(is_open):
    """齒輪按鈕：開／關設定面板；打開時順便重新讀一次 Ollama 已安裝的模型清單。"""
    now_open = not is_open
    s = llm_settings.load_settings()
    ok, res = llm_settings.list_local_models(s)
    choices = list(res) if ok and res else []
    if s["local_model"] not in choices:
        choices.append(s["local_model"])
    return (gr.Column(visible=now_open), now_open,
            gr.Dropdown(choices=choices, value=s["local_model"]),
            f"金鑰狀態：{llm_settings.key_status_text(s)}")


def save_settings_cb(mode, api_key, api_base, api_model, local_model, local_host):
    s = _settings_from_form(mode, api_key, api_base, api_model, local_model, local_host)
    llm_settings.save_settings(s)
    saved = llm_settings.load_settings()
    print(f"[LLM設定] 設定已儲存：模式={saved['mode']}，API 模型={saved['api_model']}，"
          f"本機模型={saved['local_model']}，金鑰={llm_settings.key_status_text(saved)}", flush=True)
    msg = "✅ 設定已儲存。"
    if saved["mode"] == "api" and not llm_settings.effective_api_key(saved):
        msg += "\n\nℹ️ 目前選的是雲端 API 但還沒有金鑰，按「Execute Evaluate」時會自動改用本機模型。"
    return msg, f"金鑰狀態：{llm_settings.key_status_text(saved)}", ""


def clear_key_cb():
    saved = llm_settings.save_settings({}, clear_key=True)
    print("[LLM設定] 已清除儲存的 API 金鑰。", flush=True)
    return "✅ 已清除儲存的 API 金鑰。", f"金鑰狀態：{llm_settings.key_status_text(saved)}"


def test_api_cb(mode, api_key, api_base, api_model, local_model, local_host):
    yield "⏳ 正在測試雲端 API...（最多等約 20 秒）"
    s = _settings_from_form(mode, api_key, api_base, api_model, local_model, local_host)
    ok, msg = llm_settings.test_api(s)
    yield msg + "\n\n（測試用的是目前表單上的內容；要讓評估使用請按「儲存設定」。結果也已顯示在後台輸出。）"


def test_local_cb(mode, api_key, api_base, api_model, local_model, local_host):
    yield "⏳ 正在測試本機模型...（第一次要載入模型，可能需要一兩分鐘）"
    s = _settings_from_form(mode, api_key, api_base, api_model, local_model, local_host)
    ok, msg = llm_settings.test_local(s)
    yield msg + "\n\n（結果也已顯示在後台輸出。）"


def main():
    # 預先初始化系統，避免第一個請求延遲太久
    init_system()

    _s = llm_settings.load_settings()

    with gr.Blocks(title="Edge AI RAG - 人資法務助手") as demo:
        # 標題列：右上角是齒輪按鈕
        with gr.Row():
            gr.Markdown("# 🛡️ 企業內資深人資法務 AI 助手")
            gear_btn = gr.Button("⚙️", scale=0, min_width=56)

        settings_open = gr.State(False)
        with gr.Column(visible=False) as settings_panel:
            gr.Markdown(
                "### ⚙️ 模型設定（用在「Execute Evaluate」的出題官／裁判官）\n"
                "選「雲端 API」時，API 連不上或金鑰無效會**自動改用本機模型**，不會中斷評估；"
                "選「只用本機模型」就完全不需要網路和金鑰。"
            )
            mode_radio = gr.Radio(
                choices=[("雲端 API（失效時自動改用本機模型）", "api"), ("只用本機模型", "local")],
                value=_s["mode"], label="出題官／裁判官使用哪種模型")
            api_key_box = gr.Textbox(label="API 金鑰", type="password",
                                     placeholder="貼上你的 API 金鑰（NVIDIA 金鑰通常以 nvapi- 開頭）；留空＝沿用已儲存的")
            key_info = gr.Markdown(f"金鑰狀態：{llm_settings.key_status_text(_s)}")
            api_model_box = gr.Textbox(label="API 模型名稱", value=_s["api_model"])
            local_model_dd = gr.Dropdown(choices=[_s["local_model"]], value=_s["local_model"],
                                         allow_custom_value=True, label="本機模型（Ollama）")
            with gr.Accordion("進階（一般不用改）", open=False):
                api_base_box = gr.Textbox(label="API 網址（OpenAI 相容）", value=_s["api_base"])
                local_host_box = gr.Textbox(label="Ollama 位址（空白＝預設 http://127.0.0.1:11434）",
                                            value=_s["local_host"])
            with gr.Row():
                save_btn = gr.Button("💾 儲存設定", variant="primary")
                test_api_btn = gr.Button("🔌 測試 API")
                test_local_btn = gr.Button("🖥️ 測試本機模型")
                clear_key_btn = gr.Button("🗑️ 清除已儲存金鑰")
            settings_status = gr.Markdown("")

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

        # ⚙️ 設定面板的事件
        _form = [mode_radio, api_key_box, api_base_box, api_model_box, local_model_dd, local_host_box]
        gear_btn.click(toggle_settings_panel, [settings_open],
                       [settings_panel, settings_open, local_model_dd, key_info])
        save_btn.click(save_settings_cb, _form, [settings_status, key_info, api_key_box])
        clear_key_btn.click(clear_key_cb, None, [settings_status, key_info])
        test_api_btn.click(test_api_cb, _form, [settings_status])
        test_local_btn.click(test_local_cb, _form, [settings_status])

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
    #
    # 【2026-09-18，修正在會議場地啟動 RAG 出現亂碼／瀏覽器連不上的真正原因】
    # Gradio 的 launch() 預設會另外發一個 HTTP 請求把匿名使用資料回報給 Gradio
    # 官方的遙測伺服器（這是 Gradio 本身的行為，不是這支程式加的，官方 GitHub
    # issue #4719 就是在講這個：網路受限時這個請求會卡住或逾時，拖慢甚至卡死
    # 整個 launch() 的啟動流程）。在家測試網路正常，這個請求幾乎瞬間就回來，
    # 感覺不出來；但在會議現場，如果網路被限制或不穩，這個請求就會卡住甚至
    # 逾時失敗，把逾時的例外內容印到終端機（看起來像亂碼），同時因為 launch()
    # 還卡在等這個請求，Flask/uvicorn 伺服器遲遲沒有真正開始監聽
    # 127.0.0.1:7860，inbrowser=True 跳出的瀏覽器分頁在伺服器準備好之前就先
    # 連過去，就會看到「無法連上網站，回應時間過長」。關掉這個對外遙測請求的
    # 設定已經移到檔案最上面用 GRADIO_ANALYTICS_ENABLED 環境變數處理（原因見
    # 上面 import 區塊的說明），這裡不用再加任何額外參數。
    demo.launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)

if __name__ == "__main__":
    main()

# 測試 CI 掃描故意加入的違規代碼
def test_insecure_code():
    try:
        # 1. 觸發 Bandit/Semgrep：使用危險的 shell 執行
        import os
        os.system("echo 'testing security scan'")
    except:
        # 2. 觸發 Ruff：不合規範的 bare except
        pass
