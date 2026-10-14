"""
terminal_app.py：門禁現場即時人臉辨識主程式。

主要功能：
  - 讀取攝影機畫面，由背景執行緒做人臉偵測、活體偵測、ArcFace 身分比對
  - 辨識成功後寫入打卡紀錄，語音問候並播報當日行事曆
  - 偵測放行瞬間是否有多人（尾隨），畫面顯示橫幅警示並拍照存證
  - 以 OpenCV 視窗顯示辨識結果，按 q 離開

串接模組：
  - vision_core：人臉偵測、對齊、特徵萃取、活體偵測
  - database_mgr：特徵比對、打卡紀錄、行事曆查詢
  - security：YOLO 人數統計與尾隨證據圖
  - audio_player：TTS 語音播報
  - utils：畫面中文字繪製
  - 由 service_manager（backend_main 管理後台）啟動
"""
import sys
import cv2
import numpy as np
from access import vision_core as vc
from shared import database_mgr as db
import time
import threading
import queue
from access import utils
from access import security as sec
from access.audio_player import SmartAudioPlayer
import os
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
DB_QUERY_COOLDOWN = 3.0    # 同一人重複寫入資料庫的最短間隔（秒）
voice_history = {}
VOICE_RESET_TIME = 43200   # 同一句問候語再次播報的間隔（秒，12 小時）
last_seen_dict = {}        # 每位員工最後一次成功打卡的時間
ATTENDANCE_COOLDOWN = 60   # 打卡冷卻時間（秒）

# 尾隨警示狀態，與個人打卡結果分開存放
shared_tailgate_ts = None
TAILGATE_BANNER_HOLD = 3.0  # 尾隨橫幅最少顯示秒數

# 活體連續判定計數器
attack_counter = 0  # 連續假臉幀數
real_counter = 0    # 連續真臉幀數

audio_player = SmartAudioPlayer(speed=1.0)

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

def _get_greeting_by_time():
    """依目前時段回傳問候語：12 點前早安、18 點前午安、其餘晚安。"""
    hour = time.localtime().tm_hour
    if hour < 12:
        return "早安"
    elif hour < 18:
        return "午安"
    else:
        return "晚安"

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

def analyze_in_background(frame_copy, detector):
    """單幀完整分析：人臉偵測 → 活體 → 身分比對與打卡 → 尾隨偵測，結果寫入共用狀態。"""
    global is_analyzing, shared_face_data, cooldown_dict, voice_history, last_seen_dict
    global attack_counter, real_counter

    try:
        # 1. 臉部偵測
        rgb_img, detections = vc.face_detect_bgr(frame_copy, detector)
        result_packet = None

        if len(detections) == 1:
            face_info = detections[0]
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

                    _publish_face_data({
                        'coords': (x1, y1, x2, y2),
                        'text': f"⚠️ 拒絕：非活體攻擊 (score={liveness_score:.3f})",
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

                # 3. 身分比對
                landmarks = [face_info['left_eye'], face_info['right_eye'], face_info['nose'], face_info['left_lip'], face_info['right_lip']]
                aligned_face = vc.face_align(rgb_img, landmarks)
                face_DNA = vc.feature_extract(aligned_face)

                db_name, _ = db.recognize_face(face_DNA)
                emp_id = db.get_employee_id(db_name) if db_name not in ["Unknown", "Empty DB"] else "N/A"

                display_text = "掃描中..."
                color_bgr = (0, 255, 255)

                if db_name in ["Unknown", "Empty DB"]:
                    display_text = "未登記臉孔，請聯繫管理員"
                    color_bgr = (0, 0, 255)
                else:
                    # 4. 打卡：冷卻期內只更新畫面，不寫資料庫、不播語音
                    if (current_time - last_seen_dict.get(db_name, 0)) < ATTENDANCE_COOLDOWN:
                        display_text = "打卡冷卻中..."
                        color_bgr = (200, 200, 200)
                    else:
                        if db_name not in cooldown_dict or (current_time - cooldown_dict[db_name]['time']) > DB_QUERY_COOLDOWN:
                            db_status_text = db.log_attendance(db_name)
                            # 只有上班、下班打卡成功才會播報問候語
                            audio_msg = None

                            if "In:" in db_status_text:
                                display_text = "上班報到成功"
                                audio_msg = f"{db_name}，{_get_greeting_by_time()}"
                                color_bgr = (0, 255, 0)

                                # 上班打卡後播報今日行事曆
                                emp_id_val = db.get_employee_id(db_name)
                                event = db.get_today_event(emp_id_val)
                                if event:
                                    event_time, event_title = event
                                    calendar_text = f"{db_name}，{event_time}，{event_title}"
                                    audio_player.speak(calendar_text, "calendar")
                            elif "Out:" in db_status_text:
                                display_text = "下班打卡成功"
                                audio_msg = f"{db_name}，{_get_greeting_by_time()}"
                                color_bgr = (0, 255, 255)
                            else:
                                display_text = db_status_text
                                color_bgr = (255, 255, 255)

                            v_key = f"{db_name}_{display_text}"
                            if audio_msg and (current_time - voice_history.get(v_key, 0)) > VOICE_RESET_TIME:
                                audio_player.speak(audio_msg, f"user_{db_name}")
                                voice_history[v_key] = current_time

                            # 記錄打卡時間以啟動冷卻
                            last_seen_dict[db_name] = current_time
                            cooldown_dict[db_name] = {'time': current_time, 'msg': display_text, 'color': color_bgr}
                        else:
                            display_text = cooldown_dict[db_name]['msg']
                            color_bgr = cooldown_dict[db_name]['color']

                result_packet = {
                    'coords': (x1, y1, x2, y2),
                    'text': display_text,
                    'color': color_bgr,
                    'status': 'single',
                    'name': db_name,
                    'emp_id': emp_id
                }

            # 5. 尾隨偵測（YOLO）：只拍照存證並記錄時間，不覆蓋當事人的打卡結果
            person_count, person_boxes = sec.count_persons(frame_copy)
            if person_count >= 2:
                sec.generate_evidence(frame_copy, person_boxes[-1])
                _publish_tailgate_alert(current_time)

            _publish_face_data(result_packet)

        elif len(detections) > 1:
            # 多人同框：只顯示提示，不辨識
            _publish_face_data({'status': 'multiple', 'text': "偵測到多人，請依序辨識", 'color': (0, 0, 255)})
        else:
            # 畫面無人：計數器歸零
            attack_counter = 0
            real_counter = 0
            _publish_face_data(None)

    except Exception as e:
        print(f"[Analyze Error] 發生錯誤: {e}")
        traceback.print_exc()
    finally:
        is_analyzing = False

def main():
    """開啟攝影機與視窗，主迴圈負責取畫面、送分析、繪製介面，按 q 結束並釋放資源。"""
    global is_analyzing, shared_face_data
    db.init_db()
    vc.open_vision()  # 迴圈期間持有視覺模型
    detector = vc.detector
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    # 啟動常駐分析執行緒
    worker_thread = threading.Thread(target=_analysis_worker, args=(detector,), daemon=True)
    worker_thread.start()

    # 辨識結果快取，避免畫面閃爍
    ui_cache = None
    ui_cache_expire = 0

    while True:
        ret, raw_frame = cap.read()
        if not ret: break

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

        # 有新結果就更新快取；超過 0.5 秒沒更新才清空
        if latest_face_data is not None:
            ui_cache = latest_face_data
            ui_cache_expire = current_time + 0.5
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

        # 頂部橫幅：尾隨警示優先，其次是活體或安全性警告
        if latest_tailgate_ts is not None and (current_time - latest_tailgate_ts) < TAILGATE_BANNER_HOLD:
            cv2.rectangle(canvas, (0, 0), (1280, 60), (0, 140, 255), -1)
            canvas = utils.put_chinese_text(canvas, "警示：偵測到尾隨，未打卡者已於背景拍照存證", (240, 15), (255, 255, 255), 25)
        elif ui_cache and ui_cache.get('status') == 'single':
            _msg = str(ui_cache.get('text', ''))
            if "警告" in _msg or "拒絕" in _msg:
                cv2.rectangle(canvas, (0, 0), (1280, 60), (0, 0, 255), -1)
                canvas = utils.put_chinese_text(canvas, "SECURITY ALERT: HIGH RISK DETECTED", (350, 15), (255, 255, 255), 25)

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        canvas = utils.put_chinese_text(canvas, now_str, (100, 600), (150, 150, 150), 24)

        cv2.imshow("Smart Access Terminal", canvas)
        if cv2.waitKey(1) & 0xFF == ord('q'): break

    # 結束：釋放攝影機、視窗、執行緒與模型
    cap.release()
    cv2.destroyAllWindows()
    _frame_queue.put(None)
    audio_player.stop()
    vc.close_vision()
    sec.release_yolo()

if __name__ == "__main__":
    main()
