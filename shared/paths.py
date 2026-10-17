"""
paths.py：專案路徑與執行期資料位置的統一定義。

主要功能：
  - PROJECT_ROOT：專案根目錄；MODELS_DIR：所有模型；DOCS_DIR：文件資料夾（助理讀取的法規、辦法、聲明書等輸入資料）
  - RUNTIME_DIR：執行期資料夾（資料庫、助理的向量庫、尾隨證據照片、LLM 設定），
    整個資料夾不進版控；可用環境變數 SMART_ACCESS_RUNTIME_DIR 指定其他位置
  - VENV_PYTHON：後台啟動子服務時使用的 Python 直譯器；可用環境變數 SMART_ACCESS_PYTHON 覆蓋
  - DOCS_DIR 可用環境變數 SMART_ACCESS_DOCS_DIR 覆蓋

串接模組：
  - 所有需要檔案路徑的模組（access、admin、rag、shared）都從這裡取得路徑，不各自推算
"""
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODELS_DIR = os.path.join(PROJECT_ROOT, "models")
DOCS_DIR = os.environ.get("SMART_ACCESS_DOCS_DIR") or os.path.join(PROJECT_ROOT, "data", "docs")

RUNTIME_DIR = os.environ.get("SMART_ACCESS_RUNTIME_DIR") or os.path.join(PROJECT_ROOT, "runtime")
DB_PATH = os.path.join(RUNTIME_DIR, "database.db")
ASSISTANT_DIR = os.path.join(RUNTIME_DIR, "assistant")
ANOMALY_DIR = os.path.join(RUNTIME_DIR, "anomaly_logs")
LLM_SETTINGS_PATH = os.path.join(RUNTIME_DIR, "llm_settings.json")

VENV_PYTHON = os.environ.get("SMART_ACCESS_PYTHON") or os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")

os.makedirs(RUNTIME_DIR, exist_ok=True)
