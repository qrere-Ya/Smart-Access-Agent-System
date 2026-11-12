"""前台自我檢查：角色與通行碼、模型檔、後台連線（連不上只警告）。"""
import os

import pytest

from access import front_check as fc
from shared import role_config as rc

TOKEN = "oGo0iq6_xu9Jp7cwTM0ttP-BTbTQ9Zefin8GaJMQpyc"


def levels(results):
    return [level for level, _ in results]


def test_role_check_fails_on_standalone_and_needs_the_token(monkeypatch, tmp_path):
    from shared import paths
    monkeypatch.setattr(paths, "ROLE_PATH", str(tmp_path / "role.json"))
    monkeypatch.delenv(rc.TOKEN_ENV, raising=False)
    monkeypatch.delenv(rc.ROLE_ENV, raising=False)
    monkeypatch.setattr(fc.lan_discovery, "auto_address", lambda: "169.254.1.1")
    assert levels(fc.check_role()) == ["FAIL"]
    rc.save(rc.LinkSettings(rc.FRONT, back_host="192.168.50.1", back_fingerprint="ab" * 32))
    assert levels(fc.check_role()) == ["OK", "OK", "FAIL"]                # 角色對了、網路線有位址，但還沒設通行碼
    monkeypatch.setenv(rc.TOKEN_ENV, TOKEN)
    assert levels(fc.check_role()) == ["OK", "OK", "OK"]

    def no_address():
        raise fc.lan_discovery.DiscoveryError("網路線還沒有位址")
    monkeypatch.setattr(fc.lan_discovery, "auto_address", no_address)
    assert levels(fc.check_role()) == ["OK", "WARN", "OK"]                # 網路線沒插好只是警告


def test_file_check_names_every_missing_model(tmp_path):
    results = fc.check_files(models_dir=str(tmp_path))
    missing = [text for level, text in results if level == "FAIL"]
    assert len(missing) == len(fc.REQUIRED_MODELS)
    for name in fc.REQUIRED_MODELS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    assert "FAIL" not in levels(fc.check_files(models_dir=str(tmp_path)))


def test_back_check_only_warns_when_the_back_is_unreachable(monkeypatch, tmp_path):
    from shared import paths
    monkeypatch.setattr(paths, "ROLE_PATH", str(tmp_path / "role.json"))
    monkeypatch.setenv(rc.TOKEN_ENV, TOKEN)
    monkeypatch.delenv(rc.ROLE_ENV, raising=False)
    rc.save(rc.LinkSettings(rc.FRONT, back_host="127.0.0.1", back_port=59999, bind_host="127.0.0.1",
                             back_fingerprint="ab" * 32))
    assert levels(fc.check_back()) == ["WARN"]
    assert os.environ[rc.TOKEN_ENV] == TOKEN


class _FakeSession:
    """假的 onnxruntime session：記錄呼叫次數，可設定成推論時出錯。"""

    class _Meta:
        name, shape = "input", [1, 3, "batch", 80]

    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def get_inputs(self):
        return [self._Meta()]

    def run(self, _outputs, feed):
        if self.fail:
            raise RuntimeError("cudnn")
        assert feed["input"].shape == (1, 3, 1, 80)               # 不固定的維度當 1
        self.calls += 1


def test_time_inference_warms_up_once_and_runs_the_requested_times():
    session = _FakeSession()
    assert fc.time_inference(session, runs=5) >= 0
    assert session.calls == 6                                      # 1 次暖機＋5 次計時


def test_a_failing_inference_is_raised_so_the_check_can_warn():
    with pytest.raises(RuntimeError):
        fc.time_inference(_FakeSession(fail=True), runs=2)


def test_gpu_speed_is_ok_only_when_faster_than_cpu():
    assert fc.judge_gpu_speed(3.0, 12.0)[0] == "OK"
    level, text = fc.judge_gpu_speed(15.0, 12.0)
    assert level == "WARN" and "cuDNN" in text


# --- Wi-Fi 警告 ---

WIFI = {"Name": "Wi-Fi", "Description": "Intel(R) Wi-Fi 6E AX211 160MHz", "Media": "Native 802.11"}
WIRED = {"Name": "乙太網路", "Description": "Realtek PCIe GbE Family Controller", "Media": "802.3"}
VPN = {"Name": "Tailscale", "Description": "Tailscale Tunnel", "Media": "Unspecified"}


def test_wireless_adapters_are_found_by_media_type_name_or_description():
    assert fc.wireless_adapters([WIRED, VPN]) == []
    assert fc.wireless_adapters([WIRED, WIFI]) == ["Wi-Fi"]
    assert fc.wireless_adapters([{"Name": "無線網路", "Description": "", "Media": ""}]) == ["無線網路"]
    assert fc.wireless_adapters([{"Name": "行動", "Description": "Mobile Broadband Adapter", "Media": "Wireless WAN"}])
    assert fc.wireless_adapters([{"Name": "x", "Description": "WLAN card", "Media": None}]) == ["x"]
    assert fc.wireless_adapters([{}, {"Name": None}]) == []                      # 壞資料不出錯


def test_wifi_connected_is_a_warning_with_the_adapter_name_never_a_failure():
    level, text = fc.check_wifi(lambda: [WIRED, WIFI])[0]
    assert level == "WARN" and "Wi-Fi" in text and "建議關閉" in text and "測試時可忽略" in text


def test_no_wifi_is_ok_and_a_failed_query_only_warns():
    assert fc.check_wifi(lambda: [WIRED, VPN])[0][0] == "OK"
    assert fc.check_wifi(lambda: [])[0][0] == "OK"

    def broken():
        raise OSError("powershell 不存在")

    level, text = fc.check_wifi(broken)[0]
    assert level == "WARN" and "無法檢查" in text and "OSError" in text


def test_the_wifi_check_runs_as_part_of_the_front_check():
    import inspect
    assert "check_wifi" in inspect.getsource(fc.main)
