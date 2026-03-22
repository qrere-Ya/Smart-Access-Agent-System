import os
import sqlite3
import datetime
import glob
from PIL import Image, ImageTk
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from tkinter import messagebox

# 資路庫與照片路徑設定
DB_PATH = '../database/database.db'
ANOMALY_PATH = '../anomaly_logs'

def get_today_str():
    return datetime.datetime.now().strftime("%Y-%m-%d")

def fetch_dashboard_metrics():
    # 計算今日數據
    today = get_today_str()
    total_in = 0
    late_count = 0
    anomaly_count = 0

    # 撈取資料庫數據
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # 算今天總打卡人數與遲到人數
        cursor.execute("SELECT status FROM Attendance WHERE timestamp LIKE ?", (f"{today}%",))
        records = cursor.fetchall()

        total_in = len(records)
        late_count = sum(1 for r in records if "遲到" in r[0])
        conn.close()
    except:
        pass # 如果資料庫沒建忽略

    # 算今天異常截圖
    if os.path.exists(ANOMALY_PATH):
        today_images = glob.glob(os.path.join(ANOMALY_PATH, f"tailgate_{today.replace('-', '')}*.jpg"))
        anomaly_count = len(today_images)

    return total_in, late_count, anomaly_count

def fetch_employee_names():
    # 從資料庫撈取所有註冊過的員工名單
    names = []
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM Users")
        names = [row[0] for row in cursor.fetchall()]
        conn.close()
    except:
        pass
    return names

def search_attendance():
    # 查詢選定員工的打卡紀錄
    name = combo_name.get().strop()
    if not name:
        messagebox.showwarning("請選擇或輸入員工姓名！")
        return
    
    # 清空表格
    for row in tree.get_children():
        tree.delete(row)

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp, status FROM Attendance WHERE name=? ORDER BY timestamp DESC", (name,))
        records = cursor.fetchall()
        conn.close()

        if not records:
            messagebox.showinfo(f"找不到 [{name}] 的紀錄")
            return
        
        for rec in records:
            # 根據狀態給予不同顏色Tag
            status = rec[1]
            tag = "normal"
            if "遲到" in status or "早退" in status:
                tag = "warning"
            
            tree.insert("", ttk.END, value=(rec[0], status), tags=(tag,))
    
    except Exception as e:
        messagebox.showerror(f"讀取失敗: {e}")

def show_latest_anomaly():
    # 在介面右側顯示最新的異常圖
    if not os.path.exists(ANOMALY_PATH):
        messagebox.showinfo("目前沒有異常紀錄")
        return
    # 抓取資料夾內所有 jpg 並按時間排序找最新的一張
    list_of_files = glob.glob(os.path.join(ANOMALY_PATH, '*.jpg'))
    if not list_of_files:
        messagebox.showinfo("目前沒有任何尾隨異常紀錄")
        return
    
    latest_file = max(list_of_files, key=os.path.getctime)
    
    try:
        # 用 Pillow 讀取讀片並縮放以符合 UI 大小
        img = Image.open(latest_file)
        img = img.resize((450,400), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(img)

        # 更新圖片標籤
        lbl_image.config(image=photo)
        lbl_image.image = photo # 必須保留參照，否則會被垃圾回收機制清除
        lbl_img_path.config(text=f"最新證據: {os.path.basename(latest_file)}")
    except Exception as e:
        messagebox.showerror(f"無法載入圖片: {e}")

# ==========================================
# UI 介面設計 
# ==========================================

# 主視窗
app = ttk.Window(themename="superhero")
app.title("智能儀錶板")
app.geometry("1000x650")

# 頂部: 今日數據
frame_metrics = ttk.Frame(app, padding=10)
frame_metrics.pack(fill=X)

total_in, late_count, anomaly_count = fetch_dashboard_metrics()

# 製作三張數據片
meter1 = ttk.Meter(frame_metrics, metersize=120, padding=5, amounttotal=50, amountused=total_in,
                   metertype="semi", subtext="今日總打卡", textright="人", bootstyle=SUCCESS)
meter1.pack(side=LEFT, padx=20)

meter2 = ttk.Meter(frame_metrics, metersize=120, padding=5, amounttotal=20, amountused=late_count, 
                   metertype="semi", subtext="今日遲到", textright="人", bootstyle=WARNING)
meter2.pack(side=LEFT, padx=20)

meter3 = ttk.Meter(frame_metrics, metersize=120, padding=5, amounttotal=10, amountused=anomaly_count, 
                   metertype="semi", subtext="尾隨異常", textright="次", bootstyle=DANGER)
meter3.pack(side=LEFT, padx=20)

# 中間: 左右雙欄排版
frame_main = ttk.Frame(app, padding=10)
frame_main.pack(fill=BOTH, expand=True)

# 左半邊: 搜尋與表格區
frame_left = ttk.Labelframe(frame_main, text="員工出缺勤查詢", padding=15)
frame_left.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 10))

#搜尋列
frame_search = ttk.Frame(frame_left)
frame_search.pack(fill=X, pady=(0, 10))

ttk.Label(frame_search, text="選擇員工").pack(side=LEFT, padx=(0, 5))
# 自動載入資料庫名單
combo_name = ttk.Combobox(frame_search, values=fetch_employee_names(), state="readonly", width=15)
combo_name.pack(side=LEFT, padx=5)
ttk.Button(frame_search, text="查詢", bootstyle=(PRIMARY, OUTLINE), command=search_attendance).pack(side=LEFT, padx=5)

#紀錄表格
columns = ("時間", "狀態")
tree = ttk.Treeview(frame_left, columns=columns, show="headings", height=12)
tree.heading("時間", text="打卡時間(Time)")
tree.column("時間", width=200, anchor=CENTER)
tree.heading("狀態", text="狀態(Status)")
tree.column("狀態", width=100, anchor=CENTER)
# 設定警告顏色 Tag
tree.tag_configure("warning", foreground="#ffc107") # 黃色字體提示遲到
tree.pack(fill=BOTH, expand=True)

# 右半邊: 異常突變預覽區
frame_right = ttk.Labelframe(frame_main, text="監控證據庫", padding=15)
frame_right.pack(side=RIGHT, fill=BOTH, expand=True)

btn_load_img = ttk.Button(frame_right, text="載入最新異常截圖", bootstyle=DANGER, command=show_latest_anomaly)
btn_load_img.pack(fill=X, padx=(0, 10))

lbl_img_path = ttk.Button(frame_right, text="尚未載入圖片", bootstyle=SECONDARY)
lbl_img_path.pack(pady=5)

# 放圖片的容器
lbl_image = ttk.Label(frame_right, text="[ 無影像資料 ]", anchor=CENTER, background="#222222")
lbl_image.pack(fill=BOTH, expand=True)

# 啟動應用程式
if __name__ == "__main__":
    app.mainloop()
