"""
account_admin.py：員工帳號管理的資料庫操作。

主要功能：
  - 列出所有員工（含角色、啟用狀態、最新打卡紀錄）
  - 停用 / 啟用帳號：停用後 recognize_face 不再比對該員工，刷臉會被判定為查無資料
  - 刪除帳號：只刪 Users 資料，Attendance 打卡歷史保留
  - 設定或取消管理者：管理者才能登入後台；最後一位管理者不能被取消、停用或刪除（否則沒有人能登入）
  只處理資料庫，不碰任何畫面元件。

串接模組：
  - database_mgr：取得資料庫路徑
  - backend_main：「員工帳號管理」視窗呼叫這裡的函式
"""
import sqlite3

from shared import database_mgr


def list_users():
    """回傳所有員工的 dict 清單（依 id 排序）；查詢失敗回傳空清單。"""
    try:
        conn = sqlite3.connect(database_mgr.DB_PATH)
        c = conn.cursor()
        c.execute("""
            SELECT u.id, u.employee_id, u.name, u.gender, u.role, u.is_active,
                   COALESCE((SELECT status FROM Attendance a WHERE a.name = u.name
                             ORDER BY timestamp DESC LIMIT 1), '待機'),
                   COALESCE((SELECT timestamp FROM Attendance a WHERE a.name = u.name
                             ORDER BY timestamp DESC LIMIT 1), '-')
            FROM Users u
            ORDER BY u.id
        """)
        rows = c.fetchall()
        conn.close()
        return [
            {
                "id": r[0],
                "employee_id": r[1],
                "name": r[2],
                "gender": r[3],
                "role": r[4] or "employee",
                # 只有明確為 0 才算停用，None 視為啟用
                "is_active": r[5] != 0,
                "last_status": r[6],
                "last_time": r[7],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[Account Admin][WARN] list_users 查詢失敗: {e}")
        return []


def _admin_ids():
    try:
        return {a["id"] for a in database_mgr.list_admins()}
    except sqlite3.Error:      # 資料庫讀不到時交給後面的更新動作回報失敗
        return set()


def _would_remove_last_admin(user_ids):
    """這批帳號若全部失去管理者資格，會不會讓啟用中的管理者變成零個。"""
    admins = _admin_ids()
    return bool(admins) and admins <= set(user_ids)


LAST_ADMIN_MESSAGE = "這是最後一位管理者，不能取消、停用或刪除，否則之後沒有人能登入後台。請先指定另一位管理者。"


def set_role(user_id, role, operator=None):
    """設定管理者或一般員工，回傳 (成功與否, 訊息)；停用中的帳號不能設為管理者。"""
    if role not in (database_mgr.ROLE_ADMIN, database_mgr.ROLE_EMPLOYEE):
        return False, "角色只能是 admin 或 employee"
    user = next((u for u in list_users() if u["id"] == user_id), None)
    if user is None:
        return False, "找不到這位員工"
    if role == database_mgr.ROLE_ADMIN and not user["is_active"]:
        return False, "這個帳號已停用，請先啟用再設為管理者"
    if user["role"] == role:
        return True, "角色沒有變動"
    if role == database_mgr.ROLE_EMPLOYEE and _would_remove_last_admin([user_id]):
        return False, LAST_ADMIN_MESSAGE
    try:
        conn = sqlite3.connect(database_mgr.DB_PATH, timeout=5)
        conn.execute("UPDATE Users SET role=? WHERE id=?", (role, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        return False, f"更新失敗：{e}"
    who = f"{operator} 將 " if operator else ""
    action = "設為管理者" if role == database_mgr.ROLE_ADMIN else "取消管理者"
    database_mgr.add_anomaly_event("admin_changed", None, f"{who}{user['name']} {action}")
    return True, "已設為管理者" if role == database_mgr.ROLE_ADMIN else "已取消管理者"


def set_active(user_id, active):
    """停用或啟用一個員工帳號，回傳 (成功與否, 訊息)。"""
    if not active and _would_remove_last_admin([user_id]):
        return False, LAST_ADMIN_MESSAGE
    try:
        conn = sqlite3.connect(database_mgr.DB_PATH, timeout=5)
        conn.execute("UPDATE Users SET is_active=? WHERE id=?", (1 if active else 0, user_id))
        conn.commit()
        conn.close()
        return True, ("已啟用" if active else "已停用")
    except Exception as e:
        return False, f"更新失敗：{e}"


def delete_user(user_id):
    """刪除一個員工帳號與人臉特徵（打卡紀錄以姓名記錄，不受影響），回傳 (成功與否, 訊息)。"""
    if _would_remove_last_admin([user_id]):
        return False, LAST_ADMIN_MESSAGE
    try:
        conn = sqlite3.connect(database_mgr.DB_PATH, timeout=5)
        conn.execute("DELETE FROM Users WHERE id=?", (user_id,))
        conn.commit()
        conn.close()
        return True, "已刪除"
    except Exception as e:
        return False, f"刪除失敗：{e}"
