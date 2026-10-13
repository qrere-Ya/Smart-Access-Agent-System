"""
resource_manager.py：中央資源協調器，管理高耗能模型的載入與釋放，避免記憶體與顯存不足。

主要功能：
  - ManagedResource：高耗能元件的基底類別，子類只需實作 _load / _unload
      生命週期：INITIALIZED（建構時不載入）→ ACQUIRED（第一次使用才載入）→ EXECUTING（使用中）→ RELEASED
  - ResourceManager：載入前檢查剩餘記憶體與顯存，不足時驅逐閒置最久的元件；背景執行緒回收閒置逾時的元件
  - 每個 Python 行程一個單例（get_manager），不做跨行程協調

串接模組：
  - vision_core、security、audio_player、rag_resources：各自的模型繼承 ManagedResource
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
    """驅逐完所有可驅逐元件後，可用資源仍不足以載入。"""


def cuda_available():
    """torch 是否偵測到可用的 CUDA"""
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def _free_vram_mb():
    """取得整張顯卡的剩餘顯存（MB），優先用 nvidia-smi 以包含其他行程的占用，取不到回傳 None"""
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
    """取得系統剩餘記憶體（MB），取不到回傳 None"""
    try:
        import psutil
        return psutil.virtual_memory().available // 2**20
    except Exception:
        return None


def _purge():
    """清空 CUDA 快取並強制垃圾回收"""
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
        self._lock = threading.Lock()  # 只保護登錄表，呼叫 evict() 時不可持有
        self._resources = {}
        self._reaper = None

    def register(self, res):
        """登錄元件；有閒置逾時設定時啟動背景回收執行緒"""
        with self._lock:
            self._resources[res.name] = res
            if res.idle_ttl and self._reaper is None:
                self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
                self._reaper.start()

    def request(self, res, max_wait=6.0):
        """
        載入前申請資源：不足時驅逐其他元件，最多重試 max_wait 秒（受害者正在執行時需要等待）。
        連實際需要量都不夠就拋出 ResourceDenied；只有安全緩衝不足則放行並警告。
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
        # 安全緩衝只是軟門檻；實際需要量放不下才拒絕
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
        """目前剩餘資源扣掉 margin 後是否放得下 res；需求為 0 的項目不查詢"""
        for need, free_fn in ((res.est_vram_mb, _free_vram_mb), (res.est_ram_mb, _free_ram_mb)):
            if not need:
                continue
            free = free_fn()
            if free is not None and free - margin < need:
                return False
        return True

    def _pick_victim(self, tried):
        """挑選要驅逐的元件：已載入、非本執行緒使用中，優先閒置且最久沒用的"""
        me = threading.get_ident()
        with self._lock:
            cands = [r for r in self._resources.values()
                     if r.name not in tried and r.is_loaded and (r.holder != me or r.self_evictable)]
        cands.sort(key=lambda r: (r.state is State.EXECUTING, r.last_used))
        return cands[0] if cands else None

    def report_released(self, res):
        """元件釋放後回報"""
        print(f"[ResourceManager] {res.name} 已釋放", flush=True)

    def evict_all(self, reason="global pressure"):
        """資源吃緊時，不論閒置與否全部驅逐"""
        with self._lock:
            targets = [r for r in self._resources.values() if r.is_loaded]
        for r in targets:
            r.evict()

    def _reap_loop(self):
        """背景迴圈：每 5 秒檢查並回收閒置逾時的元件"""
        while True:
            time.sleep(5)
            with self._lock:
                targets = list(self._resources.values())
            for r in targets:
                r.reap_if_idle()


_MANAGER = None
_MANAGER_LOCK = threading.Lock()


def get_manager():
    """取得本行程唯一的 ResourceManager"""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = ResourceManager()
        return _MANAGER


class ManagedResource:
    """
    高耗能元件基底類別。子類實作：
      _load()        回傳實體物件（此時才真正載入）
      _unload(impl)  可選，明確關閉或搬離 GPU
    建構時不載入任何東西。
    """
    est_vram_mb = 0
    est_ram_mb = 0
    idle_ttl = None  # 閒置回收秒數，None 表示不做閒置回收
    # True 表示即使本執行緒正在使用也允許被驅逐，適用於驅逐後下次呼叫會自動重新載入的元件（如 Ollama 的模型）
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

    @contextmanager
    def use(self):
        """借用元件：尚未載入就先申請資源並載入，離開區塊後記錄最後使用時間"""
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

    def open(self):
        """長時間持有（如相機迴圈）：載入並釘住，不受閒置回收，但仍可被驅逐"""
        with self._lock:
            self._pins += 1
        with self.use():
            pass

    def close(self):
        """結束長時間持有，沒有其他持有者時釋放"""
        with self._lock:
            self._pins = max(0, self._pins - 1)
            if self._pins == 0:
                self._release_locked("close")

    @contextmanager
    def session(self):
        """context manager 版本的 open / close"""
        self.open()
        try:
            yield self
        finally:
            self.close()

    def release(self, reason="done"):
        """無條件釋放"""
        with self._lock:
            self._release_locked(reason)

    def evict(self, timeout=2.0):
        """被動驅逐：等正在執行的推論結束後釋放，等不到鎖回傳 False"""
        if not self._lock.acquire(timeout=timeout):
            return False
        try:
            self._release_locked("evicted")
            return True
        finally:
            self._lock.release()

    def reap_if_idle(self):
        """閒置超過 idle_ttl 且沒有被釘住時釋放"""
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
        """釋放流程：解除指標、清 CUDA 快取、強制 GC、回報"""
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
