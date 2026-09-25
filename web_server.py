from flask import Flask, render_template, request, jsonify
import cv2
import numpy as np
import vision_core as vc
import database_mgr as db
import os
import socket
import psutil
import random
import string
import time

def _find_wifi_direct_adapter_ip():
    """
    【2026-09-06，新增：支援自製 WiFiDirectHotspotCore 熱點】
    跟 backend_main.py 裡的同名邏輯是同一套做法，這裡是這個檔案自己獨立的一份
    （這個函式在 backend_main.py 裡也有一份幾乎一模一樣的，屬於同一段程式碼被
    複製貼上到兩個檔案的既有狀況，這次沒有動手合併成共用模組，只先讓兩邊的
    邏輯保持一致，避免「GUI 那邊修好了、這裡卻沒修到」這種各講各話的情況）。

    透過 `ipconfig /all` 找出「裝置描述裡有英文『Wi-Fi Direct』字樣」的那張
    網卡，直接讀出它當下真正的 IPv4 位址；找不到就傳回 None，讓呼叫端改用
    原本「比對固定號碼／猜私有網段」的邏輯，不會讓這個函式本身出錯。

    【誠實補充】這段邏輯沒辦法在這個沙盒環境用真正的 Windows + WiFiDirect
    硬體驗證，只用模擬的 `ipconfig /all` 文字測過解析邏輯本身（細節見
    backend_main.py 那份的說明）。
    """
    import subprocess
    import re
    try:
        result = subprocess.run(
            ["ipconfig", "/all"],
            capture_output=True,
            timeout=5,
        )
        raw = result.stdout
        output = None
        for enc in ("cp950", "utf-8", "big5"):
            try:
                output = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if output is None:
            output = raw.decode("utf-8", errors="ignore")

        blocks, current = [], []
        for line in output.splitlines():
            if line and not line[0].isspace():
                if current:
                    blocks.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            blocks.append("\n".join(current))

        for block in blocks:
            if "Wi-Fi Direct" in block or "WiFi Direct" in block:
                # 【2026-09-07，修正：同一張網卡可能同時掛兩組 IPv4】
                # 跟 backend_main.py 那份同步修正：同一個網卡區塊裡可能一次
                # 列出兩組 IPv4 Address，真正在用的是「後面那組」，原本
                # re.search 只抓第一個會抓錯，改成 re.findall 抓全部後取
                # 最後一個。
                matches = re.findall(r"IPv4.*?:\s*([\d.]+)", block)
                if matches:
                    return matches[-1]
    except Exception as e:
        print(f"[WiFi Direct IP Discovery Error] {e}")
    return None


def get_local_ip_offline():
    """
    智慧化抓取本機 IPv4 位址，優先匹配虛擬熱點/ICS 網段。
    解決 Windows 11 Virtual AP 路由優先級導致抓錯實體網卡 IP 的問題。
    """
    try:
        # 第零優先：Windows 自己建立的 Wi-Fi Direct 虛擬網卡（例如
        # WiFiDirectHotspotCore 開的熱點），找到就直接用它實際拿到的 IP。
        wifi_direct_ip = _find_wifi_direct_adapter_ip()
        if wifi_direct_ip:
            return wifi_direct_ip

        hostname = socket.gethostname()
        _, _, ip_list = socket.gethostbyname_ex(hostname)
        if not ip_list:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(('8.8.8.8', 80))
                ip = s.getsockname()[0]
                s.close()
                return ip
            except:
                return '127.0.0.1'
        priority_ips = ['192.168.137.1', '192.168.5.1']
        for pip in priority_ips:
            if pip in ip_list:
                return pip
        private_ips = [ip for ip in ip_list if ip != '127.0.0.1' and (ip.startswith('192.168.') or ip.startswith('10.'))]
        if private_ips:
            gateway_like = [ip for ip in private_ips if ip.endswith('.1')]
            return gateway_like[0] if gateway_like else private_ips[0]
        for ip in ip_list:
            if ip != '127.0.0.1':
                return ip
    except Exception as e:
        print(f"[IP Discovery Error] {e}")
    return '127.0.0.1'

# --- Token 安全管理機制 (Sliding Window Cache) ---
valid_tokens = {} # { "token_string": expiry_timestamp }

def verify_token(token):
    """ 驗證 Token 是否存在且未過期 """
    if not token or token not in valid_tokens:
        return False
    if time.time() > valid_tokens[token]:
        del valid_tokens[token]
        return False
    return True

# --- Flask 應用初始化 (僅執行一次) ---
app = Flask(__name__, template_folder='templates')

@app.route('/api/internal/generate_token', methods=['GET'])
def internal_generate_token():
    """ 內部專用 API：由後台 UI 呼叫以生成合法的 Token """
    # 安全檢查：僅允許本機呼叫
    if request.remote_addr != '127.0.0.1':
        return jsonify({"error": "Forbidden"}), 403

    token = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    valid_tokens[token] = time.time() + 60 # 給予 60 秒有效期

    # 順便清理過期 Token (防止記憶體洩漏)
    now = time.time()
    expired_keys = [t for t, exp in valid_tokens.items() if now > exp]
    for k in expired_keys:
        del valid_tokens[k]

    return jsonify({"token": token})

@app.route('/')
def index():
    """ 手機連線驗證 Token，通過後才顯示註冊頁面 """
    token = request.args.get('t')
    if not verify_token(token):
        return '<div style="text-align:center; margin-top:50px; font-family:sans-serif;">' \
               '<h2 style="color:red;">❌ 授權碼無效或已過期</h2>' \
               '<p>請重新掃描電腦螢幕上的 QR Code</p></div>', 403
    return render_template('register.html')

@app.route('/api/register', methods=['POST'])
def api_register():
    """ 處理手機端註冊請求的 API (含最終 Token 核銷) """
    try:
        # 1. Token 最終核對
        token = request.form.get('token')
        if not verify_token(token):
            return jsonify({"status": "fail", "message": "安全驗證失敗：授權碼已失效，請重新掃描 QR Code"}), 403

        # 2. 取得表單資料
        name = request.form.get('name')
        emp_id = request.form.get('emp_id')
        gender = request.form.get('gender')
        img_file = request.files.get('image')

        if not name or not emp_id or not img_file or not gender:
            return jsonify({"status": "fail", "message": "錯誤:姓名、工號與照片皆為必填"})

        # 3. 影像處理與辨識
        filestr = img_file.read()
        npimg = np.frombuffer(filestr, np.uint8)
        frame = cv2.imdecode(npimg, cv2.IMREAD_COLOR)

        # 【資源規範】註冊是偶發請求：申請視覺模型 -> 辨識 -> 離開區塊即無條件釋放（不常駐）
        with vc.vision_session():
            rgb_img, detections = vc.face_detect_bgr(frame, vc.detector)
            if len(detections) != 1:
                return jsonify({"status": "fail", "message": "註冊失敗:請確保照片中有一張清晰可辨的人臉"})

            face_info = detections[0]
            landmarks = [face_info['left_eye'], face_info['right_eye'], face_info['nose'], face_info['left_lip'], face_info['right_lip']]
            aligned_face = vc.face_align(rgb_img, landmarks)
            face_dna = vc.feature_extract(aligned_face)

        # 4. 寫入資料庫（內含姓名/員工編號重複檢查，重複會直接被拒絕、不寫入）
        success, db_message = db.register_user(name, gender, emp_id, face_dna)

        if not success:
            print(f"[Web Server] 拒絕重複註冊: {db_message}")
            return jsonify({"status": "fail", "message": db_message}), 409

        # 5. 【核銷機制】註冊成功後，立刻將此 Token 從緩衝區刪除 (閱後即焚)
        valid_tokens.pop(token, None)

        print(f"[Web Server] 新員工註冊成功: {name} ({emp_id}) - Token 已核銷")
        return jsonify({"status": "success", "message": f"註冊成功! 歡迎加入，{name}。您可以關閉此頁面了。"})
    except Exception as e:
        print(f"[Web Server Error]{e}")
        return jsonify({"status": "error", "message": f"伺服器內部錯誤: {e}"})

if __name__ == '__main__':
    local_ip = get_local_ip_offline()
    print("\n" + "="*40)
    print("--- 遠端註冊伺服器 (Sliding Window 安全模式) 已啟動 ---")
    print(f"本地動態 IP: {local_ip}")
    print(f"請在手機瀏覽器輸入: http://{local_ip}:5000")
    print("="*40 + "\n")
    # debug=False 避免 Flask 的 reloader 導致記憶體隔離/二次啟動問題
    app.run(host='0.0.0.0', port=5000, debug=False)
