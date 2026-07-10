"""持续盯盘：排除气温盘，只报可真实下单的套利（满足 $1 最低名义金额）。

硬顶：单笔 BUY ≤ MAX_SPEND_USD（$5）。

用法:
  .venv/bin/python scripts/watch_arb.py
  .venv/bin/python scripts/watch_arb.py --interval 60 --min-profit 0.3
  # 发现可执行机会就真下单（仍受 $5 硬顶）
  .venv/bin/python scripts/watch_arb.py --execute --yes --interval 60
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, ".")

from polymarket_agent.arbitrage import (
    _partition_markets,
    _level_price_size,
    _yes_ask_gamma,
    looks_complete_partition,
    taker_fee_usdc,
)
from polymarket_agent.markets import (
    best_levels,
    fetch_order_book,
    iter_active_events,
    parse_json_list,
)
from polymarket_agent.risk import MAX_SPEND_USD, max_shares_for_budget

MIN_NOTIONAL = 1.0
TEMP_KW = ("temperature", "highest temperature", "lowest temperature")


def is_temp(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in TEMP_KW)


def analyze(ev: dict, markets: list[dict], kind: str) -> dict | None:
    part = _partition_markets(markets)
    if len(part) < 2 or not looks_complete_partition(part):
        return None
    cost_g = 0.0
    for m in part:
        a = _yes_ask_gamma(m)
        if a is None:
            return None
        cost_g += a
    if cost_g > 0.998:
        return None

    def one(m):
        tids = parse_json_list(m.get("clobTokenIds"))
        book = fetch_order_book(str(tids[0]))
        ask, _ = best_levels(book)
        p, s = _level_price_size(ask)
        return m, p, s

    asks = list(ThreadPoolExecutor(min(12, len(part))).map(one, part))
    if any(p is None or p <= 0 for _, p, _ in asks):
        return None
    cost = sum(p for _, p, _ in asks)
    if cost >= 0.999:
        return None
    book = min(s for _, _, s in asks)
    min_p = min(p for _, p, _ in asks)
    share_floor = max(float(m.get("orderMinSize") or 5) for m, _, _ in asks)
    need = max(share_floor, MIN_NOTIONAL / min_p)
    fee1 = sum(taker_fee_usdc(p, 1, m) for m, p, _ in asks)
    edge_net = (1 - cost) - fee1
    if edge_net <= 0.002:
        return None
    executable = need <= book + 1e-9
    budget_cap = max_shares_for_budget(cost, MAX_SPEND_USD)
    size = 0.0
    if executable:
        # Prefer min executable size; never exceed $5 hard spend cap
        size = min(book, budget_cap)
        if size < need - 1e-9:
            # Book ok for min notional, but $5 cap can't fund the floor → paper only
            executable = False
            size = 0.0
        else:
            # Use the smaller of book and budget; at least the floor
            size = max(need, min(book, budget_cap))
            size = min(size, book, budget_cap)
    fee = sum(taker_fee_usdc(p, size, m) for m, p, _ in asks) if size else 0.0
    profit = size * (1 - cost) - fee if size else 0.0
    notional = size * cost if size else 0.0
    return {
        "kind": kind,
        "title": ev.get("title"),
        "slug": ev.get("slug"),
        "legs": len(asks),
        "cost": cost,
        "edge_net": edge_net,
        "min_p": min_p,
        "need": need,
        "book": book,
        "executable": executable,
        "size": size,
        "notional": notional,
        "profit": profit,
        "budget_cap_usd": MAX_SPEND_USD,
    }


def scan_once(max_events: int = 2100) -> tuple[list[dict], list[dict]]:
    exec_hits, paper_hits = [], []
    for ev in iter_active_events(max_events=max_events):
        title = ev.get("title") or ""
        if is_temp(title):
            continue
        markets = [m for m in (ev.get("markets") or []) if not m.get("closed")]
        cands = []
        if ev.get("negRisk") or any(m.get("negRisk") for m in markets):
            cands.append(("neg_risk", markets))
        mls = [m for m in markets if m.get("sportsMarketType") == "moneyline"]
        if len(mls) >= 2:
            cands.append(("moneyline", mls))
        for kind, ms in cands:
            try:
                r = analyze(ev, ms, kind)
            except Exception:
                continue
            if not r:
                continue
            (exec_hits if r["executable"] else paper_hits).append(r)
    exec_hits.sort(key=lambda x: x["profit"], reverse=True)
    paper_hits.sort(key=lambda x: x["edge_net"], reverse=True)
    return exec_hits, paper_hits


def try_execute(hit: dict, *, min_edge: float = 0.002) -> bool:
    """Re-plan from live books and FOK-buy the complete set under $5 cap.

    Returns True if an order batch was submitted (fill not guaranteed).
    """
    from scripts.execute_arb import collect_legs, execute_plan, plan_trade, print_plan
    from polymarket_agent.markets import fetch_event_by_slug
    from polymarket_agent.risk import assert_buy_spend
    from polymarket_agent.session import warm

    slug = hit["slug"]
    moneyline_only = hit["kind"] == "moneyline"
    print(
        f"  >> EXECUTE {slug} kind={hit['kind']} "
        f"scan_notional=${hit['notional']:.2f} hard_cap=${MAX_SPEND_USD:.2f}",
        flush=True,
    )
    warm()
    event = fetch_event_by_slug(slug)
    if not event:
        print(f"  !! abort: event not found {slug}", flush=True)
        return False
    legs = collect_legs(event, moneyline_only=moneyline_only)
    complete = looks_complete_partition([l["market"] for l in legs])
    if not complete:
        print("  !! abort: incomplete partition on re-check", flush=True)
        return False
    try:
        plan = plan_trade(legs, max_size=None)
    except Exception as exc:  # noqa: BLE001
        print(f"  !! abort plan: {exc}", flush=True)
        return False
    print_plan(event, plan, complete)
    if plan["edge_after"] < min_edge:
        print(
            f"  !! abort: edge_after_fee {plan['edge_after']:.4%} < {min_edge:.4%}",
            flush=True,
        )
        return False
    if plan["notional"] > MAX_SPEND_USD + 1e-9:
        print(
            f"  !! abort: notional ${plan['notional']:.4f} > cap ${MAX_SPEND_USD:.2f}",
            flush=True,
        )
        return False
    assert_buy_spend(plan["notional"], label=f"watch_arb {slug}")
    out = execute_plan(plan, order_type="FOK")
    print(f"  << done response={out.get('response')}", flush=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=90, help="seconds between scans")
    parser.add_argument(
        "--max-events",
        type=int,
        default=2100,
        help="Gamma /events pagination ceiling is ~2100 (offset>=2100 → 422)",
    )
    parser.add_argument("--min-profit", type=float, default=0.2)
    parser.add_argument("--min-edge", type=float, default=0.002)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually place orders on executable hits (hard cap $5)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="with --execute, skip confirm (required for unattended auto-trade)",
    )
    parser.add_argument(
        "--max-trades",
        type=int,
        default=1,
        help="max live trades per scan cycle (default 1)",
    )
    args = parser.parse_args(argv)

    if args.execute and not args.yes:
        print("ABORT: --execute requires --yes for unattended trading.", flush=True)
        return 2

    print(
        f"watch_arb start execute={args.execute} hard_cap=${MAX_SPEND_USD:.2f} "
        f"min_profit=${args.min_profit} interval={args.interval}s",
        flush=True,
    )

    seen: set[str] = set()
    traded: set[str] = set()
    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{ts}] scanning non-temp arbs...", flush=True)
        try:
            exec_hits, paper_hits = scan_once(max_events=args.max_events)
        except Exception as exc:  # noqa: BLE001
            print(f"scan error: {exc}", flush=True)
            if args.once:
                return 1
            time.sleep(args.interval)
            continue

        good = [r for r in exec_hits if r["profit"] >= args.min_profit]
        print(
            f"executable>={args.min_profit}: {len(good)} | "
            f"paper-only edges: {len(paper_hits)} | hard_cap=${MAX_SPEND_USD:.2f}",
            flush=True,
        )
        for r in good[:8]:
            key = f"{r['slug']}:{r['kind']}"
            mark = "NEW" if key not in seen else "still"
            seen.add(key)
            print(
                f"  [{mark}] ${r['profit']:.2f} | size={r['size']:.1f} "
                f"notional=${r['notional']:.2f} edge={r['edge_net']:.2%} | "
                f"{r['title']} ({r['slug']})",
                flush=True,
            )

        if args.execute and good:
            placed = 0
            for r in good:
                if placed >= args.max_trades:
                    break
                key = f"{r['slug']}:{r['kind']}"
                if key in traded:
                    print(f"  skip already-traded {key}", flush=True)
                    continue
                try:
                    ok = try_execute(r, min_edge=args.min_edge)
                except Exception as exc:  # noqa: BLE001
                    print(f"  !! execute error {key}: {exc}", flush=True)
                    ok = False
                if ok:
                    traded.add(key)
                    placed += 1

        if paper_hits[:5]:
            print("  paper (not executable yet):", flush=True)
            for r in paper_hits[:5]:
                print(
                    f"    edge={r['edge_net']:.2%} need={r['need']:.0f} "
                    f"book={r['book']:.1f} | {r['title']}",
                    flush=True,
                )
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
