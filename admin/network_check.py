"""
network_check.py：後台網路自我檢查——兩台直連前跑一次，逐項回報哪裡不對（不改任何設定、不寫入資料）。

主要功能：
  - 角色是不是後台、通行碼環境變數有沒有設且夠強
  - 有沒有「網路線」那張網卡的 169.254.x.x 位址（Wi-Fi、藍牙、VPN 的不算）
  - 連線服務（TCP 8770）與自動尋找（UDP 8772）有沒有人在聽，聽的位址是不是網路線那一段
  - Windows 防火牆規則是否存在、已啟用、放行、而且只放行 169.254.0.0/16
    （讀規則不需要系統管理員，讀不到就警告）
  - 網路線那張網卡的網路類別（公用／私人）會不會讓規則不套用
  - 每項回報 OK／WARN／FAIL，有 FAIL 時結束碼為 1；啟動：python -m admin.network_check 或 deploy\\檢查後台網路.bat

設計：判斷邏輯都是純函式（吃資料、回傳結果），系統呼叫（psutil、PowerShell）集中在 read_* 函式，測試時替換。

串接模組：
  - shared.role_config（角色與通行碼）、shared.lan_discovery（網卡與位址）
  - deploy\\setup_back_network.ps1 建立被檢查的規則；deploy\\檢查後台網路.bat 是雙擊入口
"""
import base64
import ipaddress
import json
import os
import subprocess  # noqa: S404  # nosec B404  只執行固定的唯讀 PowerShell 查詢
import sys

from shared import lan_discovery, role_config

RULE_PREFIX = "SmartAccess"
# (規則顯示名稱, 協定, 埠)：必須和 deploy\setup_back_network.ps1 的規則一致（測試會核對）
SERVICE_RULE = ("SmartAccess 後台連線（只允許網路線直連）", "TCP")
DISCOVERY_RULE = ("SmartAccess 後台尋找回應（只允許網路線直連）", "UDP")

_RULES_QUERY = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    "Get-NetFirewallRule -DisplayName 'SmartAccess*' -ErrorAction SilentlyContinue | ForEach-Object {"
    "$p=$_|Get-NetFirewallPortFilter;$a=$_|Get-NetFirewallAddressFilter;"
    "[pscustomobject]@{Name=$_.DisplayName;Enabled=[string]$_.Enabled;Action=[string]$_.Action;"
    "Direction=[string]$_.Direction;Profile=[string]$_.Profile;Protocol=[string]$p.Protocol;"
    "LocalPort=[string]$p.LocalPort;RemotePort=[string]$p.RemotePort;RemoteAddress=[string]$a.RemoteAddress}"
    "} | ConvertTo-Json -Compress")
_PROFILE_QUERY = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                  "Get-NetConnectionProfile | ForEach-Object {[pscustomobject]@{Name=$_.InterfaceAlias;"
                  "Category=[string]$_.NetworkCategory}} | ConvertTo-Json -Compress")


# --- 系統呼叫（測試時替換）---

def _powershell(command):
    """執行一段固定的唯讀 PowerShell，回傳 stdout 文字；失敗丟 OSError／RuntimeError。"""
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                       "powershell.exe")
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")      # 避免中文與引號在命令列被弄壞
    done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],  # noqa: S603
                          capture_output=True, timeout=30, check=False)         # nosec B603
    if done.returncode != 0:
        raise RuntimeError((done.stderr or b"").decode("utf-8", "replace").strip()[:200] or "PowerShell 失敗")
    return done.stdout.decode("utf-8", "replace")


def read_firewall_rules():
    """名稱以 SmartAccess 開頭的防火牆規則清單（dict）；讀不到丟例外。"""
    return parse_json_list(_powershell(_RULES_QUERY))


def read_network_profiles():
    """每張網卡的網路類別 [{"Name", "Category"}]；讀不到丟例外。"""
    return parse_json_list(_powershell(_PROFILE_QUERY))


def read_listeners():
    """本機正在聽的 {(協定, 位址, 埠)}，協定是 TCP 或 UDP；讀不到丟例外。"""
    import psutil
    found = set()
    for kind, label in (("tcp", "TCP"), ("udp", "UDP")):
        for conn in psutil.net_connections(kind=kind):
            if conn.laddr and (label == "UDP" or conn.status == psutil.CONN_LISTEN):
                found.add((label, conn.laddr.ip, conn.laddr.port))
    return found


# --- 純判斷 ---

def parse_json_list(text):
    """PowerShell 的 ConvertTo-Json：一筆是物件、多筆是陣列、沒有是空字串；一律轉成 dict 清單。"""
    text = (text or "").strip().lstrip("\ufeff")
    if not text:
        return []
    data = json.loads(text)
    items = data if isinstance(data, list) else [data]
    return [item for item in items if isinstance(item, dict)]


def remote_scope(value):
    """防火牆遠端位址欄位 → "link_local"（剛好 169.254.0.0/16）、"any"（任何來源）或 "other"；
    Windows 會把網段寫成 169.254.0.0/255.255.0.0，也可能是用逗號隔開的清單。"""
    parts = [p.strip() for p in str(value or "").split(",") if p.strip()]
    if not parts or any(p.lower() in ("any", "*") for p in parts):
        return "any"
    try:
        nets = {ipaddress.ip_network(p, strict=False) for p in parts}
    except ValueError:
        return "other"
    return "link_local" if nets == {lan_discovery.LINK_LOCAL_NET} else "other"


def profile_covers(profile, category):
    """規則的 Profile（Any 或 "Domain, Private" 之類）是否涵蓋這個網路類別（Public／Private／DomainAuthenticated）。"""
    names = {p.strip().lower() for p in str(profile or "").split(",") if p.strip()}
    if not names or "any" in names or "all" in names:
        return True
    wanted = "domain" if str(category).lower().startswith("domain") else str(category).lower()
    return wanted in names


def check_role():
    """後台角色與通行碼。"""
    settings = role_config.load()
    if settings.role != role_config.BACK:
        return [("FAIL", f"角色是 {settings.role}，不是 back（執行 deploy\\設定後台網路.bat，或 "
                         "python -m shared.role_config set --role back --bind-host auto）")]
    results = [("OK", f"角色：back，綁定 {settings.bind_host or '（未設定）'}，連線埠 {settings.back_port}")]
    results.append(("OK", "通行碼已設定且夠強") if role_config.link_token()
                   else ("FAIL", f"通行碼沒設定或不夠強（環境變數 {role_config.TOKEN_ENV}，至少 "
                                 f"{role_config.MIN_TOKEN_CHARS} 字元；用 python -m shared.role_config token 產生，"
                                 "要和前台一樣；設好後要開新的終端機才讀得到）"))
    return results


def check_adapter(details=None, settings=None):
    """網路線網卡有沒有 169.254.x.x；auto 模式下沒有就是 FAIL（連線服務起不來），其他情況只警告。"""
    settings = settings or role_config.load()
    details = lan_discovery.link_local_details() if details is None else details
    wired = [(name, address) for name, address, is_wired in details if is_wired]
    if wired:
        extra = [f"{n}（{a}）" for n, a, is_wired in details if not is_wired]
        note = f"；另有非網路線的 169.254 位址：{'、'.join(extra)}（不會被使用）" if extra else ""
        return [("OK", "網路線位址：" + "、".join(f"{a}（{n}）" for n, a in wired) + note)]
    others = [f"{n}（{a}）" for n, a, _w in details]
    hint = ("找不到網路線網卡的 169.254.x.x 位址：確認網路線兩端都插好、兩台都開機、網卡設為「自動取得 IP」"
            "（位址分配約需 30 到 60 秒）")
    if others:
        hint += f"；只看到非網路線的位址：{'、'.join(others)}"
    level = "FAIL" if settings.bind_host.strip().lower() == role_config.AUTO else "WARN"
    return [(level, hint)]


def check_listeners(listeners, port=role_config.DEFAULT_BACK_PORT, discovery_port=lan_discovery.DISCOVERY_PORT):
    """連線服務與自動尋找有沒有人在聽；聽在 0.0.0.0 要警告（後台不該對所有網路開放）。"""
    results = []
    services = (("TCP", port, "連線服務", "後台介面開著時會自動啟動，或 python -m admin.link_server"),
                ("UDP", discovery_port, "自動尋找", "隨連線服務一起啟動"))
    for label, number, what, how in services:
        addresses = sorted(ip for proto, ip, p in listeners if proto == label and p == number)
        if not addresses:
            results.append(("WARN", f"{what}（{label} {number}）沒有人在聽：{how}"))
        elif any(ip in ("0.0.0.0", "::") for ip in addresses):  # noqa: S104  # nosec B104  這裡是檢查，不是綁定
            results.append(("WARN", f"{what}（{label} {number}）聽在所有網路介面，應只聽網路線那一段"))
        elif all(ipaddress.ip_address(ip) in lan_discovery.LINK_LOCAL_NET for ip in addresses):
            results.append(("OK", f"{what}（{label} {number}）執行中，只聽 {'、'.join(addresses)}"))
        else:
            results.append(("OK", f"{what}（{label} {number}）執行中，聽在 {'、'.join(addresses)}"))
    return results


def _rule_problems(rule, protocol, port):
    """一條規則哪裡不符；沒有問題回傳空清單。"""
    problems = []
    if str(rule.get("Enabled")).lower() != "true":
        problems.append("沒有啟用")
    if str(rule.get("Action")).lower() != "allow" or str(rule.get("Direction")).lower() != "inbound":
        problems.append("不是「入站允許」")
    if str(rule.get("Protocol")).upper() != protocol or str(rule.get("LocalPort")) != str(port):
        problems.append(f"協定或埠不是 {protocol} {port}")
    scope = remote_scope(rule.get("RemoteAddress"))
    if scope == "any":
        problems.append("允許任何來源（應該只放行 169.254.0.0/16）")
    elif scope == "other":
        problems.append(f"放行範圍不是 169.254.0.0/16（{rule.get('RemoteAddress')}）")
    return problems


def check_firewall(rules, port=role_config.DEFAULT_BACK_PORT, discovery_port=lan_discovery.DISCOVERY_PORT):
    """規則齊全、啟用、放行、範圍只有 169.254.0.0/16；rules 是 None 代表讀不到。"""
    if rules is None:
        return [("WARN", "讀不到防火牆規則，沒辦法確認（可用系統管理員身分重跑，或到 Windows 防火牆進階設定看規則）")]
    by_name = {str(r.get("Name")): r for r in rules}
    results = []
    for (name, protocol), number in ((SERVICE_RULE, port), (DISCOVERY_RULE, discovery_port)):
        rule = by_name.get(name)
        if rule is None:
            results.append(("FAIL", f"缺防火牆規則「{name}」：右鍵「設定後台網路.bat」→ 以系統管理員身分執行"))
            continue
        problems = _rule_problems(rule, protocol, number)
        results.append(("FAIL", f"防火牆規則「{name}」：{'、'.join(problems)}") if problems
                       else ("OK", f"防火牆規則「{name}」：{protocol} {number}，只放行 169.254.0.0/16"))
    return results


def check_category(profiles, rules, adapter_names):
    """網路線網卡的網路類別；沒有插線的直連網路多半被 Windows 當成「公用」，規則要涵蓋它才會生效。"""
    if profiles is None:
        return [("WARN", "讀不到網路類別，略過")]
    mine = [p for p in profiles if str(p.get("Name")) in adapter_names]
    if not mine:
        return [("WARN", "網路線網卡還沒有網路類別資料（剛插線時要等一下），略過")]
    results = []
    for item in mine:
        category = str(item.get("Category"))
        uncovered = [str(r.get("Name")) for r in (rules or []) if not profile_covers(r.get("Profile"), category)]
        if uncovered:
            results.append(("FAIL", f"網卡「{item.get('Name')}」的網路類別是 {category}，但規則只套用在其他類別："
                                    f"{'、'.join(uncovered)}（重跑設定後台網路.bat）"))
        else:
            results.append(("OK", f"網卡「{item.get('Name')}」的網路類別：{category}，防火牆規則會套用"))
    return results


def _safe(label, function):
    """系統呼叫失敗（沒權限、指令不存在）時回傳 None，不讓整個檢查中斷。"""
    try:
        return function()
    except Exception as e:  # noqa: BLE001
        print(f"[network_check] 讀取{label}失敗：{type(e).__name__}", file=sys.stderr)
        return None


def run_checks():
    """跑完整檢查，回傳 [(等級, 說明)]。"""
    settings = role_config.load()
    results = check_role()
    details = lan_discovery.link_local_details()
    results += check_adapter(details, settings)
    listeners = _safe("監聽埠", read_listeners)
    rules = _safe("防火牆規則", read_firewall_rules)
    if listeners is None:
        results.append(("WARN", "讀不到本機監聽埠，略過"))
    else:
        results += check_listeners(listeners, settings.back_port)
    results += check_firewall(rules, settings.back_port)
    results += check_category(_safe("網路類別", read_network_profiles), rules,
                              {name for name, _a, wired in details if wired})
    return results


def main():
    results = run_checks()
    for level, text in results:
        print(f"[{level:4}] {text}")
    failed = sum(1 for level, _ in results if level == "FAIL")
    print(f"\n檢查完成：{len(results)} 項，{failed} 項失敗，{sum(1 for lv, _ in results if lv == 'WARN')} 項警告")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
