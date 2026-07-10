"""Polymarket 套利检测（只读，不碰私钥）。

三类可验证机会：
1. binary_buy  — 同一盘买 Yes+No 成本 < 1（扣费后仍有边）
2. binary_sell — 同一盘卖 Yes+No 收入 > 1（需已持有或能 mint/拆分）
3. neg_risk_buy — 互斥多结果事件，买齐所有 Yes 成本 < 1

体育跨盘口（让球 vs 精确比分）需要 exact-score 子盘；当前活跃赛程里
这类盘很少，脚本会在有数据时再做一致性检验。
"""
from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Iterable

from polymarket_agent.markets import (
    best_levels,
    fetch_order_book,
    iter_active_events,
    parse_json_list,
)

# 官方 taker fee ≈ C * rate * p * (p*(1-p))^exponent（见 docs / feeSchedule）
DEFAULT_MIN_EDGE = 0.003  # 扣费后至少 0.3%
DEFAULT_MIN_SIZE = 20.0  # 两边可成交份额下限
DEFAULT_MIN_LIQUIDITY = 200.0
MAX_WORKERS = 10


@dataclass
class ArbOpportunity:
    kind: str
    edge: float
    edge_after_fee: float
    cost_or_proceeds: float
    size: float
    fee_rate: float
    event_title: str
    event_slug: str
    question: str
    market_slug: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _f(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fee_params(market: dict) -> tuple[float, float]:
    """Return (rate, exponent) from market feeSchedule."""
    schedule = market.get("feeSchedule")
    if isinstance(schedule, dict) and schedule.get("rate") is not None:
        return float(schedule["rate"]), float(schedule.get("exponent") or 1)
    if market.get("feesEnabled"):
        return 0.05, 1.0
    return 0.0, 1.0


def taker_fee_usdc(price: float, shares: float, market: dict) -> float:
    """USDC taker fee for buying `shares` at `price`."""
    if market.get("feesEnabled") is False:
        return 0.0
    rate, exp = fee_params(market)
    if rate <= 0 or shares <= 0:
        return 0.0
    p = min(max(price, 0.0), 1.0)
    return shares * p * rate * ((p * (1.0 - p)) ** exp)


def taker_fee_rate(market: dict) -> float:
    """Peak-ish headline rate for display only."""
    rate, _exp = fee_params(market)
    return rate


def _level_price_size(level: dict | None) -> tuple[float | None, float]:
    if not level:
        return None, 0.0
    return _f(level.get("price")), _f(level.get("size")) or 0.0


def binary_book_arb(
    market: dict,
    event_title: str = "",
    event_slug: str = "",
    min_edge: float = DEFAULT_MIN_EDGE,
    min_size: float = DEFAULT_MIN_SIZE,
) -> list[ArbOpportunity]:
    outcomes = [str(x) for x in parse_json_list(market.get("outcomes"))]
    token_ids = [str(x) for x in parse_json_list(market.get("clobTokenIds"))]
    if len(token_ids) != 2:
        return []
    if outcomes and set(x.lower() for x in outcomes) not in ({"yes", "no"}, set()):
        # Still allow non Yes/No labels if exactly two complementary outcomes.
        pass

    books = [fetch_order_book(tid) for tid in token_ids]
    asks, bids = [], []
    for book in books:
        ask, bid = best_levels(book)
        asks.append(ask)
        bids.append(bid)

    fee_display = taker_fee_rate(market)
    out: list[ArbOpportunity] = []
    question = market.get("question") or ""
    market_slug = market.get("slug") or ""

    yes_ask_p, yes_ask_s = _level_price_size(asks[0])
    no_ask_p, no_ask_s = _level_price_size(asks[1])
    if yes_ask_p is not None and no_ask_p is not None:
        cost = yes_ask_p + no_ask_p
        size = min(yes_ask_s, no_ask_s)
        # 每 share 结算 $1；两边 taker 各付一次动态手续费
        fee_usdc = taker_fee_usdc(yes_ask_p, size, market) + taker_fee_usdc(
            no_ask_p, size, market
        )
        edge = 1.0 - cost
        edge_after = edge - (fee_usdc / size if size else 0.0)
        if edge_after >= min_edge and size >= min_size:
            out.append(
                ArbOpportunity(
                    kind="binary_buy",
                    edge=edge,
                    edge_after_fee=edge_after,
                    cost_or_proceeds=cost,
                    size=size,
                    fee_rate=fee_display,
                    event_title=event_title,
                    event_slug=event_slug,
                    question=question,
                    market_slug=market_slug,
                    detail=f"buy Yes@{yes_ask_p:.4f} + No@{no_ask_p:.4f}",
                )
            )

    yes_bid_p, yes_bid_s = _level_price_size(bids[0])
    no_bid_p, no_bid_s = _level_price_size(bids[1])
    if yes_bid_p is not None and no_bid_p is not None:
        proceeds = yes_bid_p + no_bid_p
        size = min(yes_bid_s, no_bid_s)
        fee_usdc = taker_fee_usdc(yes_bid_p, size, market) + taker_fee_usdc(
            no_bid_p, size, market
        )
        edge = proceeds - 1.0
        edge_after = edge - (fee_usdc / size if size else 0.0)
        if edge_after >= min_edge and size >= min_size:
            out.append(
                ArbOpportunity(
                    kind="binary_sell",
                    edge=edge,
                    edge_after_fee=edge_after,
                    cost_or_proceeds=proceeds,
                    size=size,
                    fee_rate=fee_display,
                    event_title=event_title,
                    event_slug=event_slug,
                    question=question,
                    market_slug=market_slug,
                    detail=f"sell Yes@{yes_bid_p:.4f} + No@{no_bid_p:.4f}",
                )
            )
    return out


def _partition_markets(markets: list[dict]) -> list[dict]:
    """互斥结果全集：未关闭且有 token 的都算（含 active=False 的 Other/Person 占位）。"""
    out = []
    for m in markets:
        if m.get("closed"):
            continue
        if not parse_json_list(m.get("clobTokenIds")):
            continue
        out.append(m)
    return out


def _active_tradable(markets: list[dict]) -> list[dict]:
    """可下单子集。套利完备性请用 _partition_markets。"""
    active = []
    for m in _partition_markets(markets):
        if m.get("active") is False:
            continue
        if m.get("acceptingOrders") is False:
            continue
        active.append(m)
    return active


def _yes_ask_gamma(market: dict) -> float | None:
    ask = _f(market.get("bestAsk"))
    if ask is not None:
        return ask
    prices = parse_json_list(market.get("outcomePrices"))
    if prices:
        return _f(prices[0])
    return None


def _bundle_from_asks(
    event: dict,
    asks: list[tuple[dict, float, float]],
    kind: str,
    min_edge: float,
    min_size: float,
) -> list[ArbOpportunity]:
    if len(asks) < 2:
        return []
    cost = sum(p for _, p, _ in asks)
    size = min(s for _, _, s in asks)
    fee_usdc = sum(taker_fee_usdc(p, size, m) for m, p, _ in asks)
    edge = 1.0 - cost
    edge_after = edge - (fee_usdc / size if size else 0.0)
    if edge_after < min_edge or size < min_size:
        return []
    legs = ", ".join(
        f"{(m.get('groupItemTitle') or m.get('question') or '')[:40]}@{p:.4f}" for m, p, _ in asks
    )
    return [
        ArbOpportunity(
            kind=kind,
            edge=edge,
            edge_after_fee=edge_after,
            cost_or_proceeds=cost,
            size=size,
            fee_rate=max(taker_fee_rate(m) for m, _, _ in asks),
            event_title=event.get("title") or "",
            event_slug=event.get("slug") or "",
            question=event.get("title") or "",
            market_slug=event.get("slug") or "",
            detail=f"buy all Yes ({len(asks)} legs): {legs}",
        )
    ]


def looks_complete_partition(markets: list[dict]) -> bool:
    """启发式：结果集是否覆盖全部可能（有 Other / 区间兜底 / 固定枚举）。"""
    titles = [
        ((m.get("groupItemTitle") or "") + " " + (m.get("question") or "")).lower()
        for m in markets
    ]
    joined = " | ".join(titles)
    if any(
        k in joined
        for k in (
            "other",
            "another",
            "none of the above",
            "or below",
            "or above",
            "or higher",
            "or lower",
            "no next",
            "no meeting",
            "draw",
        )
    ):
        return True
    bare_nums: list[int] = []
    has_plus = False
    for t in titles:
        s = t.strip()
        if re.fullmatch(r"\d+", s):
            bare_nums.append(int(s))
        elif re.fullmatch(r"\d+\+", s):
            bare_nums.append(int(s[:-1]))
            has_plus = True
    if bare_nums and has_plus:
        # 计数盘缺 0..min-1 则不完备（如只有 4..15+）
        if min(bare_nums) > 0:
            return False
        return True
    if all(m.get("sportsMarketType") == "moneyline" for m in markets) and len(markets) >= 2:
        return True
    has_low = any("or below" in t or "or lower" in t for t in titles)
    has_high = any("or higher" in t or "or above" in t or re.search(r"\d+\+", t) for t in titles)
    if has_low and has_high:
        return True
    return False


def screen_bundle_gamma(
    event: dict,
    markets: list[dict],
    kind: str,
    min_edge: float = DEFAULT_MIN_EDGE,
    require_complete: bool = True,
) -> bool:
    """粗筛：Gamma bestAsk 加总是否可能 < 1。用全集（含占位盘）。"""
    part = _partition_markets(markets)
    if len(part) < 2:
        return False
    if require_complete and not looks_complete_partition(part):
        return False
    cost = 0.0
    for m in part:
        ask = _yes_ask_gamma(m)
        if ask is None:
            return False
        cost += ask
    return cost <= 1.01 or (1.0 - cost) >= (min_edge * 0.5)


def _bundle_yes_buy_arb(
    event: dict,
    markets: list[dict],
    kind: str,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_size: float = DEFAULT_MIN_SIZE,
    use_clob: bool = True,
    require_complete: bool = True,
) -> list[ArbOpportunity]:
    """买齐一组互斥且完备的 Yes ask；成本 < 1 才是买侧套利。"""
    part = _partition_markets(markets)
    if len(part) < 2:
        return []
    complete = looks_complete_partition(part)
    if require_complete and not complete:
        return []

    asks: list[tuple[dict, float, float]] = []
    for m in part:
        if use_clob:
            token_ids = [str(x) for x in parse_json_list(m.get("clobTokenIds"))]
            book = fetch_order_book(token_ids[0])
            ask, _ = best_levels(book)
            price, size = _level_price_size(ask)
            if price is None:
                # 占位盘常无卖单；Gamma ask 通常是 1.0
                price = _yes_ask_gamma(m)
                size = 0.0
        else:
            price = _yes_ask_gamma(m)
            size = max(_market_liquidity(m) / 50.0, min_size)
        if price is None:
            return []
        asks.append((m, price, size if use_clob else max(size, min_size)))

    opps = _bundle_from_asks(event, asks, kind, min_edge, min_size)
    if not complete:
        for o in opps:
            o.kind = kind + "_incomplete"
            o.detail += " | WARNING: outcome set may lack Other/catch-all — not guaranteed arb"
    return opps


def neg_risk_buy_arb(
    event: dict,
    markets: list[dict],
    min_edge: float = DEFAULT_MIN_EDGE,
    min_size: float = DEFAULT_MIN_SIZE,
) -> list[ArbOpportunity]:
    if not event.get("negRisk") and not any(m.get("negRisk") for m in markets):
        return []
    return _bundle_yes_buy_arb(
        event, markets, kind="neg_risk_buy", min_edge=min_edge, min_size=min_size
    )


def moneyline_bundle_arb(
    event: dict,
    markets: list[dict],
    min_edge: float = DEFAULT_MIN_EDGE,
    min_size: float = DEFAULT_MIN_SIZE,
) -> list[ArbOpportunity]:
    """足球独赢三选一（主/平/客）等：买齐所有 moneyline Yes。"""
    moneylines = [m for m in markets if m.get("sportsMarketType") == "moneyline"]
    if len(moneylines) < 2:
        return []
    return _bundle_yes_buy_arb(
        event, moneylines, kind="moneyline_buy", min_edge=min_edge, min_size=min_size
    )


_SCORE_RE = re.compile(r"(\d+)\s*[-:]\s*(\d+)")


def _parse_score(text: str) -> tuple[int, int] | None:
    m = _SCORE_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def sports_spread_vs_scores(
    event: dict,
    markets: list[dict],
    min_edge: float = DEFAULT_MIN_EDGE,
) -> list[ArbOpportunity]:
    """若同事件同时有 spreads + exact scores，检验让球盘是否被精确比分蕴含。

    当前 Polymarket more-markets 事件多数只有 spreads/totals，没有 exact-score 网格；
    没有真正的比分子盘时直接跳过，避免把 O/U 0.5 之类误当成比分。
    """
    spreads = [m for m in markets if m.get("sportsMarketType") == "spreads"]
    score_markets = [
        m
        for m in markets
        if m.get("sportsMarketType")
        in {"soccer_exact_score", "exact_score", "correct_score"}
        or "exact score" in ((m.get("question") or "") + (m.get("groupItemTitle") or "")).lower()
        or "correct score" in ((m.get("question") or "") + (m.get("groupItemTitle") or "")).lower()
    ]
    if not spreads or not score_markets:
        return []

    scores = []
    for m in score_markets:
        title = " ".join(
            filter(None, [m.get("groupItemTitle"), m.get("question"), m.get("slug")])
        )
        parsed = _parse_score(title)
        if parsed is None and "any other" not in title.lower():
            continue
        prices = parse_json_list(m.get("outcomePrices"))
        if not prices:
            continue
        yes = _f(prices[0])
        if yes is None:
            continue
        scores.append((title, parsed, yes, m))

    if not scores:
        return []

    # Infer home/away from event title "A vs. B"
    title = event.get("title") or ""
    parts = re.split(r"\s+vs\.?\s+", title, flags=re.I)
    if len(parts) < 2:
        return []
    home = parts[0].replace(" - More Markets", "").strip()
    away = re.sub(r"\s*-\s*More Markets.*$", "", parts[1], flags=re.I).strip()

    out: list[ArbOpportunity] = []
    for sp in spreads:
        line = _f(sp.get("line"))
        group = sp.get("groupItemTitle") or sp.get("question") or ""
        prices = parse_json_list(sp.get("outcomePrices"))
        if line is None or not prices:
            continue
        spread_yes = _f(prices[0])
        if spread_yes is None:
            continue

        team = group.split("(")[0].replace("Spread:", "").strip()
        implied = 0.0
        matched = 0
        for score_title, parsed, yes, _m in scores:
            if parsed is None:
                continue
            home_goals, away_goals = parsed
            margin_home = home_goals - away_goals
            if team.lower().startswith(home.lower()[:4]) or home.lower() in team.lower():
                covers = (margin_home + line) > 0
            elif team.lower().startswith(away.lower()[:4]) or away.lower() in team.lower():
                margin_away = away_goals - home_goals
                covers = (margin_away + line) > 0
            else:
                continue
            if covers:
                implied += yes
                matched += 1

        if matched == 0:
            continue
        gap = spread_yes - implied
        if abs(gap) >= max(min_edge, 0.03):
            out.append(
                ArbOpportunity(
                    kind="sports_consistency",
                    edge=abs(gap),
                    edge_after_fee=abs(gap),
                    cost_or_proceeds=gap,
                    size=0.0,
                    fee_rate=taker_fee_rate(sp),
                    event_title=title,
                    event_slug=event.get("slug") or "",
                    question=sp.get("question") or group,
                    market_slug=sp.get("slug") or "",
                    detail=(
                        f"spread_yes={spread_yes:.4f} vs exact_sum={implied:.4f} "
                        f"(gap={gap:+.4f}, matched_scores={matched}). "
                        "Verify score grid completeness before trading."
                    ),
                )
            )
    return out


def _market_liquidity(market: dict) -> float:
    for key in ("liquidityNum", "liquidityClob", "liquidity"):
        val = _f(market.get(key))
        if val is not None:
            return val
    return 0.0


def binary_gamma_suspect(market: dict, min_edge: float = DEFAULT_MIN_EDGE) -> bool:
    """Gamma 只有 Yes 侧；互补假设下 buy≈1+spread。只有交叉盘才值得立刻复核。"""
    yes_ask = _f(market.get("bestAsk"))
    yes_bid = _f(market.get("bestBid"))
    if yes_ask is None or yes_bid is None:
        return False
    if yes_bid > yes_ask + 1e-9:  # crossed book
        return True
    # 互补估计：No ask ≈ 1 - Yes bid
    buy_est = yes_ask + max(0.0, 1.0 - yes_bid)
    sell_est = yes_bid + max(0.0, 1.0 - yes_ask)
    return (1.0 - buy_est) >= min_edge or (sell_est - 1.0) >= min_edge


def gamma_near_misses(
    events: list[dict],
    min_edge: float = 0.0,
    limit: int = 20,
) -> list[dict]:
    """不打 CLOB，用 Gamma ask 估算最接近套利的 bundle（含负边，方便看市场有多紧）。"""
    rows = []
    for ev in events:
        raw = [m for m in (ev.get("markets") or []) if not m.get("closed")]
        bundles: list[tuple[str, list[dict]]] = []
        if ev.get("negRisk") or any(m.get("negRisk") for m in raw):
            bundles.append(("neg_risk", _partition_markets(raw)))
        mls = [m for m in raw if m.get("sportsMarketType") == "moneyline"]
        if len(mls) >= 2:
            bundles.append(("moneyline", _partition_markets(mls)))
        for kind, ms in bundles:
            if len(ms) < 2:
                continue
            asks = []
            ok = True
            for m in ms:
                a = _yes_ask_gamma(m)
                if a is None:
                    ok = False
                    break
                asks.append(a)
            if not ok:
                continue
            cost = sum(asks)
            rows.append(
                {
                    "kind": kind,
                    "title": ev.get("title"),
                    "slug": ev.get("slug"),
                    "legs": len(asks),
                    "cost": cost,
                    "edge": 1.0 - cost,
                    "complete": looks_complete_partition(ms),
                }
            )
    rows.sort(key=lambda r: (r["complete"], r["edge"]), reverse=True)
    complete_first = [r for r in rows if r["complete"] and r["edge"] >= min_edge]
    if complete_first:
        return complete_first[:limit]
    return rows[:limit]


def scan_event_gamma(
    event: dict,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_liquidity: float = DEFAULT_MIN_LIQUIDITY,
) -> dict:
    """Phase 1: 不打 CLOB，只标记候选。"""
    raw = [m for m in (event.get("markets") or []) if not m.get("closed")]
    tradable = _active_tradable(raw)
    out = {
        "event": event,
        "bundle_candidates": [],  # (kind, markets)
        "binary_candidates": [],
        "sports": sports_spread_vs_scores(event, tradable, min_edge=min_edge),
    }
    if event.get("negRisk") or any(m.get("negRisk") for m in raw):
        part = _partition_markets(raw)
        if screen_bundle_gamma(event, part, "neg_risk_buy", min_edge=min_edge):
            out["bundle_candidates"].append(("neg_risk_buy", part))
    moneylines = [m for m in raw if m.get("sportsMarketType") == "moneyline"]
    if len(moneylines) >= 2:
        part = _partition_markets(moneylines)
        if screen_bundle_gamma(event, part, "moneyline_buy", min_edge=min_edge):
            out["bundle_candidates"].append(("moneyline_buy", part))

    for m in tradable:
        if len(parse_json_list(m.get("clobTokenIds"))) != 2:
            continue
        if _market_liquidity(m) < min_liquidity and not (
            m.get("bestBid") is not None and m.get("bestAsk") is not None
        ):
            continue
        if binary_gamma_suspect(m, min_edge=min_edge):
            out["binary_candidates"].append(m)
    return out


def scan_event(
    event: dict,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_size: float = DEFAULT_MIN_SIZE,
    min_liquidity: float = DEFAULT_MIN_LIQUIDITY,
    check_books: bool = True,
) -> list[ArbOpportunity]:
    markets = [m for m in (event.get("markets") or []) if not m.get("closed")]
    if not markets:
        return []

    found: list[ArbOpportunity] = []
    title = event.get("title") or ""
    slug = event.get("slug") or ""

    if check_books:
        screened = scan_event_gamma(event, min_edge=min_edge, min_liquidity=min_liquidity)
        for kind, ms in screened["bundle_candidates"]:
            found.extend(
                _bundle_yes_buy_arb(
                    event, ms, kind=kind, min_edge=min_edge, min_size=min_size, use_clob=True
                )
            )
        for m in screened["binary_candidates"]:
            found.extend(
                binary_book_arb(
                    m,
                    event_title=title,
                    event_slug=slug,
                    min_edge=min_edge,
                    min_size=min_size,
                )
            )
        found.extend(screened["sports"])
    else:
        found.extend(sports_spread_vs_scores(event, markets, min_edge=min_edge))
    return found


def scan_platform(
    max_events: int = 400,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_size: float = DEFAULT_MIN_SIZE,
    min_liquidity: float = DEFAULT_MIN_LIQUIDITY,
    workers: int = MAX_WORKERS,
    progress: bool = True,
    max_binary_clob: int = 250,
    events: list[dict] | None = None,
) -> list[ArbOpportunity]:
    if events is None:
        events = list(iter_active_events(max_events=max_events))
        if progress:
            print(f"loaded {len(events)} active events", flush=True)

    # Phase 1: gamma screen (CPU/light HTTP already fetched)
    bundle_jobs = []
    binary_jobs = []
    sports_hits: list[ArbOpportunity] = []
    for ev in events:
        screened = scan_event_gamma(ev, min_edge=min_edge, min_liquidity=min_liquidity)
        sports_hits.extend(screened["sports"])
        for kind, ms in screened["bundle_candidates"]:
            bundle_jobs.append((ev, kind, ms))
        for m in screened["binary_candidates"]:
            vol = _f(m.get("volume24hr")) or _f(m.get("volumeNum")) or 0.0
            binary_jobs.append((vol, ev, m))

    binary_jobs.sort(key=lambda x: x[0], reverse=True)
    binary_jobs = binary_jobs[:max_binary_clob]
    if progress:
        print(
            f"phase1: bundle_candidates={len(bundle_jobs)} "
            f"binary_clob={len(binary_jobs)} sports_flags={len(sports_hits)}",
            flush=True,
        )

    results: list[ArbOpportunity] = list(sports_hits)

    def _verify_bundle(job) -> list[ArbOpportunity]:
        ev, kind, ms = job
        try:
            return _bundle_yes_buy_arb(
                ev, ms, kind=kind, min_edge=min_edge, min_size=min_size, use_clob=True
            )
        except Exception as exc:  # noqa: BLE001
            if progress:
                print(f"  ! bundle error {ev.get('slug')}: {exc}", flush=True)
            return []

    def _verify_binary(job) -> list[ArbOpportunity]:
        _vol, ev, m = job
        try:
            return binary_book_arb(
                m,
                event_title=ev.get("title") or "",
                event_slug=ev.get("slug") or "",
                min_edge=min_edge,
                min_size=min_size,
            )
        except Exception as exc:  # noqa: BLE001
            if progress:
                print(f"  ! binary error {m.get('slug')}: {exc}", flush=True)
            return []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_verify_bundle, j) for j in bundle_jobs]
        futs += [pool.submit(_verify_binary, j) for j in binary_jobs]
        done = 0
        total = len(futs)
        for fut in as_completed(futs):
            results.extend(fut.result())
            done += 1
            if progress and (done % 40 == 0 or done == total):
                print(f"phase2 verified {done}/{total}, hits={len(results)}", flush=True)

    results.sort(key=lambda x: x.edge_after_fee, reverse=True)
    return results


def format_report(opps: Iterable[ArbOpportunity], limit: int = 50) -> str:
    rows = list(opps)
    if not rows:
        return "No executable arb found after fee/size filters."
    lines = [f"Found {len(rows)} opportunities (showing top {min(limit, len(rows))}):\n"]
    for i, o in enumerate(rows[:limit], 1):
        lines.append(
            f"{i}. [{o.kind}] edge_after_fee={o.edge_after_fee:.4%} "
            f"raw={o.edge:.4%} size≈{o.size:.1f} fee={o.fee_rate:.4%}"
        )
        lines.append(f"   event: {o.event_title} ({o.event_slug})")
        lines.append(f"   market: {o.question}")
        lines.append(f"   {o.detail}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan Polymarket for arb opportunities")
    parser.add_argument("--max-events", type=int, default=300)
    parser.add_argument("--min-edge", type=float, default=DEFAULT_MIN_EDGE)
    parser.add_argument("--min-size", type=float, default=DEFAULT_MIN_SIZE)
    parser.add_argument("--min-liquidity", type=float, default=DEFAULT_MIN_LIQUIDITY)
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--max-binary-clob", type=int, default=200)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    events = list(iter_active_events(max_events=args.max_events))
    print(f"loaded {len(events)} active events", flush=True)
    near = gamma_near_misses(events, min_edge=-1.0, limit=15)
    print("\nGamma COMPLETE bundle leaderboard (ask-sum edge, before CLOB/fees):", flush=True)
    for i, r in enumerate(near, 1):
        flag = "complete" if r.get("complete") else "INCOMPLETE"
        print(
            f"  {i}. [{r['kind']}/{flag}] edge={r['edge']:+.4f} cost={r['cost']:.4f} "
            f"legs={r['legs']} | {r['title']}",
            flush=True,
        )

    opps = scan_platform(
        max_events=args.max_events,
        min_edge=args.min_edge,
        min_size=args.min_size,
        min_liquidity=args.min_liquidity,
        workers=args.workers,
        max_binary_clob=args.max_binary_clob,
        events=events,
    )
    if args.json:
        print(json.dumps([o.to_dict() for o in opps], ensure_ascii=False, indent=2))
    else:
        print(format_report(opps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
