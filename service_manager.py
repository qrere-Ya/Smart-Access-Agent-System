"""
背景服務管理層：啟動/停止/監控三組子服務（RAG、人臉辨識、QR 註冊伺服器），以及跟這些
服務有關的網路小工具（port 檢查、IP 位址探測）。

【2026-09-12，P1 架構重構抽出】原本這些邏輯是 backend_main.py 的 BackendManagerApp
方法，程式碼裡混雜了大量 Tkinter 相關的呼叫（self.root.after() 把讀取到的輸出丟回主
執行緒、self.xxx_btn.config() 改按鈕文字），讓這支檔案完全沒辦法脫離 Tkinter 單獨測試
或重用。這裡抽出來之後，這個模組完全不 import 任何 Tkinter 相關套件，只透過「呼叫端
傳進來的 callback 函式」跟外面溝通（子行程印出一行輸出、或是整批啟動流程做完了），
要怎麼安全地把這些事件轉回 GUI 執行緒、怎麼更新畫面，是呼叫端（backend_main.py）自己
的事，這個模組完全不管。

服務目前是否在跑、對應的 subprocess.Popen 物件，存在這個模組自己的全域變數裡
（_service_procs），不是 BackendManagerApp 的實例屬性——這支桌面程式本來就只會有一個
視窗實例在跑，用模組層級的全域變數剛好對應「這台電腦上到底有哪些服務正在跑」這個事實。
"""
import os
import re
import sys
import shutil
import socket
import subprocess
import threading
import time

# 【合併專案調整】改用 __file__ 相對路徑，不管從哪個資料夾執行都能正確定位到專案根目錄
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# ==========================================
# 【三個獨立開關】每個服務各自用的網路埠（拿來判斷「是不是已經在跑了」，避免同一個
# 服務被重複啟動、浪費資源、甚至互搶 port 導致啟動失敗）：
#   - ollama serve            → 11434（Ollama 官方預設）
#   - litellm --config ...    → 4000（config.yaml 沒指定 port，litellm 預設用 4000）
#   - app.py（RAG 助理網頁）   → 7860（app.py 裡 demo.launch(server_port=7860) 寫死的）
#   - web_server.py（QR 註冊） → 5000（web_server.py 裡 app.run(port=5000) 寫死的）
#   - terminal_app.py（人臉辨識）沒有網路埠，直接看行程物件是否還存活來判斷。
# ==========================================
PORT_OLLAMA = 11434
PORT_LITELLM = 4000
PORT_RAG_APP = 7860
PORT_WEB_SERVER = 5000

_RAG_GROUP = ["ollama", "litellm", "rag_app"]

# key: 服務名稱, value: subprocess.Popen 物件（只存「本程式自己啟動」的行程，如果偵測到
# port 已經被別的行程佔用，代表服務已經在跑，不會重複啟動、也不會存進這裡——這樣
# 「停止」的時候才不會誤殺不是我們啟動的行程）。
_service_procs = {}
# 防止 RAG 這組服務在「啟動中」的背景執行緒還沒跑完時，又被按一次造成重複啟動。
_rag_busy = False


# ==========================================
# 【共用小工具】判斷某個 port 是否已經有人在用、找出佔用某個 port 的行程、等待某個
# port 就緒。
# ==========================================
def is_port_open(host, port, timeout=0.3):
    """快速檢查某個 port 現在是不是已經有服務在監聽（代表服務已經在跑，不用重複啟動）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port_open(host, port, timeout=20, interval=0.5):
    """輪詢等待某個 port 就緒（給剛啟動、還在載入模型的服務一點時間），最多等 timeout 秒。"""
    waited = 0.0
    while waited < timeout:
        if is_port_open(host, port):
            return True
        time.sleep(interval)
        waited += interval
    return False


def _find_pid_on_port(port):
    """用 psutil 找出目前是誰佔用這個 port，回傳 PID（找不到則回傳 None）。用在「停止」
    的時候：就算這個服務不是本程式啟動的（例如 Ollama 本來就常駐、或是上一次執行本
    程式時啟動、這次程式重開後 Popen 物件已經遺失），也還是能真的把資源釋放掉，而不是
    「按了停止卻其實什麼都沒關掉」。"""
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
    停止一個服務：優先用本程式自己存的 Popen 物件關閉（乾淨、快）；如果沒有（代表是
    外部或前一次啟動的），且有給 port，才退而求其次用 psutil 依 port 找出行程強制關閉。
    回傳 True/False 代表有沒有成功處理掉。

    【2026-09-18，修正「開開關關」快速切換 QR 註冊伺服器會連續逾時的真正原因】
    原本 terminate() 送出之後就立刻 return True，完全沒有等行程真的結束、也沒有
    確認 port 有沒有真的釋放。如果使用者按「停止」之後很快又按「啟動」（例如測試
    WiFi Direct 熱點時常見的「開開關關」操作），舊行程的 TCP 監聽 port 這時候
    很可能還沒真的釋放，新行程 bind 同一個 port 會直接失敗、啟動後立刻當掉——但
    畫面已經顯示「已啟動」成功訊息，之後 QR 畫面每 5 秒打一次 API 都會連續逾時，
    完全看不出問題出在「上一個行程沒關乾淨」。改成：terminate() 之後最多等 5 秒
    讓行程真的結束（逾時就算了，不強制 kill，避免影響其他正常情況）；如果有給
    port，再額外最多等 3 秒確認 port 真的釋放掉，這樣「停止」回傳的時候，port
    才是真的可以馬上重新綁定的狀態，不會再有快速切換造成的假成功。
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


# ==========================================
# 【子行程輸出讀取】在背景執行緒裡逐行讀取子行程的輸出，讀到一行就呼叫 on_line(line)。
# 這個函式本身不知道畫面存不存在、要更新哪個文字框——安不安全地把這件事轉回 GUI
# 執行緒，是呼叫端的責任。
# ==========================================
def _read_process_output(proc, on_line):
    """
    一定要在背景執行緒裡呼叫：readline() 讀不到新資料的時候會整個卡住等待，如果直接在
    GUI 主執行緒做，整個視窗會被凍住、按什麼都沒反應。啟動時已經把 stderr 併進 stdout
    了，兩種訊息都看得到。
    """
    try:
        for raw_line in iter(proc.stdout.readline, ""):
            if not raw_line:
                break
            on_line(raw_line.rstrip("\n"))
    except Exception as e:
        # 【讓「讀取當掉」也現形】讀取本身出錯（最常見是編碼對不起來）以前會被整個吞掉，
        # 畫面上只看得到「輸出中斷、查不到結束代碼」，完全找不到原因。這裡把例外內容
        # 講出來，才看得出來是不是編碼出了問題。
        on_line(f"⚠️（讀取輸出時發生例外：{e!r}）")
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        # 【讓「悄悄死掉」現形】讀到這裡代表子行程的輸出已經停了，通常就是這個行程已經
        # 結束——不管是正常結束、還是背景直接當掉都一樣。結束碼 0 代表正常關閉，不是 0
        # 通常就代表是異常結束、值得往上找錯誤訊息。
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
    """把 _read_process_output() 丟進背景執行緒去跑，並統一加上 prefix。"""
    def _wrapped_on_line(line):
        on_line(f"{prefix}{line}")
    threading.Thread(target=_read_process_output, args=(proc, _wrapped_on_line), daemon=True).start()


# ==========================================
# 【子行程啟動共用設定】
# ==========================================
def _python_child_env():
    """
    【修正：子行程印中文/emoji 會直接當機】Python 子行程要把文字印出去之前，得先决定
    要用什麼編碼把文字轉成位元組。一旦輸出改成接一條「水管」(pipe) 過來，Python 一
    偵測到對方不是終端機，就會退回用 Windows 系統的預設語言編碼（繁體中文 Windows 上
    是 cp950，也就是 Big5），而 cp950 裡面根本沒有「🤖」這種 emoji，一遇到就爆炸。
    解法：帶一個環境變數 PYTHONIOENCODING=utf-8，只強制「文字要印出去的時候用哪種
    編碼」這一件事，改成 UTF-8，從根本解決。

    【教訓，記錄一下】一開始這裡還多加了 PYTHONUTF8=1（Python 的「全域 UTF-8 模式」，
    影響範圍遠遠不只是文字編碼），而且「順手」也套用到 LiteLLM 身上——結果反而把原本
    跑得好好的 LiteLLM 搞當機（結束碼 3，沒有任何錯誤訊息）。現在只留下真正解決問題
    所需要的 PYTHONIOENCODING，範圍降到最小；LiteLLM 不需要這個修正，已經從它的啟動
    設定裡拿掉了（見 _start_rag_services_worker() 裡 LiteLLM 那一段）。

    【2026-09-18 新增：RAG 助理網頁載入 bge-m3 完就直接當機，結束碼 3221225477】
    這個結束碼換算成十六進位是 0xC0000005（Windows 的 STATUS_ACCESS_VIOLATION，
    等於 Linux/macOS 的 segfault）——是作業系統層級直接把整個行程砍掉，不是
    Python 的例外，所以 RAG App 那邊的輸出完全看不到任何 traceback，就是「印完
    Loading weights: 100% 之後整個沒了」。從實際發生的時間點（bge-m3 權重讀完、
    緊接著第一次呼叫 get_text_embedding() 做測試向量）來看，最吻合的已知成因是
    Windows 上經典的「OpenMP / MKL 動態庫重複初始化衝突」：這個行程裡同時載入了
    faiss-cpu（rag_index.py）跟 torch（bge-m3 embedding 模型底層用的框架），這兩個
    套件各自內建/連結了自己的 OpenMP 執行期（torch 帶的是 libiomp5md.dll），在同一
    個行程裡被重複初始化時，有些情況會印出「OMP: Error #15」這種警告訊息，但也有
    不少情況（尤其是子行程輸出被導到 pipe、不是真的終端機時）不會印出任何警告，
    直接在第一次真正呼叫到底層數學運算（也就是這裡的第一次 embedding 推論）的當下
    整個行程当場中止，符合這次「連錯誤訊息都沒有」的症狀。標準解法是設定環境變數
    KMP_DUPLICATE_LIB_OK=TRUE，告訴 OpenMP 執行期「就算偵測到重複初始化也不要
    直接砍掉行程」。這裡直接加在 _python_child_env()（三個子服務共用同一個函式）
    而不是只加在 RAG App 那條路徑，是因為人臉辨識終端機那邊同時也用了
    onnxruntime + opencv + tensorflow，理論上有一樣的風險，先一次修掉。

    【還沒辦法 100% 排除的另一種可能，先記錄下來】HuggingFaceEmbedding()
    目前沒有明確指定 device 參數，預設會偵測到有 NVIDIA 顯示卡就自動用 CUDA——
    但同一時間 Ollama 的 log 顯示這張 4060 只剩 1.4 GiB 可用顯存（其餘被 Ollama
    自己占用），如果 pip 裝到的 torch==2.12.0 剛好是有 CUDA 支援的版本（不是
    CPU-only 版本），bge-m3 想搬上 GPU 時可能撞到顯存不足，在某些驅動版本下
    顯示卡驅動層級的記憶體錯誤也會以 access violation 的方式讓行程直接消失，
    而不是乾淨的 Python OutOfMemoryError。如果加了 KMP_DUPLICATE_LIB_OK=TRUE
    之後重跑還是在同一個地方當掉，下一步要檢查的就是這個：確認
    `python -c "import torch; print(torch.__version__, torch.cuda.is_available())"`
    的結果，如果是 True，就要把 HuggingFaceEmbedding 改成明確指定
    device="cpu"，避免跟 Ollama 搶顯存。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # 見上面這次新增的說明：避免 faiss-cpu 跟 torch 各自的 OpenMP 執行期在同一個
    # 子行程裡重複初始化、導致沒有任何錯誤訊息的 access violation（0xC0000005）。
    env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    return env


def _hidden_console_popen_kwargs():
    """
    用來隱藏子行程的黑色主控台視窗。

    【走過的彎路，記錄一下】中途曾經懷疑過是 CREATE_NO_WINDOW 導致 LiteLLM 當掉，
    改成「有主控台但隱藏起來」(CREATE_NEW_CONSOLE) 試過一次，結果結束碼還是一樣的
    3——代表這個理論是錯的，真正的原因其實是環境變數（見 _python_child_env()）。
    既然改了也沒用，就改回原本單純、從頭到尾都證實可以正常運作的 CREATE_NO_WINDOW。
    """
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def _project_python_exe():
    """
    【修正：手動跑完全正常，按鈕啟動卻連一行輸出都沒有就當掉】以前用 sys.executable
    啟動子服務——意思是「這支程式自己是用哪一個 python.exe 跑的，就用同一個來啟動
    它們」。這其實是不安全的假設：這支程式本身可能是被別的 Python 環境啟動的，如果
    sys.executable 指到的不是這個專案自己的 venv，那個環境裡通常不會裝 torch /
    llama-index 這些 RAG 需要的重量級套件，子服務一啟動就會因為 import 失敗直接死掉，
    而且可能連 Traceback 都來不及印出來。
    解法：直接指定這個專案自己的 venv 裡的 python.exe，不管這支程式自己是被哪個
    Python 環境啟動的，子服務永遠都會用「這個專案自己的」venv 來跑。找不到這個 venv
    時才退回用 sys.executable，不會讓程式直接壞掉。
    """
    venv_python = os.path.join(_PROJECT_ROOT, "venv", "Scripts", "python.exe")
    if os.path.exists(venv_python):
        return venv_python
    return sys.executable


# ==========================================
# 【開關一】RAG 服務：Ollama + LiteLLM Proxy + app.py（RAG 助理網頁）
# ==========================================
def rag_group_running():
    """
    【修正：狀態畫面會騙人】判斷 RAG 這組服務現在是不是「真的」還在跑。只檢查
    _service_procs 這個字典裡「有沒有存過」Popen 物件是不夠的，物件存在不代表行程
    還活著——服務啟動後如果馬上當掉，Popen 物件還是會一直留在字典裡（只有真的按下
    「停止」才會被清掉）。這裡改成用 proc.poll()（回傳 None 代表行程還活著）真正確認
    一次，跟 port 有沒有開一起判斷，才不會顯示假的狀態。
    """
    if is_port_open("127.0.0.1", PORT_RAG_APP):
        return True
    for k in _RAG_GROUP:
        proc = _service_procs.get(k)
        if proc is not None and proc.poll() is None:
            return True
    return False


def is_rag_busy():
    return _rag_busy


def start_rag_services_async(on_output_line, on_finished):
    """
    在背景執行緒裡依序啟動 Ollama → LiteLLM Proxy → app.py，三個都是各自獨立的子行程：

      1. ollama serve —— 本地 LLM 引擎。RAG 助理啟動時會馬上去問它模型資訊，所以這裡
         會先啟動它、並且「實際等到 port 11434 真的開了」才繼續下一步，避免 app.py
         因為 Ollama 還沒準備好就連線失敗。如果 port 11434 已經有人在聽（代表 Ollama
         本來就常駐在背景），就不重複啟動。
      2. litellm --config config.yaml —— 一定要用獨立的 venv_litellm（Python 3.11+）
         啟動，不能用這支程式自己所在的 Python 3.10 主環境直接 import litellm（會撞到
         已知的相容性問題）。這一步不用等待它完全就緒才繼續，因為 app.py 本身不依賴
         它，只有之後按「Execute Evaluate」評估功能才會用到。
      3. app.py（RAG 助理 Gradio 網頁，port 7860）——只有前面都處理好才啟動。

    on_output_line(prefix, line)：子行程每印一行就呼叫一次，prefix 是 "[Ollama] "
    / "[LiteLLM] " / "[RAG App] " 其中一個，用來分辨是誰印的。
    on_finished(started, warnings)：全部處理完之後呼叫一次，started/warnings 都是
    字串清單。

    這個函式本身立刻回傳（不會卡住呼叫端），實際工作丟進背景執行緒去跑。
    """
    global _rag_busy
    _rag_busy = True
    threading.Thread(target=_start_rag_services_worker, args=(on_output_line, on_finished), daemon=True).start()


def _start_rag_services_worker(on_output_line, on_finished):
    global _rag_busy
    warnings = []
    started = []

    def emit(prefix, line):
        on_output_line(prefix, line)

    # 1. Ollama：先看 port 是否已開，沒開才啟動，並實際等到就緒
    if is_port_open("127.0.0.1", PORT_OLLAMA):
        started.append("Ollama（本來就已經在跑，沒有重複啟動）")
        emit("[Ollama] ", "（本來就已經在背景執行，不是本程式啟動的，看不到它的輸出）")
    else:
        ollama_exe = shutil.which("ollama")
        if ollama_exe:
            try:
                # errors="replace"：這裡故意不指定 encoding（沒有強迫 Ollama 用哪種編碼
                # 印字，它是 Go 寫的，跟 Python 的編碼設定無關），讀取這一端就照系統預設
                # 去解，只加 errors="replace" 當安全網。
                # 【資源規範】Ollama 官方環境變數：閒置 2 分鐘卸載模型、同時只載入 1 個模型、
                # 不並行（每多 1 個並行 slot，KV cache 就多一份），避免模型與 KV cache 常駐吃滿記憶體/顯存。
                # 使用者自己已設定的值優先（setdefault）。
                _ollama_env = _python_child_env()
                _ollama_env.setdefault("OLLAMA_KEEP_ALIVE", "2m")
                _ollama_env.setdefault("OLLAMA_MAX_LOADED_MODELS", "1")
                _ollama_env.setdefault("OLLAMA_NUM_PARALLEL", "1")
                proc = subprocess.Popen(
                    [ollama_exe, "serve"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                    errors="replace", env=_ollama_env,
                    **_hidden_console_popen_kwargs(),
                )
                _service_procs["ollama"] = proc
                _start_output_reader_thread(proc, lambda line: emit("[Ollama] ", line))
                if wait_port_open("127.0.0.1", PORT_OLLAMA, timeout=20):
                    started.append("Ollama")
                else:
                    warnings.append("Ollama 已啟動，但等了 20 秒 port 11434 還沒開，可能還在載入、或啟動異常。")
            except Exception as e:
                warnings.append(f"Ollama 啟動失敗：{e}")
        else:
            warnings.append("找不到 ollama 指令，請確認 Ollama 已安裝並加入系統 PATH。")

    # 2. LiteLLM Proxy：一樣先看 port 4000 是否已被佔用
    if is_port_open("127.0.0.1", PORT_LITELLM):
        started.append("LiteLLM Proxy（本來就已經在跑，沒有重複啟動）")
        emit("[LiteLLM] ", "（本來就已經在背景執行，不是本程式啟動的，看不到它的輸出）")
    else:
        litellm_exe = os.path.join(_PROJECT_ROOT, "venv_litellm", "Scripts", "litellm.exe")
        config_path = os.path.join(_PROJECT_ROOT, "config.yaml")
        if os.path.exists(litellm_exe):
            try:
                # PYTHONUNBUFFERED=1：litellm.exe 骨子裡還是在跑 Python，加這個環境變數
                # 強制它每印一行就馬上送出，才能「即時」看到輸出。
                # 【不要加 _python_child_env() 的 UTF-8 設定！】實測發現這樣反而會把
                # LiteLLM 搞當機（結束碼 3，卡在「Waiting for application startup」之
                # 後）——LiteLLM 是別人寫的套件，不會印我們自己寫的中文/emoji 文字，
                # 本來就不需要這個修正，這裡維持只帶 PYTHONUNBUFFERED 這一個環境變數，
                # 就是最早測試成功、能完整跑到「Uvicorn running」那個版本的設定。
                litellm_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
                proc = subprocess.Popen(
                    [litellm_exe, "--config", config_path],
                    cwd=_PROJECT_ROOT, env=litellm_env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                    errors="replace",
                    **_hidden_console_popen_kwargs(),
                )
                _service_procs["litellm"] = proc
                _start_output_reader_thread(proc, lambda line: emit("[LiteLLM] ", line))
                started.append("LiteLLM Proxy")
            except Exception as e:
                warnings.append(f"LiteLLM Proxy 啟動失敗：{e}")
        else:
            warnings.append("找不到 venv_litellm，這次先跳過 LiteLLM Proxy（評估功能會用不了，其他功能不受影響）。")

    # 3. RAG 助理網頁（app.py，port 7860）
    if is_port_open("127.0.0.1", PORT_RAG_APP):
        started.append("RAG 助理網頁（本來就已經在跑，沒有重複啟動）")
        emit("[RAG App] ", "（本來就已經在背景執行，不是本程式啟動的，看不到它的輸出）")
    else:
        rag_script = os.path.join(_PROJECT_ROOT, "app.py")
        python_exe = _project_python_exe()
        emit("[RAG App] ", f"（使用的 Python 直譯器：{python_exe}）")
        try:
            # "-u"：讓 Python 不要緩衝輸出。encoding="utf-8"：讀取這一端也要講清楚要用
            # 什麼編碼「解讀」讀到的位元組，不然 Python 預設會用系統的地區編碼
            # （cp950／繁體中文 Windows）去解——子行程用 UTF-8 寫、我們卻用 cp950
            # 讀，兩邊對不起來，只要一遇到中文或 emoji 就會讓讀取執行緒當場停擺。
            proc = subprocess.Popen(
                [python_exe, "-u", rag_script], cwd=_PROJECT_ROOT,
                env=_python_child_env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                encoding="utf-8", errors="replace",
                **_hidden_console_popen_kwargs(),
            )
            _service_procs["rag_app"] = proc
            _start_output_reader_thread(proc, lambda line: emit("[RAG App] ", line))
            started.append("RAG 助理網頁")
        except Exception as e:
            warnings.append(f"RAG 助理網頁啟動失敗：{e}")

    _rag_busy = False
    on_finished(started, warnings)


def stop_rag_services():
    """停止 RAG 這組服務（Ollama / LiteLLM / 助理網頁），回傳每一個有沒有成功處理掉的
    布林值清單，呼叫端可以用 any(...) 判斷「到底有沒有真的停掉什麼」。"""
    return [stop_service(k, p) for k, p in
            [("ollama", PORT_OLLAMA), ("litellm", PORT_LITELLM), ("rag_app", PORT_RAG_APP)]]


# ==========================================
# 【開關二】人臉辨識終端機：terminal_app.py（獨立於 RAG、獨立於註冊）
# ==========================================
def face_service_running():
    """跟 rag_group_running() 一樣的道理：proc 物件存在不代表行程還活著，這裡順便把
    已經死掉的殘留 Popen 物件清掉（對應原本 backend_main.py 裡 _update_face_status()
    每次刷新狀態都會做的清理動作）。"""
    proc = _service_procs.get("terminal")
    if proc is not None and proc.poll() is None:
        return True
    _service_procs.pop("terminal", None)
    return False


def start_face_service(on_output_line):
    """
    啟動人臉辨識終端機。回傳 (ok: bool, message: str)。

    原本沒有加 stdout/stderr 導向，print 出來的東西有沒有地方看，其實要碰運氣（要看
    啟動這支程式的當下背後有沒有一個終端機視窗）。這裡改成不管怎樣都用 pipe 把輸出
    接過來，攝影機畫面本身是另一個獨立的視窗，不受影響、還是會照樣跳出來。
    """
    terminal_script = os.path.join(_PROJECT_ROOT, "terminal_app.py")
    python_exe = _project_python_exe()
    on_output_line(f"（使用的 Python 直譯器：{python_exe}）")
    try:
        proc = subprocess.Popen(
            [python_exe, "-u", terminal_script], cwd=_PROJECT_ROOT,
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


# ==========================================
# 【開關三】QR 註冊伺服器：web_server.py（獨立於 RAG、獨立於人臉辨識）
# ==========================================
def reg_server_running():
    """跟 rag_group_running() 同樣的道理：不能只看 _service_procs 裡有沒有存過 Popen
    物件（物件存在不代表行程沒死掉），要用 proc.poll() 真的確認一次。"""
    if is_port_open("127.0.0.1", PORT_WEB_SERVER):
        return True
    proc = _service_procs.get("web_server")
    return proc is not None and proc.poll() is None


def start_reg_service(on_output_line):
    """啟動 QR 註冊伺服器。回傳 (ok: bool, message: str)。"""
    web_script = os.path.join(_PROJECT_ROOT, "web_server.py")
    python_exe = _project_python_exe()
    on_output_line(f"（使用的 Python 直譯器：{python_exe}）")
    try:
        proc = subprocess.Popen(
            [python_exe, "-u", web_script], cwd=_PROJECT_ROOT,
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


# ==========================================
# 關閉主視窗時，一次停掉所有還在背景跑的服務
# ==========================================
def any_service_running():
    return bool(_service_procs) or is_port_open("127.0.0.1", PORT_RAG_APP) or is_port_open("127.0.0.1", PORT_WEB_SERVER)


def stop_all_services():
    stop_service("ollama", PORT_OLLAMA)
    stop_service("litellm", PORT_LITELLM)
    stop_service("rag_app", PORT_RAG_APP)
    stop_service("terminal")
    stop_service("web_server", PORT_WEB_SERVER)


# ==========================================
# 【IP 位址探測】QR 註冊要放進網址裡的本機 IP。跟服務啟停沒有直接關係，但同樣是純
# 網路邏輯、不碰畫面，所以也歸在這個模組裡。
# ==========================================
def _find_wifi_direct_adapter_ip():
    """
    【2026-09-06，新增：支援自製 WiFiDirectHotspotCore 熱點】專門找「Windows 自己建立
    的 Wi-Fi Direct 虛擬網卡」目前拿到的 IPv4 位址。

    背景：原本 get_local_ip_offline() 只認得 192.168.137.1 / 192.168.5.1 這兩組固定
    號碼，分別是 Windows 內建網路共享(ICS)跟 MyPublicWiFi 這兩套軟體慣用的網段。如果
    改用自製的 WiFiDirectHotspotCore，Windows 會另外生出一張全新的虛擬網卡，實際拿到
    的 IP 位址不一定是上面那兩個號碼，用固定清單去比對會抓不到。

    做法：去找「哪一張網卡是 Wi-Fi Direct 虛擬網卡」——這種虛擬網卡不管換到哪台電腦、
    哪次開機，Windows 內部登記的裝置描述文字都固定包含英文的「Wi-Fi Direct」字樣，用
    `ipconfig /all` 把每張網卡的完整資訊印出來，找出「描述裡有 Wi-Fi Direct」的那個
    區塊，直接讀出它當下真正的 IPv4 位址，找不到就傳回 None、讓呼叫端改用原本的邏輯。

    【誠實補充】這段邏輯是照 `ipconfig /all` 一般固定的排版格式寫的，沒辦法在沒有真正
    Windows + WiFiDirect 硬體的環境實際驗證過。整段包在 try/except 裡：只要任何一個
    環節出狀況（包含這裡不是 Windows，根本沒有 ipconfig 指令），就安靜地傳回 None，
    不會讓原本能動的功能也跟著壞掉。
    """
    try:
        result = subprocess.run(["ipconfig", "/all"], capture_output=True, timeout=5)
        raw = result.stdout
        # Windows 主控台在繁體中文系統下常用 cp950（Big5）編碼，不一定是 utf-8，依序
        # 嘗試幾種常見編碼，全部失敗才用「忽略錯誤字元」的方式硬解。
        output = None
        for enc in ("cp950", "utf-8", "big5"):
            try:
                output = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if output is None:
            output = raw.decode("utf-8", errors="ignore")

        # 把整段輸出，依「不縮排的網卡標題行」切成一段一段的網卡區塊
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
                # 【2026-09-07，修正：同一張網卡可能同時掛兩組 IPv4】像 MyPublicWiFi 這種
                # 工具建立的虛擬網卡，同一個區塊裡可能一次列出兩組 IPv4 Address，手機真正
                # 連進來、QR Code 網址在用的是「後面那組」，所以抓出全部再取最後一個。
                matches = re.findall(r"IPv4.*?:\s*([\d.]+)", block)
                if matches:
                    return matches[-1]
    except Exception as e:
        print(f"[WiFi Direct IP Discovery Error] {e}")
    return None


def get_local_ip_offline():
    """智慧化抓取本機 IPv4 位址，優先匹配虛擬熱點/ICS 網段。解決 Windows 11 Virtual AP
    路由優先級導致抓錯實體網卡 IP 的問題。"""
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

        private_ips = [ip for ip in ip_list if ip != '127.0.0.1' and (ip.startswith('192.168.') or ip.startswith('10.'))]
        if private_ips:
            gateway_like = [ip for ip in private_ips if ip.endswith('.1')]
            return gateway_like[0] if gateway_like else private_ips[0]

        for ip in ip_list:
            if ip != '127.0.0.1':
                return ip
    except Exception as e:
        print(f"[IP Discovery Error] {e}")
    return '127.0.0.1'
