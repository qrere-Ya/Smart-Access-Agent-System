"""
login_window.py：管理後台的人臉登入畫面（ttkbootstrap）。

主要功能：
  - 開鏡頭預覽，背景執行緒逐幀判斷（face_login.FaceLoginVerifier），畫面定時取最新結果顯示提示與進度
  - 通過就建立工作階段（auth.Session）並寫入工作階段檔，後台才會打開；關閉視窗代表放棄登入
  - 失敗與鎖定會寫入異常事件（login_failed、login_locked）；失敗 5 次鎖 60 秒，倒數期間不判斷
  - 還沒有任何管理者時進入「初次設定」：在本機從啟用中的員工挑一位設為第一位管理者，再用他的臉登入
  - 鏡頭打不開（例如被門禁終端機占用）會說明原因並提供重試；視窗關閉時釋放鏡頭與視覺模型
  鏡頭與視覺函式可由外部傳入，測試不需要真的鏡頭。

串接模組：
  - face_login：逐幀判斷；auth：失敗計數與工作階段
  - account_admin、database_mgr：管理者名單與設定第一位管理者、記錄異常事件
  - backend_main：啟動時呼叫 run_login
"""
import queue
import threading
import time
from tkinter import messagebox

import ttkbootstrap as ttk
from ttkbootstrap.constants import BOTH, DANGER, LEFT, LIGHT, PRIMARY, SUCCESS, W, WARNING, X

from admin import account_admin, face_login
from shared import auth
from shared import database_mgr as db

PREVIEW_SIZE = (480, 360)
POLL_MS = 40
FONT = "微軟正黑體"


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


class LoginWindow(ttk.Toplevel):
    def __init__(self, master, camera_factory=open_camera, vision_factory=face_login.RealVision, guard=None):
        super().__init__(master)
        self.title("管理後台登入")
        self.geometry("560x680")
        self.resizable(False, False)
        self.session = None
        self._camera_factory, self._vision_factory = camera_factory, vision_factory
        self._guard = guard or auth.LoginGuard()
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._generation = 0
        self._thread = None
        self._photo = None
        self._closed = False

        ttk.Label(self, text="🔐 管理後台登入", font=(FONT, 18, "bold"), bootstyle=PRIMARY).pack(pady=(18, 2))
        self.hint = ttk.Label(self, text="", font=(FONT, 10), bootstyle=LIGHT, wraplength=500, justify=LEFT)
        self.hint.pack(padx=30, anchor=W)
        self.body = ttk.Frame(self)
        self.body.pack(fill=BOTH, expand=True, padx=30, pady=10)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()
        self._start()

    # --- 流程 ---

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
            ttk.Label(self.body, text="資料庫裡還沒有可用的員工，請先用手機 QR Code 註冊員工後再開啟後台。",
                      font=(FONT, 11), bootstyle=WARNING, wraplength=480).pack(pady=20)
            return
        labels = {f"{u['name']}（{u['employee_id']}）": u["id"] for u in users}
        ttk.Label(self.body, text="初次設定：選擇第一位管理者", font=(FONT, 12, "bold")).pack(anchor=W, pady=(10, 6))
        self.setup_choice = ttk.Combobox(self.body, values=list(labels), state="readonly", font=(FONT, 12))
        self.setup_choice.pack(fill=X)
        self.setup_choice.current(0)

        def confirm():
            label = self.setup_choice.get()
            if not label or not messagebox.askyesno("確認", f"要把「{label}」設為第一位管理者嗎？", parent=self):
                return
            ok, msg = account_admin.set_role(labels[label], db.ROLE_ADMIN, operator="初次設定")
            if ok:
                self._start()
            else:
                messagebox.showwarning("無法設定", msg, parent=self)

        self.setup_button = ttk.Button(self.body, text="設為第一位管理者", bootstyle=SUCCESS, command=confirm)
        self.setup_button.pack(fill=X, pady=14, ipady=6)

    def _build_scan(self):
        self.hint.configure(text="請正對鏡頭，保持畫面中只有你一個人，光線充足。連續驗證通過才會登入。")
        self.preview = ttk.Label(self.body)
        self.preview.pack()
        self.status = ttk.Label(self.body, text="啟動鏡頭中…", font=(FONT, 13, "bold"))
        self.status.pack(pady=(10, 4))
        self.bar = ttk.Progressbar(self.body, maximum=100, bootstyle=SUCCESS)
        self.bar.pack(fill=X, pady=4)
        self.retry = ttk.Button(self.body, text="重試", command=self._start)
        self._stop.set()                  # 重試時先讓上一輪的背景執行緒結束
        self._stop = stop = threading.Event()
        self._generation += 1
        self._events = queue.Queue()
        self._thread = threading.Thread(
            target=_scan_loop, daemon=True,
            args=(self._camera_factory, self._vision_factory, self._guard, stop, self._events))
        self._thread.start()
        self.after(POLL_MS, self._poll, self._generation, self._events)

    # --- 畫面執行緒 ---

    def _show_frame(self, frame):
        try:
            import cv2
            from PIL import Image, ImageTk

            small = cv2.cvtColor(cv2.flip(cv2.resize(frame, PREVIEW_SIZE), 1), cv2.COLOR_BGR2RGB)
            self._photo = ImageTk.PhotoImage(Image.fromarray(small))
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
            self.status.configure(text=event[1], bootstyle=DANGER, wraplength=480)
            self._stop.set()
            if not self.retry.winfo_ismapped():
                self.retry.pack(pady=8)
            return
        self._show_frame(event[1])
        if kind == "locked":
            self.status.configure(text=f"失敗次數過多，請 {int(event[2]) + 1} 秒後再試", bootstyle=WARNING)
            self.bar.configure(value=0)
            return
        step = event[2]
        self.bar.configure(value=step.progress * 100)
        self.status.configure(text=step.message, bootstyle=SUCCESS if step.status == face_login.SUCCESS else PRIMARY)
        if step.distance is not None:
            print(f"[登入] 狀態 {step.status}，與管理者的距離 {step.distance}")
        if step.status == face_login.FAILED:
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
            self._thread.join(timeout=3)   # 背景執行緒先結束再銷毀視窗，Tk 物件才不會被別的執行緒回收而讓程式中止
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
