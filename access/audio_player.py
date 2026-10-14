"""
audio_player.py：門禁語音播報（TTS）。

主要功能：
  - 用 sherpa-onnx（Piper 中文模型）把文字合成語音，經 sounddevice 從喇叭播放
  - 背景執行緒依序播放佇列中的訊息，不阻塞主程式
  - 同一類別的語音有冷卻時間，避免重複堆疊
  - TTS 模型第一次播報才載入，閒置逾時或被驅逐時釋放

串接模組：
  - resource_manager：ManagedResource 資源協調
  - terminal_app：打卡問候與行事曆播報
"""
import os
import time
import queue
import threading
import sounddevice as sd
import sherpa_onnx
import numpy as np
from shared.resource_manager import ManagedResource
from shared import paths


class _TtsResource(ManagedResource):
    """sherpa-onnx TTS 引擎的載入與釋放。"""
    def __init__(self):
        super().__init__("audio.sherpa-tts", est_ram_mb=300, idle_ttl=120)

    def _load(self):
        model_dir = os.path.join(paths.MODELS_DIR, "vits-piper-zh_CN-huayan-medium")
        print("[Audio] 正在載入 sherpa-onnx 本地端語音引擎...")
        tts_config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=os.path.join(model_dir, "zh_CN-huayan-medium.onnx"),
                    tokens=os.path.join(model_dir, "tokens.txt"),
                    data_dir=os.path.join(model_dir, "espeak-ng-data"), # Piper 格式模型需要
                ),
                num_threads=2, # 限制執行緒，避免搶佔影像辨識資源
                debug=False,
                provider="cpu",
            ),
            rule_fsts="",
            max_num_sentences=1,
        )
        return sherpa_onnx.OfflineTts(tts_config)


class SmartAudioPlayer:
    def __init__(self, speed=1.0):
        """建立播放器：啟動背景播放執行緒並播報開機語音"""
        self.audio_queue = queue.Queue()
        self.is_running = True
        self.cooldown = 4.0
        self.last_spoken = {}
        self.speed = speed
        self._tts_res = _TtsResource()
        self.sample_rate = 22050  # 模型載入後會改成模型實際取樣率

        self.worker_thread = threading.Thread(target=self._audio_worker, daemon=True)
        self.worker_thread.start()

        # 開機語音，同時觸發第一次模型載入
        self.speak("智慧門禁系統，已啟用。", "system_init")

    def _audio_worker(self):
        """背景執行緒：從佇列取文字、合成語音並播放"""
        while self.is_running:
            try:
                text = self.audio_queue.get(timeout=1)
                # 前加逗號、後加句號，讓語氣自然收尾
                safe_text = f"，{text}。"

                with self._tts_res.use() as tts:
                    self.sample_rate = tts.sample_rate
                    audio = tts.generate(safe_text, sid=0, speed=self.speed)

                if audio is not None:
                    # 尾端補 0.4 秒靜音，避免最後的音被截掉
                    silence_length = int(self.sample_rate * 0.4)
                    silence_array = np.zeros(silence_length, dtype=np.float32)

                    padded_audio = np.concatenate((audio.samples, silence_array))

                    sd.play(padded_audio, self.sample_rate)
                    sd.wait()

                self.audio_queue.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                print(f"[Audio Playback Error] {e}")

    def speak(self, text, category="general"):
        """加入播報佇列；同一類別在冷卻時間內的重複播報會被忽略"""
        curr = time.time()
        if category in self.last_spoken:
            if curr - self.last_spoken[category] < self.cooldown:
                return
        self.last_spoken[category] = curr
        self.audio_queue.put(text)

    def stop(self):
        """停止播放執行緒並釋放 TTS 模型"""
        self.is_running = False
        self._tts_res.release("stop")
