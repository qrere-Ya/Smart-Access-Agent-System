"""
front_verify.py：前台驗證報告——在前台電腦跑一次，自動收集證據，寫成一份報告檔，帶回後台給 Claude 看或一起對照。

主要功能：
  - 自動收集：前台自我檢查（含 GPU 實測速度那一行與 Wi-Fi 警告）、螢幕資訊（儀表板與辨識各會放哪個螢幕）、
    儀表板／辨識程式／連線代理是否在跑、攝影機能不能開（辨識正在用時只回報「被占用」，不算失敗）、
    前台與後台的時鐘差、runtime\\logs 各記錄檔最近的 ERROR／WARN／Traceback
  - 需要人聽的：打卡語音試聽一次、三種警示音型各響一次，各問 y/n（可加備註）
  - 報告寫在 runtime\\verification\\前台_日期_時間.md；通行碼、金鑰、雜湊類的長字串一律蓋掉
  - 前台不上網：報告用隨身碟複製到後台電腦的 runtime\\verification\\，或把內容貼給 Claude

設計：判斷與格式化是純函式；系統呼叫（螢幕、行程、攝影機、播放聲音、讀記錄檔、鍵盤）都可替換，測試不需要硬體。

串接模組：
  - front_check（自我檢查各項）、front_screens（螢幕）、front_agent（辨識心跳、前台資料庫的時鐘差）
  - audio_player（語音試聽）、alert_sound（警示音型）；shared.verify_common（輸入容錯與敏感資訊過濾）
  - 啟動：前台驗證.bat 或 python -m access.front_verify
"""
import datetime
import os
import re
import sys
import time

from access import alert_sound, front_screens
from shared import paths, role_config
from shared.verify_common import Console, QuitSession, SkipStep, atomic_write, md_table, scrub

LOG_TAIL_LINES = 30                     # 每個記錄檔看最後幾行
MAX_LOG_HITS = 15                       # 每個記錄檔最多列幾行
_LOG_PATTERN = re.compile(r"ERROR|WARN|Traceback|Exception|Error\b|失敗|錯誤")
PROCESSES = (("儀表板", ("access.front_qt", "access.front_dashboard")), ("辨識程式", ("access.terminal_app",)),
             ("連線代理", ("access.front_agent",)))      # QML 版或舊的 Tk 版儀表板都算
ALERT_LABELS = (("tailgate", "尾隨（三聲急促高音）"), ("spoof", "非活體（兩聲低沉長音）"),
                ("no_scan_walkby", "經過未打卡（單聲中音）"))


def format_check_results(results):
    """前台自我檢查的 [(等級, 說明)] → 表格列。"""
    return [(level, text) for level, text in results]


def gpu_line(results):
    """從檢查結果中挑出 GPU 實際推論（毫秒）那一行；沒有回傳 None。"""
    for _level, text in results:
        if "毫秒" in text:
            return text
    return None


def describe_monitors(monitors):
    """螢幕資訊文字行，並標出儀表板與辨識視窗各會放在哪一個（沒有螢幕資料時說明會用目前螢幕）。"""
    if not monitors:
        return ["取不到螢幕資訊（非 Windows 或讀取失敗），程式會用目前螢幕"]
    dashboard = front_screens.pick_monitor(monitors, "dashboard")
    recognition = front_screens.pick_monitor(monitors, "recognition")
    lines = []
    for index, m in enumerate(monitors, start=1):
        roles = [name for name, chosen in (("儀表板", dashboard), ("辨識視窗", recognition)) if chosen is m]
        lines.append(f"螢幕 {index}：{m.width}×{m.height}，位置 ({m.x}, {m.y})，{'主螢幕' if m.primary else '延伸螢幕'}"
                     + (f"，會放：{'、'.join(roles)}" if roles else ""))
    if len(monitors) == 1:
        lines.append("只偵測到 1 個螢幕：儀表板與辨識視窗會放在同一個螢幕（外接顯示器沒接或沒設成延伸模式）")
    return lines


def running_processes(process_iter):
    """儀表板、辨識程式、連線代理是否在執行：看命令列有沒有對應的模組名稱。process_iter 回傳命令列字串清單。"""
    lines = list(process_iter())
    return {label: any(m in line for line in lines for m in modules) for label, modules in PROCESSES}


def _psutil_command_lines():
    """目前所有行程的命令列（系統呼叫，測試時替換）。"""
    import psutil
    out = []
    for proc in psutil.process_iter(["cmdline"]):
        try:
            out.append(" ".join(proc.info.get("cmdline") or []))
        except Exception:  # noqa: BLE001, S112  # nosec B112  行程剛好結束或沒權限，略過
            continue
    return out


def scan_logs(logs_dir, tail=LOG_TAIL_LINES, max_hits=MAX_LOG_HITS):
    """每個 .log 的最後幾行中有 ERROR／WARN／Traceback 的行 {檔名: [行…]}；沒有記錄資料夾回傳空。"""
    out = {}
    try:
        names = sorted(n for n in os.listdir(logs_dir) if n.endswith(".log"))
    except OSError:
        return out
    for name in names:
        try:
            with open(os.path.join(logs_dir, name), encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()[-tail:]
        except OSError:
            continue
        hits = [line.strip()[:200] for line in lines if _LOG_PATTERN.search(line)]
        out[name] = hits[-max_hits:]
    return out


def camera_probe(recognition_running, open_fn=None):
    """攝影機能不能開：辨識正在用就只回報被占用（不算失敗）；回傳 (狀態, 說明)，狀態是 OK／BUSY／FAIL。"""
    if recognition_running:
        return "BUSY", "辨識程式正在使用攝影機，這一項略過（辨識有在跑，代表攝影機是好的）"
    try:
        ok = (open_fn or _open_camera_once)()
    except Exception as e:  # noqa: BLE001
        return "FAIL", f"攝影機檢查出錯：{type(e).__name__}"
    return ("OK", "攝影機可以開並讀到畫面") if ok else ("FAIL", "攝影機打不開或讀不到畫面")


def _open_camera_once():
    """開預設攝影機讀一幀就釋放（系統呼叫）。"""
    import cv2
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    try:
        return bool(cap.isOpened() and cap.read()[0])
    finally:
        cap.release()


def clock_line(offset):
    """前台與後台的時鐘差一行文字；offset 是最近一次量到的秒數（前台比後台快為負、慢為正），None＝沒量過。"""
    if offset is None:
        return "還沒量到時鐘差（前台還沒連上過後台）"
    note = "（超過 30 秒，事件時間已自動校正）" if abs(offset) > 30 else ""
    return f"前台與後台時鐘差約 {offset:+.1f} 秒{note}"


def _read_clock_offset():
    """前台資料庫裡最近一次量到的時鐘差（系統呼叫）。"""
    try:
        from access.front_store import FrontStore
        value = FrontStore(os.path.join(paths.RUNTIME_DIR, "front_agent.db")).get_meta("clock_offset")
        return float(value) if value is not None else None
    except Exception:  # noqa: BLE001
        return None


def collect_checks():
    """跑前台自我檢查各項（攝影機另外處理，避免辨識程式在用時被誤判）。"""
    from access import front_check
    results = []
    for step in (front_check.check_role, front_check.check_files, front_check.check_packages, front_check.check_gpu,
                 front_check.check_wifi, front_check.check_back):
        try:
            results += step()
        except Exception as e:  # noqa: BLE001  單項出意外不中斷
            results.append(("WARN", f"{step.__name__} 執行出錯：{type(e).__name__}"))
    return results


QA_QUESTIONS = (("talk", "Smart_Chat 一般對話", "請用一句話介紹你自己"),
                ("chat", "Smart_RAG 知識庫問答", "公司員工繼續工作滿一年，每年有幾天特別休假？"))
QA_TIMEOUT_SECONDS = 240.0          # 每一題最久等多久（第一題後台要載入模型）


def qa_probe(controllers=None, clock=time.time, sleep=time.sleep, timeout=QA_TIMEOUT_SECONDS):
    """問答連線測試：用前台真正的問答控制器各問一題，回報通過與否、花幾秒、回答長度（不印回答內容）。
    後台兩個開關預設關閉，沒開放就回報「未開放」；前台代理要在跑（它負責同步開關）。回傳報告用的文字行。"""
    from access import front_chat
    from access.front_store import FrontStore

    if controllers is None:
        store = FrontStore(os.path.join(paths.RUNTIME_DIR, "front_agent.db"))
        controllers = {"talk": front_chat.FrontTalk(store), "chat": front_chat.FrontChat(store)}
    lines = []
    for key, label, question in QA_QUESTIONS:
        controller = controllers[key]
        gate = controller.availability()
        if gate != front_chat.IDLE:
            why = "後台沒開放（或前台代理還沒同步到）" if gate == front_chat.CLOSED else "後台離線"
            lines.append(f"{label}：略過，{why}")
            continue
        started = clock()
        if not controller.submit(question):
            lines.append(f"{label}：失敗，送不出問題")
            continue
        view = controller.view()
        while view["state"] in (front_chat.ASKING, front_chat.PREPARING) and clock() - started < timeout:
            sleep(0.5)
            view = controller.view()
        seconds = clock() - started
        if view["state"] == front_chat.ANSWER and view["answer"]:
            extra = f"，來源 {len(view['sources'])} 份" if key == "chat" else ""
            lines.append(f"{label}：通過，{seconds:.1f} 秒，回答 {len(view['answer'])} 字{extra}")
        else:
            lines.append(f"{label}：失敗（{view['state']}：{view['message']}），{seconds:.1f} 秒")
        controller.clear()                                       # 前台不留任何對話
    return lines


def _play_voice():
    """試聽打卡語音一次（系統呼叫）。"""
    from access import audio_player
    audio_player._demo(["葉君緯，早安"])


def _play_alert(kind):
    """響一種警示音型（系統呼叫，會等響完）。"""
    alert_sound._play_with_winsound(alert_sound.pattern_for(kind))


def render(started, sections):
    """報告全文：sections 是 [(標題, [文字行] 或 (表頭, 列) 表格)]。"""
    lines = ["# 前台驗證報告", "", f"產生時間：{started.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for title, body in sections:
        lines += [f"## {title}", ""]
        if isinstance(body, tuple):
            lines.append(md_table(*body))
        else:
            lines += [f"- {text}" for text in body] or ["- （沒有資料）"]
        lines.append("")
    return "\n".join(lines)


def run(console, now=datetime.datetime.now, ask_sounds=True, collect=collect_checks, monitors_fn=None,
        process_fn=_psutil_command_lines, read_heartbeat=None, read_offset=_read_clock_offset, logs_dir=None,
        play_voice=_play_voice, play_alert=_play_alert, camera_fn=None, out_dir=None, qa_fn=None):
    """收集並寫報告；回傳報告路徑。各系統相依都可傳入測試用的假函式。"""
    from access import front_agent
    started = now()
    secrets = [role_config.link_token()]
    folder = out_dir or os.path.join(paths.RUNTIME_DIR, "verification")
    path = os.path.join(folder, f"前台_{started.strftime('%Y-%m-%d_%H%M')}.md")
    sections = []

    def save():
        atomic_write(path, scrub(render(started, sections), secrets))

    results = collect()
    sections.append(("前台自我檢查", (["等級", "說明"], format_check_results(results))))
    no_gpu = "沒有 GPU 實測那一行（可能沒有 NVIDIA 顯卡或走 CPU）"
    sections.append(("GPU 實測速度（5070）", [gpu_line(results) or no_gpu]))
    save()
    monitors = (monitors_fn or front_screens.list_monitors)()
    sections.append(("螢幕", describe_monitors(monitors)))
    procs = running_processes(process_fn)
    sections.append(("執行中的前台程式", [f"{name}：{'執行中' if on else '沒在跑'}" for name, on in procs.items()]))
    heartbeat = (read_heartbeat or front_agent.read_terminal_heartbeat)()
    state, text = camera_probe(bool(heartbeat.get("recognition_running")), camera_fn)
    sections.append(("攝影機", [f"[{state}] {text}"]))
    sections.append(("時鐘", [clock_line(read_offset())]))
    logs = scan_logs(logs_dir or os.path.join(paths.RUNTIME_DIR, "logs"))
    log_lines = []
    for name, hits in logs.items():
        found = f"有 {len(hits)} 行 ERROR／WARN" if hits else "沒有 ERROR／WARN"
        log_lines.append(f"{name}：最近 {LOG_TAIL_LINES} 行{found}")
        log_lines += [f"　{hit}" for hit in hits]
    sections.append(("記錄檔（runtime\\logs）", log_lines or ["沒有記錄檔"]))
    save()
    if qa_fn is not None:                                        # 問答連線測試：命令列入口才會帶（測試預設不跑）
        console.say("\n== 問答連線測試：Smart_Chat 與 Smart_RAG 各問一題（第一題後台要載入模型，最久約 4 分鐘）==")
        try:
            qa_lines = qa_fn()
        except Exception as e:  # noqa: BLE001  單項出意外不中斷
            qa_lines = [f"問答連線測試出錯（{type(e).__name__}）"]
        sections.append(("問答連線測試（Smart_Chat、Smart_RAG）", qa_lines))
        save()
    if ask_sounds:
        human = []
        try:
            console.say("\n== 語音試聽：接下來會念「葉君緯，早安」==")
            console.pause("準備好按 Enter（s 略過、q 結束）")
            try:
                play_voice()
            except Exception as e:  # noqa: BLE001
                human.append(f"語音播放失敗（{type(e).__name__}）")
            else:
                ok, note = console.yes_no("語音聽得到、而且清楚嗎？")
                human.append(f"語音：{_label(ok)} {note}".strip())
            for kind, label in ALERT_LABELS:
                console.say(f"\n== 警示音：{label} ==")
                console.pause("按 Enter 播放（s 略過、q 結束）")
                try:
                    play_alert(kind)
                except Exception as e:  # noqa: BLE001
                    human.append(f"警示音 {label} 播放失敗（{type(e).__name__}）")
                    continue
                ok, note = console.yes_no(f"聽得到 {label} 嗎？")
                human.append(f"警示音 {label}：{_label(ok)} {note}".strip())
        except SkipStep:
            human.append("使用者略過聲音測試")
        except QuitSession:
            human.append("使用者提前結束聲音測試")
        sections.append(("人工回答（聲音）", human))
        save()
    return path


def _label(value):
    return "聽得到、清楚" if value else "不行" if value is False else "沒有明確回答"


def main():
    """命令列入口。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("前台驗證中，請稍候（自我檢查要載入模型，約 20 到 60 秒）…")
    start = time.time()
    path = run(Console(), qa_fn=qa_probe)
    print(f"\n完成（{time.time() - start:.0f} 秒）。報告：{path}")
    print("前台不上網：請把這個檔案用隨身碟複製到後台電腦的 runtime\\verification\\ 資料夾，")
    print("或把內容貼給 Claude，一起對照後台的驗證報告。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
