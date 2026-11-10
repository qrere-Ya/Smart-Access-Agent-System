"""PassThroughMonitor 的單元測試：以模擬幀序列驗證觸發、不觸發、冷卻與去重規則。"""
from access.pass_through import PassThroughMonitor

W = 1000
BIG = (100, 0, 500, 600)    # 寬 400 = 40%
SMALL = (100, 0, 200, 300)  # 寬 100 = 10%


def run(mon, start, end, boxes, step=0.5, **kw):
    """從 start 到 end 每 step 秒餵一幀，回傳觸發時間清單。"""
    hits, t = [], start
    while t <= end:
        if mon.update(t, boxes, W, **kw):
            hits.append(t)
        t += step
    return hits


def test_pass_without_scan_triggers_once():
    assert run(PassThroughMonitor(), 0, 10, [BIG]) == [3.0]


def test_scan_prevents_trigger():
    mon = PassThroughMonitor()
    assert run(mon, 0, 1, [BIG]) == []
    mon.note_scan(1.0)
    assert run(mon, 1.5, 10, [BIG]) == []


def test_scan_before_box_appears_still_counts():
    mon = PassThroughMonitor()
    mon.note_scan(0.0)
    assert run(mon, 0.5, 10, [BIG]) == []


def test_small_box_never_triggers():
    assert run(PassThroughMonitor(), 0, 20, [SMALL]) == []


def test_brief_appearance_does_not_trigger():
    mon = PassThroughMonitor()
    assert run(mon, 0, 2, [BIG]) == []
    assert run(mon, 2.5, 8, []) == []


def test_cooldown_blocks_next_person_then_allows():
    mon = PassThroughMonitor()
    assert run(mon, 0, 5, [BIG]) == [3.0]
    run(mon, 5.5, 8, [])  # 人離開
    assert run(mon, 8.5, 20, [BIG]) == []  # 冷卻中（30 秒）
    run(mon, 20.5, 25, [])
    assert run(mon, 40, 50, [BIG]) == [43.0]


def test_short_detection_flicker_is_same_stay():
    mon = PassThroughMonitor()
    hits = run(mon, 0, 1.5, [BIG]) + run(mon, 2, 2.5, []) + run(mon, 3, 6, [BIG])
    assert hits == [3.0]


def test_tailgate_frame_suppresses_duplicate():
    mon = PassThroughMonitor()
    assert mon.update(0, [BIG, BIG], W, tailgate=True) is False
    assert run(mon, 0.5, 10, [BIG, BIG]) == []


def test_two_people_without_tailgate_record_still_triggers():
    assert run(PassThroughMonitor(), 0, 5, [BIG, BIG]) == [3.0]


def test_leaving_and_returning_after_scan_is_new_stay():
    mon = PassThroughMonitor(cooldown_seconds=0)
    mon.note_scan(0.0)
    run(mon, 0.5, 2, [BIG])
    run(mon, 3, 5, [])  # 離開
    assert run(mon, 6, 10, [BIG]) == [9.0]
