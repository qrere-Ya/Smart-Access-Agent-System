import cv2
import numpy as np
import vision_core as vc
import database_mgr as db
import time
import threading
import utils
import security as sec

from PIL import Image, ImageDraw, ImageFont

# 全域變數
is_analyzing = False
shared_face_data = None
cooldown_dict = {}
COOLDOWN_TIME = 8

def analyze_in_background(frame_copy, detector):
    global is_analyzing, shared_face_data, cooldown_dict

    try:
        rgb_img, detections = vc.face_detect_bgr(frame_copy, detector)

        # 避免沒抓到臉時出錯
        if len(detections) > 0:
            face_info = detections[0] # 處理畫面中的第一張臉
            # 從字典中取出座標並畫出人臉的綠色框框
            x1, y1, x2, y2 = (face_info['x1'], face_info['y1'], 
                            face_info['x2'], face_info['y2'],
            )
            
            # 進行人臉辨識 (打卡模式)
            # 抓出 5 個特徵點
            landmarks = [
                face_info['left_eye'], face_info['right_eye'], 
                face_info['nose'], face_info['left_lip'], 
                face_info['right_lip']
            ]
            # 對齊人臉 -> 萃取 DNA -> 去資料庫比對
            aligned_face = vc.face_align(rgb_img, landmarks)
            face_DNA = vc.feature_extract(aligned_face)
            name, distance = db.recognize_face(face_DNA)

            # 冷卻與打卡邏輯
            if name == "Unknown":
                display_text = "訪客（未登記）"
                color = (0, 0, 255 ) # 紅色警告
            
            elif name == "Empty DB":
                display_text = "資料庫為空！按 [E] 註冊"
                color = (0, 0 , 255) # 紅色警告

            else:
                current_time = time.time()
                # 如果這個人是第一次出現，或是距離上次打卡已經超過冷卻時間
                if name not in cooldown_dict or (current_time - cooldown_dict[name]['time']) > COOLDOWN_TIME:
                    display_text = db.log_attendance(name)
                    cooldown_dict[name] = {'time': current_time, 'msg': display_text}
                    current_color = (0, 255, 0) # 綠色

                    person_count, person_boxes = sec.count_persons(frame_copy)
                    
                    if person_count >= 2:
                        print("偵測到疑似尾隨！")
                        suspicious_box = person_boxes[-1]
                        save_path = sec.generate_evidence(frame_copy, suspicious_box)
                        print(f"以存檔圖片: {save_path}")

                        display_text = f"WARNING TAILGATING"
                        current_color = (0, 0, 255) # 變成紅色

                    # 同樣把警告訊息存入字典讓它在畫面上停留
                    cooldown_dict[name] = {'time': current_time, 'msg': display_text, 'color': current_color}
                    color = current_color

                else:
                    time_passed = current_time - cooldown_dict[name]['time']
                    
                    if time_passed < 3.0:
                        display_text = cooldown_dict[name]['msg']
                        color = cooldown_dict[name]['color']
                    
                    else:
                        remain_time = int(COOLDOWN_TIME - time_passed)
                        display_text = f"{name} (Wait {remain_time}s)"
                        color = (0, 255, 255) # 黃色
            
            # 把算好的結果寫到白板上，讓主迴圈去畫
            shared_face_data = {'coords': (x1, y1, x2, y2), 'text': display_text, 'color': color, 'dna': face_DNA}
        
        else:
            shared_face_data = None # 畫面沒人臉
    
    except Exception as e:
        print(f"背景分析錯誤: {e}")
    
    finally:
        is_analyzing = False

def main():
    global is_analyzing, shared_face_data

    print("啟動智能門禁系統")
    db.init_db()
    # 使用 vision_core 裡面已經初始化好的 detector 和 sess
    detector = vc.detector
    
    # 開啟 Webcam
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1080)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 640)

    while True:
        ret, frame = cap.read()
        if not ret: break

        # 在畫面上加入 無觸控 UI 提示文字
        cv2.putText(frame, "[E] Register | [V] Visitor | [Q] Quit", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        
        # 核心多執行緒邏輯
        if not is_analyzing:
            is_analyzing = True #上鎖
            frame_copy = frame.copy()
            # 啟動背景執行緒
            threading.Thread(target=analyze_in_background, args=(frame_copy, detector)).start()

        if shared_face_data is not None:
            x1, y1, x2, y2 = shared_face_data['coords']
            text = shared_face_data['text']
            color = shared_face_data['color']

            # 把文字寫在人臉框框的上方
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            text_y = max(0, y1 - 30)
            frame = utils.put_chinese_text(frame, text, (x1, text_y), text_color=color, font_size=24)

        # 顯示最終畫面
        cv2.imshow("Smart Access System", frame)

        # ---------------------------------------------------------
        # 鍵盤監聽與狀態機
        # ---------------------------------------------------------
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break # 按 Q 退出程式
        elif key == ord('e'):
            # 按 E 註冊新員工
            # 當按下 E 時，終端機會暫停，要求你輸入名字
            if shared_face_data is not None and shared_face_data['dna'] is not None:
                print("\n=== 進入註冊模式 ===")
                # 必須要鏡頭前有臉，才能註冊！
                new_name = input("請輸入新員工姓名 (輸入玩按 Enter): ")
                # 呼叫 db 的註冊功能！
                db.register_user(new_name, shared_face_data['dna'])
                print(f"{new_name} 註冊成功! 請查看鏡頭。")
            else:
                print("畫面中沒有人臉，無法註冊！請站到鏡頭前。")
        
        elif key == ord('v'):
            # 按 V 訪客模式 (我們之後 Phase 4 再來接 OpenClaw 跟 Line)
            print("\n=== 訪客模式 ===")
            print("系統語音：您好，請問今天要拜訪哪位員工？(此功能建置中)")

    # 釋放資源
    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()