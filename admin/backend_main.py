"""
backend_main.py：門禁系統管理後台主程式（Tkinter / ttkbootstrap）。

主要功能：
  - 主選單：四張功能卡片、即時時鐘、底部 CPU/RAM 狀態列
  - 營運狀態與尾隨監控：今日打卡統計、異常件數、人臉辨識終端機開關與即時輸出
  - 員工帳號管理：列出、停用、啟用、刪除員工
  - 遠端 QR Code 註冊：註冊伺服器開關，每 5 秒刷新動態 QR Code
  - 管理者助理：另開聊天視窗（rag.chat_window），查公司文件與考勤、異常紀錄
  只負責畫面，實際工作轉發給其他模組。

串接模組：
  - service_manager：啟動、停止、監控背景服務與 IP 偵測
  - console_panel：內建終端機文字框
  - dashboard_stats：儀表板統計查詢
  - account_admin：帳號管理資料庫操作
  - database_mgr：初始化資料庫
"""
import ttkbootstrap as ttk
from ttkbootstrap.constants import (
    BOTH, DANGER, E, INFO, LEFT, LIGHT, LINK, OUTLINE, PRIMARY, RIGHT, SECONDARY, SUCCESS, W, WARNING, X, Y,
)
import tkinter as tk
from tkinter import messagebox
import os
import datetime
from shared import database_mgr as db
import pyqrcode
import io
from PIL import Image, ImageTk

from admin import service_manager
from admin import console_panel
from admin import dashboard_stats
from admin import account_admin
from shared import paths

ANOMALY_DIR = paths.ANOMALY_DIR


class BackendManagerApp:
    """管理後台主視窗：建立主選單並開啟各功能視窗。"""

    def __init__(self, root):
        self.root = root
        self.root.title("智慧門禁 - 戰情控制中心")
        self.root.geometry("640x640")
        self.root.minsize(580, 580)
        self.root.resizable(True, True)

        # 三個服務的狀態文字，視窗關閉後仍保留，重開視窗時再同步實際狀態
        self.face_status_var = ttk.StringVar(value="⚪ 人臉辨識未啟動")
        self.reg_status_var = ttk.StringVar(value="⚪ 註冊伺服器未啟動")

        db.init_db()

        self._build_main_menu()

        # 關閉視窗時詢問是否一併停止背景服務
        self.root.protocol("WM_DELETE_WINDOW", self.on_app_close)

    def _build_main_menu(self):
        """建立主選單：標題列、功能卡片牆、底部狀態列（顏色取自 ttkbootstrap 主題）"""
        self.style = ttk.Style()

        # 頂部標題列：左邊品牌與狀態標籤，右邊時鐘
        header = ttk.Frame(self.root, padding=(25, 20, 25, 10))
        header.pack(fill=X)

        brand_col = ttk.Frame(header)
        brand_col.pack(side=LEFT)
        ttk.Label(brand_col, text="🔐 Smart Access Hub", font=("Helvetica", 20, "bold"),
                  bootstyle=PRIMARY).pack(anchor=W)
        ttk.Label(brand_col, text="邊緣運算門禁後臺系統", font=("微軟正黑體", 10),
                  bootstyle=SECONDARY).pack(anchor=W, pady=(2, 0))
        self._build_status_pill(brand_col, "🟢 系統運作中", SUCCESS)

        clock_col = ttk.Frame(header)
        clock_col.pack(side=RIGHT)
        self.clock_var = ttk.StringVar(value="--:--:--")
        self.date_var = ttk.StringVar(value="----/--/--")
        ttk.Label(clock_col, textvariable=self.clock_var, font=("Consolas", 16, "bold"), bootstyle=LIGHT).pack(anchor=E)
        ttk.Label(clock_col, textvariable=self.date_var, font=("微軟正黑體", 9), bootstyle=SECONDARY).pack(anchor=E)
        self._tick_clock()

        ttk.Separator(self.root, bootstyle=SECONDARY).pack(fill=X, padx=25)

        ttk.Label(self.root, text="功能模組", font=("微軟正黑體", 10, "bold"),
                  bootstyle=SECONDARY, padding=(25, 15, 0, 5)).pack(fill=X)

        # 2x2 功能卡片牆
        card_wall = ttk.Frame(self.root, padding=(15, 5, 15, 5))
        card_wall.pack(fill=BOTH, expand=True)
        card_wall.columnconfigure(0, weight=1)
        card_wall.columnconfigure(1, weight=1)
        card_wall.rowconfigure(0, weight=1)
        card_wall.rowconfigure(1, weight=1)

        # (圖示, 標題, 說明, 色系, 點擊要呼叫的函式)
        modules = [
            ("📊", "營運狀態與尾隨監控", "今日打卡統計、安全警報、人臉辨識即時輸出", SUCCESS, self.open_dashboard),
            ("👤", "員工帳號管理", "列出、停用、啟用、刪除員工帳號與角色", INFO, self.open_account_admin),
            ("📱", "遠端 QR Code 註冊", "產生動態安全碼，讓新進員工手機掃碼註冊", WARNING, self.open_qr_registration),
            ("🤖", "管理者助理", "用聊天查公司文件與法規、考勤紀錄、異常事件", PRIMARY,
             self.open_assistant_window),
        ]
        for idx, (icon, title, desc, style, cmd) in enumerate(modules):
            row, col = divmod(idx, 2)
            self._create_module_card(card_wall, icon, title, desc, style, cmd, row, col)

        self._build_status_footer()

    def _create_module_card(self, master, icon, title, desc, style, cmd, row, col):
        """用 Canvas 手繪一張深色懸浮卡片（圓角、陰影、圖示色塊），整張可點擊，滑鼠移入時亮起。"""
        colors = self.style.colors
        accent = colors.get(style)
        page_bg = colors.bg
        fg = colors.fg

        # 由頁面底色算出卡片、陰影、邊框的相對明暗，換主題也不會失真
        panel_fill = self._blend_hex(page_bg, "#ffffff", 0.07)
        hover_fill = self._blend_hex(page_bg, "#ffffff", 0.13)
        shadow_fill = self._blend_hex(page_bg, "#000000", 0.35)
        border_idle = self._blend_hex(page_bg, "#ffffff", 0.16)
        desc_fg = self._blend_hex(fg, page_bg, 0.45)

        canvas = tk.Canvas(master, width=262, height=136, highlightthickness=0, bd=0, bg=page_bg, cursor="hand2")
        canvas.grid(row=row, column=col, padx=10, pady=10, sticky="nsew")

        def draw(fill, border):
            canvas.delete("all")
            # 先畫偏移的暗色圓角矩形當陰影，再畫卡片本體
            self._rounded_rect(canvas, 6, 8, 260, 134, radius=16, fill=shadow_fill, outline="")
            self._rounded_rect(canvas, 2, 2, 256, 128, radius=16, fill=fill, outline=border, width=1)
            # 圖示色塊、標題、說明
            self._rounded_rect(canvas, 16, 16, 54, 54, radius=10, fill=accent, outline="")
            canvas.create_text(35, 35, text=icon, font=("Segoe UI Emoji", 16))
            canvas.create_text(16, 72, text=title, anchor="w", font=("微軟正黑體", 12, "bold"), fill=fg)
            canvas.create_text(16, 96, text=desc, anchor="nw", width=224, font=("微軟正黑體", 8), fill=desc_fg)

        draw(panel_fill, border_idle)

        # 滑鼠移入時卡片變亮、邊框換成模組色
        canvas.bind("<Button-1>", lambda e: cmd())
        canvas.bind("<Enter>", lambda e: draw(hover_fill, accent))
        canvas.bind("<Leave>", lambda e: draw(panel_fill, border_idle))

    def _build_status_pill(self, master, text, style):
        """畫膠囊形狀的小狀態標籤"""
        colors = self.style.colors
        accent = colors.get(style)
        page_bg = colors.bg
        pill_fill = self._blend_hex(accent, page_bg, 0.78)

        pill = tk.Canvas(master, width=120, height=24, highlightthickness=0, bd=0, bg=page_bg)
        pill.pack(anchor=W, pady=(6, 0))
        self._rounded_rect(pill, 1, 1, 119, 23, radius=11, fill=pill_fill, outline="")
        pill.create_text(60, 12, text=text, font=("微軟正黑體", 8, "bold"), fill=accent)

    @staticmethod
    def _rounded_rect(canvas, x1, y1, x2, y2, radius=18, **kwargs):
        """在 Canvas 上用平滑多邊形畫圓角矩形（Tkinter 沒有原生圓角）"""
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, **kwargs)

    @staticmethod
    def _blend_hex(hex_color, target_hex, ratio):
        """把 hex_color 往 target_hex 混色（ratio 0~1，越大越接近 target_hex），回傳新的色碼"""
        hex_color = hex_color.lstrip("#")
        target_hex = target_hex.lstrip("#")
        r1, g1, b1 = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        r2, g2, b2 = int(target_hex[0:2], 16), int(target_hex[2:4], 16), int(target_hex[4:6], 16)
        r = round(r1 + (r2 - r1) * ratio)
        g = round(g1 + (g2 - g1) * ratio)
        b = round(b1 + (b2 - b1) * ratio)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _tick_clock(self):
        """每秒更新頂部時鐘；主視窗已銷毀就停止排程"""
        if not self.root.winfo_exists():
            return
        now = datetime.datetime.now()
        self.clock_var.set(now.strftime("%H:%M:%S"))
        self.date_var.set(now.strftime("%Y/%m/%d"))
        self.root.after(1000, self._tick_clock)

    def _build_status_footer(self):
        """建立底部 CPU/RAM 狀態列"""
        ttk.Separator(self.root, bootstyle=SECONDARY).pack(fill=X, padx=25)
        footer = ttk.Frame(self.root, padding=(25, 8, 25, 14))
        footer.pack(fill=X)

        self.sys_status_var = ttk.StringVar(value="系統資源監控準備中…")
        ttk.Label(footer, textvariable=self.sys_status_var, font=("Consolas", 9), bootstyle=SECONDARY).pack(side=LEFT)
        self._tick_system_status()

    def _tick_system_status(self):
        """每 3 秒讀取一次 CPU/RAM 顯示在狀態列；讀不到（沒裝 psutil）就顯示提示"""
        if not self.root.winfo_exists():
            return
        res = dashboard_stats.get_system_resources()
        if res is not None:
            self.sys_status_var.set(
                f"🖥️ CPU {res['cpu_percent']:.0f}%　💾 RAM {res['ram_used_gb']:.1f} / {res['ram_total_gb']:.1f} GB"
            )
        else:
            self.sys_status_var.set("系統資源監控未啟用（未偵測到 psutil）")
        self.root.after(3000, self._tick_system_status)

    def _msgbox_parent(self, win_attr):
        """回傳 messagebox 的 parent 參數，讓提示視窗浮在目前使用者正在看的子視窗上；子視窗已關閉則回傳空字典"""
        win = getattr(self, win_attr, None)
        try:
            if win is not None and win.winfo_exists():
                return {"parent": win}
        except Exception:  # noqa: S110
            pass
        return {}

    # --- 人臉辨識終端機開關 ---
    def toggle_face_service(self):
        """人臉辨識終端機開關：執行中就停止，否則啟動"""
        if service_manager.face_service_running():
            service_manager.stop_face_service()
            self._update_face_status()
            messagebox.showinfo("提示", "已停止人臉辨識終端機。", **self._msgbox_parent("dash_win"))
            return

        def on_output_line(line):
            self.root.after(0, lambda: console_panel.append_line(getattr(self, "face_console", None), line))

        ok, message = service_manager.start_face_service(on_output_line)
        self._update_face_status()
        if ok:
            messagebox.showinfo("提示", message, **self._msgbox_parent("dash_win"))
        else:
            messagebox.showerror("啟動失敗", message, **self._msgbox_parent("dash_win"))

    def _update_face_status(self):
        """依實際狀態更新人臉辨識狀態文字與按鈕文字"""
        if service_manager.face_service_running():
            self.face_status_var.set("🟢 人臉辨識執行中")
            btn_text = "🛑 停止人臉辨識終端機"
        else:
            self.face_status_var.set("⚪ 人臉辨識未啟動")
            btn_text = "📷 啟動人臉辨識終端機"
        if hasattr(self, "face_btn") and self.face_btn.winfo_exists():
            self.face_btn.config(text=btn_text)

    # --- QR 註冊伺服器開關 ---
    def toggle_registration_service(self):
        """QR 註冊伺服器開關：執行中就停止，否則啟動"""
        if service_manager.reg_server_running():
            service_manager.stop_reg_service()
            self._update_reg_status()
            messagebox.showinfo("提示", "已停止 QR 註冊伺服器。", **self._msgbox_parent("reg_win"))
            return

        def on_output_line(line):
            self.root.after(0, lambda: console_panel.append_line(getattr(self, "reg_console", None), line))

        ok, message = service_manager.start_reg_service(on_output_line)
        self._update_reg_status()
        if ok:
            messagebox.showinfo("提示", message, **self._msgbox_parent("reg_win"))
        else:
            messagebox.showerror("啟動失敗", message, **self._msgbox_parent("reg_win"))

    def _update_reg_status(self):
        """依實際狀態更新註冊伺服器狀態文字與按鈕文字"""
        running = service_manager.reg_server_running()
        if running:
            self.reg_status_var.set("🟢 註冊伺服器執行中")
            btn_text = "🛑 停止 QR 註冊伺服器"
        else:
            self.reg_status_var.set("⚪ 註冊伺服器未啟動")
            btn_text = "📱 啟動 QR 註冊伺服器"
        if hasattr(self, "reg_btn") and self.reg_btn.winfo_exists():
            self.reg_btn.config(text=btn_text)

    # --- 關閉主視窗 ---
    def on_app_close(self):
        """關閉主視窗時，詢問是否一併停止還在背景跑的服務"""
        if service_manager.any_service_running():
            if messagebox.askyesno("確認關閉",
                                   "偵測到有後端服務仍在執行中，要一併停止嗎？\n（選「否」會讓它們繼續在背景執行）"):
                service_manager.stop_all_services()
        self.root.destroy()

    # --- 視窗 1：營運狀態與尾隨監控 ---
    def open_dashboard(self):
        """開啟儀表板：左邊統計與打卡清單，右邊人臉辨識即時輸出"""
        self.dash_win = ttk.Toplevel(self.root)
        self.dash_win.title("實時營運狀態")
        self.dash_win.geometry("1260x680")

        main_split = ttk.Frame(self.dash_win)
        main_split.pack(fill=BOTH, expand=True)

        left_col = ttk.Frame(main_split)
        left_col.pack(side=LEFT, fill=BOTH, expand=True)

        right_col = ttk.Frame(main_split, width=340)
        right_col.pack(side=RIGHT, fill=Y)
        right_col.pack_propagate(False)  # 固定右欄寬度

        # 人臉辨識終端機開關
        face_ctrl_frame = ttk.Frame(left_col, padding=(20, 15, 20, 0))
        face_ctrl_frame.pack(fill=X)
        ttk.Label(face_ctrl_frame, textvariable=self.face_status_var, font=("微軟正黑體", 11)).pack(side=LEFT)
        self.face_btn = ttk.Button(
            face_ctrl_frame, text="📷 啟動人臉辨識終端機",
            bootstyle=(SUCCESS, OUTLINE), command=self.toggle_face_service,
        )
        self.face_btn.pack(side=RIGHT)
        self._update_face_status()

        # 右邊即時顯示 terminal_app.py 的輸出
        self.face_console = console_panel.build_console_panel(right_col, "人臉辨識即時輸出")

        self.count_var = tk.StringVar(value="載入中...")
        self.anomaly_var = tk.StringVar(value="載入中...")

        # 頂部統計卡片
        card_frame = ttk.Frame(left_col, padding=20)
        card_frame.pack(fill=X)

        f1 = ttk.Labelframe(card_frame, text="今日到職", padding=15, bootstyle=SUCCESS)
        f1.pack(side=LEFT, expand=True, fill=BOTH, padx=10)
        ttk.Label(f1, textvariable=self.count_var, font=("Helvetica", 28, "bold"), bootstyle=SUCCESS).pack()

        f2 = ttk.Labelframe(card_frame, text="安全警報", padding=15, bootstyle=DANGER)
        f2.pack(side=LEFT, expand=True, fill=BOTH, padx=10)
        ttk.Label(f2, textvariable=self.anomaly_var, font=("Helvetica", 28, "bold"), bootstyle=DANGER).pack()

        ttk.Button(f2, text="查看證據照", bootstyle=(DANGER, LINK), command=self.open_evidence_folder).pack()

        # 今日打卡清單
        list_frame = ttk.Frame(left_col, padding=20)
        list_frame.pack(fill=BOTH, expand=True)

        self.tree = ttk.Treeview(list_frame, columns=("time", "name", "status", "note"),
                                 show="headings", bootstyle=INFO)
        self.tree.heading("time", text="記錄時間")
        self.tree.heading("name", text="員工姓名")
        self.tree.heading("status", text="系統判定結果")
        self.tree.heading("note", text="備註")
        self.tree.column("status", width=200)
        self.tree.pack(fill=BOTH, expand=True)

        self.refresh_dashboard_data()

    def refresh_dashboard_data(self):
        """每 5 秒更新儀表板的人數、打卡清單與異常件數"""
        if not self.dash_win.winfo_exists():
            return

        # 查詢失敗時 result 為 None，維持畫面原本的數字
        result = dashboard_stats.get_today_attendance_summary(limit=20)
        if result is not None:
            count, logs = result
            self.count_var.set(f"{count} 人")
            self.tree.delete(*self.tree.get_children())
            for log in logs:
                note = "需要查核" if "異常" in log[2] or "遲到" in log[2] else "正常"
                self.tree.insert("", "end", values=(log[0], log[1], log[2], note))

        self.anomaly_var.set(f"{dashboard_stats.get_anomaly_count()} 件")

        self.dash_win.after(5000, self.refresh_dashboard_data)

    def open_evidence_folder(self):
        """開啟尾隨證據照片資料夾（Windows 專用）"""
        target = os.path.abspath(ANOMALY_DIR)
        if not os.path.exists(target):
            os.makedirs(target)
        os.startfile(target)  # noqa: S606

    # --- 視窗 2：員工帳號管理 ---
    def open_account_admin(self):
        """開啟員工帳號管理視窗，可停用、啟用、刪除所選員工"""
        admin_win = ttk.Toplevel(self.root)
        self.admin_win = admin_win
        admin_win.title("員工帳號管理")
        admin_win.geometry("1000x600")

        frame = ttk.Frame(admin_win, padding=20)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text="👤 員工帳號管理", font=("微軟正黑體", 16, "bold"),
                  bootstyle=PRIMARY).pack(anchor=W, pady=(0, 5))
        ttk.Label(
            frame,
            text="停用的帳號會立刻無法再刷臉打卡（系統判定為查無資料）；刪除不會影響已經留存的打卡歷史紀錄。",
            font=("微軟正黑體", 9), bootstyle=SECONDARY,
        ).pack(anchor=W, pady=(0, 10))

        columns = ("emp_id", "name", "gender", "role", "active", "last_status", "last_time")
        tree = ttk.Treeview(frame, columns=columns, show="headings", bootstyle=PRIMARY, selectmode="extended")
        tree.heading("emp_id", text="員工編號")
        tree.heading("name", text="姓名")
        tree.heading("gender", text="性別")
        tree.heading("role", text="角色")
        tree.heading("active", text="狀態")
        tree.heading("last_status", text="最新打卡狀態")
        tree.heading("last_time", text="最後刷卡時間")
        tree.pack(fill=BOTH, expand=True, pady=(0, 10))

        def refresh():
            """重新載入員工清單"""
            tree.delete(*tree.get_children())
            for u in account_admin.list_users():
                status_text = "🟢 啟用中" if u["is_active"] else "⚪ 已停用"
                tree.insert(
                    "", "end", iid=str(u["id"]),
                    values=(u["employee_id"], u["name"], u["gender"], u["role"],
                            status_text, u["last_status"], u["last_time"]),
                )

        def selected_ids():
            return [int(iid) for iid in tree.selection()]

        def require_selection():
            """取得所選員工 id，沒選就提示"""
            ids = selected_ids()
            if not ids:
                messagebox.showinfo("提示", "請先選取至少一筆員工資料。", **self._msgbox_parent("admin_win"))
            return ids

        def do_deactivate():
            ids = require_selection()
            for uid in ids:
                account_admin.set_active(uid, False)
            if ids:
                refresh()

        def do_activate():
            ids = require_selection()
            for uid in ids:
                account_admin.set_active(uid, True)
            if ids:
                refresh()

        def do_delete():
            ids = require_selection()
            if not ids:
                return
            names = [tree.item(str(uid), "values")[1] for uid in ids]
            if not messagebox.askyesno(
                "確認刪除",
                "確定要刪除以下員工帳號嗎？（打卡歷史紀錄不會被刪除，只是這個人之後沒辦法再刷臉打卡）\n\n"
                + "、".join(names),
                **self._msgbox_parent("admin_win"),
            ):
                return
            for uid in ids:
                account_admin.delete_user(uid)
            refresh()

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=X)
        ttk.Button(btn_frame, text="🔄 重新整理", bootstyle=(SECONDARY, OUTLINE),
                   command=refresh).pack(side=LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="⏸️ 停用所選", bootstyle=(WARNING, OUTLINE),
                   command=do_deactivate).pack(side=LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="▶️ 啟用所選", bootstyle=(SUCCESS, OUTLINE),
                   command=do_activate).pack(side=LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="🗑️ 刪除所選", bootstyle=(DANGER, OUTLINE), command=do_delete).pack(side=LEFT)

        refresh()

    def open_future_module(self, module_name):
        messagebox.showinfo("開發中", f"🚀 【{module_name}】模組正同步開發中！\n此介面已與核心資料庫連結成功。")

    # --- 視窗 3：管理者助理（另開行程的聊天視窗） ---
    def open_assistant_window(self):
        """開啟管理者助理聊天視窗；模型在那個行程裡載入，這裡只負責啟動與提示。"""
        ok, message = service_manager.start_assistant_window(lambda line: print(f"[管理者助理] {line}"))
        if ok:
            messagebox.showinfo("管理者助理", message, parent=self.root)
        else:
            messagebox.showwarning("管理者助理", message, parent=self.root)

    # --- 視窗 4：手機遠端 QR Code 註冊 ---
    def open_qr_registration(self):
        """開啟 QR Code 註冊視窗：每 5 秒向註冊伺服器取新 Token 並重畫 QR Code"""
        reg_window = ttk.Toplevel(self.root)
        self.reg_win = reg_window
        reg_window.title("手機遠端註冊 - 動態安全碼")
        reg_window.geometry("780x600")

        main_split = ttk.Frame(reg_window)
        main_split.pack(fill=BOTH, expand=True)

        left_col = ttk.Frame(main_split)
        left_col.pack(side=LEFT, fill=BOTH, expand=True)

        right_col = ttk.Frame(main_split, width=340)
        right_col.pack(side=RIGHT, fill=Y)
        right_col.pack_propagate(False)

        # 每次刷新都重新偵測 IP，因為熱點網卡的 IP 可能在開窗後才穩定
        def _current_base_url():
            return f"http://{service_manager.get_local_ip_offline()}:5000"

        # 註冊伺服器開關
        reg_ctrl_frame = ttk.Frame(left_col, padding=(20, 15, 20, 0))
        reg_ctrl_frame.pack(fill=X)
        ttk.Label(reg_ctrl_frame, textvariable=self.reg_status_var, font=("微軟正黑體", 11)).pack(side=LEFT)
        self.reg_btn = ttk.Button(
            reg_ctrl_frame, text="📱 啟動 QR 註冊伺服器",
            bootstyle=(WARNING, OUTLINE), command=self.toggle_registration_service,
        )
        self.reg_btn.pack(side=RIGHT)
        self._update_reg_status()

        # 右邊顯示 web_server.py 的輸出
        self.reg_console = console_panel.build_console_panel(right_col, "註冊伺服器即時輸出")

        ttk.Label(left_col, text="請新進員工掃描此碼進行註冊", font=("微軟正黑體", 14)).pack(pady=20)

        self.qr_label = ttk.Label(left_col)
        self.qr_label.pack(pady=10)

        self.url_label = ttk.Label(left_col, font=("Consolas", 10))
        self.url_label.pack(pady=10)

        # 刷新計時器 id，關窗時用來取消
        self.qr_timer_id = None

        # 伺服器啟動需要載入模型，前 30 秒連不上屬正常，只顯示提示不印錯誤
        self._qr_fail_count = 0
        _QR_STARTUP_GRACE_ATTEMPTS = 6  # 6 次 x 5 秒 = 30 秒

        def update_qr():
            """向註冊伺服器取 Token、產生 QR Code 並顯示，每 5 秒執行一次"""
            try:
                import urllib.request
                import json

                # 1. 取得 Token
                with urllib.request.urlopen("http://127.0.0.1:5000/api/internal/generate_token", timeout=2) as response:
                    data = json.loads(response.read().decode())
                    token = data['token']

                dynamic_url = f"{_current_base_url()}?t={token}"

                # 2. 在記憶體中產生 QR Code
                qr = pyqrcode.create(dynamic_url)
                buffer = io.BytesIO()
                qr.png(buffer, scale=6)
                buffer.seek(0)

                # 3. 轉成 Tkinter 可用的圖片
                img = Image.open(buffer)
                photo = ImageTk.PhotoImage(img)

                # 4. 更新畫面
                self.qr_label.config(image=photo)
                self.qr_label.image = photo
                self.url_label.config(text=f"目前網址: {dynamic_url}")
                self._qr_fail_count = 0

            except Exception as e:
                self._qr_fail_count += 1
                if self._qr_fail_count <= _QR_STARTUP_GRACE_ATTEMPTS:
                    self.url_label.config(text="伺服器準備中，請稍候...")
                else:
                    # 超過 30 秒仍連不上，印出詳細錯誤
                    print(f"[QR Update Error] 已連續 {self._qr_fail_count} 次連線失敗"
                          f"（約 {self._qr_fail_count * 5} 秒），可能是 web_server.py 沒有成功啟動：{e}")
                    self.url_label.config(text="⚠️ 伺服器連線失敗，請確認「啟動 QR 註冊伺服器」是否正常運作")
                    import traceback
                    traceback.print_exc()

            self.qr_timer_id = reg_window.after(5000, update_qr)

        # 關窗時取消計時器
        def on_close():
            if self.qr_timer_id:
                reg_window.after_cancel(self.qr_timer_id)
            reg_window.destroy()

        reg_window.protocol("WM_DELETE_WINDOW", on_close)

        update_qr()


if __name__ == "__main__":
    # 深色主題，也可換成 darkly、cyborg、solar；卡片顏色會自動依主題計算
    app = ttk.Window(themename="superhero")
    BackendManagerApp(app)
    app.mainloop()
