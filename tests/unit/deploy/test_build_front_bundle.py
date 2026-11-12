"""前台安裝包的打包：用 AST 追出前台用到的模組、只放白名單內的東西、MANIFEST 與檔案一致、不帶資料與後台程式。"""
import hashlib
import json
import os

import pytest

from deploy import build_front_bundle as bb
from shared import paths


def test_front_entry_closure_on_the_real_project_covers_the_front_and_nothing_of_the_back():
    mods = bb.project_modules(root=paths.PROJECT_ROOT)
    for needed in ("access/terminal_app.py", "access/terminal_backend.py", "access/front_store.py",
                   "access/front_agent.py", "access/front_check.py", "shared/role_config.py",
                   "access/front_dashboard.py",
                   "access/front_view.py", "shared/ui_theme.py",
                   "shared/link_protocol.py", "shared/database_mgr.py", "access/__init__.py", "shared/__init__.py"):
        assert needed in mods
    assert not [m for m in mods if m.startswith(("admin/", "rag/"))]
    assert "access/web_server.py" not in mods


def _fake_project(tmp_path):
    root = tmp_path / "proj"
    for rel, text in {
        "access/__init__.py": "", "shared/__init__.py": "",
        "access/terminal_app.py": "from access import terminal_backend\nimport os\n",
        "access/terminal_backend.py": "def f():\n    from shared import helper\n",
        "shared/helper.py": "import json\n",
        "shared/unused.py": "x = 1\n",
        "admin/secret.py": "x = 2\n",
        "access/front_agent.py": "", "access/front_check.py": "", "shared/role_config.py": "",
        "requirements/front.txt": "numpy\n", "requirements/lock.txt": "numpy==1\n",
        "models/m.onnx": "m", "models/voice/v.onnx": "v", "models/other.bin": "no",
        "deploy/front_template/安裝前台.bat": "echo hi\n", "deploy/front_template/install_front.ps1": "Write-Host 'x'",
        "runtime/people.db": "PRIVATE", "data/docs/a.txt": "PRIVATE",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(bb, "MODEL_FILES", ("m.onnx",))
    monkeypatch.setattr(bb, "MODEL_DIRS", ("voice",))
    return _fake_project(tmp_path), tmp_path / "out"


def test_closure_follows_lazy_imports_and_skips_unused_and_back_modules(fake):
    root, _ = fake
    mods = bb.project_modules(root=str(root))
    assert "shared/helper.py" in mods                       # 函式裡的延後 import 也要追到
    assert "shared/unused.py" not in mods and "admin/secret.py" not in mods


def test_bundle_contains_only_the_whitelist_and_never_private_data(fake):
    root, out = fake
    bundle, archive, count = bb.build(str(out), make_zip=True, root=str(root))
    files = {os.path.relpath(os.path.join(h, n), bundle).replace(os.sep, "/")
             for h, _d, names in os.walk(bundle) for n in names}
    assert "models/m.onnx" in files and "models/voice/v.onnx" in files and "models/other.bin" not in files
    assert not [f for f in files if f.startswith(("runtime/", "data/", "admin/"))]
    assert {"MANIFEST.json", "讀我.txt", "安裝前台.bat", "install_front.ps1", "requirements/lock.txt"} <= files
    assert os.path.isfile(archive) and count == len(files) - 1       # MANIFEST 不含自己


def test_manifest_matches_every_file_and_detects_tampering(fake):
    root, out = fake
    bundle, _archive, _count = bb.build(str(out), make_zip=False, root=str(root))
    manifest = json.load(open(os.path.join(bundle, "MANIFEST.json"), encoding="utf-8"))["files"]
    for rel, digest in manifest.items():
        with open(os.path.join(bundle, *rel.split("/")), "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == digest
    assert "MANIFEST.json" not in manifest


def test_scripts_are_written_for_windows(fake):
    root, out = fake
    bundle, _a, _c = bb.build(str(out), make_zip=False, root=str(root))
    bat = open(os.path.join(bundle, "安裝前台.bat"), "rb").read()
    assert b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b"")
    ps1 = open(os.path.join(bundle, "install_front.ps1"), "rb").read()
    assert ps1.startswith(b"\xef\xbb\xbf")                           # PowerShell 5.1 要 BOM


def test_a_missing_model_stops_the_build_instead_of_shipping_a_broken_bundle(fake):
    root, out = fake
    os.remove(root / "models" / "m.onnx")
    with pytest.raises(FileNotFoundError):
        bb.build(str(out), make_zip=False, root=str(root))


def test_requirement_files_carry_a_bom_so_pip_does_not_read_them_as_gbk(fake):
    root, out = fake
    bundle, _a, _c = bb.build(str(out), make_zip=False, root=str(root))
    for name in ("front.txt", "lock.txt"):
        assert open(os.path.join(bundle, "requirements", name), "rb").read().startswith(b"\xef\xbb\xbf")


LF, CRLF = bytes([10]), bytes([13, 10])


def test_batch_files_run_from_the_repo_use_crlf_so_cmd_does_not_die_on_them():
    """deploy/ 底下直接使用的 .bat（front_template 的在打包時才轉 CRLF）。"""
    folder = os.path.join(paths.PROJECT_ROOT, "deploy")
    for name in os.listdir(folder):
        if name.endswith(".bat"):
            data = open(os.path.join(folder, name), "rb").read()
            assert data.count(LF) == data.count(CRLF), name


def test_the_real_usage_guide_is_in_the_template_and_describes_only_what_exists():
    path = os.path.join(paths.PROJECT_ROOT, "deploy", "front_template", "前台使用說明.txt")
    text = open(path, encoding="utf-8").read()
    for needle in ("啟動前台門禁", "▶ 啟動人臉辨識", "F11", "第二個螢幕", "主螢幕", "停止前台辨識",
                   "runtime" + chr(92) + "logs", "前台檢查.bat", "設定前台網路.bat", "SMART_ACCESS_ALERT_SOUND",
                   "紅色橫幅"):
        assert needle in text, needle
    assert "維護選單" not in text                                        # 沒有維護選單，不能寫成有


def test_usage_guide_is_bundled_with_crlf_and_a_bom_so_notepad_reads_the_chinese(fake):
    root, out = fake
    guide = root / "deploy" / "front_template" / "前台使用說明.txt"
    guide.write_bytes("第一行".encode("utf-8") + LF + "第二行".encode("utf-8") + LF)
    bundle, _a, _c = bb.build(str(out), make_zip=False, root=str(root))
    data = open(os.path.join(bundle, "前台使用說明.txt"), "rb").read()
    bom = bytes([0xEF, 0xBB, 0xBF])
    assert data.startswith(bom) and data.count(bom) == 1
    assert CRLF in data and LF not in data.replace(CRLF, b"")
    manifest = json.load(open(os.path.join(bundle, "MANIFEST.json"), encoding="utf-8"))["files"]
    assert "前台使用說明.txt" in manifest


def test_text_files_inside_model_folders_are_copied_byte_for_byte(fake):
    """回歸：語音模型的 tokens.txt 被轉成 CRLF 加 BOM，sherpa-onnx 讀不了，辨識程式一啟動就死。"""
    root, out = fake
    tokens = b"_ 0" + LF + b"^ 1" + LF
    (root / "models" / "voice" / "tokens.txt").write_bytes(tokens)
    bundle, _a, _c = bb.build(str(out), make_zip=False, root=str(root))
    copied = open(os.path.join(bundle, "models", "voice", "tokens.txt"), "rb").read()
    assert copied == tokens
    assert bytes([0xEF, 0xBB, 0xBF]) not in copied and CRLF not in copied
