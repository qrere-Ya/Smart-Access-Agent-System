import ttkbootstrap as ttk
from ttkbootstrap.constants import *
import tkinter as tk
from tkinter import messagebox
import os
import datetime
import database_mgr as db  # 引入你的資料庫模組
import pyqrcode
import png
import random
import string
import io
from PIL import Image, ImageTk
from tkinter import PhotoImage

import service_manager
import console_panel
import dashboard_stats
import account_admin

# 【合併專案調整】改用 __file__ 相對路徑，不管從哪個資料夾執行都能正確定位到專案根目錄
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ANOMALY_DIR = os.path.join(_PROJECT_ROOT, "anomaly_logs")


class BackendManagerApp:
    """
    【2026-09-12，P1 架構重構】這個檔案原本身兼三個角色（畫視窗、實際啟動/監控背景
    子行程、查資料庫算統計數字）全部塞在同一個 1185 行的檔案裡。現在拆成 3 個各司其職
    的模組：

      - service_manager.py   啟動/停止/監控三組背景子行程（RAG、人臉辨識、QR 註冊），
                              完全不 import Tkinter，可以脫離這支程式單獨測試。
      - console_panel.py     內建終端機小框框元件（純畫面元件，不含業務邏輯）。
      - dashboard_stats.py   戰情儀表板要顯示的統計數字查詢。

    【2026-09-12，P2 新增】再加上 account_admin.py：帳號管理的資料庫操作（列出員工、
    停用/啟用、刪除），原本「📇 員工資料庫管理」那個唯讀視窗升級成「👤 員工帳號管理」，
    可以真的操作帳號了（見 open_account_admin()）。

    【2026-09-12，P3 卡片牆 + 風格更新 v1/v2】主選單從一排直立按鈕改成 2x2 的功能
    模組卡片（見 _build_main_menu / _create_module_card），v1 先做成淺色圓角卡片，
    使用者反饋還是不夠好看，v2 改成深色懸浮卡片（深色底＋陰影模擬立體感＋圓角方形
    色塊圖示＋頂部時鐘／狀態標籤／底部系統資源列），整體風格參考深色 SaaS 管理後台
    的組織方式（深色底、卡片式模組、自我監控意識），配色與畫法是自己抓/自己算的，
    不是照著任何截圖描。純視覺層改動，四個功能各自的行為（開哪個視窗、呼叫哪個
    函式）完全沒有變。

    這個檔案現在只保留「畫面」的工作：畫視窗、畫卡片/按鈕、把點擊轉發給上面幾個模組，
    再把回傳結果轉成畫面上看得懂的文字/顏色/按鈕狀態——不自己動手做真正的粗活（開行程、
    查 port、寫資料庫查詢）。
    """

    def __init__(self, root):
        self.root = root
        self.root.title("智慧門禁 - 戰情控制中心")
        # 【主選單版面】三個服務開關都已經搬到各自對應的視窗裡了（見下方 StringVar
        # 那段說明），現在主選單只剩：頂部標題列 + 四張功能模組卡片 + 底部系統狀態列。
        # 【2026-09-12，P3 風格更新】比第一版卡片牆多了頂部時鐘/日期跟底部系統資源列，
        # 需要多一點高度，實測抓 640x640 剛好放得下，同時維持允許使用者自行拉高/拉寬
        # 視窗（resizable True），就算某些電腦的中文字型渲染出來比這裡測試的更大，
        # 使用者也還是能自己把窗戶拉大看到全部內容，不會被鎖死看不到。
        self.root.geometry("640x640")
        self.root.minsize(580, 580)
        self.root.resizable(True, True)

        # 【三個開關搬到各自對應的視窗裡了，不再擺在主選單】狀態文字變數在這裡先建立好
        # （StringVar 本身不是畫面元件，建立一次就能一直用，不會因為視窗開開關關而消失）：
        #   - RAG 開關 → 放進「🤖 AI 代理與個人化設定」視窗（open_ai_agent_settings）
        #   - 人臉辨識開關 → 放進「📊 營運狀態與尾隨監控」視窗（open_dashboard）
        #   - 註冊伺服器開關 → 放進「📱 遠端 QR Code 註冊」視窗（open_qr_registration）
        # 對應的按鈕（self.rag_btn / self.face_btn / self.reg_btn）則是每次開啟該視窗時
        # 才建立，視窗關閉後按鈕本身會被銷毀，但背景服務不受影響、繼續在跑（服務本身的
        # 執行狀態現在存在 service_manager 模組裡，不會因為視窗關掉就跟著消失），下次
        # 重新打開視窗時，會呼叫 _update_*_status() 把畫面同步成真正的目前狀態。
        self.rag_status_var = ttk.StringVar(value="⚪ RAG 服務未啟動")
        self.face_status_var = ttk.StringVar(value="⚪ 人臉辨識未啟動")
        self.reg_status_var = ttk.StringVar(value="⚪ 註冊伺服器未啟動")

        # 初始化資料庫 (確保資料表存在)
        db.init_db()

        # 建立主畫面
        self._build_main_menu()

        # 關閉視窗時，詢問是否要一併停止還在跑的背景服務，避免留下孤兒行程
        self.root.protocol("WM_DELETE_WINDOW", self.on_app_close)

    def _build_main_menu(self):
        # 【2026-09-12，P3 風格更新 v2】你說淺色版還是土，改成深色版——類似你附的
        # 那張深色 SaaS 儀表板截圖（深藍/深灰底、懸浮圓角卡片、色塊圖示、陰影立體感）。
        # 這不是照著那張截圖描：那張圖是一個完整的專案管理後台（甜甜圈圖表、行事曆、
        # 團隊狀態列表），跟我們這裡「四個功能模組的啟動器」性質不一樣，這裡借用的是
        # 它的「視覺語言」——深色底、懸浮感圓角卡片、飽和色塊圖示、簡潔留白——套用在
        # 我們自己的排版跟內容上，配色數值、卡片比例、陰影畫法都是自己抓/自己算的。
        #
        # 所有顏色（除了下面 _create_module_card 手繪陰影/懸浮效果的相對明暗計算）
        # 都是從 ttkbootstrap 主題色盤動態抓出來的，不是寫死的色碼，換主題也不會壞掉、
        # 不會出現「卡片背景跟外框顏色對不起來」這種色塊接縫問題。
        self.style = ttk.Style()

        # ---- 頂部標題列：左邊品牌 + 狀態小標籤，右邊即時時鐘 ----
        header = ttk.Frame(self.root, padding=(25, 20, 25, 10))
        header.pack(fill=X)

        brand_col = ttk.Frame(header)
        brand_col.pack(side=LEFT)
        ttk.Label(brand_col, text="🔐 Smart Access Hub", font=("Helvetica", 20, "bold"), bootstyle=PRIMARY).pack(anchor=W)
        ttk.Label(brand_col, text="邊緣運算門禁後臺系統", font=("微軟正黑體", 10), bootstyle=SECONDARY).pack(anchor=W, pady=(2, 0))
        # 小狀態標籤，呼應深色後台常見的「系統運作中」提示，用 Canvas 畫一個膠囊形狀
        # （原理跟卡片的圓角矩形一樣，半徑設成貼齊高度就會是完全的膠囊角）。
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

        # ---- 卡片牆：2x2 的功能模組卡片 ----
        # 四個功能各自呼叫的函式（self.open_dashboard 等）完全沒有變，行為跟以前
        # 一模一樣，只是長相從「淡色調卡片」變成「深色懸浮卡片」。
        card_wall = ttk.Frame(self.root, padding=(15, 5, 15, 5))
        card_wall.pack(fill=BOTH, expand=True)
        card_wall.columnconfigure(0, weight=1)
        card_wall.columnconfigure(1, weight=1)
        card_wall.rowconfigure(0, weight=1)
        card_wall.rowconfigure(1, weight=1)

        # (圖示, 標題, 說明, 色系, 點擊要呼叫的函式)。AI 代理這張原本用 SECONDARY
        # （灰色），深色底下灰色圖示徽章存在感太弱，這次改用 PRIMARY（主題藍），
        # 四張卡片色系變成「綠/青/橘/藍」，彼此對比更清楚。
        modules = [
            ("📊", "營運狀態與尾隨監控", "今日打卡統計、安全警報、人臉辨識即時輸出", SUCCESS, self.open_dashboard),
            ("👤", "員工帳號管理", "列出、停用、啟用、刪除員工帳號與角色", INFO, self.open_account_admin),
            ("📱", "遠端 QR Code 註冊", "產生動態安全碼，讓新進員工手機掃碼註冊", WARNING, self.open_qr_registration),
            ("🤖", "AI 代理與個人化設定", "啟動 RAG 智慧問答服務（勞基法規＋員工資料庫）", PRIMARY, self.open_ai_agent_settings),
        ]
        for idx, (icon, title, desc, style, cmd) in enumerate(modules):
            row, col = divmod(idx, 2)
            self._create_module_card(card_wall, icon, title, desc, style, cmd, row, col)

        # ---- 底部系統狀態列：即時 CPU/RAM ----
        # dashboard_stats.get_system_resources() 其實 P1 就寫好了，只是那時候刻意沒接
        # 到畫面上（保證 P1 不改變視覺）。現在是視覺層改版，正好是接上它的時機。
        self._build_status_footer()

    def _create_module_card(self, master, icon, title, desc, style, cmd, row, col):
        """
        建立一張深色懸浮卡片，取代原本的淡色調卡片。畫法還是用 tkinter.Canvas 手繪
        圓角矩形（Tkinter 原生元件沒有圓角），但這次多畫一層「陰影」：卡片本體往右
        下偏移幾個像素、疊一塊更暗的圓角矩形在底下，模擬懸浮在頁面上的立體感（網頁
        的 box-shadow 在 Tkinter 沒有對應功能，用「多畫一層深色色塊」是最常見的克難
        作法）。卡片本身的底色/邊框顏色都是拿目前頁面底色（colors.bg）去算「亮一點」
        或「暗一點」的相對色調，不是寫死的深灰色碼——不管使用者之後把主題換成哪個
        ttkbootstrap 內建主題，算出來的卡片永遠會比頁面底色亮一階、陰影永遠比底色暗
        一階，不會出現顏色兜不起來的狀況。圖示徽章改成圓角「方形」（不是圓形）比較
        貼近你參考截圖裡那種色塊圖示的觀感。整張卡片（畫布本身）都能點擊。
        """
        colors = self.style.colors
        accent = colors.get(style)
        page_bg = colors.bg
        fg = colors.fg

        panel_fill = self._blend_hex(page_bg, "#ffffff", 0.07)   # 卡片本體：比底色亮一階
        hover_fill = self._blend_hex(page_bg, "#ffffff", 0.13)   # 滑鼠移上去：再亮一點
        shadow_fill = self._blend_hex(page_bg, "#000000", 0.35)  # 陰影：比底色暗一階
        border_idle = self._blend_hex(page_bg, "#ffffff", 0.16)
        desc_fg = self._blend_hex(fg, page_bg, 0.45)  # 說明文字：主文字色往底色混，變成次要文字的灰階

        canvas = tk.Canvas(master, width=262, height=136, highlightthickness=0, bd=0, bg=page_bg, cursor="hand2")
        canvas.grid(row=row, column=col, padx=10, pady=10, sticky="nsew")

        def draw(fill, border):
            canvas.delete("all")
            # 陰影層：卡片本體往右下偏移 4px 畫一塊暗色圓角矩形，露出邊緣模擬立體懸浮感。
            self._rounded_rect(canvas, 6, 8, 260, 134, radius=16, fill=shadow_fill, outline="")
            self._rounded_rect(canvas, 2, 2, 256, 128, radius=16, fill=fill, outline=border, width=1)
            # 圖示徽章：圓角方形色塊（不是圓形），貼近深色後台常見的色塊圖示畫法。
            self._rounded_rect(canvas, 16, 16, 54, 54, radius=10, fill=accent, outline="")
            canvas.create_text(35, 35, text=icon, font=("Segoe UI Emoji", 16))
            canvas.create_text(16, 72, text=title, anchor="w", font=("微軟正黑體", 12, "bold"), fill=fg)
            canvas.create_text(16, 96, text=desc, anchor="nw", width=224, font=("微軟正黑體", 8), fill=desc_fg)

        draw(panel_fill, border_idle)

        # 滑鼠移上去：卡片亮一階 + 邊框變成模組主題色，給一個跟該模組色系一致的提示。
        canvas.bind("<Button-1>", lambda e: cmd())
        canvas.bind("<Enter>", lambda e: draw(hover_fill, accent))
        canvas.bind("<Leave>", lambda e: draw(panel_fill, border_idle))

    def _build_status_pill(self, master, text, style):
        """ 畫一個小膠囊形狀的狀態標籤（圓角矩形，角半徑貼齊高度就會變成完全的膠囊
        角），底色是模組主題色跟頁面底色混出來的淡淡色調，比整塊塗滿主題色更耐看，
        也比較不會在深色底下顯得太搶眼。 """
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
        """ 在 Canvas 上畫一個圓角矩形：用平滑多邊形（smooth=True）近似圓角，這是
        Tkinter 沒有原生圓角元件時最常見的手動畫法。 """
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, **kwargs)

    @staticmethod
    def _blend_hex(hex_color, target_hex, ratio):
        """ 把 hex_color 往 target_hex 方向混出中間色（ratio 0~1，越大越接近
        target_hex）。Tkinter 的色塊不支援透明度，沒辦法像網頁那樣用「顏色疊在底色上
        15% 透明度」做出淡色調，這裡直接對 RGB 數值做內插算出等效的淡色調/加深色，
        效果一樣、只是算法不同——也因為是算出來的，換主題（換 colors.bg）結果會自動
        跟著調整，不用每個主題各寫一份色碼。 """
        hex_color = hex_color.lstrip("#")
        target_hex = target_hex.lstrip("#")
        r1, g1, b1 = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        r2, g2, b2 = int(target_hex[0:2], 16), int(target_hex[2:4], 16), int(target_hex[4:6], 16)
        r = round(r1 + (r2 - r1) * ratio)
        g = round(g1 + (g2 - g1) * ratio)
        b = round(b1 + (b2 - b1) * ratio)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _tick_clock(self):
        """ 頂部時鐘每秒更新一次。主視窗被關掉（使用者關閉整個後台程式）就不用再排程
        下一次了，避免對已銷毀的視窗呼叫 after() 出錯。 """
        if not self.root.winfo_exists():
            return
        now = datetime.datetime.now()
        self.clock_var.set(now.strftime("%H:%M:%S"))
        self.date_var.set(now.strftime("%Y/%m/%d"))
        self.root.after(1000, self._tick_clock)

    def _build_status_footer(self):
        ttk.Separator(self.root, bootstyle=SECONDARY).pack(fill=X, padx=25)
        footer = ttk.Frame(self.root, padding=(25, 8, 25, 14))
        footer.pack(fill=X)

        self.sys_status_var = ttk.StringVar(value="系統資源監控準備中…")
        ttk.Label(footer, textvariable=self.sys_status_var, font=("Consolas", 9), bootstyle=SECONDARY).pack(side=LEFT)
        self._tick_system_status()

    def _tick_system_status(self):
        """ 每 3 秒讀一次 CPU/RAM，資料來源是 dashboard_stats.get_system_resources()
        （P1 就寫好、當時故意沒接上畫面的那個函式）。讀不到（例如這台電腦沒裝 psutil）
        就顯示提示文字，不會讓程式當掉，也不會一直重試洗版。 """
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
        """
        【避免提示視窗躲起來讓人找不到】messagebox.showinfo() 這種提示視窗，如果沒有
        指定要顯示在「哪一個視窗上面」，預設會依附在最底層那個看不見的主視窗上，
        很容易被使用者正在看的子視窗（例如「AI 代理與個人化設定」）擋住、跑到後面
        去，讓人以為「根本沒有跳出提示視窗」，其實它一直都在，只是被擋住了。
        這裡回傳一個可以直接展開進 messagebox.showinfo(..., **kwargs) 的字典，
        指定「現在使用者正在看的那個子視窗」當作提示視窗的依附對象，讓提示視窗
        確實浮在最上面、使用者一定看得到；如果那個子視窗已經被關掉了，就不指定，
        改用預設行為（不會因此讓程式當掉）。
        """
        win = getattr(self, win_attr, None)
        try:
            if win is not None and win.winfo_exists():
                return {"parent": win}
        except Exception:
            pass
        return {}

    # ==========================================
    # 【開關一】RAG 服務：實際啟動/停止/狀態查詢都轉發給 service_manager，這裡只負責
    # 更新畫面。
    # ==========================================
    def toggle_rag_services(self):
        if service_manager.is_rag_busy():
            return
        if service_manager.rag_group_running():
            self._stop_rag_services()
        else:
            self._start_rag_services()

    def _start_rag_services(self):
        self.rag_status_var.set("🟡 RAG 服務啟動中…")
        if hasattr(self, "rag_btn") and self.rag_btn.winfo_exists():
            self.rag_btn.config(state="disabled")

        def on_output_line(prefix, line):
            # 這個 callback 會從 service_manager 內部的背景執行緒被呼叫，一定要用
            # self.root.after() 轉回主執行緒才能安全更新畫面元件。
            self.root.after(0, lambda: console_panel.append_line(
                getattr(self, "rag_console", None), f"{prefix}{line}"))

        def on_finished(started, warnings):
            self.root.after(0, lambda: self._finish_rag_start(started, warnings))

        service_manager.start_rag_services_async(on_output_line, on_finished)

    def _finish_rag_start(self, started, warnings):
        self._update_rag_status()
        # 【視窗可能已經被關掉】啟動 RAG 是背景執行緒在跑，使用者按下啟動後很可能已經
        # 把「AI 代理與個人化設定」這個視窗關掉了，等背景執行緒真的跑完、回頭要把
        # self.rag_btn 按鈕改回「normal」狀態時，這顆按鈕可能已經隨著視窗一起被銷毀，
        # 這裡先確認按鈕還存在才操作，避免直接讓整支程式當掉。
        if hasattr(self, "rag_btn") and self.rag_btn.winfo_exists():
            self.rag_btn.config(state="normal")
        msg = ""
        if started:
            msg += "已就緒：" + "、".join(started) + "\n"
        if warnings:
            msg += "\n⚠️ 需要留意：\n" + "\n\n".join(warnings)
        if msg:
            messagebox.showinfo("RAG 服務啟動結果", msg, **self._msgbox_parent("agent_win"))

    def _stop_rag_services(self):
        results = service_manager.stop_rag_services()
        self._update_rag_status()
        if any(results):
            messagebox.showinfo("提示", "已停止 RAG 服務（Ollama / LiteLLM / 助理網頁）。", **self._msgbox_parent("agent_win"))
        else:
            messagebox.showinfo("提示", "RAG 服務目前沒有在跑。", **self._msgbox_parent("agent_win"))

    def _update_rag_status(self):
        running = service_manager.rag_group_running()
        if running:
            self.rag_status_var.set("🟢 RAG 服務執行中")
            btn_text = "🛑 停止 RAG 服務"
        else:
            self.rag_status_var.set("⚪ RAG 服務未啟動")
            btn_text = "🧠 啟動 RAG（Ollama + LiteLLM + 助理網頁）"
        # 【視窗可能已經關閉】RAG 開關現在放在「AI 代理與個人化設定」這個可以被使用者
        # 關掉的子視窗裡，狀態文字（StringVar）本身不受影響會一直更新，但按鈕元件
        # self.rag_btn 只在視窗開著的時候才存在，視窗關掉後就被銷毀了，這裡先確認
        # 按鈕還存在才更新它的文字，不存在就跳過（下次重新打開視窗會自動同步正確狀態）。
        if hasattr(self, "rag_btn") and self.rag_btn.winfo_exists():
            self.rag_btn.config(text=btn_text)

    # ==========================================
    # 【開關二】人臉辨識終端機：terminal_app.py（獨立於 RAG、獨立於註冊）
    # ==========================================
    def toggle_face_service(self):
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
        if service_manager.face_service_running():
            self.face_status_var.set("🟢 人臉辨識執行中")
            btn_text = "🛑 停止人臉辨識終端機"
        else:
            self.face_status_var.set("⚪ 人臉辨識未啟動")
            btn_text = "📷 啟動人臉辨識終端機"
        # 【視窗可能已經關閉】按鈕現在放在「營運狀態與尾隨監控」這個可以被關掉的子視窗
        # 裡，先確認按鈕還存在才更新，避免視窗關掉後還去操作已銷毀的元件而讓程式當掉。
        if hasattr(self, "face_btn") and self.face_btn.winfo_exists():
            self.face_btn.config(text=btn_text)

    # ==========================================
    # 【開關三】QR 註冊伺服器：web_server.py（獨立於 RAG、獨立於人臉辨識）
    # ==========================================
    def toggle_registration_service(self):
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
        running = service_manager.reg_server_running()
        if running:
            self.reg_status_var.set("🟢 註冊伺服器執行中")
            btn_text = "🛑 停止 QR 註冊伺服器"
        else:
            self.reg_status_var.set("⚪ 註冊伺服器未啟動")
            btn_text = "📱 啟動 QR 註冊伺服器"
        # 【視窗可能已經關閉】按鈕現在放在「遠端 QR Code 註冊」這個可以被關掉的子視窗
        # 裡，先確認按鈕還存在才更新，避免視窗關掉後還去操作已銷毀的元件而讓程式當掉。
        if hasattr(self, "reg_btn") and self.reg_btn.winfo_exists():
            self.reg_btn.config(text=btn_text)

    # ==========================================
    # 關閉視窗：詢問是否連同背景服務一起停掉
    # ==========================================
    def on_app_close(self):
        """ 關閉後台主視窗時，詢問是否要一併停止還在背景跑的服務，避免留下孤兒行程。 """
        if service_manager.any_service_running():
            if messagebox.askyesno("確認關閉", "偵測到有後端服務仍在執行中，要一併停止嗎？\n（選「否」會讓它們繼續在背景執行）"):
                service_manager.stop_all_services()
        self.root.destroy()

    # ==========================================
    # 視窗 1：戰情儀表板 (Dashboard)
    # ==========================================
    def open_dashboard(self):
        self.dash_win = ttk.Toplevel(self.root)
        self.dash_win.title("實時營運狀態")
        # 內容分成左右兩欄：左邊維持原本的監控畫面，右邊新增一個小終端機顯示人臉辨識
        # 的即時輸出，所以寬度要比原本的 900 多留一欄的空間。
        self.dash_win.geometry("1260x680")

        main_split = ttk.Frame(self.dash_win)
        main_split.pack(fill=BOTH, expand=True)

        left_col = ttk.Frame(main_split)
        left_col.pack(side=LEFT, fill=BOTH, expand=True)

        right_col = ttk.Frame(main_split, width=340)
        right_col.pack(side=RIGHT, fill=Y)
        right_col.pack_propagate(False)  # 固定右欄寬度，不會被裡面的文字框撐大或縮小

        # 【服務開關搬過來這裡】人臉辨識終端機是「尾隨監控」實際運作的來源（尾隨偵測、
        # 打卡都靠它），所以把開關直接放進這個監控畫面，開關跟它控制的東西擺在一起。
        face_ctrl_frame = ttk.Frame(left_col, padding=(20, 15, 20, 0))
        face_ctrl_frame.pack(fill=X)
        ttk.Label(face_ctrl_frame, textvariable=self.face_status_var, font=("微軟正黑體", 11)).pack(side=LEFT)
        self.face_btn = ttk.Button(
            face_ctrl_frame, text="📷 啟動人臉辨識終端機",
            bootstyle=(SUCCESS, OUTLINE), command=self.toggle_face_service,
        )
        self.face_btn.pack(side=RIGHT)
        # 視窗一打開就同步顯示「真正的」目前狀態（例如上次沒關就跑到現在），
        # 不要只顯示寫死的預設文字，不然狀態文字跟按鈕文字可能跟實際情況對不上。
        self._update_face_status()

        # 【內建終端機小框框】右邊即時顯示 terminal_app.py 印出來的訊息（辨識到誰、
        # 打卡結果、異常偵測等等），不寫進 log 檔案，純粹顯示在畫面上。
        self.face_console = console_panel.build_console_panel(right_col, "人臉辨識即時輸出")

        # 數據標籤變數 (用來自動更新)
        self.count_var = tk.StringVar(value="載入中...")
        self.anomaly_var = tk.StringVar(value="載入中...")

        # --- 頂部統計卡片 ---
        card_frame = ttk.Frame(left_col, padding=20)
        card_frame.pack(fill=X)

        # 今日人數卡片
        f1 = ttk.Labelframe(card_frame, text="今日到職", padding=15, bootstyle=SUCCESS)
        f1.pack(side=LEFT, expand=True, fill=BOTH, padx=10)
        ttk.Label(f1, textvariable=self.count_var, font=("Helvetica", 28, "bold"), bootstyle=SUCCESS).pack()

        # 異常警報卡片
        f2 = ttk.Labelframe(card_frame, text="安全警報", padding=15, bootstyle=DANGER)
        f2.pack(side=LEFT, expand=True, fill=BOTH, padx=10)
        ttk.Label(f2, textvariable=self.anomaly_var, font=("Helvetica", 28, "bold"), bootstyle=DANGER).pack()

        # 開啟證據資料夾按鈕
        ttk.Button(f2, text="查看證據照", bootstyle=(DANGER, LINK), command=self.open_evidence_folder).pack()

        # --- 今日打卡清單 ---
        list_frame = ttk.Frame(left_col, padding=20)
        list_frame.pack(fill=BOTH, expand=True)

        self.tree = ttk.Treeview(list_frame, columns=("time", "name", "status", "note"), show="headings", bootstyle=INFO)
        self.tree.heading("time", text="記錄時間")
        self.tree.heading("name", text="員工姓名")
        self.tree.heading("status", text="系統判定結果")
        self.tree.heading("note", text="備註")
        self.tree.column("status", width=200)
        self.tree.pack(fill=BOTH, expand=True)

        # 啟動自動更新
        self.refresh_dashboard_data()

    def refresh_dashboard_data(self):
        """ 每 5 秒自動更新儀表板數據。實際查詢邏輯搬到 dashboard_stats.py，這裡只
        負責把查到的資料轉成畫面上的文字/表格列。 """
        if not self.dash_win.winfo_exists():
            return

        # 1. 更新人數 + 今日打卡清單。查詢失敗時 result 是 None，維持畫面原本的數字，
        # 跟原本 `except: pass` 的行為一致。
        result = dashboard_stats.get_today_attendance_summary(limit=20)
        if result is not None:
            count, logs = result
            self.count_var.set(f"{count} 人")
            self.tree.delete(*self.tree.get_children())
            for log in logs:
                note = "需要查核" if "異常" in log[2] or "遲到" in log[2] else "正常"
                self.tree.insert("", "end", values=(log[0], log[1], log[2], note))

        # 2. 更新異常件數
        self.anomaly_var.set(f"{dashboard_stats.get_anomaly_count()} 件")

        # 5秒後再次執行自己 (循環更新)
        self.dash_win.after(5000, self.refresh_dashboard_data)

    def open_evidence_folder(self):
        """ 開啟異常紀錄的資料夾(支援 Windows/Mac) """
        target = os.path.abspath(ANOMALY_DIR)
        if not os.path.exists(target):
            os.makedirs(target)
        os.startfile(target)  # Windows 專用

    # ==========================================
    # 視窗 2：員工帳號管理 (Account Admin)
    # ==========================================
    def open_account_admin(self):
        """
        【P2，2026-09-12 新增】列出所有員工（含角色、啟用狀態、最新打卡狀態），可以
        停用/啟用/刪除選取的帳號。實際的資料庫操作都在 account_admin.py 裡，這裡只
        負責畫面跟把按鈕點擊轉發過去、再把結果刷新回畫面上。
        """
        admin_win = ttk.Toplevel(self.root)
        self.admin_win = admin_win  # 存起來，這樣提示/確認視窗才能指定顯示在它上面
        admin_win.title("員工帳號管理")
        admin_win.geometry("1000x600")

        frame = ttk.Frame(admin_win, padding=20)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text="👤 員工帳號管理", font=("微軟正黑體", 16, "bold"), bootstyle=PRIMARY).pack(anchor=W, pady=(0, 5))
        ttk.Label(
            frame,
            text="停用的帳號會立刻無法再刷臉打卡（系統判定為查無資料）；刪除不會影響已經留存的打卡歷史紀錄。",
            font=("微軟正黑體", 9), bootstyle=SECONDARY,
        ).pack(anchor=W, pady=(0, 10))

        columns = ("emp_id", "name", "gender", "role", "active", "last_status", "last_time")
        tree = ttk.Treeview(frame, columns=columns, show="headings", bootstyle=PRIMARY, selectmode="extended")
        tree.heading("emp_id", text="員工編號"); tree.heading("name", text="姓名")
        tree.heading("gender", text="性別"); tree.heading("role", text="角色")
        tree.heading("active", text="狀態"); tree.heading("last_status", text="最新打卡狀態")
        tree.heading("last_time", text="最後刷卡時間")
        tree.pack(fill=BOTH, expand=True, pady=(0, 10))

        def refresh():
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
        ttk.Button(btn_frame, text="🔄 重新整理", bootstyle=(SECONDARY, OUTLINE), command=refresh).pack(side=LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="⏸️ 停用所選", bootstyle=(WARNING, OUTLINE), command=do_deactivate).pack(side=LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="▶️ 啟用所選", bootstyle=(SUCCESS, OUTLINE), command=do_activate).pack(side=LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="🗑️ 刪除所選", bootstyle=(DANGER, OUTLINE), command=do_delete).pack(side=LEFT)

        refresh()

    def open_future_module(self, module_name):
        messagebox.showinfo("開發中", f"🚀 【{module_name}】模組正同步開發中！\n此介面已與核心資料庫連結成功。")

    # ==========================================
    # 視窗 4：AI 代理與個人化設定
    # ==========================================
    def open_ai_agent_settings(self):
        """ 【服務開關搬過來這裡】RAG（Ollama + LiteLLM + 助理網頁）是這個視窗要設定、
        要使用的智慧問答服務，所以開關直接放進這個視窗，開關跟它控制的東西擺在一起。 """
        agent_win = ttk.Toplevel(self.root)
        self.agent_win = agent_win  # 存起來，這樣啟動結果的提示視窗才能指定顯示在它上面
        agent_win.title("AI 代理與個人化設定")
        # 內容分成左右兩欄：左邊維持原本的設定畫面，右邊新增一個小終端機顯示 RAG 三個
        # 子服務（Ollama / LiteLLM / 助理網頁）的即時輸出，寬度要比原本的 520 多留一欄。
        agent_win.geometry("880x400")

        main_split = ttk.Frame(agent_win)
        main_split.pack(fill=BOTH, expand=True)

        left_col = ttk.Frame(main_split)
        left_col.pack(side=LEFT, fill=BOTH, expand=True)

        right_col = ttk.Frame(main_split, width=340)
        right_col.pack(side=RIGHT, fill=Y)
        right_col.pack_propagate(False)

        ttk.Label(
            left_col, text="🤖 AI 代理與個人化設定",
            font=("微軟正黑體", 16, "bold"), bootstyle=PRIMARY,
        ).pack(pady=(20, 5), padx=20, anchor=W)
        ttk.Label(
            left_col, text="管理 RAG 智慧問答服務（勞基法規 + 員工資料庫問答）",
            font=("微軟正黑體", 10), bootstyle=SECONDARY,
        ).pack(padx=20, anchor=W)

        rag_ctrl_frame = ttk.Labelframe(left_col, text="RAG 智慧問答服務", padding=15, bootstyle=INFO)
        rag_ctrl_frame.pack(fill=X, padx=20, pady=20)

        ttk.Label(rag_ctrl_frame, textvariable=self.rag_status_var, font=("微軟正黑體", 11)).pack(anchor=W, pady=(0, 10))
        self.rag_btn = ttk.Button(
            rag_ctrl_frame, text="🧠 啟動 RAG（Ollama + LiteLLM + 助理網頁）",
            bootstyle=(INFO, OUTLINE), command=self.toggle_rag_services,
        )
        self.rag_btn.pack(fill=X, ipady=8)

        ttk.Label(
            left_col,
            text="啟動完成後，可在瀏覽器開啟 http://127.0.0.1:7860 使用 RAG 助理問答網頁。",
            font=("微軟正黑體", 9), bootstyle=SECONDARY, wraplength=420, justify=LEFT,
        ).pack(padx=20, pady=(0, 15), anchor=W)

        # 【內建終端機小框框】右邊即時顯示 Ollama / LiteLLM / 助理網頁三個子服務印出來
        # 的訊息（各自會加上 [Ollama]／[LiteLLM]／[RAG App] 前綴分辨是誰印的），
        # 不寫進 log 檔案，純粹顯示在畫面上。
        self.rag_console = console_panel.build_console_panel(right_col, "RAG 服務即時輸出")

        # 視窗一打開就同步顯示「真正的」目前狀態，不要只顯示寫死的預設文字。
        self._update_rag_status()

    # ==========================================
    # 視窗 3：手機遠端 QR Code 註冊
    # ==========================================
    def open_qr_registration(self):
        """ 開啟一個新視窗，顯示動態刷新的手機註冊 QR Code """
        reg_window = ttk.Toplevel(self.root)
        self.reg_win = reg_window  # 存起來，這樣提示視窗才能指定顯示在它上面，不會被擋住
        reg_window.title("手機遠端註冊 - 動態安全碼")
        # 內容分成左右兩欄：左邊維持原本的 QR Code 畫面，右邊新增一個小終端機顯示
        # 註冊伺服器的即時輸出，寬度要比原本的 420 多留一欄的空間。
        reg_window.geometry("780x600")

        main_split = ttk.Frame(reg_window)
        main_split.pack(fill=BOTH, expand=True)

        left_col = ttk.Frame(main_split)
        left_col.pack(side=LEFT, fill=BOTH, expand=True)

        right_col = ttk.Frame(main_split, width=340)
        right_col.pack(side=RIGHT, fill=Y)
        right_col.pack_propagate(False)

        # 1. 基礎連線設定
        # 【2026-09-18，修正「WiFi 熱點的路由跟 QR 系統的 IP 對不上」的真正原因】
        # 原本這裡只在視窗一打開的當下呼叫一次 get_local_ip_offline()，之後整個
        # 視窗開著的期間，不管過多久、QR Code 每 5 秒重刷幾次，網址裡的 IP 都是
        # 用這個「開窗當下」拍的一次快照，不會再更新。但 WiFiDirectHotspotCore
        # 開的虛擬網卡，常常是在剛開熱點、甚至要等手機真的連上去之後，才會拿到
        # 穩定的 IP——如果剛好在那個 IP 還沒穩定下來的當下打開這個視窗，就會把
        # 錯的/過期的 IP 寫死用一整個視窗生命週期，QR Code 掃出來的網址手機永遠
        # 連不到；重新關開視窗「剛好」IP 已經穩定了，看起來就像「關掉開啟來就對
        # 上了」，其實只是運氣好重新拍到一次正確的快照而已，不是真的修好。改成
        # base_url 不再是開窗當下算好、之後都不變的固定值，而是 update_qr() 每次
        # 重新整理（每 5 秒一次）都重新呼叫 get_local_ip_offline() 重新偵測一次，
        # IP 換了會在最多 5 秒內自動反映到新產生的 QR Code／網址上，不用使用者
        # 自己發現「不對」再手動關開視窗。
        def _current_base_url():
            return f"http://{service_manager.get_local_ip_offline()}:5000"

        # 【服務開關搬過來這裡】QR Code 要能被手機掃到、掃了要能真的送出註冊資料，
        # 都得靠 web_server.py 這個伺服器在跑，所以開關直接放進這個註冊畫面，
        # 開關跟它控制的東西擺在一起。
        reg_ctrl_frame = ttk.Frame(left_col, padding=(20, 15, 20, 0))
        reg_ctrl_frame.pack(fill=X)
        ttk.Label(reg_ctrl_frame, textvariable=self.reg_status_var, font=("微軟正黑體", 11)).pack(side=LEFT)
        self.reg_btn = ttk.Button(
            reg_ctrl_frame, text="📱 啟動 QR 註冊伺服器",
            bootstyle=(WARNING, OUTLINE), command=self.toggle_registration_service,
        )
        self.reg_btn.pack(side=RIGHT)
        # 視窗一打開就同步顯示「真正的」目前狀態，不要只顯示寫死的預設文字。
        self._update_reg_status()

        # 【內建終端機小框框】右邊即時顯示 web_server.py 印出來的訊息（誰掃了 QR
        # Code、註冊成功或失敗等等），不寫進 log 檔案，純粹顯示在畫面上。
        self.reg_console = console_panel.build_console_panel(right_col, "註冊伺服器即時輸出")

        # UI 元件
        ttk.Label(left_col, text="請新進員工掃描此碼進行註冊", font=("微軟正黑體", 14)).pack(pady=20)

        # 用於顯示 QR Code 的 Label
        self.qr_label = ttk.Label(left_col)
        self.qr_label.pack(pady=10)

        # 用於顯示當前網址的 Label
        self.url_label = ttk.Label(left_col, font=("Consolas", 10))
        self.url_label.pack(pady=10)

        # 建立計時器追蹤，用於關窗時停止
        self.qr_timer_id = None

        # 【2026-09-06，讓終端機不要看起來像出大事】web_server.py 啟動時要先載入人臉
        # 辨識模型（TensorFlow/RetinaFace），實測要花好幾秒。這幾秒鐘裡，這個每 5 秒
        # 問一次「有沒有新驗證碼」的計時器已經開始跑了，一定會連續撲空個一兩次——
        # 這是正常的啟動過渡現象，不是壞掉，晚一點一定會自己接上（QR Code 照樣會
        # 正常顯示，這點已經請使用者實際測試確認過）。原本的寫法一撲空就把整段
        # Python 錯誤堆疊印出來，看起來像出了大事，容易讓人虛驚一場。
        # 現在改成：啟動後前幾次撲空只安靜顯示「伺服器準備中」，不印錯誤堆疊；
        # 只有真的連續撲空超過 30 秒（正常啟動時間的好幾倍）才印出詳細警告——
        # 這種情況才代表 web_server.py 可能真的沒有成功啟動，才值得使用者去注意。
        self._qr_fail_count = 0
        _QR_STARTUP_GRACE_ATTEMPTS = 6  # 6 次 x 5 秒 = 30 秒的啟動緩衝期

        def update_qr():
            """ 動態刷新 QR Code 的核心迴圈 - 改為呼叫內部 API 以確保 Token 狀態同步 """
            try:
                import urllib.request
                import json

                # 1. 呼叫 Flask 內部 API 獲取 Token
                with urllib.request.urlopen("http://127.0.0.1:5000/api/internal/generate_token", timeout=2) as response:
                    data = json.loads(response.read().decode())
                    token = data['token']

                dynamic_url = f"{_current_base_url()}?t={token}"

                # 2. 記憶體內生成 QR Code
                qr = pyqrcode.create(dynamic_url)
                buffer = io.BytesIO()
                qr.png(buffer, scale=6)
                buffer.seek(0)

                # 3. 轉換為 Tkinter 可用的 PhotoImage
                img = Image.open(buffer)
                photo = ImageTk.PhotoImage(img)

                # 4. 更新 UI
                self.qr_label.config(image=photo)
                self.qr_label.image = photo
                self.url_label.config(text=f"目前網址: {dynamic_url}")
                self._qr_fail_count = 0  # 成功了，失敗次數歸零

            except Exception as e:
                self._qr_fail_count += 1
                if self._qr_fail_count <= _QR_STARTUP_GRACE_ATTEMPTS:
                    # 還在合理的啟動緩衝期內（30 秒內），安靜提示就好，不要嚇人
                    self.url_label.config(text="伺服器準備中，請稍候...")
                else:
                    # 超過 30 秒還連不上，代表 web_server.py 可能真的沒有成功啟動，
                    # 這時候才值得印出詳細錯誤，提醒使用者去檢查
                    print(f"[QR Update Error] 已連續 {self._qr_fail_count} 次連線失敗"
                          f"（約 {self._qr_fail_count * 5} 秒），可能是 web_server.py 沒有成功啟動：{e}")
                    self.url_label.config(text="⚠️ 伺服器連線失敗，請確認「啟動 QR 註冊伺服器」是否正常運作")
                    import traceback
                    traceback.print_exc()

            # 每 5000 毫秒刷新一次
            self.qr_timer_id = reg_window.after(5000, update_qr)

        # 綁定視窗關閉事件，確保停止計時器避免記憶體洩漏
        def on_close():
            if self.qr_timer_id:
                reg_window.after_cancel(self.qr_timer_id)
            reg_window.destroy()

        reg_window.protocol("WM_DELETE_WINDOW", on_close)

        # 立即執行第一次刷新
        update_qr()


if __name__ == "__main__":
    # 【2026-09-12，P3 風格更新 v2】淺色版（cosmo）你覺得還是土，改用 "superhero"——
    # ttkbootstrap 內建主題，深藍灰底（不是純黑）、主色是飽和藍，最接近你參考的那種
    # 深色 SaaS 後台觀感；换成 _create_module_card 的懸浮陰影卡片畫法之後，質感落差
    # 會很明顯。之後如果還想再換別的深色主題（"darkly"、"cyborg"、"solar" 都是
    # ttkbootstrap 內建的深色選項），這裡改一個字串就好，卡片的顏色是動態算出來的，
    # 不用跟著重寫。
    app = ttk.Window(themename="superhero")
    BackendManagerApp(app)
    app.mainloop()
