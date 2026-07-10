"""信号分层 + 三角验证 。

核心公理:情报的价值 = 它还没被市场定价的程度。
  P0 已定价   — 盘口价/英文头条/刷屏消息。无 edge,只配当截止线。
  P1 半定价   — 中文先爆英文未跟 / 小圈子在传。分钟级窗口,扩散即贬值。
  P2 未定价   — 一手观察/亲验/语言地理隔离的局部信息。唯一值得下注的层。

规则(写死在代码里,不靠自觉):
  - 单一来源不下注:P2 需要 >=2 个独立来源互证,或 verified_firsthand=True。
  - 信号只降级不升级:P2 -> P1 -> P0(扩散是单向的)。
  - 每条信号必须关联到具体盘(condition_id),关联不上的"大新闻"是噪音。

存储:~/market-intel/signals/signals.jsonl,append + id 去重,Think-in-Code 沙盒里分析。
"""
from __future__ import annotations

import json
import hashlib
import os
import time
from dataclasses import dataclass, field, asdict

from intel.paths import SIGNALS

SIGNALS_PATH = os.path.join(SIGNALS, "signals.jsonl")

TIERS = ("P0", "P1", "P2")


@dataclass
class Signal:
    claim: str                      # 一句话:发生了什么
    tier: str                       # P0 / P1 / P2
    sources: list = field(default_factory=list)   # [{platform, url_or_id, seen_at}]
    condition_id: str = ""          # 关联的 Polymarket 盘;空 = 还没找到 = 还是噪音
    verified_firsthand: bool = False
    created_at: float = field(default_factory=time.time)
    notes: str = ""

    @property
    def id(self) -> str:
        return hashlib.sha1(self.claim.encode("utf-8")).hexdigest()[:16]

    def independent_sources(self) -> int:
        return len({s.get("platform") for s in self.sources})

    def tradeable(self) -> tuple[bool, str]:
        """闸门:这条信号够不够格进下一步(scout 容量检查 -> paper)。"""
        if self.tier not in TIERS:
            return False, f"tier 非法: {self.tier}"
        if self.tier == "P0":
            return False, "P0 已定价,无 edge,扔掉"
        if not self.condition_id:
            return False, "没关联到具体盘 —— 关联不上的大新闻是噪音"
        if self.tier == "P2" and not (self.independent_sources() >= 2 or self.verified_firsthand):
            return False, "P2 需要 >=2 独立来源互证或亲验,单一来源不下注"
        return True, "过闸,下一步: scout 查容量(edge×容量才是钱), 然后 paper"

    def demote(self, reason: str = "") -> None:
        """扩散即贬值,只降不升。"""
        idx = TIERS.index(self.tier)
        if idx > 0:
            self.tier = TIERS[idx - 1]
            self.notes += f" | 降级@{time.strftime('%m-%d %H:%M')}: {reason}"


def save(sig: Signal) -> bool:
    """append + id 去重。返回是否新写入。"""
    seen = set()
    if os.path.exists(SIGNALS_PATH):
        with open(SIGNALS_PATH, encoding="utf-8") as fh:
            for line in fh:
                try:
                    seen.add(json.loads(line).get("_id"))
                except Exception:
                    pass
    if sig.id in seen:
        return False
    row = asdict(sig)
    row["_id"] = sig.id
    with open(SIGNALS_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def load_all() -> list[dict]:
    if not os.path.exists(SIGNALS_PATH):
        return []
    rows = []
    with open(SIGNALS_PATH, encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows
