"""後台下達前台自動驗證：請求編號越來越大、報告存檔會蓋掉敏感字串、等待逾時回 None、協定驗證擋掉壞格式。"""
import pytest

from admin import front_verify_report as fvr
from shared import front_control, paths
from shared import link_protocol as lp


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "FRONT_CONTROL_PATH", str(tmp_path / "control.json"))
    monkeypatch.setattr(paths, "RUNTIME_DIR", str(tmp_path))


def test_request_numbers_only_grow_and_keep_other_switches(tmp_path):
    front_control.set_talk_enabled(True)
    assert front_control.verify_request() == 0
    first = front_control.request_verify(now=1000)
    assert front_control.request_verify(now=500) == first + 1               # 時鐘倒退也不會變小
    assert front_control.talk_enabled() is True


def test_save_hides_secrets_and_read_returns_none_before_arrival():
    assert fvr.read(1) is None
    path = fvr.save(1, ["通過", "金鑰 " + "A" * 50], remote_addr="169.254.1.2")
    text = fvr.read(1)
    assert path.endswith("前台自動_1.md") and "通過" in text and "A" * 50 not in text and "169.254.1.2" in text


def test_ask_returns_the_report_when_it_arrives_and_none_on_timeout():
    ticks = iter(range(0, 10000, 5))
    request_id, text = fvr.ask(wait_seconds=20, clock=lambda: next(ticks), sleep=lambda s: None, say=lambda m: None)
    assert text is None and request_id == front_control.verify_request()
    fvr.save(request_id + 1000, ["x"])

    def arrive(_s):
        fvr.save(front_control.verify_request(), ["Smart_RAG 知識庫問答：通過"])

    ticks = iter(range(0, 10000, 5))
    rid, text = fvr.ask(wait_seconds=100, clock=lambda: next(ticks), sleep=arrive, say=lambda m: None)
    assert "Smart_RAG 知識庫問答：通過" in text


@pytest.mark.parametrize("bad", [None, [], {"request": 0, "lines": ["x"]}, {"request": True, "lines": ["x"]},
                                 {"request": 1, "lines": []}, {"request": 1, "lines": [1]},
                                 {"request": 1, "lines": ["x"] * 61}])
def test_malformed_reports_are_rejected(bad):
    with pytest.raises(lp.ProtocolError):
        lp.validate_verify_report(bad)


def test_valid_reports_are_trimmed_and_cleaned():
    request, lines = lp.validate_verify_report({"request": 3, "lines": ["a\x00b\n", "x" * 999], "extra": 1})
    assert request == 3 and lines[0] == "ab" and len(lines[1]) == lp.MAX_REPORT_LINE
