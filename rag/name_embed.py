"""
name_embed.py：不用任何模型的姓名向量，給只查出缺勤的後台聊天視窗用。

主要功能：
  - hash_embed：每個文字對應一個獨立的方向（整個字串的雜湊），所以只有「一模一樣」才相似、其餘都是零相似度；
    不載入 bge-m3，後台聊天視窗啟動時不用佔顯存與載入時間
  - 姓名比對的分數是「向量相似度與編輯距離取較高者」（entity_resolver）；這裡的向量不提供模糊比對，
    錯字的容錯完全靠編輯距離：錯一個字的三字姓名落在「請確認是不是這位」（ambiguous），不會悄悄配錯人，
    與其用字元重疊的向量讓「王小」這種片段高分而被直接採用（真模型驗證時發現過），不如寧可多問一句
  - 取捨：失去 bge-m3 對暱稱、同音異字的語意比對；這類情況會回「查無此人」，使用者改輸入正確姓名即可

串接模組：
  - entity_resolver.NameResolver：embed_fn 參數
  - bootstrap.create_attendance_runtime：只查出缺勤的助理用它比對員工姓名
"""
import zlib

import numpy as np

DIM = 4096


def hash_embed(texts):
    """文字清單 → (筆數, DIM) 的 float32 矩陣；相同文字同一個方向，不同文字（幾乎一定）互相垂直。"""
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for row, text in enumerate(texts):
        text = "".join(str(text).split())
        if text:
            out[row, zlib.crc32(text.encode("utf-8")) % DIM] = 1.0
    return out
