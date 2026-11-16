"""
link_protocol.py：後台（admin.link_server）與前台（access.front_agent）之間的通訊協定——欄位驗證與編碼（純邏輯）。

主要功能：
  - 路徑與標頭：全部在 /v1 底下，通行碼放 Authorization: Bearer；回應都是 JSON
  - 人臉快照：特徵用 float32 小端位元組再 base64；版本是整份資料（編號、姓名、員工編號、角色、特徵）的 SHA-256，
    前台帶著版本問，沒變就不重送
  - 事件（前台→後台）：kind 只有 punch（打卡）、tailgate（尾隨）、no_scan_walkby（經過未打卡）；
    event_id 是 32 位小寫十六進位（前台產生，補送靠它去重）；ts 是事件發生時間（前台用校正後的時鐘）；
    punch 只收員工編號（user_id，後台用它查姓名，不信任前台送來的姓名）；證據照片是 JPEG（base64），有大小上限
  - 驗證不合格丟 ProtocolError，訊息是給日誌看的白話；後台據此回 400（前台看到 400 就知道這筆永遠不會成功，不再重送）
  - 時間檢查：事件時間不能比後台現在晚超過 MAX_FUTURE_SECONDS，也不能比 MAX_AGE_DAYS 天前更早

串接模組：
  - admin.link_server：收到請求先經這裡驗證；access.front_agent：送出前用同一套驗證自己的事件
"""
import base64
import datetime
import hashlib
import re
import unicodedata
import uuid

import numpy as np

API_PREFIX = "/v1"
MAX_BODY_BYTES = 3 * 1024 * 1024          # 一次請求的上限（含 base64 的證據照片）
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024      # 證據照片（解碼後）的上限
MAX_FUTURE_SECONDS = 300
MAX_AGE_DAYS = 30
KINDS = ("punch", "tailgate", "no_scan_walkby")
EMBEDDING_DIM = 512                       # ArcFace 特徵維度
_EVENT_ID = re.compile(r"^[0-9a-f]{32}$")
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_DETAIL = 200


class ProtocolError(ValueError):
    """欄位不合格（永久錯誤，重送也不會成功）。"""


def new_event_id():
    return uuid.uuid4().hex


def format_ts(moment):
    return moment.strftime(_TS_FORMAT)


def parse_ts(text):
    try:
        return datetime.datetime.strptime(str(text), _TS_FORMAT)
    except ValueError:
        raise ProtocolError(f"時間格式要是 YYYY-MM-DD HH:MM:SS：{str(text)[:30]!r}") from None


# --- 人臉特徵 ---

def encode_embedding(vector):
    """numpy 特徵 → base64 字串（float32 小端）。"""
    return base64.b64encode(np.asarray(vector, dtype="<f4").tobytes()).decode("ascii")


def decode_embedding(text, dim=EMBEDDING_DIM):
    """base64 字串 → float32 向量；長度不對、含非數值（NaN、無限大）丟 ProtocolError。"""
    try:
        raw = base64.b64decode(str(text), validate=True)
    except (ValueError, TypeError):
        raise ProtocolError("特徵不是合法的 base64") from None
    if len(raw) != dim * 4:
        raise ProtocolError(f"特徵長度不對：{len(raw)} 位元組（要 {dim * 4}）")
    vector = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    if not np.all(np.isfinite(vector)):
        raise ProtocolError("特徵含有非數值")
    return vector


def faces_payload(faces):
    """後台：把資料層的人臉清單轉成傳輸格式並算版本。回傳 {version, faces}。"""
    items = [{"id": int(f["id"]), "name": str(f["name"]), "employee_id": str(f["employee_id"] or ""),
              "role": str(f["role"]), "embedding": encode_embedding(f["embedding"])} for f in faces]
    digest = hashlib.sha256()
    for item in items:
        digest.update(repr((item["id"], item["name"], item["employee_id"], item["role"], item["embedding"])).encode())
    return {"version": digest.hexdigest(), "faces": items}


def parse_faces(payload):
    """前台：驗證並解出後台送來的人臉快照；回傳 (版本, [{id, name, employee_id, role, embedding(numpy)}])。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("faces"), list):
        raise ProtocolError("人臉快照格式不對")
    version = payload.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
        raise ProtocolError("人臉快照的版本不對")
    faces = []
    for item in payload["faces"]:
        if not isinstance(item, dict):
            raise ProtocolError("人臉資料格式不對")
        user_id, name = item.get("id"), item.get("name")
        if not isinstance(user_id, int) or isinstance(user_id, bool) or not isinstance(name, str) or not name:
            raise ProtocolError("人臉資料缺編號或姓名")
        faces.append({"id": user_id, "name": name[:50], "employee_id": str(item.get("employee_id") or "")[:50],
                      "role": str(item.get("role") or "employee")[:20],
                      "embedding": decode_embedding(item.get("embedding"))})
    return version, faces


# --- 事件 ---

def _jpeg_bytes(text):
    try:
        raw = base64.b64decode(str(text), validate=True)
    except (ValueError, TypeError):
        raise ProtocolError("證據照片不是合法的 base64") from None
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ProtocolError(f"證據照片太大：{len(raw)} 位元組（上限 {MAX_EVIDENCE_BYTES}）")
    if not raw.startswith(b"\xff\xd8\xff"):
        raise ProtocolError("證據照片不是 JPEG")
    return raw


def validate_event(data, now=None):
    """後台：驗證前台送來的事件；回傳整理過的 dict（evidence 已解成位元組），不合格丟 ProtocolError。"""
    if not isinstance(data, dict):
        raise ProtocolError("事件要是 JSON 物件")
    now = now or datetime.datetime.now()
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):
        raise ProtocolError("event_id 要是 32 位小寫十六進位")
    kind = data.get("kind")
    if kind not in KINDS:
        raise ProtocolError(f"kind 只能是 {'、'.join(KINDS)}")
    when = parse_ts(data.get("ts"))
    if when > now + datetime.timedelta(seconds=MAX_FUTURE_SECONDS):
        raise ProtocolError("事件時間比後台現在還晚太多（兩台電腦的時鐘差太多？）")
    if when < now - datetime.timedelta(days=MAX_AGE_DAYS):
        raise ProtocolError(f"事件時間超過 {MAX_AGE_DAYS} 天前")
    detail = data.get("detail", "")
    if not isinstance(detail, str) or len(detail) > _MAX_DETAIL:
        raise ProtocolError(f"detail 要是 {_MAX_DETAIL} 字以內的文字")
    event = {"event_id": event_id, "kind": kind, "ts": when, "detail": detail, "user_id": None, "evidence": None}
    if kind == "punch":
        user_id = data.get("user_id")
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise ProtocolError("打卡事件要有員工編號 user_id（正整數）")
        event["user_id"] = user_id
        if data.get("evidence") is not None:
            raise ProtocolError("打卡事件不收證據照片")
    elif data.get("evidence") is not None:
        event["evidence"] = _jpeg_bytes(data["evidence"])
    return event


def build_event(kind, when, user_id=None, detail="", evidence=None, event_id=None):
    """前台：組出要送的事件（evidence 是 JPEG 位元組或 None）；用同一套驗證確認自己送的格式沒問題。"""
    data = {"event_id": event_id or new_event_id(), "kind": kind, "ts": format_ts(when), "detail": detail}
    if user_id is not None:
        data["user_id"] = user_id
    if evidence is not None:
        data["evidence"] = base64.b64encode(evidence).decode("ascii")
    validate_event(data, now=when + datetime.timedelta(seconds=1))
    return data


# --- 前台心跳（前台→後台；後台只顯示，不拿來做任何安全或打卡判斷）---

_HEARTBEAT_COUNTS = ("pending_events", "rejected_events", "dropped_events", "faces")
_HEARTBEAT_FLAGS = ("clock_warning", "recognition_running", "camera_ok")
_MAX_COUNT = 10_000_000
_MAX_CLOCK_OFFSET = 10 * 365 * 24 * 3600.0
_HEX_VERSION = re.compile(r"^[0-9a-f]{0,64}$")


def validate_heartbeat(data):
    """後台：驗證前台回報的狀態，只留白名單欄位並限制範圍；不合格丟 ProtocolError（其他欄位一律丟棄）。"""
    if not isinstance(data, dict):
        raise ProtocolError("心跳要是 JSON 物件")
    status = {}
    for key in _HEARTBEAT_COUNTS:
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _MAX_COUNT:
            raise ProtocolError(f"{key} 要是 0 到 {_MAX_COUNT} 的整數")
        status[key] = value
    for key in _HEARTBEAT_FLAGS:
        if not isinstance(data.get(key), bool):
            raise ProtocolError(f"{key} 要是 true 或 false")
        status[key] = data[key]
    offset = data.get("clock_offset_seconds")
    if not isinstance(offset, (int, float)) or isinstance(offset, bool) or abs(offset) > _MAX_CLOCK_OFFSET:
        raise ProtocolError("clock_offset_seconds 要是合理範圍內的數字")
    status["clock_offset_seconds"] = round(float(offset), 1)
    version = data.get("faces_version", "")
    if not isinstance(version, str) or not _HEX_VERSION.match(version):
        raise ProtocolError("faces_version 要是十六進位字串")
    status["faces_version"] = version
    return status


# --- 前台知識庫問答（前台→後台送問題、之後取結果；見 docs/adr/0001-front-knowledge-chat.md）---

MAX_CHAT_QUESTION = 300                    # 前台問題的字數上限（只收純文字）
MAX_CHAT_ANSWER = 4000                     # 回答的字數上限
CHAT_PATH = "/chat"
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


def validate_chat_question(data):
    """後台：驗證前台送來的問題，回傳整理過的問題文字；欄位不合格丟 ProtocolError。
    只收一個字串欄位 question（其他欄位一律丟棄）；去掉控制字元與頭尾空白，不能是空的，長度有上限。"""
    if not isinstance(data, dict):
        raise ProtocolError("問題要是 JSON 物件")
    question = data.get("question")
    if not isinstance(question, str):
        raise ProtocolError("question 要是文字")
    question = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") and c not in "\t"
                       else c for c in question).strip()
    if not question:
        raise ProtocolError("問題不能是空的")
    if len(question) > MAX_CHAT_QUESTION:
        raise ProtocolError(f"問題最多 {MAX_CHAT_QUESTION} 字")
    return question


def valid_job_id(job_id):
    """工作編號是 32 位小寫十六進位（secrets.token_hex(16)）。"""
    return isinstance(job_id, str) and bool(_JOB_ID.match(job_id))
