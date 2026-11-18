"""
front_verify_report.py：前台自動驗證報告的存放與下達（後台側）。

主要功能：
  - save(request_id, lines, remote_addr)：把前台回傳的報告存成 runtime/verification/前台自動_<請求編號>.md
    （先蓋掉通行碼、金鑰、雜湊類長字串）
  - ask(wait_seconds)：後台下達一次前台自動驗證（front_control.request_verify），等前台在下一次同步（約 10 秒）收到、
    跑完（第一題問答要載入模型，最久約 4 分鐘）並回傳；回傳報告文字，逾時回 None
  - 命令列：python -m admin.front_verify_report（後台連線服務要開著、前台代理要在跑；不需要在前台操作任何東西）

串接模組：admin.link_server（/v1/morning 帶 verify_request、POST /v1/verify-report 收報告）、
access.front_agent（前台收到請求後執行）、shared.front_control（請求編號）、shared.verify_common（蓋掉敏感字串、原子寫檔）。
"""
import datetime
import os
import sys
import time

from shared import front_control, paths, verify_common


def report_path(request_id):
    return os.path.join(paths.RUNTIME_DIR, "verification", f"前台自動_{request_id}.md")


def save(request_id, lines, remote_addr="", now=datetime.datetime.now):
    """存下前台回傳的報告（每行先蓋掉敏感字串）；回傳檔案路徑。"""
    body = [f"# 前台自動驗證報告（請求 {request_id}）", "",
            f"- 收到時間：{now():%Y-%m-%d %H:%M:%S}", f"- 前台位址：{remote_addr}", ""]
    body += [f"- {verify_common.scrub(line)}" for line in lines]
    path = report_path(request_id)
    verify_common.atomic_write(path, "\n".join(body) + "\n")
    return path


def read(request_id):
    """讀回報告文字；還沒收到回 None。"""
    try:
        with open(report_path(request_id), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def ask(wait_seconds=420, clock=time.time, sleep=time.sleep, say=print):
    """下達請求並等前台回傳；回傳 (請求編號, 報告文字或 None)。"""
    request_id = front_control.request_verify()
    say(f"已下達前台自動驗證（請求 {request_id}），等前台下一次同步收到並跑完…")
    deadline = clock() + wait_seconds
    while clock() < deadline:
        text = read(request_id)
        if text is not None:
            return request_id, text
        sleep(2)
    return request_id, None


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    request_id, text = ask()
    if text is None:
        print("逾時：前台沒有回報。確認前台代理在跑、後台連線服務開著（前台版本要含自動驗證功能）。")
        return 1
    print(text)
    print(f"報告檔：{report_path(request_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
