import os
import time
import queue
import threading
import sounddevice as sd
import sherpa_onnx
import numpy as np

class SmartAudioPlayer:
    def __init__(self, speed=1.0):
        """ 初始化 sherpa-onnx 語音引擎 """
        self.audio_queue = queue.Queue()
        self.is_running = True
        self.cooldown = 4.0
        self.last_spoken = {}
        self.speed = speed
        self.tts = None

        print("[Audio] 正在初始化 sherpa-onnx 本地端語音引擎...")

        # 設定模型路徑 (請確保資料夾名稱與您下載的一致)
        # 【合併專案調整】audio_player.py 現在跟專案根目錄同一層（少一層 src/ 巢狀），
        # 所以只需要往上找一層 (dirname 一次)，不再是兩次。
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        model_dir = os.path.join(BASE_DIR, "models", "vits-piper-zh_CN-huayan-medium")

        try:
            # Sherpa-onnx 設定 (專為 CPU 邊緣運算優化)
            tts_config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=os.path.join(model_dir, "zh_CN-huayan-medium.onnx"),
                        tokens=os.path.join(model_dir, "tokens.txt"),
                        data_dir=os.path.join(model_dir, "espeak-ng-data"), # Piper 格式模型專用
                    ),
                    num_threads=2, # 限制執行緒避免搶佔影像辨識資源
                    debug=False,
                    provider="cpu",
                ),
                rule_fsts="",
                max_num_sentences=1,
            )

            self.tts = sherpa_onnx.OfflineTts(tts_config)
            self.sample_rate = self.tts.sample_rate
            print("[Audio] 語音引擎已就緒。")

        except Exception as e:
            print(f"[Audio Error] sherpa-onnx 初始化失敗，請檢查模型路徑: {e}")

        # 啟動背景播放執行緒
        self.worker_thread = threading.Thread(target=self._audio_worker, daemon=True)
        self.worker_thread.start()

        # 模型載入完成後，立刻播報初始化語音 (Warm-up)
        if self.tts:
            self.speak("智慧門禁系統，已啟用。", "system_init")

    def _audio_worker(self):
        """ 背景執行緒：負責將文字轉成音訊陣列並直接驅動喇叭播放 """
        while self.is_running:
            try:
                text = self.audio_queue.get(timeout=1)
                if self.tts is None:
                    self.audio_queue.task_done()
                    continue

                # 【優化 1】：除了字首加逗號，字尾加上句號，強迫模型語氣收尾
                safe_text = f"，{text}。"

                # 推論產生音訊
                audio = self.tts.generate(safe_text, sid=0, speed=self.speed)

                if audio is not None:
                    # 【優化 2 - 解決尾音被吃】：手動建立 0.4 秒的純靜音陣列
                    silence_length = int(self.sample_rate * 0.4)
                    silence_array = np.zeros(silence_length, dtype=np.float32)

                    # 將原本的音訊與靜音陣列接合起來
                    padded_audio = np.concatenate((audio.samples, silence_array))

                    # 播放加上靜音尾巴的完整音訊
                    sd.play(padded_audio, self.sample_rate)
                    sd.wait()

                self.audio_queue.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                print(f"[Audio Playback Error] {e}")

    def speak(self, text, category="general"):
        """ 播報接口：支援冷卻判定，防止語音堆疊 """
        curr = time.time()
        if category in self.last_spoken:
            # 如果同一類別的語音在冷卻時間內，則忽略此次播報
            if curr - self.last_spoken[category] < self.cooldown:
                return
        self.last_spoken[category] = curr
        self.audio_queue.put(text)

    def stop(self):
        """ 安全關閉語音執行緒 """
        self.is_running = False
