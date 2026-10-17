"""
service_manager.py：背景服務管理層。

主要功能：
  - 啟動、停止、監控子服務：人臉辨識（terminal_app.py）、QR 註冊（web_server.py）、管理者助理視窗與它用到的 Ollama
  - 以 port 判斷服務是否已在執行，避免重複啟動；只停止自己啟動的行程，外部啟動的改用 port 找 PID
  - 在背景執行緒讀取子行程輸出，以 callback 交給呼叫端
  - 偵測本機區網 IP，供 QR Code 網址使用
  不依賴 Tkinter，可獨立測試。

串接模組：
  - backend_main：管理後台視窗呼叫這裡的函式並更新畫面
  - admin.assistant_window、access.terminal_app、access.web_server：以 python -m 的方式當成子行程啟動
"""
import os
import re
import sys
import shutil
import socket
import subprocess
import threading
import time

from shared import paths

_PROJECT_ROOT = paths.PROJECT_ROOT

# 各服務使用的 port，用來判斷服務是否已在執行
PORT_OLLAMA = 11434
PORT_WEB_SERVER = 5000

# 本程式自己啟動的行程 {服務名稱: Popen}；port 已被外部占用時不會存進來，避免停止時誤殺
_service_procs = {}


# --- 共用小工具 ---
def is_port_open(host, port, timeout=0.3):
    """檢查某個 port 是否已有服務在監聽。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port_open(host, port, timeout=20, interval=0.5):
    """輪詢等待 port 就緒，最多等 timeout 秒。"""
    waited = 0.0
    while waited < timeout:
        if is_port_open(host, port):
            return True
        time.sleep(interval)
        waited += interval
    return False


def _find_pid_on_port(port):
    """用 psutil 找出占用該 port 的 PID，找不到回傳 None。"""
    try:
        import psutil
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                return conn.pid
    except Exception as e:
        print(f"[Port 查詢失敗] {e}")
    return None


def stop_service(key, port=None):
    """
    停止一個服務：優先關閉自己存的 Popen；沒有的話，有給 port 就依 port 找行程關閉。
    關閉後會等行程結束與 port 釋放，讓呼叫端能立刻重新啟動。回傳是否成功處理。
    """
    proc = _service_procs.pop(key, None)
    if proc is not None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if port is not None:
                waited = 0.0
                while is_port_open("127.0.0.1", port) and waited < 3.0:
                    time.sleep(0.2)
                    waited += 0.2
            return True
        except Exception as e:
            print(f"[停止 {key} 失敗] {e}")
            return False
    if port is not None:
        pid = _find_pid_on_port(port)
        if pid:
            try:
                import psutil
                psutil.Process(pid).terminate()
                waited = 0.0
                while is_port_open("127.0.0.1", port) and waited < 3.0:
                    time.sleep(0.2)
                    waited += 0.2
                return True
            except Exception as e:
                print(f"[依 port 停止 {key} 失敗] {e}")
    return False


# --- 子行程輸出讀取 ---
def _read_process_output(proc, on_line):
    """逐行讀取子行程輸出並呼叫 on_line，行程結束時回報結束碼；必須在背景執行緒執行，否則會卡住畫面。"""
    try:
        for raw_line in iter(proc.stdout.readline, ""):
            if not raw_line:
                break
            on_line(raw_line.rstrip("\n"))
    except Exception as e:
        on_line(f"⚠️（讀取輸出時發生例外：{e!r}）")
    finally:
        try:
            proc.stdout.close()
        except Exception:  # noqa: S110
            pass
        # 輸出停止通常代表行程已結束，結束碼非 0 代表異常關閉
        try:
            exit_code = proc.poll()
            if exit_code is None:
                exit_code = proc.wait(timeout=2)
        except Exception:
            exit_code = None
        if exit_code == 0:
            on_line("（行程已結束，結束碼 0，屬於正常關閉）")
        elif exit_code is not None:
            on_line(f"⚠️（行程已經結束了，結束碼 {exit_code}——不是 0，代表可能是異常關閉，往上找找看有沒有錯誤訊息）")
        else:
            on_line("（行程的輸出已經中斷，但目前查不到結束代碼）")


def _start_output_reader_thread(proc, on_line, prefix=""):
    """在背景執行緒讀取子行程輸出，每行加上 prefix。"""
    def _wrapped_on_line(line):
        on_line(f"{prefix}{line}")
    threading.Thread(target=_read_process_output, args=(proc, _wrapped_on_line), daemon=True).start()


# --- 子行程啟動設定 ---
def _python_child_env():
    """
    子行程的環境變數：
      - PYTHONIOENCODING=utf-8：輸出接 pipe 時預設會用 cp950，印 emoji 會出錯
      - KMP_DUPLICATE_LIB_OK=TRUE：避免 faiss-cpu 與 torch 的 OpenMP 重複初始化造成行程直接中止
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    return env


def _hidden_console_popen_kwargs():
    """Windows 下隱藏子行程的黑色主控台視窗。"""
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def _project_python_exe():
    """回傳專案自己 venv 的 python.exe（子服務需要裡面的套件），找不到才用 sys.executable。"""
    venv_python = paths.VENV_PYTHON
    if os.path.exists(venv_python):
        return venv_python
    return sys.executable


# --- 本機 LLM 服務：Ollama ---
def start_ollama(on_output_line):
    """
    確保本機 Ollama 在執行：port 沒開才啟動，並等到 port 就緒。
    回傳 (啟動結果說明或 None, 警告說明或 None)；on_output_line(line) 接 Ollama 的輸出。
    """
    if is_port_open("127.0.0.1", PORT_OLLAMA):
        on_output_line("（本來就已經在背景執行，不是本程式啟動的，看不到它的輸出）")
        return "Ollama（本來就已經在跑，沒有重複啟動）", None
    ollama_exe = shutil.which("ollama")
    if not ollama_exe:
        return None, "找不到 ollama 指令，請確認 Ollama 已安裝並加入系統 PATH。"
    try:
        # 限制 Ollama 資源：閒置 2 分鐘卸載模型、同時只載入 1 個模型、不並行（使用者已設定的值優先）
        env = _python_child_env()
        env.setdefault("OLLAMA_KEEP_ALIVE", "2m")
        env.setdefault("OLLAMA_MAX_LOADED_MODELS", "1")
        env.setdefault("OLLAMA_NUM_PARALLEL", "1")
        proc = subprocess.Popen(  # noqa: S603
            [ollama_exe, "serve"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            errors="replace", env=env,
            **_hidden_console_popen_kwargs(),
        )
        _service_procs["ollama"] = proc
        _start_output_reader_thread(proc, on_output_line)
        if wait_port_open("127.0.0.1", PORT_OLLAMA, timeout=20):
            return "Ollama", None
        return None, "Ollama 已啟動，但等了 20 秒 port 11434 還沒開，可能還在載入、或啟動異常。"
    except Exception as e:
        return None, f"Ollama 啟動失敗：{e}"


# --- 管理者助理聊天視窗：admin.assistant_window ---
def assistant_window_running():
    """聊天視窗行程是否還活著，已結束的殘留 Popen 順便清掉。"""
    proc = _service_procs.get("assistant")
    if proc is not None and proc.poll() is None:
        return True
    _service_procs.pop("assistant", None)
    return False


def start_assistant_window(on_output_line):
    """另開一個行程顯示管理者助理聊天視窗（模型在那個行程裡載入，不拖慢後台）。回傳 (成功與否, 訊息)。"""
    if assistant_window_running():
        return False, "管理者助理視窗已經開著了，請到工作列找它。"
    python_exe = _project_python_exe()
    try:
        proc = subprocess.Popen(  # noqa: S603
            [python_exe, "-u", "-m", "admin.assistant_window"], cwd=_PROJECT_ROOT,
            env=_python_child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace",
            **_hidden_console_popen_kwargs(),
        )
        _service_procs["assistant"] = proc
        _start_output_reader_thread(proc, on_output_line)
        return True, "管理者助理視窗已開啟，第一次載入模型需要一點時間。"
    except Exception as e:
        return False, f"管理者助理視窗開啟失敗：{e}"


def stop_assistant_window():
    return stop_service("assistant")


# --- 人臉辨識終端機：terminal_app.py ---
def face_service_running():
    """判斷人臉辨識行程是否還活著，已死掉的殘留 Popen 順便清掉。"""
    proc = _service_procs.get("terminal")
    if proc is not None and proc.poll() is None:
        return True
    _service_procs.pop("terminal", None)
    return False


def start_face_service(on_output_line):
    """啟動人臉辨識終端機，輸出透過 pipe 交給 on_output_line。回傳 (成功與否, 訊息)。"""
    terminal_module = "access.terminal_app"
    python_exe = _project_python_exe()
    on_output_line(f"（使用的 Python 直譯器：{python_exe}）")
    try:
        proc = subprocess.Popen(  # noqa: S603
            [python_exe, "-u", "-m", terminal_module], cwd=_PROJECT_ROOT,
            env=_python_child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace",
            **_hidden_console_popen_kwargs(),
        )
        _service_procs["terminal"] = proc
        _start_output_reader_thread(proc, on_output_line)
        return True, "人臉辨識終端機已啟動，會另外彈出攝影機畫面視窗。"
    except Exception as e:
        return False, f"人臉辨識終端機啟動失敗：{e}"


def stop_face_service():
    return stop_service("terminal")


# --- QR 註冊伺服器：web_server.py ---
def reg_server_running():
    """判斷 QR 註冊伺服器是否在執行（port 已開，或自己啟動的行程還活著）。"""
    if is_port_open("127.0.0.1", PORT_WEB_SERVER):
        return True
    proc = _service_procs.get("web_server")
    return proc is not None and proc.poll() is None


def start_reg_service(on_output_line):
    """啟動 QR 註冊伺服器。回傳 (成功與否, 訊息)。"""
    web_module = "access.web_server"
    python_exe = _project_python_exe()
    on_output_line(f"（使用的 Python 直譯器：{python_exe}）")
    try:
        proc = subprocess.Popen(  # noqa: S603
            [python_exe, "-u", "-m", web_module], cwd=_PROJECT_ROOT,
            env=_python_child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace",
            **_hidden_console_popen_kwargs(),
        )
        _service_procs["web_server"] = proc
        _start_output_reader_thread(proc, on_output_line)
        return True, "QR 註冊伺服器已啟動，現在可以按「📱 遠端 QR Code 註冊」開啟畫面了。"
    except Exception as e:
        return False, f"QR 註冊伺服器啟動失敗：{e}"


def stop_reg_service():
    return stop_service("web_server", PORT_WEB_SERVER)


# --- 關閉主視窗時停止所有服務 ---
def any_service_running():
    return bool(_service_procs) or is_port_open("127.0.0.1", PORT_WEB_SERVER)


def stop_all_services():
    stop_service("ollama", PORT_OLLAMA)
    stop_service("terminal")
    stop_service("web_server", PORT_WEB_SERVER)
    stop_service("assistant")


# --- 本機 IP 偵測：QR Code 網址使用 ---
def _find_wifi_direct_adapter_ip():
    """用 `ipconfig /all` 找出描述含「Wi-Fi Direct」的網卡並回傳它的 IPv4，找不到或出錯回傳 None。"""
    try:
        result = subprocess.run(["ipconfig", "/all"], capture_output=True, timeout=5)  # noqa: S607
        raw = result.stdout
        # 繁體中文 Windows 常用 cp950，依序嘗試幾種編碼
        output = None
        for enc in ("cp950", "utf-8", "big5"):
            try:
                output = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if output is None:
            output = raw.decode("utf-8", errors="ignore")

        # 依網卡標題行切成區塊
        blocks, current = [], []
        for line in output.splitlines():
            if line and not line[0].isspace():
                if current:
                    blocks.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            blocks.append("\n".join(current))

        for block in blocks:
            if "Wi-Fi Direct" in block or "WiFi Direct" in block:
                # 同一張網卡可能列出兩組 IPv4，實際使用的是最後一組
                matches = re.findall(r"IPv4.*?:\s*([\d.]+)", block)
                if matches:
                    return matches[-1]
    except Exception as e:
        print(f"[WiFi Direct IP Discovery Error] {e}")
    return None


def get_local_ip_offline():
    """取得本機區網 IPv4：優先 Wi-Fi Direct 網卡，其次虛擬熱點網段，再其次一般私有網段。"""
    try:
        wifi_direct_ip = _find_wifi_direct_adapter_ip()
        if wifi_direct_ip:
            return wifi_direct_ip

        hostname = socket.gethostname()
        _, _, ip_list = socket.gethostbyname_ex(hostname)

        if not ip_list:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(('8.8.8.8', 80))
                ip = s.getsockname()[0]
                s.close()
                return ip
            except Exception:
                return '127.0.0.1'

        priority_ips = ['192.168.137.1', '192.168.5.1']
        for pip in priority_ips:
            if pip in ip_list:
                return pip

        private_ips = [ip for ip in ip_list
                       if ip != '127.0.0.1' and (ip.startswith('192.168.') or ip.startswith('10.'))]
        if private_ips:
            gateway_like = [ip for ip in private_ips if ip.endswith('.1')]
            return gateway_like[0] if gateway_like else private_ips[0]

        for ip in ip_list:
            if ip != '127.0.0.1':
                return ip
    except Exception as e:
        print(f"[IP Discovery Error] {e}")
    return '127.0.0.1'
