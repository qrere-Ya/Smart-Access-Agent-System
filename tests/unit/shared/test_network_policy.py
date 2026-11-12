"""全本地模式：開關讀寫（環境變數優先、壞檔當關閉）、語言模型出口一律本機且直接呼叫雲端會被拒絕、晨報不連網也不重試。"""
import json

import pytest

from admin import morning_brief as mb
from admin import news_feed as nf
from rag import llm_settings
from shared import network_policy as np_, paths


@pytest.fixture
def policy(tmp_path, monkeypatch):
    path = str(tmp_path / "network_policy.json")
    monkeypatch.setattr(paths, "NETWORK_POLICY_PATH", path)
    monkeypatch.delenv(np_.ENV_NAME, raising=False)
    return path


# --- 開關 ---

def test_default_is_off(policy):
    assert np_.local_only() is False


def test_the_file_switch_round_trips(policy):
    np_.set_local_only(True)
    assert np_.local_only() is True
    np_.set_local_only(False)
    assert np_.local_only() is False


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"local_only": "yes"}', '{"local_only": 1}'])
def test_a_broken_file_means_off(policy, content):
    with open(policy, "w", encoding="utf-8") as f:
        f.write(content)
    assert np_.local_only() is False


@pytest.mark.parametrize("value,expected", [("1", True), ("TRUE", True), ("on", True), ("0", False), ("off", False)])
def test_the_environment_wins_over_the_file(policy, monkeypatch, value, expected):
    np_.set_local_only(not expected)
    monkeypatch.setenv(np_.ENV_NAME, value)
    assert np_.local_only() is expected


def test_an_unreadable_environment_value_is_ignored(policy, monkeypatch):
    np_.set_local_only(True)
    monkeypatch.setenv(np_.ENV_NAME, "maybe")
    assert np_.local_only() is True


def test_require_online_refuses_only_in_local_mode(policy):
    np_.require_online("新聞")
    np_.set_local_only(True)
    with pytest.raises(np_.OfflineError):
        np_.require_online("新聞")


# --- 語言模型 ---

def test_settings_are_forced_to_local_even_when_api_is_asked_for(policy, tmp_path, monkeypatch):
    monkeypatch.setattr(llm_settings, "SETTINGS_PATH", str(tmp_path / "llm.json"))
    with open(llm_settings.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump({"mode": "api"}, f)
    assert llm_settings.load_settings()["mode"] == "api"
    np_.set_local_only(True)
    assert llm_settings.load_settings()["mode"] == "local"
    assert llm_settings.settings_for("api")["mode"] == "local"


def test_calling_the_cloud_directly_is_refused_before_any_connection(policy, monkeypatch):
    np_.set_local_only(True)

    def boom(*a, **k):
        raise AssertionError("不該連線")

    monkeypatch.setattr(llm_settings.urllib.request, "urlopen", boom)
    llm = llm_settings.OpenAICompatLLM("https://example.com/v1", "k", "m")
    with pytest.raises(llm_settings.LLMCallError) as err:
        list(llm.stream_complete("hi"))
    assert err.value.kind == "local_only"
    assert "全本地" in llm_settings.explain(err.value)[0]


# --- 晨報 ---

def test_the_morning_news_is_not_fetched_in_local_mode(policy):
    np_.set_local_only(True)

    def boom(*a, **k):
        raise AssertionError("不該連線")

    cards, problem = nf.fetch_ai_news(get=boom)
    assert cards == [] and problem == nf.OFFLINE_NOTE


def test_a_local_mode_brief_is_not_retried_and_says_why(policy):
    brief = {"date": "2026-11-09", "generated_at": "2026-11-09 08:30", "news": [], "news_problem": nf.OFFLINE_NOTE,
             "news_tries": 1}
    assert mb.wants_news_retry(brief) is True       # 關閉時：連不上就重試
    np_.set_local_only(True)
    assert mb.wants_news_retry(brief) is False and mb.retry_wait(brief) is None
