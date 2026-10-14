"""
app.py：RAG 法務/考勤助理的 Gradio 網頁入口（http://127.0.0.1:7860）。

主要功能：
  - 聊天介面：Basic 直接問 LLM，RAG 經防護欄與路由後查考勤或法規，逐字串流輸出
  - Execute Evaluate：執行 LLM-as-a-Judge 自動化評估
  - 右上角設定面板：選擇出題官/裁判官用雲端 API 或本機模型，並可測試連線、儲存或清除金鑰

串接模組：
  - main_guardrail_rag：初始化與問答流程
  - evaluate_rag：自動化評估
  - llm_settings：評估用 LLM 設定
  - 由 service_manager（backend_main 管理後台）啟動
"""
import sys

# 標準輸出改為 UTF-8，避免不同電腦的系統字碼頁讓終端機中文變亂碼
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import os

# huggingface_hub 在第一次被 import 時就固定 HF_HUB_OFFLINE，所以要在 import gradio 之前判斷：
# 本機有 BAAI/bge-m3 快取就設為離線模式
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

# 關閉 Gradio 的對外遙測請求（網路受限時會拖慢啟動）
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr
from rag.main_guardrail_rag import get_chat_response, stream_chat_response, init_system
from rag.eval.evaluate_rag import run_dynamic_evaluation
from rag import llm_settings

def chat_interface(message, history, route):
    """處理一則使用者訊息：新版 Gradio 的 messages 格式，逐字更新最後一則 assistant 訊息"""
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": ""})

    # 傳入對話歷史，讓防護欄與路由看得懂簡短追問
    for token in stream_chat_response(message, route, history):
        history[-1]["content"] += token
        yield history, ""

def trigger_evaluation(history):
    """執行自動化評估，逐步更新同一個對話視窗"""
    for updated_history in run_dynamic_evaluation(history):
        yield updated_history

# --- 設定面板：評估用的出題官/裁判官 LLM（邏輯在 llm_settings.py）---
def _settings_from_form(mode, api_key, api_base, api_model, local_model, local_host):
    """把表單內容合併到已儲存的設定；金鑰留空代表沿用原本的"""
    s = llm_settings.load_settings()
    s["mode"] = mode if mode in ("api", "local") else "local"
    s["api_base"] = (api_base or "").strip() or llm_settings.NVIDIA_BASE
    s["api_model"] = (api_model or "").strip() or llm_settings.DEFAULTS["api_model"]
    s["local_model"] = (local_model or "").strip() or llm_settings.DEFAULTS["local_model"]
    s["local_host"] = (local_host or "").strip()
    if (api_key or "").strip():
        s["api_key"] = api_key.strip()
    return s


def toggle_settings_panel(is_open):
    """齒輪按鈕：開關設定面板，打開時重新讀取 Ollama 已安裝的模型清單"""
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
    """儲存設定"""
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
    """清除已儲存的 API 金鑰"""
    saved = llm_settings.save_settings({}, clear_key=True)
    print("[LLM設定] 已清除儲存的 API 金鑰。", flush=True)
    return "✅ 已清除儲存的 API 金鑰。", f"金鑰狀態：{llm_settings.key_status_text(saved)}"


def test_api_cb(mode, api_key, api_base, api_model, local_model, local_host):
    """測試雲端 API（使用目前表單內容）"""
    yield "⏳ 正在測試雲端 API...（最多等約 20 秒）"
    s = _settings_from_form(mode, api_key, api_base, api_model, local_model, local_host)
    ok, msg = llm_settings.test_api(s)
    yield msg + "\n\n（測試用的是目前表單上的內容；要讓評估使用請按「儲存設定」。結果也已顯示在後台輸出。）"


def test_local_cb(mode, api_key, api_base, api_model, local_model, local_host):
    """測試本機模型（使用目前表單內容）"""
    yield "⏳ 正在測試本機模型...（第一次要載入模型，可能需要一兩分鐘）"
    s = _settings_from_form(mode, api_key, api_base, api_model, local_model, local_host)
    ok, msg = llm_settings.test_local(s)
    yield msg + "\n\n（結果也已顯示在後台輸出。）"


def main():
    """初始化系統、建立 Gradio 介面並啟動"""
    init_system()

    _s = llm_settings.load_settings()

    with gr.Blocks(title="Edge AI RAG - 人資法務助手") as demo:
        # 標題列，右上角是齒輪按鈕
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

        # Enter 送出訊息
        msg.submit(chat_interface, [msg, chatbot, route], [chatbot, msg])

        # 設定面板事件
        _form = [mode_radio, api_key_box, api_base_box, api_model_box, local_model_dd, local_host_box]
        gear_btn.click(toggle_settings_panel, [settings_open],
                       [settings_panel, settings_open, local_model_dd, key_info])
        save_btn.click(save_settings_cb, _form, [settings_status, key_info, api_key_box])
        clear_key_btn.click(clear_key_cb, None, [settings_status, key_info])
        test_api_btn.click(test_api_cb, _form, [settings_status])
        test_local_btn.click(test_local_cb, _form, [settings_status])

        # 評估與聊天共用同一個對話視窗
        eval_btn.click(trigger_evaluation, [chatbot], [chatbot])

    # inbrowser=True：啟動完成後自動開啟瀏覽器分頁
    demo.launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)

if __name__ == "__main__":
    main()
