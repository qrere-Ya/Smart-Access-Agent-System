"""
chat_worker.py：聊天視窗的背景工作（載入模型、重新掃描文件、套用語言模型設定）。

主要功能：
  - 每個工作都在背景執行緒跑，結果一律放進事件佇列，由畫面執行緒輪詢處理
  - 背景執行緒不碰任何 Tk／customtkinter 元件，也不呼叫 after（Tk 不是執行緒安全的）
  - 佇列事件格式 (kind, payload)：status、note、ready、failed、docs、settings_applied
  - 載入遇到暫時性失敗（資源不足、語言模型服務未就緒）會自動重試幾次；找不到模型檔這類永久性錯誤不重試

串接模組：
  - rag.chat_window：建立事件佇列並在 _tick 取出事件；rag.bootstrap：組裝助理、重新掃描文件、重載語言模型
  - rag.chat_settings_dialog：用 spawn 開背景執行緒；admin.service_manager：自動啟動 Ollama
"""
import threading
import time

from rag import bootstrap

LOAD_RETRIES = 3            # 載入失敗（資源暫時不足、語言模型服務還沒就緒）後自動再試幾次
LOAD_RETRY_DELAY = 10       # 每次重試前等幾秒，讓 Ollama 啟動完成或其他程式釋放記憶體


def spawn(target, *args):
    """開一條 daemon 背景執行緒。"""
    threading.Thread(target=target, args=args, daemon=True).start()


def describe_report(report):
    """把文件重新掃描的結果整理成一行提示文字。"""
    parts = []
    for label, items in (("新增", report.added), ("更新", report.updated), ("移除", report.removed)):
        if items:
            parts.append(f"{label}：{'、'.join(items)}")
    if report.failed:
        parts.append("讀不出內容：" + "；".join(f"{n}（{why}）" for n, why in report.failed.items()))
    return "📚 文件已重新掃描　" + ("　".join(parts) if parts else "沒有變動")


def load_runtime(events, runtime_factory, auto_start_ollama, retries=None, delay=None, sleep=time.sleep):
    """背景載入模型與助理（本機 Ollama 沒開會自動啟動），進度與結果放進事件佇列；暫時性失敗會自動重試。"""
    retries = LOAD_RETRIES if retries is None else retries
    delay = LOAD_RETRY_DELAY if delay is None else delay
    try:
        if auto_start_ollama:
            events.put(("status", "啟動本機語言模型服務…"))
            from admin import service_manager

            started, warning = service_manager.start_ollama(lambda line: None)
            if warning:
                events.put(("note", f"⚠️ {warning}"))
    except Exception as e:
        events.put(("note", f"⚠️ 啟動語言模型服務時出錯：{type(e).__name__}：{e}"))
    for attempt in range(retries + 1):
        try:
            runtime = runtime_factory(progress=lambda text: events.put(("status", text)))
            events.put(("ready", runtime))
            return
        except RuntimeError as e:         # 資源暫時不足（ResourceDenied 也是）或語言模型服務還沒就緒：等一下再試
            if attempt >= retries:
                events.put(("failed", f"{type(e).__name__}：{e}"))
                return
            events.put(("status", f"資源或服務尚未就緒，{delay} 秒後重試（第 {attempt + 1}/{retries} 次）…"))
            sleep(delay)
        except Exception as e:            # 找不到模型檔、檔案損壞等永久性錯誤，重試沒有用
            events.put(("failed", f"{type(e).__name__}：{e}"))
            return


def start_load_runtime(events, runtime_factory, auto_start_ollama):
    """在背景執行緒載入模型。"""
    spawn(load_runtime, events, runtime_factory, auto_start_ollama)


def start_refresh_docs(runtime, events):
    """在背景重新掃描文件；失敗也會送出事件，讓按鈕恢復。"""
    def run():
        try:
            report = bootstrap.refresh_documents(runtime)
        except Exception as e:  # 掃描失敗也要讓按鈕恢復，不然這次開著的期間都不能再掃
            events.put(("docs", f"⚠️ 文件重新掃描失敗：{type(e).__name__}：{e}"))
            return
        events.put(("docs", describe_report(report)))

    spawn(run)


def start_apply_settings(runtime, events, show):
    """在背景重載語言模型；完成後把 (show, 提示文字) 放進事件佇列，由畫面執行緒更新設定視窗。"""
    def run():
        note = bootstrap.reload_llm(runtime)
        events.put(("settings_applied", (show, note)))

    spawn(run)
