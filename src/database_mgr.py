import sqlite3
import io
import os
import numpy as np
import datetime

# 將 Numpy 陣列轉成二進制格式，以便存入資料庫
def adapt_array(arr):
    out = io.BytesIO()
    np.save(out, arr)
    out.seek(0)
    
    return sqlite3.Binary(out.read())

# 從資料庫讀取二進制資料，轉回 Numpy 陣列
def convert_array(text):
    out = io.BytesIO(text)
    out.seek(0)

    return np.load(out)

sqlite3.register_adapter(np.ndarray, adapt_array)
sqlite3.register_converter("ARRAY", convert_array)

# 資料庫檔案路徑
DB_PATH = '../database/database.db' 

def init_db():
    # 確保 database 資料夾存在，沒有的話就自動建一個！
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn_db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn_db.execute("""CREATE TABLE IF NOT EXISTS Users 
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                    name TEXT NOT NULL, 
                    embedding ARRAY NOT NULL,
                    work_start TEXT DEFAULT '09:00',  
                    work_end TEXT DEFAULT '18:00')""")
    conn_db.execute("""CREATE TABLE IF NOT EXISTS Attendance 
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                    name TEXT NOT NULL, 
                    timestamp DATETIME NOT NULL,
                    status TEXT NOT NULL)""")
    conn_db.commit()
    conn_db.close()
    print("資料庫初始化完成!")

def register_user(name, face_embedding, work_start="09:00", work_end="18:00"):
    conn_db = sqlite3.connect(DB_PATH)
    conn_db.execute("INSERT INTO Users (name, embedding, work_start, work_end) VALUES (?, ?, ?, ?)", 
                    (name, face_embedding, work_start, work_end))
    conn_db.commit()
    conn_db.close()

    print(f"註冊成功: {name} (班表: {work_start} - {work_end})")

def log_attendance(name):
    conn_db = sqlite3.connect(DB_PATH)
    cursor = conn_db.cursor()
    
    # 取得現在的時間與日期
    now = datetime.datetime.now()
    today_date = now.strftime("%Y-%m-%d") 
    current_time = now.strftime("%H:%M:%S") 
    full_timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("SELECT id, status FROM Attendance WHERE name=? AND timestamp LIKE ? ORDER BY timestamp ASC", (name, f"{today_date}%"))
    records = cursor.fetchall()

    # 狀態 0：今天第一次刷臉 (處理上班、遲到)
    if len(records) == 0: 
        if current_time >= "07:40:00" and current_time <= "08:00:00":
            status = "上班"
            conn_db.execute("INSERT INTO Attendance (name, timestamp, status) VALUES (?, ?, ?)", (name, full_timestamp, status))
            conn_db.commit()
            msg = f"In: {name} (Morning)" 
        
        elif current_time > "08:00:00" and current_time < "12:00:00":
            # 延後遲到判斷的時間，中午前來都算遲到
            status = "遲到"
            conn_db.execute("INSERT INTO Attendance (name, timestamp, status) VALUES (?, ?, ?)", (name, full_timestamp, status))
            conn_db.commit()
            msg = f"In: {name} (Be late)"
        
        else:
            msg = "您的上班時間還未到或已過早上打卡時間..."

    # 狀態 1：今天第二次刷臉 (處理下班、早退，以及午休防誤觸)
    elif len(records) == 1: 
        # 防誤觸緩衝區：中午到下午四點半前經過鏡頭，不紀錄下班，只給提示
        if current_time >= "12:00:00" and current_time < "16:30:00":
            msg = f"Keep going: {name}!" 
            return msg # 直接結束，不寫入資料庫
            
        elif current_time >= "16:30:00" and current_time < "17:40:00":
            status = "早退"
            conn_db.execute("INSERT INTO Attendance (name, timestamp, status) VALUES (?, ?, ?)", (name, full_timestamp, status))
            conn_db.commit()
            msg = f"Out: {name} (Leave early)"
            
        elif current_time >= "17:40:00" and current_time <= "18:00:00":
            status = "下班"
            conn_db.execute("INSERT INTO Attendance (name, timestamp, status) VALUES (?, ?, ?)", (name, full_timestamp, status))
            conn_db.commit()
            msg = f"Out: {name} (Bye!)"
            
        elif current_time > "18:00:00":
            status = "下班 (加班)"
            conn_db.execute("INSERT INTO Attendance (name, timestamp, status) VALUES (?, ?, ?)", (name, full_timestamp, status))
            conn_db.commit()
            msg = f"Out: {name} (Overtime)"

        else:
            msg = f"Done: {name} (Morning)"

    # 狀態 2：今天第三次(或以上)刷臉 (啟動更新機制！)
    else:
        # 取得上一筆(也就是下班那筆)的專屬 ID
        last_record_id = records[-1][0] 
        
        if current_time >= "16:30:00" and current_time < "17:40:00":
            status = "早退"
        elif current_time >= "17:40:00" and current_time <= "18:00:00":
            status = "下班"
        elif current_time > "18:00:00":
            status = "下班 (加班)"
        else:
            return f"Done: {name} (All Day)" # 如果不是上述時間，就維持完成狀態

        conn_db.execute("UPDATE Attendance SET timestamp=?, status=? WHERE id=?", (full_timestamp, status, last_record_id))
        conn_db.commit()
        msg = f"Updated: {name} ({status})"

    conn_db.close()
    print(f"系統日誌: {msg} - 時間: {full_timestamp}")

    return msg

def recognize_face(target_embedding, threshold=1.0):
    conn_db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    cursor = conn_db.cursor()

    cursor.execute("SELECT name, embedding FROM Users")
    db_data = cursor.fetchall()
    conn_db.close()

    # 如果資料庫是空的，直接結束
    if len(db_data) == 0:
        return("Empty DB", 99.9)

    total_distances = []
    total_names = []
    for data in db_data:
        total_names.append(data[0])
        db_embedding = data[1]

        # 計算距離
        distance = round(np.linalg.norm(db_embedding - target_embedding),2)
        total_distances.append(distance)
    
    # 找出距離最小的那個人
    idx_min = np.argmin(total_distances)
    name, distance = total_names[idx_min], total_distances[idx_min]

    # 判斷是否低於門檻值
    if distance < threshold:
        return (name, distance)
    else:
        return ("Unknown", distance)
        
if __name__ == "__main__":
    print("啟動資料庫測試...")
    init_db()

    # 捏造一組假的 DNA
    fake_dna = np.random.rand(512)

    # 測試註冊
    register_user("Simon", fake_dna)

    # 測試辨識 (拿一模一樣的 DNA 去比，距離應該要是 0.0)
    result = recognize_face(fake_dna)
    print("辨識結果", result)

    # 測試未知人物 (捏造另一組完全不同的 DNA)
    unknown_dna = np.random.rand(512).astype(np.float32)
    result2 = recognize_face(unknown_dna)
    print("辨識未知人物結果:", result2)