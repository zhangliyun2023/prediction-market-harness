"""紧急快通道下单。默认 dry-run；加 --yes 才真下单（不再交互确认）。

硬顶：BUY 名义金额 ≤ MAX_SPEND_USD（$5），写死不可抬。

用法:
  # 预热客户端（常驻终端里先跑一次）
  .venv/bin/python scripts/fast_trade.py warm

  # 按 slug 买 Yes（市价，最多 5 USDC）
  .venv/bin/python scripts/fast_trade.py buy --slug <market-slug> --amount 5 --yes

  # 按 token_id 卖
  .venv/bin/python scripts/fast_trade.py sell --token <token_id> --size 10 --yes

  # 限价
  .venv/bin/python scripts/fast_trade.py limit --token <token_id> --side BUY --price 0.42 --size 20 --yes
"""
from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, ".")

from polymarket_agent.markets import fetch_market_by_slug, parse_json_list
from polymarket_agent.orders import batch_limit_orders, limit_order, market_buy, market_sell
from polymarket_agent.risk import MAX_SPEND_USD, assert_buy_spend
from polymarket_agent.session import warm


def resolve_token(slug: str | None, token: str | None, outcome: str = "yes") -> str:
    if token:
        return str(token)
    if not slug:
        raise SystemExit("需要 --slug 或 --token")
    market = fetch_market_by_slug(slug)
    if not market:
        raise SystemExit(f"找不到 slug: {slug}")
    tokens = [str(x) for x in parse_json_list(market.get("clobTokenIds"))]
    outcomes = [str(x).lower() for x in parse_json_list(market.get("outcomes"))]
    want = outcome.lower()
    if outcomes and want in outcomes:
        return tokens[outcomes.index(want)]
    if want in {"yes", "y", "0"}:
        return tokens[0]
    if want in {"no", "n", "1"}:
        return tokens[1]
    raise SystemExit(f"无法解析 outcome={outcome}, outcomes={outcomes}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fast Polymarket trade path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_warm = sub.add_parser("warm", help="prebuild authenticated client")
    p_buy = sub.add_parser("buy", help="market buy by USDC amount")
    p_sell = sub.add_parser("sell", help="market sell by shares")
    p_limit = sub.add_parser("limit", help="limit order")

    for p in (p_buy, p_sell, p_limit):
        p.add_argument("--slug")
        p.add_argument("--token")
        p.add_argument("--outcome", default="yes")
        p.add_argument("--yes", action="store_true", help="actually submit (no prompt)")

    p_buy.add_argument(
        "--amount",
        type=float,
        required=True,
        help=f"USDC to spend (hard cap ${MAX_SPEND_USD:.2f})",
    )
    p_sell.add_argument("--size", type=float, required=True, help="shares to sell")
    p_limit.add_argument("--side", required=True, choices=["BUY", "SELL", "buy", "sell"])
    p_limit.add_argument("--price", type=float, required=True)
    p_limit.add_argument("--size", type=float, required=True)
    p_limit.add_argument("--tif", default="GTC", choices=["GTC", "FOK", "FAK"])

    args = parser.parse_args(argv)
    t0 = time.perf_counter()

    if args.cmd == "warm":
        warm()
        print(f"warm total {(time.perf_counter() - t0) * 1000:.0f}ms")
        return 0

    token_id = resolve_token(args.slug, args.token, getattr(args, "outcome", "yes"))
    print(f"token={token_id}")
    print(f"hard_cap=${MAX_SPEND_USD:.2f}")

    if args.cmd == "buy":
        assert_buy_spend(args.amount, label="fast_trade buy")
    elif args.cmd == "limit" and args.side.upper() == "BUY":
        assert_buy_spend(args.price * args.size, label="fast_trade limit BUY")

    if not args.yes:
        print("DRY-RUN. Add --yes to submit.")
        return 0

    warm()
    if args.cmd == "buy":
        out = market_buy(token_id, args.amount)
    elif args.cmd == "sell":
        out = market_sell(token_id, args.size)
    else:
        from py_clob_client_v2 import OrderType

        out = limit_order(
            token_id,
            args.price,
            args.size,
            args.side.upper(),
            order_type=getattr(OrderType, args.tif),
        )

    print(json.dumps(out, default=str, ensure_ascii=False, indent=2))
    print(f"wall_ms={(time.perf_counter() - t0) * 1000:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
