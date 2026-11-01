"""
ui_theme.py：後台與管理者助理共用的 CustomTkinter 主題（色彩、字型、間距與少量建構函式）。

主要功能：
  - 色彩 token（亮／暗兩套，以 rag/chat_window 的深色風格為基準）、字型與間距常數
  - apply_theme()：設定外觀模式與預設主題；color()／pair()：取 token 的單色或 (亮, 暗) 雙色
  - 建構函式：primary／secondary／danger 按鈕、卡片、區塊標題、狀態徽章（pill）
  - 對話框：show_message()／confirm()（CTkToplevel，置中並 grab_set）
  - style_treeview()：customtkinter 沒有表格，以 ttk.Treeview 調樣式，亮暗色都可讀（不新增依賴）
  - contrast_ratio()：WCAG 對比度計算，供測試確認文字與底色看得清楚
  畫面以外的邏輯不放這裡。

串接模組：
  - rag.chat_window、admin.backend_main：後續批次遷移時改用這裡的 token 與元件
  - tests/unit/shared/test_ui_theme.py（純邏輯）、tests/integration/test_ui_theme_widgets.py（真 Tk）
"""
import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

FONT_FAMILY = "微軟正黑體"
FONT_SIZE_SMALL = 12
FONT_SIZE_BODY = 13
FONT_SIZE_INPUT = 14
FONT_SIZE_SECTION = 16
FONT_SIZE_TITLE = 18
FONT_SIZE_HERO = 24

PAD_XS = 4
PAD_SM = 8
PAD_MD = 14
PAD_LG = 18
RADIUS_CARD = 12
RADIUS_PILL = 12
RADIUS_BUTTON = 8
ROW_HEIGHT = 30

MODES = ("light", "dark")

# 每個 token 都必須在 light、dark 兩套同時存在（測試會檢查）
COLORS = {
    "light": {
        "primary": "#2563eb", "primary_hover": "#1d4ed8", "on_primary": "#ffffff",
        "danger": "#b91c1c", "danger_hover": "#991b1b", "on_danger": "#ffffff",
        "warning": "#b45309", "success": "#15803d", "info": "#2563eb", "on_status": "#ffffff",
        "secondary": "#e5e7eb", "secondary_hover": "#d1d5db", "on_secondary": "#111827",
        "bg": "#f3f4f6", "card": "#ffffff", "border": "#d1d5db",
        "text": "#111827", "text_muted": "#4b5563",
        "table_bg": "#ffffff", "table_alt": "#f3f4f6", "table_head": "#e5e7eb", "table_select": "#bfdbfe",
    },
    "dark": {
        "primary": "#2563eb", "primary_hover": "#1d4ed8", "on_primary": "#ffffff",
        "danger": "#dc2626", "danger_hover": "#b91c1c", "on_danger": "#ffffff",
        "warning": "#f59e0b", "success": "#22c55e", "info": "#60a5fa", "on_status": "#111827",
        "secondary": "#2b2f3a", "secondary_hover": "#3a4050", "on_secondary": "#e8eaf0",
        "bg": "#1a1d24", "card": "#242831", "border": "#3a4050",
        "text": "#e8eaf0", "text_muted": "#9aa4b2",
        "table_bg": "#242831", "table_alt": "#2b2f3a", "table_head": "#3a4050", "table_select": "#1e40af",
    },
}

# 徽章：狀態名稱 -> (底色 token, 字色 token)
BADGE_STYLES = {
    "success": ("success", "on_status"), "warning": ("warning", "on_status"),
    "danger": ("danger", "on_danger"), "info": ("info", "on_status"), "neutral": ("secondary", "on_secondary"),
}


def font(size=FONT_SIZE_BODY, bold=False):
    """回傳 customtkinter 用的字型 tuple。"""
    return (FONT_FAMILY, size, "bold") if bold else (FONT_FAMILY, size)


def current_mode():
    """目前實際生效的外觀（'light' 或 'dark'）；跟隨系統時也會解析成其中之一。"""
    return "dark" if ctk.get_appearance_mode().lower() == "dark" else "light"


def color(name, mode=None):
    """取單一模式下的 token 色碼；mode 省略就用目前外觀。"""
    return COLORS[mode or current_mode()][name]


def pair(name):
    """取 (亮, 暗) 雙色，直接餵給 customtkinter 的 fg_color／text_color，切換外觀會自動換色。"""
    return (COLORS["light"][name], COLORS["dark"][name])


def apply_theme(mode="system", color_theme="blue"):
    """設定外觀模式（system／light／dark）與預設主題；在建立任何視窗前呼叫。"""
    if mode not in ("system", "light", "dark"):
        raise ValueError(f"未知的外觀模式：{mode}")
    ctk.set_appearance_mode(mode)
    ctk.set_default_color_theme(color_theme)


# --- 對比度 ---

def _luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast_ratio(fg, bg):
    """WCAG 對比度（1～21）；一般文字建議至少 4.5。"""
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# --- 建構函式 ---

def primary_button(master, text, command=None, **kw):
    """主要動作按鈕（實心主色）。"""
    return ctk.CTkButton(master, text=text, command=command, font=font(), corner_radius=RADIUS_BUTTON,
                         fg_color=pair("primary"), hover_color=pair("primary_hover"),
                         text_color=pair("on_primary"), **kw)


def secondary_button(master, text, command=None, **kw):
    """次要按鈕（透明底加邊線，與助理視窗標題列按鈕一致）。"""
    return ctk.CTkButton(master, text=text, command=command, font=font(), corner_radius=RADIUS_BUTTON,
                         fg_color="transparent", border_width=1, border_color=pair("border"),
                         hover_color=pair("secondary_hover"), text_color=pair("text"), **kw)


def danger_button(master, text, command=None, **kw):
    """危險動作按鈕（刪除、清除等）。"""
    return ctk.CTkButton(master, text=text, command=command, font=font(), corner_radius=RADIUS_BUTTON,
                         fg_color=pair("danger"), hover_color=pair("danger_hover"),
                         text_color=pair("on_danger"), **kw)


def card(master, **kw):
    """卡片容器：圓角、卡片底色加細邊線。"""
    opts = {"corner_radius": RADIUS_CARD, "fg_color": pair("card"), "border_width": 1,
            "border_color": pair("border"), **kw}
    return ctk.CTkFrame(master, **opts)


def section_title(master, text, **kw):
    """區塊標題。"""
    return ctk.CTkLabel(master, text=text, font=font(FONT_SIZE_SECTION, bold=True), text_color=pair("text"),
                        anchor="w", **kw)


def status_badge(master, text, kind="neutral"):
    """狀態徽章（pill）；kind 為 success／warning／danger／info／neutral，之後可用 set_badge 改狀態。"""
    badge = ctk.CTkLabel(master, text=text, font=font(FONT_SIZE_SMALL, bold=True), corner_radius=RADIUS_PILL,
                         padx=PAD_SM, pady=1)
    set_badge(badge, text, kind)
    return badge


def set_badge(badge, text, kind):
    """更新徽章的文字與狀態顏色。"""
    if kind not in BADGE_STYLES:
        raise ValueError(f"未知的徽章種類：{kind}")
    bg, fg = BADGE_STYLES[kind]
    badge.configure(text=text, fg_color=pair(bg), text_color=pair(fg))


# --- 對話框 ---

class _Dialog(ctk.CTkToplevel):
    """模態對話框：置中於父視窗、grab_set，關閉（含按 X）等同取消。"""

    def __init__(self, master, title, message, buttons, kind):
        super().__init__(master)
        self.result = None
        self.withdraw()
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        accent = {"error": "danger", "warning": "warning", "info": "info"}.get(kind, "info")
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(padx=PAD_LG, pady=(PAD_LG, PAD_SM), fill="x")
        accent_bar = ctk.CTkFrame(body, width=4, fg_color=pair(accent), corner_radius=2)
        accent_bar.pack(side="left", fill="y", padx=(0, PAD_MD))
        ctk.CTkLabel(body, text=message, font=font(), wraplength=360, justify="left", anchor="w").pack(side="left")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=PAD_LG, pady=(PAD_SM, PAD_LG), fill="x")
        for label, value, style in reversed(buttons):
            make = {"primary": primary_button, "danger": danger_button}.get(style, secondary_button)
            make(row, label, lambda v=value: self._choose(v), width=90).pack(side="right", padx=(PAD_SM, 0))
        self.bind("<Escape>", lambda e: self.destroy())
        self._center(master)
        self.deiconify()
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:  # 視窗尚未可見（例如父視窗被隱藏）時不強制模態，仍可操作
            pass
        self.focus_set()

    def _choose(self, value):
        self.result = value
        self.destroy()

    def _center(self, master):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        if master is not None and master.winfo_viewable():
            x = master.winfo_rootx() + (master.winfo_width() - w) // 2
            y = master.winfo_rooty() + (master.winfo_height() - h) // 2
        else:
            x = (self.winfo_screenwidth() - w) // 2
            y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")


def show_message(master, title, message, kind="info"):
    """顯示訊息對話框（kind：info／warning／error），按確定後才返回。"""
    dialog = _Dialog(master, title, message, [("確定", True, "primary")], kind)
    dialog.wait_window()


def confirm(master, title, message, ok_text="確定", cancel_text="取消", danger=False):
    """確認對話框，回傳 True／False；關閉視窗或按 Esc 視為 False。"""
    buttons = [(cancel_text, False, "secondary"), (ok_text, True, "danger" if danger else "primary")]
    dialog = _Dialog(master, title, message, buttons, "warning" if danger else "info")
    dialog.wait_window()
    return bool(dialog.result)


# --- 表格（ttk.Treeview 樣式） ---

def style_treeview(tree=None, mode=None, style_name="Smart.Treeview"):
    """
    把 ttk.Treeview 調成與主題一致的亮／暗樣式並回傳樣式名稱。
    tree 給定就直接套用（加斑馬紋 tag 'odd'／'even'）；切換外觀後要再呼叫一次。
    用 clam 內建主題才吃得到底色與標題色，這會改整個行程的 ttk 主題，所以只在需要表格的視窗呼叫。
    """
    mode = mode or current_mode()
    c = COLORS[mode]
    style = ttk.Style(tree)
    style.theme_use("clam")
    style.configure(style_name, background=c["table_bg"], fieldbackground=c["table_bg"], foreground=c["text"],
                    bordercolor=c["border"], lightcolor=c["table_bg"], darkcolor=c["table_bg"],
                    rowheight=ROW_HEIGHT, font=font(), borderwidth=0)
    style.configure(f"{style_name}.Heading", background=c["table_head"], foreground=c["text"],
                    font=font(bold=True), relief="flat", borderwidth=0)
    style.map(f"{style_name}.Heading", background=[("active", c["secondary_hover"])])
    style.map(style_name, background=[("selected", c["table_select"])], foreground=[("selected", c["text"])])
    if tree is not None:
        tree.configure(style=style_name)
        tree.tag_configure("odd", background=c["table_bg"])
        tree.tag_configure("even", background=c["table_alt"])
    return style_name


def zebra_tags(index):
    """依列序回傳斑馬紋 tag，插入列時用：tree.insert(..., tags=zebra_tags(i))。"""
    return ("even",) if index % 2 else ("odd",)
