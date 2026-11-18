"""
mcp_client.py：Smart_Chat 的 MCP 用戶端——用官方 MCP SDK（stdio）連到 MCP 伺服器，取得工具清單並呼叫工具。

主要功能：
  - McpToolbox：背景執行緒跑 asyncio，依設定啟動各個 MCP 伺服器（子行程）、initialize、list_tools，連線一直保持；
    之後同步呼叫 call(name, arguments) 取得工具結果文字；伺服器掛掉下一次呼叫會重新啟動
  - 設定：runtime/mcp_servers.json（格式同常見的 MCP 設定：{"mcpServers": {"名稱": {"command", "args", "env"}}}），
    沒有這個檔就用內建的天氣與搜尋伺服器（rag/mcp_servers，免費、免金鑰）；想接別的免費 MCP 伺服器就加在這個檔
  - 安全：設定檔只有後台管理者能改（不是前台使用者輸入）；子行程只拿到 MCP SDK 預設的少量環境變數（不含 API 金鑰），
    再加設定檔指定的；工具參數只收該工具宣告過的欄位、文字或數字、長度有上限；工具結果只當外部資料、長度截斷；
    全本地模式整組工具停用（available() 回 False）；單次呼叫與啟動都有逾時
  - describe() 把工具清單整理成給語言模型選工具用的文字；label_of() 給畫面「使用工具」晶片的名稱

串接模組：rag.talk.Talker（選工具、呼叫）；rag.talk_tools.ToolError；shared.network_policy（全本地模式）；shared.paths。
"""
import asyncio
import atexit
import json
import os
import sys
import threading
from contextlib import AsyncExitStack

from rag.talk_tools import ToolError
from shared import network_policy, paths

START_TIMEOUT = 40.0
CALL_TIMEOUT = 30.0
MAX_RESULT_CHARS = 1800
MAX_ARGUMENT_CHARS = 200
CONFIG_NAME = "mcp_servers.json"
LABELS = {"get_weather": "天氣查詢", "search_news": "新聞搜尋", "search_wikipedia": "維基百科"}


def default_config():
    """內建的兩個 MCP 伺服器（用目前這個 Python 啟動）。"""
    python = sys.executable
    return {"mcpServers": {
        "weather": {"command": python, "args": ["-m", "rag.mcp_servers.weather_server"]},
        "search": {"command": python, "args": ["-m", "rag.mcp_servers.search_server"]},
    }}


def load_config(path=None):
    """讀 runtime/mcp_servers.json；沒有檔、壞檔或格式不對就用內建設定。"""
    target = path or os.path.join(paths.RUNTIME_DIR, CONFIG_NAME)
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
        servers = data["mcpServers"]
        if isinstance(servers, dict) and servers and all(isinstance(v, dict) and v.get("command")
                                                         for v in servers.values()):
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return default_config()


class _Tool:
    def __init__(self, server, name, description, schema):
        self.server, self.name, self.description = server, name, " ".join(str(description or "").split())[:200]
        self.schema = schema if isinstance(schema, dict) else {}

    @property
    def properties(self):
        props = self.schema.get("properties")
        return props if isinstance(props, dict) else {}


class McpToolbox:
    def __init__(self, config=None, start_timeout=START_TIMEOUT, call_timeout=CALL_TIMEOUT):
        self._config = config
        self._start_timeout, self._call_timeout = start_timeout, call_timeout
        self._tools = {}                         # 工具名稱 → _Tool
        self._sessions = {}                      # 伺服器名稱 → ClientSession
        self._loop = None
        self._thread = None
        self._serve_future = None
        self._stop = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        atexit.register(self.close)

    # --- 啟動與關閉 ---

    def warm_up(self):
        """背景先把伺服器啟動起來（不等結果），第一題用到工具時就不必再等。"""
        threading.Thread(target=self._start_quietly, daemon=True, name="mcp-warmup").start()

    def _start_quietly(self):
        try:
            self._ensure_started()
        except Exception as e:  # noqa: BLE001
            print(f"[mcp] 背景啟動失敗：{type(e).__name__}")

    def _ensure_started(self):
        with self._lock:
            if self._ready.is_set() and self._loop is not None and self._loop.is_running():
                return True
            self._tools, self._sessions = {}, {}
            self._ready.clear()
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._thread = threading.Thread(target=self._run_loop, args=(loop,), daemon=True, name="mcp-loop")
            self._thread.start()
            started = threading.Event()
            self._serve_future = asyncio.run_coroutine_threadsafe(self._serve(started), loop)
            if not started.wait(self._start_timeout):
                print("[mcp] 啟動 MCP 伺服器逾時")
                self._shutdown_locked()
                return False
            self._ready.set()
            return bool(self._tools)

    @staticmethod
    def _run_loop(loop):
        asyncio.set_event_loop(loop)
        loop.run_forever()

    async def _serve(self, started):
        """同一個任務裡連線、等待、關閉（MCP SDK 的連線只能在進入它的那個任務裡結束）。"""
        self._stop = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                await self._connect_all(stack)
                started.set()
                await self._stop.wait()
        except Exception as e:  # noqa: BLE001
            print(f"[mcp] MCP 連線結束：{type(e).__name__}")
        finally:
            started.set()

    async def _connect_all(self, stack):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        config = (self._config or load_config()).get("mcpServers", {})
        for name, entry in config.items():
            try:
                params = StdioServerParameters(command=entry["command"], args=list(entry.get("args", [])),
                                               env=entry.get("env") or None, cwd=paths.PROJECT_ROOT)
                read, write = await stack.enter_async_context(stdio_client(params, errlog=self._devnull()))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
            except Exception as e:  # noqa: BLE001  某個伺服器啟動不了不影響其他的
                print(f"[mcp] 伺服器 {name} 連線失敗：{type(e).__name__}")
                continue
            self._sessions[name] = session
            for tool in listed.tools:
                key = tool.name if tool.name not in self._tools else f"{name}.{tool.name}"
                self._tools[key] = _Tool(name, tool.name, tool.description, tool.inputSchema)

    @staticmethod
    def _devnull():
        return open(os.devnull, "w")                  # noqa: SIM115  伺服器的記錄輸出不要吵（也避免寫到被接走的管線）

    def _shutdown_locked(self):
        loop, future = self._loop, getattr(self, "_serve_future", None)
        self._tools, self._sessions, self._loop, self._serve_future = {}, {}, None, None
        self._ready.clear()
        if loop is None:
            return
        if loop.is_running():
            loop.call_soon_threadsafe(self._stop.set)        # 讓 _serve 自己在同一個任務裡關掉所有連線
            if future is not None:
                try:
                    future.result(10)
                except Exception as e:  # noqa: BLE001
                    print(f"[mcp] 關閉伺服器時出錯（略過）：{type(e).__name__}")
            loop.call_soon_threadsafe(loop.stop)

    def close(self):
        with self._lock:
            self._shutdown_locked()

    # --- 給 Talker 用 ---

    def available(self):
        """工具現在能不能用：全本地模式不上網；伺服器要有至少一個工具。"""
        if network_policy.local_only():
            return False
        try:
            return self._ensure_started() and bool(self._tools)
        except Exception as e:  # noqa: BLE001
            print(f"[mcp] 檢查工具失敗：{type(e).__name__}")
            return False

    def tool_names(self):
        return list(self._tools)

    def describe(self):
        """給語言模型選工具用的文字：每個工具一行（名稱、參數、說明）。"""
        lines = []
        for name, tool in self._tools.items():
            args = "、".join(tool.properties) or "無參數"
            lines.append(f"- {name}（參數：{args}）：{tool.description}")
        return "\n".join(lines)

    def label_of(self, name):
        return LABELS.get(self._tools[name].name, name) if name in self._tools else name

    def clean_arguments(self, name, arguments):
        """工具參數驗證：工具要存在、只收它宣告過的欄位、值是文字或數字（文字截短）、必填欄位都要有；不合格回 None。"""
        tool = self._tools.get(name)
        if tool is None or not isinstance(arguments, dict):
            return None
        clean = {}
        for key, value in arguments.items():
            if key not in tool.properties or isinstance(value, bool) or not isinstance(value, (str, int, float)):
                continue
            clean[key] = " ".join(value.split())[:MAX_ARGUMENT_CHARS] if isinstance(value, str) else value
        required = tool.schema.get("required") or []
        if any(key not in clean or clean[key] == "" for key in required) or not clean:
            return None
        return clean

    def call(self, name, arguments):
        """呼叫工具；回傳結果文字（截斷）。失敗丟 ToolError（只帶一句說明）。"""
        if network_policy.local_only():
            raise ToolError("目前是全本地模式，不能上網")
        clean = self.clean_arguments(name, arguments)
        if clean is None:
            raise ToolError("工具參數不合格")
        if not self._ensure_started():
            raise ToolError("工具服務沒有啟動")
        tool, loop = self._tools[name], self._loop
        session = self._sessions.get(tool.server)
        if session is None or loop is None:
            raise ToolError("工具服務沒有啟動")
        future = asyncio.run_coroutine_threadsafe(session.call_tool(tool.name, clean), loop)
        try:
            result = future.result(self._call_timeout)
        except Exception as e:  # noqa: BLE001  逾時或伺服器掛了：丟掉這份連線，下一次重新啟動
            future.cancel()
            print(f"[mcp] 呼叫工具 {name} 失敗：{type(e).__name__}")
            with self._lock:
                self._shutdown_locked()
            raise ToolError("工具暫時沒有回應") from None
        text = "\n".join(getattr(part, "text", "") for part in (result.content or []) if getattr(part, "text", ""))
        if getattr(result, "isError", False):
            raise ToolError("工具回報錯誤")
        return text[:MAX_RESULT_CHARS]


_default = None


def default_toolbox():
    """全程式共用的一份（後台連線服務裡的 Smart_Chat 用）；第一次取得時就在背景暖機。"""
    global _default
    if _default is None:
        _default = McpToolbox()
        _default.warm_up()
    return _default
