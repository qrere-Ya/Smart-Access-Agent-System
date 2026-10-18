"""
llm 單元測試：依設定選擇本機或雲端 API（雲端失效改本機、兩者都不能用給白話說明）。網路與模型都用假的。
"""
import pytest

from rag import llm
from rag import llm_settings as ls

SETTINGS = {"mode": "local", "api_base": "http://x", "api_key": "k", "api_model": "m",
            "local_model": "qwen2:7b", "local_host": ""}


@pytest.fixture
def fake_checks(monkeypatch):
    state = {"local": (True, None, [], "", 0.0), "api": (True, None, "OK", 0.0)}
    monkeypatch.setattr(ls, "check_local", lambda s, **k: state["local"])
    monkeypatch.setattr(ls, "check_api", lambda s, **k: state["api"])
    monkeypatch.setattr(ls, "local_host", lambda s: "http://127.0.0.1:11434")
    monkeypatch.setattr(llm, "HEALTH_CACHE_SECONDS", 0.0)
    return state


def test_local_mode_uses_the_local_model(fake_checks):
    model, note = llm.build_llm(SETTINGS)
    assert isinstance(model, ls.OllamaChatLLM) and "本機模型（qwen2:7b）" in note


def test_api_mode_uses_the_api_with_the_local_model_as_backup(fake_checks):
    model, note = llm.build_llm({**SETTINGS, "mode": "api"})
    assert isinstance(model, ls.FailoverLLM) and isinstance(model.fallback, ls.OllamaChatLLM)
    assert "失效時自動改用本機模型" in note


def test_api_mode_without_a_working_local_model_has_no_backup(fake_checks):
    fake_checks["local"] = (False, ls.LLMCallError("local_down", "local"), [], "", 0.0)
    model, note = llm.build_llm({**SETTINGS, "mode": "api"})
    assert isinstance(model, ls.FailoverLLM) and model.fallback is None and "自動改用" not in note


def test_broken_api_falls_back_to_local_and_says_why(fake_checks):
    fake_checks["api"] = (False, ls.LLMCallError("auth", "api", "401"), "", 0.0)
    model, note = llm.build_llm({**SETTINGS, "mode": "api"})
    assert isinstance(model, ls.OllamaChatLLM) and "雲端 API 目前無法使用" in note


def test_nothing_usable_returns_none_with_an_explanation(fake_checks):
    fake_checks["local"] = (False, ls.LLMCallError("local_down", "local"), [], "", 0.0)
    model, note = llm.build_llm(SETTINGS)
    assert model is None and "本機模型也無法使用" in note


def test_output_limit_is_passed_to_the_client(fake_checks):
    model, _ = llm.build_llm(SETTINGS, max_tokens=200)
    assert model.max_tokens == 200


def test_api_key_is_never_part_of_the_note(fake_checks):
    _, note = llm.build_llm({**SETTINGS, "mode": "api", "api_key": "nvapi-SECRET"})
    assert "nvapi-SECRET" not in note


def test_the_health_checks_run_once_for_the_two_llms_built_back_to_back(monkeypatch):
    calls = {"local": 0, "api": 0}

    def local(s, **k):
        calls["local"] += 1
        return (True, None, [], "", 0.0)

    def api(s, **k):
        calls["api"] += 1
        return (True, None, "OK", 0.0)

    monkeypatch.setattr(ls, "check_local", local)
    monkeypatch.setattr(ls, "check_api", api)
    monkeypatch.setattr(ls, "local_host", lambda s: "http://127.0.0.1:11434")
    monkeypatch.setattr(llm, "HEALTH_CACHE_SECONDS", 60.0)
    llm.clear_health_cache()
    settings = {**SETTINGS, "mode": "api"}
    first, _ = llm.build_llm(settings)
    second, _ = llm.build_llm(settings, max_tokens=200)
    assert calls == {"local": 1, "api": 1} and first is not second
    llm.build_llm({**settings, "local_model": "other"})        # 設定改了就要重新檢查
    assert calls == {"local": 2, "api": 2}
    llm.clear_health_cache()
