"""
backend_main.py：門禁系統管理後台主程式（CustomTkinter，套用 shared/ui_theme）。

主要功能：
  - 單一主視窗（固定大小、可縮放）：頂部品牌與時鐘、淺色／深色／跟隨系統按鈕（記在 runtime/ui_mode.json）、
    中間內容區、底部 CPU/RAM 狀態列與異常通知；功能不再另開彈出視窗，按卡片直接換成該功能頁，頁首「返回主選單」回來
  - 主選單：六張功能卡片
  - 營運狀態與尾隨監控：今日打卡統計、異常件數、人臉辨識終端機開關與即時輸出；
    雙機的後台角色改顯示「前台連線」（燈號、最後回報時間、鏡頭與辨識狀態）與前台事件串流，並自動啟動連線服務
  - 員工帳號管理（account_panel）、出勤報告（calendar_panel，內嵌 report_panel）
  - 遠端 QR Code 註冊：註冊伺服器開關，每 5 秒刷新動態 QR Code
  - 企業知識庫與出勤查詢：模型很大，另開獨立行程的聊天視窗（rag.chat_window），查公司文件與考勤、異常紀錄
  - 登入保護：啟動先用人臉登入（login_window），閒置 15 分鐘或按「登出」就回到登入畫面
  整個行程只建立一個主視窗，登入畫面與主畫面共用。只負責畫面，實際工作轉發給其他模組。

串接模組：
  - service_manager：啟動、停止、監控背景服務與 IP 偵測
  - login_window、auth：人臉登入、工作階段與閒置逾時
  - account_panel、calendar_panel、anomaly_panel、digest_window、schedule_window：各功能頁（嵌在內容區）
  - console_panel：內建終端機文字框；notification_bar：異常通知
  - dashboard_stats：儀表板統計查詢
  - ui_theme：色彩、字型、元件與對話框
  - database_mgr：初始化資料庫
"""
import datetime
import gc
import io
import os
import queue
import threading
import time
import tkinter as tk

import customtkinter as ctk
import pyqrcode
from PIL import Image

from admin import (
    account_panel,
    anomaly_panel,
    calendar_fetch,
    calendar_panel,
    console_panel,
    dashboard_stats,
    digest_window,
    front_status,
    login_window,
    notification_bar,
    schedule_window,
    service_manager,
)
from shared import auth, front_control, paths, phone_address, ui_theme
from shared import database_mgr as db

ANOMALY_DIR = paths.ANOMALY_DIR
NEWLINE = chr(10)
WINDOW_MAX = (1280, 820)      # 主視窗的統一大小（螢幕放不下才縮小）
WINDOW_MIN = (1000, 640)
NOTICE_SECONDS = 8            # 底部提示訊息顯示幾秒


class BackendManagerApp:
    """管理後台主視窗：建立主選單並開啟各功能視窗。"""

    def __init__(self, root, session=None):
        self.root = root
        self.session = session        # 登入後的工作階段；None 代表沒有啟用登入保護（測試或單獨開發時）
        self.relogin = False          # 登出或閒置逾時後，main() 會再顯示登入畫面
        self._last_touch = 0.0
        self._alive = True            # teardown 之後，定時更新的函式不再動舊畫面
        self._console_lines = queue.Queue()   # 讀取執行緒只放進來，畫面更新留給畫面執行緒
        self.root.title("智慧門禁 - 戰情控制中心")
        self._place_window()
        self.root.resizable(True, True)
        ui_theme.style_window(self.root)
        self.page_key = None          # 目前顯示的頁面："home"＝主選單，其餘是功能頁
        self.page = None              # 目前功能頁的外框（含頁首與內容）；主選單時是 None
        self._page_args = None
        self._notice_job = None

        # 兩個服務的狀態文字，視窗關閉後仍保留，重開視窗時再同步實際狀態
        self.face_status_var = tk.StringVar(master=root, value="⚪ 人臉辨識未啟動")
        self.reg_status_var = tk.StringVar(master=root, value="⚪ 註冊伺服器未啟動")

        db.init_db()

        # 雙機的後台角色：自動啟動並看守連線服務；單機模式完全不碰
        self.link_wanted = service_manager.link_service_wanted()
        self.link_note = ""
        self._link_lines = []

        self._build_shell()
        self.show_home()
        self._start_session_watch()
        if self.link_wanted:
            self._tick_link_service()

        # 關閉視窗時詢問是否一併停止背景服務
        self.root.protocol("WM_DELETE_WINDOW", self.on_app_close)

    def _place_window(self):
        """統一的主視窗大小，置中；螢幕比預設小就縮到放得下。"""
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width, height = min(WINDOW_MAX[0], screen_w - 40), min(WINDOW_MAX[1], screen_h - 100)
        self.root.geometry(f"{width}x{height}+{(screen_w - width) // 2}+{max(0, (screen_h - height) // 2 - 20)}")
        self.root.minsize(min(WINDOW_MIN[0], width), min(WINDOW_MIN[1], height))

    def _build_shell(self):
        """建立固定不變的外框：頂部標題列、底部狀態列、中間內容區。切換功能頁只換內容區。"""
        self._build_header()
        ctk.CTkFrame(self.root, height=1, fg_color=ui_theme.pair("border")).pack(fill="x", padx=25)
        self._build_status_footer()                      # 先 pack 底部，內容區才不會把它擠掉
        self.content = ctk.CTkFrame(self.root, fg_color="transparent", height=1)
        self.content.pack(fill="both", expand=True)

    def _build_header(self):
        """標題列：品牌與狀態徽章、外觀按鈕、時鐘、登入者與登出。"""
        header = ctk.CTkFrame(self.root, fg_color="transparent", height=1)
        header.pack(fill="x", padx=25, pady=(16, 10))

        brand_col = ctk.CTkFrame(header, fg_color="transparent", height=1)
        brand_col.pack(side="left")
        ctk.CTkLabel(brand_col, text="🔐 Smart Access Hub", font=ui_theme.font(ui_theme.FONT_SIZE_HERO, bold=True),
                     text_color=ui_theme.pair("text"), anchor="w").pack(anchor="w")
        ui_theme.muted_label(brand_col, "邊緣運算門禁後臺系統").pack(anchor="w", pady=(2, 0))
        badges = ctk.CTkFrame(brand_col, fg_color="transparent", height=1)
        badges.pack(anchor="w", pady=(6, 0))
        ui_theme.status_badge(badges, "● 系統運作中", "success").pack(side="left")
        if self.link_wanted:      # 雙機後台：標題列就看得到前台有沒有連上，不用點進營運狀態頁
            self.front_badge = ui_theme.status_badge(badges, "● 前台未連線", "neutral")
            self.front_badge.pack(side="left", padx=(8, 0))
            self._refresh_front_badge()

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

        mode_col = ctk.CTkFrame(header, fg_color="transparent", height=1)
        mode_col.pack(side="right", padx=(0, 30))
        ui_theme.muted_label(mode_col, "外觀").pack(anchor="e")
        labels = [label for _mode, label in ui_theme.MODE_CHOICES]
        self.mode_buttons = ctk.CTkSegmentedButton(
            mode_col, values=labels, command=self._on_mode_choice, font=ui_theme.font(ui_theme.FONT_SIZE_SMALL),
            selected_color=ui_theme.pair("primary"), selected_hover_color=ui_theme.pair("primary_hover"),
            unselected_color=ui_theme.pair("secondary"), unselected_hover_color=ui_theme.pair("secondary_hover"),
            text_color=ui_theme.pair("on_secondary"))
        self.mode_buttons.pack(anchor="e", pady=(2, 0))
        self.mode_buttons.set(dict(ui_theme.MODE_CHOICES)[ui_theme.load_mode()])
        self._tick_clock()

    def _on_mode_choice(self, label):
        """按下淺色／深色／系統：立刻換外觀並記住；目前頁面重建一次，表格等元件才會跟著換色。"""
        mode = next((m for m, text in ui_theme.MODE_CHOICES if text == label), None)
        if mode is None:
            return
        ctk.set_appearance_mode(mode)
        ui_theme.save_mode(mode)
        ui_theme.style_window(self.root)
        self._rebuild_page()

    # --- 頁面：主選單與各功能頁都畫在同一個內容區 ---
    def _clear_page(self):
        """清掉內容區現有的畫面（主選單或功能頁）。"""
        self.root.update_idletasks()      # 先跑完閒置時排的重畫，銷毀後才不會對已不存在的元件重畫而出錯
        for child in self.content.winfo_children():
            child.destroy()
        self.page = None

    def _rebuild_page(self):
        """依目前頁面重畫（換外觀後用）。"""
        if self.page_key in (None, "home") or self._page_args is None:
            self.show_home()
        else:
            self._open_page(self.page_key, *self._page_args)

    def show_home(self):
        """回到主選單：功能卡片牆。"""
        self._clear_page()
        self.page_key, self._page_args = "home", None
        ui_theme.muted_label(self.content, "功能模組").pack(fill="x", padx=25, pady=(12, 4))
        card_wall = ctk.CTkFrame(self.content, fg_color="transparent", height=1)
        card_wall.pack(fill="both", expand=True, padx=15, pady=(0, 5))
        card_wall.columnconfigure((0, 1), weight=1, uniform="cards")
        card_wall.rowconfigure((0, 1, 2), weight=1, uniform="cards")

        # (圖示, 標題, 說明, 強調色 token, 點擊要呼叫的函式)
        modules = [
            ("📊", "營運狀態與尾隨監控", "今日打卡統計、安全警報、人臉辨識即時輸出", "success", self.open_dashboard),
            ("👤", "員工帳號管理", "列出、停用、啟用、刪除員工帳號與角色", "info", self.open_account_admin),
            ("📱", "遠端 QR Code 註冊", "產生動態安全碼，讓新進員工手機掃碼註冊", "warning", self.open_qr_registration),
            ("🤖", "企業知識庫與出勤查詢", "用聊天查公司文件與法規、考勤紀錄、異常事件（模型另開獨立視窗）", "primary",
             self.open_assistant_window),
            ("🗓️", "出勤報告", "每日出勤報告（可匯出 CSV）；週六日與國定假日自動判斷", "success",
             self.open_calendar),
            ("📰", "出勤日報", "查看每天的出勤明細與 AI 摘要（排程時間在底部「⚙ 排程設定」）", "info",
             self.open_report),
        ]
        for idx, (icon, title, desc, accent, cmd) in enumerate(modules):
            row, col = divmod(idx, 2)
            self._create_module_card(card_wall, icon, title, desc, accent, cmd, row, col)

    def _open_page(self, key, title, build):
        """切換到功能頁：頁首有「返回主選單」與標題，下方是 build(body) 畫的內容；回傳內容區的外框。"""
        self._clear_page()
        self.page_key, self._page_args = key, (title, build)
        page = ctk.CTkFrame(self.content, fg_color="transparent", height=1)
        page.pack(fill="both", expand=True)
        bar = ctk.CTkFrame(page, fg_color="transparent", height=1)
        bar.pack(fill="x", padx=20, pady=(10, 4))
        ui_theme.secondary_button(bar, "← 返回主選單", self.show_home, width=120, height=32).pack(side="left")
        ctk.CTkLabel(bar, text=title, font=ui_theme.font(ui_theme.FONT_SIZE_TITLE, bold=True),
                     text_color=ui_theme.pair("text")).pack(side="left", padx=16)
        body = ctk.CTkFrame(page, fg_color="transparent", height=1)
        body.pack(fill="both", expand=True)
        self.page = page
        build(body)
        return body

    def notice(self, text, seconds=NOTICE_SECONDS):
        """底部狀態列顯示一則短暫提示（取代彈出訊息）。"""
        if not self._alive or not self.root.winfo_exists():
            return
        self.notice_var.set(text)
        if self._notice_job is not None:
            self.root.after_cancel(self._notice_job)
        self._notice_job = self.root.after(int(seconds * 1000), lambda: self.notice_var.set(""))

    def _create_module_card(self, master, icon, title, desc, accent, cmd, row, col):
        """建立一張整張可點擊的功能卡片：左邊圖示色塊，右邊標題與說明；滑鼠移入時亮起並換成強調色邊框。"""
        card = ui_theme.card(master, height=108)
        card.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
        card.pack_propagate(False)
        ctk.CTkLabel(card, text=icon, width=56, height=56, corner_radius=12, fg_color=ui_theme.pair(accent),
                     font=("Segoe UI Emoji", 26), text_color=ui_theme.pair("on_status")).pack(
            side="left", padx=(22, 16))
        text_col = ctk.CTkFrame(card, fg_color="transparent", height=1)
        text_col.pack(side="left", fill="x", expand=True, padx=(0, 16))      # 不 fill y：文字在卡片裡垂直置中
        ctk.CTkLabel(text_col, text=title, font=ui_theme.font(ui_theme.FONT_SIZE_TITLE, bold=True),
                     text_color=ui_theme.pair("text"), anchor="w").pack(anchor="w")
        ui_theme.muted_label(text_col, desc, wraplength=440, justify="left").pack(anchor="w", pady=(4, 0))

        def on_enter(_event=None):
            if not card.winfo_exists():       # 換頁銷毀卡片時，虛擬螢幕會補送進出事件，這時不要再動已不存在的卡片
                return
            card.configure(fg_color=ui_theme.pair("secondary"), border_color=ui_theme.pair(accent))

        def on_leave(_event=None):
            if not card.winfo_exists():
                return
            try:   # 指標移到卡片內的子元件時也會觸發離開事件，這時不要還原
                widget = card.winfo_containing(*card.winfo_pointerxy())
                while widget is not None and widget is not card:
                    widget = widget.master
                if widget is card:
                    return
            except (tk.TclError, KeyError):
                pass
            card.configure(fg_color=ui_theme.pair("card"), border_color=ui_theme.pair("border"))

        def on_click(event=None):
            if event is None:         # 不帶事件的呼叫不是使用者點的，不能因此換頁（見 ui_theme.event_only）
                return
            # 換頁會銷毀這張卡片：等這次點擊事件處理完再換，不要在事件還在分派時銷毀正在處理它的元件
            self.root.after_idle(cmd)

        stack = [card]
        while stack:
            widget = stack.pop()
            stack.extend(w for w in widget.winfo_children() if isinstance(w, ctk.CTkBaseClass))   # 略過元件內部的畫布
            widget.bind("<Button-1>", on_click, add="+")
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
        for name in ("digest_runner", "morning_runner"):
            if getattr(self, name, None) is not None:
                getattr(self, name).stop()
        for event in ("<Key>", "<Button>", "<Motion>"):
            self.root.unbind_all(event)
        self.root.update_idletasks()      # 先跑完閒置時排的重畫，銷毀後才不會對已不存在的元件重畫而出錯
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
        footer = ctk.CTkFrame(self.root, fg_color="transparent", height=1)
        footer.pack(side="bottom", fill="x", padx=25, pady=(8, 14))
        ctk.CTkFrame(self.root, height=1, fg_color=ui_theme.pair("border")).pack(side="bottom", fill="x", padx=25)

        self.sys_status_var = tk.StringVar(master=self.root, value="系統資源監控準備中…")
        self.notice_var = tk.StringVar(master=self.root, value="")
        ctk.CTkLabel(footer, textvariable=self.sys_status_var, font=ui_theme.mono_font(ui_theme.FONT_SIZE_SMALL),
                     text_color=ui_theme.pair("text_muted")).pack(side="left")
        ctk.CTkLabel(footer, textvariable=self.notice_var, font=ui_theme.font(ui_theme.FONT_SIZE_SMALL),
                     text_color=ui_theme.pair("warning")).pack(side="left", padx=(24, 0))
        self.notification_bar = notification_bar.NotificationBar(footer, on_open=self.open_anomalies)  # 紅點與彈窗
        self.notification_bar.frame.pack(side="right")
        self.digest_runner = digest_window.digest_runner(self.root, on_new=self._digest_ready)
        self.morning_runner = digest_window.morning_runner(self.root, on_new=self._morning_ready)   # 晨報只給前台
        ui_theme.link_button(footer, "⚙ 排程設定", self.open_schedule, width=90,
                             height=24).pack(side="right", padx=(0, 10))
        self.calendar_refresher = calendar_fetch.CalendarRefresher()
        self.root.after(5000, self._tick_digest)
        self._tick_system_status()
        self._drain_console_lines()

    def _drain_console_lines(self):
        """畫面執行緒每 150 毫秒取出服務輸出（人臉辨識、QR 註冊）的新行，顯示到對應的主控台。"""
        if not self._alive or not self.root.winfo_exists():
            return
        for _ in range(200):                      # 一次最多處理 200 行，避免輸出暴增時卡住畫面
            try:
                which, line = self._console_lines.get_nowait()
            except queue.Empty:
                break
            console_panel.append_line(getattr(self, which, None), line)
        self.root.after(150, self._drain_console_lines)

    def _tick_link_service(self):
        """每 10 秒確認後台連線服務還在執行，沒有就啟動（網路線剛插好、位址還在分配時會失敗，下一輪再試）。"""
        if not self._alive or not self.root.winfo_exists():
            return
        if not service_manager.link_service_running():
            def on_output_line(line):
                self._link_lines = (self._link_lines + [line])[-20:]

            ok, message = service_manager.start_link_service(on_output_line)
            self.link_note = "" if ok else message
        else:
            self.link_note = ""
        self.root.after(10000, self._tick_link_service)

    def _refresh_front_badge(self):
        """依後台收到的最新前台心跳更新主選單的前台連線徽章（不連任何機器）。"""
        kind, text = front_status.badge_view(front_status.load())
        ui_theme.set_badge(self.front_badge, text, kind)

    def _tick_system_status(self):
        """每 3 秒讀取一次 CPU/RAM 顯示在狀態列；讀不到（沒裝 psutil）就顯示提示"""
        if not self._alive or not self.root.winfo_exists():
            return
        if self.link_wanted and self.front_badge.winfo_exists():
            self._refresh_front_badge()
        res = dashboard_stats.get_system_resources()
        if res is not None:
            self.sys_status_var.set(
                f"🖥️ CPU {res['cpu_percent']:.0f}%　💾 RAM {res['ram_used_gb']:.1f} / {res['ram_total_gb']:.1f} GB"
            )
        else:
            self.sys_status_var.set("系統資源監控未啟用（未偵測到 psutil）")
        self.root.after(3000, self._tick_system_status)

    # --- 人臉辨識終端機開關 ---
    def toggle_face_service(self):
        """人臉辨識終端機開關：執行中就停止，否則啟動"""
        if service_manager.face_service_running():
            service_manager.stop_face_service()
            self._update_face_status()
            self.notice("已停止人臉辨識終端機。")
            return

        def on_output_line(line):
            self._console_lines.put(("face_console", line))        # 在讀取執行緒呼叫：只放佇列，不碰 tk

        ok, message = service_manager.start_face_service(on_output_line)
        self._update_face_status()
        if ok:
            self.notice(message)
        else:
            ui_theme.show_message(self.root, "啟動失敗", message, "error")

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
        if service_manager.reg_server_running():
            service_manager.stop_reg_service()
            self._update_reg_status()
            self.notice("已停止 QR 註冊伺服器。")
            return

        def on_output_line(line):
            self._console_lines.put(("reg_console", line))         # 在讀取執行緒呼叫：只放佇列，不碰 tk

        ok, message = service_manager.start_reg_service(on_output_line)
        self._update_reg_status()
        if ok:
            self.notice(message)
        else:
            ui_theme.show_message(self.root, "啟動失敗", message, "error")

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

    # --- 頁面 1：營運狀態與尾隨監控 ---
    def open_dashboard(self):
        """切到儀表板頁：左邊統計與打卡清單，右邊人臉辨識即時輸出"""
        self._open_page("dashboard", "📊 營運狀態與尾隨監控", self._build_dashboard)

    def _build_dashboard(self, body):
        self.dash_page = body
        main_split = ctk.CTkFrame(body, fg_color="transparent", height=1)
        main_split.pack(fill="both", expand=True)

        right_col = ctk.CTkFrame(main_split, fg_color="transparent", width=360, height=1)
        right_col.pack(side="right", fill="y")
        right_col.pack_propagate(False)  # 固定右欄寬度
        left_col = ctk.CTkFrame(main_split, fg_color="transparent", height=1)
        left_col.pack(side="left", fill="both", expand=True)

        if self.link_wanted:
            # 雙機後台：前台在另一台電腦，這裡看前台連線狀態與前台送來的事件（沒有本機的人臉辨識開關）
            self._build_link_card(left_col)
            self._build_event_stream(right_col)
        else:
            # 人臉辨識終端機開關
            face_ctrl_frame = ctk.CTkFrame(left_col, fg_color="transparent", height=1)
            face_ctrl_frame.pack(fill="x", padx=20, pady=(15, 0))
            ctk.CTkLabel(face_ctrl_frame, textvariable=self.face_status_var,
                         font=ui_theme.font(ui_theme.FONT_SIZE_INPUT),
                         text_color=ui_theme.pair("text")).pack(side="left")
            self.face_btn = ui_theme.secondary_button(face_ctrl_frame, "📷 啟動人臉辨識終端機",
                                                      self.toggle_face_service)
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

    def _build_link_card(self, parent):
        """雙機後台：前台連線狀態卡片（燈號、最後回報時間、細節）與連線服務狀態。"""
        box = ui_theme.card(parent, height=1)
        box.pack(fill="x", padx=20, pady=(15, 0))
        ui_theme.section_title(box, "🔌 前台連線").pack(anchor="w", padx=15, pady=(12, 0))
        self.link_headline = ctk.CTkLabel(box, text="", font=ui_theme.font(18, bold=True), anchor="w")
        self.link_headline.pack(fill="x", padx=15, pady=(4, 0))
        self.link_detail = ui_theme.muted_label(box, "")
        self.link_detail.configure(justify="left", anchor="w")
        self.link_detail.pack(fill="x", padx=15, pady=(2, 0))
        self.link_service_label = ui_theme.muted_label(box, "")
        self.link_service_label.configure(anchor="w")
        self.link_service_label.pack(fill="x", padx=15, pady=(6, 6))
        # 前台人臉辨識的遠端停止／恢復：前台只有「啟動」按鈕，停止由這裡下指令（前台同步時套用）
        control = ctk.CTkFrame(box, fg_color="transparent")
        control.pack(fill="x", padx=15, pady=(0, 12))
        self.face_control_label = ui_theme.muted_label(control, "")
        self.face_control_label.configure(anchor="w", justify="left", wraplength=520)
        self.face_control_label.pack(side="left", fill="x", expand=True)
        self.face_control_confirming = False
        self.face_control_cancel = ui_theme.secondary_button(control, "取消", self._cancel_face_control, width=70)
        self.face_control_btn = ui_theme.secondary_button(control, "", self._press_face_control, width=170)
        self.face_control_btn.pack(side="right")
        self._refresh_face_control()

    def _refresh_face_control(self):
        """依後台目前的設定更新「停止／恢復前台辨識」按鈕與說明（按下去要在卡片內再確認一次）。"""
        enabled = front_control.recognition_enabled()
        if self.face_control_confirming:
            self.face_control_btn.configure(text="確定停止" if enabled else "確定恢復")
            self.face_control_cancel.pack(side="right", padx=(0, 8))
            return
        self.face_control_cancel.pack_forget()
        if enabled:
            self.face_control_btn.configure(text="⏸ 停止前台辨識")
            self.face_control_label.configure(text="前台人臉辨識：允許（前台的使用者自己按「啟動」）")
        else:
            self.face_control_btn.configure(text="▶ 恢復前台辨識")
            self.face_control_label.configure(
                text="前台人臉辨識：已由後台停止（最慢約 10 秒套用；恢復後前台仍要有人按「啟動」；"
                     "前台離線時下次連上才套用）")

    def _press_face_control(self):
        """第一次按進入確認，第二次按才寫入設定。"""
        if not self.face_control_confirming:
            self.face_control_confirming = True
            self.face_control_label.configure(text="確定要停止前台人臉辨識嗎？前台門禁將無法打卡。"
                                              if front_control.recognition_enabled() else "確定要恢復前台人臉辨識嗎？")
        else:
            self.face_control_confirming = False
            front_control.set_recognition_enabled(not front_control.recognition_enabled())
        self._refresh_face_control()

    def _cancel_face_control(self):
        self.face_control_confirming = False
        self._refresh_face_control()

    def _build_event_stream(self, parent):
        """雙機後台：右邊顯示前台送來的事件（打卡、尾隨、經過未打卡），新的在前。"""
        box = ui_theme.card(parent)
        box.pack(fill="both", expand=True, padx=(0, 15), pady=15)
        ui_theme.section_title(box, "📨 前台事件串流").pack(anchor="w", padx=ui_theme.PAD_MD,
                                                          pady=(ui_theme.PAD_MD, 6))
        table_box, self.event_tree = ui_theme.table(box, (("time", "時間", 140), ("text", "事件", 200)), height=14)
        table_box.pack(fill="both", expand=True, padx=ui_theme.PAD_MD, pady=(0, ui_theme.PAD_MD))

    def _refresh_link_card(self):
        """讀後台收到的最新前台心跳並更新畫面（不連任何機器）。"""
        tone, headline, details = front_status.describe(front_status.load())
        colour = {"success": "success", "danger": "danger"}.get(tone, "text_muted")
        self.link_headline.configure(text=headline, text_color=ui_theme.pair(colour))
        self.link_detail.configure(text=NEWLINE.join(details))
        if service_manager.link_service_running():
            self.link_service_label.configure(text="🟢 後台連線服務執行中（只給前台使用，TLS 加密）")
        else:
            reason = f"：{self.link_note}" if self.link_note else "（正在嘗試啟動）"
            self.link_service_label.configure(text=f"🔴 後台連線服務未執行{reason}")
        self._refresh_face_control()
        events = dashboard_stats.get_recent_events(limit=30)
        if events is not None:
            ui_theme.fill_table(self.event_tree, [(ts[5:], text) for ts, text in events])

    def refresh_dashboard_data(self, page=None):
        """每 5 秒更新儀表板的人數、打卡清單與異常件數；離開儀表板頁（或換外觀重畫成新的一頁）後這一輪就停止。"""
        page = page or self.dash_page
        if page is not self.dash_page or not page.winfo_exists() or not self._alive:
            return
        if self.link_wanted:
            self._refresh_link_card()

        # 查詢失敗時 result 為 None，維持畫面原本的數字
        result = dashboard_stats.get_today_attendance_summary(limit=20)
        if result is not None:
            count, logs = result
            self.count_var.set(f"{count} 人")
            ui_theme.fill_table(self.tree, [
                (log[0], log[1], log[2], "需要查核" if "異常" in log[2] or "遲到" in log[2] else "正常")
                for log in logs])

        self.anomaly_var.set(f"{dashboard_stats.get_anomaly_count()} 件")

        self.root.after(5000, self.refresh_dashboard_data, page)       # 掛在主視窗上：頁面銷毀時不會留下失效的排程

    def open_evidence_folder(self):
        """開啟尾隨證據照片資料夾（Windows 專用）"""
        target = os.path.abspath(ANOMALY_DIR)
        if not os.path.exists(target):
            os.makedirs(target)
        os.startfile(target)  # noqa: S606

    # --- 頁面 2：員工帳號管理 ---
    def open_account_admin(self):
        """切到員工帳號管理頁，可停用、啟用、刪除所選員工與設定管理者"""
        operator = self.session.admin_name if self.session is not None else None

        def build(body):
            self.account_view = account_panel.AccountPanel(body, operator)
            self.account_view.pack(fill="both", expand=True)

        self._open_page("accounts", "👤 員工帳號管理", build)

    def open_calendar(self):
        """切到出勤報告頁"""
        def build(body):
            self.calendar_view = calendar_panel.CalendarPanel(body)
            self.calendar_view.pack(fill="both", expand=True)

        self._open_page("calendar", "🗓️ 出勤報告", build)

    def _tick_digest(self):
        """每分鐘看一次該不該產生科技早報與出勤日報（到時間、當天要上班、還沒有那份才會產生）。"""
        if not self._alive or not self.root.winfo_exists():
            return
        self.morning_runner.tick()
        self.digest_runner.tick()
        self.calendar_refresher.tick()          # 國定假日資料到期才在背景更新，其餘時候什麼都不做
        self.root.after(60000, self._tick_digest)

    @staticmethod
    def _morning_ready(_report):
        """科技早報產生完成：後台不顯示，前台代理下一輪同步就會拿到。"""
        print("[後台] 科技早報已產生，前台下次同步會顯示。")

    def _digest_ready(self, _report):
        """排程產生了新出勤日報：人在主選單就直接切過去看；正在別的功能頁就不打斷，只在底部提示。"""
        if not self._alive or not self.content.winfo_exists():       # 畫面已清掉：背景才做完的日報不再動畫面
            return
        if self.page_key == "home":
            self.open_report("digest")
        else:
            self.notice("新的出勤日報已產生，回主選單按「出勤日報」卡片查看。")

    def open_report(self, kind="digest"):
        """切到出勤日報頁（kind 保留給舊呼叫端）；已經在出勤日報頁就只重畫。"""
        view = getattr(self, "report_view", None)
        if self.page_key == "report" and view is not None and view.winfo_exists():
            view.show(kind)
            return

        def build(body):
            self.report_view = digest_window.ReportView(body, self.digest_runner, self.morning_runner, kind,
                                                        on_close=self.show_home)
            self.report_view.pack(fill="both", expand=True)

        self._open_page("report", "📋 出勤日報", build)

    def open_schedule(self):
        """切到排程設定頁（出勤日報與科技早報的排程時間、立即產生、全本地模式）；入口是底部「⚙ 排程設定」。"""
        def build(body):
            self.schedule_view = schedule_window.ScheduleView(body, self.digest_runner, self.morning_runner,
                                                              self.open_report, on_close=self.show_home)
            self.schedule_view.pack(fill="both", expand=True)

        self._open_page("schedule", "⚙ 排程設定", build)

    def open_anomalies(self):
        """切到異常事件頁；在這裡標為已讀會同步清掉通知列的紅點。"""
        bar = self.notification_bar

        def build(body):
            self.anomaly_view = anomaly_panel.AnomalyPanel(body, bar.notifier, bar.mark_all_read)
            self.anomaly_view.pack(fill="both", expand=True)

        self._open_page("anomaly", "🔔 異常事件", build)

    # --- 企業知識庫與出勤查詢（模型要另開行程載入，所以是獨立的聊天視窗） ---
    def open_assistant_window(self):
        """開啟企業知識庫聊天視窗；模型在那個行程裡載入，這裡只負責啟動與提示。"""
        token = self.session.token if self.session is not None else None
        ok, message = service_manager.start_assistant_window(lambda line: print(f"[企業知識庫] {line}"), token)
        if ok:
            self.notice(message, seconds=12)
        else:
            ui_theme.show_message(self.root, "企業知識庫與出勤查詢", message, "warning")

    # --- 頁面 4：手機遠端 QR Code 註冊 ---
    @staticmethod
    def _fetch_qr_token():
        """向本機註冊伺服器取一組新 Token（在背景執行緒呼叫，連不上會阻塞到逾時）；不走系統代理。"""
        import json
        import urllib.request

        url = "http://127.0.0.1:5000/api/internal/generate_token"   # 固定的本機網址
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=2) as response:  # nosec B310
            return json.loads(response.read().decode())["token"]  # noqa: S105  這是註冊用的動態碼，不是密碼

    @staticmethod
    def _detect_qr_channels():
        """這台電腦所有可當註冊通道的位址（在背景執行緒呼叫，會執行 ipconfig）。"""
        return service_manager.get_phone_addresses()

    def open_qr_registration(self):
        """切到 QR Code 註冊頁：每 5 秒（背景）向註冊伺服器取新 Token、偵測所有網路位址並重畫 QR Code；
        所有位址都是註冊通道，可在選單切換"""
        self._open_page("qr", "📱 手機遠端註冊 - 動態安全碼", self._build_qr_page)

    def _build_qr_page(self, reg_window):
        self.reg_page = reg_window

        main_split = ctk.CTkFrame(reg_window, fg_color="transparent", height=1)
        main_split.pack(fill="both", expand=True)

        right_col = ctk.CTkFrame(main_split, fg_color="transparent", width=360, height=1)
        right_col.pack(side="right", fill="y")
        right_col.pack_propagate(False)
        left_col = ctk.CTkFrame(main_split, fg_color="transparent", height=1)
        left_col.pack(side="left", fill="both", expand=True)

        # 註冊通道：這台電腦所有可用的網路位址都是通道（手機連哪個網路就選哪個），預設選最適合手機的；
        # 每 5 秒在背景重新偵測（熱點網卡的位址可能在開窗後才穩定），位址集合沒變就保留使用者選的
        self._reg_closed = False
        self._qr_channels = None            # None＝還沒偵測過；[] ＝偵測過但沒有可用位址
        self._qr_selected_ip = None
        self._qr_last_token = None
        self._qr_image = None
        self._qr_blank = ctk.CTkImage(light_image=Image.new("RGBA", (1, 1), (0, 0, 0, 0)), size=(1, 1))
        self._qr_results = queue.Queue()    # 背景執行緒只把結果放進來，畫面更新留給畫面執行緒（tk 不能跨執行緒呼叫）
        self._qr_fetching = False
        self._qr_next_fetch = 0.0
        self._qr_fail_count = 0
        self.qr_timer_id = None

        # 註冊伺服器開關
        reg_ctrl_frame = ctk.CTkFrame(left_col, fg_color="transparent", height=1)
        reg_ctrl_frame.pack(fill="x", padx=20, pady=(15, 0))
        ctk.CTkLabel(reg_ctrl_frame, textvariable=self.reg_status_var, font=ui_theme.font(ui_theme.FONT_SIZE_INPUT),
                     text_color=ui_theme.pair("text")).pack(side="left")
        self.reg_btn = ui_theme.secondary_button(reg_ctrl_frame, "📱 啟動 QR 註冊伺服器",
                                                self.toggle_registration_service)
        self.reg_btn.pack(side="right")
        self._update_reg_status()

        channel_frame = ctk.CTkFrame(left_col, fg_color="transparent", height=1)
        channel_frame.pack(fill="x", padx=20, pady=(10, 0))
        ctk.CTkLabel(channel_frame, text="註冊通道（手機連哪個網路就選哪個）",
                     font=ui_theme.font(ui_theme.FONT_SIZE_SMALL), text_color=ui_theme.pair("text_muted"),
                     anchor="w").pack(fill="x")
        self.channel_combo = ui_theme.combo(channel_frame, ["偵測網路位址中…"], command=lambda label: on_channel(label))
        self.channel_combo.pack(fill="x", pady=(2, 0))

        # 右邊顯示 web_server.py 的輸出
        self.reg_console = console_panel.build_console_panel(right_col, "註冊伺服器即時輸出")

        ctk.CTkLabel(left_col, text="請新進員工掃描此碼進行註冊", font=ui_theme.font(ui_theme.FONT_SIZE_TITLE),
                     text_color=ui_theme.pair("text")).pack(pady=20)

        self.qr_label = ctk.CTkLabel(left_col, text="", fg_color="transparent")
        self.qr_label.pack(pady=10)

        self.url_label = ctk.CTkLabel(left_col, text="", font=ui_theme.mono_font(ui_theme.FONT_SIZE_SMALL),
                                      text_color=ui_theme.pair("text_muted"), wraplength=380)
        self.url_label.pack(pady=10)

        _QR_STARTUP_GRACE_ATTEMPTS = 6  # 伺服器啟動需要載入模型，前 6 次（約 30 秒）連不上屬正常，只顯示提示不印錯誤

        def worker():
            """背景執行緒：偵測所有通道、向註冊伺服器取 Token，結果放進佇列（不碰任何 tk 元件）。"""
            try:
                channels = self._detect_qr_channels()
            except Exception as e:  # noqa: BLE001
                print(f"[QR 註冊] 偵測網路位址失敗：{type(e).__name__}")
                channels = None
            try:
                outcome = ("ok", self._fetch_qr_token())
            except Exception as e:  # noqa: BLE001
                outcome = ("err", e)
            self._qr_results.put((outcome[0], outcome[1], channels))

        def set_channels(channels):
            """更新通道清單與選單；位址集合沒變就保留使用者選的，選的位址消失就回到預設（最適合手機的）。"""
            ips = [c["ip"] for c in channels]
            if self._qr_channels is not None and {c["ip"] for c in self._qr_channels} == set(ips):
                return
            self._qr_channels = channels
            if self._qr_selected_ip not in ips:
                chosen = phone_address.best(channels)
                self._qr_selected_ip = chosen["ip"] if chosen else None
            labels = [c["label"] for c in channels] or ["（沒有可用的網路位址）"]
            self.channel_combo.configure(values=labels)
            self.channel_combo.set(next((c["label"] for c in channels if c["ip"] == self._qr_selected_ip), labels[0]))

        def render():
            """依目前選的通道與最新 Token 畫 QR Code 與網址。"""
            if self._qr_channels is None or self._qr_last_token is None and self._qr_channels:
                self.url_label.configure(text="伺服器準備中，請稍候...")
                return
            if not self._qr_channels or not self._qr_selected_ip:
                self.qr_label.configure(image=self._qr_blank)   # 不能傳 None：tkinter 會略過，標籤仍指著舊圖
                self._qr_image = None
                self.url_label.configure(text="找不到區網位址：請讓筆電連上 Wi-Fi 或開行動熱點，手機才連得到")
                return
            dynamic_url = f"http://{self._qr_selected_ip}:5000?t={self._qr_last_token}"
            qr = pyqrcode.create(dynamic_url)
            buffer = io.BytesIO()
            qr.png(buffer, scale=6)
            buffer.seek(0)
            img = Image.open(buffer)
            new_image = ctk.CTkImage(light_image=img, size=img.size)   # 自帶一圈白邊，亮暗色都掃得到
            self.qr_label.configure(image=new_image)                    # 先換上新圖，舊圖才能放掉
            self._qr_image = new_image
            self.url_label.configure(text=f"目前網址: {dynamic_url}")

        def on_channel(label):
            """使用者在選單選了另一個通道：立刻用最新 Token 重畫。"""
            for item in self._qr_channels or []:
                if item["label"] == label:
                    self._qr_selected_ip = item["ip"]
                    break
            render()

        def apply(kind, value, channels):
            """套用背景執行緒回來的結果（在畫面執行緒執行）。"""
            if channels is not None:
                set_channels(channels)
            if kind == "ok":
                self._qr_fail_count = 0
                self._qr_last_token = value
                render()
                return
            self._qr_fail_count += 1
            if self._qr_channels == []:
                render()                                    # 沒有網路位址：提示比「伺服器連線失敗」更有用
            elif self._qr_fail_count <= _QR_STARTUP_GRACE_ATTEMPTS:
                self.url_label.configure(text="伺服器準備中，請稍候...")
            else:
                # 超過 30 秒仍連不上，只在主控台印出詳細錯誤，畫面不顯示例外內容
                print(f"[QR Update Error] 已連續 {self._qr_fail_count} 次連線失敗"
                      f"（約 {self._qr_fail_count * 5} 秒），可能是 web_server.py 沒有成功啟動：{value}")
                self.url_label.configure(text="⚠️ 伺服器連線失敗，請確認「啟動 QR 註冊伺服器」是否正常運作")

        def window_alive():
            try:
                return not self._reg_closed and bool(reg_window.winfo_exists())
            except tk.TclError:
                return False

        def tick():
            """每 0.2 秒在畫面執行緒執行：收背景結果、每 5 秒再啟動一次背景取 Token；視窗關了就停止。"""
            if not window_alive():
                return
            while True:
                try:
                    item = self._qr_results.get_nowait()
                except queue.Empty:
                    break
                self._qr_fetching = False
                try:
                    apply(*item)
                except tk.TclError:          # 視窗在這一刻被銷毀：不再更新
                    return
            now = time.monotonic()
            if not self._qr_fetching and now >= self._qr_next_fetch:
                self._qr_fetching = True
                self._qr_next_fetch = now + 5.0
                threading.Thread(target=worker, daemon=True).start()
            self.qr_timer_id = self.root.after(200, tick)

        # 離開這一頁（返回主選單、換外觀重畫、登出）時頁面被銷毀：設旗標並取消計時器，背景結果就不會再碰已不存在的元件
        def on_destroy(event):
            # CTkFrame 的事件綁在內部畫布上，事件的 widget 可能是畫布
            if event.widget is reg_window or event.widget is getattr(reg_window, "_canvas", None):
                self._reg_closed = True
                if self.qr_timer_id:
                    try:
                        self.root.after_cancel(self.qr_timer_id)
                    except tk.TclError:
                        pass

        reg_window.bind("<Destroy>", ui_theme.event_only(on_destroy), add="+")

        self.url_label.configure(text="伺服器準備中，請稍候...")
        self._qr_tick = tick                    # 測試用：模擬頁面銷毀後才輪到的那一次刷新
        tick()


def create_hidden_root():
    """
    建立整個行程唯一的主視窗並先隱藏。隱藏後要立刻 update 一次：customtkinter 在第一次 update／mainloop 前
    認為視窗還不存在，之後 resizable 觸發的標題列處理會把剛 deiconify 的視窗又藏回去，主畫面就永遠不出現。
    """
    ui_theme.apply_theme(ui_theme.load_mode())          # 沿用使用者上次選的淺色／深色／跟隨系統
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
