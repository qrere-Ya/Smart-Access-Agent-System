"""
service_manager 單元測試：port 偵測、子行程的啟動指令（以 python -m 啟動各模組）、RAG 只管 Ollama 與助理網頁、
停止與狀態判斷、子行程輸出的讀取、本機 IP 的偵測順序。子行程用假物件取代，不會真的啟動服務。
"""
import io
import socket
import subprocess
import sys
import time
import types

import pytest

from admin import service_manager as sm
from access import web_server as ws
from shared import paths


class FakePopen:
    instances = []

    def __init__(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.stdout = io.StringIO("第一行\n第二行\n")
        FakePopen.instances.append(self)

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


@pytest.fixture
def fake_popen(monkeypatch):
    FakePopen.instances = []
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(sm, "_service_procs", {})
    return FakePopen


def wait_for(predicate, seconds=3.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------- port ----------

def test_port_detection_with_a_real_listening_socket():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]
    try:
        assert sm.is_port_open("127.0.0.1", port) is True
        assert sm.wait_port_open("127.0.0.1", port, timeout=1) is True
    finally:
        server.close()
    assert sm.is_port_open("127.0.0.1", port) is False
    assert sm.wait_port_open("127.0.0.1", port, timeout=0.3, interval=0.1) is False


# ---------- 啟動指令 ----------

def test_face_and_registration_services_start_as_python_modules(fake_popen):
    lines = []
    ok, _ = sm.start_face_service(lines.append)
    assert ok
    ok, _ = sm.start_reg_service(lines.append)
    assert ok
    face, reg = fake_popen.instances
    assert face.args == [sm._project_python_exe(), "-u", "-m", "access.terminal_app"]
    assert reg.args == [sm._project_python_exe(), "-u", "-m", "access.web_server"]
    for popen in (face, reg):
        assert popen.kwargs["cwd"] == paths.PROJECT_ROOT
        assert popen.kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert set(sm._service_procs) == {"terminal", "web_server"}
    assert wait_for(lambda: lines.count("第一行") == 2)


def test_start_failures_are_reported_not_raised(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("找不到檔案")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(sm, "_service_procs", {})
    ok, message = sm.start_face_service(lambda line: None)
    assert not ok and "找不到檔案" in message
    ok, message = sm.start_reg_service(lambda line: None)
    assert not ok and "找不到檔案" in message


def test_stopping_everything_stops_ollama_terminal_web_server_and_the_assistant(monkeypatch):
    calls = []
    monkeypatch.setattr(sm, "stop_service", lambda key, port=None: calls.append((key, port)) or True)
    assert not hasattr(sm, "PORT_LITELLM") and not hasattr(sm, "PORT_RAG_APP")
    sm.stop_all_services()
    assert [key for key, _ in calls] == ["ollama", "terminal", "web_server", "assistant"]


# ---------- 本機 LLM 與管理者助理視窗 ----------

def test_ollama_already_running_is_not_started_twice(monkeypatch, fake_popen):
    monkeypatch.setattr(sm, "is_port_open", lambda host, port, timeout=0.3: True)
    lines = []
    started, warning = sm.start_ollama(lines.append)
    assert "本來就已經在跑" in started and warning is None and fake_popen.instances == [] and lines


def test_ollama_start_limits_its_resources_and_waits_for_the_port(monkeypatch, fake_popen):
    monkeypatch.setattr(sm, "is_port_open", lambda host, port, timeout=0.3: False)
    monkeypatch.setattr(sm, "wait_port_open", lambda host, port, timeout=20, interval=0.5: True)
    monkeypatch.setattr(sm.shutil, "which", lambda name: "C:/ollama.exe")
    monkeypatch.setattr(sm, "_service_procs", {})
    started, warning = sm.start_ollama(lambda line: None)
    popen = fake_popen.instances[0]
    assert (started, warning) == ("Ollama", None) and popen.args == ["C:/ollama.exe", "serve"]
    assert popen.kwargs["env"]["OLLAMA_MAX_LOADED_MODELS"] == "1" and "ollama" in sm._service_procs


def test_ollama_that_never_opens_its_port_gives_a_warning(monkeypatch, fake_popen):
    monkeypatch.setattr(sm, "is_port_open", lambda host, port, timeout=0.3: False)
    monkeypatch.setattr(sm, "wait_port_open", lambda host, port, timeout=20, interval=0.5: False)
    monkeypatch.setattr(sm.shutil, "which", lambda name: "C:/ollama.exe")
    monkeypatch.setattr(sm, "_service_procs", {})
    started, warning = sm.start_ollama(lambda line: None)
    assert started is None and "等了 20 秒" in warning


def test_missing_ollama_command_gives_a_warning(monkeypatch):
    monkeypatch.setattr(sm, "is_port_open", lambda host, port, timeout=0.3: False)
    monkeypatch.setattr(sm.shutil, "which", lambda name: None)
    assert sm.start_ollama(lambda line: None) == (None, "找不到 ollama 指令，請確認 Ollama 已安裝並加入系統 PATH。")


def test_assistant_window_starts_as_a_module_and_is_not_opened_twice(monkeypatch, fake_popen):
    monkeypatch.setattr(sm, "_service_procs", {})
    ok, _ = sm.start_assistant_window(lambda line: None)
    popen = fake_popen.instances[0]
    assert ok and popen.args == [sm._project_python_exe(), "-u", "-m", "rag.chat_window"]
    assert popen.kwargs["cwd"] == paths.PROJECT_ROOT
    popen.poll = lambda: None  # 視窗還開著
    assert sm.assistant_window_running()
    ok, message = sm.start_assistant_window(lambda line: None)
    assert not ok and "已經開著" in message and len(fake_popen.instances) == 1


def test_assistant_window_start_failure_is_reported(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("找不到檔案")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(sm, "_service_procs", {})
    ok, message = sm.start_assistant_window(lambda line: None)
    assert not ok and "找不到檔案" in message


def test_closed_assistant_window_is_forgotten(monkeypatch):
    class Dead:
        def poll(self):
            return 0

    monkeypatch.setattr(sm, "_service_procs", {"assistant": Dead()})
    assert sm.assistant_window_running() is False and "assistant" not in sm._service_procs


# ---------- 停止與狀態 ----------

def test_stop_service_terminates_a_real_process(monkeypatch):
    monkeypatch.setattr(sm, "_service_procs", {})
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    sm._service_procs["demo"] = proc
    assert sm.stop_service("demo") is True
    assert proc.poll() is not None and "demo" not in sm._service_procs


def test_stopping_an_unknown_service_does_nothing(monkeypatch):
    monkeypatch.setattr(sm, "_service_procs", {})
    assert sm.stop_service("nobody") is False


ALIVE = types.SimpleNamespace(poll=lambda: None)
DEAD = types.SimpleNamespace(poll=lambda: 1)


def test_face_service_state_cleans_up_dead_processes(monkeypatch):
    monkeypatch.setattr(sm, "_service_procs", {"terminal": DEAD})
    assert sm.face_service_running() is False and "terminal" not in sm._service_procs
    sm._service_procs["terminal"] = ALIVE
    assert sm.face_service_running() is True


def test_service_state_combines_ports_and_process_objects(monkeypatch):
    monkeypatch.setattr(sm, "is_port_open", lambda host, port, timeout=0.3: False)
    monkeypatch.setattr(sm, "_service_procs", {})
    assert not sm.reg_server_running() and not sm.any_service_running()
    sm._service_procs["ollama"] = ALIVE
    sm._service_procs["web_server"] = DEAD
    assert not sm.reg_server_running() and sm.any_service_running()
    monkeypatch.setattr(sm, "is_port_open", lambda host, port, timeout=0.3: port == sm.PORT_WEB_SERVER)
    assert sm.reg_server_running()


# ---------- 子行程輸出與環境 ----------

def test_process_output_is_read_line_by_line_with_an_exit_summary():
    proc = subprocess.Popen([sys.executable, "-c", "print('a'); print('b')"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    lines = []
    sm._read_process_output(proc, lines.append)
    assert lines[:2] == ["a", "b"] and "結束碼 0" in lines[-1] and "正常關閉" in lines[-1]


def test_abnormal_exit_codes_are_called_out():
    proc = subprocess.Popen([sys.executable, "-c", "import sys; print('x'); sys.exit(3)"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    lines = []
    sm._read_process_output(proc, lines.append)
    assert "結束碼 3" in lines[-1] and "異常" in lines[-1]


def test_child_environment_forces_utf8_and_respects_user_settings(monkeypatch):
    monkeypatch.delenv("KMP_DUPLICATE_LIB_OK", raising=False)
    env = sm._python_child_env()
    assert env["PYTHONIOENCODING"] == "utf-8" and env["KMP_DUPLICATE_LIB_OK"] == "TRUE"
    monkeypatch.setenv("KMP_DUPLICATE_LIB_OK", "FALSE")
    assert sm._python_child_env()["KMP_DUPLICATE_LIB_OK"] == "FALSE"


def test_interpreter_falls_back_when_the_project_one_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "VENV_PYTHON", str(tmp_path / "no_python.exe"))
    assert sm._project_python_exe() == sys.executable
    monkeypatch.setattr(paths, "VENV_PYTHON", sys.executable)
    assert sm._project_python_exe() == sys.executable


# ---------- IP 偵測（後台與註冊伺服器各有一份相同邏輯） ----------

IPCONFIG = b"""Windows IP Configuration

Ethernet adapter Ethernet:

   Connection-specific DNS Suffix  . :
   IPv4 Address. . . . . . . . . . . : 192.168.1.50

Wireless LAN adapter Local Area Connection* 3:

   Description . . . . . . . . . . . : Microsoft Wi-Fi Direct Virtual Adapter #2
   IPv4 Address. . . . . . . . . . . : 192.168.137.1
   IPv4 Address. . . . . . . . . . . : 10.20.30.1
"""

MODULES = [sm, ws]


@pytest.mark.parametrize("module", MODULES)
def test_wifi_direct_adapter_ip_uses_the_last_address_of_that_adapter(module, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=IPCONFIG))
    assert module._find_wifi_direct_adapter_ip() == "10.20.30.1"


@pytest.mark.parametrize("module", MODULES)
def test_no_wifi_direct_adapter_means_none(module, monkeypatch):
    other = IPCONFIG.replace(b"Wi-Fi Direct", b"Intel Wireless")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=other))
    assert module._find_wifi_direct_adapter_ip() is None


@pytest.mark.parametrize("module", MODULES)
def test_ipconfig_failure_means_none(module, monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("ipconfig")

    monkeypatch.setattr(subprocess, "run", boom)
    assert module._find_wifi_direct_adapter_ip() is None


def local_ip(module, monkeypatch, ips, wifi=None):
    monkeypatch.setattr(module, "_find_wifi_direct_adapter_ip", lambda: wifi)
    monkeypatch.setattr(socket, "gethostname", lambda: "host")
    monkeypatch.setattr(socket, "gethostbyname_ex", lambda host: (host, [], ips))
    return module.get_local_ip_offline()


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("ips, wifi, expected", [
    (["192.168.1.50"], "172.16.0.9", "172.16.0.9"),                       # Wi-Fi Direct 最優先
    (["10.0.0.5", "192.168.137.1"], None, "192.168.137.1"),               # 熱點網段優先於一般私有網段
    (["192.168.1.50", "192.168.1.1"], None, "192.168.1.1"),               # 私有網段中偏好 .1
    (["192.168.1.50", "10.0.0.7"], None, "192.168.1.50"),
    (["8.8.4.4"], None, "8.8.4.4"),
    (["127.0.0.1"], None, "127.0.0.1"),
])
def test_local_ip_preference_order(module, monkeypatch, ips, wifi, expected):
    assert local_ip(module, monkeypatch, ips, wifi) == expected


@pytest.mark.parametrize("module", MODULES)
def test_local_ip_falls_back_to_loopback_when_everything_fails(module, monkeypatch):
    class OfflineSocket:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, address):
            raise OSError("沒有網路")

    monkeypatch.setattr(socket, "socket", OfflineSocket)
    assert local_ip(module, monkeypatch, []) == "127.0.0.1"

    def boom(host):
        raise OSError("名稱解析失敗")

    monkeypatch.setattr(socket, "gethostbyname_ex", boom)
    assert module.get_local_ip_offline() == "127.0.0.1"
