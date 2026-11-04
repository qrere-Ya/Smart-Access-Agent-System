"""
backend_main.py：門禁系統管理後台主程式（CustomTkinter，套用 shared/ui_theme）。

主要功能：
  - 主選單：五張功能卡片、即時時鐘、底部 CPU/RAM 狀態列與異常通知
  - 營運狀態與尾隨監控：今日打卡統計、異常件數、人臉辨識終端機開關與即時輸出
  - 員工帳號管理（account_panel）、出勤報告與假日請假（calendar_panel，內嵌 report_panel）
  - 遠端 QR Code 註冊：註冊伺服器開關，每 5 秒刷新動態 QR Code
  - 企業知識庫與出勤查詢：另開聊天視窗（rag.chat_window），查公司文件與考勤、異常紀錄
  - 登入保護：啟動先用人臉登入（login_window），閒置 15 分鐘或按「登出」就回到登入畫面
  整個行程只建立一個主視窗，登入畫面與主畫面共用。只負責畫面，實際工作轉發給其他模組。

串接模組：
  - service_manager：啟動、停止、監控背景服務與 IP 偵測
  - login_window、auth：人臉登入、工作階段與閒置逾時
  - account_panel、calendar_panel：各功能視窗
  - console_panel：內建終端機文字框；notification_bar：異常通知
  - dashboard_stats：儀表板統計查詢
  - ui_theme：色彩、字型、元件與對話框
  - database_mgr：初始化資料庫
"""
import datetime
import gc
import io
import os
import time
import tkinter as tk

import customtkinter as ctk
import pyqrcode
from PIL import Image

from admin import (
    account_panel,
    anomaly_panel,
    calendar_panel,
    console_panel,
    dashboard_stats,
    login_window,
    notification_bar,
    service_manager,
)
from shared import auth, paths, ui_theme
from shared import database_mgr as db

ANOMALY_DIR = paths.ANOMALY_DIR


class BackendManagerApp:
    """管理後台主視窗：建立主選單並開啟各功能視窗。"""

    def __init__(self, root, session=None):
        self.root = root
        self.session = session        # 登入後的工作階段；None 代表沒有啟用登入保護（測試或單獨開發時）
        self.relogin = False          # 登出或閒置逾時後，main() 會再顯示登入畫面
        self._last_touch = 0.0
        self._alive = True            # teardown 之後，定時更新的函式不再動舊畫面
        self.root.title("智慧門禁 - 戰情控制中心")
        self.root.geometry("700x680")
        self.root.minsize(640, 640)
        self.root.resizable(True, True)
        ui_theme.style_window(self.root)

        # 兩個服務的狀態文字，視窗關閉後仍保留，重開視窗時再同步實際狀態
        self.face_status_var = tk.StringVar(master=root, value="⚪ 人臉辨識未啟動")
        self.reg_status_var = tk.StringVar(master=root, value="⚪ 註冊伺服器未啟動")

        db.init_db()

        self._build_main_menu()
        self._start_session_watch()

        # 關閉視窗時詢問是否一併停止背景服務
        self.root.protocol("WM_DELETE_WINDOW", self.on_app_close)

    def _build_main_menu(self):
        """建立主選單：標題列、功能卡片牆、底部狀態列。"""
        header = ctk.CTkFrame(self.root, fg_color="transparent", height=1)
        header.pack(fill="x", padx=25, pady=(20, 10))

        brand_col = ctk.CTkFrame(header, fg_color="transparent", height=1)
        brand_col.pack(side="left")
        ctk.CTkLabel(brand_col, text="🔐 Smart Access Hub", font=ui_theme.font(ui_theme.FONT_SIZE_HERO, bold=True),
                     text_color=ui_theme.pair("text"), anchor="w").pack(anchor="w")
        ui_theme.muted_label(brand_col, "邊緣運算門禁後臺系統").pack(anchor="w", pady=(2, 0))
        ui_theme.status_badge(brand_col, "● 系統運作中", "success").pack(anchor="w", pady=(6, 0))

        clock_col = ctk.CTkFrame(header, fg_color="transparent", height=1)
        clock_col.pack(side="right")
        self.clock_var = tk.StringVar(master=self.root, value="--:--:--")
        self.date_var = tk.StringVar(master=self.root, value="----/--/--")
        ctk.CTkLabel(clock_col, textvariable=self.clock_var, font=ui_theme.mono_font(ui_theme.FONT_SIZE_HERO),
                     text_color=ui_theme.pair("text"), anchor="e").pack(anchor="e")
        ctk.CTkLabel(clock_col, textvariable=self.date_var, font=ui_theme.font(ui_theme.FONT_SIZE_SMALL),
                     text_color=ui_theme.pair("text_muted"), anchor="e").pack(anchor="e")
        if self.session is not None:
            who = ctk.CTkFrame(clock_col, fg_color="transparent", height=1)
            who.pack(anchor="e", pady=(4, 0))
            ctk.CTkLabel(who, text=f"👤 {self.session.admin_name}", font=ui_theme.font(ui_theme.FONT_SIZE_SMALL),
                         text_color=ui_theme.pair("info")).pack(side="left")
            ui_theme.secondary_button(who, "登出", self.logout, width=56, height=26).pack(side="left", padx=(8, 0))
        self._tick_clock()

        ctk.CTkFrame(self.root, height=1, fg_color=ui_theme.pair("border")).pack(fill="x", padx=25)
        ui_theme.muted_label(self.root, "功能模組").pack(fill="x", padx=25, pady=(12, 4))

        card_wall = ctk.CTkFrame(self.root, fg_color="transparent", height=1)
        card_wall.pack(fill="both", expand=True, padx=15, pady=(0, 5))
        card_wall.columnconfigure((0, 1), weight=1, uniform="cards")
        card_wall.rowconfigure((0, 1, 2), weight=1, uniform="cards")

        # (圖示, 標題, 說明, 強調色 token, 點擊要呼叫的函式)
        modules = [
            ("📊", "營運狀態與尾隨監控", "今日打卡統計、安全警報、人臉辨識即時輸出", "success", self.open_dashboard),
            ("👤", "員工帳號管理", "列出、停用、啟用、刪除員工帳號與角色", "info", self.open_account_admin),
            ("📱", "遠端 QR Code 註冊", "產生動態安全碼，讓新進員工手機掃碼註冊", "warning", self.open_qr_registration),
            ("🤖", "企業知識庫與出勤查詢", "用聊天查公司文件與法規、考勤紀錄、異常事件", "primary",
             self.open_assistant_window),
            ("🗓️", "出勤報告與假日請假", "每日出勤報告（可匯出 CSV）、登錄假日、請假與每週休息日", "success",
             lambda: calendar_panel.open_calendar_window(self.root)),
        ]
        for idx, (icon, title, desc, accent, cmd) in enumerate(modules):
            row, col = divmod(idx, 2)
            self._create_module_card(card_wall, icon, title, desc, accent, cmd, row, col)

        self._build_status_footer()

    def _create_module_card(self, master, icon, title, desc, accent, cmd, row, col):
        """建立一張整張可點擊的功能卡片：左邊圖示色塊，右邊標題與說明；滑鼠移入時亮起並換成強調色邊框。"""
        card = ui_theme.card(master, height=108)
        card.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
        card.pack_propagate(False)
        ctk.CTkLabel(card, text=icon, width=44, height=44, corner_radius=10, fg_color=ui_theme.pair(accent),
                     font=("Segoe UI Emoji", 20), text_color=ui_theme.pair("on_status")).pack(
            side="left", anchor="n", padx=(14, 10), pady=16)
        text_col = ctk.CTkFrame(card, fg_color="transparent", height=1)
        text_col.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=14)
        ctk.CTkLabel(text_col, text=title, font=ui_theme.font(ui_theme.FONT_SIZE_INPUT, bold=True),
                     text_color=ui_theme.pair("text"), anchor="w").pack(anchor="w")
        ui_theme.muted_label(text_col, desc, wraplength=210, justify="left").pack(anchor="w", pady=(2, 0))

        def on_enter(_event=None):
            card.configure(fg_color=ui_theme.pair("secondary"), border_color=ui_theme.pair(accent))

        def on_leave(_event=None):
            try:   # 指標移到卡片內的子元件時也會觸發離開事件，這時不要還原
                widget = card.winfo_containing(*card.winfo_pointerxy())
                while widget is not None and widget is not card:
                    widget = widget.master
                if widget is card:
                    return
            except (tk.TclError, KeyError):
                pass
            card.configure(fg_color=ui_theme.pair("card"), border_color=ui_theme.pair("border"))

        stack = [card]
        while stack:
            widget = stack.pop()
            stack.extend(w for w in widget.winfo_children() if isinstance(w, ctk.CTkBaseClass))   # 略過元件內部的畫布
            widget.bind("<Button-1>", lambda e: cmd(), add="+")
            widget.bind("<Enter>", on_enter, add="+")
            widget.bind("<Leave>", on_leave, add="+")

    # --- 登入保護：活動延長期限、過期或登出就回到登入畫面 ---
    def _start_session_watch(self):
        if self.session is None:
            return
        for event in ("<Key>", "<Button>", "<Motion>"):
            self.root.bind_all(event, self._note_activity, add="+")
        self._tick_session()

    def _note_activity(self, _event=None):
        """有操作就延長工作階段；每 5 秒最多寫一次檔。"""
        now = time.time()
        if self.session is None or now - self._last_touch < 5:
            return
        self._last_touch = now
        self.session.touch(now)
        auth.touch_token(self.session.token)

    def _tick_session(self):
        if self.session is None or not self._alive or not self.root.winfo_exists():
            return
        # 以工作階段檔為準：助理視窗那邊有操作也會延長，檔案被刪或過期就登出
        if auth.verify_token(self.session.token) is None:
            self.logout("閒置超過 15 分鐘，已自動登出。")
            return
        self.root.after(1000, self._tick_session)

    def logout(self, reason=None):
        """登出：關掉助理視窗、清除工作階段，結束主迴圈後由 main() 清掉畫面並重新登入（背景服務不受影響）。"""
        if self.session is None:
            return
        service_manager.stop_assistant_window()
        auth.clear_session()
        self.session = None
        self.relogin = True
        if reason:
            print(f"[後台] {reason}")
        self.root.quit()

    def teardown(self):
        """清掉整個後台畫面但保留同一個主視窗（同一個行程反覆建立 Tk 根視窗會讓 Tk 中止）。"""
        self._alive = False
        if getattr(self, "notification_bar", None) is not None:
            self.notification_bar.stop()
        for event in ("<Key>", "<Button>", "<Motion>"):
            self.root.unbind_all(event)
        for child in self.root.winfo_children():
            child.destroy()
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def _tick_clock(self):
        """每秒更新頂部時鐘；主視窗已銷毀就停止排程"""
        if not self._alive or not self.root.winfo_exists():
            return
        now = datetime.datetime.now()
        self.clock_var.set(now.strftime("%H:%M:%S"))
        self.date_var.set(now.strftime("%Y/%m/%d"))
        self.root.after(1000, self._tick_clock)

    def _build_status_footer(self):
        """建立底部 CPU/RAM 狀態列與異常通知"""
        ctk.CTkFrame(self.root, height=1, fg_color=ui_theme.pair("border")).pack(fill="x", padx=25)
        footer = ctk.CTkFrame(self.root, fg_color="transparent", height=1)
        footer.pack(fill="x", padx=25, pady=(8, 14))

        self.sys_status_var = tk.StringVar(master=self.root, value="系統資源監控準備中…")
        ctk.CTkLabel(footer, textvariable=self.sys_status_var, font=ui_theme.mono_font(ui_theme.FONT_SIZE_SMALL),
                     text_color=ui_theme.pair("text_muted")).pack(side="left")
        self.notification_bar = notification_bar.NotificationBar(footer, on_open=self.open_anomalies)  # 紅點與彈窗
        self.notification_bar.frame.pack(side="right")
        self._tick_system_status()

    def _tick_system_status(self):
        """每 3 秒讀取一次 CPU/RAM 顯示在狀態列；讀不到（沒裝 psutil）就顯示提示"""
        if not self._alive or not self.root.winfo_exists():
            return
        res = dashboard_stats.get_system_resources()
        if res is not None:
            self.sys_status_var.set(
                f"🖥️ CPU {res['cpu_percent']:.0f}%　💾 RAM {res['ram_used_gb']:.1f} / {res['ram_total_gb']:.1f} GB"
            )
        else:
            self.sys_status_var.set("系統資源監控未啟用（未偵測到 psutil）")
        self.root.after(3000, self._tick_system_status)

    def _dialog_parent(self, win_attr):
        """回傳對話框的父視窗：浮在使用者正在看的子視窗上；子視窗已關閉就用主視窗。"""
        win = getattr(self, win_attr, None)
        try:
            if win is not None and win.winfo_exists():
                return win
        except tk.TclError:
            pass
        return self.root

    # --- 人臉辨識終端機開關 ---
    def toggle_face_service(self):
        """人臉辨識終端機開關：執行中就停止，否則啟動"""
        parent = self._dialog_parent("dash_win")
        if service_manager.face_service_running():
            service_manager.stop_face_service()
            self._update_face_status()
            ui_theme.show_message(parent, "提示", "已停止人臉辨識終端機。")
            return

        def on_output_line(line):
            self.root.after(0, lambda: console_panel.append_line(getattr(self, "face_console", None), line))

        ok, message = service_manager.start_face_service(on_output_line)
        self._update_face_status()
        if ok:
            ui_theme.show_message(parent, "提示", message)
        else:
            ui_theme.show_message(parent, "啟動失敗", message, "error")

    def _update_face_status(self):
        """依實際狀態更新人臉辨識狀態文字與按鈕文字"""
        if service_manager.face_service_running():
            self.face_status_var.set("🟢 人臉辨識執行中")
            btn_text = "🛑 停止人臉辨識終端機"
        else:
            self.face_status_var.set("⚪ 人臉辨識未啟動")
            btn_text = "📷 啟動人臉辨識終端機"
        if hasattr(self, "face_btn") and self.face_btn.winfo_exists():
            self.face_btn.configure(text=btn_text)

    # --- QR 註冊伺服器開關 ---
    def toggle_registration_service(self):
        """QR 註冊伺服器開關：執行中就停止，否則啟動"""
        parent = self._dialog_parent("reg_win")
        if service_manager.reg_server_running():
            service_manager.stop_reg_service()
            self._update_reg_status()
            ui_theme.show_message(parent, "提示", "已停止 QR 註冊伺服器。")
            return

        def on_output_line(line):
            self.root.after(0, lambda: console_panel.append_line(getattr(self, "reg_console", None), line))

        ok, message = service_manager.start_reg_service(on_output_line)
        self._update_reg_status()
        if ok:
            ui_theme.show_message(parent, "提示", message)
        else:
            ui_theme.show_message(parent, "啟動失敗", message, "error")

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
            self.reg_btn.configure(text=btn_text)

    # --- 關閉主視窗 ---
    def on_app_close(self):
        """關閉主視窗時，詢問是否一併停止還在背景跑的服務"""
        if service_manager.any_service_running():
            if ui_theme.confirm(self.root, "確認關閉",
                                "偵測到有後端服務仍在執行中，要一併停止嗎？\n（選「否」會讓它們繼續在背景執行）",
                                ok_text="是", cancel_text="否"):
                service_manager.stop_all_services()
        if self.session is not None:
            service_manager.stop_assistant_window()
            auth.clear_session()
            self.session = None
        self.root.destroy()

    def _new_window(self, title, size):
        """建立套用主題底色的子視窗。"""
        win = ctk.CTkToplevel(self.root)
        win.title(title)
        win.geometry(size)
        ui_theme.style_window(win)
        return win

    # --- 視窗 1：營運狀態與尾隨監控 ---
    def open_dashboard(self):
        """開啟儀表板：左邊統計與打卡清單，右邊人臉辨識即時輸出"""
        self.dash_win = self._new_window("實時營運狀態", "1260x700")

        main_split = ctk.CTkFrame(self.dash_win, fg_color="transparent", height=1)
        main_split.pack(fill="both", expand=True)

        right_col = ctk.CTkFrame(main_split, fg_color="transparent", width=360, height=1)
        right_col.pack(side="right", fill="y")
        right_col.pack_propagate(False)  # 固定右欄寬度
        left_col = ctk.CTkFrame(main_split, fg_color="transparent", height=1)
        left_col.pack(side="left", fill="both", expand=True)

        # 人臉辨識終端機開關
        face_ctrl_frame = ctk.CTkFrame(left_col, fg_color="transparent", height=1)
        face_ctrl_frame.pack(fill="x", padx=20, pady=(15, 0))
        ctk.CTkLabel(face_ctrl_frame, textvariable=self.face_status_var, font=ui_theme.font(ui_theme.FONT_SIZE_INPUT),
                     text_color=ui_theme.pair("text")).pack(side="left")
        self.face_btn = ui_theme.secondary_button(face_ctrl_frame, "📷 啟動人臉辨識終端機", self.toggle_face_service)
        self.face_btn.pack(side="right")
        self._update_face_status()

        # 右邊即時顯示 terminal_app.py 的輸出
        self.face_console = console_panel.build_console_panel(right_col, "人臉辨識即時輸出")

        self.count_var = tk.StringVar(master=self.root, value="載入中...")
        self.anomaly_var = tk.StringVar(master=self.root, value="載入中...")

        # 頂部統計卡片
        card_frame = ctk.CTkFrame(left_col, fg_color="transparent", height=1)
        card_frame.pack(fill="x", padx=10, pady=15)
        for title, var, tone in (("今日到職", self.count_var, "success"), ("安全警報", self.anomaly_var, "danger")):
            box = ui_theme.card(card_frame, height=1)
            box.pack(side="left", expand=True, fill="both", padx=10)
            ui_theme.muted_label(box, title).pack(anchor="w", padx=15, pady=(12, 0))
            ctk.CTkLabel(box, textvariable=var, font=ui_theme.font(30, bold=True),
                         text_color=ui_theme.pair(tone)).pack(pady=(0, 6))
            if tone == "danger":
                ui_theme.link_button(box, "查看證據照", self.open_evidence_folder).pack(pady=(0, 10))
            else:
                ctk.CTkFrame(box, height=1, fg_color="transparent").pack(pady=(0, 10))

        # 今日打卡清單
        box, self.tree = ui_theme.table(
            left_col, (("time", "記錄時間", 170), ("name", "員工姓名", 130), ("status", "系統判定結果", 200),
                       ("note", "備註", 120)), height=12)
        box.pack(fill="both", expand=True, padx=20, pady=(0, 20))

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
            ui_theme.fill_table(self.tree, [
                (log[0], log[1], log[2], "需要查核" if "異常" in log[2] or "遲到" in log[2] else "正常")
                for log in logs])

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
        """開啟員工帳號管理視窗，可停用、啟用、刪除所選員工與設定管理者"""
        operator = self.session.admin_name if self.session is not None else None
        self.admin_win = account_panel.open_account_window(self.root, operator)

    def open_anomalies(self):
        """開啟異常事件列表（已開著就拉到最前面）；在這裡標為已讀會同步清掉通知列的紅點。"""
        win = getattr(self, "anomaly_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            return
        bar = self.notification_bar
        self.anomaly_win = anomaly_panel.open_anomaly_window(self.root, bar.notifier, bar.mark_all_read)

    # --- 視窗 3：企業知識庫與出勤查詢（另開行程的聊天視窗） ---
    def open_assistant_window(self):
        """開啟企業知識庫聊天視窗；模型在那個行程裡載入，這裡只負責啟動與提示。"""
        token = self.session.token if self.session is not None else None
        ok, message = service_manager.start_assistant_window(lambda line: print(f"[企業知識庫] {line}"), token)
        ui_theme.show_message(self.root, "企業知識庫與出勤查詢", message, "info" if ok else "warning")

    # --- 視窗 4：手機遠端 QR Code 註冊 ---
    def open_qr_registration(self):
        """開啟 QR Code 註冊視窗：每 5 秒向註冊伺服器取新 Token 並重畫 QR Code"""
        reg_window = self._new_window("手機遠端註冊 - 動態安全碼", "820x620")
        self.reg_win = reg_window

        main_split = ctk.CTkFrame(reg_window, fg_color="transparent", height=1)
        main_split.pack(fill="both", expand=True)

        right_col = ctk.CTkFrame(main_split, fg_color="transparent", width=360, height=1)
        right_col.pack(side="right", fill="y")
        right_col.pack_propagate(False)
        left_col = ctk.CTkFrame(main_split, fg_color="transparent", height=1)
        left_col.pack(side="left", fill="both", expand=True)

        # 每次刷新都重新偵測 IP，因為熱點網卡的 IP 可能在開窗後才穩定
        def _current_base_url():
            return f"http://{service_manager.get_local_ip_offline()}:5000"

        # 註冊伺服器開關
        reg_ctrl_frame = ctk.CTkFrame(left_col, fg_color="transparent", height=1)
        reg_ctrl_frame.pack(fill="x", padx=20, pady=(15, 0))
        ctk.CTkLabel(reg_ctrl_frame, textvariable=self.reg_status_var, font=ui_theme.font(ui_theme.FONT_SIZE_INPUT),
                     text_color=ui_theme.pair("text")).pack(side="left")
        self.reg_btn = ui_theme.secondary_button(reg_ctrl_frame, "📱 啟動 QR 註冊伺服器",
                                                self.toggle_registration_service)
        self.reg_btn.pack(side="right")
        self._update_reg_status()

        # 右邊顯示 web_server.py 的輸出
        self.reg_console = console_panel.build_console_panel(right_col, "註冊伺服器即時輸出")

        ctk.CTkLabel(left_col, text="請新進員工掃描此碼進行註冊", font=ui_theme.font(ui_theme.FONT_SIZE_TITLE),
                     text_color=ui_theme.pair("text")).pack(pady=20)

        self.qr_label = ctk.CTkLabel(left_col, text="", fg_color="transparent")
        self.qr_label.pack(pady=10)

        self.url_label = ctk.CTkLabel(left_col, text="", font=ui_theme.mono_font(ui_theme.FONT_SIZE_SMALL),
                                      text_color=ui_theme.pair("text_muted"), wraplength=380)
        self.url_label.pack(pady=10)

        # 刷新計時器 id，關窗時用來取消
        self.qr_timer_id = None

        # 伺服器啟動需要載入模型，前 30 秒連不上屬正常，只顯示提示不印錯誤
        self._qr_fail_count = 0
        _QR_STARTUP_GRACE_ATTEMPTS = 6  # 6 次 x 5 秒 = 30 秒

        def update_qr():
            """向註冊伺服器取 Token、產生 QR Code 並顯示，每 5 秒執行一次"""
            try:
                import json
                import urllib.request

                # 1. 取得 Token
                url = "http://127.0.0.1:5000/api/internal/generate_token"   # 固定的本機網址
                with urllib.request.urlopen(url, timeout=2) as response:  # nosec B310
                    data = json.loads(response.read().decode())
                    token = data['token']  # noqa: S105  這是註冊用的動態碼，不是密碼

                dynamic_url = f"{_current_base_url()}?t={token}"

                # 2. 在記憶體中產生 QR Code
                qr = pyqrcode.create(dynamic_url)
                buffer = io.BytesIO()
                qr.png(buffer, scale=6)
                buffer.seek(0)

                # 3. 轉成 CTkImage（自帶一圈白邊，亮暗色都掃得到）
                img = Image.open(buffer)
                self._qr_image = ctk.CTkImage(light_image=img, size=img.size)

                # 4. 更新畫面
                self.qr_label.configure(image=self._qr_image)
                self.url_label.configure(text=f"目前網址: {dynamic_url}")
                self._qr_fail_count = 0

            except Exception as e:
                self._qr_fail_count += 1
                if self._qr_fail_count <= _QR_STARTUP_GRACE_ATTEMPTS:
                    self.url_label.configure(text="伺服器準備中，請稍候...")
                else:
                    # 超過 30 秒仍連不上，只在主控台印出詳細錯誤，畫面不顯示例外內容
                    print(f"[QR Update Error] 已連續 {self._qr_fail_count} 次連線失敗"
                          f"（約 {self._qr_fail_count * 5} 秒），可能是 web_server.py 沒有成功啟動：{e}")
                    self.url_label.configure(text="⚠️ 伺服器連線失敗，請確認「啟動 QR 註冊伺服器」是否正常運作")

            self.qr_timer_id = reg_window.after(5000, update_qr)

        # 關窗時取消計時器
        def on_close():
            if self.qr_timer_id:
                reg_window.after_cancel(self.qr_timer_id)
            reg_window.destroy()

        reg_window.protocol("WM_DELETE_WINDOW", on_close)

        update_qr()


def create_hidden_root():
    """
    建立整個行程唯一的主視窗並先隱藏。隱藏後要立刻 update 一次：customtkinter 在第一次 update／mainloop 前
    認為視窗還不存在，之後 resizable 觸發的標題列處理會把剛 deiconify 的視窗又藏回去，主畫面就永遠不出現。
    """
    ui_theme.apply_theme("system")
    app = ctk.CTk()
    ui_theme.style_window(app)
    app.withdraw()
    app.update()
    return app


def main():
    """
    先人臉登入再開後台；登出或閒置逾時後回到登入畫面，直接關閉視窗才結束。
    整個行程只建立一個主視窗：同一個行程反覆建立 Tk 根視窗，Tk 會因樣式元素重複建立而中止。
    """
    app = create_hidden_root()
    db.init_db()
    while True:
        session = login_window.run_login(app, isolate_vision=True)    # 視覺放在登入完就結束的子行程，省約 1.3GB
        if session is None:
            break
        app.deiconify()
        backend = BackendManagerApp(app, session)
        app.mainloop()
        if not backend.relogin:      # 使用者直接關閉主視窗（on_app_close 已處理）
            break
        backend.teardown()
        app.withdraw()
        gc.collect()                 # 由主執行緒回收舊畫面的 Tk 物件，避免被別的執行緒回收而中止
    try:
        app.destroy()
    except Exception:  # noqa: S110  主視窗可能已經關掉
        pass


if __name__ == "__main__":
    main()
