"""
web_server.py：手機 QR Code 遠端註冊伺服器（Flask，port 5000）。

主要功能：
  - 後台產生 8 碼一次性 Token（60 秒有效，僅限本機呼叫）
  - 手機掃 QR Code 帶 Token 開啟註冊頁，驗證通過才顯示表單
  - 接收姓名、員工編號、性別與照片，萃取人臉特徵後寫入資料庫，成功後 Token 立即作廢
  - 開啟時所有網卡位址都是註冊通道（shared.phone_address）：啟動時印出每個可連的網址，
    Wi-Fi Direct 熱點與區網優先、VPN 排最後

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
from shared import phone_address
import secrets
import string
import time

def get_local_ip_offline():
    """最適合手機的本機區網 IPv4（邏輯在 shared.phone_address）；完全沒有位址回 127.0.0.1。"""
    try:
        return phone_address.best_ip() or "127.0.0.1"
    except Exception as e:  # noqa: BLE001
        print(f"[IP Discovery Error] {e}")
        return "127.0.0.1"


def print_channels(port=5000, addresses=None):
    """啟動時印出所有註冊通道（這台電腦每個可用位址都能連，建議的那個標星號）。"""
    addresses = phone_address.list_addresses() if addresses is None else addresses
    if not addresses:
        print("找不到區網位址：請讓筆電連上 Wi-Fi 或開行動熱點，手機才連得到。")
        return
    chosen = phone_address.best(addresses)
    print("請在手機瀏覽器輸入下面任一個網址（手機要和這台電腦在同一個網路）：")
    for item in addresses:
        mark = "★建議 " if item is chosen else "       "
        print(f"  {mark}http://{item['ip']}:{port}   {item['label']}")


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

    token = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
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
            landmarks = [face_info['left_eye'], face_info['right_eye'], face_info['nose'],
                         face_info['left_lip'], face_info['right_lip']]
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
        print(f"[Web Server Error]{type(e).__name__}: {e}")
        return jsonify({"status": "error", "message": "伺服器內部錯誤，請稍後再試或通知管理員"}), 500

if __name__ == '__main__':
    print("\n" + "="*40)
    print("--- 遠端註冊伺服器 (Sliding Window 安全模式) 已啟動 ---")
    print_channels(5000)
    print("="*40 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False)  # noqa: S104
