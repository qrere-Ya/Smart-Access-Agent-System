"""
convert_embedding.py：把 bge-m3 轉成 fp16 的 safetensors，載入尖峰的 RAM 由約 3.3GB 降到約 2.1GB。

主要功能：
  - python -m rag.convert_embedding：讀 models/bge-m3，寫出 models/bge-m3-fp16（約 1.1GB）
  - 先寫到暫存資料夾、完成後才改名，中途失敗不會留下半成品；已轉過就略過
  - 沒轉檔也能用：rag.embedding 找不到轉好的資料夾就退回原本的模型

串接模組：
  - rag.embedding：載入時優先使用轉好的資料夾；shared.paths：模型位置
"""
import os
import shutil
import sys

from rag.embedding import FP16_SUFFIX
from shared import paths


def convert(source, target=None):
    """轉檔並回傳目標資料夾；目標已存在就直接回傳。"""
    source = os.path.normpath(source)
    target = target or source + FP16_SUFFIX
    if os.path.isfile(os.path.join(target, "model.safetensors")):
        return target
    if not os.path.isdir(source):
        raise FileNotFoundError(f"找不到向量模型資料夾：{source}")
    from sentence_transformers import SentenceTransformer

    partial = target + ".partial"
    shutil.rmtree(partial, ignore_errors=True)
    try:
        model = SentenceTransformer(source, device="cpu")
        model.half()
        model.save(partial, safe_serialization=True)
        shutil.rmtree(target, ignore_errors=True)
        os.replace(partial, target)
    finally:
        shutil.rmtree(partial, ignore_errors=True)
    return target


def main():
    source = os.path.join(paths.MODELS_DIR, "bge-m3")
    try:
        target = convert(source)
    except FileNotFoundError as e:
        print(e)
        return 1
    print(f"完成：{target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
