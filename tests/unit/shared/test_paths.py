"""
paths 單元測試：路徑之間的關係、可用環境變數覆蓋的項目（在獨立的子行程中驗證，不會在專案資料夾建立 runtime/）。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from shared import paths

ROOT = Path(__file__).resolve().parents[3]
KEYS = ("PROJECT_ROOT", "MODELS_DIR", "LAWS_DIR", "RUNTIME_DIR", "DB_PATH", "FAISS_DIR",
        "ANOMALY_DIR", "LLM_SETTINGS_PATH", "EVAL_RESULTS_DIR", "VENV_PYTHON")


def load_paths(**env):
    code = ("import json; from shared import paths; "
            f"print(json.dumps({{k: getattr(paths, k) for k in {KEYS!r}}}))")
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("SMART_ACCESS_")}
    full_env.update(env)
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=full_env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_project_root_is_the_repository_root():
    assert Path(paths.PROJECT_ROOT) == ROOT
    assert (ROOT / "shared" / "paths.py").is_file()


def test_runtime_files_live_inside_the_runtime_folder():
    runtime = Path(paths.RUNTIME_DIR)
    assert runtime.is_dir()
    for value in (paths.DB_PATH, paths.FAISS_DIR, paths.ANOMALY_DIR, paths.LLM_SETTINGS_PATH, paths.EVAL_RESULTS_DIR):
        assert Path(value).parent == runtime
    assert Path(paths.DB_PATH).name == "database.db"
    assert Path(paths.LLM_SETTINGS_PATH).name == "llm_settings.json"


def test_models_folder_is_at_the_project_root():
    assert Path(paths.MODELS_DIR) == ROOT / "models"


def test_runtime_folder_can_be_moved_with_an_environment_variable(tmp_path):
    target = tmp_path / "elsewhere"
    result = load_paths(SMART_ACCESS_RUNTIME_DIR=str(target))
    assert result["RUNTIME_DIR"] == str(target) and target.is_dir()
    assert Path(result["DB_PATH"]) == target / "database.db"
    assert Path(result["FAISS_DIR"]) == target / "faiss_storage"
    assert Path(result["ANOMALY_DIR"]) == target / "anomaly_logs"


def test_laws_folder_and_interpreter_can_be_overridden(tmp_path):
    result = load_paths(SMART_ACCESS_RUNTIME_DIR=str(tmp_path / "rt"), SMART_ACCESS_LAWS_DIR=str(tmp_path / "laws"),
                        SMART_ACCESS_PYTHON="C:/custom/python.exe")
    assert result["LAWS_DIR"] == str(tmp_path / "laws") and result["VENV_PYTHON"] == "C:/custom/python.exe"


def test_defaults_without_overrides(tmp_path):
    result = load_paths(SMART_ACCESS_RUNTIME_DIR=str(tmp_path / "rt"))
    assert Path(result["LAWS_DIR"]) == ROOT / "data" / "laws"
    assert Path(result["VENV_PYTHON"]) == ROOT / ".venv" / "Scripts" / "python.exe"
