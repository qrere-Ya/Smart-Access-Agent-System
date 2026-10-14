"""
rag_resources 單元測試：Ollama 模型資源在釋放時會要求 Ollama 卸載模型（用本機假伺服器驗證請求內容）、
位址正規化、embedding 代理不持有模型。
"""
import socket

from rag import rag_resources as rr


def free_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def test_releasing_the_ollama_resource_asks_ollama_to_unload_the_model(fake_llm_server):
    base, requests = fake_llm_server
    resource = rr.OllamaModelResource("qwen2:7b", host=base)
    with resource.use() as model_name:
        assert model_name == "qwen2:7b"
    resource.release("test")
    path, body = requests[-1]
    assert path == "/api/generate" and body == {"model": "qwen2:7b", "keep_alive": 0}


def test_unload_failure_is_reported_but_never_raised(capsys):
    resource = rr.OllamaModelResource("qwen2:7b", host=free_url())
    with resource.use():
        pass
    resource.release("test")
    assert "卸載" in capsys.readouterr().out


def test_ollama_resource_can_always_be_evicted_and_needs_no_memory():
    resource = rr.OllamaModelResource("qwen2:7b")
    assert resource.self_evictable is True
    assert resource.est_ram_mb == 0 and resource.est_vram_mb == 0


def test_host_normalization(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert rr.OllamaModelResource("m")._host == "http://127.0.0.1:11434"
    assert rr.OllamaModelResource("m", host="myhost:9/")._host == "http://myhost:9"
    monkeypatch.setenv("OLLAMA_HOST", "10.0.0.2:11434")
    assert rr.OllamaModelResource("m")._host == "http://10.0.0.2:11434"


def test_embedding_proxy_does_not_load_the_model():
    proxy = rr.ManagedHFEmbedding("some/model", device="cpu")
    assert proxy.class_name() == "ManagedHFEmbedding"
    assert proxy.model_name == "some/model"
    assert not proxy._res.is_loaded
    proxy.release()
    assert proxy.evict() is True
