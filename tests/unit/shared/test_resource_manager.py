"""
resource_manager 單元測試：模型的生命週期（建構時不載入、第一次使用才載入、可釋放、可再載入）、
閒置回收、被釘住時不回收、資源不足時驅逐其他元件、無法滿足時拒絕。
"""
import threading
import time

import pytest

from shared import resource_manager as rm


class Res(rm.ManagedResource):
    def __init__(self, name, manager, **kwargs):
        super().__init__(name, manager=manager, **kwargs)
        self.loaded, self.unloaded = 0, 0

    def _load(self):
        self.loaded += 1
        return {"model": self.name}

    def _unload(self, impl):
        self.unloaded += 1


@pytest.fixture
def mgr():
    return rm.ResourceManager(safety_margin_mb=0)


def test_nothing_is_loaded_at_construction(mgr):
    res = Res("a", mgr)
    assert res.state is rm.State.INITIALIZED and not res.is_loaded and res.loaded == 0


def test_first_use_loads_and_later_uses_reuse(mgr):
    res = Res("a", mgr)
    with res.use() as impl:
        assert impl == {"model": "a"}
        assert res.state is rm.State.EXECUTING and res.holder == threading.get_ident()
    assert res.state is rm.State.ACQUIRED and res.holder is None and res.last_used > 0
    with res.use():
        pass
    assert res.loaded == 1


def test_release_unloads_and_the_next_use_reloads(mgr):
    res = Res("a", mgr)
    with res.use():
        pass
    res.release("test")
    assert res.state is rm.State.RELEASED and not res.is_loaded and res.unloaded == 1
    res.release("again")
    assert res.unloaded == 1
    with res.use():
        pass
    assert res.loaded == 2


def test_a_failing_unload_still_marks_the_resource_released(mgr):
    class Broken(Res):
        def _unload(self, impl):
            raise RuntimeError("關閉失敗")

    res = Broken("a", mgr)
    with res.use():
        pass
    with pytest.raises(RuntimeError):
        res.release("test")
    assert res.state is rm.State.RELEASED and not res.is_loaded


def test_evict_releases_a_loaded_resource(mgr):
    res = Res("a", mgr)
    with res.use():
        pass
    assert res.evict() is True and res.unloaded == 1
    assert res.evict() is True and res.unloaded == 1


def test_evict_gives_up_while_another_thread_holds_the_lock(mgr):
    res = Res("a", mgr)
    entered, leave = threading.Event(), threading.Event()

    def hold():
        with res.use():
            entered.set()
            leave.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    entered.wait(5)
    assert res.evict(timeout=0.1) is False
    leave.set()
    worker.join(5)


def test_pinned_resources_survive_idle_reaping_until_closed(mgr):
    res = Res("a", mgr, idle_ttl=1)
    res.open()
    res.open()
    res.last_used = time.time() - 100
    res.reap_if_idle()
    assert res.is_loaded
    res.close()
    res.reap_if_idle()
    assert res.is_loaded
    res.close()
    assert not res.is_loaded and res.unloaded == 1


def test_idle_resources_are_reaped_after_their_ttl(mgr):
    res = Res("a", mgr, idle_ttl=30)
    with res.use():
        pass
    res.reap_if_idle()
    assert res.is_loaded
    res.last_used = time.time() - 100
    res.reap_if_idle()
    assert not res.is_loaded


def test_resources_without_a_ttl_are_never_reaped(mgr):
    res = Res("a", mgr)
    with res.use():
        pass
    res.last_used = time.time() - 10_000
    res.reap_if_idle()
    assert res.is_loaded


def test_session_context_manager_loads_then_releases(mgr):
    res = Res("a", mgr)
    with res.session():
        assert res.is_loaded
    assert not res.is_loaded


def test_fits_compares_free_memory_minus_margin_with_the_estimate(monkeypatch):
    manager = rm.ResourceManager(safety_margin_mb=100)
    res = Res("a", manager, est_ram_mb=500)
    monkeypatch.setattr(rm, "_free_ram_mb", lambda: 600)
    assert manager._fits(res, 100) is True
    monkeypatch.setattr(rm, "_free_ram_mb", lambda: 599)
    assert manager._fits(res, 100) is False
    assert manager._fits(res, 0) is True


def test_unknown_free_memory_is_treated_as_enough(monkeypatch, mgr):
    monkeypatch.setattr(rm, "_free_ram_mb", lambda: None)
    monkeypatch.setattr(rm, "_free_vram_mb", lambda: None)
    assert mgr._fits(Res("a", mgr, est_ram_mb=10**6, est_vram_mb=10**6), 0) is True


def test_resources_that_need_nothing_never_query_free_memory(monkeypatch, mgr):
    def boom():
        raise AssertionError("不該查詢剩餘資源")

    monkeypatch.setattr(rm, "_free_ram_mb", boom)
    monkeypatch.setattr(rm, "_free_vram_mb", boom)
    assert mgr._fits(Res("a", mgr), 0) is True


def test_loading_evicts_another_resource_when_memory_is_short(monkeypatch, mgr):
    victim = Res("victim", mgr, est_ram_mb=900)
    newcomer = Res("newcomer", mgr, est_ram_mb=500)
    with victim.use():
        pass
    monkeypatch.setattr(rm, "_free_ram_mb", lambda: 100 if victim.is_loaded else 1000)
    with newcomer.use():
        pass
    assert victim.unloaded == 1 and newcomer.is_loaded and not victim.is_loaded


def test_loading_is_denied_when_nothing_can_be_evicted(monkeypatch, mgr):
    monkeypatch.setattr(rm, "_free_ram_mb", lambda: 100)
    with pytest.raises(rm.ResourceDenied, match="newcomer"):
        mgr.request(Res("newcomer", mgr, est_ram_mb=500), max_wait=0.2)


def test_a_low_safety_margin_alone_does_not_block_loading(monkeypatch, capsys):
    manager = rm.ResourceManager(safety_margin_mb=512)
    monkeypatch.setattr(rm, "_free_ram_mb", lambda: 600)
    manager.request(Res("a", manager, est_ram_mb=500), max_wait=0.2)
    assert "仍放行" in capsys.readouterr().out


def test_victim_choice_prefers_idle_and_least_recently_used(mgr):
    old, recent, busy = Res("old", mgr), Res("recent", mgr), Res("busy", mgr)
    for res in (old, recent, busy):
        with res.use():
            pass
    old.last_used, recent.last_used, busy.last_used = 10.0, 20.0, 1.0
    busy.state = rm.State.EXECUTING
    assert mgr._pick_victim(set()) is old
    assert mgr._pick_victim({"old"}) is recent
    assert mgr._pick_victim({"old", "recent"}) is busy
    assert mgr._pick_victim({"old", "recent", "busy"}) is None


def test_resources_in_use_by_the_current_thread_are_only_victims_if_self_evictable(mgr):
    class SelfEvictable(Res):
        self_evictable = True

    plain, flexible = Res("plain", mgr), SelfEvictable("flexible", mgr)
    with plain.use():
        with flexible.use():
            assert mgr._pick_victim(set()) is flexible
            assert mgr._pick_victim({"flexible"}) is None


def test_evict_all_releases_every_loaded_resource(mgr):
    a, b = Res("a", mgr), Res("b", mgr)
    with a.use():
        pass
    mgr.evict_all()
    assert a.unloaded == 1 and b.unloaded == 0


def test_the_global_manager_is_a_singleton():
    assert rm.get_manager() is rm.get_manager()


def test_base_class_requires_a_load_implementation(mgr):
    with pytest.raises(NotImplementedError):
        rm.ManagedResource("x", manager=mgr)._load()
