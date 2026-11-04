"""
tools.py：企業知識庫的唯讀工具清單（每個工具包一個 queries 的固定查詢並負責排版）。

主要功能：
  - TOOLS：工具名稱、說明、是否需要指定員工；router 只能從這份清單選工具，不在清單內的一律不執行
  - run：以名稱執行工具，回傳結果文字、提醒清單與原始資料列；缺少必要參數時丟 ValueError
  - 工具只讀資料庫（queries 是唯讀連線），結果筆數有上限；人名與日期由呼叫端先解析好再傳入

串接模組：
  - queries：固定查詢
  - answer：解析好人名與日期後呼叫 run
  - router：DATA_TOOLS 取自這裡的工具名稱
"""
import datetime
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from rag import queries
from shared import event_labels

MAX_LISTED_ROWS = 30
HOLIDAY_LOOKAHEAD_DAYS = 365
_WEEKDAY_NAMES = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")


@dataclass
class ToolArgs:
    """工具參數；start、end 是 YYYY-MM-DD，ranged 表示問題裡有明確的時間說法。"""
    today: str
    person: str = ""
    start: Optional[str] = None
    end: Optional[str] = None
    ranged: bool = False
    status: Optional[str] = None
    event_type: Optional[str] = None


@dataclass
class ToolResult:
    text: str
    notes: List[str] = field(default_factory=list)
    rows: Optional[list] = None   # 原始資料列（異常事件要用來列證據照片路徑）


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    run: Callable[[ToolArgs], ToolResult]
    needs_person: bool = False       # 一定要指定一位員工
    optional_person: bool = False    # 問題提到某位員工時只看那位


def span_text(start, end):
    return start if start == end else f"{start} 至 {end}"


def lines_block(title: str, lines: List[str], with_total: bool = True) -> str:
    """標題加上清單；筆數只在每行代表一筆紀錄時才標示，查詢數量碰到上限時寫「至少」。"""
    if not lines:
        return f"{title}\n（沒有符合的紀錄）"
    shown = lines[:MAX_LISTED_ROWS]
    more = f"\n…還有 {len(lines) - len(shown)} 筆未列出" if len(lines) > len(shown) else ""
    if not with_total:
        return f"{title}\n" + "\n".join(shown)
    total = f"至少 {len(lines)}" if len(lines) >= queries.MAX_ROWS else str(len(lines))
    return f"{title}（共 {total} 筆）\n" + "\n".join(shown) + more


def summary_text(s: dict) -> str:
    """某天出勤摘要的排版。"""
    def names(items):
        return "、".join(f"{p['name']}（{p['timestamp'][11:16]}）" for p in items) or "無"

    head = f"{s['date']}　出勤摘要" + (f"（{s['day_off']}，不用上班）" if s.get("day_off") else "")
    on_leave = f"請假：{'、'.join(s['on_leave'])}\n" if s.get("on_leave") else ""
    note = ("（休息日沒有打卡屬正常）" if s.get("day_off")
            else "（沒有打卡紀錄不等於缺勤：可能是系統尚未登錄的假日或請假）")
    return (f"{head}\n有打卡人數：{s['punched_count']}\n遲到：{names(s['late'])}\n"
            f"早退：{names(s['early_leave'])}\n加班：{names(s['overtime'])}\n{on_leave}"
            f"在職但當天沒有打卡紀錄：{'、'.join(s['no_record']) or '無'}\n{note}")


def _records(a: ToolArgs) -> ToolResult:
    span = span_text(a.start, a.end) if a.ranged else "全部期間"
    rows = queries.attendance_records(a.person, a.start, a.end, a.status, limit=queries.MAX_ROWS)
    return ToolResult(lines_block(f"{a.person}　{span}　打卡紀錄" + (f"（只看{a.status}）" if a.status else ""),
                                  [f"{r['timestamp']}　{r['status']}" for r in rows]))


def _count(a: ToolArgs) -> ToolResult:
    span = span_text(a.start, a.end) if a.ranged else "全部期間"
    counts = queries.count_by_status(a.person, a.start, a.end)
    lines = [f"{status}：{n} 次" for status, n in sorted(counts.items())]
    return ToolResult(lines_block(f"{a.person}　{span}　各狀態次數", lines, with_total=False))


def _status_list(a: ToolArgs) -> ToolResult:
    notes = []
    status = a.status or "遲到"
    if not a.status:
        notes.append("問題沒有指定狀態，預設查「遲到」。")
    start, end = (a.start, a.end) if a.ranged else (a.today, a.today)
    rows = queries.status_events(start, end, status)
    return ToolResult(lines_block(f"{span_text(start, end)}　{status}紀錄",
                                  [f"{r['timestamp']}　{r['name']}" for r in rows]), notes)


def _summary(a: ToolArgs) -> ToolResult:
    notes = []
    day = a.start if a.ranged and a.start == a.end else a.today
    if a.ranged and a.start != a.end:
        notes.append(f"出勤摘要一次只看一天，已改看 {day}。")
    return ToolResult(summary_text(queries.daily_summary(day)), notes)


def _anomalies(a: ToolArgs) -> ToolResult:
    rows = queries.anomaly_events(a.start, a.end, a.event_type)
    label = f"{span_text(a.start, a.end)}　" if a.ranged else "最近的"
    return ToolResult(lines_block(f"{label}異常事件", [
        f"{r['timestamp']}　{event_labels.describe(r['event_type'], r['detail'], '　')}" for r in rows]), rows=rows)


def _roster(a: ToolArgs) -> ToolResult:
    people = queries.roster()
    active = sum(1 for p in people if p["active"])
    return ToolResult(lines_block(f"員工名單（共 {len(people)} 人，在職 {active} 人）", [
        f"{p['name']}（{p['employee_id']}）{'' if p['active'] else '　已停用'}" for p in people]))


def _leaves(a: ToolArgs) -> ToolResult:
    start, end = (a.start, a.end) if a.ranged else (a.today, a.today)
    rows = queries.leaves(start, end, a.person or None)
    who = f"{a.person}　" if a.person else ""
    return ToolResult(lines_block(f"{span_text(start, end)}　{who}請假", [
        f"{r['name']}　{span_text(r['start_date'], r['end_date'])}" + (f"　{r['kind']}" if r["kind"] else "")
        for r in rows]))


def _holidays(a: ToolArgs) -> ToolResult:
    notes = []
    if a.ranged:
        start, end = a.start, a.end
    else:
        start = a.today
        end = (datetime.date.fromisoformat(a.today) + datetime.timedelta(days=HOLIDAY_LOOKAHEAD_DAYS)).isoformat()
        notes.append("問題沒有指定時間，列出今天起一年內的假日。")
    rows = queries.holidays(start, end)
    rest = "、".join(_WEEKDAY_NAMES[d] for d in queries.rest_weekdays()) or "無"
    text = lines_block(f"{span_text(start, end)}　登錄的假日", [f"{r['date']}　{r['name'] or '假日'}" for r in rows])
    return ToolResult(f"{text}\n每週固定休息日：{rest}", notes)


TOOLS = {t.name: t for t in (
    Tool("records", "某位員工的打卡紀錄", _records, needs_person=True),
    Tool("count", "某位員工各種打卡狀態的次數", _count, needs_person=True),
    Tool("status_list", "某段時間誰遲到、早退或加班", _status_list),
    Tool("summary", "某一天的出勤摘要與誰沒有打卡", _summary),
    Tool("anomalies", "異常事件（尾隨、經過鏡頭未打卡）", _anomalies),
    Tool("roster", "員工名單", _roster),
    Tool("leaves", "某段時間誰請假（可只看某位員工）", _leaves, optional_person=True),
    Tool("holidays", "登錄的假日與每週固定休息日", _holidays),
)}


def run(name: str, args: ToolArgs) -> ToolResult:
    """執行工具；名稱不在清單內或缺少必要的人名就丟 ValueError，不會執行任何查詢。"""
    tool = TOOLS.get(name)
    if tool is None:
        raise ValueError(f"沒有這個工具：{name!r}")
    if tool.needs_person and not args.person:
        raise ValueError(f"工具 {name} 需要指定員工")
    return tool.run(args)
