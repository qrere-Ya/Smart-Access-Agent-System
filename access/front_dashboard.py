"""
front_dashboard.py：前台儀表板——門口那台電腦「第二個螢幕」的畫面（CustomTkinter，1920×1080 設計）。

主要功能：
  - 主畫面（參考後台與一般 AI 平台首頁的做法）由上到下：功能模組小圖示卡（早報、剛剛打卡、系統狀態；每張帶一行即時預覽，
    整張可點）、「最近打卡」與「連線與辨識」兩張卡、資源狀態監控（CPU、記憶體、GPU 進度條與網路上下行）、日期與大時鐘
  - 點功能模組進入全版畫面、按「← 返回」回主畫面（沒人操作 90 秒自動回主畫面）：
      今日科技早報（晨報來自後台，前台離線也顯示最後同步到的；每頁 3 則、每 20 秒自動換頁）、
      剛剛打卡（姓名與早安／午安／晚安，10 分鐘後消失）、系統狀態
  - 早報每則右側有原文的 QR Code 讓人用手機掃（前台不上網、不開瀏覽器）；前台本機（shared.network_policy）
    或後台（同步時取得）開了全本地模式就不顯示 QR
  - 尾隨、經過未打卡、非活體時，任何畫面上方都會出現紅色警示橫幅（60 秒後消失）
  - 兩種主題可切換並記住：深色藍（預設）與半透明白（淺色底加視窗半透明）；沒有帳號，也不連網路
  - 資料來自前台本機（FrontStore、辨識程式寫的心跳、front_resources 的系統資源）；這支只讀、不寫（主題設定除外）
  - 右上角「啟動人臉辨識」按鈕：辨識程式不會跟著前台開，按了才啟動（隱藏黑色視窗）；執行中按鈕停用，前台不能停止
    （停止由後台或前台電腦的 Windows 操作）；儀表板關閉時辨識與連線代理一起收掉
  - 前台不提供任何設定或管理員功能（連線設定在安裝與網路設定時處理，全本地由後台控制）
  - 雙螢幕：自動放在第一個非主螢幕（外接顯示器），只有一個螢幕就放那一個；F11 或右上角按鈕切全螢幕（Esc 離開），
    下次開啟沿用上次的選擇（front_screens 記在 runtime/screen_prefs.json）
  人臉辨識畫面在主螢幕（筆電）由 terminal_app 顯示。

串接模組：
  - front_view：畫面內容整理（純邏輯）；front_resources：資源監控；front_store：本機資料
  - front_agent：辨識心跳；shared.ui_theme：色彩與元件
  - 啟動：python -m access.front_dashboard（啟動前台.bat 會一併啟動）
"""
import datetime
import os
import time
import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

from access import front_agent, front_launcher, front_resources, front_screens, front_view
from access.front_store import FrontStore, ScreenState
from PIL import ImageTk

from access import front_qr
from shared import network_policy, paths, ui_theme

REFRESH_MS = 1000
DETAIL_IDLE_SECONDS = 90.0            # 在模組全版畫面沒人操作這麼久，自動回主畫面
MODULE_KEYS = ("news", "greetings", "status")
THEME_LABELS = {"dark": "🌙 深色藍", "frosted": "☀ 半透明白"}
FROSTED_ALPHA = 0.97                  # 半透明白：只讓桌面微弱透出，不影響閱讀
DESIGN_WIDTH, DESIGN_HEIGHT = 1920, 1080
TONE_COLOR = {"success": "success", "warning": "warning", "danger": "danger", "info": "info"}
QR_SIZE = 150                         # 新聞卡片右側 QR Code 的邊長（未縮放）
BIG_STYLE, MINI_STYLE = "Smart.Treeview", "Smart.Mini.Treeview"
TABLE_FONT_BIG, TABLE_FONT_MINI = 19, 16     # 表格字級（點）：模組全版畫面的表格大，主畫面的小表格小一點
GREET_COLUMNS = (("time", "時間", 90), ("name", "姓名", 260), ("greeting", "問候", 120))
STATUS_COLUMNS = (("item", "項目", 180), ("state", "狀態", 360))
FONT_SCALE = 0.92                     # 字級基準：1920×1080 下中等大小（不要太大也不要太小）
MIN_SCALE, MAX_SCALE = 0.5, 1.6
TASKBAR_ALLOWANCE = 80                # 非全螢幕時預留給工作列與標題列的高度


def _font(size, bold=False):
    return ui_theme.font(max(9, round(size * FONT_SCALE)), bold=bold)


def _mono(size):
    return ui_theme.mono_font(max(9, round(size * FONT_SCALE)))


def window_scale(width, height, dpi=1.0):
    """視窗大小 → 縮放倍率：以 1920×1080 為 1.0，取較小的邊、扣掉系統 DPI 縮放，限制範圍、取 0.05 的倍數。"""
    raw = min(width / DESIGN_WIDTH, height / DESIGN_HEIGHT) / (dpi or 1.0)
    return round(max(MIN_SCALE, min(MAX_SCALE, raw)) * 20) / 20


class FrontDashboard:
    def __init__(self, root, store, now=datetime.datetime.now, clock=time.time, read_heartbeat=None,
                 read_resources=None, recognition=None, monitors=None):
        self.recognition = recognition                       # front_launcher.ManagedProcess；None 就不顯示辨識開關
        self.root, self.store, self._now, self._clock = root, store, now, clock
        self.screen = ScreenState(store)
        self._read_heartbeat = read_heartbeat or front_agent.read_terminal_heartbeat
        self._read_resources = read_resources or front_resources.ResourceMonitor().read
        self._signatures = {}
        self._trees = []
        self._grid_opts = {}
        self.current = None                                  # 目前開著的模組；None 是主畫面
        self._touched = clock()
        self._page_text = ""
        self._titles = {}
        self.theme = self.screen.theme()
        if self.theme not in THEME_LABELS:
            self.theme = "dark"
        root.title("智慧門禁 前台")
        self.monitor = front_screens.pick_monitor(front_screens.list_monitors() if monitors is None else monitors,
                                                  "dashboard")      # 有第二個螢幕就放在第二個螢幕
        if self.monitor is not None:
            x, y, width, height = front_screens.windowed_box(self.monitor)
        else:                                                       # 取不到螢幕資訊：用目前螢幕，留出工作列
            x, y = 0, 0
            width = min(DESIGN_WIDTH, root.winfo_screenwidth())
            height = min(DESIGN_HEIGHT, root.winfo_screenheight() - TASKBAR_ALLOWANCE)
        self._window_geometry = f"{width}x{height}+{x}+{y}"
        root.geometry(self._window_geometry)
        self.fullscreen = False
        root.minsize(800, 450)
        self._scale = 1.0
        self._resize_job = None
        self._build()
        self._apply_theme()
        self.refresh()
        root.bind("<Configure>", self._on_configure, add="+")
        root.bind("<F11>", lambda _e: self.toggle_fullscreen(), add="+")
        root.bind("<Escape>", lambda _e: self.set_fullscreen(False), add="+")
        if front_screens.load_pref("dashboard_fullscreen"):         # 上次是全螢幕，這次沿用
            root.after(300, lambda: self.set_fullscreen(True, remember=False))

    # --- 全螢幕 ---
    def set_fullscreen(self, on, remember=True):
        """全螢幕開關：先確認視窗在目標螢幕上再全螢幕，離開時還原成一般視窗的位置與大小。"""
        if on == self.fullscreen:
            return
        if on:
            self._window_geometry = self.root.geometry()
            if self.monitor is not None:                            # 全螢幕之前，視窗要先在目標螢幕上
                self.root.geometry(f"+{self.monitor.x}+{self.monitor.y}")
                self.root.update_idletasks()
        if on:        # 視窗縮小過時 CustomTkinter 會把最大尺寸也縮到現在的大小，全螢幕會被拒絕：先放寬最大尺寸
            tk.Wm.wm_maxsize(self.root, self.root.winfo_screenwidth() * 2, self.root.winfo_screenheight() * 2)
        self.fullscreen = on
        self.root.attributes("-fullscreen", on)
        if not on:
            self.root.geometry(self._window_geometry)
        self.fullscreen_button.configure(text="⛶ 離開全螢幕" if on else "⛶ 全螢幕 (F11)")
        if remember:
            front_screens.save_pref("dashboard_fullscreen", on)

    def toggle_fullscreen(self):
        self.set_fullscreen(not self.fullscreen)

    # --- 建立畫面 ---
    def _build(self):
        """標題列、警示橫幅、舞台（主畫面或模組全版畫面）、底部頁尾。"""
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(2, weight=1)

        top = ctk.CTkFrame(root, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=48, pady=(18, 6))
        top.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(top, text="🔐 智慧門禁 前台", font=_font(34, bold=True),
                     text_color=ui_theme.pair("primary"), anchor="w").grid(row=0, column=0, sticky="w")
        self.link_badge = ui_theme.status_badge(top, "後台連線", "neutral")
        self.link_badge.configure(font=_font(18, bold=True))
        self.link_badge.grid(row=0, column=1, sticky="w", padx=24, ipadx=8, ipady=3)
        self.theme_button = ui_theme.secondary_button(top, "", self.toggle_theme, width=170, height=40)
        self.theme_button.configure(font=_font(17))
        self.theme_button.grid(row=0, column=3, sticky="e", padx=(0, 14))
        self.fullscreen_button = ui_theme.secondary_button(top, "⛶ 全螢幕 (F11)", self.toggle_fullscreen,
                                                           width=170, height=40)
        self.fullscreen_button.configure(font=_font(17))
        self.fullscreen_button.grid(row=0, column=4, sticky="e")
        self.face_button = ui_theme.primary_button(top, "", self.start_recognition, width=220, height=40)
        self.face_button.configure(font=_font(17, bold=True))
        if self.recognition is not None:
            self.face_button.grid(row=0, column=2, sticky="e", padx=(0, 14))

        self.alert_frame = ctk.CTkFrame(root, fg_color=ui_theme.pair("danger"), corner_radius=ui_theme.RADIUS_CARD)
        self.alert_frame.grid(row=1, column=0, sticky="ew", padx=48, pady=(4, 6))
        self.alert_title = ctk.CTkLabel(self.alert_frame, text="", font=_font(34, bold=True),
                                        text_color=ui_theme.pair("on_danger"))
        self.alert_title.pack(side="left", padx=30, pady=8)
        self.alert_detail = ctk.CTkLabel(self.alert_frame, text="", font=_font(24),
                                         text_color=ui_theme.pair("on_danger"))
        self.alert_detail.pack(side="left", padx=(0, 30))
        self._hide(self.alert_frame)

        stage = ctk.CTkFrame(root, fg_color="transparent")
        stage.grid(row=2, column=0, sticky="nsew", padx=36, pady=(0, 2))
        stage.grid_columnconfigure(0, weight=1)
        stage.grid_rowconfigure(0, weight=1)
        self.home = ctk.CTkFrame(stage, fg_color="transparent")
        self.home.grid(row=0, column=0, sticky="nsew")
        self.detail = ctk.CTkFrame(stage, fg_color="transparent")
        self.detail.grid(row=0, column=0, sticky="nsew")
        self._hide(self.detail)
        self._build_home()
        self._build_detail()

        self.footer_label = ctk.CTkLabel(root, text="", font=_font(16), text_color=ui_theme.pair("text_muted"),
                                         anchor="center")
        self.footer_label.grid(row=3, column=0, sticky="ew", padx=48, pady=(2, 10))
        root.bind_all("<Button-1>", self._note_touch, add="+")

    def _build_home(self):
        """主畫面由上到下：功能模組（小圖示卡）、最近打卡與連線辨識兩張卡、資源狀態監控、日期與大時鐘。"""
        home = self.home
        home.grid_columnconfigure(0, weight=1)
        home.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(home, text="🗂  功能模組", font=_font(26, bold=True), text_color=ui_theme.pair("text"),
                     anchor="w").grid(row=0, column=0, sticky="w", padx=12, pady=(6, 2))
        ctk.CTkFrame(home, height=2, fg_color=ui_theme.pair("primary")).grid(row=0, column=0, sticky="ew", padx=12,
                                                                              pady=(46, 0))
        wall = ctk.CTkFrame(home, fg_color="transparent")
        wall.grid(row=1, column=0, sticky="w", padx=4, pady=(14, 4))
        self.tiles = {key: self._make_tile(wall, key, column) for column, key in enumerate(MODULE_KEYS)}

        mid = ctk.CTkFrame(home, fg_color="transparent")
        mid.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        mid.grid_columnconfigure((0, 1), weight=1, uniform="mid")
        mid.grid_rowconfigure(0, weight=1)
        mini_greet_card = self._mini_card(mid, 0, "👋  最近打卡")
        self.mini_greet = self._table(mini_greet_card, GREET_COLUMNS, height=3, big=False)
        self.mini_status = self._table(self._mini_card(mid, 1, "📡  連線與辨識"), STATUS_COLUMNS, height=4, big=False)

        res = ui_theme.card(home)
        res.grid(row=3, column=0, sticky="ew", padx=12, pady=(8, 4))
        ctk.CTkLabel(res, text="🖥️  資源狀態監控", font=_font(22, bold=True), text_color=ui_theme.pair("text"),
                     anchor="w").pack(anchor="w", padx=24, pady=(10, 4))
        grid = ctk.CTkFrame(res, fg_color="transparent")
        grid.pack(fill="x", padx=18, pady=(0, 10))
        grid.grid_columnconfigure((0, 1), weight=1, uniform="res")
        self.meters = {}
        for index, (key, label) in enumerate((("cpu", "CPU"), ("ram", "記憶體 RAM"), ("gpu", "GPU"))):
            self.meters[key] = self._meter(grid, label, index // 2, index % 2)
        net = ctk.CTkFrame(grid, fg_color=ui_theme.pair("bg"), corner_radius=ui_theme.RADIUS_CARD)
        net.grid(row=1, column=1, sticky="nsew", padx=6, pady=5)
        self.net_down = ctk.CTkLabel(net, text="↓ —", font=_font(22), text_color=ui_theme.pair("text"))
        self.net_down.pack(side="left", padx=24, pady=16)
        self.net_up = ctk.CTkLabel(net, text="↑ —", font=_font(22), text_color=ui_theme.pair("text"))
        self.net_up.pack(side="right", padx=24)

        clock = ctk.CTkFrame(home, fg_color="transparent")
        clock.grid(row=4, column=0, pady=(4, 2))
        self.date_label = ctk.CTkLabel(clock, text="", font=_font(26), text_color=ui_theme.pair("text_muted"))
        self.date_label.pack(side="left", padx=(0, 28))
        self.time_label = ctk.CTkLabel(clock, text="", font=_font(64, bold=True),
                                       text_color=ui_theme.pair("text"))
        self.time_label.pack(side="left")

    def _mini_card(self, master, column, title):
        card = ui_theme.card(master)
        card.grid(row=0, column=column, sticky="nsew", padx=12, pady=6)
        ctk.CTkLabel(card, text=title, font=_font(22, bold=True), text_color=ui_theme.pair("text"),
                     anchor="w").pack(anchor="w", padx=24, pady=(14, 2))
        ctk.CTkFrame(card, height=1, fg_color=ui_theme.pair("border")).pack(fill="x", padx=24)
        return card

    def _meter(self, master, label, row, column):
        """一格資源：名稱、百分比、進度條。"""
        box = ctk.CTkFrame(master, fg_color=ui_theme.pair("bg"), corner_radius=ui_theme.RADIUS_CARD)
        box.grid(row=row, column=column, sticky="nsew", padx=6, pady=5)
        head = ctk.CTkFrame(box, fg_color="transparent")
        head.pack(fill="x", padx=22, pady=(8, 2))
        ctk.CTkLabel(head, text=label, font=_font(18), text_color=ui_theme.pair("text_muted")).pack(side="left")
        value = ctk.CTkLabel(head, text="—", font=_font(20, bold=True), text_color=ui_theme.pair("text"))
        value.pack(side="right")
        bar = ctk.CTkProgressBar(box, height=10, progress_color=ui_theme.pair("success"),
                                 fg_color=ui_theme.pair("border"))
        bar.set(0)
        bar.pack(fill="x", padx=22, pady=(0, 12))
        return {"value": value, "bar": bar}

    def _make_tile(self, master, key, column):
        """小圖示卡：圓形色塊圖示、名稱、一行即時預覽；整張可點，滑鼠移入時亮起並換成強調色邊框。"""
        card = ui_theme.card(master, width=270, height=216)
        card.grid(row=0, column=column, padx=8, pady=6)
        card.pack_propagate(False)
        disc = ctk.CTkFrame(card, width=72, height=72, corner_radius=36, fg_color=ui_theme.pair("info"))
        disc.pack(pady=(18, 8))
        disc.pack_propagate(False)
        icon = ctk.CTkLabel(disc, text="", font=("Segoe UI Emoji", 26), text_color=ui_theme.pair("on_status"))
        icon.pack(expand=True)
        title = ctk.CTkLabel(card, text="", font=_font(24, bold=True), text_color=ui_theme.pair("text"))
        title.pack()
        summary = ctk.CTkLabel(card, text="", font=_font(18), text_color=ui_theme.pair("text_muted"),
                               justify="center", wraplength=240)
        summary.pack(pady=(6, 10))
        parts = {"card": card, "disc": disc, "icon": icon, "title": title, "summary": summary, "accent": "info"}

        def on_enter(_event=None):
            card.configure(fg_color=ui_theme.pair("secondary"), border_color=ui_theme.pair(parts["accent"]))

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
            stack.extend(w for w in widget.winfo_children() if isinstance(w, ctk.CTkBaseClass))
            widget.bind("<Button-1>", lambda _e, k=key: self.open_module(k), add="+")
            widget.bind("<Enter>", on_enter, add="+")
            widget.bind("<Leave>", on_leave, add="+")
        return parts

    def _build_detail(self):
        """模組的全版畫面：上方返回鈕與標題，下方是該模組的內容（三個模組疊在同一格，一次顯示一個）。"""
        self.detail.grid_columnconfigure(0, weight=1)
        self.detail.grid_rowconfigure(1, weight=1)
        bar = ctk.CTkFrame(self.detail, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        self.back_button = ui_theme.secondary_button(bar, "← 返回", self.go_home, width=150, height=52)
        self.back_button.configure(font=_font(24, bold=True))
        self.back_button.pack(side="left")
        self.detail_title = ctk.CTkLabel(bar, text="", font=_font(34, bold=True),
                                         text_color=ui_theme.pair("text"))
        self.detail_title.pack(side="left", padx=24)
        self.news_page = ctk.CTkLabel(bar, text="", font=_font(22), text_color=ui_theme.pair("text_muted"))
        self.news_page.pack(side="right", padx=12)
        body = ctk.CTkFrame(self.detail, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self.panels = {key: ui_theme.card(body) for key in MODULE_KEYS}
        for panel in self.panels.values():
            panel.grid(row=0, column=0, sticky="nsew", padx=12, pady=(6, 12))
        self.news_body = ctk.CTkFrame(self.panels["news"], fg_color="transparent")
        self.news_body.pack(fill="both", expand=True, padx=28, pady=16)
        self._news_labels = []
        self._qr_cache = {}
        self._qr_widgets = []
        self._last_view = None
        self.panels["news"].bind("<Configure>", self._wrap_news, add="+")
        self.greet_table = self._table(self.panels["greetings"], GREET_COLUMNS, height=4)
        self.status_table = self._table(self.panels["status"], STATUS_COLUMNS, height=4)
        for panel in self.panels.values():
            self._hide(panel)

    def _hide(self, widget):
        """藏起一個 grid 元件。不用 grid_remove：CustomTkinter 縮放時會照記住的 grid 呼叫把它重新放回來，
        用 grid_forget（它會清掉那筆記錄）並自己記下位置，要顯示時再放回去。"""
        if widget.winfo_manager() == "grid":
            self._grid_opts[widget] = {k: v for k, v in widget.grid_info().items() if k != "in"}
            widget.grid_forget()

    def _show(self, widget):
        if widget.winfo_manager() != "grid":
            widget.grid(**self._grid_opts.get(widget, {}))

    def _table(self, master, columns, height, big=True):
        """表格（ttk.Treeview，套主題色與斑馬紋）；欄寬可隨視窗伸縮。"""
        box, tree = ui_theme.table(master, columns, selectmode="none", height=height)
        name = BIG_STYLE if big else MINI_STYLE
        box.pack_propagate(False)       # 表格自己的欄寬總和不能反過來撐大外框，否則視窗縮小時內容會被擠出去
        box.configure(width=10, height=height * 34)
        for key, _title, width, *_ in columns:
            tree.column(key, width=width // 4, minwidth=40, stretch=True)   # 要求寬度小、靠伸縮撐滿
        box.pack(fill="both", expand=True, padx=18, pady=(6, 12))
        total = sum(c[2] for c in columns)

        def spread(event):
            """欄寬依比例撐滿表格（Tk 自己的伸縮不按比例，欄位會被擠得很窄）。"""
            if not tree.winfo_exists():
                return
            for key, _title, width, *_ in columns:
                tree.column(key, width=max(40, int(event.width * width / total) - 2))

        tree.bind("<Configure>", spread, add="+")
        self._trees.append((tree, name))
        return tree

    def _style_tables(self):
        """表格的色彩、字級與列高跟著主題與縮放倍率走（ttk 不吃 CustomTkinter 的縮放，要自己設）。"""
        style = ttk.Style(self.root)
        for tree, name in self._trees:
            ui_theme.style_treeview(tree, style_name=name)
            for tone in ("success", "warning", "danger"):
                tree.tag_configure(tone, foreground=ui_theme.color(tone))
        for name, base in ((BIG_STYLE, TABLE_FONT_BIG), (MINI_STYLE, TABLE_FONT_MINI)):
            size = max(10, round(base * self._scale))
            style.configure(name, font=(ui_theme.FONT_FAMILY, size), rowheight=round(size * 2.2))
            style.configure(f"{name}.Heading", font=(ui_theme.FONT_FAMILY, size, "bold"))

    @staticmethod
    def _fill(tree, rows, tones=None):
        """清空後插入列（斑馬紋；有色調的列用該色調的字色）。"""
        tree.delete(*tree.get_children())
        for index, values in enumerate(rows):
            tags = ui_theme.zebra_tags(index) + ((tones[index],) if tones and tones[index] in TONE_COLOR else ())
            tree.insert("", "end", values=values, tags=tags)

    # --- 模組切換 ---
    def open_module(self, key):
        """進入某個模組的全版畫面。"""
        if key not in self.panels:
            return
        self.current = key
        self._touched = self._clock()
        for name, panel in self.panels.items():
            if name == key:
                self._show(panel)
            else:
                self._hide(panel)
        self.news_page.configure(text=self._page_text if key == "news" else "")
        self.detail_title.configure(text=self._titles.get(key, ""))
        self._hide(self.home)
        self._show(self.detail)

    def go_home(self):
        """回到主畫面。"""
        self.current = None
        self._hide(self.detail)
        self._show(self.home)

    def _on_configure(self, event):
        """視窗大小改變：稍等一下（拖曳時不要每格都重排）再依新大小縮放整個畫面。"""
        if event.widget is not self.root:
            return
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(250, self._rescale)

    def _rescale(self):
        self._resize_job = None
        if not self.root.winfo_exists() or not self.home.winfo_exists():   # 畫面已被拆掉就不要再動全域縮放
            return
        try:
            dpi = ctk.ScalingTracker.get_window_scaling(self.root)
        except Exception:  # noqa: BLE001
            dpi = 1.0
        scale = window_scale(self.root.winfo_width(), self.root.winfo_height(), dpi)
        if scale != self._scale:
            self._scale = scale
            ctk.set_widget_scaling(scale)
            self._style_tables()
            self._signatures.pop("news", None)                   # QR 圖片要用新倍率重做
            self._qr_cache.clear()
            if self._last_view is not None:
                self._render_news(self._last_view["news"])

    def _note_touch(self, _event=None):
        self._touched = self._clock()

    # --- 主題 ---
    def _apply_theme(self):
        frosted = self.theme == "frosted"
        ctk.set_appearance_mode("light" if frosted else "dark")
        self.root.configure(fg_color=ui_theme.pair("bg"))
        try:
            self.root.attributes("-alpha", FROSTED_ALPHA if frosted else 1.0)
        except Exception:  # noqa: BLE001, S110  # nosec B110  部分系統不支援視窗透明度，主題仍可用
            pass
        self.theme_button.configure(text=THEME_LABELS["dark" if frosted else "frosted"])
        self._style_tables()
        self._signatures.clear()                                     # 配色變了，內容區重畫

    def start_recognition(self):
        """啟動人臉辨識（會在筆電螢幕開攝影機畫面）；前台只能啟動、不能停止，要停止由後台或前台電腦的 Windows 操作。"""
        if self.recognition is None or self.recognition.running():
            return
        ok, message = self.recognition.start()
        if not ok:
            print(f"[front_dashboard][WARN] 人臉辨識{message}")
        self._render_recognition()

    def _render_recognition(self):
        """依辨識程式是否還活著更新按鈕：執行中停用（不能停止）；辨識程式自己結束（按 q）就回到可啟動。"""
        if self.recognition is None:
            return
        on = self.recognition.running()
        if self._changed("recognition_switch", on):
            if on:
                self.face_button.configure(text="✔ 人臉辨識執行中", state="disabled", fg_color="transparent",
                                           border_width=1, border_color=ui_theme.pair("success"),
                                           text_color_disabled=ui_theme.pair("success"))
            else:
                self.face_button.configure(text="▶ 啟動人臉辨識", state="normal", fg_color=ui_theme.pair("primary"),
                                           border_width=0, hover_color=ui_theme.pair("primary_hover"),
                                           text_color=ui_theme.pair("on_primary"))

    def toggle_theme(self):
        self.theme = "dark" if self.theme == "frosted" else "frosted"
        try:
            self.screen.set_theme(self.theme)
        except Exception:  # noqa: BLE001, S110  # nosec B110  記不住主題不影響使用
            pass
        self._apply_theme()
        self.refresh()

    # --- 更新 ---
    def local_only(self):
        """全本地模式：前台本機的設定（環境變數或設定檔）或後台同步來的開關，任一個開就算；後台值壞掉當作關。"""
        return network_policy.local_only() or self.store.get_meta(front_agent.BACK_LOCAL_ONLY_KEY) is True

    def refresh(self):
        """讀一次本機資料並更新畫面；內容沒變的區塊不重畫。"""
        view = front_view.snapshot(self.store, self.screen, self._read_heartbeat, self._now(), self._clock(),
                                   local_only=self.local_only())
        self._last_view = view
        self.time_label.configure(text=view["time"])
        self.date_label.configure(text=view["date"])
        tone, text = view["link"]
        ui_theme.set_badge(self.link_badge, text, tone)
        self._render_recognition()
        self._render_alert(view["alert"])
        self._render_tiles(view["tiles"])
        self._render_news(view["news"])
        self._render_greetings(view["greetings"])
        self._render_status(view["status"])
        self._render_resources()
        self._render_footer()
        if self.current is not None and self._clock() - self._touched > DETAIL_IDLE_SECONDS:
            self.go_home()

    def _changed(self, key, value):
        if self._signatures.get(key) == value:
            return False
        self._signatures[key] = value
        return True

    @staticmethod
    def _clear(frame):
        for child in frame.winfo_children():
            child.destroy()

    def _render_alert(self, alert):
        if alert is None:
            if self._changed("alert", None):
                self._hide(self.alert_frame)
            return
        if self._changed("alert", (alert["title"], alert["detail"])):
            self.alert_title.configure(text="⚠ " + alert["title"])
            self.alert_detail.configure(text=alert["detail"])
            self._show(self.alert_frame)

    def _render_tiles(self, tiles):
        signature = tuple((t["key"], t["accent"], t["summary"]) for t in tiles)
        if not self._changed("tiles", signature):
            return
        for tile in tiles:
            parts = self.tiles[tile["key"]]
            parts["accent"] = tile["accent"]
            parts["icon"].configure(text=tile["icon"])
            parts["disc"].configure(fg_color=ui_theme.pair(TONE_COLOR.get(tile["accent"], "info")))
            parts["title"].configure(text=tile["title"])
            parts["summary"].configure(text=tile["summary"])
            parts["card"].configure(fg_color=ui_theme.pair("card"), border_color=ui_theme.pair("border"))
            self._titles[tile["key"]] = tile["title"]
        if self.current is not None:
            self.detail_title.configure(text=self._titles.get(self.current, ""))

    def _render_news(self, news):
        signature = (news["state"], news["heading"], news["page"],
                     tuple((c["title"], c["link"]) for c in news["cards"]), news["message"])
        if not self._changed("news", signature):
            return
        self._titles["news"] = news["heading"]
        self._page_text = f"{news['page']} / {news['pages']}" if news["pages"] > 1 else ""
        if self.current == "news":
            self.detail_title.configure(text=news["heading"])
            self.news_page.configure(text=self._page_text)
        for child in self.news_body.winfo_children():
            child.destroy()
        self._news_labels = []
        self._qr_widgets = []
        if news["state"] != "ok":
            self._news_text(self.news_body, news["message"], 22, muted=True).pack(anchor="w", pady=30)
        else:
            for index, card in enumerate(news["cards"], start=1):
                self._news_card(index, card)
        self._wrap_news()

    def _news_text(self, master, text, size, bold=False, muted=False):
        """會自動換行的一段文字（換行寬度由 _wrap_news 依視窗寬度設定）。"""
        label = ctk.CTkLabel(master, text=text, font=_font(size, bold=bold), justify="left", anchor="w",
                             text_color=ui_theme.pair("text_muted" if muted else "text"))
        self._news_labels.append(label)
        return label

    def _news_card(self, index, card):
        """一則新聞，像 AI 回覆一樣由上到下讀：類別與標題、發生什麼事、為什麼重要、可以怎麼用、來源。"""
        outer = ctk.CTkFrame(self.news_body, fg_color=ui_theme.pair("bg"), corner_radius=ui_theme.RADIUS_CARD)
        outer.pack(fill="x", pady=7)
        if card.get("link"):
            self._qr_box(outer, card["link"])
        box = ctk.CTkFrame(outer, fg_color="transparent")
        box.pack(side="left", fill="both", expand=True)
        head = ctk.CTkFrame(box, fg_color="transparent")
        head.pack(fill="x", padx=22, pady=(12, 0))
        ctk.CTkLabel(head, text=f" {card['category']} ", font=_font(16, bold=True), corner_radius=10,
                     fg_color=ui_theme.pair("primary"), text_color=ui_theme.pair("on_primary")).pack(side="left")
        ctk.CTkLabel(head, text=card["source"], font=_font(16),
                     text_color=ui_theme.pair("text_muted")).pack(side="right")
        self._news_text(box, f"{index}. {card['title']}", 24, bold=True).pack(fill="x", padx=22, pady=(6, 2))
        self._news_text(box, card["what"], 19).pack(fill="x", padx=22)
        self._news_text(box, f"為什麼重要：{card['why']}", 17, muted=True).pack(fill="x", padx=22, pady=(4, 0))
        self._news_text(box, f"可以怎麼用：{card['how']}", 17, muted=True).pack(fill="x", padx=22, pady=(0, 12))

    def _qr_box(self, master, url):
        """卡片右側的 QR Code：手機掃了就能開原文（前台自己不上網）。圖片做不出來就不放。"""
        size = max(60, int(QR_SIZE * self._scale))            # 純 tk 的圖片不吃 CustomTkinter 縮放，自己依倍率縮
        photo = self._qr_cache.get((url, size))
        if photo is None:
            img = front_qr.image(url, size)
            if img is None:
                return
            photo = self._qr_cache[(url, size)] = ImageTk.PhotoImage(img)
        side = ctk.CTkFrame(master, fg_color="transparent")
        side.pack(side="right", padx=(0, 18), pady=12)
        label = tk.Label(side, image=photo, bd=0, highlightthickness=0, bg=ui_theme.color("bg"))   # 純 tk，只吃單一顏色
        label.pack()
        ctk.CTkLabel(side, text="手機掃描看原文", font=_font(14),
                     text_color=ui_theme.pair("text_muted")).pack(pady=(4, 0))
        self._qr_widgets.append(label)

    def _wrap_news(self, _event=None):
        """文字的換行寬度跟著早報區的寬度走，放大縮小都不會超出。"""
        panel = self.panels["news"]
        width = panel.winfo_width()
        if width <= 1:
            return
        for label in self._news_labels:
            try:
                reserve = 130 + (QR_SIZE + 40 if self._qr_widgets else 0)
                label.configure(wraplength=max(200, int(label._reverse_widget_scaling(width - reserve))))
            except Exception:  # noqa: BLE001, S110  # nosec B110  元件剛被銷毀時略過
                pass

    def _render_greetings(self, rows):
        if not self._changed("greetings", tuple(rows)):
            return
        shown = list(rows) or [("", "辨識成功的人會顯示在這裡", "")]
        self._fill(self.greet_table, shown)
        self._fill(self.mini_greet, shown[:4])

    def _render_status(self, rows):
        if not self._changed("status", tuple(rows)):
            return
        table_rows = [(label, text) for label, text, _tone in rows]
        tones = [tone for _label, _text, tone in rows]
        self._fill(self.status_table, table_rows, tones)
        self._fill(self.mini_status, table_rows, tones)

    def _render_resources(self):
        """資源狀態監控：CPU、記憶體、GPU 進度條（超過 70% 黃、90% 紅）與網路上下行；讀不到的顯示「—」。"""
        try:
            data = self._read_resources()
        except Exception:  # noqa: BLE001
            data = {}
        for key, meter in self.meters.items():
            value = data.get(key)
            if value is None:
                meter["value"].configure(text="未偵測到" if key == "gpu" else "—")
                meter["bar"].set(0)
                continue
            meter["value"].configure(text=f"{value:.0f}%")
            meter["bar"].set(max(0.0, min(1.0, value / 100.0)))
            tone = front_view.load_tone(value)
            meter["bar"].configure(progress_color=ui_theme.pair(tone))
        self.net_down.configure(text=front_view.speed_text("↓", data.get("down_kbps")))
        self.net_up.configure(text=front_view.speed_text("↑", data.get("up_kbps")))

    def _render_footer(self):
        text = "智慧門禁 前台　點上方模組進入；在模組畫面沒人操作會自動回到這一頁"
        if self._changed("footer", text):
            self.footer_label.configure(text=text)

    def tick(self):
        """定時更新：任何一次更新出錯都不能讓畫面停止。"""
        if not self.root.winfo_exists():
            return
        try:
            self.refresh()
        except Exception as e:  # noqa: BLE001
            print(f"[front_dashboard][WARN] 更新失敗，下一輪再試：{type(e).__name__}")
        self.root.after(REFRESH_MS, self.tick)


def main():
    front_launcher.redirect_output_if_windowless("front_dashboard")      # pythonw 沒有黑色視窗，print 改寫記錄檔
    store = FrontStore(os.path.join(paths.RUNTIME_DIR, "front_agent.db"))
    agent = front_launcher.ManagedProcess("access.front_agent", "front_agent")
    recognition = front_launcher.ManagedProcess("access.terminal_app", "terminal_app")
    ok, message = agent.start()                                          # 連線代理跟著儀表板一起開，人臉辨識由開關啟動
    if not ok:
        print(f"[front_dashboard][WARN] 連線代理{message}")
    ui_theme.apply_theme("dark")
    root = ctk.CTk()
    app = FrontDashboard(root, store, recognition=recognition)
    root.after(REFRESH_MS, app.tick)
    try:
        root.mainloop()
    finally:
        recognition.stop()                                               # 關閉儀表板就一起收掉辨識與代理
        agent.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
