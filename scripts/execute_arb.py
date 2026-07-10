"""执行 neg-risk / moneyline 买齐套利。默认 dry-run，不花钱。

硬顶：单笔 BUY 合计名义金额 ≤ MAX_SPEND_USD（$5），写死不可抬。

用法:
  # 只预览
  .venv/bin/python scripts/execute_arb.py <event-slug>

  # 限制份数（仍受 $5 硬顶约束）
  .venv/bin/python scripts/execute_arb.py <event-slug> --size 5

  # 真下单（需再敲 yes）
  .venv/bin/python scripts/execute_arb.py <event-slug> --size 5 --execute
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, ".")

from polymarket_agent.arbitrage import (
    _level_price_size,
    _partition_markets,
    looks_complete_partition,
    taker_fee_usdc,
)
from polymarket_agent.markets import (
    best_levels,
    fetch_event_by_slug,
    fetch_order_book,
    parse_json_list,
)
from polymarket_agent.risk import MAX_SPEND_USD, assert_buy_spend, max_shares_for_budget
from polymarket_agent.session import warm


def _leg_from_market(m: dict) -> dict | None:
    token_ids = [str(x) for x in parse_json_list(m.get("clobTokenIds"))]
    if not token_ids:
        return None
    book = fetch_order_book(token_ids[0])
    ask, _ = best_levels(book)
    price, size = _level_price_size(ask)
    if price is None:
        price = float(m["bestAsk"]) if m.get("bestAsk") is not None else None
        size = 0.0
    return {
        "title": m.get("groupItemTitle") or m.get("question") or "",
        "condition_id": m.get("conditionId"),
        "token_id": token_ids[0],
        "price": price,
        "size": size,
        "market": m,
    }


def collect_legs(event: dict, moneyline_only: bool = False) -> list[dict]:
    markets = event.get("markets") or []
    if moneyline_only:
        markets = [m for m in markets if m.get("sportsMarketType") == "moneyline"]
    part = _partition_markets(markets)
    legs: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(part)))) as pool:
        futs = [pool.submit(_leg_from_market, m) for m in part]
        for fut in as_completed(futs):
            leg = fut.result()
            if leg:
                legs.append(leg)
    # stable-ish order by title for readability
    legs.sort(key=lambda x: x["title"])
    return legs


MIN_MARKETABLE_BUY_USD = 1.0  # CLOB: marketable BUY notional per leg


def plan_trade(legs: list[dict], max_size: float | None) -> dict:
    if not legs or any(l["price"] is None for l in legs):
        raise RuntimeError("有腿没有 ask，无法买齐")
    book_size = min(l["size"] for l in legs)
    min_price = min(float(l["price"]) for l in legs)
    cost = sum(float(l["price"]) for l in legs)
    # 份额下限：平台 orderMinSize + 市价买单每腿名义金额 >= $1
    share_floor = max(
        float((l.get("market") or {}).get("orderMinSize") or 0) for l in legs
    )
    notional_floor = MIN_MARKETABLE_BUY_USD / min_price if min_price > 0 else float("inf")
    size_floor = max(share_floor, notional_floor)
    # HARD CAP: never plan a complete-set buy above MAX_SPEND_USD
    budget_cap_shares = max_shares_for_budget(cost, MAX_SPEND_USD)
    size = book_size if max_size is None else min(book_size, max_size)
    size = min(size, budget_cap_shares)
    if size < size_floor - 1e-9:
        raise RuntimeError(
            f"无法满足最小名义金额: 最便宜腿 @{min_price} 需要 size>={size_floor:.1f} "
            f"才达到每腿 ${MIN_MARKETABLE_BUY_USD:.0f}，但盘口/预算只允许 {size:.1f} "
            f"(book={book_size:.1f}, budget_cap=${MAX_SPEND_USD:.2f}→{budget_cap_shares:.1f} shares)"
        )
    if size <= 0:
        raise RuntimeError("可成交量为 0")
    # 若用户没限仓，至少抬到可下单下限（仍受 $5 硬顶）
    if max_size is None:
        size = max(size_floor, min(book_size, budget_cap_shares, size))
        size = min(size, budget_cap_shares)
    fee = sum(taker_fee_usdc(l["price"], size, l["market"]) for l in legs)
    edge = 1.0 - cost
    edge_after = edge - (fee / size)
    notional = cost * size
    assert_buy_spend(notional, label="arb plan notional")
    profit = size * edge - fee
    return {
        "size": size,
        "cost": cost,
        "fee": fee,
        "edge": edge,
        "edge_after": edge_after,
        "notional": notional,
        "profit": profit,
        "legs": legs,
        "size_floor": size_floor,
        "min_price": min_price,
        "budget_cap_usd": MAX_SPEND_USD,
    }


def print_plan(event: dict, plan: dict, complete: bool) -> None:
    print(f"event: {event.get('title')}")
    print(f"slug:  {event.get('slug')}")
    print(f"complete_partition: {complete}")
    print(f"legs: {len(plan['legs'])}")
    for leg in plan["legs"]:
        print(
            f"  BUY {plan['size']:.4f} @ {leg['price']:.4f}  "
            f"{leg['title'][:40]:40} (book {leg['size']:.2f})"
        )
    print(
        f"cost/share={plan['cost']:.4f}  edge={plan['edge']:.4%}  "
        f"edge_after_fee={plan['edge_after']:.4%}"
    )
    print(
        f"size={plan['size']:.4f}  notional≈${plan['notional']:.4f}  "
        f"fee≈${plan['fee']:.4f}  expected_profit≈${plan['profit']:.4f}  "
        f"hard_cap=${MAX_SPEND_USD:.2f}"
    )


def execute_plan(plan: dict, order_type: str = "FOK") -> dict:
    from py_clob_client_v2 import OrderType

    from polymarket_agent.orders import batch_limit_orders

    warm()
    ot = getattr(OrderType, order_type.upper())
    legs = [
        {
            "token_id": leg["token_id"],
            "price": float(leg["price"]),
            "size": float(plan["size"]),
            "side": "BUY",
            "title": leg["title"],
        }
        for leg in plan["legs"]
    ]
    t0 = time.perf_counter()
    out = batch_limit_orders(legs, order_type=ot)
    print(
        f"batch posted n={out['n']} sign={out['sign_ms']:.0f}ms "
        f"post={out['post_ms']:.0f}ms total={out['elapsed_ms']:.0f}ms "
        f"wall={(time.perf_counter() - t0) * 1000:.0f}ms"
    )
    print(out["response"])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute complete-set buy arb")
    parser.add_argument("slug", help="event slug")
    parser.add_argument("--size", type=float, default=None, help="max shares per leg")
    parser.add_argument("--moneyline", action="store_true", help="only moneyline legs")
    parser.add_argument("--execute", action="store_true", help="actually place orders")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="with --execute, skip interactive confirm (for urgent path)",
    )
    parser.add_argument("--order-type", default="FOK", choices=["FOK", "FAK", "GTC"])
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.002,
        help="abort if edge_after_fee below this",
    )
    args = parser.parse_args(argv)

    t0 = time.perf_counter()
    event = fetch_event_by_slug(args.slug)
    if not event:
        print("event not found")
        return 1

    legs = collect_legs(event, moneyline_only=args.moneyline)
    complete = looks_complete_partition([l["market"] for l in legs])
    plan = plan_trade(legs, args.size)
    print_plan(event, plan, complete)
    print(f"plan_ms={(time.perf_counter() - t0) * 1000:.0f}")

    if not complete:
        print("\nABORT: outcome set does not look complete (missing Other/catch-all).")
        return 2
    if plan["edge_after"] < args.min_edge:
        print(f"\nABORT: edge_after_fee {plan['edge_after']:.4%} < min {args.min_edge:.4%}")
        return 3

    if not args.execute:
        print("\nDRY-RUN only. Re-run with --execute [--yes] to spend money.")
        return 0

    assert_buy_spend(plan["notional"], label="execute_arb")
    print(
        f"\n即将真实下单: {len(plan['legs'])} 腿 × {plan['size']:.4f} 份, "
        f"大约花费 ${plan['notional']:.4f} (hard_cap=${MAX_SPEND_USD:.2f})"
    )
    if not args.yes:
        confirm = input("确认真实下单？输入 yes 继续: ")
        if confirm.strip().lower() != "yes":
            print("已取消，没有下单。")
            return 0

    execute_plan(plan, order_type=args.order_type)
    print(f"wall_ms={(time.perf_counter() - t0) * 1000:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
