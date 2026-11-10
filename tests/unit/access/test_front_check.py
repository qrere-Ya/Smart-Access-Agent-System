"""前台自我檢查：角色與通行碼、模型檔、後台連線（連不上只警告）。"""
import os

from access import front_check as fc
from shared import role_config as rc

TOKEN = "oGo0iq6_xu9Jp7cwTM0ttP-BTbTQ9Zefin8GaJMQpyc"


def levels(results):
    return [level for level, _ in results]


def test_role_check_fails_on_standalone_and_needs_the_token(monkeypatch, tmp_path):
    from shared import paths
    monkeypatch.setattr(paths, "ROLE_PATH", str(tmp_path / "role.json"))
    monkeypatch.delenv(rc.TOKEN_ENV, raising=False)
    monkeypatch.delenv(rc.ROLE_ENV, raising=False)
    assert levels(fc.check_role()) == ["FAIL"]
    rc.save(rc.LinkSettings(rc.FRONT, back_host="192.168.50.1", bind_host="192.168.50.2",
                             back_fingerprint="ab" * 32))
    assert levels(fc.check_role()) == ["OK", "FAIL"]                      # 角色對了但還沒設通行碼
    monkeypatch.setenv(rc.TOKEN_ENV, TOKEN)
    assert levels(fc.check_role()) == ["OK", "OK"]


def test_file_check_names_every_missing_model(tmp_path):
    results = fc.check_files(models_dir=str(tmp_path))
    missing = [text for level, text in results if level == "FAIL"]
    assert len(missing) == len(fc.REQUIRED_MODELS)
    for name in fc.REQUIRED_MODELS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    assert "FAIL" not in levels(fc.check_files(models_dir=str(tmp_path)))


def test_back_check_only_warns_when_the_back_is_unreachable(monkeypatch, tmp_path):
    from shared import paths
    monkeypatch.setattr(paths, "ROLE_PATH", str(tmp_path / "role.json"))
    monkeypatch.setenv(rc.TOKEN_ENV, TOKEN)
    monkeypatch.delenv(rc.ROLE_ENV, raising=False)
    rc.save(rc.LinkSettings(rc.FRONT, back_host="127.0.0.1", back_port=59999, bind_host="127.0.0.1",
                             back_fingerprint="ab" * 32))
    assert levels(fc.check_back()) == ["WARN"]
    assert os.environ[rc.TOKEN_ENV] == TOKEN
