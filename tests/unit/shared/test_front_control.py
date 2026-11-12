"""後台對前台的遙控設定：預設允許、壞檔當允許、只有明確寫 false 才停止、原子寫入。"""
import json

from shared import front_control


def test_missing_or_broken_files_mean_allowed(tmp_path):
    path = str(tmp_path / "control.json")
    assert front_control.recognition_enabled(path) is True                        # 沒檔案
    for text in ("壞掉", "[]", "null", '{"recognition_enabled": "no"}', '{"recognition_enabled": 0}', "{}"):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        assert front_control.recognition_enabled(path) is True, text


def test_only_an_explicit_false_stops_recognition_and_it_round_trips(tmp_path):
    path = str(tmp_path / "sub" / "control.json")                                 # 資料夾不存在也能寫
    front_control.set_recognition_enabled(False, path)
    assert front_control.recognition_enabled(path) is False
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"recognition_enabled": False}
    front_control.set_recognition_enabled(True, path)
    assert front_control.recognition_enabled(path) is True
    assert not (tmp_path / "sub" / "control.json.tmp").exists()                   # 暫存檔已取代
