"""
entity_resolver.py：把使用者輸入的姓名對應到名單上的人（容錯錯字，但不亂配）。

主要功能：
  - 姓名完全一致 → 直接採用
  - 否則用「向量相似度」與「編輯距離」中較高的分數比對名單：
      夠高且明顯勝過第二名 → 直接採用（confident）
      中間分數、或兩個人分數太接近 → 回傳候選讓使用者確認（ambiguous）
      都不夠高 → 查無此人（unknown），不會硬配一個最像的
  - 可以直接比對一個姓名字串，也可以從一整句問題裡找出提到的人

分數門檻（HIGH、LOW、MARGIN）是依少量姓名實驗訂的起始值，要用真實名單與評估集校準。

串接模組：
  - queries.roster：名單來源（只有姓名與編號）
  - 正式環境的 embed_fn 來自 bge-m3 的資源包裝
  - router / answer（之後）：先解析出是誰，才呼叫固定查詢函式
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Sequence, Tuple

import numpy as np
from rapidfuzz.distance import Levenshtein

HIGH = 0.75
LOW = 0.55
MARGIN = 0.10
_MAX_CANDIDATES = 3

EmbedFn = Callable[[List[str]], np.ndarray]


@dataclass
class Resolution:
    """status：exact、confident（name 有值）、ambiguous（候選待確認）、multiple（提到不只一個人）、unknown。"""
    status: str
    name: str = ""
    candidates: List[Tuple[str, float]] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.status in ("exact", "confident")


def _unit(vectors) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def _edit_similarity(a: str, b: str) -> float:
    return 1.0 - Levenshtein.distance(a, b) / max(len(a), len(b), 1)


class NameResolver:
    def __init__(self, names: Sequence[str], embed_fn: EmbedFn):
        self._embed_fn = embed_fn
        self._names: List[str] = []
        self._vectors = np.zeros((0, 1), dtype=np.float32)
        self.update_names(names)

    def update_names(self, names: Sequence[str]):
        """名單有變動時重新載入（相同就不重算）。"""
        names = sorted({n.strip() for n in names if n and n.strip()})
        if names == self._names:
            return
        self._names = names
        self._vectors = _unit(self._embed_fn(names)) if names else np.zeros((0, 1), dtype=np.float32)

    def _decide(self, scores: Dict[str, float]) -> Resolution:
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if not ranked:
            return Resolution("unknown")
        best_name, best = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        top = [(n, round(s, 3)) for n, s in ranked[:_MAX_CANDIDATES] if s >= LOW]
        if best >= HIGH and best - second >= MARGIN:
            return Resolution("confident", best_name, top)
        if best >= LOW:
            return Resolution("ambiguous", "", top)
        return Resolution("unknown")

    def resolve(self, mention: str) -> Resolution:
        """比對一個姓名字串（例如 LLM 從問題裡擷取出的人名）。"""
        mention = (mention or "").strip()
        if not mention or not self._names:
            return Resolution("unknown")
        if mention in self._names:
            return Resolution("exact", mention, [(mention, 1.0)])
        vec = _unit(self._embed_fn([mention]))[0]
        cosines = self._vectors @ vec
        scores = {n: max(float(c), _edit_similarity(mention, n)) for n, c in zip(self._names, cosines, strict=True)}
        return self._decide(scores)

    def find_in_message(self, message: str) -> Resolution:
        """從一整句問題裡找出提到的人：先找完整姓名，找不到再用滑動視窗做容錯比對。"""
        message = (message or "").strip()
        if not message or not self._names:
            return Resolution("unknown")
        exact = [n for n in self._names if n in message]
        if exact:
            longest = max(exact, key=len)
            others = [n for n in exact if n not in longest]
            if not others:
                return Resolution("exact", longest, [(longest, 1.0)])
            return Resolution("multiple", "", [(n, 1.0) for n in sorted(exact)])
        windows = self._windows(message)
        if not windows:
            return Resolution("unknown")
        window_vecs = _unit(self._embed_fn(windows))
        sims = self._vectors @ window_vecs.T
        scores = {}
        for row, name in enumerate(self._names):
            edit_best = max(_edit_similarity(name, w) for w in windows if abs(len(w) - len(name)) <= 1) \
                if any(abs(len(w) - len(name)) <= 1 for w in windows) else 0.0
            scores[name] = max(float(sims[row].max()), edit_best)
        return self._decide(scores)

    def _windows(self, message: str) -> List[str]:
        lengths = {n for name in self._names for n in (len(name) - 1, len(name), len(name) + 1) if n >= 2}
        seen, out = set(), []
        for size in sorted(lengths):
            for start in range(0, len(message) - size + 1):
                w = message[start:start + size]
                if w not in seen:
                    seen.add(w)
                    out.append(w)
        return out
