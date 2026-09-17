"""
把訓練好的模型匯出成 ONNX，並用 onnxsim 簡化計算圖
（清掉多餘節點，通常能再降低一點推論延遲，也讓模型檔案更精簡）。

輸出檔名會帶上是哪個 checkpoint 匯出的（跟 evaluate_antispoof.py 的
roc_data_<model_tag>.npz 同一個邏輯），避免評估/匯出 finetuned 版時，
不小心覆蓋掉凍結骨幹版的 .onnx，兩份都留著才方便跟 MiniFASNetV2 對照比較。

用法：
    python scripts/export_onnx.py                                  # 匯出 outputs/antispoof_best.pth（凍結骨幹版）
    python scripts/export_onnx.py outputs/antispoof_finetuned_best.pth  # 匯出指定的 checkpoint（例如微調過的版本）
"""
import os
import sys

import torch
import torch.nn as nn
from torchvision import models

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")
NUM_CLASSES = 2


def main():
    model_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(OUTPUT_DIR, "antispoof_best.pth")
    print(f"匯出模型：{model_path}")

    # 檔名帶上是哪個 checkpoint 匯出的，避免匯出 finetuned 版時覆蓋掉凍結骨幹版的 .onnx
    model_tag = os.path.splitext(os.path.basename(model_path))[0]
    raw_onnx_path = os.path.join(OUTPUT_DIR, f"antispoof_mobilenetv3_{model_tag}.onnx")
    sim_onnx_path = os.path.join(OUTPUT_DIR, f"antispoof_mobilenetv3_{model_tag}_sim.onnx")

    model = models.mobilenet_v3_small(weights=None)
    in_features = model.classifier[-1].in_features
    model.classifier[-1] = nn.Linear(in_features, NUM_CLASSES)
    model.load_state_dict(torch.load(model_path, map_location="cpu"))
    model.eval()

    dummy_input = torch.randn(1, 3, 224, 224)
    export_kwargs = dict(
        input_names=["input"], output_names=["output"],
        dynamic_axes={"input": {0: "batch_size"}, "output": {0: "batch_size"}},
        opset_version=12,
    )
    try:
        # 新版 PyTorch 的 torch.onnx.export 預設會先嘗試新的 dynamo 匯出器，
        # 需要額外裝 onnxscript 套件才能用；這裡明確指定用舊版（TorchScript-based）
        # 匯出器，跟這支腳本原本的 dynamic_axes/opset_version 介面相容，不需要
        # 多裝套件。舊版 PyTorch 沒有 dynamo 參數，TypeError 就退回原本的呼叫方式。
        torch.onnx.export(model, dummy_input, raw_onnx_path, dynamo=False, **export_kwargs)
    except TypeError:
        torch.onnx.export(model, dummy_input, raw_onnx_path, **export_kwargs)
    print(f"已匯出 {raw_onnx_path}")

    try:
        from onnxsim import simplify
        import onnx

        onnx_model = onnx.load(raw_onnx_path)
        simplified_model, check_ok = simplify(onnx_model)
        if check_ok:
            onnx.save(simplified_model, sim_onnx_path)
            print(f"已用 onnxsim 簡化並存至 {sim_onnx_path}")
        else:
            print(f"⚠️ onnxsim 簡化後驗證失敗，仍使用未簡化版本 {os.path.basename(raw_onnx_path)} 即可")
    except ImportError:
        print(f"⚠️ 未安裝 onnxsim（pip install onnxsim），略過簡化步驟，直接使用 {os.path.basename(raw_onnx_path)}")


if __name__ == "__main__":
    main()
