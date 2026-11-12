"""
punch_state.py：同一個人連續被辨識到時，這一幀該「寫入打卡」、「繼續顯示剛才的結果」還是「顯示冷卻中」（純邏輯）。

主要功能：
  - punch_phase：剛打卡成功的前 hold 秒繼續顯示當時的結果（例如「上班報到成功」），讓人看得到；
    之後到 cooldown 秒為止顯示「打卡冷卻中」；超過冷卻才會再寫入
  - 修正前的做法是冷卻判斷排在前面，打卡成功的那一幀下一幀就被「冷卻中」蓋掉，使用者幾乎看不到成功畫面

串接模組：
  - terminal_app：背景分析每幀呼叫
"""
LOG, SHOW_RESULT, COOLDOWN = "log", "show_result", "cooldown"


def punch_phase(now, last_punch, hold, cooldown):
    """last_punch 是上次成功寫入打卡的時間（沒有打過為 None）。"""
    if last_punch is None:
        return LOG
    elapsed = now - last_punch
    if elapsed < hold:
        return SHOW_RESULT
    if elapsed < cooldown:
        return COOLDOWN
    return LOG
