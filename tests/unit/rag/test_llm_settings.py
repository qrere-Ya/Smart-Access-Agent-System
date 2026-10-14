"""
llm_settings 單元測試：設定讀寫、金鑰來源、錯誤分類與白話說明、雲端 API 與 Ollama 的串流呼叫、
自動改用本機模型、出題官／裁判官的組裝。雲端與 Ollama 都用本機的假伺服器模擬，不需要網路與金鑰。
"""
import io
import json
import socket
import ssl
import urllib.error

import pytest

from rag import llm_settings as ls


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "llm_settings.json"
    monkeypatch.setattr(ls, "SETTINGS_PATH", str(path))
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    return path


def closed_port_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def make_settings(base, **overrides):
    s = dict(ls.DEFAULTS, api_base=base, api_key="good-key", api_model="m", local_host=base, mode="api")
    s.update(overrides)
    return s


# ---------- 設定讀寫 ----------

def test_defaults_when_nothing_is_saved(settings_file):
    assert ls.load_settings() == ls.DEFAULTS


def test_environment_key_switches_the_default_mode_to_api(settings_file, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    assert ls.load_settings()["mode"] == "api"


def test_saved_values_override_defaults_and_non_strings_are_ignored(settings_file):
    settings_file.write_text(json.dumps({"mode": "api", "local_model": "llama3", "api_model": 5, "junk": "x"}),
                             encoding="utf-8")
    s = ls.load_settings()
    assert s["mode"] == "api" and s["local_model"] == "llama3"
    assert s["api_model"] == ls.DEFAULTS["api_model"] and "junk" not in s


def test_corrupt_settings_file_falls_back_to_defaults(settings_file):
    settings_file.write_text("{不是 json", encoding="utf-8")
    assert ls.load_settings() == ls.DEFAULTS


def test_save_merges_strips_and_normalizes(settings_file):
    saved = ls.save_settings({"mode": "api", "local_model": "  llama3  ", "api_key": " k1 "})
    assert saved["local_model"] == "llama3" and saved["api_key"] == "k1" and saved["mode"] == "api"
    assert json.loads(settings_file.read_text(encoding="utf-8")) == saved
    assert ls.save_settings({"mode": "壞值"})["mode"] == "local"


def test_blank_api_key_keeps_the_stored_one_unless_cleared(settings_file):
    ls.save_settings({"api_key": "secret"})
    assert ls.save_settings({"api_key": "  ", "local_model": "x"})["api_key"] == "secret"
    assert ls.save_settings({}, clear_key=True)["api_key"] == ""


def test_effective_api_key_sources(settings_file, monkeypatch):
    assert ls.effective_api_key({"api_key": "  abc  ", "api_base": ls.NVIDIA_BASE}) == "abc"
    monkeypatch.setenv("NVIDIA_API_KEY", "from-env")
    assert ls.effective_api_key({"api_key": "", "api_base": ls.NVIDIA_BASE}) == "from-env"
    assert ls.effective_api_key({"api_key": "", "api_base": "https://example.com/v1"}) == ""


def test_key_status_text_never_shows_the_key(settings_file, monkeypatch):
    assert "尚未設定" in ls.key_status_text({"api_key": "", "api_base": "https://example.com"})
    saved = ls.key_status_text({"api_key": "topsecret", "api_base": ls.NVIDIA_BASE})
    assert "已儲存" in saved and "topsecret" not in saved
    monkeypatch.setenv("NVIDIA_API_KEY", "envkey")
    assert "環境變數" in ls.key_status_text({"api_key": "", "api_base": ls.NVIDIA_BASE})


@pytest.mark.parametrize("configured, env, expected", [
    ("", None, "http://127.0.0.1:11434"),
    ("", "192.168.1.5:11434", "http://192.168.1.5:11434"),
    ("http://host:1234/", None, "http://host:1234"),
    ("myhost:9", None, "http://myhost:9"),
])
def test_local_host_resolution(settings_file, monkeypatch, configured, env, expected):
    if env:
        monkeypatch.setenv("OLLAMA_HOST", env)
    assert ls.local_host({"local_host": configured}) == expected


# ---------- 錯誤分類與白話說明 ----------

@pytest.mark.parametrize("status, source, expected", [
    (401, "api", "auth"), (403, "api", "auth"), (404, "api", "model_or_url"), (429, "api", "rate"),
    (400, "api", "bad_request"), (422, "api", "bad_request"), (500, "api", "server"), (503, "api", "server"),
    (418, "api", "api_other"), (404, "local", "local_model_missing"), (500, "local", "local_other"),
])
def test_http_status_classification(status, source, expected):
    assert ls._classify_http(status, "", source) == expected


def test_local_not_found_text_in_body_means_missing_model():
    assert ls._classify_http(500, "model 'x' NOT FOUND", "local") == "local_model_missing"


@pytest.mark.parametrize("exc, source, expected", [
    (socket.timeout("t"), "api", "timeout"),
    (urllib.error.URLError(socket.timeout("t")), "api", "timeout"),
    (ssl.SSLError("bad cert"), "api", "ssl"),
    (urllib.error.URLError(socket.gaierror("dns")), "api", "dns"),
    (urllib.error.URLError(socket.gaierror("dns")), "local", "ollama_down"),
    (urllib.error.URLError(ConnectionRefusedError()), "api", "network"),
    (urllib.error.URLError(ConnectionRefusedError()), "local", "ollama_down"),
    (ValueError("其他"), "api", "api_other"),
    (ValueError("其他"), "local", "local_other"),
])
def test_exception_classification(exc, source, expected):
    assert ls._classify_exc(exc, source).kind == expected


def test_http_error_keeps_status_and_body():
    exc = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, io.BytesIO(b"invalid key"))
    err = ls._classify_exc(exc, "api")
    assert (err.kind, err.status) == ("auth", 401) and "invalid key" in err.detail


def test_an_existing_call_error_is_returned_as_is():
    err = ls.LLMCallError("rate", "api")
    assert ls._classify_exc(err, "api") is err


@pytest.mark.parametrize("kind", [
    "empty_key", "auth", "model_or_url", "rate", "bad_request", "server", "timeout", "dns", "network",
    "ssl", "ollama_down", "local_model_missing", "local_other", "api_other", "something_new",
])
def test_every_error_kind_has_a_plain_explanation(settings_file, kind):
    what, todo = ls.explain(ls.LLMCallError(kind, "api", "detail"), ls.DEFAULTS)
    assert what and todo


def test_error_formatting_and_exception_description(settings_file):
    err = ls.LLMCallError("auth", "api")
    assert "**" in ls.format_error(err, ls.DEFAULTS) and "**" not in ls.format_error(err, ls.DEFAULTS, markdown=False)
    assert "金鑰" in ls.describe_exception(urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b"")))


# ---------- 雲端 API 與 Ollama 的呼叫 ----------

def test_cloud_api_streams_pieces_in_order(fake_llm_server):
    base, requests = fake_llm_server
    llm = ls.OpenAICompatLLM(base, "good-key", "some-model", max_tokens=8)
    assert [d.delta for d in llm.stream_complete("嗨")] == ["你", "好", "嗎"]
    assert llm.complete("嗨") == "你好嗎"
    path, body = requests[-1]
    assert path.endswith("/chat/completions") and body["model"] == "some-model" and body["max_tokens"] == 8
    assert body["messages"] == [{"role": "user", "content": "嗨"}] and body["stream"] is True


def test_cloud_api_rejects_a_bad_key(fake_llm_server):
    base, _ = fake_llm_server
    with pytest.raises(ls.LLMCallError) as info:
        list(ls.OpenAICompatLLM(base, "bad-key", "m").stream_complete("嗨"))
    assert info.value.kind == "auth" and info.value.status == 401


def test_ollama_streams_and_sends_the_shared_context_size(fake_llm_server):
    base, requests = fake_llm_server
    llm = ls.OllamaChatLLM(base, "qwen2:7b", max_tokens=8)
    assert llm.complete("嗨") == "OK!"
    path, body = requests[-1]
    assert path == "/api/chat" and body["model"] == "qwen2:7b"
    assert body["options"]["num_ctx"] == ls.LOCAL_NUM_CTX and body["options"]["num_predict"] == 8


def test_ollama_missing_model_and_unreachable_server(fake_llm_server):
    base, _ = fake_llm_server
    with pytest.raises(ls.LLMCallError) as info:
        ls.OllamaChatLLM(base, "missing").complete("嗨")
    assert info.value.kind == "local_model_missing"
    with pytest.raises(ls.LLMCallError) as info:
        ls.OllamaChatLLM(closed_port_url(), "qwen2:7b").complete("嗨")
    assert info.value.kind == "ollama_down"


def test_listing_local_models(fake_llm_server):
    base, _ = fake_llm_server
    ok, names = ls.list_local_models({"local_host": base})
    assert ok and names == ["qwen2:7b", "llama3:latest"]
    ok, err = ls.list_local_models({"local_host": closed_port_url()})
    assert not ok and err.kind == "ollama_down"


@pytest.mark.parametrize("requested, installed", [
    ("qwen2:7b", True), ("llama3", True), ("llama3:latest", True), ("qwen2", True),
    ("qwen2:14b", False), ("mistral", False),
])
def test_model_installed_matching(requested, installed):
    assert ls._model_installed(requested, ["qwen2:7b", "llama3:latest"]) is installed


def test_check_local_and_check_api(fake_llm_server, settings_file):
    base, _ = fake_llm_server
    ok, err, names, text, _ = ls.check_local(make_settings(base), actually_generate=True)
    assert ok and err is None and text == "OK!"
    ok, err, _, _, _ = ls.check_local(make_settings(base, local_model="nope"))
    assert not ok and err.kind == "local_model_missing"
    ok, err, text, _ = ls.check_api(make_settings(base))
    assert ok and text == "你好嗎"
    ok, err, _, _ = ls.check_api(make_settings(base, api_key=""))
    assert not ok and err.kind == "empty_key"


def test_test_buttons_report_in_plain_language(fake_llm_server, settings_file):
    base, _ = fake_llm_server
    ok, msg = ls.test_api(make_settings(base))
    assert ok and "成功" in msg
    ok, msg = ls.test_api(make_settings(base, api_key="bad-key"))
    assert not ok and "失敗" in msg
    ok, msg = ls.test_local(make_settings(base))
    assert ok and "成功" in msg
    ok, msg = ls.test_local(make_settings(base, local_model="nope"))
    assert not ok and "qwen2:7b" in msg


# ---------- 自動改用本機模型 ----------

class _Failing:
    label = lambda self: "壞掉的雲端"  # noqa: E731

    def stream_complete(self, prompt):
        raise ls.LLMCallError("auth", "api", "金鑰失效")
        yield  # 讓這個函式成為產生器


class _Working:
    def label(self):
        return "本機備援"

    def stream_complete(self, prompt):
        yield ls.Delta("備援的答案")


def test_failover_switches_permanently_and_leaves_a_notice():
    llm = ls.FailoverLLM(_Failing(), _Working())
    assert llm.complete("嗨") == "備援的答案"
    assert llm.switched and llm.label() == "本機備援"
    notices = llm.pop_notices()
    assert len(notices) == 1 and "自動改用" in notices[0]
    assert llm.pop_notices() == []
    assert llm.complete("再問") == "備援的答案" and llm.pop_notices() == []


def test_failover_without_a_fallback_propagates_the_error():
    with pytest.raises(ls.LLMCallError):
        ls.FailoverLLM(_Failing(), None).complete("嗨")


def test_failover_does_not_switch_while_the_primary_works():
    primary = _Working()
    llm = ls.FailoverLLM(primary, _Failing())
    assert llm.complete("嗨") == "備援的答案" and not llm.switched and llm.label() == "本機備援"


def test_judge_uses_the_cloud_api_with_local_fallback(fake_llm_server, settings_file):
    base, _ = fake_llm_server
    llm, note = ls.build_judge_llm(make_settings(base))
    assert isinstance(llm, ls.FailoverLLM) and "雲端 API" in note


def test_judge_falls_back_to_local_when_the_cloud_key_is_bad(fake_llm_server, settings_file):
    base, _ = fake_llm_server
    llm, note = ls.build_judge_llm(make_settings(base, api_key="bad-key"))
    assert isinstance(llm, ls.OllamaChatLLM) and "已自動改用" in note


def test_judge_in_local_mode(fake_llm_server, settings_file):
    base, _ = fake_llm_server
    llm, note = ls.build_judge_llm(make_settings(base, mode="local"))
    assert isinstance(llm, ls.OllamaChatLLM) and "只用本機模型" in note


def test_judge_is_unavailable_when_nothing_works(settings_file):
    llm, note = ls.build_judge_llm(make_settings(closed_port_url(), mode="local"))
    assert llm is None and "評估無法進行" in note
