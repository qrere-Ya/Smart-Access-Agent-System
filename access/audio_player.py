"""
audio_player.py：門禁語音播報（TTS）。

主要功能：
  - 用 sherpa-onnx（Piper 中文模型）把文字合成語音，經 sounddevice 從喇叭播放
  - 背景執行緒依序播放佇列中的訊息，不阻塞主程式
  - 同一類別的語音有冷卻時間，避免重複堆疊
  - 只有打卡問候（「葉君緯，早安」，speak 的 pause=True）：以逗號切成「名字」與「問候」兩段分別合成，
    中間插入約 0.5 秒停頓；開機語音與行事曆播報整句一氣呵成；
    語速預設 0.8（這個中文模型原速偏快，聽不清楚），開頭補 0.2 秒靜音避免喇叭剛啟動時吃掉第一個字；
    環境變數 SMART_ACCESS_TTS_SPEED 可現場調整；
  - 音量：模型原始音量偏小（峰值約 0.5），預設放大 2.0 倍，超過上限的部分用柔和壓縮避免爆音；
    環境變數 SMART_ACCESS_TTS_VOLUME（0.5 到 3.0）可調；Windows 系統音量另外也要夠大；
    試聽：python -m access.audio_player "葉君緯，早安" --speed 0.8 --volume 2.0
  - 開機就載入 TTS 模型並暖機一次（合成一句、開好喇叭輸出串流），模型常駐不做閒置回收，
    所以第一次打卡不會等載入、閒置後也不會重新載入；喇叭輸出串流一直開著（每次播放不再重新開裝置），
    緩衝用 high latency，辨識程式吃 CPU 時比較不會斷斷續續；同一段文字（常是姓名）合成過就用記憶體快取

串接模組：
  - resource_manager：ManagedResource 資源協調
  - terminal_app：打卡問候與行事曆播報
"""
import collections
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
DEFAULT_VOLUME = 2.0         # 放大倍數；原始峰值約 0.5，使用者在前台試聽後選 2.0；超過 0.8 的部分柔和壓縮不爆音
SOFT_KNEE = 0.8              # 放大後超過這個振幅的部分用柔和壓縮，最大不超過 1.0
SEGMENT_CACHE_SIZE = 128      # 合成過的短句（多半是姓名）留在記憶體，同一個人再打卡不必重新合成
SPLIT_MARKS = "，,、；;"       # 這些符號切成不同段，每段分別合成並在中間停頓


def speech_speed(default=DEFAULT_SPEED, environ=None):
    """語速：環境變數 SMART_ACCESS_TTS_SPEED（0.5 到 1.5）優先，壞值或超出範圍用預設。"""
    value = (os.environ if environ is None else environ).get("SMART_ACCESS_TTS_SPEED")
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return default
    return speed if 0.5 <= speed <= 1.5 else default


def volume_setting(default=DEFAULT_VOLUME, environ=None):
    """音量倍數：環境變數 SMART_ACCESS_TTS_VOLUME（0.5 到 3.0）優先，壞值或超出範圍用預設。"""
    value = (os.environ if environ is None else environ).get("SMART_ACCESS_TTS_VOLUME")
    try:
        gain = float(value)
    except (TypeError, ValueError):
        return default
    return gain if 0.5 <= gain <= 3.0 else default


def apply_volume(samples, gain):
    """放大音量：先乘倍數，超過 SOFT_KNEE 的部分用 tanh 柔和壓縮（最大 1.0），比直接削平的聲音乾淨。"""
    audio = np.asarray(samples, dtype=np.float32) * np.float32(gain)
    magnitude = np.abs(audio)
    over = magnitude > SOFT_KNEE
    if over.any():
        squeezed = SOFT_KNEE + (1.0 - SOFT_KNEE) * np.tanh((magnitude[over] - SOFT_KNEE) / (1.0 - SOFT_KNEE))
        audio[over] = np.sign(audio[over]) * squeezed.astype(np.float32)
    return audio


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


def segments_for(text, pause):
    """要合成的段落：打卡問候（pause=True）依逗號切開、中間停頓；其他語音（開機、行事曆）整句一氣呵成。"""
    if pause:
        return plan_segments(text)
    clean = str(text).strip().rstrip("。.!！?？ ")
    return [clean] if clean else []


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
        super().__init__("audio.sherpa-tts", est_ram_mb=300)      # 常駐：閒置後重新載入會讓下一次打卡延遲好幾秒

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
    def __init__(self, speed=None, volume=None):
        """建立播放器：啟動背景播放執行緒並播報開機語音"""
        self.audio_queue = queue.Queue()
        self.is_running = True
        self.cooldown = 4.0
        self.last_spoken = {}
        self.speed = speech_speed() if speed is None else speed
        self.volume = volume_setting() if volume is None else volume
        self._tts_res = _TtsResource()
        self.sample_rate = 22050  # 模型載入後會改成模型實際取樣率
        self._stream = None
        self._stream_rate = None
        self._cache = collections.OrderedDict()

        self.worker_thread = threading.Thread(target=self._audio_worker, daemon=True)
        self.worker_thread.start()

        # 開機語音，同時觸發第一次模型載入
        self.speak("智慧門禁系統已啟用", "system_init")          # 開機語音一氣呵成，不停頓

    def _warm_up(self):
        """載入模型、合成一句、開好輸出串流並送一小段靜音，讓第一次真的播報沒有載入與開裝置的延遲。"""
        try:
            with self._tts_res.use() as tts:
                self.sample_rate = tts.sample_rate
                tts.generate("你好。", sid=0, speed=self.speed)
            self._play(np.zeros(int(self.sample_rate * 0.1), dtype=np.float32))
        except Exception as e:  # noqa: BLE001  暖機失敗不影響之後正常播報
            print(f"[Audio Warm-up Error] {e}")

    def _synthesize(self, tts, segment):
        """合成一段（有快取）；回傳取樣陣列或 None。"""
        key = (segment, self.speed)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        audio = tts.generate(f"{segment}。", sid=0, speed=self.speed)
        if audio is None or not len(audio.samples):
            return None
        samples = np.asarray(audio.samples, dtype=np.float32)
        self._cache[key] = samples
        while len(self._cache) > SEGMENT_CACHE_SIZE:
            self._cache.popitem(last=False)
        return samples

    def _play(self, samples):
        """從一直開著的輸出串流播放（寫入會等到緩衝收下）；裝置出錯就關掉重開一次。"""
        for attempt in range(2):
            try:
                if self._stream is None or self._stream_rate != self.sample_rate:
                    self._close_stream()
                    self._stream = sd.OutputStream(samplerate=self.sample_rate, channels=1, dtype="float32",
                                                   latency="high")
                    self._stream.start()
                    self._stream_rate = self.sample_rate
                self._stream.write(np.ascontiguousarray(samples, dtype=np.float32).reshape(-1, 1))
                return
            except sd.PortAudioError:
                self._close_stream()
                if attempt:
                    raise

    def _close_stream(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception as e:  # noqa: BLE001  關不掉就丟掉，下一次播放會重開
                print(f"[Audio] 關閉輸出串流失敗：{type(e).__name__}")

    def _audio_worker(self):
        """背景執行緒：先暖機，再從佇列取文字、合成語音並播放"""
        self._warm_up()
        while self.is_running:
            try:
                text, pause = self.audio_queue.get(timeout=1)
                chunks = []
                with self._tts_res.use() as tts:
                    self.sample_rate = tts.sample_rate
                    for segment in segments_for(text, pause):          # 打卡問候才分段（名字、問候中間停頓）
                        samples = self._synthesize(tts, segment)
                        if samples is not None:
                            chunks.append(samples)

                if chunks:
                    self._play(apply_volume(assemble(chunks, self.sample_rate), self.volume))

                self.audio_queue.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                print(f"[Audio Playback Error] {e}")

    def speak(self, text, category="general", pause=False):
        """加入播報佇列；同一類別在冷卻時間內的重複播報會被忽略。pause=True 才在逗號處停頓（只有打卡問候用）"""
        curr = time.time()
        if category in self.last_spoken:
            if curr - self.last_spoken[category] < self.cooldown:
                return
        self.last_spoken[category] = curr
        self.audio_queue.put((text, pause))

    def stop(self):
        """停止播放執行緒並釋放 TTS 模型"""
        self.is_running = False
        self._tts_res.release("stop")
        self._close_stream()


def _demo(argv=None):
    """試聽：python -m access.audio_player "葉君緯，早安" --speed 0.8 --pause 0.5"""
    import argparse
    parser = argparse.ArgumentParser(description="試聽語音播報")
    parser.add_argument("text", nargs="?", default="葉君緯，早安")
    parser.add_argument("--speed", type=float, default=speech_speed())
    parser.add_argument("--pause", type=float, default=SEGMENT_PAUSE)
    parser.add_argument("--volume", type=float, default=volume_setting())
    args = parser.parse_args(argv)
    resource = _TtsResource()
    with resource.use() as tts:
        chunks = [tts.generate(f"{s}。", sid=0, speed=args.speed).samples for s in plan_segments(args.text)]
        rate = tts.sample_rate
    print(f"語速 {args.speed}、停頓 {args.pause} 秒、音量 {args.volume} 倍、分成 {len(chunks)} 段")
    sd.play(apply_volume(assemble(chunks, rate, pause=args.pause), args.volume), rate)
    sd.wait()
    resource.release("demo")


if __name__ == "__main__":
    _demo()
