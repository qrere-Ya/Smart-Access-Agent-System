"""
Tk 事件繫結的安全檢查（讀原始碼，不開視窗）：tkinter 偶爾會以零個參數呼叫繫結函式，
所以所有 bind／bind_all／tag_bind 的處理函式要嘛用 ui_theme.event_only 包起來，要嘛事件參數有預設值。
新增繫結卻忘了處理時，這個測試會指出是哪個檔案、哪一行。
"""
import ast
import pathlib

from shared import paths

ROOT = pathlib.Path(paths.PROJECT_ROOT)
FOLDERS = ("admin", "rag", "access", "shared")
BIND_METHODS = {"bind", "bind_all", "tag_bind", "bind_class"}


def _event_param_is_optional(func):
    args = func.args
    positional = args.posonlyargs + args.args
    if positional and positional[0].arg == "self":
        positional = positional[1:]
    required = len(positional) - len(args.defaults)
    return required <= 0 or args.vararg is not None


def _definitions(tree):
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
    return found


def _handler_of(call):
    # bind(sequence, func, add)；tag_bind(tag, sequence, func)
    index = 2 if call.func.attr in ("tag_bind", "bind_class") else 1
    return call.args[index] if len(call.args) > index else None


def _is_wrapped(handler):
    if not isinstance(handler, ast.Call):
        return False
    name = handler.func.attr if isinstance(handler.func, ast.Attribute) else getattr(handler.func, "id", None)
    return name == "event_only"


def _unprotected_binds():
    problems = []
    for folder in FOLDERS:
        for path in sorted((ROOT / folder).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            defs = _definitions(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in BIND_METHODS):
                    continue
                handler = _handler_of(node)
                if handler is None or _is_wrapped(handler):
                    continue
                name = handler.attr if isinstance(handler, ast.Attribute) else getattr(handler, "id", None)
                func = defs.get(name) if name else (handler if isinstance(handler, ast.Lambda) else None)
                if func is not None and _event_param_is_optional(func):
                    continue
                problems.append(f"{folder}/{path.name}:{node.lineno}")
    return problems


def test_every_tk_bind_handler_ignores_a_call_without_an_event():
    assert _unprotected_binds() == []
