"""不用密钥，纯看行情。用法: python scripts/fetch_market.py [slug]"""
import sys

sys.path.insert(0, ".")
from polymarket_agent.markets import fetch_active_markets, fetch_market_by_slug


def main():
    if len(sys.argv) > 1:
        market = fetch_market_by_slug(sys.argv[1])
        if not market:
            print("没找到这个 slug 的市场")
            return
        markets = [market]
    else:
        markets = fetch_active_markets(limit=5)

    for m in markets:
        print("-" * 40)
        print("question:", m.get("question"))
        print("slug:", m.get("slug"))
        print("conditionId:", m.get("conditionId"))
        print("clobTokenIds:", m.get("clobTokenIds"))


if __name__ == "__main__":
    main()
