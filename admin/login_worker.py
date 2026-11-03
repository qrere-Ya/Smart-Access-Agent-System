"""
login_worker.py：後台人臉登入的視覺子行程（開鏡頭、人臉判斷），登入結束就結束，把記憶體整個還給系統。

主要功能：
  - 為什麼要獨立行程：載入視覺模型（TensorFlow、ONNX、CUDA）會讓行程多占約 1.3GB，而且 Python 與 CUDA 不會把
    記憶體還給系統；後台登入完只剩一個視窗，不該一直背著這些。子行程結束，記憶體就回收了
  - 輸出協定（子行程 → 登入視窗）：每行一個 "@@" 加 JSON；step 含縮小的預覽畫面（JPEG，base64）與逐幀判斷結果，
    error 含說明文字。其他輸出（模型載入訊息）不以 "@@" 開頭，登入視窗會忽略
  - 輸入協定（登入視窗 → 子行程）：每行一個指令，reset（鎖定期間清掉連續幀）、stop（結束）；
    視覺載入完成後才開始讀指令（載入原生函式庫時同時阻塞讀 stdin 管線，在 Windows 上會死鎖）
  - 只做判斷，不做決定的後續：失敗計數、鎖定、異常事件與建立工作階段都在登入視窗那一邊
  - serve() 的鏡頭、視覺與輸出入都可注入，單元測試不需要真的鏡頭或模型
  python -m admin.login_worker 由登入視窗啟動，不要手動執行

串接模組：
  - face_login：逐幀判斷（FaceLoginVerifier、RealVision、LoginStep）
  - login_window：啟動子行程、讀取輸出並轉成畫面事件
"""
import base64
import dataclasses
import json
import sys
import threading

from admin import face_login

PREFIX = "@@"
PREVIEW_SIZE = (480, 360)
JPEG_QUALITY = 70
STEP_FIELDS = {f.name for f in dataclasses.fields(face_login.LoginStep)}
KNOWN_STATUSES = {face_login.NO_FACE, face_login.MULTIPLE, face_login.NOT_FRONTAL, face_login.ALIGNING,
                  face_login.VERIFYING, face_login.FAILED, face_login.SUCCESS, face_login.NO_ADMIN}


def encode_frame(frame):
    """把畫面縮成預覽大小並壓成 JPEG（base64 文字）。"""
    import cv2

    small = cv2.resize(frame, PREVIEW_SIZE)
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise ValueError("畫面編碼失敗")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def decode_frame(text):
    """encode_frame 的反向，回傳 BGR 畫面；壞掉的資料丟 ValueError。"""
    import cv2
    import numpy as np

    try:
        data = np.frombuffer(base64.b64decode(text, validate=True), dtype=np.uint8)
    except (ValueError, TypeError):
        raise ValueError("預覽資料不是合法的 base64") from None
    try:
        frame = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    except cv2.error:
        frame = None
    if frame is None:
        raise ValueError("預覽資料不是合法的圖片")
    return frame


def make_line(message):
    """組成一行輸出（以 PREFIX 開頭的 JSON）。"""
    return PREFIX + json.dumps(message, ensure_ascii=False)


def parse_line(line):
    """解析一行輸出，回傳訊息 dict；不是協定行或格式不對回傳 None（登入視窗會直接忽略）。"""
    line = line.strip()
    if not line.startswith(PREFIX):
        return None
    try:
        message = json.loads(line[len(PREFIX):])
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    if message.get("t") == "error" and isinstance(message.get("message"), str):
        return message
    if message.get("t") == "step" and isinstance(message.get("jpg"), str) and _valid_step(message.get("step")):
        return message
    return None


def _valid_step(step):
    """逐幀結果的欄位與型別要正確：未知欄位、未知狀態、錯誤型別都拒絕，不信任子行程輸出的任何東西。"""
    if not isinstance(step, dict) or not step or not set(step) <= STEP_FIELDS:
        return False
    if step.get("status") not in KNOWN_STATUSES or not isinstance(step.get("message"), str):
        return False
    if not isinstance(step.get("progress", 0.0), (int, float)):
        return False
    if step.get("admin_name") is not None and not isinstance(step["admin_name"], str):
        return False
    if step.get("distance") is not None and not isinstance(step["distance"], (int, float)):
        return False
    return step.get("pose") is None or isinstance(step["pose"], dict)


def step_to_message(frame, step):
    """逐幀結果轉成要輸出的訊息。"""
    return {"t": "step", "jpg": encode_frame(frame), "step": dataclasses.asdict(step)}


class Commands:
    """從輸入讀到的指令狀態：stop 要結束，reset 要清掉連續幀。"""

    def __init__(self):
        self.stop = threading.Event()
        self.reset = threading.Event()

    def listen(self, lines):
        """讀指令直到輸入結束；輸入結束（登入視窗已關閉）也視為要結束。"""
        for raw in lines:
            command = raw.strip()
            if command == "reset":
                self.reset.set()
            elif command == "stop":
                break
        self.stop.set()


def serve(camera_factory, vision_factory, emit, commands, on_ready=None):
    """
    主迴圈：讀鏡頭、逐幀判斷、輸出結果，直到收到 stop 或發生錯誤。emit(message) 負責輸出一則訊息。
    on_ready 在視覺載入完成後呼叫（用來開始監聽指令）。
    """
    camera = vision = None
    try:
        camera = camera_factory()
        if camera is None:
            emit({"t": "error", "message": "打不開鏡頭。請確認鏡頭已連接，且沒有被門禁終端機或其他程式占用。"})
            return
        vision = vision_factory()
        if on_ready is not None:
            on_ready()
        verifier = face_login.FaceLoginVerifier(vision)
        while not commands.stop.is_set():
            if commands.reset.is_set():
                commands.reset.clear()
                verifier.reset()
            ok, frame = camera.read()
            if not ok:
                emit({"t": "error", "message": "讀不到鏡頭畫面。"})
                return
            emit(step_to_message(frame, verifier.step(frame)))
    except Exception as e:  # 只回報型別，不把內部細節傳給畫面
        emit({"t": "error", "message": f"登入畫面發生錯誤：{type(e).__name__}"})
    finally:
        if camera is not None:
            camera.release()
        close = getattr(vision, "close", None)
        if close:
            close()


def main():
    """子行程進入點：stdout 只用來輸出協定行，其餘輸出（模型載入訊息）改走 stderr。"""
    from admin.login_window import open_camera

    protocol = sys.stdout
    sys.stdout = sys.stderr
    commands = Commands()

    def emit(message):
        protocol.write(make_line(message) + "\n")
        protocol.flush()

    def listen():
        # 要等視覺載入完才開始讀指令：載入原生函式庫（DLL）時若同時有執行緒阻塞讀 stdin 管線，Windows 上會卡死。
        # 載入期間收到的指令留在管線裡，載入完才讀
        threading.Thread(target=commands.listen, args=(sys.stdin,), daemon=True).start()

    serve(open_camera, face_login.RealVision, emit, commands, on_ready=listen)


if __name__ == "__main__":
    main()
