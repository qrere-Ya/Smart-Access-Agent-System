"""
帳號管理：列出所有員工、停用/啟用、刪除。

【2026-09-12，P2 新增】對應 Syscom Cubi 截圖裡「帳號管理」表格的概念。這裡只負責
資料庫操作，回傳查詢結果或 (ok, message)，不碰任何畫面元件——跟 service_manager.py /
dashboard_stats.py 同樣的分工方式：backend_main.py 呼叫這裡的函式，再把結果轉成
畫面上的文字/表格列。

【重要】"停用" 不是只在畫面上打個勾而已：database_mgr.recognize_face() 已經改成只
會拿 is_active=1 的員工特徵去做 1:N 比對（見 database_mgr.py 的修改），所以停用一個
員工之後，他在門口刷臉會被系統判定為查無資料，這是刻意的行為，停用要真的有效果，
不能只是好看的按鈕。
"""
import sqlite3

import database_mgr


def list_users():
    """
    回傳所有員工，依 id 排序，每一筆是一個 dict：
    {id, employee_id, name, gender, role, is_active, last_status, last_time}
    （last_status/last_time 是這個人最近一筆打卡紀錄，查無紀錄則是 '待機' / '-'，
    跟原本 backend_main.py 的員工資料庫視窗查詢邏輯一致）。查詢失敗回傳空清單，
    不拋例外——呼叫端看到空清單，畫面上就是「目前沒有資料可顯示」，不會讓程式當掉。
    """
    try:
        conn = sqlite3.connect(database_mgr.DB_PATH)
        c = conn.cursor()
        c.execute("""
            SELECT u.id, u.employee_id, u.name, u.gender, u.role, u.is_active,
                   COALESCE((SELECT status FROM Attendance a WHERE a.name = u.name ORDER BY timestamp DESC LIMIT 1), '待機'),
                   COALESCE((SELECT timestamp FROM Attendance a WHERE a.name = u.name ORDER BY timestamp DESC LIMIT 1), '-')
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
                # 舊資料庫升級後、還沒被明確設成 0 的一律視為啟用（None 或 1 都算啟用），
                # 只有明確是 0 才算停用，避免舊資料被誤判成停用。
                "is_active": r[5] != 0,
                "last_status": r[6],
                "last_time": r[7],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[Account Admin][WARN] list_users 查詢失敗: {e}")
        return []


def set_active(user_id, active):
    """停用/啟用一個員工帳號。回傳 (ok: bool, message: str)。"""
    try:
        conn = sqlite3.connect(database_mgr.DB_PATH, timeout=5)
        conn.execute("UPDATE Users SET is_active=? WHERE id=?", (1 if active else 0, user_id))
        conn.commit()
        conn.close()
        return True, ("已啟用" if active else "已停用")
    except Exception as e:
        return False, f"更新失敗：{e}"


def delete_user(user_id):
    """
    刪除一個員工帳號（連同人臉特徵一起刪除）。回傳 (ok: bool, message: str)。

    【刻意保留】只刪除 Users 表的這一筆，不動 Attendance 打卡紀錄——打卡紀錄是用姓名
    字串記錄，不是用 Users.id 關聯，就算把這個人的帳號刪掉，他過去的打卡歷史還是完整
    保留在 Attendance 表裡，之後要查歷史紀錄還是查得到，只是這個人之後沒辦法再刷臉
    打卡而已（1:N 比對的來源就是 Users 表，這筆刪掉了自然就比對不到）。
    """
    try:
        conn = sqlite3.connect(database_mgr.DB_PATH, timeout=5)
        conn.execute("DELETE FROM Users WHERE id=?", (user_id,))
        conn.commit()
        conn.close()
        return True, "已刪除"
    except Exception as e:
        return False, f"刪除失敗：{e}"
