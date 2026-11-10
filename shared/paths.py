"""
paths.py：專案路徑與執行期資料位置的統一定義。

主要功能：
  - PROJECT_ROOT：專案根目錄；MODELS_DIR：所有模型；DOCS_DIR：文件資料夾（助理讀取的法規、辦法、聲明書等輸入資料）
  - RUNTIME_DIR：執行期資料夾（資料庫、助理的向量庫、尾隨證據照片、LLM 設定、後台登入的工作階段檔、
    遠端測試入口的金鑰雜湊與提問紀錄），整個資料夾不進版控；可用環境變數 SMART_ACCESS_RUNTIME_DIR 指定其他位置
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
RAG_DIR = os.path.join(RUNTIME_DIR, "rag")
ANOMALY_DIR = os.path.join(RUNTIME_DIR, "anomaly_logs")
LLM_SETTINGS_PATH = os.path.join(RUNTIME_DIR, "llm_settings.json")
SESSION_PATH = os.path.join(RUNTIME_DIR, "session.json")
LOGIN_LOCK_PATH = os.path.join(RUNTIME_DIR, "login_lock.json")
ANOMALY_SEEN_PATH = os.path.join(RUNTIME_DIR, "anomaly_seen.json")
DIGEST_PATH = os.path.join(RUNTIME_DIR, "daily_digest.json")
BRIEF_PATH = os.path.join(RUNTIME_DIR, "morning_brief.json")
DIGEST_SETTINGS_PATH = os.path.join(RUNTIME_DIR, "digest_settings.json")
ROLE_PATH = os.path.join(RUNTIME_DIR, "role.json")
NETWORK_POLICY_PATH =os.path.join(RUNTIME_DIR, "network_policy.json")
LINK_TLS_DIR = os.path.join(RUNTIME_DIR, "link_tls")          # 後台連線服務的憑證與私鑰（只在後台，不進版控）
REMOTE_KEYS_PATH =os.path.join(RUNTIME_DIR, "remote_keys.json")
REMOTE_LOG_PATH = os.path.join(RUNTIME_DIR, "remote_questions.jsonl")
REMOTE_EVENTS_PATH = os.path.join(RUNTIME_DIR, "remote_events.jsonl")

VENV_PYTHON = os.environ.get("SMART_ACCESS_PYTHON") or os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")

os.makedirs(RUNTIME_DIR, exist_ok=True)
