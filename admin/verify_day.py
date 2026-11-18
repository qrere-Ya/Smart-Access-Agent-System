"""
verify_day.py：驗證當天的引導流程（後台）——一步一步帶著做真機驗證，自動擷取證據，寫成報告，最後可選擇請 Claude 分析。

主要功能：
  - 階段 0 自動檢查（不需要人）：呼叫展示前健檢（admin.preflight，含前台區段與網路檢查），
    並記錄 git 版本、時間、Python 版本
  - 引導項目 1 到 13：印出要做什麼 → 等人按 Enter → 自動擷取證據（前台心跳、異常事件、出勤紀錄、防火牆檢查結果）→ 判定；
    需要人聽或看的（聲音、橫幅、QR、語音、螢幕位置、學校光線下登入）用 y/n 加備註記錄
  - 「停止前台辨識」會真的寫後台設定並輪詢前台心跳，量出前台花了幾秒收掉；不論成功失敗、Ctrl+C，最後一定恢復允許
  - 「拔網路線補送」記下離線與接回的時間，接回後輪詢出勤資料庫，看那一筆有沒有補送進來
  - 報告 runtime/verification/日期_時間/report.md：每完成一項就立刻寫檔；寫入前把通行碼、金鑰、雜湊類的長字串蓋掉
  - 結尾問要不要請 Claude 分析（預設不要）：用 claude -p 只給讀取權限，輸出存成同資料夾的 分析.md
  - --auto：只跑不需要人的項目（健檢、前台心跳、今天的出勤、防火牆），其餘標「未做」

設計：所有系統相依（時間、資料庫路徑、前台狀態、後台遙控開關、鍵盤、claude）都放在 Context，測試時換成假的。

串接模組：
  - admin.preflight／admin.network_check：自動檢查；admin.front_status：前台心跳；shared.front_control：後台遙控開關
  - shared.verify_common：輸入容錯、敏感資訊過濾、報告表格；admin.anomaly_notifier：證據照片路徑檢查
  - 前台那一側請在前台電腦跑 前台驗證.bat（access.front_verify），把報告複製過來
  - 啟動：驗證當天.bat 或 python -m admin.verify_day
"""
import argparse
import csv
import datetime
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess  # noqa: S404  # nosec B404  只執行固定參數的 git 與 claude
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from admin import front_status
from shared import front_control, paths, role_config
from shared.verify_common import (Console, QuitSession, SkipStep, atomic_write, md_table, parse_answer, scrub)

PASS, FAIL, TODO, MANUAL = "通過", "失敗", "未做", "需人工判斷"
ICON = {PASS: "✅", FAIL: "❌", TODO: "⬜", MANUAL: "🟨"}
STAMP = "%Y-%m-%d %H:%M:%S"
POLL_SECONDS = 2.0               # 輪詢前台心跳／資料庫的間隔
STOP_TIMEOUT = 60.0              # 後台停止辨識後，最久等前台收掉幾秒
RESYNC_TIMEOUT = 60.0            # 接回網路線後，最久等補送幾秒
CLAUDE_TIMEOUT = 300             # 請 Claude 分析的最長等待秒數
MAX_EVIDENCE_LINES = 25          # 一項證據最多列幾行（報告不要被洗版）
CLAUDE_PROMPT = ("請閱讀這份前後台雙機驗證報告：{path}\n用繁體中文列出：1) 失敗與可疑的項目，2) 各自可能的原因，"
                 "3) 下一步建議（具體到要檢查哪個檔案或做什麼操作）。只讀取報告，不要修改任何檔案。")


@dataclass
class Result:
    """一個驗證項目的結果。"""
    number: int
    title: str
    status: str = TODO
    evidence: List[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Step:
    """一個引導項目：編號、標題、要做什麼、執行函式、不需要人時能不能跑（--auto）。"""
    number: int
    title: str
    instruction: str
    run: Callable
    auto_ok: bool = False


class Session:
    """一次驗證的證據資料夾與報告；每記錄一項就整份重寫（先寫暫存檔再取代）。"""

    def __init__(self, root, now, secrets=()):
        base = os.path.join(root, now.strftime("%Y-%m-%d_%H%M"))
        folder, n = base, 1
        while os.path.exists(folder):                      # 同一分鐘跑兩次也不會覆蓋
            n += 1
            folder = f"{base}_{n}"
        os.makedirs(folder)
        self.folder, self.started, self.secrets = folder, now, tuple(secrets)
        self.report_path = os.path.join(folder, "report.md")
        self.meta: List[str] = []
        self.results: List[Result] = []
        self.save()

    def record(self, result):
        """加入或更新一項結果並立刻存檔。"""
        self.results = [r for r in self.results if r.number != result.number] + [result]
        self.results.sort(key=lambda r: r.number)
        self.save()

    def save(self):
        atomic_write(self.report_path, scrub(render_report(self.started, self.meta, self.results), self.secrets))


def summary_counts(results):
    """各種結果的數量。"""
    return {status: sum(1 for r in results if r.status == status) for status in (PASS, FAIL, MANUAL, TODO)}


def render_report(started, meta, results):
    """報告全文（Markdown）。"""
    counts = summary_counts(results)
    lines = ["# 前後台雙機驗證報告", "", f"開始時間：{started.strftime(STAMP)}", ""]
    lines += [f"- {m}" for m in meta]
    lines += ["", "## 總表", "",
              f"通過 {counts[PASS]}　失敗 {counts[FAIL]}　需人工判斷 {counts[MANUAL]}　未做 {counts[TODO]}", ""]
    rows = [(r.number, r.title, f"{ICON[r.status]} {r.status}", "\n".join(r.evidence[:MAX_EVIDENCE_LINES]), r.note)
            for r in results]
    lines.append(md_table(["#", "項目", "結果", "證據", "說明"], rows))
    return "\n".join(lines) + "\n"


# --- 資料庫（只讀）---

def _connect_ro(db_path):
    """以唯讀方式開資料庫（驗證流程絕不寫入真資料）。"""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=3)


def query_anomalies_since(db_path, since):
    """開始時間之後發生或持續到之後的異常事件 [{id, time, kind, detail, evidence, count}]（合併事件看最後發生時間）。"""
    stamp = since.strftime(STAMP)
    with closing(_connect_ro(db_path)) as con:
        rows = con.execute("SELECT id, timestamp, event_type, detail, evidence_path, event_count FROM AnomalyEvents "
                           "WHERE timestamp >= ? OR COALESCE(end_timestamp, timestamp) >= ? ORDER BY id",
                           (stamp, stamp)).fetchall()
    return [{"id": r[0], "time": r[1], "kind": r[2], "detail": r[3] or "", "evidence": r[4] or "",
             "count": r[5] or 1} for r in rows]


def query_attendance_since(db_path, since):
    """開始時間之後的出勤紀錄 [{time, name, status}]（時間是事件發生時間，離線補送的也是原本打卡的時間）。"""
    with closing(_connect_ro(db_path)) as con:
        rows = con.execute("SELECT timestamp, name, status FROM Attendance WHERE timestamp >= ? ORDER BY timestamp",
                           (since.strftime(STAMP),)).fetchall()
    return [{"time": r[0], "name": r[1], "status": r[2]} for r in rows]


def query_attendance_today(db_path, today):
    """今天所有出勤紀錄（舊到新）。"""
    with closing(_connect_ro(db_path)) as con:
        rows = con.execute("SELECT timestamp, name, status FROM Attendance WHERE timestamp LIKE ? ORDER BY timestamp",
                           (today.strftime("%Y-%m-%d") + "%",)).fetchall()
    return [{"time": r[0], "name": r[1], "status": r[2]} for r in rows]


def copy_evidence(events, dest_dir, root=None):
    """把異常事件的證據照片複製進報告資料夾（路徑必須在證據資料夾內），回傳複製後的檔名清單。"""
    from admin.anomaly_notifier import safe_evidence_path
    copied = []
    for event in events:
        source = safe_evidence_path(event.get("evidence"), root)
        if source:
            name = f"事件{event['id']}_{Path(source).name}"
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(source, os.path.join(dest_dir, name))
            copied.append(name)
    return copied


def event_lines(events):
    """異常事件的證據文字行。"""
    from shared import event_labels
    return [f"{e['time']}　{event_labels.describe(e['kind'], e['detail'])}（{e['count']} 次）" for e in events]


# --- 判定（純函式）---

def judge_no_tailgate(events):
    """多人同框但只有遠處路人：不應該出現尾隨事件。回傳 (結果, 說明)。"""
    tails = [e for e in events if e["kind"] == "tailgate"]
    if tails:
        return FAIL, f"出現 {len(tails)} 筆尾隨事件，遠處的人被算進去了"
    return PASS, "沒有尾隨事件"


def judge_tailgate(events):
    """兩人貼近同進：應該有尾隨事件，而且有證據照片。回傳 (結果, 說明)。"""
    tails = [e for e in events if e["kind"] == "tailgate"]
    if not tails:
        return FAIL, "沒有尾隨事件（兩人貼近同進卻沒有被偵測到）"
    if not any(e["evidence"] for e in tails):
        return FAIL, f"有 {len(tails)} 筆尾隨事件，但都沒有證據照片"
    return PASS, f"偵測到 {len(tails)} 筆尾隨事件，有證據照片"


def combine(auto_status, answers):
    """自動判定加人工回答：任何一邊失敗就失敗；有回答看不懂（None）算需人工判斷；全過才通過。"""
    if auto_status == FAIL or any(a is False for a in answers):
        return FAIL
    if auto_status == MANUAL or any(a is None for a in answers):
        return MANUAL
    return PASS


def poll_until(check, timeout, interval, clock=time.monotonic, sleep=time.sleep):
    """每隔 interval 秒呼叫 check，成立回傳花了幾秒，超過 timeout 回傳 None。"""
    start = clock()
    while True:
        if check():
            return clock() - start
        if clock() - start >= timeout:
            return None
        sleep(interval)


def probe_summary(folder):
    """活體量測資料夾（scores.csv）每個模型分數的摘要文字行；讀不到回傳空清單。"""
    try:
        with open(os.path.join(folder, "scores.csv"), newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
    except OSError:
        return []
    if len(rows) < 2 or len(rows[0]) <= 7:
        return []
    lines = []
    for index, model in enumerate(rows[0][7:], start=7):
        values = []
        for row in rows[1:]:
            try:
                values.append(float(row[index]))
            except (IndexError, ValueError):
                continue
        if values:
            lines.append(f"{os.path.basename(folder)}｜{model}：{len(values)} 幀，最小 {min(values):.3f}／"
                         f"中位數 {statistics.median(values):.3f}／最大 {max(values):.3f}，"
                         f"低於 0.85 的有 {sum(v < 0.85 for v in values) / len(values):.0%}")
    return lines


def todays_probe_lines(root, today):
    """今天量過的活體資料夾摘要。"""
    stamp = today.strftime("%Y%m%d")
    try:
        names = sorted(n for n in os.listdir(root) if stamp in n)
    except OSError:
        return []
    lines = []
    for name in names:
        lines += probe_summary(os.path.join(root, name))
    return lines


# --- 執行環境 ---

@dataclass
class Context:
    """所有系統相依；測試時換掉。"""
    console: Console
    session: Session
    now: Callable = datetime.datetime.now
    sleep: Callable = time.sleep
    clock: Callable = time.monotonic
    db_path: Optional[str] = None
    load_front: Callable = front_status.load
    set_recognition: Callable = front_control.set_recognition_enabled
    anomaly_root: Optional[str] = None
    probe_root: Optional[str] = None
    run_preflight: Optional[Callable] = None
    auto: bool = False

    def db(self):
        return self.db_path or paths.DB_PATH


def _front_line(state):
    """前台心跳的一行文字。"""
    if not state["seen"]:
        return "後台還沒收到過前台的回報"
    s = state["status"]
    running = "執行中" if s.get("recognition_running") else "未執行"
    camera = "正常" if s.get("camera_ok") else "異常"
    link = "在線" if state["online"] else "離線"
    return (f"{link}（{int(state['age'] or 0)} 秒前回報）；辨識{running}；鏡頭{camera}；"
            f"人臉資料 {s.get('faces', 0)} 筆；待補送 {s.get('pending_events', 0)} 筆")


def _db_error(result, error):
    """資料庫讀不到時的結果。"""
    result.status, result.note = FAIL, f"讀不到後台資料庫（{type(error).__name__}）"
    return result


def phase0(ctx):
    """階段 0：自動檢查（展示前健檢含前台與網路）；同時回傳完整 Check 清單給後面的項目用。"""
    result = Result(0, "自動檢查（展示前健檢＋網路）")
    try:
        from admin import preflight
        checks = (ctx.run_preflight or preflight.run_all)()
    except Exception as e:  # noqa: BLE001  健檢本身出意外也要有報告
        result.status, result.note = FAIL, f"健檢執行出錯：{type(e).__name__}"
        return result, []
    bad = [c for c in checks if c.status != "通過"]
    fails = sum(c.status == "失敗" for c in checks)
    result.evidence = [f"[{c.status}] {c.name}：{c.detail}" for c in bad] or ["全部通過"]
    result.status = FAIL if fails else PASS
    result.note = f"共 {len(checks)} 項，失敗 {fails}、注意 {len(bad) - fails}"
    return result, checks


def step_front_running(ctx):
    """1 前台啟動人臉辨識能正常跑：讀前台心跳。"""
    result = Result(1, "前台啟動人臉辨識能正常跑")
    state = ctx.load_front()
    result.evidence = [_front_line(state)]
    status = state["status"]
    if state["online"] and status.get("recognition_running") and status.get("camera_ok"):
        result.status = PASS
    else:
        result.status = FAIL
        result.note = "前台要在線、辨識執行中、鏡頭正常；請按前台「▶ 啟動人臉辨識」再重做這一項"
    return result


def step_multi_person(ctx):
    """2 多人同框只處理最近的人、遠處的人不算尾隨。"""
    result = Result(2, "多人同框只處理最近的人、遠處不算尾隨")
    since = ctx.now()
    ctx.console.pause()
    try:
        events = query_anomalies_since(ctx.db(), since)
    except sqlite3.Error as e:
        return _db_error(result, e)
    auto_status, text = judge_no_tailgate(events)
    result.evidence = [text] + event_lines(events)
    ok, note = ctx.console.yes_no("最近那個人有被正常辨識與打卡、遠處的人沒有觸發尾隨橫幅嗎？")
    result.status = combine(auto_status, [ok])
    result.note = f"人工回答：{'是' if ok else '否' if ok is False else '看不懂'} {note}".strip()
    return result


def step_tailgate_spoof(ctx):
    """3 尾隨與非活體：橫幅加聲音。"""
    result = Result(3, "尾隨與非活體：橫幅加聲音")
    since = ctx.now()
    ctx.console.say("先做尾隨：兩個人貼近、幾乎同時通過鏡頭。")
    ctx.console.pause("尾隨做完按 Enter")
    try:
        events = query_anomalies_since(ctx.db(), since)
    except sqlite3.Error as e:
        return _db_error(result, e)
    auto_status, text = judge_tailgate(events)
    copied = copy_evidence([e for e in events if e["kind"] == "tailgate"],
                           os.path.join(ctx.session.folder, "證據"), ctx.anomaly_root)
    result.evidence = [text] + event_lines(events) + [f"證據照片：{name}" for name in copied]
    tail_ok, tail_note = ctx.console.yes_no("尾隨：有看到紅色橫幅，並聽到三聲急促的高音嗎？")
    ctx.console.say("再做非活體：拿照片或手機螢幕翻拍的臉對著鏡頭。")
    ctx.console.pause("非活體做完按 Enter")
    spoof_ok, spoof_note = ctx.console.yes_no("非活體：有顯示非活體橫幅，並聽到兩聲低沉的長音嗎？")
    result.status = combine(auto_status, [tail_ok, spoof_ok])
    result.note = (f"尾隨人工：{_yn(tail_ok)} {tail_note}；非活體人工：{_yn(spoof_ok)} {spoof_note}").strip()
    return result


def _yn(value):
    return "是" if value else "否" if value is False else "看不懂"


def step_login_light(ctx):
    """4 學校光線下後台登入活體 0.85 夠不夠。"""
    result = Result(4, "學校光線下後台登入（活體門檻 0.85）")
    ctx.console.say("可先量分數：python -m access.liveness_probe --label genuine --seconds 30（正對鏡頭）。")
    ctx.console.pause("在學校的光線下，登出後台再用臉登入一次；做完按 Enter")
    lines = todays_probe_lines(ctx.probe_root or os.path.join(paths.RUNTIME_DIR, "liveness_probe"), ctx.now().date())
    result.evidence = lines or ["今天沒有活體量測資料（沒跑 access.liveness_probe）"]
    ok, note = ctx.console.yes_no("後台人臉登入順利嗎（一兩次內就進得去）？")
    result.status = combine(PASS, [ok])
    result.note = f"人工回答：{_yn(ok)} {note}".strip()
    return result


def step_stop_recognition(ctx):
    """5 後台按「停止前台辨識」，前台約 10 秒收掉；一定恢復允許。"""
    result = Result(5, "後台停止前台辨識，前台約 10 秒收掉")
    state = ctx.load_front()
    if not state["online"] or not state["status"].get("recognition_running"):
        result.note = "前台要在線且辨識執行中才能做這一項；先按前台「▶ 啟動人臉辨識」"
        result.evidence = [_front_line(state)]
        return result
    ctx.console.pause("這一項會自動把前台辨識停掉，按 Enter 開始")

    def stopped():
        s = ctx.load_front()
        return s["online"] and not s["status"].get("recognition_running")

    try:
        ctx.set_recognition(False)
        elapsed = poll_until(stopped, STOP_TIMEOUT, POLL_SECONDS, ctx.clock, ctx.sleep)
    finally:
        ctx.set_recognition(True)                         # 不論成功、逾時、Ctrl+C 都恢復允許
    if elapsed is None:
        result.status, result.note = FAIL, f"{int(STOP_TIMEOUT)} 秒內前台辨識沒有停止"
    else:
        result.status, result.note = PASS, f"前台約 {elapsed:.0f} 秒後收掉辨識"
    result.evidence = [_front_line(ctx.load_front()), "已恢復後台允許；前台要人再按「▶ 啟動人臉辨識」"]
    ctx.console.say("後台已恢復允許；請在前台再按一次「▶ 啟動人臉辨識」。")
    return result


def step_offline_resync(ctx):
    """6 拔網路線再接回：暫存與補送。"""
    result = Result(6, "拔網路線再接回：暫存與補送")
    ctx.console.say("1) 拔掉前後台之間的網路線　2) 在前台打卡一次　3) 等約 10 秒")
    t_off = ctx.now()
    ctx.console.pause("拔線並打卡後按 Enter")
    during = ctx.load_front()
    result.evidence.append(f"拔線期間後台看到的前台：{_front_line(during)}")
    ctx.console.say("現在把網路線接回去。")
    ctx.console.pause("接回後按 Enter")
    t_on = ctx.now()

    def arrived():
        try:
            return any(row["time"] >= t_off.strftime(STAMP) for row in query_attendance_since(ctx.db(), t_off))
        except sqlite3.Error:
            return False

    elapsed = poll_until(arrived, RESYNC_TIMEOUT, POLL_SECONDS, ctx.clock, ctx.sleep)
    try:
        rows = query_attendance_since(ctx.db(), t_off)
    except sqlite3.Error as e:
        return _db_error(result, e)
    result.evidence += [f"{r['time']}　{r['name']}　{r['status']}" for r in rows] or ["拔線之後沒有任何出勤紀錄"]
    result.evidence.append(f"接回後前台：{_front_line(ctx.load_front())}")
    off_seen = not during["online"]
    if elapsed is None:
        result.status, result.note = FAIL, f"接回後 {int(RESYNC_TIMEOUT)} 秒內沒有看到離線期間的打卡補送進來"
    else:
        result.status = PASS if off_seen else MANUAL
        result.note = f"接回後約 {elapsed:.0f} 秒補送完成"
        if not off_seen:
            result.note += "；但拔線期間後台沒看到前台離線，請確認真的拔了線"
    result.note += f"（離線 {t_off.strftime('%H:%M:%S')}，接回 {t_on.strftime('%H:%M:%S')}）"
    return result


def step_real_punch(ctx):
    """7 早上時段真實打卡寫入。"""
    result = Result(7, "早上時段真實打卡寫入")
    ctx.console.pause("請在早上（打卡時段）用前台真的打一次卡，做完按 Enter")
    try:
        rows = query_attendance_today(ctx.db(), ctx.now().date())
    except sqlite3.Error as e:
        return _db_error(result, e)
    result.evidence = [f"{r['time']}　{r['name']}　{r['status']}" for r in rows[-5:]] or ["今天沒有出勤紀錄"]
    result.status = PASS if rows else FAIL
    result.note = f"今天共 {len(rows)} 筆出勤紀錄" if rows else "今天沒有任何出勤紀錄（可能不在打卡時段）"
    return result


def step_today_punch_auto(ctx):
    """7（--auto）只讀今天的出勤，不等人。"""
    result = Result(7, "早上時段真實打卡寫入（只讀今天的紀錄）")
    try:
        rows = query_attendance_today(ctx.db(), ctx.now().date())
    except sqlite3.Error as e:
        return _db_error(result, e)
    result.evidence = [f"{r['time']}　{r['name']}　{r['status']}" for r in rows[-5:]] or ["今天沒有出勤紀錄"]
    result.status = PASS if rows else MANUAL
    result.note = f"今天共 {len(rows)} 筆" if rows else "今天還沒有出勤紀錄，之後要真的打卡再用互動模式驗證"
    return result


def _manual_step(number, title, question, hint=""):
    """純人工判斷的項目（QR、第二螢幕、語音…）。"""
    def run(ctx):
        result = Result(number, title)
        if hint:
            ctx.console.say(hint)
        ok, note = ctx.console.yes_no(question)
        result.status = combine(PASS, [ok])
        result.note = f"人工回答：{_yn(ok)} {note}".strip()
        return result
    return run


def step_qr_phone(ctx):
    """8 手機掃新聞 QR。"""
    ctx.console.say("請用手機掃前台儀表板「早報」每則新聞右邊的 QR Code。")
    ctx.console.pause("掃完按 Enter")
    return _manual_step(8, "手機掃新聞 QR", "手機有開到對應的原文網頁嗎？")(ctx)


def step_firewall(ctx, checks):
    """9 前台防火牆規則（後台這一側由健檢判定，前台那一側看前台報告）。"""
    result = Result(9, "防火牆規則（後台側自動判定）")
    items = [c for c in checks if "防火牆" in c.name]
    if not items:
        result.status, result.note = MANUAL, "健檢沒有回傳防火牆項目（可能不是後台角色）"
        return result
    result.evidence = [f"[{c.status}] {c.name}：{c.detail}" for c in items]
    if any(c.status == "失敗" for c in items):
        result.status = FAIL
    elif all(c.status == "通過" for c in items):
        result.status = PASS
    else:
        result.status = MANUAL
    result.note = "前台那一側的規則看前台報告（前台驗證.bat）"
    return result


def step_second_screen(ctx):
    """10 真第二螢幕與 F11。"""
    ctx.console.say("儀表板應在第二螢幕、辨識視窗在筆電螢幕；兩邊各按一次 F11 切全螢幕再切回。")
    ctx.console.say("前台驗證報告會列出螢幕資訊。")
    ctx.console.pause("看完按 Enter")
    return _manual_step(10, "真第二螢幕：位置與 F11", "兩個視窗位置都對、F11 都正常嗎？")(ctx)


def step_gpu_line(ctx):
    """11 5070 的 GPU 實測速度那一行（在前台報告）。"""
    ctx.console.say("請看前台驗證報告裡「GPU 實際推論」那一行（每次約幾毫秒、CPU 約幾毫秒）。")
    ctx.console.pause("看完按 Enter")
    return _manual_step(11, "5070 的 GPU 實測速度", "報告顯示 GPU 比 CPU 快（沒有警告）嗎？")(ctx)


def step_voice(ctx):
    """12 打卡語音清楚度與音量。"""
    ctx.console.say("請在前台打卡一次，聽「名字（停頓）早安」的語音。")
    ctx.console.pause("聽完按 Enter")
    return _manual_step(12, "打卡語音清楚度與音量", "語音清楚、速度剛好、音量夠大嗎？")(ctx)


QA_SOURCES = (("front_talk", "Smart_Chat 一般對話"), ("front_knowledge", "Smart_RAG 知識庫問答"))
QA_WINDOW_HOURS = 6


def query_front_qa(db_path, since):
    """前台問答（兩種來源）在 since 之後的紀錄數與最近一題（只讀）：{來源: (筆數, 最近時間)}。"""
    conn = _connect_ro(db_path)
    try:
        out = {}
        for source, _label in QA_SOURCES:
            row = conn.execute("SELECT COUNT(*), MAX(ts) FROM ChatLogs WHERE source = ? AND ts >= ?",
                               (source, since)).fetchone()
            out[source] = (row[0], row[1])
        return out
    finally:
        conn.close()


def step_front_qa(ctx):
    """13 前台 Smart_Chat 與 Smart_RAG 的問答連線：前台用 前台驗證.bat 的「問答連線測試」（或手動）各問一題後，
    後台的對話紀錄裡要有這兩種來源的紀錄（只讀資料庫，不需要等人）。"""
    result = Result(13, "前台問答連線（Smart_Chat、Smart_RAG）")
    since = (ctx.now() - datetime.timedelta(hours=QA_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        found = query_front_qa(ctx.db(), since)
    except sqlite3.Error as e:
        return _db_error(result, e)
    result.evidence = [f"{label}：最近 {QA_WINDOW_HOURS} 小時 {found[source][0]} 題"
                       + (f"（最近一題 {found[source][1]}）" if found[source][0] else "")
                       for source, label in QA_SOURCES]
    got = [source for source, _label in QA_SOURCES if found[source][0] > 0]
    if len(got) == len(QA_SOURCES):
        result.status, result.note = PASS, "兩種前台問答都有送到後台並被記錄"
    elif got:
        result.status, result.note = MANUAL, "只有一種有紀錄；另一種要先在後台「前台連線」卡片開放，再到前台問一題"
    else:
        result.status = MANUAL
        result.note = ("沒有前台問答紀錄：兩個開關可能被關閉，請先在後台「前台連線」卡片開放，"
                       "再在前台執行「前台驗證.bat」的問答連線測試")
    return result


def build_steps(checks_holder):
    """所有引導項目（編號 1 到 13）；項目 9 要用階段 0 的健檢結果。"""
    return [
        Step(1, "前台啟動人臉辨識", "前台要在線，並已按「▶ 啟動人臉辨識」。", step_front_running, auto_ok=True),
        Step(2, "多人同框", "一人站近鏡頭打卡，另一人站遠處走動約 30 秒。", step_multi_person),
        Step(3, "尾隨與非活體", "兩人貼近同進（尾隨），再拿照片翻拍（非活體）。", step_tailgate_spoof),
        Step(4, "學校光線下後台登入", "在學校光線下用臉登入後台。", step_login_light),
        Step(5, "後台停止前台辨識", "自動停止前台辨識並計時，最後自動恢復。", step_stop_recognition),
        Step(6, "拔線補送", "拔網路線→前台打卡→接回，看補送。", step_offline_resync),
        Step(7, "早上真實打卡", "早上時段在前台打卡。", step_real_punch),
        Step(8, "手機掃 QR", "手機掃前台新聞的 QR。", step_qr_phone),
        Step(9, "防火牆規則", "自動判定（後台側）。", lambda ctx: step_firewall(ctx, checks_holder.get("checks", [])),
             auto_ok=True),
        Step(10, "第二螢幕與 F11", "看儀表板與辨識視窗的位置、F11。", step_second_screen),
        Step(11, "GPU 實測速度", "看前台驗證報告的 GPU 那一行。", step_gpu_line),
        Step(12, "語音清楚度與音量", "前台打卡聽語音。", step_voice),
        Step(13, "前台問答連線", "自動判定（讀對話紀錄）；前台先做問答連線測試。", step_front_qa, auto_ok=True),
    ]


# --- 請 Claude 分析 ---

def run_claude_analysis(report_path, out_path, which=shutil.which, runner=subprocess.run):
    """用 claude -p 分析報告（只給 Read 權限；提示文字走標準輸入，避免命令列特殊字元）；回傳 (成功與否, 說明)。"""
    exe = which("claude")
    if not exe:
        return False, "找不到 claude 指令，略過分析（報告本身不受影響，可以把 report.md 貼給 Claude）"
    folder = os.path.dirname(os.path.abspath(report_path))
    prompt = CLAUDE_PROMPT.format(path=os.path.abspath(report_path))
    try:
        done = runner([exe, "-p", "--allowedTools", "Read"], input=prompt.encode("utf-8"), capture_output=True,  # noqa: S603
                      cwd=folder, timeout=CLAUDE_TIMEOUT, check=False)                                  # nosec B603
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"呼叫 claude 失敗（{type(e).__name__}），略過分析"
    text = (done.stdout or b"").decode("utf-8", "replace").strip()
    if done.returncode != 0 or not text:
        return False, f"claude 沒有回傳分析結果（結束碼 {done.returncode}），略過"
    atomic_write(out_path, scrub(text))
    return True, f"分析已存到 {out_path}"


# --- 主流程 ---

def git_commit():
    """目前的 git 版本（短碼）；不在 git 專案或沒有 git 回傳「未知」。"""
    try:
        done = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,  # noqa: S603,S607
                              timeout=10, check=False, cwd=paths.PROJECT_ROOT)                           # nosec B603 B607
        return done.stdout.strip() or "未知"
    except (OSError, subprocess.SubprocessError):
        return "未知"


def run_session(ctx, commit="未知"):
    """跑完整流程並持續存檔；回傳結果清單。"""
    session = ctx.session
    session.meta = [f"git 版本：{commit}", f"Python：{platform.python_version()}",
                    f"角色：{role_config.role()}", f"模式：{'只跑自動項目（--auto）' if ctx.auto else '互動引導'}"]
    ctx.console.say("== 階段 0：自動檢查（約十幾秒）==")
    result, checks = phase0(ctx)
    session.record(result)
    holder = {"checks": checks}
    ctx.console.say(f"　{ICON[result.status]} {result.title}：{result.note}")
    steps = build_steps(holder)
    for number in range(1, 14):                                   # 先把全部項目列成「未做」，中途結束報告也完整
        step = next(s for s in steps if s.number == number)
        session.record(Result(number, step.title, TODO, [], "尚未進行"))
    quitting = False
    for step in steps:
        if quitting:
            break
        if ctx.auto and not step.auto_ok and step.number != 7:
            session.record(Result(step.number, step.title, TODO, [], "--auto 不做需要人的項目"))
            continue
        ctx.console.say(f"\n== 項目 {step.number}：{step.title} ==\n{step.instruction}")
        try:
            run = step_today_punch_auto if (ctx.auto and step.number == 7) else step.run
            outcome = run(ctx)
        except SkipStep:
            outcome = Result(step.number, step.title, TODO, [], "使用者略過")
        except QuitSession:
            outcome = Result(step.number, step.title, TODO, [], "使用者結束")
            quitting = True
        except Exception as e:  # noqa: BLE001  單一項目出意外不能讓整個流程沒有報告
            outcome = Result(step.number, step.title, FAIL, [], f"這一項執行時出錯：{type(e).__name__}")
        session.record(outcome)
        ctx.console.say(f"　{ICON[outcome.status]} {outcome.status}　{outcome.note}")
    return session.results


def finish(ctx, no_claude=False, which=shutil.which, runner=subprocess.run):
    """印總表、報告位置，並詢問要不要請 Claude 分析（預設不要）。"""
    counts = summary_counts(ctx.session.results)
    ctx.console.say(f"\n== 總表 ==\n通過 {counts[PASS]}　失敗 {counts[FAIL]}　需人工判斷 {counts[MANUAL]}　"
                    f"未做 {counts[TODO]}\n報告：{ctx.session.report_path}")
    if ctx.auto or no_claude:
        return None
    try:
        answer = ctx.console.ask("要請 Claude 分析這份報告嗎？（y/N）> ")
    except (QuitSession, SkipStep):
        return None
    if parse_answer(answer)[0] != "y":                      # 全形、大小寫、「是」都認得；其他一律當作不要
        return None
    ok, text = run_claude_analysis(ctx.session.report_path, os.path.join(ctx.session.folder, "分析.md"), which, runner)
    ctx.console.say(text)
    return ok


def main(argv=None):
    """命令列入口：0＝沒有失敗項，1＝有失敗項。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="驗證當天引導流程（後台）")
    parser.add_argument("--auto", action="store_true", help="只跑不需要人的項目")
    parser.add_argument("--no-claude", action="store_true", help="結尾不詢問是否請 Claude 分析")
    parser.add_argument("--out", default=os.path.join(paths.RUNTIME_DIR, "verification"), help="報告放的資料夾")
    args = parser.parse_args(argv)
    secrets = [role_config.link_token()]
    session = Session(args.out, datetime.datetime.now(), secrets)
    ctx = Context(Console(), session, auto=args.auto)
    print(f"證據與報告資料夾：{session.folder}")
    try:
        results = run_session(ctx, git_commit())
    except KeyboardInterrupt:
        print("\n已中斷；已完成的項目都留在報告裡。")
        results = session.results
    finish(ctx, args.no_claude)
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
