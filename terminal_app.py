import sys
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

# 【2026-09-18，控制台編碼保護】不同電腦的 Windows「非 Unicode 程式的語言」
# 設定（系統地區設定裡的作用中字碼頁）不一定相同：在家裡測試機是一種字碼頁，
# 帶到外部場地展示的電腦可能是另一種（例如簡體中文 936），這時候 Python 預設
# 用系統字碼頁去 print 中文字，終端機顯示出來就會是亂碼，但程式本身其實沒有
# 壞掉。這裡直接把標準輸出/錯誤輸出重新設定成 UTF-8，不管換到哪一台電腦、
# 系統字碼頁是什麼，終端機印出來的中文字都不會亂碼（errors='replace' 是保險，
# 萬一終端機本身真的無法顯示某個字元，顯示 ? 而不是直接噴例外讓程式中斷）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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

# 【2026-09-18，尾隨改為非阻斷式】尾隨警示改成獨立於個人打卡結果之外的狀態，
# 不再借用 shared_face_data 的 text/color 去蓋掉當事人自己的打卡結果文字。
# 背景分析執行緒寫、主執行緒讀，跟 shared_face_data 共用同一把鎖
# （_shared_data_lock，定義在下面）。
shared_tailgate_ts = None
TAILGATE_BANNER_HOLD = 3.0  # 尾隨提示橫幅至少停留幾秒，避免每幀忽隱忽現

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

def _publish_tailgate_alert(ts):
    """執行緒安全地記錄最近一次偵測到尾隨的時間，只由背景分析執行緒呼叫。"""
    global shared_tailgate_ts
    with _shared_data_lock:
        shared_tailgate_ts = ts

def _get_greeting_by_time():
    """
    【2026-09-18，語音訊息簡化】依目前時間回傳「早安／午安／晚安」問候語，
    取代原本打卡成功時固定播報的「打卡成功，祝您工作順利」「下班辛苦了」。
    分界：12 點前＝早安、12~17 點台＝午安、18 點以後＝晚安。直接用已經
    import 好的 time.localtime()，不用另外多 import datetime。
    """
    hour = time.localtime().tm_hour
    if hour < 12:
        return "早安"
    elif hour < 18:
        return "午安"
    else:
        return "晚安"

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
                    # 【2026-09-20】使用者要求：非活體不再播語音，只在畫面上顯示（見下方
                    # _publish_face_data 的紅字「拒絕：非活體攻擊」）。語音只保留辨識成功的問候。

                    # 【2026-09-18，加上實際分數方便判斷是門檻問題還是光線問題】
                    # 使用者反映關掉一盞燈、真人也會被判非活體，需要看到當下實際的
                    # liveness_score 數值，才能判斷是臨界值問題（例如剛好卡在 0.75
                    # 左右）還是分數掉很多的更嚴重光線適應問題。同時印在畫面上
                    # （方便現場直接看到）跟主控台（方便截圖/複製貼上回報）。
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
                    # 【2026-09-20】未登記臉孔不再播語音，只顯示上面的畫面文字。
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
                            # 【2026-09-20】語音只保留「辨識成功 → 某某某，早安/午安/晚安」。
                            # audio_msg 預設 None，只有上班/下班打卡成功兩個分支會設成問候語。
                            audio_msg = None

                            if "In:" in db_status_text:
                                display_text = "上班報到成功"
                                # 【2026-09-18，語音訊息簡化】使用者要求打卡成功語音
                                # 改成依時段問候（早安/午安/晚安），不用原本固定的
                                # 「打卡成功，祝您工作順利」。畫面文字 display_text
                                # 維持不變，使用者只要求簡化「語音」。
                                audio_msg = f"{db_name}，{_get_greeting_by_time()}"
                                color_bgr = (0, 255, 0)

                                # 【任務三】整合行事曆播報
                                # 【2026-09-20】使用者要求把行事曆語音加回來（上班打卡成功後才播）。
                                emp_id_val = db.get_employee_id(db_name)
                                event = db.get_today_event(emp_id_val)
                                if event:
                                    event_time, event_title = event
                                    calendar_text = f"{db_name}，{event_time}，{event_title}"
                                    # 用 audio_player（sherpa-onnx + sounddevice，Windows 可用），
                                    # 不用依賴 Linux aplay 的 voice_agent.py。
                                    audio_player.speak(calendar_text, "calendar")
                            elif "Out:" in db_status_text:
                                display_text = "下班打卡成功"
                                # 同上，下班打卡語音也改成依時段問候
                                audio_msg = f"{db_name}，{_get_greeting_by_time()}"
                                color_bgr = (0, 255, 255)
                            else:
                                display_text = db_status_text
                                # 【2026-09-20】其他狀態訊息（非成功打卡）不再播語音，只顯示畫面文字。
                                color_bgr = (255, 255, 255)

                            v_key = f"{db_name}_{display_text}"
                            if audio_msg and (current_time - voice_history.get(v_key, 0)) > VOICE_RESET_TIME:
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
            # 【2026-09-18，改為非阻斷式】尾隨偵測不再覆蓋 result_packet 的
            # text/color——原本這裡一偵測到尾隨，就會把當事人剛打卡成功/失敗
            # 的訊息直接蓋掉，變成畫面上只顯示「警告：偵測到疑似尾隨」，看起來
            # 像是打卡被擋下來；但其實 db.log_attendance() 在更早的地方（上面
            # 「確認為穩定真人，進入身分比對」那段）就已經執行完畢，只是 UI
            # 訊息被蓋掉而已。改成：背景拍照存證（不變，沿用 anomaly_logs 機制）
            # ＋ 獨立記錄尾隨發生時間，交給 main() 在畫面「上方」疊一個獨立的
            # 提示橫幅，不去動 result_packet，當事人自己的打卡結果維持正常顯示。
            person_count, person_boxes = sec.count_persons(frame_copy)
            if person_count >= 2:
                sec.generate_evidence(frame_copy, person_boxes[-1])
                _publish_tailgate_alert(current_time)
                # 【2026-09-20】尾隨不再播語音，只顯示畫面上方橫幅＋背景拍照存證。

            _publish_face_data(result_packet)

        elif len(detections) > 1:
            _publish_face_data({'status': 'multiple', 'text': "偵測到多人，請依序辨識", 'color': (0, 0, 255)})
            # 【2026-09-20】多人進入不再播語音，只顯示畫面文字「偵測到多人，請依序辨識」。
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
    vc.open_vision()  # 【資源規範】相機迴圈期間明確持有視覺模型（可被協調器驅逐，驅逐後下次推論自動重新申請）
    detector = vc.detector  # 只是借用代理，不持有模型
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
            latest_tailgate_ts = shared_tailgate_ts

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

            elif ui_cache.get('status') == 'multiple':
                canvas = utils.put_chinese_text(canvas, "！ 偵測到多人進入 ！", (100, 350), (0, 0, 255), 45)
        else:
            canvas = utils.put_chinese_text(canvas, "等待臉孔進入...", (100, 350), (150, 150, 150), 30)

        # --- 頂部橫幅：尾隨提示優先，其次是當事人自己的活體/安全性警告 ---
        # 【2026-09-18】尾隨橫幅獨立於 ui_cache 之外判斷、繪製：即使當事人自己
        # 的打卡訊息已經正常顯示在下方、甚至下一幀 ui_cache 已經換成別人或清空，
        # 尾隨警示都還是要在 TAILGATE_BANNER_HOLD 秒內持續顯示在畫面最上方，
        # 不會因為蓋掉 result_packet 而讓打卡流程「看起來」被擋下來。
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
        
    cap.release()
    cv2.destroyAllWindows()
    _frame_queue.put(None)  # 通知常駐分析執行緒可以結束了
    audio_player.stop()
    vc.close_vision()   # 【資源規範 D】無條件釋放視覺模型
    sec.release_yolo()

if __name__ == "__main__":
    main()