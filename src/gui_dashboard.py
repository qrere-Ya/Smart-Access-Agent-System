import tkinter as tk
from tkinter import ttk, messagebox
import sqlite3
import os

# 資路庫與照片路徑設定
DB_PATH = '../database/database.db'
ANOMALY_PATH = '../anomaly_logs'

def search_attendance():
    # 查詢資料庫並將結果顯示
    name = entry_name.get().strip()

    if not name:
        messagebox.showwarning("請輸入員工姓名!")
        return
    
    # 先清空目前表格內所有舊資料
    for row in tree.get_children():
        tree.delete(row)

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # 撈取該員工的所有打卡紀錄，並按時間由新到舊排序
        cursor.execute("SELECT timestamp, status FROM Attendance WHERE name=? ORDER BY timestamp DESC", (name,))
        records = cursor.fetchall()
        conn.close()

        if len(records) == 0:
            messagebox.showinfo(f"資料庫中找不到 [{name}] 的打卡紀錄")
            return
        
        # 將撈到的資料一筆一筆塞進表格中
        for rec in records:
            timestamp, status = rec[0], rec[1]
            tree.insert("", tk.END, values=(timestamp, status))

    except Exception as e:
        messagebox.showwarning(f"讀取資料庫失敗: {e}")

def open_anomaly_folder():
    folder_path = os.path.abspath(ANOMALY_PATH)

    if os.path.exists(folder_path):
        os.startfile(folder_path) # 彈出 Windows 檔案總管
    else:
        messagebox.showinfo("目前沒有異常截圖資料")

# ==========================================
# UI 介面設計 
# ==========================================

# 主視窗
root = tk.Tk()
root.title("後台管理儀表板")
root.geometry("550x450")
root.configure(bg="#f0f0f0") # 淡灰色背景

# 頂部搜尋塊
frame_top = tk.Frame(root, bg="#f0f0f0", pady=20)
frame_top.pack(fill=tk.X)

lbl_name = tk.Label(frame_top, text="員工姓名查詢:", font=("微軟正黑體", 14), bg="#f0f0f0")
lbl_name.pack(side=tk.LEFT, padx=(20, 10))

entry_name = tk.Entry(frame_top, font=("微軟正黑體",14), bg="#f0f0f0")
entry_name.pack(side=tk.LEFT, padx=10)

btn_search = tk.Button(frame_top, text="查詢紀錄", font=("微軟正黑體", 12), bg="#4CAF50", fg="white", command=search_attendance)
btn_search.pack(side=tk.LEFT, padx=10)

# 中間表格區塊
frame_middle = tk.Frame(root, padx=20)
frame_middle.pack(fill=tk.BOTH, expand=True)

# 定義表格的欄位
columns = ("時間(Time)", "打卡狀態(Status)")
tree = ttk.Treeview(frame_middle, columns=columns, show="headings", height=10)

# 設定欄位標題與寬度
tree.heading("時間(Time)", text="時間(Time)")
tree.column("時間(Time)", width=220, anchor=tk.CENTER)

tree.heading("打卡狀態(Status)", text="打卡狀態(Status)")
tree.column("打卡狀態(Status)", width=220, anchor=tk.CENTER)

tree.pack(fill=tk.BOTH, expand=True)

# 底部房按鈕區塊
frame_bottom = tk.Frame(root, bg="#f0f0f0", pady=20)
frame_bottom.pack(fill=tk.X)

btn_anomaly = tk.Button(frame_bottom, text="查看尾隨異常截圖", font=("微軟正黑體", 12), bg="#f44336", fg="white", command=open_anomaly_folder)
btn_anomaly.pack(side=tk.BOTTOM)

# 啟動應用程式
if __name__ == "__main__":
    root.mainloop()
