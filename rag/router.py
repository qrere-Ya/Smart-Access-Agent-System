"""
router.py：判斷一個問題要「查文件」、「查資料庫」，還是兩者都要（意圖判斷）。

主要功能：
  - 請 LLM 只輸出一段 JSON：要不要查文件、要用哪個固定查詢、問的是誰、什麼狀態、什麼事件類型
  - LLM 的輸出一律先驗證：查詢種類、狀態只接受白名單內的值，人名長度有限制，格式不對就當作失敗
  - LLM 失敗或輸出不合格時，退回用關鍵字判斷，所以意圖判斷不會讓整個流程中斷
  - 日期和人名不在這裡決定：日期由 dates 解析、人名由 entity_resolver 對照名單

串接模組：
  - llm：提供意圖判斷用的 LLM
  - answer：依判斷結果決定呼叫 queries 的哪個函式、要不要做文件檢索
"""
import datetime
import json
import re
from dataclasses import dataclass
from typing import Optional

DATA_TOOLS = ("records", "count", "status_list", "summary", "anomalies", "roster")
STATUS_ALIASES = {"遲到": "遲到", "早退": "早退", "上班": "上班", "下班": "下班",
                  "加班": "下班 (加班)", "下班 (加班)": "下班 (加班)"}
EVENT_TYPES = ("tailgate",)
_WEEKDAYS = "一二三四五六日"
_MAX_PERSON_CHARS = 20

_DATA_WORDS = ("打卡", "出勤", "遲到", "早退", "缺勤", "沒打卡", "上班時間", "下班時間", "加班時數", "考勤",
               "異常", "尾隨", "員工名單", "有哪些員工", "幾位員工", "幾個員工")
_DOC_WORDS = ("規定", "法", "條", "辦法", "要點", "規則", "權益", "補償", "資遣", "特休", "休假", "請假", "工資",
              "薪資", "怎麼", "如何", "可以嗎", "合法", "違法", "應該", "是否", "多少", "幾天", "幾小時", "幾年",
              "條件", "程序", "流程", "文件")


@dataclass
class Plan:
    docs: bool
    data: Optional[str] = None
    person: str = ""
    status: Optional[str] = None
    event_type: Optional[str] = None
    source: str = "llm"  # llm 或 heuristic


_EXAMPLES = """範例：
問題：特別休假有幾天？
{"docs": true, "data": "none", "person": "", "status": "", "event_type": ""}
問題：王小明這個月遲到幾次？
{"docs": false, "data": "count", "person": "王小明", "status": "遲到", "event_type": ""}
問題：今天誰遲到？
{"docs": false, "data": "status_list", "person": "", "status": "遲到", "event_type": ""}
問題：李大華昨天的打卡紀錄
{"docs": false, "data": "records", "person": "李大華", "status": "", "event_type": ""}
問題：今天的出勤狀況怎麼樣？
{"docs": false, "data": "summary", "person": "", "status": "", "event_type": ""}
問題：最近有沒有尾隨事件？
{"docs": false, "data": "anomalies", "person": "", "status": "", "event_type": "tailgate"}
問題：公司有哪些員工？
{"docs": false, "data": "roster", "person": "", "status": "", "event_type": ""}
問題：陳志遠這個月遲到五次，公司可以怎麼處理？
{"docs": true, "data": "count", "person": "陳志遠", "status": "遲到", "event_type": ""}
問題：你好
{"docs": false, "data": "none", "person": "", "status": "", "event_type": ""}"""


def build_prompt(question: str, history: str, today: datetime.date) -> str:
    """意圖判斷的提示詞：說明欄位、給範例，只要求輸出 JSON。"""
    weekday = _WEEKDAYS[today.weekday()]
    return (
        "你是門禁與人資系統的意圖判斷器。判斷使用者的問題，只輸出一行 JSON，不要任何其他文字。\n"
        "欄位：\n"
        "- docs：問題需要查公司文件、法規、辦法的內容就是 true，否則 false。\n"
        "- data：要查資料庫時填一種：records（某人的打卡紀錄）、count（某人各種狀態的次數）、"
        "status_list（某段時間誰遲到、早退、加班）、summary（某天的出勤摘要或誰沒打卡）、"
        "anomalies（異常、尾隨事件）、roster（員工名單）；不需要查資料庫填 none。\n"
        "- person：問題裡提到的人名，沒有就留空字串。\n"
        "- status：遲到、早退、上班、下班、加班 其中之一，沒有就留空字串。\n"
        "- event_type：問異常或尾隨事件時填 tailgate，否則留空字串。\n"
        "日期由別的程式處理，不用輸出。\n"
        f"{_EXAMPLES}\n"
        f"今天是 {today.isoformat()}（星期{weekday}）。\n"
        f"{('前面的對話：' + history + chr(10)) if history else ''}"
        f"問題：{question}\n"
    )


def parse_plan(text: str) -> Optional[Plan]:
    """從 LLM 輸出取出 JSON 並驗證；格式不對或欄位不合法回傳 None。"""
    match = re.search(r"\{.*?\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("docs"), bool):
        return None
    data = raw.get("data") or "none"
    if data == "none":
        data = None
    elif data not in DATA_TOOLS:
        return None
    status = (raw.get("status") or "").strip()
    if status and status not in STATUS_ALIASES:
        return None
    event_type = (raw.get("event_type") or "").strip()
    if event_type and event_type not in EVENT_TYPES:
        return None
    person = raw.get("person") or ""
    if not isinstance(person, str):
        return None
    person = person.strip()
    if len(person) > _MAX_PERSON_CHARS:
        return None
    return Plan(docs=raw["docs"], data=data, person=person, status=STATUS_ALIASES.get(status),
                event_type=event_type or None)


def heuristic_plan(question: str) -> Plan:
    """LLM 失敗時的備案：用關鍵字判斷；簡單但不會出錯。"""
    wants_data = any(word in question for word in _DATA_WORDS)
    status = next((STATUS_ALIASES[w] for w in ("遲到", "早退", "加班") if w in question), None)
    tool = None
    if wants_data:
        if "異常" in question or "尾隨" in question:
            tool = "anomalies"
        elif "員工名單" in question or "有哪些員工" in question or "幾位員工" in question:
            tool = "roster"
        elif "誰" in question and status:
            tool = "status_list"
        elif "幾次" in question or "次數" in question:
            tool = "count"
        elif "沒打卡" in question or "出勤狀況" in question or "出勤情形" in question or "缺勤" in question:
            tool = "summary"
        else:
            tool = "records"
    wants_docs = not wants_data or any(word in question for word in _DOC_WORDS)
    return Plan(docs=wants_docs, data=tool, status=status,
                event_type="tailgate" if tool == "anomalies" else None, source="heuristic")


def make_plan(llm, question: str, history: str, today: datetime.date) -> Plan:
    """先問 LLM；失敗或輸出不合格就用關鍵字備案。"""
    if llm is not None:
        try:
            plan = parse_plan(llm.complete(build_prompt(question, history, today)))
        except Exception as e:
            print(f"[router] 意圖判斷失敗，改用關鍵字備案：{type(e).__name__}")
            plan = None
        if plan is not None:
            return plan
    return heuristic_plan(question)
