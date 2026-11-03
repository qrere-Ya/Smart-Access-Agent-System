"""
bge-m3 載入來源的選擇與轉檔：旁邊有轉好的 fp16 safetensors 就優先用，否則用原本的資料夾；
轉檔用假模型，不需要真的模型與 GPU。
"""
import sys
import types

import pytest

from rag import convert_embedding, embedding


@pytest.fixture
def model_dir(tmp_path):
    path = tmp_path / "bge-m3"
    path.mkdir()
    return path


def test_without_a_converted_folder_the_original_is_used(model_dir):
    assert embedding.fp16_source(model_dir) == str(model_dir)


def test_a_converted_folder_next_to_the_model_is_preferred(model_dir):
    fast = model_dir.parent / "bge-m3-fp16"
    fast.mkdir()
    (fast / "model.safetensors").write_bytes(b"x")
    assert embedding.fp16_source(model_dir) == str(fast)


def test_an_incomplete_converted_folder_is_ignored(model_dir):
    (model_dir.parent / "bge-m3-fp16").mkdir()
    assert embedding.fp16_source(model_dir) == str(model_dir)


def test_only_the_gpu_path_uses_the_converted_folder(model_dir, monkeypatch):
    fast = model_dir.parent / "bge-m3-fp16"
    fast.mkdir()
    (fast / "model.safetensors").write_bytes(b"x")
    monkeypatch.setattr(embedding, "cuda_available", lambda: True)
    assert embedding._EmbedResource(model_dir, "cuda")._source == str(fast)
    assert embedding._EmbedResource(model_dir, "cpu")._source == str(model_dir)


class FakeModel:
    def __init__(self, source, device):
        self.source, self.halved = source, False

    def half(self):
        self.halved = True

    def save(self, path, safe_serialization):
        import os

        os.makedirs(path)
        with open(os.path.join(path, "model.safetensors"), "wb") as f:
            f.write(b"weights")
        if FakeModel.fail:
            raise OSError("disk full")


FakeModel.fail = False


@pytest.fixture
def fake_sentence_transformers(monkeypatch):
    FakeModel.fail = False
    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=FakeModel))
    return FakeModel


def test_convert_writes_the_fp16_folder_and_skips_when_it_exists(model_dir, fake_sentence_transformers):
    target = convert_embedding.convert(str(model_dir))
    assert target == str(model_dir) + "-fp16" and (model_dir.parent / "bge-m3-fp16" / "model.safetensors").exists()
    FakeModel.fail = True                      # 已經轉過就不會再載入模型
    assert convert_embedding.convert(str(model_dir)) == target


def test_a_failed_conversion_leaves_no_partial_folder(model_dir, fake_sentence_transformers):
    FakeModel.fail = True
    with pytest.raises(OSError):
        convert_embedding.convert(str(model_dir))
    assert not list(model_dir.parent.glob("bge-m3-fp16*"))


def test_convert_reports_a_missing_model(tmp_path):
    with pytest.raises(FileNotFoundError):
        convert_embedding.convert(str(tmp_path / "missing"))
