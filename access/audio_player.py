"""
audio_player.py：門禁語音播報（TTS）。

主要功能：
  - 用 sherpa-onnx（Piper 中文模型）把文字合成語音，經 sounddevice 從喇叭播放
  - 背景執行緒依序播放佇列中的訊息，不阻塞主程式
  - 同一類別的語音有冷卻時間，避免重複堆疊
  - 打卡問候（「葉君緯，早安」）：以逗號切成「名字」與「問候」兩段分別合成，中間插入約 0.5 秒停頓；
    語速預設 0.8（這個中文模型原速偏快，聽不清楚），開頭補 0.2 秒靜音避免喇叭剛啟動時吃掉第一個字；
    環境變數 SMART_ACCESS_TTS_SPEED 可現場調整；試聽：python -m access.audio_player "葉君緯，早安" --speed 0.8
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

DEFAULT_SPEED = 0.8          # 1.0 是模型原速，聽起來太快；數字越小越慢
SEGMENT_PAUSE = 0.5          # 名字與問候之間的停頓（秒）
LEAD_SILENCE = 0.2           # 開頭靜音，喇叭剛啟動時才不會吃掉第一個字
TAIL_SILENCE = 0.4           # 結尾靜音，避免最後的音被截掉
SPLIT_MARKS = "，,、；;"       # 這些符號切成不同段，每段分別合成並在中間停頓


def speech_speed(default=DEFAULT_SPEED, environ=None):
    """語速：環境變數 SMART_ACCESS_TTS_SPEED（0.5 到 1.5）優先，壞值或超出範圍用預設。"""
    value = (os.environ if environ is None else environ).get("SMART_ACCESS_TTS_SPEED")
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return default
    return speed if 0.5 <= speed <= 1.5 else default


def plan_segments(text):
    """把一句話依逗號等符號切成幾段（去掉空白段）；沒有分隔符號就是一段。"""
    parts, current = [], ""
    for ch in str(text):
        if ch in SPLIT_MARKS:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return [p.strip().rstrip("。.!！?？ ") for p in parts if p.strip().rstrip("。.!！?？ ")]


def assemble(chunks, sample_rate, pause=SEGMENT_PAUSE, lead=LEAD_SILENCE, tail=TAIL_SILENCE):
    """把各段語音接起來：開頭靜音、段與段之間停頓、結尾靜音。chunks 是 float32 取樣陣列的清單。"""
    def silence(seconds):
        return np.zeros(int(sample_rate * seconds), dtype=np.float32)

    pieces = [silence(lead)]
    for index, chunk in enumerate(chunks):
        if index:
            pieces.append(silence(pause))
        pieces.append(np.asarray(chunk, dtype=np.float32))
    pieces.append(silence(tail))
    return np.concatenate(pieces)


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
    def __init__(self, speed=None):
        """建立播放器：啟動背景播放執行緒並播報開機語音"""
        self.audio_queue = queue.Queue()
        self.is_running = True
        self.cooldown = 4.0
        self.last_spoken = {}
        self.speed = speech_speed() if speed is None else speed
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
                chunks = []
                with self._tts_res.use() as tts:
                    self.sample_rate = tts.sample_rate
                    for segment in plan_segments(text):                  # 名字、問候分開合成，中間才有明顯停頓
                        audio = tts.generate(f"{segment}。", sid=0, speed=self.speed)
                        if audio is not None and len(audio.samples):
                            chunks.append(audio.samples)

                if chunks:
                    sd.play(assemble(chunks, self.sample_rate), self.sample_rate)
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


def _demo(argv=None):
    """試聽：python -m access.audio_player "葉君緯，早安" --speed 0.8 --pause 0.5"""
    import argparse
    parser = argparse.ArgumentParser(description="試聽語音播報")
    parser.add_argument("text", nargs="?", default="葉君緯，早安")
    parser.add_argument("--speed", type=float, default=speech_speed())
    parser.add_argument("--pause", type=float, default=SEGMENT_PAUSE)
    args = parser.parse_args(argv)
    resource = _TtsResource()
    with resource.use() as tts:
        chunks = [tts.generate(f"{s}。", sid=0, speed=args.speed).samples for s in plan_segments(args.text)]
        rate = tts.sample_rate
    print(f"語速 {args.speed}、停頓 {args.pause} 秒、分成 {len(chunks)} 段")
    sd.play(assemble(chunks, rate, pause=args.pause), rate)
    sd.wait()
    resource.release("demo")


if __name__ == "__main__":
    _demo()
