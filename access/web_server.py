"""
web_server.py：手機 QR Code 遠端註冊伺服器（Flask，port 5000）。

主要功能：
  - 後台產生 8 碼一次性 Token（60 秒有效，僅限本機呼叫）
  - 手機掃 QR Code 帶 Token 開啟註冊頁，驗證通過才顯示表單
  - 接收姓名、員工編號、性別與照片，萃取人臉特徵後寫入資料庫，成功後 Token 立即作廢
  - 自動找出本機區網 IP，優先使用 Wi-Fi Direct 熱點網卡

串接模組：
  - vision_core：人臉偵測、對齊、特徵萃取
  - database_mgr：register_user 寫入員工
  - templates/register.html：註冊頁面
  - 由 service_manager（backend_main 管理後台）啟動
"""
from flask import Flask, render_template, request, jsonify
import cv2
import numpy as np
from access import vision_core as vc
from shared import database_mgr as db
import os
import socket
import psutil
import random
import string
import time

def _find_wifi_direct_adapter_ip():
    """
    用 `ipconfig /all` 找出描述含「Wi-Fi Direct」的網卡並回傳它的 IPv4，找不到回傳 None。
    （service_manager.py 內有一份相同邏輯）
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

        # 依網卡切成區塊
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
                # 同一張網卡可能列出兩組 IPv4，取最後一組
                matches = re.findall(r"IPv4.*?:\s*([\d.]+)", block)
                if matches:
                    return matches[-1]
    except Exception as e:
        print(f"[WiFi Direct IP Discovery Error] {e}")
    return None


def get_local_ip_offline():
    """取得本機區網 IPv4：優先 Wi-Fi Direct 網卡，其次虛擬熱點網段，再其次一般私有網段。"""
    try:
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

# --- Token 管理 ---
valid_tokens = {} # { "token_string": expiry_timestamp }

def verify_token(token):
    """驗證 Token 是否存在且未過期，過期的順便刪除"""
    if not token or token not in valid_tokens:
        return False
    if time.time() > valid_tokens[token]:
        del valid_tokens[token]
        return False
    return True

app = Flask(__name__, template_folder='templates')

@app.route('/api/internal/generate_token', methods=['GET'])
def internal_generate_token():
    """內部 API：產生 60 秒有效的 Token，僅限本機呼叫"""
    if request.remote_addr != '127.0.0.1':
        return jsonify({"error": "Forbidden"}), 403

    token = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    valid_tokens[token] = time.time() + 60

    # 順便清掉已過期的 Token
    now = time.time()
    expired_keys = [t for t, exp in valid_tokens.items() if now > exp]
    for k in expired_keys:
        del valid_tokens[k]

    return jsonify({"token": token})

@app.route('/')
def index():
    """手機開啟頁面：Token 驗證通過才顯示註冊頁"""
    token = request.args.get('t')
    if not verify_token(token):
        return '<div style="text-align:center; margin-top:50px; font-family:sans-serif;">' \
               '<h2 style="color:red;">❌ 授權碼無效或已過期</h2>' \
               '<p>請重新掃描電腦螢幕上的 QR Code</p></div>', 403
    return render_template('register.html')

@app.route('/api/register', methods=['POST'])
def api_register():
    """處理手機端註冊請求：驗證 Token、萃取人臉特徵、寫入資料庫，成功後作廢 Token"""
    try:
        # 1. 驗證 Token
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

        # 3. 解碼照片並萃取人臉特徵（模型只在這個區塊內載入，離開即釋放）
        filestr = img_file.read()
        npimg = np.frombuffer(filestr, np.uint8)
        frame = cv2.imdecode(npimg, cv2.IMREAD_COLOR)

        with vc.vision_session():
            rgb_img, detections = vc.face_detect_bgr(frame, vc.detector)
            if len(detections) != 1:
                return jsonify({"status": "fail", "message": "註冊失敗:請確保照片中有一張清晰可辨的人臉"})

            face_info = detections[0]
            landmarks = [face_info['left_eye'], face_info['right_eye'], face_info['nose'], face_info['left_lip'], face_info['right_lip']]
            aligned_face = vc.face_align(rgb_img, landmarks)
            face_dna = vc.feature_extract(aligned_face)

        # 4. 寫入資料庫（姓名或員工編號重複會被拒絕）
        success, db_message = db.register_user(name, gender, emp_id, face_dna)

        if not success:
            print(f"[Web Server] 拒絕重複註冊: {db_message}")
            return jsonify({"status": "fail", "message": db_message}), 409

        # 5. 註冊成功，Token 立即作廢
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
    app.run(host='0.0.0.0', port=5000, debug=False)
