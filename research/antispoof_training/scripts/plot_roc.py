import numpy as np
import matplotlib.pyplot as plt
from matplotlib import font_manager

_cjk_font_path = r"C:\Windows\Fonts\msjh.ttc"
font_manager.fontManager.addfont(_cjk_font_path)
plt.rcParams["font.family"] = font_manager.FontProperties(fname=_cjk_font_path).get_name()
plt.rcParams["axes.unicode_minus"] = False

FILES = {
    "MiniFASNetV2（現成模型）": {"path": "outputs/roc_data_minifasnetv2.npz", "eer": 0.3787},
    "凍結骨幹（基準）": {"path": "outputs/roc_data_antispoof_best.npz", "eer": 0.1373},
    "解凍微調": {"path": "outputs/roc_data_antispoof_finetuned_best.npz", "eer": 0.0783},
}

fig, ax = plt.subplots(figsize=(7, 6), dpi=150)

for label, info in FILES.items():
    data = np.load(info["path"])
    fpr, tpr = data["fpr"], data["tpr"]
    order = np.argsort(fpr)
    fpr, tpr = fpr[order], tpr[order]
    auc = np.trapezoid(tpr, fpr)
    ax.plot(fpr, tpr, linewidth=2, label=f"{label}（AUC={auc:.3f}, EER={info['eer']*100:.2f}%）")

ax.plot([0, 1], [0, 1], linestyle="--", color="gray", linewidth=1, label="隨機猜測")
ax.set_xlabel("False Positive Rate")
ax.set_ylabel("True Positive Rate")
ax.set_title("活體偵測模型 ROC 曲線對照（CelebA-Spoof 測試集，n=67,170）")
ax.legend(loc="lower right", fontsize=9)
fig.tight_layout()
fig.savefig("roc_comparison.png", dpi=150)
print("saved roc_comparison.png")
