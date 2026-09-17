import queue
import threading
import os
import subprocess
import time

# --- TTS 全域配置 ---
# 【合併專案調整】改用 __file__ 相對路徑，不管從哪個資料夾執行都能正確定位到專案根目錄
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PIPER_EXE = "piper" # 假設 piper 已加入系統路徑，或提供完整路徑
MODEL_PATH = os.path.join(_PROJECT_ROOT, "models", "vits-piper-zh_CN-huayan-medium", "zh_CN-huayan-medium.onnx")
CONFIG_PATH = os.path.join(_PROJECT_ROOT, "models", "vits-piper-zh_CN-huayan-medium", "zh_CN-huayan-medium.onnx.json")

# 建立 FIFO 任務佇列
voice_queue = queue.Queue()

def tts_worker():
    """
    【任務二】TTS 消費者執行緒
    監控 Queue，一旦有文字就呼叫 Piper 合成並播放
    """
    print("[VoiceAgent] TTS Worker 執行緒已啟動 (Daemon)...")
    while True:
        try:
            # 阻塞式獲取任務
            text = voice_queue.get()
            if text is None: break # 停止信號

            print(f"[VoiceAgent] 正在播報: {text}")

            # 呼叫 Piper CLI 進行合成並直接透過 aplay/ffplay 或 sounddevice 播放
            # 這裡使用 shell 管道將合成的 wav 傳給播放器 (邊緣運算常用做法)
            # 在 Windows 上建議使用 ffplay 或自定義的 python 播放模組
            cmd = f'echo "{text}" | {PIPER_EXE} --model {MODEL_PATH} --config {CONFIG_PATH} --output_raw | aplay -r 22050 -f S16_LE -t raw'

            # 為了確保在 Windows 上的相容性，實作中建議使用 subprocess.run
            # 注意：實際環境中需確認 piper 執行檔名稱與路徑
            subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # 完成任務
            voice_queue.task_done()

        except Exception as e:
            print(f"[VoiceAgent] 播放錯誤: {e}")
            time.sleep(1)

def speak_async(text):
    """
    【任務二】TTS 生產者介面
    將精簡文字放入佇列，不阻塞主程式
    """
    # 確保文字精簡 (由呼叫端控制，此處僅做 put)
    voice_queue.put(text)

# 啟動背景 Daemon 執行緒
worker_thread = threading.Thread(target=tts_worker, daemon=True)
worker_thread.start()
