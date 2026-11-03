"""
login_window.py：管理後台的人臉登入畫面（CustomTkinter，套用 shared/ui_theme）。

主要功能：
  - 開鏡頭預覽，背景執行緒逐幀判斷（face_login.FaceLoginVerifier），畫面定時取最新結果顯示提示與進度
  - 正式啟動時（isolate_vision=True）鏡頭與人臉判斷放在獨立子行程（login_worker），登入結束就結束它，
    視覺模型占用的約 1.3GB 記憶體才會還給系統；失敗計數、鎖定、異常事件與工作階段仍在這裡處理
  - 通過就建立工作階段（auth.Session）並寫入工作階段檔，後台才會打開；關閉視窗代表放棄登入
  - 失敗與鎖定會寫入異常事件（login_failed、login_locked）；失敗 5 次鎖 60 秒，倒數期間不判斷；
    兩次計入的失敗至少相隔數秒，同一次站在鏡頭前的連續不過只算一次（也只寫一筆事件）
  - 還沒有任何管理者時進入「初次設定」：在本機從啟用中的員工挑一位設為第一位管理者，再用他的臉登入
  - 鏡頭打不開（例如被門禁終端機占用）會說明原因並提供重試；視窗關閉時釋放鏡頭與視覺模型
  鏡頭與視覺函式可由外部傳入，測試不需要真的鏡頭。背景執行緒只把結果放進佇列，不碰任何 Tk 元件。

串接模組：
  - face_login：逐幀判斷；login_worker：視覺子行程的輸出協定；auth：失敗計數與工作階段
  - account_admin、database_mgr：管理者名單與設定第一位管理者、記錄異常事件
  - ui_theme：色彩、字型、按鈕、確認對話框
  - backend_main：啟動時呼叫 run_login（與主畫面共用同一個根視窗）
"""
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk

import customtkinter as ctk

from admin import account_admin, face_login, login_worker
from shared import auth, paths, ui_theme
from shared import database_mgr as db

PREVIEW_SIZE = (480, 360)
POLL_MS = 40


def open_camera():
    """開預設鏡頭；打不開回傳 None。"""
    import cv2

    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap.release()
        return None
    return cap


def _scan_loop(camera_factory, vision_factory, guard, stop, events):
    """背景執行緒：讀鏡頭、逐幀判斷，結果放進佇列。刻意不拿視窗物件：Tk 物件若在別的執行緒被回收會讓程式直接中止。"""
    camera = vision = None
    try:
        camera = camera_factory()
        if camera is None:
            events.put(("error", "打不開鏡頭。請確認鏡頭已連接，且沒有被門禁終端機或其他程式占用。"))
            return
        vision = vision_factory()
        verifier = face_login.FaceLoginVerifier(vision)
        while not stop.is_set():
            ok, frame = camera.read()
            if not ok:
                events.put(("error", "讀不到鏡頭畫面。"))
                return
            if guard.locked_for() > 0:
                events.put(("locked", frame, guard.locked_for()))
                time.sleep(0.2)
                verifier.reset()
                continue
            events.put(("step", frame, verifier.step(frame)))
    except Exception as e:
        events.put(("error", f"登入畫面發生錯誤：{type(e).__name__}"))
    finally:
        if camera is not None:
            camera.release()
        close = getattr(vision, "close", None)
        if close:
            close()


WORKER_COMMAND = (sys.executable, "-m", "admin.login_worker")
WORKER_EXIT_WAIT = 5      # 登入結束後最多等子行程幾秒讓它釋放鏡頭，逾時就強制結束


def _stop_worker(child):
    """請子行程結束並等它釋放鏡頭；它不回應就強制結束。"""
    try:
        if child.stdin and not child.stdin.closed:
            child.stdin.write("stop\n")
            child.stdin.flush()
            child.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        child.wait(timeout=WORKER_EXIT_WAIT)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def _worker_scan_loop(command, guard, stop, events):
    """
    背景執行緒：啟動視覺子行程，讀它的輸出並轉成和 _scan_loop 一樣的畫面事件。
    子行程輸出不可信任：格式、欄位、型別不對的行一律忽略。同樣不碰任何視窗物件。
    """
    child = None
    try:
        child = subprocess.Popen(  # noqa: S603  固定的指令，沒有任何外部輸入
            list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # 讀取輸出的迴圈會卡在等下一行：子行程一時沒輸出時，要有人在收到停止後主動結束它，迴圈才讀得到結尾
        threading.Thread(target=lambda: (stop.wait(), _stop_worker(child)), daemon=True).start()
        for raw in child.stdout:
            if stop.is_set():
                return
            message = login_worker.parse_line(raw)
            if message is None:
                continue
            if message["t"] == "error":
                events.put(("error", message["message"]))
                return
            try:
                frame = login_worker.decode_frame(message["jpg"])
                step = face_login.LoginStep(**message["step"])
            except (ValueError, TypeError):
                continue
            remaining = guard.locked_for()
            if remaining > 0:                 # 鎖定期間不判斷：通知子行程清掉連續幀，這邊只顯示倒數
                events.put(("locked", frame, remaining))
                try:
                    child.stdin.write("reset\n")
                    child.stdin.flush()
                except (OSError, ValueError):
                    pass
                continue
            events.put(("step", frame, step))
        if not stop.is_set():
            events.put(("error", "登入用的視覺程式意外結束，請按「重試」。"))
    except Exception as e:
        events.put(("error", f"登入畫面發生錯誤：{type(e).__name__}"))
    finally:
        if child is not None:
            _stop_worker(child)


class LoginWindow(ctk.CTkToplevel):
    def __init__(self, master, camera_factory=open_camera, vision_factory=face_login.RealVision, guard=None,
                 isolate_vision=False, worker_command=WORKER_COMMAND):
        super().__init__(master)
        self._isolate_vision, self._worker_command = isolate_vision, worker_command
        self.title("管理後台登入")
        self.geometry("560x620")
        self.resizable(False, False)
        ui_theme.style_window(self)
        self.session = None
        self._camera_factory, self._vision_factory = camera_factory, vision_factory
        self._guard = guard or auth.LoginGuard(path=paths.LOGIN_LOCK_PATH, min_interval=auth.FAILURE_INTERVAL_SECONDS)
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._generation = 0
        self._thread = None
        self._photo = None
        self._closed = False

        ctk.CTkLabel(self, text="管理後台登入", font=ui_theme.font(ui_theme.FONT_SIZE_TITLE, bold=True),
                     text_color=ui_theme.pair("info")).pack(pady=(18, 2))
        self.hint = ui_theme.muted_label(self, "", wraplength=500, justify="left")
        self.hint.pack(padx=30, anchor="w")
        self.body = ctk.CTkFrame(self, fg_color="transparent", height=1)
        self.body.pack(fill="both", expand=True, padx=30, pady=10)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._grab()
        self._start()

    # --- 流程 ---

    def _grab(self):
        """設為模態；視窗尚未可見時不強制，仍可操作。"""
        try:
            if not self.winfo_viewable():   # 已經可見時再等一次永遠等不到「可見狀態改變」，會卡死
                self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass

    def _start(self):
        for child in self.body.winfo_children():
            child.destroy()
        if not db.list_admins():
            self._build_setup()
        else:
            self._build_scan()

    def _build_setup(self):
        self.hint.configure(text="目前系統還沒有任何管理者。請從已註冊的員工中選出第一位管理者（之後可以在「員工帳號管理」"
                                 "指定其他組員）。選定後，會用他的臉登入。")
        users = [u for u in account_admin.list_users() if u["is_active"]]
        if not users:
            ctk.CTkLabel(self.body, text="資料庫裡還沒有可用的員工，請先用手機 QR Code 註冊員工後再開啟後台。",
                         font=ui_theme.font(ui_theme.FONT_SIZE_INPUT), text_color=ui_theme.pair("warning"),
                         wraplength=480, justify="left").pack(pady=20)
            return
        labels = {f"{u['name']}（{u['employee_id']}）": u["id"] for u in users}
        ui_theme.section_title(self.body, "初次設定：選擇第一位管理者").pack(anchor="w", pady=(10, 6))
        self.setup_choice = ui_theme.combo(self.body, list(labels))
        self.setup_choice.pack(fill="x")
        self.setup_choice.set(next(iter(labels)))

        def confirm():
            label = self.setup_choice.get()
            if label not in labels or not ui_theme.confirm(self, "確認", f"要把「{label}」設為第一位管理者嗎？"):
                return
            self._grab()
            ok, msg = account_admin.set_role(labels[label], db.ROLE_ADMIN, operator="初次設定")
            if ok:
                self._start()
            else:
                ui_theme.show_message(self, "無法設定", msg, "warning")
                self._grab()

        self.setup_button = ui_theme.primary_button(self.body, "設為第一位管理者", confirm, height=40)
        self.setup_button.pack(fill="x", pady=14)

    def _build_scan(self):
        self.hint.configure(text="請正對鏡頭，保持畫面中只有你一個人，光線充足。連續驗證通過才會登入。")
        self._photo = self._placeholder()
        self.preview = ctk.CTkLabel(self.body, text="", image=self._photo)   # 先放同尺寸的底色圖，預留預覽空間
        self.preview.pack()
        self.status = ctk.CTkLabel(
            self.body, text="啟動鏡頭中…", font=ui_theme.font(ui_theme.FONT_SIZE_SECTION, bold=True),
            text_color=ui_theme.pair("text"), wraplength=480)
        self.status.pack(pady=(10, 4))
        self.bar = ctk.CTkProgressBar(self.body, progress_color=ui_theme.pair("success"),
                                      fg_color=ui_theme.pair("secondary"))
        self.bar.set(0)
        self.bar.pack(fill="x", pady=4)
        self.retry = ui_theme.secondary_button(self.body, "重試", self._start)
        self._stop.set()                  # 重試時先讓上一輪的背景執行緒結束
        self._stop = stop = threading.Event()
        self._generation += 1
        self._events = queue.Queue()
        if self._isolate_vision:
            target, args = _worker_scan_loop, (self._worker_command, self._guard, stop, self._events)
        else:
            target = _scan_loop
            args = (self._camera_factory, self._vision_factory, self._guard, stop, self._events)
        self._thread = threading.Thread(target=target, daemon=True, args=args)
        self._thread.start()
        self.after(POLL_MS, self._poll, self._generation, self._events)

    # --- 畫面執行緒 ---

    @staticmethod
    def _placeholder():
        """鏡頭畫面出現前的底色圖（亮暗色各一張）。"""
        from PIL import Image

        light, dark = (Image.new("RGB", PREVIEW_SIZE, ui_theme.color("secondary", m)) for m in ui_theme.MODES)
        return ctk.CTkImage(light_image=light, dark_image=dark, size=PREVIEW_SIZE)

    def _set_status(self, text, color_name="text"):
        """更新狀態文字與顏色（color_name 為 ui_theme 的色彩名稱）。"""
        self.status.configure(text=text, text_color=ui_theme.pair(color_name))

    def _show_frame(self, frame):
        try:
            import cv2
            from PIL import Image

            small = cv2.cvtColor(cv2.flip(cv2.resize(frame, PREVIEW_SIZE), 1), cv2.COLOR_BGR2RGB)
            self._photo = ctk.CTkImage(light_image=Image.fromarray(small), size=PREVIEW_SIZE)
            self.preview.configure(image=self._photo)
        except Exception:  # noqa: S110  預覽失敗不影響驗證
            pass

    def _poll(self, generation, events):
        if self._closed or not self.winfo_exists() or generation != self._generation:
            return
        latest = None
        try:
            while True:
                latest = events.get_nowait()
                if latest[0] == "step" and latest[2].status in (face_login.SUCCESS, face_login.FAILED):
                    break
        except queue.Empty:
            pass
        if latest is not None:
            self._handle(latest)
        if not self._closed:
            self.after(POLL_MS, self._poll, generation, events)

    def _handle(self, event):
        kind = event[0]
        if kind == "error":
            self._set_status(event[1], "danger")
            self._stop.set()
            if not self.retry.winfo_ismapped():
                self.retry.pack(pady=8)
            return
        self._show_frame(event[1])
        if kind == "locked":
            self._set_status(f"失敗次數過多，請 {int(event[2]) + 1} 秒後再試", "warning")
            self.bar.set(0)
            return
        step = event[2]
        self.bar.set(max(0.0, min(1.0, step.progress)))
        self._set_status(step.message, "success" if step.status == face_login.SUCCESS else "info")
        if step.distance is not None:
            print(f"[登入] 狀態 {step.status}，與管理者的距離 {step.distance}，姿勢 {step.pose}")
        elif step.status == face_login.NOT_FRONTAL:
            print(f"[登入] 還不是正面：{step.message}，姿勢 {step.pose}")
        if step.status == face_login.FAILED:
            if self._guard.counts_now():
                db.add_anomaly_event("login_failed", None, "管理者人臉登入失敗（不是活體，或不是管理者）")
                if self._guard.record_failure():
                    db.add_anomaly_event("login_locked", None, f"連續失敗，登入鎖定 {int(auth.LOCK_SECONDS)} 秒")
        elif step.status == face_login.SUCCESS:
            self._guard.record_success()
            self.session = auth.Session(step.admin_name)
            auth.save_session(self.session)
            print(f"[登入] {step.admin_name} 登入成功")
            self._finish()

    def _finish(self):
        self._closed = True
        self._stop.set()
        if self._thread is not None:
            # 背景執行緒先結束再銷毀視窗，Tk 物件才不會被別的執行緒回收而讓程式中止；子行程要多留時間釋放鏡頭
            self._thread.join(timeout=3 + (WORKER_EXIT_WAIT + 2 if self._isolate_vision else 0))
        self.grab_release()
        self.destroy()

    def _cancel(self):
        self.session = None
        self._finish()


def run_login(root, **kwargs):
    """顯示登入視窗並等待結果；成功回傳 Session，使用者關閉視窗回傳 None。"""
    window = LoginWindow(root, **kwargs)
    root.wait_window(window)
    return window.session
