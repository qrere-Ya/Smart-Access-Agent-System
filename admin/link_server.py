"""
link_server.py：後台的雙機連線服務（Flask，只給前台使用，只綁網路線那張網卡的位址）。

主要功能：
  - GET  /v1/ping    ：回應與後台時間（前台用來校正時鐘、判斷後台是否在線）
  - GET  /v1/faces   ：給前台的人臉快照（編號、姓名、員工編號、角色、特徵與整份資料的版本）；
                       帶 ?have=版本 且沒變時只回 unchanged，不重送
  - GET  /v1/morning ：最新晨報（只有科技新聞；日報與出勤資料不會從這裡出去）
  - POST /v1/events  ：前台送來的打卡、尾隨、經過未打卡事件（證據照片用 base64 的 JPEG）
      · 打卡只信員工編號，姓名由後台查；事件時間用前台送來的（離線補送也能算在正確的時間）
      · event_id 已經收過就回 duplicate，不再寫入，所以前台可以放心重送
  - 安全：每個請求都要 Authorization: Bearer 通行碼（常數時間比對，錯誤一律回 401 不透露原因）、請求大小上限、
    欄位驗證（link_protocol）、證據照片只收 JPEG 並由後台決定檔名；服務只綁設定裡的內網位址、不接受 0.0.0.0
  - 回應碼：200 成功或重複；400 欄位不合格（永久，不要重送）；401 通行碼不對；413 太大；422 找不到這位員工
    （永久）；5xx 暫時錯誤（可以重送）

串接模組：
  - shared.link_protocol：驗證與編碼；shared.role_config：角色、位址、通行碼
  - shared.database_mgr：人臉快照、打卡、異常事件、收件去重；shared.digest_store：晨報
  - 之後由 service_manager 與 backend_main 啟動（雙機模式）；單機模式不會啟動這個服務

不做的事：沒有 HTTPS（網路線直連、只放行前台 IP 的防火牆規則由 N2c 處理）、沒有登入失敗鎖定
（通行碼 32 字元以上，直連網路不是暴力破解的目標）；這兩點是已知限制。
"""
import datetime
import os
import sys

from flask import Flask, jsonify, request

from access import pass_through
from shared import database_mgr as db
from shared import digest_store, link_protocol as lp, paths, role_config

_EVIDENCE_PREFIX = {"tailgate": "tailgate", pass_through.EVENT_TYPE: pass_through.EVENT_TYPE}


def create_app(now=datetime.datetime.now, token_matches=role_config.token_matches):
    """建立 Flask 應用；now 與 token_matches 可以換成假的方便測試。"""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = lp.MAX_BODY_BYTES

    def reply(payload, status=200):
        payload = {**payload, "server_time": lp.format_ts(now())}
        response = jsonify(payload)
        response.status_code = status
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.before_request
    def authenticate():
        header = request.headers.get("Authorization", "")
        scheme, _, candidate = header.partition(" ")
        if scheme != "Bearer" or not token_matches(candidate.strip()):
            return reply({"ok": False, "error": "unauthorized"}, 401)
        return None

    @app.errorhandler(413)
    def too_large(_error):
        return reply({"ok": False, "error": "too_large"}, 413)

    @app.errorhandler(404)
    def not_found(_error):
        return reply({"ok": False, "error": "not_found"}, 404)

    @app.errorhandler(405)
    def wrong_method(_error):
        return reply({"ok": False, "error": "method_not_allowed"}, 405)

    @app.get(lp.API_PREFIX + "/ping")
    def ping():
        return reply({"ok": True})

    @app.get(lp.API_PREFIX + "/faces")
    def faces():
        payload = lp.faces_payload(db.list_link_faces())
        if request.args.get("have") == payload["version"]:
            return reply({"ok": True, "unchanged": True, "version": payload["version"]})
        return reply({"ok": True, "unchanged": False, **payload})

    @app.get(lp.API_PREFIX + "/morning")
    def morning():
        brief = digest_store.load_brief()
        if not brief:
            return reply({"ok": True, "brief": None})
        keep = ("date", "generated_at", "news", "news_problem")
        return reply({"ok": True, "brief": {k: brief.get(k) for k in keep}})

    @app.post(lp.API_PREFIX + "/events")
    def events():
        data = request.get_json(silent=True)
        try:
            event = lp.validate_event(data, now=now())
        except lp.ProtocolError as e:
            return reply({"ok": False, "error": "invalid", "message": str(e)}, 400)
        user = None
        if event["kind"] == "punch":
            user = db.get_active_user(event["user_id"])
            if user is None:
                return reply({"ok": False, "error": "unknown_user"}, 422)
        if not db.claim_link_event(event["event_id"]):
            return reply({"ok": True, "status": "duplicate"})
        try:
            message = _apply(event, user)
        except Exception as e:  # noqa: BLE001  任何寫入失敗都取消登記，讓前台可以重送
            db.release_link_event(event["event_id"])
            print(f"[link_server] 事件處理失敗（{event['kind']}）：{type(e).__name__}")
            return reply({"ok": False, "error": "server_error"}, 500)
        return reply({"ok": True, "status": "applied", "message": message})

    return app


def _apply(event, user):
    """把一筆驗證過的事件寫進資料庫，回傳給前台看的說明文字。"""
    if event["kind"] == "punch":
        name, _employee_id = user
        return db.log_attendance(name, event["user_id"], now=event["ts"])

    def save_evidence():
        if event["evidence"] is None:
            return None
        stamp = event["ts"].strftime("%Y%m%d_%H%M%S")
        path = os.path.join(paths.ANOMALY_DIR, f"{_EVIDENCE_PREFIX[event['kind']]}_{stamp}.jpg")
        os.makedirs(paths.ANOMALY_DIR, exist_ok=True)
        try:
            with open(path, "xb") as f:       # 同一秒同類型已存在就不覆蓋
                f.write(event["evidence"])
        except FileExistsError:
            pass
        return path

    _event_id, merged = db.record_anomaly_event(event["kind"], event["detail"], make_evidence=save_evidence,
                                                now=event["ts"])
    return "merged" if merged else "recorded"


def make_server(settings=None):
    """依角色設定建立 werkzeug 伺服器（只綁設定的內網位址）；不是後台角色或沒有通行碼就丟 ConfigError。"""
    from werkzeug.serving import make_server as werkzeug_make_server
    settings = settings or role_config.load()
    if settings.role != role_config.BACK:
        raise role_config.ConfigError("目前的角色不是後台，不啟動連線服務"
                                      "（python -m shared.role_config set --role back ...）")
    role_config.require_token()
    return werkzeug_make_server(settings.bind_host, settings.back_port, create_app(), threaded=True)


def main():
    db.init_db()
    try:
        server = make_server()
    except role_config.ConfigError as e:
        print(f"[link_server] 無法啟動：{e}")
        return 2
    print(f"[link_server] 後台連線服務啟動：http://{server.host}:{server.port}（只給前台使用）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
