"""
front_check.py：前台自我檢查——安裝完或出問題時跑一次，逐項回報哪裡有問題（不改任何設定、不寫入資料）。

主要功能：
  - 角色設定與通行碼（是否為前台、位址合法、通行碼已設定）
  - 必要的模型檔、字型、套件是否都在、能不能載入
  - 攝影機能不能開、後台連不連得上（用通行碼 ping 後台；連不上只警告，前台離線也能辨識）
  - 每項回報 OK／WARN／FAIL，有 FAIL 時結束碼為 1

串接模組：
  - role_config、front_agent（BackClient）、paths；install_front.ps1 安裝最後一步會呼叫它
"""
import importlib
import os
import sys
import time

from shared import lan_discovery, paths, role_config

REQUIRED_MODELS = ("arcface_r100_v1.onnx", "MiniFASNetV2.onnx", "yolov8s.pt",
                   os.path.join("vits-piper-zh_CN-huayan-medium", "zh_CN-huayan-medium.onnx"))
REQUIRED_PACKAGES = ("cv2", "numpy", "onnxruntime", "sklearn", "skimage", "PIL", "requests", "customtkinter", "psutil",
                     "pyqrcode", "sherpa_onnx", "sounddevice", "ultralytics", "retinaface")
FONT_NAME = "msjh.ttc"


def check_role():
    """前台角色、位址與通行碼。"""
    settings = role_config.load()
    if settings.role != role_config.FRONT:
        return [("FAIL", f"角色是 {settings.role}，不是 front（用 python -m shared.role_config set --role front …）")]
    results = [("OK", f"角色：front，後台 {settings.back_host}:{settings.back_port}")]
    try:
        results.append(("OK", f"網路線位址：{lan_discovery.auto_address()}"))
    except lan_discovery.DiscoveryError as e:
        results.append(("WARN", str(e)))
    results.append(("OK", "通行碼已設定") if role_config.link_token()
                   else ("FAIL", f"通行碼沒設定或太短（環境變數 {role_config.TOKEN_ENV}，至少 "
                                 f"{role_config.MIN_TOKEN_CHARS} 字元，要和後台一樣）"))
    return results


def check_files(models_dir=None):
    """模型檔與字型。"""
    models_dir = models_dir or paths.MODELS_DIR
    results = []
    for name in REQUIRED_MODELS:
        path = os.path.join(models_dir, name)
        results.append(("OK", f"模型 {name}") if os.path.isfile(path) else ("FAIL", f"缺模型檔：{path}"))
    font = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", FONT_NAME)
    results.append(("OK", "字型 微軟正黑體") if os.path.isfile(font)
                   else ("WARN", f"找不到字型 {FONT_NAME}，畫面中文可能顯示成方框"))
    return results


def check_packages():
    """第三方套件能不能載入（retinaface 會一併載入 TensorFlow，要幾秒）。"""
    results = []
    for name in REQUIRED_PACKAGES:
        try:
            importlib.import_module(name)
            results.append(("OK", f"套件 {name}"))
        except Exception as e:  # noqa: BLE001  任何載入失敗都要如實回報
            results.append(("FAIL", f"套件 {name} 載入失敗：{type(e).__name__}: {e}"))
    return results


def check_gpu():
    """有 NVIDIA 顯卡時，確認 PyTorch 與 onnxruntime 真的用得到（不然會靜默退回 CPU，辨識變慢）；沒顯卡只提示。"""
    import shutil
    import subprocess  # nosec B404
    if not shutil.which("nvidia-smi"):
        return [("OK", "沒有 NVIDIA 顯卡，使用 CPU（辨識較慢）")]
    try:
        name = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],  # noqa: S603, S607  # nosec B603 B607
                              capture_output=True, text=True, timeout=10, check=True).stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return [("WARN", "nvidia-smi 執行失敗，無法確認顯卡")]
    import torch
    results = [("OK", f"PyTorch 可使用 {name}") if torch.cuda.is_available()
               else ("WARN", f"偵測到 {name}，但 PyTorch 用不到 GPU（驅動太舊或裝到 CPU 版）：尾隨偵測會用 CPU")]
    results.extend(_check_onnx_cuda(name))
    return results


def _dummy_input(session):
    """依模型的輸入形狀做一張全零的假輸入（沒寫死的維度當 1）。"""
    import numpy as np
    meta = session.get_inputs()[0]
    shape = [d if isinstance(d, int) and d > 0 else 1 for d in meta.shape]
    return {meta.name: np.zeros(shape, dtype=np.float32)}


def time_inference(session, runs=20):
    """用假輸入真的推論 runs 次，回傳每次平均毫秒（先暖機一次，不算在內）；推論出錯就丟例外。"""
    feed = _dummy_input(session)
    session.run(None, feed)
    start = time.perf_counter()
    for _ in range(runs):
        session.run(None, feed)
    return (time.perf_counter() - start) / runs * 1000.0


def judge_gpu_speed(gpu_ms, cpu_ms):
    """GPU 與 CPU 每次推論的毫秒數 → (等級, 說明)：GPU 比 CPU 快才算真的有加速。"""
    if gpu_ms < cpu_ms:
        return "OK", f"GPU 每次推論約 {gpu_ms:.1f} 毫秒（CPU 約 {cpu_ms:.1f} 毫秒）"
    return ("WARN", f"GPU 推論 {gpu_ms:.1f} 毫秒不比 CPU（{cpu_ms:.1f} 毫秒）快：可能 cuDNN 沒載好，辨識速度會受影響")


def _check_onnx_cuda(gpu_name):
    """真的建一個 CUDA session 並實際推論、和 CPU 比速度才算數（套件列出 CUDA provider 不代表程式庫找得到，
    建得出 session 也不代表推論跑得動；啟動時印的 cuDNN 載入失敗訊息就是這種情況）。"""
    import onnxruntime
    try:
        onnxruntime.preload_dlls()
    except Exception:  # noqa: BLE001, S110  # nosec B110  載不到就由下面的 session 結果回報
        pass
    # 用大模型：GPU 該明顯比 CPU 快，才看得出 cuDNN 有沒有載好
    model = os.path.join(paths.MODELS_DIR, "arcface_r100_v1.onnx")
    try:
        session = onnxruntime.InferenceSession(model, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        device = session.get_providers()[0]
    except Exception as e:  # noqa: BLE001
        return [("WARN", f"onnxruntime 建立 session 失敗：{type(e).__name__}")]
    if device != "CUDAExecutionProvider":
        return [("WARN", f"偵測到 {gpu_name}，但 onnxruntime 退回 CPU（缺 CUDA 12 或 cuDNN 9 程式庫）："
                         "人臉與活體會用 CPU")]
    results = [("OK", f"onnxruntime 在 {gpu_name} 上用 GPU 建立 session")]
    try:
        gpu_ms = time_inference(session)
    except Exception as e:  # noqa: BLE001
        results.append(("WARN", f"GPU 實際推論失敗（{type(e).__name__}）：cuDNN 可能沒載好，辨識可能出錯或退回 CPU"))
        return results
    try:
        cpu_ms = time_inference(onnxruntime.InferenceSession(model, providers=["CPUExecutionProvider"]))
    except Exception as e:  # noqa: BLE001
        results.append(("OK", f"GPU 實際推論正常，每次約 {gpu_ms:.1f} 毫秒（CPU 對照失敗：{type(e).__name__}）"))
        return results
    results.append(judge_gpu_speed(gpu_ms, cpu_ms))
    return results


def check_camera(index=0):
    """攝影機能不能開並讀到一幀。"""
    try:
        import cv2
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        ok, _ = cap.read() if cap.isOpened() else (False, None)
        cap.release()
    except Exception as e:  # noqa: BLE001
        return [("FAIL", f"攝影機檢查出錯：{type(e).__name__}")]
    return [("OK", f"攝影機 {index} 可讀取") if ok else ("FAIL", f"攝影機 {index} 打不開或讀不到畫面")]


def check_back():
    """用通行碼 ping 後台；連不上只警告（前台離線時照樣辨識與暫存事件）。"""
    from access.front_agent import BackCertError, BackClient
    settings = role_config.load()
    if settings.role != role_config.FRONT or not role_config.link_token():
        return [("WARN", "角色或通行碼未就緒，略過後台連線檢查")]
    host, port, fingerprint = settings.back_host, settings.back_port, settings.back_fingerprint
    if host.strip().lower() == role_config.AUTO:
        found = lan_discovery.discover_back(role_config.link_token())
        if not found:
            return [("WARN", "自動尋找沒有找到後台：後台還沒開、網路線沒插好（位址分配要 30 到 60 秒）、"
                             "後台防火牆沒開 UDP 或通行碼不一致")]
        host, port, fingerprint = found
    try:
        BackClient(host, port, role_config.link_token(), fingerprint, timeout=(3.0, 5.0)).ping()
        return [("OK", f"後台 {host}:{port} 加密連線正常（憑證指紋相符）、通行碼正確")]
    except BackCertError as e:
        return [("FAIL", str(e))]
    except Exception as e:  # noqa: BLE001
        return [("WARN", f"連不上後台（{type(e).__name__}）：後台還沒開、網路線、防火牆或通行碼不一致都有可能")]


def main():
    results = []
    for step in (check_role, check_files, check_packages, check_gpu, check_camera, check_back):
        results += step()
    for level, text in results:
        print(f"[{level:4}] {text}")
    failed = sum(1 for level, _ in results if level == "FAIL")
    print(f"\n檢查完成：{len(results)} 項，{failed} 項失敗，{sum(1 for lv, _ in results if lv == 'WARN')} 項警告")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
