import os
import time
import queue
import threading
import sounddevice as sd
import sherpa_onnx
import numpy as np
from resource_manager import ManagedResource


class _TtsResource(ManagedResource):
    """sherpa-onnx TTS 引擎：Initialized 時不載入，第一次播報才申請，閒置逾時/被驅逐時釋放。"""
    def __init__(self):
        super().__init__("audio.sherpa-tts", est_ram_mb=300, idle_ttl=120)

    def _load(self):
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        model_dir = os.path.join(BASE_DIR, "models", "vits-piper-zh_CN-huayan-medium")
        print("[Audio] 正在載入 sherpa-onnx 本地端語音引擎...")
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
        return sherpa_onnx.OfflineTts(tts_config)


class SmartAudioPlayer:
    def __init__(self, speed=1.0):
        """ 初始化 sherpa-onnx 語音引擎 """
        self.audio_queue = queue.Queue()
        self.is_running = True
        self.cooldown = 4.0
        self.last_spoken = {}
        self.speed = speed
        # 【資源規範 A. Initialized】建構子不載入模型；由背景執行緒在第一次播報時才申請
        self._tts_res = _TtsResource()
        self.sample_rate = 22050  # 載入後以模型實際值更新

        # 啟動背景播放執行緒
        self.worker_thread = threading.Thread(target=self._audio_worker, daemon=True)
        self.worker_thread.start()

        # 啟動播報（開機語音）（同時觸發第一次模型載入）。【2026-09-20】使用者要求保留開機語音。
        self.speak("智慧門禁系統，已啟用。", "system_init")

    def _audio_worker(self):
        """ 背景執行緒：負責將文字轉成音訊陣列並直接驅動喇叭播放 """
        while self.is_running:
            try:
                text = self.audio_queue.get(timeout=1)
                # 【優化 1】：除了字首加逗號，字尾加上句號，強迫模型語氣收尾
                safe_text = f"，{text}。"

                # 推論產生音訊
                with self._tts_res.use() as tts:
                    self.sample_rate = tts.sample_rate
                    audio = tts.generate(safe_text, sid=0, speed=self.speed)

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
        self._tts_res.release("stop")
