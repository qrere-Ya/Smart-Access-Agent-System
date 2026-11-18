"""
model.py：前台儀表板（PySide6 + QML 版）的資料模型——把原本 Tk 儀表板的邏輯整理成一個 QObject 給 QML 畫面讀。

主要功能：
  - DashboardModel：每秒讀一次本機資料（front_view.snapshot、資源、辨識程式狀態、兩個問答控制器）更新屬性；
    只有內容真的變了才發出變更訊號，QML 清單不會每秒重畫或捲回頂端
  - 屬性（唯讀，QML 綁定）：clock、link、alert、tiles、news、greetings、status、resources、recognition、
    qaTalk／qaChat（Smart_Chat 與 Smart_RAG 兩個問答面板的顯示內容）、palette（目前外觀的顏色）、
    theme、fullscreen、current
  - 槽（QML 呼叫）：openModule、goHome、submit、clearPanel、toggleTheme、toggleFullscreen、leaveFullscreen、
    startRecognition、touch、iconUri
  - 閒置規則：模組畫面 90 秒沒人操作回主畫面；問答面板半小時沒人操作才清除輸入與回答
    （離開面板回主畫面不清；前台不保存任何對話）
  - 讀資料（資料庫、資源）在背景執行緒做（refresh_async），畫面執行緒只負責把結果套到屬性，資料庫忙碌時畫面不會卡住
  - 圖示：assets/icons 的 SVG 換成目前外觀的主色與強調色，包成 data URI 給 QML 的 Image 直接畫（Qt 內建 SVG）
  - 只讀本機資料（主題與全螢幕偏好除外）；沒有網路、沒有帳號、沒有設定功能

串接模組：
  - access.front_view：畫面內容整理；access.front_chat：兩個問答控制器；access.front_store：本機資料
  - access.front_launcher.ManagedProcess：人臉辨識的啟動；access.front_resources：系統資源
  - access.front_qr：新聞原文的 QR Code；shared.ui_theme：顏色；shared.svg_icon：圖示 SVG 文字
  - access.front_qt.app：建立視窗並載入 qml/Main.qml
"""
import base64
import datetime
import concurrent.futures
import io
import time
from urllib.parse import quote

from PySide6.QtCore import Property, QObject, Signal, Slot

from access import front_agent, front_chat, front_qr, front_resources, front_screens, front_view
from access.front_store import ScreenState
from shared import network_policy, svg_icon, ui_theme

DETAIL_IDLE_SECONDS = 90.0            # 模組全版畫面沒人操作這麼久，自動回主畫面
CHAT_IDLE_SECONDS = 1800.0            # 問答面板沒人操作半小時，自動清除輸入與回答（兩個模組各自計時，離開面板不清）
MODULE_KEYS = ("talk", "chat", "news", "greetings", "status")     # talk＝Smart_Chat、chat＝Smart_RAG（舊名）
THEMES = ("dark", "frosted")
FROSTED_ALPHA = 0.97
TILE_TOKEN = {"talk": "mod_chat", "chat": "mod_rag", "news": "mod_news", "greetings": "mod_greet",
              "status": "mod_status"}                 # 每個模組自己的顏色，不隨狀態變
QR_PIXELS = 240


def qr_data_uri(url):
    """新聞原文網址的 QR Code，做成 PNG 的 data URI；做不出來回傳空字串。"""
    image = front_qr.image(url, QR_PIXELS)
    if image is None:
        return ""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def qa_display(view, messages):
    """問答面板要顯示的內容：對話泡泡清單（使用者在右、助理在左）、狀態文字、思考中、要不要顯示歡迎畫面與輸入列、輸入列能不能用。"""
    state = view["state"]
    asking = state in ("asking", "preparing")
    return {"state": state, "messages": list(messages), "thinking": asking,
            "status": "" if state in ("answer", "idle") else view["message"],
            "welcome": state == "idle" and not messages, "inputVisible": state not in ("closed", "offline"),
            "inputEnabled": not asking}


class DashboardModel(QObject):
    clockChanged = Signal()
    linkChanged = Signal()
    alertChanged = Signal()
    tilesChanged = Signal()
    newsChanged = Signal()
    greetingsChanged = Signal()
    statusChanged = Signal()
    resourcesChanged = Signal()
    recognitionChanged = Signal()
    qaTalkChanged = Signal()
    qaChatChanged = Signal()
    uiChanged = Signal()
    currentChanged = Signal()
    panelCleared = Signal(str)                    # 某個問答面板被清空：QML 的輸入框跟著清空
    fullscreenRequested = Signal(bool)
    collected = Signal(object)                    # 背景執行緒讀好的資料，交回畫面執行緒套用
    themeApplied = Signal(float)                  # 外觀換了：視窗透明度（半透明白略透）

    def __init__(self, store, now=datetime.datetime.now, clock=time.time, read_heartbeat=None, read_resources=None,
                 recognition=None, chat=None, talk=None, parent=None):
        super().__init__(parent)
        self.store, self._now, self._clock = store, now, clock
        self.screen = ScreenState(store)
        self._read_heartbeat = read_heartbeat or front_agent.read_terminal_heartbeat
        self._read_resources = read_resources or front_resources.ResourceSampler().read
        self.recognition = recognition
        self._controllers = {"chat": chat or front_chat.FrontChat(store), "talk": talk or front_chat.FrontTalk(store)}
        self._messages = {"chat": [], "talk": []}         # 兩個面板各自的對話泡泡（只在記憶體，半小時沒人操作就清掉）
        self._pending = {"chat": False, "talk": False}   # 已送出、還在等這一題的結果
        self._panel_touched = {"chat": clock(), "talk": clock()}
        self._touched = clock()
        self._current = ""
        self._fullscreen = False
        theme = self.screen.theme()
        self._theme = theme if theme in THEMES else "dark"
        self._icons = {}
        self._qr = {}
        self._refreshing = False
        self._collecting = False
        self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="front-qt-read")
        self.collected.connect(self._on_collected)
        self._values = {"clock": {}, "link": {}, "alert": None, "tiles": [], "news": {}, "greetings": [],
                        "status": [], "resources": {}, "recognition": {}, "qaTalk": {}, "qaChat": {}}
        self.refresh()

    # --- QML 讀的屬性 ---
    def _get(name):                                                       # noqa: N805  產生唯讀屬性的取值函式
        return lambda self: self._values[name]

    clock = Property("QVariant", _get("clock"), notify=clockChanged)
    link = Property("QVariant", _get("link"), notify=linkChanged)
    alert = Property("QVariant", _get("alert"), notify=alertChanged)
    tiles = Property("QVariant", _get("tiles"), notify=tilesChanged)
    news = Property("QVariant", _get("news"), notify=newsChanged)
    greetings = Property("QVariant", _get("greetings"), notify=greetingsChanged)
    status = Property("QVariant", _get("status"), notify=statusChanged)
    resources = Property("QVariant", _get("resources"), notify=resourcesChanged)
    recognitionState = Property("QVariant", _get("recognition"), notify=recognitionChanged)
    qaTalk = Property("QVariant", _get("qaTalk"), notify=qaTalkChanged)
    qaChat = Property("QVariant", _get("qaChat"), notify=qaChatChanged)

    @Property("QVariant", notify=uiChanged)
    def palette(self):
        mode = "light" if self._theme == "frosted" else "dark"
        return dict(ui_theme.COLORS[mode], mode=mode)

    @Property(str, notify=uiChanged)
    def theme(self):
        return self._theme

    @Property(bool, notify=uiChanged)
    def fullscreen(self):
        return self._fullscreen

    @Property(str, notify=currentChanged)
    def current(self):
        return self._current

    # --- 內部：只有變了才發訊號 ---
    def _set(self, name, value, signal):
        if self._values.get(name) != value:
            self._values[name] = value
            signal.emit()

    def local_only(self):
        """全本地模式：前台本機設定或後台同步來的開關，任一個開就算。"""
        return network_policy.local_only() or self.store.get_meta(front_agent.BACK_LOCAL_ONLY_KEY) is True

    def recognition_allowed(self):
        return self.store.get_meta(front_agent.BACK_RECOGNITION_KEY) is not False

    def _recognition_view(self, allowed):
        """辨識按鈕的狀態：後台停止→收掉辨識並停用；執行中停用（前台不能停止）；結束後回到可啟動。"""
        if self.recognition is None:
            return {"visible": False}
        if not allowed and self.recognition.running():
            self.recognition.stop()                              # 只有後台的指令能停止辨識
        on = self.recognition.running()
        last_exit = getattr(self.recognition, "last_exit", None)
        if not allowed:
            return {"visible": True, "text": "後台已停止人臉辨識", "enabled": False, "tone": "warning"}
        if on:
            return {"visible": True, "text": "人臉辨識執行中", "enabled": False, "tone": "success"}
        text = "啟動人臉辨識" if last_exit is None else f"重新啟動（剛結束，代碼 {last_exit}）"
        return {"visible": True, "text": text, "enabled": True, "tone": "primary"}

    def refresh(self):
        """讀一次本機資料並更新屬性；內容沒變的屬性不發訊號。閒置規則裡的動作也會呼叫 refresh，所以不可重入。"""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self._refresh()
        finally:
            self._refreshing = False

    def refresh_async(self):
        """排一次背景讀取；上一次還沒讀完就略過（資料庫忙碌時不堆積，也不會卡住畫面）。"""
        if self._collecting or self._refreshing:
            return
        self._collecting = True
        self._worker.submit(self._collect_job)

    def _collect_job(self):
        try:
            data = self._collect()
        except Exception as e:  # noqa: BLE001  讀取出錯：這一輪略過，下一輪再試
            print(f"[front_qt][WARN] 讀取本機資料失敗：{type(e).__name__}")
            data = None
        self.collected.emit(data)

    def _on_collected(self, data):
        self._collecting = False
        if data is None or self._refreshing:
            return
        self._refreshing = True
        try:
            self._apply(data)
        finally:
            self._refreshing = False

    def _collect(self):
        """讀這一輪要用的本機資料（會碰資料庫與資源檔，可以在背景執行緒做；不改任何畫面屬性）。"""
        view = front_view.snapshot(self.store, self.screen, self._read_heartbeat, self._now(), self._clock(),
                                   local_only=self.local_only(), chat_state=self._controllers["chat"].availability(),
                                   talk_state=self._controllers["talk"].availability())
        return {"view": view, "allowed": self.recognition_allowed(), "resources": self._resources(),
                "qa": {key: ctrl.view() for key, ctrl in self._controllers.items()}}

    def _refresh(self):
        self._apply(self._collect())

    def _apply(self, data):
        view = data["view"]
        self._set("clock", {"time": view["time"], "date": view["date"]}, self.clockChanged)
        tone, text = view["link"]
        self._set("link", {"tone": tone, "text": text}, self.linkChanged)
        alert = view["alert"]
        self._set("alert", None if alert is None else {"title": alert["title"], "detail": alert["detail"]},
                  self.alertChanged)
        tiles = [dict(t, token=TILE_TOKEN[t["key"]]) for t in view["tiles"] if t["key"] in TILE_TOKEN]
        self._set("tiles", tiles, self.tilesChanged)
        news = dict(view["news"])
        news["cards"] = [dict(c, qr=self._qr_uri(c.get("link"))) for c in news["cards"]]
        news["pageText"] = f"{news['page']} / {news['pages']}" if news["pages"] > 1 else ""
        self._set("news", news, self.newsChanged)
        rows = view["greetings"] or [("", "辨識成功的人會顯示在這裡", "")]
        self._set("greetings", [{"time": a, "name": b, "greeting": c} for a, b, c in rows], self.greetingsChanged)
        self._set("status", [{"item": a, "state": b, "tone": c} for a, b, c in view["status"]], self.statusChanged)
        self._set("resources", data["resources"], self.resourcesChanged)
        self._set("recognition", self._recognition_view(data["allowed"]), self.recognitionChanged)
        for key, name, signal in (("talk", "qaTalk", self.qaTalkChanged), ("chat", "qaChat", self.qaChatChanged)):
            self._absorb(key, data["qa"][key])
            self._set(name, qa_display(data["qa"][key], self._messages[key]), signal)
        self._idle_rules()

    def _absorb(self, key, view):
        """等待中的那一題有結果了：把回答（或失敗說明）加進這個面板的對話泡泡。"""
        if not self._pending[key]:
            return
        state = view["state"]
        if state == "answer" and view["answer"]:
            self._messages[key].append({"role": "assistant", "text": view["answer"], "sources": list(view["sources"])})
        elif state in ("error", "busy", "closed", "offline"):
            self._messages[key].append({"role": "error", "text": view["message"] or "這一題沒有成功", "sources": []})
        else:
            return
        self._pending[key] = False

    def _qr_uri(self, url):
        if not url:
            return ""
        if url not in self._qr:
            self._qr[url] = qr_data_uri(url)
        return self._qr[url]

    def _resources(self):
        try:
            data = self._read_resources()
        except Exception:  # noqa: BLE001
            data = {}
        out = {}
        for key in ("cpu", "ram", "gpu"):
            value = data.get(key)
            out[key] = {"text": "未偵測到" if key == "gpu" else "—", "ratio": 0.0, "tone": "success"} if value is None \
                else {"text": f"{value:.0f}%", "ratio": max(0.0, min(1.0, value / 100.0)),
                      "tone": front_view.load_tone(value)}
        out["down"] = front_view.speed_text("↓", data.get("down_kbps"))
        out["up"] = front_view.speed_text("↑", data.get("up_kbps"))
        return out

    def _idle_rules(self):
        now = self._clock()
        for key in self._controllers:                            # 兩個問答模組各自計時，沒人操作半小時就重置
            if now - self._panel_touched[key] > CHAT_IDLE_SECONDS:
                self.clearPanel(key)
                self._panel_touched[key] = now
        if self._current and now - self._touched > DETAIL_IDLE_SECONDS:
            self.goHome()

    # --- QML 呼叫的動作 ---
    @Slot()
    def touch(self):
        """有人在操作（滑鼠、鍵盤）：重新計算閒置時間。"""
        self._touched = self._clock()
        if self._current in self._panel_touched:
            self._panel_touched[self._current] = self._touched

    @Slot(str)
    def openModule(self, key):
        if key not in MODULE_KEYS:
            return
        self._current = key
        self._touched = self._clock()
        if key in self._panel_touched:
            self._panel_touched[key] = self._touched
        self.currentChanged.emit()
        self.refresh()

    @Slot()
    def goHome(self):
        """回到主畫面；問答面板的內容保留，半小時沒人操作才重置。"""
        self._current = ""
        self.currentChanged.emit()

    @Slot(str, str, result=bool)
    def submit(self, key, text):
        """送出問題；空白、處理中、後台沒開放時回傳 False（輸入框保留原樣）。"""
        controller = self._controllers.get(key)
        question = (text or "").strip()
        if controller is None or not controller.submit(question):
            self.refresh()
            return False
        self._messages[key].append({"role": "user", "text": question, "sources": []})
        self._pending[key] = True
        self.touch()
        self.refresh()
        return True

    @Slot(str)
    def clearPanel(self, key):
        controller = self._controllers.get(key)
        if controller is None:
            return
        controller.clear()
        self._messages[key] = []
        self._pending[key] = False
        self.panelCleared.emit(key)
        self.refresh()

    @Slot()
    def toggleTheme(self):
        self._theme = "dark" if self._theme == "frosted" else "frosted"
        try:
            self.screen.set_theme(self._theme)
        except Exception:  # noqa: BLE001, S110  # nosec B110  記不住主題不影響使用
            pass
        self.uiChanged.emit()
        self.themeApplied.emit(FROSTED_ALPHA if self._theme == "frosted" else 1.0)

    @Slot()
    def leaveFullscreen(self):
        self.setFullscreen(False)

    @Slot()
    def toggleFullscreen(self):
        self.setFullscreen(not self._fullscreen)

    def setFullscreen(self, on, remember=True):
        if on == self._fullscreen:
            return
        self._fullscreen = on
        self.uiChanged.emit()
        self.fullscreenRequested.emit(on)
        if remember:
            front_screens.save_pref("dashboard_fullscreen", on)

    @Slot()
    def startRecognition(self):
        """啟動人臉辨識（前台只能啟動、不能停止）。"""
        if self.recognition is None or self.recognition.running() or not self.recognition_allowed():
            return
        ok, message = self.recognition.start()
        if not ok:
            print(f"[front_qt][WARN] 人臉辨識{message}")
        self.refresh()

    @Slot(str, str, str, result=str)
    def iconUri(self, name, color, accent):
        """SVG 圖示（主色、強調色換成指定顏色）的 data URI；圖示不存在回傳空字串。"""
        key = (name, color, accent)
        if key not in self._icons:
            text = svg_icon.colored_svg(name, color, accent)
            self._icons[key] = "" if text is None else "data:image/svg+xml;utf8," + quote(text)
        return self._icons[key]
