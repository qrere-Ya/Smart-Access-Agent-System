import cv2
import numpy as np
import vision_core as vc
import database_mgr as db
import time
import threading
import queue
import utils
import security as sec
from audio_player import SmartAudioPlayer
import os
import traceback

# --- 系統全域變數設定 ---
is_analyzing = False       
shared_face_data = None    

# 狀態冷卻與防抖機制
cooldown_dict = {}
DB_QUERY_COOLDOWN = 3.0
voice_history = {}
VOICE_RESET_TIME = 43200
last_seen_dict = {}        # 【任務一】記錄每位員工最後一次成功打卡的時間戳記
ATTENDANCE_COOLDOWN = 60   # 打卡冷卻時間 (秒)

# 【優化 1】：雙向防抖計數器
attack_counter = 0  # 連續假臉計數
real_counter = 0    # 連續真臉計數

audio_player = SmartAudioPlayer(speed=1.0)

# 【效能優化，2026-09-02】常駐分析工作執行緒 + 佇列，取代「每一幀重新產生一個新
# threading.Thread」的做法。執行緒的建立與銷毀本身有系統開銷，高幀率下會累積成
# 看得到的效能損耗。改成只建立一個常駐 worker，主迴圈透過 _frame_queue 把要分析
# 的影格交給它——同一時間依然只會有一個分析工作在跑（is_analyzing 這個旗標邏輯完全
# 不變，行為跟以前一樣會跳過忙碌時的幀），只是底層不用每次都重新生一個執行緒。
# maxsize=1：反正 is_analyzing 已經確保「還在分析時不會塞新影格進來」，這裡設 1
# 只是多一層保險，真的意外塞滿時用 put_nowait + queue.Full 保守處理，不讓主迴圈卡住。
_frame_queue = queue.Queue(maxsize=1)

# shared_face_data 是「背景分析執行緒寫、主執行緒讀」的共用資料，雖然在 CPython 裡
# 單純的物件參照賦值本身是原子操作，不會讀到「寫一半」的髒資料，但用一個輕量的
# Lock 包住讀寫兩端，是比較嚴謹、也對未來維護比較安全的做法（如果之後這裡的邏輯
# 從「整個物件一次換掉」變成「先讀再改」這種複合操作，沒有 Lock 就會有真正的
# race condition）。
_shared_data_lock = threading.Lock()

def _publish_face_data(value):
    """執行緒安全地更新 shared_face_data，只由背景分析執行緒呼叫。"""
    global shared_face_data
    with _shared_data_lock:
        shared_face_data = value

def _analysis_worker(detector):
    """
    常駐背景工作執行緒：不斷從 _frame_queue 拿最新一幀影像丟給 analyze_in_background()
    分析。取代原本「每一幀都重新開一個 threading.Thread」的做法。
    """
    while True:
        frame_copy = _frame_queue.get()
        if frame_copy is None:  # 收到停止信號
            break
        analyze_in_background(frame_copy, detector)
        _frame_queue.task_done()

def draw_target(img, x1, y1, x2, y2, color=(255, 200, 0), length=40):
    """ 繪製科技感對焦框 """
    cv2.line(img, (x1, y1), (x1 + length, y1), color, 3)
    cv2.line(img, (x1, y1), (x1, y1 + length), color, 3)
    cv2.line(img, (x2, y1), (x2 - length, y1), color, 3)
    cv2.line(img, (x2, y1), (x2, y1 + length), color, 3)
    cv2.line(img, (x1, y2), (x1 + length, y2), color, 3)
    cv2.line(img, (x1, y2), (x1, y2 - length), color, 3)
    cv2.line(img, (x2, y2), (x2 - length, y2), color, 3)
    cv2.line(img, (x2, y2), (x2, y2 - length), color, 3)

def analyze_in_background(frame_copy, detector):
    """ 核心 AI 分析執行緒 """
    global is_analyzing, shared_face_data, cooldown_dict, voice_history, last_seen_dict
    global attack_counter, real_counter
    
    try:
        # 1. 臉部偵測
        rgb_img, detections = vc.face_detect_bgr(frame_copy, detector)
        result_packet = None

        if len(detections) == 1:
            face_info = detections[0]
            x1, y1, x2, y2 = (int(face_info['x1']), int(face_info['y1']), int(face_info['x2']), int(face_info['y2']))
            
            # --- 活體偵測第一關 ---
            liveness_score = vc.check_liveness(frame_copy, (x1, y1, x2, y2))
            current_time = time.time()

            if liveness_score == -1.0:
                # 人臉不完整/在邊緣：不破壞既有 UI，直接 return 等待下一幀
                return

            # 【優化 2】：稍微放寬門檻至 0.80，減少殘影誤判
            if liveness_score < 0.80: 
                attack_counter += 1
                real_counter = 0 # 假臉出現，真臉計數歸零

                # 連續 3 幀確認為假臉，才觸發警報
                if attack_counter >= 3:
                    if (current_time - voice_history.get("Liveness_Fail", 0)) > 5.0:
                        audio_player.speak("警告，偵測到非活體入侵", "security")
                        voice_history["Liveness_Fail"] = current_time

                    _publish_face_data({
                        'coords': (x1, y1, x2, y2),
                        'text': "⚠️ 拒絕：非活體攻擊",
                        'color': (0, 0, 255),
                        'status': 'single',
                        'name': 'FAKE',
                        'emp_id': 'DENIED'
                    })
                return # 絕對不放行到下一步 (省下 ArcFace 算力)

            else:
                # 分數 >= 0.80 (是真人)
                attack_counter = 0
                real_counter += 1

                # 【優化 3】：連續 2 幀確認是穩定的真人，才啟動極吃 CPU 的 ArcFace 特徵提取
                if real_counter < 2:
                    # 還在確認中，先更新框框位置給 UI，讓畫面看起來順暢
                    _publish_face_data({
                        'coords': (x1, y1, x2, y2),
                        'text': "活體驗證中...",
                        'color': (0, 255, 255),
                        'status': 'single',
                        'name': 'SCANNING',
                        'emp_id': '---'
                    })
                    return

                # --- 確認為穩定真人，進入身分比對 ---
                landmarks = [face_info['left_eye'], face_info['right_eye'], face_info['nose'], face_info['left_lip'], face_info['right_lip']]
                aligned_face = vc.face_align(rgb_img, landmarks)
                face_DNA = vc.feature_extract(aligned_face)
                
                # 獲取資料庫姓名
                db_name, _ = db.recognize_face(face_DNA)
                emp_id = db.get_employee_id(db_name) if db_name not in ["Unknown", "Empty DB"] else "N/A"

                display_text = "掃描中..."
                color_bgr = (0, 255, 255)

                if db_name in ["Unknown", "Empty DB"]:
                    display_text = "未登記臉孔，請聯繫管理員"
                    color_bgr = (0, 0, 255)
                    if (current_time - voice_history.get("Unknown_Voice", 0)) > 5.0:
                        audio_player.speak("查無資料，請先完成註冊", "system")
                        voice_history["Unknown_Voice"] = current_time
                else:
                    # 【任務一】打卡冷卻機制 (Debounce)
                    # 檢查距離上次成功打卡是否超過 60 秒
                    if (current_time - last_seen_dict.get(db_name, 0)) < ATTENDANCE_COOLDOWN:
                        # 在冷卻期間，僅更新 UI 狀態，跳過資料庫寫入與語音播報
                        display_text = "打卡冷卻中..."
                        color_bgr = (200, 200, 200)
                    else:
                        # 過冷卻期，執行正式打卡邏輯
                        if db_name not in cooldown_dict or (current_time - cooldown_dict[db_name]['time']) > DB_QUERY_COOLDOWN:
                            db_status_text = db.log_attendance(db_name)

                            if "In:" in db_status_text:
                                display_text = "上班報到成功"
                                audio_msg = f"员工 {db_name}，打卡成功，祝您工作順利"
                                color_bgr = (0, 255, 0)

                                # 【任務三】整合行事曆播報
                                emp_id_val = db.get_employee_id(db_name)
                                event = db.get_today_event(emp_id_val)
                                if event:
                                    event_time, event_title = event
                                    calendar_text = f"{db_name}，{event_time}，{event_title}"
                                    # 【修正】原本呼叫 voice_agent.py（依賴 Linux 專用的 aplay 指令），
                                    # 在 Windows 上會靜默失敗、完全不會播報。改用專案裡確定能在
                                    # Windows 正常運作的 audio_player（sherpa-onnx + sounddevice）。
                                    audio_player.speak(calendar_text, "calendar")
                            elif "Out:" in db_status_text:
                                display_text = "下班打卡成功"
                                audio_msg = f"员工 {db_name}，下班辛苦了"
                                color_bgr = (0, 255, 255)
                            else:
                                display_text = db_status_text
                                audio_msg = f"员工 {db_name}，{db_status_text}"
                                color_bgr = (255, 255, 255)

                            v_key = f"{db_name}_{display_text}"
                            if (current_time - voice_history.get(v_key, 0)) > VOICE_RESET_TIME:
                                audio_player.speak(audio_msg, f"user_{db_name}")
                                voice_history[v_key] = current_time

                            # 更新打卡時間戳記 (觸發冷卻)
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

            # --- 防尾隨安防檢測 (YOLO) ---
            person_count, person_boxes = sec.count_persons(frame_copy)
            if person_count >= 2:
                sec.generate_evidence(frame_copy, person_boxes[-1])
                if result_packet:
                    result_packet['text'] = "警告：偵測到疑似尾隨"
                    result_packet['color'] = (0, 0, 255)
                    if (current_time - voice_history.get("Tailgate_Alert", 0)) > 5.0:
                        audio_player.speak("警告，偵測到疑似尾隨", "security")
                        voice_history["Tailgate_Alert"] = current_time

            _publish_face_data(result_packet)

        elif len(detections) > 1:
            _publish_face_data({'status': 'multiple', 'text': "偵測到多人，請依序辨識", 'color': (0, 0, 255)})
            if (time.time() - voice_history.get("Multi_Voice", 0)) > 5.0:
                audio_player.speak("偵測到多人進入，請依序辨識", "multi")
                voice_history["Multi_Voice"] = time.time()
        else:
            # 畫面中無人，將計數器清零
            attack_counter = 0
            real_counter = 0
            _publish_face_data(None)

    except Exception as e:
        print(f"[Analyze Error] 發生錯誤: {e}")
        traceback.print_exc() 
    finally:
        is_analyzing = False

def main():
    global is_analyzing, shared_face_data
    db.init_db()
    detector = vc.detector
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    # 【效能優化】啟動常駐分析工作執行緒（只建立一次），取代「每一幀重新開執行緒」
    worker_thread = threading.Thread(target=_analysis_worker, args=(detector,), daemon=True)
    worker_thread.start()

    # 【優化 4】：UI 視覺暫留快取 (解決畫面閃爍與卡頓感)
    ui_cache = None
    ui_cache_expire = 0

    while True:
        ret, raw_frame = cap.read()
        if not ret: break

        canvas = np.zeros((720, 1280, 3), dtype=np.uint8)
        canvas[:] = (30, 30, 30) 

        cam_w = 540
        start_x = (1280 - cam_w) // 2
        cam_frame = raw_frame[:, start_x : start_x + cam_w]
        canvas[0:720, 740:1280] = cam_frame

        if not is_analyzing:
            is_analyzing = True
            try:
                # 【效能優化】丟進佇列給常駐 worker 處理，不再每一幀都重新開一個執行緒
                _frame_queue.put_nowait(cam_frame.copy())
            except queue.Full:
                # 理論上不會發生（is_analyzing 已經擋住重複送幀），保守處理避免卡死主迴圈
                is_analyzing = False

        # --- UI 快取邏輯 ---
        current_time = time.time()

        with _shared_data_lock:
            latest_face_data = shared_face_data

        if latest_face_data is not None:
            ui_cache = latest_face_data
            ui_cache_expire = current_time + 0.5  # 資料保留 0.5 秒 (防閃爍緩衝)
        elif current_time > ui_cache_expire:
            ui_cache = None # 超過 0.5 秒沒更新，才真正清空畫面

        # --- 繪製背景 ---
        # 【2026-09-15，依你的決定移除】原本這裡會先呼叫 draw_target() 畫一個固定位置、
        # 不會動的灰色 ROI 引導框（提示使用者把臉對準哪個位置）。你去別家公司看過對方的
        # 產品後，覺得這個引導框沒有必要，這裡拿掉了。會跟著偵測到的臉部移動的追蹤框
        # （下面 `if ui_cache.get('status') == 'single':` 區塊裡那次 draw_target() 呼叫）
        # 沒有被動到，維持原樣。
        cv2.rectangle(canvas, (40, 40), (700, 680), (45, 45, 45), -1)
        canvas = utils.put_chinese_text(canvas, "Smart Access Terminal", (60, 70), (0, 255, 255), 35)
        
        # --- 依據快取繪製資訊 ---
        if ui_cache:
            if ui_cache.get('status') == 'single':
                # 繪製人臉追蹤框 (隨臉移動)
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
                
                if "警告" in msg or "拒絕" in msg:
                    cv2.rectangle(canvas, (0, 0), (1280, 60), (0, 0, 255), -1)
                    canvas = utils.put_chinese_text(canvas, "SECURITY ALERT: HIGH RISK DETECTED", (350, 15), (255, 255, 255), 25)
            
            elif ui_cache.get('status') == 'multiple':
                canvas = utils.put_chinese_text(canvas, "！ 偵測到多人進入 ！", (100, 350), (0, 0, 255), 45)
        else:
            canvas = utils.put_chinese_text(canvas, "等待臉孔進入...", (100, 350), (150, 150, 150), 30)

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        canvas = utils.put_chinese_text(canvas, now_str, (100, 600), (150, 150, 150), 24)

        cv2.imshow("Smart Access Terminal", canvas)
        if cv2.waitKey(1) & 0xFF == ord('q'): break
        
    cap.release()
    cv2.destroyAllWindows()
    audio_player.stop()
    _frame_queue.put(None)  # 通知常駐分析執行緒可以結束了

if __name__ == "__main__":
    main()