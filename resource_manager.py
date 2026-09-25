"""
中央資源協調器 (Resource Manager) —— 依 resource-lifecycle-standard v1.1.0 實作。

- 每個 Python 行程一個單例（get_manager()）。跨行程協調不在規範內，這裡不自創 IPC。
- 生命週期四階段：INITIALIZED -> ACQUIRED -> EXECUTING -> RELEASED。
- 所有高耗能組件繼承 ManagedResource，只需實作 _load() / _unload()。
"""
import gc
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from enum import Enum


class State(Enum):
    INITIALIZED = "initialized"
    ACQUIRED = "acquired"
    EXECUTING = "executing"
    RELEASED = "released"


class ResourceDenied(RuntimeError):
    """驅逐完所有可驅逐組件後，可用資源仍不足以配置。"""


def cuda_available():
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def _free_vram_mb():
    # 優先用 nvidia-smi：回報的是「整張顯卡」剩餘量（含其他行程如 Ollama 的占用），
    # 而且不必在本行程建立 CUDA context（每個 context 本身就吃 300~500MB 顯存）。
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        pass
    torch = sys.modules.get("torch")
    try:
        if torch is not None and torch.cuda.is_available():
            return torch.cuda.mem_get_info()[0] // 2**20
    except Exception:
        pass
    return None


def _free_ram_mb():
    try:
        import psutil
        return psutil.virtual_memory().available // 2**20
    except Exception:
        return None


def _purge():
    """釋放鏈後半段：清空 CUDA 快取 -> 強制 GC。"""
    torch = sys.modules.get("torch")
    try:
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    gc.collect()


class ResourceManager:
    def __init__(self, safety_margin_mb=512):
        self.safety_margin_mb = safety_margin_mb
        self._lock = threading.Lock()  # 只保護登錄表；呼叫 evict() 時絕不持有
        self._resources = {}
        self._reaper = None

    def register(self, res):
        with self._lock:
            self._resources[res.name] = res
            if res.idle_ttl and self._reaper is None:
                self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
                self._reaper.start()

    # ---- B. Acquired：申報預估開銷、取得許可（不足則驅逐他人）----
    def request(self, res, max_wait=6.0):
        """
        【2026-09-22 修正】原本這裡呼叫 victim.evict() 之後完全不管回傳值：evict() 內部鎖
        有 timeout=2.0 秒，如果被選中的受害者當下正在執行中（例如 Ollama 正在生成一段
        較長的回答，這在先前遇到的 CPU fallback 情況下很常見地會超過 2 秒），evict() 會
        逾時直接放棄、回傳 False，但這裡完全沒檢查，照樣把它算進「已經試過」，如果剛好
        沒有其他候選人可以驅逐，就會在受害者其實只是還在忙、馬上就執行完了的情況下，
        誤判成「資源真的不足」拋出 ResourceDenied。

        改成在 max_wait 秒（預設 6 秒，讓同一個受害者的 2 秒鎖逾時大約可以被重試 2~3 次）
        的預算內持續重試，逾時才真的視為這一輪驅逐失敗；只有預算用完、需求仍然不夠，
        才會走到下面拋出 ResourceDenied 或放行警告——這個專案目前只有 bge-m3／Ollama
        兩個受管資源，實務上「候選人」最多就一個，重試同一個候選人已經足夠；未來如果
        受管資源變多，_pick_victim() 本來就會優先挑「沒在執行中、最久沒用」的候選人，
        只有全部候選人都在執行中時才會退而重試同一個。
        """
        deadline = time.monotonic() + max_wait
        while not self._fits(res, self.safety_margin_mb):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            victim = self._pick_victim({res.name})
            if victim is None:
                break
            victim.evict(timeout=min(2.0, remaining))
        # 已經驅逐完所有能驅逐的：安全緩衝（margin）只是「軟」門檻，緩衝不夠但「實際需要量」
        # 還放得下就放行並警告；連實際需要量都不夠才真的拒絕（載入下去確定會 OOM）。
        if not self._fits(res, 0):
            raise ResourceDenied(
                f"[ResourceManager] '{res.name}' 需要 VRAM≈{res.est_vram_mb}MB / RAM≈{res.est_ram_mb}MB，"
                f"可驅逐的組件都已驅逐或等待逾時（已重試 {max_wait:.0f} 秒），目前可用 "
                f"VRAM={_free_vram_mb()}MB / RAM={_free_ram_mb()}MB，仍不足。"
                f"請關閉其他占用記憶體的程式（例如另一個服務或瀏覽器分頁）後重試。")
        if not self._fits(res, self.safety_margin_mb):
            print(f"[ResourceManager] ⚠️ 載入 '{res.name}' 後剩餘緩衝低於 {self.safety_margin_mb}MB，仍放行。",
                  flush=True)

    def _fits(self, res, margin):
        # 【2026-09-22 效能】原本不管 need 是不是 0，都會無條件呼叫 _free_vram_mb()／
        # _free_ram_mb()（tuple 字面值在建立當下就會把兩個函式都執行一次）——對
        # OllamaModelResource 這種 est_vram_mb=est_ram_mb=0 的資源（設計上就是「純被驅逐者」，
        # 見 rag_resources.py 說明）來說，等於每次 acquire 都白白多 spawn 一次 nvidia-smi
        # 子行程。改成 need 是 0 就直接跳過，不呼叫對應的 free-mem 函式。
        for need, free_fn in ((res.est_vram_mb, _free_vram_mb), (res.est_ram_mb, _free_ram_mb)):
            if not need:
                continue
            free = free_fn()
            if free is not None and free - margin < need:
                return False
        return True

    def _pick_victim(self, tried):
        me = threading.get_ident()
        with self._lock:
            cands = [r for r in self._resources.values()
                     if r.name not in tried and r.is_loaded and (r.holder != me or r.self_evictable)]
        cands.sort(key=lambda r: (r.state is State.EXECUTING, r.last_used))  # 閒置且最久沒用的優先
        return cands[0] if cands else None

    # ---- D. Released：組件回報 ----
    def report_released(self, res):
        print(f"[ResourceManager] {res.name} 已釋放", flush=True)

    # ---- 全域資源不足警告：不論閒置與否，全部強制降級 ----
    def evict_all(self, reason="global pressure"):
        with self._lock:
            targets = [r for r in self._resources.values() if r.is_loaded]
        for r in targets:
            r.evict()

    def _reap_loop(self):
        while True:
            time.sleep(5)
            with self._lock:
                targets = list(self._resources.values())
            for r in targets:
                r.reap_if_idle()


_MANAGER = None
_MANAGER_LOCK = threading.Lock()


def get_manager():
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = ResourceManager()
        return _MANAGER


class ManagedResource:
    """
    子類只需實作：
      _load()          -> 回傳實體物件（此時才真正載入模型/資料）
      _unload(impl)    -> 可選，明確關閉（例如 .close()、offload）；不需自己 del/gc
    建構時（Initialized）絕不載入任何東西。
    """
    est_vram_mb = 0
    est_ram_mb = 0
    idle_ttl = None  # 秒；None＝不做閒置回收（只靠 release/evict）
    # True＝就算「當前執行緒自己正在 use() 它」也允許被驅逐。只適用於「占用不在本行程內、
    # 驅逐後下次呼叫會自動重新載入」的組件（例如 Ollama 伺服器端的模型）。
    self_evictable = False

    def __init__(self, name, est_vram_mb=None, est_ram_mb=None, idle_ttl=None, manager=None):
        self.name = name
        if est_vram_mb is not None:
            self.est_vram_mb = est_vram_mb
        if est_ram_mb is not None:
            self.est_ram_mb = est_ram_mb
        if idle_ttl is not None:
            self.idle_ttl = idle_ttl
        self.state = State.INITIALIZED
        self.last_used = 0.0
        self.holder = None  # 目前正在 use() 內的執行緒
        self._impl = None
        self._pins = 0
        self._lock = threading.RLock()
        self._manager = manager or get_manager()
        self._manager.register(self)

    @property
    def is_loaded(self):
        return self._impl is not None

    def _load(self):
        raise NotImplementedError

    def _unload(self, impl):
        pass

    # ---- A/B/C：借用（自動申請 -> 執行）----
    @contextmanager
    def use(self):
        with self._lock:
            if self._impl is None:
                self._manager.request(self)
                self._impl = self._load()
                self.state = State.ACQUIRED
            self.state = State.EXECUTING
            self.holder = threading.get_ident()
            try:
                yield self._impl
            finally:
                self.holder = None
                self.last_used = time.time()
                if self._impl is not None:
                    self.state = State.ACQUIRED

    # ---- 長時間持有（如相機迴圈）：不受閒置回收，仍可被驅逐 ----
    def open(self):
        with self._lock:
            self._pins += 1
        with self.use():
            pass

    def close(self):
        with self._lock:
            self._pins = max(0, self._pins - 1)
            if self._pins == 0:
                self._release_locked("close")

    @contextmanager
    def session(self):
        self.open()
        try:
            yield self
        finally:
            self.close()

    # ---- D. Released：解除指標 -> 清空 CUDA 快取 -> 強制 GC -> 回報 ----
    def release(self, reason="done"):
        with self._lock:
            self._release_locked(reason)

    def evict(self, timeout=2.0):
        """被動驅逐：不論閒置與否一律釋放；只等正在跑的那一次推論結束（避免砍到半途）。"""
        if not self._lock.acquire(timeout=timeout):
            return False
        try:
            self._release_locked("evicted")
            return True
        finally:
            self._lock.release()

    def reap_if_idle(self):
        if not self.idle_ttl or self._pins or not self.is_loaded:
            return
        if time.time() - self.last_used < self.idle_ttl:
            return
        if self._lock.acquire(blocking=False):
            try:
                if not self._pins and time.time() - self.last_used >= self.idle_ttl:
                    self._release_locked("idle ttl")
            finally:
                self._lock.release()

    def _release_locked(self, reason):
        impl, self._impl = self._impl, None
        if impl is None:
            return
        try:
            self._unload(impl)
        finally:
            del impl
            _purge()
            self.state = State.RELEASED
            self._manager.report_released(self)
