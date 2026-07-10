"""猎场扫描器（scout）。只读公开接口，不碰私钥。

解决两个选盘盲点：
1. 别按娱乐性选盘：热门盘定价最有效（=送钱给做市商），要反过来找
   "冷清、定价懒、对手是散户"的盘。
2. edge × 容量才是钱：20% 的 edge 如果订单簿只能塞 $30，期望收益 $6，
   不值得花研究时间。所以最后一步用真实订单簿量容量。

流程：
  scan_universe()  拉活跃市场 + 过滤死盘/定局盘
  hunt_score()     按"猎场分"排序（定价懒 + 量适中 + 流动性浅）
  measure_capacity() 对 top N 拉真实订单簿，量 2% 滑点内能部署多少美元
  scout()          串起来，按"假设 5% edge 的期望$"排序输出
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from polymarket_agent.markets import (
    GAMMA_HOST,
    _get,
    fetch_order_book,
    parse_json_list,
)

# ---- 过滤阈值 ----
PRICE_FLOOR = 0.02   # 低于 2¢ 视为已定局（No 侧同理），没肉
PRICE_CEIL = 0.98
PAGE_SIZE = 100
PAGE_SLEEP = 0.3     # 分页/订单簿请求间隔，防限速
BOOK_SLEEP = 0.3

# ---- 猎场分参数 ----
# spread：越宽越好 = 定价越懒。0.5¢ 以内视为职业盘（0 分），8¢ 封顶（满分）。
SPREAD_LO = 0.005
SPREAD_HI = 0.08
# 24h 成交量甜区：太热(> $50k)=职业盘守着，太冷(< $500)=没对手盘、成交都难。
VOL_SWEET_LO = 500.0
VOL_SWEET_HI = 50_000.0
# 流动性（Gamma 的 liquidity 字段，做市资金深度）：越浅越好。
# ≤ $2k 满分；每高一个数量级扣一半，$200k 以上 0 分（有人守）。
LIQ_SHALLOW = 2_000.0
LIQ_DEEP_DECADES = 2.0
# 权重：定价懒是核心信号，其次对手盘结构，最后深度。
W_SPREAD, W_VOL, W_LIQ = 0.40, 0.35, 0.25

# ---- 容量假设 ----
ASSUMED_EDGE = 0.05   # 假设我方研究后有 5% 的 edge（只是标尺，不是预言）
MAX_SLIPPAGE = 0.02   # 吃单不越过 best_ask * 1.02


@dataclass
class Candidate:
    question: str
    slug: str
    token_id: str        # Yes 侧 clobTokenId（clobTokenIds[0]）
    mid: float           # Gamma bestBid/bestAsk 中间价（缺则用 outcomePrices[0]）
    spread: float
    volume24h: float
    liquidity: float | None
    score: float = 0.0


@dataclass
class HuntingGround:
    cand: Candidate
    book_mid: float | None    # 真实订单簿中间价（比 Gamma 快照新鲜）
    best_ask: float | None
    capacity_usd: float       # 2% 滑点内可从 ask 侧吃到的美元
    expected_usd: float       # capacity × 5% edge 假设


def _f(v, default=None) -> float | None:
    """Gamma 数值字段可能是 str/None/缺失，统一容错转 float。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def scan_universe(max_markets: int = 300) -> list[Candidate]:
    """分页拉活跃市场，过滤死盘（24h 量 0/None）和定局盘（价格出 2%-98%）。

    注意：order=volume24hr 降序意味着先看到热盘——它们大多会被
    hunt_score 的量甜区压下去，这里只负责把"根本没人交易"和
    "已经赛完"的踢掉。
    """
    out: list[Candidate] = []
    seen: set[str] = set()
    offset = 0
    while offset < max_markets:
        page = _get(
            f"{GAMMA_HOST}/markets",
            {
                "active": "true",
                "closed": "false",
                "limit": min(PAGE_SIZE, max_markets - offset),
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
        if not page:
            break
        for m in page:
            vol24 = _f(m.get("volume24hr"))
            if not vol24:  # None 或 0 → 死盘
                continue
            token_ids = parse_json_list(m.get("clobTokenIds"))
            if not token_ids:
                continue
            bid = _f(m.get("bestBid"))
            ask = _f(m.get("bestAsk"))
            prices = [p for p in (_f(x) for x in parse_json_list(m.get("outcomePrices"))) if p is not None]
            if bid is not None and ask is not None and ask > 0:
                mid = (bid + ask) / 2
            elif prices:
                mid = prices[0]
            else:
                continue
            if mid < PRICE_FLOOR or mid > PRICE_CEIL:
                continue  # 已定局，剩的是尾部风险不是 edge
            spread = _f(m.get("spread"))
            if spread is None and bid is not None and ask is not None:
                spread = max(ask - bid, 0.0)
            if spread is None:
                spread = 0.0  # 没数据按最紧算，宁可漏不冤枉
            tid = str(token_ids[0])
            if tid in seen:
                continue
            seen.add(tid)
            out.append(
                Candidate(
                    question=(m.get("question") or m.get("slug") or "?").strip(),
                    slug=m.get("slug") or "",
                    token_id=tid,
                    mid=mid,
                    spread=spread,
                    volume24h=vol24,
                    liquidity=_f(m.get("liquidity")),
                )
            )
        if len(page) < PAGE_SIZE:
            break
        offset += len(page)
        time.sleep(PAGE_SLEEP)
    return out


def hunt_score(c: Candidate) -> float:
    """猎场分 0-100：这个盘的对手方有多"好欺负"。

    - spread 分：价差宽 = 没人认真报价 = 定价懒。线性 0.5¢→8¢。
    - 量分：24h 量在 $500-$50k 甜区满分；出区后每差一个数量级扣满一分
      （对数距离）。太热=职业做市盘，太冷=想赢也没人接。
    - 流动性分：深度浅 = 没有做市商守着 = 价格容易错。≤$2k 满分，
      对数衰减，两个数量级（$200k）归零。
    """
    from math import log10

    # spread ∈ [0,1] 概率单位
    s = (c.spread - SPREAD_LO) / (SPREAD_HI - SPREAD_LO)
    spread_score = min(max(s, 0.0), 1.0)

    if VOL_SWEET_LO <= c.volume24h <= VOL_SWEET_HI:
        vol_score = 1.0
    elif c.volume24h < VOL_SWEET_LO:
        vol_score = max(0.0, 1.0 - log10(VOL_SWEET_LO / max(c.volume24h, 1.0)))
    else:
        vol_score = max(0.0, 1.0 - log10(c.volume24h / VOL_SWEET_HI))

    if c.liquidity is None:
        liq_score = 0.5  # 缺数据给中性分
    elif c.liquidity <= LIQ_SHALLOW:
        liq_score = 1.0
    else:
        liq_score = max(0.0, 1.0 - log10(c.liquidity / LIQ_SHALLOW) / LIQ_DEEP_DECADES)

    return 100.0 * (W_SPREAD * spread_score + W_VOL * vol_score + W_LIQ * liq_score)


def measure_capacity(token_id: str) -> tuple[float | None, float | None, float]:
    """拉真实订单簿，量 ask 侧容量。

    返回 (book_mid, best_ask, capacity_usd)：
    从 best_ask 起，累计所有 price <= best_ask*(1+2%) 的档位美元额
    （price × size）。即"以 ≤2% 滑点吃单，能部署多少美元"。
    CLOB /book 的档位排序不保证方向，这里自己排。
    """
    book = fetch_order_book(token_id)
    asks = sorted(
        ((float(a["price"]), float(a["size"])) for a in book.get("asks", [])),
        key=lambda x: x[0],
    )
    bids = sorted(
        ((float(b["price"]), float(b["size"])) for b in book.get("bids", [])),
        key=lambda x: -x[0],
    )
    if not asks:
        return None, None, 0.0
    best_ask = asks[0][0]
    book_mid = (best_ask + bids[0][0]) / 2 if bids else None
    limit = best_ask * (1 + MAX_SLIPPAGE)
    capacity = sum(p * sz for p, sz in asks if p <= limit)
    return book_mid, best_ask, capacity


def scout(max_markets: int = 300, top_n: int = 20) -> list[HuntingGround]:
    """全流程：扫宇宙 → 猎场分排序 → top N 量真实容量 → 按期望$排序。

    只量了 Yes 侧买入容量；若 edge 在 No 侧，量级大致对称（No ask ≈ 1 - Yes bid），
    作为筛选标尺够用。
    """
    cands = scan_universe(max_markets)
    for c in cands:
        c.score = hunt_score(c)
    cands.sort(key=lambda c: c.score, reverse=True)

    grounds: list[HuntingGround] = []
    for c in cands[:top_n]:
        try:
            book_mid, best_ask, cap = measure_capacity(c.token_id)
        except Exception:
            book_mid, best_ask, cap = None, None, 0.0  # 单盘失败不拖垮整轮
        grounds.append(
            HuntingGround(
                cand=c,
                book_mid=book_mid,
                best_ask=best_ask,
                capacity_usd=cap,
                expected_usd=cap * ASSUMED_EDGE,
            )
        )
        time.sleep(BOOK_SLEEP)
    grounds.sort(key=lambda g: g.expected_usd, reverse=True)
    return grounds
