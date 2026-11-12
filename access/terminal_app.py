"""
terminal_app.py：門禁現場即時人臉辨識主程式。

主要功能：
  - 讀取攝影機畫面，由背景執行緒做人臉偵測、活體偵測、ArcFace 身分比對（身分比對、打卡與異常事件經 terminal_backend：
    單機讀寫本機資料庫，前台角色用本機快取與離線佇列）
  - 辨識成功後打卡，鏡頭畫面下方顯示半透明黑框「打卡成功　姓名　早安／午安／晚安」，語音念同一句
  - 單機版上班打卡後播報當日行事曆
  - 偵測放行瞬間是否有多人（尾隨），畫面顯示橫幅警示並拍照存證
  - 偵測有人停留在鏡頭前卻未打卡（pass_through），記為 no_scan_walkby 異常事件
  - 以 OpenCV 視窗顯示辨識結果，按 q 離開

串接模組：
  - vision_core：人臉偵測、對齊、特徵萃取、活體偵測
  - terminal_backend：特徵比對、打卡紀錄、行事曆、異常事件（單機包住 database_mgr；前台用 front_store 的快取與佇列）
  - security：YOLO 人數統計與尾隨證據圖
  - audio_player：TTS 語音播報
  - utils：畫面中文字繪製
  - alert_sound：尾隨、非活體、經過未打卡時在背景響短提示音（各自冷卻，SMART_ACCESS_ALERT_SOUND=0 可關）
  - front_screens：前台角色時辨識視窗放在主螢幕，F11／f 切全螢幕、Esc 離開（其他角色維持原本視窗）
  - 由 service_manager（backend_main 管理後台）啟動
"""
import sys
import cv2
import numpy as np
from access import vision_core as vc
import time
import threading
import queue
from access import utils
from access import security as sec
from access import pass_through
from access import pose
from access import punch_state
from access import terminal_backend
from access import front_screens
from access.alert_sound import AlertSound
from shared import role_config
from access.audio_player import SmartAudioPlayer
import traceback

# 標準輸出改為 UTF-8，避免不同電腦的系統字碼頁讓終端機中文變亂碼
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# --- 全域狀態 ---
is_analyzing = False       # 背景分析是否進行中（忙碌時主迴圈跳過該幀）
shared_face_data = None    # 最新辨識結果，背景執行緒寫、主執行緒讀

# 打卡與語音冷卻
cooldown_dict = {}
RESULT_HOLD = 4.0          # 打卡成功後，結果畫面至少停留的秒數（人離開鏡頭也繼續顯示）
voice_history = {}
VOICE_RESET_TIME = 43200   # 同一句問候語再次播報的間隔（秒，12 小時）
last_seen_dict = {}        # 每位員工最後一次成功打卡的時間
TERMINAL_MAX_YAW = 0.35    # 轉頭上限（鼻子偏離雙眼中點占雙眼距離的比例），比後台登入寬鬆，只擋明顯側臉
TERMINAL_MAX_ROLL = 25.0   # 歪頭上限（度）
TERMINAL_PITCH = (0.20, 0.80)  # 仰頭低頭的容許範圍，比後台登入寬（鏡頭常裝得比臉高或低）
ATTENDANCE_COOLDOWN = 60   # 打卡冷卻時間（秒）

# 尾隨警示狀態，與個人打卡結果分開存放
shared_tailgate_ts = None
TAILGATE_BANNER_HOLD = 3.0  # 尾隨橫幅最少顯示秒數

# 經過未打卡監控（純邏輯，參數見 access/pass_through.py）
pass_monitor = pass_through.PassThroughMonitor()

# 活體連續判定計數器
attack_counter = 0  # 連續假臉幀數
real_counter = 0    # 連續真臉幀數

audio_player = SmartAudioPlayer()
alert_sound = AlertSound()      # 警示提示音（尾隨、非活體、經過未打卡），各自冷卻 20 秒、背景播放

# 資料後端：單機讀寫本機資料庫，前台用本機快取與佇列（main 依角色建立）
backend = terminal_backend.LocalBackend()

# 主迴圈把影格丟進佇列，由常駐的分析執行緒取出處理
_frame_queue = queue.Queue(maxsize=1)

# 保護 shared_face_data 與 shared_tailgate_ts 的讀寫
_shared_data_lock = threading.Lock()

def _publish_face_data(value):
    """更新最新辨識結果（執行緒安全），只由背景分析執行緒呼叫。"""
    global shared_face_data
    with _shared_data_lock:
        shared_face_data = value

def _publish_tailgate_alert(ts):
    """記錄最近一次偵測到尾隨的時間（執行緒安全），只由背景分析執行緒呼叫。"""
    global shared_tailgate_ts
    with _shared_data_lock:
        shared_tailgate_ts = ts

def _analysis_worker(detector):
    """常駐背景執行緒：從 _frame_queue 取影格交給 analyze_in_background 分析。"""
    while True:
        frame_copy = _frame_queue.get()
        if frame_copy is None:  # 停止信號
            break
        analyze_in_background(frame_copy, detector)
        _frame_queue.task_done()

def draw_target(img, x1, y1, x2, y2, color=(255, 200, 0), length=40):
    """在人臉位置畫四角對焦框。"""
    cv2.line(img, (x1, y1), (x1 + length, y1), color, 3)
    cv2.line(img, (x1, y1), (x1, y1 + length), color, 3)
    cv2.line(img, (x2, y1), (x2 - length, y1), color, 3)
    cv2.line(img, (x2, y1), (x2, y1 + length), color, 3)
    cv2.line(img, (x1, y2), (x1 + length, y2), color, 3)
    cv2.line(img, (x1, y2), (x1, y2 - length), color, 3)
    cv2.line(img, (x2, y2), (x2 - length, y2), color, 3)
    cv2.line(img, (x2, y2), (x2, y2 - length), color, 3)

def _check_pass_through(frame, person_boxes, tailgate_logged):
    """每幀結尾判斷是否有人經過鏡頭卻未打卡，是就記錄異常事件（失敗不影響辨識）。"""
    try:
        if pass_monitor.update(time.time(), person_boxes, frame.shape[1], tailgate_logged):
            big = max(person_boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            backend.anomaly(pass_through.EVENT_TYPE, pass_through.EVENT_DETAIL, frame, big)
            alert_sound.notify(pass_through.EVENT_TYPE)
    except Exception as e:
        print(f"[terminal_app][WARN] 經過未打卡判斷或寫入失敗: {e}")


def _log_punch(recognition, current_time):
    """打卡（單機寫資料庫、前台進佇列）並播報語音，回傳 PunchOutcome；同時記下時間讓冷卻與結果停留生效。"""
    outcome = backend.punch(recognition)
    name = recognition.name
    if outcome.calendar:
        audio_player.speak(outcome.calendar, "calendar")
    v_key = f"{name}_{outcome.voice_key or outcome.text}"
    if outcome.audio and (current_time - voice_history.get(v_key, 0)) > VOICE_RESET_TIME:
        audio_player.speak(outcome.audio, f"user_{name}")
        voice_history[v_key] = current_time

    last_seen_dict[name] = current_time
    cooldown_dict[name] = {'msg': outcome.text, 'color': outcome.color, 'success': outcome.success,
                           'greeting': outcome.greeting}
    return outcome


def analyze_in_background(frame_copy, detector):
    """單幀完整分析：人臉偵測 → 活體 → 身分比對與打卡 → 尾隨偵測，結果寫入共用狀態。"""
    global is_analyzing, shared_face_data, cooldown_dict, voice_history, last_seen_dict
    global attack_counter, real_counter
    person_boxes, tailgate_logged = [], False  # 供 finally 的經過未打卡判斷使用

    try:
        # YOLO 人數偵測只服務打卡系統（尾隨、經過未打卡）；它出錯不能中斷人臉辨識與打卡
        try:
            _, person_boxes = sec.count_persons(frame_copy)
            person_boxes = pose.near_person_boxes(person_boxes)        # 只看離鏡頭最近與差不多近的人，背景路人不算
        except Exception as e:  # noqa: BLE001
            person_boxes = []
            print(f"[terminal_app][WARN] 人數偵測失敗，這一幀略過尾隨判斷：{type(e).__name__}")
        # 1. 臉部偵測
        rgb_img, detections = vc.face_detect_bgr(frame_copy, detector)
        result_packet = None

        if len(detections) >= 1:                            # 多人入鏡：只處理離鏡頭最近的那一張臉
            face_info = pose.nearest_face(detections)
            x1, y1, x2, y2 = (int(face_info['x1']), int(face_info['y1']), int(face_info['x2']), int(face_info['y2']))

            # 2. 活體偵測
            liveness_score = vc.check_liveness(frame_copy, (x1, y1, x2, y2))
            current_time = time.time()

            if liveness_score == -1.0:
                # 人臉不完整或在畫面邊緣，等下一幀
                return

            if liveness_score < 0.80:
                attack_counter += 1
                real_counter = 0

                # 連續 3 幀判定假臉才拒絕
                if attack_counter >= 3:
                    # 非活體只顯示畫面文字，不播語音
                    print(f"[terminal_app] 非活體判定：liveness_score={liveness_score:.4f}"
                          f"（門檻 0.80，分數越接近 1.0 越像真人，越接近 0.0 越像攻擊）")
                    backend.alert("spoof", "偵測到非真人（照片或螢幕翻拍）")
                    alert_sound.notify("spoof")

                    _publish_face_data({
                        'coords': (x1, y1, x2, y2),
                        'text': f"拒絕：非活體攻擊 (score={liveness_score:.3f})",
                        'color': (0, 0, 255),
                        'status': 'single',
                        'name': 'FAKE',
                        'emp_id': 'DENIED'
                    })
                return # 不進入身分比對

            else:
                attack_counter = 0
                real_counter += 1

                # 連續 2 幀判定真人才做 ArcFace 特徵萃取（較耗 CPU）
                if real_counter < 2:
                    _publish_face_data({
                        'coords': (x1, y1, x2, y2),
                        'text': "活體驗證中...",
                        'color': (0, 255, 255),
                        'status': 'single',
                        'name': 'SCANNING',
                        'emp_id': '---'
                    })
                    return

                # 3. 姿勢：明顯轉頭、低頭時不比對身分，只提示（側臉比對不準，會被誤報成「未登記臉孔」）
                frontal, hint, _pose = pose.frontal_check(face_info, max_yaw=TERMINAL_MAX_YAW,
                                                          max_roll=TERMINAL_MAX_ROLL, pitch_range=TERMINAL_PITCH)
                if not frontal:
                    _publish_face_data({
                        'coords': (x1, y1, x2, y2),
                        'text': hint,
                        'color': (0, 255, 255),
                        'status': 'single',
                        'name': 'SCANNING',
                        'emp_id': '---'
                    })
                    return

                # 4. 身分比對
                landmarks = [face_info['left_eye'], face_info['right_eye'], face_info['nose'],
                             face_info['left_lip'], face_info['right_lip']]
                aligned_face = vc.face_align(rgb_img, landmarks)
                face_DNA = vc.feature_extract(aligned_face)

                recognition = backend.recognize(face_DNA)
                db_name, emp_id = recognition.name, recognition.employee_id
                if recognition.known:
                    pass_monitor.note_scan(time.time())

                display_text = "掃描中..."
                color_bgr = (0, 255, 255)
                hold_seconds = 0.5
                success, greeting = False, ""

                if db_name in ["Unknown", "Empty DB"]:
                    display_text = "未登記臉孔，請聯繫管理員"
                    color_bgr = (0, 0, 255)
                else:
                    # 5. 打卡：剛成功的幾秒繼續顯示結果，之後冷卻期內只更新畫面，不寫資料庫、不播語音
                    phase = punch_state.punch_phase(current_time, last_seen_dict.get(db_name), RESULT_HOLD,
                                                    ATTENDANCE_COOLDOWN)
                    if phase == punch_state.SHOW_RESULT:
                        shown = cooldown_dict[db_name]
                        display_text, color_bgr = shown['msg'], shown['color']
                        success, greeting = shown.get('success', False), shown.get('greeting', '')
                        hold_seconds = RESULT_HOLD
                    elif phase == punch_state.COOLDOWN:
                        display_text = "打卡冷卻中..."
                        color_bgr = (200, 200, 200)
                    else:
                        outcome = _log_punch(recognition, current_time)
                        display_text, color_bgr = outcome.text, outcome.color
                        success, greeting = outcome.success, outcome.greeting
                        hold_seconds = RESULT_HOLD

                result_packet = {
                    'coords': (x1, y1, x2, y2),
                    'text': display_text,
                    'color': color_bgr,
                    'status': 'single',
                    'name': db_name,
                    'emp_id': emp_id,
                    'hold': hold_seconds,
                    'success': success,
                    'greeting': greeting
                }

            # 6. 尾隨偵測（YOLO）：只拍照存證並記錄時間，不覆蓋當事人的打卡結果
            person_count = len(person_boxes)
            if person_count >= 2:
                tailgate_logged = True
                try:
                    # 連續尾隨合併成一筆；只有新增事件時才產生證據照片
                    backend.anomaly("tailgate", f"畫面有 {person_count} 人，只辨識到 {len(detections)} 張臉",
                                    frame_copy, person_boxes[-1])
                except Exception as e:
                    print(f"[terminal_app][WARN] 異常事件寫入失敗（不影響警示與證據照片）: {e}")
                _publish_tailgate_alert(current_time)
                alert_sound.notify("tailgate")

            _publish_face_data(result_packet)

        else:
            # 畫面無人：計數器歸零
            attack_counter = 0
            real_counter = 0
            _publish_face_data(None)

    except Exception as e:
        print(f"[Analyze Error] 發生錯誤: {e}")
        traceback.print_exc()
    finally:
        _check_pass_through(frame_copy, person_boxes, tailgate_logged)
        is_analyzing = False

WINDOW_NAME = "Smart Access Terminal"


class _TerminalWindow:
    """前台角色的辨識視窗：放在主螢幕（筆電），F11／f 切全螢幕、Esc 離開；其他角色維持原本的視窗。"""

    def __init__(self, enabled):
        self.enabled, self.fullscreen, self.box = enabled, False, None
        if not enabled:
            return
        front_screens.enable_dpi_awareness()
        monitor = front_screens.pick_monitor(front_screens.list_monitors(), "recognition")
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
        if monitor is not None:
            x, y, width, height = front_screens.windowed_box(monitor)
            self.box = (x, y, min(1280, width), min(720, height))
            cv2.resizeWindow(WINDOW_NAME, self.box[2], self.box[3])
            cv2.moveWindow(WINDOW_NAME, x, y)
        if front_screens.load_pref("recognition_fullscreen"):
            self.set_fullscreen(True, remember=False)

    def set_fullscreen(self, on, remember=True):
        if not self.enabled or on == self.fullscreen:
            return
        self.fullscreen = on
        cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN if on else cv2.WINDOW_NORMAL)
        if not on and self.box is not None:
            cv2.resizeWindow(WINDOW_NAME, self.box[2], self.box[3])
            cv2.moveWindow(WINDOW_NAME, self.box[0], self.box[1])
        if remember:
            front_screens.save_pref("recognition_fullscreen", on)

    def handle_key(self, key):
        action = front_screens.fullscreen_key(key) if self.enabled else None
        if action == "toggle":
            self.set_fullscreen(not self.fullscreen)
        elif action == "leave":
            self.set_fullscreen(False)


def main():
    """開啟攝影機與視窗，主迴圈負責取畫面、送分析、繪製介面，按 q 結束並釋放資源。"""
    global is_analyzing, shared_face_data, backend
    backend = terminal_backend.create_backend()
    backend.start()
    vc.open_vision()  # 迴圈期間持有視覺模型
    window = _TerminalWindow(enabled=role_config.role() == "front")
    detector = vc.detector
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print("[terminal_app][ERROR] 開不起攝影機 0（確認有接攝影機、沒有別的程式在用、"
              "Windows 隱私設定允許桌面程式使用攝影機）")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    # 啟動常駐分析執行緒
    worker_thread = threading.Thread(target=_analysis_worker, args=(detector,), daemon=True)
    worker_thread.start()

    # 辨識結果快取，避免畫面閃爍
    ui_cache = None
    ui_cache_expire = 0
    note_text, note_at = "", 0.0

    while True:
        ret, raw_frame = cap.read()
        if time.time() - note_at >= 2.0:     # 每 2 秒：寫心跳給前台代理、更新後台連線提示（不要每幀都查）
            note_at = time.time()
            backend.heartbeat(bool(ret))
            note_text = backend.note()
        if not ret:
            print("[terminal_app][ERROR] 讀不到攝影機畫面（沒接、被別的程式占用，或 Windows 隱私設定擋住），"
                  "辨識程式結束。")
            break

        canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
        canvas[:] = (30, 30, 30)

        # 裁出攝影機畫面中央 540 像素寬，貼在畫布右側
        cam_w = 540
        start_x = (1280 - cam_w) // 2
        cam_frame = raw_frame[:, start_x : start_x + cam_w]
        canvas[0:720, 740:1280] = cam_frame

        # 分析空閒時才送新影格
        if not is_analyzing:
            is_analyzing = True
            try:
                _frame_queue.put_nowait(cam_frame.copy())
            except queue.Full:
                is_analyzing = False

        current_time = time.time()

        with _shared_data_lock:
            latest_face_data = shared_face_data
            latest_tailgate_ts = shared_tailgate_ts

        # 有新結果就更新快取；超過保留秒數（預設 0.5，打卡結果較久）沒更新才清空
        if latest_face_data is not None:
            ui_cache = latest_face_data
            ui_cache_expire = current_time + latest_face_data.get('hold', 0.5)
        elif current_time > ui_cache_expire:
            ui_cache = None

        # 左側資訊面板
        cv2.rectangle(canvas, (40, 40), (700, 680), (45, 45, 45), -1)
        canvas = utils.put_chinese_text(canvas, "Smart Access Terminal", (60, 70), (0, 255, 255), 35)

        # 依辨識結果繪製追蹤框與文字
        if ui_cache:
            if ui_cache.get('status') == 'single':
                cx1, cy1, cx2, cy2 = ui_cache['coords']
                draw_target(canvas, cx1 + 740, cy1, cx2 + 740, cy2, color=ui_cache['color'], length=20)

                n = str(ui_cache.get('name', ''))
                tid = str(ui_cache.get('emp_id', ''))
                msg = str(ui_cache.get('text', ''))
                clr = ui_cache.get('color', (255, 255, 255))

                if n not in ['FAKE', 'SCANNING']:
                    canvas = utils.put_chinese_text(canvas, f"員工姓名：{n}", (100, 180), (255, 255, 255), 32)
                    canvas = utils.put_chinese_text(canvas, f"員工編號：{tid}", (100, 250), (200, 200, 200), 28)

                canvas = utils.put_chinese_text(canvas, f"【{msg}】", (100, 350), clr, 45)

            elif ui_cache.get('status') == 'multiple':
                canvas = utils.put_chinese_text(canvas, "！ 偵測到多人進入 ！", (100, 350), (0, 0, 255), 45)
        else:
            canvas = utils.put_chinese_text(canvas, "等待臉孔進入...", (100, 350), (150, 150, 150), 30)

        # 打卡成功：鏡頭畫面下方置中的半透明黑框（姓名與問候，語音念的也是同一句）
        if ui_cache and ui_cache.get('status') == 'single' and ui_cache.get('success'):
            who = str(ui_cache.get('name', ''))
            view = utils.draw_message_box(
                canvas[0:720, 740:1280],
                [("打卡成功", (0, 255, 0), 36), (f"姓名 {who}　{ui_cache.get('greeting', '')}", (255, 255, 255), 30)],
                center=(270, 605), padding=20, gap=10)
            canvas[0:720, 740:1280] = view
        if note_text:
            canvas = utils.put_chinese_text(canvas, note_text, (100, 640), (0, 165, 255), 24)

        # 頂部橫幅：尾隨警示優先，其次是活體或安全性警告
        if latest_tailgate_ts is not None and (current_time - latest_tailgate_ts) < TAILGATE_BANNER_HOLD:
            cv2.rectangle(canvas, (0, 0), (1280, 60), (0, 140, 255), -1)
            canvas = utils.put_chinese_text(canvas, "警示：偵測到尾隨，未打卡者已於背景拍照存證",
                                            (240, 15), (255, 255, 255), 25)
        elif ui_cache and ui_cache.get('status') == 'single':
            _msg = str(ui_cache.get('text', ''))
            if "警告" in _msg or "拒絕" in _msg:
                cv2.rectangle(canvas, (0, 0), (1280, 60), (0, 0, 255), -1)
                canvas = utils.put_chinese_text(canvas, "SECURITY ALERT: HIGH RISK DETECTED",
                                                (350, 15), (255, 255, 255), 25)

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        canvas = utils.put_chinese_text(canvas, now_str, (100, 600), (150, 150, 150), 24)

        cv2.imshow(WINDOW_NAME, canvas)
        key = cv2.waitKeyEx(1)
        window.handle_key(key)
        if key == ord('q'):
            break

    # 結束：釋放攝影機、視窗、執行緒與模型
    backend.close()
    cap.release()
    cv2.destroyAllWindows()
    _frame_queue.put(None)
    audio_player.stop()
    vc.close_vision()
    sec.release_yolo()

if __name__ == "__main__":
    main()
